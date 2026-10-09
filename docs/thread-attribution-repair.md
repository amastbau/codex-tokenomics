# Thread attribution repair

Each rollout thread is a separate telemetry session. Parent session identifiers must
not merge a user chat, worker subagent, and approval reviewer into one agent total.

The normalizer uses explicit event thread identifiers, then the rollout's own
identity. Standard rollout filenames identify forked threads even when their
initial metadata retains an ancestor's ID. Metadata labels attach to that thread.

Existing databases need a one-time replay after installing this change. Run with
the collector stopped:

```sh
systemctl --user stop codex-tokenomics.service
python -m codex_tokenomics.reindex \
  --database "$HOME/.local/share/codex-tokenomics/telemetry.db" \
  --session-root "$HOME/.codex/sessions"
systemctl --user start codex-tokenomics.service
```

Use the project's virtual environment for `python`. Always restart the collector,
including when repair exits unsuccessfully.

Repair creates a private SQLite backup beside the original database. It rebuilds
in isolation through the existing collector allowlist, verifies every old response's
token counters, checks thread identities and database integrity, and only then
publishes the repaired database. Missing source files, changed counters, or a
concurrently writing collector prevent publication. It prints aggregate results
and the backup path, never rollout content.

Historical incident and notification records are preserved. Historical incident
rates reflect the grouping in effect when those alerts were generated; repair
does not invent replacement alerts or replay notification delivery.

## Local verification, 2026-10-08

The completed repair reconciled all 18,643 existing response records and kept their
1,984,142,179 total tokens unchanged. The live dashboard's agent and model totals
matched its overall total; there were zero thread attribution mismatches. The
collector was active with zero parse failures.

The 166 focused normalization, collection, storage, and repair tests passed, along
with all five dashboard tests and lint for changed code. The full suite still has
181 failures and 33 setup errors from the unfinished email-removal edits; this
repair preserves those edits rather than reverting them.

Generated with Codex.
