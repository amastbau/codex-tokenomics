"""Reconcile history, orchestrate live cycles, and expose content-free health."""

import os
import signal
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from codex_tokenomics.collector import CollectionResult, Collector
from codex_tokenomics.config import AppConfig
from codex_tokenomics.detector import DetectionEngine, IncidentTransition
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
            if boundary is not None:
                stage = "detection_failed"
                transitions = self.detector.evaluate(self._now(), boundary.started_at)
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
