import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codex_tokenomics import cli
from codex_tokenomics.notifiers import CommandResult
from codex_tokenomics.storage import IngestCursor, TelemetryStore
from codex_tokenomics.telemetry import SessionRecord, TurnRecord, UsageSample

SECRET = "PROHIBITED-CLI-CONTENT-196d"


@dataclass
class FakeRunner:
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(list(argv))
        return CommandResult(0, SECRET, SECRET)

    def only_call(self, command: str) -> list[str]:
        calls = [call for call in self.calls if call[0] == command]
        assert len(calls) == 1
        return calls[0]


@pytest.fixture
def configured(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    database = tmp_path / "telemetry.db"
    sessions = tmp_path / "sessions"
    config = tmp_path / "config.toml"
    config.write_text(
        (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
        .replace('"~/.codex/sessions"', f'"{sessions}"')
        .replace('"~/.local/share/codex-tokenomics/telemetry.db"', f'"{database}"')
    )
    with TelemetryStore.open(database) as store:
        store.ingest([
            SessionRecord("session-1", "2026-10-08T12:00:00Z", cwd="/workspace/project"),
            TurnRecord(
                "session-1", "2026-10-08T12:00:00Z", "turn-1", model="gpt-6.1-sol",
            ),
            UsageSample(
                "session-1", "2026-10-08T12:00:00Z", "response-1", turn_id="turn-1",
                input_tokens=80, cached_input_tokens=50, cache_write_input_tokens=0,
                output_tokens=20, reasoning_output_tokens=0, total_tokens=100,
            ),
        ], IngestCursor("fixture", "/synthetic", 1, 2, 3))
    yield config, database


def invoke(
    arguments: list[str], *, runner: FakeRunner | None = None,
) -> tuple[int, str, str]:
    stdout: list[str] = []
    stderr: list[str] = []
    code = cli.main(arguments, runner=runner, stdout=stdout.append, stderr=stderr.append)
    return code, "".join(stdout), "".join(stderr)


@pytest.mark.parametrize("command", [
    "health", "summary", "sessions", "models", "agents", "usage", "timeline",
    "anomalies", "incidents",
])
def test_every_report_command_supports_json(configured: tuple[Path, Path], command: str) -> None:
    config, _ = configured
    arguments = [command, "--config", str(config), "--format", "json"]
    if command == "timeline":
        arguments.extend(["--session", "session-1"])
    code, stdout, stderr = invoke(arguments)
    assert code == 0, stderr
    output = json.loads(stdout)
    assert output is not None
    if command == "health":
        assert output["backlog_bytes"] == 0
        assert output["integrity_check"] == "ok"
    assert SECRET not in stdout + stderr


def test_cli_json_never_contains_prohibited_content(configured: tuple[Path, Path]) -> None:
    config, _ = configured
    code, stdout, stderr = invoke([
        "timeline", "--config", str(config), "--session", "session-1", "--format", "json",
    ])
    assert code == 0, stderr
    assert SECRET not in stdout
    assert json.loads(stdout)[0]["total_tokens"] == 100


def test_raw_query_is_read_only_and_json_formatted(configured: tuple[Path, Path]) -> None:
    config, _ = configured
    code, stdout, stderr = invoke([
        "query", "--config", str(config), "--sql",
        "SELECT total_tokens FROM usage_samples", "--format", "json",
    ])
    assert code == 0, stderr
    assert json.loads(stdout) == [{"total_tokens": 100}]


def test_unsafe_query_returns_stable_error_without_statement_content(
    configured: tuple[Path, Path],
) -> None:
    config, _ = configured
    code, stdout, stderr = invoke([
        "query", "--config", str(config), "--sql", f"DELETE FROM sessions /* {SECRET} */",
        "--format", "json",
    ])
    assert code == 2
    assert stdout == ""
    assert "query rejected" in stderr.lower()
    assert SECRET not in stderr


def test_validate_config_prints_explicit_detector_values(configured: tuple[Path, Path]) -> None:
    config, _ = configured
    code, stdout, stderr = invoke(["validate-config", str(config), "--format", "json"])
    assert code == 0, stderr
    values = json.loads(stdout)
    assert values["detector"]["session_absolute_tokens_per_minute"] == 250_000
    assert values["notifications"]["email_recipient"] == "amastbau@redhat.com"


def test_email_notification_test_is_always_dry_run(configured: tuple[Path, Path]) -> None:
    config, _ = configured
    runner = FakeRunner()
    code, stdout, stderr = invoke([
        "notification-test", "--config", str(config), "--email-dry-run", "--format", "json",
    ], runner=runner)
    assert code == 0, stderr
    assert json.loads(stdout)["email"] == "dry_run"
    assert runner.only_call("gws")[-1] == "--dry-run"


def test_desktop_notification_test_uses_one_safe_argv(configured: tuple[Path, Path]) -> None:
    config, _ = configured
    runner = FakeRunner()
    code, stdout, stderr = invoke([
        "notification-test", "--config", str(config), "--desktop", "--format", "json",
    ], runner=runner)
    assert code == 0, stderr
    assert json.loads(stdout)["desktop"] == "sent"
    call = runner.only_call("notify-send")
    assert call[:3] == ["notify-send", "--urgency=critical", "--app-name=Codex Tokenomics"]
    assert "test" in call[4].lower()


def test_notification_test_requires_an_explicit_safe_mode(configured: tuple[Path, Path]) -> None:
    config, _ = configured
    runner = FakeRunner()
    code, _, stderr = invoke(["notification-test", "--config", str(config)], runner=runner)
    assert code == 2
    assert "choose" in stderr.lower()
    assert runner.calls == []


def test_help_lists_every_task_eight_command() -> None:
    code, stdout, stderr = invoke(["--help"])
    assert code == 0, stderr
    for command in (
        "validate-config", "daemon", "health", "summary", "sessions", "models", "agents",
        "usage", "timeline", "anomalies", "incidents", "query", "notification-test",
    ):
        assert command in stdout


def test_default_console_output_is_not_swallowed(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "notification-test" in capsys.readouterr().out


def test_daemon_wires_and_runs_monitor_service(
    configured: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = configured
    calls: list[str] = []

    class FakeService:
        def run_forever(self) -> None:
            calls.append("run_forever")

    monkeypatch.setattr(cli, "_create_monitor_service", lambda *_args, **_kwargs: FakeService())
    code, stdout, stderr = invoke(["daemon", "--config", str(config)])
    assert code == 0, stderr
    assert stdout == ""
    assert calls == ["run_forever"]
