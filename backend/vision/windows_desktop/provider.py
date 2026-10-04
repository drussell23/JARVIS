"""WindowsDesktopProvider: the YabaiSpaceDetector read surface, served from Windows.

Runs wherever the JARVIS backend runs (WSL, or native Windows) and talks to
``daemon.py`` -- the Win32 half -- over 127.0.0.1. Callers obtain it through
``create_space_detector()`` / ``get_yabai_detector()`` and use it exactly as
they use the yabai detector: same method names, same dict shapes
(``space_id``/``is_current``/``applications``/``windows[{app,title,id}]``).

Scope: the QUERY surface (enumeration, summaries, descriptions) plus capture.
yabai's window-moving surface (teleport, ghost display, boomerang) is not
mirrored; those attributes are absent, so ``hasattr`` probes degrade the same
way they do when yabai is not installed.

Mapping choices (Windows -> yabai vocabulary):
  * desktop number (1-based) -> ``space_id`` / raw ``index``
  * a pinned window (shown on every desktop) belongs to the CURRENT desktop,
    as yabai reports a sticky window on the focused space -- totals never
    double count it
  * Windows has no fullscreen desktops: ``is_fullscreen`` on a space is False;
    a window covering its monitor is ``is_fullscreen`` on the window
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import protocol as P

logger = logging.getLogger(__name__)

_SNAPSHOT_TTL_S = float(os.environ.get("JARVIS_DESKTOP_AGENT_SNAPSHOT_TTL_S", "1.0"))
_STATUS_TTL_S = float(os.environ.get("JARVIS_DESKTOP_AGENT_STATUS_TTL_S", "5.0"))
_QUERY_TIMEOUT_S = float(os.environ.get("JARVIS_DESKTOP_AGENT_TIMEOUT_S", "5.0"))
_CAPTURE_TIMEOUT_S = float(os.environ.get("JARVIS_DESKTOP_AGENT_CAPTURE_TIMEOUT_S", "65.0"))


class AgentError(Exception):
    """A daemon answer that is not a success, carrying its protocol code."""

    def __init__(self, code: str, detail: str = "", status: int = 0, payload: Optional[dict] = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.status, self.payload = code, detail, status, payload or {}


@dataclass
class CaptureResult:
    """One capture. ``ok`` False carries the daemon's classified error."""

    ok: bool
    png: bytes = b""
    meta: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    detail: str = ""

    def to_image(self):
        from PIL import Image

        return Image.open(io.BytesIO(self.png)).convert("RGB") if self.ok else None

    def to_array(self):
        """RGB ``np.ndarray`` (H, W, 3) -- the shape the capture engine stores."""
        import numpy as np

        img = self.to_image()
        return np.asarray(img) if img is not None else None


# --- pure mapping (no I/O; unit-tested) ---------------------------------------

def _owned_by(window: Dict[str, Any], current: int) -> Optional[int]:
    return current if window.get("pinned") else window.get("desktop")


def snapshot_to_spaces(snap: Dict[str, Any], include_display_info: bool = True) -> List[Dict[str, Any]]:
    """Daemon snapshot -> ``YabaiSpaceDetector.enumerate_all_spaces`` shape."""
    current = snap.get("current")
    spaces = []
    for d in snap.get("desktops", []):
        number = int(d["number"])
        members = sorted((w for w in snap.get("windows", []) if _owned_by(w, current) == number),
                         key=lambda w: w.get("z", 0))
        applications: List[str] = []
        for w in members:
            if w.get("app") and w["app"] not in applications:
                applications.append(w["app"])
        if not members:
            primary = "Empty"
        elif len(applications) == 1:
            primary = applications[0]
        else:
            primary = f"{applications[0]} and {len(applications) - 1} others"
        spaces.append({
            "space_id": number,
            "space_name": d.get("name") or f"Desktop {number}",
            "is_current": bool(d.get("is_current")),
            "is_visible": bool(d.get("is_current")),
            "is_fullscreen": False,
            "window_count": len(members),
            "window_ids": [w["hwnd"] for w in members],
            "applications": applications,
            "primary_activity": primary,
            "type": "virtual_desktop",
            "display": (members[0].get("display", 1) if members else 1) if include_display_info else None,
            "uuid": d.get("id", ""),
            "windows": [{
                "app": w.get("app", "Unknown"),
                "title": w.get("title", ""),
                "id": w["hwnd"],
                "minimized": bool(w.get("minimized")),
                "hidden": False,
                "is-native-fullscreen": bool(w.get("fullscreen")),
                "is_fullscreen": bool(w.get("fullscreen")),
                "can-move": True,
                "pinned": bool(w.get("pinned")),
            } for w in members],
        })
    return spaces


def snapshot_to_raw(snap: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    """Daemon snapshot -> ``yabai -m query --spaces|--windows`` JSON."""
    current = snap.get("current")
    if kind == "spaces":
        return [{
            "id": s["space_id"], "index": s["space_id"], "uuid": s["uuid"], "label": s["space_name"],
            "type": "float", "display": s["display"] or 1, "windows": s["window_ids"],
            "has-focus": s["is_current"], "is-visible": s["is_visible"],
            "is-native-fullscreen": False,
        } for s in snapshot_to_spaces(snap)]
    if kind == "windows":
        out = []
        for w in snap.get("windows", []):
            space = _owned_by(w, current)
            fr = w.get("frame", {})
            out.append({
                "id": w["hwnd"], "pid": w.get("pid", 0), "app": w.get("app", "Unknown"),
                "title": w.get("title", ""), "space": space, "display": w.get("display", 1),
                "frame": {"x": float(fr.get("x", 0)), "y": float(fr.get("y", 0)),
                          "w": float(fr.get("w", 0)), "h": float(fr.get("h", 0))},
                "has-focus": bool(w.get("focused")), "is-minimized": bool(w.get("minimized")),
                "is-hidden": False, "is-native-fullscreen": bool(w.get("fullscreen")),
                "zoom-fullscreen": False, "is-sticky": bool(w.get("pinned")),
                "is-visible": space == current and not w.get("minimized"),
            })
        return out
    raise ValueError(f"unknown query kind {kind!r}")


def summarize_spaces(spaces: List[Dict[str, Any]]) -> Dict[str, Any]:
    """``get_workspace_summary`` shape over already-enumerated spaces."""
    if not spaces:
        return {"total_spaces": 0, "total_windows": 0, "total_applications": 0, "spaces": [],
                "current_space": None, "primary_activity": "No spaces detected", "all_applications": []}
    app_counts: Dict[str, int] = {}
    for s in spaces:
        for app in s.get("applications", []):
            app_counts[app] = app_counts.get(app, 0) + 1
    return {
        "total_spaces": len(spaces),
        "total_windows": sum(s.get("window_count", 0) for s in spaces),
        "total_applications": len(app_counts),
        "spaces": spaces,
        "current_space": next((s for s in spaces if s.get("is_current")), None),
        "primary_activity": max(app_counts, key=app_counts.get) if app_counts else "Empty",
        "all_applications": list(app_counts),
    }


def describe_summary(summary: Dict[str, Any]) -> str:
    """Natural-language workspace description in Windows vocabulary."""
    if summary["total_spaces"] == 0:
        return "Unable to detect virtual desktops. The Windows desktop agent may not be running."
    parts = [f"You have {summary['total_spaces']} virtual desktops"]
    if summary["total_windows"] > 0:
        parts.append(f" with {summary['total_windows']} windows across "
                     f"{summary['total_applications']} applications.")
    else:
        parts.append(" with no windows currently open.")
    cur = summary.get("current_space")
    if cur:
        parts.append(f"\n\nCurrently viewing Desktop {cur['space_id']}")
        parts.append(f" with {cur['primary_activity']}." if cur["window_count"] else ", which is empty.")
    parts.append("\n\nDesktop breakdown:")
    for s in summary["spaces"]:
        line = f"\n• Desktop {s['space_id']}" + (" [CURRENT]" if s["is_current"] else "") + ": "
        if s["window_count"] == 0:
            line += "Empty"
        else:
            line += ", ".join(s["applications"][:3])
            if len(s["applications"]) > 3:
                line += f" and {len(s['applications']) - 3} more"
            title = s["windows"][0]["title"] if s["windows"] else ""
            if title:
                line += f' - "{title[:50]}{"..." if len(title) > 50 else ""}"'
        parts.append(line)
    return "".join(parts)


# --- the provider ---------------------------------------------------------------

class WindowsDesktopProvider:
    """Drop-in for ``YabaiSpaceDetector``'s query surface, backed by the Windows agent."""

    backend_name = "windows_agent"

    def __init__(self, enable_vision: bool = True, config: Any = None, auto_start: bool = True,
                 *, endpoint_loader: Callable[[], Optional[Dict[str, Any]]] = P.load_endpoint) -> None:
        # enable_vision / config / auto_start are accepted for signature parity.
        self.enable_vision = enable_vision
        self._load_endpoint = endpoint_loader
        self._endpoint: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._snap: Optional[Dict[str, Any]] = None
        self._snap_at = 0.0
        self._status = None
        self._status_at = 0.0
        self._last_error: Optional[str] = None
        self._callbacks: List[Callable[[Any], None]] = []
        self._monitor_task: Optional[asyncio.Task] = None
        self._health = {"successes": 0, "failures": 0, "last_success": None,
                        "last_failure": None, "last_latency_ms": None}

    # -- transport --------------------------------------------------------------
    def _resolve(self, refresh: bool = False) -> Dict[str, Any]:
        if refresh or self._endpoint is None:
            self._endpoint = self._load_endpoint()
        if not self._endpoint:
            raise AgentError("agent_not_running", "no endpoint.json found -- start daemon.py on Windows")
        return self._endpoint

    @staticmethod
    def _decode(status: int, ctype: str, headers: Dict[str, str], body: bytes) -> Tuple[Any, Dict[str, Any]]:
        if status == 200 and ctype.startswith("image/"):
            return body, json.loads(headers.get("X-Capture-Meta", "{}"))
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise AgentError(P.ERR_INTERNAL, f"non-JSON reply (HTTP {status})", status)
        if status != 200 or payload.get("ok") is False:
            raise AgentError(payload.get("error", P.ERR_INTERNAL), payload.get("detail", ""), status, payload)
        return payload, {}

    def _record(self, ok: bool, started: float, err: Optional[str] = None) -> None:
        now = time.time()
        if ok:
            self._health.update(successes=self._health["successes"] + 1, last_success=now,
                                last_latency_ms=round((time.monotonic() - started) * 1000, 1))
        else:
            self._health.update(failures=self._health["failures"] + 1, last_failure=now)
            self._last_error = err

    def _get_sync(self, path: str, timeout: float = _QUERY_TIMEOUT_S):
        for attempt in (0, 1):  # one retry after re-reading endpoint.json (daemon restarted)
            ep = self._resolve(refresh=attempt == 1)
            req = urllib.request.Request(f"http://127.0.0.1:{ep['port']}{path}",
                                         headers={P.TOKEN_HEADER: ep["token"]})
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    out = self._decode(r.status, r.headers.get("Content-Type", ""), dict(r.headers), r.read())
            except urllib.error.HTTPError as e:
                try:
                    out = self._decode(e.code, e.headers.get("Content-Type", ""), dict(e.headers), e.read())
                except AgentError as ae:
                    if ae.code == P.ERR_UNAUTHORIZED and attempt == 0:
                        continue
                    self._record(False, started, ae.code)
                    raise
            except (urllib.error.URLError, OSError) as e:
                if attempt == 0:
                    continue
                self._record(False, started, "agent_unreachable")
                raise AgentError("agent_unreachable", str(e))
            self._record(True, started)
            return out
        raise AgentError("agent_unreachable", "retry exhausted")

    async def _get_async(self, path: str, timeout: float = _QUERY_TIMEOUT_S):
        import aiohttp

        for attempt in (0, 1):
            ep = self._resolve(refresh=attempt == 1)
            started = time.monotonic()
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                    async with s.get(f"http://127.0.0.1:{ep['port']}{path}",
                                     headers={P.TOKEN_HEADER: ep["token"]}) as r:
                        body = await r.read()
                        out = self._decode(r.status, r.headers.get("Content-Type", ""), dict(r.headers), body)
            except AgentError as ae:
                if ae.code == P.ERR_UNAUTHORIZED and attempt == 0:
                    continue
                self._record(False, started, ae.code)
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                if attempt == 0:
                    continue
                self._record(False, started, "agent_unreachable")
                raise AgentError("agent_unreachable", f"{type(e).__name__}: {e}")
            self._record(True, started)
            return out
        raise AgentError("agent_unreachable", "retry exhausted")

    # -- snapshot cache ------------------------------------------------------------
    def _fresh(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._snap is not None and time.monotonic() - self._snap_at < _SNAPSHOT_TTL_S:
                return self._snap
        return None

    def _store(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._snap, self._snap_at = snap, time.monotonic()
        return snap

    def snapshot(self) -> Optional[Dict[str, Any]]:
        cached = self._fresh()
        if cached is not None:
            return cached
        try:
            return self._store(self._get_sync("/v1/snapshot")[0])
        except AgentError as e:
            logger.debug(f"[WIN-DESKTOP] snapshot failed: {e}")
            return None

    async def snapshot_async(self) -> Optional[Dict[str, Any]]:
        cached = self._fresh()
        if cached is not None:
            return cached
        try:
            return self._store((await self._get_async("/v1/snapshot"))[0])
        except AgentError as e:
            logger.debug(f"[WIN-DESKTOP] snapshot failed: {e}")
            return None

    # -- status (YabaiStatus vocabulary) ---------------------------------------------
    def get_status(self):
        from vision.yabai_space_detector import YabaiStatus

        if self._status is not None and time.monotonic() - self._status_at < _STATUS_TTL_S:
            return self._status
        try:
            self._get_sync("/v1/health", timeout=min(_QUERY_TIMEOUT_S, 2.0))
            status = YabaiStatus.AVAILABLE
        except AgentError as e:
            status = {"agent_not_running": YabaiStatus.NOT_RUNNING,
                      "agent_unreachable": YabaiStatus.NOT_RUNNING,
                      P.ERR_UNAUTHORIZED: YabaiStatus.NO_PERMISSIONS}.get(e.code, YabaiStatus.ERROR)
        if status != self._status:
            for cb in list(self._callbacks):
                try:
                    cb(status)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[WIN-DESKTOP] status callback error: {exc}")
        self._status, self._status_at = status, time.monotonic()
        return status

    def is_available(self) -> bool:
        from vision.yabai_space_detector import YabaiStatus

        return self.get_status() == YabaiStatus.AVAILABLE

    @property
    def yabai_available(self) -> bool:
        return self.is_available()

    def ensure_running(self) -> bool:
        # The daemon lives in the interactive Windows session; it cannot be started from here.
        return self.is_available()

    async def ensure_running_async(self) -> bool:
        return await asyncio.get_running_loop().run_in_executor(None, self.is_available)

    def register_status_callback(self, callback: Callable[[Any], None]) -> None:
        self._callbacks.append(callback)

    def get_health(self) -> Dict[str, Any]:
        return dict(self._health, last_error=self._last_error, backend=self.backend_name)

    def get_detailed_status(self) -> Dict[str, Any]:
        status = self.get_status()
        ep = self._endpoint or {}
        return {
            "status": status.value,
            "status_description": ("Windows desktop agent reachable" if self.is_available()
                                   else f"Windows desktop agent unavailable ({self._last_error})"),
            "health": self.get_health(),
            "installation": {"backend": self.backend_name, "endpoint_file": ep.get("_path"),
                             "agent_version": ep.get("version"), "port": ep.get("port")},
            "recommendations": [] if self.is_available() else [
                "Start the agent on Windows: uv run --no-project --python 3.11 --with-requirements "
                "backend\\vision\\windows_desktop\\requirements-windows.txt "
                "backend\\vision\\windows_desktop\\daemon.py"],
        }

    async def start_health_monitoring(self, interval_seconds: Optional[float] = None) -> None:
        if self._monitor_task and not self._monitor_task.done():
            return
        interval = interval_seconds or 30.0

        async def _loop() -> None:
            while True:
                self._status_at = 0.0  # force a probe
                await asyncio.get_running_loop().run_in_executor(None, self.get_status)
                await asyncio.sleep(interval)

        self._monitor_task = asyncio.create_task(_loop())

    async def stop_health_monitoring(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None

    # -- enumeration (sync) -------------------------------------------------------------
    def enumerate_all_spaces(self, include_display_info: bool = True, auto_start: bool = True) -> List[Dict[str, Any]]:
        snap = self.snapshot()
        return snapshot_to_spaces(snap, include_display_info) if snap else []

    def get_current_space(self) -> Optional[Dict[str, Any]]:
        return next((s for s in self.enumerate_all_spaces() if s["is_current"]), None)

    def get_space_info(self, space_id: int) -> Optional[Dict[str, Any]]:
        return next((s for s in self.enumerate_all_spaces() if s["space_id"] == int(space_id)), None)

    def get_space_count(self) -> int:
        return len(self.enumerate_all_spaces())

    def get_windows_for_space(self, space_id: int) -> List[Dict[str, Any]]:
        info = self.get_space_info(space_id)
        return info["windows"] if info else []

    def get_display_for_space(self, space_id: int) -> Optional[int]:
        info = self.get_space_info(space_id)
        return info["display"] if info else None

    def enumerate_spaces_by_display(self) -> Dict[int, List[Dict[str, Any]]]:
        by_display: Dict[int, List[Dict[str, Any]]] = {}
        for s in self.enumerate_all_spaces(include_display_info=True):
            by_display.setdefault(s["display"] or 1, []).append(s)
        return by_display

    def get_current_user_space(self) -> Optional[int]:
        cur = self.get_current_space()
        return cur["space_id"] if cur else None

    def get_workspace_summary(self) -> Dict[str, Any]:
        return summarize_spaces(self.enumerate_all_spaces())

    def describe_workspace(self) -> str:
        return describe_summary(self.get_workspace_summary())

    # -- enumeration (async) --------------------------------------------------------------
    async def enumerate_all_spaces_async(self, include_display_info: bool = True) -> List[Dict[str, Any]]:
        snap = await self.snapshot_async()
        return snapshot_to_spaces(snap, include_display_info) if snap else []

    async def find_windows_on_space_async(self, space_id: int) -> List[Dict[str, Any]]:
        spaces = await self.enumerate_all_spaces_async()
        return next((s["windows"] for s in spaces if s["space_id"] == int(space_id)), [])

    async def get_workspace_summary_async(self) -> Dict[str, Any]:
        return summarize_spaces(await self.enumerate_all_spaces_async())

    async def describe_workspace_async(self) -> str:
        return describe_summary(await self.get_workspace_summary_async())

    async def query_raw_async(self, kind: str) -> List[Dict[str, Any]]:
        """``yabai -m query --<kind>`` equivalent; raises AgentError when unreachable."""
        snap = await self.snapshot_async()
        if snap is None:
            raise AgentError("agent_unreachable", self._last_error or "no snapshot")
        return snapshot_to_raw(snap, kind)

    # -- capture -------------------------------------------------------------------------
    async def _capture(self, path: str) -> CaptureResult:
        try:
            png, meta = await self._get_async(path, timeout=_CAPTURE_TIMEOUT_S)
            return CaptureResult(ok=True, png=png, meta=meta)
        except AgentError as e:
            return CaptureResult(ok=False, error=e.code, detail=e.detail, meta=e.payload)

    async def capture_window_async(self, window_id: int, max_dim: Optional[int] = None) -> CaptureResult:
        """Render one window -- on ANY desktop, without switching to it."""
        q = f"?max_dim={int(max_dim)}" if max_dim else ""
        return await self._capture(f"/v1/windows/{int(window_id)}/capture{q}")

    async def capture_space_async(self, space_id: int, max_dim: Optional[int] = None) -> CaptureResult:
        """Composite of one desktop's windows in z-order, without switching to it."""
        q = f"?max_dim={int(max_dim)}" if max_dim else ""
        return await self._capture(f"/v1/spaces/{int(space_id)}/capture{q}")
