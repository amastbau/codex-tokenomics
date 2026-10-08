"""Bounded, content-free reports and sandboxed read-only SQLite queries."""

import math
import sqlite3
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path


class QueryError(RuntimeError):
    """Base class for stable query failures that never embeds SQL text."""


class QueryRejected(QueryError):
    """The requested report, filter, or SQL operation is not permitted."""


class QueryTimedOut(QueryError):
    """SQLite execution exceeded the configured wall-clock limit."""


class QueryRowLimitExceeded(QueryError):
    """A raw query returned more than the configured number of rows."""


_DENIED_ACTIONS = frozenset(
    getattr(sqlite3, name)
    for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_ALTER_TABLE",
        "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE",
        "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT", "SQLITE_ATTACH", "SQLITE_DETACH",
    )
    if hasattr(sqlite3, name)
)
_SAFE_PRAGMAS = frozenset({"integrity_check", "quick_check", "table_info", "table_xinfo"})


def _deny_mutation_and_attach(
    action: int, first: str | None, second: str | None, _database: str | None,
    _trigger: str | None,
) -> int:
    if action in _DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        pragma = (first or "").casefold()
        # An argument can change even otherwise informational pragmas.
        if pragma not in _SAFE_PRAGMAS or second is not None:
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        function_name = (second or first or "").casefold()
        if function_name in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _open_read_only(path: Path, *, timeout_ms: int | None = None) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve(strict=True)
    timeout_seconds = 5.0 if timeout_ms is None else max(0, timeout_ms) / 1000
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro", uri=True, timeout=timeout_seconds,
    )
    connection.row_factory = sqlite3.Row
    try:
        if timeout_ms is not None:
            connection.execute(f"PRAGMA busy_timeout={max(0, timeout_ms)}")
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(_deny_mutation_and_attach)
    except BaseException:
        connection.close()
        raise
    return connection


def _remaining_timeout_ms(deadline: int) -> int:
    remaining_ns = deadline - time.monotonic_ns()
    if remaining_ns <= 0:
        raise QueryTimedOut("query execution timed out")
    return max(1, math.ceil(remaining_ns / 1_000_000))


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _column_names(description: Sequence[Sequence[object]]) -> tuple[str, ...]:
    names: list[str] = []
    used: set[str] = set()
    suffixes: dict[str, int] = {}
    for column in description:
        base = str(column[0])
        candidate = base
        if candidate in used:
            suffix = suffixes.get(base, 2)
            candidate = f"{base}_{suffix}"
            while candidate in used:
                suffix += 1
                candidate = f"{base}_{suffix}"
            suffixes[base] = suffix + 1
        used.add(candidate)
        names.append(candidate)
    return tuple(names)


def _utc_filter(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QueryRejected(f"invalid {name} filter")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat()
    except (OverflowError, ValueError):
        raise QueryRejected(f"invalid {name} filter") from None


_REPORT_SQL = {
    "summary": """
        SELECT
          (SELECT COUNT(*) FROM sessions) AS sessions,
          (SELECT COUNT(*) FROM usage_samples u {usage_where}) AS responses,
          (SELECT COALESCE(SUM(input_tokens), 0) FROM usage_samples u {usage_where}) AS input_tokens,
          (SELECT COALESCE(SUM(cached_input_tokens), 0) FROM usage_samples u {usage_where})
            AS cached_input_tokens,
          (SELECT COALESCE(SUM(cache_write_input_tokens), 0) FROM usage_samples u {usage_where})
            AS cache_write_input_tokens,
          (SELECT COALESCE(SUM(output_tokens), 0) FROM usage_samples u {usage_where}) AS output_tokens,
          (SELECT COALESCE(SUM(reasoning_output_tokens), 0) FROM usage_samples u {usage_where})
            AS reasoning_output_tokens,
          (SELECT COALESCE(SUM(total_tokens), 0) FROM usage_samples u {usage_where}) AS total_tokens,
          (SELECT COUNT(*) FROM alert_incidents WHERE recovered_at IS NULL) AS active_incidents
    """,
    "sessions": """
        SELECT s.session_id, s.thread_id, s.parent_thread_id, s.source, s.agent_kind,
               s.cli_version, s.model_provider, s.context_window, s.first_seen_at, s.last_seen_at,
               COUNT(u.response_id) AS responses,
               COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
               COALESCE(SUM(u.cached_input_tokens), 0) AS cached_input_tokens,
               COALESCE(SUM(u.cache_write_input_tokens), 0) AS cache_write_input_tokens,
               COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(u.reasoning_output_tokens), 0) AS reasoning_output_tokens,
               COALESCE(SUM(u.total_tokens), 0) AS total_tokens
        FROM sessions s LEFT JOIN usage_samples u ON u.session_id=s.session_id {where}
        GROUP BY s.session_id
        ORDER BY total_tokens DESC, s.session_id LIMIT ?
    """,
    "models": """
        SELECT COALESCE(t.model, 'unknown') AS model, COUNT(*) AS responses,
               SUM(u.input_tokens) AS input_tokens,
               SUM(u.cached_input_tokens) AS cached_input_tokens,
               SUM(u.cache_write_input_tokens) AS cache_write_input_tokens,
               SUM(u.output_tokens) AS output_tokens,
               SUM(u.reasoning_output_tokens) AS reasoning_output_tokens,
               SUM(u.total_tokens) AS total_tokens
        FROM usage_samples u
        LEFT JOIN turns t ON t.session_id=u.session_id AND t.turn_id=u.turn_id {where}
        GROUP BY COALESCE(t.model, 'unknown')
        ORDER BY total_tokens DESC, model LIMIT ?
    """,
    "agents": """
        SELECT s.agent_kind, COUNT(DISTINCT s.session_id) AS sessions, COUNT(u.response_id) AS responses,
               COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
               COALESCE(SUM(u.cached_input_tokens), 0) AS cached_input_tokens,
               COALESCE(SUM(u.cache_write_input_tokens), 0) AS cache_write_input_tokens,
               COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(u.reasoning_output_tokens), 0) AS reasoning_output_tokens,
               COALESCE(SUM(u.total_tokens), 0) AS total_tokens
        FROM sessions s LEFT JOIN usage_samples u ON u.session_id=s.session_id {where}
        GROUP BY s.agent_kind ORDER BY total_tokens DESC, s.agent_kind LIMIT ?
    """,
    "usage": """
        SELECT u.session_id, u.response_id, u.thread_id, u.turn_id, u.root_turn_id, u.observed_at,
               COALESCE(t.model, 'unknown') AS model, s.agent_kind,
               u.input_tokens, u.cached_input_tokens, u.cache_write_input_tokens,
               u.output_tokens, u.reasoning_output_tokens, u.total_tokens,
               u.cumulative_total_tokens
        FROM usage_samples u JOIN sessions s ON s.session_id=u.session_id
        LEFT JOIN turns t ON t.session_id=u.session_id AND t.turn_id=u.turn_id {where}
        ORDER BY u.observed_at DESC, u.session_id, u.response_id LIMIT ?
    """,
    "timeline": """
        SELECT u.observed_at, u.session_id, u.response_id, u.turn_id,
               COALESCE(t.model, 'unknown') AS model, t.reasoning_effort, t.status,
               u.input_tokens, u.cached_input_tokens, u.cache_write_input_tokens,
               u.output_tokens, u.reasoning_output_tokens, u.total_tokens
        FROM usage_samples u
        LEFT JOIN turns t ON t.session_id=u.session_id AND t.turn_id=u.turn_id {where}
        ORDER BY u.observed_at, u.response_id LIMIT ?
    """,
    "anomalies": """
        SELECT incident_id, scope_type, scope_id, trigger, observed_rate, baseline_rate,
               absolute_threshold, opened_at, recovered_at,
               CASE WHEN baseline_rate>0 THEN observed_rate/baseline_rate ELSE NULL END
                 AS baseline_multiple
        FROM alert_incidents i {where}
        ORDER BY opened_at DESC, incident_id LIMIT ?
    """,
    "incidents": """
        SELECT i.incident_id, i.scope_type, i.scope_id, i.trigger, i.observed_rate,
               i.baseline_rate, i.absolute_threshold, i.opened_at, i.below_since, i.recovered_at,
               SUM(CASE WHEN n.outcome_code IN ('failed','unavailable','timeout') THEN 1 ELSE 0 END)
                 AS notification_failures
        FROM alert_incidents i LEFT JOIN notification_attempts n ON n.incident_id=i.incident_id
        {where} GROUP BY i.incident_id ORDER BY i.opened_at DESC, i.incident_id LIMIT ?
    """,
}


class QueryService:
    """Open a new immutable read-only connection for each bounded operation."""

    def __init__(self, database_path: Path, row_limit: int, timeout_ms: int) -> None:
        if type(row_limit) is not int or row_limit <= 0:
            raise ValueError("row_limit must be a positive integer")
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        self.database_path = Path(database_path)
        self.row_limit = row_limit
        self.timeout_ms = timeout_ms

    def run_report(
        self, name: str, filters: Mapping[str, object] | None = None,
    ) -> list[dict[str, object]]:
        filters = {} if filters is None else filters
        if not isinstance(filters, Mapping):
            raise QueryRejected("report filters must be a mapping")
        deadline = time.monotonic_ns() + self.timeout_ms * 1_000_000
        if name == "health":
            if filters:
                raise QueryRejected("unsupported filter for health report")
            return self._health_report(deadline)
        if name not in _REPORT_SQL:
            raise QueryRejected("unknown report")
        where, parameters, limit = self._filters(name, filters)
        if name == "summary":
            # Repeated scalar subqueries each need the same bound parameters.
            usage_where = f"WHERE {where}" if where else ""
            sql = _REPORT_SQL[name].format(usage_where=usage_where)
            parameters = parameters * 7
        else:
            sql = _REPORT_SQL[name].format(where=f"WHERE {where}" if where else "")
            parameters.append(limit)
        return self._execute(
            sql, parameters, limit=self.row_limit, raw=False, deadline=deadline,
        )

    def run_sql(
        self, statement: str, parameters: Sequence[object] = (),
    ) -> list[dict[str, object]]:
        if not isinstance(statement, str) or not statement.strip():
            raise QueryRejected("query rejected")
        if isinstance(parameters, (str, bytes, bytearray)):
            raise QueryRejected("query parameters must be a sequence")
        return self._execute(
            statement, parameters, limit=self.row_limit, raw=True,
            deadline=time.monotonic_ns() + self.timeout_ms * 1_000_000,
        )

    def _filters(
        self, name: str, filters: Mapping[str, object],
    ) -> tuple[str, list[object], int]:
        supported = {"since", "until", "session", "limit"}
        unknown = set(filters) - supported
        if unknown:
            raise QueryRejected("unsupported filter")
        limit = filters.get("limit", self.row_limit)
        if type(limit) is not int or not 0 < limit <= self.row_limit:
            raise QueryRejected("invalid limit filter")
        if name == "summary" and "session" in filters:
            raise QueryRejected("unsupported filter")
        conditions: list[str] = []
        parameters: list[object] = []
        timestamp_column = "i.opened_at" if name in {"anomalies", "incidents"} else (
            "s.first_seen_at" if name == "sessions" else "u.observed_at"
        )
        session_column = "i.scope_id" if name in {"anomalies", "incidents"} else (
            "s.session_id" if name in {"sessions", "agents"} else "u.session_id"
        )
        if "since" in filters:
            conditions.append(f"{timestamp_column}>=?")
            parameters.append(_utc_filter(filters["since"], "since"))
        if "until" in filters:
            conditions.append(f"{timestamp_column}<=?")
            parameters.append(_utc_filter(filters["until"], "until"))
        if "session" in filters:
            session = filters["session"]
            if not isinstance(session, str) or not session:
                raise QueryRejected("invalid session filter")
            conditions.append(f"{session_column}=?")
            parameters.append(session)
        return " AND ".join(conditions), parameters, limit

    def _health_report(self, deadline: int) -> list[dict[str, object]]:
        sql = (
            "SELECT last_poll_at, last_success_at, files_seen, parse_failures, "
            "notification_failures, service_version FROM service_health WHERE singleton=1"
        )
        rows = self._execute(sql, (), limit=1, raw=False, deadline=deadline)
        try:
            integrity = self._execute(
                "PRAGMA integrity_check", (), limit=self.row_limit, raw=False,
                deadline=deadline,
            )
        except QueryTimedOut:
            raise
        except QueryError:
            integrity_status = "unavailable"
        else:
            integrity_status = "ok" if len(integrity) == 1 and next(
                iter(integrity[0].values()), None,
            ) == "ok" else "failed"
        for row in rows:
            row["integrity_check"] = integrity_status
            row["status"] = "ok" if integrity_status == "ok" and row["last_success_at"] else (
                "degraded"
            )
        return rows

    def _execute(
        self, statement: str, parameters: Sequence[object], *, limit: int, raw: bool,
        deadline: int,
    ) -> list[dict[str, object]]:
        timed_out = False
        recursive = False

        def progress() -> int:
            nonlocal timed_out
            timed_out = time.monotonic_ns() >= deadline
            return int(timed_out)

        def authorize(
            action: int, first: str | None, second: str | None,
            database: str | None, trigger: str | None,
        ) -> int:
            nonlocal recursive
            if action == getattr(sqlite3, "SQLITE_RECURSIVE", -1):
                recursive = True
            return _deny_mutation_and_attach(action, first, second, database, trigger)

        try:
            connection = _open_read_only(
                self.database_path, timeout_ms=_remaining_timeout_ms(deadline),
            )
        except (OSError, sqlite3.Error):
            if time.monotonic_ns() >= deadline:
                raise QueryTimedOut("query execution timed out") from None
            raise QueryRejected("database unavailable") from None
        try:
            connection.set_authorizer(authorize)
            connection.set_progress_handler(progress, 1000)
            try:
                cursor = connection.execute(statement, tuple(parameters))
                names = _column_names(cursor.description or ())
                fetched = cursor.fetchmany(limit + 1)
                if len(fetched) > limit and raw and recursive:
                    # Do not return a prefix from a potentially infinite generator. Discard
                    # bounded chunks until it finishes or the execution deadline interrupts it.
                    while cursor.fetchmany(limit + 1):
                        pass
                if len(fetched) > limit:
                    raise QueryRowLimitExceeded("query row limit exceeded")
            except sqlite3.Error:
                if timed_out or time.monotonic_ns() >= deadline:
                    raise QueryTimedOut("query execution timed out") from None
                raise QueryRejected("query rejected") from None
            if time.monotonic_ns() >= deadline:
                raise QueryTimedOut("query execution timed out")
            return [
                {name: _json_value(value) for name, value in zip(names, row, strict=True)}
                for row in fetched
            ]
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()
