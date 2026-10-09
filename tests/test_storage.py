import json
import os
import sqlite3
from dataclasses import FrozenInstanceError, replace
from itertools import permutations
from pathlib import Path

import pytest

from codex_tokenomics import storage as storage_module
from codex_tokenomics.storage import IngestCursor, TelemetryStore
from codex_tokenomics.telemetry import (
    EventContext,
    RateLimitSample,
    SessionRecord,
    ToolEvent,
    TurnRecord,
    UsageSample,
    normalize_event,
)

TIMESTAMP = "2026-10-08T08:00:00+00:00"
SECRET = "PROHIBITED-CONTENT-9b84f1"
USAGE = UsageSample(
    session_id="session-1", timestamp=TIMESTAMP, response_id="response-1",
    input_tokens=1000, cached_input_tokens=900, cache_write_input_tokens=20,
    output_tokens=337, reasoning_output_tokens=37, total_tokens=1337,
    thread_id="thread-1", turn_id="turn-1", root_turn_id="root-turn-1",
    cumulative_total_tokens=8000,
)
CURSOR = IngestCursor(
    file_key="file-1", source_path="/synthetic/session.jsonl", device=7, inode=42,
    offset=200, session_id="session-1", parse_failures=2, updated_at=TIMESTAMP,
)


@pytest.fixture
def store(tmp_path: Path):
    with TelemetryStore.open(tmp_path / "private" / "telemetry.db") as opened:
        yield opened


def row(store: TelemetryStore, table: str) -> dict:
    return dict(store.connection.execute(f"SELECT * FROM {table}").fetchone())


def test_open_applies_migration_once_and_enables_safe_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "private" / "telemetry.db"
    for _ in range(2):
        with TelemetryStore.open(path) as store:
            assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 1
            assert store.connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()[0][0] == storage_module.SCHEMA_VERSION
            assert store.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
            assert store.integrity_check() == "ok"


def test_new_runtime_directories_and_files_are_private(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode bits are not supported")
    path = tmp_path / "private" / "nested" / "telemetry.db"
    with TelemetryStore.open(path) as store:
        store.ingest([USAGE], CURSOR)
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.parent.parent.stat().st_mode & 0o777 == 0o700
        for runtime_file in path.parent.iterdir():
            assert runtime_file.stat().st_mode & 0o777 == 0o600


def test_existing_database_permissions_are_tightened(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode bits are not supported")
    path = tmp_path / "telemetry.db"
    path.touch(mode=0o644)
    path.chmod(0o644)
    with TelemetryStore.open(path):
        assert path.stat().st_mode & 0o777 == 0o600


def test_ingest_commits_records_and_cursor_atomically(store: TelemetryStore) -> None:
    result = store.ingest([USAGE], CURSOR)
    assert (result.inserted, result.duplicates) == (1, 0)
    assert store.get_cursor("file-1") == CURSOR
    assert store.get_cursor("absent") is None
    assert store.total_tokens(session_id="session-1") == 1337


def test_cursor_failure_rolls_back_usage_and_session_changes(store: TelemetryStore) -> None:
    store.ingest([USAGE], CURSOR)
    new_usage = replace(USAGE, session_id="session-2", response_id="response-2")
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest([new_usage], replace(CURSOR, offset=-1))
    assert store.get_cursor("file-1") == CURSOR
    assert store.total_tokens() == 1337
    assert store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_invalid_record_rolls_back_earlier_records_and_cursor(store: TelemetryStore) -> None:
    invalid = replace(USAGE, response_id="response-2", total_tokens=-1)
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest([USAGE, invalid], CURSOR)
    assert store.get_cursor("file-1") is None
    assert store.total_tokens() == 0
    assert store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_duplicate_response_is_counted_once(store: TelemetryStore) -> None:
    store.ingest([USAGE], CURSOR)
    result = store.ingest([replace(USAGE, total_tokens=9000)], replace(CURSOR, offset=300))
    assert (result.inserted, result.duplicates) == (0, 1)
    assert store.total_tokens(session_id="session-1") == 1337
    assert store.get_cursor("file-1").offset == 300


def test_usage_identity_is_session_and_response_only(store: TelemetryStore) -> None:
    result = store.ingest([
        USAGE,
        replace(USAGE, thread_id="another-thread", turn_id="another-turn"),
        replace(USAGE, session_id="session-2"),
        replace(USAGE, response_id="response-2"),
    ], CURSOR)
    assert (result.inserted, result.duplicates) == (3, 1)
    assert store.total_tokens() == 4011
    assert store.total_tokens(session_id="session-1") == 2674


def test_records_before_metadata_create_and_enrich_placeholder(store: TelemetryStore) -> None:
    store.ingest([USAGE], CURSOR)
    placeholder = row(store, "sessions")
    assert (placeholder["source"], placeholder["agent_kind"]) == ("unknown", "unknown")
    metadata = SessionRecord(
        session_id="session-1", timestamp="2026-10-08T07:59:00+00:00",
        source="cli", agent_kind="user", cli_version="0.155.1",
    )
    store.ingest([metadata, SessionRecord(session_id="session-1", timestamp=TIMESTAMP)], CURSOR)
    enriched = row(store, "sessions")
    assert (enriched["source"], enriched["agent_kind"], enriched["cli_version"]) == (
        "cli", "user", "0.155.1",
    )
    assert enriched["first_seen_at"] == "2026-10-08T07:59:00+00:00"
    assert enriched["last_seen_at"] == TIMESTAMP
    assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.total_tokens() == 1337


def test_all_session_fields_are_persisted(store: TelemetryStore) -> None:
    session = SessionRecord(
        session_id="session-1", timestamp=TIMESTAMP, thread_id="thread-1",
        parent_thread_id="parent-1", source="subagent", agent_kind="review",
        cli_version="0.155.1", model_provider="openai", cwd="/synthetic/project",
        workspace_roots=("/synthetic/project", "/synthetic/other"),
    )
    store.ingest([session], CURSOR)
    assert row(store, "sessions") == {
        "session_id": "session-1", "thread_id": "thread-1", "parent_thread_id": "parent-1",
        "source": "subagent", "agent_kind": "review", "cli_version": "0.155.1",
        "model_provider": "openai", "cwd": "/synthetic/project",
        "workspace_roots": '["/synthetic/project","/synthetic/other"]',
        "context_window": None, "first_seen_at": TIMESTAMP, "last_seen_at": TIMESTAMP,
    }


def test_all_turn_fields_are_persisted_and_lifecycle_merges(store: TelemetryStore) -> None:
    context = TurnRecord(
        session_id="session-1", timestamp=TIMESTAMP, turn_id="turn-1", thread_id="thread-1",
        root_turn_id="root-turn-1", model="gpt-6.1-sol", model_provider="openai",
        reasoning_effort="high", collaboration_mode="default", sandbox_mode="workspace-write",
        approval_mode="on-request", cwd="/synthetic/project", workspace_roots=("/synthetic/project",),
        context_window=258400,
    )
    lifecycle = TurnRecord(
        session_id="session-1", timestamp="2026-10-08T08:00:03+00:00", turn_id="turn-1",
        status="completed", started_at=1791446400, completed_at=1791446403,
        duration_ms=3000, time_to_first_token_ms=40,
    )
    store.ingest([context, lifecycle, context], CURSOR)
    assert row(store, "turns") == {
        "turn_id": "turn-1", "session_id": "session-1", "thread_id": "thread-1",
        "root_turn_id": "root-turn-1", "model": "gpt-6.1-sol", "model_provider": "openai",
        "reasoning_effort": "high", "collaboration_mode": "default",
        "sandbox_mode": "workspace-write", "approval_mode": "on-request",
        "cwd": "/synthetic/project", "workspace_roots": '["/synthetic/project"]',
        "context_window": 258400, "status": "completed",
        "observed_at": "2026-10-08T08:00:03+00:00", "started_at": "2026-10-08T08:00:00+00:00",
        "completed_at": "2026-10-08T08:00:03+00:00", "duration_ms": 3000,
        "time_to_first_token_ms": 40,
    }
    assert row(store, "sessions")["context_window"] == 258400


def test_turn_identity_is_scoped_by_session(store: TelemetryStore) -> None:
    store.ingest([
        TurnRecord(
            session_id="session-1", timestamp=TIMESTAMP, turn_id="turn-1",
            model="gpt-6.1-sol",
        ),
        TurnRecord(
            session_id="session-2", timestamp=TIMESTAMP, turn_id="turn-1",
            model="gpt-6.1-mini",
        ),
    ], CURSOR)

    rows = store.connection.execute(
        "SELECT session_id, turn_id, model FROM turns ORDER BY session_id"
    ).fetchall()
    assert [tuple(item) for item in rows] == [
        ("session-1", "turn-1", "gpt-6.1-sol"),
        ("session-2", "turn-1", "gpt-6.1-mini"),
    ]


def test_all_usage_fields_round_trip(store: TelemetryStore) -> None:
    store.ingest([USAGE], CURSOR)
    assert store.usage_samples(TIMESTAMP, TIMESTAMP) == (USAGE,)
    assert row(store, "responses")["response_id"] == "response-1"
    assert store.total_tokens() == 1337


def test_all_rate_limit_fields_are_persisted_and_replay_is_deduplicated(store: TelemetryStore) -> None:
    sample = RateLimitSample(
        session_id="session-1", timestamp=TIMESTAMP, window="primary", used_percent=12.5,
        limit_id="codex", limit_name="tokens", window_minutes=300, resets_at=1791446400,
    )
    first = store.ingest([sample], CURSOR)
    second = store.ingest([sample], CURSOR)
    assert (first.inserted, second.duplicates) == (1, 1)
    persisted = row(store, "rate_limit_samples")
    assert persisted.pop("sample_id")
    assert persisted == {
        "session_id": "session-1", "observed_at": TIMESTAMP, "window": "primary",
        "limit_id": "codex", "limit_name": "tokens", "used_value": 12.5, "limit_value": 100.0,
        "window_seconds": 18000, "resets_at": "2026-10-08T08:00:00+00:00",
    }
    store.ingest([replace(sample, window="secondary")], CURSOR)
    assert store.connection.execute("SELECT COUNT(*) FROM rate_limit_samples").fetchone()[0] == 2


def test_all_tool_fields_are_persisted_and_stable_event_id_deduplicates(store: TelemetryStore) -> None:
    tool = ToolEvent(
        session_id="session-1", timestamp=TIMESTAMP, event_id="tool-1", name="exec_command",
        tool_type="command_execution", status="completed", thread_id="thread-1", turn_id="turn-1",
        started_at_ms=1791446400000, completed_at_ms=1791446401000, duration_ms=1000, status_code=0,
    )
    store.ingest([tool, tool], CURSOR)
    assert row(store, "tool_events") == {
        "event_id": "tool-1", "session_id": "session-1", "observed_at": TIMESTAMP,
        "tool_name": "exec_command", "tool_type": "command_execution", "status": "completed",
        "thread_id": "thread-1", "turn_id": "turn-1", "started_at_ms": 1791446400000,
        "completed_at_ms": 1791446401000, "duration_ms": 1000, "status_code": 0,
    }


def test_database_and_wal_never_contain_prohibited_content(store: TelemetryStore, capsys) -> None:
    fixture = Path(__file__).parent / "fixtures" / "mixed-session.jsonl"
    context = EventContext(
        source_path="/synthetic/session.jsonl", device=7, inode=42, byte_offset=0,
        session_id="session-1",
    )
    records = tuple(
        record for index, line in enumerate(fixture.read_text().splitlines())
        for record in normalize_event(json.loads(line), replace(context, byte_offset=index * 100))
    )
    assert records
    store.ingest(records, CURSOR)
    store.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    for runtime_file in store.path.parent.iterdir():
        assert SECRET.encode() not in runtime_file.read_bytes()
    assert SECRET not in json.dumps(store.health_snapshot())
    assert SECRET not in repr(store.usage_samples("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"))
    assert capsys.readouterr() == ("", "")


def test_ingest_rejects_source_blobs_without_writing_them(store: TelemetryStore) -> None:
    with pytest.raises(TypeError, match="normalized telemetry"):
        store.ingest([{"payload": SECRET}], CURSOR)
    assert store.get_cursor("file-1") is None
    assert store.total_tokens() == 0


def test_cursor_is_immutable_and_counters_survive_restart(tmp_path: Path) -> None:
    with pytest.raises(FrozenInstanceError):
        CURSOR.offset = 300
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as store:
        store.ingest([], CURSOR)
    with TelemetryStore.open(path) as store:
        assert store.get_cursor("file-1") == CURSOR


def test_future_schema_is_rejected_without_altering_it(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as store, store.connection:
        store.connection.execute(
            "INSERT INTO schema_migrations VALUES (99, ?)", (TIMESTAMP,),
        )
    with pytest.raises(RuntimeError, match="newer schema"):
        TelemetryStore.open(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 99


def test_version_one_database_migrates_turn_identity_to_session_scope(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_migrations SET version=1")
        connection.execute("ALTER TABLE turns RENAME TO turns_v2")
        connection.execute("""
            CREATE TABLE turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
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
                time_to_first_token_ms INTEGER
            )
        """)
        connection.execute("INSERT INTO turns SELECT * FROM turns_v2")
        connection.execute("DROP TABLE turns_v2")
        connection.commit()

    with TelemetryStore.open(path) as store:
        store.ingest([
            TurnRecord(
                session_id="session-1", timestamp=TIMESTAMP, turn_id="turn-1",
                model="gpt-6.1-sol",
            ),
            TurnRecord(
                session_id="session-2", timestamp=TIMESTAMP, turn_id="turn-1",
                model="gpt-6.1-mini",
            ),
        ], CURSOR)
        assert store.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == (
            storage_module.SCHEMA_VERSION
        )
        assert store.integrity_check() == "ok"


def test_reopen_and_reingest_does_not_change_totals(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as first:
        first.ingest([USAGE], CURSOR)
    with TelemetryStore.open(path) as second:
        result = second.ingest([USAGE], replace(CURSOR, offset=300))
        assert result.duplicates == 1
        assert second.total_tokens(session_id="session-1") == 1337
        assert second.get_cursor("file-1").offset == 300


def test_maintenance_never_deletes_telemetry(store: TelemetryStore) -> None:
    store.ingest([USAGE], CURSOR)
    before = store.table_counts()
    store.maintain()
    assert store.table_counts() == before
    assert store.integrity_check() == "ok"


def open_incident(store: TelemetryStore, **overrides) -> bool:
    arguments = {
        "incident_id": "incident-1", "scope_type": "session", "scope_id": "session-1",
        "trigger": "absolute+relative", "observed_rate": 4000, "baseline_rate": 1000,
        "absolute_threshold": 3000, "opened_at": TIMESTAMP,
    }
    return store.open_incident(**(arguments | overrides))


def test_incident_opening_is_idempotent_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as first:
        assert open_incident(first) is True
        assert open_incident(first, observed_rate=9000) is False
    with TelemetryStore.open(path) as second:
        assert open_incident(second) is False
        assert row(second, "alert_incidents") == {
            "incident_id": "incident-1", "scope_type": "session", "scope_id": "session-1",
            "trigger": "absolute+relative", "observed_rate": 4000.0, "baseline_rate": 1000.0,
            "absolute_threshold": 3000.0, "opened_at": TIMESTAMP,
            "below_since": None, "recovered_at": None,
        }


def test_incident_recovery_is_idempotent(store: TelemetryStore) -> None:
    open_incident(store)
    recovered = "2026-10-08T08:02:00+00:00"
    assert store.recover_incident("incident-1", recovered) is True
    assert store.recover_incident("incident-1", "2026-10-08T08:05:00+00:00") is False
    assert store.recover_incident("absent", recovered) is False
    assert row(store, "alert_incidents")["recovered_at"] == recovered


def test_aggregate_incident_accepts_missing_baseline(store: TelemetryStore) -> None:
    assert open_incident(
        store, scope_type="aggregate", scope_id="all", trigger="absolute", baseline_rate=None,
    ) is True
    assert row(store, "alert_incidents")["baseline_rate"] is None


def test_notification_attempts_are_unique_and_foreign_key_protected(store: TelemetryStore) -> None:
    open_incident(store)
    arguments = {
        "incident_id": "incident-1", "channel": "email", "attempted_at": TIMESTAMP,
        "attempt_number": 1, "outcome_code": "failed",
    }
    assert store.record_notification_attempt(**arguments) is True
    assert store.record_notification_attempt(**arguments) is False
    assert store.record_notification_attempt(**(arguments | {
        "attempt_number": 2, "outcome_code": "sent",
    })) is True
    assert store.connection.execute("SELECT COUNT(*) FROM notification_attempts").fetchone()[0] == 2
    assert store.health_snapshot()["notification_failures"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.record_notification_attempt(**(arguments | {"incident_id": "absent"}))


@pytest.mark.parametrize("field,value", [
    ("scope_type", "unknown"), ("trigger", "unknown"), ("observed_rate", float("inf")),
    ("baseline_rate", float("nan")), ("absolute_threshold", -1),
])
def test_incident_rejects_invalid_detector_values(store: TelemetryStore, field, value) -> None:
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        open_incident(store, **{field: value})
    assert store.connection.execute("SELECT COUNT(*) FROM alert_incidents").fetchone()[0] == 0


def test_notification_outcome_cannot_persist_raw_error_text(store: TelemetryStore, capsys) -> None:
    open_incident(store)
    with pytest.raises(ValueError, match="outcome code"):
        store.record_notification_attempt(
            incident_id="incident-1", channel="email", attempted_at=TIMESTAMP,
            attempt_number=1, outcome_code=f"error: {SECRET}",
        )
    store.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    assert store.connection.execute("SELECT COUNT(*) FROM notification_attempts").fetchone()[0] == 0
    for runtime_file in store.path.parent.iterdir():
        assert SECRET.encode() not in runtime_file.read_bytes()
    assert capsys.readouterr() == ("", "")


def test_health_reports_committed_file_counters_and_poll_times(store: TelemetryStore) -> None:
    store.ingest([], CURSOR)
    store.ingest([], replace(
        CURSOR, offset=300, parse_failures=3, updated_at="2026-10-08T08:01:00+00:00",
    ))
    store.ingest([], replace(CURSOR, file_key="file-2", parse_failures=1))
    snapshot = store.health_snapshot()
    assert snapshot == {
        "last_poll_at": "2026-10-08T08:01:00+00:00",
        "last_success_at": "2026-10-08T08:01:00+00:00",
        "files_seen": 2, "parse_failures": 4, "notification_failures": 0,
        "service_version": "0.1.0", "integrity_check": "ok",
    }
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest([], replace(CURSOR, offset=-1, parse_failures=100))
    assert store.health_snapshot() == snapshot


def test_usage_query_uses_utc_inclusive_bounds_and_session_filter(store: TelemetryStore) -> None:
    store.ingest([
        replace(USAGE, response_id="before", timestamp="2026-10-08T07:59:59Z"),
        replace(USAGE, response_id="at-start", timestamp="2026-10-08T10:00:00+02:00"),
        replace(USAGE, response_id="at-end", timestamp="2026-10-08T08:01:00Z"),
        replace(USAGE, response_id="after", timestamp="2026-10-08T08:01:01Z"),
        replace(USAGE, session_id="session-2", response_id="other-session"),
    ], CURSOR)
    samples = store.usage_samples(TIMESTAMP, "2026-10-08T08:01:00Z", session_id="session-1")
    assert [sample.response_id for sample in samples] == ["at-start", "at-end"]
    assert len(store.usage_samples(TIMESTAMP, "2026-10-08T08:01:00Z")) == 3
    assert store.usage_samples(TIMESTAMP, TIMESTAMP, session_id="absent") == ()


def test_missing_optional_fields_have_content_free_fallbacks(store: TelemetryStore) -> None:
    store.ingest([
        replace(USAGE, thread_id=None),
        ToolEvent(
            session_id="session-1", timestamp=TIMESTAMP, event_id="tool-1",
            name="exec_command", tool_type="command_execution",
        ),
        RateLimitSample(
            session_id="session-1", timestamp=TIMESTAMP, window="primary", used_percent=10,
        ),
    ], CURSOR)
    assert row(store, "usage_samples")["thread_id"] == "session-1"
    assert row(store, "tool_events")["status"] == "unknown"
    assert row(store, "rate_limit_samples")["limit_name"] == "primary"


def test_older_turn_lifecycle_cannot_revert_completion(store: TelemetryStore) -> None:
    store.ingest([
        TurnRecord(
            session_id="session-1", timestamp="2026-10-08T08:00:03Z", turn_id="turn-1",
            status="completed", completed_at=1791446403, duration_ms=3000,
        ),
        TurnRecord(
            session_id="session-1", timestamp=TIMESTAMP, turn_id="turn-1",
            status="started", started_at=1791446400,
        ),
    ], CURSOR)
    persisted = row(store, "turns")
    assert persisted["status"] == "completed"
    assert persisted["started_at"] == TIMESTAMP
    assert persisted["duration_ms"] == 3000


@pytest.mark.parametrize("order", list(permutations(("started", "context", "completed"))))
@pytest.mark.parametrize("completion_has_timing", [True, False])
def test_turn_lifecycle_permutations_preserve_completion_and_context(
    store: TelemetryStore, order: tuple[str, ...], completion_has_timing: bool,
) -> None:
    records = {
        "started": TurnRecord(
            session_id="session-1", timestamp=TIMESTAMP, turn_id="turn-1",
            status="started", started_at=1791446400,
        ),
        "context": TurnRecord(
            session_id="session-1", timestamp="2026-10-08T08:00:02Z", turn_id="turn-1",
            model="gpt-6.1-sol", reasoning_effort="high", context_window=258400,
            workspace_roots=("/synthetic/project",),
        ),
        "completed": TurnRecord(
            session_id="session-1", timestamp="2026-10-08T08:00:01Z", turn_id="turn-1",
            status="completed", completed_at=1791446401 if completion_has_timing else None,
            duration_ms=1000 if completion_has_timing else None,
        ),
    }
    store.ingest([records[name] for name in order], CURSOR)
    persisted = row(store, "turns")
    assert persisted["status"] == "completed"
    assert persisted["observed_at"] == "2026-10-08T08:00:02+00:00"
    assert persisted["started_at"] == TIMESTAMP
    assert persisted["model"] == "gpt-6.1-sol"
    assert persisted["reasoning_effort"] == "high"
    assert persisted["context_window"] == 258400
    assert persisted["workspace_roots"] == '["/synthetic/project"]'
    assert persisted["completed_at"] == (
        "2026-10-08T08:00:01+00:00" if completion_has_timing else None
    )
    assert persisted["duration_ms"] == (1000 if completion_has_timing else None)


def test_reopening_updates_running_service_version(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path):
        pass
    monkeypatch.setattr(storage_module, "version", lambda _: "0.2.0")
    with TelemetryStore.open(path) as store:
        assert store.health_snapshot()["service_version"] == "0.2.0"


def test_existing_wal_and_shm_permissions_are_tightened(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode bits are not supported")
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as first:
        first.ingest([USAGE], CURSOR)
        wal = path.with_name(path.name + "-wal")
        shm = path.with_name(path.name + "-shm")
        wal.chmod(0o644)
        shm.chmod(0o644)
        with TelemetryStore.open(path):
            assert wal.stat().st_mode & 0o777 == 0o600
            assert shm.stat().st_mode & 0o777 == 0o600


def test_failed_migration_rolls_back_all_ddl(tmp_path: Path, monkeypatch) -> None:
    original_read_text = Path.read_text

    def invalid_schema(path, *args, **kwargs):
        if path.name == "schema.sql":
            return "CREATE TABLE migration_probe (id INTEGER); CREATE TABLE broken ("
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", invalid_schema)
    path = tmp_path / "telemetry.db"
    with pytest.raises(sqlite3.OperationalError):
        TelemetryStore.open(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
