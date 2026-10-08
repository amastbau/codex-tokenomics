"""Safe per-user installation of the service unit and read-only query skill."""

import os
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from codex_tokenomics.config import AppConfig, load_config

SERVICE_NAME = "codex-tokenomics.service"


class InstallError(RuntimeError):
    """Installation cannot proceed without risking or leaving invalid user state."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    config_directory: Path
    config_file: Path
    data_directory: Path
    unit_directory: Path
    unit_file: Path
    skill_directory: Path
    skill_file: Path

    @classmethod
    def for_home(cls, home: Path) -> "RuntimePaths":
        resolved = home.expanduser().resolve()
        config_directory = resolved / ".config/codex-tokenomics"
        data_directory = resolved / ".local/share/codex-tokenomics"
        unit_directory = resolved / ".config/systemd/user"
        skill_directory = resolved / ".codex/skills/codex-tokenomics"
        return cls(
            config_directory=config_directory,
            config_file=config_directory / "config.toml",
            data_directory=data_directory,
            unit_directory=unit_directory,
            unit_file=unit_directory / SERVICE_NAME,
            skill_directory=skill_directory,
            skill_file=skill_directory / "SKILL.md",
        )

    def create_restricted(self) -> None:
        project_directories = (
            self.config_directory,
            self.data_directory,
            self.skill_directory,
        )
        for directory in project_directories:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

        for directory in (self.unit_directory,):
            existed = directory.exists()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not existed:
                directory.chmod(0o700)


@dataclass(frozen=True, slots=True)
class InstallResult:
    config_path: Path
    data_directory: Path
    unit_path: Path
    skill_path: Path
    enabled: bool
    config: AppConfig


@dataclass(frozen=True, slots=True)
class UninstallResult:
    config_removed: bool
    unit_removed: bool
    skill_removed: bool
    database_removed: bool


def _resource_bytes(*parts: str) -> bytes:
    return resources.files("codex_tokenomics").joinpath("resources", *parts).read_bytes()


def _write_exclusive(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as error:
        raise InstallError(f"refusing to replace existing file: {path}") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        path.chmod(mode)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _systemd_argument(path: Path) -> str:
    value = str(path)
    if "\n" in value or "\r" in value:
        raise InstallError("service paths cannot contain line breaks")
    if any(character.isspace() for character in value) or any(c in value for c in '\\"'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _render_unit(executable: Path, config_path: Path) -> bytes:
    template = _resource_bytes("codex-tokenomics.service").decode("utf-8")
    rendered = template.format(
        executable=_systemd_argument(executable.expanduser().resolve()),
        config_path=_systemd_argument(config_path.resolve()),
    )
    return rendered.encode("utf-8")


def _systemctl_user(*arguments: str) -> None:
    try:
        completed = subprocess.run(
            ["systemctl", "--user", *arguments],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except OSError as error:
        raise InstallError("could not execute systemctl --user") from error
    if completed.returncode != 0:
        raise InstallError("systemctl --user command failed")


def install(config_source: Path, home: Path, executable: Path, enable: bool) -> InstallResult:
    """Validate and install files under *home*, optionally enabling the user service."""
    config = load_config(config_source)
    paths = RuntimePaths.for_home(home)
    collisions = [
        path for path in (paths.config_file, paths.unit_file, paths.skill_file) if path.exists()
    ]
    if collisions:
        raise InstallError(f"installation target already exists: {collisions[0]}")

    paths.create_restricted()
    created: list[Path] = []
    try:
        _write_exclusive(paths.config_file, config_source.read_bytes())
        created.append(paths.config_file)
        _write_exclusive(paths.unit_file, _render_unit(executable, paths.config_file))
        created.append(paths.unit_file)
        _write_exclusive(
            paths.skill_file,
            _resource_bytes("codex-tokenomics-skill", "SKILL.md"),
        )
        created.append(paths.skill_file)
        if enable:
            _systemctl_user("daemon-reload")
            _systemctl_user("enable", "--now", SERVICE_NAME)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise

    return InstallResult(
        config_path=paths.config_file,
        data_directory=paths.data_directory,
        unit_path=paths.unit_file,
        skill_path=paths.skill_file,
        enabled=enable,
        config=config,
    )


def _remove_if_present(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _remove_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        pass


def uninstall(home: Path, preserve_database: bool = True) -> UninstallResult:
    """Remove installed files while preserving telemetry unless explicitly opted out."""
    paths = RuntimePaths.for_home(home)
    if paths.unit_file.exists() and home.expanduser().resolve() == Path.home().resolve():
        _systemctl_user("disable", "--now", SERVICE_NAME)

    unit_removed = _remove_if_present(paths.unit_file)
    config_removed = _remove_if_present(paths.config_file)
    skill_removed = _remove_if_present(paths.skill_file)
    database_removed = False
    if not preserve_database:
        database_removed = _remove_if_present(paths.data_directory / "telemetry.db")

    _remove_if_empty(paths.skill_directory)
    _remove_if_empty(paths.config_directory)
    if not preserve_database:
        _remove_if_empty(paths.data_directory)

    if unit_removed and home.expanduser().resolve() == Path.home().resolve():
        _systemctl_user("daemon-reload")
    return UninstallResult(
        config_removed=config_removed,
        unit_removed=unit_removed,
        skill_removed=skill_removed,
        database_removed=database_removed,
    )
