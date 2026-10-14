"""
desktop_open - the one way JARVIS opens a URL, a file or an application.

Five call sites used to shell out to macOS ``open`` / ``open -a`` / osascript
(runtime_task_orchestrator, ApplicationLauncherExecutor x2,
NavigationExecutor x2), so on any other OS nothing a command asked to open
ever opened. Each platform now answers with its OWN registry of what exists,
never a table of names in code:

* Windows
    - URL / file: ShellExecute (``os.startfile``) -> the user's default
      browser / the file's associated app.
    - URL in a named browser: the browsers Windows has registered under
      ``StartMenuInternet`` (each carries its own command line).
    - App: the Start-menu app index (``Get-StartApps``: classic and Store
      apps by display name, launched through ``shell:AppsFolder``), then
      PATH / ``App Paths`` via ShellExecute.
* macOS: ``open`` / ``open -a`` (activating an app already running), and
  the ``/Applications`` bundle as the last resort -- the behaviour the five
  call sites had.
* Linux: ``xdg-open`` for URLs and files; ``gtk-launch`` / PATH for apps.

Names are matched adaptively: exact, then the shortest whole-word
containment ("chrome" -> "Google Chrome", not "Chrome Remote Desktop"), then
a similarity cutoff (``JARVIS_APP_MATCH_CUTOFF``). The app index is cached
(``JARVIS_APP_INDEX_TTL_S``) and refreshed once on a miss, so an app
installed a minute ago is found. Every entry point is async and never raises:
the outcome says what happened and how.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class OpenOutcome:
    ok: bool
    kind: str            # "url" | "path" | "app"
    target: str          # what was asked for
    resolved: str = ""   # what was actually opened (app display name, URL, path)
    method: str = ""     # how (shell_execute, apps_folder, browser_command, open, xdg-open, ...)
    error: str = ""

    def describe(self) -> str:
        if self.ok:
            shown = self.resolved or self.target
            return f"Opened {shown}"
        return f"Could not open {self.target}: {self.error or 'not found'}"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def looks_like_url(target: str) -> bool:
    """A URL with a scheme, or a bare host ("github.com/x"), never a path."""
    t = (target or "").strip()
    if not t or " " in t:
        return False
    if _SCHEME_RE.match(t) and not re.match(r"^[a-z]:[\\/]", t, re.IGNORECASE):
        return bool(urlparse(t).scheme)
    host = t.split("/", 1)[0]
    return "." in host and not Path(t).expanduser().exists()


def normalize_url(target: str) -> str:
    t = target.strip()
    return t if _SCHEME_RE.match(t) else f"https://{t}"


# ---------------------------------------------------------------------------
# Adaptive name matching (shared by apps and browsers)
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()


def best_match(query: str, names: Sequence[str]) -> Optional[str]:
    """The entry in *names* the user most plausibly meant by *query*."""
    q = _norm(query)
    if not q or not names:
        return None
    normalized = {n: _norm(n) for n in names}
    exact = [n for n, v in normalized.items() if v == q]
    if exact:
        return min(exact, key=len)
    q_words = set(q.split())
    contains = [n for n, v in normalized.items() if q_words <= set(v.split())]
    if contains:
        return min(contains, key=len)
    cutoff = _env_float("JARVIS_APP_MATCH_CUTOFF", 0.75)
    by_norm: Dict[str, str] = {}
    for n, v in normalized.items():
        by_norm.setdefault(v, n)
    close = difflib.get_close_matches(q, list(by_norm), n=1, cutoff=cutoff)
    return by_norm[close[0]] if close else None


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

async def _run(argv: List[str], timeout: float) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timeout"
    text = (out or b"").decode(errors="replace") or (err or b"").decode(errors="replace")
    return proc.returncode or 0, text


async def _spawn_detached(argv: List[str]) -> None:
    """Start a GUI program without waiting for it to exit."""
    await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )


def _launch_timeout() -> float:
    return _env_float("JARVIS_DESKTOP_OPEN_TIMEOUT_S", 15.0)


# ---------------------------------------------------------------------------
# Windows registries: Start-menu apps, registered browsers
# ---------------------------------------------------------------------------

class _TimedIndex:
    """A registry snapshot with a TTL and one refresh on a miss."""

    def __init__(self, ttl_env: str, ttl_default: float) -> None:
        self._ttl_env, self._ttl_default = ttl_env, ttl_default
        self._data: Dict[str, str] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, loader, *, force: bool = False) -> Dict[str, str]:
        async with self._lock:
            ttl = _env_float(self._ttl_env, self._ttl_default)
            if force or not self._data or time.monotonic() - self._loaded_at > ttl:
                try:
                    self._data = await loader()
                    self._loaded_at = time.monotonic()
                except Exception as exc:  # noqa: BLE001 -- keep the last good snapshot
                    logger.warning("[DesktopOpen] index refresh failed: %s", exc)
            return dict(self._data)


_start_apps = _TimedIndex("JARVIS_APP_INDEX_TTL_S", 300.0)
_browsers = _TimedIndex("JARVIS_APP_INDEX_TTL_S", 300.0)


async def _load_start_apps() -> Dict[str, str]:
    """Display name -> AppUserModelID, from the Start menu (classic + Store)."""
    rc, out = await _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
        timeout=_launch_timeout(),
    )
    if rc != 0 or not out.strip():
        raise RuntimeError(f"Get-StartApps rc={rc}: {out[:200]}")
    rows = json.loads(out)
    if isinstance(rows, dict):
        rows = [rows]
    return {r["Name"]: r["AppID"] for r in rows if r.get("Name") and r.get("AppID")}


async def _load_browsers() -> Dict[str, str]:
    """Display name -> open command, for every browser Windows has registered."""
    def _read() -> Dict[str, str]:
        import winreg

        found: Dict[str, str] = {}
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                root = winreg.OpenKey(hive, r"SOFTWARE\Clients\StartMenuInternet")
            except OSError:
                continue
            with root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    key = winreg.EnumKey(root, i)
                    try:
                        with winreg.OpenKey(root, key) as k:
                            try:
                                name = winreg.QueryValueEx(k, "")[0] or key
                            except OSError:
                                name = key
                        with winreg.OpenKey(root, key + r"\shell\open\command") as c:
                            command = winreg.QueryValueEx(c, "")[0]
                    except OSError:
                        continue
                    found.setdefault(name, command)
        return found

    return await asyncio.to_thread(_read)


async def _resolve_indexed(index: _TimedIndex, loader, name: str) -> Optional[Tuple[str, str]]:
    entries = await index.get(loader)
    match = best_match(name, list(entries))
    if match is None:
        entries = await index.get(loader, force=True)   # installed since the snapshot?
        match = best_match(name, list(entries))
    return (match, entries[match]) if match else None


def _command_argv(command: str) -> List[str]:
    """argv of a registered open command (quotes honoured, Windows rules)."""
    return [part.strip('"') for part in shlex.split(command, posix=False)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def preferred_browser() -> Optional[str]:
    """The browser URLs open in: JARVIS_DEFAULT_BROWSER when set; otherwise
    Google Chrome on macOS (v283.3: JARVIS's own UI runs there) and the
    operating system's default browser everywhere else (None)."""
    explicit = os.environ.get("JARVIS_DEFAULT_BROWSER", "").strip()
    if explicit:
        return explicit
    return "Google Chrome" if _IS_MACOS else None


async def open_url(url: str, *, browser: Optional[str] = None) -> OpenOutcome:
    """Open *url* in *browser* if named and installed, else the default browser."""
    url = normalize_url(url)
    try:
        if _IS_WINDOWS:
            if browser:
                hit = await _resolve_indexed(_browsers, _load_browsers, browser)
                if hit:
                    name, command = hit
                    argv = _command_argv(command)
                    if argv:
                        await _spawn_detached([argv[0], url])
                        return OpenOutcome(True, "url", url, f"{url} in {name}", "browser_command")
                logger.info("[DesktopOpen] browser %r not registered; using the default", browser)
            await asyncio.to_thread(os.startfile, url)  # type: ignore[attr-defined]
            return OpenOutcome(True, "url", url, url, "shell_execute")
        if _IS_MACOS:
            if browser:
                rc, _ = await _run(["open", "-a", browser, url], _launch_timeout())
                if rc == 0:
                    return OpenOutcome(True, "url", url, f"{url} in {browser}", "open -a")
            rc, out = await _run(["open", url], _launch_timeout())
            return OpenOutcome(rc == 0, "url", url, url, "open", "" if rc == 0 else out.strip())
        rc, out = await _run(["xdg-open", url], _launch_timeout())
        return OpenOutcome(rc == 0, "url", url, url, "xdg-open", "" if rc == 0 else out.strip())
    except Exception as exc:  # noqa: BLE001
        return OpenOutcome(False, "url", url, error=f"{type(exc).__name__}: {exc}")


async def open_path(path: str) -> OpenOutcome:
    """Open a file or folder with the platform's associated application."""
    p = str(Path(path).expanduser())
    try:
        if not Path(p).exists():
            return OpenOutcome(False, "path", p, error="no such file or folder")
        if _IS_WINDOWS:
            await asyncio.to_thread(os.startfile, p)  # type: ignore[attr-defined]
            return OpenOutcome(True, "path", p, p, "shell_execute")
        argv = ["open", p] if _IS_MACOS else ["xdg-open", p]
        rc, out = await _run(argv, _launch_timeout())
        return OpenOutcome(rc == 0, "path", p, p, argv[0], "" if rc == 0 else out.strip())
    except Exception as exc:  # noqa: BLE001
        return OpenOutcome(False, "path", p, error=f"{type(exc).__name__}: {exc}")


async def open_app(name: str) -> OpenOutcome:
    """Launch (or bring forward) the application the user called *name*."""
    name = (name or "").strip()
    if not name:
        return OpenOutcome(False, "app", name, error="no application named")
    try:
        if _IS_WINDOWS:
            return await _open_app_windows(name)
        if _IS_MACOS:
            return await _open_app_macos(name)
        return await _open_app_linux(name)
    except Exception as exc:  # noqa: BLE001
        return OpenOutcome(False, "app", name, error=f"{type(exc).__name__}: {exc}")


async def open_target(target: str, *, browser: Optional[str] = None) -> OpenOutcome:
    """Open whatever *target* is: a URL, an existing path, or an application."""
    t = (target or "").strip()
    if looks_like_url(t):
        return await open_url(t, browser=browser)
    if t and Path(t).expanduser().exists():
        return await open_path(t)
    return await open_app(t)


# ---------------------------------------------------------------------------
# Per-platform app launch
# ---------------------------------------------------------------------------

async def _open_app_windows(name: str) -> OpenOutcome:
    hit = await _resolve_indexed(_start_apps, _load_start_apps, name)
    if hit:
        display, app_id = hit
        await _spawn_detached(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
        return OpenOutcome(True, "app", name, display, "apps_folder")
    # Not in the Start menu: an executable on PATH or registered under App Paths.
    candidate = shutil.which(name) or name
    try:
        await asyncio.to_thread(os.startfile, candidate)  # type: ignore[attr-defined]
        return OpenOutcome(True, "app", name, candidate, "shell_execute")
    except OSError as exc:
        return OpenOutcome(False, "app", name, error=f"not installed or not found ({exc.strerror or exc})")


async def _open_app_macos(name: str) -> OpenOutcome:
    rc, _ = await _run(["pgrep", "-f", name], _launch_timeout())
    if rc == 0:
        rc, out = await _run(["osascript", "-e", f'tell application "{name}" to activate'],
                             _launch_timeout())
        if rc == 0:
            return OpenOutcome(True, "app", name, name, "activate")
    rc, out = await _run(["open", "-a", name], _launch_timeout())
    if rc == 0:
        return OpenOutcome(True, "app", name, name, "open -a")
    bundle = Path("/Applications") / f"{name}.app"
    if bundle.exists():
        rc, out = await _run(["open", str(bundle)], _launch_timeout())
        if rc == 0:
            return OpenOutcome(True, "app", name, str(bundle), "open bundle")
    return OpenOutcome(False, "app", name, error=out.strip() or "application not found")


async def _open_app_linux(name: str) -> OpenOutcome:
    for candidate in dict.fromkeys([name, name.lower(), name.lower().replace(" ", "-")]):
        exe = shutil.which(candidate)
        if exe:
            await _spawn_detached([exe])
            return OpenOutcome(True, "app", name, exe, "exec")
    if shutil.which("gtk-launch"):
        rc, out = await _run(["gtk-launch", name.lower().replace(" ", "-")], _launch_timeout())
        if rc == 0:
            return OpenOutcome(True, "app", name, name, "gtk-launch")
    return OpenOutcome(False, "app", name, error="application not found")


__all__ = [
    "OpenOutcome", "best_match", "looks_like_url", "normalize_url",
    "open_app", "open_path", "open_target", "open_url", "preferred_browser",
]
