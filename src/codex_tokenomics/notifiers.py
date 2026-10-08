"""Telemetry-only desktop and delegated Gmail notifications."""

import html
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from codex_tokenomics.config import NotificationConfig
from codex_tokenomics.detector import IncidentTransition
from codex_tokenomics.storage import TelemetryStore

type OutcomeCode = Literal["sent", "dry_run", "failed", "unavailable", "timeout", "skipped"]

DISCLOSURE = "Automated by Codex Tokenomics; implementation assisted by Codex"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> CommandResult: ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class SubprocessRunner:
    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, argv: Sequence[str]) -> CommandResult:
        completed = subprocess.run(
            list(argv), check=False, capture_output=True, text=True, shell=False,
            timeout=self.timeout_seconds,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _label(value: str) -> str:
    """Keep identifiers on one plain-text line without interpreting them."""
    return "".join(character if character.isprintable() else " " for character in value)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification timestamps require a timezone")
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class AlertView:
    transition: IncidentTransition
    agent_kind: str
    model: str

    @classmethod
    def from_transition(
        cls, transition: IncidentTransition, store: TelemetryStore | None = None,
    ) -> "AlertView":
        if transition.scope_type == "aggregate":
            return cls(transition, "multiple", "multiple")
        agent_kind, model = "unknown", "unknown"
        if store is not None:
            session = store.connection.execute(
                "SELECT agent_kind FROM sessions WHERE session_id=?", (transition.scope_id,),
            ).fetchone()
            if session is not None:
                agent_kind = session["agent_kind"]
            turn = store.connection.execute(
                "SELECT model FROM turns WHERE session_id=? AND model IS NOT NULL "
                "ORDER BY observed_at DESC, turn_id DESC LIMIT 1", (transition.scope_id,),
            ).fetchone()
            if turn is not None:
                model = turn["model"]
        return cls(transition, agent_kind, model)

    @property
    def subject(self) -> str:
        state = "opened" if self.transition.state == "opened" else "recovered"
        return f"Codex Tokenomics: token spike {state}"

    @property
    def body(self) -> str:
        transition = self.transition
        tokens = transition.token_breakdown
        baseline = "unknown" if transition.baseline_rate is None else (
            f"{transition.baseline_rate:g} tokens/minute"
        )
        lines = [
            f"Incident: {_label(transition.incident_id)}",
            f"State: {transition.state}",
            f"Scope: {transition.scope_type}",
            f"Scope identifier: {_label(transition.scope_id)}",
            f"Agent kind: {_label(self.agent_kind)}",
            f"Model: {_label(self.model)}",
            f"Trigger: {transition.trigger}",
            f"Observed rate: {transition.observed_rate:g} tokens/minute",
            f"Baseline rate: {baseline}",
            f"Absolute threshold: {transition.absolute_threshold:g} tokens/minute",
            "Token breakdown (current rate window):",
            f"Input tokens: {tokens.input_tokens}",
            f"Cached input tokens: {tokens.cached_input_tokens}",
            f"Cache-write input tokens: {tokens.cache_write_input_tokens}",
            f"Output tokens: {tokens.output_tokens}",
            f"Reasoning output tokens: {tokens.reasoning_output_tokens}",
            f"Total tokens: {tokens.total_tokens}",
            f"Opened at: {_timestamp(transition.opened_at)}",
        ]
        if transition.recovered_at is not None:
            lines.append(f"Recovered at: {_timestamp(transition.recovered_at)}")
        lines.append(DISCLOSURE)
        return "\n".join(lines)


def _view(alert: AlertView | IncidentTransition) -> AlertView:
    return alert if isinstance(alert, AlertView) else AlertView.from_transition(alert)


def _deliver(runner: CommandRunner, argv: Sequence[str], *, dry_run: bool = False) -> OutcomeCode:
    try:
        result = runner.run(argv)
    except FileNotFoundError:
        return "unavailable"
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "failed"
    if result.returncode != 0:
        return "failed"
    return "dry_run" if dry_run else "sent"


class DesktopNotifier:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def send_open(self, alert: AlertView | IncidentTransition) -> OutcomeCode:
        return self._send(alert, "critical")

    def send_recovery(self, alert: AlertView | IncidentTransition) -> OutcomeCode:
        return self._send(alert, "normal")

    def _send(
        self, alert: AlertView | IncidentTransition, urgency: Literal["critical", "normal"],
    ) -> OutcomeCode:
        view = _view(alert)
        return _deliver(self.runner, [
            "notify-send", f"--urgency={urgency}", "--app-name=Codex Tokenomics",
            view.subject, html.escape(view.body),
        ])


class EmailNotifier:
    def __init__(self, runner: CommandRunner, recipient: str) -> None:
        self.runner = runner
        self.recipient = recipient

    def send(self, alert: AlertView | IncidentTransition) -> OutcomeCode:
        view = _view(alert)
        if view.transition.state == "recovered":
            return "skipped"
        return _deliver(self.runner, self._argv(view))

    def validate(self, alert: AlertView | IncidentTransition) -> OutcomeCode:
        return _deliver(self.runner, [*self._argv(_view(alert)), "--dry-run"], dry_run=True)

    def _argv(self, view: AlertView) -> list[str]:
        return [
            "gws", "gmail", "+send", "--to", self.recipient,
            "--subject", view.subject, "--body", view.body,
        ]


class NotificationDispatcher:
    """Persist delivery outcomes and consume a bounded per-incident email budget.

    Desktop attempt 1 identifies opening; attempt 2 identifies recovery. Desktop
    attempts have no retry policy. Email attempt numbers are chronological and
    the configured delays govern retries after failed/unavailable/timed-out calls.
    """

    def __init__(
        self, store: TelemetryStore, desktop: DesktopNotifier, email: EmailNotifier,
        config: NotificationConfig, *, clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.desktop = desktop
        self.email = email
        self.config = config
        self.clock = clock if clock is not None else SystemClock()

    def dispatch(self, transition: IncidentTransition) -> None:
        if transition.state == "opened":
            incident = self.store.connection.execute(
                "SELECT recovered_at FROM alert_incidents WHERE incident_id=?",
                (transition.incident_id,),
            ).fetchone()
            if incident is not None and incident["recovered_at"] is not None:
                return
        alert = AlertView.from_transition(transition, self.store)
        number = 1 if transition.state == "opened" else 2
        if not any(row["attempt_number"] == number for row in self._attempts(
            transition.incident_id, "desktop",
        )):
            attempted_at = self.clock.now()
            outcome = self.desktop.send_open(alert) if transition.state == "opened" else (
                self.desktop.send_recovery(alert)
            )
            self._record(transition.incident_id, "desktop", number, attempted_at, outcome)
        if transition.state == "opened":
            self._dispatch_email(alert)

    def _attempts(self, incident_id: str, channel: str) -> tuple[sqlite3.Row, ...]:
        return tuple(self.store.connection.execute(
            "SELECT attempt_number, attempted_at, outcome_code FROM notification_attempts "
            "WHERE incident_id=? AND channel=? ORDER BY attempt_number", (incident_id, channel),
        ))

    def _record(
        self, incident_id: str, channel: str, number: int, attempted_at: datetime,
        outcome: OutcomeCode,
    ) -> None:
        self.store.record_notification_attempt(
            incident_id=incident_id, channel=channel, attempted_at=attempted_at,
            attempt_number=number, outcome_code=outcome,
        )

    def _dispatch_email(self, alert: AlertView) -> None:
        attempts = self._attempts(alert.transition.incident_id, "email")
        if any(row["outcome_code"] == "sent" for row in attempts):
            return
        last = attempts[-1] if attempts else None
        if last is not None and last["outcome_code"] not in {"failed", "unavailable", "timeout"}:
            return
        number = last["attempt_number"] + 1 if last is not None else 1
        previous_at = datetime.fromisoformat(last["attempted_at"]) if last is not None else None
        delays = self.config.email_retry_delays_seconds
        while number <= len(delays) + 1:
            if previous_at is not None:
                retry_at = previous_at + timedelta(seconds=delays[number - 2])
                remaining = (retry_at - self.clock.now()).total_seconds()
                if remaining > 0:
                    self.clock.sleep(remaining)
            attempted_at = self.clock.now()
            outcome = self.email.send(alert)
            self._record(alert.transition.incident_id, "email", number, attempted_at, outcome)
            if outcome not in {"failed", "unavailable", "timeout"}:
                return
            previous_at = attempted_at
            number += 1
