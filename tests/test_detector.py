from collections.abc import Iterator, Sequence
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_tokenomics.config import DetectorConfig
from codex_tokenomics.detector import DetectionEngine
from codex_tokenomics.storage import IngestCursor, TelemetryStore
from codex_tokenomics.telemetry import UsageSample

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
LIVE_BOUNDARY = NOW - timedelta(hours=1)
CONFIG = DetectorConfig(
    session_absolute_tokens_per_minute=250_000,
    aggregate_absolute_tokens_per_minute=1_000_000,
    relative_multiplier=3.0,
    relative_minimum_tokens_per_minute=100_000,
    rate_window_seconds=60,
    baseline_window_seconds=600,
    minimum_baseline_buckets=3,
    recovery_seconds=300,
)
RECOVERY_CONFIG = replace(CONFIG, relative_minimum_tokens_per_minute=3_000_000)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TelemetryStore]:
    with TelemetryStore.open(tmp_path / "telemetry.db") as opened:
        yield opened


def sample(
    tokens: int, seconds: float, *, session_id: str = "session-1", response_id: str | None = None,
) -> UsageSample:
    return UsageSample(
        session_id=session_id, timestamp=(NOW + timedelta(seconds=seconds)).isoformat(),
        response_id=response_id or f"response-{seconds}", total_tokens=tokens,
        input_tokens=900_000, cached_input_tokens=800_000, cache_write_input_tokens=700_000,
        output_tokens=600_000, reasoning_output_tokens=500_000, cumulative_total_tokens=9_000_000,
    )


def ingest(store: TelemetryStore, samples: Sequence[UsageSample]) -> None:
    store.ingest(samples, IngestCursor(
        file_key="fixture", source_path="/synthetic/session.jsonl", device=1, inode=2,
        offset=0, updated_at=NOW.isoformat(),
    ))


def baseline(store: TelemetryStore, rates: Sequence[int], *, session_id: str = "session-1") -> None:
    ingest(store, [
        sample(tokens, -(index + 2) * 60 + 30, session_id=session_id)
        for index, tokens in enumerate(rates)
    ])


def test_absolute_threshold_opens_incident(store: TelemetryStore) -> None:
    ingest(store, [sample(130_000, -30), sample(130_000, -5)])
    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, LIVE_BOUNDARY)
    assert len(transitions) == 1
    transition = transitions[0]
    assert (transition.scope_type, transition.scope_id, transition.state) == (
        "session", "session-1", "opened",
    )
    assert transition.trigger == "absolute"
    assert transition.observed_rate == 260_000
    assert transition.absolute_threshold == 250_000
    assert transition.baseline_rate is None
    assert transition.opened_at == NOW
    assert transition.recovered_at is None
    assert transition.incident_id
    assert store.table_counts()["alert_incidents"] == 1
    with pytest.raises(FrozenInstanceError):
        transition.state = "recovered"


def test_transition_breakdown_sums_unique_current_window_by_scope(store: TelemetryStore) -> None:
    first = replace(sample(100, -120), input_tokens=1, cached_input_tokens=2,
                    cache_write_input_tokens=3, output_tokens=4, reasoning_output_tokens=5)
    second = replace(sample(200, 0), input_tokens=10, cached_input_tokens=20,
                     cache_write_input_tokens=30, output_tokens=40, reasoning_output_tokens=50)
    other = replace(sample(400, -30, session_id="session-2"), input_tokens=7,
                    cached_input_tokens=8, cache_write_input_tokens=9, output_tokens=10,
                    reasoning_output_tokens=11)
    ingest(store, [sample(900_000, -121), first, first, second, other, sample(900_000, 1)])
    config = replace(RECOVERY_CONFIG, rate_window_seconds=120,
                     session_absolute_tokens_per_minute=100,
                     aggregate_absolute_tokens_per_minute=300)
    transitions = DetectionEngine(store, config).evaluate(NOW, NOW - timedelta(seconds=121))
    assert [(item.scope_id, item.observed_rate) for item in transitions] == [
        ("session-1", 150), ("session-2", 200), ("all", 350),
    ]
    expected = [(11, 22, 33, 44, 55, 300), (7, 8, 9, 10, 11, 400), (18, 30, 42, 54, 66, 700)]
    for transition, values in zip(transitions, expected, strict=True):
        tokens = transition.token_breakdown
        assert (tokens.input_tokens, tokens.cached_input_tokens, tokens.cache_write_input_tokens,
                tokens.output_tokens, tokens.reasoning_output_tokens, tokens.total_tokens) == values
        with pytest.raises(FrozenInstanceError):
            tokens.total_tokens = 999


def test_transition_breakdown_excludes_usage_at_or_before_live_boundary(store: TelemetryStore) -> None:
    current = replace(sample(260_000, 0), input_tokens=13, cached_input_tokens=7,
                      cache_write_input_tokens=3, output_tokens=11, reasoning_output_tokens=5)
    ingest(store, [sample(900_000, -45), sample(900_000, -30), current])
    transitions = DetectionEngine(store, RECOVERY_CONFIG).evaluate(NOW, NOW - timedelta(seconds=30))
    assert len(transitions) == 1
    tokens = transitions[0].token_breakdown
    assert (tokens.input_tokens, tokens.cached_input_tokens, tokens.cache_write_input_tokens,
            tokens.output_tokens, tokens.reasoning_output_tokens, tokens.total_tokens) == (
        13, 7, 3, 11, 5, 260_000,
    )


def test_recovery_transition_breakdown_uses_current_window_instead_of_opening(
    store: TelemetryStore,
) -> None:
    ingest(store, [sample(260_000, 0)])
    engine = DetectionEngine(store, RECOVERY_CONFIG)
    opening = engine.evaluate(NOW, LIVE_BOUNDARY)[0]
    assert engine.evaluate(NOW + timedelta(seconds=61), LIVE_BOUNDARY) == ()
    ingest(store, [replace(sample(17, 350), input_tokens=1, cached_input_tokens=2,
                          cache_write_input_tokens=3, output_tokens=4, reasoning_output_tokens=5)])
    recovered = engine.evaluate(NOW + timedelta(seconds=361), LIVE_BOUNDARY)[0]
    assert recovered.incident_id == opening.incident_id
    assert recovered.state == "recovered"
    tokens = recovered.token_breakdown
    assert (tokens.input_tokens, tokens.cached_input_tokens, tokens.cache_write_input_tokens,
            tokens.output_tokens, tokens.reasoning_output_tokens, tokens.total_tokens) == (1, 2, 3, 4, 5, 17)
    assert recovered.observed_rate == 17


def test_relative_spike_opens_below_absolute_threshold(store: TelemetryStore) -> None:
    baseline(store, [40_000, 40_000, 40_000])
    ingest(store, [sample(130_000, -5)])
    config = replace(CONFIG, aggregate_absolute_tokens_per_minute=1_000_000,
                     relative_minimum_tokens_per_minute=120_000)
    transitions = DetectionEngine(store, config).evaluate(NOW, LIVE_BOUNDARY)
    assert {item.scope_type for item in transitions} == {"session", "aggregate"}
    assert all(item.trigger == "relative" for item in transitions)
    assert all(item.baseline_rate == 40_000 for item in transitions)
    assert all(item.observed_rate == 130_000 for item in transitions)


def test_relative_detector_waits_for_minimum_baseline_buckets(store: TelemetryStore) -> None:
    baseline(store, [40_000])
    ingest(store, [sample(200_000, -5)])
    assert DetectionEngine(store, CONFIG).evaluate(NOW, LIVE_BOUNDARY) == ()


def test_relative_baseline_is_median_of_completed_prior_buckets(store: TelemetryStore) -> None:
    baseline(store, [20_000, 40_000, 900_000])
    ingest(store, [sample(130_000, -5)])
    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, LIVE_BOUNDARY)
    assert len(transitions) == 2
    assert all(item.baseline_rate == 40_000 for item in transitions)
    assert all(item.trigger == "relative" for item in transitions)


def test_relative_minimum_prevents_spikes_against_zero_baseline(store: TelemetryStore) -> None:
    baseline(store, [0, 0, 0])
    ingest(store, [sample(99_999, -5)])
    assert DetectionEngine(store, CONFIG).evaluate(NOW, LIVE_BOUNDARY) == ()


def test_zero_usage_gaps_count_only_after_first_observation(store: TelemetryStore) -> None:
    ingest(store, [sample(40_000, -210), sample(110_000, -5)])
    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, LIVE_BOUNDARY)
    assert len(transitions) == 2
    assert all(item.baseline_rate == 0 for item in transitions)
    assert all(item.trigger == "relative" for item in transitions)


def test_aggregate_can_open_without_any_session_exceeding_its_threshold(
    store: TelemetryStore,
) -> None:
    ingest(store, [sample(200_000, -5, session_id=f"session-{index}") for index in range(6)])
    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, LIVE_BOUNDARY)
    assert len(transitions) == 1
    assert transitions[0].scope_type == "aggregate"
    assert transitions[0].scope_id == "all"
    assert transitions[0].observed_rate == 1_200_000
    assert transitions[0].absolute_threshold == 1_000_000


def test_unique_total_tokens_are_counted_once_despite_other_token_counters(
    store: TelemetryStore,
) -> None:
    usage = sample(130_000, -5)
    ingest(store, [usage, usage, replace(usage, total_tokens=900_000)])
    assert DetectionEngine(store, CONFIG).evaluate(NOW, LIVE_BOUNDARY) == ()


def test_configured_window_and_both_absolute_thresholds_are_used(store: TelemetryStore) -> None:
    ingest(store, [sample(50_000, -90), sample(50_000, -5)])
    config = replace(
        CONFIG, rate_window_seconds=120, session_absolute_tokens_per_minute=50_000,
        aggregate_absolute_tokens_per_minute=45_000,
    )
    transitions = DetectionEngine(store, config).evaluate(NOW, LIVE_BOUNDARY)
    assert len(transitions) == 2
    assert all(item.observed_rate == 50_000 for item in transitions)
    assert {item.absolute_threshold for item in transitions} == {50_000, 45_000}


def test_absolute_and_relative_conditions_produce_one_incident_per_scope(
    store: TelemetryStore,
) -> None:
    baseline(store, [40_000, 40_000, 40_000])
    ingest(store, [sample(260_000, -5)])
    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, LIVE_BOUNDARY)
    assert len(transitions) == 2
    assert next(item for item in transitions if item.scope_type == "session").trigger == (
        "absolute+relative"
    )
    assert next(item for item in transitions if item.scope_type == "aggregate").trigger == "relative"


def test_baseline_excludes_a_completed_bucket_overlapping_the_current_window(
    store: TelemetryStore,
) -> None:
    baseline(store, [40_000, 40_000, 40_000, 40_000])
    ingest(store, [sample(900_000, -50), sample(130_000, 25)])
    transitions = DetectionEngine(store, CONFIG).evaluate(NOW + timedelta(seconds=30), LIVE_BOUNDARY)
    assert len(transitions) == 2
    assert all(item.baseline_rate == 40_000 for item in transitions)
    assert all(item.observed_rate == 130_000 for item in transitions)


def test_baseline_window_excludes_older_buckets(store: TelemetryStore) -> None:
    baseline(store, [40_000, 40_000, 40_000])
    ingest(store, [sample(900_000, -270), sample(130_000, -5)])
    config = replace(CONFIG, baseline_window_seconds=240)
    transitions = DetectionEngine(store, config).evaluate(NOW, LIVE_BOUNDARY)
    assert len(transitions) == 2
    assert all(item.baseline_rate == 40_000 for item in transitions)


def test_utc_event_timestamps_and_inclusive_window_ignore_future_and_old_samples(
    store: TelemetryStore,
) -> None:
    in_window = replace(sample(130_000, -60), timestamp="2026-10-08T14:59:00+03:00")
    ingest(store, [sample(999_999, 1), sample(999_999, -60.000001),
                   sample(130_000, 0), in_window])
    transitions = DetectionEngine(store, CONFIG).evaluate(
        NOW.astimezone(timezone(timedelta(hours=3))), LIVE_BOUNDARY,
    )
    assert len(transitions) == 1
    assert transitions[0].observed_rate == 260_000
    assert transitions[0].opened_at == NOW


@pytest.mark.parametrize("seconds", [-30, 0])
def test_historical_reconciliation_never_opens_incident(
    store: TelemetryStore, seconds: int,
) -> None:
    ingest(store, [sample(2_000_000, seconds)])
    assert DetectionEngine(store, CONFIG).evaluate(NOW, live_after=NOW) == ()
    assert store.table_counts()["alert_incidents"] == 0


def test_historical_spike_cannot_combine_with_new_small_sample_to_open(
    store: TelemetryStore,
) -> None:
    ingest(store, [sample(2_000_000, -30), sample(1, -5)])
    assert DetectionEngine(store, CONFIG).evaluate(NOW, live_after=NOW - timedelta(seconds=10)) == ()


def test_history_can_inform_relative_baseline_without_opening_alerts(store: TelemetryStore) -> None:
    baseline(store, [40_000, 40_000, 40_000])
    ingest(store, [sample(130_000, -5)])
    transitions = DetectionEngine(store, CONFIG).evaluate(NOW, NOW - timedelta(seconds=10))
    assert len(transitions) == 2
    assert all(item.baseline_rate == 40_000 for item in transitions)


def test_persisted_open_incident_does_not_resend_after_engine_or_store_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as store:
        ingest(store, [sample(260_000, -5)])
        engine = DetectionEngine(store, CONFIG)
        opening = engine.evaluate(NOW, LIVE_BOUNDARY)
        assert len(opening) == 1
        assert engine.evaluate(NOW + timedelta(seconds=1), LIVE_BOUNDARY) == ()
        assert DetectionEngine(store, CONFIG).evaluate(NOW + timedelta(seconds=2), LIVE_BOUNDARY) == ()
    with TelemetryStore.open(path) as reopened:
        assert DetectionEngine(reopened, CONFIG).evaluate(
            NOW + timedelta(seconds=3), LIVE_BOUNDARY,
        ) == ()
        assert reopened.table_counts()["alert_incidents"] == 1


def opened_engine(
    store: TelemetryStore, config: DetectorConfig = RECOVERY_CONFIG,
) -> tuple[DetectionEngine, str]:
    ingest(store, [sample(260_000, -5)])
    engine = DetectionEngine(store, config)
    opening = engine.evaluate(NOW, LIVE_BOUNDARY)
    assert len(opening) == 1
    return engine, opening[0].incident_id


def test_recovery_requires_continuous_configured_duration(store: TelemetryStore) -> None:
    engine, incident_id = opened_engine(store)
    assert engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY) == ()
    assert engine.evaluate(NOW + timedelta(seconds=359), LIVE_BOUNDARY) == ()
    recovery = engine.evaluate(NOW + timedelta(seconds=360), LIVE_BOUNDARY)
    assert len(recovery) == 1
    assert recovery[0].state == "recovered"
    assert recovery[0].incident_id == incident_id
    assert recovery[0].observed_rate == 0
    assert recovery[0].opened_at == NOW
    assert recovery[0].recovered_at == NOW + timedelta(seconds=360)
    assert recovery[0].trigger == "absolute"
    row = store.connection.execute("SELECT * FROM alert_incidents").fetchone()
    assert row["below_since"] == (NOW + timedelta(seconds=60)).isoformat()
    assert row["recovered_at"] == (NOW + timedelta(seconds=360)).isoformat()
    assert row["observed_rate"] == 260_000


def test_recovery_uses_a_changed_configured_duration(store: TelemetryStore) -> None:
    engine, _ = opened_engine(store, replace(RECOVERY_CONFIG, recovery_seconds=7))
    assert engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY) == ()
    assert engine.evaluate(NOW + timedelta(seconds=66), LIVE_BOUNDARY) == ()
    recovery = engine.evaluate(NOW + timedelta(seconds=67), LIVE_BOUNDARY)
    assert len(recovery) == 1
    assert recovery[0].state == "recovered"


def test_above_threshold_sample_resets_recovery_progress(store: TelemetryStore) -> None:
    engine, _ = opened_engine(store)
    assert engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY) == ()
    ingest(store, [sample(260_000, 175)])
    assert engine.evaluate(NOW + timedelta(seconds=180), LIVE_BOUNDARY) == ()
    assert store.connection.execute("SELECT below_since FROM alert_incidents").fetchone()[0] is None
    assert engine.evaluate(NOW + timedelta(seconds=240), LIVE_BOUNDARY) == ()
    assert engine.evaluate(NOW + timedelta(seconds=539), LIVE_BOUNDARY) == ()
    recovery = engine.evaluate(NOW + timedelta(seconds=540), LIVE_BOUNDARY)
    assert len(recovery) == 1
    assert recovery[0].state == "recovered"


def test_relative_condition_alone_prevents_recovery(store: TelemetryStore) -> None:
    baseline(store, [40_000, 40_000, 40_000])
    ingest(store, [sample(130_000, -5)])
    engine = DetectionEngine(store, replace(CONFIG, recovery_seconds=10))
    assert len(engine.evaluate(NOW, LIVE_BOUNDARY)) == 2
    assert engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY) == ()
    ingest(store, [sample(130_000, 65)])
    assert engine.evaluate(NOW + timedelta(seconds=70), LIVE_BOUNDARY) == ()
    assert all(row[0] is None for row in store.connection.execute(
        "SELECT below_since FROM alert_incidents",
    ))


def test_recovery_progress_survives_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as store:
        engine, incident_id = opened_engine(store)
        assert engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY) == ()
    with TelemetryStore.open(path) as reopened:
        engine = DetectionEngine(reopened, RECOVERY_CONFIG)
        assert engine.evaluate(NOW + timedelta(seconds=359), LIVE_BOUNDARY) == ()
        recovered = engine.evaluate(NOW + timedelta(seconds=360), LIVE_BOUNDARY)
        assert len(recovered) == 1
        assert recovered[0].incident_id == incident_id
        assert recovered[0].state == "recovered"
    with TelemetryStore.open(path) as reopened_again:
        assert DetectionEngine(reopened_again, RECOVERY_CONFIG).evaluate(
            NOW + timedelta(seconds=361), LIVE_BOUNDARY,
        ) == ()


@pytest.mark.parametrize("scope_type", ["session", "aggregate"])
def test_advanced_live_boundary_restarts_persisted_recovery_after_pre_restart_burst(
    tmp_path: Path, scope_type: str,
) -> None:
    config = RECOVERY_CONFIG if scope_type == "session" else replace(
        RECOVERY_CONFIG, session_absolute_tokens_per_minute=1_000_000,
        aggregate_absolute_tokens_per_minute=250_000,
    )
    path = tmp_path / "telemetry.db"
    restart_boundary = NOW + timedelta(seconds=360)
    with TelemetryStore.open(path) as store:
        engine, incident_id = opened_engine(store, config)
        assert engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY) == ()
        ingest(store, [sample(260_000, 350)])
    with TelemetryStore.open(path) as reopened:
        engine = DetectionEngine(reopened, config)
        assert engine.evaluate(restart_boundary, live_after=restart_boundary) == ()
        row = reopened.connection.execute(
            "SELECT incident_id, below_since, recovered_at FROM alert_incidents",
        ).fetchone()
        assert row["incident_id"] == incident_id
        assert row["below_since"] == restart_boundary.isoformat()
        assert row["recovered_at"] is None
        assert engine.evaluate(NOW + timedelta(seconds=659), restart_boundary) == ()
        recovery = engine.evaluate(NOW + timedelta(seconds=660), restart_boundary)
        assert len(recovery) == 1
        assert recovery[0].state == "recovered"
        assert recovery[0].scope_type == scope_type
        assert recovery[0].incident_id == incident_id
        assert recovery[0].opened_at == NOW
        assert recovery[0].recovered_at == NOW + timedelta(seconds=660)
        assert reopened.table_counts()["alert_incidents"] == 1
        assert DetectionEngine(reopened, config).evaluate(
            NOW + timedelta(seconds=661), restart_boundary,
        ) == ()


def test_recovery_rearms_one_new_incident_for_a_later_spike(store: TelemetryStore) -> None:
    engine, first_id = opened_engine(store, replace(RECOVERY_CONFIG, recovery_seconds=7))
    engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY)
    assert len(engine.evaluate(NOW + timedelta(seconds=67), LIVE_BOUNDARY)) == 1
    ingest(store, [sample(260_000, 80)])
    opening = engine.evaluate(NOW + timedelta(seconds=85), LIVE_BOUNDARY)
    assert len(opening) == 1
    assert opening[0].state == "opened"
    assert opening[0].incident_id != first_id
    assert store.table_counts()["alert_incidents"] == 2
    assert engine.evaluate(NOW + timedelta(seconds=86), LIVE_BOUNDARY) == ()


def test_backward_evaluations_cannot_shorten_recovery_or_reopen_completed_incident(
    store: TelemetryStore,
) -> None:
    engine, _ = opened_engine(store, replace(RECOVERY_CONFIG, recovery_seconds=7))
    engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY)
    assert engine.evaluate(NOW + timedelta(seconds=56), LIVE_BOUNDARY) == ()
    assert store.connection.execute("SELECT below_since FROM alert_incidents").fetchone()[0] == (
        NOW + timedelta(seconds=60)
    ).isoformat()
    assert engine.evaluate(NOW + timedelta(seconds=66), LIVE_BOUNDARY) == ()
    assert len(engine.evaluate(NOW + timedelta(seconds=67), LIVE_BOUNDARY)) == 1
    assert DetectionEngine(store, RECOVERY_CONFIG).evaluate(NOW, LIVE_BOUNDARY) == ()
    assert store.table_counts()["alert_incidents"] == 1


def test_sessions_and_aggregate_recover_independently(store: TelemetryStore) -> None:
    config = replace(
        RECOVERY_CONFIG, aggregate_absolute_tokens_per_minute=400_000, recovery_seconds=120,
    )
    ingest(store, [sample(300_000, -5, session_id="session-1"),
                   sample(300_000, -5, session_id="session-2")])
    engine = DetectionEngine(store, config)
    assert len(engine.evaluate(NOW, LIVE_BOUNDARY)) == 3
    assert engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY) == ()
    ingest(store, [sample(300_000, 90), sample(300_000, 151)])
    assert engine.evaluate(NOW + timedelta(seconds=100), LIVE_BOUNDARY) == ()
    recovery = engine.evaluate(NOW + timedelta(seconds=180), LIVE_BOUNDARY)
    assert {(item.scope_type, item.scope_id) for item in recovery} == {
        ("session", "session-2"), ("aggregate", "all"),
    }
    assert all(item.state == "recovered" for item in recovery)


def test_out_of_order_samples_use_event_time_without_negative_rates(store: TelemetryStore) -> None:
    ingest(store, [sample(130_000, -5), sample(999_999, -300), sample(130_000, -30)])
    transitions = DetectionEngine(store, RECOVERY_CONFIG).evaluate(NOW, LIVE_BOUNDARY)
    assert len(transitions) == 1
    assert transitions[0].observed_rate == 260_000
    assert all(item.observed_rate >= 0 for item in transitions)


def test_burst_between_evaluations_resets_continuous_recovery(store: TelemetryStore) -> None:
    engine, _ = opened_engine(store)
    engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY)
    ingest(store, [sample(260_000, 120)])
    assert engine.evaluate(NOW + timedelta(seconds=360), LIVE_BOUNDARY) == ()
    assert engine.evaluate(NOW + timedelta(seconds=659), LIVE_BOUNDARY) == ()
    recovery = engine.evaluate(NOW + timedelta(seconds=660), LIVE_BOUNDARY)
    assert len(recovery) == 1
    assert recovery[0].state == "recovered"


def test_late_out_of_order_burst_resets_persisted_recovery(store: TelemetryStore) -> None:
    engine, _ = opened_engine(store)
    engine.evaluate(NOW + timedelta(seconds=60), LIVE_BOUNDARY)
    engine.evaluate(NOW + timedelta(seconds=250), LIVE_BOUNDARY)
    ingest(store, [sample(260_000, 120)])
    assert engine.evaluate(NOW + timedelta(seconds=360), LIVE_BOUNDARY) == ()
    row = store.connection.execute("SELECT below_since, recovered_at FROM alert_incidents").fetchone()
    assert row["below_since"] == (NOW + timedelta(seconds=360)).isoformat()
    assert row["recovered_at"] is None


@pytest.mark.parametrize("argument", ["now", "live_after"])
def test_naive_evaluation_timestamps_are_rejected(store: TelemetryStore, argument: str) -> None:
    arguments = {"now": NOW, "live_after": LIVE_BOUNDARY}
    arguments[argument] = arguments[argument].replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        DetectionEngine(store, CONFIG).evaluate(**arguments)
