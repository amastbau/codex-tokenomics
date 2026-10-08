# Local Installation Verification

Generated with Codex.

Package version: `0.1.0`

Date: 2026-10-08

## Commands

| Command | Result |
|---|---|
| `cp config.example.toml /tmp/codex-tokenomics-config.toml` | Pass: complete explicit installation config created. |
| `uv run codex-tokenomics validate-config /tmp/codex-tokenomics-config.toml` | Pass: config validated with explicit detector, notification, collector, path, and query values. |
| `uv run codex-tokenomics install --config /tmp/codex-tokenomics-config.toml --enable` | Pass: installer completed and reported the user service enabled. |
| `systemctl --user status codex-tokenomics.service --no-pager` | Pass after fix: service active and running. |
| `journalctl --user -u codex-tokenomics.service --since=-5m --no-pager` | Pass with caveat: initial collection failures were observed, then no new failures after the storage identity fix. |
| `codex-tokenomics health --format json` | Pass with caveat: database integrity and service status were `ok`; heartbeat was recent; backlog was nonzero while active Codex sessions were still writing; notification failures were recorded from live alert email attempts. |
| `codex-tokenomics notification-test --desktop` | Pass outside sandbox: desktop test notification reported sent. |
| `codex-tokenomics notification-test --email-dry-run` | Pass: Gmail validation reported dry-run and did not send email. |
| `codex-tokenomics summary --format json` | Pass: historical content-free telemetry query returned results. |
| `test -f ~/.codex/skills/codex-tokenomics/SKILL.md` | Pass: local Codex skill installed. |
| `codex-tokenomics models --format json` | Pass: model report returned content-free results. |

## Fix Applied During Verification

The first service start exposed real rollout data where the same turn identifier can appear in multiple sessions. Storage now scopes turn identity by `(session_id, turn_id)` and migrates version-1 databases to that identity model on open.

## Service State

The user service is enabled and active. The latest health check reported status `ok`, SQLite integrity `ok`, zero parse failures, and a recent heartbeat. The backlog was not zero because active Codex sessions continued appending rollout data during verification.
