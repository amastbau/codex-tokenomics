CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    thread_id TEXT,
    parent_thread_id TEXT,
    source TEXT NOT NULL,
    agent_kind TEXT NOT NULL,
    cli_version TEXT,
    model_provider TEXT,
    cwd TEXT,
    workspace_roots TEXT NOT NULL DEFAULT '[]',
    context_window INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
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
    root_turn_id TEXT,
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
CREATE INDEX usage_observed_at ON usage_samples(observed_at);
CREATE INDEX usage_session_observed_at ON usage_samples(session_id, observed_at);
CREATE TABLE rate_limit_samples (
    sample_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    observed_at TEXT NOT NULL,
    window TEXT NOT NULL CHECK (window IN ('primary', 'secondary')),
    limit_id TEXT,
    limit_name TEXT NOT NULL,
    used_value REAL,
    limit_value REAL,
    window_seconds INTEGER,
    resets_at TEXT
);
CREATE TABLE tool_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    observed_at TEXT NOT NULL,
    thread_id TEXT,
    turn_id TEXT,
    tool_name TEXT NOT NULL,
    tool_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_ms INTEGER,
    completed_at_ms INTEGER,
    duration_ms INTEGER,
    status_code INTEGER
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
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
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
    parse_failures INTEGER NOT NULL DEFAULT 0 CHECK (parse_failures >= 0),
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
