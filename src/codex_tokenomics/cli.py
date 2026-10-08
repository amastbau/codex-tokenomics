"""Command-line entrypoint for the local Codex Tokenomics service and reports."""

import argparse
import contextlib
import io
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from codex_tokenomics.collector import Collector
from codex_tokenomics.config import AppConfig, ConfigError, load_config
from codex_tokenomics.detector import DetectionEngine, IncidentTransition, TokenBreakdown
from codex_tokenomics.notifiers import (
    CommandRunner,
    DesktopNotifier,
    EmailNotifier,
    NotificationDispatcher,
    SubprocessRunner,
    SystemClock,
)
from codex_tokenomics.query import (
    QueryError,
    QueryService,
    QueryTimedOut,
    _open_read_only,
    _remaining_timeout_ms,
)
from codex_tokenomics.service import MonitorService, ServiceError
from codex_tokenomics.storage import TelemetryStore

DEFAULT_CONFIG = Path("~/.config/codex-tokenomics/config.toml").expanduser()
Output = Callable[[str], object]


def _jsonable(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _emit(output: Output, value: object, output_format: str) -> None:
    if output_format == "json":
        output(json.dumps(_jsonable(value), allow_nan=False, sort_keys=True) + "\n")
        return
    rows = value if isinstance(value, list) else [value]
    if not rows:
        output("No rows.\n")
        return
    for row in rows:
        if isinstance(row, dict):
            output("\t".join(f"{key}={item}" for key, item in row.items()) + "\n")
        else:
            output(f"{row}\n")


def _query_service(args: argparse.Namespace) -> QueryService:
    config = load_config(args.config)
    return QueryService(config.paths.database, config.query.row_limit, config.query.timeout_ms)


def command_validate_config(args: argparse.Namespace) -> int:
    config = load_config(args.path)
    _emit(args.stdout, asdict(config), args.format)
    return 0


def _create_monitor_service(
    config: AppConfig, runner: CommandRunner | None = None,
) -> MonitorService:
    store = TelemetryStore.open(config.paths.database)
    command_runner = runner if runner is not None else SubprocessRunner()
    clock = SystemClock()
    try:
        dispatcher = NotificationDispatcher(
            store, DesktopNotifier(command_runner),
            EmailNotifier(command_runner, config.notifications.email_recipient),
            config.notifications, clock=clock,
        )
        return MonitorService(
            config, store, Collector(config.paths.session_root, store),
            DetectionEngine(store, config.detector), dispatcher, clock,
        )
    except BaseException:
        store.close()
        raise


def command_daemon(args: argparse.Namespace) -> int:
    service = _create_monitor_service(load_config(args.config), args.runner)
    service.run_forever()
    return 0


def _report(name: str, args: argparse.Namespace) -> int:
    filters = {
        key: getattr(args, key)
        for key in ("since", "until", "session", "limit")
        if hasattr(args, key) and getattr(args, key) is not None
    }
    rows = _query_service(args).run_report(name, filters)
    _emit(args.stdout, rows, args.format)
    return 0


def command_health(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    # Reuse the service's health semantics without opening the database for writes.
    deadline = time.monotonic_ns() + config.query.timeout_ms * 1_000_000
    try:
        connection = _open_read_only(
            config.paths.database, timeout_ms=_remaining_timeout_ms(deadline),
        )
    except sqlite3.Error:
        if time.monotonic_ns() >= deadline:
            raise QueryTimedOut("query execution timed out") from None
        raise
    connection.set_progress_handler(lambda: int(time.monotonic_ns() >= deadline), 1000)
    store = TelemetryStore(config.paths.database, connection)
    runner = args.runner if args.runner is not None else SubprocessRunner()
    clock = SystemClock()
    try:
        service = MonitorService(
            config, store, Collector(config.paths.session_root, store),
            DetectionEngine(store, config.detector),
            NotificationDispatcher(
                store, DesktopNotifier(runner),
                EmailNotifier(runner, config.notifications.email_recipient),
                config.notifications, clock=clock,
            ),
            clock,
        )
        health = service.health()
        if time.monotonic_ns() >= deadline:
            raise QueryTimedOut("query execution timed out")
        _emit(args.stdout, asdict(health), args.format)
    finally:
        store.close()
    return 0


def command_summary(args: argparse.Namespace) -> int:
    return _report("summary", args)


def command_sessions(args: argparse.Namespace) -> int:
    return _report("sessions", args)


def command_models(args: argparse.Namespace) -> int:
    return _report("models", args)


def command_agents(args: argparse.Namespace) -> int:
    return _report("agents", args)


def command_usage(args: argparse.Namespace) -> int:
    return _report("usage", args)


def command_timeline(args: argparse.Namespace) -> int:
    return _report("timeline", args)


def command_anomalies(args: argparse.Namespace) -> int:
    return _report("anomalies", args)


def command_incidents(args: argparse.Namespace) -> int:
    return _report("incidents", args)


def command_query(args: argparse.Namespace) -> int:
    rows = _query_service(args).run_sql(args.sql, tuple(args.parameter))
    _emit(args.stdout, rows, args.format)
    return 0


def _test_transition() -> IncidentTransition:
    now = datetime.now(UTC)
    return IncidentTransition(
        incident_id="notification-test", scope_type="session", scope_id="notification-test",
        state="opened", trigger="absolute", observed_rate=1, baseline_rate=0,
        absolute_threshold=1, opened_at=now, recovered_at=None,
        token_breakdown=TokenBreakdown(1, 0, 0, 0, 0, 1),
    )


def command_notification_test(args: argparse.Namespace) -> int:
    if not args.desktop and not args.email_dry_run:
        raise QueryError("choose --desktop and/or --email-dry-run")
    config = load_config(args.config)
    runner = args.runner if args.runner is not None else SubprocessRunner()
    transition = _test_transition()
    outcomes = {}
    if args.desktop:
        outcomes["desktop"] = DesktopNotifier(runner).send_open(transition)
    if args.email_dry_run:
        # There is intentionally no CLI path from notification-test to EmailNotifier.send.
        outcomes["email"] = EmailNotifier(
            runner, config.notifications.email_recipient,
        ).validate(transition)
    _emit(args.stdout, outcomes, args.format)
    return 0 if all(value in {"sent", "dry_run"} for value in outcomes.values()) else 1


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "validate-config": command_validate_config,
    "daemon": command_daemon,
    "health": command_health,
    "summary": command_summary,
    "sessions": command_sessions,
    "models": command_models,
    "agents": command_agents,
    "usage": command_usage,
    "timeline": command_timeline,
    "anomalies": command_anomalies,
    "incidents": command_incidents,
    "query": command_query,
    "notification-test": command_notification_test,
}


def _format_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json"), default="text")


def _config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)


def _report_options(parser: argparse.ArgumentParser, *, session: bool = True) -> None:
    _config_option(parser)
    _format_option(parser)
    parser.add_argument("--since")
    parser.add_argument("--until")
    if session:
        parser.add_argument("--session")
    parser.add_argument("--limit", type=int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-tokenomics", description="Local content-free Codex token telemetry",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="validate a complete TOML config")
    validate.add_argument("path", type=Path)
    _format_option(validate)

    daemon = subparsers.add_parser("daemon", help="run reconciliation and live monitoring")
    _config_option(daemon)

    health = subparsers.add_parser("health", help="show persisted collector health")
    _config_option(health)
    _format_option(health)

    for name in ("summary", "sessions", "models", "agents", "usage", "timeline",
                 "anomalies", "incidents"):
        report = subparsers.add_parser(name, help=f"show the {name} report")
        _report_options(report, session=name != "summary")

    query = subparsers.add_parser("query", help="run one bounded read-only SQL statement")
    _config_option(query)
    _format_option(query)
    query.add_argument("--sql", required=True)
    query.add_argument("--parameter", action="append", default=[])

    notification = subparsers.add_parser(
        "notification-test", help="test desktop or validate Gmail without sending email",
    )
    _config_option(notification)
    _format_option(notification)
    notification.add_argument("--desktop", action="store_true")
    notification.add_argument("--email-dry-run", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None, *, runner: CommandRunner | None = None,
    stdout: Output | None = None, stderr: Output | None = None,
) -> int:
    out = (lambda text: print(text, end="")) if stdout is None else stdout
    err = (lambda text: print(text, end="", file=sys.stderr)) if (
        stderr is None
    ) else stderr
    parser = _parser()
    captured_out, captured_err = io.StringIO(), io.StringIO()
    parse_exit: SystemExit | None = None
    try:
        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
            try:
                args = parser.parse_args(argv)
            except SystemExit as error:
                parse_exit = error
        if parse_exit is not None:
            if captured_out.getvalue():
                out(captured_out.getvalue())
            if captured_err.getvalue():
                err(captured_err.getvalue())
            return int(parse_exit.code)
        args.stdout = out
        args.stderr = err
        args.runner = runner
        return COMMANDS[args.command](args)
    except ConfigError as error:
        err(f"configuration invalid: {error}\n")
    except QueryError as error:
        err(f"{error}\n")
    except ServiceError as error:
        err(f"service failed: {error}\n")
    except (FileNotFoundError, OSError, sqlite3.Error):
        err("operation unavailable\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
