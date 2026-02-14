"""
JARVIS BrowsingAgent — Structured Web Automation

Architecture:
- SearchHandler: Wraps existing WebSearchExtractor (DuckDuckGo/Brave/Bing/Google/SearXNG)
- BrowsingEngine: Headless Playwright for navigation/extraction (CDP opt-in)
- ContentExtractor: DOM content extraction via Playwright
- FormHandler: Programmatic form interaction via Playwright
- ContentIntelligence: Optional J-Prime summarization
- BrowsingTelemetry: Events to Reactor-Core

v6.4: Initial implementation with all 6 design corrections.
"""

from __future__ import annotations

import atexit
import asyncio
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Environment helpers (match codebase convention)
# =============================================================================

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


# =============================================================================
# BrowsingEngine — Playwright lifecycle (headless default, CDP opt-in)
# =============================================================================

class BrowsingEngine:
    """Playwright browser lifecycle manager.

    Default: launches headless Chromium (invisible, no user disruption).
    Opt-in: connect to user's Chrome via CDP (BROWSE_USE_CDP=true) for
    authenticated sites that need the user's cookies.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._default_context = None
        self._pages: Dict[str, Any] = {}  # tab_id -> Page
        self._page_access_times: Dict[str, float] = {}  # tab_id -> last access time
        self._lock: Optional[asyncio.Lock] = None
        self._initialized = False

        # Config (all env-var driven)
        self._nav_timeout = _env_int("BROWSE_NAV_TIMEOUT_MS", 15000)
        self._action_timeout = _env_int("BROWSE_ACTION_TIMEOUT_MS", 5000)
        self._max_pages = _env_int("BROWSE_MAX_PAGES", 10)
        self._use_cdp = _env_bool("BROWSE_USE_CDP", False)
        self._user_agent = os.getenv("BROWSE_USER_AGENT", None)

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def initialize(self) -> bool:
        """Start Playwright. Returns False if not installed."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.debug("[BROWSE-ENGINE] Playwright not installed")
            return False

        try:
            self._playwright = await async_playwright().start()

            if self._use_cdp:
                connected = await self._connect_cdp()
                if connected:
                    self._initialized = True
                    return True
                logger.info("[BROWSE-ENGINE] CDP connection failed, falling back to headless")

            # Default: launch headless Chromium
            pw = self._playwright
            assert pw is not None  # guaranteed by try/except above
            self._browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ],
            )

            self._default_context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=self._user_agent,
                locale=os.getenv("BROWSE_LOCALE", "en-US"),
            )
            self._default_context.set_default_timeout(self._action_timeout)
            self._default_context.set_default_navigation_timeout(self._nav_timeout)

            self._initialized = True
            logger.info("[BROWSE-ENGINE] Initialized (headless Chromium)")
            return True

        except Exception as e:
            logger.warning(f"[BROWSE-ENGINE] Failed to initialize Playwright: {e}")
            await self._cleanup()
            return False

    async def _connect_cdp(self) -> bool:
        """Try to connect to user's Chrome via CDP."""
        cdp_port = None

        # Try env var first
        env_port = os.getenv("BROWSE_CDP_PORT")
        if env_port:
            try:
                cdp_port = int(env_port)
            except ValueError:
                pass

        # Try BrowserStabilityManager
        if cdp_port is None:
            try:
                from core.browser_stability import get_active_cdp_port
                cdp_port = get_active_cdp_port()
            except (ImportError, Exception) as e:
                logger.debug(f"[BROWSE-ENGINE] get_active_cdp_port() failed: {e}")

        if cdp_port is None:
            return False

        try:
            cdp_url = f"http://localhost:{cdp_port}"
            pw = self._playwright
            if pw is None:
                return False
            self._browser = await pw.chromium.connect_over_cdp(
                cdp_url, timeout=10000,
            )
            contexts = self._browser.contexts
            if contexts:
                self._default_context = contexts[0]
            else:
                self._default_context = await self._browser.new_context()

            self._default_context.set_default_timeout(self._action_timeout)
            self._default_context.set_default_navigation_timeout(self._nav_timeout)

            logger.info(f"[BROWSE-ENGINE] Connected via CDP on port {cdp_port}")
            return True

        except Exception as e:
            logger.warning(f"[BROWSE-ENGINE] CDP connection failed on port {cdp_port}: {e}")
            return False

    async def new_page(self, url: Optional[str] = None, tab_id: Optional[str] = None) -> Optional[Any]:
        """Open a new page. Enforces max_pages limit with LRU eviction."""
        if not self._initialized or not self._default_context:
            return None

        async with self._get_lock():
            # Enforce max pages — evict least-recently-used
            if len(self._pages) >= self._max_pages:
                lru_id = min(
                    self._page_access_times,
                    key=self._page_access_times.get,  # type: ignore[arg-type]
                    default=next(iter(self._pages), None),
                )
                if lru_id:
                    await self._close_page_unlocked(lru_id)
                    logger.debug(f"[BROWSE-ENGINE] Evicted LRU page {lru_id}")

            tab_id = tab_id or str(uuid.uuid4())[:8]
            try:
                page = await self._default_context.new_page()
                if url:
                    await page.goto(url, wait_until="domcontentloaded")
                self._pages[tab_id] = page
                self._page_access_times[tab_id] = time.monotonic()
                return page
            except Exception as e:
                logger.warning(f"[BROWSE-ENGINE] Failed to create page: {e}")
                return None

    async def navigate(
        self,
        url: str,
        tab_id: Optional[str] = None,
        wait_until: str = "domcontentloaded",
    ) -> Dict[str, Any]:
        """Navigate to URL. Returns {url, title, success}."""
        if not self._initialized:
            return {"success": False, "error": "Playwright not initialized"}

        try:
            page = self._pages.get(tab_id) if tab_id else None
            if page is None:
                page = await self.new_page()
                if page is None:
                    return {"success": False, "error": "Failed to create page"}

            response = await page.goto(url, wait_until=wait_until)
            title = await page.title()

            return {
                "success": True,
                "url": page.url,
                "title": title,
                "status_code": response.status if response else None,
            }
        except Exception as e:
            logger.warning(f"[BROWSE-ENGINE] Navigation failed for {url}: {e}")
            return {"success": False, "error": str(e), "url": url}

    async def get_page(self, tab_id: Optional[str] = None) -> Optional[Any]:
        """Get page by tab_id, or the most recent page. Updates LRU timestamp."""
        if tab_id and tab_id in self._pages:
            self._page_access_times[tab_id] = time.monotonic()
            return self._pages[tab_id]
        if self._pages:
            last_id = next(reversed(self._pages))
            self._page_access_times[last_id] = time.monotonic()
            return self._pages[last_id]
        return None

    async def close_page(self, tab_id: str) -> None:
        """Close a tab."""
        async with self._get_lock():
            await self._close_page_unlocked(tab_id)

    async def _close_page_unlocked(self, tab_id: str) -> None:
        page = self._pages.pop(tab_id, None)
        self._page_access_times.pop(tab_id, None)
        if page:
            try:
                await page.close()
            except Exception:
                pass

    async def shutdown(self) -> None:
        """Close all pages and disconnect (don't close browser if CDP)."""
        await self._cleanup()

    async def _cleanup(self) -> None:
        for tab_id in list(self._pages):
            try:
                page = self._pages.pop(tab_id, None)
                if page:
                    await page.close()
            except Exception:
                pass

        if self._default_context and not self._use_cdp:
            try:
                await self._default_context.close()
            except Exception:
                pass

        if self._browser and not self._use_cdp:
            try:
                await self._browser.close()
            except Exception:
                pass

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

        self._playwright = None
        self._browser = None
        self._default_context = None
        self._initialized = False


# =============================================================================
# SearchHandler — wraps existing WebSearchExtractor
# =============================================================================

class SearchHandler:
    """Structured search via existing WebSearchExtractor.

    Reuses ouroboros/native_integration.py infrastructure:
    - DuckDuckGo (free, no API key — default)
    - Brave, Bing, Google, SearXNG (API keys via env vars)
    - Built-in rate limiting, circuit breakers, caching

    Uses exponential backoff for transient init failures (never permanently disabled).
    """

    _BACKOFF_BASE = _env_float("BROWSE_SEARCH_BACKOFF_BASE", 5.0)
    _BACKOFF_CAP = _env_float("BROWSE_SEARCH_BACKOFF_CAP", 300.0)  # 5 min max

    def __init__(self):
        self._extractor = None
        self._init_failures: int = 0
        self._next_retry_at: float = 0.0  # monotonic time
        self._import_missing: bool = False  # permanent: module doesn't exist

    async def _get_extractor(self):
        if self._extractor is not None:
            return self._extractor
        # ImportError is permanent — no point retrying
        if self._import_missing:
            return None
        # Exponential backoff for transient failures
        if self._init_failures > 0 and time.monotonic() < self._next_retry_at:
            return None

        try:
            from core.ouroboros.native_integration import get_web_search_extractor
            self._extractor = get_web_search_extractor()
            await self._extractor.initialize()
            self._init_failures = 0  # reset on success
            logger.debug("[BROWSE-SEARCH] WebSearchExtractor initialized")
            return self._extractor
        except ImportError:
            self._import_missing = True
            logger.debug("[BROWSE-SEARCH] ouroboros.native_integration not available")
            return None
        except Exception as e:
            self._init_failures += 1
            backoff = min(
                self._BACKOFF_BASE * (2 ** (self._init_failures - 1)),
                self._BACKOFF_CAP,
            )
            self._next_retry_at = time.monotonic() + backoff
            logger.warning(
                f"[BROWSE-SEARCH] Init failed (attempt {self._init_failures}, "
                f"retry in {backoff:.0f}s): {e}"
            )
            return None

    async def search(self, query: str, max_results: int = 5) -> Dict[str, Any]:
        """Search via API providers. Returns structured results dict."""
        extractor = await self._get_extractor()
        if not extractor:
            return {"success": False, "error": "WebSearchExtractor unavailable", "results": []}

        try:
            results = await extractor.search(query, max_results=max_results)

            if not results:
                return {"success": True, "results": [], "provider": "none"}

            return {
                "success": True,
                "results": [
                    {
                        "title": r.title,
                        "url": r.url,
                        "snippet": r.snippet,
                        "provider": r.provider.value,
                        "type": r.result_type.value,
                        "relevance": r.relevance_score,
                    }
                    for r in results
                ],
                "provider": results[0].provider.value,
                "cached": results[0].cached,
                "count": len(results),
            }
        except Exception as e:
            logger.warning(f"[BROWSE-SEARCH] Search failed for '{query[:50]}': {e}")
            return {"success": False, "error": str(e), "results": []}


# =============================================================================
# ContentExtractor — page content via Playwright
# =============================================================================

class ContentExtractor:
    """Extract content from web pages via Playwright."""

    async def get_text(self, page, selector: str = "body") -> str:
        """Get text content of an element."""
        try:
            element = await page.query_selector(selector)
            if element:
                return await element.text_content() or ""
            return ""
        except Exception as e:
            logger.debug(f"[BROWSE-EXTRACT] get_text failed: {e}")
            return ""

    async def get_page_content(self, page, clean: bool = True) -> Dict[str, Any]:
        """Full page content extraction."""
        try:
            title = await page.title()
            url = page.url

            if clean:
                # Extract main content via JS readability-like heuristic
                content = await page.evaluate("""() => {
                    // Try common content selectors
                    const selectors = [
                        'article', 'main', '[role="main"]',
                        '.content', '.post-content', '.article-body',
                        '#content', '#main-content',
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.textContent.trim().length > 100) {
                            return el.textContent.trim();
                        }
                    }
                    // Fallback: body text minus nav/footer/header
                    const body = document.body.cloneNode(true);
                    ['nav', 'header', 'footer', 'aside', 'script', 'style', 'noscript'].forEach(tag => {
                        body.querySelectorAll(tag).forEach(el => el.remove());
                    });
                    return body.textContent.trim().substring(0, 10000);
                }""")
            else:
                content = await page.evaluate("() => document.body.textContent.trim()")

            return {
                "success": True,
                "title": title,
                "url": url,
                "content": content[:10000] if content else "",
                "length": len(content) if content else 0,
            }
        except Exception as e:
            logger.warning(f"[BROWSE-EXTRACT] get_page_content failed: {e}")
            return {"success": False, "error": str(e)}

    async def query_elements(self, page, selector: str) -> List[Dict[str, Any]]:
        """Query elements by CSS selector, return structured info."""
        try:
            elements = await page.query_selector_all(selector)
            results = []
            for el in elements[:50]:  # Cap to prevent OOM
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                text = (await el.text_content() or "").strip()[:200]
                href = await el.get_attribute("href")
                results.append({
                    "tag": tag,
                    "text": text,
                    "href": href,
                })
            return results
        except Exception as e:
            logger.debug(f"[BROWSE-EXTRACT] query_elements failed: {e}")
            return []

    async def get_structured_data(self, page) -> Dict[str, Any]:
        """Extract JSON-LD, OpenGraph, meta tags."""
        try:
            data = await page.evaluate("""() => {
                const result = {jsonLd: [], openGraph: {}, meta: {}};

                // JSON-LD
                document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                    try { result.jsonLd.push(JSON.parse(s.textContent)); } catch(e) {}
                });

                // OpenGraph
                document.querySelectorAll('meta[property^="og:"]').forEach(m => {
                    result.openGraph[m.getAttribute('property')] = m.getAttribute('content');
                });

                // Standard meta
                document.querySelectorAll('meta[name]').forEach(m => {
                    const name = m.getAttribute('name');
                    if (['description', 'author', 'keywords', 'robots'].includes(name)) {
                        result.meta[name] = m.getAttribute('content');
                    }
                });

                return result;
            }""")
            return {"success": True, **data}
        except Exception as e:
            logger.debug(f"[BROWSE-EXTRACT] get_structured_data failed: {e}")
            return {"success": False, "error": str(e)}


# =============================================================================
# FormHandler — programmatic form interaction via Playwright
# =============================================================================

class FormHandler:
    """Fill and submit web forms via Playwright."""

    async def fill_field(self, page, selector: str, value: str) -> bool:
        try:
            await page.fill(selector, value)
            return True
        except Exception as e:
            logger.debug(f"[BROWSE-FORM] fill_field failed: {e}")
            return False

    async def select_option(self, page, selector: str, value: str) -> bool:
        try:
            await page.select_option(selector, value)
            return True
        except Exception as e:
            logger.debug(f"[BROWSE-FORM] select_option failed: {e}")
            return False

    async def click_element(self, page, selector: str) -> bool:
        try:
            await page.click(selector)
            return True
        except Exception as e:
            logger.debug(f"[BROWSE-FORM] click_element failed: {e}")
            return False

    async def submit_form(self, page, selector: str = "form") -> bool:
        try:
            # Use safe Playwright API — never interpolate user selectors into JS
            element = await page.query_selector(selector)
            if element is None:
                logger.debug(f"[BROWSE-FORM] submit_form: no element for '{selector}'")
                return False
            await element.evaluate("el => el.submit()")
            return True
        except Exception as e:
            logger.debug(f"[BROWSE-FORM] submit_form failed: {e}")
            return False


# =============================================================================
# ContentIntelligence — optional J-Prime summarization
# =============================================================================

class ContentIntelligence:
    """Optional LLM-powered content analysis via JARVIS-Prime.

    Caches the httpx client to reuse TCP connections across requests.
    """

    def __init__(self):
        self._prime_url = os.getenv("JPRIME_URL", "http://localhost:8002")
        self._timeout = _env_float("BROWSE_INTELLIGENCE_TIMEOUT", 30.0)
        self._client = None  # Lazy httpx.AsyncClient

    async def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import httpx
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                base_url=self._prime_url,
            )
            return self._client
        except ImportError:
            return None

    async def _chat(self, system_msg: str, user_msg: str, max_tokens: int) -> Optional[str]:
        """Send a chat completion request to J-Prime."""
        client = await self._get_client()
        if not client:
            return None
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "local",
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": max_tokens,
                },
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content")
        except Exception as e:
            logger.debug(f"[BROWSE-INTEL] J-Prime request failed: {e}")
        return None

    async def summarize(self, content: str, query: Optional[str] = None) -> Optional[str]:
        """Summarize page content via J-Prime. Returns None if unavailable."""
        prompt = "Summarize the following web page content"
        if query:
            prompt += f" in the context of the query: '{query}'"
        prompt += f":\n\n{content[:5000]}"
        return await self._chat(
            "You are a helpful assistant that summarizes web content concisely.",
            prompt,
            500,
        )

    async def extract_answer(self, content: str, question: str) -> Optional[str]:
        """Extract specific answer from content. Returns None if unavailable."""
        return await self._chat(
            "Extract the answer to the question from the provided content. Be concise.",
            f"Question: {question}\n\nContent:\n{content[:5000]}",
            300,
        )

    async def close(self) -> None:
        """Close the cached httpx client."""
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


# =============================================================================
# BrowsingTelemetry — events to Reactor-Core
# =============================================================================

class BrowsingTelemetry:
    """Emit browsing events to Reactor-Core for learning. Optional, never blocks.

    Uses exponential backoff — never permanently disabled by transient failures.
    """

    def __init__(self):
        self._emitter = None
        self._init_failures: int = 0
        self._next_retry_at: float = 0.0
        self._import_missing: bool = False

    async def _get_emitter(self):
        if self._emitter is not None:
            return self._emitter
        if self._import_missing:
            return None
        if self._init_failures > 0 and time.monotonic() < self._next_retry_at:
            return None
        try:
            from core.telemetry_emitter import get_telemetry_emitter
            self._emitter = await get_telemetry_emitter()
            self._init_failures = 0
            return self._emitter
        except ImportError:
            self._import_missing = True
            return None
        except Exception:
            self._init_failures += 1
            backoff = min(5.0 * (2 ** (self._init_failures - 1)), 300.0)
            self._next_retry_at = time.monotonic() + backoff
            return None

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        try:
            emitter = await self._get_emitter()
            if emitter:
                await emitter.emit_interaction(
                    user_input=data.get("query", ""),
                    response=f"browsing:{event_type}",
                    success=data.get("success", True),
                    latency_ms=data.get("elapsed_ms", 0.0),
                    source="browsing_agent",
                    metadata={"event_type": event_type, **data},
                    task_type="browsing",
                )
        except Exception:
            pass  # Telemetry must never block


# =============================================================================
# BrowsingAgent — Neural Mesh agent
# =============================================================================

class BrowsingAgent:
    """Structured web browsing agent for JARVIS.

    Search via WebSearchExtractor API (always available).
    Navigate/extract/form via Playwright (optional).
    Summarize via J-Prime (optional).

    This class implements the BaseNeuralMeshAgent interface when registered
    via the adapter, but can also be used standalone via get_browsing_agent().
    """

    def __init__(self):
        self.agent_name = "browsing_agent"
        self.agent_type = "browsing"
        self.capabilities = {"navigate", "search", "extract", "fill_form", "multi_tab", "summarize"}
        self.description = "Structured web browsing via API search + Playwright"

        self._search = SearchHandler()
        self._engine = BrowsingEngine()
        self._extractor = ContentExtractor()
        self._form = FormHandler()
        self._intelligence = ContentIntelligence()
        self._telemetry = BrowsingTelemetry()

        self._playwright_available = False
        self._initialized = False

        self._task_handlers: Dict[str, Callable] = {}

    async def on_initialize(self) -> None:
        """Initialize search (always) + Playwright engine (best-effort)."""
        self._playwright_available = await self._engine.initialize()

        if not self._playwright_available:
            logger.info(
                "[BROWSE] Playwright not available — search works via API, "
                "but navigate/extract/form actions are disabled. "
                "Install: pip install playwright && python -m playwright install chromium"
            )

        self._task_handlers = {
            "search": self._handle_search,
            "navigate": self._handle_navigate,
            "extract": self._handle_extract,
            "get_page_content": self._handle_get_content,
            "fill_form": self._handle_fill_form,
            "summarize_page": self._handle_summarize,
            "close_tab": self._handle_close_tab,
            "query_elements": self._handle_query_elements,
        }

        self._initialized = True

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """Dispatch to appropriate handler based on action."""
        if not self._initialized:
            await self.on_initialize()

        action = payload.get("action", "search")
        handler = self._task_handlers.get(action)
        if not handler:
            return {"success": False, "error": f"Unknown action: {action}"}

        start = time.monotonic()
        try:
            result = await handler(payload)
            elapsed = time.monotonic() - start
            await self._telemetry.emit(action, {
                "query": payload.get("query", payload.get("url", "")),
                "success": result.get("success", False),
                "elapsed_ms": round(elapsed * 1000, 1),
            })
            return result
        except Exception as e:
            elapsed = time.monotonic() - start
            await self._telemetry.emit("error", {
                "action": action,
                "error": str(e),
                "elapsed_ms": round(elapsed * 1000, 1),
            })
            return {"success": False, "error": str(e)}

    @property
    def available(self) -> bool:
        """Agent is always available (search has no hard deps)."""
        return True

    # -------------------------------------------------------------------------
    # Task handlers
    # -------------------------------------------------------------------------

    async def _handle_search(self, payload: Dict) -> Dict:
        """Search via WebSearchExtractor API."""
        query = payload.get("query", "")
        if not query:
            return {"success": False, "error": "No query provided"}

        max_results = payload.get("max_results", 5)
        return await self._search.search(query, max_results=max_results)

    async def _handle_navigate(self, payload: Dict) -> Dict:
        """Navigate to URL via Playwright."""
        if not self._playwright_available:
            return {"success": False, "error": "Playwright not available for navigation"}

        url = payload.get("url", "")
        if not url:
            return {"success": False, "error": "No URL provided"}

        # Normalize URL
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        tab_id = payload.get("tab_id")
        wait_until = payload.get("wait_until", "domcontentloaded")
        return await self._engine.navigate(url, tab_id=tab_id, wait_until=wait_until)

    async def _handle_extract(self, payload: Dict) -> Dict:
        """Extract text from current page by CSS selector."""
        if not self._playwright_available:
            return {"success": False, "error": "Playwright not available for extraction"}

        page = await self._engine.get_page(payload.get("tab_id"))
        if not page:
            return {"success": False, "error": "No active page"}

        selector = payload.get("selector", "body")
        text = await self._extractor.get_text(page, selector)
        return {"success": True, "text": text, "selector": selector}

    async def _handle_get_content(self, payload: Dict) -> Dict:
        """Get full page content."""
        if not self._playwright_available:
            return {"success": False, "error": "Playwright not available"}

        page = await self._engine.get_page(payload.get("tab_id"))
        if not page:
            return {"success": False, "error": "No active page"}

        clean = payload.get("clean", True)
        return await self._extractor.get_page_content(page, clean=clean)

    async def _handle_fill_form(self, payload: Dict) -> Dict:
        """Fill a form field."""
        if not self._playwright_available:
            return {"success": False, "error": "Playwright not available for forms"}

        page = await self._engine.get_page(payload.get("tab_id"))
        if not page:
            return {"success": False, "error": "No active page"}

        form_action = payload.get("form_action", "fill")
        selector = payload.get("selector", "")
        value = payload.get("value", "")

        if form_action == "fill":
            ok = await self._form.fill_field(page, selector, value)
        elif form_action == "select":
            ok = await self._form.select_option(page, selector, value)
        elif form_action == "click":
            ok = await self._form.click_element(page, selector)
        elif form_action == "submit":
            ok = await self._form.submit_form(page, selector or "form")
        else:
            return {"success": False, "error": f"Unknown form_action: {form_action}"}

        return {"success": ok, "form_action": form_action, "selector": selector}

    async def _handle_summarize(self, payload: Dict) -> Dict:
        """Summarize current page content via J-Prime."""
        if not self._playwright_available:
            return {"success": False, "error": "Playwright not available"}

        page = await self._engine.get_page(payload.get("tab_id"))
        if not page:
            return {"success": False, "error": "No active page"}

        content_result = await self._extractor.get_page_content(page, clean=True)
        if not content_result.get("success"):
            return content_result

        content = content_result.get("content", "")
        query = payload.get("query")

        summary = await self._intelligence.summarize(content, query=query)
        if summary:
            return {
                "success": True,
                "summary": summary,
                "title": content_result.get("title", ""),
                "url": content_result.get("url", ""),
            }

        # J-Prime unavailable — return raw content excerpt
        return {
            "success": True,
            "summary": content[:500] + ("..." if len(content) > 500 else ""),
            "title": content_result.get("title", ""),
            "url": content_result.get("url", ""),
            "note": "Raw excerpt (J-Prime unavailable for summarization)",
        }

    async def _handle_close_tab(self, payload: Dict) -> Dict:
        """Close a browser tab."""
        if not self._playwright_available:
            return {"success": False, "error": "Playwright not available"}

        tab_id = payload.get("tab_id", "")
        if not tab_id:
            return {"success": False, "error": "No tab_id provided"}

        await self._engine.close_page(tab_id)
        return {"success": True, "closed": tab_id}

    async def _handle_query_elements(self, payload: Dict) -> Dict:
        """Query DOM elements by CSS selector."""
        if not self._playwright_available:
            return {"success": False, "error": "Playwright not available"}

        page = await self._engine.get_page(payload.get("tab_id"))
        if not page:
            return {"success": False, "error": "No active page"}

        selector = payload.get("selector", "")
        if not selector:
            return {"success": False, "error": "No selector provided"}

        elements = await self._extractor.query_elements(page, selector)
        return {"success": True, "elements": elements, "count": len(elements)}

    async def shutdown(self) -> None:
        """Clean shutdown — closes Playwright browser + httpx client."""
        await self._engine.shutdown()
        await self._intelligence.close()


# =============================================================================
# Singleton + Factory
# =============================================================================

_browsing_agent: Optional[BrowsingAgent] = None
# Thread-safe lock for singleton creation (avoids race in asyncio.Lock init)
_browsing_init_lock = threading.Lock()
_browsing_async_lock: Optional[asyncio.Lock] = None
_atexit_registered = False


def _get_async_lock() -> asyncio.Lock:
    """Get or create the asyncio lock, thread-safely."""
    global _browsing_async_lock
    if _browsing_async_lock is None:
        with _browsing_init_lock:
            if _browsing_async_lock is None:
                _browsing_async_lock = asyncio.Lock()
    return _browsing_async_lock


def _atexit_shutdown_browsing() -> None:
    """Atexit handler: shut down Playwright to prevent orphaned Chromium processes."""
    agent = _browsing_agent
    if agent is None:
        return
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Schedule shutdown on the running loop (best-effort)
            loop.create_task(agent.shutdown())
        else:
            loop.run_until_complete(agent.shutdown())
    except Exception:
        pass  # atexit — can't raise


async def get_browsing_agent() -> Optional[BrowsingAgent]:
    """Get or create the global BrowsingAgent. Thread-safe singleton.

    Returns None if BROWSE_DISABLED=true or initialization fails.
    The agent is always usable for search (WebSearchExtractor).
    Playwright-dependent actions (navigate, extract, form) require
    pip install playwright && python -m playwright install chromium.
    """
    global _browsing_agent, _atexit_registered

    if _browsing_agent is not None:
        return _browsing_agent

    async with _get_async_lock():
        # Double-check under lock
        if _browsing_agent is not None:
            return _browsing_agent

        if _env_bool("BROWSE_DISABLED", False):
            logger.info("[BROWSE] Disabled via BROWSE_DISABLED=true")
            return None

        agent = BrowsingAgent()
        try:
            await agent.on_initialize()
            _browsing_agent = agent

            # Register atexit cleanup (once) to prevent orphaned Chromium
            if not _atexit_registered:
                atexit.register(_atexit_shutdown_browsing)
                _atexit_registered = True

            logger.info(
                "[BROWSE] BrowsingAgent initialized "
                f"(playwright={'yes' if agent._playwright_available else 'no'})"
            )
            return _browsing_agent
        except Exception as e:
            logger.warning(f"[BROWSE] Failed to initialize BrowsingAgent: {e}")
            return None
