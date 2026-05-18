"""SWE-bench op-isolation + routing fix — spine.

Closes the regression + latent gaps diagnosed from soak
bt-2026-05-17-213727 (psf__requests-3362 + django__django-16255 fused
into one Frankenstein op; complexity stayed 'simple'; PLAN skipped):

  #1  _coalesce_key collapsed every empty-target envelope to the shared
      "" key -> distinct SWE-bench problems coalesced into one op
      (" | ".join(descs)). Fix: empty target_files falls back to the
      EXISTING evidence["signature"] provenance (B.2.1) else the unique
      idempotency_key. Distinct instance_ids => strictly distinct ops.

  #2  task_complexity was 'simple' (file-count heuristic) -> STANDARD
      route -> PLAN starved. Fix #2a: intake stamps the
      _COMPLEX_FLOOR_SOURCES floor at ctx-creation (earliest point,
      before route/budget). Fix #2b: the orchestrator classify must NOT
      DOWNGRADE a pre-stamped floor (general no-downgrade property).

  #3  PlanGenerator._should_skip returned 'no_target_files' for
      empty-target envelopes. Fix: sources in the existing
      _EMPTY_TARGET_FILES_EXEMPT_SOURCES never skip PLAN (localize-from-
      issue NEEDS PLAN).

All fixes compose existing closed-set / signature mechanisms — no new
keys, no flags, no hardcoded tables, no duplication.
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.complexity_classifier import (
    ComplexityClass,
    OperationComplexityClassifier,
    _COMPLEX_FLOOR_SOURCES,
)
from backend.core.ouroboros.governance.intake.intent_envelope import (
    _EMPTY_TARGET_FILES_EXEMPT_SOURCES,
    make_envelope,
)
from backend.core.ouroboros.governance.intake import unified_intake_router
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    UnifiedIntakeRouter,
)
from backend.core.ouroboros.governance import orchestrator as _orch_mod
from backend.core.ouroboros.governance.phase_runners import (
    classify_runner as _classify_runner_mod,
)
from backend.core.ouroboros.governance.plan_generator import PlanGenerator


def _env(source: str, *, sig: str = "", tfiles=(), desc: str = "issue text"):
    ev = {"signature": sig} if sig else {}
    return make_envelope(
        source=source, description=desc, target_files=tuple(tfiles),
        repo="r", confidence=1.0, urgency="low", evidence=ev,
        requires_human_ack=False,
    )


# ---------------------------------------------------------------------------
# #1 — coalesce-key: distinct SWE-bench problems never fuse
# ---------------------------------------------------------------------------


def test_distinct_swe_bench_instances_get_distinct_coalesce_keys():
    ck = UnifiedIntakeRouter._coalesce_key
    s = SimpleNamespace()
    psf = _env("swe_bench_pro", sig="psf__requests-3362")
    dj = _env("swe_bench_pro", sig="django__django-16255")
    k_psf, k_dj = ck(s, psf), ck(s, dj)
    assert k_psf != k_dj, (
        "REGRESSION: distinct SWE-bench problems share a coalesce key "
        "-> they fuse into one Frankenstein op (bt-2026-05-17-213727)"
    )
    assert ck(s, _env("swe_bench_pro", sig="psf__requests-3362")) == k_psf


def test_empty_target_key_is_never_the_shared_empty_string():
    """Regression pin: empty target_files MUST NOT key to "" (the old
    behavior that collapsed the key-space)."""
    ck = UnifiedIntakeRouter._coalesce_key
    s = SimpleNamespace()
    assert ck(s, _env("swe_bench_pro", sig="x")) != ""
    assert ck(s, _env("vision_sensor")) != ""


def test_nonempty_target_files_coalesce_key_unchanged():
    """Byte-identical for the normal path: file-set keying preserved."""
    ck = UnifiedIntakeRouter._coalesce_key
    s = SimpleNamespace()
    assert ck(s, _env("backlog", tfiles=("b.py", "a.py"))) == "a.py|b.py"


def test_empty_target_no_signature_is_unique_never_coalesces():
    ck = UnifiedIntakeRouter._coalesce_key
    s = SimpleNamespace()
    assert ck(s, _env("vision_sensor")) != ck(s, _env("vision_sensor"))


def test_flush_coalesced_single_env_returns_it_unmerged():
    """A buffer holding one envelope (post-fix steady state for
    distinct SWE-bench problems) is returned verbatim — never the
    " | ".join Frankenstein merge path."""
    r = SimpleNamespace(_coalesce_buffer={}, _coalesce_timestamps={})
    psf = _env("swe_bench_pro", sig="psf__requests-3362", desc="psf only")
    key = UnifiedIntakeRouter._coalesce_key(r, psf)
    r._coalesce_buffer[key] = [psf]
    out = UnifiedIntakeRouter._flush_coalesced(r, key)
    assert out is psf
    assert " | " not in out.description


# ---------------------------------------------------------------------------
# #2 — complexity floor stamped at intake (pre-route) + no-downgrade
# ---------------------------------------------------------------------------


def test_ast_intake_stamps_complexity_floor_after_ctx_create():
    """unified_intake_router MUST import _COMPLEX_FLOOR_SOURCES and
    stamp task_complexity='complex' when envelope.source is in it,
    AFTER OperationContext.create (earliest pre-route point)."""
    src = inspect.getsource(unified_intake_router)
    assert "_COMPLEX_FLOOR_SOURCES" in src
    assert 'object.__setattr__(ctx, "task_complexity", "complex")' in src
    assert "envelope.source in _COMPLEX_FLOOR_SOURCES" in src
    i_create = src.index("OperationContext.create(")
    i_stamp = src.index(
        'object.__setattr__(ctx, "task_complexity", "complex")'
    )
    assert i_stamp > i_create, (
        "floor stamp MUST run AFTER ctx creation (earliest pre-route "
        "point; create() hardcodes task_complexity='')"
    )


def test_ast_orchestrator_has_no_downgrade_guard():
    src = inspect.getsource(_orch_mod)
    assert "_CX_RANK" in src and "NO-DOWNGRADE" in src
    i_rank = src.index("_CX_RANK = {")
    i_set = src.index(
        'object.__setattr__(ctx, "task_complexity", _eff_cx)'
    )
    assert i_rank < i_set, "rank logic MUST precede the stamp"


def test_ast_classify_runner_threads_source_into_classify():
    """Trace-1 root fix: CLASSIFYRunner is the LIVE phase-dispatcher
    path. It MUST pass source= into the complexity classifier or the
    _COMPLEX_FLOOR_SOURCES floor can never fire (the orchestrator
    inline block is dead under the dispatcher)."""
    src = inspect.getsource(_classify_runner_mod)
    classify_calls = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "classify"
        and {kw.arg for kw in n.keywords} >= {"description", "target_files"}
    ]
    assert classify_calls, (
        "could not locate the OperationComplexityClassifier.classify "
        "call in classify_runner"
    )
    assert any(
        "source" in {kw.arg for kw in c.keywords} for c in classify_calls
    ), (
        "REGRESSION: classify_runner stopped threading source= into "
        "the complexity classifier — _COMPLEX_FLOOR_SOURCES is dead on "
        "the live path (Trace-1)"
    )


def test_ast_classify_runner_has_no_downgrade_guard_parity():
    """classify_runner MUST carry the SAME _CX_RANK no-downgrade stamp
    as the orchestrator inline block (parity) so a pre-stamped floor
    cannot be clobbered on the live path."""
    src = inspect.getsource(_classify_runner_mod)
    assert "_CX_RANK" in src, (
        "classify_runner missing _CX_RANK no-downgrade parity (Trace-1)"
    )
    i_rank = src.index("_CX_RANK = {")
    i_set = src.index(
        'object.__setattr__(ctx, "task_complexity", _eff_cx)'
    )
    assert i_rank < i_set, "rank logic MUST precede the stamp"
    # Identical rank table to the orchestrator (no drift between paths).
    import ast as _ast
    def _rank(mod_src):
        s = mod_src.index("_CX_RANK = {") + len("_CX_RANK = ")
        return _ast.literal_eval(mod_src[s:mod_src.index("}", s) + 1])
    assert _rank(src) == _rank(inspect.getsource(_orch_mod)), (
        "classify_runner _CX_RANK MUST match orchestrator _CX_RANK "
        "(parity — no per-path divergence)"
    )


def test_no_downgrade_rank_semantics_preserve_floor():
    """Re-derive the exact rank table from source via ast.literal_eval
    (safe — literal only) and prove a 'complex' pre-stamp is NOT
    downgraded by a 'simple' classification, but IS upgraded."""
    src = inspect.getsource(_orch_mod)
    start = src.index("_CX_RANK = {") + len("_CX_RANK = ")
    end = src.index("}", start) + 1
    rank = ast.literal_eval(src[start:end])
    assert isinstance(rank, dict) and rank["complex"] > rank["simple"]

    def stronger(prev, new):
        return prev if rank.get(prev, -1) > rank.get(new, -1) else new

    assert stronger("complex", "simple") == "complex"      # floor held
    assert stronger("simple", "architectural") == "architectural"  # upgrade


@pytest.mark.parametrize("tfiles", [(), ("requests/models.py",)])
def test_classifier_floors_swe_bench_pro_complex(tfiles):
    res = OperationComplexityClassifier().classify(
        "fix bug", list(tfiles), source="swe_bench_pro",
    )
    assert res.complexity is ComplexityClass.COMPLEX
    assert "swe_bench_pro" in _COMPLEX_FLOOR_SOURCES


# ---------------------------------------------------------------------------
# #3 — PLAN never skipped for localize-from-issue sources
# ---------------------------------------------------------------------------


def _skip(source: str, tfiles=(), desc: str = "short"):
    ctx = SimpleNamespace(
        signal_source=source, target_files=tuple(tfiles), description=desc,
    )
    return PlanGenerator._should_skip(SimpleNamespace(), ctx)


def test_plan_not_skipped_for_localize_from_issue_sources():
    # Empty target_files + long issue text: pre-fix returned
    # "no_target_files"; now PLAN MUST run.
    assert _skip("swe_bench_pro", (), "x" * 5000) == ""
    assert _skip("vision_sensor", (), "x" * 5000) == ""


def test_plan_skip_unchanged_for_non_exempt_sources():
    # Byte-identical for organic sources: 0 files still skips PLAN.
    assert _skip("test_failure", (), "x" * 5000) == "no_target_files"
    assert "swe_bench_pro" in _EMPTY_TARGET_FILES_EXEMPT_SOURCES
    assert "test_failure" not in _EMPTY_TARGET_FILES_EXEMPT_SOURCES


def test_ast_plan_skip_reuses_existing_closed_set():
    src = inspect.getsource(PlanGenerator._should_skip)
    assert "_EMPTY_TARGET_FILES_EXEMPT_SOURCES" in src, (
        "PLAN-skip exemption MUST reuse the existing closed set, not a "
        "new hardcoded source list"
    )
