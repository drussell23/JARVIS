"""Move 5 Slice 1 — Confidence Probe Bridge primitive regression spine.

Coverage tracks:

  * Env knobs — defaults, floors, ceilings, garbage tolerance
  * Closed taxonomy — ProbeOutcome 5-value pin
  * Frozen-dataclass shape + serialization round-trip
  * canonical_fingerprint normalization (whitespace + case)
  * compute_convergence full decision tree (every input → exactly
    one outcome) — converged / diverged / exhausted / failed paths
  * make_probe_answer convenience constructor
  * probe_answer_from_dict round-trip + schema-mismatch tolerance
  * Defensive contract — never raises
  * Authority invariants — AST-pinned (stdlib + Phase 7.6 +
    confidence_monitor ONLY)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.verification import (
    confidence_probe_bridge as bridge,
)
from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION,
    ConvergenceVerdict,
    ProbeAnswer,
    ProbeOutcome,
    ProbeQuestion,
    bridge_enabled,
    canonical_fingerprint,
    compute_convergence,
    convergence_quorum,
    make_probe_answer,
    max_questions,
    max_tool_rounds_per_question,
    probe_answer_from_dict,
)


# ---------------------------------------------------------------------------
# 1. Env knobs — defaults + floors + ceilings + garbage tolerance
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_bridge_enabled_default_true_post_graduation(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", raising=False,
        )
        # Slice 5 graduation flipped this default.
        assert bridge_enabled() is True

    @pytest.mark.parametrize(
        "value,expected",
        [
            # Post-graduation: empty = unset = default true
            ("", True),
            ("0", False), ("false", False), ("no", False),
            ("garbage", False),
            ("1", True), ("true", True), ("YES", True),
            ("on", True),
        ],
    )
    def test_bridge_enabled_env_matrix(
        self, monkeypatch, value, expected,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", value,
        )
        assert bridge_enabled() is expected

    def test_max_questions_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS", raising=False,
        )
        assert max_questions() == 3

    def test_max_questions_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS", "1",
        )
        assert max_questions() == 2  # floor

    def test_max_questions_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS", "100",
        )
        assert max_questions() == 5  # ceiling

    def test_max_questions_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS", "garbage",
        )
        assert max_questions() == 3

    def test_convergence_quorum_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM",
            raising=False,
        )
        assert convergence_quorum() == 2

    def test_convergence_quorum_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM", "1",
        )
        assert convergence_quorum() == 2  # floor (single agreement
                                          # is meaningless)

    def test_max_tool_rounds_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS", raising=False,
        )
        assert max_tool_rounds_per_question() == 5

    def test_max_tool_rounds_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS", "0",
        )
        assert max_tool_rounds_per_question() == 1  # floor

    def test_max_tool_rounds_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS", "100",
        )
        assert max_tool_rounds_per_question() == 10  # ceiling


# ---------------------------------------------------------------------------
# 2. Closed taxonomy — ProbeOutcome 5-value pin
# ---------------------------------------------------------------------------


class TestProbeOutcomeTaxonomy:
    def test_taxonomy_pinned(self):
        # Closed 5-value taxonomy. Adding a value requires explicit
        # work; this catches silent additions.
        expected = {
            "converged",
            "diverged",
            "exhausted",
            "disabled",
            "failed",
        }
        assert {o.value for o in ProbeOutcome} == expected

    def test_schema_version_pinned(self):
        assert CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION == \
            "confidence_probe_bridge.1"


# ---------------------------------------------------------------------------
# 3. Frozen dataclass shape + serialization
# ---------------------------------------------------------------------------


class TestDataclassShape:
    def test_probe_question_is_frozen(self):
        q = ProbeQuestion(question="x", resolution_method="read_file")
        with pytest.raises((AttributeError, Exception)):
            q.question = "y"  # type: ignore[misc]

    def test_probe_answer_is_frozen(self):
        a = ProbeAnswer(
            question="q",
            answer_text="x",
            evidence_fingerprint="abc",
        )
        with pytest.raises((AttributeError, Exception)):
            a.answer_text = "y"  # type: ignore[misc]

    def test_convergence_verdict_is_frozen(self):
        v = ConvergenceVerdict(
            outcome=ProbeOutcome.CONVERGED,
            agreement_count=2,
            distinct_count=1,
            total_answers=2,
            canonical_answer="x",
            canonical_fingerprint="abc",
            detail="x",
        )
        with pytest.raises((AttributeError, Exception)):
            v.detail = "y"  # type: ignore[misc]

    def test_probe_question_to_dict(self):
        q = ProbeQuestion(
            question="what is foo?",
            resolution_method="read_file",
            max_tool_rounds=3,
        )
        d = q.to_dict()
        assert d["question"] == "what is foo?"
        assert d["resolution_method"] == "read_file"
        assert d["max_tool_rounds"] == 3

    def test_probe_answer_to_dict(self):
        a = ProbeAnswer(
            question="q",
            answer_text="function",
            evidence_fingerprint="abc",
            tool_rounds_used=2,
        )
        d = a.to_dict()
        assert d["answer_text"] == "function"
        assert d["evidence_fingerprint"] == "abc"
        assert d["tool_rounds_used"] == 2
        assert d["schema_version"] == \
            CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION

    def test_convergence_verdict_to_dict(self):
        v = ConvergenceVerdict(
            outcome=ProbeOutcome.CONVERGED,
            agreement_count=2,
            distinct_count=1,
            total_answers=2,
            canonical_answer="function",
            canonical_fingerprint="abc",
            detail="x",
        )
        d = v.to_dict()
        assert d["outcome"] == "converged"
        assert d["agreement_count"] == 2
        assert d["canonical_answer"] == "function"

    def test_convergence_verdict_is_actionable(self):
        for actionable_outcome in (
            ProbeOutcome.CONVERGED,
            ProbeOutcome.DIVERGED,
        ):
            v = ConvergenceVerdict(
                outcome=actionable_outcome,
                agreement_count=0,
                distinct_count=0,
                total_answers=0,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail="x",
            )
            assert v.is_actionable() is True

        for non_actionable in (
            ProbeOutcome.EXHAUSTED,
            ProbeOutcome.DISABLED,
            ProbeOutcome.FAILED,
        ):
            v = ConvergenceVerdict(
                outcome=non_actionable,
                agreement_count=0,
                distinct_count=0,
                total_answers=0,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail="x",
            )
            assert v.is_actionable() is False


# ---------------------------------------------------------------------------
# 4. canonical_fingerprint — whitespace + case + edge cases
# ---------------------------------------------------------------------------


class TestCanonicalFingerprint:
    def test_identical_strings_same_fingerprint(self):
        a = canonical_fingerprint("function")
        b = canonical_fingerprint("function")
        assert a == b
        assert len(a) == 64  # sha256 hex

    def test_case_normalized(self):
        a = canonical_fingerprint("Function")
        b = canonical_fingerprint("FUNCTION")
        c = canonical_fingerprint("function")
        assert a == b == c

    def test_whitespace_normalized(self):
        a = canonical_fingerprint("  function  ")
        b = canonical_fingerprint("function")
        c = canonical_fingerprint("\nfunction\t")
        d = canonical_fingerprint("function\t\nfunction")
        assert a == b == c
        assert a != d  # genuinely different content

    def test_collapsed_internal_whitespace(self):
        a = canonical_fingerprint("foo  bar")
        b = canonical_fingerprint("foo bar")
        c = canonical_fingerprint("foo\tbar")
        assert a == b == c

    def test_distinct_strings_distinct_fingerprints(self):
        a = canonical_fingerprint("function")
        b = canonical_fingerprint("class")
        c = canonical_fingerprint("method")
        assert a != b != c

    def test_empty_returns_empty(self):
        assert canonical_fingerprint("") == ""
        assert canonical_fingerprint("   ") == ""
        assert canonical_fingerprint("\n\t") == ""

    def test_non_string_coerced_defensively(self):
        # Int → str coercion
        result = canonical_fingerprint(12345)  # type: ignore[arg-type]
        assert len(result) == 64

    def test_never_raises_on_garbage(self):
        # Cannot raise; defensive sentinel
        for garbage in (None, [], {}, object()):
            result = canonical_fingerprint(garbage)  # type: ignore[arg-type]
            # Either valid hex or empty — never raises
            assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 5. make_probe_answer — convenience constructor
# ---------------------------------------------------------------------------


class TestMakeProbeAnswer:
    def test_builds_with_fingerprint(self):
        a = make_probe_answer("q", "function", tool_rounds_used=3)
        assert a.question == "q"
        assert a.answer_text == "function"
        assert len(a.evidence_fingerprint) == 64
        assert a.tool_rounds_used == 3

    def test_normalizes_for_fingerprint(self):
        a = make_probe_answer("q", "Function")
        b = make_probe_answer("q", "function")
        assert a.evidence_fingerprint == b.evidence_fingerprint

    def test_handles_none_question(self):
        a = make_probe_answer(None, "x")  # type: ignore[arg-type]
        assert a.question == ""

    def test_handles_none_answer(self):
        a = make_probe_answer("q", None)  # type: ignore[arg-type]
        # None coerces; fingerprint computed defensively
        assert isinstance(a.answer_text, str)


# ---------------------------------------------------------------------------
# 6. compute_convergence — full decision tree
# ---------------------------------------------------------------------------


class TestComputeConvergence:
    def test_empty_returns_exhausted(self):
        v = compute_convergence([], quorum=2, max_probes=3)
        assert v.outcome is ProbeOutcome.EXHAUSTED
        assert v.total_answers == 0
        assert "no answers" in v.detail

    def test_all_agree_converged(self):
        answers = [
            make_probe_answer("q", "function"),
            make_probe_answer("q", "function"),
            make_probe_answer("q", "function"),
        ]
        v = compute_convergence(answers, quorum=2, max_probes=3)
        assert v.outcome is ProbeOutcome.CONVERGED
        assert v.agreement_count == 3
        assert v.canonical_answer == "function"

    def test_two_agree_one_disagree_converged(self):
        # cluster=2 hits quorum=2
        answers = [
            make_probe_answer("q", "function"),
            make_probe_answer("q", "function"),
            make_probe_answer("q", "class"),
        ]
        v = compute_convergence(answers, quorum=2, max_probes=3)
        assert v.outcome is ProbeOutcome.CONVERGED
        assert v.agreement_count == 2
        assert v.canonical_answer == "function"

    def test_all_distinct_diverged(self):
        answers = [
            make_probe_answer("q", "function"),
            make_probe_answer("q", "class"),
            make_probe_answer("q", "method"),
        ]
        v = compute_convergence(answers, quorum=2, max_probes=3)
        assert v.outcome is ProbeOutcome.DIVERGED
        assert v.distinct_count == 3
        assert v.canonical_answer is None

    def test_single_answer_exhausted_keep_probing(self):
        # 1 answer < max_probes; caller should keep probing
        answers = [make_probe_answer("q", "function")]
        v = compute_convergence(answers, quorum=2, max_probes=3)
        assert v.outcome is ProbeOutcome.EXHAUSTED
        assert v.total_answers == 1
        assert "keep probing" in v.detail

    def test_two_distinct_within_budget_exhausted(self):
        answers = [
            make_probe_answer("q", "function"),
            make_probe_answer("q", "class"),
        ]
        v = compute_convergence(answers, quorum=2, max_probes=3)
        # 2 < max_probes; caller should keep probing
        assert v.outcome is ProbeOutcome.EXHAUSTED
        assert "keep probing" in v.detail

    def test_quorum_3_requires_all_agree_at_K3(self):
        # quorum=3 with 3 probes: need ALL three to agree
        answers = [
            make_probe_answer("q", "function"),
            make_probe_answer("q", "function"),
            make_probe_answer("q", "class"),
        ]
        v = compute_convergence(answers, quorum=3, max_probes=3)
        # Cluster=2 < quorum=3, budget exhausted, partial agreement
        assert v.outcome is ProbeOutcome.EXHAUSTED
        assert "partial" in v.detail.lower()

    def test_canonical_answer_from_largest_cluster(self):
        answers = [
            make_probe_answer("q", "method"),
            make_probe_answer("q", "function"),
            make_probe_answer("q", "function"),
        ]
        v = compute_convergence(answers, quorum=2, max_probes=3)
        assert v.outcome is ProbeOutcome.CONVERGED
        # Canonical = function (cluster of 2), not method (cluster
        # of 1).
        assert v.canonical_answer == "function"

    def test_exhausted_partial_agreement_at_budget(self):
        # 4 probes: {A, A, B, B} — split 2-2, no winner
        answers = [
            make_probe_answer("q", "A"),
            make_probe_answer("q", "A"),
            make_probe_answer("q", "B"),
            make_probe_answer("q", "B"),
        ]
        v = compute_convergence(answers, quorum=3, max_probes=4)
        # Largest cluster=2 < quorum=3, but distinct=2 < total=4 →
        # not full divergence either → EXHAUSTED
        assert v.outcome is ProbeOutcome.EXHAUSTED

    def test_empty_fingerprint_answers_yield_no_signal(self):
        # All answers with empty fingerprint (e.g., all blank
        # answer_text) → DIVERGED at budget
        answers = [
            make_probe_answer("q", ""),
            make_probe_answer("q", ""),
            make_probe_answer("q", ""),
        ]
        v = compute_convergence(answers, quorum=2, max_probes=3)
        # Budget hit, no usable signal
        assert v.outcome is ProbeOutcome.DIVERGED

    def test_filters_non_probe_answer_inputs(self):
        # Only ProbeAnswer instances counted; bare dicts ignored
        answers = [
            make_probe_answer("q", "function"),
            {"not": "a probe answer"},  # type: ignore[list-item]
            "neither is this",  # type: ignore[list-item]
            make_probe_answer("q", "function"),
        ]
        v = compute_convergence(answers, quorum=2, max_probes=2)
        assert v.outcome is ProbeOutcome.CONVERGED
        assert v.total_answers == 2

    def test_default_quorum_and_max_probes_used_when_none(
        self, monkeypatch,
    ):
        # When None passed, env defaults apply
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM", "2",
        )
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS", "3",
        )
        answers = [
            make_probe_answer("q", "function"),
            make_probe_answer("q", "function"),
        ]
        v = compute_convergence(answers)  # no quorum/max_probes
        assert v.outcome is ProbeOutcome.CONVERGED

    def test_never_raises_on_garbage_iterable(self):
        # Pass a non-iterable — must not raise
        v = compute_convergence(None)  # type: ignore[arg-type]
        # Whatever the outcome, it's a valid ConvergenceVerdict
        assert isinstance(v, ConvergenceVerdict)


# ---------------------------------------------------------------------------
# 7. probe_answer_from_dict — round-trip + schema mismatch
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_preserves_equality(self):
        a = make_probe_answer("q", "function", tool_rounds_used=3)
        d = a.to_dict()
        b = probe_answer_from_dict(d)
        assert b == a

    def test_schema_mismatch_returns_none(self):
        a = make_probe_answer("q", "x")
        d = a.to_dict()
        d["schema_version"] = "wrong.0"
        assert probe_answer_from_dict(d) is None

    def test_missing_required_field_returns_none(self):
        a = make_probe_answer("q", "x")
        d = a.to_dict()
        del d["question"]
        assert probe_answer_from_dict(d) is None

    def test_malformed_payload_returns_none(self):
        # Type coercion failures
        bad = {
            "question": "q",
            "answer_text": "x",
            "evidence_fingerprint": "abc",
            "tool_rounds_used": "not_an_int",
            "schema_version": CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION,
        }
        assert probe_answer_from_dict(bad) is None


# ---------------------------------------------------------------------------
# 8. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


_FORBIDDEN_AUTHORITY_SUBSTRINGS = (
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_guardian",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
    "tool_executor",
)


_FORBIDDEN_MUTATION_TOOL_NAMES = (
    "edit_file",
    "write_file",
    "delete_file",
    "run_tests",
    "bash",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "verification"
                / "confidence_probe_bridge.py"
            )
        cur = cur.parent
    raise RuntimeError("repo root not found")


class TestAuthorityInvariants:
    def test_no_forbidden_authority_imports(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                        if fb in alias.name:
                            offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                    if fb in mod:
                        offenders.append(mod)
        assert offenders == [], (
            f"confidence_probe_bridge imports forbidden modules: "
            f"{offenders}"
        )

    def test_no_mutation_tool_names_in_source(self):
        # Slice 1 is pure-data; the bridge must not name any
        # mutation tool. Slice 2 ships the prober and its
        # allowlist; that allowlist will be AST-pinned in Slice 5.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        # Allow these in comments/docstrings (they appear in
        # the docstring as exclusions). Just check ast for
        # actual code references.
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            # Function calls, attribute access — string Constants
            # in docstrings are fine.
            if isinstance(node, ast.Name):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.id == fb:
                        offenders.append(node.id)
            elif isinstance(node, ast.Attribute):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.attr == fb:
                        offenders.append(node.attr)
        assert offenders == [], (
            f"bridge module references mutation-tool names in "
            f"code (not docstring): {offenders}"
        )

    def test_governance_imports_in_allowlist(self):
        # Allowed governance modules: hypothesis_probe (Phase 7.6
        # primitive — not currently imported but reserved) +
        # confidence_monitor (Verdict enum — not currently imported
        # but reserved for Slice 4).
        # Slice 1 is pure data — actually imports NEITHER.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        allowed = (
            "hypothesis_probe",  # Phase 7.6
            "confidence_monitor",  # Slice 4 wire-up
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                ok = any(sub in mod for sub in allowed)
                assert ok, (
                    f"unexpected governance module: {mod}"
                )

    def test_module_does_not_perform_disk_writes(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        forbidden_tokens = (
            ".write_text(",
            ".write_bytes(",
            "os.replace(",
            "NamedTemporaryFile",
        )
        for tok in forbidden_tokens:
            assert tok not in source, (
                f"forbidden disk-write token: {tok!r}"
            )

    def test_module_has_no_async_functions(self):
        # Slice 1 is pure-sync data + compute. Async runner ships
        # in Slice 3.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        async_defs = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        ]
        assert async_defs == [], (
            f"Slice 1 must be pure-sync; async found: {async_defs}"
        )

    def test_public_api_exported(self):
        expected = {
            "CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION",
            "ConvergenceVerdict",
            "ProbeAnswer",
            "ProbeOutcome",
            "ProbeQuestion",
            "bridge_enabled",
            "canonical_fingerprint",
            "compute_convergence",
            "convergence_quorum",
            "make_probe_answer",
            "max_questions",
            "max_tool_rounds_per_question",
            "probe_answer_from_dict",
        }
        assert set(bridge.__all__) == expected

    def test_module_is_pure_stdlib_plus_governance(self):
        # Imports in Slice 1: stdlib only (no governance imports
        # yet — they'll be added in Slices 3-4 as needed).
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        # Walk imports; ensure the imports list is reasonable.
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        # Stdlib-only patterns
        for name in imports:
            if name.startswith("backend."):
                # Allowed governance per allowlist test above
                continue
            # Stdlib check — any name starting with "backend." would
            # have been caught above. Everything else should be
            # stdlib.
            top_level = name.split(".")[0]
            stdlib_top = {
                "enum", "hashlib", "logging", "os", "re",
                "collections", "dataclasses", "typing", "__future__",
            }
            assert top_level in stdlib_top or top_level == "backend", (
                f"unexpected import: {name}"
            )
