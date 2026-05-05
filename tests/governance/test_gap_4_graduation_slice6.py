"""Tests for Gap #4 Slice 6 — graduation: master flag default-true,
FlagRegistry self-registration, 4 shipped_code_invariants AST pins.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.review_coordinator import (
    MASTER_FLAG_ENV_VAR,
    is_master_flag_enabled,
    register_flags,
    register_shipped_invariants,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)


# ===========================================================================
# Master flag default — graduated to TRUE on 2026-05-04
# ===========================================================================


def test_master_flag_default_is_true_post_graduation():
    assert is_master_flag_enabled() is True


def test_master_flag_explicit_off_disables(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    assert is_master_flag_enabled() is False


# ===========================================================================
# register_flags — module-owned discovery
# ===========================================================================


class _StubRegistry:
    def __init__(self):
        self.registered = []

    def register(self, spec):
        self.registered.append(spec)


def test_register_flags_seeds_four_specs():
    reg = _StubRegistry()
    count = register_flags(reg)
    assert count == 4
    names = {spec.name for spec in reg.registered}
    assert names == {
        "JARVIS_REVIEW_BRANCH_ENABLED",
        "JARVIS_REVIEW_TIMEOUT_S",
        "JARVIS_DIFF_ARCHIVE_SIZE",
        "JARVIS_REVIEW_BRANCH_GIT_TIMEOUT_S",
    }


def test_master_flag_default_true_in_registry():
    reg = _StubRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == "JARVIS_REVIEW_BRANCH_ENABLED"
    )
    assert master.default is True


def test_default_timeout_is_300():
    reg = _StubRegistry()
    register_flags(reg)
    timeout = next(
        s for s in reg.registered if s.name == "JARVIS_REVIEW_TIMEOUT_S"
    )
    assert timeout.default == 300.0


# ===========================================================================
# register_shipped_invariants — 4 AST pins
# ===========================================================================


def test_register_shipped_invariants_returns_four():
    invariants = register_shipped_invariants()
    assert len(invariants) == 4
    names = {inv.invariant_name for inv in invariants}
    assert names == {
        "review_state_vocabulary_frozen",
        "diff_outcome_vocabulary_frozen",
        "orchestrator_review_hook_present",
        "serpent_repl_review_handlers_present",
    }


def _get_validator(name: str):
    invariants = register_shipped_invariants()
    matches = [inv for inv in invariants if inv.invariant_name == name]
    assert matches, f"missing invariant {name!r}"
    return matches[0].validate


def _load(rel_path: str):
    repo_root = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
    src = (repo_root / rel_path).read_text()
    import ast
    return ast.parse(src), src


# ===========================================================================
# Each pin passes against today's shipped source
# ===========================================================================


def test_pin_review_state_passes_today():
    validator = _get_validator("review_state_vocabulary_frozen")
    tree, src = _load(
        "backend/core/ouroboros/governance/review_branch_manager.py",
    )
    assert validator(tree, src) == ()


def test_pin_diff_outcome_passes_today():
    validator = _get_validator("diff_outcome_vocabulary_frozen")
    tree, src = _load(
        "backend/core/ouroboros/battle_test/diff_archive.py",
    )
    assert validator(tree, src) == ()


def test_pin_orchestrator_hook_passes_today():
    validator = _get_validator("orchestrator_review_hook_present")
    tree, src = _load(
        "backend/core/ouroboros/governance/orchestrator.py",
    )
    assert validator(tree, src) == ()


def test_pin_serpent_repl_handlers_passes_today():
    validator = _get_validator("serpent_repl_review_handlers_present")
    tree, src = _load(
        "backend/core/ouroboros/battle_test/serpent_flow.py",
    )
    assert validator(tree, src) == ()


# ===========================================================================
# Synthetic-positive: each pin fires on a deliberately broken source
# ===========================================================================


def test_pin_review_state_detects_missing_value():
    validator = _get_validator("review_state_vocabulary_frozen")
    fake = """
class ReviewState(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    # REJECTED removed
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
"""
    import ast
    tree = ast.parse(fake)
    violations = validator(tree, fake)
    assert violations
    assert "REJECTED" in violations[0]


def test_pin_orchestrator_hook_detects_removed_marker():
    validator = _get_validator("orchestrator_review_hook_present")
    import ast
    tree = ast.parse("# no hook here\n")
    violations = validator(tree, "# no hook here\n")
    assert len(violations) >= 1


def test_pin_serpent_repl_detects_missing_handler():
    validator = _get_validator("serpent_repl_review_handlers_present")
    fake = """
class SerpentREPL:
    async def _handle_accept(self, line):
        pass
    # _handle_reject + _handle_review removed
"""
    import ast
    tree = ast.parse(fake)
    violations = validator(tree, fake)
    assert violations
    assert any("_handle_reject" in v or "_handle_review" in v for v in violations)


# ===========================================================================
# End-to-end production-boot integration
# ===========================================================================


def test_ensure_seeded_resolves_our_flags():
    """Flags must be reachable via the production seed-boot path."""
    from backend.core.ouroboros.governance.flag_registry import ensure_seeded
    reg = ensure_seeded()
    master = reg.get_spec("JARVIS_REVIEW_BRANCH_ENABLED")
    timeout = reg.get_spec("JARVIS_REVIEW_TIMEOUT_S")
    assert master is not None and master.default is True
    assert timeout is not None and timeout.default == 300.0


def test_shipped_invariants_pass_live_validation():
    """All 4 Gap #4 pins pass when validated against today's source."""
    from backend.core.ouroboros.governance.meta import (
        shipped_code_invariants as sci,
    )
    results = sci.validate_all()
    ours = [
        r for r in results
        if r.invariant_name in {
            "review_state_vocabulary_frozen",
            "diff_outcome_vocabulary_frozen",
            "orchestrator_review_hook_present",
            "serpent_repl_review_handlers_present",
        }
    ]
    assert ours == [], (
        f"Gap #4 pin violations: {[r.detail for r in ours]}"
    )
