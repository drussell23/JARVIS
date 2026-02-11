#!/usr/bin/env python3
"""
Google Workspace OAuth Setup for JARVIS.

Triggers the one-time OAuth authorization flow to create
~/.jarvis/google_workspace_token.json with full workspace scopes.

Usage:
    python3 backend/scripts/google_oauth_setup.py

This will open your browser. Sign in with your Google account and
grant the requested permissions. The token is saved automatically.
"""

import os
import sys
import json
from pathlib import Path

# Suppress noisy warnings
import warnings
warnings.filterwarnings("ignore")

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Same scopes as GoogleWorkspaceAgent (google_workspace_agent.py:576-589)
SCOPES = [
    # Gmail
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/gmail.modify',
    # Calendar
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events',
    # Drive (for attachments)
    'https://www.googleapis.com/auth/drive.file',
    # Contacts
    'https://www.googleapis.com/auth/contacts.readonly',
]

JARVIS_DIR = Path.home() / '.jarvis'
CREDENTIALS_PATH = JARVIS_DIR / 'google_credentials.json'
TOKEN_PATH = JARVIS_DIR / 'google_workspace_token.json'


def main():
    print("=" * 60)
    print("  JARVIS Google Workspace OAuth Setup")
    print("=" * 60)
    print()

    # Check credentials file
    if not CREDENTIALS_PATH.exists():
        print(f"  ERROR: Credentials file not found at {CREDENTIALS_PATH}")
        print("  You need a Google Cloud OAuth client credentials file.")
        sys.exit(1)

    print(f"  Credentials: {CREDENTIALS_PATH}")
    print(f"  Token path:  {TOKEN_PATH}")
    print(f"  Scopes:      {len(SCOPES)} (Gmail, Calendar, Drive, Contacts)")
    print()

    creds = None

    # Check for existing token
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            if creds and creds.valid:
                print("  Token already exists and is valid.")
                _verify_services(creds)
                return
            elif creds and creds.expired and creds.refresh_token:
                print("  Token expired, refreshing...")
                creds.refresh(Request())
                _save_token(creds)
                print("  Token refreshed successfully.")
                _verify_services(creds)
                return
            else:
                print("  Existing token is invalid. Starting fresh OAuth flow...")
                creds = None
        except Exception as e:
            print(f"  Existing token failed to load: {e}")
            creds = None

    # Run OAuth flow
    print("  Opening browser for Google sign-in...")
    print("  (Sign in and grant all requested permissions)")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_PATH), SCOPES
    )
    creds = flow.run_local_server(port=0)

    # Save token
    _save_token(creds)
    print()
    print(f"  Token saved to {TOKEN_PATH}")
    print()

    # Verify
    _verify_services(creds)


def _save_token(creds):
    """Save credentials to token file."""
    os.makedirs(str(JARVIS_DIR), exist_ok=True)
    with open(str(TOKEN_PATH), 'w') as f:
        f.write(creds.to_json())


def _verify_services(creds):
    """Verify the token works by testing each service."""
    print("  Verifying services...")
    print()

    # Gmail
    try:
        gmail = build('gmail', 'v1', credentials=creds)
        profile = gmail.users().getProfile(userId='me').execute()
        email = profile.get('emailAddress', 'unknown')
        total = profile.get('messagesTotal', 0)
        print(f"    Gmail:     {email} ({total:,} messages)")
    except Exception as e:
        print(f"    Gmail:     FAILED - {e}")

    # Calendar
    try:
        cal = build('calendar', 'v3', credentials=creds)
        from datetime import datetime, timedelta
        now = datetime.utcnow().isoformat() + 'Z'
        tomorrow = (datetime.utcnow() + timedelta(days=1)).isoformat() + 'Z'
        events = cal.events().list(
            calendarId='primary',
            timeMin=now,
            timeMax=tomorrow,
            maxResults=5,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        items = events.get('items', [])
        print(f"    Calendar:  {len(items)} event(s) in next 24h")
        for item in items[:3]:
            start = item.get('start', {}).get('dateTime', item.get('start', {}).get('date', ''))
            summary = item.get('summary', '(no title)')
            if 'T' in start:
                start = start.split('T')[1][:5]
            print(f"               - {start}: {summary}")
    except Exception as e:
        print(f"    Calendar:  FAILED - {e}")

    # Contacts
    try:
        people = build('people', 'v1', credentials=creds)
        results = people.people().connections().list(
            resourceName='people/me',
            pageSize=5,
            personFields='names'
        ).execute()
        connections = results.get('connections', [])
        total = results.get('totalPeople', len(connections))
        print(f"    Contacts:  {total} contact(s)")
    except Exception as e:
        print(f"    Contacts:  FAILED - {e}")

    # Drive
    try:
        drive = build('drive', 'v3', credentials=creds)
        about = drive.about().get(fields='user').execute()
        drive_email = about.get('user', {}).get('emailAddress', 'unknown')
        print(f"    Drive:     Connected ({drive_email})")
    except Exception as e:
        print(f"    Drive:     FAILED - {e}")

    print()
    print("=" * 60)
    print("  Google Workspace Tier 1 is READY")
    print("=" * 60)


if __name__ == "__main__":
    main()
