"""Collect complete rollout lines through the content-free telemetry boundary."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from codex_tokenomics.storage import IngestCursor, TelemetryStore
from codex_tokenomics.telemetry import EventContext, SessionRecord, TelemetryRecord, normalize_event

ROLLOUT_ID = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl"
)


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Per-scan counts and sessions with normalized records, including replays."""

    files_seen: int
    records_inserted: int
    duplicates: int
    parse_failures: int
    changed_session_ids: frozenset[str]


class Collector:
    def __init__(self, session_root: Path, store: TelemetryStore) -> None:
        self.session_root = Path(session_root).resolve()
        self.store = store

    def scan_once(self) -> CollectionResult:
        """Rediscover rollouts and commit only normalized records and complete offsets."""
        files_seen = inserted = duplicates = parse_failures = 0
        changed_session_ids: set[str] = set()
        for path in sorted(self.session_root.rglob("*.jsonl")):
            if not path.is_file():
                continue
            file_key = str(path)
            match = ROLLOUT_ID.fullmatch(path.name)
            # Forked rollouts can retain their ancestor's session_meta.id.
            # The rollout filename still identifies the actual thread.
            thread_id = match.group(1) if match else None
            previous = self.store.get_cursor(file_key)
            records: list[TelemetryRecord] = []
            file_failures = 0
            try:
                stream = path.open("rb")
            except (FileNotFoundError, IsADirectoryError):
                continue
            with stream:
                identity = os.fstat(stream.fileno())
                resume = previous is not None and (
                    previous.device == identity.st_dev and previous.inode == identity.st_ino
                    and identity.st_size >= previous.offset
                )
                offset = previous.offset if resume else 0
                session_id = previous.session_id if resume else None
                stream.seek(offset)
                # Bound this pass to the opened file's snapshot; live appends wait for the next scan.
                while offset < identity.st_size:
                    line = stream.readline(identity.st_size - offset)
                    if not line or not line.endswith(b"\n"):
                        break
                    line_offset = offset
                    offset += len(line)
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (ValueError, RecursionError):
                        event = None
                    if not isinstance(event, dict):
                        file_failures += 1
                        continue
                    context = EventContext(
                        path, identity.st_dev, identity.st_ino, line_offset, session_id, thread_id,
                    )
                    normalized = normalize_event(event, context)
                    for record in normalized:
                        if isinstance(record, SessionRecord):
                            session_id = record.session_id
                        changed_session_ids.add(record.session_id)
                    records.extend(normalized)
            result = self.store.ingest(records, IngestCursor(
                file_key=file_key, source_path=path, device=identity.st_dev, inode=identity.st_ino,
                offset=offset, session_id=session_id,
                parse_failures=(previous.parse_failures if previous is not None else 0)
                + file_failures,
            ))
            files_seen += 1
            inserted += result.inserted
            duplicates += result.duplicates
            parse_failures += file_failures
        return CollectionResult(
            files_seen, inserted, duplicates, parse_failures, frozenset(changed_session_ids),
        )
