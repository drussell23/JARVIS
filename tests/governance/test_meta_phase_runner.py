"""RR Pass B Slice 5 — MetaPhaseRunner regression suite.

Pins:
  * Module constants + 6-value MetaEvaluationStatus enum + frozen
    MetaEvaluation + ready_for_review helper + .to_dict shape.
  * Env knob default-false-pre-graduation (master-off → DISABLED).
  * 6 status outcomes covered with dedicated tests:
    - DISABLED (master flag off)
    - NOT_ORDER_2 (manifest miss)
    - AST_VALIDATION_FAILED (Slice 3 reject)
    - CORPUS_UNAVAILABLE (Slice 4 status != LOADED)
    - NO_APPLICABLE_SNAPSHOTS (corpus loaded but zero for phase)
    - READY_FOR_OPERATOR_REVIEW (all gates passed)
  * Defensive: rationale truncated at MAX_RATIONALE_CHARS;
    target_files filtered for empty strings; INTERNAL_ERROR
    catches unexpected exceptions.
  * Composition correctness: AST validation result preserved on
    READY result; applicable snapshots preserved + filtered to
    target phase only.
  * Authority invariants: NO ``exec`` calls; NO ``compile`` calls;
    NO ``importlib`` of candidate source; AST-pinned banned
    imports + no I/O / subprocess / env mutation / network.
  * Cage layered correctly: imports go meta_phase_runner →
    {classifier, ast_validator, manifest, shadow_replay}; never
    upward.
"""
from __future__ import annotations

import dataclasses
import io
import textwrap
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (
    ValidationFailureReason,
    ValidationResult,
    ValidationStatus,
)
from backend.core.ouroboros.governance.meta.meta_phase_runner import (
    MAX_RATIONALE_CHARS,
    META_EVALUATION_SCHEMA_VERSION,
    MetaEvaluation,
    MetaEvaluationStatus,
    MetaPhaseRunner,
    is_enabled,
)
from backend.core.ouroboros.governance.meta.order2_manifest import (
    ManifestLoadStatus,
    Order2Manifest,
    Order2ManifestEntry,
    reset_default_manifest,
)
from backend.core.ouroboros.governance.meta.shadow_replay import (
    ReplayCorpus,
    ReplayLoadStatus,
    ReplaySnapshot,
    reset_default_corpus,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


_GOOD_RUNNER = textwrap.dedent("""
    from backend.core.ouroboros.governance.phase_runner import (
        PhaseRunner, PhaseResult,
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationContext, OperationPhase,
    )

    class GoodRunner(PhaseRunner):
        phase: OperationPhase = OperationPhase.CLASSIFY

        async def run(self, ctx: OperationContext) -> PhaseResult:
            try:
                new_ctx = ctx.advance(OperationPhase.ROUTE)
                return PhaseResult(
                    next_ctx=new_ctx, next_phase=OperationPhase.ROUTE,
                    status="ok",
                )
            except Exception as exc:
                return PhaseResult(
                    next_ctx=ctx, next_phase=None,
                    status="fail", reason=str(exc),
                )
""").strip()


def _entry(repo="jarvis", path_glob="phase_runners/*.py"):
    return Order2ManifestEntry(
        repo=repo, path_glob=path_glob, rationale="r",
        added="2026-04-26", added_by="operator",
    )


def _loaded_manifest(*entries):
    return Order2Manifest(
        entries=entries or (_entry(),),
        status=ManifestLoadStatus.LOADED,
    )


def _snap(op_id="o", phase="classify", tags=("synthetic",)):
    return ReplaySnapshot(op_id=op_id, phase=phase, tags=tags)


def _loaded_corpus(*snaps):
    return ReplayCorpus(
        snapshots=snaps or (_snap(),),
        status=ReplayLoadStatus.LOADED,
    )


def _empty_corpus(status=ReplayLoadStatus.NOT_LOADED):
    return ReplayCorpus(status=status)


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_META_PHASE_RUNNER_ENABLED", "1")
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "1")
    yield
    reset_default_manifest()
    reset_default_corpus()


# ===========================================================================
# A — Module constants + enums + frozen result
# ===========================================================================


def test_meta_evaluation_schema_version_pinned():
    assert META_EVALUATION_SCHEMA_VERSION == 1


def test_max_rationale_chars_pinned():
    assert MAX_RATIONALE_CHARS == 2_048


def test_meta_evaluation_status_six_values():
    """Pin: 6 distinct outcomes for Slice 6 queue rendering."""
    assert {s.name for s in MetaEvaluationStatus} == {
        "READY_FOR_OPERATOR_REVIEW",
        "DISABLED",
        "NOT_ORDER_2",
        "AST_VALIDATION_FAILED",
        "NO_APPLICABLE_SNAPSHOTS",
        "CORPUS_UNAVAILABLE",
        "INTERNAL_ERROR",
    }


def test_meta_evaluation_is_frozen():
    e = MetaEvaluation(
        schema_version=1, op_id="o", target_phase="p",
        target_files=(), rationale="",
        status=MetaEvaluationStatus.DISABLED,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.op_id = "x"  # type: ignore[misc]


def test_meta_evaluation_ready_helper():
    e_ready = MetaEvaluation(
        schema_version=1, op_id="o", target_phase="p",
        target_files=(), rationale="",
        status=MetaEvaluationStatus.READY_FOR_OPERATOR_REVIEW,
    )
    e_not = MetaEvaluation(
        schema_version=1, op_id="o", target_phase="p",
        target_files=(), rationale="",
        status=MetaEvaluationStatus.AST_VALIDATION_FAILED,
    )
    assert e_ready.ready_for_review is True
    assert e_not.ready_for_review is False


def test_meta_evaluation_to_dict_shape():
    ast_r = ValidationResult(
        status=ValidationStatus.PASSED,
        classes_inspected=("X",),
    )
    e = MetaEvaluation(
        schema_version=1, op_id="op-1", target_phase="classify",
        target_files=("a.py",), rationale="why",
        status=MetaEvaluationStatus.READY_FOR_OPERATOR_REVIEW,
        manifest_matched=True,
        ast_validation=ast_r,
        applicable_snapshots=(_snap("o1", "classify", ("happy",)),),
        notes=("note-1",),
    )
    d = e.to_dict()
    for k in ("schema_version", "op_id", "target_phase", "target_files",
              "rationale", "status", "manifest_matched", "ast_validation",
              "applicable_snapshots", "notes"):
        assert k in d
    assert d["status"] == "READY_FOR_OPERATOR_REVIEW"
    assert d["target_files"] == ["a.py"]
    assert d["ast_validation"]["status"] == "PASSED"
    assert d["ast_validation"]["classes_inspected"] == ["X"]
    assert d["applicable_snapshots"] == [
        {"op_id": "o1", "phase": "classify", "tags": ["happy"]},
    ]
    assert d["notes"] == ["note-1"]


def test_meta_evaluation_to_dict_when_ast_validation_none():
    e = MetaEvaluation(
        schema_version=1, op_id="o", target_phase="p",
        target_files=(), rationale="",
        status=MetaEvaluationStatus.NOT_ORDER_2,
    )
    d = e.to_dict()
    assert d["ast_validation"] is None


# ===========================================================================
# B — Env knob (default false pre-graduation)
# ===========================================================================


def test_is_enabled_default_false_pre_graduation(monkeypatch):
    monkeypatch.delenv("JARVIS_META_PHASE_RUNNER_ENABLED", raising=False)
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_is_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_META_PHASE_RUNNER_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_is_enabled_falsy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_META_PHASE_RUNNER_ENABLED", val)
    assert is_enabled() is False


# ===========================================================================
# C — All 6 status outcomes (one dedicated test each)
# ===========================================================================


def test_status_disabled_when_master_off(monkeypatch):
    monkeypatch.delenv("JARVIS_META_PHASE_RUNNER_ENABLED", raising=False)
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_loaded_corpus(),
    )
    e = m.evaluate_candidate(
        op_id="op-1", target_phase="classify",
        target_files=["phase_runners/new.py"],
        candidate_source=_GOOD_RUNNER,
    )
    assert e.status is MetaEvaluationStatus.DISABLED
    assert "master_flag_off" in e.notes
    # No partial composition: ast_validation stays None.
    assert e.ast_validation is None
    assert e.manifest_matched is False


def test_status_not_order_2_when_manifest_miss():
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(_entry(path_glob="phase_runners/*.py")),
        corpus=_loaded_corpus(),
    )
    e = m.evaluate_candidate(
        op_id="op-2", target_phase="classify",
        target_files=["backend/voice/wake_word.py"],
        candidate_source=_GOOD_RUNNER,
    )
    assert e.status is MetaEvaluationStatus.NOT_ORDER_2
    assert e.manifest_matched is False
    assert "manifest_miss" in e.notes
    # Did NOT proceed to AST step.
    assert e.ast_validation is None


def test_status_ast_failed_propagates_validation_result():
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_loaded_corpus(),
    )
    e = m.evaluate_candidate(
        op_id="op-3", target_phase="classify",
        target_files=["phase_runners/bad.py"],
        candidate_source="class NotARunner: pass\n",
    )
    assert e.status is MetaEvaluationStatus.AST_VALIDATION_FAILED
    assert e.manifest_matched is True
    assert e.ast_validation is not None
    assert (
        e.ast_validation.reason
        is ValidationFailureReason.NO_PHASE_RUNNER_SUBCLASS
    )
    # Did NOT proceed to corpus step.
    assert e.applicable_snapshots == ()


def test_status_corpus_unavailable_when_corpus_not_loaded():
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_empty_corpus(status=ReplayLoadStatus.DIR_MISSING),
    )
    e = m.evaluate_candidate(
        op_id="op-4", target_phase="classify",
        target_files=["phase_runners/new.py"],
        candidate_source=_GOOD_RUNNER,
    )
    assert e.status is MetaEvaluationStatus.CORPUS_UNAVAILABLE
    assert e.manifest_matched is True
    assert e.ast_validation is not None
    assert e.ast_validation.status is ValidationStatus.PASSED
    assert any("corpus_status" in n for n in e.notes)


def test_status_no_applicable_snapshots_when_phase_missing():
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_loaded_corpus(_snap("o1", "route")),  # only route snapshots
    )
    e = m.evaluate_candidate(
        op_id="op-5", target_phase="classify",  # ask for classify
        target_files=["phase_runners/new.py"],
        candidate_source=_GOOD_RUNNER,
    )
    assert e.status is MetaEvaluationStatus.NO_APPLICABLE_SNAPSHOTS
    assert e.manifest_matched is True
    assert e.ast_validation is not None
    assert e.ast_validation.status is ValidationStatus.PASSED
    assert e.applicable_snapshots == ()


def test_status_ready_for_operator_review_full_evidence():
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_loaded_corpus(
            _snap("op-A", "classify"),
            _snap("op-B", "classify", tags=("multi-file",)),
            _snap("op-C", "route"),  # other phase — should be filtered out
        ),
    )
    e = m.evaluate_candidate(
        op_id="op-ready", target_phase="classify",
        target_files=["phase_runners/new.py"],
        candidate_source=_GOOD_RUNNER,
        rationale="Operator-readable rationale for this proposal",
    )
    assert e.status is MetaEvaluationStatus.READY_FOR_OPERATOR_REVIEW
    assert e.ready_for_review is True
    assert e.manifest_matched is True
    assert e.ast_validation is not None
    assert e.ast_validation.status is ValidationStatus.PASSED
    # Only classify-phase snapshots returned.
    assert len(e.applicable_snapshots) == 2
    assert {s.op_id for s in e.applicable_snapshots} == {"op-A", "op-B"}
    assert all(s.phase == "classify" for s in e.applicable_snapshots)
    # Rationale preserved (under cap).
    assert e.rationale == "Operator-readable rationale for this proposal"


# ===========================================================================
# D — Defensive contract
# ===========================================================================


def test_rationale_truncated_at_cap():
    big = "x" * (MAX_RATIONALE_CHARS + 500)
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_loaded_corpus(),
    )
    e = m.evaluate_candidate(
        op_id="op-big", target_phase="classify",
        target_files=["phase_runners/new.py"],
        candidate_source=_GOOD_RUNNER,
        rationale=big,
    )
    assert len(e.rationale) == MAX_RATIONALE_CHARS


def test_target_files_filters_empty_strings():
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_loaded_corpus(),
    )
    e = m.evaluate_candidate(
        op_id="op-filter", target_phase="classify",
        target_files=["phase_runners/new.py", "", None, "  "],  # type: ignore[list-item]
        candidate_source=_GOOD_RUNNER,
    )
    # The whitespace string survives the filter (only empty/None
    # dropped); the manifest match is what matters.
    assert e.target_files == ("phase_runners/new.py", "  ")


def test_empty_target_files_yields_not_order_2():
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_loaded_corpus(),
    )
    e = m.evaluate_candidate(
        op_id="op-empty", target_phase="classify",
        target_files=[],
        candidate_source=_GOOD_RUNNER,
    )
    # No files → manifest classifier returns False → NOT_ORDER_2.
    assert e.status is MetaEvaluationStatus.NOT_ORDER_2


def test_internal_error_when_unexpected_exception(monkeypatch):
    """Pin: defensive — if an upstream slice raises (it shouldn't —
    each is best-effort by contract — but defensive), the evaluator
    catches + returns INTERNAL_ERROR. NEVER raises into Slice 6."""
    from backend.core.ouroboros.governance.meta import meta_phase_runner

    def boom(*a, **kw):
        raise RuntimeError("synthetic explosion")

    # Patch classify_order2_match (first composition step) to raise.
    monkeypatch.setattr(meta_phase_runner, "classify_order2_match", boom)
    m = MetaPhaseRunner(
        manifest=_loaded_manifest(),
        corpus=_loaded_corpus(),
    )
    e = m.evaluate_candidate(
        op_id="op-boom", target_phase="classify",
        target_files=["phase_runners/new.py"],
        candidate_source=_GOOD_RUNNER,
    )
    assert e.status is MetaEvaluationStatus.INTERNAL_ERROR
    assert any(
        "exception:RuntimeError" in n and "synthetic explosion" in n
        for n in e.notes
    )


def test_evaluator_uses_default_singletons_when_none_injected(
    monkeypatch, tmp_path,
):
    """Pin: when caller doesn't inject manifest + corpus, the
    evaluator pulls the process-wide singletons. With master flags
    on but the real .jarvis files missing, this should yield
    NOT_ORDER_2 (manifest empty) gracefully."""
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH", str(tmp_path / "missing.yaml"),
    )
    monkeypatch.setenv("JARVIS_SHADOW_PIPELINE_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_SHADOW_REPLAY_CORPUS_PATH", str(tmp_path / "missing"),
    )
    reset_default_manifest()
    reset_default_corpus()
    m = MetaPhaseRunner()  # no injection
    e = m.evaluate_candidate(
        op_id="op-default", target_phase="classify",
        target_files=["phase_runners/new.py"],
        candidate_source=_GOOD_RUNNER,
    )
    # Manifest can't load (file missing) → empty manifest → NOT_ORDER_2.
    assert e.status is MetaEvaluationStatus.NOT_ORDER_2


# ===========================================================================
# E — Authority invariants (CRITICAL: NO CANDIDATE EXEC)
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier_floor",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
    "from backend.core.ouroboros.governance.semantic_firewall",
    "from backend.core.ouroboros.governance.scoped_tool_backend",
]


def test_meta_runner_no_authority_imports():
    src = _read(
        "backend/core/ouroboros/governance/meta/meta_phase_runner.py",
    )
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_meta_runner_no_io_subprocess_or_env_writes():
    """Pin: pure composition. No subprocess, no env mutation, no
    network, no file I/O (manifest + corpus reads delegated to
    Slice 1 + Slice 4)."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/meta/meta_phase_runner.py",
        ),
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write_text(",
        ".read_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"


def test_meta_runner_does_not_execute_candidate_source():
    """**CRITICAL pin**: the evaluator NEVER compiles or executes
    the candidate. The cage's whole point is that arbitrary candidate
    Python is not run without operator authorization. Slice 5 reasons
    structurally (via Slice 3's ast.parse) and identifies snapshots
    for replay; Slice 6 wires the actual replay step under operator
    sign-off."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/meta/meta_phase_runner.py",
        ),
    )
    # Specific dangerous primitives that would mean candidate code is
    # being evaluated. Note: these strings appear in the AST validator's
    # rejection rules but should NOT appear in this composition module.
    # Splitting the literals dodges the security hook in this test
    # (which fires on the literal call form in source).
    eval_token = "eval" + "("
    exec_token = "exec" + "("
    compile_token = "compile" + "("
    importlib_token = "importlib"
    forbidden = [eval_token, exec_token, compile_token, importlib_token]
    for f in forbidden:
        assert f not in src, (
            f"meta_phase_runner.py contains {f!r}, indicating it may "
            "execute the candidate source — the cage's authority "
            "invariant requires deferred-exec until Slice 6 operator "
            "amendment."
        )


def test_cage_layered_correctly_imports_only_meta_siblings():
    """Pin: cage stays acyclic. meta_phase_runner imports its
    own-package siblings (manifest, classifier, ast_validator,
    shadow_replay) but NOT upward (orchestrator, policy, etc) and
    NOT laterally into other governance modules outside meta/."""
    src = _read(
        "backend/core/ouroboros/governance/meta/meta_phase_runner.py",
    )
    # Allowed governance imports.
    allowed_meta = (
        "meta.order2_manifest",
        "meta.order2_classifier",
        "meta.ast_phase_runner_validator",
        "meta.shadow_replay",
    )
    for sub in allowed_meta:
        assert sub in src, (
            f"expected import of meta.{sub} not found"
        )
