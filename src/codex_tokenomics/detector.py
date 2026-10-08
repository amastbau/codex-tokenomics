"""Configured UTC token-rate detection with persisted incident identities."""

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Literal
from uuid import uuid4

from codex_tokenomics.config import DetectorConfig
from codex_tokenomics.storage import TelemetryStore
from codex_tokenomics.telemetry import UsageSample

type _TimedSample = tuple[datetime, UsageSample]


@dataclass(frozen=True, slots=True)
class IncidentTransition:
    incident_id: str
    scope_type: Literal["session", "aggregate"]
    scope_id: str
    state: Literal["opened", "recovered"]
    trigger: Literal["absolute", "relative", "absolute+relative"]
    observed_rate: float
    baseline_rate: float | None
    absolute_threshold: float
    opened_at: datetime
    recovered_at: datetime | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("detector timestamps require a timezone")
    return value.astimezone(UTC)


class DetectionEngine:
    def __init__(self, store: TelemetryStore, config: DetectorConfig) -> None:
        self.store = store
        self.config = config

    def evaluate(self, now: datetime, live_after: datetime) -> tuple[IncidentTransition, ...]:
        """Emit only new persisted transitions; startup history informs baseline alone."""
        now, live_after = _utc(now), _utc(live_after)
        active = self._active_incidents()
        completed = self._latest_recoveries()
        window_start = now - timedelta(seconds=self.config.rate_window_seconds)
        history_start = min(
            window_start, now - timedelta(seconds=self.config.baseline_window_seconds),
        )
        # A persisted quiet period may span many polls or a restart. Read enough
        # event-time history to verify every intervening rate/baseline change.
        for incident in active.values():
            if incident["below_since"] is not None:
                since = datetime.fromisoformat(incident["below_since"])
                history_start = min(
                    history_start,
                    since - timedelta(seconds=self.config.baseline_window_seconds),
                    since - timedelta(seconds=self.config.rate_window_seconds),
                )
        samples = tuple(
            (_utc(datetime.fromisoformat(item.timestamp)), item)
            for item in self.store.usage_samples(history_start, now)
        )
        first_observations = self._first_observations(now)
        session_ids = {
            item.session_id for timestamp, item in samples
            if timestamp >= window_start and timestamp > live_after
        }
        session_ids.update(scope_id for scope_type, scope_id in active if scope_type == "session")
        scopes = [("session", session_id) for session_id in sorted(session_ids)]
        scopes.append(("aggregate", "all"))
        transitions = []
        for scope_type, scope_id in scopes:
            key = (scope_type, scope_id)
            if key in completed and now <= completed[key]:
                continue
            scoped_samples = samples if scope_type == "aggregate" else tuple(
                item for item in samples if item[1].session_id == scope_id
            )
            first = min(first_observations.values(), default=None) if (
                scope_type == "aggregate"
            ) else first_observations.get(scope_id)
            observed_rate = self._observed_rate(scoped_samples, now, live_after)
            baseline_rate = self._baseline_rate(scoped_samples, now, first)
            absolute_threshold = self._absolute_threshold(scope_type)
            trigger = self._trigger(observed_rate, baseline_rate, absolute_threshold)
            if key in active:
                recovered = self._recover_transition(
                    active[key], scoped_samples, now, live_after, first,
                    observed_rate, baseline_rate, absolute_threshold, trigger,
                )
                if recovered is not None:
                    transitions.append(recovered)
                continue
            if trigger is None:
                continue
            incident_id = uuid4().hex
            if self.store.open_incident(
                incident_id=incident_id, scope_type=scope_type, scope_id=scope_id,
                trigger=trigger, observed_rate=observed_rate, baseline_rate=baseline_rate,
                absolute_threshold=absolute_threshold, opened_at=now,
            ):
                transitions.append(IncidentTransition(
                    incident_id=incident_id, scope_type=scope_type, scope_id=scope_id,
                    state="opened", trigger=trigger, observed_rate=observed_rate,
                    baseline_rate=baseline_rate, absolute_threshold=absolute_threshold,
                    opened_at=now, recovered_at=None,
                ))
        return tuple(transitions)

    def _first_observations(self, now: datetime) -> dict[str, datetime]:
        rows = self.store.connection.execute(
            "SELECT session_id, MIN(observed_at) AS first_at FROM usage_samples "
            "WHERE observed_at<=? GROUP BY session_id", (now.isoformat(),),
        )
        return {row["session_id"]: datetime.fromisoformat(row["first_at"]) for row in rows}

    def _active_incidents(self) -> dict[tuple[str, str], sqlite3.Row]:
        rows = self.store.connection.execute(
            "SELECT incident_id, scope_type, scope_id, trigger, observed_rate, baseline_rate, "
            "absolute_threshold, opened_at, below_since, recovered_at FROM alert_incidents "
            "WHERE recovered_at IS NULL ORDER BY opened_at, incident_id"
        )
        return {(row["scope_type"], row["scope_id"]): row for row in rows}

    def _latest_recoveries(self) -> dict[tuple[str, str], datetime]:
        rows = self.store.connection.execute(
            "SELECT scope_type, scope_id, MAX(recovered_at) AS recovered_at "
            "FROM alert_incidents WHERE recovered_at IS NOT NULL GROUP BY scope_type, scope_id"
        )
        return {
            (row["scope_type"], row["scope_id"]): datetime.fromisoformat(row["recovered_at"])
            for row in rows
        }

    def _set_below_since(self, incident_id: str, below_since: datetime | None) -> None:
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE alert_incidents SET below_since=? "
                "WHERE incident_id=? AND recovered_at IS NULL",
                (below_since.isoformat() if below_since is not None else None, incident_id),
            )

    def _recover_transition(
        self, incident: sqlite3.Row, samples: tuple[_TimedSample, ...], now: datetime,
        live_after: datetime, first: datetime | None, observed_rate: float,
        baseline_rate: float | None, absolute_threshold: float, trigger: str | None,
    ) -> IncidentTransition | None:
        opened_at = datetime.fromisoformat(incident["opened_at"])
        below_since = datetime.fromisoformat(incident["below_since"]) if (
            incident["below_since"] is not None
        ) else None
        if now < opened_at or (below_since is not None and now < below_since):
            return None
        if trigger is not None:
            if below_since is not None:
                self._set_below_since(incident["incident_id"], None)
            return None
        # A new live boundary excludes earlier usage, so that interval cannot
        # prove continuous quiet progress carried over from a previous run.
        if below_since is not None and below_since < live_after:
            below_since = live_after
            self._set_below_since(incident["incident_id"], below_since)
        if below_since is None or self._intervening_spike(
            samples, below_since, now, live_after, first, absolute_threshold,
        ):
            self._set_below_since(incident["incident_id"], now)
            return None
        if (now - below_since).total_seconds() < self.config.recovery_seconds:
            return None
        if not self.store.recover_incident(incident["incident_id"], now):
            return None
        return IncidentTransition(
            incident_id=incident["incident_id"], scope_type=incident["scope_type"],
            scope_id=incident["scope_id"], state="recovered", trigger=incident["trigger"],
            observed_rate=observed_rate, baseline_rate=baseline_rate,
            absolute_threshold=absolute_threshold, opened_at=opened_at, recovered_at=now,
        )

    def _intervening_spike(
        self, samples: tuple[_TimedSample, ...], since: datetime, now: datetime,
        live_after: datetime, first: datetime | None, absolute_threshold: float,
    ) -> bool:
        # Rates can rise at live sample arrivals. Relative thresholds can fall
        # when a completed bucket enters or an old bucket leaves the baseline.
        candidates = {since}
        candidates.update(
            timestamp for timestamp, _ in samples if since < timestamp <= now and timestamp > live_after
        )
        width = self.config.rate_window_seconds
        for offset in (0, self.config.baseline_window_seconds):
            lower = math.ceil((since.timestamp() - offset) / width)
            upper = math.floor((now.timestamp() - offset) / width)
            for bucket in range(lower, upper + 1):
                boundary = datetime.fromtimestamp(bucket * width + offset, UTC)
                if offset:
                    # datetime/SQLite timestamps have microsecond precision;
                    # an inclusive lower bound excludes its bucket immediately after it.
                    boundary += timedelta(microseconds=1)
                if since < boundary <= now:
                    candidates.add(boundary)
        return any(
            self._trigger(
                self._observed_rate(samples, candidate, live_after),
                self._baseline_rate(samples, candidate, first), absolute_threshold,
            ) is not None
            for candidate in sorted(candidates)
        )

    def _absolute_threshold(self, scope_type: str) -> float:
        return float(self.config.aggregate_absolute_tokens_per_minute if scope_type == "aggregate"
                     else self.config.session_absolute_tokens_per_minute)

    def _observed_rate(
        self, samples: tuple[_TimedSample, ...], now: datetime, live_after: datetime,
    ) -> float:
        start = now - timedelta(seconds=self.config.rate_window_seconds)
        tokens = sum(
            item.total_tokens for timestamp, item in samples
            if start <= timestamp <= now and timestamp > live_after
        )
        return tokens * 60.0 / self.config.rate_window_seconds

    def _baseline_rate(
        self, samples: tuple[_TimedSample, ...], now: datetime, first: datetime | None,
    ) -> float | None:
        if first is None:
            return None
        width = self.config.rate_window_seconds
        lower = now - timedelta(seconds=self.config.baseline_window_seconds)
        current_start = now - timedelta(seconds=width)
        first_bucket = max(math.floor(first.timestamp() / width), math.ceil(lower.timestamp() / width))
        stop_bucket = math.floor(current_start.timestamp() / width)
        if stop_bucket - first_bucket < self.config.minimum_baseline_buckets:
            return None
        totals = dict.fromkeys(range(first_bucket, stop_bucket), 0)
        for timestamp, item in samples:
            bucket = math.floor(timestamp.timestamp() / width)
            if bucket in totals:
                totals[bucket] += item.total_tokens
        return float(median(tokens * 60.0 / width for tokens in totals.values()))

    def _trigger(
        self, current: float, baseline: float | None, absolute_threshold: float,
    ) -> Literal["absolute", "relative", "absolute+relative"] | None:
        absolute = current >= absolute_threshold
        relative = baseline is not None and (
            current >= self.config.relative_minimum_tokens_per_minute
            and current >= baseline * self.config.relative_multiplier
        )
        if absolute and relative:
            return "absolute+relative"
        if absolute:
            return "absolute"
        if relative:
            return "relative"
        return None
