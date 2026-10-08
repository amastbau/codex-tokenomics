import stat
from pathlib import Path

import pytest

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


def test_uninstall_deletes_only_the_database_on_explicit_opt_in(tmp_path: Path) -> None:
    source = write_config(tmp_path)
    result = install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=False)
    database = result.data_directory / "telemetry.db"
    database.write_bytes(b"database")
    keep = result.data_directory / "keep.txt"
    keep.write_text("keep")

    uninstall(tmp_path, preserve_database=False)

    assert not database.exists()
    assert keep.read_text() == "keep"


def test_enable_uses_systemctl_argv_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_config(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "codex_tokenomics.installer._systemctl_user", lambda *args: calls.append(args),
    )

    install(source, tmp_path, Path("/opt/bin/codex-tokenomics"), enable=True)

    assert calls == [
        ("daemon-reload",),
        ("enable", "--now", "codex-tokenomics.service"),
    ]
