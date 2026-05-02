"""SBT-Probe Escalation Bridge Slice 3 — adapter tests.

Production BranchProber adapter wrapping Move 5's
ReadonlyEvidenceProber. Tests cover:
  * Deterministic branch_id → resolution_method rotation
  * Method always within READONLY_TOOL_ALLOWLIST
  * EvidenceKind mapping per method
  * Empty answer → empty evidence (defensive)
  * Resolver crash → empty evidence (defensive)
  * BranchEvidence content_hash = sha256(answer_text)
  * Singleton lazy-construction + reset_for_tests
  * Authority allowlist (no orchestrator-tier imports)
"""
from __future__ import annotations

import ast
import hashlib
import pathlib
from typing import Optional

import pytest

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (
    ProbeAnswer,
    ProbeQuestion,
)
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (
    READONLY_TOOL_ALLOWLIST,
    ReadonlyEvidenceProber,
)
from backend.core.ouroboros.governance.verification.sbt_branch_prober_adapter import (
    ADAPTER_DEFAULT_CONFIDENCE,
    ReadonlyBranchProberAdapter,
    SBT_BRANCH_PROBER_ADAPTER_SCHEMA_VERSION,
    _evidence_kind_for_method,
    _select_method_for_branch,
    get_default_branch_prober,
    reset_default_branch_prober_for_tests,
)
from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchEvidence,
    BranchTreeTarget,
    EvidenceKind,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResolver:
    """Test resolver that returns a fixed answer_text."""

    def __init__(self, answer_text: str = "test answer") -> None:
        self.answer_text = answer_text
        self.calls = 0

    def resolve(
        self, question: ProbeQuestion, *,
        max_tool_rounds: Optional[int] = None,
    ) -> ProbeAnswer:
        self.calls += 1
        return ProbeAnswer(
            question=question.question,
            answer_text=self.answer_text,
            evidence_fingerprint="fp",
            tool_rounds_used=1,
        )


class _CrashingResolver:
    def resolve(self, *args, **kwargs) -> ProbeAnswer:
        raise RuntimeError("forced crash")


class _EmptyResolver:
    def resolve(self, question: ProbeQuestion, **kwargs) -> ProbeAnswer:
        return ProbeAnswer(
            question=question.question,
            answer_text="",
            evidence_fingerprint="",
            tool_rounds_used=0,
        )


def _target() -> BranchTreeTarget:
    return BranchTreeTarget(
        decision_id="op-x|test",
        ambiguity_kind="probe_exhausted",
        ambiguity_payload={"hint": "test"},
    )


# ---------------------------------------------------------------------------
# Method rotation
# ---------------------------------------------------------------------------


class TestMethodRotation:
    def test_select_method_for_branch_returns_allowlist_member(self):
        """Every selection MUST be in READONLY_TOOL_ALLOWLIST."""
        for branch_id in [
            "branch-0", "branch-1", "branch-2", "anon",
            "op-x_lvl0_pos5",
        ]:
            method = _select_method_for_branch(branch_id)
            assert method in READONLY_TOOL_ALLOWLIST

    def test_select_method_is_deterministic(self):
        """Same branch_id → same method on every call (idempotent)."""
        a = _select_method_for_branch("branch-x")
        b = _select_method_for_branch("branch-x")
        assert a == b

    def test_select_method_distributes_across_allowlist(self):
        """Across many distinct branch_ids the rotation produces
        multiple methods — confirms the hash spreads (we're not
        always picking the same one)."""
        seen = set()
        for i in range(200):
            seen.add(_select_method_for_branch(f"branch-{i}"))
        # With 9 allowlist entries + 200 hashed inputs, expect
        # most to appear.
        assert len(seen) >= 5

    def test_select_method_handles_garbage(self):
        """None / empty / non-string falls back to first allowlist
        entry."""
        for g in [None, "", " "]:
            m = _select_method_for_branch(g)  # type: ignore[arg-type]
            assert m in READONLY_TOOL_ALLOWLIST


# ---------------------------------------------------------------------------
# EvidenceKind mapping
# ---------------------------------------------------------------------------


class TestEvidenceKindMapping:
    def test_known_methods_map_to_expected_kinds(self):
        assert _evidence_kind_for_method("read_file") is EvidenceKind.PATTERN_MATCH
        assert _evidence_kind_for_method("search_code") is EvidenceKind.PATTERN_MATCH
        assert _evidence_kind_for_method("get_callers") is EvidenceKind.CALLER_GRAPH
        assert _evidence_kind_for_method("list_symbols") is EvidenceKind.SYMBOL_LOOKUP
        assert _evidence_kind_for_method("list_dir") is EvidenceKind.FILE_READ
        assert _evidence_kind_for_method("git_blame") is EvidenceKind.FILE_READ

    def test_unknown_method_defaults_to_pattern_match(self):
        assert _evidence_kind_for_method("unknown_tool") is EvidenceKind.PATTERN_MATCH

    def test_garbage_method_defaults_safely(self):
        assert _evidence_kind_for_method(None) is EvidenceKind.PATTERN_MATCH  # type: ignore[arg-type]
        assert _evidence_kind_for_method("") is EvidenceKind.PATTERN_MATCH


# ---------------------------------------------------------------------------
# Adapter probe_branch behavior
# ---------------------------------------------------------------------------


class TestAdapterProbeBranch:
    def test_happy_path_emits_one_evidence(self):
        adapter = ReadonlyBranchProberAdapter(
            resolver=_FakeResolver("canonical answer"),
        )
        evidence = adapter.probe_branch(
            target=_target(), branch_id="b-0",
            depth=0, prior_evidence=(),
        )
        assert len(evidence) == 1
        assert isinstance(evidence[0], BranchEvidence)

    def test_content_hash_is_sha256_of_answer_text(self):
        adapter = ReadonlyBranchProberAdapter(
            resolver=_FakeResolver("specific answer text"),
        )
        evidence = adapter.probe_branch(
            target=_target(), branch_id="b-1", depth=0,
        )
        expected = hashlib.sha256(
            "specific answer text".encode("utf-8"),
        ).hexdigest()
        assert evidence[0].content_hash == expected

    def test_identical_answers_yield_identical_fingerprints(self):
        """Cross-branch convergence depends on this — identical
        answer text → identical content_hash → SBT classifies
        as CONVERGED."""
        adapter = ReadonlyBranchProberAdapter(
            resolver=_FakeResolver("same answer"),
        )
        e1 = adapter.probe_branch(
            target=_target(), branch_id="b-A", depth=0,
        )
        e2 = adapter.probe_branch(
            target=_target(), branch_id="b-B", depth=0,
        )
        assert e1[0].content_hash == e2[0].content_hash

    def test_source_tool_is_a_real_allowlist_entry(self):
        adapter = ReadonlyBranchProberAdapter(
            resolver=_FakeResolver("test"),
        )
        evidence = adapter.probe_branch(
            target=_target(), branch_id="b-tool", depth=0,
        )
        assert evidence[0].source_tool in READONLY_TOOL_ALLOWLIST

    def test_default_confidence(self):
        adapter = ReadonlyBranchProberAdapter(
            resolver=_FakeResolver("test"),
        )
        evidence = adapter.probe_branch(
            target=_target(), branch_id="b-conf", depth=0,
        )
        assert evidence[0].confidence == ADAPTER_DEFAULT_CONFIDENCE

    def test_custom_confidence_is_clamped(self):
        adapter = ReadonlyBranchProberAdapter(
            resolver=_FakeResolver("test"),
            confidence=999.0,
        )
        evidence = adapter.probe_branch(
            target=_target(), branch_id="b-clamp", depth=0,
        )
        assert evidence[0].confidence == 1.0

    def test_empty_answer_yields_empty_evidence(self):
        adapter = ReadonlyBranchProberAdapter(
            resolver=_EmptyResolver(),
        )
        evidence = adapter.probe_branch(
            target=_target(), branch_id="b-empty", depth=0,
        )
        assert evidence == ()

    def test_resolver_crash_yields_empty_evidence(self):
        adapter = ReadonlyBranchProberAdapter(
            resolver=_CrashingResolver(),
        )
        evidence = adapter.probe_branch(
            target=_target(), branch_id="b-crash", depth=0,
        )
        assert evidence == ()

    def test_adapter_never_raises_on_garbage_target(self):
        adapter = ReadonlyBranchProberAdapter(
            resolver=_FakeResolver("test"),
        )
        # Non-target object — adapter must defend.
        try:
            evidence = adapter.probe_branch(
                target=object(),  # type: ignore[arg-type]
                branch_id="b-bad", depth=0,
            )
            assert isinstance(evidence, tuple)
        except Exception:
            pytest.fail("adapter raised on garbage target")


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


class TestSingletonAccessor:
    def test_get_default_returns_adapter(self):
        reset_default_branch_prober_for_tests()
        a = get_default_branch_prober()
        assert isinstance(a, ReadonlyBranchProberAdapter)

    def test_singleton_is_stable_across_calls(self):
        reset_default_branch_prober_for_tests()
        a1 = get_default_branch_prober()
        a2 = get_default_branch_prober()
        assert a1 is a2

    def test_reset_for_tests_drops_singleton(self):
        a1 = get_default_branch_prober()
        reset_default_branch_prober_for_tests()
        a2 = get_default_branch_prober()
        assert a1 is not a2


# ---------------------------------------------------------------------------
# Schema + visible constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_schema_version_constant(self):
        assert SBT_BRANCH_PROBER_ADAPTER_SCHEMA_VERSION == (
            "sbt_branch_prober_adapter.1"
        )

    def test_default_confidence_in_unit_range(self):
        assert 0.0 < ADAPTER_DEFAULT_CONFIDENCE <= 1.0


# ---------------------------------------------------------------------------
# Authority allowlist
# ---------------------------------------------------------------------------


class TestAuthorityAllowlist:
    def _source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "verification" / "sbt_branch_prober_adapter.py"
        )
        return path.read_text()

    def test_imports_in_allowlist(self):
        allowed = {
            "backend.core.ouroboros.governance.verification.confidence_probe_bridge",
            "backend.core.ouroboros.governance.verification.readonly_evidence_prober",
            "backend.core.ouroboros.governance.verification.speculative_branch",
        }
        tree = ast.parse(self._source())
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or (
                    "governance" in module and module
                ):
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    if module not in allowed:
                        raise AssertionError(
                            f"adapter imported module outside "
                            f"allowlist: {module!r} at line {lineno}"
                        )

    def test_no_orchestrator_tier_imports(self):
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for ban in banned_substrings:
                    if ban in module:
                        raise AssertionError(
                            f"adapter imported BANNED orchestrator-tier "
                            f"substring {ban!r} via {module!r}"
                        )

    def test_no_exec_eval_compile_calls(self):
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"adapter must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )
