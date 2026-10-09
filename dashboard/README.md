# Codex Tokenomics UI

Heavily inspired by [Spendgeist](https://github.com/rkishony/Spendgeist), the
agent cost monitor for Cursor. See screenshots and install it at
[agentcostmonitor.com](https://agentcostmonitor.com/).

Local read-only dashboard for the `codex-tokenomics` SQLite database.

## Run

The dashboard runs as a separate user service from the telemetry collector. Install
the included unit once to start the UI at login and restart it after a crash:

```bash
mkdir -p ~/.config/systemd/user
cp codex-tokenomics-ui.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-tokenomics-ui.service
```

Run the installation commands from this directory. The unit uses
`~/tokenomic/codex-tokenomics/dashboard`; adjust its paths if the project is
moved.

Check the UI service and endpoint:

```bash
systemctl --user status codex-tokenomics-ui.service
curl --fail http://127.0.0.1:8765/api/all
```

For a manual foreground run, first stop the UI service if it is running, then use:

```bash
cd /home/amastbau/tokenomic/codex-tokenomics/dashboard
python server.py
```

Open:

```text
http://127.0.0.1:8765/
```

The default database path is:

```text
~/.local/share/codex-tokenomics/telemetry.db
```

To use another database:

```bash
python server.py --db /path/to/telemetry.db --port 8765
```

## Global filters

The Include models and Exclude models checkboxes apply across usage totals, costs,
sessions, agent/model breakdowns, recent usage, and price charts/tables. An empty
Include list means all models. Exclusions take priority. Selections are saved in
this browser; Clear model filters resets models while keeping the date range.

Filtering uses each response's recorded model, including `unknown` when attribution
is missing. Mixed-model sessions retain only matching response totals. Sessions and
agent groups without matching usage are hidden; retained collector metadata after a
usage reset does not count as activity. Model choices remain available when filtered out.

Collector health remains global. Session alerts are filtered by the model recorded
when they opened and keep their original rates. Aggregate alerts cannot be split by
model and are hidden while model filters are active.

All telemetry API endpoints accept repeated `include_model` and `exclude_model`
parameters alongside `since` and `until`, for example:
`/api/all?include_model=gpt-6.1-sol&exclude_model=gpt-6-astra`.
`/api/health` always reports overall collector health.

## Safety boundary

- The backend opens SQLite with `mode=ro` and `PRAGMA query_only=ON`.
- It serves only aggregate/session telemetry, token counters, model/runtime metadata, tool names,
  incident history, notification outcomes, and service health.
- It does not read `~/.codex/sessions`.
- It does not read prompts, responses, tool arguments, tool outputs, reasoning text, command
  input/output, or raw JSON.

## Test

```bash
python -m unittest -v
node test_ui_sort.mjs
```

## Estimated USD costs

The dashboard estimates token costs per model, agent kind, and session. Tokens and
costs use the selected dates and models. Session timestamps remain lifetime metadata;
model lists show matching responses when model filters are active.

`pricing.py` contains a public Standard API rate snapshot checked on 2026-10-08. Sources:
[OpenAI pricing](https://developers.openai.com/api/docs/pricing),
[model pages](https://developers.openai.com/api/docs/models), and
[cache accounting](https://developers.openai.com/api/docs/guides/prompt-caching).
Rates are shown in the dashboard. Update the snapshot when published prices change, then
restart the UI server. There are no API calls, API keys, or LLM requests in the estimator.

For each response, ordinary input is input minus cached reads minus cache writes.
These three disjoint categories use their own model rates. Output includes reasoning, so
reasoning is not charged twice. Long-context prices apply above 272,000 input tokens per
request; GPT-5.5 applies that adjustment across its session, including when the request
crossing the threshold falls outside the selected date range. GPT-5.5 has no cache-write
premium. Mixed-model sessions sum each response at its recorded model's rate.

Amounts are **current-rate estimates, not invoices or historical billed costs**. Standard
processing is assumed because service tier and region are not recorded. Tool fees, taxes,
regional premiums, Fast/Ultrafast premiums, and contract discounts are excluded. Unknown
models/providers or invalid cache counters are marked unpriced. API totals are null when
coverage is incomplete; the separate known subtotal is explicitly labeled in the UI.
Displaying historical Astra usage does not select or run Astra.

Assisted-by: Codex
