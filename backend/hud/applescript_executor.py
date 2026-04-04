"""AppleScriptExecutor -- deterministic macOS actions via osascript.

Handles app launching, URL navigation, window management -- all without
LLM calls. These are Tier 0 deterministic actions per Manifesto S5.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ExecutorResult:
    success: bool
    output: str
    error: Optional[str] = None


class AppleScriptExecutor:
    """Executes deterministic macOS actions via osascript and open."""

    def discover_app(self, name: str) -> str:
        """Dynamically discover installed app by fuzzy name match."""
        query = name.lower()
        for search_dir in ["/Applications", "/System/Applications",
                           "/System/Applications/Utilities",
                           os.path.expanduser("~/Applications")]:
            try:
                for item in os.listdir(search_dir):
                    if item.endswith(".app"):
                        app = item[:-4]
                        if app.lower() == query:
                            return app
                        if query in app.lower():
                            return app
            except OSError:
                continue
        return name

    def infer_url(self, text: str) -> str:
        """Infer a URL from natural language.

        'LinkedIn' -> 'https://linkedin.com'
        'search Google for X' -> 'https://google.com/search?q=X'
        'https://...' -> passthrough
        """
        if text.startswith(("http://", "https://")):
            return text

        lower = text.lower().strip()

        # Search patterns
        search_match = re.match(r"search\s+google\s+for\s+(.+)", lower)
        if search_match:
            from urllib.parse import quote
            return f"https://google.com/search?q={quote(search_match.group(1))}"

        search_match = re.match(r"search\s+(.+?)\s+for\s+(.+)", lower)
        if search_match:
            site = search_match.group(1)
            query = search_match.group(2)
            from urllib.parse import quote
            # Common sites with search URLs
            if "youtube" in site:
                return f"https://youtube.com/results?search_query={quote(query)}"
            if "linkedin" in site:
                return f"https://linkedin.com/search/results/all/?keywords={quote(query)}"
            return f"https://google.com/search?q={quote(query)}+site:{site}"

        # Direct site names -- try adding .com
        clean = re.sub(r"[^a-z0-9]", "", lower)
        common = {
            "linkedin": "https://linkedin.com",
            "github": "https://github.com",
            "google": "https://google.com",
            "youtube": "https://youtube.com",
            "twitter": "https://x.com",
            "reddit": "https://reddit.com",
            "gmail": "https://mail.google.com",
            "stackoverflow": "https://stackoverflow.com",
        }
        if clean in common:
            return common[clean]

        # Fallback: assume .com
        return f"https://{clean}.com"

    async def open_app(self, app_name: str) -> ExecutorResult:
        """Open a macOS application by name."""
        resolved = self.discover_app(app_name)
        logger.info("[AppleScript] Opening app: %s (resolved: %s)", app_name, resolved)
        proc = subprocess.run(["open", "-a", resolved], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return ExecutorResult(success=True, output=f"Opened {resolved}")
        # Fallback with original name
        proc2 = subprocess.run(["open", "-a", app_name], capture_output=True, text=True, timeout=10)
        if proc2.returncode == 0:
            return ExecutorResult(success=True, output=f"Opened {app_name}")
        return ExecutorResult(success=False, output="", error=f"Cannot find app: {app_name}")

    async def open_url(self, url: str) -> ExecutorResult:
        """Open a URL in the default browser."""
        resolved = self.infer_url(url)
        logger.info("[AppleScript] Opening URL: %s", resolved)
        proc = subprocess.run(["open", resolved], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return ExecutorResult(success=True, output=f"Opened {resolved}")
        return ExecutorResult(success=False, output="", error=f"Failed to open URL: {resolved}")

    async def activate_app(self, app_name: str) -> ExecutorResult:
        """Bring an app to the foreground."""
        resolved = self.discover_app(app_name)
        script = f'tell application "{resolved}" to activate'
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return ExecutorResult(success=proc.returncode == 0, output=f"Activated {resolved}",
                              error=proc.stderr[:100] if proc.returncode != 0 else None)

    async def run_script(self, script: str) -> ExecutorResult:
        """Execute arbitrary AppleScript (must pass Iron Gate first)."""
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        output = proc.stdout.strip()[:500]
        return ExecutorResult(success=proc.returncode == 0, output=output,
                              error=proc.stderr[:200] if proc.returncode != 0 else None)
