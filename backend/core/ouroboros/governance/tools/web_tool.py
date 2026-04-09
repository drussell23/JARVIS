"""
Web Access Tool
================

Enables the governance pipeline to fetch web content and search the internet.

Capabilities:
  1. web_fetch  -- GET a URL and return its content (HTML stripped to text)
  2. web_search -- Search using DuckDuckGo HTML (no API key required)

Safety:
  - URL allowlist for fetch (configurable, default allows common dev resources)
  - Rate limiting (max 10 requests per minute)
  - Response size limit (max 500KB)
  - Timeout enforcement (default 15s)
  - No POST/PUT/DELETE -- read-only access

Environment:
  JARVIS_WEB_TOOL_ENABLED       -- "true" to enable (default: "true")
  JARVIS_WEB_TOOL_TIMEOUT       -- default timeout in seconds (default: 15)
  JARVIS_WEB_TOOL_MAX_SIZE      -- max response bytes (default: 512000)
  JARVIS_WEB_TOOL_UNRESTRICTED  -- "true" to skip domain allowlist (default: "false")
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

logger = logging.getLogger("Ouroboros.WebTool")

# ---------------------------------------------------------------------------
# Allowed URL patterns (domains) for fetch
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWED_DOMAINS: frozenset[str] = frozenset({
    "github.com",
    "raw.githubusercontent.com",
    "docs.python.org",
    "pypi.org",
    "stackoverflow.com",
    "developer.mozilla.org",
    "docs.rs",
    "crates.io",
    "npmjs.com",
    "registry.npmjs.org",
    "pkg.go.dev",
    "api.github.com",
    "arxiv.org",
    "en.wikipedia.org",
})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class WebToolConfig:
    """Runtime configuration for the WebTool."""

    enabled: bool = False
    timeout_s: float = 15.0
    max_response_bytes: int = 512_000
    rate_limit_per_minute: int = 10
    allowed_domains: frozenset[str] = field(default_factory=lambda: _DEFAULT_ALLOWED_DOMAINS)
    unrestricted_fetch: bool = False

    @classmethod
    def from_env(cls) -> WebToolConfig:
        """Build config from environment variables."""
        enabled = os.getenv("JARVIS_WEB_TOOL_ENABLED", "true").lower() in ("true", "1", "yes")
        return cls(
            enabled=enabled,
            timeout_s=float(os.getenv("JARVIS_WEB_TOOL_TIMEOUT", "15")),
            max_response_bytes=int(os.getenv("JARVIS_WEB_TOOL_MAX_SIZE", "512000")),
            unrestricted_fetch=os.getenv(
                "JARVIS_WEB_TOOL_UNRESTRICTED", "false"
            ).lower() in ("true", "1"),
        )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class WebResult:
    """Result of a single URL fetch."""

    url: str
    status_code: int
    content: str
    content_type: str = ""
    error: str = ""
    truncated: bool = False


@dataclass
class SearchResult:
    """Result of a web search query."""

    query: str
    results: List[Dict[str, str]]  # [{title, url, snippet}]
    error: str = ""


# ---------------------------------------------------------------------------
# WebTool
# ---------------------------------------------------------------------------

class WebTool:
    """Read-only web access tool for the governance pipeline.

    All requests are GET-only. Rate limiting and URL validation are enforced
    locally. The tool is disabled by default; set ``JARVIS_WEB_TOOL_ENABLED=true``
    to activate.

    Usage::

        tool = WebTool()
        result = await tool.fetch("https://docs.python.org/3/library/asyncio.html")
        search = await tool.search("python asyncio timeout best practices")
        await tool.close()
    """

    def __init__(self, config: Optional[WebToolConfig] = None) -> None:
        self._config = config or WebToolConfig.from_env()
        self._request_times: List[float] = []
        self._session: Any = None  # lazy aiohttp.ClientSession

    # -- Properties ----------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        """Whether the tool is enabled for use."""
        return self._config.enabled

    @property
    def config(self) -> WebToolConfig:
        """Current configuration (read-only access)."""
        return self._config

    # -- Session management --------------------------------------------------

    async def _get_session(self) -> Any:
        """Lazy-create an aiohttp.ClientSession."""
        if self._session is None:
            try:
                import aiohttp  # type: ignore[import-untyped]
            except ImportError:
                raise RuntimeError(
                    "aiohttp is not installed -- WebTool requires: pip install aiohttp"
                )
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._config.timeout_s),
                headers={
                    "User-Agent": "JARVIS-Ouroboros/1.0 (governance pipeline)",
                },
            )
        return self._session

    async def close(self) -> None:
        """Close the aiohttp session if open."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- Guards --------------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """Return True if we are within the per-minute rate limit."""
        now = time.monotonic()
        # Evict entries older than 60 seconds
        self._request_times = [t for t in self._request_times if now - t < 60.0]
        if len(self._request_times) >= self._config.rate_limit_per_minute:
            return False
        self._request_times.append(now)
        return True

    def _validate_url(self, url: str) -> Optional[str]:
        """Validate *url* against scheme and domain allowlist.

        Returns an error message string on failure, or ``None`` on success.
        """
        try:
            parsed = urlparse(url)
        except Exception as exc:
            return f"Invalid URL: {exc}"

        if parsed.scheme not in ("http", "https"):
            return f"Only http/https URLs allowed, got: {parsed.scheme!r}"

        if not self._config.unrestricted_fetch:
            domain = parsed.hostname or ""
            if domain not in self._config.allowed_domains:
                return f"Domain {domain!r} not in allowed domains"

        return None

    # -- Public API ----------------------------------------------------------

    async def fetch(self, url: str) -> WebResult:
        """Fetch *url* via GET and return its text content.

        HTML responses are automatically stripped to plain text.
        """
        if not self._config.enabled:
            return WebResult(url=url, status_code=0, content="", error="WebTool is disabled")

        error = self._validate_url(url)
        if error:
            logger.warning("URL validation failed for %s: %s", url, error)
            return WebResult(url=url, status_code=0, content="", error=error)

        if not self._check_rate_limit():
            return WebResult(
                url=url,
                status_code=429,
                content="",
                error=f"Rate limit exceeded (max {self._config.rate_limit_per_minute}/min)",
            )

        try:
            session = await self._get_session()
            async with session.get(url) as response:
                content_type = response.headers.get("Content-Type", "")
                raw = await response.read()

                # Enforce size limit
                truncated = len(raw) > self._config.max_response_bytes
                if truncated:
                    raw = raw[: self._config.max_response_bytes]

                text = raw.decode("utf-8", errors="replace")

                # Strip HTML tags for readability
                if "html" in content_type.lower():
                    text = self._strip_html(text)

                return WebResult(
                    url=url,
                    status_code=response.status,
                    content=text,
                    content_type=content_type,
                    truncated=truncated,
                )

        except asyncio.TimeoutError:
            return WebResult(
                url=url,
                status_code=0,
                content="",
                error=f"Request timed out after {self._config.timeout_s}s",
            )
        except Exception as exc:
            logger.warning("Fetch failed for %s: %s", url, exc, exc_info=True)
            return WebResult(url=url, status_code=0, content="", error=str(exc))

    async def search(self, query: str, max_results: int = 5) -> SearchResult:
        """Search the web via DuckDuckGo HTML (no API key required).

        Returns up to *max_results* items with title, URL, and snippet.
        """
        if not self._config.enabled:
            return SearchResult(query=query, results=[], error="WebTool is disabled")

        if not self._check_rate_limit():
            return SearchResult(
                query=query,
                results=[],
                error=f"Rate limit exceeded (max {self._config.rate_limit_per_minute}/min)",
            )

        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        try:
            session = await self._get_session()
            async with session.get(search_url) as response:
                if response.status != 200:
                    return SearchResult(
                        query=query,
                        results=[],
                        error=f"Search returned status {response.status}",
                    )
                text = await response.text()
                results = self._parse_ddg_results(text, max_results)
                return SearchResult(query=query, results=results)

        except asyncio.TimeoutError:
            return SearchResult(query=query, results=[], error="Search timed out")
        except Exception as exc:
            logger.warning("Search failed for %r: %s", query, exc, exc_info=True)
            return SearchResult(query=query, results=[], error=str(exc))

    # -- HTML helpers --------------------------------------------------------

    @staticmethod
    def _strip_html(text: str) -> str:
        """Strip HTML tags and decode entities, preserving text content."""
        # Remove script and style blocks entirely
        text = re.sub(
            r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(
            r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE
        )
        # Remove all remaining tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Decode HTML entities
        text = html.unescape(text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _parse_ddg_results(html_text: str, max_results: int) -> List[Dict[str, str]]:
        """Parse DuckDuckGo HTML search results page.

        Extracts result link titles/URLs and snippet text.
        """
        results: List[Dict[str, str]] = []

        # DuckDuckGo HTML results sit inside <a class="result__a"> tags
        link_pattern = r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
        snippet_pattern = r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>'

        links = re.findall(link_pattern, html_text, re.DOTALL)
        snippets = re.findall(snippet_pattern, html_text, re.DOTALL)

        for i, (raw_url, raw_title) in enumerate(links[:max_results]):
            # DuckDuckGo wraps actual URLs behind a redirect — extract the real URL
            url = raw_url
            if "uddg=" in url:
                try:
                    from urllib.parse import parse_qs

                    params = parse_qs(urlparse(url).query)
                    url = params.get("uddg", [url])[0]
                except Exception:
                    pass  # keep the redirect URL as fallback

            result: Dict[str, str] = {
                "title": re.sub(r"<[^>]+>", "", raw_title).strip(),
                "url": url,
                "snippet": (
                    re.sub(r"<[^>]+>", "", snippets[i]).strip()
                    if i < len(snippets)
                    else ""
                ),
            }
            results.append(result)

        return results

    # -- MCP tool definitions ------------------------------------------------

    def to_tool_definitions(self) -> List[Dict[str, Any]]:
        """Return MCP-compatible tool definitions for registration."""
        return [
            {
                "name": "web_fetch",
                "description": (
                    "Fetch a URL and return its text content. "
                    "HTML is stripped to plain text."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to fetch",
                        },
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "web_search",
                "description": (
                    "Search the web and return results with titles, "
                    "URLs, and snippets."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max results to return (default 5)",
                        },
                    },
                    "required": ["query"],
                },
            },
        ]
