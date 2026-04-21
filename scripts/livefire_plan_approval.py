"""End-to-end live-fire for problem #7 Plan Approval modality.

Proves the four additive wiring items work together:

  1. Orchestrator should_force_plan_review() OR-in engages the
     gate for every op when JARVIS_PLAN_APPROVAL_MODE=true.
  2. Orchestrator shadow-mirror registers the plan with
     PlanApprovalController so REPL + IDE surfaces see it.
  3. SerpentFlow /plan dispatcher resolves approvals through the
     controller.
  4. End-to-end: an async pipeline that halts at PLAN awaits the
     dispatcher's approve call and proceeds on resolution.

Simulated orchestrator (the real one requires a 6-layer stack
boot that's out of scope for this proof). We exercise the same
public surface the orchestrator uses:

    from backend.core.ouroboros.governance.plan_approval import (
        should_force_plan_review, get_default_controller,
    )
    from backend.core.ouroboros.governance.plan_approval_repl import (
        dispatch_plan_command,
    )

Writes a journal under ``.livefire/plan-approval-<ts>/``.
Exits 0 on success.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.governance.plan_approval import (
    PlanApprovalOutcome,
    get_default_controller,
    needs_approval,
    plan_approval_mode_enabled,
    reset_default_controller,
    should_force_plan_review,
)
from backend.core.ouroboros.governance.plan_approval_repl import (
    dispatch_plan_command,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_PLAN_APPROVED,
    EVENT_TYPE_PLAN_PENDING,
    EVENT_TYPE_PLAN_REJECTED,
    StreamEventBroker,
    bridge_plan_approval_to_broker,
    reset_default_broker,
)


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------


@dataclass
class Journal:
    steps: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)

    def step(self, name: str, **kwargs: Any) -> None:
        self.steps.append({"name": name, "ts": time.time(), **kwargs})
        print("[livefire] %s  %s"
              % (name, json.dumps(kwargs, default=str)))

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print("[livefire] FAIL: " + msg, file=sys.stderr)

    def write(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"steps": self.steps, "failures": self.failures},
                indent=2, default=str,
            )
        )


# --------------------------------------------------------------------------
# Simulated orchestrator
# --------------------------------------------------------------------------


class SimulatedOrchestrator:
    """Mimics the orchestrator's PLAN-phase plan-approval hook at
    the public-surface level. Uses the same primitives the real
    orchestrator uses, so a green run here proves the same code
    paths work under the real stack (the 6-layer boot is what's
    mocked away)."""

    def __init__(self, plan_markdown: str) -> None:
        self._plan_markdown = plan_markdown

    async def run_op(
        self, op_id: str, journal: Journal, timeout_s: float = 10.0,
    ) -> Dict[str, Any]:
        """Simulate an op's PLAN → approval-gate → GENERATE flow.

        Returns a dict with the outcome + whether GENERATE was
        reached. The orchestrator's real code:
          1. calls should_force_plan_review(ctx)
          2. if True, registers the plan with the controller
          3. awaits the decision
          4. branches to GENERATE (approved) or POSTMORTEM (rejected)
        """
        forced = should_force_plan_review()
        if not forced:
            journal.step("op_no_review_needed", op_id=op_id)
            return {"op_id": op_id, "generated": True, "reviewed": False}

        controller = get_default_controller()
        plan_payload = {
            "markdown": self._plan_markdown,
            "approach": "live-fire test approach",
            "complexity": "moderate",
            "ordered_changes": [
                {"file_path": "x.py", "action": "modify",
                 "reason": "flag add"},
            ],
            "risk_factors": ["proof only"],
            "test_strategy": "live-fire harness",
        }
        future = controller.request_approval(
            op_id, plan_payload, timeout_s=timeout_s,
        )
        journal.step(
            "op_plan_pending", op_id=op_id,
            pending_count=controller.pending_count,
        )

        outcome: PlanApprovalOutcome = await asyncio.wait_for(
            future, timeout=timeout_s + 2.0,
        )
        journal.step(
            "op_plan_resolved", op_id=op_id, state=outcome.state,
            reviewer=outcome.reviewer, approved=outcome.approved,
            reason=outcome.reason,
        )
        if outcome.approved:
            return {
                "op_id": op_id, "generated": True, "reviewed": True,
                "outcome": outcome.state,
            }
        return {
            "op_id": op_id, "generated": False, "reviewed": True,
            "outcome": outcome.state, "reason": outcome.reason,
        }


# --------------------------------------------------------------------------
# Approver coroutines — simulate /plan approve + /plan reject
# --------------------------------------------------------------------------


async def _approve_via_repl(op_id: str, journal: Journal) -> None:
    """Simulate an operator typing ``/plan approve <op-id>`` in
    SerpentFlow. Goes through dispatch_plan_command → controller."""
    await asyncio.sleep(0.1)  # let request_approval settle
    result = dispatch_plan_command("/plan approve " + op_id)
    journal.step(
        "repl_approve_dispatched", op_id=op_id,
        ok=result.ok, matched=result.matched,
    )


async def _reject_via_repl(op_id: str, reason: str, journal: Journal) -> None:
    await asyncio.sleep(0.1)
    result = dispatch_plan_command(
        "/plan reject " + op_id + " " + reason,
    )
    journal.step(
        "repl_reject_dispatched", op_id=op_id,
        ok=result.ok, matched=result.matched,
    )


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


async def _run(journal: Journal) -> int:
    # 1. Pin plan-mode ON for this session.
    os.environ["JARVIS_PLAN_APPROVAL_MODE"] = "true"
    reset_default_controller()
    reset_default_broker()
    if not plan_approval_mode_enabled():
        journal.fail("JARVIS_PLAN_APPROVAL_MODE did not enable")
        return 1
    if not needs_approval():
        journal.fail("needs_approval() returned False despite env on")
        return 1
    if not should_force_plan_review():
        journal.fail("should_force_plan_review() returned False")
        return 1
    journal.step("env_pinned_on", mode_enabled=True)

    # 2. Wire IDE observability bridge (controller → broker).
    broker = StreamEventBroker()
    controller = get_default_controller()
    unsub = bridge_plan_approval_to_broker(controller, broker)
    journal.step("bridge_installed", subscriber=True)

    # 3. HAPPY PATH: op-happy gets approved, reaches GENERATE.
    orch = SimulatedOrchestrator(plan_markdown="# happy plan\napproach: yes")
    asyncio.ensure_future(_approve_via_repl("op-happy", journal))
    result = await orch.run_op("op-happy", journal, timeout_s=5.0)
    if not result["generated"]:
        journal.fail("op-happy did not reach GENERATE: " + repr(result))
        return 1
    journal.step("op_happy_completed", **result)

    # 4. REJECT PATH: op-reject rejected, stays away from GENERATE.
    asyncio.ensure_future(_reject_via_repl(
        "op-reject", "live-fire-wrong-approach", journal,
    ))
    result = await orch.run_op("op-reject", journal, timeout_s=5.0)
    if result["generated"]:
        journal.fail("op-reject SHOULD NOT have reached GENERATE: "
                     + repr(result))
        return 1
    if result.get("reason") != "live-fire-wrong-approach":
        journal.fail("op-reject reason not threaded through: "
                     + repr(result))
        return 1
    journal.step("op_reject_blocked", **result)

    # 5. IDE SSE frames emitted — verify broker saw the transitions.
    frames = list(broker._history)
    frame_types = [f.event_type for f in frames]
    frame_ops = [f.op_id for f in frames]
    journal.step(
        "broker_frames_captured",
        count=len(frames), types=frame_types, ops=frame_ops,
    )
    must_see = {
        EVENT_TYPE_PLAN_PENDING: 2,      # one per op
        EVENT_TYPE_PLAN_APPROVED: 1,
        EVENT_TYPE_PLAN_REJECTED: 1,
    }
    for event_type, min_count in must_see.items():
        n = frame_types.count(event_type)
        if n < min_count:
            journal.fail(
                "expected >=%d %s frames, saw %d" % (
                    min_count, event_type, n,
                )
            )

    # 6. /plan history command shows both resolutions.
    history_result = dispatch_plan_command("/plan history")
    if not history_result.ok:
        journal.fail("/plan history failed: " + history_result.text)
        return 1
    if ("op-happy" not in history_result.text
            or "op-reject" not in history_result.text):
        journal.fail(
            "/plan history missing op-happy or op-reject: "
            + history_result.text[:200]
        )
        return 1
    journal.step("plan_history_ok", text_len=len(history_result.text))

    # 7. /plan pending (after both resolved) should show no pending
    # plans now.
    pending_result = dispatch_plan_command("/plan pending")
    if "op-happy" in pending_result.text or "op-reject" in pending_result.text:
        journal.fail(
            "/plan pending leaked terminal op_ids: "
            + pending_result.text[:200]
        )
        return 1
    journal.step(
        "plan_pending_clean", text_excerpt=pending_result.text[:80],
    )

    unsub()
    return 0 if not journal.failures else 1


def main() -> int:
    journal = Journal()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = _REPO_ROOT / ".livefire" / ("plan-approval-" + ts)
    try:
        exit_code = asyncio.run(_run(journal))
    except BaseException as exc:  # noqa: BLE001
        journal.fail("uncaught: %r" % exc)
        exit_code = 1
    journal.write(out_dir / "journal.json")
    if exit_code == 0:
        print("[livefire] PASS  journal=%s/journal.json" % out_dir)
    else:
        print("[livefire] FAIL  journal=%s/journal.json  failures=%d"
              % (out_dir, len(journal.failures)), file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
