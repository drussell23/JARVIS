"""SWE-bench cognition-feed fix — spine.

Closes the capability-layer defect diagnosed from soak
``bt-2026-05-17-194855`` where ``psf__requests-3362`` terminated as a
CLASSIFY/GENERATE NO-OP. Root cause was an architecturally incoherent
task hand-off, NOT model cognition:

  Fix (1a) — the SWE-Bench envelope surfaced ``prepared.target_paths``
  (the *test_patch* paths) as ``target_files``. The agent is forbidden
  to edit tests (Phase C scorer rejects test edits as cheating) and
  surfacing gold_patch paths would leak the solution. The authentic
  SWE-bench task is *localize the bug from the issue text*. So a
  SWE-bench envelope now carries NO target_files, honoured by
  ``intent_envelope._EMPTY_TARGET_FILES_EXEMPT_SOURCES`` (same
  epistemic class as ``vision_sensor``).

  Fix (2/3) — the file-count complexity heuristic classified the op
  ``simple`` (1 file) → STANDARD route → PLAN skipped
  (``insufficient_budget``). With empty target_files it would be
  *worse* (0 files → still simple). ``complexity_classifier``
  ``_COMPLEX_FLOOR_SOURCES`` now floors benchmark sources to COMPLEX,
  routing them via the urgency_router's always-on Priority-4 matrix
  (no feature flag) → Claude-plans + full PLAN budget.

Both fixes COMPOSE existing closed-set patterns (no new code paths,
no feature flags, no hardcoded routing tables). The AST pins are the
load-bearing structural defense; the behavioural tests prove the
end-to-end task hand-off is now coherent.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.complexity_classifier import (
    ComplexityClass,
    OperationComplexityClassifier,
    _COMPLEX_FLOOR_SOURCES,
)
from backend.core.ouroboros.governance.intake.intent_envelope import (
    EnvelopeValidationError,
    _EMPTY_TARGET_FILES_EXEMPT_SOURCES,
    make_envelope,
)
from backend.core.ouroboros.governance.swe_bench_pro import envelope_builder
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
    build_evaluation_envelope,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    PreparedProblem,
)
from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
    UrgencyRouter,
)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/governance/test_swe_bench_pro_envelope_builder.py —
# reuse the established construction pattern, no duplication of intent)
# ---------------------------------------------------------------------------


@pytest.fixture
def problem() -> ProblemSpec:
    return ProblemSpec(
        instance_id="psf__requests-3362",
        repo="psf/requests",
        base_commit="36453b95b130",
        problem_statement=(
            "Uncertain about content/text vs "
            "iter_content(decode_unicode=True/False)."
        ),
        test_patch=(
            "--- a/tests/test_requests.py\n"
            "+++ b/tests/test_requests.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        ),
        gold_patch="--- a/requests/models.py\n+++ b/requests/models.py\n",
        repo_url="https://github.com/psf/requests",
    )


@pytest.fixture
def prepared(problem: ProblemSpec, tmp_path: Path) -> PreparedProblem:
    wt = tmp_path / "wt-psf"
    wt.mkdir()
    # NOTE target_paths is deliberately the test file — the whole point
    # of Fix (1a) is that build_evaluation_envelope IGNORES this.
    return PreparedProblem(
        problem_instance_id=problem.instance_id,
        worktree_path=wt,
        base_commit=problem.base_commit,
        repo_url=problem.repo_url,
        branch_name="swebp/psf__requests-3362",
        target_paths=("tests/test_requests.py",),
        elapsed_s=0.8,
    )


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    # Prove the COMPLEX route via the ALWAYS-ON Priority-4 matrix, not
    # the default-OFF F2 envelope_routing_override flag.
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    monkeypatch.delenv(
        "JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED", raising=False
    )
    monkeypatch.delenv(
        envelope_builder.ENVELOPE_URGENCY_ENV_VAR, raising=False
    )
    yield


# ---------------------------------------------------------------------------
# AST pins — structural regression defense
# ---------------------------------------------------------------------------


def _fn_source(module, name: str) -> str:
    src = inspect.getsource(module)
    tree = ast.parse(src)
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    seg = ast.get_source_segment(src, node)
    assert seg is not None
    return seg


def test_ast_pin_builder_never_surfaces_test_patch_targets():
    """build_evaluation_envelope MUST NOT thread prepared.target_paths
    into target_files — that is the exact inversion that caused the
    bt-2026-05-17-194855 no-op. target_files MUST be the empty tuple."""
    seg = _fn_source(envelope_builder, "build_evaluation_envelope")
    assert "tuple(prepared.target_paths)" not in seg, (
        "REGRESSION: builder re-surfaced the test_patch paths as the "
        "agent's target — re-opens the no-op vector"
    )
    assert "target_files: Tuple[str, ...] = ()" in seg, (
        "SWE-bench envelope MUST carry an empty target_files tuple "
        "(authentic localize-from-issue protocol)"
    )
    # bt session cited for postmortem discoverability.
    assert "bt-2026-05-17" in inspect.getsource(envelope_builder), (
        "builder MUST cite the diagnosing soak session"
    )


def test_ast_pin_intent_envelope_exempt_set_closed_and_correct():
    """The empty-target_files exemption MUST be a closed frozenset
    containing BOTH the pre-existing vision_sensor exemption AND the
    new swe_bench_pro one (no inline string drift)."""
    assert isinstance(_EMPTY_TARGET_FILES_EXEMPT_SOURCES, frozenset)
    assert "swe_bench_pro" in _EMPTY_TARGET_FILES_EXEMPT_SOURCES
    assert "vision_sensor" in _EMPTY_TARGET_FILES_EXEMPT_SOURCES


def test_ast_pin_complexity_floor_set_closed_and_correct():
    assert isinstance(_COMPLEX_FLOOR_SOURCES, frozenset)
    assert "swe_bench_pro" in _COMPLEX_FLOOR_SOURCES


def test_ast_pin_orchestrator_threads_source_into_complexity_classify():
    """The orchestrator's complexity classify() call MUST pass a
    ``source=`` kwarg — without it the floor can never engage and the
    fix is silently dead."""
    from backend.core.ouroboros.governance import orchestrator

    tree = ast.parse(inspect.getsource(orchestrator))
    classify_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "classify"
        and {kw.arg for kw in n.keywords} >= {"description", "target_files"}
    ]
    assert classify_calls, (
        "could not locate the OperationComplexityClassifier.classify "
        "call in orchestrator"
    )
    assert any(
        "source" in {kw.arg for kw in c.keywords} for c in classify_calls
    ), (
        "REGRESSION: orchestrator stopped threading source= into the "
        "complexity classifier — _COMPLEX_FLOOR_SOURCES is now dead"
    )


# ---------------------------------------------------------------------------
# Behavioural — Fix (1a): empty target_files, coherent hand-off
# ---------------------------------------------------------------------------


def test_swe_bench_envelope_has_empty_target_files_and_constructs(
    problem, prepared, clean_env,
):
    env = build_evaluation_envelope(problem, prepared)
    assert env.source == "swe_bench_pro"
    assert env.target_files == (), (
        "SWE-bench envelope MUST NOT carry target_files — the agent "
        "localizes from the issue (test_patch paths were "
        f"{prepared.target_paths!r}, correctly ignored)"
    )
    # evidence still carries the worktree + signature for advisor +
    # dedup (Fix 1a must not regress evidence composition).
    assert env.evidence["signature"] == problem.instance_id


def test_intent_envelope_exemption_boundary():
    """swe_bench_pro + empty target_files constructs; a non-exempt
    source with empty target_files (no user_attachments) still fails
    closed — the exemption is precise, not a blanket hole."""
    ok = make_envelope(
        source="swe_bench_pro",
        description="localize from issue",
        target_files=(),
        repo="psf/requests",
        confidence=1.0,
        urgency="low",
        evidence={"signature": "psf__requests-3362"},
        requires_human_ack=False,
    )
    assert ok.target_files == ()

    with pytest.raises(EnvelopeValidationError):
        make_envelope(
            source="backlog",  # valid source, NOT empty-target exempt
            description="no targets, no attachments",
            target_files=(),
            repo="r",
            confidence=1.0,
            urgency="low",
            evidence={},
            requires_human_ack=False,
        )


def test_dedup_key_stable_despite_empty_target_files(
    problem, prepared, clean_env,
):
    """Empty target_files MUST NOT break dedup — the evidence
    signature (== instance_id) carries it. Two builds for the same
    problem still collapse to one dedup_key."""
    a = build_evaluation_envelope(problem, prepared)
    b = build_evaluation_envelope(problem, prepared)
    assert a.dedup_key == b.dedup_key
    assert a.causal_id != b.causal_id  # distinct ops, shared dedup


# ---------------------------------------------------------------------------
# Behavioural — Fix (2/3): complexity floor + COMPLEX route, no flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("targets", [[], ["requests/models.py"]])
def test_complexity_floor_swe_bench_pro_to_complex(targets):
    c = OperationComplexityClassifier()
    res = c.classify(
        "fix iter_content unicode mismatch", targets,
        source="swe_bench_pro",
    )
    assert res.complexity is ComplexityClass.COMPLEX, (
        "swe_bench_pro MUST floor to COMPLEX regardless of file count "
        "(0 files would otherwise misclassify SIMPLE → no PLAN budget)"
    )


@pytest.mark.parametrize("targets", [[], ["foo.py"]])
def test_generic_source_byte_identical_pre_fix(targets):
    """Non-floored sources MUST be unchanged: <=1 file still SIMPLE.
    The fix is additive — zero behaviour change for organic ops."""
    c = OperationComplexityClassifier()
    res = c.classify("fix a bug", targets, source="test_failure")
    assert res.complexity is ComplexityClass.SIMPLE
    # default source="" also preserved
    assert c.classify("fix a bug", targets).complexity is (
        ComplexityClass.SIMPLE
    )


def test_floor_does_not_override_architectural():
    """Architectural keywords MUST still win (ARCHITECTURAL > COMPLEX)
    — the floor only prevents UNDER-classification."""
    c = OperationComplexityClassifier()
    res = c.classify(
        "introduce a new capability / system design change", [],
        source="swe_bench_pro",
    )
    assert res.complexity is ComplexityClass.ARCHITECTURAL


def _ctx(*, urgency: str, source: str, complexity: str) -> SimpleNamespace:
    return SimpleNamespace(
        signal_urgency=urgency,
        signal_source=source,
        task_complexity=complexity,
        target_files=[],
        cross_repo=False,
    )


def test_urgency_router_swe_bench_complex_routes_COMPLEX_no_flag(
    clean_env,
):
    """task_complexity='complex' + source='swe_bench_pro' + low
    urgency MUST route COMPLEX via the always-on Priority-4 matrix —
    NOT the default-OFF F2 envelope_routing_override flag (proven by
    clean_env clearing it)."""
    router = UrgencyRouter()
    route, reason = router.classify(
        _ctx(urgency="low", source="swe_bench_pro", complexity="complex")
    )
    assert route is ProviderRoute.COMPLEX, (
        f"expected COMPLEX (Claude-plans + full PLAN budget); "
        f"got {route} reason={reason!r}"
    )


def test_urgency_router_pre_fix_simple_would_not_be_complex(clean_env):
    """Regression baseline: the PRE-FIX classification (complexity=
    'simple') does NOT route COMPLEX — this is exactly the budget
    starvation that skipped PLAN and produced the no-op."""
    router = UrgencyRouter()
    route, _ = router.classify(
        _ctx(urgency="low", source="swe_bench_pro", complexity="simple")
    )
    assert route is not ProviderRoute.COMPLEX, (
        "if 'simple' routed COMPLEX the fix would be unfalsifiable"
    )
