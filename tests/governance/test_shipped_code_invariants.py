"""Priority E — Shipped-code structural invariants regression spine.

Pins the structural enforcement that prevents future refactors from
silently regressing Priority A wiring (the soak #3 silent-disable
gap). This module promotes the source-grep test in A3 to a runtime-
callable structural primitive callable from CI / battle-test boot.

Pins:
  §1   Master flag default true; case-tolerant
  §2   Frozen ShippedCodeInvariant + InvariantViolation schemas
  §3   Registry — idempotent + overwrite contract + alphabetical-
       stable list + reset_for_tests
  §4   Seed invariant `plan_runner_default_claims_wiring` registered
       at module load
  §5   AST helper _is_phase_result_call detects ast.Call with Name
       'PhaseResult'
  §6   AST helper _is_return_phase_result detects Return with
       PhaseResult value
  §7   Bytes window helper extracts correct lookback range
  §8   Live invariant check passes against shipped plan_runner.py
       (proves Priority A wiring is intact)
  §9   Live invariant check FAILS on a synthetic plan_runner-shaped
       file with a Return PhaseResult and NO helper call (proves
       the validator detects regressions)
  §10  validate_invariant returns empty when target file missing
  §11  validate_invariant returns empty when validator raises
  §12  validate_all master-off returns empty
  §13  Order-2 manifest extended with the new entries
  §14  Authority invariants — no orchestrator/policy/iron_gate imports
  §15  Public API surface
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    InvariantViolation,
    SHIPPED_CODE_INVARIANTS_SCHEMA_VERSION,
    ShippedCodeInvariant,
    list_shipped_code_invariants,
    register_shipped_code_invariant,
    reset_registry_for_tests,
    shipped_code_invariants_enabled,
    unregister_shipped_code_invariant,
    validate_all,
    validate_invariant,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    _bytes_window_above,
    _is_phase_result_call,
    _is_return_phase_result,
    _validate_plan_runner_default_claims,
)


@pytest.fixture
def fresh_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ===========================================================================
# §1 — Master flag
# ===========================================================================


def test_master_flag_default_true(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", raising=False,
    )
    assert shipped_code_invariants_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_reads_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", val,
    )
    assert shipped_code_invariants_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_master_flag_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", val,
    )
    assert shipped_code_invariants_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_master_flag_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", val,
    )
    assert shipped_code_invariants_enabled() is False


# ===========================================================================
# §2 — Frozen schemas
# ===========================================================================


def test_invariant_dataclass_frozen() -> None:
    inv = ShippedCodeInvariant(
        invariant_name="x", target_file="y.py",
        description="d", validate=lambda t, s: (),
    )
    with pytest.raises(Exception):
        inv.invariant_name = "z"  # type: ignore[misc]


def test_violation_dataclass_frozen_and_serialises() -> None:
    v = InvariantViolation(
        invariant_name="x", target_file="y.py", detail="d",
    )
    with pytest.raises(Exception):
        v.detail = "other"  # type: ignore[misc]
    d = v.to_dict()
    assert d["invariant_name"] == "x"
    assert d["target_file"] == "y.py"
    assert d["detail"] == "d"
    assert d["schema_version"] == SHIPPED_CODE_INVARIANTS_SCHEMA_VERSION


# ===========================================================================
# §3 — Registry surface
# ===========================================================================


def test_register_idempotent_on_identical(fresh_registry) -> None:
    fn = lambda t, s: ()
    inv = ShippedCodeInvariant(
        invariant_name="custom", target_file="foo.py",
        description="d", validate=fn,
    )
    register_shipped_code_invariant(inv)
    register_shipped_code_invariant(inv)
    assert sum(
        1 for i in list_shipped_code_invariants()
        if i.invariant_name == "custom"
    ) == 1


def test_register_rejects_different_without_overwrite(
    fresh_registry,
) -> None:
    inv1 = ShippedCodeInvariant(
        invariant_name="custom", target_file="foo.py",
        description="A", validate=lambda t, s: (),
    )
    inv2 = ShippedCodeInvariant(
        invariant_name="custom", target_file="foo.py",
        description="B", validate=lambda t, s: (),
    )
    register_shipped_code_invariant(inv1)
    register_shipped_code_invariant(inv2)
    custom = [
        i for i in list_shipped_code_invariants()
        if i.invariant_name == "custom"
    ]
    assert len(custom) == 1
    assert custom[0].description == "A"


def test_unregister_returns_correct_status(fresh_registry) -> None:
    register_shipped_code_invariant(
        ShippedCodeInvariant(
            invariant_name="ephemeral", target_file="foo.py",
            description="d", validate=lambda t, s: (),
        ),
    )
    assert unregister_shipped_code_invariant("ephemeral") is True
    assert unregister_shipped_code_invariant("ephemeral") is False
    assert unregister_shipped_code_invariant("never") is False


def test_list_alphabetical_stable(fresh_registry) -> None:
    invs = list_shipped_code_invariants()
    names = [i.invariant_name for i in invs]
    assert names == sorted(names)


# ===========================================================================
# §4 — Seed invariant
# ===========================================================================


def test_seed_plan_runner_invariant_registered(fresh_registry) -> None:
    invs = list_shipped_code_invariants()
    names = [i.invariant_name for i in invs]
    assert "plan_runner_default_claims_wiring" in names


# ===========================================================================
# §5-§7 — AST + bytes helpers
# ===========================================================================


def test_is_phase_result_call_detects_real_call() -> None:
    src = "PhaseResult(next_ctx=ctx, next_phase=None)"
    tree = ast.parse(src, mode="eval")
    assert _is_phase_result_call(tree.body)


def test_is_phase_result_call_rejects_other_calls() -> None:
    for src in [
        "OtherFunc(x)", "x.PhaseResult()", "func()", "1 + 2",
    ]:
        tree = ast.parse(src, mode="eval")
        assert not _is_phase_result_call(tree.body)


def test_is_return_phase_result_detects_correctly() -> None:
    src = "def f():\n    return PhaseResult(x=1)\n"
    tree = ast.parse(src)
    rets = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
    assert len(rets) == 1
    assert _is_return_phase_result(rets[0])


def test_is_return_phase_result_rejects_other_returns() -> None:
    src = "def f():\n    return 42\n    return None\n"
    tree = ast.parse(src)
    for ret in [n for n in ast.walk(tree) if isinstance(n, ast.Return)]:
        assert not _is_return_phase_result(ret)


def test_bytes_window_extracts_correctly() -> None:
    src = "\n".join(f"line{i}" for i in range(1, 11))  # 10 lines
    window = _bytes_window_above(src, target_line=8, lookback=3)
    # Lines 5, 6, 7 (lookback=3 above line 8, exclusive of 8)
    assert "line5" in window
    assert "line6" in window
    assert "line7" in window
    assert "line8" not in window


# ===========================================================================
# §8 — Live invariant against shipped plan_runner.py PASSES
# ===========================================================================


def test_live_invariant_passes_on_shipped_plan_runner(
    fresh_registry,
) -> None:
    """Priority E's central guarantee: the shipped plan_runner.py
    has the helper call before every PhaseResult exit."""
    violations = validate_all()
    plan_runner_violations = [
        v for v in violations
        if v.invariant_name == "plan_runner_default_claims_wiring"
    ]
    assert plan_runner_violations == []


# ===========================================================================
# §9 — Synthetic regression PROVES the validator catches refactors
# ===========================================================================


def test_validator_detects_missing_helper_call(tmp_path) -> None:
    """Synthesize a plan_runner-shaped file with a Return PhaseResult
    but NO helper call. Validator should flag it."""
    bad_source = textwrap.dedent("""
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseResult, PhaseRunner,
        )

        class BadRunner(PhaseRunner):
            async def run(self, ctx):
                # No _capture_default_claims_at_plan_exit call here —
                # this is the regression we want to catch.
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="fail",
                    reason="silent_disable",
                )
    """)
    tree = ast.parse(bad_source)
    violations = _validate_plan_runner_default_claims(tree, bad_source)
    assert len(violations) == 1
    assert "_capture_default_claims_at_plan_exit" in violations[0]


def test_validator_passes_when_helper_present(tmp_path) -> None:
    """Sanity: synthesized file WITH the helper call before each
    return passes."""
    good_source = textwrap.dedent("""
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseResult, PhaseRunner,
        )

        class GoodRunner(PhaseRunner):
            async def run(self, ctx):
                # Helper call precedes the return — invariant holds.
                await _capture_default_claims_at_plan_exit(
                    ctx, exit_reason="planned",
                )
                return PhaseResult(
                    next_ctx=ctx, next_phase=None, status="ok",
                    reason="planned",
                )
    """)
    tree = ast.parse(good_source)
    violations = _validate_plan_runner_default_claims(tree, good_source)
    assert violations == ()


def test_validator_filters_docstring_false_positives(tmp_path) -> None:
    """A docstring containing 'return PhaseResult(...)' must NOT
    trigger the validator (AST filters this; bytes-only regex
    couldn't)."""
    source_with_docstring = textwrap.dedent('''
        """The PLAN runner has 7 exit paths, each with
        `return PhaseResult(...)`. Documenting in a docstring
        must not be flagged as an exit point."""
        # No actual return statements at all.
    ''')
    tree = ast.parse(source_with_docstring)
    violations = _validate_plan_runner_default_claims(
        tree, source_with_docstring,
    )
    assert violations == ()


# ===========================================================================
# §10-§12 — Defensive contracts
# ===========================================================================


def test_validate_invariant_missing_target_returns_empty(
    fresh_registry,
) -> None:
    inv = ShippedCodeInvariant(
        invariant_name="missing",
        target_file="nonexistent/path.py",
        description="d",
        validate=lambda t, s: ("would have flagged",),
    )
    register_shipped_code_invariant(inv)
    violations = validate_invariant(inv)
    assert violations == ()


def test_validate_invariant_validator_raises_returns_empty(
    fresh_registry,
) -> None:
    def boom(tree, source):
        raise RuntimeError("boom")

    inv = ShippedCodeInvariant(
        invariant_name="boom",
        target_file=(
            "backend/core/ouroboros/governance/phase_runners/"
            "plan_runner.py"
        ),
        description="d",
        validate=boom,
    )
    register_shipped_code_invariant(inv)
    violations = validate_invariant(inv)
    assert violations == ()


def test_validate_all_master_off_returns_empty(
    fresh_registry, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", "false",
    )
    violations = validate_all()
    assert violations == ()


# ===========================================================================
# §13 — Order-2 manifest extension
# ===========================================================================


def test_order2_manifest_includes_priority_e_entries() -> None:
    """The 3 Priority A/E governance entries are pinned in the
    manifest. Operator amendment of these files now requires the
    Pass B amend protocol."""
    here = Path(__file__).resolve().parent
    cur = here
    while cur != cur.parent and not (cur / "CLAUDE.md").exists():
        cur = cur.parent
    manifest = cur / ".jarvis" / "order2_manifest.yaml"
    assert manifest.exists()
    text = manifest.read_text(encoding="utf-8")
    assert "verification/default_claims.py" in text
    assert "plan_generator.py" in text
    assert "shipped_code_invariants.py" in text


# ===========================================================================
# §14 — Authority invariants
# ===========================================================================


def test_no_authority_imports() -> None:
    from backend.core.ouroboros.governance.meta import shipped_code_invariants
    src = inspect.getsource(shipped_code_invariants)
    forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator", "gate",
        "semantic_guardian", "semantic_firewall",
    )
    for token in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{token}" not in src
        ), f"shipped_code_invariants must not import {token}"
        assert (
            f"import backend.core.ouroboros.governance.{token}" not in src
        ), f"shipped_code_invariants must not import {token}"


def test_pure_stdlib_imports() -> None:
    """Only stdlib + meta package may be imported."""
    from backend.core.ouroboros.governance.meta import shipped_code_invariants
    src = inspect.getsource(shipped_code_invariants)
    # No subprocess, no network, no LLM
    assert "import subprocess" not in src
    assert "import socket" not in src
    assert "anthropic" not in src
    assert "openai" not in src


# ===========================================================================
# §15 — Public API
# ===========================================================================


def test_public_api_exposed() -> None:
    from backend.core.ouroboros.governance.meta import shipped_code_invariants
    expected = {
        "InvariantViolation",
        "ShippedCodeInvariant",
        "ShippedCodeValidator",
        "list_shipped_code_invariants",
        "register_shipped_code_invariant",
        "reset_registry_for_tests",
        "shipped_code_invariants_enabled",
        "unregister_shipped_code_invariant",
        "validate_all",
        "validate_invariant",
    }
    for name in expected:
        assert name in shipped_code_invariants.__all__
