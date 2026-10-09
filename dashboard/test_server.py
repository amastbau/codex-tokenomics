import sqlite3
import tempfile
import unittest
from pathlib import Path

import server


SCHEMA = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    source TEXT,
    agent_kind TEXT,
    cli_version TEXT,
    model_provider TEXT,
    context_window INTEGER,
    first_seen_at TEXT,
    last_seen_at TEXT,
    thread_id TEXT,
    parent_thread_id TEXT
);
CREATE TABLE turns (
    turn_id TEXT,
    session_id TEXT,
    model TEXT,
    observed_at TEXT,
    model_provider TEXT,
    started_at TEXT
);
CREATE TABLE usage_samples (
    session_id TEXT,
    response_id TEXT PRIMARY KEY,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    cache_write_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    observed_at TEXT,
    turn_id TEXT
);
CREATE TABLE alert_incidents (
    incident_id TEXT PRIMARY KEY,
    scope_type TEXT,
    scope_id TEXT,
    trigger TEXT,
    observed_rate REAL,
    baseline_rate REAL,
    absolute_threshold REAL,
    opened_at TEXT,
    below_since TEXT,
    recovered_at TEXT
);
CREATE TABLE notification_attempts (
    attempt_id INTEGER PRIMARY KEY,
    incident_id TEXT,
    channel TEXT,
    attempted_at TEXT,
    attempt_number INTEGER,
    outcome_code TEXT
);
CREATE TABLE service_health (
    singleton INTEGER PRIMARY KEY,
    last_poll_at TEXT,
    last_success_at TEXT,
    files_seen INTEGER,
    parse_failures INTEGER,
    notification_failures INTEGER
);
"""


class ServerQueriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "telemetry.db"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "session-1", "cli", "user", "0.161.0", "openai", 258400,
                "2026-10-08T10:00:00+00:00", "2026-10-08T10:30:00+00:00",
                "thread-1", None,
            ),
        )
        self.connection.execute(
            "INSERT INTO turns (turn_id, session_id, model, observed_at) VALUES (?, ?, ?, ?)",
            ("turn-1", "session-1", "gpt-6-sol", "2026-10-08T10:15:00+00:00"),
        )
        self.connection.execute(
            "INSERT INTO usage_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("session-1", "response-1", 80, 50, 5, 20, 3, 100,
             "2026-10-08T10:20:00+00:00", "turn-1"),
        )
        self.connection.execute(
            "INSERT INTO alert_incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "incident-1", "session", "session-1", "absolute", 260000, 120000,
                250000, "2026-10-08T10:21:00+00:00", None, None,
            ),
        )
        self.connection.execute(
            "INSERT INTO notification_attempts VALUES (?, ?, ?, ?, ?, ?)",
            (1, "incident-1", "desktop", "2026-10-08T10:21:01+00:00", 1, "sent"),
        )
        self.connection.execute(
            "INSERT INTO service_health VALUES (?, ?, ?, ?, ?, ?)",
            (1, "2026-10-08T10:22:00+00:00", "2026-10-08T10:22:00+00:00", 3, 0, 0),
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.tmp.cleanup()

    def test_summary_is_content_free_and_aggregated(self) -> None:
        summary = server.query_summary(self.connection)

        self.assertEqual(summary["sessions"], 1)
        self.assertEqual(summary["responses"], 1)
        self.assertEqual(summary["total_tokens"], 100)
        self.assertEqual(summary["active_incidents"], 1)

    def test_summary_honors_date_range(self) -> None:
        self.connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "session-2", "cli", "user", "0.161.0", "openai", 258400,
                "2026-10-09T10:00:00+00:00", "2026-10-09T10:30:00+00:00",
                "thread-2", None,
            ),
        )
        self.connection.execute(
            "INSERT INTO usage_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("session-2", "response-2", 8, 5, 0, 2, 0, 10,
             "2026-10-09T10:20:00+00:00", None),
        )
        self.connection.commit()

        summary = server.query_summary(
            self.connection,
            since="2026-10-09T00:00:00+00:00",
            until="2026-10-09T23:59:59+00:00",
        )

        self.assertEqual(summary["sessions"], 1)
        self.assertEqual(summary["responses"], 1)
        self.assertEqual(summary["total_tokens"], 10)

    def test_sessions_include_metadata_label_and_token_totals(self) -> None:
        rows = server.query_sessions(self.connection, limit=10)

        self.assertEqual(rows[0]["session_id"], "session-1")
        self.assertEqual(rows[0]["label"], "user cli · 1 responses · 100 tokens")
        self.assertEqual(rows[0]["models"], "gpt-6-sol")

    def test_incidents_include_notification_counts(self) -> None:
        rows = server.query_incidents(self.connection, limit=10)

        self.assertEqual(rows[0]["incident_id"], "incident-1")
        self.assertEqual(rows[0]["status"], "active")
        self.assertEqual(rows[0]["notification_attempts"], 1)

    def add_usage(self, response_id, model, *, observed_at="2026-10-08T10:25:00+00:00",
                  input_tokens=80, cached=50, written=5, output=20):
        turn_id = f"turn-{response_id}"
        self.connection.execute(
            "INSERT INTO turns (turn_id, session_id, model, observed_at) VALUES (?, ?, ?, ?)",
            (turn_id, "session-1", model, observed_at),
        )
        self.connection.execute(
            "INSERT INTO usage_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("session-1", response_id, input_tokens, cached, written, output, 3,
             input_tokens + output, observed_at, turn_id),
        )

    def test_cost_prices_input_cache_reads_writes_and_output_without_double_counting(self):
        # 25*2 + 50*.2 + 5*2.5 + 20*10 = $272.5 / million.
        result = server.query_all(self.connection)
        self.assertAlmostEqual(result["summary"].get("estimated_usd", -1), 0.0002725)
        for section in ("sessions", "agents", "models"):
            self.assertAlmostEqual(result[section][0]["estimated_usd"], 0.0002725)

    def test_session_cost_uses_each_response_model_not_one_session_model(self):
        self.add_usage("response-2", "gpt-6.1-sol")
        result = server.query_all(self.connection)
        self.assertAlmostEqual(result["summary"].get("estimated_usd", -1), 0.0005400)
        self.assertAlmostEqual(result["sessions"][0]["estimated_usd"], 0.0005400)
        self.assertAlmostEqual(sum(r["estimated_usd"] for r in result["models"]), 0.0005400)

    def test_filtered_session_tokens_and_cost_exclude_outside_range_responses(self):
        self.add_usage("response-2", "gpt-6.1-sol")
        result = server.query_all(self.connection, since="2026-10-08T10:24:00+00:00")
        self.assertEqual(result["sessions"][0]["total_tokens"], 100)
        self.assertAlmostEqual(result["summary"].get("estimated_usd", -1), 0.0002675)
        self.assertAlmostEqual(result["sessions"][0]["estimated_usd"], 0.0002675)

    def test_unknown_model_marks_total_incomplete_and_keeps_known_subtotal(self):
        self.add_usage("response-2", "unpublished-model")
        result = server.query_all(self.connection)
        summary = result["summary"]
        self.assertEqual(summary.get("unpriced_responses", -1), 1)
        self.assertIsNone(summary["estimated_usd"])
        self.assertEqual(summary["unpriced_tokens"], 100)
        self.assertAlmostEqual(summary["known_cost_usd"], 0.0002725)
        unknown = next(r for r in result["models"] if r["model"] == "unpublished-model")
        self.assertIsNone(unknown["estimated_usd"])

    def test_invalid_cache_counters_are_unpriced_not_negative_cost(self):
        self.add_usage("response-2", "gpt-6-sol", cached=79, written=5)
        result = server.query_summary(self.connection)
        self.assertEqual(result.get("unpriced_responses", -1), 1)
        self.assertIsNone(result["estimated_usd"])

    def test_long_context_is_priced_per_request_with_strict_threshold(self):
        self.connection.execute("DELETE FROM usage_samples")
        self.add_usage("at-threshold", "gpt-6.1-sol", input_tokens=272000,
                       cached=100000, written=20000, output=1000)
        self.add_usage("over-threshold", "gpt-6.1-sol", input_tokens=272001,
                       cached=100000, written=20000, output=1000)
        summary = server.query_summary(self.connection)
        self.assertAlmostEqual(summary.get("estimated_usd", -1), 1.117004)

    def test_gpt_55_session_long_context_applies_even_outside_selected_range(self):
        self.connection.execute("DELETE FROM usage_samples")
        self.add_usage("long", "gpt-5.5", observed_at="2026-10-08T10:20:00+00:00",
                       input_tokens=272001, cached=0, written=0, output=0)
        self.add_usage("short", "gpt-5.5", input_tokens=80, cached=50, written=0, output=20)
        summary = server.query_summary(self.connection, since="2026-10-08T10:24:00+00:00")
        self.assertAlmostEqual(summary.get("estimated_usd", -1), 0.00125)

    def test_empty_range_has_zero_cost_and_complete_coverage(self):
        result = server.query_all(self.connection, since="2099-01-01T00:00:00+00:00")
        self.assertEqual(result["summary"].get("estimated_usd", -1), 0)
        self.assertEqual(result["summary"]["unpriced_responses"], 0)

    def test_cost_query_works_without_write_permissions(self):
        with server.open_database(self.db_path) as connection:
            summary = server.query_summary(connection)
            self.assertAlmostEqual(summary.get("estimated_usd", -1), 0.0002725)
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_non_openai_turn_provider_is_not_charged_openai_rates(self):
        self.connection.execute("UPDATE turns SET model_provider='another-provider'")
        summary = server.query_summary(self.connection)
        self.assertEqual(summary["unpriced_responses"], 1)
        self.assertIsNone(summary["estimated_usd"])

    def test_each_published_model_rate_prices_all_categories(self):
        cases = {"gpt-6-sol": .269, "gpt-6.1-sol": .267,
                 "gpt-6-luna": .01345, "gpt-6-astra": 1.345,
                 "gpt-5.6-sol": .538, "gpt-5.6-terra": .289,
                 "gpt-5.6-luna": .0289, "gpt-5.5": .71}
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.connection.execute("DELETE FROM usage_samples")
                self.add_usage(model, model, input_tokens=100000, cached=20000,
                               written=10000, output=10000)
                self.assertAlmostEqual(server.query_summary(self.connection)["estimated_usd"],
                                       expected)

    def test_open_database_is_read_only(self) -> None:
        read_only = server.open_database(self.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                read_only.execute("CREATE TABLE should_fail(value INTEGER)")
        finally:
            read_only.close()

    def test_include_model_filters_mixed_sessions_across_every_usage_section(self):
        self.add_usage("response-2", "gpt-6.1-sol")
        selected = server.ModelFilter(include=("gpt-6.1-sol",))
        result = server.query_all(self.connection, model_filter=selected)
        self.assertEqual(result["summary"]["sessions"], 1)
        self.assertEqual(result["summary"]["responses"], 1)
        self.assertEqual(result["summary"]["total_tokens"], 100)
        self.assertAlmostEqual(result["summary"]["estimated_usd"], .0002675)
        self.assertEqual(result["sessions"][0]["models"], "gpt-6.1-sol")
        self.assertEqual(result["sessions"][0]["total_tokens"], 100)
        self.assertEqual(result["agents"][0]["total_tokens"], 100)
        self.assertEqual([row["model"] for row in result["models"]], ["gpt-6.1-sol"])
        self.assertEqual([row["response_id"] for row in result["usage"]], ["response-2"])
        self.assertIn("gpt-6-sol", result["available_models"])
        self.assertIn("gpt-6.1-sol", result["available_models"])
        self.assertEqual(result["filters"]["include_models"], ["gpt-6.1-sol"])
        self.assertEqual(result["health"], server.query_health(self.connection))

    def test_exclusions_win_over_inclusions_and_apply_before_usage_limit(self):
        self.add_usage("response-2", "gpt-6.1-sol")
        selected = server.ModelFilter(
            include=("gpt-6-sol", "gpt-6.1-sol"), exclude=("gpt-6.1-sol",),
        )
        result = server.query_all(self.connection, model_filter=selected)
        self.assertEqual(result["summary"]["total_tokens"], 100)
        self.assertAlmostEqual(result["summary"]["estimated_usd"], .0002725)
        self.assertEqual([row["model"] for row in result["models"]], ["gpt-6-sol"])
        usage = server.query_usage(self.connection, limit=1, model_filter=selected)
        self.assertEqual([row["response_id"] for row in usage], ["response-1"])

    def test_date_and_model_filters_intersect_and_empty_matches_are_zero(self):
        self.add_usage("response-2", "gpt-6.1-sol")
        result = server.query_all(
            self.connection, until="2026-10-08T10:22:00+00:00",
            model_filter=server.ModelFilter(include=("gpt-6.1-sol",)),
        )
        self.assertEqual(result["summary"]["sessions"], 0)
        self.assertEqual(result["summary"]["responses"], 0)
        self.assertEqual(result["summary"]["estimated_usd"], 0)
        for section in ("sessions", "models", "agents", "usage", "incidents"):
            self.assertEqual(result[section], [])

    def test_unknown_model_can_be_included_and_excluded_without_inventing_prices(self):
        self.add_usage("unknown-response", None)
        selected = server.ModelFilter(include=("unknown",))
        result = server.query_all(self.connection, model_filter=selected)
        self.assertEqual(result["summary"]["responses"], 1)
        self.assertEqual(result["summary"]["unpriced_responses"], 1)
        self.assertIsNone(result["summary"]["estimated_usd"])
        self.assertEqual(result["sessions"][0]["models"], "unknown")
        result = server.query_all(
            self.connection, model_filter=server.ModelFilter(exclude=("unknown",)),
        )
        self.assertEqual(result["summary"]["responses"], 1)
        self.assertAlmostEqual(result["summary"]["estimated_usd"], .0002725)

    def test_incident_model_is_the_model_at_opening_and_aggregate_alerts_are_hidden(self):
        self.add_usage("response-2", "gpt-6.1-sol")
        self.connection.execute(
            "INSERT INTO alert_incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("aggregate-1", "aggregate", "all", "absolute", 260000, 120000,
             250000, "2026-10-08T10:21:00+00:00", None, None),
        )
        result = server.query_all(
            self.connection, model_filter=server.ModelFilter(include=("gpt-6-sol",)),
        )
        self.assertEqual([row["incident_id"] for row in result["incidents"]], ["incident-1"])
        self.assertEqual(result["summary"]["active_incidents"], 1)
        result = server.query_all(
            self.connection, model_filter=server.ModelFilter(include=("gpt-6.1-sol",)),
        )
        self.assertEqual(result["incidents"], [])
        self.assertEqual(result["summary"]["active_incidents"], 0)
        self.assertEqual(server.query_summary(self.connection)["active_incidents"], 2)

    def test_model_filter_parameters_are_bound_and_read_only(self):
        selected = server.ModelFilter(include=("gpt-6-sol') OR 1=1 --",))
        with server.open_database(self.db_path) as connection:
            result = server.query_all(connection, model_filter=selected)
            self.assertEqual(result["summary"]["responses"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM usage_samples").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_preserved_metadata_after_usage_reset_does_not_count_as_activity(self):
        self.connection.execute("DELETE FROM notification_attempts")
        self.connection.execute("DELETE FROM alert_incidents")
        self.connection.execute("DELETE FROM usage_samples")
        result = server.query_all(self.connection)
        self.assertEqual(result["summary"]["sessions"], 0)
        self.assertEqual(result["summary"]["total_tokens"], 0)
        for section in ("sessions", "agents", "models", "usage", "incidents"):
            self.assertEqual(result[section], [])
        self.assertIn("gpt-6-sol", result["available_models"])

    def test_later_turn_completion_does_not_change_incident_model_attribution(self):
        self.connection.execute(
            "UPDATE turns SET observed_at=?, started_at=? WHERE turn_id='turn-1'",
            ("2026-10-08T10:30:00+00:00", "2026-10-08T10:15:00+00:00"),
        )
        rows = server.query_incidents(
            self.connection, model_filter=server.ModelFilter(include=("gpt-6-sol",)),
        )
        self.assertEqual([row["incident_id"] for row in rows], ["incident-1"])


if __name__ == "__main__":
    unittest.main()
