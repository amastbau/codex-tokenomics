"""Safe per-user installation of the service unit and read-only query skill."""

import hashlib
import json
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from codex_tokenomics.config import AppConfig, load_config

SERVICE_NAME = "codex-tokenomics.service"
MANIFEST_VERSION = 1
MANIFEST_NAME = ".install-manifest.json"


class InstallError(RuntimeError):
    """Installation cannot proceed without risking or leaving invalid user state."""


def _absolute_without_links(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(expanded))


def _current_home() -> Path:
    return _absolute_without_links(Path.home())


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    home: Path
    config_directory: Path
    config_file: Path
    data_directory: Path
    unit_directory: Path
    unit_file: Path
    skill_directory: Path
    skill_file: Path
    manifest_file: Path

    @classmethod
    def for_home(cls, home: Path) -> "RuntimePaths":
        selected_home = _absolute_without_links(home)
        config_directory = selected_home / ".config/codex-tokenomics"
        data_directory = selected_home / ".local/share/codex-tokenomics"
        unit_directory = selected_home / ".config/systemd/user"
        skill_directory = selected_home / ".codex/skills/codex-tokenomics"
        return cls(
            home=selected_home,
            config_directory=config_directory,
            config_file=config_directory / "config.toml",
            data_directory=data_directory,
            unit_directory=unit_directory,
            unit_file=unit_directory / SERVICE_NAME,
            skill_directory=skill_directory,
            skill_file=skill_directory / "SKILL.md",
            manifest_file=config_directory / MANIFEST_NAME,
        )

    def artifacts(self) -> dict[str, Path]:
        return {
            "config": self.config_file,
            "unit": self.unit_file,
            "skill": self.skill_file,
        }

    def create_restricted(self) -> None:
        for relative in (
            Path(".config"),
            Path(".config/systemd"),
            Path(".config/systemd/user"),
            Path(".local"),
            Path(".local/share"),
            Path(".codex"),
            Path(".codex/skills"),
        ):
            _ensure_directory(self.home, relative)
        for relative in (
            Path(".config/codex-tokenomics"),
            Path(".local/share/codex-tokenomics"),
            Path(".codex/skills/codex-tokenomics"),
        ):
            directory = _ensure_directory(self.home, relative)
            directory.chmod(0o700, follow_symlinks=False)


@dataclass(frozen=True, slots=True)
class InstallResult:
    config_path: Path
    data_directory: Path
    unit_path: Path
    skill_path: Path
    manifest_path: Path
    enabled: bool
    config: AppConfig


@dataclass(frozen=True, slots=True)
class UninstallResult:
    config_removed: bool
    unit_removed: bool
    skill_removed: bool
    database_removed: bool


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _assert_directory(path: Path) -> None:
    status = _lstat(path)
    if status is None:
        raise InstallError(f"required directory does not exist: {path}")
    if stat.S_ISLNK(status.st_mode):
        raise InstallError(f"refusing symlink directory: {path}")
    if not stat.S_ISDIR(status.st_mode):
        raise InstallError(f"path ancestry is not a directory: {path}")


def _assert_home_ancestry(home: Path) -> None:
    for path in reversed((home, *home.parents)):
        _assert_directory(path)


def _ensure_directory(home: Path, relative: Path) -> Path:
    _assert_home_ancestry(home)
    current = home
    for component in relative.parts:
        current /= component
        status = _lstat(current)
        if status is None:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            status = _lstat(current)
        if status is None:
            raise InstallError(f"could not create runtime directory: {current}")
        if stat.S_ISLNK(status.st_mode):
            raise InstallError(f"refusing symlink directory: {current}")
        if not stat.S_ISDIR(status.st_mode):
            raise InstallError(f"path ancestry is not a directory: {current}")
    return current


def _validate_ancestry(paths: RuntimePaths) -> None:
    _assert_home_ancestry(paths.home)
    relatives = (
        Path(".config/codex-tokenomics"),
        Path(".config/systemd/user"),
        Path(".local/share/codex-tokenomics"),
        Path(".codex/skills/codex-tokenomics"),
    )
    for relative in relatives:
        current = paths.home
        for component in relative.parts:
            current /= component
            status = _lstat(current)
            if status is None:
                break
            if stat.S_ISLNK(status.st_mode):
                raise InstallError(f"refusing symlink directory: {current}")
            if not stat.S_ISDIR(status.st_mode):
                raise InstallError(f"path ancestry is not a directory: {current}")


def _assert_regular_or_missing(path: Path) -> os.stat_result | None:
    status = _lstat(path)
    if status is None:
        return None
    if stat.S_ISLNK(status.st_mode):
        raise InstallError(f"refusing symlink file: {path}")
    if not stat.S_ISREG(status.st_mode):
        raise InstallError(f"installation target is not a regular file: {path}")
    return status


def _resource_bytes(*parts: str) -> bytes:
    return resources.files("codex_tokenomics").joinpath("resources", *parts).read_bytes()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_regular(path: Path) -> bytes:
    if _assert_regular_or_missing(path) is None:
        raise InstallError(f"installed artifact is missing: {path}")
    return path.read_bytes()


def _write_exclusive(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as error:
        if path.is_symlink():
            raise InstallError(f"refusing symlink file: {path}") from error
        raise InstallError(f"refusing to replace existing file: {path}") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        path.chmod(mode, follow_symlinks=False)
    except BaseException:
        status = _lstat(path)
        if status is not None and stat.S_ISREG(status.st_mode):
            path.unlink()
        raise


def _atomic_replace(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    _write_exclusive(temporary, payload, mode=mode)
    try:
        _assert_regular_or_missing(path)
        os.replace(temporary, path)
        path.chmod(mode, follow_symlinks=False)
    finally:
        status = _lstat(temporary)
        if status is not None and stat.S_ISREG(status.st_mode):
            temporary.unlink()


def _systemd_argument(path: Path) -> str:
    value = str(path)
    if "\n" in value or "\r" in value:
        raise InstallError("service paths cannot contain line breaks")
    value = value.replace("%", "%%").replace("$", "$$")
    if any(character.isspace() for character in value) or any(c in value for c in '\\"'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _render_unit(executable: Path, config_path: Path) -> bytes:
    template = _resource_bytes("codex-tokenomics.service").decode("utf-8")
    rendered = template.format(
        executable=_systemd_argument(executable.expanduser().resolve()),
        config_path=_systemd_argument(config_path),
    )
    return rendered.encode("utf-8")


def _manifest_bytes(paths: RuntimePaths, payloads: dict[str, bytes]) -> bytes:
    document = {
        "version": MANIFEST_VERSION,
        "artifacts": {
            name: {
                "path": str(path.relative_to(paths.home)),
                "sha256": _digest(payloads[name]),
            }
            for name, path in paths.artifacts().items()
        },
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _load_manifest(paths: RuntimePaths) -> tuple[dict[str, str], bytes] | None:
    if _assert_regular_or_missing(paths.manifest_file) is None:
        return None
    payload = paths.manifest_file.read_bytes()
    try:
        document = json.loads(payload)
        if set(document) != {"version", "artifacts"} or document["version"] != MANIFEST_VERSION:
            raise ValueError
        artifacts = document["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != set(paths.artifacts()):
            raise ValueError
        hashes: dict[str, str] = {}
        for name, expected_path in paths.artifacts().items():
            item = artifacts[name]
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise ValueError
            if item["path"] != str(expected_path.relative_to(paths.home)):
                raise ValueError
            digest = item["sha256"]
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError
            int(digest, 16)
            hashes[name] = digest
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InstallError("installer ownership manifest is invalid") from error
    return hashes, payload


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


def _restore_snapshot(
    snapshots: dict[Path, bytes | None], installed_payloads: dict[Path, bytes],
) -> None:
    for path, previous in snapshots.items():
        status = _assert_regular_or_missing(path)
        if status is None:
            if previous is not None:
                _write_exclusive(path, previous)
            continue
        if _digest(path.read_bytes()) != _digest(installed_payloads[path]):
            continue
        if previous is None:
            path.unlink()
        else:
            _atomic_replace(path, previous)


def _deactivate_after_failure() -> bool:
    try:
        _systemctl_user("disable", "--now", SERVICE_NAME)
    except InstallError:
        return False
    return True


def _reload_after_rollback() -> bool:
    try:
        _systemctl_user("daemon-reload")
    except InstallError:
        return False
    return True


def install(config_source: Path, home: Path, executable: Path, enable: bool) -> InstallResult:
    """Install or safely converge owned files under *home*."""
    paths = RuntimePaths.for_home(home)
    if enable and paths.home != _current_home():
        raise InstallError("service activation is allowed only for the current home")
    config = load_config(config_source)
    _validate_ancestry(paths)

    payloads = {
        "config": config_source.read_bytes(),
        "unit": _render_unit(executable, paths.config_file),
        "skill": _resource_bytes("codex-tokenomics-skill", "SKILL.md"),
    }
    manifest = _load_manifest(paths)
    old_manifest = manifest[1] if manifest is not None else None
    old_hashes = manifest[0] if manifest is not None else None

    snapshots: dict[Path, bytes | None] = {}
    installed_payloads = {path: payloads[name] for name, path in paths.artifacts().items()}
    for name, path in paths.artifacts().items():
        status = _assert_regular_or_missing(path)
        if old_hashes is None:
            if status is not None:
                raise InstallError(f"installation target already exists without ownership: {path}")
            snapshots[path] = None
        elif status is None:
            snapshots[path] = None
        else:
            current = path.read_bytes()
            if _digest(current) != old_hashes[name]:
                raise InstallError(f"installed artifact was modified: {path}")
            snapshots[path] = current
    snapshots[paths.manifest_file] = old_manifest

    paths.create_restricted()
    new_manifest = _manifest_bytes(paths, payloads)
    installed_payloads[paths.manifest_file] = new_manifest
    activation_started = False
    try:
        for name, path in paths.artifacts().items():
            if snapshots[path] is None:
                _write_exclusive(path, payloads[name])
            elif snapshots[path] != payloads[name]:
                if old_hashes is None or _digest(_read_regular(path)) != old_hashes[name]:
                    raise InstallError(f"installed artifact was modified: {path}")
                _atomic_replace(path, payloads[name])
        if old_manifest is None:
            _write_exclusive(paths.manifest_file, new_manifest)
        elif old_manifest != new_manifest:
            if _read_regular(paths.manifest_file) != old_manifest:
                raise InstallError("installer ownership manifest changed during installation")
            _atomic_replace(paths.manifest_file, new_manifest)
        if enable:
            activation_started = True
            _systemctl_user("daemon-reload")
            _systemctl_user("enable", "--now", SERVICE_NAME)
    except BaseException:
        can_rollback = not activation_started or _deactivate_after_failure()
        if can_rollback:
            _restore_snapshot(snapshots, installed_payloads)
            if activation_started:
                _reload_after_rollback()
        raise

    return InstallResult(
        config_path=paths.config_file,
        data_directory=paths.data_directory,
        unit_path=paths.unit_file,
        skill_path=paths.skill_file,
        manifest_path=paths.manifest_file,
        enabled=enable,
        config=config,
    )


def _remove_owned(path: Path, expected_hash: str) -> bool:
    status = _assert_regular_or_missing(path)
    if status is None or _digest(path.read_bytes()) != expected_hash:
        return False
    path.unlink()
    return True


def _remove_if_empty(path: Path) -> None:
    status = _lstat(path)
    if status is None:
        return
    if stat.S_ISLNK(status.st_mode):
        raise InstallError(f"refusing symlink directory: {path}")
    if not stat.S_ISDIR(status.st_mode):
        raise InstallError(f"path ancestry is not a directory: {path}")
    try:
        path.rmdir()
    except OSError:
        pass


def uninstall(home: Path, preserve_database: bool = True) -> UninstallResult:
    """Remove only manifest-owned unchanged artifacts; telemetry is always preserved."""
    del preserve_database  # API compatibility; database deletion is intentionally absent.
    paths = RuntimePaths.for_home(home)
    _validate_ancestry(paths)
    manifest = _load_manifest(paths)
    if manifest is None:
        return UninstallResult(False, False, False, False)
    hashes, manifest_payload = manifest

    matches: dict[str, bool] = {}
    for name, path in paths.artifacts().items():
        status = _assert_regular_or_missing(path)
        matches[name] = status is None or _digest(path.read_bytes()) == hashes[name]

    if paths.home == _current_home() and matches["unit"]:
        _systemctl_user("disable", "--now", SERVICE_NAME)

    removed = {
        name: _remove_owned(path, hashes[name]) if matches[name] else False
        for name, path in paths.artifacts().items()
    }
    all_absent = all(_lstat(path) is None for path in paths.artifacts().values())
    if all_absent and _read_regular(paths.manifest_file) == manifest_payload:
        paths.manifest_file.unlink()

    _remove_if_empty(paths.skill_directory)
    _remove_if_empty(paths.config_directory)
    if paths.home == _current_home() and matches["unit"]:
        _systemctl_user("daemon-reload")
    return UninstallResult(
        config_removed=removed["config"],
        unit_removed=removed["unit"],
        skill_removed=removed["skill"],
        database_removed=False,
    )
