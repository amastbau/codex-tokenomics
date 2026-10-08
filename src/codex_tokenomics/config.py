"""Required, immutable configuration with no implicit detector defaults."""

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path


class ConfigError(ValueError):
    """Configuration is incomplete, unsupported, or inconsistent."""


@dataclass(frozen=True, slots=True)
class PathsConfig:
    session_root: Path
    database: Path


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    poll_interval_seconds: float


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    session_absolute_tokens_per_minute: int
    aggregate_absolute_tokens_per_minute: int
    relative_multiplier: float
    relative_minimum_tokens_per_minute: int
    rate_window_seconds: int
    baseline_window_seconds: int
    minimum_baseline_buckets: int
    recovery_seconds: int


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    email_recipient: str
    email_retry_delays_seconds: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class QueryConfig:
    row_limit: int
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    paths: PathsConfig
    collector: CollectorConfig
    detector: DetectorConfig
    notifications: NotificationConfig
    query: QueryConfig


def _check_keys(data: Mapping[str, object], schema: type, location: str) -> None:
    if not isinstance(data, Mapping):
        raise ConfigError(f"{location}: expected a table")
    required = {field.name for field in fields(schema)}
    unknown = data.keys() - required
    if unknown:
        raise ConfigError(f"{location}: unknown key(s): {', '.join(map(str, unknown))}")
    missing = required - data.keys()
    if missing:
        raise ConfigError(f"{location}: missing key(s): {', '.join(sorted(missing))}")


def _section(data: Mapping[str, object], name: str, schema: type) -> Mapping[str, object]:
    section = data[name]
    _check_keys(section, schema, name)
    return section


def _positive_number(value: object, name: str, *, integer: bool = False) -> int | float:
    allowed = (int,) if integer else (int, float)
    if type(value) not in allowed or value <= 0:
        raise ConfigError(f"{name}: expected a positive {'integer' if integer else 'number'}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{name}: expected a finite positive number")
    return value


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name}: expected a non-empty string")
    return value


def _path(value: object, name: str) -> Path:
    raw_path = _nonempty_string(value, name)
    try:
        return Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise ConfigError(f"{name}: could not resolve path: {error}") from error


def validate_config(data: Mapping[str, object]) -> AppConfig:
    """Validate all required sections and values, returning an immutable snapshot."""
    _check_keys(data, AppConfig, "config")
    paths = _section(data, "paths", PathsConfig)
    collector = _section(data, "collector", CollectorConfig)
    detector = _section(data, "detector", DetectorConfig)
    notifications = _section(data, "notifications", NotificationConfig)
    query = _section(data, "query", QueryConfig)

    detector_config = DetectorConfig(**{
        key: _positive_number(value, f"detector.{key}", integer=key != "relative_multiplier")
        for key, value in detector.items()
    })
    if detector_config.baseline_window_seconds < (
        detector_config.rate_window_seconds * detector_config.minimum_baseline_buckets
    ):
        raise ConfigError(
            "detector.baseline_window_seconds must be >= "
            "rate_window_seconds * minimum_baseline_buckets"
        )

    delays = notifications["email_retry_delays_seconds"]
    if not isinstance(delays, list) or not delays:
        raise ConfigError("notifications.email_retry_delays_seconds: expected a non-empty array")
    retry_delays = tuple(
        _positive_number(value, f"notifications.email_retry_delays_seconds[{index}]")
        for index, value in enumerate(delays)
    )

    return AppConfig(
        paths=PathsConfig(**{key: _path(value, f"paths.{key}") for key, value in paths.items()}),
        collector=CollectorConfig(
            poll_interval_seconds=_positive_number(
                collector["poll_interval_seconds"], "collector.poll_interval_seconds"
            )
        ),
        detector=detector_config,
        notifications=NotificationConfig(
            email_recipient=_nonempty_string(
                notifications["email_recipient"], "notifications.email_recipient"
            ),
            email_retry_delays_seconds=retry_delays,
        ),
        query=QueryConfig(**{
            key: _positive_number(value, f"query.{key}", integer=True)
            for key, value in query.items()
        }),
    )


def load_config(path: Path) -> AppConfig:
    """Load TOML from an explicit path and validate it without runtime defaults."""
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from error
    return validate_config(data)
