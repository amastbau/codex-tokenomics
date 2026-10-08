import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from codex_tokenomics import collector as collector_module
from codex_tokenomics.collector import Collector
from codex_tokenomics.storage import TelemetryStore

TIMESTAMP = "2026-10-08T08:00:00+00:00"
SECRET = "PROHIBITED-CONTENT-9b84f1"


@pytest.fixture
def store(tmp_path: Path):
    with TelemetryStore.open(tmp_path / "private" / "telemetry.db") as opened:
        yield opened


def metadata(session_id: str, thread_source: str = "user") -> dict:
    return {
        "type": "session_meta", "timestamp": TIMESTAMP,
        "payload": {
            "id": session_id, "source": "cli", "thread_source": thread_source,
            "base_instructions": SECRET,
        },
    }


def usage(response_id: str, tokens: int, session_id: str | None = None,
          timestamp: str = TIMESTAMP) -> dict:
    payload = {
        "response_id": response_id,
        "usage": {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens},
        "message": SECRET,
    }
    if session_id is not None:
        payload["session_id"] = session_id
    return {"type": "token_usage_record", "timestamp": timestamp, "payload": payload}


def encoded(*events: dict) -> bytes:
    return b"".join(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
                    for event in events)


def write_rollout(path: Path, *events: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(*events))
    return path


def append_bytes(path: Path, data: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(data)


def cursor_row(store: TelemetryStore) -> dict:
    return dict(store.connection.execute("SELECT * FROM ingest_cursors").fetchone())


def test_discovers_user_subagent_and_guardian_files(tmp_path: Path, store: TelemetryStore) -> None:
    root = tmp_path / "sessions"
    for index, kind in enumerate(("user", "subagent", "guardian_review")):
        write_rollout(root / "2026" / "10" / "08" / kind / f"rollout-{index}.jsonl",
                      metadata(f"session-{index}", kind), usage(f"response-{index}", 10))
    (root / "ignored.txt").write_bytes(encoded(usage("ignored", 9000, "session-ignored")))

    result = Collector(root, store).scan_once()

    assert (result.files_seen, result.records_inserted, result.duplicates, result.parse_failures) == (
        3, 6, 0, 0,
    )
    assert result.changed_session_ids == {"session-0", "session-1", "session-2"}
    assert {row[0] for row in store.connection.execute("SELECT agent_kind FROM sessions")} == {
        "user", "subagent", "guardian",
    }
    assert store.total_tokens() == 30


def test_append_ingests_only_new_records_and_preserves_metadata_context(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    rollout = write_rollout(root / "rollout.jsonl", metadata("session-1"), usage("r1", 100))
    collector = Collector(root, store)
    assert collector.scan_once().records_inserted == 2
    idle = collector.scan_once()
    assert (idle.records_inserted, idle.duplicates, idle.changed_session_ids) == (0, 0, set())

    append_bytes(rollout, encoded(usage("r2", 50)))
    result = collector.scan_once()

    assert (result.records_inserted, result.duplicates) == (1, 0)
    assert result.changed_session_ids == {"session-1"}
    assert store.total_tokens() == 150
    assert cursor_row(store)["session_id"] == "session-1"
    assert cursor_row(store)["byte_offset"] == rollout.stat().st_size


def test_partial_line_is_reread_after_completion(tmp_path: Path, store: TelemetryStore) -> None:
    root = tmp_path / "sessions"
    complete = encoded(metadata("session-1"), usage("r1", 100))
    partial_event = usage("r2", 50)
    partial_event["payload"]["message"] = f"{SECRET} שלום"
    pending = encoded(partial_event)
    cut = pending.index("שלום".encode()) + 1
    rollout = write_rollout(root / "rollout.jsonl")
    rollout.write_bytes(complete + pending[:cut])
    collector = Collector(root, store)

    first = collector.scan_once()

    assert (first.records_inserted, first.parse_failures) == (2, 0)
    assert store.total_tokens() == 100
    assert cursor_row(store)["byte_offset"] == len(complete)
    append_bytes(rollout, pending[cut:])
    second = collector.scan_once()
    assert (second.records_inserted, second.parse_failures) == (1, 0)
    assert store.total_tokens() == 150
    assert cursor_row(store)["byte_offset"] == rollout.stat().st_size


def test_truncation_restarts_and_clears_stale_session_context(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    old_metadata = metadata("old-session")
    old_metadata["payload"]["base_instructions"] = SECRET * 20
    rollout = write_rollout(root / "rollout.jsonl", old_metadata, usage("r1", 100))
    collector = Collector(root, store)
    collector.scan_once()
    old_inode = rollout.stat().st_ino
    rollout.write_bytes(encoded(usage("r2", 50, "new-session"), usage("unattributed", 9000)))
    assert rollout.stat().st_ino == old_inode
    assert rollout.stat().st_size < cursor_row(store)["byte_offset"]

    result = collector.scan_once()

    assert (result.records_inserted, result.duplicates) == (1, 0)
    assert result.changed_session_ids == {"new-session"}
    assert store.total_tokens() == 150
    assert cursor_row(store)["session_id"] is None
    assert cursor_row(store)["byte_offset"] == rollout.stat().st_size


def test_replacement_larger_than_old_file_restarts_from_new_identity(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    rollout = write_rollout(root / "rollout.jsonl", metadata("old-session"), usage("r1", 100))
    collector = Collector(root, store)
    collector.scan_once()
    old_identity = cursor_row(store)
    replacement = write_rollout(root / "replacement.tmp", metadata("new-session"),
                                usage("r2", 50), usage("r3", 25))
    replacement.replace(rollout)
    assert rollout.stat().st_ino != old_identity["inode"]
    assert rollout.stat().st_size > old_identity["byte_offset"]

    result = collector.scan_once()

    assert (result.records_inserted, result.duplicates) == (3, 0)
    assert result.changed_session_ids == {"new-session"}
    assert store.total_tokens() == 175
    assert cursor_row(store)["session_id"] == "new-session"
    assert cursor_row(store)["inode"] == rollout.stat().st_ino


def test_changed_device_replays_from_start_without_double_counting(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    write_rollout(root / "rollout.jsonl", metadata("session-1"), usage("r1", 100))
    collector = Collector(root, store)
    collector.scan_once()
    cursor = store.get_cursor(cursor_row(store)["file_key"])
    store.ingest([], replace(cursor, device=cursor.device + 1))

    result = collector.scan_once()

    assert (result.records_inserted, result.duplicates) == (0, 2)
    assert store.total_tokens() == 100


def test_new_date_and_hidden_directories_are_discovered_after_start(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    collector = Collector(root, store)
    assert collector.scan_once().files_seen == 0
    write_rollout(root / "2026" / "10" / "09" / ".internal" / "rollout.jsonl",
                  usage("new", 77, "new-session"))
    (root / "directory.jsonl").mkdir()

    result = collector.scan_once()

    assert (result.files_seen, result.records_inserted) == (1, 1)
    assert store.total_tokens() == 77


def test_late_timestamp_in_appended_record_is_collected(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    rollout = write_rollout(root / "rollout.jsonl", usage("recent", 100, "session-1"))
    collector = Collector(root, store)
    collector.scan_once()
    append_bytes(rollout, encoded(usage("late", 50, "session-1", "2026-10-07T01:00:00Z")))

    assert collector.scan_once().records_inserted == 1
    assert store.total_tokens() == 150
    assert [sample.response_id for sample in store.usage_samples(
        "2026-10-07T00:00:00Z", "2026-10-09T00:00:00Z",
    )] == ["late", "recent"]


def test_restart_restores_complete_offset_and_metadata_context(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    database = tmp_path / "private" / "telemetry.db"
    rollout = write_rollout(root / "rollout.jsonl", metadata("session-1"), usage("r1", 100))
    pending = encoded(usage("r2", 50))
    append_bytes(rollout, pending[:-1])
    with TelemetryStore.open(database) as store:
        Collector(root, store).scan_once()
        assert store.total_tokens() == 100
    append_bytes(rollout, b"\n")

    with TelemetryStore.open(database) as store:
        result = Collector(root, store).scan_once()
        assert (result.records_inserted, result.duplicates) == (1, 0)
        assert store.total_tokens() == 150
        assert cursor_row(store)["session_id"] == "session-1"


@pytest.mark.parametrize("bad_line", [
    f'{{"message":"{SECRET}",}}\n'.encode(), b"\xff\n", b"[1, 2]\n", b"\n",
], ids=["invalid-json", "invalid-utf8", "non-object", "blank"])
def test_malformed_complete_line_advances_cursor_with_content_free_counter(
    tmp_path: Path, store: TelemetryStore, bad_line: bytes, capsys, caplog,
) -> None:
    root = tmp_path / "sessions"
    complete = encoded(metadata("session-1"), usage("r1", 100)) + bad_line
    rollout = write_rollout(root / "rollout.jsonl")
    rollout.write_bytes(complete + SECRET.encode())
    collector = Collector(root, store)

    first = collector.scan_once()

    assert (first.records_inserted, first.parse_failures) == (2, 1)
    assert store.total_tokens() == 100
    assert cursor_row(store)["byte_offset"] == len(complete)
    assert cursor_row(store)["parse_failures"] == 1
    assert cursor_row(store)["updated_at"] is not None
    assert store.health_snapshot()["parse_failures"] == 1
    idle = Collector(root, store).scan_once()
    assert (idle.records_inserted, idle.parse_failures) == (0, 0)
    assert cursor_row(store)["parse_failures"] == 1
    append_bytes(rollout, b"\n")
    third = collector.scan_once()
    assert third.parse_failures == 1
    assert cursor_row(store)["parse_failures"] == 2
    assert SECRET not in "\n".join(store.connection.iterdump())
    assert SECRET not in repr((first, idle, third, store.health_snapshot()))
    for persisted in store.path.parent.iterdir():
        assert SECRET.encode() not in persisted.read_bytes()
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err + caplog.text


def test_parse_counter_survives_replacement(tmp_path: Path, store: TelemetryStore) -> None:
    root = tmp_path / "sessions"
    rollout = write_rollout(root / "rollout.jsonl")
    rollout.write_bytes(b"not-json\n")
    collector = Collector(root, store)
    assert collector.scan_once().parse_failures == 1
    replacement = write_rollout(root / "replacement.tmp")
    replacement.write_bytes(b"still-not-json\n")
    replacement.replace(rollout)

    assert collector.scan_once().parse_failures == 1
    assert cursor_row(store)["parse_failures"] == 2


def test_usage_before_metadata_enriches_placeholder_and_sets_later_context(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    write_rollout(root / "rollout.jsonl", usage("early", 100, "session-1"),
                  metadata("session-1", "subagent"), usage("later", 50))

    result = Collector(root, store).scan_once()

    assert (result.records_inserted, result.duplicates) == (2, 1)
    assert store.total_tokens() == 150
    assert store.connection.execute("SELECT agent_kind FROM sessions").fetchone()[0] == "subagent"
    assert cursor_row(store)["session_id"] == "session-1"


def test_mixed_fixture_persists_all_telemetry_without_content(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    rollout = write_rollout(root / "rollout.jsonl")
    rollout.write_bytes((Path(__file__).parent / "fixtures" / "mixed-session.jsonl").read_bytes())

    result = Collector(root, store).scan_once()

    assert (result.records_inserted, result.duplicates, result.parse_failures) == (6, 2, 0)
    counts = store.table_counts()
    assert (counts["usage_samples"], counts["rate_limit_samples"], counts["tool_events"],
            counts["turns"]) == (1, 2, 1, 1)
    assert store.total_tokens() == 1337
    assert SECRET not in "\n".join(store.connection.iterdump())


def test_failed_atomic_ingest_can_be_retried_without_losing_complete_lines(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    write_rollout(root / "rollout.jsonl", metadata("session-1"), usage("r1", 100))
    collector = Collector(root, store)
    store.connection.execute(
        "CREATE TRIGGER reject_cursor BEFORE INSERT ON ingest_cursors "
        "BEGIN SELECT RAISE(ABORT, 'test rejection'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="test rejection"):
        collector.scan_once()

    assert store.total_tokens() == 0
    assert store.table_counts()["ingest_cursors"] == 0
    store.connection.execute("DROP TRIGGER reject_cursor")
    assert collector.scan_once().records_inserted == 2
    assert store.total_tokens() == 100


@pytest.mark.parametrize("becomes_directory", [False, True], ids=["removed", "directory"])
def test_file_changed_during_discovery_does_not_prevent_other_ingestion(
    tmp_path: Path, store: TelemetryStore, monkeypatch, becomes_directory: bool,
) -> None:
    root = tmp_path / "sessions"
    removed = write_rollout(root / "a-removed.jsonl", usage("gone", 9000, "gone-session"))
    write_rollout(root / "b-active.jsonl", usage("active", 77, "active-session"))
    original_open = Path.open

    def disappearing_open(path, *args, **kwargs):
        if path == removed:
            path.unlink()
            if becomes_directory:
                path.mkdir()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disappearing_open)

    result = Collector(root, store).scan_once()

    assert (result.files_seen, result.records_inserted) == (1, 1)
    assert store.total_tokens() == 77


def test_replayed_response_is_a_duplicate_without_changing_totals(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    rollout = write_rollout(root / "rollout.jsonl", metadata("session-1"), usage("r1", 100))
    collector = Collector(root, store)
    collector.scan_once()
    append_bytes(rollout, encoded(usage("r1", 9000)))

    result = collector.scan_once()

    assert (result.records_inserted, result.duplicates) == (0, 1)
    assert store.total_tokens() == 100


def test_distinct_tool_offsets_keep_idless_events_separate(
    tmp_path: Path, store: TelemetryStore,
) -> None:
    root = tmp_path / "sessions"
    tool = {
        "type": "event_msg", "timestamp": TIMESTAMP,
        "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "status": "completed", "command": [SECRET],
        }},
    }
    rollout = write_rollout(root / "rollout.jsonl", metadata("session-1"), tool)
    collector = Collector(root, store)
    collector.scan_once()
    append_bytes(rollout, encoded(tool))

    assert collector.scan_once().records_inserted == 1
    assert Collector(root, store).scan_once().duplicates == 0
    assert store.table_counts()["tool_events"] == 2


@pytest.mark.parametrize("bad_line", [
    b'{"nested":' + b"[" * 2000 + b"0\n",
    b'{"huge_integer":' + b"1" * 5000 + b"}\n",
], ids=["deeply-nested", "oversized-integer"])
def test_malformed_nested_or_oversized_json_does_not_stop_collection(
    tmp_path: Path, store: TelemetryStore, bad_line: bytes,
) -> None:
    root = tmp_path / "sessions"
    rollout = write_rollout(root / "rollout.jsonl")
    rollout.write_bytes(bad_line + encoded(usage("r1", 77, "session-1")))

    result = Collector(root, store).scan_once()

    assert (result.records_inserted, result.parse_failures) == (1, 1)
    assert store.total_tokens() == 77
    assert cursor_row(store)["byte_offset"] == rollout.stat().st_size


def test_scan_defers_new_bytes_appended_after_its_file_snapshot(
    tmp_path: Path, store: TelemetryStore, monkeypatch,
) -> None:
    root = tmp_path / "sessions"
    rollout = write_rollout(root / "rollout.jsonl", metadata("session-1"), usage("r1", 100))
    initial_size = rollout.stat().st_size
    original_normalize = collector_module.normalize_event

    def append_while_normalizing(event, context):
        if event.get("type") == "session_meta":
            append_bytes(rollout, encoded(usage("r2", 50)))
        return original_normalize(event, context)

    monkeypatch.setattr(collector_module, "normalize_event", append_while_normalizing)
    collector = Collector(root, store)

    first = collector.scan_once()

    assert first.records_inserted == 2
    assert store.total_tokens() == 100
    assert cursor_row(store)["byte_offset"] == initial_size
    assert collector.scan_once().records_inserted == 1
    assert store.total_tokens() == 150
