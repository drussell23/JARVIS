#!/usr/bin/env python3
"""attach_livefire — behavioral proof of the multi-modal pipeline.

Drives a single ``/attach``-style op through the full Ouroboros stack
to a live Anthropic API call and captures Claude's response text from
the session debug.log.

Usage
-----
    PYTHONPATH=. python3 scripts/attach_livefire.py \\
        <pdf_path> <prompt_text>

The harness's own ``run()`` is a blocking lifecycle loop (boot →
await shutdown_event → teardown). We kick it off as a task, wait for
intake to reach ACTIVE, submit the envelope, poll the session log for
Claude's response around our op_id, then trip the shutdown event to
let the harness clean up.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path


def _load_env_files() -> None:
    for path in (Path(".env"), Path("backend/.env")):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v


async def _wait_for_intake_active(harness, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        intake = getattr(harness, "_intake_service", None)
        if intake is not None:
            router = getattr(intake, "_router", None)
            if router is not None:
                return True
        await asyncio.sleep(0.5)
    return False


async def main() -> int:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <pdf_path> <prompt>", file=sys.stderr)
        return 2

    pdf_path = os.path.abspath(sys.argv[1])
    prompt = sys.argv[2]
    if not os.path.isfile(pdf_path):
        print(f"[livefire] FATAL: PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    _load_env_files()
    os.environ["JARVIS_PROVIDER_OVERRIDE"] = "claude-api"
    os.environ["JARVIS_GENERATE_ATTACHMENTS_ENABLED"] = "true"
    os.environ.setdefault("JARVIS_VISION_SENSOR_ENABLED", "false")
    os.environ.setdefault("JARVIS_GITHUB_ISSUE_SENSOR_ENABLED", "false")
    os.environ.setdefault("JARVIS_RUNTIME_HEALTH_SENSOR_ENABLED", "false")
    os.environ.setdefault("JARVIS_OPPORTUNITY_MINER_SENSOR_ENABLED", "false")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[livefire] FATAL: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness, HarnessConfig,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )

    repo_root = Path(".").resolve()
    config = HarnessConfig(
        repo_path=repo_root,
        cost_cap_usd=0.50,
        idle_timeout_s=240,
    )
    harness = BattleTestHarness(config)

    print(f"[livefire] launching harness.run() in task …")
    run_task = asyncio.create_task(harness.run())

    # Wait for intake service + router to become available.
    ok = await _wait_for_intake_active(harness, timeout_s=45.0)
    if not ok:
        print("[livefire] FATAL: intake service never became active", file=sys.stderr)
        _shutdown = getattr(harness, "_shutdown_event", None)
        if _shutdown is not None:
            _shutdown.set()
        await asyncio.wait_for(run_task, timeout=30)
        return 3
    print("[livefire] intake active — submitting envelope")

    # Build envelope — same shape as /attach REPL produces.
    env = make_envelope(
        source="voice_human",
        description=prompt,
        target_files=(),
        repo="jarvis",
        confidence=0.95,
        urgency="critical",     # routes to Claude direct (IMMEDIATE)
        evidence={
            "user_attachments": [{"path": pdf_path, "kind": "user_provided"}],
            "attach_source": "livefire_script",
        },
        requires_human_ack=False,
    )
    target_op = env.causal_id
    print(f"[livefire] op: causal_id={target_op}")

    router = harness._intake_service._router  # type: ignore[attr-defined]
    verdict = await router.ingest(env)
    print(f"[livefire] router.ingest → {verdict}")

    # Locate session log.
    sessions = sorted(
        Path(".ouroboros/sessions").glob("bt-*"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    session_log = sessions[0] / "debug.log" if sessions else None
    if session_log is None:
        print("[livefire] FATAL: no session dir")
        return 3
    print(f"[livefire] watching {session_log}")

    # Poll the log for our op's breadcrumbs + Claude response.
    hoist_line: str | None = None
    multi_modal_line: str | None = None
    response_window: str | None = None
    terminal_seen = False
    deadline = time.time() + 240

    while time.time() < deadline:
        await asyncio.sleep(2)
        if not session_log.exists():
            continue
        try:
            content = session_log.read_text(errors="replace")
        except OSError:
            continue

        if hoist_line is None:
            m = re.search(
                rf"\[IntakeRouter\] attachments_hoisted op={re.escape(target_op)}[^\n]*",
                content,
            )
            if m:
                hoist_line = m.group(0)
                print(f"[livefire] ✓ hoisted")

        if multi_modal_line is None:
            m = re.search(
                rf"\[ClaudeProvider\] multi_modal op={re.escape(target_op)}[^\n]*",
                content,
            )
            if m:
                multi_modal_line = m.group(0)
                print(f"[livefire] ✓ multi_modal shipped to Claude")

        # Look for our op's generation response. Claude's text often
        # appears inside a candidate_generator log line as rationale or
        # raw_response, or inside the stream_renderer's final dump.
        if not terminal_seen and (
            f"op={target_op}" in content or target_op in content
        ):
            if re.search(
                r"(POSTMORTEM|terminal_phase|stream_renderer.*op=|"
                r"full_content|rationale|raw_response|first_token_ms)",
                content,
            ):
                terminal_seen = True

        # Behavioral proof: Claude references the seeded phrase from the PDF.
        lc = content.lower()
        if any(k in lc for k in ("division by zero", "line 42", "compute.py", "divbyzero")):
            # Grab a window around the first match.
            idx = -1
            for needle in ("division by zero", "line 42", "compute.py"):
                idx = lc.find(needle)
                if idx >= 0:
                    break
            if idx >= 0:
                window_start = max(0, idx - 500)
                response_window = content[window_start : idx + 800]
                print(f"[livefire] ✓ Claude response references PDF text")
                break

    # Trigger shutdown.
    _shutdown = getattr(harness, "_shutdown_event", None)
    if _shutdown is not None:
        _shutdown.set()
    try:
        await asyncio.wait_for(run_task, timeout=45)
    except asyncio.TimeoutError:
        print("[livefire] warning: harness shutdown exceeded 45s; cancelling")
        run_task.cancel()
        try:
            await run_task
        except Exception:  # noqa: BLE001
            pass

    print()
    print("═══════════════════════════════════════════════════════════════════")
    print("  ATTACH LIVE-FIRE REPORT")
    print("═══════════════════════════════════════════════════════════════════")
    print(f"target_op_id:    {target_op}")
    print(f"pdf:             {pdf_path}")
    print(f"prompt:          {prompt[:120]}{'…' if len(prompt) > 120 else ''}")
    print()
    print(f"hoisted:         {'✓ ' + hoist_line if hoist_line else '✗ not observed'}")
    print()
    print(f"multi_modal:     {'✓ ' + multi_modal_line if multi_modal_line else '✗ not observed'}")
    print()
    if response_window:
        print("CLAUDE RESPONSE WINDOW (contains PDF-referencing text):")
        print("-------------------------------------------------------------------")
        print(response_window)
        print("-------------------------------------------------------------------")
        return 0
    else:
        print("CLAUDE RESPONSE: not captured within 4-min budget.")
        print()
        print("Last 50 lines of session log for diagnosis:")
        print("-------------------------------------------------------------------")
        try:
            lines = session_log.read_text(errors="replace").splitlines()
            for ln in lines[-50:]:
                print(ln)
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
