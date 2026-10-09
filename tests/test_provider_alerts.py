"""Provider restrictions must affect alerts, never collected usage."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from codex_tokenomics.collector import Collector
from codex_tokenomics.config import (
    AppConfig,
    CollectorConfig,
    DetectorConfig,
    NotificationConfig,
    PathsConfig,
    QueryConfig,
)
from codex_tokenomics.detector import (
    DetectionEngine,
    IncidentTransition,
    TokenBreakdown,
)
from codex_tokenomics.notifiers import (
    CommandResult,
    DesktopNotifier,
    EmailNotifier,
    NotificationDispatcher,
)
from codex_tokenomics.service import MonitorService
from codex_tokenomics.storage import IngestCursor, TelemetryStore
from codex_tokenomics.telemetry import SessionRecord, TurnRecord, UsageSample

NOW = datetime(2026, 10, 9, 8, tzinfo=UTC)
CONFIG = DetectorConfig(250_000, 1_000_000, 3.0, 100_000, 60, 600, 3, 300)
LIVE = NOW - timedelta(hours=1)


@pytest.fixture
def store(tmp_path):
    with TelemetryStore.open(tmp_path / "telemetry.db") as opened:
        yield opened


def add_usage(store, provider, tokens, *, session="session-1", seconds=0, turn_provider=None):
    timestamp = (NOW + timedelta(seconds=seconds)).isoformat()
    turn_id = f"turn-{seconds}"
    store.ingest([
        SessionRecord(session, timestamp, model_provider=provider),
        TurnRecord(session, timestamp, turn_id, model="gpt-6.1-sol",
                   model_provider=turn_provider),
        UsageSample(session, timestamp, f"response-{seconds}", tokens, 0, 0, 0, 0,
                    tokens, turn_id=turn_id),
    ], IngestCursor("fixture", "/synthetic/session.jsonl", 1, 2, 0))


@pytest.mark.parametrize("provider", ["enmaas", "local", "custom", "", None])
def test_other_or_unknown_providers_cannot_open_session_or_aggregate_alerts(store, provider):
    add_usage(store, provider, 2_000_000)

    assert DetectionEngine(store, CONFIG).evaluate(NOW, LIVE) == ()
    assert store.table_counts()["alert_incidents"] == 0
    assert store.total_tokens() == 2_000_000


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google", "xai", "azure", "bedrock"])
def test_major_providers_still_open_alerts(store, provider):
    add_usage(store, provider, 260_000)

    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, LIVE)

    assert [(item.scope_type, item.observed_rate) for item in transitions] == [("session", 260_000)]


def test_enmaas_cannot_push_combined_usage_over_aggregate_threshold(store):
    add_usage(store, "openai", 240_000, session="paid")
    add_usage(store, "enmaas", 2_000_000, session="internal")

    assert DetectionEngine(store, CONFIG).evaluate(NOW, LIVE) == ()
    assert store.total_tokens() == 2_240_000


def test_aggregate_rate_and_breakdown_contain_only_major_provider_usage(store):
    add_usage(store, "openai", 600_000, session="paid-1")
    add_usage(store, "anthropic", 600_000, session="paid-2")
    add_usage(store, "enmaas", 2_000_000, session="internal")

    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, LIVE)

    assert [(item.scope_id, item.observed_rate) for item in transitions] == [
        ("paid-1", 600_000), ("paid-2", 600_000), ("all", 1_200_000),
    ]
    assert transitions[-1].token_breakdown.total_tokens == 1_200_000
    assert store.total_tokens() == 3_200_000


def test_response_provider_overrides_session_provider_in_mixed_sessions(store):
    add_usage(store, "openai", 2_000_000, seconds=-5, turn_provider="enmaas")
    add_usage(store, "openai", 260_000, turn_provider="openai")

    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, LIVE)

    assert [(item.scope_type, item.observed_rate) for item in transitions] == [("session", 260_000)]
    assert transitions[0].token_breakdown.total_tokens == 260_000


def test_excluded_history_cannot_supply_a_relative_alert_baseline(store):
    for seconds in (-210, -150, -90):
        add_usage(store, "openai", 40_000, seconds=seconds, turn_provider="enmaas")
    add_usage(store, "openai", 130_000)

    assert DetectionEngine(store, CONFIG).evaluate(NOW, LIVE) == ()


@pytest.mark.parametrize("scope_type,provider,tokens,expected_attempts", [
    ("session", "enmaas", 2_000_000, 0),
    ("aggregate", "enmaas", 2_000_000, 0),
    ("session", None, 2_000_000, 0),
    ("session", "openai", 260_000, 2),
    ("aggregate", "openai", 1_100_000, 2),
])
def test_pending_alerts_obey_provider_policy_on_service_restart(
    store, tmp_path, scope_type, provider, tokens, expected_attempts,
):
    add_usage(store, provider, tokens)
    store.open_incident(
        incident_id="old-incident", scope_type=scope_type,
        scope_id="session-1" if scope_type == "session" else "all",
        trigger="absolute", observed_rate=tokens, baseline_rate=None,
        absolute_threshold=250_000 if scope_type == "session" else 1_000_000, opened_at=NOW,
    )

    class Runner:
        def run(self, argv):
            return CommandResult(0, "", "")

    class Clock:
        def now(self):
            return NOW

    config = AppConfig(
        PathsConfig(tmp_path / "sessions", tmp_path / "telemetry.db"), CollectorConfig(2),
        CONFIG, NotificationConfig("alerts@example.test", (10, 30)), QueryConfig(1000, 2000),
    )
    runner = Runner()
    dispatcher = NotificationDispatcher(
        store, DesktopNotifier(runner), EmailNotifier(runner, "alerts@example.test"),
        config.notifications, clock=Clock(),
    )
    service = MonitorService(
        config, store, Collector(config.paths.session_root, store),
        DetectionEngine(store, CONFIG), dispatcher, Clock(),
    )

    cycle = service.run_once()

    assert store.table_counts()["notification_attempts"] == expected_attempts
    assert len(cycle.transitions) == (1 if expected_attempts else 0)


def test_legacy_mixed_aggregate_alert_is_not_retried_when_major_usage_is_below_threshold(store):
    add_usage(store, "openai", 10, session="paid")
    add_usage(store, "enmaas", 2_000_000, session="internal")
    unrestricted = replace(CONFIG, aggregate_absolute_tokens_per_minute=2_000_000)
    # A legacy incident's recorded rate includes the internal provider.
    transition = IncidentTransition(
        "legacy", "aggregate", "all", "opened", "absolute", 2_000_010, None,
        2_000_000, NOW, None, TokenBreakdown(0, 0, 0, 0, 0, 0),
    )

    assert not DetectionEngine(store, unrestricted).alert_is_allowed(transition)
