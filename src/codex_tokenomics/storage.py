"""Transactional persistence of normalized, content-free telemetry.

No source mappings, event JSON, tool arguments, outputs, or exception text cross
this boundary. Column values are selected explicitly from normalized record types.
"""

import hashlib
import json
import math
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Self

from codex_tokenomics.telemetry import (
    RateLimitSample,
    SessionRecord,
    TelemetryRecord,
    ToolEvent,
    TurnRecord,
    UsageSample,
)

SCHEMA_VERSION = 2
TABLE_NAMES = (
    "sessions", "turns", "responses", "usage_samples", "rate_limit_samples", "tool_events",
    "alert_incidents", "notification_attempts", "ingest_cursors", "service_health",
    "schema_migrations",
)
NOTIFICATION_OUTCOMES = frozenset({"sent", "dry_run", "failed", "unavailable", "timeout", "skipped"})


@dataclass(frozen=True, slots=True)
class IngestCursor:
    file_key: str
    source_path: str | Path
    device: int
    inode: int
    offset: int
    session_id: str | None = None
    parse_failures: int = 0
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    inserted: int
    duplicates: int


def _utc_timestamp(value: str | datetime) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("telemetry timestamps require a timezone")
    return parsed.astimezone(UTC).isoformat()


def _epoch_timestamp(value: int | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _workspace_roots(value: tuple[str, ...]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class TelemetryStore:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> "TelemetryStore":
        """Open a private database and apply migrations under an exclusive transaction."""
        path = Path(path)
        missing_parents = []
        directory = path.parent
        while not directory.exists():
            missing_parents.append(directory)
            directory = directory.parent
        for directory in reversed(missing_parents):
            directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        if os.name == "posix":
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                try:
                    sidecar.chmod(0o600)
                except FileNotFoundError:
                    pass
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        store = cls(path, connection)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            store._migrate()
        except BaseException:
            connection.close()
            raise
        return store

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.execute("BEGIN EXCLUSIVE")
        try:
            exists = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            schema_version = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0] if exists else 0
            if schema_version > SCHEMA_VERSION:
                raise RuntimeError("database has a newer schema than this service")
            if schema_version == 0:
                schema = Path(__file__).with_name("schema.sql").read_text()
                # execute() preserves the transaction; executescript() would commit it first.
                for statement in schema.split(";"):
                    if statement.strip():
                        self.connection.execute(statement)
                self.connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, _now()),
                )
                self.connection.execute(
                    "INSERT INTO service_health (singleton, service_version) VALUES (1, ?)",
                    (version("codex-tokenomics"),),
                )
                schema_version = SCHEMA_VERSION
            if schema_version < 2:
                self._migrate_turn_identity_to_session_scope()
                self.connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (2, _now()),
                )
            self.connection.execute(
                "UPDATE service_health SET service_version=? WHERE singleton=1",
                (version("codex-tokenomics"),),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _migrate_turn_identity_to_session_scope(self) -> None:
        self.connection.execute("ALTER TABLE turns RENAME TO turns_v1")
        self.connection.execute("""
            CREATE TABLE turns (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                turn_id TEXT NOT NULL,
                thread_id TEXT,
                root_turn_id TEXT,
                model TEXT,
                model_provider TEXT,
                reasoning_effort TEXT,
                collaboration_mode TEXT,
                sandbox_mode TEXT,
                approval_mode TEXT,
                cwd TEXT,
                workspace_roots TEXT NOT NULL DEFAULT '[]',
                context_window INTEGER,
                status TEXT,
                observed_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                duration_ms INTEGER,
                time_to_first_token_ms INTEGER,
                PRIMARY KEY (session_id, turn_id)
            )
        """)
        self.connection.execute("""
            INSERT INTO turns (
                session_id, turn_id, thread_id, root_turn_id, model, model_provider,
                reasoning_effort, collaboration_mode, sandbox_mode, approval_mode, cwd,
                workspace_roots, context_window, status, observed_at, started_at, completed_at,
                duration_ms, time_to_first_token_ms
            )
            SELECT
                session_id, turn_id, thread_id, root_turn_id, model, model_provider,
                reasoning_effort, collaboration_mode, sandbox_mode, approval_mode, cwd,
                workspace_roots, context_window, status, observed_at, started_at, completed_at,
                duration_ms, time_to_first_token_ms
            FROM turns_v1
        """)
        self.connection.execute("DROP TABLE turns_v1")

    def ingest(self, records: Sequence[TelemetryRecord], cursor: IngestCursor) -> IngestResult:
        """Commit typed records and the last complete-line cursor together.

        Counts refer to incoming records, excluding automatically created sessions
        and response identities. Metadata/lifecycle merges count as duplicates when
        their identity already exists, even when they enrich the retained row.
        """
        inserted = duplicates = 0
        with self.connection:
            for record in records:
                if not isinstance(record, (
                    SessionRecord, TurnRecord, UsageSample, RateLimitSample, ToolEvent,
                )):
                    raise TypeError("ingest accepts only normalized telemetry records")
                timestamp = _utc_timestamp(record.timestamp)
                is_new_session = self._ensure_session(record.session_id, timestamp)
                if isinstance(record, SessionRecord):
                    self._merge_session(record)
                    is_new = is_new_session
                elif isinstance(record, TurnRecord):
                    is_new = self._merge_turn(record)
                elif isinstance(record, UsageSample):
                    is_new = self._insert_usage(record)
                elif isinstance(record, RateLimitSample):
                    is_new = self._insert_rate_limit(record)
                else:
                    is_new = self._insert_tool(record)
                inserted += is_new
                duplicates += not is_new
            self._upsert_cursor(cursor)
        return IngestResult(inserted, duplicates)

    def _ensure_session(self, session_id: str, timestamp: str) -> bool:
        result = self.connection.execute(
            "INSERT INTO sessions (session_id, source, agent_kind, first_seen_at, last_seen_at) "
            "VALUES (?, 'unknown', 'unknown', ?, ?) ON CONFLICT(session_id) DO NOTHING",
            (session_id, timestamp, timestamp),
        )
        self.connection.execute(
            "UPDATE sessions SET first_seen_at=MIN(first_seen_at, ?), "
            "last_seen_at=MAX(last_seen_at, ?) WHERE session_id=?",
            (timestamp, timestamp, session_id),
        )
        return result.rowcount == 1

    def _merge_session(self, record: SessionRecord) -> None:
        self.connection.execute(
            "UPDATE sessions SET "
            "thread_id=COALESCE(?, thread_id), parent_thread_id=COALESCE(?, parent_thread_id), "
            "source=COALESCE(NULLIF(?, 'unknown'), source), "
            "agent_kind=COALESCE(NULLIF(?, 'unknown'), agent_kind), "
            "cli_version=COALESCE(?, cli_version), model_provider=COALESCE(?, model_provider), "
            "cwd=COALESCE(?, cwd), "
            "workspace_roots=CASE WHEN ?='[]' THEN workspace_roots ELSE ? END "
            "WHERE session_id=?",
            (record.thread_id, record.parent_thread_id, record.source, record.agent_kind,
             record.cli_version, record.model_provider, record.cwd,
             _workspace_roots(record.workspace_roots), _workspace_roots(record.workspace_roots),
             record.session_id),
        )

    def _merge_turn(self, record: TurnRecord) -> bool:
        exists = self.connection.execute(
            "SELECT 1 FROM turns WHERE session_id=? AND turn_id=?",
            (record.session_id, record.turn_id),
        ).fetchone()
        result = self.connection.execute(
            "INSERT INTO turns (turn_id, session_id, thread_id, root_turn_id, model, "
            "model_provider, reasoning_effort, collaboration_mode, sandbox_mode, approval_mode, "
            "cwd, workspace_roots, context_window, status, observed_at, started_at, completed_at, "
            "duration_ms, time_to_first_token_ms) VALUES (" + ",".join("?" for _ in range(19)) + ") "
            "ON CONFLICT(session_id, turn_id) DO UPDATE SET "
            "thread_id=COALESCE(excluded.thread_id, turns.thread_id), "
            "root_turn_id=COALESCE(excluded.root_turn_id, turns.root_turn_id), "
            "model=COALESCE(excluded.model, turns.model), "
            "model_provider=COALESCE(excluded.model_provider, turns.model_provider), "
            "reasoning_effort=COALESCE(excluded.reasoning_effort, turns.reasoning_effort), "
            "collaboration_mode=COALESCE(excluded.collaboration_mode, turns.collaboration_mode), "
            "sandbox_mode=COALESCE(excluded.sandbox_mode, turns.sandbox_mode), "
            "approval_mode=COALESCE(excluded.approval_mode, turns.approval_mode), "
            "cwd=COALESCE(excluded.cwd, turns.cwd), "
            "workspace_roots=CASE WHEN excluded.workspace_roots='[]' THEN turns.workspace_roots "
            "ELSE excluded.workspace_roots END, "
            "context_window=COALESCE(excluded.context_window, turns.context_window), "
            # A turn's lifecycle is monotonic; context observation times cannot suppress completion.
            "status=CASE WHEN turns.status='completed' THEN turns.status "
            "ELSE COALESCE(excluded.status, turns.status) END, "
            "observed_at=MAX(excluded.observed_at, turns.observed_at), "
            "started_at=COALESCE(excluded.started_at, turns.started_at), "
            "completed_at=COALESCE(excluded.completed_at, turns.completed_at), "
            "duration_ms=COALESCE(excluded.duration_ms, turns.duration_ms), "
            "time_to_first_token_ms=COALESCE(excluded.time_to_first_token_ms, "
            "turns.time_to_first_token_ms)",
            (record.turn_id, record.session_id, record.thread_id, record.root_turn_id, record.model,
             record.model_provider, record.reasoning_effort, record.collaboration_mode,
             record.sandbox_mode, record.approval_mode, record.cwd,
             _workspace_roots(record.workspace_roots), record.context_window, record.status,
             _utc_timestamp(record.timestamp), _epoch_timestamp(record.started_at),
             _epoch_timestamp(record.completed_at), record.duration_ms, record.time_to_first_token_ms),
        )
        self.connection.execute(
            "UPDATE sessions SET model_provider=COALESCE(?, model_provider), "
            "cwd=COALESCE(?, cwd), context_window=COALESCE(?, context_window) WHERE session_id=?",
            (record.model_provider, record.cwd, record.context_window, record.session_id),
        )
        return exists is None and result.rowcount == 1

    def _insert_usage(self, record: UsageSample) -> bool:
        result = self.connection.execute(
            "INSERT INTO usage_samples (session_id, response_id, thread_id, turn_id, root_turn_id, "
            "observed_at, input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, "
            "reasoning_output_tokens, total_tokens, cumulative_total_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, response_id) DO NOTHING",
            (record.session_id, record.response_id, record.thread_id or record.session_id,
             record.turn_id, record.root_turn_id, _utc_timestamp(record.timestamp),
             record.input_tokens, record.cached_input_tokens, record.cache_write_input_tokens,
             record.output_tokens, record.reasoning_output_tokens, record.total_tokens,
             record.cumulative_total_tokens),
        )
        self.connection.execute(
            "INSERT INTO responses (session_id, response_id, turn_id) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id, response_id) DO NOTHING",
            (record.session_id, record.response_id, record.turn_id),
        )
        return result.rowcount == 1

    def _insert_rate_limit(self, record: RateLimitSample) -> bool:
        timestamp = _utc_timestamp(record.timestamp)
        # Hash only named scalar telemetry, never a source event or arbitrary mapping.
        identity = json.dumps((
            record.session_id, timestamp, record.window, record.limit_id, record.limit_name,
            record.used_percent, record.window_minutes, record.resets_at,
        ), separators=(",", ":"), ensure_ascii=False)
        sample_id = hashlib.sha256(identity.encode()).hexdigest()
        result = self.connection.execute(
            "INSERT INTO rate_limit_samples (sample_id, session_id, observed_at, window, limit_id, "
            "limit_name, used_value, limit_value, window_seconds, resets_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 100, ?, ?) ON CONFLICT(sample_id) DO NOTHING",
            (sample_id, record.session_id, timestamp, record.window, record.limit_id,
             record.limit_name or record.limit_id or record.window, record.used_percent,
             record.window_minutes * 60 if record.window_minutes is not None else None,
             _epoch_timestamp(record.resets_at)),
        )
        return result.rowcount == 1

    def _insert_tool(self, record: ToolEvent) -> bool:
        result = self.connection.execute(
            "INSERT INTO tool_events (event_id, session_id, observed_at, thread_id, turn_id, "
            "tool_name, tool_type, status, started_at_ms, completed_at_ms, duration_ms, status_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
            (record.event_id, record.session_id, _utc_timestamp(record.timestamp), record.thread_id,
             record.turn_id, record.name, record.tool_type, record.status or "unknown",
             record.started_at_ms, record.completed_at_ms, record.duration_ms, record.status_code),
        )
        return result.rowcount == 1

    def _upsert_cursor(self, cursor: IngestCursor) -> None:
        timestamp = _utc_timestamp(cursor.updated_at) if cursor.updated_at else _now()
        self.connection.execute(
            "INSERT INTO ingest_cursors (file_key, source_path, device, inode, session_id, "
            "byte_offset, parse_failures, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_key) DO UPDATE SET source_path=excluded.source_path, "
            "device=excluded.device, inode=excluded.inode, session_id=excluded.session_id, "
            "byte_offset=excluded.byte_offset, parse_failures=excluded.parse_failures, "
            "updated_at=excluded.updated_at",
            (cursor.file_key, str(cursor.source_path), cursor.device, cursor.inode,
             cursor.session_id, cursor.offset, cursor.parse_failures, timestamp),
        )
        self.connection.execute(
            "UPDATE service_health SET "
            "last_poll_at=MAX(COALESCE(last_poll_at, ?), ?), "
            "last_success_at=MAX(COALESCE(last_success_at, ?), ?), "
            "files_seen=(SELECT COUNT(*) FROM ingest_cursors), "
            "parse_failures=(SELECT COALESCE(SUM(parse_failures), 0) FROM ingest_cursors) "
            "WHERE singleton=1",
            (timestamp, timestamp, timestamp, timestamp),
        )

    def get_cursor(self, file_key: str) -> IngestCursor | None:
        row = self.connection.execute(
            "SELECT * FROM ingest_cursors WHERE file_key=?", (file_key,),
        ).fetchone()
        if row is None:
            return None
        return IngestCursor(
            file_key=row["file_key"], source_path=row["source_path"], device=row["device"],
            inode=row["inode"], offset=row["byte_offset"], session_id=row["session_id"],
            parse_failures=row["parse_failures"], updated_at=row["updated_at"],
        )

    def usage_samples(
        self, start: str | datetime, end: str | datetime, session_id: str | None = None,
        *, model_providers: tuple[str, ...] | None = None,
    ) -> tuple[UsageSample, ...]:
        """Read unique samples in the inclusive UTC interval, ordered deterministically."""
        parameters = [_utc_timestamp(start), _utc_timestamp(end)]
        sql = (
            "SELECT u.* FROM usage_samples u "
            "LEFT JOIN turns t ON t.session_id=u.session_id AND t.turn_id=u.turn_id "
            "LEFT JOIN sessions s ON s.session_id=u.session_id "
            "WHERE u.observed_at>=? AND u.observed_at<=?"
        )
        if session_id is not None:
            sql += " AND u.session_id=?"
            parameters.append(session_id)
        if model_providers is not None:
            sql += (
                " AND COALESCE(t.model_provider, s.model_provider) IN "
                f"({','.join('?' for _ in model_providers)})"
            )
            parameters.extend(model_providers)
        sql += " ORDER BY u.observed_at, u.session_id, u.response_id"
        return tuple(UsageSample(
            session_id=row["session_id"], timestamp=row["observed_at"],
            response_id=row["response_id"], thread_id=row["thread_id"], turn_id=row["turn_id"],
            root_turn_id=row["root_turn_id"], input_tokens=row["input_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            cache_write_input_tokens=row["cache_write_input_tokens"],
            output_tokens=row["output_tokens"], reasoning_output_tokens=row["reasoning_output_tokens"],
            total_tokens=row["total_tokens"], cumulative_total_tokens=row["cumulative_total_tokens"],
        ) for row in self.connection.execute(sql, parameters))

    def total_tokens(self, session_id: str | None = None) -> int:
        sql = "SELECT COALESCE(SUM(total_tokens), 0) FROM usage_samples"
        parameters = ()
        if session_id is not None:
            sql += " WHERE session_id=?"
            parameters = (session_id,)
        return self.connection.execute(sql, parameters).fetchone()[0]

    def open_incident(
        self, *, incident_id: str, scope_type: str, scope_id: str, trigger: str,
        observed_rate: float, absolute_threshold: float, opened_at: str | datetime,
        baseline_rate: float | None = None, below_since: str | datetime | None = None,
    ) -> bool:
        """Persist an opening once; replay cannot overwrite the original detector values."""
        for value in (observed_rate, absolute_threshold, baseline_rate):
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                raise ValueError("incident rates require finite nonnegative numbers")
        with self.connection:
            result = self.connection.execute(
                "INSERT INTO alert_incidents (incident_id, scope_type, scope_id, trigger, "
                "observed_rate, baseline_rate, absolute_threshold, opened_at, below_since) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(incident_id) DO NOTHING",
                (incident_id, scope_type, scope_id, trigger, observed_rate, baseline_rate,
                 absolute_threshold, _utc_timestamp(opened_at),
                 _utc_timestamp(below_since) if below_since is not None else None),
            )
        return result.rowcount == 1

    def recover_incident(self, incident_id: str, recovered_at: str | datetime) -> bool:
        """Persist the first recovery time for an existing open incident."""
        with self.connection:
            result = self.connection.execute(
                "UPDATE alert_incidents SET recovered_at=? "
                "WHERE incident_id=? AND recovered_at IS NULL",
                (_utc_timestamp(recovered_at), incident_id),
            )
        return result.rowcount == 1

    def record_notification_attempt(
        self, *, incident_id: str, channel: str, attempted_at: str | datetime,
        attempt_number: int, outcome_code: str,
    ) -> bool:
        """Persist only fixed delivery codes, never subprocess output or raw errors."""
        if outcome_code not in NOTIFICATION_OUTCOMES:
            raise ValueError("unsupported notification outcome code")
        with self.connection:
            result = self.connection.execute(
                "INSERT INTO notification_attempts (incident_id, channel, attempted_at, "
                "attempt_number, outcome_code) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(incident_id, channel, attempt_number) DO NOTHING",
                (incident_id, channel, _utc_timestamp(attempted_at), attempt_number, outcome_code),
            )
            self.connection.execute(
                "UPDATE service_health SET notification_failures=(SELECT COUNT(*) "
                "FROM notification_attempts WHERE outcome_code IN ('failed', 'unavailable', 'timeout')) "
                "WHERE singleton=1"
            )
        return result.rowcount == 1

    def table_counts(self) -> dict[str, int]:
        return {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLE_NAMES
        }

    def maintain(self) -> None:
        """Checkpoint WAL and refresh query statistics without a telemetry retention policy."""
        self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self.connection.execute("PRAGMA optimize")

    def integrity_check(self) -> str:
        rows = self.connection.execute("PRAGMA integrity_check").fetchall()
        return "ok" if len(rows) == 1 and rows[0][0] == "ok" else "failed"

    def health_snapshot(self) -> dict[str, object]:
        snapshot = dict(self.connection.execute(
            "SELECT last_poll_at, last_success_at, files_seen, parse_failures, "
            "notification_failures, service_version FROM service_health WHERE singleton=1"
        ).fetchone())
        snapshot["integrity_check"] = self.integrity_check()
        return snapshot
