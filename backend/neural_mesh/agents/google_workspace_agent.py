"""
JARVIS Neural Mesh - Google Workspace Agent
=============================================

A production agent specialized in Google Workspace administration and communication.
Handles Gmail, Calendar, Drive, and Contacts integrations for the "Chief of Staff" role.

Capabilities:
- fetch_unread_emails: Get unread emails with intelligent filtering
- check_calendar_events: View calendar events for any date
- draft_email_reply: Create draft email responses
- send_email: Send emails directly
- search_email: Search emails with advanced queries
- create_calendar_event: Schedule new events
- get_contacts: Retrieve contact information
- workspace_summary: Get daily briefing summary

This agent handles all "Admin" and "Communication" tasks, enabling JARVIS to:
- "Check my schedule"
- "Draft an email to Mitra"
- "What meetings do I have today?"
- "Send an email about the project update"

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeType,
    MessageType,
    MessagePriority,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Google API Availability Check
# =============================================================================

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    import base64
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False
    logger.warning(
        "Google API libraries not available. Install: "
        "pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
    )


# =============================================================================
# Configuration
# =============================================================================

# OAuth 2.0 scopes for Google Workspace
GOOGLE_WORKSPACE_SCOPES = [
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


@dataclass
class GoogleWorkspaceConfig:
    """Configuration for Google Workspace Agent."""

    credentials_path: str = field(
        default_factory=lambda: os.getenv(
            'GOOGLE_CREDENTIALS_PATH',
            str(Path.home() / '.jarvis' / 'google_credentials.json')
        )
    )
    token_path: str = field(
        default_factory=lambda: os.getenv(
            'GOOGLE_TOKEN_PATH',
            str(Path.home() / '.jarvis' / 'google_workspace_token.json')
        )
    )
    # Email defaults
    default_email_limit: int = 10
    max_email_body_preview: int = 500
    # Calendar defaults
    calendar_lookahead_days: int = 7
    default_event_duration_minutes: int = 60
    # Caching
    cache_ttl_seconds: float = 300.0  # 5 minutes
    # Retry
    max_retries: int = 3
    retry_delay_seconds: float = 1.0


# =============================================================================
# Intent Detection for Routing
# =============================================================================

class WorkspaceIntent(Enum):
    """Types of workspace intents this agent handles."""

    # Email
    CHECK_EMAIL = "check_email"
    SEND_EMAIL = "send_email"
    DRAFT_EMAIL = "draft_email"
    SEARCH_EMAIL = "search_email"

    # Calendar
    CHECK_CALENDAR = "check_calendar"
    CREATE_EVENT = "create_event"
    FIND_FREE_TIME = "find_free_time"

    # General
    DAILY_BRIEFING = "daily_briefing"
    GET_CONTACTS = "get_contacts"

    # Unknown
    UNKNOWN = "unknown"


class WorkspaceIntentDetector:
    """
    Detects workspace-related intents from natural language queries.

    This enables intelligent routing so that queries like:
    - "Check my schedule" → CHECK_CALENDAR
    - "Draft an email to Mitra" → DRAFT_EMAIL
    - "What meetings today?" → CHECK_CALENDAR
    """

    # Intent patterns (lowercase) - more precise patterns that must match as phrases
    INTENT_PATTERNS: Dict[WorkspaceIntent, List[str]] = {
        WorkspaceIntent.DRAFT_EMAIL: [
            "draft email", "draft an email", "write email", "compose email",
            "draft reply", "write a reply", "draft response", "draft to",
            "write an email", "compose a reply",
        ],
        WorkspaceIntent.SEND_EMAIL: [
            "send email", "send an email", "send message", "send a message",
            "email to", "message to",
        ],
        WorkspaceIntent.CHECK_EMAIL: [
            "check email", "check my email", "any emails", "new emails",
            "any new emails", "unread email", "unread emails", "my inbox",
            "show inbox", "what emails", "read my email", "show email",
            "show my email", "check inbox", "any new mail", "check mail",
        ],
        WorkspaceIntent.SEARCH_EMAIL: [
            "search email", "find email", "look for email", "emails from",
            "emails about", "emails containing", "search inbox", "find emails",
        ],
        WorkspaceIntent.CHECK_CALENDAR: [
            "check calendar", "check my calendar", "my schedule", "my meetings",
            "what's on my calendar", "calendar today", "upcoming events",
            "what meetings", "events today", "what's on today",
            "agenda", "appointments", "busy today", "today's calendar",
            "schedule today", "schedule for today", "meetings today",
            "what do i have today", "what's happening today",
        ],
        WorkspaceIntent.CREATE_EVENT: [
            "schedule meeting", "create event", "add event", "schedule event",
            "book meeting", "set up meeting", "calendar event", "add to calendar",
            "create a meeting", "schedule a meeting",
        ],
        WorkspaceIntent.FIND_FREE_TIME: [
            "when am i free", "free time", "my availability", "open slots",
            "find time", "when available", "schedule time", "free slots",
        ],
        WorkspaceIntent.DAILY_BRIEFING: [
            "daily briefing", "morning briefing", "daily summary",
            "today's agenda", "brief me", "catch me up", "what's today",
            "give me a briefing", "morning summary", "give me my briefing",
        ],
        WorkspaceIntent.GET_CONTACTS: [
            "contact info", "email address for", "phone number for",
            "contact for", "find contact", "get contact",
        ],
    }

    # Required keywords for each intent (at least one must be present for match)
    REQUIRED_KEYWORDS: Dict[WorkspaceIntent, Set[str]] = {
        WorkspaceIntent.CHECK_EMAIL: {"email", "emails", "inbox", "mail"},
        WorkspaceIntent.SEND_EMAIL: {"send", "email"},
        WorkspaceIntent.DRAFT_EMAIL: {"draft", "compose", "write", "email"},
        WorkspaceIntent.SEARCH_EMAIL: {"search", "find", "email", "emails"},
        WorkspaceIntent.CHECK_CALENDAR: {"calendar", "schedule", "meeting", "meetings", "agenda", "events", "appointments"},
        WorkspaceIntent.CREATE_EVENT: {"schedule", "create", "add", "book", "meeting", "event"},
        WorkspaceIntent.FIND_FREE_TIME: {"free", "available", "availability"},
        WorkspaceIntent.DAILY_BRIEFING: {"briefing", "summary", "brief", "catch"},  # "catch me up"
        WorkspaceIntent.GET_CONTACTS: {"contact", "phone", "address"},
    }

    # Name extraction patterns
    NAME_PATTERNS = [
        r"email (?:to|for) (\w+)",
        r"message (?:to|for) (\w+)",
        r"draft (?:to|for) (\w+)",
        r"contact (?:info )?(?:for )?(\w+)",
        r"meeting with (\w+)",
        r"schedule with (\w+)",
        r"to (\w+)$",  # "send email to John"
    ]

    def detect(self, query: str) -> Tuple[WorkspaceIntent, float, Dict[str, Any]]:
        """
        Detect workspace intent from a natural language query.

        Args:
            query: The user's query

        Returns:
            Tuple of (intent, confidence, metadata)
        """
        query_lower = query.lower().strip()
        # Strip punctuation from words for keyword matching
        query_words = set(
            word.strip("?!.,;:'\"") for word in query_lower.split()
        )

        # Score each intent
        scores: Dict[WorkspaceIntent, float] = {}

        for intent, patterns in self.INTENT_PATTERNS.items():
            # First check if required keywords are present
            required = self.REQUIRED_KEYWORDS.get(intent, set())
            if required and not any(kw in query_words for kw in required):
                continue  # Skip this intent if no required keywords

            score = 0.0
            matched_patterns = []

            for pattern in patterns:
                if pattern in query_lower:
                    # Full phrase match gets high score
                    score += 2.0
                    matched_patterns.append(pattern)

            # Only count if we had phrase matches
            if score > 0:
                scores[intent] = score

        if not scores:
            return WorkspaceIntent.UNKNOWN, 0.0, {}

        # Get best match
        best_intent = max(scores, key=scores.get)
        best_score = scores[best_intent]

        # Normalize confidence (2.0 per pattern match, expect 1-2 matches for good confidence)
        confidence = min(1.0, best_score / 4.0)

        # Extract metadata
        metadata = {
            "matched_intent": best_intent.value,
            "all_scores": {k.value: v for k, v in scores.items()},
            "extracted_names": self._extract_names(query),
            "extracted_dates": self._extract_dates(query),
        }

        return best_intent, confidence, metadata

    def _extract_names(self, query: str) -> List[str]:
        """Extract person names from query."""
        names = []
        for pattern in self.NAME_PATTERNS:
            matches = re.findall(pattern, query, re.IGNORECASE)
            names.extend(matches)
        return list(set(names))

    def _extract_dates(self, query: str) -> Dict[str, Any]:
        """Extract date references from query."""
        query_lower = query.lower()
        dates = {}

        if "today" in query_lower:
            dates["today"] = date.today().isoformat()
        if "tomorrow" in query_lower:
            dates["tomorrow"] = (date.today() + timedelta(days=1)).isoformat()
        if "yesterday" in query_lower:
            dates["yesterday"] = (date.today() - timedelta(days=1)).isoformat()
        if "this week" in query_lower:
            dates["week_start"] = (date.today() - timedelta(days=date.today().weekday())).isoformat()
            dates["week_end"] = (date.today() + timedelta(days=6 - date.today().weekday())).isoformat()
        if "next week" in query_lower:
            next_monday = date.today() + timedelta(days=7 - date.today().weekday())
            dates["next_week_start"] = next_monday.isoformat()
            dates["next_week_end"] = (next_monday + timedelta(days=6)).isoformat()

        return dates

    def is_workspace_query(self, query: str) -> Tuple[bool, float]:
        """
        Check if a query is workspace-related (for routing decisions).

        Returns:
            Tuple of (is_workspace_related, confidence)
        """
        intent, confidence, _ = self.detect(query)
        is_workspace = intent != WorkspaceIntent.UNKNOWN
        return is_workspace, confidence


# =============================================================================
# Google API Client
# =============================================================================

class GoogleWorkspaceClient:
    """
    Async-compatible client for Google Workspace APIs.

    Handles authentication and provides methods for:
    - Gmail operations
    - Calendar operations
    - Contacts operations
    """

    def __init__(self, config: Optional[GoogleWorkspaceConfig] = None):
        """Initialize the Google Workspace client."""
        self.config = config or GoogleWorkspaceConfig()
        self._creds: Optional[Any] = None
        self._gmail_service = None
        self._calendar_service = None
        self._people_service = None
        self._authenticated = False
        self._lock = asyncio.Lock()

        # Cache
        self._cache: Dict[str, Tuple[Any, float]] = {}

    async def authenticate(self) -> bool:
        """
        Authenticate with Google APIs.

        Returns:
            True if authentication successful
        """
        if not GOOGLE_API_AVAILABLE:
            logger.error("Google API libraries not available")
            return False

        async with self._lock:
            if self._authenticated:
                return True

            try:
                # Run OAuth in thread pool (it's blocking)
                loop = asyncio.get_event_loop()
                success = await loop.run_in_executor(
                    None, self._authenticate_sync
                )
                self._authenticated = success
                return success

            except Exception as e:
                logger.exception(f"Authentication failed: {e}")
                return False

    def _authenticate_sync(self) -> bool:
        """Synchronous authentication (run in thread pool)."""
        try:
            # Check for existing token
            if os.path.exists(self.config.token_path):
                self._creds = Credentials.from_authorized_user_file(
                    self.config.token_path, GOOGLE_WORKSPACE_SCOPES
                )

            # Refresh or get new credentials
            if not self._creds or not self._creds.valid:
                if self._creds and self._creds.expired and self._creds.refresh_token:
                    logger.info("Refreshing Google OAuth token...")
                    self._creds.refresh(Request())
                else:
                    if not os.path.exists(self.config.credentials_path):
                        logger.error(
                            f"Google credentials file not found: {self.config.credentials_path}"
                        )
                        return False

                    logger.info("Starting OAuth flow for Google Workspace...")
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self.config.credentials_path, GOOGLE_WORKSPACE_SCOPES
                    )
                    self._creds = flow.run_local_server(port=0)

                # Save token
                os.makedirs(os.path.dirname(self.config.token_path), exist_ok=True)
                with open(self.config.token_path, 'w') as token:
                    token.write(self._creds.to_json())

            # Build services
            self._gmail_service = build('gmail', 'v1', credentials=self._creds)
            self._calendar_service = build('calendar', 'v3', credentials=self._creds)
            self._people_service = build('people', 'v1', credentials=self._creds)

            logger.info("Google Workspace APIs authenticated successfully")
            return True

        except Exception as e:
            logger.exception(f"Sync authentication failed: {e}")
            return False

    async def _ensure_authenticated(self) -> bool:
        """Ensure client is authenticated."""
        if not self._authenticated:
            return await self.authenticate()
        return True

    def _get_cached(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        if key in self._cache:
            value, timestamp = self._cache[key]
            if (datetime.now().timestamp() - timestamp) < self.config.cache_ttl_seconds:
                return value
            del self._cache[key]
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        """Cache a value."""
        self._cache[key] = (value, datetime.now().timestamp())

    # =========================================================================
    # Gmail Operations
    # =========================================================================

    async def fetch_unread_emails(
        self,
        limit: int = 10,
        label: str = "INBOX",
    ) -> Dict[str, Any]:
        """
        Fetch unread emails.

        Args:
            limit: Maximum number of emails to fetch
            label: Label to filter by

        Returns:
            Dictionary with email list and metadata
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated", "emails": []}

        cache_key = f"unread:{label}:{limit}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._fetch_unread_sync(limit, label)
            )
            self._set_cached(cache_key, result)
            return result

        except Exception as e:
            logger.exception(f"Error fetching emails: {e}")
            return {"error": str(e), "emails": []}

    def _fetch_unread_sync(self, limit: int, label: str) -> Dict[str, Any]:
        """Synchronous email fetch."""
        results = self._gmail_service.users().messages().list(
            userId='me',
            labelIds=[label, 'UNREAD'],
            maxResults=limit,
        ).execute()

        messages = results.get('messages', [])
        emails = []

        for msg_data in messages:
            msg = self._gmail_service.users().messages().get(
                userId='me',
                id=msg_data['id'],
                format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Date'],
            ).execute()

            headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}

            emails.append({
                "id": msg['id'],
                "thread_id": msg['threadId'],
                "from": headers.get('From', 'Unknown'),
                "to": headers.get('To', ''),
                "subject": headers.get('Subject', '(no subject)'),
                "date": headers.get('Date', ''),
                "snippet": msg.get('snippet', '')[:self.config.max_email_body_preview],
                "labels": msg.get('labelIds', []),
            })

        return {
            "emails": emails,
            "count": len(emails),
            "total_unread": results.get('resultSizeEstimate', 0),
        }

    async def search_emails(
        self,
        query: str,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """
        Search emails with Gmail query syntax.

        Args:
            query: Gmail search query
            limit: Maximum results

        Returns:
            Search results
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated", "emails": []}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._search_emails_sync(query, limit)
            )
        except Exception as e:
            logger.exception(f"Error searching emails: {e}")
            return {"error": str(e), "emails": []}

    def _search_emails_sync(self, query: str, limit: int) -> Dict[str, Any]:
        """Synchronous email search."""
        results = self._gmail_service.users().messages().list(
            userId='me',
            q=query,
            maxResults=limit,
        ).execute()

        messages = results.get('messages', [])
        emails = []

        for msg_data in messages:
            msg = self._gmail_service.users().messages().get(
                userId='me',
                id=msg_data['id'],
                format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Date'],
            ).execute()

            headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}

            emails.append({
                "id": msg['id'],
                "from": headers.get('From', 'Unknown'),
                "subject": headers.get('Subject', '(no subject)'),
                "date": headers.get('Date', ''),
                "snippet": msg.get('snippet', '')[:self.config.max_email_body_preview],
            })

        return {
            "emails": emails,
            "count": len(emails),
            "query": query,
        }

    async def draft_email(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create an email draft.

        Args:
            to: Recipient email
            subject: Email subject
            body: Email body
            reply_to_id: Optional message ID to reply to

        Returns:
            Draft info
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated"}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._draft_email_sync(to, subject, body, reply_to_id)
            )
        except Exception as e:
            logger.exception(f"Error creating draft: {e}")
            return {"error": str(e)}

    def _draft_email_sync(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to_id: Optional[str],
    ) -> Dict[str, Any]:
        """Synchronous draft creation."""
        message = MIMEMultipart()
        message['to'] = to
        message['subject'] = subject
        message.attach(MIMEText(body, 'plain'))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

        draft_body = {'message': {'raw': raw}}
        if reply_to_id:
            draft_body['message']['threadId'] = reply_to_id

        draft = self._gmail_service.users().drafts().create(
            userId='me',
            body=draft_body,
        ).execute()

        return {
            "status": "created",
            "draft_id": draft['id'],
            "message_id": draft['message']['id'],
            "to": to,
            "subject": subject,
        }

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an email.

        Args:
            to: Recipient email
            subject: Email subject
            body: Plain text body
            html_body: Optional HTML body

        Returns:
            Send result
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated"}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._send_email_sync(to, subject, body, html_body)
            )
        except Exception as e:
            logger.exception(f"Error sending email: {e}")
            return {"error": str(e)}

    def _send_email_sync(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: Optional[str],
    ) -> Dict[str, Any]:
        """Synchronous email send."""
        if html_body:
            message = MIMEMultipart('alternative')
            message['to'] = to
            message['subject'] = subject
            message.attach(MIMEText(body, 'plain'))
            message.attach(MIMEText(html_body, 'html'))
        else:
            message = MIMEText(body, 'plain')
            message['to'] = to
            message['subject'] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

        result = self._gmail_service.users().messages().send(
            userId='me',
            body={'raw': raw},
        ).execute()

        return {
            "status": "sent",
            "message_id": result['id'],
            "thread_id": result.get('threadId'),
            "to": to,
            "subject": subject,
        }

    # =========================================================================
    # Calendar Operations
    # =========================================================================

    async def get_calendar_events(
        self,
        date_str: Optional[str] = None,
        days: int = 1,
    ) -> Dict[str, Any]:
        """
        Get calendar events for a date range.

        Args:
            date_str: Start date (ISO format) or None for today
            days: Number of days to look ahead

        Returns:
            Events data
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated", "events": []}

        # Parse date
        if date_str:
            try:
                start_date = datetime.fromisoformat(date_str)
            except ValueError:
                start_date = datetime.now()
        else:
            start_date = datetime.now()

        # Set time bounds
        time_min = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=days)

        cache_key = f"calendar:{time_min.isoformat()}:{days}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._get_events_sync(time_min, time_max)
            )
            self._set_cached(cache_key, result)
            return result

        except Exception as e:
            logger.exception(f"Error fetching calendar: {e}")
            return {"error": str(e), "events": []}

    def _get_events_sync(
        self,
        time_min: datetime,
        time_max: datetime,
    ) -> Dict[str, Any]:
        """Synchronous calendar fetch."""
        events_result = self._calendar_service.events().list(
            calendarId='primary',
            timeMin=time_min.isoformat() + 'Z',
            timeMax=time_max.isoformat() + 'Z',
            singleEvents=True,
            orderBy='startTime',
        ).execute()

        events = events_result.get('items', [])
        formatted_events = []

        for event in events:
            start = event.get('start', {})
            end = event.get('end', {})

            formatted_events.append({
                "id": event.get('id'),
                "title": event.get('summary', '(No title)'),
                "description": event.get('description', ''),
                "location": event.get('location', ''),
                "start": start.get('dateTime') or start.get('date'),
                "end": end.get('dateTime') or end.get('date'),
                "is_all_day": 'date' in start and 'dateTime' not in start,
                "attendees": [
                    {
                        "email": a.get('email'),
                        "name": a.get('displayName'),
                        "response": a.get('responseStatus'),
                    }
                    for a in event.get('attendees', [])
                ],
                "meeting_link": event.get('hangoutLink'),
                "status": event.get('status'),
            })

        return {
            "events": formatted_events,
            "count": len(formatted_events),
            "date_range": {
                "start": time_min.isoformat(),
                "end": time_max.isoformat(),
            },
        }

    async def create_calendar_event(
        self,
        title: str,
        start: str,
        end: Optional[str] = None,
        description: str = "",
        location: str = "",
        attendees: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create a calendar event.

        Args:
            title: Event title
            start: Start time (ISO format)
            end: End time (ISO format) or None for default duration
            description: Event description
            location: Event location
            attendees: List of attendee emails

        Returns:
            Created event info
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated"}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._create_event_sync(
                    title, start, end, description, location, attendees
                )
            )
        except Exception as e:
            logger.exception(f"Error creating event: {e}")
            return {"error": str(e)}

    def _create_event_sync(
        self,
        title: str,
        start: str,
        end: Optional[str],
        description: str,
        location: str,
        attendees: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Synchronous event creation."""
        # Parse start time
        start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))

        # Calculate end time if not provided
        if end:
            end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
        else:
            end_dt = start_dt + timedelta(minutes=self.config.default_event_duration_minutes)

        event_body = {
            'summary': title,
            'description': description,
            'location': location,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'America/Los_Angeles'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'America/Los_Angeles'},
        }

        if attendees:
            event_body['attendees'] = [{'email': email} for email in attendees]

        event = self._calendar_service.events().insert(
            calendarId='primary',
            body=event_body,
        ).execute()

        return {
            "status": "created",
            "event_id": event.get('id'),
            "title": title,
            "start": start,
            "end": end_dt.isoformat(),
            "link": event.get('htmlLink'),
        }

    # =========================================================================
    # Contacts Operations
    # =========================================================================

    async def get_contacts(
        self,
        query: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Get contacts, optionally filtered by query.

        Args:
            query: Optional search query
            limit: Maximum results

        Returns:
            Contacts data
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated", "contacts": []}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._get_contacts_sync(query, limit)
            )
        except Exception as e:
            logger.exception(f"Error fetching contacts: {e}")
            return {"error": str(e), "contacts": []}

    def _get_contacts_sync(
        self,
        query: Optional[str],
        limit: int,
    ) -> Dict[str, Any]:
        """Synchronous contacts fetch."""
        # Use the connections API
        results = self._people_service.people().connections().list(
            resourceName='people/me',
            pageSize=limit,
            personFields='names,emailAddresses,phoneNumbers,organizations',
        ).execute()

        connections = results.get('connections', [])
        contacts = []

        for person in connections:
            names = person.get('names', [{}])
            emails = person.get('emailAddresses', [])
            phones = person.get('phoneNumbers', [])
            orgs = person.get('organizations', [])

            name = names[0].get('displayName', '') if names else ''

            # Filter by query if provided
            if query:
                query_lower = query.lower()
                if query_lower not in name.lower():
                    email_match = any(
                        query_lower in e.get('value', '').lower()
                        for e in emails
                    )
                    if not email_match:
                        continue

            contacts.append({
                "name": name,
                "emails": [e.get('value') for e in emails if e.get('value')],
                "phones": [p.get('value') for p in phones if p.get('value')],
                "organization": orgs[0].get('name') if orgs else None,
            })

        return {
            "contacts": contacts,
            "count": len(contacts),
        }


# =============================================================================
# Google Workspace Agent
# =============================================================================

class GoogleWorkspaceAgent(BaseNeuralMeshAgent):
    """
    Google Workspace Agent - "Chief of Staff" for Admin & Communication.

    This agent handles all Google Workspace operations including:
    - Gmail (read, send, draft, search)
    - Calendar (view, create events)
    - Contacts (lookup)

    It provides intelligent routing so natural language queries like
    "Check my schedule" or "Draft an email to Mitra" are automatically
    handled by this agent.

    Usage:
        agent = GoogleWorkspaceAgent()
        await coordinator.register_agent(agent)

        # The agent will automatically handle workspace queries
        result = await agent.execute_task({
            "action": "check_calendar_events",
            "date": "today",
        })
    """

    def __init__(self, config: Optional[GoogleWorkspaceConfig] = None) -> None:
        """Initialize the Google Workspace Agent."""
        super().__init__(
            agent_name="google_workspace_agent",
            agent_type="admin",  # Admin/Communication agent type
            capabilities={
                # Email capabilities
                "fetch_unread_emails",
                "search_email",
                "draft_email_reply",
                "send_email",
                # Calendar capabilities
                "check_calendar_events",
                "create_calendar_event",
                "find_free_time",
                # Contacts
                "get_contacts",
                # Composite
                "workspace_summary",
                "daily_briefing",
                # Routing
                "handle_workspace_query",
            },
            version="1.0.0",
        )

        self.config = config or GoogleWorkspaceConfig()
        self._client: Optional[GoogleWorkspaceClient] = None
        self._intent_detector = WorkspaceIntentDetector()

        # Statistics
        self._email_queries = 0
        self._calendar_queries = 0
        self._emails_sent = 0
        self._drafts_created = 0
        self._events_created = 0

    async def on_initialize(self) -> None:
        """Initialize agent resources."""
        logger.info("Initializing GoogleWorkspaceAgent")

        # Create client (lazy authentication)
        self._client = GoogleWorkspaceClient(self.config)

        # Subscribe to workspace-related messages
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_workspace_message,
        )

        logger.info("GoogleWorkspaceAgent initialized (authentication deferred)")

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("GoogleWorkspaceAgent started - ready for workspace operations")

        # Optionally authenticate on start
        # await self._ensure_client()

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info(
            f"GoogleWorkspaceAgent stopping - processed "
            f"{self._email_queries} email queries, "
            f"{self._calendar_queries} calendar queries, "
            f"{self._emails_sent} emails sent, "
            f"{self._events_created} events created"
        )

    async def _ensure_client(self) -> bool:
        """Ensure client is authenticated."""
        if self._client is None:
            self._client = GoogleWorkspaceClient(self.config)
        return await self._client.authenticate()

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """
        Execute a workspace task.

        Supported actions:
        - fetch_unread_emails: Get unread emails
        - search_email: Search emails
        - draft_email_reply: Create email draft
        - send_email: Send an email
        - check_calendar_events: Get calendar events
        - create_calendar_event: Create a calendar event
        - get_contacts: Get contacts
        - workspace_summary: Get daily briefing
        - handle_workspace_query: Natural language query handler
        """
        action = payload.get("action", "")

        logger.debug(f"GoogleWorkspaceAgent executing: {action}")

        # Ensure authenticated
        if action != "handle_workspace_query":
            if not await self._ensure_client():
                return {"error": "Google Workspace authentication failed"}

        # Route to appropriate handler
        if action == "fetch_unread_emails":
            return await self._fetch_unread_emails(payload)
        elif action == "search_email":
            return await self._search_email(payload)
        elif action == "draft_email_reply":
            return await self._draft_email(payload)
        elif action == "send_email":
            return await self._send_email(payload)
        elif action == "check_calendar_events":
            return await self._check_calendar(payload)
        elif action == "create_calendar_event":
            return await self._create_event(payload)
        elif action == "get_contacts":
            return await self._get_contacts(payload)
        elif action == "workspace_summary":
            return await self._get_workspace_summary(payload)
        elif action == "daily_briefing":
            return await self._get_workspace_summary(payload)
        elif action == "handle_workspace_query":
            return await self._handle_natural_query(payload)
        else:
            raise ValueError(f"Unknown workspace action: {action}")

    async def _fetch_unread_emails(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch unread emails."""
        limit = payload.get("limit", self.config.default_email_limit)
        label = payload.get("label", "INBOX")

        self._email_queries += 1

        result = await self._client.fetch_unread_emails(limit=limit, label=label)

        # Add to knowledge graph
        if self.knowledge_graph and result.get("emails"):
            await self.add_knowledge(
                knowledge_type=KnowledgeType.OBSERVATION,
                data={
                    "type": "email_check",
                    "unread_count": result.get("count", 0),
                    "checked_at": datetime.now().isoformat(),
                },
                confidence=1.0,
            )

        return result

    async def _search_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Search emails."""
        query = payload.get("query", "")
        limit = payload.get("limit", self.config.default_email_limit)

        self._email_queries += 1

        return await self._client.search_emails(query=query, limit=limit)

    async def _draft_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create email draft."""
        to = payload.get("to", "")
        subject = payload.get("subject", "")
        body = payload.get("body", "")
        reply_to = payload.get("reply_to_id")

        if not to:
            return {"error": "Recipient 'to' is required"}
        if not subject:
            return {"error": "Subject is required"}
        if not body:
            return {"error": "Email body is required"}

        result = await self._client.draft_email(
            to=to,
            subject=subject,
            body=body,
            reply_to_id=reply_to,
        )

        if result.get("status") == "created":
            self._drafts_created += 1

        return result

    async def _send_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send an email."""
        to = payload.get("to", "")
        subject = payload.get("subject", "")
        body = payload.get("body", "")
        html_body = payload.get("html_body")

        if not to:
            return {"error": "Recipient 'to' is required"}
        if not subject:
            return {"error": "Subject is required"}
        if not body:
            return {"error": "Email body is required"}

        result = await self._client.send_email(
            to=to,
            subject=subject,
            body=body,
            html_body=html_body,
        )

        if result.get("status") == "sent":
            self._emails_sent += 1

            # Record in knowledge graph
            if self.knowledge_graph:
                await self.add_knowledge(
                    knowledge_type=KnowledgeType.OBSERVATION,
                    data={
                        "type": "email_sent",
                        "to": to,
                        "subject": subject,
                        "sent_at": datetime.now().isoformat(),
                    },
                    confidence=1.0,
                )

        return result

    async def _check_calendar(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Check calendar events."""
        date_str = payload.get("date")
        days = payload.get("days", 1)

        # Handle relative dates
        if date_str:
            date_lower = date_str.lower()
            if date_lower == "today":
                date_str = date.today().isoformat()
            elif date_lower == "tomorrow":
                date_str = (date.today() + timedelta(days=1)).isoformat()

        self._calendar_queries += 1

        result = await self._client.get_calendar_events(date_str=date_str, days=days)

        # Add observation to knowledge graph
        if self.knowledge_graph:
            await self.add_knowledge(
                knowledge_type=KnowledgeType.OBSERVATION,
                data={
                    "type": "calendar_check",
                    "event_count": result.get("count", 0),
                    "date_range": result.get("date_range"),
                    "checked_at": datetime.now().isoformat(),
                },
                confidence=1.0,
            )

        return result

    async def _create_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a calendar event."""
        title = payload.get("title", "")
        start = payload.get("start", "")
        end = payload.get("end")
        description = payload.get("description", "")
        location = payload.get("location", "")
        attendees = payload.get("attendees", [])

        if not title:
            return {"error": "Event title is required"}
        if not start:
            return {"error": "Start time is required"}

        result = await self._client.create_calendar_event(
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
        )

        if result.get("status") == "created":
            self._events_created += 1

        return result

    async def _get_contacts(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get contacts."""
        query = payload.get("query")
        limit = payload.get("limit", 20)

        return await self._client.get_contacts(query=query, limit=limit)

    async def _get_workspace_summary(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get a comprehensive workspace summary (daily briefing).

        Returns summary of:
        - Unread emails
        - Today's calendar events
        - Upcoming deadlines
        """
        # Fetch in parallel
        email_task = self._client.fetch_unread_emails(limit=5)
        calendar_task = self._client.get_calendar_events(days=1)

        email_result, calendar_result = await asyncio.gather(
            email_task, calendar_task, return_exceptions=True
        )

        # Build summary
        summary = {
            "generated_at": datetime.now().isoformat(),
            "date": date.today().isoformat(),
        }

        # Email summary
        if isinstance(email_result, dict) and not email_result.get("error"):
            summary["email"] = {
                "unread_count": email_result.get("total_unread", 0),
                "recent_emails": [
                    {
                        "from": e.get("from"),
                        "subject": e.get("subject"),
                    }
                    for e in email_result.get("emails", [])[:3]
                ],
            }
        else:
            summary["email"] = {"error": str(email_result)}

        # Calendar summary
        if isinstance(calendar_result, dict) and not calendar_result.get("error"):
            events = calendar_result.get("events", [])
            summary["calendar"] = {
                "event_count": len(events),
                "events": [
                    {
                        "title": e.get("title"),
                        "start": e.get("start"),
                        "location": e.get("location"),
                    }
                    for e in events
                ],
            }
        else:
            summary["calendar"] = {"error": str(calendar_result)}

        # Generate human-readable brief
        unread = summary.get("email", {}).get("unread_count", 0)
        event_count = summary.get("calendar", {}).get("event_count", 0)

        summary["brief"] = (
            f"Good morning! You have {unread} unread emails and "
            f"{event_count} events scheduled for today."
        )

        if event_count > 0:
            first_event = summary["calendar"]["events"][0]
            summary["brief"] += (
                f" Your first meeting is '{first_event['title']}' "
                f"starting at {first_event['start']}."
            )

        return summary

    async def _handle_natural_query(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle a natural language workspace query.

        This is the main entry point for intelligent routing.
        """
        query = payload.get("query", "")

        if not query:
            return {"error": "No query provided"}

        # Detect intent
        intent, confidence, metadata = self._intent_detector.detect(query)

        logger.info(
            f"Detected workspace intent: {intent.value} (confidence={confidence:.2f})"
        )

        # Route based on intent
        if intent == WorkspaceIntent.CHECK_EMAIL:
            return await self._fetch_unread_emails({
                "limit": payload.get("limit", 5),
            })

        elif intent == WorkspaceIntent.CHECK_CALENDAR:
            dates = metadata.get("extracted_dates", {})
            return await self._check_calendar({
                "date": dates.get("today") or dates.get("tomorrow"),
                "days": 1,
            })

        elif intent == WorkspaceIntent.DRAFT_EMAIL:
            names = metadata.get("extracted_names", [])
            # If we have a name, we'd need to look up the email
            return {
                "status": "draft_ready",
                "message": "Ready to draft email",
                "detected_recipient": names[0] if names else None,
                "instructions": "Please provide: to, subject, and body",
            }

        elif intent == WorkspaceIntent.SEND_EMAIL:
            return {
                "status": "send_ready",
                "message": "Ready to send email",
                "instructions": "Please provide: to, subject, and body",
            }

        elif intent == WorkspaceIntent.DAILY_BRIEFING:
            return await self._get_workspace_summary({})

        elif intent == WorkspaceIntent.GET_CONTACTS:
            names = metadata.get("extracted_names", [])
            return await self._get_contacts({
                "query": names[0] if names else None,
            })

        elif intent == WorkspaceIntent.CREATE_EVENT:
            return {
                "status": "event_ready",
                "message": "Ready to create calendar event",
                "instructions": "Please provide: title, start, and optionally end, description, location, attendees",
            }

        else:
            return {
                "status": "unknown_intent",
                "detected_intent": intent.value,
                "confidence": confidence,
                "message": "I'm not sure what workspace action you'd like. Try asking about emails, calendar, or contacts.",
            }

    async def _handle_workspace_message(self, message: AgentMessage) -> None:
        """Handle incoming workspace messages from other agents."""
        if message.payload.get("type") != "workspace_request":
            return

        query = message.payload.get("query", "")
        action = message.payload.get("action")

        try:
            if action:
                result = await self.execute_task({
                    "action": action,
                    **message.payload,
                })
            else:
                result = await self._handle_natural_query({"query": query})

            # Send response
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "workspace_response",
                        "result": result,
                    },
                    from_agent=self.agent_name,
                )
        except Exception as e:
            logger.exception(f"Error handling workspace message: {e}")
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "workspace_response",
                        "error": str(e),
                    },
                    from_agent=self.agent_name,
                )

    # =========================================================================
    # Convenience methods for direct access
    # =========================================================================

    async def check_schedule(self, date_str: str = "today") -> Dict[str, Any]:
        """Quick method to check today's schedule."""
        return await self.execute_task({
            "action": "check_calendar_events",
            "date": date_str,
            "days": 1,
        })

    async def check_emails(self, limit: int = 5) -> Dict[str, Any]:
        """Quick method to check unread emails."""
        return await self.execute_task({
            "action": "fetch_unread_emails",
            "limit": limit,
        })

    async def draft_reply(
        self,
        to: str,
        subject: str,
        body: str,
    ) -> Dict[str, Any]:
        """Quick method to draft an email."""
        return await self.execute_task({
            "action": "draft_email_reply",
            "to": to,
            "subject": subject,
            "body": body,
        })

    async def briefing(self) -> Dict[str, Any]:
        """Get daily briefing."""
        return await self.execute_task({
            "action": "workspace_summary",
        })

    def is_workspace_query(self, query: str) -> Tuple[bool, float]:
        """
        Check if a query should be routed to this agent.

        Used by the orchestrator for intelligent routing.
        """
        return self._intent_detector.is_workspace_query(query)

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics."""
        return {
            "email_queries": self._email_queries,
            "calendar_queries": self._calendar_queries,
            "emails_sent": self._emails_sent,
            "drafts_created": self._drafts_created,
            "events_created": self._events_created,
            "capabilities": list(self.capabilities),
        }
