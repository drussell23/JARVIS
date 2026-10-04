"""Windows desktop agent: virtual desktops + off-desktop window capture over loopback.

Runs NATIVELY on the Windows host, inside the interactive (console or RDP)
session whose desktops it reports -- a service in session 0 has no desktop
to see, and WSL cannot make Win32 UI calls at all. The WSL side reaches it
at 127.0.0.1 through mirrored networking (see ``provider.py``).

    uv run --no-project --python 3.11 --with-requirements ^
        backend\\vision\\windows_desktop\\requirements-windows.txt ^
        backend\\vision\\windows_desktop\\daemon.py

Why this exists: macOS cannot render a window on another Space without
switching to it. Windows can -- a window on a non-current virtual desktop is
only CLOAKED by the shell (DWM keeps its surface), and
``PrintWindow(PW_RENDERFULLCONTENT)`` asks the window to render into our
DC. Measured 2026-10-04 on build 26300 inside an RDP session: a Notepad
window moved to desktop 2 captured cleanly while the view stayed on 1.

Endpoints (all GET, all require ``protocol.TOKEN_HEADER``):
  /v1/health                         session state + desktop count
  /v1/snapshot                       desktops and their windows (neutral shape)
  /v1/windows/{hwnd}/capture         PNG of one window   [?max_dim=N]
  /v1/spaces/{number}/capture        PNG composite of one desktop [?max_dim=N]
Captures return ``image/png`` with an ``X-Capture-Meta`` JSON header; every
failure is a JSON ``{"ok": false, "error": <protocol.ERR_*>, ...}`` body.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sys
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import protocol as P  # noqa: E402  (stdlib-only sibling, see protocol.py)

from aiohttp import web  # noqa: E402

log = logging.getLogger("desktop_agent")
VERSION = "1.0.0"

# --- Win32 constants -------------------------------------------------------
PW_RENDERFULLCONTENT = 0x2
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
DWM_CLOAKED_APP = 0x1
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WTS_CONNECTSTATE = {0: "active", 1: "connected", 2: "connect_query", 3: "shadow",
                    4: "disconnected", 5: "idle", 6: "listen", 7: "reset",
                    8: "down", 9: "init"}
BLANK_LUMA_MAX = 8  # a frame whose brightest pixel is below this is black


class CaptureError(Exception):
    """A classified capture failure -> one JSON error payload."""

    def __init__(self, code: str, detail: str, status: int = 409, **extra: Any):
        super().__init__(detail)
        self.code, self.detail, self.status, self.extra = code, detail, status, extra


def _hresult(exc: BaseException) -> Optional[str]:
    """Best-effort HRESULT / Win32 error code from any Win32-ish exception."""
    for attr in ("hresult", "winerror"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return hex(val & 0xFFFFFFFF)
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return hex(args[0] & 0xFFFFFFFF)
    return None


class DesktopBackend:
    """Every Win32/COM call. Instantiated and used on ONE thread only."""

    def __init__(self) -> None:
        import comtypes  # COM must be initialised on the thread that uses it

        comtypes.CoInitialize()
        import pyvda
        import win32api
        import win32con
        import win32gui
        import win32process
        import win32ts
        import win32ui
        from PIL import Image

        self.pyvda, self.Image = pyvda, Image
        self.win32api, self.win32con, self.win32gui = win32api, win32con, win32gui
        self.win32process, self.win32ts, self.win32ui = win32process, win32ts, win32ui
        self.user32 = ctypes.windll.user32
        self.dwm = ctypes.windll.dwmapi
        # Physical pixels everywhere: per-monitor-v2 DPI awareness.
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:  # noqa: BLE001 -- older builds
            self.user32.SetProcessDPIAware()
        self._app_names: Dict[str, str] = {}

    # --- session ----------------------------------------------------------
    def session(self) -> Dict[str, Any]:
        ts = self.win32ts
        sid = ts.ProcessIdToSessionId(os.getpid())
        info: Dict[str, Any] = {"id": sid}
        try:
            state = ts.WTSQuerySessionInformation(
                ts.WTS_CURRENT_SERVER_HANDLE, ts.WTS_CURRENT_SESSION, ts.WTSConnectState)
            info["state"] = WTS_CONNECTSTATE.get(int(state), str(state))
        except Exception as exc:  # noqa: BLE001
            info["state"] = "unknown"
            info["state_error"] = f"{type(exc).__name__}: {exc}"
        try:
            proto = ts.WTSQuerySessionInformation(
                ts.WTS_CURRENT_SERVER_HANDLE, ts.WTS_CURRENT_SESSION, ts.WTSClientProtocolType)
            info["remote"] = int(proto) == 2  # 0 console, 2 RDP
        except Exception:  # noqa: BLE001
            info["remote"] = None
        return info

    # --- enumeration ------------------------------------------------------
    def _cloaked(self, hwnd: int) -> int:
        val = wintypes.DWORD()
        self.dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED,
                                       ctypes.byref(val), ctypes.sizeof(val))
        return int(val.value)

    def _frame(self, hwnd: int) -> Dict[str, int]:
        """Visible frame (DWM extended bounds: no invisible resize border)."""
        rect = wintypes.RECT()
        hr = self.dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                                            ctypes.byref(rect), ctypes.sizeof(rect))
        if hr != 0:
            l, t, r, b = self.win32gui.GetWindowRect(hwnd)
        else:
            l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
        return {"x": l, "y": t, "w": r - l, "h": b - t}

    def _app_name(self, hwnd: int) -> Tuple[str, str, int]:
        """(friendly app name, exe basename, pid). UWP frames resolve to the child app."""
        _, pid = self.win32process.GetWindowThreadProcessId(hwnd)
        exe = self._exe_path(pid)
        if exe and os.path.basename(exe).lower() == "applicationframehost.exe":
            children: List[int] = []
            self.win32gui.EnumChildWindows(hwnd, lambda h, _: children.append(h) or True, None)
            for child in children:
                _, cpid = self.win32process.GetWindowThreadProcessId(child)
                if cpid != pid:
                    pid, exe = cpid, self._exe_path(cpid)
                    break
        if not exe:
            return "Unknown", "", pid
        if exe not in self._app_names:
            name = os.path.splitext(os.path.basename(exe))[0]
            try:
                (lang, cp), *_ = self.win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation")
                desc = self.win32api.GetFileVersionInfo(
                    exe, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription")
                if desc and desc.strip():
                    name = desc.strip()
            except Exception:  # noqa: BLE001 -- no version resource
                pass
            self._app_names[exe] = name
        return self._app_names[exe], os.path.basename(exe), pid

    def _exe_path(self, pid: int) -> str:
        try:
            h = self.win32api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            try:
                return self.win32process.GetModuleFileNameEx(h, None)
            finally:
                self.win32api.CloseHandle(h)
        except Exception:  # noqa: BLE001 -- protected / exited process
            return ""

    def _monitors(self) -> List[Tuple[int, Tuple[int, int, int, int]]]:
        mons = []
        for i, (hmon, _, rect) in enumerate(self.win32api.EnumDisplayMonitors(), start=1):
            mons.append((int(hmon), rect))
        return mons

    def snapshot(self) -> Dict[str, Any]:
        vda, gui = self.pyvda, self.win32gui
        desktops = vda.get_virtual_desktops()
        current = vda.VirtualDesktop.current().number
        monitors = self._monitors()
        mon_index = {h: i for i, (h, _) in enumerate(monitors, start=1)}
        foreground = gui.GetForegroundWindow()
        hwnds: List[int] = []
        gui.EnumWindows(lambda h, _: hwnds.append(h) or True, None)  # top of z-order first

        windows, skipped = [], 0
        for z, hwnd in enumerate(hwnds):
            try:
                if not gui.IsWindowVisible(hwnd) or gui.GetWindow(hwnd, GW_OWNER):
                    continue
                title = gui.GetWindowText(hwnd)
                if not title or gui.GetWindowLong(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                    continue
                cloaked = self._cloaked(hwnd)
                if cloaked & DWM_CLOAKED_APP:  # app-cloaked: suspended UWP frame etc.
                    continue
                try:
                    view = vda.AppView(hwnd)
                    pinned = bool(view.is_pinned() or view.is_app_pinned())
                    number = None if pinned else view.desktop.number
                except Exception:  # noqa: BLE001 -- not a tracked application view
                    skipped += 1
                    continue
                app, exe, pid = self._app_name(hwnd)
                frame = self._frame(hwnd)
                hmon = self.win32api.MonitorFromWindow(hwnd, 2)  # NEAREST
                mrect = dict(monitors).get(int(hmon))
                fullscreen = bool(mrect) and (frame["x"], frame["y"], frame["x"] + frame["w"],
                                              frame["y"] + frame["h"]) == tuple(mrect)
                windows.append({
                    "hwnd": hwnd, "desktop": number, "pinned": pinned, "title": title,
                    "app": app, "exe": exe, "pid": pid, "z": z,
                    "minimized": bool(gui.IsIconic(hwnd)), "fullscreen": fullscreen,
                    "focused": hwnd == foreground, "cloaked": cloaked,
                    "display": mon_index.get(int(hmon), 1), "frame": frame,
                })
            except Exception as exc:  # noqa: BLE001 -- window vanished mid-walk
                log.debug("skip hwnd %s: %s", hwnd, exc)
                skipped += 1

        out_desktops = []
        for d in desktops:
            try:
                name = d.name
            except Exception:  # noqa: BLE001 -- name API absent on some builds
                name = ""
            out_desktops.append({"number": d.number, "id": str(d.id), "name": name or "",
                                 "is_current": d.number == current})
        return {"ok": True, "current": current, "desktops": out_desktops,
                "windows": windows, "skipped": skipped, "displays": len(monitors),
                "session": self.session(), "timestamp": time.time()}

    # --- capture ------------------------------------------------------------
    def _render(self, hwnd: int):
        """PrintWindow into a memory DC; returns a PIL image cropped to the visible frame."""
        gui, ui = self.win32gui, self.win32ui
        l, t, r, b = gui.GetWindowRect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            raise CaptureError(P.ERR_DC_UNAVAILABLE, f"window has no area ({w}x{h})")
        hdc = src = mem = bmp = None
        try:
            hdc = gui.GetWindowDC(hwnd)
            if not hdc:
                raise CaptureError(P.ERR_DC_UNAVAILABLE, "GetWindowDC returned NULL")
            src = ui.CreateDCFromHandle(hdc)
            mem = src.CreateCompatibleDC()
            bmp = ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(src, w, h)
            mem.SelectObject(bmp)
            if not self.user32.PrintWindow(wintypes.HWND(hwnd), mem.GetSafeHdc(), PW_RENDERFULLCONTENT):
                raise CaptureError(P.ERR_PRINTWINDOW_FAILED, "PrintWindow returned 0",
                                   last_error=ctypes.GetLastError())
            info = bmp.GetInfo()
            img = self.Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                        bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        finally:
            if bmp is not None:
                gui.DeleteObject(bmp.GetHandle())
            if mem is not None:
                mem.DeleteDC()
            if src is not None:
                src.DeleteDC()
            if hdc:
                gui.ReleaseDC(hwnd, hdc)
        fr = self._frame(hwnd)
        box = (fr["x"] - l, fr["y"] - t, fr["x"] - l + fr["w"], fr["y"] - t + fr["h"])
        if 0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h:
            img = img.crop(box)
        return img

    def _guard(self, hwnd: int) -> None:
        gui = self.win32gui
        if not gui.IsWindow(hwnd):
            raise CaptureError(P.ERR_WINDOW_NOT_FOUND, f"no window {hwnd}", status=404)
        if gui.IsIconic(hwnd):
            raise CaptureError(P.ERR_WINDOW_MINIMIZED, "window is minimized; nothing is rendered")
        # PrintWindow sends WM_PRINT to the window's own thread: a hung app
        # would block this (single) worker forever. Refuse up front.
        if self.user32.IsHungAppWindow(wintypes.HWND(hwnd)):
            raise CaptureError(P.ERR_WINDOW_NOT_RESPONDING, "window is not responding")

    def _classify_blank(self, img, what: str) -> Dict[str, Any]:
        lo, hi = img.convert("L").getextrema()
        if hi < BLANK_LUMA_MAX:
            sess = self.session()
            if sess.get("state") != "active":
                raise CaptureError(P.ERR_SESSION_DISCONNECTED,
                                   f"{what} rendered black and the session is {sess.get('state')}",
                                   status=503, session=sess)
            raise CaptureError(P.ERR_BLANK_FRAME, f"{what} rendered fully black", session=sess)
        return {"luma": [lo, hi]}

    def capture_window(self, hwnd: int, max_dim: Optional[int]) -> Tuple[bytes, Dict[str, Any]]:
        self._guard(hwnd)
        img = self._render(hwnd)
        meta = self._classify_blank(img, f"window {hwnd}")
        meta.update({"hwnd": hwnd, "cloaked": self._cloaked(hwnd),
                     "frame_sha1": hashlib.sha1(img.tobytes()).hexdigest()})
        return self._encode(img, max_dim, meta)

    def capture_desktop(self, number: int, max_dim: Optional[int]) -> Tuple[bytes, Dict[str, Any]]:
        snap = self.snapshot()
        if number not in {d["number"] for d in snap["desktops"]}:
            raise CaptureError(P.ERR_SPACE_NOT_FOUND, f"no desktop {number}", status=404)
        sm = self.win32api.GetSystemMetrics
        ox, oy, vw, vh = sm(76), sm(77), sm(78), sm(79)  # virtual screen
        canvas = self.Image.new("RGB", (vw, vh))
        members = [w for w in snap["windows"]
                   if (w["desktop"] == number or w["pinned"]) and not w["minimized"]]
        drawn, skipped = [], []
        for w in sorted(members, key=lambda w: -w["z"]):  # bottom of z-order first
            try:
                self._guard(w["hwnd"])
                img = self._render(w["hwnd"])
                canvas.paste(img, (w["frame"]["x"] - ox, w["frame"]["y"] - oy))
                drawn.append(w["hwnd"])
            except CaptureError as exc:
                skipped.append({"hwnd": w["hwnd"], "error": exc.code})
            except Exception as exc:  # noqa: BLE001
                skipped.append({"hwnd": w["hwnd"], "error": P.ERR_WIN32, "hresult": _hresult(exc)})
        if members and not drawn:
            raise CaptureError(skipped[0]["error"], f"no window on desktop {number} could be rendered",
                               skipped=skipped, session=snap["session"])
        meta: Dict[str, Any] = {"desktop": number, "drawn": drawn, "skipped": skipped,
                                "origin": [ox, oy], "empty": not members}
        if drawn:
            meta.update(self._classify_blank(canvas, f"desktop {number}"))
        return self._encode(canvas, max_dim, meta)

    @staticmethod
    def _encode(img, max_dim: Optional[int], meta: Dict[str, Any]) -> Tuple[bytes, Dict[str, Any]]:
        meta["size"] = list(img.size)
        if max_dim and max(img.size) > max_dim:
            img = img.copy()
            img.thumbnail((max_dim, max_dim))
            meta["scaled_to"] = list(img.size)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=False)
        meta["captured_at"] = time.time()
        return buf.getvalue(), meta


class Win32Worker:
    """Serialises every DesktopBackend call onto one COM-initialised thread."""

    def __init__(self) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="win32")
        self.backend: DesktopBackend = self._pool.submit(DesktopBackend).result()

    async def call(self, name: str, *args: Any, timeout: float = 20.0) -> Any:
        loop = asyncio.get_running_loop()
        fn = getattr(self.backend, name)
        return await asyncio.wait_for(loop.run_in_executor(self._pool, fn, *args), timeout)


def _json_error(code: str, detail: str, status: int, **extra: Any) -> web.Response:
    body = {"ok": False, "error": code, "detail": detail}
    body.update(extra)
    return web.json_response(body, status=status)


def build_app(worker: Win32Worker, token: str) -> web.Application:
    @web.middleware
    async def guard(request: web.Request, handler):
        supplied = request.headers.get(P.TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return _json_error(P.ERR_UNAUTHORIZED, "missing or wrong token", 401)
        try:
            return await handler(request)
        except web.HTTPException as exc:
            return _json_error(P.ERR_NOT_FOUND, exc.reason, exc.status)
        except CaptureError as exc:
            return _json_error(exc.code, exc.detail, exc.status, **exc.extra)
        except asyncio.TimeoutError:
            return _json_error(P.ERR_WINDOW_NOT_RESPONDING, "Win32 call timed out", 504)
        except Exception as exc:  # noqa: BLE001 -- never let a request kill the daemon
            log.exception("request failed: %s", request.path)
            code = P.ERR_WIN32 if _hresult(exc) else P.ERR_INTERNAL
            return _json_error(code, f"{type(exc).__name__}: {exc}", 500, hresult=_hresult(exc))

    def _max_dim(request: web.Request) -> Optional[int]:
        raw = request.query.get("max_dim")
        return max(64, int(raw)) if raw and raw.isdigit() else None

    def _png(png: bytes, meta: Dict[str, Any]) -> web.Response:
        return web.Response(body=png, content_type="image/png",
                            headers={"X-Capture-Meta": json.dumps(meta, separators=(",", ":"))})

    async def health(_: web.Request) -> web.Response:
        snap = await worker.call("snapshot")
        return web.json_response({"ok": True, "version": VERSION, "protocol": P.PROTOCOL_VERSION,
                                  "pid": os.getpid(), "session": snap["session"],
                                  "desktops": len(snap["desktops"]), "current": snap["current"]})

    async def snapshot(_: web.Request) -> web.Response:
        return web.json_response(await worker.call("snapshot"))

    async def capture_window(request: web.Request) -> web.Response:
        hwnd = int(request.match_info["hwnd"])
        return _png(*await worker.call("capture_window", hwnd, _max_dim(request)))

    async def capture_space(request: web.Request) -> web.Response:
        number = int(request.match_info["number"])
        return _png(*await worker.call("capture_desktop", number, _max_dim(request), timeout=60.0))

    app = web.Application(middlewares=[guard])
    app.router.add_get("/v1/health", health)
    app.router.add_get("/v1/snapshot", snapshot)
    app.router.add_get(r"/v1/windows/{hwnd:\d+}/capture", capture_window)
    app.router.add_get(r"/v1/spaces/{number:\d+}/capture", capture_space)
    return app


def _write_endpoint(path: str, record: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.replace(tmp, path)


async def _serve(port: int) -> None:
    worker = Win32Worker()
    token = secrets.token_urlsafe(32)
    runner = web.AppRunner(build_app(worker, token), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError as exc:
        log.error("cannot bind 127.0.0.1:%s (%s) -- is another agent already running?", port, exc)
        await runner.cleanup()
        raise SystemExit(3)

    path = P.windows_endpoint_path()
    record = {"port": port, "token": token, "pid": os.getpid(), "version": VERSION,
              "protocol": P.PROTOCOL_VERSION, "started_at": time.time()}
    _write_endpoint(path, record)
    sess = await worker.call("session")
    log.info("desktop agent %s on 127.0.0.1:%s (session %s, %s) endpoint=%s",
             VERSION, port, sess.get("id"), sess.get("state"), path)
    try:
        await asyncio.Event().wait()
    finally:
        try:  # remove the record only if it is still ours
            with open(path, "r", encoding="utf-8") as fh:
                if json.load(fh).get("pid") == os.getpid():
                    os.remove(path)
        except (OSError, ValueError):
            pass
        await runner.cleanup()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("desktop agent must run natively on Windows (not WSL)")
    logging.basicConfig(level=os.environ.get("JARVIS_DESKTOP_AGENT_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(_serve(P.DEFAULT_PORT))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
