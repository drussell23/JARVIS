"""Slice 6 — dynamic budget reconciliation via L2 soft-stop re-dispatch.

Closes the governance paradox surfaced by soak bt-2026-05-25-174218:
the Ansible op (op-019e603d-178f) terminated at T+14s with `directive=
'cancel'` despite having 106s of UNUSED L2 budget. Empirical chain:

  10:52:15  L2 deadline reconciliation: pipeline_remaining=0.0s
            l2_timebox_env=120.0s effective=120.0s winning_cap=l2_timebox_fresh
  10:52:15  [L2 Repair] Iteration 1/5 starting (0s elapsed, 120s remaining)
  10:52:30  [L2 Repair] Iteration 1/5 tests: ❌ FAILED (unknown)
  10:52:30  [L2 Repair] Iteration 2/5 starting (14s elapsed, 106s remaining)
  10:52:30  PhaseDispatcher: status=fail reason='l2_stopped' — terminal
  10:52:30  l2_escape_return directive='cancel'

Iter 2's _generate_repair_candidate returned candidate=None with a
non-timeout stop_reason (empty_candidates or generate_error). Pre-Slice-6
``_l2_hook`` unconditionally mapped any L2_STOPPED → ('cancel', ctx),
murdering the op despite 106s of unused L2 budget. L2 used only 14s
of its fresh 120s window before being terminal-cancelled.

# Fix mechanism — soft vs hard stop classification + bounded re-dispatch

**1. _l2_hook now classifies stop_reasons:**

  HARD (genuinely exhausted — re-dispatch gains nothing):
    - timebox_exhausted
    - max_iterations_exhausted
    - max_validation_runs_exhausted
    - deadline_budget_exhausted

  SOFT (transient — fresh L2 dispatch could converge):
    - generate_error:<TypeName>
    - empty_candidates
    - consecutive_provider_timeouts_exhausted:N

On SOFT stop, returns ("l2_retry", ctx, stop_reason) — caller may
re-dispatch. On HARD stop, preserves pre-Slice-6 ("cancel", ctx)
verbatim.

**2. VALIDATE_RETRY's L2 dispatch block wrapped in bounded loop:**

  _l2_max_dispatches = JARVIS_L2_DISPATCH_RETRIES + 1  (default: 2)

  while _l2_dispatch_idx < _l2_max_dispatches:
      _l2_dispatch_idx += 1
      ... reconciliation ...  # fresh 120s on each pass
      directive = await self._l2_hook(...)
      if directive == "break":   _l2_break_directive = directive; break
      if directive == "l2_retry":
          if budget exhausted: advance to CANCELLED + return ctx
          else: continue  # re-dispatch
      if directive in ("cancel", "fatal"): return ctx

# Operator bindings honored

* Each L2 dispatch gets a fresh JARVIS_L2_TIMEBOX_S window via the
  pre-existing Session V reconciliation — no L2 dispatch ever sees
  ``pipeline_remaining=0.0s`` again.
* The harness wall-clock watchdog + cost_governor + IDLE timeout
  cap session-level wall time INDEPENDENTLY — re-dispatch never
  violates a global safety invariant.
* HARD stops still hard-stop (no infinite retry on genuinely
  exhausted budget).
* l2_redispatch / l2_soft_retries_exhausted FSM events provide
  Manifesto §8 absolute observability.

# Test surface (2 AST pins + 5 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "orchestrator.py"
)


def _parse() -> ast.Module:
    return ast.parse(ORCHESTRATOR_FILE.read_text(), filename=str(ORCHESTRATOR_FILE))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_l2_hook_classifies_soft_vs_hard_stops() -> None:
    """``_l2_hook`` must classify L2_STOPPED stop_reasons into HARD
    (preserved cancel) vs SOFT (new l2_retry directive). Without
    classification the bounded-retry loop has nothing to consume."""
    src = ORCHESTRATOR_FILE.read_text()
    # The classification structure must be present
    assert "_l2_hard_stop_prefixes" in src, (
        "Missing _l2_hard_stop_prefixes in _l2_hook — Slice 6 "
        "classification was reverted."
    )
    # All four HARD prefixes must be enumerated (matches repair_engine
    # _stopped() invocations for genuinely-exhausted conditions).
    for prefix in (
        '"timebox_exhausted"',
        '"max_iterations_exhausted"',
        '"max_validation_runs_exhausted"',
        '"deadline_budget_exhausted"',
    ):
        assert prefix in src, (
            f"Slice 6 HARD prefix {prefix} missing — soft/hard "
            "boundary will leak"
        )
    # The SOFT path must emit the new directive shape
    assert '"l2_retry"' in src, (
        "_l2_hook does not emit l2_retry directive — VALIDATE_RETRY "
        "loop will see no SOFT signal and revert to the old "
        "unconditional cancel."
    )
    # Both the soft-stop log + ledger event must be present
    assert "l2_soft_stop" in src, (
        "Missing l2_soft_stop ledger event tag"
    )


def test_ast_pin_validate_retry_loop_handles_l2_retry() -> None:
    """The VALIDATE_RETRY loop's L2 dispatch block must be wrapped
    in a bounded retry loop that consumes the l2_retry directive
    and re-dispatches with a fresh budget."""
    src = ORCHESTRATOR_FILE.read_text()
    # Bounded retry loop primitives
    assert "JARVIS_L2_DISPATCH_RETRIES" in src, (
        "Missing JARVIS_L2_DISPATCH_RETRIES env knob — operators "
        "cannot tune the re-dispatch cap"
    )
    assert "_l2_max_dispatches" in src, (
        "Missing _l2_max_dispatches counter — retry is unbounded "
        "or absent"
    )
    assert "_l2_dispatch_idx" in src, (
        "Missing _l2_dispatch_idx — no per-attempt tracking"
    )
    assert "_l2_soft_stop_history" in src, (
        "Missing _l2_soft_stop_history — operators lose audit trail "
        "of which stop_reasons fired across the retry cascade"
    )
    # The handler branch for l2_retry must exist
    assert 'directive[0] == "l2_retry"' in src, (
        "Loop does not match 'l2_retry' directive — Slice 6 "
        "wiring is dead code"
    )
    # Exhaustion path must terminal-fail with explicit reason
    assert "l2_soft_stop_retries_exhausted" in src, (
        "Missing l2_soft_stop_retries_exhausted terminal_reason — "
        "exhausted retries silently fall through"
    )
    # FSM event tags for observability (Manifesto §8)
    assert '"l2_redispatch"' in src, (
        "Missing l2_redispatch FSM tag — re-dispatch not observable"
    )
    assert '"l2_soft_retries_exhausted"' in src, (
        "Missing l2_soft_retries_exhausted FSM tag"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 5
# ──────────────────────────────────────────────────────────────────────


def test_spine_hard_stop_taxonomy_matches_repair_engine() -> None:
    """The four HARD prefixes in _l2_hook MUST exactly match the
    _stopped() invocations in repair_engine.py that indicate genuine
    budget exhaustion (not transient generation failure). Drift here
    would silently demote a HARD stop to SOFT — infinite retry risk."""
    re_file = (
        REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
        / "repair_engine.py"
    )
    re_src = re_file.read_text()
    # Each canonical HARD stop_reason must actually appear in
    # repair_engine.py as a _stopped() argument
    for hard in (
        '"timebox_exhausted"',
        '"max_iterations_exhausted"',
        '"max_validation_runs_exhausted"',
        '"deadline_budget_exhausted"',
    ):
        assert hard in re_src, (
            f"HARD prefix {hard} not found in repair_engine.py — "
            f"orchestrator and engine taxonomies have drifted; "
            f"Slice 6 classification is stale."
        )


def test_spine_default_retry_count_is_one() -> None:
    """Default JARVIS_L2_DISPATCH_RETRIES=1 means up to 2 total
    dispatches per op (initial + 1 retry). Sane production default
    that doubles L2 chances without inflating cost catastrophically."""
    src = ORCHESTRATOR_FILE.read_text()
    # Default reads "1" from the env (operator can raise/lower)
    assert 'os.environ.get("JARVIS_L2_DISPATCH_RETRIES", "1")' in src, (
        "Default JARVIS_L2_DISPATCH_RETRIES is not '1' — production "
        "default drifted from Slice 6 design"
    )
    # +1 conversion (retries → total dispatches) — AST-walk to find
    # the BinOp where left side is the env.get call and right is 1.
    tree = ast.parse(src, filename=str(ORCHESTRATOR_FILE))
    found_conversion = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and isinstance(node.right, ast.Constant)
            and node.right.value == 1
            and isinstance(node.left, ast.Call)
        ):
            # Look inside the int(...) call for the env lookup
            inner_src = ast.unparse(node.left)
            if "JARVIS_L2_DISPATCH_RETRIES" in inner_src:
                found_conversion = True
                break
    assert found_conversion, (
        "Missing `int(os.environ.get('JARVIS_L2_DISPATCH_RETRIES', '1')) + 1` "
        "BinOp — off-by-one risk on the retry cap (retries vs total dispatches)"
    )


def test_spine_break_directive_routes_to_outer_break() -> None:
    """When _l2_hook returns ('break', ...) inside the retry loop,
    we must capture it AND break the inner loop AND break the OUTER
    VALIDATE_RETRY while loop so the candidate proceeds to GATE.

    Pre-Slice 6 used `break` directly which broke a single layer; the
    new structure must preserve the outer-break semantic via the
    captured-directive pattern."""
    src = ORCHESTRATOR_FILE.read_text()
    # Capture pattern
    assert "_l2_break_directive = directive" in src, (
        "_l2_break_directive not captured — break path leaks out "
        "of the retry loop without unwinding correctly"
    )
    # Outer-loop unwind
    assert "if _l2_break_directive is not None:" in src, (
        "Missing post-inner-loop break-directive handler — converged "
        "L2 candidate never reaches GATE"
    )


def test_spine_retries_exhausted_terminal_advances_ctx() -> None:
    """When ALL re-dispatches consume their budget on soft stops,
    the orchestrator must advance ctx to CANCELLED terminal phase
    AND record a FAILED ledger entry — leaving ctx unadvanced would
    break the orchestrator's terminal-state invariant."""
    src = ORCHESTRATOR_FILE.read_text()
    # ctx must be advanced explicitly
    assert (
        "ctx.advance(\n                                    OperationPhase.CANCELLED,"
        in src
        or "OperationPhase.CANCELLED,\n                                    terminal_reason_code=(" in src
    ), (
        "Retries-exhausted path does NOT advance ctx to CANCELLED — "
        "terminal-state invariant broken"
    )
    # Ledger record with soft_stop_history
    assert "soft_stop_history" in src, (
        "Missing soft_stop_history in ledger record — operators "
        "lose audit trail of which provider failure shapes ate the budget"
    )


def test_spine_legacy_l2_skipped_path_preserved() -> None:
    """When repair_engine is None or best_validation is None, the
    pre-Slice-6 ``l2_skipped`` FSM event must still fire. Slice 6's
    restructure (inner while loop) must not break the no-L2 path."""
    src = ORCHESTRATOR_FILE.read_text()
    assert '"l2_skipped"' in src, (
        "l2_skipped FSM event lost — no-L2 path broken by Slice 6 restructure"
    )
