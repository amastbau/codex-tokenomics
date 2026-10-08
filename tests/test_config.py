import copy
import tomllib
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from codex_tokenomics.config import ConfigError, load_config, validate_config

EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "config.example.toml"
VALID_CONFIG = tomllib.loads(EXAMPLE_PATH.read_text())
NUMERIC_KEYS = [
    (section, key)
    for section in ("collector", "detector", "query")
    for key in VALID_CONFIG[section]
]


def test_valid_config_loads_every_required_detector_value(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(EXAMPLE_PATH.read_bytes())
    config = load_config(path)
    assert config.detector.session_absolute_tokens_per_minute == 250_000
    assert config.detector.aggregate_absolute_tokens_per_minute == 1_000_000
    assert config.notifications.email_recipient == "amastbau@redhat.com"
    assert config.notifications.email_retry_delays_seconds == (10, 30)
    assert config.collector.poll_interval_seconds == 2
    assert config.query.row_limit == 1000
    assert config.query.timeout_ms == 2000
    for key, value in VALID_CONFIG["detector"].items():
        assert getattr(config.detector, key) == value


@pytest.mark.parametrize("section", VALID_CONFIG)
def test_missing_section_is_rejected(section: str) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    del data[section]
    with pytest.raises(ConfigError, match=section):
        validate_config(data)


@pytest.mark.parametrize(
    "section,key", [(section, key) for section in VALID_CONFIG for key in VALID_CONFIG[section]]
)
def test_missing_value_is_rejected(section: str, key: str) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    del data[section][key]
    with pytest.raises(ConfigError, match=key):
        validate_config(data)


@pytest.mark.parametrize("section", [None, *VALID_CONFIG])
def test_unknown_key_is_rejected(section: str | None) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    target = data if section is None else data[section]
    target["hardcoded_escape_hatch"] = 1
    with pytest.raises(ConfigError, match="unknown key"):
        validate_config(data)


@pytest.mark.parametrize("section", VALID_CONFIG)
@pytest.mark.parametrize("value", [None, [], "not a table"])
def test_non_mapping_section_is_rejected(section: str, value: object) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data[section] = value
    with pytest.raises(ConfigError, match=section):
        validate_config(data)


@pytest.mark.parametrize("section,key", NUMERIC_KEYS)
@pytest.mark.parametrize("value", [0, -1, True, "2", None, float("inf"), float("nan")])
def test_invalid_numeric_value_is_rejected(section: str, key: str, value: object) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data[section][key] = value
    with pytest.raises(ConfigError, match=key):
        validate_config(data)


@pytest.mark.parametrize(
    "section,key",
    [(section, key) for section, key in NUMERIC_KEYS
     if key not in {"poll_interval_seconds", "relative_multiplier"}],
)
def test_integer_fields_reject_fractional_numbers(section: str, key: str) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data[section][key] = 1.5
    with pytest.raises(ConfigError, match=key):
        validate_config(data)


def test_fractional_poll_interval_and_relative_multiplier_are_supported() -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data["collector"]["poll_interval_seconds"] = 0.5
    data["detector"]["relative_multiplier"] = 1.5
    config = validate_config(data)
    assert config.collector.poll_interval_seconds == 0.5
    assert config.detector.relative_multiplier == 1.5


@pytest.mark.parametrize("baseline_window,valid", [(300, False), (360, True)])
def test_baseline_must_hold_minimum_buckets(baseline_window: int, valid: bool) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data["detector"]["baseline_window_seconds"] = baseline_window
    if valid:
        assert validate_config(data).detector.baseline_window_seconds == 360
    else:
        with pytest.raises(ConfigError, match="baseline_window_seconds"):
            validate_config(data)


def test_paths_are_expanded_and_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    data = copy.deepcopy(VALID_CONFIG)
    data["paths"]["session_root"] = "~/synthetic/../sessions"
    data["paths"]["database"] = "data/../telemetry.db"
    config = validate_config(data)
    assert config.paths.session_root == tmp_path / "sessions"
    assert config.paths.database == tmp_path / "telemetry.db"


@pytest.mark.parametrize("key", ["session_root", "database"])
@pytest.mark.parametrize("value", ["", "  ", 1, None])
def test_invalid_paths_are_rejected(key: str, value: object) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data["paths"][key] = value
    with pytest.raises(ConfigError, match=key):
        validate_config(data)


@pytest.mark.parametrize("value", ["", "  ", 1, None])
def test_empty_or_non_string_recipient_is_rejected(value: object) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data["notifications"]["email_recipient"] = value
    with pytest.raises(ConfigError, match="email_recipient"):
        validate_config(data)


@pytest.mark.parametrize("value", [[], "10", None, [0], [-1], [True], ["10"], [float("nan")]])
def test_invalid_retry_delays_are_rejected(value: object) -> None:
    data = copy.deepcopy(VALID_CONFIG)
    data["notifications"]["email_retry_delays_seconds"] = value
    with pytest.raises(ConfigError, match="email_retry_delays_seconds"):
        validate_config(data)


def test_config_is_immutable_and_independent_of_input() -> None:
    data = copy.deepcopy(VALID_CONFIG)
    config = validate_config(data)
    for instance in [config, *(getattr(config, field.name) for field in fields(config))]:
        field = fields(instance)[0]
        with pytest.raises(FrozenInstanceError):
            setattr(instance, field.name, None)
    data["notifications"]["email_retry_delays_seconds"].append(60)
    assert config.notifications.email_retry_delays_seconds == (10, 30)


def test_malformed_toml_has_a_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[detector\n")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(path)
