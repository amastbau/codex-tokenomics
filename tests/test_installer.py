import os
import stat
from pathlib import Path

import pytest

from codex_tokenomics import installer
from codex_tokenomics.installer import InstallError, install, uninstall


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def write_config(tmp_path: Path) -> Path:
    sessions = tmp_path / "sessions"
    database = tmp_path / ".local/share/codex-tokenomics/telemetry.db"
    source = tmp_path / "source.toml"
    source.write_text(
        (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
        .replace('"~/.codex/sessions"', f'"{sessions}"')
        .replace('"~/.local/share/codex-tokenomics/telemetry.db"', f'"{database}"')
    )
    return source


def test_install_writes_restricted_files_into_fake_home(tmp_path: Path) -> None:
    source = write_config(tmp_path)

    result = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert mode(result.config_path) == 0o600
    assert mode(result.data_directory) == 0o700
    assert mode(result.config_path.parent) == 0o700
    assert mode(result.unit_path.parent) == 0o700
    assert mode(result.skill_path.parent) == 0o700
    assert mode(result.manifest_path) == 0o600
    unit = result.unit_path.read_text()
    assert "ExecStart=/opt/bin/codex-tokenomics daemon" in unit
    assert f"--config {result.config_path}" in unit
    assert "UMask=0077" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=10" in unit


def test_install_validates_before_writing_any_runtime_files(tmp_path: Path) -> None:
    source = tmp_path / "bad.toml"
    source.write_text("[paths]\n")

    with pytest.raises(ValueError):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert not (tmp_path / ".config/codex-tokenomics").exists()
    assert not (tmp_path / ".config/systemd/user/codex-tokenomics.service").exists()
    assert not (tmp_path / ".codex/skills/codex-tokenomics").exists()


def test_install_refuses_to_overwrite_an_existing_user_file(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    config_path = tmp_path / ".config/codex-tokenomics/config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("owned by user")

    with pytest.raises(InstallError, match="already exists"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert config_path.read_text() == "owned by user"
    assert not (tmp_path / ".config/systemd/user/codex-tokenomics.service").exists()


def test_install_hardens_existing_project_directories_without_changing_shared_unit_dir(
    tmp_path: Path,
) -> None:
    source = write_config(tmp_path)
    project_directories = [
        tmp_path / ".config/codex-tokenomics",
        tmp_path / ".local/share/codex-tokenomics",
        tmp_path / ".codex/skills/codex-tokenomics",
    ]
    unit_directory = tmp_path / ".config/systemd/user"
    for directory in [*project_directories, unit_directory]:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)

    install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert [mode(path) for path in project_directories] == [0o700, 0o700, 0o700]
    assert mode(unit_directory) == 0o755


def test_uninstall_preserves_database_and_unrelated_skill_files_by_default(
    tmp_path: Path,
) -> None:
    source = write_config(tmp_path)
    result = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    database = result.data_directory / "telemetry.db"
    database.write_bytes(b"database")
    unrelated = result.skill_path.parent / "notes.txt"
    unrelated.write_text("preserve me")

    uninstall(tmp_path)

    assert database.read_bytes() == b"database"
    assert unrelated.read_text() == "preserve me"
    assert not result.config_path.exists()
    assert not result.unit_path.exists()
    assert not result.skill_path.exists()


def test_uninstall_always_preserves_database_even_if_legacy_flag_is_false(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    result = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    database = result.data_directory / "telemetry.db"
    database.write_bytes(b"database")
    keep = result.data_directory / "keep.txt"
    keep.write_text("keep")

    uninstall(tmp_path, preserve_database=False)

    assert database.read_bytes() == b"database"
    assert keep.read_text() == "keep"


def test_enable_rejects_alternate_home_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_config(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "codex_tokenomics.installer._systemctl_user", lambda *args: calls.append(args),
    )

    with pytest.raises(InstallError, match="current home"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=True)

    assert calls == []
    assert not (tmp_path / ".config/codex-tokenomics").exists()


def test_enable_failure_disables_before_rolling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_config(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr("codex_tokenomics.installer._current_home", lambda: tmp_path)
    wants_link = (
        tmp_path / ".config/systemd/user/default.target.wants/codex-tokenomics.service"
    )
    unit = tmp_path / ".config/systemd/user/codex-tokenomics.service"

    def systemctl(*arguments: str) -> None:
        calls.append(arguments)
        if arguments[:2] == ("enable", "--now"):
            wants_link.parent.mkdir()
            wants_link.symlink_to(unit)
            raise InstallError("synthetic activation failure")
        if arguments[:2] == ("disable", "--now"):
            assert unit.exists()
            wants_link.unlink()

    monkeypatch.setattr("codex_tokenomics.installer._systemctl_user", systemctl)

    with pytest.raises(InstallError, match="synthetic activation failure"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=True)

    assert calls == [
        ("daemon-reload",),
        ("enable", "--now", "codex-tokenomics.service"),
        ("disable", "--now", "codex-tokenomics.service"),
        ("daemon-reload",),
    ]
    assert not (tmp_path / ".config/codex-tokenomics/config.toml").exists()
    assert not (tmp_path / ".config/systemd/user/codex-tokenomics.service").exists()
    assert not (tmp_path / ".codex/skills/codex-tokenomics/SKILL.md").exists()
    assert not wants_link.exists()
    assert not wants_link.is_symlink()


def test_enable_failure_preserves_recoverable_files_when_disable_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_config(tmp_path)
    monkeypatch.setattr("codex_tokenomics.installer._current_home", lambda: tmp_path)

    def systemctl(*arguments: str) -> None:
        if arguments[0] in {"enable", "disable"}:
            raise InstallError("synthetic systemctl failure")

    monkeypatch.setattr("codex_tokenomics.installer._systemctl_user", systemctl)

    with pytest.raises(InstallError, match="synthetic systemctl failure"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=True)

    assert (tmp_path / ".config/codex-tokenomics/config.toml").exists()
    assert (tmp_path / ".config/systemd/user/codex-tokenomics.service").exists()
    assert (tmp_path / ".codex/skills/codex-tokenomics/SKILL.md").exists()
    assert (tmp_path / ".config/codex-tokenomics/.install-manifest.json").exists()


def test_repeated_install_converges_and_updates_only_owned_unchanged_files(
    tmp_path: Path,
) -> None:
    source = write_config(tmp_path)
    first = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    first_manifest = first.manifest_path.read_bytes()

    second = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    assert second.config_path.read_bytes() == source.read_bytes()
    assert second.manifest_path.read_bytes() == first_manifest

    source.write_text(source.read_text().replace(
        'email_recipient = "amastbau@redhat.com"',
        'email_recipient = "updated@example.test"',
    ))
    third = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    assert "updated@example.test" in third.config_path.read_text()
    assert third.manifest_path.read_bytes() != first_manifest


def test_repeated_install_repairs_owned_file_permissions(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    result = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    owned_files = [
        result.config_path,
        result.unit_path,
        result.skill_path,
        result.manifest_path,
    ]
    for path in owned_files:
        path.chmod(0o666)

    install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert [mode(path) for path in owned_files] == [0o600] * 4


def test_install_refuses_to_replace_modified_owned_artifact(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    result = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    result.unit_path.write_text("hand modified")

    with pytest.raises(InstallError, match="modified"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert result.unit_path.read_text() == "hand modified"


def test_uninstall_without_manifest_preserves_handwritten_files(tmp_path: Path) -> None:
    paths = [
        tmp_path / ".config/codex-tokenomics/config.toml",
        tmp_path / ".config/systemd/user/codex-tokenomics.service",
        tmp_path / ".codex/skills/codex-tokenomics/SKILL.md",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("handwritten")

    uninstall(tmp_path)

    assert [path.read_text() for path in paths] == ["handwritten"] * 3


def test_uninstall_preserves_modified_owned_file_and_manifest(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    result = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    result.config_path.write_text("modified by user")

    uninstall(tmp_path)

    assert result.config_path.read_text() == "modified by user"
    assert result.manifest_path.exists()
    assert not result.unit_path.exists()
    assert not result.skill_path.exists()


def test_repeated_uninstall_is_safe(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    result = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    first = uninstall(tmp_path)
    second = uninstall(tmp_path)

    assert first.config_removed is True
    assert second.config_removed is False
    assert not result.manifest_path.exists()


@pytest.mark.parametrize("relative", [".config", ".local", ".codex"])
def test_install_rejects_symlink_ancestry_without_touching_target(
    tmp_path: Path, relative: str,
) -> None:
    source = write_config(tmp_path)
    outside = tmp_path.parent / f"outside-{tmp_path.name}-{relative[1:]}"
    outside.mkdir()
    (tmp_path / relative).symlink_to(outside, target_is_directory=True)

    with pytest.raises(InstallError, match="symlink"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert list(outside.iterdir()) == []


def test_install_rejects_symlink_above_selected_home(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    selected_home = real_parent / "home"
    selected_home.mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    source = write_config(tmp_path)

    with pytest.raises(InstallError, match="symlink"):
        install(
            source,
            linked_parent / "home",
            Path("/opt/bin/codex-tokenomics"),
            enable=False,
        )

    assert list(selected_home.iterdir()) == []


def test_install_rejects_dangling_symlink_target(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    config = tmp_path / ".config/codex-tokenomics/config.toml"
    config.parent.mkdir(parents=True)
    config.symlink_to(tmp_path / "missing")

    with pytest.raises(InstallError, match="symlink"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert config.is_symlink()


def test_uninstall_rejects_symlink_ancestry_without_removing_outside_file(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"outside-uninstall-{tmp_path.name}"
    protected = outside / "codex-tokenomics/config.toml"
    protected.parent.mkdir(parents=True)
    protected.write_text("outside")
    (tmp_path / ".config").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InstallError, match="symlink"):
        uninstall(tmp_path)

    assert protected.read_text() == "outside"


def test_systemd_paths_escape_specifier_and_variable_expansion(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    executable = Path(f"/opt/bin/codex%token${os.getpid()}")

    result = install(source, tmp_path, executable, enable=False)

    unit = result.unit_path.read_text()
    assert "codex%%token$$" in unit


def test_parent_swap_during_install_cannot_write_outside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_config(tmp_path)
    outside = tmp_path.parent / f"outside-write-race-{tmp_path.name}"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("safe")
    original_open = installer.os.open
    swapped = False

    def swap_then_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode_value: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and path == "config.toml" and flags & os.O_CREAT and dir_fd is not None:
            project = tmp_path / ".config/codex-tokenomics"
            project.rename(tmp_path / ".config/codex-tokenomics-owned")
            project.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode_value, dir_fd=dir_fd)

    monkeypatch.setattr(installer.os, "open", swap_then_open)

    with pytest.raises(InstallError, match="symlink|directory"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)

    assert sentinel.read_text() == "safe"
    assert not (outside / "config.toml").exists()


def test_parent_swap_during_uninstall_cannot_remove_outside_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_config(tmp_path)
    install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    outside = tmp_path.parent / f"outside-remove-race-{tmp_path.name}"
    outside.mkdir()
    protected = outside / "config.toml"
    protected.write_text("safe")
    original_unlink = installer.os.unlink
    swapped = False

    def swap_then_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and path == "config.toml" and dir_fd is not None:
            project = tmp_path / ".config/codex-tokenomics"
            project.rename(tmp_path / ".config/codex-tokenomics-owned")
            project.symlink_to(outside, target_is_directory=True)
            swapped = True
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(installer.os, "unlink", swap_then_unlink)

    with pytest.raises(InstallError, match="symlink|directory"):
        uninstall(tmp_path)

    assert protected.read_text() == "safe"


def test_parent_swap_during_activation_rollback_cannot_delete_outside_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_config(tmp_path)
    outside = tmp_path.parent / f"outside-rollback-race-{tmp_path.name}"
    outside.mkdir()
    protected = outside / "config.toml"
    protected.write_text("safe")
    monkeypatch.setattr(installer, "_current_home", lambda: tmp_path)
    original_unlink = installer.os.unlink
    swapped = False

    def systemctl(*arguments: str) -> None:
        if arguments[:2] == ("enable", "--now"):
            raise InstallError("synthetic activation failure")

    def swap_then_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and path == "config.toml" and dir_fd is not None:
            project = tmp_path / ".config/codex-tokenomics"
            project.rename(tmp_path / ".config/codex-tokenomics-owned")
            project.symlink_to(outside, target_is_directory=True)
            swapped = True
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(installer, "_systemctl_user", systemctl)
    monkeypatch.setattr(installer.os, "unlink", swap_then_unlink)

    with pytest.raises(InstallError, match="synthetic activation failure|symlink|directory"):
        install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=True)

    assert protected.read_text() == "safe"
