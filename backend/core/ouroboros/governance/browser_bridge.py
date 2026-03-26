"""
BrowserBridge — Connect the governance pipeline to browser automation.

Enables visual verification during VALIDATE phase: navigate to a URL,
take screenshots, read page text, verify expected UI elements exist.

Supports two backends (auto-detected):
  1. Playwright (via subprocess — never imported directly)
  2. MCP browser server (placeholder for claude-in-chrome integration)

Boundary Principle:
  Deterministic: Backend detection, subprocess invocation, result parsing.
  Agentic: What to verify and how to interpret screenshots (via Vision).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_BROWSER_MODE = os.environ.get("JARVIS_BROWSER_MODE", "auto")
_BROWSER_TIMEOUT_S = float(os.environ.get("JARVIS_BROWSER_TIMEOUT_S", "15"))
_SCREENSHOT_DIR = Path(
    os.environ.get("JARVIS_SCREENSHOT_DIR", tempfile.gettempdir())
)


@dataclass(frozen=True)
class BrowserAction:
    """One browser action to perform."""
    action_type: str  # "navigate", "click", "screenshot", "read_text", "fill_form"
    target: str       # URL, CSS selector, or XPath
    value: Optional[str] = None  # for fill_form
    timeout_s: float = _BROWSER_TIMEOUT_S


@dataclass
class BrowserResult:
    """Result from a browser action."""
    success: bool
    action_type: str
    screenshot_path: Optional[str] = None
    page_text: Optional[str] = None
    error: Optional[str] = None
    duration_s: float = 0.0


class BrowserBridge:
    """Bridge between governance pipeline and browser automation.

    Auto-detects available backend (playwright > mcp > disabled).
    All browser interaction is via subprocess — never imports playwright
    directly into the main process.

    Usage:
        bridge = BrowserBridge()
        if bridge.is_available:
            result = await bridge.navigate("http://localhost:3000")
            result = await bridge.screenshot()
            result = await bridge.verify_ui(
                "http://localhost:3000/login",
                expected_elements=["Sign In", "Email", "Password"],
            )
    """

    def __init__(self, mode: str = _BROWSER_MODE) -> None:
        self._mode = mode
        self._backend: Optional[str] = None
        self._detected = False

    @property
    def is_available(self) -> bool:
        if not self._detected:
            self._detect_backend()
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        if not self._detected:
            self._detect_backend()
        return self._backend or "none"

    def _detect_backend(self) -> None:
        self._detected = True
        if self._mode == "disabled":
            self._backend = None
            return

        if self._mode in ("auto", "playwright"):
            if shutil.which("playwright") is not None or shutil.which("npx") is not None:
                self._backend = "playwright"
                return

        if self._mode in ("auto", "mcp"):
            # MCP browser server — placeholder detection
            if os.environ.get("JARVIS_MCP_BROWSER_URL"):
                self._backend = "mcp"
                return

        self._backend = None

    async def navigate(self, url: str) -> BrowserResult:
        """Navigate to a URL."""
        action = BrowserAction(action_type="navigate", target=url)
        return await self._execute(action)

    async def screenshot(self, selector: str = "body") -> BrowserResult:
        """Take a screenshot, optionally of a specific element."""
        action = BrowserAction(action_type="screenshot", target=selector)
        return await self._execute(action)

    async def read_page_text(self, url: str) -> BrowserResult:
        """Navigate to URL and extract all visible text."""
        action = BrowserAction(action_type="read_text", target=url)
        return await self._execute(action)

    async def verify_ui(
        self,
        url: str,
        expected_elements: Optional[List[str]] = None,
    ) -> BrowserResult:
        """Navigate to URL and verify expected elements are present."""
        t0 = time.monotonic()
        expected_elements = expected_elements or []

        # Step 1: Read page text
        text_result = await self.read_page_text(url)
        if not text_result.success:
            return BrowserResult(
                success=False,
                action_type="verify_ui",
                error=f"Failed to read page: {text_result.error}",
                duration_s=time.monotonic() - t0,
            )

        page_text = (text_result.page_text or "").lower()

        # Step 2: Check expected elements
        missing = [
            elem for elem in expected_elements
            if elem.lower() not in page_text
        ]

        # Step 3: Screenshot for evidence
        screenshot_result = await self.screenshot()

        success = len(missing) == 0
        error = None
        if missing:
            error = f"Missing UI elements: {', '.join(missing)}"

        return BrowserResult(
            success=success,
            action_type="verify_ui",
            screenshot_path=screenshot_result.screenshot_path,
            page_text=text_result.page_text,
            error=error,
            duration_s=time.monotonic() - t0,
        )

    async def _execute(self, action: BrowserAction) -> BrowserResult:
        """Route action to the detected backend."""
        if not self.is_available:
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error="No browser backend available",
            )

        if self._backend == "playwright":
            return await self._run_playwright(action)
        if self._backend == "mcp":
            return await self._run_mcp(action)

        return BrowserResult(
            success=False,
            action_type=action.action_type,
            error=f"Unknown backend: {self._backend}",
        )

    async def _run_playwright(self, action: BrowserAction) -> BrowserResult:
        """Execute action via Playwright subprocess."""
        t0 = time.monotonic()

        # Build a small inline script for playwright
        screenshot_path = str(
            _SCREENSHOT_DIR / f"ouroboros_{int(time.time())}.png"
        )

        if action.action_type == "navigate":
            script = (
                f"const {{ chromium }} = require('playwright');\n"
                f"(async () => {{\n"
                f"  const browser = await chromium.launch({{ headless: true }});\n"
                f"  const page = await browser.newPage();\n"
                f"  await page.goto({json.dumps(action.target)}, {{ timeout: {int(action.timeout_s * 1000)} }});\n"
                f"  console.log(JSON.stringify({{ success: true, title: await page.title() }}));\n"
                f"  await browser.close();\n"
                f"}})();\n"
            )
        elif action.action_type == "screenshot":
            script = (
                f"const {{ chromium }} = require('playwright');\n"
                f"(async () => {{\n"
                f"  const browser = await chromium.launch({{ headless: true }});\n"
                f"  const page = await browser.newPage();\n"
                f"  await page.goto('about:blank');\n"
                f"  await page.screenshot({{ path: {json.dumps(screenshot_path)} }});\n"
                f"  console.log(JSON.stringify({{ success: true, path: {json.dumps(screenshot_path)} }}));\n"
                f"  await browser.close();\n"
                f"}})();\n"
            )
        elif action.action_type == "read_text":
            script = (
                f"const {{ chromium }} = require('playwright');\n"
                f"(async () => {{\n"
                f"  const browser = await chromium.launch({{ headless: true }});\n"
                f"  const page = await browser.newPage();\n"
                f"  await page.goto({json.dumps(action.target)}, {{ timeout: {int(action.timeout_s * 1000)} }});\n"
                f"  const text = await page.innerText('body');\n"
                f"  console.log(JSON.stringify({{ success: true, text: text.substring(0, 10000) }}));\n"
                f"  await browser.close();\n"
                f"}})();\n"
            )
        else:
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error=f"Unsupported action: {action.action_type}",
                duration_s=time.monotonic() - t0,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                "node", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=action.timeout_s + 5,
            )

            if proc.returncode != 0:
                return BrowserResult(
                    success=False,
                    action_type=action.action_type,
                    error=stderr.decode()[:500] if stderr else "playwright failed",
                    duration_s=time.monotonic() - t0,
                )

            try:
                data = json.loads(stdout.decode())
                return BrowserResult(
                    success=data.get("success", False),
                    action_type=action.action_type,
                    screenshot_path=data.get("path"),
                    page_text=data.get("text"),
                    duration_s=time.monotonic() - t0,
                )
            except json.JSONDecodeError:
                return BrowserResult(
                    success=False,
                    action_type=action.action_type,
                    error="Invalid JSON from playwright",
                    duration_s=time.monotonic() - t0,
                )

        except asyncio.TimeoutError:
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error=f"Playwright timed out after {action.timeout_s}s",
                duration_s=time.monotonic() - t0,
            )
        except FileNotFoundError:
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error="node not found — playwright requires Node.js",
                duration_s=time.monotonic() - t0,
            )
        except Exception as exc:
            return BrowserResult(
                success=False,
                action_type=action.action_type,
                error=str(exc),
                duration_s=time.monotonic() - t0,
            )

    async def _run_mcp(self, action: BrowserAction) -> BrowserResult:
        """Execute action via MCP browser server (placeholder)."""
        logger.debug(
            "[BrowserBridge] MCP backend not yet implemented for action=%s",
            action.action_type,
        )
        return BrowserResult(
            success=False,
            action_type=action.action_type,
            error="MCP browser backend not yet implemented",
        )


# Singleton
_bridge: Optional[BrowserBridge] = None


def get_browser_bridge() -> BrowserBridge:
    """Get or create the singleton BrowserBridge."""
    global _bridge
    if _bridge is None:
        _bridge = BrowserBridge()
    return _bridge
