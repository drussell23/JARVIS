"""JARVIS says only what happened, once.

Pinned from the 2026-10-14 12:19 session:

* a J-Prime 503 became "I apologize, but I'm experiencing technical
  difficulties. Error: Cloud client not available" -- written by the router
  AS the model's content, parsed as the classifier's JSON, and a cloud lane
  that the paid-lane authority had switched off was "fallen back to";
* "Firefox is now open and ready for you to use" was said while the open's
  own check logged "no window appeared for Mozilla Firefox within 10s", and
  the Start-menu "Firefox" was never matched for the Mind's "Mozilla Firefox";
* every typed command ran twice, ~30 s apart: the WebSocket never ACKed the
  client's reliable send, the client re-sent over REST, and the command id
  that would have deduplicated it never reached the pipeline.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.core import desktop_open as d
from backend.core import prime_router as pr


# --------------------------------------------------------------- the router
def _router():
    router = pr.PrimeRouter(pr.PrimeRouterConfig())
    router._initialized = True
    return router


class _FailingPrime:
    is_available = True
    _status = None
    _config = types.SimpleNamespace(prime_host="localhost")

    async def generate(self, **_kw):
        raise RuntimeError('Prime returned 503: {"error":"every resident model is busy"}')

    async def generate_stream(self, **_kw):
        raise RuntimeError("Prime returned 503")
        yield  # pragma: no cover


def test_a_failed_generation_carries_no_invented_content(monkeypatch):
    monkeypatch.setattr(pr, "_cloud_lane_allowed", lambda: False)
    router = _router()
    router._prime_client = _FailingPrime()
    resp = asyncio.run(router._generate_internal("hi", None, None, 16, 0.0))
    assert resp.content == "" and not pr.generation_ok(resp)
    # The real reason, not the absent cloud lane it never had.
    assert "every resident model is busy" in pr.generation_failure(resp)
    assert "Cloud client" not in pr.generation_failure(resp)
    with pytest.raises(pr.GenerationUnavailable, match="busy"):
        pr.require_generation(resp)


def test_no_cloud_fallback_when_the_paid_lane_authority_refuses(monkeypatch):
    monkeypatch.setattr(pr, "_cloud_lane_allowed", lambda: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    router = _router()
    assert asyncio.run(router._get_cloud_client()) is None
    assert router._cloud_fallback_possible() is False


def test_a_stream_with_no_model_raises_instead_of_yielding_text(monkeypatch):
    monkeypatch.setattr(pr, "_cloud_lane_allowed", lambda: False)
    router = _router()
    router._prime_client = _FailingPrime()

    async def drain():
        return [c async for c in router.generate_stream("hi")]
    with pytest.raises(RuntimeError, match="no model answered"):
        asyncio.run(drain())


def test_any_non_router_reply_is_a_generation():
    assert pr.generation_ok(types.SimpleNamespace(content="x"))
    assert pr.require_generation(types.SimpleNamespace(content="x")) == "x"
    assert not pr.generation_ok(None)


# ------------------------------------------------------------- desktop_open
def test_the_minds_fuller_app_name_finds_the_start_menu_entry():
    names = ["Firefox", "Microsoft Edge", "Notepad"]
    assert d.best_match("Mozilla Firefox", names) == "Firefox"
    assert d.best_match("firefox", names) == "Firefox"
    assert d.best_match("Thunderbird", names) is None


def test_an_open_says_only_what_was_observed():
    started = d.OpenOutcome(True, "app", "Mozilla Firefox", "Mozilla Firefox", "shell_start",
                            in_front=False)
    assert not started.evidenced
    assert "no window" in started.describe() and "not confirmed" in started.describe()
    assert not started.describe().startswith("Opened")
    seen = d.OpenOutcome(True, "app", "Firefox", "Firefox", "apps_folder",
                         window="Mozilla Firefox", in_front=True)
    assert seen.evidenced and seen.describe().startswith("Opened Firefox")
    unobserved = d.OpenOutcome(True, "app", "Safari", "Safari", "open -a")   # macOS
    assert unobserved.evidenced and unobserved.describe() == "Opened Safari"


def test_an_already_open_window_brought_to_the_front_is_evidence(monkeypatch):
    """Opening a folder that is already open adds no window: the shell
    brings the existing one forward, and that counts."""
    terminal, folder = 1, 2
    windows = [(terminal, "claude", "windowsterminal.exe"), (folder, "Documents", "explorer.exe")]
    fronts = iter([terminal, folder])
    last = {"fg": terminal}

    def foreground():
        last["fg"] = next(fronts, last["fg"])
        return last["fg"]

    monkeypatch.setattr(d, "_IS_WINDOWS", True)
    monkeypatch.setattr(d, "_win_top_windows", lambda: list(windows))
    monkeypatch.setattr(d, "_win_foreground", foreground)
    monkeypatch.setattr(d, "_win_bring_forward", lambda h: True)
    monkeypatch.setattr(d, "_win_title", lambda h: dict((w[0], w[1]) for w in windows)[h])
    monkeypatch.setenv("JARVIS_OPEN_WINDOW_WAIT_S", "0.5")
    monkeypatch.setenv("JARVIS_OPEN_WINDOW_POLL_S", "0.01")
    monkeypatch.setenv("JARVIS_OPEN_TITLE_SETTLE_S", "0.02")

    async def opened():
        return d.OpenOutcome(True, "path", "Documents", "C:/Users/x/Documents", "shell")
    out = asyncio.run(d._opened_in_front(opened()))
    assert out.in_front is True and out.window == "Documents"


def test_an_unconfirmed_open_does_not_satisfy_the_goal(monkeypatch):
    from backend.core import runtime_task_orchestrator as rto_mod

    rto = rto_mod.RuntimeTaskOrchestrator()

    async def decide(goal, step):
        return {"open": "app", "app": "Mozilla Firefox", "completes_goal": True}

    async def launch(name):
        return d.OpenOutcome(True, "app", name, name, "shell_start", in_front=False)

    monkeypatch.setattr(rto, "_resolve_open_target", decide)
    monkeypatch.setattr(d, "open_app", launch)
    result = asyncio.run(rto._dispatch_to_vision("open firefox", {}))
    assert result["success"] is False and result["opened"] == "Mozilla Firefox"
    assert "no window" in result["result"]


def test_reply_phrasing_never_speaks_a_failed_generation():
    from backend.core import runtime_task_orchestrator as rto_mod

    class _Degraded:
        async def generate(self, **_kw):
            return pr.RouterResponse(content="", source="degraded", latency_ms=0, model="none")

    rto = rto_mod.RuntimeTaskOrchestrator(prime_client=_Degraded())
    outcome = "Started Mozilla Firefox, but no window of it appeared within 10s"
    assert asyncio.run(rto._phrase_for_user("open firefox", outcome)) == outcome


# ------------------------------------------------------------ one command, once
def _processor(monkeypatch):
    from backend.api import unified_command_processor as ucp
    proc = ucp.UnifiedCommandProcessor.__new__(ucp.UnifiedCommandProcessor)
    proc._dedup_cache, proc._dedup_inflight = {}, {}
    proc._dedup_ttl, proc._dedup_max = 60.0, 10
    proc.context = ucp.UnifiedContext(conversation_history=[])
    runs = []

    async def pipeline(text, **_kw):
        runs.append(text)
        await asyncio.sleep(0.05)
        return {"success": True, "response": f"did {text}"}

    monkeypatch.setattr(proc, "_execute_command_pipeline", pipeline)
    return proc, runs


def test_a_resend_while_the_command_runs_joins_it(monkeypatch):
    proc, runs = _processor(monkeypatch)

    async def go():
        return await asyncio.gather(
            proc.process_command("search for dogs", request_id="cmd_1"),
            proc.process_command("search for dogs", request_id="cmd_1"))
    first, resend = asyncio.run(go())
    assert runs == ["search for dogs"]
    assert first["response"] == resend["response"] and resend["deduplicated"] is True


def test_the_command_id_reaches_the_pipeline_through_layers_that_drop_it(monkeypatch):
    """Context handlers pass only the text; the transport's scope carries the id."""
    from backend.core.command_identity import command_scope, request_id_of
    proc, runs = _processor(monkeypatch)

    async def through_a_wrapper(text):          # the shape of a context handler
        return await proc.process_command(text)

    async def go():
        with command_scope(request_id_of({"command_id": "cmd_2"})):
            ws = asyncio.ensure_future(through_a_wrapper("open documents"))
        with command_scope("cmd_2"):
            rest = asyncio.ensure_future(through_a_wrapper("open documents"))
        return await asyncio.gather(ws, rest)
    asyncio.run(go())
    assert runs == ["open documents"]


def test_a_failed_command_is_not_rerun_by_its_context_handler(monkeypatch):
    from backend.api.simple_context_handler_enhanced import EnhancedSimpleContextHandler
    calls = []

    class _Proc:
        async def process_command(self, command, websocket=None, **_kw):
            calls.append(command)
            raise RuntimeError("the command itself failed")

    handler = EnhancedSimpleContextHandler(_Proc())
    monkeypatch.setattr(handler, "_is_shutdown_command", lambda c: False, raising=False)
    with pytest.raises(RuntimeError, match="itself failed"):
        asyncio.run(handler.process_with_context("open firefox"))
    assert calls == ["open firefox"]


def test_the_websocket_acknowledges_a_reliable_send_before_working(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import unified_websocket as uw
    from backend.core.command_identity import command_request_id
    seen = {}

    class _Manager:
        connection_health = {}

        async def connect(self, ws, client_id):
            await ws.accept()
            self.ws = ws

        async def _safe_send_json(self, client_id, payload, **_kw):
            await self.ws.send_json(payload)
            return True

        async def handle_message(self, client_id, data):
            seen["request_id"] = command_request_id.get()
            return {"type": "response", "text": "done"}

        async def disconnect(self, client_id):
            pass

        def voice_sink(self, client_id):
            return None  # the host speaks

    monkeypatch.setattr(uw, "get_ws_manager", lambda: _Manager())
    app = FastAPI()
    app.add_api_websocket_route("/ws", uw.unified_websocket_endpoint)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "command", "text": "search for dogs",
                      "messageId": "m-1", "command_id": "cmd_3"})
        assert ws.receive_json() == {"type": "ack", "ackId": "m-1"}
        assert ws.receive_json()["text"] == "done"
    assert seen["request_id"] == "cmd_3"
