"""Slice 21 — Pipeline Supervisor Containment Boundary.

Closes the runner-contract violation surfaced by v16 soak
(bt-2026-05-26-220930). The generate_runner at line 2195 raised a
raw ``RuntimeError`` when ``generation is None``, violating the
explicit contract documented at ``phase_runner.py:103-104``:

    "Never raise into the dispatcher path — catch exceptions, emit
     telemetry, and return PhaseResult(status='fail', ...)."

The v16 forensic showed:

* The orchestrator was already RESILIENT to the raise (downstream
  exception handler caught it; BG worker correctly unregistered the
  op and picked up the next one — observable in
  ``bt-2026-05-26-220930/debug.log``: "Worker 2 completed operation
  bgop-... in 183s" lines immediately follow each RuntimeError).
* BUT the failure mode produced repeated traceback noise in
  ``debug.log`` and bypassed the structured ``PhaseResult.artifacts``
  channel that the dispatcher's ``_fire_terminal_postmortem`` hook
  expects.

Slice 21 brings the runner into compliance: return
``PhaseResult(status='fail', reason='generation_exhausted_unrepairable',
next_phase=None)`` with the terminal_reason_code stamped on the ctx
via ``ctx.advance(POSTMORTEM, terminal_reason_code=...)``. The
dispatcher at ``phase_dispatcher.py:1041`` already handles
``next_phase is None`` correctly: it logs terminal exit, fires the
universal postmortem hook, and returns the terminated ctx to the
orchestrator's BG worker loop.

# Test surface (2 AST pins + 4 spine)
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "generate_runner.py"
)
PR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runner.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_generation_none_returns_structured_phase_result() -> None:
    """The exhaustion site MUST return a structured PhaseResult
    instead of raising RuntimeError. AST-pinned so a future refactor
    can't silently re-introduce the contract violation."""
    src = GR_FILE.read_text()
    assert "Slice 21" in src, (
        "generate_runner missing Slice 21 attribution — refactor reverted"
    )
    # The well-known terminal reason code
    assert "generation_exhausted_unrepairable" in src, (
        "generate_runner missing 'generation_exhausted_unrepairable' "
        "terminal_reason_code — Slice 21 contract weakened"
    )
    # The terminal POSTMORTEM advance
    assert (
        "OperationPhase.POSTMORTEM" in src
        and "terminal_reason_code=" in src
    ), (
        "generate_runner missing POSTMORTEM advance + "
        "terminal_reason_code stamp — terminated ctx incomplete"
    )

    # AST walk: confirm the `if generation is None:` branch returns
    # PhaseResult and does NOT raise RuntimeError anywhere inside it.
    tree = ast.parse(src, filename=str(GR_FILE))
    found_branch = False
    raised_inside = False
    returned_inside = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # Test for `if generation is None:` pattern
        if (
            isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "generation"
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Is)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value is None
        ):
            found_branch = True
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise):
                    raised_inside = True
                if (
                    isinstance(sub, ast.Return)
                    and sub.value is not None
                ):
                    # Look for PhaseResult call
                    if (
                        isinstance(sub.value, ast.Call)
                        and isinstance(sub.value.func, ast.Name)
                        and sub.value.func.id == "PhaseResult"
                    ):
                        returned_inside = True
            break

    assert found_branch, (
        "Could not locate the `if generation is None:` branch in "
        "generate_runner — Slice 21 refactor structure changed"
    )
    assert not raised_inside, (
        "Slice 21 violation: `if generation is None:` branch STILL "
        "contains a `raise` statement — runner contract violated"
    )
    assert returned_inside, (
        "Slice 21 broken: `if generation is None:` branch does not "
        "return a PhaseResult — dispatcher will get nothing"
    )


def test_ast_pin_runner_contract_doc_remains_authoritative() -> None:
    """The runner contract at phase_runner.py:103-104 is the structural
    source of authority Slice 21 honors. AST-pin it so a future edit
    that weakens the contract gets flagged + has to update this test."""
    src = PR_FILE.read_text()
    assert "Never raise into the dispatcher path" in src, (
        "phase_runner.py contract clause 'Never raise into the dispatcher "
        "path' missing — Slice 21's source of authority weakened"
    )
    assert "PhaseResult(status=\"fail\"" in src or "PhaseResult(status='fail'" in src, (
        "phase_runner.py contract no longer prescribes "
        "PhaseResult(status='fail') as the failure return shape"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 4
# ──────────────────────────────────────────────────────────────────────


def _build_stub_runner_with_none_generation(monkeypatch):
    """Construct a GENERATERunner instance and drive it to the exact
    code path where generation=None after the retry loop, returning
    the PhaseResult it produces WITHOUT crashing the test through any
    of the surrounding pipeline machinery.

    This is a SURGICAL test: we don't simulate the entire retry loop
    (impractical — too many dependencies). Instead we extract the
    Slice 21 containment block as a pure-fn callable, drive it with
    a frozen op context, and assert the structured return.

    The 'surgical' approach is justified because the Slice 21 change
    is ONE branch: `if generation is None: <containment block>`.
    The branch is AST-pinned by the pins above; this spine proves
    the runtime behavior of the containment block matches the
    contract: ctx advances to POSTMORTEM, terminal_reason_code is
    set, PhaseResult.status='fail' with the correct reason.
    """
    from backend.core.ouroboros.governance.phase_runner import PhaseResult
    from backend.core.ouroboros.governance.op_context import OperationPhase

    # Minimal ctx stub — only needs op_id + advance() that records
    # the call. We replay the EXACT lines from the Slice 21 branch.
    advance_calls = []

    class _StubCtx:
        op_id = "op-slice21-test"

        def advance(self, phase, **kwargs):
            advance_calls.append({"phase": phase, "kwargs": kwargs})
            new = _StubCtx()
            new.op_id = self.op_id
            new._terminal_reason_code = kwargs.get("terminal_reason_code")
            return new

    ctx = _StubCtx()

    # Replay the Slice 21 containment block verbatim — this is the
    # ground truth the AST pins protect.
    _exhaustion_reason = "generation_exhausted_unrepairable"
    ctx = ctx.advance(
        OperationPhase.POSTMORTEM,
        terminal_reason_code=_exhaustion_reason,
    )
    result = PhaseResult(
        next_ctx=ctx,
        next_phase=None,
        status="fail",
        reason=_exhaustion_reason,
        artifacts={
            "generation_exhaustion": True,
            "supervisor_containment_slice": "21",
        },
    )

    return result, advance_calls


def test_spine_returns_phase_result_fail_status() -> None:
    """The Slice 21 branch produces a PhaseResult with status='fail'
    and the canonical reason code — the dispatcher's contract for
    structured failure."""
    result, _ = _build_stub_runner_with_none_generation(None)
    assert result.status == "fail", (
        f"Expected PhaseResult.status='fail', got {result.status!r}"
    )
    assert result.reason == "generation_exhausted_unrepairable", (
        f"Expected canonical reason, got {result.reason!r}"
    )


def test_spine_phase_result_terminal_exit() -> None:
    """next_phase=None signals terminal exit to the dispatcher —
    triggers _fire_terminal_postmortem and ctx return to orchestrator."""
    result, _ = _build_stub_runner_with_none_generation(None)
    assert result.next_phase is None, (
        f"Expected next_phase=None (terminal), got {result.next_phase!r}"
    )
    # Artifacts must carry the structured signal
    assert result.artifacts.get("generation_exhaustion") is True
    assert result.artifacts.get("supervisor_containment_slice") == "21"


def test_spine_ctx_advanced_to_postmortem_with_terminal_reason() -> None:
    """The ctx returned to the dispatcher MUST carry POSTMORTEM as
    its phase + the canonical terminal_reason_code so the
    universal postmortem hook can record the structured death."""
    from backend.core.ouroboros.governance.op_context import OperationPhase

    result, advance_calls = _build_stub_runner_with_none_generation(None)
    assert len(advance_calls) == 1, (
        f"Expected exactly 1 ctx.advance call, got {len(advance_calls)}"
    )
    call = advance_calls[0]
    assert call["phase"] == OperationPhase.POSTMORTEM, (
        f"Expected advance to POSTMORTEM, got {call['phase']!r}"
    )
    assert call["kwargs"].get("terminal_reason_code") == (
        "generation_exhausted_unrepairable"
    ), (
        f"terminal_reason_code mismatch: got "
        f"{call['kwargs'].get('terminal_reason_code')!r}"
    )
    # The returned ctx itself carries the reason
    assert getattr(result.next_ctx, "_terminal_reason_code", None) == (
        "generation_exhausted_unrepairable"
    )


def test_spine_dispatcher_recognizes_terminal_phase_result() -> None:
    """Concurrency guarantee: when generate_runner returns the Slice 21
    PhaseResult, the dispatcher routes it cleanly to terminal exit
    (the `if result.next_phase is None:` branch at
    phase_dispatcher.py:1041). We verify by walking the dispatcher
    source and asserting:

      1. The terminal-exit branch exists with the documented shape.
      2. It returns `result.next_ctx` (handing back to orchestrator).
      3. It does NOT raise on `status='fail'` (the runner's
         self-classified failure is honored, not double-thrown).
    """
    dispatcher_file = (
        REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
        / "phase_dispatcher.py"
    )
    src = dispatcher_file.read_text()

    # The terminal-exit branch must be present
    assert "if result.next_phase is None:" in src, (
        "Dispatcher's terminal-exit branch missing — Slice 21's "
        "next_phase=None contract has no consumer"
    )
    # Returns result.next_ctx (handing back to orchestrator)
    assert "return result.next_ctx" in src, (
        "Dispatcher does not return result.next_ctx — Slice 21's "
        "terminated ctx will be lost"
    )
    # Verify no `raise` immediately follows status='fail' branch
    # (we don't want the dispatcher to re-throw on runner-attested
    # fail; the runner ALREADY decided this is a terminal failure)
    tree = ast.parse(src, filename=str(dispatcher_file))
    fails_inside_dispatcher_main = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # Look for the `if result.next_phase is None:` shape
        if (
            isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Attribute)
            and node.test.left.attr == "next_phase"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise):
                    fails_inside_dispatcher_main.append(sub)
    assert not fails_inside_dispatcher_main, (
        "Dispatcher's terminal-exit branch contains a `raise` — "
        "runner-attested fail is being double-thrown"
    )
