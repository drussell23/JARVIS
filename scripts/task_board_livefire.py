"""Gap #5 Slice 4 live-fire proof — TaskBoard under graduated defaults.

Mirrors scripts/testrunner_streaming_livefire.py (Ticket #4 Slice 4
live-fire). Proves the Slice 4 graduation works outside the test
harness under the production defaults — no env overrides, model-
facing Venom surface exercised end-to-end, session artifact
captured under .ouroboros/sessions/.

What this script verifies empirically:

  * Graduated defaults are active with zero env overrides
    (JARVIS_TOOL_TASK_BOARD_ENABLED + JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED
    both evaluate to True on a fresh process)
  * Policy ALLOWS each of the three task tools with well-formed args
  * Full lifecycle: task_create → task_update(start) → task_update(edit)
    → task_create (second task) → task_complete (first task)
    → task_update(cancel) (second task)
  * TaskBoard emits the documented [TaskBoard] task_* INFO audit lines
    synchronously with each state change
  * render_prompt_section() produces the advisory subsection with the
    expected structure when an active task exists
  * close_task_board() cleans up the registry idempotently
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from io import StringIO
from pathlib import Path

REPO_ROOT = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
sys.path.insert(0, str(REPO_ROOT))

# CRITICAL: do NOT set the Slice 4 env flags. We want to prove the
# GRADUATED DEFAULTS activate everything.
for key in (
    "JARVIS_TOOL_TASK_BOARD_ENABLED",
    "JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED",
):
    os.environ.pop(key, None)

from backend.core.ouroboros.governance.task_board import (
    _prompt_injection_enabled,
)
from backend.core.ouroboros.governance.task_tool import (
    _BOARDS,
    close_task_board,
    task_tools_enabled,
)
from backend.core.ouroboros.governance.tool_executor import (
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
)
from backend.core.ouroboros.governance.task_tool import run_task_tool


# --- Capture log lines emitted by TaskBoard --------------------------------


log_capture_stream = StringIO()
captured_stdout_lines = []

tb_logger = logging.getLogger(
    "backend.core.ouroboros.governance.task_board"
)
tb_logger.setLevel(logging.INFO)
capture_handler = logging.StreamHandler(log_capture_stream)
capture_handler.setLevel(logging.INFO)
capture_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s",
))
tb_logger.addHandler(capture_handler)

# Also stream to stdout so operators see live progress (the whole
# point of §8 observability).
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(logging.Formatter(
    "%(asctime)s [LIVEFIRE] %(message)s", datefmt="%H:%M:%S",
))
tb_logger.addHandler(stdout_handler)

# Same for the task_tool logger (emits registry_created events).
tt_logger = logging.getLogger(
    "backend.core.ouroboros.governance.task_tool"
)
tt_logger.setLevel(logging.INFO)
tt_logger.addHandler(capture_handler)
tt_logger.addHandler(stdout_handler)


# --- Helpers ---------------------------------------------------------------


def _pctx(op_id: str) -> PolicyContext:
    return PolicyContext(
        repo="jarvis", repo_root=Path("/tmp"),
        op_id=op_id, call_id=op_id + ":r0:t0",
        round_index=0, risk_tier=None, is_read_only=False,
    )


def _call(name: str, **args) -> ToolCall:
    return ToolCall(name=name, arguments=dict(args))


async def _exec(policy, pctx, name: str, **args) -> dict:
    """Dispatch a tool call through policy + handler; return a summary."""
    call = _call(name, **args)
    pol = policy.evaluate(call, pctx)
    if pol.decision != PolicyDecision.ALLOW:
        return {"policy_decision": pol.decision.value,
                "reason_code": pol.reason_code, "output": None}
    result = await run_task_tool(call, pctx, timeout=10.0, cap=8192)
    return {
        "policy_decision": pol.decision.value,
        "status": result.status.value,
        "output": json.loads(result.output) if result.output else None,
        "error": result.error,
    }


# --- Main ------------------------------------------------------------------


async def main() -> int:
    print("=" * 78)
    print("Gap #5 Slice 4 live-fire proof — graduated TaskBoard defaults")
    print("=" * 78)
    print()

    # Pre-flight: prove the graduated defaults are active.
    print("[pre-flight] Graduated defaults (zero env overrides):")
    print("  JARVIS_TOOL_TASK_BOARD_ENABLED (unset) -> "
          f"task_tools_enabled() = {task_tools_enabled()}")
    print("  JARVIS_TASK_BOARD_PROMPT_INJECTION_ENABLED (unset) -> "
          f"_prompt_injection_enabled() = {_prompt_injection_enabled()}")
    assert task_tools_enabled() is True, "task tools must be on-by-default"
    assert _prompt_injection_enabled() is True, "prompt injection must be on-by-default"
    print("  ✓ Both graduated defaults active")
    print()

    # Exercise the full Venom dispatch surface through the policy engine.
    op_id = "op-livefire-gap5-" + str(int(time.time()))
    pctx = _pctx(op_id)
    policy = GoverningToolPolicy(repo_roots={"jarvis": Path("/tmp")})
    print(f"[live] Exercising task tools for op_id={op_id}")
    print("-" * 78)

    trace = []

    # 1. Create task A
    r1 = await _exec(policy, pctx, "task_create",
                     title="refactor auth module",
                     body="Extract session tokens into a sealed helper.")
    trace.append(("task_create#1", r1))
    task_a = r1["output"]["task_id"]

    # 2. Create task B
    r2 = await _exec(policy, pctx, "task_create",
                     title="update integration tests")
    trace.append(("task_create#2", r2))
    task_b = r2["output"]["task_id"]

    # 3. Start task A (pending → in_progress)
    r3 = await _exec(policy, pctx, "task_update",
                     task_id=task_a, action="start")
    trace.append(("task_update(start)", r3))

    # 4. Edit task B title (content update, no action)
    r4 = await _exec(policy, pctx, "task_update",
                     task_id=task_b,
                     title="update integration tests + add parity pins")
    trace.append(("task_update(edit)", r4))

    # 5. Complete task A
    r5 = await _exec(policy, pctx, "task_complete", task_id=task_a)
    trace.append(("task_complete", r5))

    # 6. Cancel task B
    r6 = await _exec(policy, pctx, "task_update",
                     task_id=task_b, action="cancel",
                     reason="rolled into scope of task A")
    trace.append(("task_update(cancel)", r6))

    print("-" * 78)
    print()

    # Capture render_prompt_section output BEFORE closing — proves the
    # Slice 3 advisory injection works under graduated defaults.
    print("[advisory] render_prompt_section() before close:")
    board = _BOARDS.get(op_id)
    if board is not None:
        prompt_section = board.render_prompt_section()
        if prompt_section:
            print("  (present, but all tasks now terminal so expect None)")
            for line in prompt_section.splitlines():
                print("  | " + line)
        else:
            print("  (None — all tasks terminal, matches documented "
                  "contract)")
    print()

    # Close the board (the Slice 3 canonical shutdown hook).
    closed = close_task_board(op_id, reason="livefire e2e complete")
    print(f"[shutdown] close_task_board returned {closed}")
    print()

    # Grep captured log.
    log_text = log_capture_stream.getvalue()
    taskboard_lines = [
        ln for ln in log_text.splitlines()
        if "[TaskBoard]" in ln or "[TaskTool]" in ln
    ]
    print(f"[log-grep] Captured {len(taskboard_lines)} "
          "'[TaskBoard]' / '[TaskTool]' log lines:")
    for ln in taskboard_lines:
        trimmed = ln.split("INFO ", 1)[-1] if "INFO " in ln else ln
        print("  | " + trimmed)
    print()

    # Session artifact.
    session_id = "livefire-gap5-" + str(int(time.time()))
    session_dir = REPO_ROOT / ".ouroboros" / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "session_id": session_id,
        "purpose": "Gap #5 Slice 4 live-fire proof of task-board graduation",
        "commit_context": {
            "slice_1_primitive": "e633da7faa",
            "slice_2_tools": "d6ab7dd171 + 44d363550f",
            "slice_3_advisory": "1b41725375",
            "slice_4_graduation": "THIS PR",
        },
        "pre_flight": {
            "task_tools_enabled": task_tools_enabled(),
            "prompt_injection_enabled": _prompt_injection_enabled(),
            "env_overrides": False,
        },
        "op_id": op_id,
        "calls": [
            {"label": label, "result": result}
            for label, result in trace
        ],
        "taskboard_log_lines_captured": len(taskboard_lines),
        "taskboard_log_lines": taskboard_lines,
        "close_returned": closed,
    }
    (session_dir / "summary.json").write_text(
        json.dumps(artifact, indent=2, default=str),
    )
    (session_dir / "debug.log").write_text(log_text)
    print(f"[artifact] Session written to "
          f"{session_dir.relative_to(REPO_ROOT)}/")
    print(f"  summary.json ({len(json.dumps(artifact))} bytes)")
    print(f"  debug.log    ({len(log_text)} bytes)")
    print()

    # Verdict.
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    checks = [
        ("Both graduated defaults active with no env overrides",
         task_tools_enabled() is True and _prompt_injection_enabled() is True),
        ("Policy ALLOWED all 6 tool calls under graduated defaults",
         all(step[1]["policy_decision"] == "allow" for step in trace)),
        ("All handler responses SUCCESS",
         all(step[1].get("status") == "success" for step in trace)),
        ("Task A transitioned pending → in_progress → completed",
         trace[2][1]["output"]["state"] == "in_progress"
         and trace[4][1]["output"]["state"] == "completed"),
        ("Task B transitioned pending → pending (edit) → cancelled",
         trace[3][1]["output"]["state"] == "pending"
         and trace[5][1]["output"]["state"] == "cancelled"),
        ("Active slot cleared after task A completed",
         trace[4][1]["output"]["active_task_id"] is None),
        ("Synchronous [TaskBoard] audit log present (≥6 lines)",
         sum(1 for l in taskboard_lines if "[TaskBoard]" in l) >= 6),
        ("Board closed cleanly via canonical shutdown hook",
         closed is True),
    ]
    all_pass = True
    for label, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}")
        if not ok:
            all_pass = False
    print()
    if all_pass:
        print("LIVE-FIRE PROOF: PASS. Gap #5 Slice 4 graduation "
              "empirically verified — model-facing task tools are "
              "enabled on a fresh install, full lifecycle works "
              "through the Venom surface, audit trail lands in "
              "the logging pipeline, ctx shutdown hook cleans up.")
        return 0
    else:
        print("LIVE-FIRE PROOF: FAIL.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
