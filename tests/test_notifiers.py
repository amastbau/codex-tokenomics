import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_tokenomics import notifiers
from codex_tokenomics.config import NotificationConfig
from codex_tokenomics.detector import IncidentTransition, TokenBreakdown
from codex_tokenomics.notifiers import (
    AlertView,
    CommandResult,
    DesktopNotifier,
    EmailNotifier,
    SubprocessRunner,
)
from codex_tokenomics.storage import IngestCursor, TelemetryStore
from codex_tokenomics.telemetry import SessionRecord, TurnRecord

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
SECRET = "PROHIBITED-NOTIFICATION-CONTENT-87af"
DISCLOSURE = "Automated by Codex Tokenomics; implementation assisted by Codex"
OPEN = IncidentTransition(
    incident_id="incident-1", scope_type="session", scope_id="session-1", state="opened",
    trigger="absolute+relative", observed_rate=260_000, baseline_rate=40_000,
    absolute_threshold=250_000, opened_at=NOW, recovered_at=None,
    token_breakdown=TokenBreakdown(210_000, 120_000, 40_000, 50_000, 30_000, 260_000),
)
RECOVERY = replace(
    OPEN, state="recovered", observed_rate=0, recovered_at=NOW + timedelta(minutes=5),
    token_breakdown=TokenBreakdown(0, 0, 0, 0, 0, 0),
)
CONFIG = NotificationConfig("amastbau@redhat.com", (10, 30))


class FakeRunner:
    def __init__(self, results: Sequence[CommandResult | Exception] = ()) -> None:
        self.calls: list[list[str]] = []
        self.results = list(results)

    def run(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(list(argv))
        result = self.results.pop(0) if self.results else CommandResult(0, SECRET, SECRET)
        if isinstance(result, Exception):
            raise result
        return result

    def calls_named(self, command: str) -> list[list[str]]:
        return [argv for argv in self.calls if argv[0] == command]


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TelemetryStore]:
    with TelemetryStore.open(tmp_path / "telemetry.db") as opened:
        yield opened


def test_email_contains_only_transition_telemetry_and_disclosure() -> None:
    runner = FakeRunner()
    assert EmailNotifier(runner, "amastbau@redhat.com").send(OPEN) == "sent"
    argv = runner.calls_named("gws")[0]
    assert argv[:5] == ["gws", "gmail", "+send", "--to", "amastbau@redhat.com"]
    assert len(argv) == 9
    assert argv[5] == "--subject"
    assert argv[7] == "--body"
    body = argv[8]
    for value in ("incident-1", "session-1", "absolute+relative", "260000", "40000", "250000",
                  "2026-10-08T12:00:00+00:00", DISCLOSURE):
        assert value in body
    assert SECRET not in body


def test_email_validation_appends_dry_run_and_returns_non_delivery() -> None:
    runner = FakeRunner()
    assert EmailNotifier(runner, "amastbau@redhat.com").validate(OPEN) == "dry_run"
    assert len(runner.calls) == 1
    assert runner.calls[0][:3] == ["gws", "gmail", "+send"]
    assert runner.calls[0][-1] == "--dry-run"


def test_desktop_open_uses_critical_argv_and_recovery_uses_normal() -> None:
    runner = FakeRunner()
    desktop = DesktopNotifier(runner)
    assert desktop.send_open(OPEN) == "sent"
    assert desktop.send_recovery(RECOVERY) == "sent"
    assert runner.calls[0][:3] == [
        "notify-send", "--urgency=critical", "--app-name=Codex Tokenomics",
    ]
    assert runner.calls[1][:3] == [
        "notify-send", "--urgency=normal", "--app-name=Codex Tokenomics",
    ]
    assert "recovered" in runner.calls[1][3].lower()
    assert "2026-10-08T12:05:00+00:00" in runner.calls[1][4]
    assert DISCLOSURE in runner.calls[0][4]


def test_desktop_escapes_markup_and_keeps_identifiers_as_one_argument() -> None:
    runner = FakeRunner()
    transition = replace(OPEN, scope_id="session <b>& `echo` $(touch /tmp/never)\nspoof")
    DesktopNotifier(runner).send_open(transition)
    assert len(runner.calls[0]) == 5
    body = runner.calls[0][4]
    assert "session &lt;b&gt;&amp; `echo` $(touch /tmp/never)" in body
    assert "\nspoof" not in body


def test_alert_enriches_only_agent_kind_and_latest_non_null_model(store: TelemetryStore) -> None:
    store.ingest([
        SessionRecord("session-1", NOW.isoformat(), source="subagent", agent_kind="reviewer",
                      cwd=SECRET, workspace_roots=(SECRET,)),
        TurnRecord("session-1", (NOW - timedelta(minutes=2)).isoformat(), "old", model="old-model"),
        TurnRecord("session-1", (NOW - timedelta(minutes=1)).isoformat(), "new", model="gpt-6"),
        TurnRecord("session-1", NOW.isoformat(), "no-model", cwd=SECRET),
    ], IngestCursor("metadata", "/synthetic/session.jsonl", 1, 2, 0))
    alert = AlertView.from_transition(OPEN, store)
    runner = FakeRunner()
    EmailNotifier(runner, "amastbau@redhat.com").send(alert)
    body = runner.calls[0][8]
    assert "Agent kind: reviewer" in body
    assert "Model: gpt-6" in body
    assert "old-model" not in body
    assert SECRET not in body


def test_aggregate_alert_labels_model_and_agent_as_multiple(store: TelemetryStore) -> None:
    alert = AlertView.from_transition(replace(OPEN, scope_type="aggregate", scope_id="all"), store)
    runner = FakeRunner()
    EmailNotifier(runner, "amastbau@redhat.com").send(alert)
    assert "Agent kind: multiple" in runner.calls[0][8]
    assert "Model: multiple" in runner.calls[0][8]


def test_missing_session_metadata_is_unknown_and_lookup_is_parameterized(
    store: TelemetryStore,
) -> None:
    alert = AlertView.from_transition(replace(OPEN, scope_id="' OR 1=1 --"), store)
    runner = FakeRunner()
    EmailNotifier(runner, "amastbau@redhat.com").send(alert)
    assert "Agent kind: unknown" in runner.calls[0][8]
    assert "Model: unknown" in runner.calls[0][8]


@pytest.mark.parametrize(("result", "outcome"), [
    (CommandResult(7, SECRET, SECRET), "failed"),
    (FileNotFoundError(SECRET), "unavailable"),
    (PermissionError(SECRET), "failed"),
    (subprocess.TimeoutExpired("gws", 12, output=SECRET, stderr=SECRET), "timeout"),
])
def test_command_failures_reduce_to_fixed_codes_without_output(
    result: CommandResult | Exception, outcome: str, capsys: pytest.CaptureFixture[str],
) -> None:
    assert EmailNotifier(FakeRunner([result]), "amastbau@redhat.com").send(OPEN) == outcome
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_subprocess_runner_passes_argv_with_shell_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[Sequence[str], dict[str, object]]] = []

    def fake_run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 8, SECRET, SECRET)

    monkeypatch.setattr(notifiers.subprocess, "run", fake_run)
    result = SubprocessRunner(timeout_seconds=17).run(["notify-send", "$(touch /tmp/never)"])
    assert result.returncode == 8
    assert observed == [(["notify-send", "$(touch /tmp/never)"], {
        "check": False, "capture_output": True, "text": True, "shell": False, "timeout": 17,
    })]


def open_incident(store: TelemetryStore, transition: IncidentTransition = OPEN) -> None:
    store.open_incident(
        incident_id=transition.incident_id, scope_type=transition.scope_type,
        scope_id=transition.scope_id, trigger=transition.trigger,
        observed_rate=transition.observed_rate, baseline_rate=transition.baseline_rate,
        absolute_threshold=transition.absolute_threshold, opened_at=transition.opened_at,
    )


def dispatcher(
    runner: FakeRunner, store: TelemetryStore, clock: FakeClock | None = None,
    config: NotificationConfig = CONFIG,
):
    return notifiers.NotificationDispatcher(
        store, DesktopNotifier(runner), EmailNotifier(runner, config.email_recipient), config,
        clock=clock or FakeClock(),
    )


def attempts(store: TelemetryStore) -> list[tuple[str, int, str]]:
    return [tuple(row) for row in store.connection.execute(
        "SELECT channel, attempt_number, outcome_code FROM notification_attempts ORDER BY attempt_id"
    )]


def test_open_dispatch_sends_one_desktop_and_email_and_persists_success(
    store: TelemetryStore,
) -> None:
    open_incident(store)
    runner = FakeRunner()
    dispatch = dispatcher(runner, store)
    dispatch.dispatch(OPEN)
    dispatch.dispatch(OPEN)
    assert len(runner.calls_named("notify-send")) == 1
    assert len(runner.calls_named("gws")) == 1
    assert attempts(store) == [("desktop", 1, "sent"), ("email", 1, "sent")]


def test_email_failure_retries_follow_configured_delays_and_exhaust_budget(
    store: TelemetryStore,
) -> None:
    open_incident(store)
    runner = FakeRunner([CommandResult(0, "", ""), *[CommandResult(1, SECRET, SECRET)] * 3])
    clock = FakeClock()
    dispatch = dispatcher(runner, store, clock)
    dispatch.dispatch(OPEN)
    dispatch.dispatch(OPEN)
    assert clock.sleeps == [10, 30]
    assert len(runner.calls_named("gws")) == 3
    assert attempts(store) == [
        ("desktop", 1, "sent"), ("email", 1, "failed"),
        ("email", 2, "failed"), ("email", 3, "failed"),
    ]
    assert store.health_snapshot()["notification_failures"] == 3
    times = [row[0] for row in store.connection.execute(
        "SELECT attempted_at FROM notification_attempts WHERE channel='email' ORDER BY attempt_number"
    )]
    assert [datetime.fromisoformat(value) for value in times] == [
        NOW, NOW + timedelta(seconds=10), NOW + timedelta(seconds=40),
    ]


def test_retries_stop_immediately_after_first_success(store: TelemetryStore) -> None:
    open_incident(store)
    runner = FakeRunner([CommandResult(0, "", ""), CommandResult(1, SECRET, SECRET)])
    clock = FakeClock()
    dispatcher(runner, store, clock).dispatch(OPEN)
    assert len(runner.calls_named("gws")) == 2
    assert clock.sleeps == [10]
    assert attempts(store)[-1] == ("email", 2, "sent")


def test_altered_retry_config_controls_attempt_count_and_delays(store: TelemetryStore) -> None:
    open_incident(store)
    runner = FakeRunner([CommandResult(1, SECRET, SECRET)] * 5)
    clock = FakeClock()
    dispatcher(runner, store, clock, NotificationConfig("someone@example.org", (2, 7, 11))).dispatch(
        OPEN,
    )
    assert len(runner.calls_named("notify-send")) == 1
    assert len(runner.calls_named("gws")) == 4
    assert clock.sleeps == [2, 7, 11]
    assert all(argv[4] == "someone@example.org" for argv in runner.calls_named("gws"))


def test_persisted_success_suppresses_deliveries_after_real_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as first:
        open_incident(first)
        dispatcher(FakeRunner(), first).dispatch(OPEN)
    with TelemetryStore.open(path) as second:
        runner = FakeRunner()
        dispatcher(runner, second).dispatch(OPEN)
        assert runner.calls == []
        assert len(attempts(second)) == 2


def test_failed_email_restart_resumes_remaining_delay_and_attempt_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as first:
        open_incident(first)
        first.record_notification_attempt(
            incident_id="incident-1", channel="desktop", attempted_at=NOW,
            attempt_number=1, outcome_code="sent",
        )
        first.record_notification_attempt(
            incident_id="incident-1", channel="email", attempted_at=NOW,
            attempt_number=1, outcome_code="failed",
        )
    with TelemetryStore.open(path) as second:
        runner = FakeRunner([CommandResult(1, SECRET, SECRET)] * 2)
        clock = FakeClock(NOW + timedelta(seconds=4))
        dispatcher(runner, second, clock).dispatch(OPEN)
        assert len(runner.calls_named("gws")) == 2
        assert clock.sleeps == [6, 30]
        assert attempts(second)[-1] == ("email", 3, "failed")
        dispatcher(FakeRunner(), second).dispatch(OPEN)
        assert len(attempts(second)) == 4


def test_restart_does_not_delay_retry_that_is_already_due(store: TelemetryStore) -> None:
    open_incident(store)
    store.record_notification_attempt(
        incident_id="incident-1", channel="email", attempted_at=NOW,
        attempt_number=1, outcome_code="timeout",
    )
    clock = FakeClock(NOW + timedelta(seconds=12))
    runner = FakeRunner()
    dispatcher(runner, store, clock).dispatch(OPEN)
    assert clock.sleeps == []
    assert attempts(store)[-1] == ("email", 2, "sent")


@pytest.mark.parametrize("outcome", ["dry_run", "skipped"])
def test_dispatch_retries_only_previously_failed_delivery(
    store: TelemetryStore, outcome: str,
) -> None:
    open_incident(store)
    store.record_notification_attempt(
        incident_id="incident-1", channel="email", attempted_at=NOW,
        attempt_number=1, outcome_code=outcome,
    )
    runner = FakeRunner()
    dispatcher(runner, store).dispatch(OPEN)
    assert runner.calls_named("gws") == []


def test_recovery_sends_only_desktop_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.db"
    with TelemetryStore.open(path) as first:
        open_incident(first)
        runner = FakeRunner()
        dispatch = dispatcher(runner, first)
        dispatch.dispatch(OPEN)
        first.recover_incident("incident-1", RECOVERY.recovered_at)
        dispatch.dispatch(RECOVERY)
        assert len(runner.calls_named("gws")) == 1
        assert len(runner.calls_named("notify-send")) == 2
        assert attempts(first)[-1] == ("desktop", 2, "sent")
    with TelemetryStore.open(path) as second:
        runner = FakeRunner()
        dispatcher(runner, second).dispatch(RECOVERY)
        assert runner.calls == []


def test_recovery_without_open_delivery_does_not_send_open_or_email(store: TelemetryStore) -> None:
    open_incident(store)
    store.recover_incident("incident-1", RECOVERY.recovered_at)
    runner = FakeRunner()
    dispatcher(runner, store).dispatch(RECOVERY)
    assert runner.calls[0][1] == "--urgency=normal"
    assert runner.calls_named("gws") == []
    assert attempts(store) == [("desktop", 2, "sent")]


def test_failed_desktop_does_not_retry_or_block_email_or_recovery(store: TelemetryStore) -> None:
    open_incident(store)
    runner = FakeRunner([FileNotFoundError(SECRET)])
    dispatch = dispatcher(runner, store)
    dispatch.dispatch(OPEN)
    dispatch.dispatch(OPEN)
    store.recover_incident("incident-1", RECOVERY.recovered_at)
    dispatch.dispatch(RECOVERY)
    assert attempts(store) == [
        ("desktop", 1, "unavailable"), ("email", 1, "sent"), ("desktop", 2, "sent"),
    ]
    assert len(runner.calls_named("notify-send")) == 2


def test_new_incident_id_rearms_each_channel(store: TelemetryStore) -> None:
    later = replace(OPEN, incident_id="incident-2", opened_at=NOW + timedelta(minutes=10))
    open_incident(store)
    open_incident(store, later)
    runner = FakeRunner()
    dispatch = dispatcher(runner, store)
    dispatch.dispatch(OPEN)
    dispatch.dispatch(later)
    assert len(runner.calls_named("notify-send")) == 2
    assert len(runner.calls_named("gws")) == 2


def test_notification_attempt_storage_and_logs_exclude_raw_outputs(
    store: TelemetryStore, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    open_incident(store)
    runner = FakeRunner([PermissionError(SECRET), *[CommandResult(1, SECRET, SECRET)] * 3])
    dispatcher(runner, store).dispatch(OPEN)
    rows = list(store.connection.execute("SELECT * FROM notification_attempts"))
    assert SECRET not in repr([tuple(row) for row in rows])
    assert SECRET not in repr(store.health_snapshot())
    assert SECRET not in caplog.text
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert all(SECRET not in argument for argv in runner.calls for argument in argv)
    for path in store.path.parent.iterdir():
        assert SECRET.encode() not in path.read_bytes()


def test_dispatcher_enriches_session_metadata_before_delivering(store: TelemetryStore) -> None:
    open_incident(store)
    store.ingest([
        SessionRecord("session-1", NOW.isoformat(), agent_kind="reviewer"),
        TurnRecord("session-1", NOW.isoformat(), "turn", model="gpt-6"),
    ], IngestCursor("metadata", "/synthetic/session.jsonl", 1, 2, 0))
    runner = FakeRunner()
    dispatcher(runner, store).dispatch(OPEN)
    assert all("Agent kind: reviewer" in argv[-1] for argv in runner.calls)
    assert all("Model: gpt-6" in argv[-1] for argv in runner.calls)


def test_email_adapter_skips_recovery_without_executing_gmail() -> None:
    runner = FakeRunner()
    assert EmailNotifier(runner, "amastbau@redhat.com").send(RECOVERY) == "skipped"
    assert runner.calls == []


def test_replayed_open_after_persisted_recovery_sends_no_notifications(
    store: TelemetryStore,
) -> None:
    open_incident(store)
    store.recover_incident("incident-1", RECOVERY.recovered_at)
    runner = FakeRunner()
    dispatcher(runner, store).dispatch(OPEN)
    assert runner.calls == []
    assert attempts(store) == []


@pytest.mark.parametrize(("failure", "outcome"), [
    (FileNotFoundError(SECRET), "unavailable"),
    (subprocess.TimeoutExpired("gws", 4, output=SECRET, stderr=SECRET), "timeout"),
])
def test_unavailable_and_timeout_email_attempts_are_persisted_and_retried(
    store: TelemetryStore, failure: Exception, outcome: str,
) -> None:
    open_incident(store)
    runner = FakeRunner([CommandResult(0, "", ""), failure])
    clock = FakeClock()
    dispatcher(runner, store, clock).dispatch(OPEN)
    assert attempts(store) == [("desktop", 1, "sent"), ("email", 1, outcome), ("email", 2, "sent")]
    assert clock.sleeps == [10]
    assert store.health_snapshot()["notification_failures"] == 1


def test_failed_recovery_desktop_is_not_retried(store: TelemetryStore) -> None:
    open_incident(store)
    store.recover_incident("incident-1", RECOVERY.recovered_at)
    runner = FakeRunner([CommandResult(1, SECRET, SECRET)])
    dispatcher(runner, store).dispatch(RECOVERY)
    dispatcher(runner, store).dispatch(RECOVERY)
    assert len(runner.calls) == 1
    assert attempts(store) == [("desktop", 2, "failed")]


def test_unknown_baseline_and_utc_timestamps_render_without_invented_rates() -> None:
    transition = replace(OPEN, baseline_rate=None)
    runner = FakeRunner()
    EmailNotifier(runner, "amastbau@redhat.com").send(transition)
    assert "Baseline rate: unknown" in runner.calls[0][8]


@pytest.mark.parametrize("channel", ["desktop", "email"])
def test_alert_renders_each_current_window_token_category_independently(channel: str) -> None:
    runner = FakeRunner()
    if channel == "desktop":
        DesktopNotifier(runner).send_open(OPEN)
    else:
        EmailNotifier(runner, "amastbau@redhat.com").send(OPEN)
    body = runner.calls[0][-1]
    for expected in (
        "Input tokens: 210000", "Cached input tokens: 120000", "Cache-write input tokens: 40000",
        "Output tokens: 50000", "Reasoning output tokens: 30000", "Total tokens: 260000",
    ):
        assert expected in body


def test_recovery_renders_its_current_breakdown_instead_of_opening_counts() -> None:
    runner = FakeRunner()
    DesktopNotifier(runner).send_recovery(RECOVERY)
    body = runner.calls[0][-1]
    assert "Input tokens: 0" in body
    assert "Total tokens: 0" in body
    assert "210000" not in body
