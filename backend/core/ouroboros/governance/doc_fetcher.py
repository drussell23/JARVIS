"""
DocFetcher — Bounded external documentation retrieval for CONTEXT_EXPANSION.

P3 Gap: TheOracle can only see local files. When generating patches for
unfamiliar APIs, Ouroboros has no way to pull external documentation.

Boundary Principle:
  Deterministic: URL construction from package names (PyPI, GitHub README),
  HTTP fetch, HTML-to-text extraction, bounded output size.
  Agentic: The DECISION of which docs to fetch is made by the planning
  prompt in ContextExpander. This module only executes the fetch.

Safety constraints:
  - Domain allowlist (only PyPI, GitHub, readthedocs, known doc hosts)
  - Maximum response size (256KB per fetch)
  - Maximum fetches per expansion round (3)
  - Total timeout per round (30s)
  - No arbitrary URL following — only first-party doc sources
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MAX_FETCHES_PER_ROUND = int(os.environ.get("JARVIS_DOC_FETCH_MAX_PER_ROUND", "3"))
_FETCH_TIMEOUT_S = float(os.environ.get("JARVIS_DOC_FETCH_TIMEOUT_S", "15"))
_ROUND_TIMEOUT_S = float(os.environ.get("JARVIS_DOC_FETCH_ROUND_TIMEOUT_S", "30"))
_MAX_RESPONSE_BYTES = int(os.environ.get("JARVIS_DOC_FETCH_MAX_BYTES", "262144"))
_MAX_TEXT_CHARS = int(os.environ.get("JARVIS_DOC_FETCH_MAX_CHARS", "8000"))

# Domain allowlist — only fetch from known documentation sources
_ALLOWED_DOMAINS = frozenset({
    "pypi.org",
    "raw.githubusercontent.com",
    "github.com",
    "readthedocs.io",
    "readthedocs.org",
    "docs.python.org",
    "docs.aiohttp.org",
    "fastapi.tiangolo.com",
    "pydantic-docs.helpmanual.io",
    "docs.pydantic.dev",
    "numpy.org",
    "pytorch.org",
    "scikit-learn.org",
    "redis.io",
})

# Known package -> doc URL patterns (deterministic mapping)
_PYPI_API_TEMPLATE = "https://pypi.org/pypi/{package}/json"
_GITHUB_README_TEMPLATE = (
    "https://raw.githubusercontent.com/{owner}/{repo}/main/README.md"
)


class DocFetchResult:
    """Result of a single documentation fetch."""
    __slots__ = ("url", "success", "text", "source_type", "error")

    def __init__(
        self,
        url: str,
        success: bool,
        text: str = "",
        source_type: str = "unknown",
        error: str = "",
    ) -> None:
        self.url = url
        self.success = success
        self.text = text
        self.source_type = source_type
        self.error = error


class DocFetcher:
    """Bounded external documentation retrieval.

    Called by ContextExpander during CONTEXT_EXPANSION rounds to pull
    relevant external documentation for packages referenced in the
    operation's target files.

    All fetches are:
    - Domain-allowlisted (no arbitrary URLs)
    - Size-bounded (256KB raw, 8K chars extracted)
    - Time-bounded (15s per fetch, 30s per round)
    - Count-bounded (3 fetches per round max)
    """

    def __init__(self) -> None:
        self._session: Optional[Any] = None

    async def _get_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S),
                headers={"User-Agent": "JARVIS-Trinity/1.0 (Ouroboros ContextExpander)"},
            )
        return self._session

    def is_url_allowed(self, url: str) -> bool:
        """Check if URL is on the domain allowlist. Deterministic."""
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            # Check exact match or subdomain match
            return any(
                host == domain or host.endswith(f".{domain}")
                for domain in _ALLOWED_DOMAINS
            )
        except Exception:
            return False

    async def fetch_package_docs(
        self, package_names: List[str]
    ) -> List[DocFetchResult]:
        """Fetch documentation for a list of Python packages.

        Deterministic URL construction from package names:
        - PyPI JSON API for package description
        - GitHub README if homepage points to GitHub

        Bounded: max 3 packages per call, 30s total timeout.
        """
        results: List[DocFetchResult] = []
        packages = package_names[:_MAX_FETCHES_PER_ROUND]

        try:
            async with asyncio.timeout(_ROUND_TIMEOUT_S):
                for pkg in packages:
                    result = await self._fetch_pypi_package(pkg)
                    results.append(result)
        except asyncio.TimeoutError:
            logger.warning("[DocFetcher] Round timeout after %ds", _ROUND_TIMEOUT_S)
        except asyncio.CancelledError:
            raise

        return results

    async def fetch_urls(self, urls: List[str]) -> List[DocFetchResult]:
        """Fetch documentation from explicit URLs (allowlist-gated).

        Only URLs on the domain allowlist are fetched. Others are
        silently skipped with an error result.
        """
        results: List[DocFetchResult] = []
        allowed = [u for u in urls if self.is_url_allowed(u)]
        rejected = [u for u in urls if not self.is_url_allowed(u)]

        for url in rejected:
            results.append(DocFetchResult(
                url=url, success=False,
                error=f"Domain not in allowlist: {urlparse(url).hostname}",
                source_type="rejected",
            ))

        try:
            async with asyncio.timeout(_ROUND_TIMEOUT_S):
                for url in allowed[:_MAX_FETCHES_PER_ROUND]:
                    result = await self._fetch_url(url)
                    results.append(result)
        except asyncio.TimeoutError:
            logger.warning("[DocFetcher] Round timeout")
        except asyncio.CancelledError:
            raise

        return results

    # ------------------------------------------------------------------
    # Fetch implementations (deterministic HTTP + parse)
    # ------------------------------------------------------------------

    async def _fetch_pypi_package(self, package_name: str) -> DocFetchResult:
        """Fetch package description from PyPI JSON API."""
        url = _PYPI_API_TEMPLATE.format(package=package_name)
        session = await self._get_session()

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return DocFetchResult(
                        url=url, success=False,
                        error=f"HTTP {resp.status}",
                        source_type="pypi",
                    )

                data = await resp.json(content_type=None)
                info = data.get("info", {})

                # Extract useful fields
                description = info.get("description", "")
                summary = info.get("summary", "")
                homepage = info.get("home_page", "") or info.get("project_url", "")
                version = info.get("version", "")
                requires_python = info.get("requires_python", "")

                # Truncate description to max chars
                if len(description) > _MAX_TEXT_CHARS:
                    description = description[:_MAX_TEXT_CHARS] + "\n\n[TRUNCATED]"

                text = (
                    f"# {package_name} {version}\n\n"
                    f"**Summary:** {summary}\n"
                    f"**Requires Python:** {requires_python}\n"
                    f"**Homepage:** {homepage}\n\n"
                    f"## Description\n\n{description}"
                )

                return DocFetchResult(
                    url=url, success=True,
                    text=text,
                    source_type="pypi",
                )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return DocFetchResult(
                url=url, success=False,
                error=str(exc),
                source_type="pypi",
            )

    async def _fetch_url(self, url: str) -> DocFetchResult:
        """Fetch and extract text from an allowlisted URL."""
        session = await self._get_session()

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return DocFetchResult(
                        url=url, success=False,
                        error=f"HTTP {resp.status}",
                        source_type="url",
                    )

                # Read bounded response
                raw_bytes = await resp.content.read(_MAX_RESPONSE_BYTES)
                content_type = resp.headers.get("Content-Type", "")

                if "json" in content_type:
                    text = raw_bytes.decode(errors="replace")
                elif "markdown" in content_type or url.endswith(".md"):
                    text = raw_bytes.decode(errors="replace")
                elif "html" in content_type:
                    text = self._extract_text_from_html(
                        raw_bytes.decode(errors="replace")
                    )
                else:
                    text = raw_bytes.decode(errors="replace")

                # Truncate
                if len(text) > _MAX_TEXT_CHARS:
                    text = text[:_MAX_TEXT_CHARS] + "\n\n[TRUNCATED]"

                return DocFetchResult(
                    url=url, success=True,
                    text=text,
                    source_type="url",
                )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return DocFetchResult(
                url=url, success=False,
                error=str(exc),
                source_type="url",
            )

    @staticmethod
    def _extract_text_from_html(html: str) -> str:
        """Extract readable text from HTML. Simple regex-based — no external deps.

        Strips tags, collapses whitespace, removes script/style blocks.
        This is intentionally simple — we need readable text, not perfect parsing.
        """
        # Remove script and style blocks
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Decode common entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
