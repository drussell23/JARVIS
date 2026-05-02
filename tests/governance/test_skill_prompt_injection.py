"""Q1 Slice 3 — Skills visible to model via prompt-builder integration.

Closes the dead-export gap: ``render_skill_tool_block`` in
``skill_venom_bridge.py`` was previously imported nowhere.

Covers:

  §1   master flag + default-on (graduated)
  §2   default empty catalog → no skill block in prompt
  §3   non-empty MODEL-reach catalog → ``## Available Skills``
       block injected
  §4   master-off → block NOT injected even with skills present
  §5   skill block placement — between tool catalog and
       Exploration-first protocol (positional pin)
  §6   skill render failure degrades silently (best-effort)
  §7   AST authority pin: providers.py imports skill_venom_bridge
       lazily inside _build_tool_section (not at module level)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.providers import (
    _build_tool_section,
    _skill_prompt_injection_enabled,
)
from backend.core.ouroboros.governance.skill_catalog import (
    SkillCatalog,
    SkillSource,
    get_default_catalog,
    reset_default_catalog,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
)
from backend.core.ouroboros.governance.skill_trigger import (
    SkillReach,
)


_PROVIDERS_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "providers.py"
)


def _make_manifest(
    name: str, *, description: str = "", trigger: str = "",
    reach: SkillReach = SkillReach.OPERATOR_PLUS_MODEL,
) -> SkillManifest:
    return SkillManifest(
        name=name,
        description=description or f"Description for {name}",
        trigger=trigger or f"Use this when working with {name}",
        entrypoint=f"backend.fake.{name}.entry",
        reach=reach,
    )


@pytest.fixture(autouse=True)
def _reset_catalog():
    reset_default_catalog()
    yield
    reset_default_catalog()


# ============================================================================
# §1 — Master flag
# ============================================================================


class TestMasterFlag:
    def test_default_on_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", raising=False,
        )
        assert _skill_prompt_injection_enabled() is True

    def test_explicit_false_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "false",
        )
        assert _skill_prompt_injection_enabled() is False

    def test_garbage_value_treated_as_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "maybe",
        )
        assert _skill_prompt_injection_enabled() is False


# ============================================================================
# §2 — Empty catalog → no block
# ============================================================================


class TestEmptyCatalog:
    def test_clean_environment_no_skill_section(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "true",
        )
        # Default catalog is empty after reset_default_catalog()
        section = _build_tool_section()
        assert "## Available Skills" not in section


# ============================================================================
# §3 — Non-empty MODEL-reach catalog → block injected
# ============================================================================


class TestSkillBlockInjected:
    def test_single_skill_appears_in_prompt(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "true",
        )
        cat = get_default_catalog()
        cat.register(
            _make_manifest(
                "format_python",
                description="Auto-format Python files via ruff",
                trigger="Use after editing .py files",
            ),
            source=SkillSource.OPERATOR,
        )
        section = _build_tool_section()
        assert "## Available Skills" in section
        # The skill name should appear (via tool-name encoding)
        assert "format_python" in section
        assert "ruff" in section

    def test_multiple_skills_all_listed(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "true",
        )
        cat = get_default_catalog()
        cat.register(
            _make_manifest(
                "alpha", description="Alpha behavior",
            ),
            source=SkillSource.OPERATOR,
        )
        cat.register(
            _make_manifest(
                "beta", description="Beta behavior",
            ),
            source=SkillSource.OPERATOR,
        )
        section = _build_tool_section()
        assert "alpha" in section
        assert "beta" in section
        assert "Alpha behavior" in section
        assert "Beta behavior" in section

    def test_operator_only_reach_excluded(self, monkeypatch):
        # OPERATOR-only reach: skill should NOT advertise to model
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "true",
        )
        cat = get_default_catalog()
        cat.register(
            _make_manifest(
                "operator_only",
                description="Hidden from model",
                reach=SkillReach.OPERATOR,
            ),
            source=SkillSource.OPERATOR,
        )
        section = _build_tool_section()
        # No model-reach skills → no block at all
        assert "## Available Skills" not in section
        assert "operator_only" not in section


# ============================================================================
# §4 — Master-off → block NOT injected
# ============================================================================


class TestMasterOffNoInjection:
    def test_master_off_skips_injection_with_skills_present(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "false",
        )
        cat = get_default_catalog()
        cat.register(
            _make_manifest(
                "should_not_appear",
                description="Hot-revert hides this even though "
                            "it's MODEL-reach",
            ),
            source=SkillSource.OPERATOR,
        )
        section = _build_tool_section()
        # Hot-revert hides the block
        assert "## Available Skills" not in section
        assert "should_not_appear" not in section


# ============================================================================
# §5 — Positional pin: skill block placement
# ============================================================================


class TestSkillBlockPosition:
    def test_skill_block_before_exploration_first_protocol(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "true",
        )
        cat = get_default_catalog()
        cat.register(
            _make_manifest("sample_skill"),
            source=SkillSource.OPERATOR,
        )
        section = _build_tool_section()
        skill_idx = section.find("## Available Skills")
        protocol_idx = section.find("CRITICAL: Exploration-first protocol")
        assert skill_idx > 0, "skill block must be present"
        assert protocol_idx > 0, "exploration protocol must be present"
        assert skill_idx < protocol_idx, (
            "skill block must appear BEFORE the exploration-first "
            "protocol so the model reads available skills as part "
            "of the tool catalog"
        )

    def test_skill_block_after_built_in_tools(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "true",
        )
        cat = get_default_catalog()
        cat.register(
            _make_manifest("sample_skill"),
            source=SkillSource.OPERATOR,
        )
        section = _build_tool_section()
        skill_idx = section.find("## Available Skills")
        # Built-in catalog uses "## Available Tools" header
        tools_idx = section.find("## Available Tools")
        assert tools_idx >= 0
        assert skill_idx > tools_idx, (
            "skill block must appear AFTER the built-in tool "
            "catalog (skills extend the tool surface)"
        )


# ============================================================================
# §6 — Best-effort: skill render failure degrades silently
# ============================================================================


class TestBestEffortDegradation:
    def test_render_failure_does_not_break_prompt(self, monkeypatch):
        """Patch render_skill_tool_block to raise; the prompt
        builder should swallow + continue."""
        import backend.core.ouroboros.governance.skill_venom_bridge as bridge

        monkeypatch.setenv(
            "JARVIS_SKILL_PROMPT_INJECTION_ENABLED", "true",
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated bridge failure")

        monkeypatch.setattr(
            bridge, "render_skill_tool_block", _boom,
        )
        # Prompt build should NOT raise + block should not appear
        section = _build_tool_section()
        assert "## Available Skills" not in section
        # But the rest of the prompt is intact
        assert "Exploration-first protocol" in section
        assert "## Available Tools" in section


# ============================================================================
# §7 — AST authority: lazy import inside _build_tool_section
# ============================================================================


class TestImportDiscipline:
    @pytest.fixture(scope="class")
    def source(self):
        return _PROVIDERS_PATH.read_text(encoding="utf-8")

    def test_skill_bridge_not_at_module_level(self, source):
        """The skill_venom_bridge import must live INSIDE
        _build_tool_section (lazy) so the providers module's
        existing import surface stays unchanged."""
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "skill_venom_bridge" not in module, (
                    "skill_venom_bridge must be lazy-imported "
                    "inside _build_tool_section, not at module "
                    "level"
                )

    def test_master_flag_helper_present(self, source):
        # Pin the env knob name canonical
        assert "_skill_prompt_injection_enabled" in source
        assert "JARVIS_SKILL_PROMPT_INJECTION_ENABLED" in source

    def test_render_skill_tool_block_referenced(self, source):
        # Pin the function name reference so a refactor that
        # renames the bridge function gets caught here.
        assert "render_skill_tool_block" in source
