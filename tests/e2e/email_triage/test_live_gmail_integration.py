"""Live Gmail integration tests (optional, slow).

These tests exercise real Gmail API calls with a service account.
They are gated behind RUN_LIVE_GMAIL_TESTS=true env var and are
skipped by default in CI.

Safety:
- Unique label prefix: jarvis/test/<uuid>/ per test session
- Cleanup in finally blocks: all created labels removed on exit
- Read-only where possible (fetch, not modify)
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest

from tests.e2e.email_triage.conftest import (
    make_triage_config,
    make_mock_notifier,
)

# Skip all tests unless RUN_LIVE_GMAIL_TESTS=true
pytestmark = [
    pytest.mark.slow,
    pytest.mark.api,
    pytest.mark.skipif(
        not os.getenv("RUN_LIVE_GMAIL_TESTS", "").lower() == "true",
        reason="Live Gmail tests disabled (set RUN_LIVE_GMAIL_TESTS=true)",
    ),
]

# Unique test run prefix for label safety
_TEST_RUN_ID = uuid.uuid4().hex[:8]
_LABEL_PREFIX = f"jarvis/test/{_TEST_RUN_ID}"


def _get_gmail_service():
    """Build a Gmail API service from service account credentials.

    Requires GMAIL_SERVICE_ACCOUNT_KEY (path to JSON key file) and
    GMAIL_TEST_USER (email address for delegated access).
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        key_path = os.getenv("GMAIL_SERVICE_ACCOUNT_KEY", "credentials.json")
        test_user = os.getenv("GMAIL_TEST_USER")

        if not test_user:
            pytest.skip("GMAIL_TEST_USER not set")

        creds = service_account.Credentials.from_service_account_file(
            key_path,
            scopes=["https://mail.google.com/"],
            subject=test_user,
        )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except ImportError:
        pytest.skip("google-auth / google-api-python-client not installed")
    except FileNotFoundError:
        pytest.skip(f"Service account key not found at {key_path}")


def _cleanup_labels(gmail_svc, label_ids):
    """Delete labels by ID, ignoring errors."""
    for label_id in label_ids:
        try:
            gmail_svc.users().labels().delete(
                userId="me", id=label_id,
            ).execute()
        except Exception:
            pass


class TestLiveGmailIntegration:
    """Live Gmail API integration tests."""

    @pytest.mark.asyncio
    async def test_label_creation_and_cleanup(self):
        """Create jarvis/* labels via Gmail API, verify, then clean up."""
        gmail_svc = _get_gmail_service()
        created_label_ids = []

        try:
            # Create 4 tier labels with unique prefix
            tier_labels = [
                f"{_LABEL_PREFIX}/tier1_critical",
                f"{_LABEL_PREFIX}/tier2_high",
                f"{_LABEL_PREFIX}/tier3_routine",
                f"{_LABEL_PREFIX}/tier4_noise",
            ]

            for label_name in tier_labels:
                result = gmail_svc.users().labels().create(
                    userId="me",
                    body={"name": label_name, "labelListVisibility": "labelShow"},
                ).execute()
                created_label_ids.append(result["id"])

            # Verify all 4 exist
            labels_response = gmail_svc.users().labels().list(userId="me").execute()
            existing_names = {l["name"] for l in labels_response.get("labels", [])}
            for label_name in tier_labels:
                assert label_name in existing_names, f"Label {label_name} not found"

        finally:
            _cleanup_labels(gmail_svc, created_label_ids)

    @pytest.mark.asyncio
    async def test_fetch_unread_returns_valid_structure(self):
        """Fetch up to 5 unread emails, verify structure."""
        gmail_svc = _get_gmail_service()

        result = gmail_svc.users().messages().list(
            userId="me",
            labelIds=["INBOX", "UNREAD"],
            maxResults=5,
        ).execute()

        messages = result.get("messages", [])
        # May be empty if no unread emails — that's OK
        for msg_stub in messages:
            assert "id" in msg_stub
            assert isinstance(msg_stub["id"], str)
            assert len(msg_stub["id"]) > 0

            # Fetch full message to check structure
            full_msg = gmail_svc.users().messages().get(
                userId="me", id=msg_stub["id"],
            ).execute()

            assert "snippet" in full_msg
            assert "labelIds" in full_msg

    @pytest.mark.asyncio
    async def test_apply_label_to_message(self):
        """Fetch 1 email, apply test label, verify, then clean up."""
        gmail_svc = _get_gmail_service()
        created_label_ids = []
        modified_msg_id = None
        applied_label_id = None

        try:
            # Create a test label
            label_name = f"{_LABEL_PREFIX}/test_apply"
            result = gmail_svc.users().labels().create(
                userId="me",
                body={"name": label_name, "labelListVisibility": "labelShow"},
            ).execute()
            applied_label_id = result["id"]
            created_label_ids.append(applied_label_id)

            # Fetch 1 email
            list_result = gmail_svc.users().messages().list(
                userId="me", maxResults=1,
            ).execute()
            messages = list_result.get("messages", [])
            if not messages:
                pytest.skip("No emails in mailbox")

            modified_msg_id = messages[0]["id"]

            # Apply label
            gmail_svc.users().messages().modify(
                userId="me",
                id=modified_msg_id,
                body={"addLabelIds": [applied_label_id]},
            ).execute()

            # Verify label applied
            msg = gmail_svc.users().messages().get(
                userId="me", id=modified_msg_id,
            ).execute()
            assert applied_label_id in msg.get("labelIds", [])

        finally:
            # Remove label from message
            if modified_msg_id and applied_label_id:
                try:
                    gmail_svc.users().messages().modify(
                        userId="me",
                        id=modified_msg_id,
                        body={"removeLabelIds": [applied_label_id]},
                    ).execute()
                except Exception:
                    pass
            _cleanup_labels(gmail_svc, created_label_ids)

    @pytest.mark.asyncio
    async def test_full_triage_cycle_with_real_inbox(self):
        """Run full triage cycle with real Gmail, mock notifier."""
        gmail_svc = _get_gmail_service()

        from unittest.mock import MagicMock, AsyncMock
        from autonomy.email_triage.runner import EmailTriageRunner
        from autonomy.email_triage.config import reset_triage_config

        # Build a minimal workspace agent wrapping real Gmail
        agent = MagicMock()
        agent._gmail_service = gmail_svc

        async def _fetch_real_emails(params):
            result = gmail_svc.users().messages().list(
                userId="me",
                labelIds=["INBOX", "UNREAD"],
                maxResults=params.get("limit", 5),
            ).execute()
            messages = []
            for stub in result.get("messages", [])[:5]:
                full = gmail_svc.users().messages().get(
                    userId="me", id=stub["id"],
                ).execute()
                headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
                messages.append({
                    "id": full["id"],
                    "from": headers.get("From", "unknown@unknown.com"),
                    "subject": headers.get("Subject", "(no subject)"),
                    "snippet": full.get("snippet", ""),
                    "labels": full.get("labelIds", []),
                })
            return {"emails": messages}

        agent._fetch_unread_emails = AsyncMock(side_effect=_fetch_real_emails)

        notifier = make_mock_notifier()
        config = make_triage_config()

        EmailTriageRunner._instance = None
        reset_triage_config()

        try:
            runner = EmailTriageRunner(
                config=config,
                workspace_agent=agent,
                router=None,  # heuristic only
                notifier=notifier,
            )
            report = await runner.run_cycle()

            from autonomy.email_triage.schemas import TriageCycleReport
            assert isinstance(report, TriageCycleReport)

            # Should have zero unhandled errors (even if inbox is empty)
            process_errors = [e for e in report.errors if e.startswith("process:")]
            assert len(process_errors) == 0, f"Processing errors: {process_errors}"

            assert report.snapshot_committed is True

        finally:
            EmailTriageRunner._instance = None
            reset_triage_config()

    @pytest.mark.asyncio
    async def test_extraction_with_real_emails(self):
        """Extract features from real emails using heuristic."""
        gmail_svc = _get_gmail_service()

        from autonomy.email_triage.extraction import extract_features

        # Fetch up to 3 emails
        result = gmail_svc.users().messages().list(
            userId="me", maxResults=3,
        ).execute()

        messages_raw = []
        for stub in result.get("messages", []):
            full = gmail_svc.users().messages().get(
                userId="me", id=stub["id"],
            ).execute()
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
            messages_raw.append({
                "id": full["id"],
                "from": headers.get("From", "unknown@unknown.com"),
                "subject": headers.get("Subject", "(no subject)"),
                "snippet": full.get("snippet", ""),
                "labels": full.get("labelIds", []),
            })

        if not messages_raw:
            pytest.skip("No emails in mailbox")

        config = make_triage_config()
        for email in messages_raw:
            features = await extract_features(email, router=None, config=config)
            assert features.message_id == email["id"]
            assert len(features.sender) > 0
            assert len(features.subject) > 0
            assert features.extraction_confidence == 0.0  # Heuristic only
