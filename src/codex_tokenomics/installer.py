"""Safe per-user installation of the service unit and read-only query skill."""

import errno
import hashlib
import json
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import TracebackType
from typing import Self

from codex_tokenomics.config import AppConfig, load_config

SERVICE_NAME = "codex-tokenomics.service"
MANIFEST_VERSION = 1
MANIFEST_NAME = ".install-manifest.json"

CONFIG_RELATIVE = Path(".config/codex-tokenomics/config.toml")
UNIT_RELATIVE = Path(".config/systemd/user/codex-tokenomics.service")
SKILL_RELATIVE = Path(".codex/skills/codex-tokenomics/SKILL.md")
MANIFEST_RELATIVE = Path(f".config/codex-tokenomics/{MANIFEST_NAME}")


class InstallError(RuntimeError):
    """Installation cannot proceed without risking or leaving invalid user state."""


class _MissingPath(Exception):
    """A descriptor-relative path component does not exist."""


def _absolute_without_links(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(expanded))


def _current_home() -> Path:
    return _absolute_without_links(Path.home())


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_flags(flags: int) -> int:
    return flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _path_error(path: Path, error: OSError) -> InstallError:
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return InstallError(f"refusing symlink or non-directory ancestry: {path}")
    return InstallError(f"could not safely access installer path: {path}")


def _validate_relative(path: Path) -> tuple[str, ...]:
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise InstallError("installer path must be a fixed relative path")
    return path.parts


class SecureHome:
    """Operate beneath a retained home descriptor without following symlinks."""

    def __init__(self, home: Path) -> None:
        self.path = _absolute_without_links(home)
        self._home_fd = self._open_absolute_directory(self.path)

    @staticmethod
    def _open_absolute_directory(path: Path) -> int:
        descriptor = os.open(os.sep, _directory_flags())
        try:
            for component in path.parts[1:]:
                try:
                    child = os.open(component, _directory_flags(), dir_fd=descriptor)
                except OSError as error:
                    raise _path_error(path, error) from error
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        if self._home_fd >= 0:
            os.close(self._home_fd)
            self._home_fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def _open_directory(self, relative: Path, *, create: bool = False) -> int:
        parts = () if str(relative) == "." else _validate_relative(relative)
        descriptor = os.dup(self._home_fd)
        traversed = Path()
        try:
            for component in parts:
                traversed /= component
                try:
                    child = os.open(component, _directory_flags(), dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise _MissingPath from None
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        child = os.open(component, _directory_flags(), dir_fd=descriptor)
                    except FileExistsError:
                        try:
                            child = os.open(component, _directory_flags(), dir_fd=descriptor)
                        except OSError as error:
                            raise _path_error(traversed, error) from error
                    except OSError as error:
                        raise _path_error(traversed, error) from error
                except OSError as error:
                    raise _path_error(traversed, error) from error
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def ensure_directory(self, relative: Path, *, mode: int | None = None) -> None:
        descriptor = self._open_directory(relative, create=True)
        try:
            if mode is not None:
                os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)

    def _open_parent(self, relative: Path) -> tuple[int, str]:
        parts = _validate_relative(relative)
        parent = Path(*parts[:-1]) if len(parts) > 1 else Path(".")
        return self._open_directory(parent), parts[-1]

    @staticmethod
    def _regular_status(parent_fd: int, name: str, relative: Path) -> os.stat_result | None:
        try:
            status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _path_error(relative, error) from error
        if stat.S_ISLNK(status.st_mode):
            raise InstallError(f"refusing symlink file: {relative}")
        if not stat.S_ISREG(status.st_mode):
            raise InstallError(f"installation target is not a regular file: {relative}")
        return status

    def read(self, relative: Path) -> bytes | None:
        try:
            parent_fd, name = self._open_parent(relative)
        except _MissingPath:
            return None
        try:
            if self._regular_status(parent_fd, name, relative) is None:
                return None
            try:
                descriptor = os.open(name, _file_flags(os.O_RDONLY), dir_fd=parent_fd)
            except OSError as error:
                raise _path_error(relative, error) from error
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise InstallError(f"installation target is not a regular file: {relative}")
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    return stream.read()
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)

    def write_exclusive(
        self, relative: Path, payload: bytes, mode: int = 0o600,
    ) -> None:
        parent_fd, name = self._open_parent(relative)
        try:
            flags = _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                descriptor = os.open(name, flags, mode, dir_fd=parent_fd)
            except FileExistsError as error:
                status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISLNK(status.st_mode):
                    raise InstallError(f"refusing symlink file: {relative}") from error
                raise InstallError(f"refusing to replace existing file: {relative}") from error
            except OSError as error:
                raise _path_error(relative, error) from error
            try:
                os.fchmod(descriptor, mode)
                with os.fdopen(os.dup(descriptor), "wb") as stream:
                    stream.write(payload)
            except BaseException:
                os.close(descriptor)
                descriptor = -1
                status = self._regular_status(parent_fd, name, relative)
                if status is not None:
                    os.unlink(name, dir_fd=parent_fd)
                raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        finally:
            os.close(parent_fd)

    def replace_owned(
        self, relative: Path, payload: bytes, expected_hash: str, mode: int = 0o600,
    ) -> None:
        parent_fd, name = self._open_parent(relative)
        temporary = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
        source_fd = -1
        temporary_fd = -1
        try:
            try:
                source_fd = os.open(name, _file_flags(os.O_RDONLY), dir_fd=parent_fd)
            except OSError as error:
                raise _path_error(relative, error) from error
            source_status = os.fstat(source_fd)
            if not stat.S_ISREG(source_status.st_mode):
                raise InstallError(f"installation target is not a regular file: {relative}")
            with os.fdopen(os.dup(source_fd), "rb") as stream:
                if _digest(stream.read()) != expected_hash:
                    raise InstallError(f"installed artifact was modified: {relative}")

            temporary_fd = os.open(
                temporary,
                _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                mode,
                dir_fd=parent_fd,
            )
            os.fchmod(temporary_fd, mode)
            with os.fdopen(os.dup(temporary_fd), "wb") as stream:
                stream.write(payload)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (source_status.st_dev, source_status.st_ino):
                raise InstallError(f"installed artifact changed during replacement: {relative}")
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError as error:
            raise _path_error(relative, error) from error
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if temporary_fd >= 0:
                os.close(temporary_fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def chmod_owned(self, relative: Path, expected_hash: str, mode: int) -> None:
        parent_fd, name = self._open_parent(relative)
        try:
            try:
                descriptor = os.open(name, _file_flags(os.O_RDONLY), dir_fd=parent_fd)
            except OSError as error:
                raise _path_error(relative, error) from error
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise InstallError(f"installation target is not a regular file: {relative}")
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    if _digest(stream.read()) != expected_hash:
                        raise InstallError(f"installed artifact was modified: {relative}")
                os.fchmod(descriptor, mode)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)

    def unlink_owned(self, relative: Path, expected_hash: str) -> bool:
        try:
            parent_fd, name = self._open_parent(relative)
        except _MissingPath:
            return False
        try:
            status = self._regular_status(parent_fd, name, relative)
            if status is None:
                return False
            try:
                descriptor = os.open(name, _file_flags(os.O_RDONLY), dir_fd=parent_fd)
            except OSError as error:
                raise _path_error(relative, error) from error
            try:
                opened_status = os.fstat(descriptor)
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    if _digest(stream.read()) != expected_hash:
                        return False
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (opened_status.st_dev, opened_status.st_ino):
                    raise InstallError(f"installed artifact changed during removal: {relative}")
                os.unlink(name, dir_fd=parent_fd)
                return True
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)

    def remove_if_empty(self, relative: Path) -> None:
        parts = _validate_relative(relative)
        parent = Path(*parts[:-1]) if len(parts) > 1 else Path(".")
        try:
            parent_fd = self._open_directory(parent)
        except _MissingPath:
            return
        try:
            try:
                status = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise InstallError(f"refusing symlink or non-directory ancestry: {relative}")
            try:
                os.rmdir(parts[-1], dir_fd=parent_fd)
            except OSError as error:
                if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise _path_error(relative, error) from error
        finally:
            os.close(parent_fd)


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

    def artifacts(self) -> dict[str, tuple[Path, Path]]:
        return {
            "config": (CONFIG_RELATIVE, self.config_file),
            "unit": (UNIT_RELATIVE, self.unit_file),
            "skill": (SKILL_RELATIVE, self.skill_file),
        }

    def create_restricted(self, secure_home: SecureHome) -> None:
        for relative in (
            Path(".config"),
            Path(".config/systemd"),
            Path(".config/systemd/user"),
            Path(".local"),
            Path(".local/share"),
            Path(".codex"),
            Path(".codex/skills"),
        ):
            secure_home.ensure_directory(relative)
        for relative in (
            Path(".config/codex-tokenomics"),
            Path(".local/share/codex-tokenomics"),
            Path(".codex/skills/codex-tokenomics"),
        ):
            secure_home.ensure_directory(relative, mode=0o700)


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


def _resource_bytes(*parts: str) -> bytes:
    return resources.files("codex_tokenomics").joinpath("resources", *parts).read_bytes()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
                "path": str(absolute.relative_to(paths.home)),
                "sha256": _digest(payloads[name]),
            }
            for name, (_, absolute) in paths.artifacts().items()
        },
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _load_manifest(
    secure_home: SecureHome, paths: RuntimePaths,
) -> tuple[dict[str, str], bytes] | None:
    payload = secure_home.read(MANIFEST_RELATIVE)
    if payload is None:
        return None
    try:
        document = json.loads(payload)
        if set(document) != {"version", "artifacts"} or document["version"] != MANIFEST_VERSION:
            raise ValueError
        artifacts = document["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != set(paths.artifacts()):
            raise ValueError
        hashes: dict[str, str] = {}
        for name, (_, expected_path) in paths.artifacts().items():
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
    secure_home: SecureHome,
    snapshots: dict[Path, bytes | None],
    installed_payloads: dict[Path, bytes],
) -> None:
    for relative, previous in snapshots.items():
        current = secure_home.read(relative)
        if current is None:
            if previous is not None:
                secure_home.write_exclusive(relative, previous)
            continue
        if _digest(current) != _digest(installed_payloads[relative]):
            continue
        if previous is None:
            secure_home.unlink_owned(relative, _digest(current))
        else:
            secure_home.replace_owned(relative, previous, _digest(current))


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
    payloads = {
        "config": config_source.read_bytes(),
        "unit": _render_unit(executable, paths.config_file),
        "skill": _resource_bytes("codex-tokenomics-skill", "SKILL.md"),
    }

    with SecureHome(paths.home) as secure_home:
        manifest = _load_manifest(secure_home, paths)
        old_manifest = manifest[1] if manifest is not None else None
        old_hashes = manifest[0] if manifest is not None else None
        snapshots: dict[Path, bytes | None] = {}
        installed_payloads: dict[Path, bytes] = {}
        for name, (relative, _) in paths.artifacts().items():
            current = secure_home.read(relative)
            if old_hashes is None:
                if current is not None:
                    raise InstallError(
                        f"installation target already exists without ownership: {relative}"
                    )
                snapshots[relative] = None
            elif current is None:
                snapshots[relative] = None
            else:
                if _digest(current) != old_hashes[name]:
                    raise InstallError(f"installed artifact was modified: {relative}")
                snapshots[relative] = current
            installed_payloads[relative] = payloads[name]
        snapshots[MANIFEST_RELATIVE] = old_manifest

        paths.create_restricted(secure_home)
        new_manifest = _manifest_bytes(paths, payloads)
        installed_payloads[MANIFEST_RELATIVE] = new_manifest
        activation_started = False
        try:
            for name, (relative, _) in paths.artifacts().items():
                previous = snapshots[relative]
                if previous is None:
                    secure_home.write_exclusive(relative, payloads[name])
                elif previous != payloads[name]:
                    current = secure_home.read(relative)
                    if current is None or old_hashes is None or _digest(current) != old_hashes[name]:
                        raise InstallError(f"installed artifact was modified: {relative}")
                    secure_home.replace_owned(relative, payloads[name], old_hashes[name])
                secure_home.chmod_owned(relative, _digest(payloads[name]), 0o600)

            if old_manifest is None:
                secure_home.write_exclusive(MANIFEST_RELATIVE, new_manifest)
            elif old_manifest != new_manifest:
                if secure_home.read(MANIFEST_RELATIVE) != old_manifest:
                    raise InstallError("installer ownership manifest changed during installation")
                secure_home.replace_owned(
                    MANIFEST_RELATIVE,
                    new_manifest,
                    _digest(old_manifest),
                )
            secure_home.chmod_owned(MANIFEST_RELATIVE, _digest(new_manifest), 0o600)

            if enable:
                activation_started = True
                _systemctl_user("daemon-reload")
                _systemctl_user("enable", "--now", SERVICE_NAME)
        except BaseException:
            can_rollback = not activation_started or _deactivate_after_failure()
            if can_rollback:
                _restore_snapshot(secure_home, snapshots, installed_payloads)
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


def uninstall(home: Path, preserve_database: bool = True) -> UninstallResult:
    """Remove only manifest-owned unchanged artifacts; telemetry is always preserved."""
    del preserve_database  # API compatibility; database deletion is intentionally absent.
    paths = RuntimePaths.for_home(home)
    with SecureHome(paths.home) as secure_home:
        manifest = _load_manifest(secure_home, paths)
        if manifest is None:
            return UninstallResult(False, False, False, False)
        hashes, manifest_payload = manifest

        matches: dict[str, bool] = {}
        for name, (relative, _) in paths.artifacts().items():
            payload = secure_home.read(relative)
            matches[name] = payload is None or _digest(payload) == hashes[name]

        if paths.home == _current_home() and matches["unit"]:
            _systemctl_user("disable", "--now", SERVICE_NAME)

        removed = {
            name: secure_home.unlink_owned(relative, hashes[name]) if matches[name] else False
            for name, (relative, _) in paths.artifacts().items()
        }
        all_absent = all(
            secure_home.read(relative) is None for relative, _ in paths.artifacts().values()
        )
        if all_absent and secure_home.read(MANIFEST_RELATIVE) == manifest_payload:
            secure_home.unlink_owned(MANIFEST_RELATIVE, _digest(manifest_payload))

        secure_home.remove_if_empty(Path(".codex/skills/codex-tokenomics"))
        secure_home.remove_if_empty(Path(".config/codex-tokenomics"))
        if paths.home == _current_home() and matches["unit"]:
            _systemctl_user("daemon-reload")
        return UninstallResult(
            config_removed=removed["config"],
            unit_removed=removed["unit"],
            skill_removed=removed["skill"],
            database_removed=False,
        )
