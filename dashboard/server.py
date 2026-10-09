#!/usr/bin/env python3
"""Tiny localhost-only, read-only UI server for Codex Tokenomics telemetry."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pricing import LONG_CONTEXT_THRESHOLD, METADATA, RATES, estimate_usd

DEFAULT_DB = Path("~/.local/share/codex-tokenomics/telemetry.db").expanduser()
ROOT = Path(__file__).resolve().parent
USAGE_FROM = (
    "FROM usage_samples u LEFT JOIN turns t "
    "ON t.session_id=u.session_id AND t.turn_id=u.turn_id"
)


@dataclass(frozen=True)
class ModelFilter:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.include or self.exclude)

    def clause(self, expression: str) -> tuple[str, list[str]]:
        clauses, params = [], []
        for models, operator in ((self.include, "IN"), (self.exclude, "NOT IN")):
            if models:
                clauses.append(f"{expression} {operator} ({','.join('?' for _ in models)})")
                params.extend(models)
        return " AND ".join(clauses), params


def open_database(path: Path) -> sqlite3.Connection:
    """Open the telemetry database read-only and content-neutrally."""
    uri = path.expanduser().resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [_row(row) for row in cursor.fetchall()]


def _limit(raw: str | None, default: int, maximum: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    return max(1, min(value, maximum))


def query_health(connection: sqlite3.Connection) -> dict[str, Any]:
    health = connection.execute(
        "SELECT last_poll_at, last_success_at, files_seen, parse_failures, notification_failures "
        "FROM service_health WHERE singleton=1"
    ).fetchone()
    integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    data = _row(health) if health is not None else {
        "last_poll_at": None, "last_success_at": None, "files_seen": None,
        "parse_failures": None, "notification_failures": None,
    }
    data["integrity_check"] = "ok" if integrity == "ok" else "failed"
    return data


def _range_clause(column: str, since: str | None, until: str | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if since:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{column} <= ?")
        params.append(until)
    return ("WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def _session_range_clause(since: str | None, until: str | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if since:
        clauses.append("COALESCE(s.last_seen_at, s.first_seen_at) >= ?")
        params.append(since)
    if until:
        clauses.append("s.first_seen_at <= ?")
        params.append(until)
    return ("WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def _append_clause(
    where: str, params: list[str], clause: str, values: list[str],
) -> tuple[str, list[str]]:
    if clause:
        where += (" AND " if where else "WHERE ") + clause
    return where, [*params, *values]


def _usage_clause(
    since: str | None, until: str | None, model_filter: ModelFilter,
) -> tuple[str, list[str]]:
    where, params = _range_clause("u.observed_at", since, until)
    clause, values = model_filter.clause("COALESCE(t.model, 'unknown')")
    return _append_clause(where, params, clause, values)


def _session_clause(
    since: str | None, until: str | None, model_filter: ModelFilter,
) -> tuple[str, list[str]]:
    where, params = _session_range_clause(since, until)
    usage_where, usage_params = _usage_clause(since, until, model_filter)
    return _append_clause(
        where, params, f"s.session_id IN (SELECT u.session_id {USAGE_FROM} {usage_where})",
        usage_params,
    )


def _incident_clause(
    since: str | None, until: str | None, model_filter: ModelFilter,
) -> tuple[str, list[str]]:
    where, params = _range_clause("i.opened_at", since, until)
    if model_filter.active:
        # Use the model recorded when the incident opened, not a later session model.
        expression = (
            "COALESCE((SELECT t.model FROM turns t WHERE t.session_id=i.scope_id "
            "AND COALESCE(t.started_at, t.observed_at) <= i.opened_at "
            "ORDER BY COALESCE(t.started_at, t.observed_at) DESC, t.turn_id DESC "
            "LIMIT 1), 'unknown')"
        )
        clause, values = model_filter.clause(expression)
        return _append_clause(where, params, f"i.scope_type='session' AND {clause}", values)
    return where, params


def query_available_models(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        f"SELECT DISTINCT COALESCE(t.model, 'unknown') AS model {USAGE_FROM} "
        "UNION SELECT DISTINCT COALESCE(model, 'unknown') FROM turns ORDER BY model"
    )
    return sorted({row[0] for row in rows} | RATES.keys())


def _empty_cost() -> dict[str, Any]:
    return {"known_cost_usd": 0.0, "priced_responses": 0,
            "unpriced_responses": 0, "unpriced_tokens": 0, "estimated_usd": 0.0}


def query_costs(
    connection: sqlite3.Connection, *, since: str | None = None, until: str | None = None,
    model_filter: ModelFilter = ModelFilter(),
) -> dict[str, Any]:
    """One read-only pass; model attribution is per response, never per session."""
    usage_where, usage_params = _usage_clause(since, until, model_filter)
    long_sessions = {
        row[0] for row in connection.execute(
            "SELECT DISTINCT u.session_id FROM usage_samples u "
            "JOIN turns t ON t.session_id=u.session_id AND t.turn_id=u.turn_id "
            "WHERE t.model='gpt-5.5' AND u.input_tokens > ?", (LONG_CONTEXT_THRESHOLD,),
        )
    }
    result: dict[str, Any] = {"summary": _empty_cost(), "sessions": {}, "agents": {}, "models": {}}
    rows = connection.execute(
        f"""SELECT u.session_id, s.agent_kind,
                   COALESCE(t.model_provider, s.model_provider) AS model_provider,
                   COALESCE(t.model, 'unknown') AS model, u.input_tokens,
                   u.cached_input_tokens, u.cache_write_input_tokens, u.output_tokens,
                   u.total_tokens
            FROM usage_samples u
            LEFT JOIN sessions s ON s.session_id=u.session_id
            LEFT JOIN turns t ON t.session_id=u.session_id AND t.turn_id=u.turn_id
            {usage_where}""", usage_params,
    )
    for row in rows:
        cost = estimate_usd(
            row["model"], row["input_tokens"], row["cached_input_tokens"],
            row["cache_write_input_tokens"], row["output_tokens"],
            long_session=row["session_id"] in long_sessions,
        ) if row["model_provider"] == "openai" else None
        buckets = [result["summary"]]
        for group, key in (("sessions", row["session_id"]), ("agents", row["agent_kind"]),
                           ("models", row["model"])):
            if key not in result[group]:
                result[group][key] = _empty_cost()
            buckets.append(result[group][key])
        for bucket in buckets:
            if cost is None:
                bucket["unpriced_responses"] += 1
                bucket["unpriced_tokens"] += row["total_tokens"] or 0
            else:
                bucket["priced_responses"] += 1
                bucket["known_cost_usd"] += cost
            bucket["estimated_usd"] = (
                None if bucket["unpriced_responses"] else bucket["known_cost_usd"]
            )
    return result


def query_summary(
    connection: sqlite3.Connection, *, since: str | None = None, until: str | None = None,
    model_filter: ModelFilter = ModelFilter(),
    _costs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage_where, usage_params = _usage_clause(since, until, model_filter)
    session_where, session_params = _session_clause(since, until, model_filter)
    incident_where, incident_params = _incident_clause(since, until, model_filter)
    result = _row(connection.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM sessions s {session_where}) AS sessions,
          COUNT(*) AS responses,
          COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
          COALESCE(SUM(u.cached_input_tokens), 0) AS cached_input_tokens,
          COALESCE(SUM(u.cache_write_input_tokens), 0) AS cache_write_input_tokens,
          COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
          COALESCE(SUM(u.reasoning_output_tokens), 0) AS reasoning_output_tokens,
          COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
          (SELECT COUNT(*) FROM alert_incidents i
           {incident_where + (' AND' if incident_where else 'WHERE')} i.recovered_at IS NULL)
            AS active_incidents
        {USAGE_FROM} {usage_where}
        """,
        [
            *session_params,
            *incident_params,
            *usage_params,
        ],
    ).fetchone())
    costs = _costs if _costs is not None else query_costs(
        connection, since=since, until=until, model_filter=model_filter,
    )
    result.update(costs["summary"])
    return result


def query_sessions(
    connection: sqlite3.Connection, *, limit: int = 200, since: str | None = None,
    until: str | None = None,
    model_filter: ModelFilter = ModelFilter(),
    _costs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    session_where, session_params = _session_clause(since, until, model_filter)
    usage_where, usage_params = _usage_clause(since, until, model_filter)
    models_expression = "u.models" if model_filter.active else "m.models"
    rows = _rows(connection.execute(
        f"""
        SELECT
          s.session_id, s.agent_kind, s.source, s.cli_version, s.model_provider,
          s.context_window, s.first_seen_at, s.last_seen_at, s.thread_id, s.parent_thread_id,
          COALESCE(u.responses, 0) AS responses,
          COALESCE(u.input_tokens, 0) AS input_tokens,
          COALESCE(u.cached_input_tokens, 0) AS cached_input_tokens,
          COALESCE(u.cache_write_input_tokens, 0) AS cache_write_input_tokens,
          COALESCE(u.output_tokens, 0) AS output_tokens,
          COALESCE(u.reasoning_output_tokens, 0) AS reasoning_output_tokens,
          COALESCE(u.total_tokens, 0) AS total_tokens,
          COALESCE({models_expression}, 'unknown') AS models
        FROM sessions s
        LEFT JOIN (
          SELECT u.session_id, COUNT(*) AS responses, SUM(u.input_tokens) AS input_tokens,
                 SUM(u.cached_input_tokens) AS cached_input_tokens,
                 SUM(u.cache_write_input_tokens) AS cache_write_input_tokens,
                 SUM(u.output_tokens) AS output_tokens,
                 SUM(u.reasoning_output_tokens) AS reasoning_output_tokens,
                 SUM(u.total_tokens) AS total_tokens,
                 GROUP_CONCAT(DISTINCT COALESCE(t.model, 'unknown')) AS models
          {USAGE_FROM} {usage_where} GROUP BY u.session_id
        ) u ON u.session_id=s.session_id
        LEFT JOIN (
          SELECT session_id, GROUP_CONCAT(model, ', ') AS models
          FROM (
            SELECT DISTINCT session_id, model FROM turns
            WHERE model IS NOT NULL ORDER BY model
          )
          GROUP BY session_id
        ) m ON m.session_id=s.session_id
        {session_where}
        ORDER BY total_tokens DESC, s.last_seen_at DESC
        LIMIT ?
        """,
        (*usage_params, *session_params, limit),
    ))
    costs = _costs if _costs is not None else query_costs(
        connection, since=since, until=until, model_filter=model_filter,
    )
    for row in rows:
        row.update(costs["sessions"].get(row["session_id"], _empty_cost()))
        row["label"] = (
            f"{row['agent_kind'] or 'unknown'} {row['source'] or 'unknown'} · "
            f"{row['responses']} responses · {row['total_tokens']} tokens"
        )
        row["cache_ratio"] = (
            row["cached_input_tokens"] / row["input_tokens"] if row["input_tokens"] else 0
        )
    return rows


def query_agents(
    connection: sqlite3.Connection, *, since: str | None = None, until: str | None = None,
    model_filter: ModelFilter = ModelFilter(),
    _costs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    usage_where, usage_params = _usage_clause(since, until, model_filter)
    where = usage_where
    if not model_filter.active and where:
        where = where.replace("WHERE", "WHERE u.response_id IS NULL OR", 1)
    rows = _rows(connection.execute(
        f"""
        SELECT s.agent_kind, COUNT(DISTINCT s.session_id) AS sessions,
               COUNT(u.response_id) AS responses,
               COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
               COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(u.reasoning_output_tokens), 0) AS reasoning_output_tokens
        FROM sessions s
        LEFT JOIN usage_samples u ON u.session_id=s.session_id
        LEFT JOIN turns t ON t.session_id=u.session_id AND t.turn_id=u.turn_id
        {where}
        GROUP BY s.agent_kind
        HAVING COUNT(u.response_id) > 0
        ORDER BY total_tokens DESC
        """,
        usage_params,
    ))
    costs = _costs if _costs is not None else query_costs(
        connection, since=since, until=until, model_filter=model_filter,
    )
    for row in rows:
        row.update(costs["agents"].get(row["agent_kind"], _empty_cost()))
    return rows


def query_models(
    connection: sqlite3.Connection, *, since: str | None = None, until: str | None = None,
    model_filter: ModelFilter = ModelFilter(),
    _costs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    usage_where, usage_params = _usage_clause(since, until, model_filter)
    rows = _rows(connection.execute(
        f"""
        SELECT COALESCE(t.model, 'unknown') AS model,
               COUNT(u.response_id) AS responses,
               COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
               COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(u.reasoning_output_tokens), 0) AS reasoning_output_tokens
        FROM usage_samples u
        LEFT JOIN turns t ON t.session_id=u.session_id AND t.turn_id=u.turn_id
        {usage_where}
        GROUP BY COALESCE(t.model, 'unknown')
        ORDER BY total_tokens DESC
        """,
        usage_params,
    ))
    costs = _costs if _costs is not None else query_costs(
        connection, since=since, until=until, model_filter=model_filter,
    )
    for row in rows:
        row.update(costs["models"].get(row["model"], _empty_cost()))
        row["rates_per_million"] = RATES.get(row["model"])
    return rows


def query_incidents(
    connection: sqlite3.Connection, *, limit: int = 200, since: str | None = None,
    until: str | None = None,
    model_filter: ModelFilter = ModelFilter(),
) -> list[dict[str, Any]]:
    incident_where, incident_params = _incident_clause(since, until, model_filter)
    return _rows(connection.execute(
        f"""
        SELECT i.incident_id, i.scope_type, i.scope_id, i.trigger, i.observed_rate,
               i.baseline_rate, i.absolute_threshold, i.opened_at, i.recovered_at,
               CASE WHEN i.recovered_at IS NULL THEN 'active' ELSE 'recovered' END AS status,
               COUNT(n.attempt_id) AS notification_attempts,
               SUM(CASE WHEN n.outcome_code IN ('failed', 'unavailable', 'timeout')
                        THEN 1 ELSE 0 END) AS notification_failures
        FROM alert_incidents i
        LEFT JOIN notification_attempts n ON n.incident_id=i.incident_id
        {incident_where}
        GROUP BY i.incident_id
        ORDER BY i.opened_at DESC
        LIMIT ?
        """,
        (*incident_params, limit),
    ))


def query_usage(
    connection: sqlite3.Connection, *, limit: int = 500, since: str | None = None,
    until: str | None = None,
    model_filter: ModelFilter = ModelFilter(),
) -> list[dict[str, Any]]:
    usage_where, usage_params = _usage_clause(since, until, model_filter)
    return _rows(connection.execute(
        f"""
        SELECT u.observed_at, u.session_id, u.response_id, u.input_tokens, u.cached_input_tokens,
               u.cache_write_input_tokens, u.output_tokens, u.reasoning_output_tokens, u.total_tokens,
               COALESCE(t.model, 'unknown') AS model
        {USAGE_FROM}
        {usage_where}
        ORDER BY u.observed_at DESC
        LIMIT ?
        """,
        (*usage_params, limit),
    ))


def query_all(
    connection: sqlite3.Connection, *, since: str | None = None, until: str | None = None,
    model_filter: ModelFilter = ModelFilter(),
) -> dict[str, Any]:
    # Keep the counters and estimates on one SQLite snapshot while the collector runs.
    if not connection.in_transaction:
        connection.execute("BEGIN")
    filters = dict(since=since, until=until, model_filter=model_filter)
    costs = query_costs(connection, **filters)
    return {
        "pricing": METADATA,
        "available_models": query_available_models(connection),
        "filters": {"include_models": list(model_filter.include), "exclude_models": list(model_filter.exclude)},
        "health": query_health(connection),
        "summary": query_summary(connection, **filters, _costs=costs),
        "sessions": query_sessions(connection, limit=300, **filters, _costs=costs),
        "agents": query_agents(connection, **filters, _costs=costs),
        "models": query_models(connection, **filters, _costs=costs),
        "incidents": query_incidents(connection, limit=300, **filters),
        "usage": query_usage(connection, limit=500, **filters),
    }


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file(ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/api/"):
            self._send_api(parsed.path.removeprefix("/api/"), parse_qs(parsed.query))
            return
        self.send_error(404, "not found")

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: object) -> None:
        body = json.dumps(value, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_api(self, name: str, query: dict[str, list[str]]) -> None:
        limit = _limit(query.get("limit", [None])[0], 200, 1000)
        since = query.get("since", [None])[0]
        until = query.get("until", [None])[0]
        model_filter = ModelFilter(
            include=tuple(dict.fromkeys(query.get("include_model", []))),
            exclude=tuple(dict.fromkeys(query.get("exclude_model", []))),
        )
        filters = dict(since=since, until=until, model_filter=model_filter)
        try:
            with open_database(self.db_path) as connection:
                handlers = {
                    "health": lambda: query_health(connection),
                    "summary": lambda: query_summary(connection, **filters),
                    "sessions": lambda: query_sessions(
                        connection, limit=limit, **filters,
                    ),
                    "agents": lambda: query_agents(connection, **filters),
                    "models": lambda: query_models(connection, **filters),
                    "incidents": lambda: query_incidents(
                        connection, limit=limit, **filters,
                    ),
                    "usage": lambda: query_usage(connection, limit=limit, **filters),
                    "all": lambda: query_all(connection, **filters),
                }
                if name not in handlers:
                    self._send_json(404, {"error": "unknown endpoint"})
                    return
                self._send_json(200, handlers[name]())
        except sqlite3.Error:
            self._send_json(500, {"error": "database read failed"})
        except OSError:
            self._send_json(500, {"error": "database path unavailable"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Codex Tokenomics local UI")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    Handler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Serving Codex Tokenomics UI at {url}")
    print(f"Reading database read-only: {args.db.expanduser()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
