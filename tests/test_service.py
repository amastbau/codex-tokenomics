import json
import signal
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_tokenomics import service as service_module
from codex_tokenomics.collector import Collector
from codex_tokenomics.config import CollectorConfig, PathsConfig, load_config
from codex_tokenomics.detector import DetectionEngine, IncidentTransition
from codex_tokenomics.storage import TelemetryStore

NOW = datetime(2026, 10, 8, 8, tzinfo=UTC)
SECRET = "PROHIBITED-SERVICE-CONTENT-2c913a"


@dataclass
class FakeClock:
    current: datetime = NOW
    sleeps: list[float] = field(default_factory=list)
    on_sleep: Callable[[], None] | None = None

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep()


@dataclass
class FakeDispatcher:
    transitions: list[IncidentTransition] = field(default_factory=list)

    def dispatch(self, transition: IncidentTransition) -> None:
        self.transitions.append(transition)

    @property
    def email_count(self) -> int:
        return sum(item.state == "opened" for item in self.transitions)


@dataclass
class ServiceHarness:
    store: TelemetryStore
    collector: Collector
    detector: DetectionEngine
    dispatcher: FakeDispatcher
    clock: FakeClock
    service: service_module.MonitorService
    path: Path

    def append(self, *events: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as stream:
            for event in events:
                stream.write(json.dumps(event).encode() + b"\n")

    def usage(self, response_id: str, tokens: int, *, timestamp: datetime | None = None) -> dict:
        return {
            "type": "token_usage_record", "timestamp": (timestamp or self.clock.now()).isoformat(),
            "payload": {
                "response_id": response_id,
                "usage": {"input_tokens": tokens - 10, "cached_input_tokens": 7,
                          "cache_write_input_tokens": 3, "output_tokens": 10,
                          "reasoning_output_tokens": 5, "total_tokens": tokens},
                "message": SECRET,
            },
        }


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[ServiceHarness]:
    config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
    config = replace(config, paths=PathsConfig(tmp_path / "sessions", tmp_path / "telemetry.db"),
                     collector=CollectorConfig(0.25),
                     detector=replace(config.detector, relative_minimum_tokens_per_minute=3_000_000))
    store = TelemetryStore.open(config.paths.database)
    collector = Collector(config.paths.session_root, store)
    detector = DetectionEngine(store, config.detector)
    dispatcher = FakeDispatcher()
    clock = FakeClock()
    service = service_module.MonitorService(config, store, collector, detector, dispatcher, clock)
    fixture = ServiceHarness(store, collector, detector, dispatcher, clock, service,
                             config.paths.session_root / "2026" / "10" / "08" / "rollout.jsonl")
    fixture.append({"type": "session_meta", "timestamp": NOW.isoformat(),
                    "payload": {"id": "session-1", "source": "cli", "thread_source": "user",
                                "base_instructions": SECRET}})
    yield fixture
    fixture.store.close()


def test_reconciliation_imports_history_without_alerting(harness: ServiceHarness) -> None:
    harness.append(harness.usage("historical-spike", 900_000))
    boundary = harness.service.reconcile()

    assert harness.store.total_tokens() == 900_000
    assert harness.store.table_counts()["alert_incidents"] == 0
    assert harness.dispatcher.transitions == []
    assert boundary.started_at == NOW
    assert harness.service.run_once().opened_incidents == 0
    assert harness.clock.sleeps == []


def test_live_append_opens_once_with_required_token_breakdown(harness: ServiceHarness) -> None:
    harness.service.reconcile()
    harness.clock.advance(1)
    harness.append(harness.usage("live-spike", 260_000))

    cycle = harness.service.run_once()
    assert cycle.opened_incidents == 1
    assert cycle.recovered_incidents == 0
    assert cycle.collection.records_inserted == 1
    assert cycle.transitions == tuple(harness.dispatcher.transitions)
    assert harness.dispatcher.email_count == 1
    assert harness.store.table_counts()["alert_incidents"] == 1
    tokens = cycle.transitions[0].token_breakdown
    assert (tokens.input_tokens, tokens.cached_input_tokens, tokens.cache_write_input_tokens,
            tokens.output_tokens, tokens.reasoning_output_tokens, tokens.total_tokens) == (
        259_990, 7, 3, 10, 5, 260_000,
    )

    assert harness.service.run_once().transitions == ()
    assert harness.dispatcher.email_count == 1
    assert harness.clock.sleeps == []


def test_run_once_reconciles_before_live_detection(harness: ServiceHarness) -> None:
    harness.append(harness.usage("old-spike", 260_000))

    cycle = harness.service.run_once()

    assert harness.store.total_tokens() == 260_000
    assert cycle.opened_incidents == 0
    assert harness.dispatcher.transitions == []
    assert harness.service.live_boundary.started_at == NOW


def test_boundary_is_established_after_scan_and_reconcile_is_idempotent(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan = harness.collector.scan_once

    def delayed_scan():
        result = scan()
        harness.clock.advance(20)
        return result

    monkeypatch.setattr(harness.collector, "scan_once", delayed_scan)
    boundary = harness.service.reconcile()
    assert boundary.started_at == NOW + timedelta(seconds=20)

    harness.clock.advance(1)
    harness.append(harness.usage("live-after-import", 260_000))
    assert harness.service.reconcile() == boundary
    assert harness.service.run_once().opened_incidents == 1


def test_restart_resumes_cursor_without_duplicate_alert(harness: ServiceHarness) -> None:
    harness.service.reconcile()
    harness.clock.advance(1)
    harness.append(harness.usage("live-before-restart", 260_000))
    harness.service.run_once()
    database_path = harness.store.path
    saved_offset = harness.store.get_cursor(str(harness.path)).offset
    harness.store.close()
    harness.store = TelemetryStore.open(database_path)
    harness.collector = Collector(harness.service.config.paths.session_root, harness.store)
    harness.detector = DetectionEngine(harness.store, harness.service.config.detector)
    harness.clock.advance(10)
    restarted = service_module.MonitorService(harness.service.config, harness.store,
                                              harness.collector, harness.detector,
                                              harness.dispatcher, harness.clock)

    assert restarted.run_once().transitions == ()
    assert restarted.live_boundary.started_at == NOW + timedelta(seconds=11)
    assert harness.store.get_cursor(str(harness.path)).offset == saved_offset
    assert harness.store.table_counts()["alert_incidents"] == 1
    assert harness.dispatcher.email_count == 1


def test_live_recovery_dispatches_once(harness: ServiceHarness) -> None:
    harness.service.reconcile()
    harness.clock.advance(1)
    harness.append(harness.usage("live-spike", 260_000))
    opening = harness.service.run_once().transitions[0]
    harness.clock.advance(61)
    assert harness.service.run_once().transitions == ()
    harness.clock.advance(300)

    cycle = harness.service.run_once()

    assert (cycle.opened_incidents, cycle.recovered_incidents) == (0, 1)
    assert cycle.transitions[0].incident_id == opening.incident_id
    assert cycle.transitions[0].token_breakdown.total_tokens == 0
    assert [(item.state, item.incident_id) for item in harness.dispatcher.transitions] == [
        ("opened", opening.incident_id), ("recovered", opening.incident_id),
    ]
    assert harness.service.run_once().transitions == ()


def test_empty_tree_still_records_content_free_heartbeat(harness: ServiceHarness) -> None:
    harness.path.unlink()
    harness.service.reconcile()
    harness.clock.advance(3)
    cycle = harness.service.run_once()
    health = harness.service.health()

    assert cycle.collection.files_seen == 0
    assert health.last_poll_at == (NOW + timedelta(seconds=3)).isoformat()
    assert health.last_success_at == health.last_poll_at
    assert health.files_seen == 0
    assert health.parse_failures == health.notification_failures == 0
    assert health.integrity_check == "ok"
    assert health.service_version == "0.1.0"
    assert health.status == "ok"
    persisted = harness.store.health_snapshot()
    assert persisted["last_success_at"] == health.last_success_at
    assert SECRET not in json.dumps(persisted)


def test_health_reports_parse_failures_and_backlog_without_content(harness: ServiceHarness) -> None:
    harness.service.reconcile()
    harness.clock.advance(1)
    with harness.path.open("ab") as stream:
        stream.write(b'{"secret":"' + SECRET.encode() + b'"\n')
        stream.write(b'{"secret":"' + SECRET.encode())

    cycle = harness.service.run_once()
    health = harness.service.health()

    assert cycle.collection.parse_failures == 1
    assert health.parse_failures == 1
    assert health.backlog_bytes == len(b'{"secret":"' + SECRET.encode())
    assert health.lag_seconds == 0
    harness.clock.advance(4)
    assert harness.service.health().lag_seconds == 4
    assert SECRET not in json.dumps(asdict(health))
    assert SECRET not in "\n".join(harness.store.connection.iterdump())
    assert harness.service.run_once().collection.parse_failures == 0
    assert harness.service.health().parse_failures == 1


def test_health_before_poll_reports_unknown_lag_and_unread_file_size(harness: ServiceHarness) -> None:
    health = harness.service.health()

    assert health.last_poll_at is None
    assert health.last_success_at is None
    assert health.lag_seconds is None
    assert health.backlog_bytes == harness.path.stat().st_size


def test_backlog_includes_new_files_and_full_replaced_files(harness: ServiceHarness) -> None:
    harness.service.reconcile()
    old_path = harness.path.with_suffix(".old")
    harness.path.rename(old_path)
    harness.append(harness.usage("replaced", 260_000))
    new_path = harness.path.parent / "new-date" / "new.jsonl"
    new_path.parent.mkdir()
    new_path.write_bytes(b"partial-new-file")
    (new_path.parent / "ignored.txt").write_bytes(b"ignored")

    health = harness.service.health()

    assert health.backlog_bytes == harness.path.stat().st_size + 16


def test_notification_failure_counts_are_exposed_without_raw_error(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_delivery(transition: IncidentTransition) -> None:
        harness.store.record_notification_attempt(
            incident_id=transition.incident_id, channel="email", attempted_at=harness.clock.now(),
            attempt_number=1, outcome_code="failed",
        )

    monkeypatch.setattr(harness.dispatcher, "dispatch", failed_delivery)
    harness.service.reconcile()
    harness.clock.advance(1)
    harness.append(harness.usage("live-spike", 260_000))
    harness.service.run_once()

    health = harness.service.health()
    assert health.notification_failures == 1
    assert health.last_success_at == harness.clock.now().isoformat()
    assert SECRET not in json.dumps(asdict(health))


def test_failed_scan_preserves_last_success_after_partial_collection(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    harness.service.reconcile()
    scan = harness.collector.scan_once

    def interrupted_scan():
        scan()
        raise OSError(SECRET)

    monkeypatch.setattr(harness.collector, "scan_once", interrupted_scan)
    harness.clock.advance(10)
    with pytest.raises(service_module.ServiceError, match="collection_failed") as caught:
        harness.service.run_once()

    health = harness.service.health()
    assert health.last_poll_at == (NOW + timedelta(seconds=10)).isoformat()
    assert health.last_success_at == NOW.isoformat()
    assert health.lag_seconds == 10
    assert health.status == "degraded"
    assert caught.value.__suppress_context__
    assert SECRET not in str(caught.value)
    assert SECRET not in json.dumps(asdict(health))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err

    monkeypatch.setattr(harness.collector, "scan_once", scan)
    harness.clock.advance(1)
    harness.service.run_once()
    assert harness.service.health().status == "ok"
    assert harness.service.health().lag_seconds == 0


def test_failed_reconciliation_does_not_establish_live_boundary(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan = harness.collector.scan_once

    def fail():
        raise OSError(SECRET)

    monkeypatch.setattr(harness.collector, "scan_once", fail)
    with pytest.raises(service_module.ServiceError, match="collection_failed"):
        harness.service.reconcile()
    assert harness.service.live_boundary is None
    assert harness.service.health().last_success_at is None

    harness.clock.advance(5)
    monkeypatch.setattr(harness.collector, "scan_once", scan)
    assert harness.service.reconcile().started_at == NOW + timedelta(seconds=5)


def test_health_degrades_without_raw_database_error(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable():
        raise sqlite3.DatabaseError(SECRET)

    monkeypatch.setattr(harness.store, "health_snapshot", unavailable)
    health = harness.service.health()

    assert health.status == "degraded"
    assert health.integrity_check == "unavailable"
    assert health.parse_failures is None
    assert health.notification_failures is None
    assert health.backlog_bytes is None
    assert SECRET not in json.dumps(asdict(health))


def test_naive_clock_is_rejected_instead_of_using_local_timezone(harness: ServiceHarness) -> None:
    harness.clock.current = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        harness.service.reconcile()
    assert harness.service.live_boundary is None


@pytest.fixture
def signal_handlers(monkeypatch: pytest.MonkeyPatch) -> dict:
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    def register(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(signal, "getsignal", lambda signum: handlers[signum])
    monkeypatch.setattr(signal, "signal", register)
    return handlers


def test_run_forever_uses_only_configured_interval_and_closes_on_stop(
    harness: ServiceHarness, signal_handlers: dict,
) -> None:
    original_handlers = dict(signal_handlers)

    def next_poll() -> None:
        if len(harness.clock.sleeps) == 1:
            harness.append(harness.usage("live-between-polls", 260_000))
        else:
            harness.service.stop()

    harness.clock.on_sleep = next_poll
    harness.service.run_forever()

    assert harness.clock.sleeps == [0.25, 0.25]
    assert harness.dispatcher.email_count == 1
    assert signal_handlers == original_handlers
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        harness.store.connection.execute("SELECT 1")
    with TelemetryStore.open(harness.store.path) as reopened:
        assert reopened.total_tokens() == 260_000
        assert reopened.table_counts()["alert_incidents"] == 1
        assert reopened.health_snapshot()["last_success_at"] == (
            NOW + timedelta(seconds=0.25)
        ).isoformat()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signals_finish_active_cycle_before_closing_without_extra_sleep(
    harness: ServiceHarness, signal_handlers: dict, monkeypatch: pytest.MonkeyPatch, signum: int,
) -> None:
    original_handlers = dict(signal_handlers)
    harness.service.reconcile()
    harness.clock.advance(1)
    harness.append(harness.usage("two-scopes", 260_000))
    harness.detector.config = replace(harness.detector.config,
                                      aggregate_absolute_tokens_per_minute=250_000)
    dispatch = harness.dispatcher.dispatch

    def stop_during_dispatch(transition: IncidentTransition) -> None:
        dispatch(transition)
        signal_handlers[signum](signum, None)

    monkeypatch.setattr(harness.dispatcher, "dispatch", stop_during_dispatch)
    harness.service.run_forever()

    assert [item.scope_type for item in harness.dispatcher.transitions] == ["session", "aggregate"]
    assert harness.clock.sleeps == []
    assert signal_handlers == original_handlers
    with TelemetryStore.open(harness.store.path) as reopened:
        assert reopened.table_counts()["alert_incidents"] == 2
        assert reopened.health_snapshot()["last_success_at"] == harness.clock.now().isoformat()


def test_stop_during_reconciliation_prevents_live_evaluation(
    harness: ServiceHarness, signal_handlers: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.append(harness.usage("historical-during-shutdown", 260_000))
    scan = harness.collector.scan_once

    def stop_after_scan():
        result = scan()
        signal_handlers[signal.SIGTERM](signal.SIGTERM, None)
        return result

    def forbidden_evaluation(*args):
        pytest.fail("shutdown during historical reconciliation must not run live detection")

    monkeypatch.setattr(harness.collector, "scan_once", stop_after_scan)
    monkeypatch.setattr(harness.detector, "evaluate", forbidden_evaluation)
    harness.service.run_forever()

    assert harness.dispatcher.transitions == []
    assert harness.clock.sleeps == []
    with TelemetryStore.open(harness.store.path) as reopened:
        assert reopened.total_tokens() == 260_000
        assert reopened.table_counts()["alert_incidents"] == 0


def test_keyboard_interrupt_during_sleep_closes_database_and_restores_handlers(
    harness: ServiceHarness, signal_handlers: dict,
) -> None:
    original_handlers = dict(signal_handlers)

    def interrupted_sleep() -> None:
        raise KeyboardInterrupt

    harness.clock.on_sleep = interrupted_sleep
    harness.service.run_forever()

    assert signal_handlers == original_handlers
    assert harness.clock.sleeps == [0.25]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        harness.store.connection.execute("SELECT 1")


def test_failed_forever_loop_closes_database_and_restores_handlers(
    harness: ServiceHarness, signal_handlers: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_handlers = dict(signal_handlers)

    def inaccessible():
        raise PermissionError(SECRET)

    monkeypatch.setattr(harness.collector, "scan_once", inaccessible)
    with pytest.raises(service_module.ServiceError, match="collection_failed"):
        harness.service.run_forever()

    assert signal_handlers == original_handlers
    assert harness.clock.sleeps == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        harness.store.connection.execute("SELECT 1")


def test_health_degrades_when_session_tree_cannot_be_scanned(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.service.reconcile()

    def inaccessible(root, *, onerror):
        onerror(PermissionError(SECRET))

    monkeypatch.setattr(service_module.os, "walk", inaccessible)
    health = harness.service.health()

    assert health.status == "degraded"
    assert health.backlog_bytes is None
    assert health.last_success_at == NOW.isoformat()
    assert SECRET not in json.dumps(asdict(health))


@pytest.mark.parametrize("stage", ["detection", "dispatch"])
def test_failed_live_stage_preserves_heartbeat_without_raw_exception(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    harness.service.reconcile()
    harness.clock.advance(10)
    harness.append(harness.usage("live-spike", 260_000))

    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)

    if stage == "detection":
        monkeypatch.setattr(harness.detector, "evaluate", fail)
    else:
        monkeypatch.setattr(harness.dispatcher, "dispatch", fail)

    with pytest.raises(service_module.ServiceError, match=f"{stage}_failed") as caught:
        harness.service.run_once()
    health = harness.service.health()

    assert health.last_poll_at == harness.clock.now().isoformat()
    assert health.last_success_at == NOW.isoformat()
    assert health.status == "degraded"
    assert caught.value.__suppress_context__
    assert SECRET not in str(caught.value)
    assert SECRET not in json.dumps(asdict(health))


def test_reconciliation_never_invokes_detector_or_dispatcher(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.append(harness.usage("history", 260_000))

    def forbidden(*args):
        pytest.fail("historical reconciliation invoked a live alert component")

    monkeypatch.setattr(harness.detector, "evaluate", forbidden)
    monkeypatch.setattr(harness.dispatcher, "dispatch", forbidden)

    assert harness.service.reconcile().started_at == NOW
    assert harness.store.total_tokens() == 260_000


def test_health_exposes_failed_integrity_as_degraded_status(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.service.reconcile()
    monkeypatch.setattr(harness.store, "integrity_check", lambda: "failed")

    health = harness.service.health()

    assert health.integrity_check == "failed"
    assert health.status == "degraded"
    assert health.last_success_at == NOW.isoformat()


def test_heartbeat_transaction_failure_preserves_previous_completed_cycle(
    harness: ServiceHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.service.reconcile()
    harness.clock.advance(10)
    evaluate = harness.detector.evaluate

    def reject_success_heartbeat(now: datetime, live_after: datetime, **kwargs):
        transitions = evaluate(now, live_after, **kwargs)
        harness.store.connection.execute(
            "CREATE TEMP TRIGGER reject_heartbeat BEFORE UPDATE ON service_health "
            "WHEN NEW.last_success_at='2026-10-08T08:00:10+00:00' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic heartbeat failure'); END"
        )
        return transitions

    monkeypatch.setattr(harness.detector, "evaluate", reject_success_heartbeat)
    with pytest.raises(service_module.ServiceError, match="heartbeat_failed"):
        harness.service.run_once()

    health = harness.service.health()
    assert health.last_poll_at == (NOW + timedelta(seconds=10)).isoformat()
    assert health.last_success_at == NOW.isoformat()
    assert health.status == "degraded"
    assert not harness.store.connection.in_transaction


@pytest.mark.parametrize("restart", [False, True])
def test_future_dated_reconciled_usage_never_becomes_live_when_clock_catches_up(
    harness: ServiceHarness, restart: bool,
) -> None:
    harness.append(harness.usage("future-history", 260_000, timestamp=NOW + timedelta(seconds=30)))
    harness.service.reconcile()
    assert harness.store.total_tokens() == 260_000
    assert harness.dispatcher.transitions == []
    if restart:
        config = harness.service.config
        harness.store.close()
        harness.store = TelemetryStore.open(config.paths.database)
        harness.collector = Collector(config.paths.session_root, harness.store)
        harness.detector = DetectionEngine(harness.store, config.detector)
        harness.clock.advance(5)
        harness.service = service_module.MonitorService(config, harness.store, harness.collector,
                                                       harness.detector, harness.dispatcher,
                                                       harness.clock)
        harness.service.reconcile()
    harness.clock.advance(30)

    cycle = harness.service.run_once()

    assert cycle.collection.records_inserted == 0
    assert cycle.transitions == ()
    assert harness.store.table_counts()["alert_incidents"] == 0
    assert harness.dispatcher.email_count == 0
    harness.clock.advance(1)
    harness.append(harness.usage("small-live-append", 10))
    cycle = harness.service.run_once()
    assert cycle.collection.records_inserted == 1
    assert cycle.transitions == ()
    harness.append(harness.usage("future-history", 500_000))
    replay = harness.service.run_once()
    assert replay.collection.records_inserted == 0
    assert replay.collection.duplicates == 1
    assert replay.transitions == ()


@pytest.mark.parametrize("event_offset", [-10, 0])
def test_usage_ingested_after_reconciliation_is_live_at_or_before_event_time_boundary(
    harness: ServiceHarness, event_offset: int,
) -> None:
    harness.service.reconcile()
    harness.clock.advance(1)
    harness.append(harness.usage("late-live-spike", 260_000,
                                timestamp=NOW + timedelta(seconds=event_offset)))

    cycle = harness.service.run_once()

    assert cycle.collection.records_inserted == 1
    assert cycle.opened_incidents == 1
    assert cycle.transitions[0].observed_rate == 260_000
    assert cycle.transitions[0].token_breakdown.total_tokens == 260_000
    assert cycle.transitions[0].opened_at == NOW + timedelta(seconds=1)
    assert harness.dispatcher.email_count == 1
    assert harness.service.run_once().transitions == ()


def test_new_ingestion_still_uses_event_time_window_for_old_and_future_usage(
    harness: ServiceHarness,
) -> None:
    harness.service.reconcile()
    harness.clock.advance(1)
    harness.append(harness.usage("old-live", 260_000, timestamp=NOW - timedelta(seconds=60)),
                   harness.usage("future-live", 260_000, timestamp=NOW + timedelta(seconds=30)))

    initial = harness.service.run_once()
    assert initial.collection.records_inserted == 2
    assert initial.transitions == ()
    harness.clock.advance(29)
    later = harness.service.run_once()
    assert later.collection.records_inserted == 0
    assert later.opened_incidents == 1
    assert later.transitions[0].observed_rate == 260_000
    assert later.transitions[0].token_breakdown.total_tokens == 260_000
