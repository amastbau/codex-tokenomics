# Codex Tokenomics Design

## Summary

Codex Tokenomics is a local-only observability service for every Codex session owned by the current operating-system user. It imports historical telemetry, follows active top-level sessions and delegated agents, detects abnormal token consumption, sends local and email alerts, and exposes a read-only query interface through a Codex skill.

The project stores non-content telemetry only. It must never retain prompts, assistant responses, reasoning text, tool arguments, tool output, command output, base instructions, world-state payloads, or raw session events.

## Goals

- Discover and monitor all local Codex user, subagent, and review/guardian sessions.
- Import all available historical non-content telemetry and collect future telemetry continuously.
- Track per-session and machine-wide token consumption without double-counting delegated work or repeated records.
- Detect both configured absolute token-rate thresholds and relative deviations from recent behavior.
- Notify immediately through GNOME and once per incident by email to `amastbau@redhat.com`.
- Retain telemetry indefinitely in a local SQLite database.
- Let Codex answer natural-language questions through a local, read-only skill.
- Remain useful across restarts, file rotation, partial writes, and newly created date directories.

## Non-goals

- Monitoring Codex Cloud tasks that do not create local session records.
- Capturing or searching conversation or tool content.
- Sending telemetry to a hosted service.
- Automatically stopping, throttling, or changing a Codex session.
- Providing a web dashboard in the initial version.

## Repository and Runtime Layout

The project is a Python package in a standalone Git repository named `codex-tokenomics`. Development must occur on feature branches; changes must not be committed directly to `main`.

Runtime data uses standard per-user locations:

- Configuration: `~/.config/codex-tokenomics/config.toml`
- Database: `~/.local/share/codex-tokenomics/telemetry.db`
- User service: `~/.config/systemd/user/codex-tokenomics.service`
- Codex skill: `~/.codex/skills/codex-tokenomics/`

Runtime directories are mode `0700`; the configuration and database are mode `0600` where supported.

## Architecture

The system has five focused components:

1. **Collector** discovers `~/.codex/sessions/**/*.jsonl`, imports historical records, and tails files that grow.
2. **Normalizer** extracts an explicit allowlist of non-content fields into typed telemetry records.
3. **Store** persists normalized telemetry, ingestion cursors, and alert state in SQLite.
4. **Detector and notifiers** compute token rates and manage spike incidents, GNOME notifications, and email delivery.
5. **Query CLI and Codex skill** expose read-only reports and safe SQL-backed questions.

The collector runs as a user-level systemd service. It polls for new and changed files at the configured interval. Polling is chosen over the experimental app-server interface because local rollout files are durable, cover historical sessions, and remain available when the app server restarts.

On startup, the collector reconciles every matching session file with its saved cursor. It then follows only appended data. A new or reset file is recognized by its device/inode, size, and persisted offset. Incomplete trailing JSON lines are held until the next poll rather than treated as corrupt.

## Content Exclusion and Security Boundary

The normalizer is allowlist-based. It constructs new typed records field by field and never stores the source JSON object. Unknown fields are ignored until reviewed and explicitly added.

Allowed telemetry includes:

- Session, thread, root-turn, turn, response, and event identifiers.
- Session source and agent kind, including user, subagent, and guardian/review sessions.
- Model, model provider, reasoning effort, CLI version, context-window size, collaboration mode, sandbox and approval modes.
- Working directory and workspace-root paths.
- Start/completion timestamps, duration, time to first token, lifecycle status, and structured status codes.
- Input, cached-input, cache-write, output, reasoning-output, and total-token counters.
- Rate-limit snapshots and context utilization.
- Tool name/type, lifecycle status, and timing only.
- Alert incidents, recovery times, detector values, and notification delivery status.
- Ingestion file identity, offset, timestamps, and parser health counters.

Explicitly prohibited fields include message content, summaries, encrypted reasoning, base instructions, tool inputs, tool outputs, command strings/output, world state, raw errors, and raw JSON blobs. Tests must include sentinel secret text in prohibited fields and prove that it never appears in the database, logs, notifications, or query output.

The service never reads Google credentials itself. Email is delegated to the already authenticated `gws gmail +send` command. Logs must not include authentication material or subprocess environments.

## Data Model

SQLite tables are normalized around stable identifiers:

- `sessions`: session identity, source, agent kind, CLI/provider metadata, paths, and first/last activity.
- `turns`: hierarchy, model/effort/runtime settings, lifecycle, and timing.
- `responses`: response identity, lifecycle, and timing.
- `usage_samples`: per-response token breakdown and observation timestamp.
- `rate_limit_samples`: structured limit/window/utilization values.
- `tool_events`: tool type/name, status, and timing without arguments or output.
- `alert_incidents`: scope, trigger, observed rate, baseline, threshold, opening, and recovery.
- `notification_attempts`: channel, timestamp, outcome code, and retry count.
- `ingest_cursors`: file identity, offset, partial-line state, and parser counters.
- `schema_migrations`: database schema version history.

Unique constraints on response/event identifiers make ingestion idempotent. Token rates are derived from unique per-response usage rather than summing cumulative thread counters. Cumulative counters are retained only for reconciliation checks. This prevents retries, repeated `token_count` events, and parent/subagent bookkeeping from inflating totals.

## Detection

Detection operates independently for each session and for the aggregate of all local sessions. It uses event timestamps rather than polling timestamps.

An incident opens when either condition is true:

1. The configured absolute token rate is exceeded.
2. The current rate is at least the configured multiple of the rolling baseline and exceeds the configured relative minimum rate.

The baseline is the median of prior completed rate buckets within the configured baseline window. Relative detection remains inactive until the configured minimum number of baseline buckets exists. An incident closes only after its scope remains below both conditions for the configured recovery duration.

No threshold or detector timing value is hard-coded. The following configuration values are required and validated at startup:

- Poll interval.
- Rate bucket/window duration.
- Per-session absolute token-rate threshold.
- Aggregate absolute token-rate threshold.
- Baseline duration and minimum bucket count.
- Relative multiplier and relative minimum token rate.
- Recovery duration.
- Email retry count and retry delays.

The installed configuration may use reviewed initial values, but application code has no silent detector defaults. Missing, non-positive, inconsistent, or unknown values cause a clear startup failure.

## Notifications

At incident opening, the detector sends:

- An immediate critical GNOME notification using `notify-send`.
- One email to the configured recipient, initially `amastbau@redhat.com`, using `gws gmail +send`.

A recovery notification is sent through GNOME. Email is not repeated during the same incident. Email failures use bounded configured retry delays; they do not open new token incidents or loop indefinitely.

Alerts contain only telemetry: scope/session identifier, agent kind, model, trigger condition, observed rate, baseline, relevant threshold, token-category breakdown, and timestamp. Every email includes an `Automated by Codex Tokenomics; implementation assisted by Codex` disclosure. No session content is included.

Notification adapters support a dry-run mode. Tests always use in-memory fakes and cannot execute real notification commands.

## Query Interface and Codex Skill

The package provides a `codex-tokenomics` CLI with health, summary, sessions, models, agents, usage, timelines, anomalies, incidents, and configuration-validation commands. Machine-readable JSON output is available for every report.

A query command permits a single read-only SQL statement for questions not covered by predefined reports. It opens SQLite with `mode=ro`, enables `PRAGMA query_only`, rejects multiple statements, installs a SQLite authorizer that denies writes and unsafe pragmas, applies row and execution limits, and returns structured output.

The local `codex-tokenomics` Codex skill documents the telemetry vocabulary and maps natural-language questions to predefined reports first, then safe read-only SQL when necessary. Example questions include:

- Which model used the most tokens today?
- Which sessions spiked in the last hour?
- Compare cached and uncached input by agent type this week.
- Show the parent session and subagents involved in the largest incident.
- Is the collector healthy, and are any files failing to parse?

The skill cannot access prohibited content because that content is absent from the database and query API.

## Reliability and Operations

- SQLite uses WAL mode, explicit transactions, foreign keys, and schema migrations.
- Collector cursors are committed in the same transaction as normalized records.
- Malformed complete lines increment a parser counter and are quarantined by file/offset metadata without storing their contents.
- Counter resets and session-file truncation produce health events rather than negative usage.
- The service exposes a health command covering last successful poll, backlog, parse failures, notification failures, database integrity, and service version.
- Graceful shutdown finishes the active transaction and persists cursors.
- Historical import never emits spike alerts. Live alerting starts only after reconciliation establishes the current boundary.
- Telemetry is retained indefinitely. Database maintenance compacts indexes without deleting observations.

## Testing Strategy

Implementation follows test-driven development. Tests use synthetic JSONL fixtures covering user sessions, subagents, review agents, duplicate records, partial lines, truncation, counter resets, malformed lines, and schema evolution.

Required test layers:

- Unit tests for allowlist normalization, configuration validation, rate calculations, incident state transitions, and SQL authorization.
- Store tests against temporary SQLite databases, including migrations and idempotency.
- Collector integration tests using temporary session trees and append operations.
- Notification contract tests with fake command runners.
- End-to-end tests that import fixtures, detect a configured spike, record one incident/email attempt, recover, and answer representative queries.
- A privacy regression test that scans the database and all captured output for prohibited sentinel content.

No test may use the real Codex session directory, send desktop notifications, or send email.

## Installation and Lifecycle

Installation is an explicit command that:

1. Creates the runtime directories with restricted permissions.
2. Writes a complete configuration after validating user-selected detector values.
3. Initializes the database.
4. Installs the systemd user unit and Codex skill.
5. Reloads and enables the user service only after an explicit installation action.
6. Runs health and dry-run notification checks.

Uninstallation stops and removes the service and skill but preserves the database unless the user explicitly requests deletion. Database deletion is a separate destructive action requiring confirmation.

## Acceptance Criteria

- All historical and newly created local session types are discovered and imported.
- Unique response usage is counted exactly once across restarts and repeated records.
- The database contains the defined telemetry and none of the prohibited content.
- Required detector values come exclusively from validated configuration.
- Absolute and relative spikes work independently for sessions and aggregate usage.
- A spike produces one GNOME alert and one email attempt; recovery re-arms only after the configured duration.
- Notification retries are bounded and observable.
- Representative natural-language questions can be answered through the local skill using read-only queries.
- Restart, partial-write, truncation, and malformed-line tests pass without data loss or duplicate usage.
- The full automated test suite passes without contacting Gmail or the desktop notification service.
