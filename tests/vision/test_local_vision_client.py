"""LocalVisionClient against a stand-in OpenAI-compatible server, and the
multi-space handler's decision to look at a space."""
from __future__ import annotations

import asyncio
import io
import os
import sys
import threading
from types import SimpleNamespace

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if BACKEND not in [os.path.abspath(p) for p in sys.path]:
    sys.path.insert(0, BACKEND)

from aiohttp import web  # noqa: E402
from PIL import Image  # noqa: E402

from vision.local_vision_client import LocalVisionClient, _to_jpeg  # noqa: E402


def _png(size=(2000, 1000)):
    buf = io.BytesIO()
    Image.new("RGB", size, (30, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakeModelServer:
    def __init__(self):
        self.mode = "ok"
        self.requests = []

    def app(self):
        async def chat(request):
            body = await request.json()
            self.requests.append(body)
            if self.mode == "http_error":
                return web.Response(status=404, text='{"error":"model not found"}')
            msg = {"role": "assistant", "content": "Notepad shows notes.txt."}
            if self.mode == "reasoning_only":
                msg = {"role": "assistant", "content": "", "reasoning": "Let me think..."}
            return web.json_response({"choices": [{"message": msg}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        return app


@pytest.fixture
def server():
    srv = FakeModelServer()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state = {}

    async def start():
        runner = web.AppRunner(srv.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        state["runner"], state["port"] = runner, site._server.sockets[0].getsockname()[1]
        ready.set()

    t = threading.Thread(target=lambda: (loop.run_until_complete(start()), loop.run_forever()), daemon=True)
    t.start()
    assert ready.wait(10)
    yield srv, f"http://127.0.0.1:{state['port']}/v1"
    asyncio.run_coroutine_threadsafe(state["runner"].cleanup(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)


def test_jpeg_is_bounded_for_every_input_kind():
    import numpy as np

    for image in (_png(), Image.open(io.BytesIO(_png())), np.zeros((900, 3000, 3), dtype=np.uint8)):
        out = Image.open(io.BytesIO(_to_jpeg(image, 1280)))
        assert out.format == "JPEG" and max(out.size) == 1280


def test_describe_sends_openai_image_payload(server):
    srv, url = server
    client = LocalVisionClient(base_url=url, model="jarvis-vision:8b")
    ans = asyncio.run(client.describe(_png(), "what is here?"))
    assert ans.ok and ans.text == "Notepad shows notes.txt." and ans.model == "jarvis-vision:8b"
    body = srv.requests[0]
    assert body["model"] == "jarvis-vision:8b"
    parts = body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[1] == {"type": "text", "text": "what is here?"}


def test_thinking_model_is_reported_not_blank(server):
    srv, url = server
    srv.mode = "reasoning_only"
    ans = asyncio.run(LocalVisionClient(base_url=url, model="m").describe(_png(), "q"))
    assert not ans.ok and ans.error == "reasoning_only"


def test_failures_never_raise(server):
    srv, url = server
    srv.mode = "http_error"
    assert asyncio.run(LocalVisionClient(base_url=url, model="m").describe(_png(), "q")).error.startswith("http_404")
    dead = LocalVisionClient(base_url="http://127.0.0.1:9/v1", model="m", timeout_s=2)
    assert not asyncio.run(dead.describe(_png(), "q")).ok
    assert asyncio.run(LocalVisionClient(base_url=url, model="m").describe(b"not an image", "q")).error.startswith("bad_image")


def test_disabled_without_a_model_or_by_flag(monkeypatch):
    monkeypatch.delenv("JARVIS_VISION_MODEL_NAME", raising=False)
    assert not LocalVisionClient().enabled
    assert asyncio.run(LocalVisionClient().describe(_png(), "q")).error == "disabled"
    monkeypatch.setenv("JARVIS_VISION_MODEL_NAME", "m")
    monkeypatch.setenv("JARVIS_LOCAL_VISION_ENABLED", "false")
    assert not LocalVisionClient().enabled


# --- handler: when to look ------------------------------------------------------

class _Detector:
    """Windows-agent-shaped detector: one notepad on desktop 2, capture works."""

    def __init__(self):
        self.captured = []

    def is_available(self):
        return True

    def get_windows_for_space(self, space_id):
        return [{"app": "Notepad", "title": "notes.txt"}] if space_id == 2 else []

    async def enumerate_all_spaces_async(self):
        return [{"space_id": 1}, {"space_id": 2}]

    async def capture_space_async(self, space_id, max_dim=None):
        self.captured.append(space_id)
        return SimpleNamespace(ok=True, png=_png((64, 48)), meta={"desktop": space_id}, error=None)


def _handler(monkeypatch, url):
    from context_intelligence.handlers import multi_space_query_handler as m
    import vision.local_vision_client as lvc

    monkeypatch.setattr(lvc, "_client", LocalVisionClient(base_url=url, model="jarvis-vision:8b"))
    h = m.MultiSpaceQueryHandler(yabai_detector=_Detector())

    async def edge(space_id):  # validator stub: both spaces exist, nothing special
        return SimpleNamespace(edge_case=None, success=True, message="", state_info=None)

    monkeypatch.setattr(h.space_manager, "handle_edge_case", edge)
    return h


def test_named_space_question_looks_and_grounds_the_prompt(server, monkeypatch):
    srv, url = server
    h = _handler(monkeypatch, url)
    res = asyncio.run(h.handle_query("what's on desktop 2"))
    assert h.yabai_detector.captured == [2]
    r = res.results[0]
    assert r.vision_analysis["description"] == "Notepad shows notes.txt."
    assert r.content_summary == "Notepad: notes.txt — Notepad shows notes.txt."
    prompt = srv.requests[0]["messages"][0]["content"][1]["text"]
    assert "- Notepad: notes.txt" in prompt and "what's on desktop 2" in prompt


def test_search_across_spaces_does_not_spend_model_calls(server, monkeypatch):
    srv, url = server
    h = _handler(monkeypatch, url)
    res = asyncio.run(h.handle_query("which space has notepad"))
    assert h.yabai_detector.captured == [] and srv.requests == []
    assert "Space 2" in res.synthesis


def test_model_failure_keeps_the_window_summary(server, monkeypatch):
    srv, url = server
    srv.mode = "reasoning_only"
    h = _handler(monkeypatch, url)
    r = asyncio.run(h.handle_query("what's on desktop 2")).results[0]
    assert r.content_summary == "Notepad: notes.txt"
    assert r.vision_analysis == {"error": "reasoning_only", "source": "vision_model", "model": "jarvis-vision:8b"}
