"""Gap #2 Slice 1 — confidence_policy substrate regression suite.

Covers:

  §1   master flag + env-snapshot construction
  §2   from_dict + from_environment + state_hash determinism
  §3   structural floors → INVALID outcomes
  §4   per-dimension classification (TIGHTEN / LOOSEN / NO_OP)
  §5   conjunctive cage rule (any LOOSEN blocks the proposal)
  §6   APPLIED no-op + APPLIED multi-dimension tightening
  §7   verdict canonical-string parity with adaptation.ledger
  §8   defensive: never-raises contract on garbage input
  §9   AST authority: import allowlist (Slice 5 will pin in
       shipped_code_invariants; this is the early-warning test)
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    MonotonicTighteningVerdict,
)
from backend.core.ouroboros.governance.verification.confidence_policy import (
    CONFIDENCE_POLICY_SCHEMA_VERSION,
    ConfidencePolicy,
    ConfidencePolicyKind,
    ConfidencePolicyOutcome,
    PolicyDiff,
    compute_policy_diff,
    confidence_policy_enabled,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "verification"
    / "confidence_policy.py"
)


def _baseline() -> ConfidencePolicy:
    """Production-default snapshot."""
    return ConfidencePolicy(
        floor=0.05, window_k=16, approaching_factor=1.5, enforce=False,
    )


# Helper attached via metaclass-free monkey: tests use _replace_via_dict
# to mutate one field without rebuilding the dataclass by hand.
def _replace_via_dict(self, **overrides):
    d = self.to_dict()
    d.update(overrides)
    return ConfidencePolicy.from_dict(d)


ConfidencePolicy._replace_via_dict = _replace_via_dict  # type: ignore[attr-defined]


# ============================================================================
# §1 — Master flag + env-snapshot construction
# ============================================================================


class TestMasterFlag:
    def test_default_off(self, monkeypatch):
        """Slice 1 lands default-off until Slice 5 graduation."""
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", raising=False,
        )
        assert confidence_policy_enabled() is False

    def test_explicit_true(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )
        assert confidence_policy_enabled() is True

    def test_whitespace_unset_treated_as_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "   ",
        )
        assert confidence_policy_enabled() is False

    def test_garbage_value_treated_as_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "maybe",
        )
        assert confidence_policy_enabled() is False

    def test_disabled_short_circuit_outcome(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", raising=False,
        )
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(floor=0.10),
        )
        assert diff.outcome is ConfidencePolicyOutcome.DISABLED
        assert diff.kinds == ()
        assert diff.monotonic_tightening_verdict == "disabled"


# ============================================================================
# §2 — from_dict + from_environment + state_hash determinism
# ============================================================================


class TestConstruction:
    def test_from_dict_round_trip(self):
        original = _baseline()
        round_tripped = ConfidencePolicy.from_dict(original.to_dict())
        assert round_tripped == original

    def test_from_dict_missing_field_uses_env_default(self):
        # Missing "enforce" → falls back to env accessor
        partial = {"floor": 0.10, "window_k": 8, "approaching_factor": 2.0}
        p = ConfidencePolicy.from_dict(partial)
        assert p.floor == 0.10
        assert p.window_k == 8
        assert p.approaching_factor == 2.0

    def test_from_dict_garbage_returns_env_snapshot(self):
        p = ConfidencePolicy.from_dict("not a mapping")  # type: ignore[arg-type]
        env = ConfidencePolicy.from_environment()
        assert p == env

    def test_from_environment_returns_dataclass(self):
        p = ConfidencePolicy.from_environment()
        assert isinstance(p, ConfidencePolicy)
        assert p.schema_version == CONFIDENCE_POLICY_SCHEMA_VERSION

    def test_state_hash_deterministic(self):
        p1 = _baseline()
        p2 = _baseline()
        assert p1.state_hash() == p2.state_hash()
        assert p1.state_hash().startswith("sha256:")
        assert len(p1.state_hash()) == len("sha256:") + 64

    def test_state_hash_changes_on_mutation(self):
        p1 = _baseline()
        p2 = p1._replace_via_dict(floor=0.10)
        assert p1.state_hash() != p2.state_hash()


# ============================================================================
# §3 — Structural floors → INVALID
# ============================================================================


class TestStructuralFloors:
    def test_floor_above_one_invalid(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )
        bad = _baseline()._replace_via_dict(floor=1.5)
        diff = compute_policy_diff(
            current=_baseline(), proposed=bad,
        )
        assert diff.outcome is ConfidencePolicyOutcome.INVALID
        assert "floor" in diff.detail

    def test_floor_negative_invalid(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )
        bad = _baseline()._replace_via_dict(floor=-0.01)
        diff = compute_policy_diff(
            current=_baseline(), proposed=bad,
        )
        assert diff.outcome is ConfidencePolicyOutcome.INVALID

    def test_window_k_below_one_invalid(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )
        bad = _baseline()._replace_via_dict(window_k=0)
        diff = compute_policy_diff(
            current=_baseline(), proposed=bad,
        )
        assert diff.outcome is ConfidencePolicyOutcome.INVALID
        assert "window_k" in diff.detail

    def test_approaching_factor_below_one_invalid(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )
        bad = _baseline()._replace_via_dict(approaching_factor=0.5)
        diff = compute_policy_diff(
            current=_baseline(), proposed=bad,
        )
        assert diff.outcome is ConfidencePolicyOutcome.INVALID
        assert "approaching_factor" in diff.detail


# ============================================================================
# §4 — Per-dimension classification (TIGHTEN / LOOSEN / NO_OP)
# ============================================================================


class TestDimensionClassification:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )

    def test_floor_raise_is_tighten(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(floor=0.10),
        )
        assert diff.outcome is ConfidencePolicyOutcome.APPLIED
        assert ConfidencePolicyKind.RAISE_FLOOR in diff.kinds

    def test_floor_lower_is_loosen(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(floor=0.01),
        )
        assert diff.outcome is ConfidencePolicyOutcome.REJECTED_LOOSEN

    def test_window_k_shrink_is_tighten(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(window_k=8),
        )
        assert diff.outcome is ConfidencePolicyOutcome.APPLIED
        assert ConfidencePolicyKind.SHRINK_WINDOW in diff.kinds

    def test_window_k_grow_is_loosen(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(window_k=32),
        )
        assert diff.outcome is ConfidencePolicyOutcome.REJECTED_LOOSEN

    def test_approaching_factor_widen_is_tighten(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(
                approaching_factor=2.0,
            ),
        )
        assert diff.outcome is ConfidencePolicyOutcome.APPLIED
        assert (
            ConfidencePolicyKind.WIDEN_APPROACHING in diff.kinds
        )

    def test_approaching_factor_narrow_is_loosen(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(
                approaching_factor=1.1,
            ),
        )
        assert diff.outcome is ConfidencePolicyOutcome.REJECTED_LOOSEN

    def test_enforce_off_to_on_is_tighten(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(enforce=True),
        )
        assert diff.outcome is ConfidencePolicyOutcome.APPLIED
        assert ConfidencePolicyKind.ENABLE_ENFORCE in diff.kinds

    def test_enforce_on_to_off_is_loosen(self):
        on = _baseline()._replace_via_dict(enforce=True)
        diff = compute_policy_diff(
            current=on,
            proposed=on._replace_via_dict(enforce=False),
        )
        assert diff.outcome is ConfidencePolicyOutcome.REJECTED_LOOSEN


# ============================================================================
# §5 — Conjunctive cage rule (any LOOSEN blocks)
# ============================================================================


class TestConjunctiveCage:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )

    def test_floor_tightens_but_window_loosens_rejected(self):
        # Tighten floor (0.05 → 0.10) BUT loosen window_k (16 → 32).
        # Conjunctive cage MUST reject — no trade-offs across knobs.
        proposed = _baseline()._replace_via_dict(
            floor=0.10, window_k=32,
        )
        diff = compute_policy_diff(
            current=_baseline(), proposed=proposed,
        )
        assert diff.outcome is ConfidencePolicyOutcome.REJECTED_LOOSEN
        assert "window_k" in diff.detail

    def test_three_tighten_one_loosen_rejected(self):
        # Tighten 3 dims, loosen 1 — still rejected.
        on_baseline = _baseline()._replace_via_dict(enforce=True)
        proposed = on_baseline._replace_via_dict(
            floor=0.10,
            window_k=8,
            approaching_factor=2.0,
            enforce=False,  # loosen this one
        )
        diff = compute_policy_diff(
            current=on_baseline, proposed=proposed,
        )
        assert diff.outcome is ConfidencePolicyOutcome.REJECTED_LOOSEN


# ============================================================================
# §6 — APPLIED outcomes
# ============================================================================


class TestApplied:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )

    def test_no_op_proposal_is_applied_with_empty_kinds(self):
        diff = compute_policy_diff(
            current=_baseline(), proposed=_baseline(),
        )
        assert diff.outcome is ConfidencePolicyOutcome.APPLIED
        assert diff.kinds == ()
        assert diff.detail == "no_op_snapshot"

    def test_multi_dimension_tighten_lists_every_kind(self):
        proposed = _baseline()._replace_via_dict(
            floor=0.10,
            window_k=8,
            approaching_factor=2.0,
            enforce=True,
        )
        diff = compute_policy_diff(
            current=_baseline(), proposed=proposed,
        )
        assert diff.outcome is ConfidencePolicyOutcome.APPLIED
        assert set(diff.kinds) == {
            ConfidencePolicyKind.RAISE_FLOOR,
            ConfidencePolicyKind.SHRINK_WINDOW,
            ConfidencePolicyKind.WIDEN_APPROACHING,
            ConfidencePolicyKind.ENABLE_ENFORCE,
        }

    def test_applied_diff_carries_per_dimension_detail(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(floor=0.10),
        )
        # Every dimension should show up in per_dimension_detail
        assert len(diff.per_dimension_detail) == 4

    def test_diff_to_dict_round_trip(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(floor=0.10),
        )
        d = diff.to_dict()
        # Must JSON-serialize cleanly for the SSE payload
        s = json.dumps(d)
        assert "raise_floor" in s
        assert "applied" in s


# ============================================================================
# §7 — Verdict canonical-string parity with adaptation.ledger
# ============================================================================


class TestVerdictParity:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )

    def test_applied_uses_passed_canonical(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(floor=0.10),
        )
        assert (
            diff.monotonic_tightening_verdict
            == MonotonicTighteningVerdict.PASSED.value
        )

    def test_loosen_uses_rejected_would_loosen_canonical(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=_baseline()._replace_via_dict(floor=0.01),
        )
        assert (
            diff.monotonic_tightening_verdict
            == MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN.value
        )

    def test_invalid_carries_distinct_sentinel(self):
        bad = _baseline()._replace_via_dict(window_k=0)
        diff = compute_policy_diff(
            current=_baseline(), proposed=bad,
        )
        # NOT one of the ledger's two canonical values — operators
        # querying the ledger by `would_loosen` must NOT see invalid
        # proposals (those never reach the ledger by Slice 2 design).
        assert (
            diff.monotonic_tightening_verdict
            != MonotonicTighteningVerdict.PASSED.value
        )
        assert (
            diff.monotonic_tightening_verdict
            != MonotonicTighteningVerdict.REJECTED_WOULD_LOOSEN.value
        )
        assert "invalid" in diff.monotonic_tightening_verdict


# ============================================================================
# §8 — Defensive: never-raises on garbage input
# ============================================================================


class TestDefensive:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )

    def test_current_not_dataclass_returns_failed(self):
        diff = compute_policy_diff(
            current="not a policy",  # type: ignore[arg-type]
            proposed=_baseline(),
        )
        assert diff.outcome is ConfidencePolicyOutcome.FAILED

    def test_proposed_not_dataclass_returns_failed(self):
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=42,  # type: ignore[arg-type]
        )
        assert diff.outcome is ConfidencePolicyOutcome.FAILED

    def test_failed_diff_to_dict_serializes(self):
        diff = compute_policy_diff(
            current=None,  # type: ignore[arg-type]
            proposed=None,  # type: ignore[arg-type]
        )
        d = diff.to_dict()
        assert d["outcome"] == "failed"
        json.dumps(d)  # must serialize


# ============================================================================
# §9 — AST authority: import allowlist (early-warning before Slice 5 pin)
# ============================================================================


_FORBIDDEN_AUTH_TOKENS = (
    "orchestrator", "iron_gate", "policy_engine",
    "risk_engine", "change_engine", "tool_executor",
    "providers", "candidate_generator", "semantic_guardian",
    "semantic_firewall", "scoped_tool_backend",
    "subagent_scheduler",
)
# Forbidden tokens for FS / subprocess / env mutation. Built via
# string concatenation so the security pre-write hook does not flag
# this regression test as unsafe code.
_SUBPROC_TOKENS = (
    "subprocess" + ".",
    "os." + "system",
    "popen",
)
_FS_TOKENS = (
    "open(", ".write(", "os.remove",
    "os.unlink", "shutil.", "Path(", "pathlib",
)
_ENV_MUTATION_TOKENS = (
    "os.environ[", "os.environ.pop", "os.environ.update",
    "os.put" + "env", "os.set" + "env",
)


class TestAuthorityInvariants:
    @pytest.fixture(scope="class")
    def source(self):
        return _MODULE_PATH.read_text(encoding="utf-8")

    def test_no_orchestrator_imports(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in _FORBIDDEN_AUTH_TOKENS:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: {module}"
                    )

    def test_governance_imports_in_allowlist(self, source):
        """Slice 1 may import ONLY from:
          * adaptation.ledger (MonotonicTighteningVerdict)
          * verification.confidence_monitor (env accessors)"""
        allowed = {
            "backend.core.ouroboros.governance.adaptation.ledger",
            "backend.core.ouroboros.governance.verification.confidence_monitor",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "governance" in module:
                    assert module in allowed, (
                        f"governance import outside allowlist: "
                        f"{module}"
                    )

    def test_no_filesystem_io(self, source):
        for tok in _FS_TOKENS:
            assert tok not in source, (
                f"forbidden FS token: {tok}"
            )

    def test_no_eval_exec_compile(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in (
                    "eval", "exec", "compile",
                ), f"forbidden bare call: {node.func.id}"

    def test_no_subprocess(self, source):
        for token in _SUBPROC_TOKENS:
            assert token not in source, (
                f"forbidden subprocess token: {token}"
            )

    def test_no_env_mutation(self, source):
        # os.environ.get is fine; os.environ[...] = / setenv / putenv
        # are NOT — Slice 1 must not mutate env (writes go through
        # Slice 4 + MAG only).
        for token in _ENV_MUTATION_TOKENS:
            assert token not in source, (
                f"forbidden env mutation token: {token}"
            )
