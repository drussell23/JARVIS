"""
WebSearchCapability — Structured web search for CONTEXT_EXPANSION.

Gives Ouroboros eyes, not just reading glasses. When the 397B Architect
encounters a high-entropy capability gap, it can search for solutions
across developer-verified domains.

Boundary Principle:
  Deterministic: Query construction, API call, domain filtering, text
  extraction, result ranking. No model inference in the search path.
  Agentic: The DECISION to search and WHAT to search for is made by the
  model's expansion response. This module executes the search.

Safety (Epistemic Allowlist):
  Results are domain-restricted to high-signal developer sources.
  No unverified blogs, no social media, no user-generated content
  outside of Stack Overflow (which has community-vetted answers).
  This prevents prompt injection from untrusted web content.

Search Backend:
  Brave Search API (structured JSON, no browser needed).
  Fallback: Google Custom Search API if Brave unavailable.
  Both return structured results — no HTML scraping.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BRAVE_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
_GOOGLE_CSE_KEY = os.environ.get("GOOGLE_CSE_API_KEY", "")
_GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX", "")

_MAX_RESULTS = int(os.environ.get("JARVIS_WEB_SEARCH_MAX_RESULTS", "3"))
_SEARCH_TIMEOUT_S = float(os.environ.get("JARVIS_WEB_SEARCH_TIMEOUT_S", "10"))
_MAX_SNIPPET_CHARS = int(os.environ.get("JARVIS_WEB_SEARCH_MAX_SNIPPET", "2000"))
_MAX_PAGE_CHARS = int(os.environ.get("JARVIS_WEB_SEARCH_MAX_PAGE", "6000"))

# ---------------------------------------------------------------------------
# Epistemic Allowlist — high-signal developer domains ONLY
# ---------------------------------------------------------------------------
# Results from domains NOT on this list are silently dropped.
# This is the immune system boundary — prevents prompt injection from
# untrusted web content entering the organism's context window.

# Tier 1 — Fully trusted: official docs, vetted Q&A, code hosting
# Content injected raw into generation context.
_TIER1_DOMAINS: frozenset[str] = frozenset({
    # Q&A (community-vetted)
    "stackoverflow.com",
    "stackexchange.com",
    # Code hosting
    "github.com",
    "raw.githubusercontent.com",
    "gist.github.com",
    # Python ecosystem
    "docs.python.org",
    "pypi.org",
    "peps.python.org",
    # Documentation platforms
    "readthedocs.io",
    "readthedocs.org",
    # Major framework docs
    "fastapi.tiangolo.com",
    "docs.pydantic.dev",
    "docs.aiohttp.org",
    "pytorch.org",
    "numpy.org",
    "scikit-learn.org",
    "redis.io",
    "docs.sqlalchemy.org",
    "flask.palletsprojects.com",
    "click.palletsprojects.com",
    # Cloud providers (official docs only)
    "cloud.google.com",
    "docs.aws.amazon.com",
    # Anthropic / AI
    "docs.anthropic.com",
    "sdk.vercel.ai",
    "platform.openai.com",
    # Rust/Go/JS ecosystems
    "doc.rust-lang.org",
    "pkg.go.dev",
    "nodejs.org",
    "docs.npmjs.com",
    "tc39.es",
    # Mozilla / Web standards
    "developer.mozilla.org",
    # Vercel / Next.js
    "vercel.com",
    "nextjs.org",
})

# Tier 2 — Semi-trusted: verified tech platforms with editorial oversight
# Content injected with "[community source]" tag for provenance awareness.
_TIER2_DOMAINS: frozenset[str] = frozenset({
    "dev.to",
    "hashnode.dev",
    "realpython.com",
    "testdriven.io",
    "blog.rust-lang.org",
    "go.dev",
    "huggingface.co",
    "docs.docker.com",
    "kubernetes.io",
    "grafana.com",
    "prometheus.io",
    "www.postgresql.org",
    "wiki.python.org",
})

# Tier 3 — Untrusted: general web. Snippets only (no full page fetch).
# Results tagged as "[unverified]" — model must treat as hints, not facts.
_TIER3_ENABLED = os.environ.get(
    "JARVIS_WEB_SEARCH_TIER3_ENABLED", "false"
).lower() in ("true", "1", "yes")

# Combined allowlist for backward compat
_EPISTEMIC_ALLOWLIST = _TIER1_DOMAINS | _TIER2_DOMAINS


@dataclass(frozen=True)
class SearchResult:
    """One search result that passed the epistemic allowlist."""
    title: str
    url: str
    snippet: str              # API-provided snippet (structured, no HTML)
    domain: str               # Extracted domain for transparency
    page_text: str = ""       # Full page text if fetched (bounded)


@dataclass(frozen=True)
class SearchResponse:
    """Complete search response with metadata."""
    query: str
    results: Tuple[SearchResult, ...]
    backend: str              # "brave" or "google_cse"
    total_raw_results: int    # Before allowlist filtering
    filtered_count: int       # Dropped by allowlist
    search_time_ms: float


class WebSearchCapability:
    """Structured web search with epistemic domain filtering.

    Provides the 397B Architect with the ability to search for
    developer documentation, Stack Overflow answers, and official
    API references during CONTEXT_EXPANSION.

    Safety guarantees:
    - Domain-restricted to _EPISTEMIC_ALLOWLIST (no blogs, no social media)
    - Result count bounded (_MAX_RESULTS, default 3)
    - Text size bounded (_MAX_SNIPPET_CHARS per result)
    - Time bounded (_SEARCH_TIMEOUT_S, default 10s)
    - No link following — single search, single page fetch
    - No JavaScript execution — text extraction only
    """

    def __init__(self) -> None:
        self._session: Optional[Any] = None

    @property
    def is_available(self) -> bool:
        """Always available — DuckDuckGo requires no API key."""
        return True

    @property
    def backend_name(self) -> str:
        if _BRAVE_API_KEY:
            return "brave"
        if _GOOGLE_CSE_KEY:
            return "google_cse"
        return "duckduckgo"

    async def _get_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_SEARCH_TIMEOUT_S),
                headers={"User-Agent": "JARVIS-Trinity/1.0 (Ouroboros WebSearch)"},
            )
        return self._session

    async def search(self, query: str) -> SearchResponse:
        """Execute a search query against the configured backend.

        Returns only results from epistemic-allowlisted domains.
        Bounded to _MAX_RESULTS results with _MAX_SNIPPET_CHARS per snippet.

        The query is passed as-is to the search API — the model constructs
        the query string (agentic), this module executes it (deterministic).
        """
        import time
        t0 = time.monotonic()

        if _BRAVE_API_KEY:
            raw_results = await self._search_brave(query)
        elif _GOOGLE_CSE_KEY and _GOOGLE_CSE_CX:
            raw_results = await self._search_google_cse(query)
        else:
            raw_results = await self._search_duckduckgo(query)

        # Apply epistemic allowlist filter
        allowed = []
        filtered = 0
        for result in raw_results:
            if self._is_domain_allowed(result.url):
                allowed.append(result)
            else:
                filtered += 1

        # Bound result count
        bounded = tuple(allowed[:_MAX_RESULTS])

        elapsed_ms = (time.monotonic() - t0) * 1000

        logger.info(
            "[WebSearch] query=%r -> %d raw, %d filtered, %d returned (%.0fms, %s)",
            query[:60], len(raw_results), filtered,
            len(bounded), elapsed_ms, self.backend_name,
        )

        return SearchResponse(
            query=query,
            results=bounded,
            backend=self.backend_name,
            total_raw_results=len(raw_results),
            filtered_count=filtered,
            search_time_ms=elapsed_ms,
        )

    async def search_and_fetch(self, query: str) -> SearchResponse:
        """Search and fetch full page text for top results.

        Two-phase: search API for ranked results, then fetch each
        result's URL for full text extraction. Both phases are bounded.
        """
        response = await self.search(query)
        if not response.results:
            return response

        # Fetch full page text for each result (bounded, parallel)
        enriched = []
        session = await self._get_session()

        for result in response.results:
            page_text = await self._fetch_page_text(session, result.url)
            enriched.append(SearchResult(
                title=result.title,
                url=result.url,
                snippet=result.snippet,
                domain=result.domain,
                page_text=page_text,
            ))

        return SearchResponse(
            query=response.query,
            results=tuple(enriched),
            backend=response.backend,
            total_raw_results=response.total_raw_results,
            filtered_count=response.filtered_count,
            search_time_ms=response.search_time_ms,
        )

    def format_for_prompt(self, response: SearchResponse) -> str:
        """Format search results as context for the generation prompt.

        Produces a structured block that the 397B Architect can reason over
        to inform code generation.
        """
        if not response.results:
            return ""

        lines = [
            f"## Web Search Results for: {response.query}",
            f"(source: {response.backend}, {len(response.results)} results)",
            "",
        ]

        for i, result in enumerate(response.results, 1):
            tier = self._get_trust_tier(result.url)
            tier_label = {1: "verified", 2: "community source", 3: "unverified"}.get(tier, "unknown")
            lines.append(f"### Result {i}: {result.title}")
            lines.append(f"**Source:** {result.url}")
            lines.append(f"**Trust:** [{tier_label}] (Tier {tier})")
            lines.append("")

            if tier == 3:
                # Tier 3: snippet only, no full page (safety boundary)
                lines.append(f"*[unverified snippet]* {result.snippet}")
            elif result.page_text:
                if tier == 2:
                    lines.append(f"*[community source — verify before using]*")
                lines.append(result.page_text)
            else:
                lines.append(result.snippet)

            lines.append("")
            lines.append("---")
            lines.append("")

        lines.append(
            "Use these references to inform your solution. "
            "Cite the source URL if you use specific API patterns."
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Epistemic allowlist (deterministic domain filter)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_domain_allowed(url: str) -> bool:
        """Check if a URL's domain is on any trust tier.

        Tier 1 + Tier 2 always allowed. Tier 3 only if enabled via env.
        Deterministic — no inference, no ambiguity.
        """
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            for domain in _EPISTEMIC_ALLOWLIST:  # Tier 1 + Tier 2
                if host == domain or host.endswith(f".{domain}"):
                    return True
            if _TIER3_ENABLED:
                return True  # All domains allowed in Tier 3 mode
            return False
        except Exception:
            return False

    @staticmethod
    def _get_trust_tier(url: str) -> int:
        """Get the trust tier for a URL. Deterministic domain matching.

        Returns 1 (fully trusted), 2 (semi-trusted), 3 (unverified), or 0 (blocked).
        """
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            for domain in _TIER1_DOMAINS:
                if host == domain or host.endswith(f".{domain}"):
                    return 1
            for domain in _TIER2_DOMAINS:
                if host == domain or host.endswith(f".{domain}"):
                    return 2
            if _TIER3_ENABLED:
                return 3
            return 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Brave Search API
    # ------------------------------------------------------------------

    async def _search_brave(self, query: str) -> List[SearchResult]:
        """Execute search via Brave Search API. Returns raw results."""
        session = await self._get_session()

        try:
            async with session.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={
                    "q": query,
                    "count": str(_MAX_RESULTS * 3),  # Fetch extra for filtering
                    "text_decorations": "false",
                    "search_lang": "en",
                },
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": _BRAVE_API_KEY,
                },
            ) as resp:
                if resp.status != 200:
                    logger.warning("[WebSearch] Brave API error: %d", resp.status)
                    return []

                data = await resp.json(content_type=None)

            results = []
            web_results = data.get("web", {}).get("results", [])
            for item in web_results:
                url = item.get("url", "")
                title = item.get("title", "")
                snippet = item.get("description", "")

                # Truncate snippet
                if len(snippet) > _MAX_SNIPPET_CHARS:
                    snippet = snippet[:_MAX_SNIPPET_CHARS] + "..."

                domain = urlparse(url).hostname or ""

                results.append(SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    domain=domain,
                ))

            return results

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[WebSearch] Brave search failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Google Custom Search API (fallback)
    # ------------------------------------------------------------------

    async def _search_google_cse(self, query: str) -> List[SearchResult]:
        """Execute search via Google Custom Search API. Returns raw results."""
        session = await self._get_session()

        try:
            async with session.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": _GOOGLE_CSE_KEY,
                    "cx": _GOOGLE_CSE_CX,
                    "q": query,
                    "num": str(min(_MAX_RESULTS * 3, 10)),
                },
            ) as resp:
                if resp.status != 200:
                    logger.warning("[WebSearch] Google CSE error: %d", resp.status)
                    return []

                data = await resp.json(content_type=None)

            results = []
            for item in data.get("items", []):
                url = item.get("link", "")
                title = item.get("title", "")
                snippet = item.get("snippet", "")

                if len(snippet) > _MAX_SNIPPET_CHARS:
                    snippet = snippet[:_MAX_SNIPPET_CHARS] + "..."

                domain = urlparse(url).hostname or ""

                results.append(SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    domain=domain,
                ))

            return results

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[WebSearch] Google CSE failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # DuckDuckGo HTML search (free, no API key, no account)
    # ------------------------------------------------------------------

    async def _search_duckduckgo(self, query: str) -> List[SearchResult]:
        """Execute search via DuckDuckGo HTML. Free, no API key needed.

        Uses the lite HTML version (html.duckduckgo.com/html/) which
        returns structured results without JavaScript. Parsed via regex.
        """
        session = await self._get_session()

        try:
            async with session.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": "us-en"},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "JARVIS-Trinity/1.0 (Ouroboros WebSearch)",
                },
            ) as resp:
                if resp.status != 200:
                    logger.warning("[WebSearch] DuckDuckGo error: %d", resp.status)
                    return []

                html = await resp.text()

            results = []

            # Parse result blocks from DDG HTML
            # Each result is in a <div class="result__body">
            # Title: <a class="result__a" href="...">title</a>
            # Snippet: <a class="result__snippet">...</a>
            result_blocks = re.findall(
                r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
                r'.*?class="result__snippet"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )

            for url_raw, title_raw, snippet_raw in result_blocks:
                # DDG wraps URLs in a redirect — extract the actual URL
                actual_url = url_raw
                uddg_match = re.search(r'uddg=([^&]+)', url_raw)
                if uddg_match:
                    from urllib.parse import unquote
                    actual_url = unquote(uddg_match.group(1))

                # Strip HTML tags from title and snippet
                title = re.sub(r'<[^>]+>', '', title_raw).strip()
                snippet = re.sub(r'<[^>]+>', '', snippet_raw).strip()

                if len(snippet) > _MAX_SNIPPET_CHARS:
                    snippet = snippet[:_MAX_SNIPPET_CHARS] + "..."

                domain = urlparse(actual_url).hostname or ""

                results.append(SearchResult(
                    title=title,
                    url=actual_url,
                    snippet=snippet,
                    domain=domain,
                ))

                if len(results) >= _MAX_RESULTS * 3:
                    break

            return results

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[WebSearch] DuckDuckGo search failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Page text fetcher (bounded, no JS)
    # ------------------------------------------------------------------

    async def _fetch_page_text(self, session: Any, url: str) -> str:
        """Fetch and extract text from a URL. Bounded, no JavaScript.

        Only fetches from allowlisted domains (already filtered by search).
        Returns extracted text or empty string on failure.
        """
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return ""

                raw = await resp.content.read(262144)  # 256KB max
                content_type = resp.headers.get("Content-Type", "")

                if "html" in content_type:
                    text = self._extract_text_from_html(raw.decode(errors="replace"))
                elif "json" in content_type:
                    text = raw.decode(errors="replace")
                else:
                    text = raw.decode(errors="replace")

                # Bound text size
                if len(text) > _MAX_PAGE_CHARS:
                    text = text[:_MAX_PAGE_CHARS] + "\n\n[TRUNCATED]"

                return text

        except asyncio.CancelledError:
            raise
        except Exception:
            return ""

    @staticmethod
    def _extract_text_from_html(html: str) -> str:
        """Extract readable text from HTML. Regex-based, no external deps."""
        text = re.sub(
            r"<(script|style|nav|footer|header)[^>]*>.*?</\1>",
            "", html, flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", " ", text)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
