"""
Browser Automation Bridge -- Governance-to-Browser Visual Verification
=======================================================================

Connects the Ouroboros governance pipeline to browser automation tools
for visual UI verification, page text extraction, and screenshot capture.

Supports two backends:
  - **playwright**: Runs a lightweight Playwright script via subprocess
    (argv-based, no shell).  Does NOT import playwright directly so the
    module loads cleanly even when playwright is not installed.
  - **mcp**: Placeholder for MCP browser server integration (future).

If neither backend is available, all operations return graceful
``BrowserResult(success=False)`` with an explanatory error — the bridge
never raises or blocks the governance pipeline.

Boundary Principle
------------------
Deterministic: subprocess management, result parsing, singleton lifecycle.
Agentic: actual page content / visual state comes from external browser.

Environment Variables
---------------------
``JARVIS_BROWSER_BRIDGE_MODE``
    Backend selection: ``"playwright"``, ``"mcp"``, ``"disabled"``, or
    ``"auto"`` (default).  Auto probes playwright first, then mcp.
``JARVIS_BROWSER_BRIDGE_TIMEOUT_S``
    Default per-action timeout in seconds (default: 15).
``JARVIS_BROWSER_SCREENSHOT_DIR``
    Directory for saved screenshots (default: /tmp/jarvis_screenshots).
``JARVIS_BROWSER_HEADLESS``
    Set to ``"0"`` or ``"false"`` for headed browser (default: headless).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.BrowserBridge")

_MODE = os.environ.get("JARVIS_BROWSER_BRIDGE_MODE", "auto").lower()
_DEFAULT_TIMEOUT_S = float(os.environ.get("JARVIS_BROWSER_BRIDGE_TIMEOUT_S", "15"))
_SCREENSHOT_DIR = Path(
    os.environ.get("JARVIS_BROWSER_SCREENSHOT_DIR", "/tmp/jarvis_screenshots")
)
_HEADLESS = os.environ.get("JARVIS_BROWSER_HEADLESS", "1").lower() not in (
    "0", "false", "no",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BrowserAction:
    """Describes a single browser automation action.

    Attributes
    ----------
    action_type:
        One of ``"navigate"``, ``"click"``, ``"screenshot"``,
        ``"read_text"``, ``"fill_form"``.
    target:
        URL (for navigate/read_text), CSS selector or XPath (for click/
        screenshot/fill_form).
    value:
        Text value for ``"fill_form"`` actions.
    timeout_s:
        Per-action timeout in seconds.
    """

    action_type: str  # "navigate" | "click" | "screenshot" | "read_text" | "fill_form"
    target: str
    value: Optional[str] = None
    timeout_s: float = _DEFAULT_TIMEOUT_S

    _VALID_TYPES = frozenset({
        "navigate", "click", "screenshot", "read_text", "fill_form",
    })

    def __post_init__(self) -> None:
        if self.action_type not in self._VALID_TYPES:
            raise ValueError(
                f"Invalid action_type {self.action_type!r}; "
                f"must be one of {sorted(self._VALID_TYPES)}"
            )


@dataclass
class BrowserResult:
    """Outcome of a single browser action.

    Attributes
    ----------
    success:
        True if the action completed without error.
    action_type:
        The action that was attempted.
    screenshot_path:
        Filesystem path to a saved screenshot (if applicable).
    page_text:
        Extracted text content from the page (if applicable).
    error:
        Error message if the action failed.
    duration_s:
        Wall-clock seconds the action took.
    page_url:
        The current page URL after the action (if available).
    elements_found:
        Number of elements matching the selector (for verify_ui).
    """

    success: bool
    action_type: str
    screenshot_path: Optional[str] = None
    page_text: Optional[str] = None
    error: Optional[str] = None
    duration_s: float = 0.0
    page_url: Optional[str] = None
    elements_found: Optional[int] = None


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


async def _check_playwright_async() -> bool:
    """Async check for playwright availability via subprocess (argv, no shell)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c", "import playwright; print('ok')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return proc.returncode == 0 and b"ok" in stdout
    except (OSError, asyncio.TimeoutError):
        return False


# ---------------------------------------------------------------------------
# Playwright runner script (passed to subprocess via argv, no shell)
# ---------------------------------------------------------------------------

# This script is passed as a constant string to ``python3 -c`` via
# ``asyncio.create_subprocess_exec`` (argv-based, safe — no shell
# interpretation).  The action payload is passed as a separate argv
# element (sys.argv[1]), NOT interpolated into the script string.

_PLAYWRIGHT_SCRIPT = '''
import asyncio
import json
import sys

async def main():
    action = json.loads(sys.argv[1])
    action_type = action["action_type"]
    target = action["target"]
    value = action.get("value")
    timeout_ms = int(action.get("timeout_s", 15) * 1000)
    headless = action.get("headless", True)
    screenshot_path = action.get("screenshot_path")

    from playwright.async_api import async_playwright
    result = {"success": False, "action_type": action_type}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            if action_type == "navigate":
                await page.goto(target, wait_until="domcontentloaded")
                result["success"] = True
                result["page_url"] = page.url

            elif action_type == "click":
                await page.click(target)
                result["success"] = True
                result["page_url"] = page.url

            elif action_type == "screenshot":
                selector = target if target != "body" else None
                path = screenshot_path or "/tmp/jarvis_screenshot.png"
                if selector:
                    elem = await page.query_selector(selector)
                    if elem:
                        await elem.screenshot(path=path)
                    else:
                        await page.screenshot(path=path, full_page=True)
                else:
                    await page.screenshot(path=path, full_page=True)
                result["success"] = True
                result["screenshot_path"] = path
                result["page_url"] = page.url

            elif action_type == "read_text":
                await page.goto(target, wait_until="domcontentloaded")
                text = await page.inner_text("body")
                result["success"] = True
                result["page_text"] = text[:50000]
                result["page_url"] = page.url

            elif action_type == "fill_form":
                await page.fill(target, value or "")
                result["success"] = True
                result["page_url"] = page.url

        except Exception as e:
            result["error"] = str(e)
        finally:
            await browser.close()

    print(json.dumps(result))

asyncio.run(main())
'''


# ---------------------------------------------------------------------------
# BrowserBridge
# ---------------------------------------------------------------------------


class BrowserBridge:
    """Browser automation bridge for governance visual verification.

    Provides a high-level async API for common browser operations.
    Delegates to playwright subprocess or MCP server depending on
    detected availability and configuration.

    Parameters
    ----------
    mode:
        Backend selection: ``"playwright"``, ``"mcp"``, ``"disabled"``,
        or ``"auto"`` (default).
    """

    def __init__(self, mode: str = "auto") -> None:
        self._mode = mode.lower() if mode else _MODE
        self._playwright_available: Optional[bool] = None
        self._mcp_available: Optional[bool] = None
        self._action_count = 0
        self._error_count = 0

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True if at least one browser backend is detected."""
        if self._mode == "disabled":
            return False
        if self._mode == "playwright":
            return self._check_playwright_cached()
        if self._mode == "mcp":
            return self._check_mcp_cached()
        # auto: try playwright, then mcp
        return self._check_playwright_cached() or self._check_mcp_cached()

    def _check_playwright_cached(self) -> bool:
        """Cached playwright availability check (import probe only)."""
        if self._playwright_available is None:
            try:
                import importlib.util
                spec = importlib.util.find_spec("playwright")
                self._playwright_available = spec is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                self._playwright_available = False
        return self._playwright_available

    def _check_mcp_cached(self) -> bool:
        """Cached MCP browser server availability check."""
        if self._mcp_available is None:
            # MCP browser integration is a future capability;
            # detect via env var for forward compatibility.
            self._mcp_available = bool(
                os.environ.get("JARVIS_MCP_BROWSER_URL")
            )
        return self._mcp_available

    # ------------------------------------------------------------------
    # High-level API
    # ------------------------------------------------------------------

    async def navigate(self, url: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> BrowserResult:
        """Navigate to a URL and return the result."""
        action = BrowserAction(
            action_type="navigate",
            target=url,
            timeout_s=timeout_s,
        )
        return await self._dispatch(action)

    async def screenshot(
        self,
        selector: str = "body",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> BrowserResult:
        """Take a screenshot, optionally scoped to a CSS selector.

        Saves the screenshot to a temporary file and returns the path
        in ``BrowserResult.screenshot_path``.
        """
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = str(
            _SCREENSHOT_DIR / f"screenshot_{uuid.uuid4().hex[:8]}.png"
        )
        action = BrowserAction(
            action_type="screenshot",
            target=selector,
            timeout_s=timeout_s,
        )
        return await self._dispatch(action, screenshot_path=screenshot_path)

    async def read_page_text(
        self, url: str, timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> BrowserResult:
        """Navigate to a URL and extract the visible text content."""
        action = BrowserAction(
            action_type="read_text",
            target=url,
            timeout_s=timeout_s,
        )
        return await self._dispatch(action)

    async def verify_ui(
        self,
        url: str,
        expected_elements: List[str],
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> BrowserResult:
        """Verify that a page contains expected text elements.

        Navigates to ``url``, takes a screenshot, reads page text, and
        checks if each item in ``expected_elements`` appears in the text.

        Returns ``BrowserResult.success = True`` only if ALL expected
        elements are found.
        """
        start = time.monotonic()

        # Step 1: Read page text
        text_result = await self.read_page_text(url, timeout_s=timeout_s)
        if not text_result.success:
            return BrowserResult(
                success=False,
                action_type="verify_ui",
                error=f"Failed to read page: {text_result.error}",
                duration_s=time.monotonic() - start,
            )

        page_text = text_result.page_text or ""

        # Step 2: Take screenshot for evidence
        screenshot_result = await self.screenshot(timeout_s=timeout_s)

        # Step 3: Check for expected elements
        found = 0
        missing: List[str] = []
        for elem in expected_elements:
            if elem.lower() in page_text.lower():
                found += 1
            else:
                missing.append(elem)

        all_found = len(missing) == 0
        error_msg = None
        if missing:
            error_msg = f"Missing elements: {missing}"

        return BrowserResult(
            success=all_found,
            action_type="verify_ui",
            screenshot_path=screenshot_result.screenshot_path,
            page_text=page_text[:10000],
            error=error_msg,
            duration_s=time.monotonic() - start,
            page_url=text_result.page_url,
            elements_found=found,
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        action: BrowserAction,
        screenshot_path: Optional[str] = None,
    ) -> BrowserResult:
        """Route an action to the appropriate backend."""
        self._action_count += 1

        if self._mode == "disabled":
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error="Browser bridge is disabled",
            )

        # Try playwright first (or exclusively if mode=playwright)
        if self._mode in ("auto", "playwright"):
            if self._check_playwright_cached():
                return await self._run_playwright(action, screenshot_path)

        # Try MCP
        if self._mode in ("auto", "mcp"):
            if self._check_mcp_cached():
                return await self._run_mcp(action)

        self._error_count += 1
        return BrowserResult(
            success=False,
            action_type=action.action_type,
            error="No browser backend available (install playwright or configure MCP)",
        )

    # ------------------------------------------------------------------
    # Playwright backend
    # ------------------------------------------------------------------

    async def _run_playwright(
        self,
        action: BrowserAction,
        screenshot_path: Optional[str] = None,
    ) -> BrowserResult:
        """Run a browser action via Playwright subprocess (argv-based, no shell).

        A self-contained Python script is passed as a child process using
        ``asyncio.create_subprocess_exec`` with explicit argv.  The action
        payload is passed as ``sys.argv[1]`` (JSON), never interpolated
        into the script string.
        """
        start = time.monotonic()

        action_payload = {
            "action_type": action.action_type,
            "target": action.target,
            "value": action.value,
            "timeout_s": action.timeout_s,
            "headless": _HEADLESS,
            "screenshot_path": screenshot_path,
        }

        try:
            # Argv-based invocation — safe, no shell injection vector.
            # The script is a constant; the payload is a separate argv element.
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", _PLAYWRIGHT_SCRIPT,
                json.dumps(action_payload),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=action.timeout_s + 10.0,
            )
        except asyncio.TimeoutError:
            self._error_count += 1
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error=f"Playwright subprocess timed out after {action.timeout_s + 10.0}s",
                duration_s=time.monotonic() - start,
            )
        except OSError as exc:
            self._error_count += 1
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error=f"Failed to launch Playwright subprocess: {exc}",
                duration_s=time.monotonic() - start,
            )

        duration = time.monotonic() - start

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:2000]
            self._error_count += 1
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error=f"Playwright exited with code {proc.returncode}: {stderr_text}",
                duration_s=duration,
            )

        # Parse JSON result from stdout
        try:
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            # Find the last line that looks like JSON (script may emit logs)
            json_line = ""
            for line in reversed(stdout_text.splitlines()):
                stripped = line.strip()
                if stripped.startswith("{"):
                    json_line = stripped
                    break
            if not json_line:
                json_line = stdout_text

            result_data = json.loads(json_line)
        except (json.JSONDecodeError, ValueError) as exc:
            self._error_count += 1
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error=f"Failed to parse Playwright output: {exc}",
                duration_s=duration,
            )

        return BrowserResult(
            success=result_data.get("success", False),
            action_type=action.action_type,
            screenshot_path=result_data.get("screenshot_path"),
            page_text=result_data.get("page_text"),
            error=result_data.get("error"),
            duration_s=duration,
            page_url=result_data.get("page_url"),
        )

    # ------------------------------------------------------------------
    # MCP backend (placeholder)
    # ------------------------------------------------------------------

    async def _run_mcp(self, action: BrowserAction) -> BrowserResult:
        """Run a browser action via MCP browser server.

        This is a placeholder for future MCP integration.  Currently
        returns a not-available result.
        """
        logger.debug(
            "[BrowserBridge] MCP backend not yet implemented for action %s",
            action.action_type,
        )
        return BrowserResult(
            success=False,
            action_type=action.action_type,
            error="MCP browser backend not yet implemented",
        )

    # ------------------------------------------------------------------
    # Health / stats
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return health status for observability."""
        return {
            "mode": self._mode,
            "is_available": self.is_available,
            "playwright_detected": self._check_playwright_cached(),
            "mcp_detected": self._check_mcp_cached(),
            "headless": _HEADLESS,
            "action_count": self._action_count,
            "error_count": self._error_count,
            "screenshot_dir": str(_SCREENSHOT_DIR),
        }


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_browser_bridge_instance: Optional[BrowserBridge] = None


def get_browser_bridge() -> BrowserBridge:
    """Return the singleton BrowserBridge instance.

    Thread-safe for first-call races (the worst case is two instances
    are created and one is discarded — no state is shared).
    """
    global _browser_bridge_instance
    if _browser_bridge_instance is None:
        _browser_bridge_instance = BrowserBridge(mode=_MODE)
        logger.info(
            "[BrowserBridge] Initialized (mode=%s, available=%s)",
            _browser_bridge_instance._mode,
            _browser_bridge_instance.is_available,
        )
    return _browser_bridge_instance
