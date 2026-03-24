"""
Communicator Context -- messages, email, calendar, web research.

The Communicator handles all outward-facing communication: sending
messages (via Executor for app-based messaging), composing emails,
managing calendar events, and performing web research.

The Architect dispatches goals to the Communicator when the task
involves reaching out to someone, checking schedules, or gathering
information from the web.

Tool access:
    workspace.*      -- Gmail, Calendar, Contacts, Drive
    browser.*        -- web search, page extraction
    memory.*         -- recall contacts, preferences, past interactions
    apps.*           -- open messaging apps (WhatsApp, Slack, etc.)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core_contexts.tools import workspace, browser, memory, apps

logger = logging.getLogger(__name__)


@dataclass
class CommunicatorResult:
    """Result of a Communicator operation.

    Attributes:
        success: Whether the communication was sent/retrieved.
        action: What was done ("email_sent", "event_created", "search_completed").
        details: Human-readable summary of the outcome.
        data: Structured data returned (emails, events, search results).
    """
    success: bool
    action: str = ""
    details: str = ""
    data: Any = None


class Communicator:
    """Communication and information exchange context.

    The Communicator provides tools for sending messages, managing
    email, scheduling events, and researching information.

    For app-based messaging (WhatsApp, Slack, iMessage), the
    Communicator delegates to the Executor context which uses the
    vision loop to interact with the app's GUI.

    Usage::

        communicator = Communicator()
        emails = await workspace.fetch_unread_emails(limit=5)
        await workspace.send_email("meryem@doubleword.ai", "Re: Partnership", "...")
        events = await workspace.get_calendar_events(days=7)
        results = await browser.web_search("Qwen3 VL benchmark results")
    """

    TOOLS = {
        "workspace.fetch_unread_emails": workspace.fetch_unread_emails,
        "workspace.send_email": workspace.send_email,
        "workspace.search_emails": workspace.search_emails,
        "workspace.get_calendar_events": workspace.get_calendar_events,
        "workspace.create_calendar_event": workspace.create_calendar_event,
        "browser.web_search": browser.web_search,
        "browser.extract_page_text": browser.extract_page_text,
        "browser.navigate": browser.navigate,
        "memory.store_memory": memory.store_memory,
        "memory.recall_memory": memory.recall_memory,
        "apps.open_app": apps.open_app,
        "apps.activate_app": apps.activate_app,
    }

    async def check_email(self, limit: int = 10) -> CommunicatorResult:
        """Check inbox for unread emails.

        Args:
            limit: Maximum emails to fetch.

        Returns:
            CommunicatorResult with email list in data field.
        """
        emails = await workspace.fetch_unread_emails(limit)
        return CommunicatorResult(
            success=True,
            action="email_checked",
            details=f"{len(emails)} unread emails",
            data=emails,
        )

    async def send_email_simple(
        self, to: str, subject: str, body: str,
    ) -> CommunicatorResult:
        """Send a simple email.

        Args:
            to: Recipient address.
            subject: Email subject.
            body: Email body text.

        Returns:
            CommunicatorResult with send status.
        """
        success = await workspace.send_email(to, subject, body)
        return CommunicatorResult(
            success=success,
            action="email_sent" if success else "email_failed",
            details=f"To: {to}, Subject: {subject}" if success else "Send failed",
        )

    async def research(self, query: str) -> CommunicatorResult:
        """Search the web for information.

        Args:
            query: Search query.

        Returns:
            CommunicatorResult with search results in data field.
        """
        results = await browser.web_search(query)
        return CommunicatorResult(
            success=len(results) > 0,
            action="search_completed",
            details=f"{len(results)} results for: {query[:60]}",
            data=results,
        )

    @classmethod
    def tool_manifest(cls) -> List[Dict[str, str]]:
        """Return the Communicator's tool manifest."""
        manifest = []
        for name, fn in cls.TOOLS.items():
            manifest.append({
                "name": name,
                "description": (fn.__doc__ or "").strip().split("\n")[0],
                "module": name.split(".")[0],
            })
        return manifest

    async def execute_tool(self, tool_name: str, **kwargs) -> Any:
        """Execute a Communicator tool by name."""
        fn = self.TOOLS.get(tool_name)
        if fn is None:
            raise KeyError(f"Unknown Communicator tool: {tool_name}")
        if asyncio.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return fn(**kwargs)
