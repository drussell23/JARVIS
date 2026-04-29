"""Priority A Slice A3 — PLAN-runner default-claim wiring spine.

Closes the silent-disable gap that nuked Phase 2 in soak #3: the
trivial-op short-circuit (and every failure exit path) now captures
the default must_hold claims unconditionally via a single chokepoint
helper.

Pins:
  §1   Helper exists at module level (not just class-private)
  §2   Helper is async (capture_claims is async)
  §3   Helper is master-flag-gated (returns 0 when off)
  §4   Helper returns 0 when op_id is empty
  §5   Helper never raises on garbage ctx
  §6   Helper writes one ledger record per default claim on happy path
  §7   Helper is called before EVERY `return PhaseResult` exit in
       plan_runner.py (source-grep invariant — catches future
       refactors that re-introduce the silent-disable gap)
  §8   Helper is called for all 7 distinct exit_reason values:
       plan_required_unavailable / plan_review_unavailable:provider_missing /
       plan_review_unavailable:gate_infra / plan_rejected /
       plan_approval_expired / user_cancelled / planned
  §9   Helper diagnostic log line includes count + exit_reason + op_id
  §10  Master-off (JARVIS_DEFAULT_CLAIMS_ENABLED=false) → no ledger
       writes from helper; no behavior change at runner level
  §11  Helper composes with Slice 2.3's plan-synthesizer on success
       path (default claims ADD to plan-synthesized claims, never
       replace)
  §12  Helper does NOT import orchestrator / candidate_generator
       (authority invariant)
"""
from __future__ import annotations

import asyncio
import inspect
import re
from types import SimpleNamespace
from typing import List

import pytest

from backend.core.ouroboros.governance.phase_runners import plan_runner as pr_mod
from backend.core.ouroboros.governance.phase_runners.plan_runner import (
    _capture_default_claims_at_plan_exit,
)


# ---------------------------------------------------------------------------
# §1-§2 — Helper surface
# ---------------------------------------------------------------------------


def test_helper_exists_at_module_level() -> None:
    assert hasattr(pr_mod, "_capture_default_claims_at_plan_exit")
    assert callable(pr_mod._capture_default_claims_at_plan_exit)


def test_helper_is_async() -> None:
    assert inspect.iscoroutinefunction(_capture_default_claims_at_plan_exit)


# ---------------------------------------------------------------------------
# §3-§5 — Defensive contracts
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_DEFAULT_CLAIMS_ENABLED", "true")
    monkeypatch.setenv(
        "OUROBOROS_BATTLE_SESSION_ID", "plan-runner-a3-test",
    )
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        reset_all_for_tests,
    )
    reset_all_for_tests()
    yield tmp_path
    reset_all_for_tests()


def test_helper_master_off_returns_zero(isolated_ledger, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DEFAULT_CLAIMS_ENABLED", "false")
    ctx = SimpleNamespace(op_id="op-a3-1", target_files=("a.py",))
    n = asyncio.run(
        _capture_default_claims_at_plan_exit(ctx, exit_reason="planned"),
    )
    assert n == 0


def test_helper_empty_op_id_returns_zero(isolated_ledger) -> None:
    ctx = SimpleNamespace(op_id="", target_files=("a.py",))
    n = asyncio.run(
        _capture_default_claims_at_plan_exit(ctx, exit_reason="planned"),
    )
    assert n == 0


def test_helper_never_raises_on_garbage_ctx(isolated_ledger) -> None:
    # ctx without op_id attr at all → returns 0, no raise
    class BadCtx:
        pass
    n = asyncio.run(
        _capture_default_claims_at_plan_exit(
            BadCtx(), exit_reason="planned",
        ),
    )
    assert n == 0


# ---------------------------------------------------------------------------
# §6 — Happy path writes ledger records
# ---------------------------------------------------------------------------


def test_helper_writes_three_ledger_records_on_happy_path(
    isolated_ledger,
) -> None:
    """Per Slice A2 seed registry: 3 default claims for an op
    touching .py files."""
    ctx = SimpleNamespace(
        op_id="op-a3-happy", target_files=("backend/foo.py",),
    )
    n = asyncio.run(
        _capture_default_claims_at_plan_exit(ctx, exit_reason="planned"),
    )
    # 3 default claims: file_parses_after_change (matches *.py),
    # test_set_hash_stable (no filter), no_new_credential_shapes (no filter)
    assert n == 3
    # Verify they round-tripped through the ledger
    from backend.core.ouroboros.governance.verification import (
        get_recorded_claims,
    )
    claims = get_recorded_claims(
        op_id="op-a3-happy", session_id="plan-runner-a3-test",
    )
    kinds = sorted(c.property.kind for c in claims)
    assert kinds == [
        "file_parses_after_change",
        "no_new_credential_shapes",
        "test_set_hash_stable",
    ]


# ---------------------------------------------------------------------------
# §7 — Source-grep invariant: every return PhaseResult is guarded
# ---------------------------------------------------------------------------


_RETURN_PHASE_RESULT_RE = re.compile(r"^\s*return PhaseResult\(")


def _scan_phase_result_returns(src: str) -> List[int]:
    """Return line numbers of every `return PhaseResult(` at code
    indentation. Filters out comments + docstrings + backtick-quoted
    mentions (e.g., `return PhaseResult(...)` inside a comment is not
    a real return statement)."""
    lines = []
    for i, line in enumerate(src.splitlines(), start=1):
        if _RETURN_PHASE_RESULT_RE.match(line):
            lines.append(i)
    return lines


def _has_helper_call_within_n_lines_above(
    src: str, target_line: int, *, n: int = 30,
) -> bool:
    """Check if `_capture_default_claims_at_plan_exit` is called in
    the n lines preceding `target_line`."""
    lines = src.splitlines()
    start = max(0, target_line - 1 - n)
    end = target_line - 1  # exclusive — return line itself doesn't count
    window = "\n".join(lines[start:end])
    return "_capture_default_claims_at_plan_exit(" in window


def test_every_phase_result_return_is_preceded_by_helper_call() -> None:
    """The structural invariant — every PLAN exit captures default
    claims. A future refactor that reintroduces the silent-disable
    gap fails this test."""
    src = inspect.getsource(pr_mod)
    return_lines = _scan_phase_result_returns(src)
    # Sanity: PLAN runner has 7 exit points (6 fail + 1 success)
    assert len(return_lines) >= 7, (
        f"expected >=7 PhaseResult returns, found {len(return_lines)}"
    )
    failures = []
    for line_no in return_lines:
        if not _has_helper_call_within_n_lines_above(src, line_no, n=30):
            # Read the line + 5 surrounding for diagnostic
            ctx_lines = src.splitlines()[max(0, line_no - 5):line_no + 2]
            failures.append(
                f"line {line_no}: no helper call within 30 lines above\n"
                f"context:\n  " + "\n  ".join(ctx_lines)
            )
    assert not failures, (
        "Slice A3 invariant violated — every PLAN exit must capture "
        "default claims:\n\n" + "\n\n".join(failures)
    )


# ---------------------------------------------------------------------------
# §8 — All 7 distinct exit_reason values used
# ---------------------------------------------------------------------------


def test_seven_distinct_exit_reasons_in_source() -> None:
    """Each PLAN exit path tags itself with a distinct exit_reason
    so observability can distinguish failure modes."""
    src = inspect.getsource(pr_mod)
    expected_reasons = (
        "plan_required_unavailable",
        "plan_review_unavailable:provider_missing",
        "plan_review_unavailable:gate_infra",
        "plan_rejected",
        "plan_approval_expired",
        "user_cancelled",
        "planned",
    )
    for reason in expected_reasons:
        assert f'exit_reason="{reason}"' in src, (
            f"PLAN exit for {reason!r} must call helper with "
            f"exit_reason={reason!r}"
        )


# ---------------------------------------------------------------------------
# §9 — Diagnostic logging
# ---------------------------------------------------------------------------


def test_helper_log_line_includes_count_and_reason(
    isolated_ledger, caplog,
) -> None:
    import logging
    caplog.set_level(logging.INFO, logger="Ouroboros.Orchestrator")
    ctx = SimpleNamespace(op_id="op-a3-log", target_files=("a.py",))
    asyncio.run(
        _capture_default_claims_at_plan_exit(
            ctx, exit_reason="plan_rejected",
        ),
    )
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "default_claims_captured" in log_text
    assert "exit_reason=plan_rejected" in log_text
    assert "op=op-a3-log" in log_text


# ---------------------------------------------------------------------------
# §10 — Master-off → no ledger writes
# ---------------------------------------------------------------------------


def test_master_off_writes_zero_records(
    isolated_ledger, monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_DEFAULT_CLAIMS_ENABLED", "false")
    ctx = SimpleNamespace(op_id="op-a3-off", target_files=("a.py",))
    n = asyncio.run(
        _capture_default_claims_at_plan_exit(
            ctx, exit_reason="planned",
        ),
    )
    assert n == 0
    from backend.core.ouroboros.governance.verification import (
        get_recorded_claims,
    )
    claims = get_recorded_claims(
        op_id="op-a3-off", session_id="plan-runner-a3-test",
    )
    assert claims == ()


# ---------------------------------------------------------------------------
# §11 — Composes additively with Slice 2.3 plan-synthesizer
# ---------------------------------------------------------------------------


def test_default_claims_add_to_plan_synthesized_claims(
    isolated_ledger,
) -> None:
    """Both Slice 2.3 plan claims AND Slice A3 default claims should
    land in the ledger for the same op when both are present."""
    from backend.core.ouroboros.governance.verification import (
        capture_claims,
        get_recorded_claims,
        synthesize_claims_from_plan,
    )

    # synthesize_claims_from_plan expects a list of test-name STRINGS
    # under test_strategy.tests_to_pass (per Slice 2.3 contract).
    plan = {
        "test_strategy": {
            "tests_to_pass": ["test_foo_works"],
        },
    }
    plan_claims = synthesize_claims_from_plan(plan, op_id="op-additive")
    assert len(plan_claims) >= 1

    async def _run():
        await capture_claims(op_id="op-additive", claims=plan_claims)
        await _capture_default_claims_at_plan_exit(
            SimpleNamespace(
                op_id="op-additive", target_files=("a.py",),
            ),
            exit_reason="planned",
        )
        return get_recorded_claims(
            op_id="op-additive", session_id="plan-runner-a3-test",
        )

    recovered = asyncio.run(_run())
    # Plan synthesized at least 1 + default 3 = at least 4
    assert len(recovered) >= 4
    kinds = {c.property.kind for c in recovered}
    # Plan claim kind
    assert "test_passes" in kinds
    # All 3 default claim kinds
    assert "file_parses_after_change" in kinds
    assert "test_set_hash_stable" in kinds
    assert "no_new_credential_shapes" in kinds


# ---------------------------------------------------------------------------
# §12 — Authority invariant
# ---------------------------------------------------------------------------


def test_helper_does_not_import_authority_modules() -> None:
    """The helper must not import orchestrator / candidate_generator /
    iron_gate / change_engine. Default-claim capture is a verification-
    layer concern; it must not couple PLAN runner to authority modules
    via this code path."""
    src = inspect.getsource(_capture_default_claims_at_plan_exit)
    forbidden = (
        "orchestrator", "candidate_generator", "iron_gate", "change_engine",
        "policy",
    )
    for token in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{token}" not in src
        ), f"helper must not import {token}"
        assert (
            f"import backend.core.ouroboros.governance.{token}" not in src
        ), f"helper must not import {token}"
