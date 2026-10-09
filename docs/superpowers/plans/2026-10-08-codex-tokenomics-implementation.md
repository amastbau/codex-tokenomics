# Codex Tokenomics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and install a local-only telemetry service that monitors every local Codex session and delegated agent, detects configurable token spikes, alerts through GNOME and Gmail, and answers read-only questions through a Codex skill.

**Architecture:** A Python user service incrementally tails Codex JSONL rollouts, normalizes only allowlisted non-content telemetry, and commits it with ingestion cursors to SQLite. Separate detector, notification, query, and installation modules consume typed interfaces so privacy, deduplication, and read-only behavior can be tested independently.

**Tech Stack:** Python 3.12+ standard library, SQLite, TOML, pytest, Ruff, `uv`, systemd user services, `notify-send`, Google Workspace `gws`, and a local Codex skill.

**Spec:** `docs/superpowers/specs/2026-10-08-codex-tokenomics-design.md`

## Global Constraints

- Monitor only `~/.codex/sessions/**/*.jsonl`; do not monitor Codex Cloud-only tasks.
- Import user, subagent, and guardian/review sessions, including historical records and sessions created after startup.
- Never persist or emit prompts, assistant responses, reasoning text, summaries, base instructions, world state, raw errors, tool arguments, tool output, command strings/output, or raw JSON.
- Build normalized records from an explicit field allowlist; ignore unknown fields.
- Store runtime data locally with directories mode `0700` and configuration/database mode `0600` where supported.
- Require every detector threshold, window, poll interval, recovery period, and email retry delay in validated configuration; application code has no silent detector defaults.
- Count unique per-response usage exactly once and treat cumulative counters only as reconciliation data.
- Detect independently per session and across all local sessions; either absolute or relative detection can open an incident.
- Historical reconciliation never sends alerts; only records appended after the live boundary can alert.
- Send one GNOME alert and one email attempt at incident opening; GNOME also reports recovery; email retries are bounded.
- Send email only to the configured recipient, initially `amastbau@redhat.com`, through the already authenticated `gws` CLI.
- Every generated email contains `Automated by Codex Tokenomics; implementation assisted by Codex`.
- Query connections are read-only, bounded, and incapable of changing SQLite or attaching another database.
- Tests never read the real Codex sessions directory or contact GNOME, Gmail, or GitHub.
- Develop on feature branches and deliver changes through a pull request; never push directly to `main`.

## Review Focus

- A rollout file can end with a partial JSON line, be truncated, or be replaced at the same path; the collector must resume without storing content, losing complete records, or creating negative usage.
- The same response can appear in repeated counters or be encountered after restart; uniqueness constraints must prevent double-counting and repeated alerts.
- Prohibited sentinel content can occur at any nesting depth in messages, tool payloads, errors, and world state; it must never appear in SQLite, logs, alerts, or query output.
- Event timestamps can be late or out of order; configured windows must remain deterministic and must not generate negative rates or shorten the recovery interval.
- A read-only SQL request can attempt multiple statements, `ATTACH`, unsafe pragmas, writes inside a CTE, or an unbounded recursive query; every case must fail safely or hit configured execution limits.

## Planned File Structure

```text
pyproject.toml                         package metadata, commands, dev dependencies
config.example.toml                   complete explicit runtime configuration
src/codex_tokenomics/__init__.py      package version
src/codex_tokenomics/config.py        typed TOML loading and validation
src/codex_tokenomics/telemetry.py     allowlisted telemetry types and normalization
src/codex_tokenomics/storage.py       SQLite schema, migrations, transactions, queries
src/codex_tokenomics/collector.py     rollout discovery and incremental JSONL ingestion
src/codex_tokenomics/detector.py      absolute/relative rates and incident state machine
src/codex_tokenomics/notifiers.py     GNOME/Gmail rendering, delivery, and retries
src/codex_tokenomics/service.py       reconciliation and live polling orchestration
src/codex_tokenomics/query.py         reports and sandboxed read-only SQL
src/codex_tokenomics/cli.py           command-line interface
src/codex_tokenomics/installer.py     per-user installation and removal
src/codex_tokenomics/schema.sql       initial normalized SQLite schema
src/codex_tokenomics/resources/       systemd unit and Codex skill templates
tests/fixtures/                        synthetic content-bearing rollout fixtures
tests/test_*.py                        focused unit/integration tests
README.md                              installation, operation, queries, and security
```

---

### Task 1: Package and Strict Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `config.example.toml`
- Create: `src/codex_tokenomics/__init__.py`
- Create: `src/codex_tokenomics/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: TOML bytes from an explicit `Path`.
- Produces: `load_config(path: Path) -> AppConfig`, `validate_config(data: Mapping[str, object]) -> AppConfig`, and immutable `AppConfig`, `PathsConfig`, `CollectorConfig`, `DetectorConfig`, `NotificationConfig`, and `QueryConfig` dataclasses.

- [ ] **Step 1: Create package metadata and write failing configuration tests**

```python
def test_valid_config_loads_every_required_detector_value(tmp_path: Path) -> None:
    path = write_config(tmp_path, VALID_CONFIG)
    config = load_config(path)
    assert config.detector.session_absolute_tokens_per_minute == 250_000
    assert config.detector.aggregate_absolute_tokens_per_minute == 1_000_000
    assert config.notifications.email_recipient == "amastbau@redhat.com"


@pytest.mark.parametrize(
    "missing_key",
    [
        "session_absolute_tokens_per_minute",
        "aggregate_absolute_tokens_per_minute",
        "relative_multiplier",
        "relative_minimum_tokens_per_minute",
        "rate_window_seconds",
        "baseline_window_seconds",
        "minimum_baseline_buckets",
        "recovery_seconds",
    ],
)
def test_missing_detector_value_is_rejected(tmp_path: Path, missing_key: str) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    del data["detector"][missing_key]
    with pytest.raises(ConfigError, match=missing_key):
        validate_config(data)


def test_unknown_key_is_rejected() -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data["detector"]["hardcoded_escape_hatch"] = 1
    with pytest.raises(ConfigError, match="unknown key"):
        validate_config(data)
```

- [ ] **Step 2: Run the configuration tests and verify RED**

Run: `uv run pytest tests/test_config.py -v`

Expected: FAIL because `codex_tokenomics.config` does not exist.

- [ ] **Step 3: Implement immutable configuration types and strict parsing**

```python
@dataclass(frozen=True, slots=True)
class DetectorConfig:
    session_absolute_tokens_per_minute: int
    aggregate_absolute_tokens_per_minute: int
    relative_multiplier: float
    relative_minimum_tokens_per_minute: int
    rate_window_seconds: int
    baseline_window_seconds: int
    minimum_baseline_buckets: int
    recovery_seconds: int


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as stream:
        return validate_config(tomllib.load(stream))
```

Validate exact section/key sets, positive numeric values, `baseline_window_seconds >= rate_window_seconds * minimum_baseline_buckets`, absolute paths after `expanduser().resolve()`, a non-empty email recipient, non-empty retry delays, and `poll_interval_seconds > 0`. Put reviewed initial values only in `config.example.toml`; do not place them in Python defaults.

Use this complete configuration shape:

```toml
[paths]
session_root = "~/.codex/sessions"
database = "~/.local/share/codex-tokenomics/telemetry.db"

[collector]
poll_interval_seconds = 2

[detector]
session_absolute_tokens_per_minute = 250000
aggregate_absolute_tokens_per_minute = 1000000
relative_multiplier = 3.0
relative_minimum_tokens_per_minute = 50000
rate_window_seconds = 60
baseline_window_seconds = 900
minimum_baseline_buckets = 5
recovery_seconds = 300

[notifications]
email_recipient = "amastbau@redhat.com"
email_retry_delays_seconds = [10, 30]

[query]
row_limit = 1000
timeout_ms = 2000
```

Use this package configuration, adjusting only the version as releases are cut:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "codex-tokenomics"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
codex-tokenomics = "codex_tokenomics.cli:main"

[dependency-groups]
dev = ["pytest>=8.0", "ruff>=0.14"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.hatch.build.targets.wheel]
packages = ["src/codex_tokenomics"]
```

- [ ] **Step 4: Run configuration tests and lint**

Run: `uv run pytest tests/test_config.py -v && uv run ruff check src tests`

Expected: PASS with no warnings.

- [ ] **Step 5: Commit the configuration foundation**

```bash
git add pyproject.toml config.example.toml src/codex_tokenomics/__init__.py src/codex_tokenomics/config.py tests/test_config.py
git commit -m "feat: add strict token monitor configuration" -m "Assisted-by: Codex"
```

### Task 2: Allowlisted Telemetry Normalization

**Files:**
- Create: `src/codex_tokenomics/telemetry.py`
- Create: `tests/fixtures/mixed-session.jsonl`
- Create: `tests/test_telemetry.py`

**Interfaces:**
- Consumes: `normalize_event(event: Mapping[str, object], context: EventContext) -> tuple[TelemetryRecord, ...]`, where `EventContext` contains only `source_path`, `device`, `inode`, `byte_offset`, and the content-free `session_id` already discovered for that file.
- Produces: frozen `SessionRecord`, `TurnRecord`, `UsageSample`, `RateLimitSample`, and `ToolEvent` dataclasses; `TelemetryRecord` is their tagged union.

- [ ] **Step 1: Write failing tests for supported records and privacy**

```python
SECRET = "PROHIBITED-CONTENT-9b84f1"


def test_token_usage_record_normalizes_only_numeric_telemetry() -> None:
    event = token_usage_event(secret=SECRET)
    records = normalize_event(event, EVENT_CONTEXT)
    usage = next(record for record in records if isinstance(record, UsageSample))
    assert usage.response_id == "response-1"
    assert usage.total_tokens == 1_337
    assert usage.cached_input_tokens == 900


def test_nested_prohibited_content_never_survives_normalization() -> None:
    records = normalize_event(event_with_secret_in_every_payload(SECRET), EVENT_CONTEXT)
    serialized = json.dumps([asdict(record) for record in records], sort_keys=True)
    assert SECRET not in serialized
    assert "arguments" not in serialized
    assert "output" not in serialized


def test_unknown_event_type_is_ignored() -> None:
    assert normalize_event({"type": "future_content_event", "payload": {"text": SECRET}}, EVENT_CONTEXT) == ()


def test_session_turn_rate_limit_and_tool_metadata_are_allowlisted() -> None:
    records = normalize_fixture("mixed-session.jsonl")
    session = next(record for record in records if isinstance(record, SessionRecord))
    turn = next(record for record in records if isinstance(record, TurnRecord))
    limit = next(record for record in records if isinstance(record, RateLimitSample))
    tool = next(record for record in records if isinstance(record, ToolEvent))
    assert (session.agent_kind, turn.model) == ("subagent", "gpt-6.1-sol")
    assert limit.limit_name == "tokens"
    assert (tool.name, tool.status) == ("exec_command", "completed")
    assert SECRET not in json.dumps([asdict(item) for item in records])
```

- [ ] **Step 2: Run normalization tests and verify RED**

Run: `uv run pytest tests/test_telemetry.py -v`

Expected: FAIL because the telemetry types and normalizer are absent.

- [ ] **Step 3: Implement explicit event handlers**

```python
HANDLERS: dict[str, Callable[[Mapping[str, object], EventContext], tuple[TelemetryRecord, ...]]] = {
    "session_meta": _normalize_session,
    "turn_context": _normalize_turn,
    "token_usage_record": _normalize_usage,
    "event_msg": _normalize_event_message,
}


def normalize_event(event: Mapping[str, object], context: EventContext) -> tuple[TelemetryRecord, ...]:
    event_type = event.get("type")
    handler = HANDLERS.get(event_type) if isinstance(event_type, str) else None
    return handler(event, context) if handler else ()
```

Read only named scalar fields. For `event_msg`, accept `task_started`, `task_complete`, `token_count`, and `item_completed`; extract timing, rate limits, and tool name/status only. Derive missing tool event IDs as a SHA-256 digest of file identity and byte offset, never payload content. Ignore messages, reasoning, custom tool input/output, world state, raw errors, and base instructions.

- [ ] **Step 4: Run normalization and privacy tests**

Run: `uv run pytest tests/test_telemetry.py -v && uv run ruff check src tests`

Expected: PASS; the sentinel is absent from serialized normalized records.

- [ ] **Step 5: Commit telemetry normalization**

```bash
git add src/codex_tokenomics/telemetry.py tests/fixtures/mixed-session.jsonl tests/test_telemetry.py
git commit -m "feat: normalize content-free Codex telemetry" -m "Assisted-by: Codex"
```

### Task 3: Transactional SQLite Store

**Files:**
- Create: `src/codex_tokenomics/schema.sql`
- Create: `src/codex_tokenomics/storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Consumes: `TelemetryStore.ingest(records: Sequence[TelemetryRecord], cursor: IngestCursor) -> IngestResult`.
- Produces: `TelemetryStore.open(path: Path)`, `get_cursor(file_key: str)`, `usage_samples(start, end, session_id=None)`, `open_incident`, `recover_incident`, `record_notification_attempt`, and `health_snapshot`; immutable `IngestCursor` stores file identity, last complete-line offset, and discovered session ID; `IngestResult` reports inserted/duplicate counts.

- [ ] **Step 1: Write failing migration, idempotency, and privacy tests**

```python
def test_ingest_commits_records_and_cursor_atomically(store: TelemetryStore) -> None:
    result = store.ingest([USAGE_SAMPLE], CURSOR_AT_200)
    assert result.inserted == 1
    assert store.get_cursor(CURSOR_AT_200.file_key).offset == 200


def test_duplicate_response_is_counted_once(store: TelemetryStore) -> None:
    store.ingest([USAGE_SAMPLE], CURSOR_AT_200)
    result = store.ingest([USAGE_SAMPLE], CURSOR_AT_300)
    assert result.duplicates == 1
    assert store.total_tokens(session_id="session-1") == USAGE_SAMPLE.total_tokens


def test_database_never_contains_prohibited_sentinel(store: TelemetryStore) -> None:
    store.ingest(normalized_fixture_records(), CURSOR_AT_EOF)
    assert SECRET.encode() not in store.path.read_bytes()
```

- [ ] **Step 2: Run storage tests and verify RED**

Run: `uv run pytest tests/test_storage.py -v`

Expected: FAIL because `TelemetryStore` does not exist.

- [ ] **Step 3: Implement schema and transactional ingestion**

```python
class TelemetryStore:
    def ingest(self, records: Sequence[TelemetryRecord], cursor: IngestCursor) -> IngestResult:
        with self.connection:
            inserted, duplicates = self._insert_records(records)
            self._upsert_cursor(cursor)
        return IngestResult(inserted=inserted, duplicates=duplicates)
```

Create the spec tables with foreign keys and uniqueness on `(session_id, response_id)` for usage and stable event IDs for tools. Enable `foreign_keys=ON`, `journal_mode=WAL`, and `synchronous=NORMAL`. Apply schema migrations inside an exclusive transaction. Create parent directories mode `0700` and database mode `0600`.

The schema must use these identities, telemetry columns, and foreign-key relationships:

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    cli_version TEXT,
    model_provider TEXT,
    cwd TEXT,
    context_window INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    root_turn_id TEXT,
    model TEXT,
    reasoning_effort TEXT,
    collaboration_mode TEXT,
    sandbox_mode TEXT,
    approval_mode TEXT,
    started_at TEXT,
    completed_at TEXT,
    duration_ms INTEGER,
    time_to_first_token_ms INTEGER
);
CREATE TABLE responses (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    response_id TEXT NOT NULL,
    turn_id TEXT,
    status TEXT,
    started_at TEXT,
    completed_at TEXT,
    duration_ms INTEGER,
    time_to_first_token_ms INTEGER,
    PRIMARY KEY (session_id, response_id)
);
CREATE TABLE usage_samples (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    response_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    turn_id TEXT,
    observed_at TEXT NOT NULL,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL CHECK (cached_input_tokens >= 0),
    cache_write_input_tokens INTEGER NOT NULL CHECK (cache_write_input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_output_tokens INTEGER NOT NULL CHECK (reasoning_output_tokens >= 0),
    total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
    cumulative_total_tokens INTEGER,
    PRIMARY KEY (session_id, response_id)
);
CREATE TABLE rate_limit_samples (
    sample_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    observed_at TEXT NOT NULL,
    limit_name TEXT NOT NULL,
    used_value REAL,
    limit_value REAL,
    window_seconds INTEGER,
    resets_at TEXT
);
CREATE TABLE tool_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_id TEXT,
    tool_name TEXT NOT NULL,
    tool_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_ms INTEGER,
    completed_at_ms INTEGER
);
CREATE TABLE alert_incidents (
    incident_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('session', 'aggregate')),
    scope_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('absolute', 'relative', 'absolute+relative')),
    observed_rate REAL NOT NULL,
    baseline_rate REAL,
    absolute_threshold REAL NOT NULL,
    opened_at TEXT NOT NULL,
    below_since TEXT,
    recovered_at TEXT
);
CREATE TABLE notification_attempts (
    attempt_id INTEGER PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES alert_incidents(incident_id),
    channel TEXT NOT NULL CHECK (channel IN ('desktop', 'email')),
    attempted_at TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    outcome_code TEXT NOT NULL,
    UNIQUE (incident_id, channel, attempt_number)
);
CREATE TABLE ingest_cursors (
    file_key TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    session_id TEXT,
    byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
    parse_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE service_health (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    last_poll_at TEXT,
    last_success_at TEXT,
    files_seen INTEGER NOT NULL DEFAULT 0,
    parse_failures INTEGER NOT NULL DEFAULT 0,
    notification_failures INTEGER NOT NULL DEFAULT 0,
    service_version TEXT NOT NULL
);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

- [ ] **Step 4: Add the Review Focus restart/deduplication test**

```python
def test_reopen_and_reingest_does_not_change_totals(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as first:
        first.ingest([USAGE_SAMPLE], CURSOR_AT_200)
    with TelemetryStore.open(path) as second:
        second.ingest([USAGE_SAMPLE], CURSOR_AT_300)
        assert second.total_tokens(session_id="session-1") == USAGE_SAMPLE.total_tokens


def test_maintenance_never_deletes_telemetry(store: TelemetryStore) -> None:
    store.ingest([USAGE_SAMPLE], CURSOR_AT_200)
    before = store.table_counts()
    store.maintain()
    assert store.table_counts() == before
    assert store.integrity_check() == "ok"
```

- [ ] **Step 5: Run store tests and commit**

Run: `uv run pytest tests/test_storage.py -v && uv run ruff check src tests`

Expected: PASS.

```bash
git add src/codex_tokenomics/schema.sql src/codex_tokenomics/storage.py tests/test_storage.py
git commit -m "feat: persist deduplicated telemetry in SQLite" -m "Assisted-by: Codex"
```

### Task 4: Historical and Live Rollout Collector

**Files:**
- Create: `src/codex_tokenomics/collector.py`
- Create: `tests/test_collector.py`

**Interfaces:**
- Consumes: `Collector(session_root: Path, store: TelemetryStore)` and `scan_once() -> CollectionResult`.
- Produces: discovery of all `*.jsonl`, complete-line incremental ingestion, cursor recovery, and `CollectionResult(files_seen, records_inserted, duplicates, parse_failures, changed_session_ids)`.

- [ ] **Step 1: Write failing discovery, append, and partial-line tests**

```python
def test_discovers_user_subagent_and_guardian_files(tmp_path: Path, store: TelemetryStore) -> None:
    session_root = build_session_tree(tmp_path, kinds=("user", "subagent", "guardian_review"))
    result = Collector(session_root, store).scan_once()
    assert result.files_seen == 3
    assert store.agent_kinds() == {"user", "subagent", "guardian_review"}


def test_partial_line_is_re_read_after_completion(tmp_path: Path, store: TelemetryStore) -> None:
    rollout = write_bytes(tmp_path / "rollout.jsonl", COMPLETE_EVENT + PARTIAL_EVENT_PREFIX)
    collector = Collector(tmp_path, store)
    collector.scan_once()
    assert store.usage_count() == 1
    append_bytes(rollout, PARTIAL_EVENT_SUFFIX + b"\n")
    collector.scan_once()
    assert store.usage_count() == 2
```

- [ ] **Step 2: Run collector tests and verify RED**

Run: `uv run pytest tests/test_collector.py -v`

Expected: FAIL because `Collector` is missing.

- [ ] **Step 3: Implement path discovery and complete-line parsing**

```python
def _complete_lines(stream: BinaryIO, offset: int) -> tuple[list[tuple[int, bytes]], int]:
    stream.seek(offset)
    data = stream.read()
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return [], offset
    complete = data[: last_newline + 1]
    lines = [(offset + start, line) for start, line in _split_with_offsets(complete)]
    return lines, offset + len(complete)
```

Keep the cursor at the last complete newline so partial content is never persisted. Compare device/inode and size to detect replacement/truncation. On malformed complete JSON, store only file identity, offset, timestamp, and an incremented parse counter; never store the line or exception message.

- [ ] **Step 4: Add truncation, replacement, late timestamp, and new-directory tests**

```python
def test_truncated_file_restarts_without_negative_usage(tmp_path: Path, store: TelemetryStore) -> None:
    rollout = write_rollout(tmp_path, [usage_event("r1", 100)])
    collector = Collector(tmp_path, store)
    collector.scan_once()
    rollout.write_text(json.dumps(usage_event("r2", 50)) + "\n")
    collector.scan_once()
    assert store.total_tokens() == 150


def test_new_date_directory_is_discovered_after_start(tmp_path: Path, store: TelemetryStore) -> None:
    collector = Collector(tmp_path, store)
    collector.scan_once()
    write_dated_rollout(tmp_path, "2026/10/09", usage_event("new", 77))
    assert collector.scan_once().records_inserted == 1
```

- [ ] **Step 5: Run collector tests and commit**

Run: `uv run pytest tests/test_collector.py -v && uv run ruff check src tests`

Expected: PASS.

```bash
git add src/codex_tokenomics/collector.py tests/test_collector.py
git commit -m "feat: collect historical and live Codex rollouts" -m "Assisted-by: Codex"
```

### Task 5: Configurable Spike Detection and Incident State

**Files:**
- Create: `src/codex_tokenomics/detector.py`
- Create: `tests/test_detector.py`

**Interfaces:**
- Consumes: `DetectionEngine(store: TelemetryStore, config: DetectorConfig)` and `evaluate(now: datetime, live_after: datetime) -> tuple[IncidentTransition, ...]`.
- Produces: `IncidentTransition(scope_type, scope_id, state, trigger, observed_rate, baseline_rate, absolute_threshold, opened_at, recovered_at)` for per-session and aggregate scopes.

- [ ] **Step 1: Write failing absolute and relative detector tests**

```python
def test_absolute_threshold_opens_incident() -> None:
    engine = engine_with_samples(tokens=[130_000, 130_000], seconds=[-30, -5])
    transition = only(engine.evaluate(NOW, live_after=NOW - timedelta(minutes=5)))
    assert transition.trigger == "absolute"
    assert transition.observed_rate == 260_000


def test_relative_spike_opens_below_absolute_threshold() -> None:
    engine = engine_with_baseline_and_current(baseline=40_000, current=130_000)
    transition = only(engine.evaluate(NOW, live_after=NOW - timedelta(hours=1)))
    assert transition.trigger == "relative"
    assert transition.baseline_rate == 40_000


def test_relative_detector_waits_for_minimum_baseline_buckets() -> None:
    engine = engine_with_baseline(bucket_rates=[40_000], current=200_000)
    assert engine.evaluate(NOW, live_after=NOW - timedelta(hours=1)) == ()
```

- [ ] **Step 2: Run detector tests and verify RED**

Run: `uv run pytest tests/test_detector.py -v`

Expected: FAIL because the detection engine does not exist.

- [ ] **Step 3: Implement rate windows, median baseline, and persisted incidents**

```python
def rate_per_minute(tokens: int, window_seconds: int) -> float:
    return tokens * 60.0 / window_seconds


def relative_triggered(current: float, baseline: float, config: DetectorConfig) -> bool:
    return (
        current >= config.relative_minimum_tokens_per_minute
        and current >= baseline * config.relative_multiplier
    )
```

Bucket by UTC event timestamps. Sum unique usage samples in `[now - rate_window, now]`; compute the median only from completed prior buckets. Evaluate every session with live samples plus an aggregate scope. Persist opening/recovery in `alert_incidents` so restart does not resend an open incident.

- [ ] **Step 4: Add recovery, aggregation, out-of-order, and no-history-alert tests**

```python
def test_recovery_requires_continuous_configured_duration() -> None:
    engine = opened_incident_engine()
    assert engine.evaluate(NOW + timedelta(seconds=299), LIVE_BOUNDARY) == ()
    recovery = only(engine.evaluate(NOW + timedelta(seconds=300), LIVE_BOUNDARY))
    assert recovery.state == "recovered"


def test_out_of_order_samples_do_not_create_negative_rate() -> None:
    engine = engine_with_out_of_order_samples()
    transitions = engine.evaluate(NOW, LIVE_BOUNDARY)
    assert all(item.observed_rate >= 0 for item in transitions)


def test_historical_reconciliation_never_opens_incident() -> None:
    engine = engine_with_historical_spike()
    assert engine.evaluate(NOW, live_after=NOW) == ()
```

- [ ] **Step 5: Run detector tests and commit**

Run: `uv run pytest tests/test_detector.py -v && uv run ruff check src tests`

Expected: PASS.

```bash
git add src/codex_tokenomics/detector.py tests/test_detector.py
git commit -m "feat: detect absolute and relative token spikes" -m "Assisted-by: Codex"
```

### Task 6: GNOME and Gmail Notification Adapters

**Files:**
- Create: `src/codex_tokenomics/notifiers.py`
- Create: `tests/test_notifiers.py`

**Interfaces:**
- Consumes: `NotificationDispatcher(store, desktop, email, config).dispatch(transition)` and a `CommandRunner.run(argv: Sequence[str]) -> CommandResult` protocol.
- Produces: `DesktopNotifier`, `EmailNotifier`, structured `AlertView`, bounded retries, notification-attempt persistence, and `EmailNotifier.validate(alert)`, which invokes Gmail with `--dry-run`.

- [ ] **Step 1: Write failing rendering and argv tests**

```python
def test_open_incident_sends_desktop_and_one_email(fake_runner: FakeRunner, store: TelemetryStore) -> None:
    dispatcher = build_dispatcher(fake_runner, store)
    dispatcher.dispatch(OPEN_TRANSITION)
    dispatcher.dispatch(OPEN_TRANSITION)
    assert fake_runner.calls_named("notify-send") == 1
    assert fake_runner.calls_named("gws") == 1


def test_email_is_plain_telemetry_with_ai_disclosure(fake_runner: FakeRunner) -> None:
    EmailNotifier(fake_runner, "amastbau@redhat.com").send(OPEN_TRANSITION)
    argv = fake_runner.only_call("gws")
    body = argv[argv.index("--body") + 1]
    assert "Automated by Codex Tokenomics; implementation assisted by Codex" in body
    assert SECRET not in body


def test_commands_use_argv_without_a_shell(fake_runner: FakeRunner) -> None:
    DesktopNotifier(fake_runner).send_open(OPEN_TRANSITION)
    assert fake_runner.only_call("notify-send")[:3] == ["notify-send", "--urgency=critical", "--app-name=Codex Tokenomics"]
```

- [ ] **Step 2: Run notifier tests and verify RED**

Run: `uv run pytest tests/test_notifiers.py -v`

Expected: FAIL because notification adapters are absent.

- [ ] **Step 3: Implement safe rendering and subprocess execution**

```python
class SubprocessRunner:
    def run(self, argv: Sequence[str]) -> CommandResult:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True, shell=False)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)
```

Build the email command as `gws gmail +send --to ... --subject ... --body ...`; `validate` appends `--dry-run`. Never interpolate a shell command. Convert subprocess failures to stable outcome codes without persisting raw stderr. Recovery sends only a GNOME notification.

- [ ] **Step 4: Add bounded retry and restart-idempotency tests**

```python
def test_email_retries_follow_configured_delays_and_stop(fake_clock: FakeClock) -> None:
    dispatcher = failing_dispatcher(delays=(10, 30), clock=fake_clock)
    dispatcher.dispatch(OPEN_TRANSITION)
    assert dispatcher.email_attempts(OPEN_TRANSITION.incident_id) == 3
    assert fake_clock.sleeps == [10, 30]


def test_persisted_success_prevents_email_after_restart(store: TelemetryStore) -> None:
    first = build_dispatcher(FakeRunner.success(), store)
    first.dispatch(OPEN_TRANSITION)
    second_runner = FakeRunner.success()
    build_dispatcher(second_runner, store).dispatch(OPEN_TRANSITION)
    assert second_runner.calls_named("gws") == 0
```

- [ ] **Step 5: Run notifier tests and commit**

Run: `uv run pytest tests/test_notifiers.py -v && uv run ruff check src tests`

Expected: PASS; no real commands execute.

```bash
git add src/codex_tokenomics/notifiers.py tests/test_notifiers.py
git commit -m "feat: notify token spike incidents" -m "Assisted-by: Codex"
```

### Task 7: Service Orchestration and Health

**Files:**
- Create: `src/codex_tokenomics/service.py`
- Create: `tests/test_service.py`

**Interfaces:**
- Consumes: `MonitorService(config, store, collector, detector, dispatcher, clock)`.
- Produces: `reconcile() -> LiveBoundary`, `run_once() -> ServiceCycle`, `run_forever()`, graceful stop, and `health() -> HealthSnapshot`.

- [ ] **Step 1: Write failing end-to-end service tests**

```python
def test_reconciliation_imports_history_without_alerting(harness: ServiceHarness) -> None:
    harness.write_historical_spike()
    boundary = harness.service.reconcile()
    assert harness.store.total_tokens() > 0
    assert harness.dispatcher.transitions == []
    assert boundary.started_at == harness.clock.now()


def test_live_append_opens_one_incident(harness: ServiceHarness) -> None:
    harness.service.reconcile()
    harness.append_live_spike()
    cycle = harness.service.run_once()
    assert cycle.opened_incidents == 1
    assert harness.dispatcher.email_count == 1
```

- [ ] **Step 2: Run service tests and verify RED**

Run: `uv run pytest tests/test_service.py -v`

Expected: FAIL because service orchestration is absent.

- [ ] **Step 3: Implement reconciliation, live cycles, and signal-safe shutdown**

```python
def run_once(self) -> ServiceCycle:
    collection = self.collector.scan_once()
    transitions = self.detector.evaluate(self.clock.now(), self.live_boundary.started_at)
    for transition in transitions:
        self.dispatcher.dispatch(transition)
    self.store.record_service_heartbeat(self.clock.now(), collection)
    return ServiceCycle.from_results(collection, transitions)
```

`run_forever` sleeps only for `config.collector.poll_interval_seconds`, exits on SIGINT/SIGTERM, and closes SQLite cleanly. `health` reports last poll, lag, parse failures, notification failures, integrity check, and version without raw errors.

- [ ] **Step 4: Add restart, malformed-line, and health tests**

```python
def test_restart_resumes_cursor_without_duplicate_alert(harness: ServiceHarness) -> None:
    harness.service.reconcile()
    harness.append_live_spike()
    harness.service.run_once()
    restarted = harness.restart_service()
    restarted.run_once()
    assert harness.store.incident_count() == 1
    assert harness.dispatcher.email_count == 1


def test_health_reports_parse_count_without_raw_error(harness: ServiceHarness) -> None:
    harness.append_bytes(b'{"secret":"' + SECRET.encode() + b'"\n')
    harness.service.run_once()
    output = json.dumps(asdict(harness.service.health()))
    assert "parse_failures" in output
    assert SECRET not in output
```

- [ ] **Step 5: Run service tests and commit**

Run: `uv run pytest tests/test_service.py -v && uv run ruff check src tests`

Expected: PASS.

```bash
git add src/codex_tokenomics/service.py tests/test_service.py
git commit -m "feat: orchestrate the local monitoring service" -m "Assisted-by: Codex"
```

### Task 8: Read-only Reports, SQL, and CLI

**Files:**
- Create: `src/codex_tokenomics/query.py`
- Create: `src/codex_tokenomics/cli.py`
- Create: `tests/test_query.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: `QueryService(database_path, row_limit, timeout_ms)`, `run_report(name, filters)`, and `run_sql(statement, parameters=())`.
- Produces: `codex-tokenomics` commands `validate-config`, `daemon`, `health`, `summary`, `sessions`, `models`, `agents`, `usage`, `timeline`, `anomalies`, `incidents`, `query`, and `notification-test`; every report supports `--format json`. `notification-test --email-dry-run` must call `EmailNotifier.validate`, never a real email send.

- [ ] **Step 1: Write failing report and read-only SQL tests**

```python
def test_models_report_ranks_total_and_cached_tokens(query: QueryService) -> None:
    rows = query.run_report("models", {"since": "2026-10-08T00:00:00Z"})
    assert rows[0]["model"] == "gpt-6.1-sol"
    assert rows[0]["total_tokens"] >= rows[1]["total_tokens"]


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM sessions",
        "ATTACH DATABASE '/tmp/other.db' AS other",
        "PRAGMA writable_schema=ON",
        "SELECT 1; SELECT 2",
        "WITH changed AS (DELETE FROM sessions RETURNING *) SELECT * FROM changed",
    ],
)
def test_unsafe_sql_is_rejected(query: QueryService, statement: str) -> None:
    with pytest.raises(QueryRejected):
        query.run_sql(statement)
```

- [ ] **Step 2: Run query tests and verify RED**

Run: `uv run pytest tests/test_query.py tests/test_cli.py -v`

Expected: FAIL because query and CLI modules do not exist.

- [ ] **Step 3: Implement reports and SQLite read-only defenses**

```python
def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.set_authorizer(_deny_mutation_and_attach)
    return connection
```

Use `Connection.set_progress_handler` to enforce the configured timeout and fetch at most `row_limit + 1` rows. Deny insert/update/delete, transaction control, schema changes, attach/detach, extension loading, and unsafe pragmas. Python `execute` must receive exactly one statement. Return JSON-safe dictionaries only.

- [ ] **Step 4: Add unbounded query, CLI JSON, and content-absence tests**

```python
def test_recursive_query_hits_execution_limit(query: QueryService) -> None:
    with pytest.raises(QueryTimedOut):
        query.run_sql("WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x) SELECT * FROM x")


def test_cli_json_never_contains_prohibited_content(cli: CliRunner) -> None:
    result = cli.invoke(["timeline", "--session", "session-1", "--format", "json"])
    assert result.exit_code == 0
    assert SECRET not in result.stdout


def test_email_notification_test_is_always_dry_run(cli: CliRunner, fake_runner: FakeRunner) -> None:
    result = cli.invoke(["notification-test", "--email-dry-run"])
    assert result.exit_code == 0
    assert "--dry-run" in fake_runner.only_call("gws")
```

- [ ] **Step 5: Run query/CLI tests and commit**

Run: `uv run pytest tests/test_query.py tests/test_cli.py -v && uv run ruff check src tests`

Expected: PASS.

```bash
git add src/codex_tokenomics/query.py src/codex_tokenomics/cli.py tests/test_query.py tests/test_cli.py
git commit -m "feat: query telemetry through a read-only CLI" -m "Assisted-by: Codex"
```

### Task 9: User Installer, systemd Unit, and Codex Skill

**Files:**
- Create: `src/codex_tokenomics/installer.py`
- Create: `src/codex_tokenomics/resources/codex-tokenomics.service`
- Create: `src/codex_tokenomics/resources/codex-tokenomics-skill/SKILL.md`
- Create: `tests/test_installer.py`
- Create: `tests/test_skill.py`

**Interfaces:**
- Consumes: `install(config_source: Path, home: Path, executable: Path, enable: bool) -> InstallResult` and `uninstall(home: Path, preserve_database: bool = True)`.
- Produces: restricted runtime directories/config, a systemd user unit, an installed local Codex skill, and CLI commands `install`/`uninstall`.

- [ ] **Step 1: Read the `skill-creator` skill before authoring the Codex skill**

Run: `sed -n '1,360p' /home/amastbau/.codex/skills/.system/skill-creator/SKILL.md`

Expected: the implementer applies its current structure, validation, and packaging rules to the local skill.

- [ ] **Step 2: Write failing installer and skill tests**

```python
def test_install_writes_restricted_files_into_fake_home(tmp_path: Path) -> None:
    result = install(VALID_CONFIG_PATH, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    assert mode(result.config_path) == 0o600
    assert mode(result.data_directory) == 0o700
    assert "ExecStart=/opt/bin/codex-tokenomics daemon" in result.unit_path.read_text()


def test_skill_routes_questions_to_read_only_commands(skill_text: str) -> None:
    assert "codex-tokenomics summary --format json" in skill_text
    assert "codex-tokenomics query --sql" in skill_text
    assert "Do not read ~/.codex/sessions" in skill_text
    assert "Do not modify the database" in skill_text


def test_uninstall_preserves_database_by_default(installed_home: Path) -> None:
    database = installed_database(installed_home)
    uninstall(installed_home)
    assert database.exists()
```

- [ ] **Step 3: Run installer/skill tests and verify RED**

Run: `uv run pytest tests/test_installer.py tests/test_skill.py -v`

Expected: FAIL because installer resources do not exist.

- [ ] **Step 4: Implement resource installation without activation in tests**

```python
def install(config_source: Path, home: Path, executable: Path, enable: bool) -> InstallResult:
    config = load_config(config_source)
    paths = RuntimePaths.for_home(home)
    paths.create_restricted()
    _copy_config(config_source, paths.config_file, mode=0o600)
    _render_unit(paths.unit_file, executable, paths.config_file)
    _copy_skill(paths.skill_directory)
    if enable:
        _systemctl_user("daemon-reload")
        _systemctl_user("enable", "--now", "codex-tokenomics.service")
    return InstallResult.from_paths(paths, config)
```

The unit sets `UMask=0077`, restarts only on failure with bounded delay, and passes the explicit config path. The skill explains the schema, prefers named report commands, uses safe SQL only when necessary, requests JSON, and never reads raw rollouts.

Render this service shape with the resolved executable and configuration paths:

```ini
[Unit]
Description=Codex Tokenomics local telemetry monitor
After=graphical-session.target network-online.target

[Service]
Type=simple
UMask=0077
ExecStart={executable} daemon --config {config_path}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

The installed skill begins with valid Codex frontmatter and contains only read-only instructions:

```markdown
---
name: codex-tokenomics
description: Query local content-free Codex session telemetry, token usage, incidents, and collector health.
---

# Codex Tokenomics

Use `codex-tokenomics <report> --format json` for named reports. Use
`codex-tokenomics query --sql '<single SELECT>' --format json` only when no named report answers
the question. Do not read `~/.codex/sessions`, do not modify the database, and never infer or
request prompt, response, tool-input, tool-output, command-output, or reasoning content.
```

- [ ] **Step 5: Validate the skill, resources, tests, and uninstall safety**

Run: `uv run pytest tests/test_installer.py tests/test_skill.py -v && uv run ruff check src tests`

Expected: PASS; fake-home tests do not call `systemctl`, `notify-send`, or `gws`.

- [ ] **Step 6: Commit installer and skill**

```bash
git add src/codex_tokenomics/installer.py src/codex_tokenomics/resources tests/test_installer.py tests/test_skill.py
git commit -m "feat: install the service and Codex query skill" -m "Assisted-by: Codex"
```

### Task 10: Full Privacy Regression, Documentation, and Release Verification

**Files:**
- Create: `tests/test_end_to_end.py`
- Modify: `README.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: the public CLI and a synthetic session directory.
- Produces: a complete end-to-end proof, user documentation, and a buildable package.

- [ ] **Step 1: Write the failing end-to-end privacy and incident test**

```python
def test_end_to_end_collect_detect_notify_query_and_recover(tmp_path: Path) -> None:
    harness = EndToEndHarness(tmp_path, prohibited_content=SECRET)
    harness.reconcile_history()
    harness.append_live_user_and_subagent_spike()
    harness.run_cycle()
    assert harness.open_incidents() == 2
    assert harness.desktop_notifications() == 2
    assert harness.email_attempts() == 2
    assert harness.query("models")[0]["total_tokens"] > 0
    harness.advance_below_threshold_through_recovery()
    assert harness.recovered_incidents() == 2
    harness.assert_secret_absent_from_all_artifacts(SECRET)
```

- [ ] **Step 2: Run the end-to-end test and verify RED**

Run: `uv run pytest tests/test_end_to_end.py -v`

Expected: FAIL until all public interfaces are wired through the CLI and harness.

- [ ] **Step 3: Finalize CLI dispatch and document exact operations**

Update `README.md` with prerequisites, the complete config schema, installation, user-service commands, health checks, GNOME/Gmail dry runs, example skill questions, database location, privacy exclusions, backup, upgrade, and safe uninstall. Include the disclosure `Documentation generated with Codex`.

Expose console scripts in `pyproject.toml`:

```toml
[project.scripts]
codex-tokenomics = "codex_tokenomics.cli:main"
```

Use one explicit command dispatch table so every documented command has an implementation and test:

```python
COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "validate-config": command_validate_config,
    "daemon": command_daemon,
    "health": command_health,
    "summary": command_summary,
    "sessions": command_sessions,
    "models": command_models,
    "agents": command_agents,
    "usage": command_usage,
    "timeline": command_timeline,
    "anomalies": command_anomalies,
    "incidents": command_incidents,
    "query": command_query,
    "notification-test": command_notification_test,
    "install": command_install,
    "uninstall": command_uninstall,
}
```

- [ ] **Step 4: Run the full quality gate**

Run: `uv run ruff check src tests && uv run pytest -v && uv build`

Expected: all lint and tests PASS; source and wheel artifacts build successfully.

- [ ] **Step 5: Run explicit privacy and command smoke checks**

Run: `uv run codex-tokenomics validate-config config.example.toml`

Expected: exit 0 and a content-free validation summary.

Run: `uv run codex-tokenomics --help`

Expected: exit 0 and all documented commands listed.

Run: `rg -n "PROHIBITED-CONTENT-9b84f1" . --glob '!tests/**' --glob '!docs/superpowers/**'`

Expected: no matches.

- [ ] **Step 6: Commit documentation and end-to-end proof**

```bash
git add README.md pyproject.toml tests/test_end_to_end.py
git commit -m "test: verify Codex Tokenomics end to end" -m "Assisted-by: Codex"
```

### Task 11: Install into the User Session and Verify Live Operation

**Files:**
- Modify outside repository through the reviewed installer only: `~/.config/codex-tokenomics/config.toml`
- Modify outside repository through the reviewed installer only: `~/.config/systemd/user/codex-tokenomics.service`
- Modify outside repository through the reviewed installer only: `~/.codex/skills/codex-tokenomics/`
- Create outside repository through the reviewed installer only: `~/.local/share/codex-tokenomics/telemetry.db`

**Interfaces:**
- Consumes: reviewed explicit threshold values in a complete config file.
- Produces: an enabled user service, historical import, healthy live polling, a GNOME dry-run alert, a Gmail `--dry-run` validation, and a discoverable local Codex skill.

- [ ] **Step 1: Create an installation config from reviewed explicit values**

Run: `cp config.example.toml /tmp/codex-tokenomics-config.toml`

Expected: a complete config whose per-session threshold, aggregate threshold, relative multiplier/minimum, windows, poll interval, recovery duration, recipient, and retry delays are all visibly specified. Do not install until the user has approved any values not already approved in the design.

- [ ] **Step 2: Validate the config before changing user state**

Run: `uv run codex-tokenomics validate-config /tmp/codex-tokenomics-config.toml`

Expected: PASS with all required values summarized and no credentials displayed.

- [ ] **Step 3: Install and enable the user service**

Run: `uv run codex-tokenomics install --config /tmp/codex-tokenomics-config.toml --enable`

Expected: restricted files installed, `systemctl --user daemon-reload` succeeds, and `codex-tokenomics.service` becomes active.

- [ ] **Step 4: Actively monitor reconciliation and health**

Run: `systemctl --user status codex-tokenomics.service --no-pager`

Expected: active/running with no restart loop.

Run: `journalctl --user -u codex-tokenomics.service --since=-5m --no-pager`

Expected: reconciliation completes, live boundary is established, parse failures are summarized without raw content, and no historical alert is sent.

Run: `codex-tokenomics health --format json`

Expected: database integrity `ok`, recent heartbeat, zero backlog, and notification dependencies detected.

- [ ] **Step 5: Validate notification integrations without sending email**

Run: `codex-tokenomics notification-test --desktop`

Expected: one GNOME test notification clearly labeled as a test.

Run: `codex-tokenomics notification-test --email-dry-run`

Expected: `gws gmail +send --dry-run` validates a message addressed to `amastbau@redhat.com`; no email is sent.

- [ ] **Step 6: Verify historical queries and skill installation**

Run: `codex-tokenomics summary --format json`

Expected: historical sessions include user, subagent, and guardian/review kinds with nonzero content-free usage totals.

Run: `test -f ~/.codex/skills/codex-tokenomics/SKILL.md && codex-tokenomics models --format json`

Expected: the skill exists and the model report succeeds.

- [ ] **Step 7: Record installation verification without runtime data**

Create `docs/verification.md` containing only commands, pass/fail results, package version, and service state. Do not copy session identifiers, paths outside documented runtime locations, raw journal payloads, or token data.

```bash
git add docs/verification.md
git commit -m "docs: record local installation verification" -m "Assisted-by: Codex"
```

### Task 12: Branch Review and Existing Pull Request Update

**Files:**
- Review: all files changed since `origin/main`

**Interfaces:**
- Consumes: the completed feature branch and verification evidence.
- Produces: an updated, verified PR #1 from `feature/initial-design`; no merge is performed without explicit user instruction.

- [ ] **Step 1: Verify the branch is clean and inspect the complete diff**

Run: `git status --short --branch && git diff --check origin/main...HEAD && git diff --stat origin/main...HEAD`

Expected: clean branch, no whitespace errors, and only Codex Tokenomics project files.

- [ ] **Step 2: Re-run the complete quality gate from a clean process**

Run: `uv run ruff check src tests && uv run pytest -v && uv build`

Expected: PASS with no warnings or network/desktop/Gmail access.

- [ ] **Step 3: Push the existing feature branch, never `main`**

Run: `git push origin feature/initial-design`

Expected: only the feature branch is updated.

- [ ] **Step 4: Update the existing pull request with AI disclosure**

```bash
gh pr edit 1 \
  --title "Implement local Codex token observability" \
  --body "Implements content-free collection, configurable spike detection, GNOME/Gmail alerts, read-only analytics, and the local Codex skill.\n\nGenerated with Codex"
```

Expected: PR #1 remains open from `feature/initial-design` into `main`. Do not merge.
