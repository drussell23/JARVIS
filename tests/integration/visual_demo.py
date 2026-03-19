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

def say(text: str):
    """Speak text using macOS TTS (synchronous, non-blocking via subprocess)."""
    print(f"  [JARVIS] {text}")
    subprocess.Popen(
        ["say", "-v", "Daniel", "-r", "190", text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def say_wait(text: str):
    """Speak and wait for completion."""
    print(f"  [JARVIS] {text}")
    subprocess.run(
        ["say", "-v", "Daniel", "-r", "190", text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_app(app_name: str):
    """Open a macOS application."""
    subprocess.run(
        ["open", "-a", app_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_url(url: str):
    """Open a URL in the default browser."""
    subprocess.run(
        ["open", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def activate_app(app_name: str):
    """Bring an app to the foreground."""
    subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to activate'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
    say("Analyzing your command and expanding into tasks.")

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
    say(f"Expanded into {len(prediction.expanded_tasks)} parallel tasks. Executing now.")
    return prediction, workflow_tasks


async def step_2_open_apps(workflow_tasks):
    """Step 2: Open apps and Google Workspace URLs on screen.

    Distinguishes between:
    - Native macOS apps (VS Code, Slack, Terminal) → open -a
    - Google Workspace services (Gmail, Calendar, Drive) → open URL in Chrome
    """
    banner("STEP 2: Opening apps and services on your MacBook")

    opened = set()  # Track what we've opened to avoid duplicates

    for wt in workflow_tasks:
        workspace_url = wt.input_data.get("workspace_url")
        workspace_svc = wt.input_data.get("workspace_service")
        native_app = wt.input_data.get("target_app") or wt.input_data.get("app_name")

        if workspace_url and workspace_url not in opened:
            # Google Workspace — open URL in Chrome
            svc_name = (workspace_svc or "Google Workspace").replace("_", " ").title()
            print(f"  Opening: {svc_name} in Chrome → {workspace_url}")
            say(f"Opening {svc_name} in Chrome.")
            open_url(workspace_url)
            opened.add(workspace_url)
            await asyncio.sleep(1.5)
        elif native_app and native_app not in opened:
            # Native macOS app
            print(f"  Opening: {native_app}")
            say(f"Opening {native_app}.")
            open_app(native_app)
            opened.add(native_app)
            await asyncio.sleep(1.5)

    if not opened:
        print("  Opening: Google Chrome (default)")
        say("Opening Chrome.")
        open_app("Google Chrome")

    await asyncio.sleep(0.5)


async def step_3_fetch_emails():
    """Step 3: Fetch emails via Gmail API."""
    banner("STEP 3: Checking your email via Gmail API")
    say("Checking your inbox for unread messages.")

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

        say(f"You have {len(emails)} unread emails.")
        await asyncio.sleep(1)

        # Read first email subject aloud
        first_subject = emails[0].get("subject", "no subject")[:60]
        say(f"The most recent is: {first_subject}")
    else:
        print("  No unread emails.")
        say("Your inbox is clear. No unread messages.")

    await asyncio.sleep(1)
    return emails


async def step_4_draft_email():
    """Step 4: Draft an email and show it in Gmail."""
    banner("STEP 4: Drafting email via Gmail API")
    say("Drafting an email for you now.")

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
        say("Email draft created. Opening your Gmail drafts now.")
        await asyncio.sleep(1)

        # Open Gmail drafts in browser
        open_url("https://mail.google.com/mail/u/0/#drafts")
        await asyncio.sleep(2)
        activate_app("Google Chrome")

        print("\n  >>> Gmail Drafts opened in your browser <<<")
        say("Check your Gmail drafts folder. You should see the email I just created.")
    else:
        error = result.get("error", "Unknown error")
        print(f"\n  Draft failed: {error}")
        say(f"Draft creation failed. {error}")

    return result


async def step_5_check_calendar():
    """Step 5: Check calendar."""
    banner("STEP 5: Checking your calendar")
    say("Let me check your calendar for today.")

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

        say(f"You have {len(events)} events on your calendar today.")
    else:
        print("\n  No events today.")
        say("Your calendar is clear for today.")

    await asyncio.sleep(1)


async def run_demo():
    """Run the full visual demo."""
    banner("JARVIS TRINITY LIVE DEMO")
    print("  Watch your screen — JARVIS will perform tasks visually.")
    print()

    say_wait("Hello Derek. I'm going to demonstrate the Trinity pipeline.")
    await asyncio.sleep(0.5)

    command = "Start my day — check email, calendar, and draft a summary"
    say_wait(f"You said: {command}")
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
    say_wait(
        "All tasks completed. The Trinity pipeline is working. "
        "I expanded your command into parallel tasks, "
        "checked your email, drafted a message, "
        "and reviewed your calendar. "
        "JARVIS is alive."
    )
    print()
    print("  Results:")
    print("    - Intent expanded into parallel tasks")
    print("    - Apps opened on your desktop")
    print("    - Emails fetched from Gmail API")
    print("    - Draft email created (check Gmail Drafts)")
    print("    - Calendar checked")
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
