"""
Atomic Google Workspace tools -- email, calendar, contacts, drive.

These tools provide the Communicator context with Google API access.
Delegates to the existing GoogleWorkspaceAgent which handles OAuth,
token refresh, caching, circuit breaking, and 3-tier fallback
(API -> native macOS -> computer use).

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_WORKSPACE_TIMEOUT_S = float(os.environ.get("TOOL_WORKSPACE_TIMEOUT_S", "30.0"))

# Lazy-initialized workspace agent
_workspace = None
_workspace_lock = asyncio.Lock()


@dataclass(frozen=True)
class Email:
    """An email message.

    Attributes:
        id: Gmail message ID.
        thread_id: Gmail thread ID.
        subject: Email subject line.
        sender: Sender email address.
        date: Date string.
        snippet: Brief preview text.
        is_unread: Whether the message is unread.
    """
    id: str
    thread_id: str
    subject: str
    sender: str
    date: str
    snippet: str
    is_unread: bool = True


@dataclass(frozen=True)
class CalendarEvent:
    """A calendar event.

    Attributes:
        id: Google Calendar event ID.
        title: Event title/summary.
        start: ISO datetime string for event start.
        end: ISO datetime string for event end.
        location: Event location (empty if none).
        description: Event description/notes.
        attendees: List of attendee email addresses.
        is_all_day: Whether this is an all-day event.
        meeting_link: Video call link (empty if none).
    """
    id: str
    title: str
    start: str
    end: str
    location: str = ""
    description: str = ""
    attendees: List[str] = field(default_factory=list)
    is_all_day: bool = False
    meeting_link: str = ""


async def fetch_unread_emails(limit: int = 10) -> List[Email]:
    """Fetch unread emails from Gmail inbox.

    Returns the most recent unread emails with subject, sender, date,
    and preview snippet.  Handles OAuth token refresh automatically.

    Args:
        limit: Maximum number of emails to fetch (default 10).

    Returns:
        List of Email objects sorted by date (newest first).
        Empty list if Gmail is not authenticated or fetch fails.

    Use when:
        The Communicator needs to check for new messages, triage inbox,
        or report unread email count to the user.
    """
    agent = await _get_workspace()
    if agent is None:
        return []

    try:
        result = await asyncio.wait_for(
            agent.fetch_unread_emails(limit=limit),
            timeout=_WORKSPACE_TIMEOUT_S,
        )
        return [
            Email(
                id=e.get("id", ""),
                thread_id=e.get("thread_id", ""),
                subject=e.get("subject", "(no subject)"),
                sender=e.get("from", ""),
                date=e.get("date", ""),
                snippet=e.get("snippet", ""),
                is_unread=True,
            )
            for e in result.get("emails", [])
        ]
    except Exception as exc:
        logger.error("[tool:workspace] fetch_unread_emails error: %s", exc)
        return []


async def send_email(
    to: str,
    subject: str,
    body: str,
    html_body: Optional[str] = None,
) -> bool:
    """Send an email via Gmail.

    Composes and sends an email.  Supports plain text and optional HTML
    body (multipart/alternative).

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain text email body.
        html_body: Optional HTML body (sent as alternative to plain text).

    Returns:
        True if the email was sent successfully.

    Use when:
        The Communicator needs to send an email on behalf of the user
        (reply to a message, compose new email, send notification).
    """
    agent = await _get_workspace()
    if agent is None:
        return False

    try:
        result = await asyncio.wait_for(
            agent.send_email(to=to, subject=subject, body=body, html_body=html_body),
            timeout=_WORKSPACE_TIMEOUT_S,
        )
        success = result.get("status") == "sent"
        if success:
            logger.info("[tool:workspace] Email sent to %s: %s", to, subject)
        return success
    except Exception as exc:
        logger.error("[tool:workspace] send_email error: %s", exc)
        return False


async def search_emails(query: str, limit: int = 10) -> List[Email]:
    """Search Gmail with a query string.

    Uses Gmail's search syntax (supports from:, subject:, is:unread, etc.).

    Args:
        query: Gmail search query (e.g., "from:meryem subject:doubleword").
        limit: Maximum results (default 10).

    Returns:
        List of matching Email objects.

    Use when:
        The Communicator needs to find specific emails (e.g., "find
        Meryem's email about the partnership").
    """
    agent = await _get_workspace()
    if agent is None:
        return []

    try:
        result = await asyncio.wait_for(
            agent.search_emails(query=query, limit=limit),
            timeout=_WORKSPACE_TIMEOUT_S,
        )
        return [
            Email(
                id=e.get("id", ""),
                thread_id=e.get("thread_id", ""),
                subject=e.get("subject", ""),
                sender=e.get("from", ""),
                date=e.get("date", ""),
                snippet=e.get("snippet", ""),
                is_unread=e.get("is_unread", False),
            )
            for e in result.get("emails", [])
        ]
    except Exception as exc:
        logger.error("[tool:workspace] search_emails error: %s", exc)
        return []


async def get_calendar_events(
    date: Optional[str] = None,
    days: int = 1,
) -> List[CalendarEvent]:
    """Get calendar events for a date range.

    Fetches events from Google Calendar.  Defaults to today if no date
    is specified.

    Args:
        date: ISO date string (e.g., "2026-03-24"). Defaults to today.
        days: Number of days to fetch (default 1 = today only).

    Returns:
        List of CalendarEvent objects sorted by start time.

    Use when:
        The Communicator needs to check the user's schedule, find free
        time slots, or report upcoming meetings.
    """
    agent = await _get_workspace()
    if agent is None:
        return []

    try:
        result = await asyncio.wait_for(
            agent.get_calendar_events(date_str=date, days=days),
            timeout=_WORKSPACE_TIMEOUT_S,
        )
        return [
            CalendarEvent(
                id=e.get("id", ""),
                title=e.get("title", ""),
                start=e.get("start", ""),
                end=e.get("end", ""),
                location=e.get("location", ""),
                description=e.get("description", ""),
                attendees=e.get("attendees", []),
                is_all_day=e.get("is_all_day", False),
                meeting_link=e.get("meeting_link", ""),
            )
            for e in result.get("events", [])
        ]
    except Exception as exc:
        logger.error("[tool:workspace] get_calendar_events error: %s", exc)
        return []


async def create_calendar_event(
    title: str,
    start: str,
    end: Optional[str] = None,
    description: str = "",
    location: str = "",
    attendees: Optional[List[str]] = None,
) -> Optional[str]:
    """Create a new Google Calendar event.

    Args:
        title: Event title/summary.
        start: ISO datetime string for start (e.g., "2026-03-25T10:00:00").
        end: ISO datetime string for end.  If not provided, defaults to
            start + 30 minutes.
        description: Optional event description/notes.
        location: Optional event location.
        attendees: Optional list of attendee email addresses.

    Returns:
        Event ID string if created successfully.  None on failure.

    Use when:
        The Communicator needs to schedule a meeting, set a reminder,
        or create a calendar event on behalf of the user.
    """
    agent = await _get_workspace()
    if agent is None:
        return None

    try:
        result = await asyncio.wait_for(
            agent.create_calendar_event(
                title=title, start=start, end=end,
                description=description, location=location,
                attendees=attendees or [],
            ),
            timeout=_WORKSPACE_TIMEOUT_S,
        )
        event_id = result.get("event_id")
        if event_id:
            logger.info("[tool:workspace] Created event: %s (%s)", title, event_id)
        return event_id
    except Exception as exc:
        logger.error("[tool:workspace] create_calendar_event error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Internal: delegate to GoogleWorkspaceAgent
# ---------------------------------------------------------------------------

async def _get_workspace():
    """Get or create the GoogleWorkspaceAgent singleton."""
    global _workspace

    if _workspace is not None:
        return _workspace

    async with _workspace_lock:
        if _workspace is not None:
            return _workspace

        for import_path in ("backend.neural_mesh.agents.google_workspace_agent",
                            "neural_mesh.agents.google_workspace_agent"):
            try:
                import importlib
                mod = importlib.import_module(import_path)
                cls = mod.GoogleWorkspaceAgent
                _workspace = cls()
                auth_ok = await _workspace.authenticate(interactive=False)
                if auth_ok:
                    logger.info("[tool:workspace] GoogleWorkspaceAgent authenticated")
                else:
                    logger.warning("[tool:workspace] Google auth failed (non-interactive)")
                return _workspace
            except (ImportError, Exception) as exc:
                logger.debug("[tool:workspace] %s: %s", import_path, exc)
                continue

        logger.warning("[tool:workspace] GoogleWorkspaceAgent not available")
        return None
