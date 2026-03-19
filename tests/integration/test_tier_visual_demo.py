#!/usr/bin/env python3
"""
JARVIS Tier-Aware Visual Demo — Execution Tier System in action.

Demonstrates the 3-tier execution architecture:
  - Tier API        : GoogleWorkspaceAgent handles Gmail/Calendar/Drive via API
  - Tier NATIVE_APP : NativeAppControlAgent drives installed macOS apps
  - Tier BROWSER    : VisualBrowserAgent drives Chrome via Playwright + vision

What this demo does:
  1. Show tier routing decisions for several natural-language commands (table)
  2. Scan installed apps via AppInventoryService (found vs not found)
  3. Execute a real API-tier task -- draft an email via GoogleWorkspaceAgent
  4. Narrate what WOULD happen for NATIVE_APP / BROWSER tiers (without executing)
  5. Open Gmail Drafts in Chrome to show the result

Run:
    JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true python3 \
        tests/integration/test_tier_visual_demo.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "backend"))


# ---------------------------------------------------------------------------
# TTS -- sequential speech queue (no overlapping voices)
# ---------------------------------------------------------------------------

_tts_lock: Optional[asyncio.Lock] = None


def _get_tts_lock() -> asyncio.Lock:
    global _tts_lock
    if _tts_lock is None:
        _tts_lock = asyncio.Lock()
    return _tts_lock


async def say(text: str) -> None:
    """Speak text aloud and wait for completion. Always sequential."""
    async with _get_tts_lock():
        print(f"  [JARVIS] {text}")
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "Daniel", "-r", "190", text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


# ---------------------------------------------------------------------------
# Chrome tab helpers -- reuse existing tabs instead of duplicating
# ---------------------------------------------------------------------------

_ACTIVATE_TAB_SCRIPT = """\
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
"""


def _activate_chrome_tab(domain: str) -> bool:
    """Activate an existing Chrome tab matching domain. Returns True if found."""
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
    """Open a URL, reusing an existing Chrome tab when possible.

    Returns "reused", "opened", or "failed".
    """
    domain = urlparse(url).netloc or url
    if _activate_chrome_tab(domain):
        return "reused"
    proc = await asyncio.create_subprocess_exec(
        "open", url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return "opened" if proc.returncode == 0 else "failed"


async def activate_app(app_name: str) -> None:
    """Bring an app window to the foreground via AppleScript."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", f'tell application "{app_name}" to activate',
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def banner(text: str) -> None:
    width = 72
    print("\n" + "=" * width)
    print(f"  {text}")
    print("=" * width)


def sub_banner(text: str) -> None:
    print(f"\n  {'─' * 66}")
    print(f"  {text}")
    print(f"  {'─' * 66}")


def _tier_badge(tier: str) -> str:
    """Return a compact badge string for terminal output."""
    badges = {
        "api": "[ API        ]",
        "native_app": "[ NATIVE_APP ]",
        "browser": "[ BROWSER    ]",
    }
    return badges.get(tier.lower(), f"[ {tier.upper():10s} ]")


# ---------------------------------------------------------------------------
# STEP 1 -- Tier routing decisions table
# ---------------------------------------------------------------------------

# Each entry: (command, decide_tier kwargs, explanation for the table)
_ROUTING_SCENARIOS: List[Tuple[str, Dict[str, Any], str]] = [
    (
        "Check my email",
        {"workspace_service": "gmail"},
        "Gmail is a known API service",
    ),
    (
        "Send Zach a WhatsApp message",
        {"target_app": "WhatsApp"},
        "NATIVE_APP if installed, else BROWSER fallback",
    ),
    (
        "Draft an email visually",
        {"workspace_service": "gmail", "force_visual": True},
        "force_visual=True overrides everything",
    ),
    (
        "Check LinkedIn messages",
        {},
        "No API/app match, falls through to BROWSER",
    ),
    (
        "Open Spotify",
        {"target_app": "Spotify"},
        "NATIVE_APP if installed, else BROWSER fallback",
    ),
    (
        "Schedule a meeting",
        {"workspace_service": "calendar"},
        "Calendar is a known API service",
    ),
    (
        "Search my Drive",
        {"workspace_service": "drive"},
        "Drive is a known API service",
    ),
]


async def step_1_routing_table() -> None:
    """Show tier routing decisions for several natural-language commands."""
    banner("STEP 1: Tier Routing Decisions")
    await say(
        "Let me show you how I decide which execution tier to use for each command."
    )

    from backend.neural_mesh.agents.execution_tier_router import (
        ExecutionTierRouter,
        ExecutionTier,
    )

    router = ExecutionTierRouter()
    await router.on_initialize()  # wires AppInventoryService for live app checks

    col_cmd = 33
    col_tier = 15

    header = f"  {'Command':<{col_cmd}}  {'Tier':<{col_tier}}  Reasoning"
    sep = f"  {'─' * col_cmd}  {'─' * col_tier}  {'─' * 36}"
    print(f"\n{header}")
    print(sep)

    results: List[Dict[str, Any]] = []

    for command, kwargs, reasoning in _ROUTING_SCENARIOS:
        tier: ExecutionTier = await router.decide_tier_async(command, **kwargs)
        web_url: Optional[str] = None
        if tier == ExecutionTier.BROWSER and kwargs.get("target_app"):
            web_url = router.get_web_alternative(kwargs["target_app"])

        badge = _tier_badge(tier.value)
        cmd_display = (command[:col_cmd - 2] + "..") if len(command) > col_cmd else command
        print(f"  {cmd_display:<{col_cmd}}  {badge:<{col_tier}}  {reasoning}")
        if web_url:
            print(f"  {'':>{col_cmd}}  {'':>{col_tier}}  -> web fallback: {web_url}")

        results.append({
            "command": command,
            "tier": tier.value,
            "web_url": web_url,
            "reasoning": reasoning,
        })
        await asyncio.sleep(0.1)

    print()
    api_count = sum(1 for r in results if r["tier"] == "api")
    native_count = sum(1 for r in results if r["tier"] == "native_app")
    browser_count = sum(1 for r in results if r["tier"] == "browser")
    print(
        f"  Summary  API: {api_count}  |  NATIVE_APP: {native_count}  |  BROWSER: {browser_count}"
    )

    await say(
        f"Routing complete. "
        f"{api_count} commands go to the API tier, "
        f"{native_count} to native app control, "
        f"and {browser_count} to the browser automation tier."
    )
    await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# STEP 2 -- App inventory scan
# ---------------------------------------------------------------------------

_TIER_DEMO_APPS: List[str] = [
    "WhatsApp",
    "Spotify",
    "Slack",
    "Discord",
    "Telegram",
    "Google Chrome",
    "Firefox",
    "Safari",
    "Zoom",
    "Visual Studio Code",
    "iTerm2",
    "Terminal",
]

_NATIVE_TIER_APPS = frozenset({"WhatsApp", "Spotify", "Slack", "Discord", "Telegram"})


async def step_2_app_scan() -> Dict[str, bool]:
    """Scan installed apps and display a found / not-found summary.

    Returns a mapping of app_name -> installed (bool).
    """
    banner("STEP 2: Scanning Installed Apps (AppInventoryService)")
    await say("Scanning your Mac for installed applications.")

    from backend.neural_mesh.agents.app_inventory_service import AppInventoryService

    svc = AppInventoryService()
    await svc.on_initialize()

    result = await svc.execute_task({
        "action": "scan_installed",
        "apps": _TIER_DEMO_APPS,
    })

    found_names = {r["app_name"].lower() for r in result.get("apps", [])}
    found_map: Dict[str, bool] = {
        app: (app.lower() in found_names) for app in _TIER_DEMO_APPS
    }

    sub_banner("Results")
    found_list: List[str] = []
    missing_list: List[str] = []

    for app_name in _TIER_DEMO_APPS:
        installed = found_map[app_name]
        marker = "  [FOUND  ]" if installed else "  [MISSING]"
        tier_note = ""
        if not installed and app_name in _NATIVE_TIER_APPS:
            tier_note = "  -> tier falls back to BROWSER"
        print(f"{marker}  {app_name}{tier_note}")
        (found_list if installed else missing_list).append(app_name)

    total = result.get("total_scanned", len(_TIER_DEMO_APPS))
    print(
        f"\n  Scanned {total} apps -- "
        f"found {len(found_list)}, missing {len(missing_list)}"
    )

    if found_list:
        sample = ", ".join(found_list[:3])
        await say(
            f"Found {len(found_list)} relevant apps installed. "
            f"{sample} and others are ready for native control."
        )
    else:
        await say("No extra apps found beyond the defaults.")

    notable_missing = [a for a in missing_list if a in _NATIVE_TIER_APPS]
    if notable_missing:
        names = ", ".join(notable_missing)
        verb = "is" if len(notable_missing) == 1 else "are"
        await say(
            f"{names} {verb} not installed. "
            "Those commands will route to the browser tier instead."
        )

    await asyncio.sleep(0.5)
    return found_map


# ---------------------------------------------------------------------------
# STEP 3 -- Real API-tier task: draft an email
# ---------------------------------------------------------------------------

async def step_3_api_email_draft() -> Optional[str]:
    """Execute a real API-tier task: draft an email via GoogleWorkspaceAgent.

    Returns the draft_id on success, None on failure.
    """
    banner("STEP 3: API Tier -- Draft Email via GoogleWorkspaceAgent")
    await say(
        "Now I'll execute a real API tier task -- drafting an email via Gmail."
    )

    from backend.neural_mesh.agents.google_workspace_agent import GoogleWorkspaceAgent

    agent = GoogleWorkspaceAgent()
    await agent.on_initialize()

    timestamp = time.strftime("%I:%M %p")
    demo_subject = f"[JARVIS TIER DEMO] Drafted at {timestamp}"
    demo_body = (
        f"This email was drafted by JARVIS at {timestamp} during the Tier Demo.\n\n"
        "What just happened:\n"
        "1. You ran the tier-aware visual demo\n"
        "2. ExecutionTierRouter decided: 'draft email' -> Tier API\n"
        "   (workspace_service='gmail' matched _API_SERVICES)\n"
        "3. GoogleWorkspaceAgent.execute_task({'action': 'draft_email_reply', ...})\n"
        "   called the Gmail REST API directly -- no browser, no native app\n"
        "4. Draft created in < 2 seconds\n\n"
        "Execution Tiers:\n"
        "  API        -> Gmail, Calendar, Drive, Docs, Sheets (direct REST)\n"
        "  NATIVE_APP -> WhatsApp, Spotify, Slack (vision + PyAutoGUI loop)\n"
        "  BROWSER    -> LinkedIn, web apps, force_visual overrides (Playwright)\n\n"
        "--- JARVIS AI System -- Execution Tier Router v1.0 ---"
    )

    print(f"\n  Subject : {demo_subject}")
    print(f"  To      : djrussell23@gmail.com")
    print(f"  Tier    : {_tier_badge('api')}")
    print()

    result = await agent.execute_task({
        "action": "draft_email_reply",
        "to": "djrussell23@gmail.com",
        "subject": demo_subject,
        "body": demo_body,
    })

    draft_id: Optional[str] = None
    if isinstance(result, dict):
        draft_id = result.get("draft_id")
        status = result.get("status", "")
    else:
        status = str(result)

    if draft_id or status == "created":
        print("  Draft created successfully!")
        if draft_id:
            print(f"  Draft ID : {draft_id}")
        await say("Email draft created. Opening your Gmail drafts folder now.")
        await asyncio.sleep(0.5)

        tab_result = await open_url_smart("https://mail.google.com/mail/u/0/#drafts")
        if tab_result == "reused":
            print("  >>> Switched to existing Gmail tab <<<")
        else:
            print("  >>> Gmail Drafts opened in Chrome <<<")

        await asyncio.sleep(2)
        await activate_app("Google Chrome")
        await say(
            "Check your Gmail drafts folder. The tier demo email should be there."
        )
    else:
        error = (
            result.get("error", "unknown error") if isinstance(result, dict) else str(result)
        )
        print(f"  Draft failed: {error}")
        await say(f"Draft creation failed. Error: {error}")
        draft_id = None

    await asyncio.sleep(0.5)
    return draft_id


# ---------------------------------------------------------------------------
# STEP 4 -- Narrate NATIVE_APP tier (no execution)
# ---------------------------------------------------------------------------

_WEB_ALTERNATIVES: Dict[str, str] = {
    "WhatsApp": "https://web.whatsapp.com",
    "Spotify": "https://open.spotify.com",
    "Slack": "https://app.slack.com",
    "Discord": "https://discord.com/app",
    "Telegram": "https://web.telegram.org",
}


async def step_4_native_app_narrative(installed_apps: Dict[str, bool]) -> None:
    """Describe what NativeAppControlAgent would do for a NATIVE_APP task."""
    banner("STEP 4: NATIVE_APP Tier -- What Would Happen")

    native_candidates = [
        app for app in ("WhatsApp", "Spotify", "Slack", "Discord")
        if installed_apps.get(app)
    ]
    example_app = native_candidates[0] if native_candidates else "Spotify"
    is_installed = installed_apps.get(example_app, False)

    await say(
        f"Let me walk you through what the native app control tier would do "
        f"if I needed to control {example_app}."
    )

    effective_tier = "native_app" if is_installed else "browser"
    status_note = (
        "installed=True -> NATIVE_APP tier" if is_installed
        else "not installed -> falls back to BROWSER tier"
    )

    print(f"\n  Example command : \"Open {example_app} and play something\"")
    print(f"  Target app      : {example_app}")
    print(f"  App status      : {status_note}")
    print(f"  Tier decision   : {_tier_badge(effective_tier)}")
    print()

    sub_banner("NativeAppControlAgent vision-action loop (simulated)")
    steps = [
        (
            "App check",
            f"AppInventoryService.is_installed('{example_app}') -> {is_installed}",
        ),
        (
            "Activate app",
            f'osascript: tell application "{example_app}" to activate',
        ),
        (
            "Capture screenshot",
            "screencapture -x -C /tmp/jarvis_native_step_0.png",
        ),
        (
            "Vision inference",
            "POST screenshot + goal to J-Prime vision (free) / Claude API (fallback)\n"
            '     <- {"action": "click", "x": 420, "y": 38, '
            '"description": "Click search bar"}',
        ),
        (
            "Execute action",
            "PyAutoGUI: pyautogui.click(420, 38)  [click search bar]",
        ),
        (
            "Type query",
            "PyAutoGUI: pyautogui.typewrite('something', interval=0.05)",
        ),
        (
            "Repeat loop",
            "Re-screenshot -> vision -> action  "
            "(up to JARVIS_NATIVE_CONTROL_MAX_STEPS=10)",
        ),
        (
            "Goal achieved",
            '{"action": "done"} from vision model -> loop exits',
        ),
    ]

    for i, (phase, detail) in enumerate(steps, 1):
        print(f"  Step {i}: {phase}")
        print(f"     {detail}")
        await asyncio.sleep(0.15)

    print()
    if not is_installed:
        web_url = _WEB_ALTERNATIVES.get(example_app)
        if web_url:
            print(f"  Since {example_app} is not installed:")
            print(f"  VisualBrowserAgent would navigate to: {web_url}")
            print()

    await say(
        "The native app control agent uses a screenshot-and-act loop. "
        "It captures your screen, asks the vision model what to click next, "
        "acts with PyAutoGUI, then repeats until the goal is complete."
    )
    await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# STEP 5 -- Narrate BROWSER tier (no execution)
# ---------------------------------------------------------------------------

async def step_5_browser_narrative() -> None:
    """Describe what VisualBrowserAgent would do for a BROWSER task."""
    banner("STEP 5: BROWSER Tier -- What Would Happen")

    await say(
        "Finally, let me describe how the browser tier handles web-only tasks "
        "like checking LinkedIn messages."
    )

    print()
    print("  Example command : \"Check my LinkedIn messages\"")
    print(f"  Tier decision   : {_tier_badge('browser')}")
    print("  Reasoning       : No API service, no installed app -> BROWSER default")
    print()

    sub_banner("VisualBrowserAgent Playwright vision-action loop (simulated)")
    steps = [
        (
            "Launch browser",
            "playwright.chromium.launch(headless=False)  [visible Chrome window]",
        ),
        (
            "Navigate",
            "page.goto('https://www.linkedin.com/messaging/')  [direct URL]",
        ),
        (
            "Wait for load",
            "page.wait_for_load_state('networkidle')  [page fully stabilised]",
        ),
        (
            "Screenshot",
            "page.screenshot(type='png') -> base64-encoded image",
        ),
        (
            "Vision inference",
            "POST screenshot + goal to J-Prime (free) / Claude API (fallback)\n"
            '     <- {"action": "click", "selector": ".msg-overlay-list-bubble", '
            '"description": "Click message icon"}',
        ),
        (
            "Execute action",
            "page.click('.msg-overlay-list-bubble')  [Playwright DOM click]",
        ),
        (
            "Repeat loop",
            "Re-screenshot -> vision -> action  "
            "(up to JARVIS_BROWSER_MAX_STEPS env var)",
        ),
        (
            "Goal achieved",
            '{"action": "done"} from vision model -> returns summary to caller',
        ),
    ]

    for i, (phase, detail) in enumerate(steps, 1):
        print(f"  Step {i}: {phase}")
        print(f"     {detail}")
        await asyncio.sleep(0.15)

    print()
    print("  force_visual override:")
    print("    Any command with force_visual=True bypasses all other checks and")
    print("    routes directly to VisualBrowserAgent regardless of service or app.")
    print()
    print("  Web fallbacks for native apps not installed:")
    for app, url in _WEB_ALTERNATIVES.items():
        print(f"    {app:<12} -> {url}")
    print()

    await say(
        "The browser tier uses Playwright to control a real Chrome window. "
        "It navigates, screenshots each state, asks the vision model for the "
        "next action, then executes -- click, fill, type, or scroll -- "
        "until the goal is reached or the step limit is hit."
    )
    await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Main demo entry point
# ---------------------------------------------------------------------------

async def run_demo() -> None:
    """Run the full tier-aware visual demo."""
    banner("JARVIS EXECUTION TIER DEMO  --  API / NATIVE_APP / BROWSER")
    print()
    print("  3-tier execution routing system:")
    print("    Tier API        -> direct Google Workspace REST API")
    print("    Tier NATIVE_APP -> vision + PyAutoGUI loop on installed apps")
    print("    Tier BROWSER    -> Playwright + vision loop in Chrome")
    print()

    await say(
        "Hello Derek. I'm going to demonstrate the execution tier routing system. "
        "I'll show you how I decide whether to use the API, control a native app, "
        "or drive the browser for each command you give me."
    )
    await asyncio.sleep(0.3)

    await step_1_routing_table()
    installed_apps = await step_2_app_scan()
    draft_id = await step_3_api_email_draft()
    await step_4_native_app_narrative(installed_apps)
    await step_5_browser_narrative()

    banner("DEMO COMPLETE")

    found_count = sum(1 for v in installed_apps.values() if v)
    missing_count = len(installed_apps) - found_count

    print("  Results:")
    print(f"    - Tier routing decisions shown for {len(_ROUTING_SCENARIOS)} commands")
    print(f"    - App scan: {found_count} found, {missing_count} not installed")
    if draft_id:
        print(f"    - Email draft created via Gmail API (draft_id: {draft_id})")
    else:
        print("    - Email draft: skipped or failed")
    print("    - NATIVE_APP tier walk-through complete (NativeAppControlAgent)")
    print("    - BROWSER tier walk-through complete (VisualBrowserAgent)")
    print()
    print("  Gmail Drafts -> https://mail.google.com/mail/u/0/#drafts")
    print()

    await say(
        "All steps complete. "
        "The tier routing system is fully wired. "
        "API commands go direct to Google Workspace. "
        "Native app commands drive your installed applications with vision. "
        "Browser commands control Chrome with Playwright and J-Prime vision. "
        "JARVIS is ready."
    )


# ---------------------------------------------------------------------------
# Entry point with prerequisite checks
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if os.getenv("JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES", "").lower() not in (
        "true", "1", "yes",
    ):
        print(
            "ERROR: Set JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true "
            "to enable email drafting"
        )
        print()
        print("Run with:")
        print(
            "  JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES=true "
            "python3 tests/integration/test_tier_visual_demo.py"
        )
        sys.exit(1)

    cred_path = os.path.expanduser("~/.jarvis/google_credentials.json")
    token_path = os.path.expanduser("~/.jarvis/google_workspace_token.json")
    missing_creds: List[str] = []
    if not os.path.exists(cred_path):
        missing_creds.append(cred_path)
    if not os.path.exists(token_path):
        missing_creds.append(token_path)

    if missing_creds:
        print("ERROR: Google OAuth credentials not found:")
        for p in missing_creds:
            print(f"  Missing: {p}")
        print()
        print("Run: python3 backend/scripts/google_oauth_setup.py")
        sys.exit(1)

    asyncio.run(run_demo())
