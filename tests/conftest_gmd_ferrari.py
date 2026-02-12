"""
Shared fixtures for Ghost Mode Display (GMD) + Ferrari Engine tests.

Provides:
- Browser launch/teardown for bouncing ball visual test
- Ferrari Engine (VideoWatcher) initialization
- N-Optic Nerve (multi-window vision) initialization
- GhostHandsOrchestrator lifecycle
- OCR bounce count extraction utility
- Skip conditions for headless/CI environments

All fixtures are env-var configurable and follow JARVIS patterns.
"""

import asyncio
import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import pytest

# ─────────────────────────────────────────────────────────────
# Skip conditions — detect capabilities at collection time
# ─────────────────────────────────────────────────────────────

_IS_MACOS = platform.system() == "Darwin"
_IS_CI = os.getenv("CI", "").lower() in ("true", "1", "yes")
_HAS_DISPLAY = os.getenv("DISPLAY") or _IS_MACOS

# Pre-flight capability checks (cached at module level)
_FERRARI_AVAILABLE: Optional[bool] = None
_OPTIC_AVAILABLE: Optional[bool] = None
_GHOST_HANDS_AVAILABLE: Optional[bool] = None


def _check_ferrari() -> bool:
    """Check if Ferrari Engine (ScreenCaptureKit) is available."""
    global _FERRARI_AVAILABLE
    if _FERRARI_AVAILABLE is not None:
        return _FERRARI_AVAILABLE
    try:
        from backend.native_extensions.fast_capture_wrapper import FastCaptureEngine
        _FERRARI_AVAILABLE = True
    except ImportError:
        _FERRARI_AVAILABLE = False
    return _FERRARI_AVAILABLE


def _check_optic() -> bool:
    """Check if N-Optic Nerve is available."""
    global _OPTIC_AVAILABLE
    if _OPTIC_AVAILABLE is not None:
        return _OPTIC_AVAILABLE
    try:
        from backend.ghost_hands.n_optic_nerve import NOpticNerve
        _OPTIC_AVAILABLE = True
    except ImportError:
        _OPTIC_AVAILABLE = False
    return _OPTIC_AVAILABLE


def _check_ghost_hands() -> bool:
    """Check if GhostHandsOrchestrator is available."""
    global _GHOST_HANDS_AVAILABLE
    if _GHOST_HANDS_AVAILABLE is not None:
        return _GHOST_HANDS_AVAILABLE
    try:
        from backend.ghost_hands.orchestrator import GhostHandsOrchestrator
        _GHOST_HANDS_AVAILABLE = True
    except ImportError:
        _GHOST_HANDS_AVAILABLE = False
    return _GHOST_HANDS_AVAILABLE


# ─────────────────────────────────────────────────────────────
# Pytest skip markers
# ─────────────────────────────────────────────────────────────

requires_macos = pytest.mark.skipif(
    not _IS_MACOS,
    reason="Requires macOS for ScreenCaptureKit and Yabai",
)

requires_display = pytest.mark.skipif(
    not _HAS_DISPLAY or _IS_CI,
    reason="Requires a display (not available in CI/headless)",
)

requires_ferrari = pytest.mark.skipif(
    not _check_ferrari(),
    reason="Ferrari Engine (ScreenCaptureKit) not available",
)

requires_optic = pytest.mark.skipif(
    not _check_optic(),
    reason="N-Optic Nerve not available",
)

requires_ghost_hands = pytest.mark.skipif(
    not _check_ghost_hands(),
    reason="GhostHandsOrchestrator not available",
)


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# Test timeouts and parameters (all env-var configurable)
BOUNCE_TEST_DURATION = _env_float("GMD_TEST_DURATION", 10.0)
BOUNCE_DETECTION_TIMEOUT = _env_float("GMD_DETECTION_TIMEOUT", 15.0)
FERRARI_FPS = _env_int("GMD_TEST_FERRARI_FPS", 30)
OCR_CHECK_INTERVAL = _env_float("GMD_TEST_OCR_INTERVAL", 0.2)
BROWSER_STARTUP_WAIT = _env_float("GMD_TEST_BROWSER_WAIT", 3.0)
BOUNCE_MIN_EXPECTED = _env_int("GMD_TEST_MIN_BOUNCES", 3)


# ─────────────────────────────────────────────────────────────
# Bounce count OCR extraction
# ─────────────────────────────────────────────────────────────

# Pattern matches "BOUNCE COUNT: 42" from OCR text
BOUNCE_PATTERN = re.compile(r"BOUNCE\s*COUNT\s*:\s*(\d+)", re.IGNORECASE)
STATUS_PATTERN = re.compile(r"STATUS\s*:\s*(VERTICAL|HORIZONTAL)", re.IGNORECASE)


def extract_bounce_count(ocr_text: str) -> Optional[int]:
    """Extract bounce count from OCR text.

    The bouncing_balls.html renders "BOUNCE COUNT: N" in large yellow text.
    OCR may introduce whitespace variations, so the pattern is flexible.

    Returns:
        int if count found, None if not detectable.
    """
    if not ocr_text:
        return None
    match = BOUNCE_PATTERN.search(ocr_text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def extract_mode(ocr_text: str) -> Optional[str]:
    """Extract bounce mode (VERTICAL/HORIZONTAL) from OCR text."""
    if not ocr_text:
        return None
    match = STATUS_PATTERN.search(ocr_text)
    return match.group(1).upper() if match else None


# ─────────────────────────────────────────────────────────────
# Browser launcher fixture
# ─────────────────────────────────────────────────────────────

class BounceTestBrowser:
    """Manages Chrome browser window for bouncing ball visual tests.

    Opens the bouncing_balls.html in a new Chrome window at a known size.
    Tracks the PID for cleanup on teardown.
    """

    def __init__(self, mode: str = "vertical", width: int = 800, height: int = 600):
        self.mode = mode
        self.width = width
        self.height = height
        self.process: Optional[subprocess.Popen] = None
        self.url: str = ""
        self._html_path = (
            Path(__file__).parent.parent
            / "backend" / "tests" / "visual_test" / "bouncing_balls.html"
        )

    async def launch(self) -> bool:
        """Launch Chrome with the bouncing ball test page."""
        if not self._html_path.exists():
            return False

        self.url = f"file://{self._html_path}?mode={self.mode}"

        # Use AppleScript on macOS for precise window control
        if _IS_MACOS:
            script = (
                f'tell application "Google Chrome"\n'
                f'  make new window\n'
                f'  set URL of active tab of front window to "{self.url}"\n'
                f'  set bounds of front window to {{100, 100, {100 + self.width}, {100 + self.height}}}\n'
                f'end tell'
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                # Give Chrome time to render
                await asyncio.sleep(BROWSER_STARTUP_WAIT)
                return proc.returncode == 0
            except (asyncio.TimeoutError, Exception):
                return False
        else:
            # Linux/other: use xdg-open or google-chrome directly
            try:
                self.process = subprocess.Popen(
                    ["google-chrome", f"--window-size={self.width},{self.height}",
                     "--new-window", self.url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                await asyncio.sleep(BROWSER_STARTUP_WAIT)
                return self.process.poll() is None
            except FileNotFoundError:
                return False

    async def close(self):
        """Close the test browser window."""
        if _IS_MACOS:
            # Close the Chrome window via AppleScript
            script = (
                f'tell application "Google Chrome"\n'
                f'  set targetURL to "{self.url}"\n'
                f'  repeat with w in windows\n'
                f'    repeat with t in tabs of w\n'
                f'      if URL of t contains "bouncing_balls" then\n'
                f'        close w\n'
                f'        return\n'
                f'      end if\n'
                f'    end repeat\n'
                f'  end repeat\n'
                f'end tell'
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        elif self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

    async def find_window_id(self) -> Optional[int]:
        """Find the window ID of the bouncing ball Chrome window.

        Uses Ferrari Engine's fast_capture for window discovery.
        Falls back to Yabai query if fast_capture unavailable.
        """
        # Try Ferrari Engine window discovery
        try:
            from backend.native_extensions.fast_capture_wrapper import FastCaptureEngine
            engine = FastCaptureEngine()
            windows = engine.get_visible_windows()
            for w in windows:
                title = w.get("title", "") or w.get("kCGWindowName", "")
                owner = w.get("owner", "") or w.get("kCGWindowOwnerName", "")
                if "bouncing" in title.lower() or (
                    "chrome" in owner.lower() and "stereoscopic" in title.lower()
                ):
                    return w.get("id") or w.get("kCGWindowNumber")
        except (ImportError, Exception):
            pass

        # Fallback: Yabai window query
        try:
            proc = await asyncio.create_subprocess_exec(
                "yabai", "-m", "query", "--windows",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if stdout:
                import json
                windows = json.loads(stdout.decode())
                for w in windows:
                    title = w.get("title", "")
                    app = w.get("app", "")
                    if "bouncing" in title.lower() or "stereoscopic" in title.lower():
                        return w.get("id")
        except (asyncio.TimeoutError, FileNotFoundError, Exception):
            pass

        return None


# ─────────────────────────────────────────────────────────────
# Pytest fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def bouncing_ball_html_path() -> Path:
    """Path to the bouncing ball visual test HTML."""
    path = (
        Path(__file__).parent.parent
        / "backend" / "tests" / "visual_test" / "bouncing_balls.html"
    )
    assert path.exists(), f"Bouncing ball HTML not found at {path}"
    return path


@pytest.fixture
async def vertical_bounce_browser() -> AsyncGenerator[BounceTestBrowser, None]:
    """Launch Chrome with vertical bouncing ball, teardown on exit."""
    browser = BounceTestBrowser(mode="vertical")
    launched = await browser.launch()
    if not launched:
        pytest.skip("Could not launch Chrome for bounce test")
    yield browser
    await browser.close()


@pytest.fixture
async def horizontal_bounce_browser() -> AsyncGenerator[BounceTestBrowser, None]:
    """Launch Chrome with horizontal bouncing ball, teardown on exit."""
    browser = BounceTestBrowser(mode="horizontal")
    launched = await browser.launch()
    if not launched:
        pytest.skip("Could not launch Chrome for bounce test")
    yield browser
    await browser.close()


@pytest.fixture
async def dual_bounce_browsers() -> AsyncGenerator[
    Tuple[BounceTestBrowser, BounceTestBrowser], None
]:
    """Launch TWO Chrome windows (vertical + horizontal) for God Mode testing."""
    v_browser = BounceTestBrowser(mode="vertical", width=600, height=500)
    h_browser = BounceTestBrowser(mode="horizontal", width=600, height=500)

    v_ok = await v_browser.launch()
    h_ok = await h_browser.launch()

    if not (v_ok and h_ok):
        await v_browser.close()
        await h_browser.close()
        pytest.skip("Could not launch both Chrome windows for dual bounce test")

    yield v_browser, h_browser

    await v_browser.close()
    await h_browser.close()


@pytest.fixture
async def ferrari_watcher():
    """Create a Ferrari Engine VideoWatcher for a window.

    Returns a factory function: await factory(window_id) -> watcher
    """
    watchers = []

    async def _create(window_id: int, fps: int = FERRARI_FPS):
        try:
            from backend.vision.macos_video_capture_advanced import VideoWatcher
            watcher = VideoWatcher(window_id, fps=fps)
            await watcher.start()
            watchers.append(watcher)
            return watcher
        except (ImportError, Exception) as e:
            pytest.skip(f"Ferrari VideoWatcher not available: {e}")
            return None

    yield _create

    # Teardown: stop all watchers
    for w in watchers:
        try:
            await w.stop()
        except Exception:
            pass


@pytest.fixture
async def n_optic_nerve():
    """Initialize and teardown N-Optic Nerve singleton."""
    try:
        from backend.ghost_hands.n_optic_nerve import NOpticNerve
        nerve = NOpticNerve.get_instance()
        started = await nerve.start()
        if not started:
            pytest.skip("N-Optic Nerve failed to start")
        yield nerve
        await nerve.stop()
    except ImportError:
        pytest.skip("N-Optic Nerve not available")


@pytest.fixture
async def ghost_orchestrator():
    """Initialize and teardown GhostHandsOrchestrator."""
    try:
        from backend.ghost_hands.orchestrator import get_ghost_hands
        orchestrator = await get_ghost_hands()
        if not orchestrator:
            pytest.skip("GhostHandsOrchestrator not available")
        yield orchestrator
        await orchestrator.stop()
    except ImportError:
        pytest.skip("GhostHandsOrchestrator not available")


@pytest.fixture
def bounce_tracker():
    """Utility for tracking bounce counts over time.

    Records (timestamp, count) pairs and provides analysis methods.
    """

    class BounceTracker:
        def __init__(self):
            self.samples: List[Tuple[float, int]] = []
            self.start_time: float = time.time()

        def record(self, count: int):
            """Record a bounce count observation."""
            self.samples.append((time.time() - self.start_time, count))

        @property
        def count(self) -> int:
            """Latest observed bounce count."""
            return self.samples[-1][1] if self.samples else 0

        @property
        def max_count(self) -> int:
            """Highest observed bounce count."""
            return max((s[1] for s in self.samples), default=0)

        @property
        def is_incrementing(self) -> bool:
            """Whether counts are monotonically non-decreasing."""
            if len(self.samples) < 2:
                return False
            counts = [s[1] for s in self.samples]
            return all(b >= a for a, b in zip(counts, counts[1:]))

        @property
        def unique_counts(self) -> int:
            """Number of distinct count values observed."""
            return len(set(s[1] for s in self.samples))

        @property
        def bounces_per_second(self) -> float:
            """Approximate bounce rate."""
            if len(self.samples) < 2:
                return 0.0
            first = self.samples[0]
            last = self.samples[-1]
            elapsed = last[0] - first[0]
            if elapsed <= 0:
                return 0.0
            return (last[1] - first[1]) / elapsed

        def summary(self) -> Dict[str, Any]:
            """Full summary of tracking session."""
            return {
                "total_samples": len(self.samples),
                "latest_count": self.count,
                "max_count": self.max_count,
                "is_incrementing": self.is_incrementing,
                "unique_counts": self.unique_counts,
                "bounces_per_second": round(self.bounces_per_second, 2),
                "duration_seconds": round(
                    self.samples[-1][0] if self.samples else 0.0, 2
                ),
            }

    return BounceTracker()
