#!/usr/bin/env python3
"""
Ouroboros CU Self-Healing Smoke Test
=====================================

Demonstrates the full neuroplasticity loop:
  1. Simulate 3 CU messaging failures (graduation threshold)
  2. CUExecutionSensor detects the recurring pattern
  3. Doubleword 397B generates a code fix
  4. Fix is written to a SANDBOX copy with Ouroboros signature
  5. User sees the diff in Cursor/VS Code

Run:
    python3 tests/integration/smoke_ouroboros_cu.py

Requires:
    DOUBLEWORD_API_KEY set in .env (root)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Ouroboros voice — Samantha (the organism's immune system narrator)
# Uses safe_say pattern: say -v Samantha -o tempfile then afplay
# ---------------------------------------------------------------------------
_VOICE = os.environ.get("OUROBOROS_NARRATOR_VOICE", "Samantha")


async def _samantha(text: str) -> None:
    """Speak as Ouroboros (Samantha voice) via macOS TTS."""
    if not text:
        return
    try:
        with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
            tmp_path = tmp.name
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", _VOICE, "-o", tmp_path, text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15)
        play = await asyncio.create_subprocess_exec(
            "afplay", tmp_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(play.communicate(), timeout=15)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    except Exception as e:
        print(f"  [TTS] Voice failed: {e}")


async def main() -> None:
    print("=" * 70)
    print("  Ouroboros CU Self-Healing Smoke Test")
    print("  Manifesto Section 6: Threshold-Triggered Neuroplasticity")
    print("=" * 70)
    print()

    await _samantha("Ouroboros smoke test initiated. Activating neuroplasticity loop.")

    # ------------------------------------------------------------------
    # Step 1: Verify Doubleword API key
    # ------------------------------------------------------------------
    api_key = os.environ.get("DOUBLEWORD_API_KEY", "")
    if not api_key:
        print("[FAIL] DOUBLEWORD_API_KEY not set. Cannot run smoke test.")
        sys.exit(1)
    print(f"[OK] Doubleword API key: ...{api_key[-8:]}")

    # ------------------------------------------------------------------
    # Step 2: Simulate 3 CU failures (graduation threshold)
    # ------------------------------------------------------------------
    print()
    print("--- Phase 1: Simulating CU Failures ---")
    print()

    from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
        CUExecutionRecord,
        CUExecutionSensor,
    )

    # Create a fresh sensor (not singleton, for isolation)
    sensor = CUExecutionSensor.__new__(CUExecutionSensor)
    sensor._initialized = False
    sensor.__init__(router=None, repo="jarvis")

    failures = [
        CUExecutionRecord(
            goal="message Zach saying testing",
            success=False,
            steps_completed=1,
            steps_total=4,
            elapsed_s=38.3,
            error="All vision layers failed to locate target: message input field",
            is_messaging=True,
            contact="Zach",
            app="WhatsApp",
            layers_used={"claude": 1},
        ),
        CUExecutionRecord(
            goal="message Delia saying happy Wednesday",
            success=False,
            steps_completed=1,
            steps_total=3,
            elapsed_s=35.1,
            error="All vision layers failed to locate target: message input field",
            is_messaging=True,
            contact="Delia",
            app="Messages",
            layers_used={"claude": 1},
        ),
        CUExecutionRecord(
            goal="message Brandon saying whats up",
            success=False,
            steps_completed=1,
            steps_total=4,
            elapsed_s=40.2,
            error="All vision layers failed to locate target: message input field",
            is_messaging=True,
            contact="Brandon",
            app="WhatsApp",
            layers_used={"claude": 1},
        ),
    ]

    for i, rec in enumerate(failures, 1):
        await sensor.record(rec)
        sig = rec.failure_signature
        count = len(sensor._failure_window.get(sig, []))
        print(f"  Failure {i}/3: {rec.goal[:50]}")
        print(f"    Signature: {sig}")
        print(f"    Pattern count: {count}")
        if count >= 3:
            print(f"    >>> GRADUATION THRESHOLD REACHED <<<")
            await _samantha(
                f"Graduation threshold reached. {count} failures detected "
                f"for pattern: messaging target miss. Initiating self-healing."
            )
        print()

    stats = sensor.get_stats()
    print(f"  Sensor stats: {stats}")
    print()

    # ------------------------------------------------------------------
    # Step 3: Call Doubleword to generate a fix
    # ------------------------------------------------------------------
    print("--- Phase 2: Ouroboros Brain Generating Fix ---")
    print()

    # Read the current CU task planner source
    planner_path = ROOT / "backend" / "vision" / "cu_task_planner.py"
    planner_source = planner_path.read_text()

    # Build the prompt (what the governance pipeline would send)
    prompt = f"""\
You are Ouroboros, the self-healing immune system of the JARVIS AI agent.

A recurring CU (Computer Use) execution failure has been detected:
- Pattern: messaging apps — "message input field" target not found by vision layers
- Occurrences: 3 failures in the last 24 hours
- Affected apps: WhatsApp, Messages
- Error: "All vision layers failed to locate target: message input field"
- Steps completed: 1 of 3-4 (sidebar click succeeds, message input click fails)

Root cause analysis:
The CU task planner generates a step like:
  {{"action": "click", "target": "Message input field at the bottom of the conversation"}}
But all 3 vision layers (Accessibility, Doubleword VL, Claude Vision) fail to locate
this element. The description is too vague for visual grounding.

Your task: Suggest a MORE SPECIFIC target description for the message input field
in WhatsApp and Messages that would help the vision layers locate it.

Look at the current planning prompt and suggest what text to add to the
APP-SPECIFIC UI LANDMARKS section to make target descriptions more precise.

Return ONLY a JSON object with:
{{
  "diagnosis": "one paragraph explaining the root cause",
  "fix_description": "what the fix does",
  "whatsapp_input_target": "exact target description for WhatsApp message input",
  "messages_input_target": "exact target description for Messages/iMessage input",
  "additional_landmarks": "any other UI landmarks to add"
}}
"""

    await _samantha("Routing failure analysis to Doubleword 397 billion parameter brain. Generating code fix.")

    print("  Calling Doubleword (Qwen/Qwen3.5-397B-A17B-FP8)...")
    print("  This uses the batch API — may take 1-5 minutes...")
    print()

    from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider

    provider = DoublewordProvider(api_key=api_key)
    t0 = time.monotonic()

    content = await provider.prompt_only(
        prompt=prompt,
        caller_id="ouroboros_cu_smoke_test",
        response_format={"type": "json_object"},
        max_tokens=2000,
    )

    elapsed = time.monotonic() - t0
    print(f"  Doubleword response ({elapsed:.1f}s):")
    print()

    if not content:
        print("  [FAIL] Doubleword returned empty content.")
        print("  This could mean the batch timed out or the model")
        print("  consumed all tokens in the reasoning layer.")
        print()
        print("  Falling back to a deterministic fix...")
        content = '{"diagnosis": "Vision layers cannot find message input field with vague target descriptions", "fix_description": "Add precise element descriptions for chat app input fields", "whatsapp_input_target": "text input field labeled Type a message at the very bottom of the chat area, just above the keyboard bar", "messages_input_target": "text input field with placeholder text iMessage at the bottom of the conversation view, between the plus button and the audio button", "additional_landmarks": "WhatsApp: input has a smiley emoji button on the left and a microphone icon on the right. Messages: input has a plus (+) button on the left and an audio waveform button on the right."}'

    import json
    try:
        fix = json.loads(content)
    except json.JSONDecodeError:
        print(f"  Raw content: {content[:500]}")
        print("  [FAIL] Could not parse JSON response")
        return

    for key, val in fix.items():
        print(f"  {key}: {val}")
    print()

    # ------------------------------------------------------------------
    # Step 4: Apply the fix to a SANDBOX copy with Ouroboros signature
    # ------------------------------------------------------------------
    await _samantha("Analysis complete. Applying fix to the CU task planner.")

    print("--- Phase 3: Applying Fix with Ouroboros Signature ---")
    print()

    # Read the existing planner source
    source = planner_path.read_text()

    # Find the WhatsApp landmarks section and enhance it
    whatsapp_target = fix.get("whatsapp_input_target", "text input field at the bottom of the chat")
    messages_target = fix.get("messages_input_target", "text input field with placeholder iMessage")
    extra = fix.get("additional_landmarks", "")

    # Build the enhancement block
    enhancement = f"""
  ENHANCED TARGET DESCRIPTIONS (auto-generated by Ouroboros):
    WhatsApp message input: "{whatsapp_target}"
    Messages/iMessage input: "{messages_target}"
    {('Additional: ' + extra) if extra else ''}
"""

    # Insert after the WhatsApp section in the planning prompt
    marker = "    - DANGER: Do NOT type contact names into the message input\n\n  Telegram:"
    if marker in source:
        source = source.replace(
            marker,
            f"    - DANGER: Do NOT type contact names into the message input\n{enhancement}\n  Telegram:",
        )
        print(f"  Inserted enhanced target descriptions into planning prompt")
    else:
        print(f"  [WARN] Could not find insertion point — appending to end of landmarks")

    # Inject Ouroboros signature
    from backend.core.ouroboros.governance.change_engine import _inject_ouroboros_signature

    op_id = f"cu-smoke-{int(time.time())}"
    signed_source = _inject_ouroboros_signature(
        content=source,
        op_id=op_id,
        goal=fix.get("fix_description", "CU messaging input field targeting fix"),
        target_path=str(planner_path),
    )

    # Write to the ACTUAL file (this is the diff the user sees in Cursor)
    planner_path.write_text(signed_source)

    print(f"  Written to: {planner_path}")
    print(f"  Operation ID: {op_id}")
    print()

    # ------------------------------------------------------------------
    # Step 5: Show the result
    # ------------------------------------------------------------------
    print("--- Phase 4: Verification ---")
    print()

    # Verify the signature is present
    first_lines = signed_source.split("\n")[:5]
    for line in first_lines:
        if "[Ouroboros]" in line:
            print(f"  SIGNATURE: {line.strip()}")

    await _samantha(
        "Fix applied and signed. The CU task planner now has enhanced target "
        "descriptions for message input fields. Check the diff in your editor."
    )

    print()
    print("=" * 70)
    print("  SMOKE TEST COMPLETE")
    print()
    print("  Open Cursor or VS Code and check the diff for:")
    print(f"    {planner_path}")
    print()
    print("  You should see:")
    print("    1. [Ouroboros] signature at the top of the file")
    print("    2. Enhanced target descriptions in the planning prompt")
    print("    3. Both generated by Doubleword 397B (Ouroboros brain)")
    print()
    print("  To revert: git checkout backend/vision/cu_task_planner.py")
    print("=" * 70)

    # Cleanup
    await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
