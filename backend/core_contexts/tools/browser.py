"""
Atomic browser automation tools -- navigate, extract, fill, search.

These tools provide the Executor context with web browser control via
Playwright (CDP connection to user's Chrome) or headless Chromium.
Delegates to the existing BrowsingAgent and VisualBrowserAgent.

No pyautogui.  Playwright handles all DOM interaction.

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = int(os.environ.get("TOOL_BROWSER_NAV_TIMEOUT_MS", "15000"))
_ACTION_TIMEOUT_MS = int(os.environ.get("TOOL_BROWSER_ACTION_TIMEOUT_MS", "5000"))
_MAX_CONTENT_CHARS = int(os.environ.get("TOOL_BROWSER_MAX_CONTENT", "10000"))

# Lazy-initialized browsing agent
_agent = None
_agent_lock = asyncio.Lock()


@dataclass(frozen=True)
class PageContent:
    """Extracted content from a web page.

    Attributes:
        url: The page URL after navigation.
        title: The page title.
        text: Main text content (stripped of nav/header/footer).
        links: List of {text, href} dicts for important links.
        meta: Page metadata (description, author, etc.).
    """
    url: str
    title: str
    text: str
    links: List[Dict[str, str]] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchResult:
    """A single web search result.

    Attributes:
        title: Result title.
        url: Result URL.
        snippet: Brief description/snippet.
    """
    title: str
    url: str
    snippet: str


async def navigate(url: str) -> Optional[PageContent]:
    """Navigate to a URL and return the page content.

    Opens the URL in Chrome (via CDP) or headless Chromium.  Waits for
    DOM content to load, then extracts the main text content (stripping
    navigation, headers, footers, scripts, and styles).

    Args:
        url: Full URL to navigate to (must include https://).

    Returns:
        PageContent with URL, title, text, links, and metadata.
        None if navigation fails (network error, timeout).

    Use when:
        The Executor needs to open a web page to read its content,
        fill a form, or interact with a web application.
    """
    agent = await _get_agent()
    if agent is None:
        return None

    try:
        result = await agent.navigate(url)
        if not result.get("success"):
            logger.warning("[tool:browser] Navigate failed: %s", result.get("error", "unknown"))
            return None

        page = agent._pages.get("default") or next(iter(agent._pages.values()), None)
        if page is None:
            return PageContent(url=result.get("url", url), title=result.get("title", ""), text="")

        content = await agent.get_page_content(page, clean=True)
        structured = await agent.get_structured_data(page)

        return PageContent(
            url=result.get("url", url),
            title=result.get("title", ""),
            text=content.get("text", "")[:_MAX_CONTENT_CHARS],
            meta=structured.get("meta", {}),
        )
    except Exception as exc:
        logger.error("[tool:browser] navigate error: %s", exc)
        return None


async def web_search(query: str, max_results: int = 5) -> List[SearchResult]:
    """Search the web and return structured results.

    Uses DuckDuckGo (free, default), Brave, or Google depending on
    available API keys.  No browser automation needed -- direct API.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return (default 5).

    Returns:
        List of SearchResult with title, URL, and snippet.
        Empty list if search fails.

    Use when:
        The Communicator or Architect needs to find information on the
        web before composing a response or planning a task.
    """
    agent = await _get_agent()
    if agent is None:
        return []

    try:
        result = await agent.search(query, max_results=max_results)
        if not result.get("success"):
            return []

        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("snippet", r.get("description", "")),
            )
            for r in result.get("results", [])
        ]
    except Exception as exc:
        logger.error("[tool:browser] search error: %s", exc)
        return []


async def extract_page_text(url: str) -> str:
    """Navigate to a URL and extract just the main text content.

    Convenience wrapper around navigate() that returns only the text
    content, suitable for feeding into a reasoning model.

    Args:
        url: Full URL to extract text from.

    Returns:
        Main text content of the page (up to 10,000 chars).
        Empty string if extraction fails.

    Use when:
        The Architect or Developer needs to read documentation, a Stack
        Overflow answer, or any web page as text input for reasoning.
    """
    page = await navigate(url)
    return page.text if page else ""


async def fill_form_field(selector: str, value: str) -> bool:
    """Fill a form field on the currently active page.

    Uses Playwright's safe selector API (no JavaScript injection).

    Args:
        selector: CSS selector or Playwright selector for the input field.
        value: Text value to enter into the field.

    Returns:
        True if the field was filled successfully.

    Use when:
        The Executor needs to enter text into a web form (login, search
        bar, text input, etc.) on a page that is already open.
    """
    agent = await _get_agent()
    if agent is None:
        return False

    try:
        page = next(iter(agent._pages.values()), None)
        if page is None:
            return False
        return await agent.fill_field(page, selector, value)
    except Exception as exc:
        logger.error("[tool:browser] fill_form_field error: %s", exc)
        return False


async def click_element(selector: str) -> bool:
    """Click a DOM element on the currently active page.

    Uses Playwright's safe selector API.

    Args:
        selector: CSS selector, text selector, or ARIA role selector
            (e.g., 'button:has-text("Submit")', '#login-btn').

    Returns:
        True if the element was clicked.

    Use when:
        The Executor needs to click a button, link, or interactive
        element on a web page.
    """
    agent = await _get_agent()
    if agent is None:
        return False

    try:
        page = next(iter(agent._pages.values()), None)
        if page is None:
            return False
        return await agent.click_element(page, selector)
    except Exception as exc:
        logger.error("[tool:browser] click_element error: %s", exc)
        return False


async def get_page_url() -> str:
    """Get the URL of the currently active browser page.

    Returns:
        Current page URL, or empty string if no page is open.

    Use when:
        The Architect needs to know the current page context for
        decision making (e.g., "are we on the right page?").
    """
    agent = await _get_agent()
    if agent is None:
        return ""

    try:
        page = next(iter(agent._pages.values()), None)
        return page.url if page else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Internal: delegate to BrowsingAgent
# ---------------------------------------------------------------------------

async def _get_agent():
    """Get or create the BrowsingAgent singleton."""
    global _agent

    if _agent is not None:
        return _agent

    async with _agent_lock:
        if _agent is not None:
            return _agent

        for import_path in ("backend.browsing.browsing_agent",
                            "browsing.browsing_agent"):
            try:
                import importlib
                mod = importlib.import_module(import_path)
                cls = mod.BrowsingAgent
                _agent = cls()
                await _agent.initialize()
                logger.info("[tool:browser] BrowsingAgent initialized")
                return _agent
            except (ImportError, Exception) as exc:
                logger.debug("[tool:browser] %s: %s", import_path, exc)
                continue

        logger.warning("[tool:browser] BrowsingAgent not available")
        return None
