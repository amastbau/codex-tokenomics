# codex-tokenomics

Local, content-free observability for Codex token usage. The service imports Codex rollout JSONL files, stores only allowlisted telemetry in SQLite, detects token-rate spikes, sends local/Gmail alerts, and exposes read-only reports through the `codex-tokenomics` CLI and installed Codex skill.

Documentation generated with Codex.

## Prerequisites

- Python 3.12 or newer.
- `uv` for local development, tests, and builds.
- A Linux user session with `systemd --user` for service activation.
- `notify-send` for desktop notifications.
- An already authenticated `gws` CLI for Gmail validation and incident email delivery.

The service reads `~/.codex/sessions/**/*.jsonl` and writes local runtime state only under the current user's configuration and data directories.

## Configuration

Create a complete TOML configuration before installation. Every detector and timing value is explicit; the Python code has no silent detector defaults.

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

Validate any candidate configuration before changing user state:

```bash
uv run codex-tokenomics validate-config config.example.toml
```

## Installation

Install the package into the current environment, then run the reviewed installer:

```bash
uv run codex-tokenomics install --config config.example.toml
```

Add `--enable` only when you want the installer to reload the user systemd manager and start the service:

```bash
uv run codex-tokenomics install --config config.example.toml --enable
```

The installer creates restricted runtime directories, writes `~/.config/codex-tokenomics/config.toml`, installs `~/.config/systemd/user/codex-tokenomics.service`, installs the local skill at `~/.codex/skills/codex-tokenomics/`, and initializes `~/.local/share/codex-tokenomics/telemetry.db`.

## User service

Useful service commands:

```bash
systemctl --user status codex-tokenomics.service --no-pager
journalctl --user -u codex-tokenomics.service --since=-5m --no-pager
systemctl --user restart codex-tokenomics.service
systemctl --user stop codex-tokenomics.service
```

The service first reconciles historical rollout files without alerting, then establishes a live boundary. Only newly ingested live records can open incidents.

## Health checks

Use the health report for service state, database integrity, backlog, parse-failure counts, and notification failure counts:

```bash
codex-tokenomics health --format json
codex-tokenomics summary --format json
codex-tokenomics sessions --format json
codex-tokenomics models --format json
codex-tokenomics incidents --format json
```

Health output is content-free. Parse failures are counted without storing malformed lines or raw exception text.

## Notification dry runs

Desktop and email integrations can be checked separately:

```bash
codex-tokenomics notification-test --desktop
codex-tokenomics notification-test --email-dry-run
```

`--desktop` sends one local test notification. `--email-dry-run` invokes `gws gmail +send --dry-run`; it validates delivery arguments without sending email. Incident emails include the disclosure `Automated by Codex Tokenomics; implementation assisted by Codex`.

## Example skill questions

After installation, Codex can use the local `codex-tokenomics` skill for questions such as:

- Which model used the most tokens today?
- Which sessions spiked in the last hour?
- Compare cached and uncached input by agent type this week.
- Show the incidents that are still open.
- Is the collector healthy?

The skill should prefer named reports such as `codex-tokenomics models --format json`. It may use `codex-tokenomics query --sql '<single SELECT>' --format json` only for read-only questions not covered by a named report.

## Database

The SQLite database lives at `~/.local/share/codex-tokenomics/telemetry.db` unless configured otherwise. It uses WAL mode, normalized tables, unique response identities, ingestion cursors, alert incidents, notification attempts, and service health rows.

The database is local-only and retained indefinitely. The query layer opens it read-only, enables SQLite query-only mode, denies writes and unsafe pragmas with an authorizer, applies row limits, and enforces execution timeouts.

## Privacy exclusions

The service stores only allowlisted telemetry: identifiers, timestamps, model/runtime metadata, token counters, rate-limit snapshots, tool names/status/timing, incident state, notification outcomes, and ingestion health.

It never stores prompts, assistant responses, reasoning text, summaries, base instructions, world-state payloads, tool arguments, tool output, command strings, command output, raw errors, raw session events, or source JSON blobs. Tests include prohibited sentinel content and verify it is absent from the database, reports, alerts, and captured outputs.

## Backup

Stop the service before making a consistent backup:

```bash
systemctl --user stop codex-tokenomics.service
cp ~/.local/share/codex-tokenomics/telemetry.db /path/to/backup/telemetry.db
systemctl --user start codex-tokenomics.service
```

If WAL sidecar files exist while the service is running, include `telemetry.db-wal` and `telemetry.db-shm` or stop the service first.

## Upgrade

From the repository, run the full local quality gate before installing a new build:

```bash
uv run ruff check src tests
uv run pytest -v
uv build
uv run codex-tokenomics validate-config ~/.config/codex-tokenomics/config.toml
uv run codex-tokenomics install --config ~/.config/codex-tokenomics/config.toml --enable
```

Schema migrations run when the database opens. Existing telemetry is preserved.

## Uninstall

The default uninstall removes owned service and skill files while preserving the database:

```bash
codex-tokenomics uninstall
```

Database deletion is intentionally not exposed as a convenience CLI action. Remove `~/.local/share/codex-tokenomics/telemetry.db` only after a separate, explicit decision to delete retained telemetry.
