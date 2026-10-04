"""Windows desktop backend: pure mapping, provider transport, wiring, and the
space-id regression in MultiSpaceQueryHandler.

The provider is driven against an in-process aiohttp stand-in for daemon.py,
so this runs on any host (the real daemon needs Windows; see daemon.py).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

BACKEND = os.path.join(os.path.dirname(__file__), "..", "..", "backend")
if os.path.abspath(BACKEND) not in [os.path.abspath(p) for p in sys.path]:
    sys.path.insert(0, os.path.abspath(BACKEND))

from aiohttp import web  # noqa: E402

from vision import windows_desktop as wd  # noqa: E402
from vision.windows_desktop import protocol as P  # noqa: E402
from vision.windows_desktop.provider import (  # noqa: E402
    WindowsDesktopProvider,
    describe_summary,
    snapshot_to_raw,
    snapshot_to_spaces,
    summarize_spaces,
)


def _win(hwnd, desktop, app, title, z, **kw):
    w = {"hwnd": hwnd, "desktop": desktop, "pinned": False, "title": title, "app": app,
         "exe": f"{app}.exe", "pid": hwnd + 1, "z": z, "minimized": False, "fullscreen": False,
         "focused": False, "cloaked": 0 if desktop == 1 else 2, "display": 1,
         "frame": {"x": 10, "y": 20, "w": 300, "h": 200}}
    w.update(kw)
    return w


SNAP = {
    "ok": True, "current": 1, "skipped": 0, "displays": 1, "session": {"id": 3, "state": "active"},
    "desktops": [{"number": 1, "id": "{A}", "name": "", "is_current": True},
                 {"number": 2, "id": "{B}", "name": "Work", "is_current": False},
                 {"number": 3, "id": "{C}", "name": "", "is_current": False}],
    "windows": [
        _win(101, 1, "Visual Studio Code", "daemon.py", 0, focused=True),
        _win(102, 1, "Firefox", "docs", 2),
        _win(201, 2, "Notepad", "notes.txt", 1),
        _win(202, 2, "Notepad", "todo.txt", 3, minimized=True),
        _win(301, None, "Spotify", "music", 4, pinned=True),
    ],
}


# --- pure mapping ---------------------------------------------------------------

def test_spaces_match_yabai_shape_and_pinned_belongs_to_current():
    spaces = snapshot_to_spaces(SNAP)
    assert [s["space_id"] for s in spaces] == [1, 2, 3]
    d1, d2, d3 = spaces
    assert d1["is_current"] and not d2["is_current"]
    assert d1["window_ids"] == [101, 102, 301]          # pinned counted once, on current
    assert d1["applications"] == ["Visual Studio Code", "Firefox", "Spotify"]
    assert d1["primary_activity"] == "Visual Studio Code and 2 others"
    assert d2["space_name"] == "Work" and d3["space_name"] == "Desktop 3"
    assert d2["applications"] == ["Notepad"] and d2["primary_activity"] == "Notepad"
    assert d3["window_count"] == 0 and d3["primary_activity"] == "Empty"
    win = d2["windows"][1]
    assert set(win) >= {"app", "title", "id", "minimized", "hidden", "is_fullscreen", "can-move"}
    assert win["minimized"] is True


def test_raw_schema_is_what_space_state_manager_reads():
    spaces = snapshot_to_raw(SNAP, "spaces")
    assert [(s["index"], s["has-focus"]) for s in spaces] == [(1, True), (2, False), (3, False)]
    wins = {w["id"]: w for w in snapshot_to_raw(SNAP, "windows")}
    assert wins[201]["space"] == 2 and wins[301]["space"] == 1 and wins[301]["is-sticky"]
    assert wins[202]["is-minimized"] and not wins[202]["is-visible"]
    assert wins[101]["frame"] == {"x": 10.0, "y": 20.0, "w": 300.0, "h": 200.0}
    with pytest.raises(ValueError):
        snapshot_to_raw(SNAP, "displays")


def test_summary_and_description():
    summary = summarize_spaces(snapshot_to_spaces(SNAP))
    assert summary["total_spaces"] == 3 and summary["total_windows"] == 5
    assert summary["current_space"]["space_id"] == 1
    text = describe_summary(summary)
    assert "3 virtual desktops" in text and "Desktop 2: Notepad" in text and "Desktop 3: Empty" in text
    assert summarize_spaces([])["primary_activity"] == "No spaces detected"
    assert "not be running" in describe_summary(summarize_spaces([]))


# --- backend selection ------------------------------------------------------------

def test_space_backend_selection(monkeypatch):
    monkeypatch.setenv("JARVIS_SPACE_BACKEND", "yabai")
    assert wd.space_backend() == "yabai"
    monkeypatch.setenv("JARVIS_SPACE_BACKEND", "windows_agent")
    assert wd.space_backend() == "windows_agent"
    assert isinstance(wd.create_space_detector(enable_vision=False), WindowsDesktopProvider)
    monkeypatch.setenv("JARVIS_SPACE_BACKEND", "auto")
    monkeypatch.setattr(wd, "_running_under_wsl", lambda: False)
    monkeypatch.setattr(wd.sys, "platform", "darwin")
    assert wd.space_backend() == "yabai"
    monkeypatch.setattr(wd, "_running_under_wsl", lambda: True)
    assert wd.space_backend() == "windows_agent"


# --- provider against a stand-in daemon --------------------------------------------

class FakeAgent:
    """Speaks daemon.py's wire contract: token header, JSON errors, PNG + meta."""

    def __init__(self, token="t0"):
        self.token = token
        self.snapshot_calls = 0

    def app(self):
        async def guard_check(request):
            if request.headers.get(P.TOKEN_HEADER) != self.token:
                return web.json_response({"ok": False, "error": P.ERR_UNAUTHORIZED}, status=401)
            return None

        async def health(request):
            return await guard_check(request) or web.json_response({"ok": True, "current": 1})

        async def snapshot(request):
            self.snapshot_calls += 1
            return await guard_check(request) or web.json_response(SNAP)

        async def capture(request):
            denied = await guard_check(request)
            if denied:
                return denied
            hwnd = int(request.match_info["hwnd"])
            if hwnd == 202:
                return web.json_response({"ok": False, "error": P.ERR_WINDOW_MINIMIZED,
                                          "detail": "minimized"}, status=409)
            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (4, 3), (200, 10, 10)).save(buf, "PNG")
            return web.Response(body=buf.getvalue(), content_type="image/png",
                                headers={"X-Capture-Meta": json.dumps({"hwnd": hwnd, "luma": [10, 90]})})

        app = web.Application()
        app.router.add_get("/v1/health", health)
        app.router.add_get("/v1/snapshot", snapshot)
        app.router.add_get(r"/v1/windows/{hwnd:\d+}/capture", capture)
        return app


@pytest.fixture
def agent_env():
    """Run FakeAgent on an ephemeral loopback port inside a private loop thread."""
    import threading

    agent = FakeAgent()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state = {}

    async def start():
        runner = web.AppRunner(agent.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        state["runner"] = runner
        state["port"] = site._server.sockets[0].getsockname()[1]
        ready.set()

    t = threading.Thread(target=lambda: (loop.run_until_complete(start()), loop.run_forever()), daemon=True)
    t.start()
    assert ready.wait(10)
    endpoint = {"port": state["port"], "token": agent.token, "started_at": 1.0}
    yield agent, endpoint
    asyncio.run_coroutine_threadsafe(state["runner"].cleanup(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)


def test_provider_sync_surface(agent_env, monkeypatch):
    monkeypatch.setattr("vision.windows_desktop.provider._SNAPSHOT_TTL_S", 60.0)
    agent, endpoint = agent_env
    prov = WindowsDesktopProvider(endpoint_loader=lambda: dict(endpoint))
    assert prov.is_available()
    assert prov.get_current_user_space() == 1
    assert [w["id"] for w in prov.get_windows_for_space(2)] == [201, 202]
    assert prov.get_windows_for_space("2") == prov.get_windows_for_space(2)
    assert prov.get_space_count() == 3
    assert "Desktop 2: Notepad" in prov.describe_workspace()
    assert agent.snapshot_calls == 1                     # one snapshot served every lookup


def test_provider_async_surface_and_capture(agent_env):
    agent, endpoint = agent_env
    prov = WindowsDesktopProvider(endpoint_loader=lambda: dict(endpoint))

    async def go():
        spaces = await prov.enumerate_all_spaces_async()
        raw = await prov.query_raw_async("windows")
        ok = await prov.capture_window_async(201, max_dim=800)
        bad = await prov.capture_window_async(202)
        return spaces, raw, ok, bad

    spaces, raw, ok, bad = asyncio.run(go())
    assert len(spaces) == 3 and any(w["space"] == 2 for w in raw)
    assert ok.ok and ok.meta["hwnd"] == 201 and ok.to_array().shape == (3, 4, 3)
    assert not bad.ok and bad.error == P.ERR_WINDOW_MINIMIZED and bad.to_array() is None


def test_provider_rereads_endpoint_after_daemon_restart(agent_env):
    agent, endpoint = agent_env
    stale = dict(endpoint, token="old-token")
    reads = iter([stale, dict(endpoint)])
    prov = WindowsDesktopProvider(endpoint_loader=lambda: next(reads))
    assert prov.get_space_count() == 3                  # 401 -> re-read endpoint.json -> retry


def test_provider_unavailable_degrades_like_missing_yabai():
    from vision.yabai_space_detector import YabaiStatus

    prov = WindowsDesktopProvider(endpoint_loader=lambda: None)
    assert prov.get_status() == YabaiStatus.NOT_RUNNING and not prov.is_available()
    assert prov.enumerate_all_spaces() == [] and prov.get_current_space() is None
    assert prov.get_workspace_summary()["total_spaces"] == 0
    assert asyncio.run(prov.capture_space_async(2)).error == "agent_not_running"
    assert not hasattr(prov, "move_window_to_space")      # mutation surface is not mirrored


# --- query handler space-id regression ---------------------------------------------

def test_space_ids_normalized_once():
    from context_intelligence.handlers.multi_space_query_handler import normalize_space_ids

    assert normalize_space_ids(["2", 2, 3, "x", None, " 4 "]) == [2, 3, 4]
    assert normalize_space_ids(None) == []


def test_core_graphics_source_reaches_the_aggregate():
    """str(space_id) against int keys meant this source never contributed (any platform)."""
    from context_intelligence.handlers.multi_space_query_handler import MultiSpaceQueryHandler

    windows = [SimpleNamespace(space_id=2, app_name="Notepad", window_title="notes.txt"),
               SimpleNamespace(space_id=1, app_name="Firefox", window_title="docs"),
               SimpleNamespace(space_id=None, app_name="Ghost", window_title="")]
    cg = SimpleNamespace(get_all_windows_across_spaces=lambda: {
        "spaces": {2: {"space_id": 2, "windows": [{"kCGWindowOwnerName": "Notepad"}]}},
        "windows": windows})
    handler = MultiSpaceQueryHandler(cg_window_detector=cg)
    data = asyncio.run(handler._aggregate_space_data(2))
    assert data["sources_used"] == ["core_graphics"]
    assert data["windows"] == [{"app": "Notepad", "title": "notes.txt", "source": "cg"}]


def test_space_state_manager_routes_raw_queries_to_the_agent(monkeypatch):
    from context_intelligence.managers import space_state_manager as ssm
    import vision.yabai_space_detector as ysd

    class Stub:
        async def query_raw_async(self, kind):
            return snapshot_to_raw(SNAP, kind)

    monkeypatch.setenv("JARVIS_SPACE_BACKEND", "windows_agent")
    monkeypatch.setattr(ysd, "_yabai_detector", Stub())
    validator = ssm.SpaceValidator()

    async def go():
        return (await validator.validate_space_exists(2),
                await validator.validate_space_exists(9),
                await validator.get_space_window_states(2))

    exists2, exists9, states = asyncio.run(go())
    assert exists2 == (True, 3) and exists9 == (False, 3)
    assert [(w.id, w.state.name) for w in states] == [(201, "VISIBLE"), (202, "MINIMIZED")]


def test_query_resolution_uses_desktop_names_and_real_spaces():
    """'desktop N' is a space; no named space means the spaces that exist, not 1..10."""
    from context_intelligence.handlers.multi_space_query_handler import (
        MultiSpaceQueryHandler,
        MultiSpaceQueryType as T,
    )

    class Detector:
        async def enumerate_all_spaces_async(self):
            return snapshot_to_spaces(SNAP)

    h = MultiSpaceQueryHandler(yabai_detector=Detector())

    async def resolve(q):
        kind = await h._classify_query_type(q)
        return kind, await h._resolve_spaces(q, kind, None)

    assert asyncio.run(resolve("what's on desktop 2")) == (T.SUMMARY, [2])
    assert asyncio.run(resolve("what's on space 2")) == (T.SUMMARY, [2])
    assert asyncio.run(resolve("compare desktop 1 and desktop 2"))[1] == [1, 2]
    assert asyncio.run(resolve("which space has notepad")) == (T.LOCATE, [1, 2, 3])
    assert asyncio.run(resolve("what's happening across my desktops")) == (T.SUMMARY, [1, 2, 3])
    assert asyncio.run(MultiSpaceQueryHandler()._resolve_spaces("find x", T.SEARCH, None)) == list(range(1, 11))
