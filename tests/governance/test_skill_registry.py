# tests/governance/test_skill_registry.py
import pytest
import yaml
from pathlib import Path
from backend.core.ouroboros.governance.skill_registry import SkillRegistry


@pytest.fixture
def skills_dir(tmp_path):
    skills = tmp_path / ".jarvis" / "skills"
    skills.mkdir(parents=True)
    return skills


def write_skill(skills_dir, name, file_pattern, instructions):
    (skills_dir / f"{name}.yaml").write_text(
        yaml.dump({"name": name, "filePattern": file_pattern, "instructions": instructions})
    )


def test_empty_dir_returns_empty_match(tmp_path):
    registry = SkillRegistry(tmp_path)
    assert registry.match(("backend/foo.py",)) == ""


def test_matching_skill_returns_instructions(tmp_path, skills_dir):
    write_skill(skills_dir, "migrations", "migrations/**", "Always wrap in transaction.")
    registry = SkillRegistry(tmp_path)
    result = registry.match(("migrations/0001_create.py",))
    assert "Always wrap in transaction." in result


def test_non_matching_skill_returns_empty(tmp_path, skills_dir):
    write_skill(skills_dir, "migrations", "migrations/**", "Always wrap in transaction.")
    registry = SkillRegistry(tmp_path)
    result = registry.match(("backend/core/foo.py",))
    assert result == ""


def test_multiple_skills_combined(tmp_path, skills_dir):
    write_skill(skills_dir, "migrations", "migrations/**", "Wrap in transaction.")
    write_skill(skills_dir, "tests", "tests/**", "Always use pytest fixtures.")
    registry = SkillRegistry(tmp_path)
    result = registry.match(("migrations/001.py", "tests/test_foo.py"))
    assert "Wrap in transaction." in result
    assert "Always use pytest fixtures." in result


def test_malformed_yaml_skipped_gracefully(tmp_path, skills_dir):
    (skills_dir / "bad.yaml").write_text("{{not: valid: yaml:")
    registry = SkillRegistry(tmp_path)
    assert registry.match(("foo.py",)) == ""


def test_missing_required_fields_skipped(tmp_path, skills_dir):
    (skills_dir / "incomplete.yaml").write_text(yaml.dump({"name": "incomplete"}))
    registry = SkillRegistry(tmp_path)
    assert registry.match(("foo.py",)) == ""


def test_no_skills_dir_is_not_an_error(tmp_path):
    registry = SkillRegistry(tmp_path)  # .jarvis/skills does not exist
    assert registry.match(("foo.py",)) == ""
