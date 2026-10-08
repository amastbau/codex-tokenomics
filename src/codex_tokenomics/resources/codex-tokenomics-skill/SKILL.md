---
name: codex-tokenomics
description: Query local content-free Codex session telemetry, token usage, incidents, and collector health.
---

# Codex Tokenomics

Answer questions using the local `codex-tokenomics` CLI. Request JSON and summarize only the
returned non-content telemetry.

Prefer named reports:

- `codex-tokenomics health --format json` for collector and database health.
- `codex-tokenomics summary --format json` for overall usage.
- `codex-tokenomics sessions --format json` for session totals and agent kinds.
- `codex-tokenomics models --format json` for model usage.
- `codex-tokenomics agents --format json` for user, subagent, and review-agent usage.
- `codex-tokenomics usage --format json` for token-category totals over time.
- `codex-tokenomics timeline --session <id> --format json` for one session's telemetry.
- `codex-tokenomics anomalies --format json` and `codex-tokenomics incidents --format json` for
  spike history.

Use `codex-tokenomics query --sql '<single SELECT>' --format json` only when no named report
answers the question. Query only documented telemetry tables and request bounded aggregate or
filtered results.

The telemetry vocabulary includes session and turn identifiers, agent kind, model and runtime
settings, lifecycle timing, token categories, tool name/type/status/timing, rate-limit samples,
incidents, notification outcomes, ingestion counters, and service health.

For SQL, the normalized tables are `sessions`, `turns`, `responses`, `usage_samples`,
`rate_limit_samples`, `tool_events`, `alert_incidents`, `notification_attempts`, `ingest_cursors`,
and `service_health`. Join session telemetry on `session_id`; join incident delivery outcomes on
`incident_id`. Select only the columns needed to answer the question.

Do not read ~/.codex/sessions. Do not modify the database. Never infer or request prompt content,
response content, summaries, tool input, tool output, command input/output, world state, raw
errors, raw JSON, or reasoning content; those fields are outside this system's data boundary.
