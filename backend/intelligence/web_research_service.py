"""
Structured Web Research Service
===============================

Shared service for live web research workflows:
- Search (provider-backed with fallback)
- Read (safe URL fetch and text extraction)
- Synthesize (structured report output)

Designed to be reused by both:
- Autonomous runtime tools
- Neural Mesh agents
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import aiohttp

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return max(value, minimum)
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        return max(value, minimum)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env_list(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[,\n;]+", raw) if item.strip()]


@dataclass
class WebResearchConfig:
    """Configuration for web research service behavior."""

    max_results_per_query: int = _env_int("JARVIS_WEBSEARCH_MAX_RESULTS", 8, minimum=1)
    max_sources_per_report: int = _env_int("JARVIS_WEBSEARCH_MAX_SOURCES", 5, minimum=1)
    max_parallel_reads: int = _env_int("JARVIS_WEBSEARCH_MAX_PARALLEL_READS", 4, minimum=1)
    max_page_chars: int = _env_int("JARVIS_WEBSEARCH_MAX_PAGE_CHARS", 12000, minimum=500)
    max_summary_points: int = _env_int("JARVIS_WEBSEARCH_MAX_SUMMARY_POINTS", 8, minimum=1)
    request_timeout_seconds: float = _env_float("JARVIS_WEBSEARCH_REQUEST_TIMEOUT", 20.0, minimum=1.0)
    connect_timeout_seconds: float = _env_float("JARVIS_WEBSEARCH_CONNECT_TIMEOUT", 6.0, minimum=0.5)
    use_ouroboros_backend: bool = _env_bool("JARVIS_WEBSEARCH_USE_OUROBOROS_BACKEND", True)
    allow_private_networks: bool = _env_bool("JARVIS_WEBSEARCH_ALLOW_PRIVATE_NETWORKS", False)
    dns_rebind_protection: bool = _env_bool("JARVIS_WEBSEARCH_DNS_REBIND_PROTECTION", True)
    user_agent: str = os.getenv(
        "JARVIS_WEBSEARCH_USER_AGENT",
        "JARVIS-WebResearch/1.0 (+https://jarvis.local)",
    )
    allowed_domains: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.allowed_domains is None:
            self.allowed_domains = [domain.lower() for domain in _env_list("JARVIS_WEBSEARCH_ALLOWED_DOMAINS")]


class WebResearchService:
    """Async web research pipeline with secure fetch and structured synthesis."""

    _LOCAL_HOSTS: Set[str] = {
        "localhost",
        "localhost.localdomain",
        "127.0.0.1",
        "::1",
    }

    def __init__(self, config: Optional[WebResearchConfig] = None):
        self.config = config or WebResearchConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._search_backend: Optional[Any] = None
        self._search_result_type_enum: Optional[Any] = None
        self._read_semaphore = asyncio.Semaphore(self.config.max_parallel_reads)
        self._metrics: Dict[str, Any] = {
            "search_requests": 0,
            "search_failures": 0,
            "page_reads": 0,
            "page_read_failures": 0,
            "research_runs": 0,
            "research_failures": 0,
            "avg_search_latency_ms": 0.0,
            "avg_read_latency_ms": 0.0,
            "avg_research_latency_ms": 0.0,
            "backend": "fallback",
        }

    async def initialize(self) -> None:
        """Initialize HTTP client and optional provider-backed search backend."""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            timeout = aiohttp.ClientTimeout(
                total=self.config.request_timeout_seconds,
                connect=self.config.connect_timeout_seconds,
            )
            connector = aiohttp.TCPConnector(limit=max(self.config.max_parallel_reads * 2, 8))

            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={"User-Agent": self.config.user_agent},
            )

            if self.config.use_ouroboros_backend:
                await self._initialize_ouroboros_backend()

            self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown underlying resources."""
        async with self._init_lock:
            if self._search_backend is not None:
                try:
                    await self._search_backend.shutdown()
                except Exception as exc:
                    logger.debug("Web search backend shutdown warning: %s", exc)
                finally:
                    self._search_backend = None

            if self._session is not None:
                await self._session.close()
                self._session = None

            self._initialized = False

    async def search(
        self,
        query: str,
        max_results: Optional[int] = None,
        include_types: Optional[Sequence[str]] = None,
        use_cache: bool = True,
    ) -> List[Dict[str, Any]]:
        """Search web and return normalized results."""
        await self.initialize()

        normalized_query = (query or "").strip()
        if not normalized_query:
            raise ValueError("Missing required query")

        limit = max_results or self.config.max_results_per_query
        limit = max(1, limit)
        start = time.perf_counter()
        self._metrics["search_requests"] += 1

        try:
            if self._search_backend is not None:
                native_types = self._resolve_native_result_types(include_types)
                raw_results = await self._search_backend.search(
                    normalized_query,
                    max_results=limit,
                    result_types=native_types,
                    use_cache=use_cache,
                )
                results = self._normalize_search_results(raw_results, limit)
                self._update_latency_metric("avg_search_latency_ms", start)
                return results

            fallback = await self._fallback_search(normalized_query, limit)
            self._update_latency_metric("avg_search_latency_ms", start)
            return fallback

        except Exception:
            self._metrics["search_failures"] += 1
            self._update_latency_metric("avg_search_latency_ms", start)
            raise

    async def read_page(
        self,
        url: str,
        max_chars: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Fetch and extract text content from a web page safely."""
        await self.initialize()
        if self._session is None:
            raise RuntimeError("WebResearchService session not initialized")

        target_url = (url or "").strip()
        if not target_url:
            raise ValueError("Missing required url")

        await self._validate_url(target_url)
        limit_chars = max_chars or self.config.max_page_chars
        start = time.perf_counter()
        self._metrics["page_reads"] += 1

        async with self._read_semaphore:
            try:
                async with self._session.get(target_url, allow_redirects=True) as response:
                    body = await response.text(errors="ignore")
                    final_url = str(response.url)
                    content_type = response.headers.get("Content-Type", "")
                    title, extracted = self._extract_text(body)
                    extracted = extracted[:limit_chars]
                    self._update_latency_metric("avg_read_latency_ms", start)
                    return {
                        "url": target_url,
                        "final_url": final_url,
                        "status_code": response.status,
                        "content_type": content_type,
                        "title": title,
                        "content": extracted,
                        "content_chars": len(extracted),
                        "fetched_at": datetime.utcnow().isoformat(),
                    }
            except Exception:
                self._metrics["page_read_failures"] += 1
                self._update_latency_metric("avg_read_latency_ms", start)
                raise

    async def research(
        self,
        query: str,
        max_results: Optional[int] = None,
        max_sources: Optional[int] = None,
        include_types: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Run search -> read -> synthesize and return a structured report."""
        start = time.perf_counter()
        self._metrics["research_runs"] += 1

        query_text = (query or "").strip()
        if not query_text:
            raise ValueError("Missing required query")

        try:
            search_results = await self.search(
                query=query_text,
                max_results=max_results,
                include_types=include_types,
                use_cache=True,
            )

            sources_limit = max_sources or self.config.max_sources_per_report
            selected = self._select_sources(search_results, max_sources=sources_limit)

            read_tasks = [
                asyncio.create_task(self._safe_read_selected_source(source), name=f"read:{source['url'][:48]}")
                for source in selected
            ]
            read_results = await asyncio.gather(*read_tasks, return_exceptions=True)

            source_documents: List[Dict[str, Any]] = []
            warnings: List[str] = []
            for idx, result in enumerate(read_results):
                source_meta = selected[idx]
                if isinstance(result, Exception):
                    warnings.append(f"Failed reading {source_meta['url']}: {result}")
                    source_documents.append({
                        **source_meta,
                        "read_success": False,
                        "content": "",
                        "title": source_meta.get("title") or "",
                    })
                    continue

                source_documents.append({
                    **source_meta,
                    "read_success": True,
                    "title": result.get("title") or source_meta.get("title") or "",
                    "content": result.get("content", ""),
                    "final_url": result.get("final_url") or source_meta["url"],
                    "status_code": result.get("status_code"),
                })

            key_points = self._synthesize_key_points(query_text, source_documents)
            summary = self._compose_summary(query_text, key_points, source_documents)

            report = {
                "query": query_text,
                "generated_at": datetime.utcnow().isoformat(),
                "summary": summary,
                "key_points": key_points,
                "sources": [
                    {
                        "title": src.get("title", ""),
                        "url": src.get("final_url", src.get("url", "")),
                        "domain": src.get("domain", ""),
                        "snippet": src.get("snippet", ""),
                        "relevance_score": src.get("relevance_score", 0.0),
                        "read_success": src.get("read_success", False),
                        "content_excerpt": src.get("content", "")[:500],
                    }
                    for src in source_documents
                ],
                "search_results_count": len(search_results),
                "sources_selected_count": len(selected),
                "sources_read_count": sum(1 for src in source_documents if src.get("read_success")),
                "warnings": warnings,
            }
            report["markdown_report"] = self.render_markdown_report(report)
            self._update_latency_metric("avg_research_latency_ms", start)
            return report

        except Exception:
            self._metrics["research_failures"] += 1
            self._update_latency_metric("avg_research_latency_ms", start)
            raise

    def render_markdown_report(self, report: Dict[str, Any]) -> str:
        """Render a research report in Markdown."""
        lines = [
            f"# Web Research Report: {report.get('query', '')}",
            "",
            f"_Generated: {report.get('generated_at', '')}_",
            "",
            "## Summary",
            report.get("summary", "No summary available."),
            "",
            "## Key Points",
        ]

        points = report.get("key_points", [])
        if not points:
            lines.append("- No key points could be extracted.")
        else:
            for point in points:
                src = point.get("source_title") or point.get("source_url") or "unknown source"
                lines.append(f"- {point.get('point', '').strip()} ({src})")

        lines.extend(["", "## Sources"])
        for source in report.get("sources", []):
            status = "read" if source.get("read_success") else "search-only"
            title = source.get("title") or source.get("url") or "Untitled"
            url = source.get("url", "")
            lines.append(f"- [{title}]({url}) ({status})")

        warnings = report.get("warnings") or []
        if warnings:
            lines.extend(["", "## Warnings"])
            for warning in warnings:
                lines.append(f"- {warning}")

        return "\n".join(lines)

    def get_health(self) -> Dict[str, Any]:
        """Service health snapshot."""
        backend_name = self._metrics.get("backend", "fallback")
        return {
            "initialized": self._initialized,
            "backend": backend_name,
            "has_http_session": self._session is not None,
            "config": {
                "max_results_per_query": self.config.max_results_per_query,
                "max_sources_per_report": self.config.max_sources_per_report,
                "max_parallel_reads": self.config.max_parallel_reads,
                "max_page_chars": self.config.max_page_chars,
                "allow_private_networks": self.config.allow_private_networks,
                "dns_rebind_protection": self.config.dns_rebind_protection,
                "allowed_domains": list(self.config.allowed_domains),
            },
        }

    def get_metrics(self) -> Dict[str, Any]:
        """Current runtime metrics."""
        return dict(self._metrics)

    async def _initialize_ouroboros_backend(self) -> None:
        """Best-effort initialization of existing Ouroboros web search backend."""
        try:
            from backend.core.ouroboros.native_integration import (
                SearchResultType,
                WebSearchConfig,
                WebSearchExtractor,
            )

            config = WebSearchConfig()
            config.max_results_per_query = max(
                config.max_results_per_query,
                self.config.max_results_per_query,
            )
            config.request_timeout = max(config.request_timeout, self.config.request_timeout_seconds)
            config.connection_timeout = max(config.connection_timeout, self.config.connect_timeout_seconds)
            extractor = WebSearchExtractor(config)
            await extractor.initialize()

            self._search_backend = extractor
            self._search_result_type_enum = SearchResultType
            self._metrics["backend"] = "ouroboros"
            logger.info("WebResearchService using Ouroboros search backend")
        except Exception as exc:
            logger.warning("Ouroboros web search backend unavailable, falling back to lite search: %s", exc)
            self._search_backend = None
            self._search_result_type_enum = None
            self._metrics["backend"] = "fallback"

    def _resolve_native_result_types(self, include_types: Optional[Sequence[str]]) -> Optional[List[Any]]:
        """Translate string type filters to native SearchResultType enum values."""
        if not include_types or self._search_result_type_enum is None:
            return None

        resolved: List[Any] = []
        for type_name in include_types:
            token = str(type_name).strip().lower()
            if not token:
                continue
            for member in self._search_result_type_enum:
                if member.name.lower() == token or str(member.value).lower() == token:
                    resolved.append(member)
                    break
        return resolved or None

    async def _fallback_search(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """Fallback web search via DuckDuckGo Lite."""
        if self._session is None:
            raise RuntimeError("WebResearchService session not initialized")

        url = "https://lite.duckduckgo.com/lite/"
        payload = {"q": query, "kl": "us-en"}
        async with self._session.post(url, data=payload) as response:
            response.raise_for_status()
            html = await response.text(errors="ignore")

        pattern = re.compile(
            r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippets = re.findall(
            r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        results: List[Dict[str, Any]] = []
        for index, match in enumerate(pattern.finditer(html)):
            href = unescape(match.group("href")).strip()
            title = self._clean_html_text(match.group("title"))
            if not href or not title:
                continue
            decoded_url = self._decode_duckduckgo_redirect(href)
            parsed = urlparse(decoded_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            snippet = self._clean_html_text(snippets[index] if index < len(snippets) else "")
            results.append({
                "title": title,
                "url": decoded_url,
                "snippet": snippet,
                "provider": "duckduckgo_lite",
                "result_type": "web_page",
                "relevance_score": max(0.0, 1.0 - (index * 0.04)),
                "cached": False,
                "domain": (parsed.netloc or "").lower(),
            })
            if len(results) >= max_results:
                break

        return self._dedupe_results(results)[:max_results]

    def _normalize_search_results(self, raw_results: Sequence[Any], max_results: int) -> List[Dict[str, Any]]:
        """Normalize native result objects to dict structure."""
        normalized: List[Dict[str, Any]] = []
        for result in raw_results:
            try:
                url = str(getattr(result, "url", "")).strip()
                if not url:
                    continue
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"}:
                    continue
                normalized.append({
                    "title": str(getattr(result, "title", "")).strip(),
                    "url": url,
                    "snippet": str(getattr(result, "snippet", "")).strip(),
                    "provider": str(getattr(getattr(result, "provider", None), "value", "")) or str(getattr(result, "provider", "")),
                    "result_type": str(getattr(getattr(result, "result_type", None), "value", "")) or str(getattr(result, "result_type", "")),
                    "relevance_score": float(getattr(result, "relevance_score", 0.0) or 0.0),
                    "cached": bool(getattr(result, "cached", False)),
                    "domain": (parsed.netloc or "").lower(),
                })
            except Exception:
                continue

        deduped = self._dedupe_results(normalized)
        deduped.sort(key=lambda item: item.get("relevance_score", 0.0), reverse=True)
        return deduped[:max_results]

    @staticmethod
    def _dedupe_results(results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: Set[str] = set()
        deduped: List[Dict[str, Any]] = []
        for item in results:
            key = item.get("url", "").rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _select_sources(self, results: Sequence[Dict[str, Any]], max_sources: int) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        seen_domains: Set[str] = set()
        for item in results:
            url = item.get("url", "")
            parsed = urlparse(url)
            domain = (parsed.netloc or "").lower()
            if not domain:
                continue
            # Prefer domain diversity for stronger synthesis
            if domain in seen_domains and len(selected) >= max_sources:
                continue
            selected.append({
                "title": item.get("title", ""),
                "url": url,
                "snippet": item.get("snippet", ""),
                "relevance_score": item.get("relevance_score", 0.0),
                "domain": domain,
                "provider": item.get("provider", ""),
                "result_type": item.get("result_type", ""),
            })
            seen_domains.add(domain)
            if len(selected) >= max_sources:
                break
        return selected

    async def _safe_read_selected_source(self, source: Dict[str, Any]) -> Dict[str, Any]:
        return await self.read_page(source["url"], max_chars=self.config.max_page_chars)

    def _synthesize_key_points(
        self,
        query: str,
        source_documents: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Generate ranked key points from source content."""
        query_terms = {
            token
            for token in re.findall(r"[a-zA-Z0-9]{3,}", query.lower())
        }

        candidates: List[Tuple[float, str, str, str]] = []
        for source in source_documents:
            if not source.get("read_success"):
                continue
            content = source.get("content", "")
            title = source.get("title") or source.get("url") or ""
            url = source.get("final_url") or source.get("url") or ""
            sentences = self._split_sentences(content)[:40]
            for idx, sentence in enumerate(sentences):
                sentence_clean = sentence.strip()
                if len(sentence_clean) < 50:
                    continue
                lower = sentence_clean.lower()
                overlap = sum(1 for term in query_terms if term in lower)
                score = overlap * 2.5
                if idx < 5:
                    score += 0.5
                if title and any(term in title.lower() for term in query_terms):
                    score += 0.2
                if score <= 0:
                    continue
                candidates.append((score, sentence_clean, url, title))

        candidates.sort(key=lambda item: item[0], reverse=True)
        seen_sentences: Set[str] = set()
        points: List[Dict[str, Any]] = []
        for score, sentence, url, title in candidates:
            normalized = sentence.lower()
            if normalized in seen_sentences:
                continue
            seen_sentences.add(normalized)
            points.append({
                "point": sentence,
                "source_url": url,
                "source_title": title,
                "score": round(score, 3),
            })
            if len(points) >= self.config.max_summary_points:
                break
        return points

    @staticmethod
    def _compose_summary(
        query: str,
        key_points: Sequence[Dict[str, Any]],
        source_documents: Sequence[Dict[str, Any]],
    ) -> str:
        if not key_points:
            successful = sum(1 for src in source_documents if src.get("read_success"))
            if successful == 0:
                return (
                    f"No readable sources were available for '{query}'. "
                    "Search results were collected, but page extraction did not return usable content."
                )
            return (
                f"Sources were retrieved for '{query}', but no high-confidence synthesized points "
                "were extracted from the available content."
            )

        top = [point["point"] for point in key_points[:3]]
        return " ".join(top)

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        raw = re.split(r"(?<=[.!?])\s+", text or "")
        return [segment.strip() for segment in raw if segment and segment.strip()]

    @staticmethod
    def _extract_text(html_text: str) -> Tuple[str, str]:
        """Extract title and readable text from HTML."""
        try:
            from bs4 import BeautifulSoup  # type: ignore

            soup = BeautifulSoup(html_text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            title = soup.title.get_text(strip=True) if soup.title else ""
            body = soup.find("main") or soup.find("article") or soup.find("body") or soup
            text = body.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            return title, text
        except Exception:
            # Fallback without bs4
            stripped = re.sub(r"(?is)<(script|style|noscript).*?>.*?(</\1>)", " ", html_text)
            title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", stripped)
            title = WebResearchService._clean_html_text(title_match.group(1)) if title_match else ""
            without_tags = re.sub(r"(?s)<[^>]+>", " ", stripped)
            text = WebResearchService._clean_html_text(without_tags)
            return title, text

    @staticmethod
    def _clean_html_text(value: str) -> str:
        cleaned = unescape(value or "")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    @staticmethod
    def _decode_duckduckgo_redirect(url: str) -> str:
        parsed = urlparse(url)
        # v242.1: Exact domain match to prevent CWE-20 URL substring bypass
        # (was: "duckduckgo.com" in parsed.netloc — matched evil-duckduckgo.com)
        _netloc = (parsed.netloc or "").lower()
        if (_netloc == "duckduckgo.com" or _netloc.endswith(".duckduckgo.com")) and parsed.path.startswith("/l/"):
            query_params = parse_qs(parsed.query)
            uddg = query_params.get("uddg", [])
            if uddg:
                return unquote(uddg[0])
        return url

    async def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise PermissionError(f"Unsupported URL scheme: {parsed.scheme}")

        host = (parsed.hostname or "").lower()
        if not host:
            raise PermissionError("URL host is required")

        if self.config.allowed_domains:
            if not any(host == domain or host.endswith(f".{domain}") for domain in self.config.allowed_domains):
                raise PermissionError(f"Domain not allowed by policy: {host}")

        if self.config.allow_private_networks:
            return

        if host in self._LOCAL_HOSTS:
            raise PermissionError(f"Blocked local host: {host}")

        ip_obj: Optional[ipaddress._BaseAddress] = None
        try:
            ip_obj = ipaddress.ip_address(host)
        except ValueError:
            ip_obj = None

        if ip_obj is not None and self._is_restricted_ip(ip_obj):
            raise PermissionError(f"Blocked private or unsafe IP target: {host}")

        if self.config.dns_rebind_protection:
            await self._enforce_dns_public_resolution(host)

    async def _enforce_dns_public_resolution(self, host: str) -> None:
        """Reject hostnames that resolve to private or local addresses."""
        try:
            loop = asyncio.get_running_loop()
            addr_info = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return
        except Exception:
            return

        for entry in addr_info:
            sockaddr = entry[4]
            if not sockaddr:
                continue
            resolved = sockaddr[0]
            try:
                ip_obj = ipaddress.ip_address(resolved)
            except ValueError:
                continue
            if self._is_restricted_ip(ip_obj):
                raise PermissionError(f"Blocked hostname resolving to private address: {host}")

    @staticmethod
    def _is_restricted_ip(ip_obj: ipaddress._BaseAddress) -> bool:
        return any([
            ip_obj.is_private,
            ip_obj.is_loopback,
            ip_obj.is_link_local,
            ip_obj.is_multicast,
            ip_obj.is_reserved,
            ip_obj.is_unspecified,
        ])

    def _update_latency_metric(self, metric_name: str, start_time: float) -> None:
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        current = float(self._metrics.get(metric_name, 0.0))
        count_key_map = {
            "avg_search_latency_ms": "search_requests",
            "avg_read_latency_ms": "page_reads",
            "avg_research_latency_ms": "research_runs",
        }
        denominator = max(1, int(self._metrics.get(count_key_map.get(metric_name, ""), 1)))
        updated = ((current * (denominator - 1)) + elapsed_ms) / denominator
        self._metrics[metric_name] = round(updated, 3)


_service_instance: Optional[WebResearchService] = None


def get_web_research_service(config: Optional[WebResearchConfig] = None) -> WebResearchService:
    """Get singleton web research service."""
    global _service_instance
    if _service_instance is None:
        _service_instance = WebResearchService(config=config)
    return _service_instance

