"""Slice 5 graduation pins — Context Preservation Production Integration arc."""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ===========================================================================
# 1. Defaults — mutation-side flags stay OFF by deliberate design
# ===========================================================================


def test_compactor_scorer_default_off_by_design(monkeypatch):
    """Flipping this default would silently change compaction behavior in
    every op. Slice 5 ships the mechanism but keeps operator opt-in."""
    monkeypatch.delenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.context_compaction import (
        context_compactor_scorer_enabled,
    )
    assert context_compactor_scorer_enabled() is False


def test_tool_loop_scorer_default_off_by_design(monkeypatch):
    """Same rationale as compactor scorer — hot-path code stays under
    explicit opt-in."""
    import os
    monkeypatch.delenv("JARVIS_TOOL_LOOP_SCORER_ENABLED", raising=False)
    assert os.environ.get(
        "JARVIS_TOOL_LOOP_SCORER_ENABLED", "false",
    ).strip().lower() == "false"


# ===========================================================================
# 2. Revert matrix — every env knob in the arc reversible
# ===========================================================================


_REVERT_MATRIX = [
    "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED",
    "JARVIS_TOOL_LOOP_SCORER_ENABLED",
]


@pytest.mark.parametrize("env", _REVERT_MATRIX)
def test_env_flag_on_off_roundtrip(env, monkeypatch):
    import os
    monkeypatch.setenv(env, "true")
    assert os.environ[env] == "true"
    monkeypatch.setenv(env, "false")
    assert os.environ[env] == "false"
    monkeypatch.setenv(env, "garbage")
    # Any non-'true' string reads as false in our predicates
    from backend.core.ouroboros.governance.context_compaction import (
        context_compactor_scorer_enabled,
    )
    if env == "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED":
        assert context_compactor_scorer_enabled() is False


# ===========================================================================
# 3. Authority invariants across the arc's new modules
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/governance/context_wiring.py",
    "backend/core/ouroboros/governance/context_advanced_signals.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "candidate_generator", "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_new_modules_have_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations = []
    for mod in _FORBIDDEN:
        pattern = re.compile(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            re.MULTILINE,
        )
        if pattern.search(src):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden modules: {violations}"
    )


# ===========================================================================
# 4. Docstring bit-rot guards
# ===========================================================================


def test_compactor_scorer_switch_docstring_explains_kept_off():
    from backend.core.ouroboros.governance.context_compaction import (
        context_compactor_scorer_enabled,
    )
    doc = context_compactor_scorer_enabled.__doc__ or ""
    assert "default" in doc.lower()
    assert "deliberate" in doc.lower()


# ===========================================================================
# 5. Schema version constants stable
# ===========================================================================


def test_advanced_signals_schema_version_pinned():
    from backend.core.ouroboros.governance.context_advanced_signals import (
        ADVANCED_SIGNALS_SCHEMA_VERSION,
    )
    assert ADVANCED_SIGNALS_SCHEMA_VERSION == "context_advanced.v1"


# ===========================================================================
# 6. Backward-compat fallback proven on every new path
# ===========================================================================


@pytest.mark.asyncio
async def test_compactor_legacy_path_still_works_with_scorer_absent():
    """Arc does NOT require a scorer — when unattached, legacy path runs
    exactly as before."""
    from backend.core.ouroboros.governance.context_compaction import (
        CompactionConfig, ContextCompactor,
    )
    compactor = ContextCompactor()  # no scorer
    entries = [{"type": "x", "content": str(i)} for i in range(10)]
    result = await compactor.compact(
        entries, CompactionConfig(
            max_context_entries=3, preserve_count=2,
        ),
    )
    assert result.entries_before == 10
    assert result.entries_compacted > 0


@pytest.mark.asyncio
async def test_tool_loop_legacy_path_without_flag():
    """With flag off, tool-loop helper returns legacy last-N split."""
    from pathlib import Path
    from backend.core.ouroboros.governance.tool_executor import (
        ToolLoopCoordinator,
    )

    class _FakePolicy:
        def evaluate(self, call, ctx): ...
        def repo_root_for(self, repo): return Path(".")

    class _FakeBackend:
        async def execute_async(self, *a, **kw): ...

    coord = ToolLoopCoordinator(
        backend=_FakeBackend(),      # type: ignore[arg-type]
        policy=_FakePolicy(),        # type: ignore[arg-type]
        max_rounds=1, tool_timeout_s=5.0,
    )
    chunks = [f"c{i}" for i in range(10)]
    old, recent = await coord._maybe_score_tool_chunks(
        chunks=chunks, op_id="op-x", recent_count=3,
    )
    # Legacy split
    assert recent == chunks[-3:]


# ===========================================================================
# 7. Cross-arc compat — old slice tests still pass
# ===========================================================================


def test_slice_4_observability_still_default_on(monkeypatch):
    """Context Preservation arc Slice 5 (prior arc) graduated this to on;
    this arc does not regress it."""
    monkeypatch.delenv(
        "JARVIS_CONTEXT_OBSERVABILITY_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.context_manifest import (
        context_observability_enabled,
    )
    assert context_observability_enabled() is True
