"""Reconcile history, orchestrate live cycles, and expose content-free health."""

import os
import signal
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from codex_tokenomics.collector import CollectionResult, Collector
from codex_tokenomics.config import AppConfig
from codex_tokenomics.detector import DetectionEngine, IncidentTransition, TokenBreakdown
from codex_tokenomics.notifiers import Clock, NotificationDispatcher
from codex_tokenomics.storage import TelemetryStore


@dataclass(frozen=True, slots=True)
class LiveBoundary:
    started_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceCycle:
    collection: CollectionResult
    transitions: tuple[IncidentTransition, ...]

    @property
    def opened_incidents(self) -> int:
        return sum(item.state == "opened" for item in self.transitions)

    @property
    def recovered_incidents(self) -> int:
        return sum(item.state == "recovered" for item in self.transitions)


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    last_poll_at: str | None
    last_success_at: str | None
    lag_seconds: float | None
    backlog_bytes: int | None
    files_seen: int | None
    parse_failures: int | None
    notification_failures: int | None
    integrity_check: Literal["ok", "failed", "unavailable"]
    service_version: str
    status: Literal["ok", "degraded"]


class ServiceError(RuntimeError):
    """A fixed stage code suitable for reporting without the underlying exception."""


class MonitorService:
    def __init__(
        self, config: AppConfig, store: TelemetryStore, collector: Collector,
        detector: DetectionEngine, dispatcher: NotificationDispatcher, clock: Clock,
    ) -> None:
        self.config = config
        self.store = store
        self.collector = collector
        self.detector = detector
        self.dispatcher = dispatcher
        self.clock = clock
        self.live_boundary: LiveBoundary | None = None
        self._live_usage_rowid = 0
        self._last_cycle_failed = False
        self._stop_requested = False

    def reconcile(self) -> LiveBoundary:
        """Finish the historical scan without invoking detection or dispatch."""
        if self.live_boundary is None:
            self._cycle(None)
            self.live_boundary = LiveBoundary(self._now())
        return self.live_boundary

    def run_once(self) -> ServiceCycle:
        boundary = self.reconcile()
        return self._cycle(boundary)

    def stop(self) -> None:
        """Request shutdown after the current cycle has finished committing."""
        self._stop_requested = True

    def run_forever(self) -> None:
        """Run on the main thread, restore signal handlers, and close the owned store."""
        previous_handlers = {}

        def request_stop(_signum: int, _frame: object) -> None:
            self.stop()

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, request_stop)
            if not self._stop_requested:
                self.reconcile()
            while not self._stop_requested:
                self.run_once()
                if not self._stop_requested:
                    self.clock.sleep(self.config.collector.poll_interval_seconds)
        except KeyboardInterrupt:
            self.stop()
        finally:
            try:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
            finally:
                self.store.close()

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("service timestamps require a timezone")
        return now.astimezone(UTC)

    def _heartbeat(self, timestamp: str, success_at: str | None, files_seen: int | None) -> None:
        # Preserve the schema/storage interface; only named health fields cross this boundary.
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE service_health SET last_poll_at=?, last_success_at=?, "
                "files_seen=COALESCE(?, files_seen) WHERE singleton=1",
                (timestamp, success_at, files_seen),
            )

    def _cycle(self, boundary: LiveBoundary | None) -> ServiceCycle:
        timestamp = self._now().isoformat()
        try:
            previous_success = self.store.connection.execute(
                "SELECT last_success_at FROM service_health WHERE singleton=1",
            ).fetchone()[0]
            self._heartbeat(timestamp, previous_success, None)
        except sqlite3.Error:
            self._last_cycle_failed = True
            raise ServiceError("heartbeat_failed") from None

        stage = "collection_failed"
        try:
            collection = self.collector.scan_once()
            transitions = ()
            if boundary is None:
                # A process-lifetime ingestion cohort, independent of event timestamps.
                self._live_usage_rowid = self.store.connection.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM usage_samples",
                ).fetchone()[0]
            else:
                stage = "detection_failed"
                pending = self._pending_open_transitions()
                detected = self.detector.evaluate(
                    self._now(), boundary.started_at, live_after_rowid=self._live_usage_rowid,
                )
                transitions = tuple(
                    item for item in (*pending, *detected) if self.detector.alert_is_allowed(item)
                )
                stage = "dispatch_failed"
                for transition in transitions:
                    self.dispatcher.dispatch(transition)
            stage = "heartbeat_failed"
            timestamp = self._now().isoformat()
            self._heartbeat(timestamp, timestamp, collection.files_seen)
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            self._last_cycle_failed = True
            try:
                # Collection updates per-file success. Restore the last completed service cycle
                # when a later file, detection, dispatch, or heartbeat fails.
                self._heartbeat(timestamp, previous_success, None)
            except sqlite3.Error:
                pass
            raise ServiceError(stage) from None
        self._last_cycle_failed = False
        return ServiceCycle(collection, transitions)

    def _pending_open_transitions(self) -> tuple[IncidentTransition, ...]:
        rows = self.store.connection.execute(
            "SELECT incident_id, scope_type, scope_id, trigger, observed_rate, baseline_rate, "
            "absolute_threshold, opened_at FROM alert_incidents "
            "WHERE recovered_at IS NULL ORDER BY opened_at, incident_id"
        )
        transitions: list[IncidentTransition] = []
        retry_budget = len(self.config.notifications.email_retry_delays_seconds) + 1
        for row in rows:
            attempts = tuple(self.store.connection.execute(
                "SELECT channel, attempt_number, outcome_code, attempted_at "
                "FROM notification_attempts WHERE incident_id=? "
                "ORDER BY attempt_number", (row["incident_id"],),
            ))
            desktop_opened = any(
                item["channel"] == "desktop" and item["attempt_number"] == 1
                for item in attempts
            )
            email_done = any(
                item["channel"] == "email" and item["outcome_code"] in {
                    "sent", "dry_run", "skipped",
                }
                for item in attempts
            )
            email_attempts = [
                item for item in attempts
                if item["channel"] == "email"
                and item["outcome_code"] in {"failed", "unavailable", "timeout"}
            ]
            email_pending = (
                not email_done
                and self._email_attempt_due(email_attempts, retry_budget)
            )
            if desktop_opened and not email_pending:
                continue
            transitions.append(IncidentTransition(
                incident_id=row["incident_id"], scope_type=row["scope_type"],
                scope_id=row["scope_id"], state="opened", trigger=row["trigger"],
                observed_rate=row["observed_rate"], baseline_rate=row["baseline_rate"],
                absolute_threshold=row["absolute_threshold"],
                opened_at=datetime.fromisoformat(row["opened_at"]), recovered_at=None,
                token_breakdown=TokenBreakdown(0, 0, 0, 0, 0, 0),
            ))
        return tuple(transitions)

    def _email_attempt_due(self, attempts: list[sqlite3.Row], retry_budget: int) -> bool:
        if not attempts:
            return True
        last = max(attempts, key=lambda item: item["attempt_number"])
        if last["attempt_number"] >= retry_budget:
            return False
        delay = self.config.notifications.email_retry_delays_seconds[last["attempt_number"] - 1]
        return self._now() >= datetime.fromisoformat(last["attempted_at"]) + timedelta(seconds=delay)

    def _backlog_bytes(self) -> int:
        root = self.config.paths.session_root
        try:
            root.stat()
        except FileNotFoundError:
            return 0

        def unreadable(error: OSError) -> None:
            raise error

        backlog = 0
        for directory, _, filenames in os.walk(root, onerror=unreadable):
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                path = Path(directory) / filename
                try:
                    identity = path.stat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(identity.st_mode):
                    continue
                cursor = self.store.get_cursor(str(path))
                offset = 0
                if cursor is not None and (
                    cursor.device == identity.st_dev and cursor.inode == identity.st_ino
                    and identity.st_size >= cursor.offset
                ):
                    offset = cursor.offset
                backlog += identity.st_size - offset
        return backlog

    def health(self) -> HealthSnapshot:
        """Return allowlisted counts/status; inaccessible inputs are explicitly unknown."""
        try:
            snapshot = self.store.health_snapshot()
        except sqlite3.Error:
            return HealthSnapshot(None, None, None, None, None, None, None, "unavailable",
                                  version("codex-tokenomics"), "degraded")
        try:
            backlog = self._backlog_bytes()
        except (OSError, sqlite3.Error):
            backlog = None
        last_poll = snapshot["last_poll_at"]
        last_success = snapshot["last_success_at"]
        lag = None if last_success is None else max(
            0.0, (self._now() - datetime.fromisoformat(last_success)).total_seconds(),
        )
        degraded = (
            self._last_cycle_failed or last_success is None or backlog is None
            or snapshot["integrity_check"] != "ok"
            or (last_poll is not None and last_poll > last_success)
        )
        return HealthSnapshot(
            last_poll, last_success, lag, backlog, snapshot["files_seen"],
            snapshot["parse_failures"], snapshot["notification_failures"],
            snapshot["integrity_check"], snapshot["service_version"],
            "degraded" if degraded else "ok",
        )
