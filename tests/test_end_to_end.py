import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codex_tokenomics import cli
from codex_tokenomics.collector import Collector
from codex_tokenomics.config import load_config
from codex_tokenomics.detector import DetectionEngine
from codex_tokenomics.notifiers import (
    CommandResult,
    DesktopNotifier,
    EmailNotifier,
    NotificationDispatcher,
)
from codex_tokenomics.service import MonitorService
from codex_tokenomics.storage import TelemetryStore

SECRET = "PROHIBITED-CONTENT-9b84f1"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    current: datetime = NOW
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


@dataclass
class FakeRunner:
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(list(argv))
        return CommandResult(0, SECRET, SECRET)

    def count(self, command: str) -> int:
        return sum(call[0] == command for call in self.calls)


class EndToEndHarness:
    def __init__(self, tmp_path: Path, prohibited_content: str) -> None:
        self.secret = prohibited_content
        self.session_root = tmp_path / "sessions"
        self.database = tmp_path / "telemetry.db"
        self.config_path = tmp_path / "config.toml"
        self.config_path.write_text(
            (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
            .replace('"~/.codex/sessions"', f'"{self.session_root}"')
            .replace('"~/.local/share/codex-tokenomics/telemetry.db"', f'"{self.database}"')
            .replace("relative_minimum_tokens_per_minute = 50000",
                     "relative_minimum_tokens_per_minute = 3000000")
        )
        self.config = load_config(self.config_path)
        self.clock = FakeClock()
        self.runner = FakeRunner()
        self.store = TelemetryStore.open(self.config.paths.database)
        self.collector = Collector(self.config.paths.session_root, self.store)
        self.detector = DetectionEngine(self.store, self.config.detector)
        self.dispatcher = NotificationDispatcher(
            self.store,
            DesktopNotifier(self.runner),
            EmailNotifier(self.runner, self.config.notifications.email_recipient),
            self.config.notifications,
            clock=self.clock,
        )
        self.service = MonitorService(
            self.config,
            self.store,
            self.collector,
            self.detector,
            self.dispatcher,
            self.clock,
        )
        self.user_path = self.session_root / "2026" / "10" / "08" / "user.jsonl"
        self.subagent_path = self.session_root / "2026" / "10" / "08" / "subagent.jsonl"
        self._write_session(self.user_path, "session-user", "user", "gpt-6.1-sol")
        self._write_session(self.subagent_path, "session-subagent", "subagent", "gpt-6.1-mini")

    def _append(self, path: Path, *events: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            for event in events:
                stream.write(json.dumps(event).encode() + b"\n")

    def _write_session(self, path: Path, session_id: str, agent_kind: str, model: str) -> None:
        self._append(
            path,
            {
                "type": "session_meta",
                "timestamp": NOW.isoformat(),
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "source": "cli",
                    "thread_source": agent_kind,
                    "base_instructions": self.secret,
                    "payload": {"message": self.secret},
                },
            },
            {
                "type": "turn_context",
                "timestamp": NOW.isoformat(),
                "payload": {
                    "session_id": session_id,
                    "turn_id": f"{session_id}-turn",
                    "model": model,
                    "effort": "medium",
                    "messages": [{"content": self.secret}],
                },
            },
        )

    def _usage(
        self, session_id: str, response_id: str, total_tokens: int, timestamp: datetime,
    ) -> dict:
        return {
            "type": "token_usage_record",
            "timestamp": timestamp.isoformat(),
            "payload": {
                "session_id": session_id,
                "turn_id": f"{session_id}-turn",
                "response_id": response_id,
                "usage": {
                    "input_tokens": total_tokens - 20,
                    "cached_input_tokens": 9,
                    "cache_write_input_tokens": 4,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 7,
                    "total_tokens": total_tokens,
                },
                "message": self.secret,
                "tool_output": self.secret,
            },
        }

    def reconcile_history(self) -> None:
        self._append(
            self.user_path,
            self._usage("session-user", "history-user", 900_000, self.clock.now()),
        )
        self.service.reconcile()

    def append_live_user_and_subagent_spike(self) -> None:
        self.clock.advance(1)
        self._append(
            self.user_path,
            self._usage("session-user", "live-user", 260_000, self.clock.now()),
        )
        self._append(
            self.subagent_path,
            self._usage("session-subagent", "live-subagent", 260_000, self.clock.now()),
        )

    def run_cycle(self) -> None:
        self.service.run_once()

    def open_incidents(self) -> int:
        return self.store.table_counts()["alert_incidents"]

    def desktop_notifications(self) -> int:
        return self.runner.count("notify-send")

    def email_attempts(self) -> int:
        return self.runner.count("gws")

    def query(self, report: str) -> list[dict[str, object]]:
        code, stdout, stderr = self._invoke([report, "--format", "json"])
        assert code == 0, stderr
        return json.loads(stdout)

    def _invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout: list[str] = []
        stderr: list[str] = []
        code = cli.main(
            [arguments[0], "--config", str(self.config_path), *arguments[1:]],
            runner=self.runner,
            stdout=stdout.append,
            stderr=stderr.append,
        )
        return code, "".join(stdout), "".join(stderr)

    def advance_below_threshold_through_recovery(self) -> None:
        self.clock.advance(61)
        self.service.run_once()
        self.clock.advance(300)
        self.service.run_once()

    def recovered_incidents(self) -> int:
        row = self.store.connection.execute(
            "SELECT COUNT(*) AS count FROM alert_incidents WHERE recovered_at IS NOT NULL"
        ).fetchone()
        return row["count"]

    def assert_secret_absent_from_all_artifacts(self, secret: str) -> None:
        report_outputs = []
        for command in ("summary", "sessions", "models", "agents", "usage", "incidents"):
            code, stdout, stderr = self._invoke([command, "--format", "json"])
            assert code == 0, stderr
            report_outputs.append(stdout + stderr)
        dump = "\n".join(self.store.connection.iterdump())
        calls = json.dumps(self.runner.calls)
        database_bytes = b"".join(
            path.read_bytes()
            for path in self.database.parent.glob(f"{self.database.name}*")
            if path.is_file()
        )
        assert secret not in dump
        assert secret not in calls
        assert secret not in "".join(report_outputs)
        assert secret.encode() not in database_bytes


def test_end_to_end_collect_detect_notify_query_and_recover(tmp_path: Path) -> None:
    harness = EndToEndHarness(tmp_path, prohibited_content=SECRET)
    harness.reconcile_history()
    harness.append_live_user_and_subagent_spike()
    harness.run_cycle()
    assert harness.open_incidents() == 2
    assert harness.desktop_notifications() == 2
    assert harness.email_attempts() == 2
    assert harness.query("models")[0]["total_tokens"] > 0
    harness.advance_below_threshold_through_recovery()
    assert harness.recovered_incidents() == 2
    harness.assert_secret_absent_from_all_artifacts(SECRET)


def test_readme_documents_release_operations_and_privacy() -> None:
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    for required in (
        "Prerequisites",
        "Configuration",
        "Installation",
        "User service",
        "Health checks",
        "Notification dry runs",
        "Example skill questions",
        "Database",
        "Privacy exclusions",
        "Backup",
        "Upgrade",
        "Uninstall",
        "Documentation generated with Codex",
    ):
        assert required in text
