import json
import sqlite3
from pathlib import Path

import pytest

from codex_tokenomics.reindex import rebuild_attribution
from codex_tokenomics.storage import IngestCursor, TelemetryStore
from codex_tokenomics.telemetry import SessionRecord, UsageSample

TIMESTAMP = "2026-10-08T08:00:00+00:00"


def legacy_database(tmp_path: Path) -> tuple[Path, Path]:
    database, root = tmp_path / "telemetry.db", tmp_path / "sessions"
    root.mkdir()
    with TelemetryStore.open(database) as store:
        for thread, kind, tokens in (
            ("parent", "user", 100), ("child", "subagent", 50),
            ("guardian", "guardian_review", 25),
        ):
            rollout = root / f"{thread}.jsonl"
            events = (
                {"type": "session_meta", "timestamp": TIMESTAMP, "payload": {
                    "id": thread, "session_id": "parent", "source": "cli",
                    "thread_source": kind,
                    "parent_thread_id": "parent" if thread != "parent" else None,
                }},
                {"type": "token_usage_record", "timestamp": TIMESTAMP, "payload": {
                    "session_id": "parent", "thread_id": thread,
                    "response_id": f"r-{thread}",
                    "usage": {"input_tokens": tokens, "output_tokens": 0,
                              "total_tokens": tokens},
                }},
            )
            rollout.write_text("".join(json.dumps(event) + "\n" for event in events))
            identity = rollout.stat()
            store.ingest([
                SessionRecord(session_id="parent", timestamp=TIMESTAMP, thread_id=thread,
                              source="cli", agent_kind="guardian"),
                UsageSample(session_id="parent", timestamp=TIMESTAMP,
                            response_id=f"r-{thread}", thread_id=thread,
                            input_tokens=tokens, cached_input_tokens=0,
                            cache_write_input_tokens=0, output_tokens=0,
                            reasoning_output_tokens=0, total_tokens=tokens),
            ], IngestCursor(str(rollout), rollout, identity.st_dev, identity.st_ino,
                            identity.st_size, "parent"))
        store.open_incident(incident_id="historical", scope_type="session", scope_id="parent",
                            trigger="absolute", observed_rate=1000,
                            absolute_threshold=100, opened_at=TIMESTAMP)
        store.record_notification_attempt(incident_id="historical", channel="desktop",
                                          attempted_at=TIMESTAMP, attempt_number=1,
                                          outcome_code="sent")
    return database, root


def test_reindex_separates_agents_preserves_counters_history_and_backup(tmp_path: Path) -> None:
    database, root = legacy_database(tmp_path)
    report = rebuild_attribution(database, root)
    assert report["before_tokens"] == report["after_tokens"] == 175
    assert report["preserved_usage_records"] == 3
    with sqlite3.connect(database) as connection:
        assert dict(connection.execute(
            "SELECT s.agent_kind,SUM(u.total_tokens) FROM sessions s "
            "JOIN usage_samples u ON u.session_id=s.session_id GROUP BY s.agent_kind"
        )) == {"user": 100, "subagent": 50, "guardian": 25}
        assert connection.execute("SELECT COUNT(*) FROM alert_incidents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM notification_attempts").fetchone()[0] == 1
    with sqlite3.connect(report["backup"]) as backup:
        assert backup.execute("SELECT SUM(total_tokens) FROM usage_samples").fetchone()[0] == 175
        assert backup.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    repeated = rebuild_attribution(database, root)
    assert repeated["after_tokens"] == 175


def test_reindex_refuses_to_publish_changed_counters(tmp_path: Path) -> None:
    database, root = legacy_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE usage_samples SET total_tokens=999 WHERE response_id='r-child'")
    with pytest.raises(RuntimeError, match="usage reconciliation failed"):
        rebuild_attribution(database, root)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT SUM(total_tokens) FROM usage_samples").fetchone()[0] == 1124
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_reindex_refuses_missing_sources(tmp_path: Path) -> None:
    database, root = legacy_database(tmp_path)
    (root / "child.jsonl").rename(root / "child.moved")
    with pytest.raises(RuntimeError, match="source files are missing"):
        rebuild_attribution(database, root)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT SUM(total_tokens) FROM usage_samples").fetchone()[0] == 175
