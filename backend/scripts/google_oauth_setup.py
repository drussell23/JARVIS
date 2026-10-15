#!/usr/bin/env python3
"""
Connect JARVIS to your Google account (Gmail, Calendar, Contacts, Drive files).

Usage (the interpreter JARVIS runs on; JARVIS prints this exact command when
Google is not connected):
    <JARVIS python> backend/scripts/google_oauth_setup.py

Opens your browser; sign in with your Google account and allow the requested
access. The token is saved where JARVIS reads it, and the services are
checked once with it.

The sign-in is the workspace agent's own (GoogleWorkspaceClient.authenticate):
the same scopes, credential and token paths (GOOGLE_CREDENTIALS_PATH,
GOOGLE_TOKEN_PATH), token refresh, and revoked-token handling JARVIS uses
while it runs. This script used to carry its own copy of all of it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _rule(title: str = "") -> None:
    print("=" * 60)
    if title:
        print(f"  {title}")
        print("=" * 60)


async def _verify(client) -> bool:
    """Read once through each service with the new sign-in."""
    checks = (
        ("Gmail", client.fetch_unread_emails(limit=5), lambda r: f"{r.get('count', len(r.get('emails', [])))} unread"),
        ("Calendar", client.get_calendar_events(days=1), lambda r: f"{len(r.get('events', []))} event(s) today"),
        ("Contacts", client.get_contacts(limit=5), lambda r: f"{len(r.get('contacts', []))} contact(s) read"),
    )
    ok = True
    for name, call, describe in checks:
        try:
            result = await call
        except Exception as exc:  # noqa: BLE001 -- report each service on its own
            result = {"error": str(exc)}
        if isinstance(result, dict) and result.get("error"):
            ok = False
            print(f"    {name:<9} FAILED - {result['error']}")
        else:
            print(f"    {name:<9} {describe(result if isinstance(result, dict) else {})}")
    return ok


async def main() -> int:
    from backend.core.env_bootstrap import load_env_once
    load_env_once()
    from backend.neural_mesh.agents.google_workspace_agent import (
        GOOGLE_API_AVAILABLE, GOOGLE_WORKSPACE_SCOPES, GoogleWorkspaceClient, GoogleWorkspaceConfig,
    )

    config = GoogleWorkspaceConfig()
    _rule("JARVIS Google Workspace sign-in")
    print(f"  Credentials: {config.credentials_path}")
    print(f"  Token:       {config.token_path}")
    print(f"  Scopes:      {len(GOOGLE_WORKSPACE_SCOPES)}")
    print()

    if not GOOGLE_API_AVAILABLE:
        print("  The Google API packages are not installed in this Python:")
        print(f"    {sys.executable}")
        print("  Install them (they are in backend/requirements-windows.txt):")
        print("    google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2")
        return 1

    if not Path(config.credentials_path).exists():
        print("  No Google OAuth client file yet. JARVIS signs in as an app you own:")
        print("    1. https://console.cloud.google.com -> APIs & Services -> Credentials")
        print("    2. Create credentials -> OAuth client ID -> Application type: Desktop app")
        print("    3. Enable the Gmail, Google Calendar, People and Google Drive APIs")
        print("    4. Download the client's JSON and save it as:")
        print(f"         {config.credentials_path}")
        print("       (or copy the one you already use on another machine)")
        print("  Then run this again.")
        return 1

    print("  Opening your browser for Google sign-in...")
    print("  (Sign in and allow the requested access; this window waits for you.)")
    print()
    client = GoogleWorkspaceClient(config)
    if not await client.authenticate(interactive=True):
        print(f"  Sign-in did not complete (state: {client.auth_state.value}).")
        reason = getattr(client, "auth_failure_reason", None)
        if reason:
            print(f"  Reason: {reason}")
        return 1

    print(f"  Signed in. Token saved to {config.token_path}")
    print()
    print("  Checking the services...")
    verified = await _verify(client)
    print()
    _rule("Google Workspace is connected" if verified else "Signed in; some services failed (above)")
    return 0 if verified else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
