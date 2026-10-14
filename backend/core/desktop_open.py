"""
desktop_open - the one way JARVIS opens a URL, a file or an application.

Five call sites used to shell out to macOS ``open`` / ``open -a`` / osascript
(runtime_task_orchestrator, ApplicationLauncherExecutor x2,
NavigationExecutor x2), so on any other OS nothing a command asked to open
ever opened. Each platform now answers with its OWN registry of what exists,
never a table of names in code:

* Windows
    - URL / file: handed to the shell (explorer.exe) -> the user's default
      browser / the file's associated app, parented by the shell, not JARVIS.
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

What was opened is also brought IN FRONT, and the outcome names its window:
everything after an open (the vision loop's look, its clicks and typing)
acts on the foreground window, and on Windows a program started by a
background process does not get the foreground -- the vision loop once
typed a search into the terminal that was still in front.
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
from dataclasses import dataclass, replace
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
    # Title of the window the open produced, once it is in front.
    window: str = ""
    # True: that window holds the foreground. False: it could not be found or
    # brought forward -- callers must not act on the screen. None: this
    # platform's open activates the target itself and is not observed.
    in_front: Optional[bool] = None

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


async def _shell_open(target: str) -> None:
    """Windows: hand *target* (URL, file, folder, shell:AppsFolder\\ID) to the shell.

    What JARVIS opens FOR THE USER must not be JARVIS's: ShellExecute from
    this process (os.startfile) made a freshly started browser a child of the
    kernel, and descendants of the kernel are what its cleanups may stop. The
    short-lived explorer.exe hands the target to the running shell and exits,
    so the app's parent is the user's shell.
    """
    await _spawn_detached(["explorer.exe", target])


async def _shell_start(file_path: str, argument: Optional[str] = None) -> Tuple[int, str]:
    """Windows: Start-Process (ShellExecute) through a short-lived PowerShell:
    resolves PATH / App Paths names and passes an argument (a URL to a named
    browser), and the launched app outlives -- and does not descend from --
    JARVIS. Values travel as argv, never interpolated into the command text."""
    script = ("& { if ($args.Count -gt 1) { Start-Process -FilePath $args[0] -ArgumentList $args[1] } "
              "else { Start-Process -FilePath $args[0] } }")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script, file_path]
    if argument:
        argv.append(argument)
    return await _run(argv, _launch_timeout())


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
                        async def _named() -> OpenOutcome:
                            rc, out = await _shell_start(argv[0], url)
                            if rc == 0:
                                return OpenOutcome(True, "url", url, f"{url} in {name}", "browser_command")
                            return OpenOutcome(False, "url", url, error=out.strip()[:120])
                        named = await _opened_in_front(_named(), os.path.basename(argv[0]))
                        if named.ok:
                            return named
                        logger.info("[DesktopOpen] %s did not start (%s); using the default",
                                    name, named.error)
                else:
                    logger.info("[DesktopOpen] browser %r not registered; using the default", browser)

            async def _default() -> OpenOutcome:
                await _shell_open(url)
                return OpenOutcome(True, "url", url, url, "shell")
            return await _opened_in_front(_default(), await asyncio.to_thread(_win_url_handler_exe))
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
            async def _shell() -> OpenOutcome:
                await _shell_open(p)
                return OpenOutcome(True, "path", p, p, "shell")
            return await _opened_in_front(_shell())
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
            return await _opened_in_front(_open_app_windows(name))
        if _IS_MACOS:
            return await _open_app_macos(name)
        return await _open_app_linux(name)
    except Exception as exc:  # noqa: BLE001
        return OpenOutcome(False, "app", name, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Bringing what was opened to the front
# ---------------------------------------------------------------------------

def _open_wait_s() -> float:
    """How long an opened program may take to show (or retitle) its window."""
    return _env_float("JARVIS_OPEN_WINDOW_WAIT_S", 10.0)


def _open_poll_s() -> float:
    return _env_float("JARVIS_OPEN_WINDOW_POLL_S", 0.1)


def _win_exe_of(pid: int) -> str:
    """Lower-case executable basename of *pid*, "" when it cannot be read."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def _win_top_windows() -> List[Tuple[int, str, str]]:
    """(hwnd, title, owner exe) of visible, unowned, titled top-level windows,
    top of the z-order first."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: List[Tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((int(hwnd), buf.value, _win_exe_of(pid.value)))
        return True

    user32.EnumWindows(_visit, 0)
    return found


def _win_url_handler_exe() -> str:
    """Executable the user's default browser runs as (UserChoice for https)."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
                            r"\https\UserChoice") as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as k:
            command = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return ""
    argv = _command_argv(command)
    return os.path.basename(argv[0]).lower() if argv else ""


def _win_foreground() -> int:
    import ctypes
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _win_bring_forward(hwnd: int) -> bool:
    """Give *hwnd* the foreground and report whether it holds it.

    Windows grants the foreground only to the thread that owns the current
    input state; attaching to the foreground thread's input for the call is
    how a process that is about to drive the UI acquires that state.
    """
    import ctypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    me = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
    return _win_foreground() == hwnd


async def _opened_in_front(open_coro, handler_exe: str = "") -> OpenOutcome:
    """Run an open, then find the window it produced and bring it forward.

    The produced window is the topmost one that is new or retitled since the
    open began (a URL in a running browser retitles its window). When the
    handler is known (*handler_exe*: the browser a URL goes to) only its
    windows qualify -- the browser the user typed the command into may be
    the very window that changes. When it is not, the window in front before
    the open is excluded: a terminal's spinner retitles itself constantly
    and is never what was opened. Waits on evidence, bounded by
    JARVIS_OPEN_WINDOW_WAIT_S.
    """
    if not _IS_WINDOWS:
        return await open_coro
    before = {h: t for h, t, _ in await asyncio.to_thread(_win_top_windows)}
    was_front = await asyncio.to_thread(_win_foreground)
    outcome = await open_coro
    if not outcome.ok:
        return outcome
    handler_exe = handler_exe.lower()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _open_wait_s()
    while loop.time() < deadline:
        now = await asyncio.to_thread(_win_top_windows)
        changed = [
            (h, t) for h, t, exe in now
            if before.get(h) != t and (exe == handler_exe if handler_exe else h != was_front)
        ]
        if changed:
            hwnd, title = changed[0]
            if await asyncio.to_thread(_win_bring_forward, hwnd):
                return replace(outcome, window=title, in_front=True)
            logger.info("[DesktopOpen] %r appeared but could not take the foreground", title)
            return replace(outcome, window=title, in_front=False)
        await asyncio.sleep(_open_poll_s())
    logger.info("[DesktopOpen] no window appeared for %s within %.0fs",
                outcome.resolved or outcome.target, _open_wait_s())
    return replace(outcome, in_front=False)


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
        await _shell_open(f"shell:AppsFolder\\{app_id}")
        return OpenOutcome(True, "app", name, display, "apps_folder")
    # Not in the Start menu: an executable on PATH or registered under App Paths.
    rc, out = await _shell_start(shutil.which(name) or name)
    if rc == 0:
        return OpenOutcome(True, "app", name, name, "shell_start")
    return OpenOutcome(False, "app", name, error=f"not installed or not found ({out.strip()[:160]})")


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
