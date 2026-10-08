from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[1]
    / "src/codex_tokenomics/resources/codex-tokenomics-skill/SKILL.md"
)


def test_skill_routes_questions_to_read_only_commands() -> None:
    skill_text = SKILL.read_text()
    assert "codex-tokenomics summary --format json" in skill_text
    assert "codex-tokenomics query --sql" in skill_text
    assert "Do not read ~/.codex/sessions" in skill_text
    assert "Do not modify the database" in skill_text


def test_skill_prefers_named_reports_and_documents_content_boundary() -> None:
    skill_text = SKILL.read_text()
    assert "Prefer named reports" in skill_text
    for report in ("health", "sessions", "models", "agents", "usage", "incidents"):
        assert f"codex-tokenomics {report}" in skill_text
    for prohibited in (
        "prompt content", "response content", "tool input", "tool output", "reasoning content",
    ):
        assert prohibited in skill_text


def test_skill_frontmatter_is_minimal_and_discriminating() -> None:
    skill_text = SKILL.read_text()
    assert skill_text.startswith("---\nname: codex-tokenomics\ndescription:")
    assert "local content-free Codex session telemetry" in skill_text.split("---", 2)[1]

