import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_tokenomics.query import (
    QueryRejected,
    QueryRowLimitExceeded,
    QueryService,
    QueryTimedOut,
    _open_read_only,
)
from codex_tokenomics.storage import IngestCursor, TelemetryStore
from codex_tokenomics.telemetry import SessionRecord, TurnRecord, UsageSample

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
SECRET = "PROHIBITED-QUERY-CONTENT-524b"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as store:
        store.ingest([
            SessionRecord(
                "session-user", NOW.isoformat(), source="cli", agent_kind="user",
                cwd="/workspace/project", workspace_roots=("/workspace/project",),
            ),
            TurnRecord(
                "session-user", NOW.isoformat(), "turn-user", model="gpt-6.1-sol",
                reasoning_effort="high", status="completed",
            ),
            UsageSample(
                "session-user", NOW.isoformat(), "response-user", turn_id="turn-user",
                input_tokens=1_000, cached_input_tokens=600, cache_write_input_tokens=100,
                output_tokens=300, reasoning_output_tokens=200, total_tokens=1_300,
            ),
            SessionRecord(
                "session-agent", NOW.isoformat(), source="subagent", agent_kind="reviewer",
                parent_thread_id="session-user",
            ),
            TurnRecord(
                "session-agent", NOW.isoformat(), "turn-agent", model="gpt-6-luna",
                reasoning_effort="medium", status="completed",
            ),
            UsageSample(
                "session-agent", NOW.isoformat(), "response-agent", turn_id="turn-agent",
                input_tokens=300, cached_input_tokens=100, cache_write_input_tokens=0,
                output_tokens=100, reasoning_output_tokens=0, total_tokens=400,
            ),
        ], IngestCursor("fixture", "/synthetic/rollout.jsonl", 1, 2, 10))
        store.open_incident(
            incident_id="incident-1", scope_type="session", scope_id="session-user",
            trigger="absolute", observed_rate=260_000, baseline_rate=20_000,
            absolute_threshold=250_000, opened_at=NOW,
        )
    yield path


@pytest.fixture
def query(database: Path) -> QueryService:
    return QueryService(database, row_limit=100, timeout_ms=10)


def test_models_report_ranks_total_and_cached_tokens(query: QueryService) -> None:
    rows = query.run_report("models", {"since": "2026-10-08T00:00:00Z"})
    assert rows[0] == {
        "model": "gpt-6.1-sol", "responses": 1, "input_tokens": 1000,
        "cached_input_tokens": 600, "cache_write_input_tokens": 100,
        "output_tokens": 300, "reasoning_output_tokens": 200, "total_tokens": 1300,
    }
    assert rows[0]["total_tokens"] >= rows[1]["total_tokens"]


@pytest.mark.parametrize("name", [
    "health", "summary", "sessions", "models", "agents", "usage", "timeline",
    "anomalies", "incidents",
])
def test_every_named_report_is_json_safe_and_content_free(
    query: QueryService, name: str,
) -> None:
    filters = {"session": "session-user"} if name == "timeline" else {}
    serialized = json.dumps(query.run_report(name, filters), allow_nan=False, sort_keys=True)
    assert SECRET not in serialized
    assert "cwd" not in serialized
    assert "workspace" not in serialized


def test_report_filters_are_parameterized_and_unknown_filters_fail(query: QueryService) -> None:
    assert query.run_report("sessions", {"session": "' OR 1=1 --"}) == []
    with pytest.raises(QueryRejected, match="unsupported filter"):
        query.run_report("models", {"future_filter": SECRET})
    with pytest.raises(QueryRejected, match="unknown report"):
        query.run_report(SECRET, {})


def test_sessions_agents_usage_timeline_and_incidents_expose_expected_telemetry(
    query: QueryService,
) -> None:
    sessions = query.run_report("sessions", {})
    assert [(row["session_id"], row["agent_kind"]) for row in sessions] == [
        ("session-user", "user"), ("session-agent", "reviewer"),
    ]
    assert query.run_report("agents", {})[0]["agent_kind"] == "user"
    assert query.run_report("usage", {"session": "session-user"})[0]["model"] == "gpt-6.1-sol"
    assert query.run_report("timeline", {"session": "session-user"})[0]["response_id"] == (
        "response-user"
    )
    incident = query.run_report("incidents", {})[0]
    assert incident["incident_id"] == "incident-1"
    assert query.run_report("anomalies", {})[0]["scope_id"] == "session-user"


@pytest.mark.parametrize("statement", [
    "DELETE FROM sessions",
    "INSERT INTO sessions(session_id) VALUES ('x')",
    "UPDATE sessions SET source='x'",
    "DROP TABLE sessions",
    "ATTACH DATABASE '/tmp/other.db' AS other",
    "DETACH DATABASE main",
    "PRAGMA writable_schema=ON",
    "PRAGMA journal_mode=WAL",
    "BEGIN",
    "VACUUM",
    "SELECT load_extension('/tmp/unknown')",
    "SELECT 1; SELECT 2",
    "WITH changed AS (DELETE FROM sessions RETURNING *) SELECT * FROM changed",
])
def test_unsafe_sql_is_rejected(query: QueryService, statement: str) -> None:
    with pytest.raises(QueryRejected):
        query.run_sql(statement)


def test_sql_accepts_one_parameterized_read_and_returns_json_safe_rows(
    query: QueryService,
) -> None:
    rows = query.run_sql(
        "SELECT session_id, total_tokens FROM usage_samples WHERE session_id=?",
        ("session-user",),
    )
    assert rows == [{"session_id": "session-user", "total_tokens": 1300}]
    assert json.dumps(rows, allow_nan=False)


def test_duplicate_and_generated_column_names_preserve_every_value(query: QueryService) -> None:
    assert query.run_sql("SELECT 1 AS x, 2 AS x, 3 AS x_2") == [{
        "x": 1, "x_2": 2, "x_2_2": 3,
    }]


@pytest.mark.parametrize("name", ["since", "until"])
def test_extreme_aware_timestamp_is_safely_rejected(query: QueryService, name: str) -> None:
    with pytest.raises(QueryRejected, match=f"invalid {name} filter"):
        query.run_report("usage", {name: "0001-01-01T00:00:00+23:59"})


def test_query_connection_is_uri_read_only_even_without_authorizer(
    database: Path,
) -> None:
    with _open_read_only(database) as connection:
        connection.set_authorizer(None)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM sessions")


def test_row_limit_is_enforced(query: QueryService) -> None:
    with pytest.raises(QueryRowLimitExceeded):
        query.run_sql(
            "WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<101) "
            "SELECT n FROM x"
        )


def test_recursive_query_hits_execution_limit(query: QueryService) -> None:
    with pytest.raises(QueryTimedOut):
        query.run_sql(
            "WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x) SELECT * FROM x"
        )


def test_locked_wal_database_cannot_bypass_query_timeout(tmp_path: Path) -> None:
    path = tmp_path / "locked.db"
    locker = sqlite3.connect(path)
    try:
        assert locker.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        locker.execute("CREATE TABLE telemetry(value INTEGER)")
        locker.commit()
        assert locker.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0] == "exclusive"
        locker.execute("BEGIN EXCLUSIVE")
        locker.execute("INSERT INTO telemetry VALUES (1)")
        started = time.monotonic()
        with pytest.raises(QueryTimedOut):
            QueryService(path, row_limit=10, timeout_ms=30).run_sql(
                "SELECT value FROM telemetry"
            )
        assert time.monotonic() - started < 0.5
    finally:
        locker.rollback()
        locker.close()


def test_database_stays_unchanged_after_queries(query: QueryService, database: Path) -> None:
    before = database.read_bytes()
    query.run_report("summary", {})
    query.run_sql("SELECT COUNT(*) AS count FROM usage_samples")
    assert database.read_bytes() == before
