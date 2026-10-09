"""Backed-up attribution repair through the existing content-free collector.

Stop the collector service before running this module, then restart it afterward.
No rollout content is printed or persisted outside the collector's allowlist.
Historical incidents and notification outcomes remain unchanged.
"""

import argparse
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from codex_tokenomics.collector import Collector
from codex_tokenomics.storage import TelemetryStore

COUNTERS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "cumulative_total_tokens",
)


def rebuild_attribution(database: Path, session_root: Path) -> dict[str, object]:
    """Rebuild in isolation; publish only after every old usage counter reconciles."""
    database, session_root = Path(database).resolve(), Path(session_root).resolve()
    backup_path = database.with_name(f"telemetry.before-thread-fix-{uuid.uuid4().hex}.db")
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    with (
        closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as live,
        closing(sqlite3.connect(backup_path)) as backup,
    ):
        live.backup(backup)

    previous = sqlite3.connect(backup_path.as_uri() + "?mode=ro", uri=True)
    previous.row_factory = sqlite3.Row
    try:
        source_paths = previous.execute("SELECT source_path FROM ingest_cursors").fetchall()
        if any(not Path(row[0]).is_file() for row in source_paths):
            raise RuntimeError("source files are missing; live database was not changed")
        old_count, old_tokens = previous.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_tokens),0) FROM usage_samples"
        ).fetchone()
        with (
            tempfile.TemporaryDirectory(prefix="tokenomics-reindex-", dir=database.parent) as tmp,
            TelemetryStore.open(Path(tmp) / "telemetry.db") as staged,
        ):
            result = Collector(session_root, staged).scan_once()
            if staged.connection.execute(
                "SELECT COUNT(*) FROM usage_samples WHERE session_id!=thread_id"
            ).fetchone()[0]:
                raise RuntimeError("thread attribution is inconsistent; live database was not changed")
            preserved = 0
            for old in previous.execute("SELECT * FROM usage_samples"):
                current = staged.connection.execute(
                    "SELECT * FROM usage_samples WHERE session_id=? AND response_id=?",
                    (old["thread_id"], old["response_id"]),
                ).fetchone()
                if current is None:
                    # Older records may lack a thread_id and fall back to the
                    # parent. A globally unique response still reconciles.
                    candidates = staged.connection.execute(
                        "SELECT * FROM usage_samples WHERE response_id=? LIMIT 2",
                        (old["response_id"],),
                    ).fetchall()
                    current = candidates[0] if len(candidates) == 1 else None
                if current is None or any(current[key] != old[key] for key in COUNTERS):
                    raise RuntimeError(
                        "usage reconciliation failed; live database was not changed"
                    )
                preserved += 1
            for table in ("alert_incidents", "notification_attempts"):
                columns = [row[1] for row in previous.execute(f"PRAGMA table_info({table})")]
                placeholders = ",".join("?" for _ in columns)
                staged.connection.executemany(
                    f"INSERT INTO {table} VALUES ({placeholders})",
                    (tuple(row) for row in previous.execute(f"SELECT * FROM {table}")),
                )
            failures = previous.execute(
                "SELECT notification_failures FROM service_health WHERE singleton=1"
            ).fetchone()[0]
            staged.connection.execute(
                "UPDATE service_health SET notification_failures=? WHERE singleton=1",
                (failures,),
            )
            staged.connection.commit()
            if staged.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("rebuilt database failed its integrity check")
            if staged.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("rebuilt database failed its foreign key check")
            new_count, new_tokens = staged.connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_tokens),0) FROM usage_samples"
            ).fetchone()
            with closing(sqlite3.connect(database)) as target:
                if tuple(target.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_tokens),0) FROM usage_samples"
                ).fetchone()) != (old_count, old_tokens):
                    raise RuntimeError("collector is still writing; live database was not changed")
                staged.connection.backup(target)
            return {
                "backup": str(backup_path), "preserved_usage_records": preserved,
                "before_usage_records": old_count, "after_usage_records": new_count,
                "before_tokens": old_tokens, "after_tokens": new_tokens,
                "files_seen": result.files_seen, "parse_failures": result.parse_failures,
            }
    finally:
        previous.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--session-root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(rebuild_attribution(args.database, args.session_root), sort_keys=True))


if __name__ == "__main__":
    main()
