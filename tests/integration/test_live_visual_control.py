#!/usr/bin/env python3
"""
JARVIS Live Visual Control Demo
================================
Watch JARVIS physically control WhatsApp and compose a Gmail in real time.

Architecture:
  - Step 1: Vision model availability check (J-Prime GCP port 8001 / Claude API fallback)
  - Step 2: WhatsApp native control via NativeAppControlAgent (vision-action loop)
  - Step 3: Gmail compose via VisualBrowserAgent (Playwright + vision loop)
  - Step 4: Summary table — what succeeded, what was simulated

J-Prime may be offline.  The demo handles all three states gracefully:
  - J-Prime available  : live vision loops for both agents
  - Claude available   : live vision loops via paid API fallback
  - Neither available  : full narrated simulation walkthrough + API email draft

Run:
    python3 tests/integration/test_live_visual_control.py

No env var prerequisites — vision availability is checked dynamically at startup.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap — allow imports from the repo root and backend/
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "backend"))

# ---------------------------------------------------------------------------
# TTS — sequential speech queue (the lock pattern: never overlapping)
# ---------------------------------------------------------------------------

_tts_lock: Optional[asyncio.Lock] = None


def _get_tts_lock() -> asyncio.Lock:
    global _tts_lock
    if _tts_lock is None:
        _tts_lock = asyncio.Lock()
    return _tts_lock


async def say(text: str) -> None:
    """Speak text aloud sequentially. Returns only after the utterance finishes."""
    async with _get_tts_lock():
        print(f"  [JARVIS] {text}")
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "Daniel", "-r", "190", text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def banner(title: str) -> None:
    width = 72
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width)


def sub_banner(title: str) -> None:
    print(f"\n  {'─' * 66}")
    print(f"  {title}")
    print(f"  {'─' * 66}")


def _status_tag(ok: bool, label_ok: str = "OK", label_fail: str = "FAIL") -> str:
    return f"[ {label_ok:^8} ]" if ok else f"[ {label_fail:^8} ]"


def _print_action_log(actions: List[Dict[str, Any]]) -> None:
    if not actions:
        return
    sub_banner("Action Log")
    for a in actions:
        step = a.get("step", "?")
        atype = a.get("action_type", "?")
        msg = a.get("message", "")
        detail = a.get("detail", {})
        detail_str = (
            ", ".join(f"{k}={v}" for k, v in detail.items()) if detail else ""
        )
        detail_part = f"  [{detail_str}]" if detail_str else ""
        print(f"    Step {step:>2}  [{atype:<8}]{detail_part}  {msg}")


# ---------------------------------------------------------------------------
# Chrome tab helper — reuse an existing tab if Gmail is already open
# ---------------------------------------------------------------------------

async def _activate_chrome_tab_if_open(domain: str) -> bool:
    """
    Attempt to focus an existing Chrome tab whose URL contains *domain*.
    Returns True when a matching tab was found and activated.
    Uses asyncio.create_subprocess_exec (not shell=True) to avoid injection.
    """
    script = (
        'tell application "Google Chrome"\n'
        '    repeat with w in windows\n'
        '        set idx to 0\n'
        '        repeat with t in tabs of w\n'
        '            set idx to idx + 1\n'
        f'           if URL of t contains "{domain}" then\n'
        '                set active tab index of w to idx\n'
        '                set index of w to 1\n'
        '                activate\n'
        '                return "found"\n'
        '            end if\n'
        '        end repeat\n'
        '    end repeat\n'
        '    return "not_found"\n'
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return stdout.decode(errors="replace").strip() == "found"
    except Exception:
        return False


async def open_url_smart(url: str) -> str:
    """
    Open *url* in the default browser, reusing an existing Chrome tab when possible.
    Returns 'reused', 'opened', or 'failed'.
    """
    from urllib.parse import urlparse

    domain = urlparse(url).netloc or url
    if await _activate_chrome_tab_if_open(domain):
        return "reused"

    proc = await asyncio.create_subprocess_exec(
        "open", url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return "opened" if proc.returncode == 0 else "failed"


# ---------------------------------------------------------------------------
# STEP 1 — Vision model availability check
# ---------------------------------------------------------------------------

async def step_1_vision_check() -> Tuple[bool, bool]:
    """
    Probe both vision backends.

    Returns:
        (jprime_ok, claude_ok)
    """
    banner("STEP 1: Vision Model Availability Check")
    await say(
        "First, let me check which vision models are available. "
        "I need at least one to run the live control loops."
    )

    # --- J-Prime (GCP LLaVA on port 8001) ---
    jprime_ok = False
    jprime_detail = "unreachable"
    try:
        from backend.core.prime_client import get_prime_client

        client = await asyncio.wait_for(get_prime_client(), timeout=5.0)
        jprime_ok = await asyncio.wait_for(client.get_vision_health(), timeout=5.0)
        jprime_detail = "healthy" if jprime_ok else "unhealthy"
    except asyncio.TimeoutError:
        jprime_detail = "timed out (5 s)"
    except Exception as exc:
        jprime_detail = f"error: {type(exc).__name__}"

    # --- Claude API (paid fallback) ---
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    claude_ok = bool(anthropic_key)
    claude_detail = "API key present" if claude_ok else "ANTHROPIC_API_KEY not set"

    sub_banner("Vision Backend Status")
    print(
        f"  {_status_tag(jprime_ok, 'ONLINE', 'OFFLINE')}  "
        f"J-Prime (GCP 136.113.252.164:8001)  {jprime_detail}"
    )
    print(
        f"  {_status_tag(claude_ok, 'ONLINE', 'OFFLINE')}  "
        f"Claude API (paid fallback)           {claude_detail}"
    )
    print()

    vision_available = jprime_ok or claude_ok
    if vision_available:
        backend = "J-Prime" if jprime_ok else "Claude API"
        print(f"  Vision backend selected: {backend}")
        await say(
            f"Vision model available via {backend}. "
            "Running live control loops."
        )
    else:
        print("  No vision backend available — running in simulation mode.")
        await say(
            "No vision model is available right now. "
            "I will walk you through a detailed simulation of every step I would take."
        )

    return jprime_ok, claude_ok


# ---------------------------------------------------------------------------
# WhatsApp install-check helper
# ---------------------------------------------------------------------------

async def _check_whatsapp_installed() -> bool:
    """Return True if WhatsApp.app is present on this Mac."""
    try:
        from backend.neural_mesh.agents.app_inventory_service import AppInventoryService

        svc = AppInventoryService()
        await svc.on_initialize()
        result = await svc.execute_task({"action": "check_app", "app_name": "WhatsApp"})
        return bool(result.get("found", False))
    except Exception:
        pass

    # Filesystem fallback — common install locations
    for candidate in (
        Path("/Applications/WhatsApp.app"),
        Path(Path.home() / "Applications" / "WhatsApp.app"),
    ):
        if candidate.exists():
            return True
    return False


# ---------------------------------------------------------------------------
# STEP 2 — WhatsApp native control
# ---------------------------------------------------------------------------

_WHATSAPP_GOAL = "Open the search bar and type 'Zach'"


async def step_2_whatsapp(vision_available: bool) -> Dict[str, Any]:
    """
    Control WhatsApp via NativeAppControlAgent.
    Falls back to a narrated simulation when no vision model is available.
    """
    banner("STEP 2: WhatsApp Native Control (NativeAppControlAgent)")

    whatsapp_installed = await _check_whatsapp_installed()
    install_tag = _status_tag(whatsapp_installed, "INSTALLED", "MISSING")
    print(f"  {install_tag}  WhatsApp.app")

    # ── Simulation path ──────────────────────────────────────────────────────
    if not vision_available:
        await say(
            "J-Prime vision server is offline. "
            "Here is what I would do if it were available."
        )
        sub_banner("Simulation: NativeAppControlAgent — WhatsApp")
        _print_whatsapp_simulation(whatsapp_installed)
        return {
            "mode": "simulated",
            "whatsapp_installed": whatsapp_installed,
            "success": None,
            "steps_taken": 0,
            "actions": [],
            "final_message": "Simulated — no vision model available",
        }

    # ── App not installed ─────────────────────────────────────────────────────
    if not whatsapp_installed:
        await say(
            "WhatsApp is not installed on this Mac. "
            "In production I would route this to the browser tier and open web.whatsapp.com. "
            "Skipping native control for now."
        )
        sub_banner("Skipped — WhatsApp not installed")
        print("  Production fallback: https://web.whatsapp.com — search for 'Zach' via browser tier.")
        return {
            "mode": "skipped",
            "whatsapp_installed": False,
            "success": False,
            "steps_taken": 0,
            "actions": [],
            "final_message": "WhatsApp not installed — would fall back to browser tier",
        }

    # ── Live vision loop ──────────────────────────────────────────────────────
    await say(
        "WhatsApp is installed. Activating it now and starting the vision-action loop."
    )

    result: Dict[str, Any] = {
        "mode": "live",
        "whatsapp_installed": True,
        "success": False,
        "steps_taken": 0,
        "actions": [],
        "final_message": "Unknown error",
    }

    try:
        from backend.neural_mesh.agents.native_app_control_agent import NativeAppControlAgent

        agent = NativeAppControlAgent()
        await agent.on_initialize()

        print(f"\n  Goal: {_WHATSAPP_GOAL}")
        print("  Running vision-action loop...\n")

        t0 = time.monotonic()
        agent_result = await agent.execute_task({
            "action": "interact_with_app",
            "app_name": "WhatsApp",
            "goal": _WHATSAPP_GOAL,
        })
        elapsed = time.monotonic() - t0

        result["success"] = agent_result.get("success", False)
        result["steps_taken"] = agent_result.get("steps_taken", 0)
        result["actions"] = agent_result.get("actions", [])
        result["final_message"] = agent_result.get("final_message", "")
        result["elapsed_s"] = round(elapsed, 2)

        _print_action_log(agent_result.get("actions", []))

        success_tag = _status_tag(result["success"], "SUCCESS", "FAILED")
        print(f"\n  {success_tag}  {result['final_message']}")
        print(f"  Steps taken: {result['steps_taken']}  |  Wall time: {elapsed:.1f}s")

        if result["success"]:
            await say(
                f"WhatsApp control complete after {result['steps_taken']} steps. "
                "Zach's conversation is ready."
            )
        else:
            await say(
                f"WhatsApp loop finished in {result['steps_taken']} steps. "
                f"{result['final_message']}"
            )

    except Exception as exc:
        result["final_message"] = f"Agent error: {exc}"
        result["error"] = str(exc)
        print(f"\n  [ ERROR   ]  {exc}")
        await say(f"WhatsApp control encountered an error: {type(exc).__name__}")

    return result


def _print_whatsapp_simulation(installed: bool) -> None:
    install_note = (
        "WhatsApp is installed — native tier applies"
        if installed
        else "WhatsApp is NOT installed — browser tier (web.whatsapp.com) would be used"
    )
    steps = [
        f"Precondition: {install_note}",
        "osascript: tell application 'WhatsApp' to activate",
        "asyncio.sleep(2.0)  — wait for window to render",
        "screencapture -x -C /tmp/jarvis_nac_step1.png",
        "Send screenshot + goal to J-Prime (port 8001)",
        "  J-Prime response: { action_type: 'key', detail: { key: 'cmd+f' }, "
        "message: 'Open search with Cmd+F' }",
        "AppleScript: key code 3 using command down   (Cmd+F)",
        "screencapture /tmp/jarvis_nac_step2.png   (search bar visible)",
        "  J-Prime response: { action_type: 'type', detail: { text: 'Zach' }, "
        "message: 'Type contact name into search' }",
        "AppleScript: keystroke 'Zach'",
        "screencapture /tmp/jarvis_nac_step3.png   (results visible)",
        "  J-Prime response: { done: true, message: 'Search results shown for Zach' }",
        "Goal achieved in 3 steps.",
    ]
    for step in steps:
        if step.startswith("  "):
            print(f"       {step.strip()}")
        else:
            print(f"       {step}")
    print()


# ---------------------------------------------------------------------------
# Gmail simulation printout helper
# ---------------------------------------------------------------------------

def _print_gmail_simulation() -> None:
    steps = [
        "Launch Chrome (headless=False) via Playwright async_playwright()",
        "Navigate to: https://mail.google.com/mail/u/0/#inbox",
        "page.screenshot(type='jpeg') → base64 → send to J-Prime port 8001",
        "  J-Prime: { action_type: 'click', detail: { x: 60, y: 720 }, "
        "message: 'Click the Compose button' }",
        "page.mouse.click(60, 720)",
        "page.screenshot() → J-Prime",
        "  J-Prime: { action_type: 'fill', detail: { selector: '[name=to]', "
        "text: 'djrussell23@gmail.com' }, message: 'Fill To field' }",
        "page.fill('[name=to]', 'djrussell23@gmail.com')",
        "page.screenshot() → J-Prime",
        "  J-Prime: { action_type: 'fill', detail: { selector: '[name=subjectbox]', "
        "text: '[JARVIS VISUAL DEMO] Typed by JARVIS' } }",
        "page.fill('[name=subjectbox]', '[JARVIS VISUAL DEMO] Typed by JARVIS')",
        "page.screenshot() → J-Prime",
        "  J-Prime: { action_type: 'click', detail: { x: 640, y: 500 }, "
        "message: 'Click into email body area' }",
        "page.mouse.click(640, 500)",
        "page.keyboard.type('This email was visually composed by JARVIS...', delay=50)",
        "page.screenshot() → J-Prime",
        "  J-Prime: { done: true, message: 'Email composed successfully' }",
        "Goal achieved in 6 steps.",
    ]
    for step in steps:
        if step.startswith("  "):
            print(f"       {step.strip()}")
        else:
            print(f"       {step}")
    print()


# ---------------------------------------------------------------------------
# Gmail API fallback
# ---------------------------------------------------------------------------

async def _draft_gmail_via_api() -> Dict[str, Any]:
    """
    API-tier fallback: draft the demo email via GoogleWorkspaceAgent.
    Sets JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true for this process if unset.
    """
    sub_banner("API Fallback: GoogleWorkspaceAgent draft_email_reply")
    os.environ.setdefault("JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES", "true")

    try:
        from backend.neural_mesh.agents.google_workspace_agent import GoogleWorkspaceAgent

        agent = GoogleWorkspaceAgent()
        await agent.on_initialize()

        timestamp = time.strftime("%I:%M %p")
        subject = f"[JARVIS VISUAL DEMO] Typed by JARVIS (API fallback at {timestamp})"
        body = (
            "This email was drafted by JARVIS using the GoogleWorkspaceAgent API fallback.\n\n"
            "What would have happened with vision available:\n"
            "1. VisualBrowserAgent launched Chrome via Playwright\n"
            "2. Navigated to https://mail.google.com\n"
            "3. J-Prime vision guided every click and keystroke\n"
            "4. Email composed entirely through visual screen understanding\n\n"
            "Vision model was unavailable this run — Gmail API used instead."
        )

        result = await agent.execute_task({
            "action": "draft_email_reply",
            "to": "djrussell23@gmail.com",
            "subject": subject,
            "body": body,
        })

        success = result.get("success") or bool(result.get("draft_id"))
        if success:
            draft_id = result.get("draft_id", "unknown")
            print(f"  [ SUCCESS ]  Draft created — ID: {draft_id}")
            await say(
                "Email draft created via Gmail API as fallback. "
                "Check your Gmail drafts folder."
            )
            return {
                "success": True,
                "draft_id": draft_id,
                "final_message": f"Draft created via API (ID: {draft_id})",
            }

        err = result.get("error") or result.get("action_required") or "Unknown error"
        print(f"  [ FAILED  ]  {err}")
        await say(f"API draft also failed: {err}")
        return {"success": False, "final_message": f"API draft failed: {err}"}

    except Exception as exc:
        print(f"  [ ERROR   ]  {exc}")
        await say(f"Gmail API fallback error: {type(exc).__name__}")
        return {"success": False, "final_message": f"Exception: {exc}"}


# ---------------------------------------------------------------------------
# STEP 3 — Gmail visual compose (VisualBrowserAgent)
# ---------------------------------------------------------------------------

_GMAIL_URL = "https://mail.google.com/mail/u/0/#inbox"
_GMAIL_GOAL = (
    "Click the Compose button, type 'djrussell23@gmail.com' in the To field, "
    "type '[JARVIS VISUAL DEMO] Typed by JARVIS' in the Subject field, "
    "and type 'This email was visually composed by JARVIS using the browser vision loop.' "
    "in the body."
)


async def step_3_gmail(vision_available: bool) -> Dict[str, Any]:
    """
    Compose a Gmail message via VisualBrowserAgent.
    Falls back to GoogleWorkspaceAgent API draft when no vision model is available.
    """
    banner("STEP 3: Gmail Visual Compose (VisualBrowserAgent)")

    # ── Simulation + API fallback ─────────────────────────────────────────────
    if not vision_available:
        await say(
            "No vision model available. "
            "Let me show you the simulation, then draft the email via API instead."
        )
        sub_banner("Simulation: VisualBrowserAgent — Gmail compose")
        _print_gmail_simulation()

        api_result = await _draft_gmail_via_api()
        return {
            "mode": "api_fallback",
            "success": api_result.get("success", False),
            "steps_taken": 0,
            "actions": [],
            "draft_id": api_result.get("draft_id"),
            "final_message": api_result.get("final_message", str(api_result)),
        }

    # ── Live Playwright vision loop ───────────────────────────────────────────
    await say(
        "Opening Gmail now and starting the browser vision loop to compose an email."
    )

    # Reuse existing Chrome tab if Gmail is already open
    tab_action = await open_url_smart(_GMAIL_URL)
    if tab_action == "reused":
        print("  Reused existing Gmail Chrome tab.")
    else:
        print(f"  Gmail tab {tab_action}. Waiting for page to load...")
        await asyncio.sleep(3)

    result: Dict[str, Any] = {
        "mode": "live",
        "success": False,
        "steps_taken": 0,
        "actions": [],
        "final_message": "Unknown error",
    }

    agent = None
    try:
        from backend.neural_mesh.agents.visual_browser_agent import VisualBrowserAgent

        agent = VisualBrowserAgent()
        await agent.on_initialize()

        print(f"\n  URL:  {_GMAIL_URL}")
        print(f"  Goal: {_GMAIL_GOAL[:90]}...")
        print("  Running browser vision-action loop...\n")

        t0 = time.monotonic()
        agent_result = await agent.execute_task({
            "action": "browse_and_interact",
            "url": _GMAIL_URL,
            "goal": _GMAIL_GOAL,
        })
        elapsed = time.monotonic() - t0

        result["success"] = agent_result.get("success", False)
        result["steps_taken"] = agent_result.get("steps_taken", 0)
        result["actions"] = agent_result.get("actions", [])
        result["final_message"] = agent_result.get("final_message", "")
        result["elapsed_s"] = round(elapsed, 2)

        _print_action_log(agent_result.get("actions", []))

        success_tag = _status_tag(result["success"], "SUCCESS", "FAILED")
        print(f"\n  {success_tag}  {result['final_message']}")
        print(f"  Steps taken: {result['steps_taken']}  |  Wall time: {elapsed:.1f}s")

        if result["success"]:
            await say(
                f"Gmail compose complete after {result['steps_taken']} steps. "
                "The email is ready to send."
            )
        else:
            await say(
                f"Gmail loop finished in {result['steps_taken']} steps. "
                f"{result['final_message']}"
            )

    except Exception as exc:
        result["final_message"] = f"Agent error: {exc}"
        result["error"] = str(exc)
        print(f"\n  [ ERROR   ]  {exc}")
        await say(f"Gmail browser agent encountered an error: {type(exc).__name__}")

    finally:
        if agent is not None:
            try:
                await agent.cleanup()
            except Exception:
                pass

    return result


# ---------------------------------------------------------------------------
# Learning-experience synthesiser
# ---------------------------------------------------------------------------

def _collect_learning_experiences(
    jprime_ok: bool,
    claude_ok: bool,
    whatsapp_result: Dict[str, Any],
    gmail_result: Dict[str, Any],
) -> List[str]:
    """
    Produce observations that JARVIS would normally emit to the Reactor for
    training / future improvement.  All logic is dynamic — nothing hardcoded.
    """
    experiences: List[str] = []

    if not jprime_ok and claude_ok:
        experiences.append(
            "Vision: J-Prime offline — Claude API fallback activated. "
            "Alert if J-Prime is down more than 30 minutes."
        )
    if jprime_ok:
        experiences.append(
            "Vision: J-Prime online and healthy. Zero paid API cost for vision."
        )

    wa_mode = whatsapp_result.get("mode", "")
    wa_ok = whatsapp_result.get("success")
    wa_steps = whatsapp_result.get("steps_taken", 0)
    if wa_mode == "live" and wa_ok:
        experiences.append(
            f"WhatsApp: Goal achieved in {wa_steps} step(s). "
            "Record as native-tier performance baseline."
        )
    elif wa_mode == "live" and not wa_ok:
        msg = whatsapp_result.get("final_message", "unknown")
        experiences.append(
            f"WhatsApp: Failed after {wa_steps} step(s) — {msg}. "
            "Candidate for retry with adjusted goal phrasing."
        )
    elif wa_mode == "skipped":
        experiences.append(
            "WhatsApp: Not installed — browser tier (web.whatsapp.com) should be wired."
        )
    elif wa_mode == "simulated":
        experiences.append(
            "WhatsApp: Simulation only. Re-run with vision available to validate."
        )

    gm_mode = gmail_result.get("mode", "")
    gm_ok = gmail_result.get("success")
    gm_steps = gmail_result.get("steps_taken", 0)
    if gm_mode == "live" and gm_ok:
        experiences.append(
            f"Gmail: Visual compose succeeded in {gm_steps} step(s). "
            "Browser vision loop is reliable for Gmail compose."
        )
    elif gm_mode == "live" and not gm_ok:
        experiences.append(
            f"Gmail: Browser loop failed after {gm_steps} step(s). "
            "Strengthen API fallback for compose reliability."
        )
    elif gm_mode == "api_fallback":
        status = "succeeded" if gm_ok else "failed"
        experiences.append(
            f"Gmail: API fallback draft {status}. "
            "Vision loop preferred when model is available."
        )

    if not experiences:
        experiences.append("No significant learning events captured in this run.")

    return experiences


# ---------------------------------------------------------------------------
# STEP 4 — Summary
# ---------------------------------------------------------------------------

async def step_4_summary(
    jprime_ok: bool,
    claude_ok: bool,
    whatsapp_result: Dict[str, Any],
    gmail_result: Dict[str, Any],
) -> None:
    """Print a results table and speak a closing summary."""
    banner("STEP 4: Demo Summary")

    vision_available = jprime_ok or claude_ok

    rows: List[Tuple[str, Optional[bool], str]] = [
        (
            "Vision / J-Prime",
            jprime_ok,
            "Online — free GCP LLaVA" if jprime_ok else "Offline",
        ),
        (
            "Vision / Claude API",
            claude_ok,
            "Online — paid fallback" if claude_ok else "ANTHROPIC_API_KEY not set",
        ),
    ]

    # WhatsApp row
    wa_mode = whatsapp_result.get("mode", "unknown")
    wa_ok = whatsapp_result.get("success")
    wa_steps = whatsapp_result.get("steps_taken", 0)
    if wa_mode == "live":
        wa_note = f"{'GOAL ACHIEVED' if wa_ok else 'FAILED'} in {wa_steps} steps"
    elif wa_mode == "simulated":
        wa_note = "Simulated walkthrough (no vision model)"
    elif wa_mode == "skipped":
        wa_note = "Not installed — browser fallback narrated"
    else:
        wa_note = str(whatsapp_result.get("final_message", ""))
    rows.append(("WhatsApp (NativeApp)", bool(wa_ok) if wa_ok is not None else None, wa_note))

    # Gmail row
    gm_mode = gmail_result.get("mode", "unknown")
    gm_ok = gmail_result.get("success")
    gm_steps = gmail_result.get("steps_taken", 0)
    if gm_mode == "live":
        gm_note = f"{'GOAL ACHIEVED' if gm_ok else 'FAILED'} in {gm_steps} steps"
    elif gm_mode == "api_fallback":
        gm_note = (
            f"API draft created (ID: {gmail_result.get('draft_id', '?')})"
            if gm_ok
            else "API fallback also failed"
        )
    else:
        gm_note = str(gmail_result.get("final_message", ""))
    rows.append(("Gmail (Browser/API)", bool(gm_ok) if gm_ok is not None else None, gm_note))

    sub_banner("Results")
    col_name = 26
    col_status = 12
    print(f"  {'Component':<{col_name}}  {'Status':<{col_status}}  Notes")
    print(f"  {'─' * col_name}  {'─' * col_status}  {'─' * 36}")
    for name, ok, note in rows:
        if ok is None:
            tag = "[  SKIP   ]"
        else:
            tag = _status_tag(ok, "OK", "FAIL")
        print(f"  {name:<{col_name}}  {tag:<{col_status}}  {note}")

    # Learning experiences
    sub_banner("Learning Experiences Emitted")
    experiences = _collect_learning_experiences(
        jprime_ok, claude_ok, whatsapp_result, gmail_result
    )
    for exp in experiences:
        print(f"  -> {exp}")

    print()

    # Closing narration — content driven dynamically by actual outcomes
    live_count = sum(
        1 for r in (whatsapp_result, gmail_result) if r.get("mode") == "live"
    )

    if vision_available and live_count == 2:
        await say(
            "Demo complete. Both WhatsApp and Gmail were controlled live using the vision loop. "
            "This is JARVIS operating as a fully autonomous visual agent."
        )
    elif vision_available and live_count == 1:
        await say(
            "Demo complete. One task ran live through the vision loop. "
            "The other was handled through an alternative path."
        )
    elif not vision_available:
        await say(
            "Demo complete in simulation mode. "
            "No vision model was available, but every step was narrated in detail. "
            "Start J-Prime or set ANTHROPIC_API_KEY to run the live loops."
        )
    else:
        await say("Demo complete.")


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

async def run_demo() -> None:
    """
    Run the full 4-step live visual control demo.

    Self-contained — no env var prerequisites required.
    Vision availability is checked dynamically at startup.
    """
    print()
    print("=" * 72)
    print("  JARVIS AI — Live Visual Control Demo")
    print("  WhatsApp Native Control + Gmail Browser Vision Loop")
    print("=" * 72)
    print(f"  Start time : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python     : {sys.version.split()[0]}")
    print(f"  Repo root  : {_ROOT}")
    print()

    await say(
        "Welcome to the JARVIS live visual control demo. "
        "I will physically control WhatsApp and compose a Gmail message "
        "using my vision-action loop."
    )

    # Step 1 — vision check
    jprime_ok, claude_ok = await step_1_vision_check()
    vision_available = jprime_ok or claude_ok
    await asyncio.sleep(0.5)

    # Step 2 — WhatsApp native control
    whatsapp_result = await step_2_whatsapp(vision_available)
    await asyncio.sleep(0.5)

    # Step 3 — Gmail visual compose
    gmail_result = await step_3_gmail(vision_available)
    await asyncio.sleep(0.5)

    # Step 4 — Summary
    await step_4_summary(jprime_ok, claude_ok, whatsapp_result, gmail_result)

    print(f"\n  End time   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("  Demo finished.\n")


if __name__ == "__main__":
    asyncio.run(run_demo())
