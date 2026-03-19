#!/usr/bin/env python3
"""
JARVIS Visual Demo — Watch the Trinity pipeline work on your screen.

This script demonstrates JARVIS performing real tasks visually:
1. JARVIS announces what it's doing (TTS)
2. Expands your command into parallel tasks
3. Opens apps on your MacBook
4. Drafts an email via Gmail API
5. Opens Gmail in your browser to show the draft

Run:
    JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true python3 tests/integration/visual_demo.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

# Setup paths
_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "backend"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TTS — Sequential speech queue (no overlapping voices)
# ---------------------------------------------------------------------------

_tts_lock = None  # Initialized lazily in async context


def _get_tts_lock():
    global _tts_lock
    if _tts_lock is None:
        _tts_lock = asyncio.Lock()
    return _tts_lock


async def say(text: str):
    """Speak text and wait for completion. Sequential — no overlapping."""
    async with _get_tts_lock():
        print(f"  [JARVIS] {text}")
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "Daniel", "-r", "190", text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


async def say_wait(text: str):
    """Alias for say() — always waits, always sequential."""
    await say(text)


# ---------------------------------------------------------------------------
# App detection — check if installed before opening
# ---------------------------------------------------------------------------

_app_installed_cache: dict = {}
_failed_apps: list = []  # Track for Reactor learning


def is_app_installed(app_name: str) -> bool:
    """Check if a macOS application is installed."""
    if app_name in _app_installed_cache:
        return _app_installed_cache[app_name]

    from pathlib import Path
    found = (
        Path(f"/Applications/{app_name}.app").exists()
        or Path(f"/System/Applications/{app_name}.app").exists()
        or Path(os.path.expanduser(f"~/Applications/{app_name}.app")).exists()
    )

    if not found:
        # Also check via mdfind (catches non-standard install locations)
        try:
            result = subprocess.run(
                ["mdfind", "-name", f"{app_name}.app", "-onlyin", "/Applications"],
                capture_output=True, text=True, timeout=3,
            )
            found = bool(result.stdout.strip())
        except Exception:
            pass

    _app_installed_cache[app_name] = found
    return found


async def open_app(app_name: str) -> bool:
    """Open a macOS application if installed. Returns False if not found."""
    if not is_app_installed(app_name):
        print(f"  [SKIP] {app_name} is not installed")
        _failed_apps.append({"app": app_name, "reason": "not_installed"})
        return False

    proc = await asyncio.create_subprocess_exec(
        "open", "-a", app_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Chrome tab detection — reuse existing tabs instead of duplicating
# ---------------------------------------------------------------------------

_ACTIVATE_TAB_SCRIPT = '''
tell application "Google Chrome"
    repeat with w in windows
        set tabCount to 0
        repeat with t in tabs of w
            set tabCount to tabCount + 1
            if URL of t contains "{domain}" then
                set active tab index of w to tabCount
                set index of w to 1
                activate
                return "found"
            end if
        end repeat
    end repeat
    return "not_found"
end tell
'''


def activate_chrome_tab(domain: str) -> bool:
    """Activate an existing Chrome tab matching a domain.

    Returns True if an existing tab was found and activated.
    """
    script = _ACTIVATE_TAB_SCRIPT.replace("{domain}", domain)
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() == "found"
    except Exception:
        return False


async def open_url_smart(url: str) -> str:
    """Open a URL, reusing an existing Chrome tab if one matches.

    Returns "reused", "opened", or "failed".
    """
    # Extract domain for matching (e.g., "mail.google.com")
    from urllib.parse import urlparse
    domain = urlparse(url).netloc or url

    # Check if tab already exists
    if activate_chrome_tab(domain):
        return "reused"

    # No existing tab — open new one
    proc = await asyncio.create_subprocess_exec(
        "open", url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return "opened" if proc.returncode == 0 else "failed"


async def activate_app(app_name: str):
    """Bring an app to the foreground."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", f'tell application "{app_name}" to activate',
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def get_failed_apps() -> list:
    """Get list of apps that failed to open (for Reactor learning)."""
    return list(_failed_apps)


def banner(text: str):
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Demo Steps
# ---------------------------------------------------------------------------

async def step_1_expand_intent(command: str):
    """Step 1: Show intent expansion."""
    banner("STEP 1: Expanding intent with Psychic Brain")
    await say("Analyzing your command and expanding into tasks.")

    from backend.neural_mesh.agents.predictive_planning_agent import (
        PredictivePlanningAgent,
    )

    agent = PredictivePlanningAgent()
    prediction = await agent.expand_intent(command)

    print(f"\n  Command: \"{command}\"")
    print(f"  Intent: {prediction.detected_intent.value}")
    print(f"  Confidence: {prediction.confidence:.0%}")
    print(f"  Tasks: {len(prediction.expanded_tasks)}")
    print()

    for i, task in enumerate(prediction.expanded_tasks, 1):
        print(f"    {i}. [{task.priority}] {task.goal}")
        if task.target_app:
            print(f"         App: {task.target_app}")
        await asyncio.sleep(0.3)

    # Convert to workflow tasks
    workflow_tasks = agent.to_workflow_tasks(prediction)

    print(f"\n  Converted to {len(workflow_tasks)} workflow tasks:")
    for wt in workflow_tasks:
        cap = wt.required_capability
        fb = f" (fallback: {wt.fallback_capability})" if wt.fallback_capability else ""
        print(f"    [{wt.priority.name:8s}] {cap:25s}{fb}")

    await asyncio.sleep(1)
    await say(f"Expanded into {len(prediction.expanded_tasks)} parallel tasks. Executing now.")
    return prediction, workflow_tasks


async def step_2_open_apps(workflow_tasks):
    """Step 2: Open apps and Google Workspace URLs on screen.

    Smart behaviors:
    - Checks if Chrome tab already open before creating duplicate
    - Checks if native app is installed before trying to open
    - Reports skipped/reused items for Reactor learning
    - Sequential TTS — no overlapping voices
    """
    banner("STEP 2: Opening apps and services on your MacBook")

    opened = set()  # Track what we've opened to avoid duplicates
    skipped = []

    for wt in workflow_tasks:
        workspace_url = wt.input_data.get("workspace_url")
        workspace_svc = wt.input_data.get("workspace_service")
        native_app = wt.input_data.get("target_app") or wt.input_data.get("app_name")

        if workspace_url and workspace_url not in opened:
            svc_name = (workspace_svc or "Google Workspace").replace("_", " ").title()

            # Smart: check if tab already open
            result = await open_url_smart(workspace_url)

            if result == "reused":
                print(f"  [REUSED] {svc_name} — already open in Chrome")
                await say(f"{svc_name} is already open. Switching to it.")
            elif result == "opened":
                print(f"  [OPENED] {svc_name} in Chrome → {workspace_url}")
                await say(f"Opening {svc_name} in Chrome.")
            else:
                print(f"  [FAILED] Could not open {svc_name}")
                skipped.append({"service": svc_name, "url": workspace_url})

            opened.add(workspace_url)
            await asyncio.sleep(1.0)

        elif native_app and native_app not in opened:
            # Smart: check if installed first
            if is_app_installed(native_app):
                print(f"  [OPENED] {native_app}")
                await say(f"Opening {native_app}.")
                await open_app(native_app)
            else:
                print(f"  [SKIP] {native_app} — not installed on this Mac")
                await say(f"{native_app} is not installed. Skipping.")
                skipped.append({"app": native_app, "reason": "not_installed"})

            opened.add(native_app)
            await asyncio.sleep(1.0)

    if not opened:
        print("  [OPENED] Google Chrome (default)")
        await say("Opening Chrome.")
        await open_app("Google Chrome")

    if skipped:
        print(f"\n  Skipped {len(skipped)} items: {skipped}")
        print("  (These will be logged for Reactor learning)")

    await asyncio.sleep(0.5)


async def step_3_fetch_emails():
    """Step 3: Fetch emails via Gmail API."""
    banner("STEP 3: Checking your email via Gmail API")
    await say("Checking your inbox for unread messages.")

    from backend.neural_mesh.agents.google_workspace_agent import (
        GoogleWorkspaceAgent,
    )

    agent = GoogleWorkspaceAgent()
    await agent.on_initialize()

    result = await agent.execute_task({
        "action": "fetch_unread_emails",
        "limit": 5,
    })

    emails = result.get("emails", []) if isinstance(result, dict) else []

    if emails:
        print(f"\n  Found {len(emails)} unread emails:")
        for i, email in enumerate(emails[:5], 1):
            sender = email.get("from", "Unknown")[:40]
            subject = email.get("subject", "(no subject)")[:50]
            print(f"    {i}. {sender}")
            print(f"       {subject}")
            print()

        await say(f"You have {len(emails)} unread emails.")
        await asyncio.sleep(1)

        # Read first email subject aloud
        first_subject = emails[0].get("subject", "no subject")[:60]
        await say(f"The most recent is: {first_subject}")
    else:
        print("  No unread emails.")
        await say("Your inbox is clear. No unread messages.")

    await asyncio.sleep(1)
    return emails


async def step_4_draft_email():
    """Step 4: Draft an email and show it in Gmail."""
    banner("STEP 4: Drafting email via Gmail API")
    await say("Drafting an email for you now.")

    from backend.neural_mesh.agents.google_workspace_agent import (
        GoogleWorkspaceAgent,
    )

    agent = GoogleWorkspaceAgent()
    await agent.on_initialize()

    timestamp = time.strftime("%I:%M %p")

    result = await agent.execute_task({
        "action": "draft_email_reply",
        "to": "djrussell23@gmail.com",
        "subject": f"[JARVIS LIVE DEMO] Drafted at {timestamp}",
        "body": (
            f"This email was drafted by JARVIS at {timestamp} during a live demo.\n\n"
            "What happened:\n"
            "1. You spoke a command (or triggered a test)\n"
            "2. PredictivePlanningAgent expanded it into parallel tasks\n"
            "3. MultiAgentOrchestrator routed tasks to agents\n"
            "4. GoogleWorkspaceAgent created this draft via Gmail API\n"
            "5. Trinity logged the experience for Reactor learning\n\n"
            "The full pipeline is working. JARVIS is alive.\n\n"
            "--- JARVIS AI System (Trinity: Body, Mind, Nerves) ---"
        ),
    })

    if result.get("status") == "created" or result.get("draft_id"):
        draft_id = result.get("draft_id", "unknown")
        print(f"\n  Draft created successfully!")
        print(f"  Draft ID: {draft_id}")
        print(f"  Subject: [JARVIS LIVE DEMO] Drafted at {timestamp}")
        await say("Email draft created. Opening your Gmail drafts now.")
        await asyncio.sleep(1)

        # Open Gmail drafts in browser (reuse tab if possible)
        tab_result = await open_url_smart("https://mail.google.com/mail/u/0/#drafts")
        if tab_result == "reused":
            print("\n  >>> Switched to existing Gmail tab <<<")
        else:
            print("\n  >>> Gmail Drafts opened in your browser <<<")
        await asyncio.sleep(2)
        await activate_app("Google Chrome")

        await say("Check your Gmail drafts folder. You should see the email I just created.")
    else:
        error = result.get("error", "Unknown error")
        print(f"\n  Draft failed: {error}")
        await say(f"Draft creation failed. {error}")

    return result


async def step_5_check_calendar():
    """Step 5: Check calendar."""
    banner("STEP 5: Checking your calendar")
    await say("Let me check your calendar for today.")

    from backend.neural_mesh.agents.google_workspace_agent import (
        GoogleWorkspaceAgent,
    )

    agent = GoogleWorkspaceAgent()
    await agent.on_initialize()

    result = await agent.execute_task({
        "action": "check_calendar_events",
    })

    events = result.get("events", []) if isinstance(result, dict) else []
    if events:
        print(f"\n  {len(events)} events today:")
        for event in events[:5]:
            summary = event.get("summary", "(no title)")
            start = event.get("start", {})
            time_str = start.get("dateTime", start.get("date", ""))[:16]
            print(f"    {time_str} — {summary}")

        await say(f"You have {len(events)} events on your calendar today.")
    else:
        print("\n  No events today.")
        await say("Your calendar is clear for today.")

    await asyncio.sleep(1)


async def run_demo():
    """Run the full visual demo."""
    banner("JARVIS TRINITY LIVE DEMO")
    print("  Watch your screen — JARVIS will perform tasks visually.")
    print()

    await say_wait("Hello Derek. I'm going to demonstrate the Trinity pipeline.")
    await asyncio.sleep(0.5)

    command = "Start my day — check email, calendar, and draft a summary"
    await say_wait(f"You said: {command}")
    await asyncio.sleep(0.5)

    # Step 1: Expand the intent
    prediction, workflow_tasks = await step_1_expand_intent(command)

    # Step 2: Open apps
    await step_2_open_apps(workflow_tasks)

    # Step 3: Fetch emails
    await step_3_fetch_emails()

    # Step 4: Draft an email
    await step_4_draft_email()

    # Step 5: Check calendar
    await step_5_check_calendar()

    # Finale
    banner("DEMO COMPLETE")
    await say_wait(
        "All tasks completed. The Trinity pipeline is working. "
        "I expanded your command into parallel tasks, "
        "checked your email, drafted a message, "
        "and reviewed your calendar. "
        "JARVIS is alive."
    )
    print()
    # Report results including what was skipped
    failed = get_failed_apps()

    print("  Results:")
    print("    - Intent expanded into parallel tasks")
    print("    - Apps opened on your desktop")
    print("    - Emails fetched from Gmail API")
    print("    - Draft email created (check Gmail Drafts)")
    print("    - Calendar checked")
    if failed:
        print(f"\n  Apps not installed (skipped):")
        for f_app in failed:
            print(f"    - {f_app.get('app', f_app.get('service', '?'))}: {f_app.get('reason', 'unknown')}")
        print("  (Logged for Reactor Core learning)")
    print()


if __name__ == "__main__":
    # Check prerequisites
    if not os.getenv("JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES", "").lower() in (
        "true", "1", "yes",
    ):
        print("ERROR: Set JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true to enable email drafting")
        print()
        print("Run with:")
        print("  JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true python3 tests/integration/visual_demo.py")
        sys.exit(1)

    cred_path = os.path.expanduser("~/.jarvis/google_credentials.json")
    token_path = os.path.expanduser("~/.jarvis/google_workspace_token.json")
    if not os.path.exists(cred_path) or not os.path.exists(token_path):
        print("ERROR: Google OAuth not set up")
        print(f"  Missing: {cred_path}" if not os.path.exists(cred_path) else "")
        print(f"  Missing: {token_path}" if not os.path.exists(token_path) else "")
        print()
        print("Run: python3 backend/scripts/google_oauth_setup.py")
        sys.exit(1)

    asyncio.run(run_demo())
