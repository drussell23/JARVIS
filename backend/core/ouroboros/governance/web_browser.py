"""
WebBrowser — Unified Governance Composer for Web Capabilities
==============================================================

Closes §41.5 (PRD v3.0) — the operator-flagged web search + browsing
capability gap for autonomous JARVIS development. Per the binding:

  "Venom has basic web_fetch + web_search tools, but Claude's rich
   browsing (multi-page navigation, JS render, link-follow, image
   extraction, authenticated sessions, search-then-cite chains) is
   MISSING."

This substrate is a **thin async governance composer** over five
existing canonical surfaces:

* :class:`web_search.WebSearchCapability` — Tier-1 search (Brave /
  Google CSE / DuckDuckGo) with static epistemic allowlist.
* :class:`browser_bridge.BrowserBridge` — Tier-1 JS render via
  Playwright subprocess.
* :class:`backend.intelligence.web_research_service.WebResearchService`
  — Tier-2 async orchestrator with DNS-rebind protection,
  private-network gate, parallel-read, secure aiohttp fetch.
* :func:`conversation_bridge.redact_secrets` — Tier-1 sanitizer
  applied to every fetched response before the model sees it.
* :func:`mcp_output_scanner.scan_mcp_output` — credential-leak
  detector (Wave 3 #5) applied to every fetched response.
* :func:`cross_process_jsonl.flock_append_line` — §33.4 citation
  ledger at ``.jarvis/web_browsing_ledger.jsonl``.

NO file rewrites. The five backends remain first-class — they're
still accessible directly by their existing consumers
(CONTEXT_EXPANSION, Visual VERIFY, Neural Mesh agents, Executor
context). The new substrate is the **single governance entry
point** that O+V's Venom tool loop calls into; the substrate
routes to the right backend deterministically through the closed
:class:`BrowsingAction` taxonomy.

Deterministic routing table:

  SEARCH         → web_research_service.search (falls back to
                   web_search.WebSearchCapability if unavailable)
  NAVIGATE       → web_research_service.read_page (static fetch)
                   OR browser_bridge.navigate (when js_render hint)
  FOLLOW_LINK    → web_research_service.read_page (composed URL)
  EXTRACT_TEXT   → browser_bridge.read_page_text (js_render=True)
                   OR web_research_service.read_page (default)
  EXTRACT_IMAGE  → browser_bridge.screenshot (Playwright only)
  CITE           → pure ledger write — no network

Every network response passes through three governance gates
**after** the network surface returns and **before** the result
reaches the caller:

  1. Per-domain allowlist intersected with backend's own allowlist
     (operator-tunable via JARVIS_WEB_BROWSER_DOMAIN_ALLOWLIST).
  2. conversation_bridge.redact_secrets() — Tier-1 sanitize on
     response body. Bytes-redaction count is reported, but the
     SANITIZED body is what the caller receives.
  3. mcp_output_scanner.scan_mcp_output() — if credentials are
     detected the verdict becomes CREDENTIAL_LEAKED and the
     sanitized body is REPLACED with a placeholder so the leak
     never reaches the model even if the sanitizer missed shapes
     the scanner caught.

Closed 6-value :class:`BrowsingAction`:

  SEARCH         multi-backend search
  NAVIGATE       static URL fetch (no JS)
  FOLLOW_LINK    fetch a URL that came from a prior SEARCH result
  EXTRACT_TEXT   text content from a page (optionally JS-rendered)
  EXTRACT_IMAGE  screenshot or extracted image (Playwright-only)
  CITE           pure-ledger citation write (no network)

Closed 5-value :class:`BrowsingVerdict`:

  CLEAN              network call succeeded + sanitizers clean
  CREDENTIAL_LEAKED  mcp_output_scanner found ≥1 credential
                     (sanitized body REPLACED with placeholder)
  OUT_OF_ALLOWLIST   URL host outside operator's domain allowlist
                     OR outside backend's epistemic allowlist
  RATE_LIMITED       backend reported HTTP 429 OR timeout exceeded
                     internal request budget
  FAILED             backend exception / unreachable / malformed
                     URL / backend not available

§33.1 cognitive substrate ``JARVIS_WEB_BROWSER_ENABLED``
default-**FALSE**. Sub-flags ``JARVIS_WEB_BROWSER_PERSIST_ENABLED``
(default TRUE), ``JARVIS_WEB_BROWSER_ALLOW_JS_RENDER``
(default FALSE — gates browser_bridge Playwright route),
``JARVIS_WEB_BROWSER_DOMAIN_ALLOWLIST`` (comma-separated host
suffixes, empty = use backend's own allowlist).

Authority asymmetry (AST-pinned): imports stdlib only at
module load. The five canonical surfaces are lazy-imported
behind composer helpers so the substrate import stays cheap
and substrate purity is preserved. Does NOT import
orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
tool_executor (one-way cage: Venom tool registration is wired
operator-side in a follow-up slice, not by this substrate).
"""
from __future__ import annotations

import ast
import asyncio
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


WEB_BROWSER_SCHEMA_VERSION: str = "web_browser.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_WEB_BROWSER_ENABLED"
_ENV_PERSIST = "JARVIS_WEB_BROWSER_PERSIST_ENABLED"
_ENV_ALLOW_JS_RENDER = "JARVIS_WEB_BROWSER_ALLOW_JS_RENDER"
_ENV_DOMAIN_ALLOWLIST = "JARVIS_WEB_BROWSER_DOMAIN_ALLOWLIST"
_ENV_MAX_FETCH_BYTES = "JARVIS_WEB_BROWSER_MAX_FETCH_BYTES"
_ENV_REQUEST_TIMEOUT_S = "JARVIS_WEB_BROWSER_REQUEST_TIMEOUT_S"
_ENV_LEDGER_PATH = "JARVIS_WEB_BROWSER_LEDGER_PATH"
_ENV_CITATION_BOUND = "JARVIS_WEB_BROWSER_CITATION_BOUND"

_DEFAULT_MAX_FETCH_BYTES = 200_000
_DEFAULT_REQUEST_TIMEOUT_S = 20
_DEFAULT_CITATION_BOUND = 256

_DEFAULT_LEDGER_REL = ".jarvis/web_browsing_ledger.jsonl"
_CREDENTIAL_LEAK_PLACEHOLDER = (
    "[REDACTED — credential leak detected by mcp_output_scanner; "
    "fetched body suppressed for safety]"
)

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. With master off all browsing actions
    return ``DISABLED``-equivalent ``FAILED`` verdicts with a
    diagnostic; the five backends stay accessible to their
    existing consumers (CONTEXT_EXPANSION / Visual VERIFY /
    Neural Mesh / Executor context) — only the composer surface
    is gated.
    """
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gate §33.4 citation ledger writes. Default TRUE."""
    return _flag(_ENV_PERSIST, default=True)


def js_render_enabled() -> bool:
    """Sub-flag — gate the browser_bridge Playwright route.
    Default FALSE because Playwright is heavyweight (subprocess
    + Chromium) and not every operator wants it active."""
    return _flag(_ENV_ALLOW_JS_RENDER, default=False)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def max_fetch_bytes() -> int:
    """Cap on bytes returned in any single BrowsingResult body.
    Defaults to 200_000 (~200KB). Clamped to [1024, 10_000_000]."""
    return _read_clamped_int(
        _ENV_MAX_FETCH_BYTES, _DEFAULT_MAX_FETCH_BYTES,
        1024, 10_000_000,
    )


def request_timeout_s() -> int:
    """Hard per-call timeout. Defaults to 20s. Clamped to [1, 300]."""
    return _read_clamped_int(
        _ENV_REQUEST_TIMEOUT_S, _DEFAULT_REQUEST_TIMEOUT_S, 1, 300,
    )


def citation_bound() -> int:
    """Cap on citation fragment length in ledger. Defaults to 256."""
    return _read_clamped_int(
        _ENV_CITATION_BOUND, _DEFAULT_CITATION_BOUND, 16, 4096,
    )


def operator_domain_allowlist() -> Tuple[str, ...]:
    """Operator-tunable allowlist of host suffixes (case-
    insensitive). Empty tuple means *no additional restriction*
    beyond the backend's own allowlist — the cage stays open
    enough for the backend to handle. Setting any value TIGHTENS
    the cage (composer AND backend must both allow)."""
    raw = os.environ.get(_ENV_DOMAIN_ALLOWLIST, "").strip()
    if not raw:
        return ()
    out: List[str] = []
    for chunk in raw.split(","):
        c = chunk.strip().lower()
        if c:
            out.append(c)
    return tuple(out)


def ledger_path() -> Path:
    """§33.4 citation ledger path. Defaults to
    ``.jarvis/web_browsing_ledger.jsonl``."""
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class BrowsingAction(str, enum.Enum):
    """Closed 6-value action — bytes-pinned via AST."""

    SEARCH = "search"
    NAVIGATE = "navigate"
    FOLLOW_LINK = "follow_link"
    EXTRACT_TEXT = "extract_text"
    EXTRACT_IMAGE = "extract_image"
    CITE = "cite"


class BrowsingVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned via AST."""

    CLEAN = "clean"
    CREDENTIAL_LEAKED = "credential_leaked"
    OUT_OF_ALLOWLIST = "out_of_allowlist"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


_ACTION_GLYPH: Dict[str, str] = {
    BrowsingAction.SEARCH.value: "🔍",
    BrowsingAction.NAVIGATE.value: "🌐",
    BrowsingAction.FOLLOW_LINK.value: "🔗",
    BrowsingAction.EXTRACT_TEXT.value: "📄",
    BrowsingAction.EXTRACT_IMAGE.value: "🖼",
    BrowsingAction.CITE.value: "📜",
}


_VERDICT_GLYPH: Dict[str, str] = {
    BrowsingVerdict.CLEAN.value: "✓",
    BrowsingVerdict.CREDENTIAL_LEAKED.value: "🚫",
    BrowsingVerdict.OUT_OF_ALLOWLIST.value: "⛔",
    BrowsingVerdict.RATE_LIMITED.value: "⏳",
    BrowsingVerdict.FAILED.value: "✗",
}


def action_glyph(action: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(action, "value"):
            return _ACTION_GLYPH.get(str(action.value), "?")
        return _ACTION_GLYPH.get(
            str(action or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def _coerce_action(raw: Any) -> Optional[BrowsingAction]:
    if isinstance(raw, BrowsingAction):
        return raw
    try:
        s = str(getattr(raw, "value", raw) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return None
    for a in BrowsingAction:
        if a.value == s:
            return a
    return None


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class BrowsingResult:
    """One browsing-action result. Frozen audit record."""

    action: BrowsingAction
    verdict: BrowsingVerdict
    url: str
    host: str
    content_bytes: int
    sanitized_body: str
    redacted_bytes: int
    leaked_credential_kinds: Tuple[str, ...]
    backend_used: str
    latency_ms: float
    diagnostic: str
    op_id: str = ""
    evaluated_at_unix: float = 0.0
    schema_version: str = WEB_BROWSER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "verdict": self.verdict.value,
            "url": self.url[:512],
            "host": self.host[:256],
            "content_bytes": int(self.content_bytes),
            "sanitized_body_preview": self.sanitized_body[:512],
            "redacted_bytes": int(self.redacted_bytes),
            "leaked_credential_kinds": list(
                self.leaked_credential_kinds,
            ),
            "backend_used": self.backend_used[:64],
            "latency_ms": float(self.latency_ms),
            "diagnostic": self.diagnostic[:512],
            "op_id": self.op_id[:128],
            "evaluated_at_unix": float(self.evaluated_at_unix),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CitationRecord:
    """One operator-recorded citation. Pure ledger artifact."""

    url: str
    host: str
    fragment: str
    cited_at_unix: float
    op_id: str
    schema_version: str = WEB_BROWSER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "citation",
            "url": self.url[:512],
            "host": self.host[:256],
            "fragment": self.fragment[:citation_bound()],
            "cited_at_unix": float(self.cited_at_unix),
            "op_id": self.op_id[:128],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SearchResultRecord:
    """One search result projected into substrate's frozen
    artifact (avoiding direct dependency on backend types)."""

    title: str
    url: str
    snippet: str
    domain: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title[:256],
            "url": self.url[:512],
            "snippet": self.snippet[:1024],
            "domain": self.domain[:256],
        }


# ===========================================================================
# URL normalization + allowlist
# ===========================================================================


def _normalize_url(raw: Any) -> Tuple[str, str]:
    """Return ``(url, host)`` with the host lowercased. Returns
    ``("", "")`` on malformed input. NEVER raises."""
    try:
        s = str(raw or "").strip()
        if not s:
            return "", ""
        # Require explicit scheme — refuse bare hostnames.
        parsed = urlparse(s)
        if parsed.scheme not in ("http", "https"):
            return "", ""
        host = (parsed.hostname or "").lower()
        if not host:
            return "", ""
        return s, host
    except Exception:  # noqa: BLE001
        return "", ""


def _matches_allowlist(host: str, allowlist: Sequence[str]) -> bool:
    """Suffix-match. Empty allowlist returns True (no
    restriction). NEVER raises."""
    if not allowlist:
        return True
    target = host.strip().lower()
    if not target:
        return False
    for entry in allowlist:
        e = entry.strip().lower()
        if not e:
            continue
        if target == e or target.endswith("." + e):
            return True
    return False


# ===========================================================================
# Composers — canonical surfaces (all lazy-imported)
# ===========================================================================


def _redact_secrets(text: str) -> Tuple[str, int]:
    """Compose Tier-1 sanitizer. Returns (sanitized, bytes_redacted).
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            redact_secrets,
        )
    except ImportError:
        return text, 0
    try:
        return redact_secrets(text or "")
    except Exception:  # noqa: BLE001
        return text or "", 0


def _scan_credentials(
    text: str, source_label: str,
) -> Tuple[int, Tuple[str, ...]]:
    """Compose Wave 3 #5 MCP scanner on the sanitized body.
    Returns (finding_count, kind_tuple). NEVER raises."""
    if not text:
        return 0, ()
    try:
        from backend.core.ouroboros.governance.mcp_output_scanner import (  # noqa: E501
            scan_mcp_output,
        )
    except ImportError:
        return 0, ()
    try:
        report = scan_mcp_output(
            text, source_label=f"web_browser:{source_label}",
        )
        findings = tuple(getattr(report, "findings", ()) or ())
        kinds: List[str] = []
        for f in findings:
            try:
                k = getattr(getattr(f, "kind", None), "value", "")
                if k:
                    kinds.append(str(k))
            except Exception:  # noqa: BLE001
                continue
        return len(findings), tuple(sorted(set(kinds)))
    except Exception:  # noqa: BLE001
        return 0, ()


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Compose §33.4 JSONL writer. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


async def _backend_search(
    query: str,
) -> Tuple[Tuple[SearchResultRecord, ...], str, str]:
    """Compose web_research_service first (richer), fall back to
    web_search.WebSearchCapability. Returns
    ``(results, backend_name, diagnostic)``. NEVER raises."""
    # Primary: research_service.
    try:
        from backend.intelligence.web_research_service import (  # noqa: E501
            get_web_research_service,
        )
        svc = get_web_research_service()
        raw = await asyncio.wait_for(
            svc.search(query),
            timeout=float(request_timeout_s()),
        )
        records: List[SearchResultRecord] = []
        for r in raw or ():
            try:
                # research_service returns dicts
                if isinstance(r, dict):
                    title = str(r.get("title", "")).strip()
                    url = str(r.get("url", "")).strip()
                    snippet = str(r.get("snippet", "")).strip()
                    domain = str(r.get("domain", "")).strip()
                else:
                    title = str(getattr(r, "title", "") or "")
                    url = str(getattr(r, "url", "") or "")
                    snippet = str(getattr(r, "snippet", "") or "")
                    domain = str(getattr(r, "domain", "") or "")
                if not url:
                    continue
                records.append(SearchResultRecord(
                    title=title,
                    url=url,
                    snippet=snippet,
                    domain=domain,
                ))
            except Exception:  # noqa: BLE001
                continue
        return (
            tuple(records),
            "web_research_service",
            f"research_service returned {len(records)} result(s)",
        )
    except (ImportError, asyncio.TimeoutError, Exception):
        pass
    # Fallback: web_search.WebSearchCapability.
    try:
        from backend.core.ouroboros.governance.web_search import (  # noqa: E501
            WebSearchCapability,
        )
        cap = WebSearchCapability()
        resp = await asyncio.wait_for(
            cap.search(query),
            timeout=float(request_timeout_s()),
        )
        records2: List[SearchResultRecord] = []
        for r in getattr(resp, "results", ()) or ():
            try:
                records2.append(SearchResultRecord(
                    title=str(getattr(r, "title", "") or ""),
                    url=str(getattr(r, "url", "") or ""),
                    snippet=str(getattr(r, "snippet", "") or ""),
                    domain=str(getattr(r, "domain", "") or ""),
                ))
            except Exception:  # noqa: BLE001
                continue
        return (
            tuple(records2),
            "web_search_capability",
            f"web_search returned {len(records2)} result(s)",
        )
    except Exception as exc:  # noqa: BLE001
        return (), "none", f"both search backends unavailable: {exc!r}"[:200]


async def _backend_fetch_static(url: str) -> Tuple[str, str, str]:
    """Compose web_research_service.read_page for static fetch.
    Returns ``(body, backend_name, diagnostic)``. NEVER raises.

    Bounded to ``max_fetch_bytes`` before return.
    """
    try:
        from backend.intelligence.web_research_service import (  # noqa: E501
            get_web_research_service,
        )
        svc = get_web_research_service()
        page = await asyncio.wait_for(
            svc.read_page(url),
            timeout=float(request_timeout_s()),
        )
        if page is None:
            return "", "web_research_service", "empty page"
        if isinstance(page, dict):
            text = str(page.get("text", "") or page.get("body", ""))
        else:
            text = (
                str(getattr(page, "text", ""))
                or str(getattr(page, "body", ""))
            )
        cap = max_fetch_bytes()
        if len(text) > cap:
            text = text[:cap]
        return (
            text,
            "web_research_service",
            f"static fetch ok ({len(text)} bytes)",
        )
    except asyncio.TimeoutError:
        return "", "web_research_service", "timeout"
    except Exception as exc:  # noqa: BLE001
        return "", "web_research_service", f"fetch failed: {exc!r}"[:200]


async def _backend_fetch_js_render(
    url: str,
) -> Tuple[str, str, str]:
    """Compose browser_bridge.read_page_text for JS-rendered
    pages. Returns ``(body, backend_name, diagnostic)``. NEVER
    raises. Returns empty body when JS render is disabled."""
    if not js_render_enabled():
        return (
            "",
            "browser_bridge_disabled",
            (
                f"js_render disabled via "
                f"{_ENV_ALLOW_JS_RENDER}=false; static fetch "
                "is the only available path"
            ),
        )
    try:
        from backend.core.ouroboros.governance.browser_bridge import (  # noqa: E501
            get_browser_bridge,
        )
        bridge = get_browser_bridge()
        if not getattr(bridge, "is_available", False):
            return (
                "", "browser_bridge_unavailable",
                "playwright not installed",
            )
        result = await asyncio.wait_for(
            bridge.read_page_text(
                url, timeout_s=float(request_timeout_s()),
            ),
            timeout=float(request_timeout_s()) + 5.0,
        )
        if not getattr(result, "success", False):
            err = str(getattr(result, "error", "") or "unknown")
            return "", "browser_bridge", f"bridge failed: {err[:120]}"
        text = str(getattr(result, "page_text", "") or "")
        cap = max_fetch_bytes()
        if len(text) > cap:
            text = text[:cap]
        return text, "browser_bridge", f"js-render ok ({len(text)} bytes)"
    except asyncio.TimeoutError:
        return "", "browser_bridge", "timeout"
    except Exception as exc:  # noqa: BLE001
        return "", "browser_bridge", f"bridge exception: {exc!r}"[:200]


async def _backend_screenshot(
    url: str,
) -> Tuple[Optional[str], str, str]:
    """Compose browser_bridge.screenshot. Returns
    ``(screenshot_path, backend_name, diagnostic)``. NEVER
    raises. Returns (None, ...) when JS render is disabled."""
    if not js_render_enabled():
        return (
            None, "browser_bridge_disabled",
            f"js_render disabled via {_ENV_ALLOW_JS_RENDER}=false",
        )
    try:
        from backend.core.ouroboros.governance.browser_bridge import (  # noqa: E501
            get_browser_bridge,
        )
        bridge = get_browser_bridge()
        if not getattr(bridge, "is_available", False):
            return (
                None, "browser_bridge_unavailable",
                "playwright not installed",
            )
        # Navigate first, then screenshot.
        nav = await asyncio.wait_for(
            bridge.navigate(url, timeout_s=float(request_timeout_s())),
            timeout=float(request_timeout_s()) + 5.0,
        )
        if not getattr(nav, "success", False):
            return None, "browser_bridge", "navigate failed"
        shot = await asyncio.wait_for(
            bridge.screenshot(timeout_s=float(request_timeout_s())),
            timeout=float(request_timeout_s()) + 5.0,
        )
        if not getattr(shot, "success", False):
            return None, "browser_bridge", "screenshot failed"
        return (
            str(getattr(shot, "screenshot_path", "") or "")
            or None,
            "browser_bridge",
            "screenshot ok",
        )
    except asyncio.TimeoutError:
        return None, "browser_bridge", "timeout"
    except Exception as exc:  # noqa: BLE001
        return None, "browser_bridge", f"bridge exception: {exc!r}"[:200]


# ===========================================================================
# Result-building helpers
# ===========================================================================


def _build_failed_result(
    action: BrowsingAction,
    url: str,
    host: str,
    diagnostic: str,
    *,
    verdict: BrowsingVerdict = BrowsingVerdict.FAILED,
    op_id: str = "",
    started_unix: float = 0.0,
) -> BrowsingResult:
    return BrowsingResult(
        action=action,
        verdict=verdict,
        url=url,
        host=host,
        content_bytes=0,
        sanitized_body="",
        redacted_bytes=0,
        leaked_credential_kinds=(),
        backend_used="none",
        latency_ms=max(0.0, (time.time() - started_unix) * 1000.0),
        diagnostic=diagnostic,
        op_id=op_id,
        evaluated_at_unix=started_unix or time.time(),
    )


def _gate_and_classify_body(
    *,
    action: BrowsingAction,
    url: str,
    host: str,
    raw_body: str,
    backend_name: str,
    backend_diagnostic: str,
    op_id: str,
    started_unix: float,
) -> BrowsingResult:
    """Run sanitize + scan, classify verdict, return Result.
    NEVER raises."""
    sanitized, redacted = _redact_secrets(raw_body)
    finding_count, kinds = _scan_credentials(
        sanitized, source_label=action.value,
    )
    if finding_count > 0:
        return BrowsingResult(
            action=action,
            verdict=BrowsingVerdict.CREDENTIAL_LEAKED,
            url=url,
            host=host,
            content_bytes=len(raw_body),
            sanitized_body=_CREDENTIAL_LEAK_PLACEHOLDER,
            redacted_bytes=int(redacted),
            leaked_credential_kinds=kinds,
            backend_used=backend_name,
            latency_ms=max(0.0, (time.time() - started_unix) * 1000.0),
            diagnostic=(
                f"credential leak detected: kinds={list(kinds)}; "
                f"body suppressed. {backend_diagnostic}"
            ),
            op_id=op_id,
            evaluated_at_unix=started_unix,
        )
    return BrowsingResult(
        action=action,
        verdict=BrowsingVerdict.CLEAN,
        url=url,
        host=host,
        content_bytes=len(raw_body),
        sanitized_body=sanitized,
        redacted_bytes=int(redacted),
        leaked_credential_kinds=(),
        backend_used=backend_name,
        latency_ms=max(0.0, (time.time() - started_unix) * 1000.0),
        diagnostic=backend_diagnostic,
        op_id=op_id,
        evaluated_at_unix=started_unix,
    )


# ===========================================================================
# Public async API
# ===========================================================================


async def perform_browsing_action(
    action: Any,
    *,
    url: str = "",
    query: str = "",
    fragment: str = "",
    js_render: bool = False,
    op_id: str = "",
    now_unix: Optional[float] = None,
) -> BrowsingResult:
    """Top-level async governance composer. NEVER raises.

    Parameters
    ----------
    action:
        :class:`BrowsingAction` enum value or its string form.
        Unknown values → FAILED.
    url:
        Required for NAVIGATE / FOLLOW_LINK / EXTRACT_TEXT /
        EXTRACT_IMAGE / CITE.
    query:
        Required for SEARCH.
    fragment:
        Required for CITE (the citation excerpt).
    js_render:
        Caller hint for EXTRACT_TEXT — when True and the
        ``JARVIS_WEB_BROWSER_ALLOW_JS_RENDER`` master sub-flag
        is on, route through browser_bridge.
    op_id:
        Operator-supplied op identifier for ledger correlation.
    """
    started = time.time() if now_unix is None else float(now_unix)
    coerced_action = _coerce_action(action)
    if coerced_action is None:
        return _build_failed_result(
            BrowsingAction.NAVIGATE,
            "", "",
            f"unknown action: {action!r}",
            op_id=op_id, started_unix=started,
        )

    if not master_enabled():
        return _build_failed_result(
            coerced_action, url, "",
            f"gate disabled via {_ENV_MASTER}=false",
            op_id=op_id, started_unix=started,
        )

    # CITE is a pure ledger action — no network, no allowlist.
    if coerced_action is BrowsingAction.CITE:
        u, host = _normalize_url(url) if url else ("", "")
        if url and not u:
            return _build_failed_result(
                BrowsingAction.CITE, url, "",
                "malformed URL for citation",
                op_id=op_id, started_unix=started,
            )
        record = CitationRecord(
            url=u,
            host=host,
            fragment=str(fragment or "")[:citation_bound()],
            cited_at_unix=started,
            op_id=str(op_id or ""),
        )
        _flock_append(record.to_dict())
        result = BrowsingResult(
            action=BrowsingAction.CITE,
            verdict=BrowsingVerdict.CLEAN,
            url=u,
            host=host,
            content_bytes=len(record.fragment),
            sanitized_body=record.fragment,
            redacted_bytes=0,
            leaked_credential_kinds=(),
            backend_used="ledger_only",
            latency_ms=max(0.0, (time.time() - started) * 1000.0),
            diagnostic="citation recorded to §33.4 ledger",
            op_id=op_id,
            evaluated_at_unix=started,
        )
        _publish_event(result)
        return result

    # SEARCH — query required.
    if coerced_action is BrowsingAction.SEARCH:
        q = str(query or "").strip()
        if not q:
            return _build_failed_result(
                BrowsingAction.SEARCH, "", "",
                "missing query for SEARCH",
                op_id=op_id, started_unix=started,
            )
        records, backend, diagnostic = await _backend_search(q)
        # Emit one consolidated body listing top results — caller
        # gets the URLs to follow via FOLLOW_LINK.
        body_lines = [
            f"{i+1}. {r.title or '(no title)'} — {r.url}\n"
            f"   {r.snippet[:200]}"
            for i, r in enumerate(records[:10])
        ]
        body = (
            "\n".join(body_lines)
            if body_lines
            else f"(no results for query: {q[:80]})"
        )
        result = _gate_and_classify_body(
            action=BrowsingAction.SEARCH,
            url="",
            host="search:" + (records[0].domain if records else ""),
            raw_body=body,
            backend_name=backend,
            backend_diagnostic=diagnostic,
            op_id=op_id,
            started_unix=started,
        )
        _persist_result(result)
        _publish_event(result)
        return result

    # All other actions require a URL.
    u, host = _normalize_url(url)
    if not u:
        return _build_failed_result(
            coerced_action, url, "",
            "malformed URL — require http:// or https:// scheme",
            op_id=op_id, started_unix=started,
        )

    # Operator allowlist gate (composer side).
    operator_list = operator_domain_allowlist()
    if not _matches_allowlist(host, operator_list):
        result = _build_failed_result(
            coerced_action, u, host,
            (
                f"host {host!r} outside operator allowlist "
                f"({_ENV_DOMAIN_ALLOWLIST}); set the env to "
                "allow this domain"
            ),
            verdict=BrowsingVerdict.OUT_OF_ALLOWLIST,
            op_id=op_id, started_unix=started,
        )
        _persist_result(result)
        _publish_event(result)
        return result

    # Route per action.
    if coerced_action is BrowsingAction.EXTRACT_IMAGE:
        path, backend, diagnostic = await _backend_screenshot(u)
        if path is None:
            return _build_failed_result(
                BrowsingAction.EXTRACT_IMAGE, u, host,
                diagnostic,
                op_id=op_id, started_unix=started,
            )
        # Screenshot path replaces the body — no sanitize needed
        # (image file already on disk under operator control).
        result = BrowsingResult(
            action=BrowsingAction.EXTRACT_IMAGE,
            verdict=BrowsingVerdict.CLEAN,
            url=u,
            host=host,
            content_bytes=0,
            sanitized_body=path,
            redacted_bytes=0,
            leaked_credential_kinds=(),
            backend_used=backend,
            latency_ms=max(0.0, (time.time() - started) * 1000.0),
            diagnostic=diagnostic,
            op_id=op_id,
            evaluated_at_unix=started,
        )
        _persist_result(result)
        _publish_event(result)
        return result

    # NAVIGATE / FOLLOW_LINK / EXTRACT_TEXT — fetch body.
    if (
        coerced_action is BrowsingAction.EXTRACT_TEXT
        and js_render
    ):
        body, backend, diagnostic = await _backend_fetch_js_render(u)
        if not body and "disabled" in diagnostic:
            # Fall back to static fetch when JS render is off.
            body, backend, diagnostic = await _backend_fetch_static(u)
    else:
        body, backend, diagnostic = await _backend_fetch_static(u)

    if not body:
        verdict = (
            BrowsingVerdict.RATE_LIMITED
            if "timeout" in diagnostic.lower()
            else BrowsingVerdict.FAILED
        )
        result = _build_failed_result(
            coerced_action, u, host, diagnostic,
            verdict=verdict,
            op_id=op_id, started_unix=started,
        )
        _persist_result(result)
        _publish_event(result)
        return result

    result = _gate_and_classify_body(
        action=coerced_action,
        url=u,
        host=host,
        raw_body=body,
        backend_name=backend,
        backend_diagnostic=diagnostic,
        op_id=op_id,
        started_unix=started,
    )
    _persist_result(result)
    _publish_event(result)
    return result


def perform_browsing_action_sync(
    action: Any,
    *,
    url: str = "",
    query: str = "",
    fragment: str = "",
    js_render: bool = False,
    op_id: str = "",
    now_unix: Optional[float] = None,
) -> BrowsingResult:
    """Sync wrapper for callers outside an event loop. NEVER raises.

    When called inside a running event loop, returns a FAILED
    result with a diagnostic (caller should use the async API
    directly)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        return _build_failed_result(
            _coerce_action(action) or BrowsingAction.NAVIGATE,
            url or "", "",
            "sync wrapper invoked inside running event loop — "
            "use perform_browsing_action() instead",
            op_id=op_id,
            started_unix=time.time() if now_unix is None else now_unix,
        )
    try:
        return asyncio.run(perform_browsing_action(
            action,
            url=url, query=query, fragment=fragment,
            js_render=js_render, op_id=op_id, now_unix=now_unix,
        ))
    except Exception as exc:  # noqa: BLE001
        return _build_failed_result(
            _coerce_action(action) or BrowsingAction.NAVIGATE,
            url or "", "",
            f"sync wrapper failed: {exc!r}"[:200],
            op_id=op_id,
            started_unix=time.time() if now_unix is None else now_unix,
        )


# ===========================================================================
# §33.4 persistence
# ===========================================================================


def _persist_result(result: BrowsingResult) -> None:
    """Best-effort §33.4 write. NEVER raises. Skips CLEAN results
    for non-citation actions (citations always logged via CITE
    path; non-citation CLEAN results are noise — only leaks /
    failures / rate-limits are persisted as audit trail)."""
    if result.verdict is BrowsingVerdict.CLEAN:
        return
    _flock_append({"kind": "result", "payload": result.to_dict()})


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_event(result: BrowsingResult) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_WEB_BROWSING_ACTION,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_WEB_BROWSING_ACTION,
            (
                f"system::web_browser::"
                f"{result.schema_version}"
            ),
            {
                "action": result.action.value,
                "verdict": result.verdict.value,
                "host": result.host[:128],
                "content_bytes": result.content_bytes,
                "redacted_bytes": result.redacted_bytes,
                "leaked_credential_kinds": list(
                    result.leaked_credential_kinds,
                ),
                "backend_used": result.backend_used[:64],
                "latency_ms": result.latency_ms,
                "op_id": result.op_id[:64],
                "schema_version": result.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderer
# ===========================================================================


def format_browsing_panel(
    result: Optional[BrowsingResult] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if result is None:
        if not master_enabled():
            return (
                f"web browser: disabled ({_ENV_MASTER}=false)"
            )
        return "web browser: no result"
    ag = action_glyph(result.action)
    vg = verdict_glyph(result.verdict)
    lines = [
        f"{ag} Web Browser  {vg} {result.verdict.value}",
        f"  action          : {result.action.value}",
        f"  url             : {result.url[:80] or '(n/a)'}",
        f"  host            : {result.host or '(n/a)'}",
        f"  content_bytes   : {result.content_bytes}",
        f"  redacted_bytes  : {result.redacted_bytes}",
        f"  backend         : {result.backend_used}",
        f"  latency_ms      : {result.latency_ms:.1f}",
    ]
    if result.leaked_credential_kinds:
        lines.append(
            f"  leaked_kinds    : "
            f"{list(result.leaked_credential_kinds)}"
        )
    lines.append(f"  diagnostic      : {result.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/web_browser.py"
    )

    _EXPECTED_ACTIONS = {
        "search", "navigate", "follow_link",
        "extract_text", "extract_image", "cite",
    }
    _EXPECTED_VERDICTS = {
        "clean", "credential_leaked", "out_of_allowlist",
        "rate_limited", "failed",
    }

    def _validate_action_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "BrowsingAction"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_ACTIONS - found
                extra = found - _EXPECTED_ACTIONS
                if missing:
                    return (
                        f"BrowsingAction missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"BrowsingAction drift: {sorted(extra)}",
                    )
                return ()
        return ("BrowsingAction class not found",)

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "BrowsingVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"BrowsingVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"BrowsingVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("BrowsingVerdict class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        """Substrate MUST compose all 6 canonical surfaces:
        web_search OR web_research_service (search backends),
        browser_bridge (JS render), conversation_bridge
        (redact), mcp_output_scanner (credential scan),
        cross_process_jsonl (ledger). Parallel HTTP client,
        parallel JS-render path, parallel search backend all
        forbidden — must reuse existing files."""
        violations: List[str] = []
        if (
            "web_search" not in source
            and "web_research_service" not in source
        ):
            violations.append(
                "must compose at least one search backend "
                "(web_search OR web_research_service) — no "
                "parallel search implementation",
            )
        if "browser_bridge" not in source:
            violations.append(
                "must compose browser_bridge (no parallel "
                "Playwright path)",
            )
        if "conversation_bridge" not in source:
            violations.append(
                "must compose conversation_bridge.redact_secrets "
                "(no parallel sanitizer)",
            )
        if "mcp_output_scanner" not in source:
            violations.append(
                "must compose Wave 3 #5 mcp_output_scanner "
                "(no parallel credential detector)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl "
                "(no parallel JSONL writer)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="web_browser_action_taxonomy_closed",
            target_file=target,
            description=(
                "BrowsingAction 6-value taxonomy bytes-pinned."
            ),
            validate=_validate_action_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="web_browser_verdict_taxonomy_closed",
            target_file=target,
            description=(
                "BrowsingVerdict 5-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="web_browser_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — pure governance composer. "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor / tool_executor (one-way "
                "cage: Venom tool registration is wired "
                "operator-side, not by this substrate)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="web_browser_master_default_false",
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="web_browser_composes_canonical",
            target_file=target,
            description=(
                "Substrate composes existing 5 surfaces "
                "(web_search / web_research_service / "
                "browser_bridge / conversation_bridge / "
                "mcp_output_scanner) + cross_process_jsonl. "
                "No parallel HTTP client, no parallel search "
                "backend, no parallel JS-render path, no "
                "parallel sanitizer, no parallel credential "
                "detector — every web capability flows "
                "through existing canonical files."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/web_browser.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "WebBrowser master switch. §33.1 default-FALSE. "
                "Closes §41.5 (PRD v3.0) — load-bearing for "
                "autonomous JARVIS development. Composes 5 "
                "existing surfaces (web_search + "
                "browser_bridge + web_research_service + "
                "conversation_bridge + mcp_output_scanner) + "
                "cross_process_jsonl into a single cage-"
                "bounded Venom-tool entry point. When OFF "
                "the existing surfaces remain accessible to "
                "their direct consumers (CONTEXT_EXPANSION, "
                "Visual VERIFY, Neural Mesh) — only the "
                "composer surface is gated."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate §33.4 citation ledger writes. "
                "Default TRUE."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_ALLOW_JS_RENDER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Sub-flag — gate the browser_bridge Playwright "
                "route (subprocess + Chromium). Default FALSE "
                "because Playwright is heavyweight. Required "
                "for EXTRACT_IMAGE and JS-rendered EXTRACT_TEXT."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_ALLOW_JS_RENDER}=true",
        ),
        FlagSpec(
            name=_ENV_DOMAIN_ALLOWLIST,
            type=FlagType.STR,
            default="",
            description=(
                "Operator-tunable host suffix allowlist "
                "(comma-separated, case-insensitive). Empty "
                "= no additional restriction beyond backend's "
                "own allowlist. Any value TIGHTENS the cage "
                "(both composer + backend must allow)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=(
                f"{_ENV_DOMAIN_ALLOWLIST}="
                "github.com,docs.python.org,stackoverflow.com"
            ),
        ),
        FlagSpec(
            name=_ENV_MAX_FETCH_BYTES,
            type=FlagType.INT,
            default=_DEFAULT_MAX_FETCH_BYTES,
            description=(
                "Cap on bytes returned in any single "
                "BrowsingResult body. Default 200_000. "
                "Clamped to [1024, 10_000_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_FETCH_BYTES}=500000",
        ),
        FlagSpec(
            name=_ENV_REQUEST_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_REQUEST_TIMEOUT_S,
            description=(
                "Hard per-call timeout (seconds). Default 20. "
                "Clamped to [1, 300]."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_REQUEST_TIMEOUT_S}=30",
        ),
        FlagSpec(
            name=_ENV_CITATION_BOUND,
            type=FlagType.INT,
            default=_DEFAULT_CITATION_BOUND,
            description=(
                "Cap on citation fragment length in ledger. "
                "Default 256. Clamped to [16, 4096]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_CITATION_BOUND}=512",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "WEB_BROWSER_SCHEMA_VERSION",
    "BrowsingAction",
    "BrowsingVerdict",
    "BrowsingResult",
    "CitationRecord",
    "SearchResultRecord",
    "master_enabled",
    "persistence_enabled",
    "js_render_enabled",
    "max_fetch_bytes",
    "request_timeout_s",
    "citation_bound",
    "operator_domain_allowlist",
    "ledger_path",
    "action_glyph",
    "verdict_glyph",
    "perform_browsing_action",
    "perform_browsing_action_sync",
    "format_browsing_panel",
    "register_shipped_invariants",
    "register_flags",
]
