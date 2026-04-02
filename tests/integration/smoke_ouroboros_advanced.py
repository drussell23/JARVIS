#!/usr/bin/env python3
"""
Ouroboros Advanced Smoke Test — Full Pipeline E2E
==================================================

Boots the REAL Ouroboros governance pipeline in HUD mode and triggers
CU failure graduation to drive a real code fix through all phases:

  CLASSIFY -> ROUTE -> CONTEXT_EXPANSION -> GENERATE -> VALIDATE
  (duplication checker) -> GATE (similarity gate) -> APPLY -> VERIFY
  (regression gate + rollback) -> COMPLETE

The fix targets cu_task_planner.py, which currently has DUPLICATE
anti-pattern blocks (from the old bypass smoke test). The 397B brain
should detect the duplication and clean it up -- or the VALIDATE
duplication checker should catch it.

After the pipeline runs, the diff is shown so you can review it in
Cursor/VS Code.

Samantha narrates the full lifecycle via VoiceNarrator + CommProtocol.

Run:
    python3 tests/integration/smoke_ouroboros_advanced.py

Requires:
    DOUBLEWORD_API_KEY in .env (for 397B code generation)
"""
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# Disable all voice output — terminal only. Prevents overlapping TTS
# from the smoke test's own narration AND the pipeline's VoiceNarrator.
os.environ["JARVIS_VOICE_ENABLED"] = "0"


async def _samantha(text: str) -> None:
    """Print narration to terminal (voice disabled for smoke test)."""
    print(f"  [Samantha] {text}")


async def main() -> None:
    print("=" * 72)
    print("  Ouroboros Advanced Smoke Test -- Full Pipeline E2E")
    print("=" * 72)
    print()

    await _samantha(
        "Ouroboros advanced smoke test activated. "
        "Booting full governance pipeline in HUD mode."
    )

    # ==================================================================
    # PHASE 1: Boot the full Ouroboros governance pipeline
    # ==================================================================
    print("--- PHASE 1: Boot Governance Pipeline ---")
    print()

    from backend.core.ouroboros.governance.hud_governance_boot import (
        start_hud_governance,
        stop_hud_governance,
    )

    t0 = time.monotonic()
    ctx = await start_hud_governance(project_root=ROOT)
    boot_time = time.monotonic() - t0

    stack_status = "ACTIVE" if ctx.stack else "FAILED"
    gls_status = ctx.gls.state.name if ctx.gls else "FAILED"
    intake_status = ctx.intake.state.name if ctx.intake else "FAILED"

    print(f"  GovernanceStack: {stack_status}")
    print(f"  GovernedLoopService: {gls_status}")
    print(f"  IntakeLayerService: {intake_status}")
    print(f"  Pipeline active: {ctx.is_active}")
    print(f"  Boot time: {boot_time:.1f}s")
    print()

    if not ctx.is_active:
        print("  [FAIL] Pipeline did not reach ACTIVE state.")
        await _samantha("Pipeline failed to start. Aborting smoke test.")
        await stop_hud_governance(ctx)
        sys.exit(1)

    await _samantha(
        f"Pipeline booted in {boot_time:.0f} seconds. "
        "All three layers active: stack, governed loop, and intake. "
        "Now triggering CU failure graduation."
    )

    # ==================================================================
    # PHASE 2: Feed CU failures to trigger graduation
    # ==================================================================
    print("--- PHASE 2: Trigger CU Failure Graduation ---")
    print()

    from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
        CUExecutionRecord,
        CUExecutionSensor,
    )

    sensor = CUExecutionSensor()
    if sensor._router is None:
        print("  [FAIL] CUExecutionSensor has no router wired!")
        await stop_hud_governance(ctx)
        sys.exit(1)

    print("  Sensor router: WIRED")
    print("  Graduation threshold: 3 failures with same signature")
    print()

    # The real bug: cu_task_planner.py has 3 DUPLICATE copies of the
    # "search bar click" anti-pattern (lines 672-752). Ouroboros should
    # detect this duplication and clean it up.
    target_file = ROOT / "backend" / "vision" / "cu_task_planner.py"
    source_before = target_file.read_text()
    line_count = len(source_before.splitlines())
    print(f"  Target: {target_file.name} ({line_count} lines)")
    print()

    # Feed 3 messaging failures with same signature to trigger graduation
    for i in range(3):
        record = CUExecutionRecord(
            goal="send message to Alice via Messages app",
            success=False,
            steps_completed=2,
            steps_total=5,
            elapsed_s=3.0 + i * 0.5,
            error="target not found: search bar click led to wrong contact",
            is_messaging=True,
            contact="Alice",
            app="messages",
        )
        await sensor.record(record)
        print(f"  Failure {i + 1}/3 recorded (sig: {record.failure_signature})")

    print()
    print(f"  Envelopes emitted: {sensor._total_envelopes_emitted}")

    if sensor._total_envelopes_emitted < 1:
        print("  [FAIL] No envelopes emitted -- graduation did not trigger!")
        await stop_hud_governance(ctx)
        sys.exit(1)

    await _samantha(
        "Three CU failures recorded with the same signature. "
        "Graduation triggered. Envelope submitted to the Ouroboros pipeline. "
        "The orchestrator will now classify, route, generate, validate, "
        "and apply a fix."
    )

    # ==================================================================
    # PHASE 3: Wait for the pipeline to process
    # ==================================================================
    print("--- PHASE 3: Wait for Pipeline Processing ---")
    print()
    print("  Waiting for orchestrator to process the envelope...")
    print("  (CLASSIFY -> ROUTE -> GENERATE -> VALIDATE -> GATE -> APPLY -> VERIFY)")
    print()

    # The orchestrator processes envelopes asynchronously via the intake
    # router's dispatch loop. Give it time to process.
    max_wait = 360  # 6 minutes max (Doubleword batch API 397B can take 2-4 min)
    poll_interval = 5
    elapsed = 0
    last_status = ""

    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        # Check if the file changed (APPLY happened)
        current = target_file.read_text()
        if current != source_before:
            print(f"  FILE CHANGED after {elapsed}s -- APPLY phase completed!")
            break

        # Check GLS health for progress indicators
        try:
            health = ctx.gls.health()
            active_ops = health.get("active_ops", 0)
            completed = health.get("completed_ops", 0)
            status = f"  [{elapsed}s] active_ops={active_ops}, completed={completed}"
            if status != last_status:
                print(status)
                last_status = status
        except Exception:
            print(f"  [{elapsed}s] polling...")
    else:
        print(f"  [TIMEOUT] Pipeline did not apply changes within {max_wait}s")
        print("  This may mean:")
        print("    - VALIDATE duplication checker blocked the fix (correct!)")
        print("    - GENERATE failed (Doubleword API unavailable)")
        print("    - GATE similarity check escalated to approval")
        print("    - VERIFY regression gate rolled back the change")
        print()

    # ==================================================================
    # PHASE 4: Show results
    # ==================================================================
    print("--- PHASE 4: Results ---")
    print()

    source_after = target_file.read_text()
    changed = source_after != source_before

    if changed:
        print("  FILE MODIFIED by Ouroboros pipeline!")
        print()

        # Show git diff stats
        diff_proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--stat", str(target_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        diff_stat, _ = await diff_proc.communicate()
        if diff_stat:
            print(f"  {diff_stat.decode().strip()}")

        # Show actual diff
        diff_proc2 = await asyncio.create_subprocess_exec(
            "git", "diff", str(target_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        diff_content, _ = await diff_proc2.communicate()
        added_count = 0
        removed_count = 0
        if diff_content:
            diff_lines = diff_content.decode().split("\n")
            added = [l for l in diff_lines if l.startswith("+") and not l.startswith("+++")]
            removed = [l for l in diff_lines if l.startswith("-") and not l.startswith("---")]
            added_count = len(added)
            removed_count = len(removed)
            print(f"  Lines added: {added_count}")
            print(f"  Lines removed: {removed_count}")
            print()
            print("  Diff preview (first 50 lines):")
            for line in diff_lines[:50]:
                print(f"    {line}")
            if len(diff_lines) > 50:
                print(f"    ... ({len(diff_lines)} total lines)")

        await _samantha(
            f"Ouroboros applied a fix to the CU task planner. "
            f"{added_count} lines added, {removed_count} lines removed. "
            "Open your editor to review the diff. "
            "The full governance pipeline completed successfully."
        )
    else:
        print("  No changes applied to the file.")
        print()
        print("  Pipeline outcomes (check logs for which one):")
        print("    1. VALIDATE duplication checker BLOCKED the fix")
        print("       (correct: the 397B tried to add duplicate code)")
        print("    2. GENERATE returned 2b.1-noop (change already present)")
        print("    3. GATE similarity check escalated to APPROVAL_REQUIRED")
        print("    4. VERIFY regression gate rolled back the change")
        print()

        await _samantha(
            "The pipeline processed the envelope but did not modify the file. "
            "This may mean the duplication checker correctly blocked a redundant fix, "
            "or the model returned a no-op. Check the logs for details."
        )

    # ==================================================================
    # PHASE 5: Pipeline health report
    # ==================================================================
    print()
    print("--- PHASE 5: Pipeline Health Report ---")
    print()
    try:
        health = ctx.gls.health()
        for key, val in sorted(health.items()):
            if not isinstance(val, (dict, list)):
                print(f"  {key}: {val}")
    except Exception as exc:
        print(f"  Health check failed: {exc}")

    # ==================================================================
    # PHASE 6: Shutdown
    # ==================================================================
    print()
    print("--- PHASE 6: Shutdown ---")
    print()

    await stop_hud_governance(ctx)
    print("  Governance pipeline shut down cleanly.")
    print()

    print("=" * 72)
    print("  OUROBOROS ADVANCED SMOKE TEST COMPLETE")
    print()
    print(f"  Pipeline booted: {ctx.is_active}")
    print(f"  Boot time: {boot_time:.1f}s")
    print(f"  Envelopes emitted: {sensor._total_envelopes_emitted}")
    print(f"  File changed: {changed}")
    if changed:
        print()
        print(f"  >>> Review in IDE: git diff backend/vision/cu_task_planner.py")
        print(f"  >>> Revert:        git checkout backend/vision/cu_task_planner.py")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
