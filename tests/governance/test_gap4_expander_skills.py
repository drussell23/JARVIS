# tests/governance/test_gap4_expander_skills.py
"""ContextExpander appends matched skill instructions to human_instructions."""
import inspect
import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.governance.context_expander import ContextExpander
from backend.core.ouroboros.governance.skill_registry import SkillRegistry


def test_context_expander_accepts_skill_registry_param():
    """ContextExpander.__init__ must accept skill_registry keyword arg."""
    sig = inspect.signature(ContextExpander.__init__)
    assert "skill_registry" in sig.parameters


def test_context_expander_calls_skill_match_in_expand_source():
    """expand() must call skill_registry.match()."""
    source = inspect.getsource(ContextExpander.expand)
    assert "skill_registry" in source
    assert "match" in source


@pytest.mark.asyncio
async def test_expand_appends_skill_instructions_when_oracle_not_ready(tmp_path):
    """When oracle not ready, skill instructions still injected via human_instructions."""
    # Write a skill
    skills_dir = tmp_path / ".jarvis" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "migs.yaml").write_text(
        yaml.dump({"name": "migs", "filePattern": "migrations/**", "instructions": "Use transactions."})
    )

    from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
    from datetime import datetime, timezone, timedelta

    ctx = OperationContext.create(
        description="add migration",
        target_files=("migrations/0001_add_col.py",),
    )

    oracle_mock = MagicMock()
    oracle_mock.is_ready.return_value = False

    registry = SkillRegistry(tmp_path)
    generator = MagicMock()
    expander = ContextExpander(
        generator=generator,
        repo_root=tmp_path,
        oracle=oracle_mock,
        skill_registry=registry,
    )

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    result_ctx = await expander.expand(ctx, deadline)

    assert "Use transactions." in result_ctx.human_instructions
