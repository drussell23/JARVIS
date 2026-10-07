"""The local client surfaces the ENGINE's words, never a KeyError about them.

2026-10-07: a corrupt adapter crashed llama.cpp's grammar sampler; J-Prime
returned the engine's error as HTTP 500, and O+V's client reported
``KeyError: 'choices'`` -- the parser reaching for a field an error body
never has. The cause was discarded at the seam that read it.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

LID = "backend.core.ouroboros.governance.local_inference_director"

#: The body J-Prime actually returned that day (llama-server's JSON error,
#: nested as a string inside J-Prime's own envelope).
JPRIME_500 = ('{"error":"{\\"error\\":{\\"code\\":500,\\"message\\":\\"got exception: Unexpected empty '
              'grammar stack after accepting piece: / (14)\\",\\"type\\":\\"server_error\\"}}"}')
GRAMMAR = "got exception: Unexpected empty grammar stack after accepting piece: / (14)"


@pytest.fixture()
def lid(monkeypatch):
    mod = importlib.import_module(LID)
    monkeypatch.setenv("JARVIS_LOCAL_STREAMING_ENABLED", "0")
    monkeypatch.delenv("JARVIS_LOCAL_TRANSPORT", raising=False)
    monkeypatch.setattr(mod, "_SCHEMA_UNSUPPORTED", set())
    monkeypatch.setattr(mod, "_REASONING_UNSUPPORTED", set())
    return mod


def test_nested_envelopes_unwrap_to_the_engines_own_message(lid):
    assert lid._engine_error_message(JPRIME_500) == GRAMMAR
    assert lid._engine_error_message({"error": {"message": "model 'x' not found"}}) == "model 'x' not found"
    assert lid._engine_error_message("plain text from a proxy") == "plain text from a proxy"
    assert lid._engine_error_message(b'{"detail": "busy"}') == "busy"


def test_an_error_status_raises_with_status_message_and_retry_after(lid):
    with pytest.raises(lid.LocalEngineError) as ei:
        lid._extract_completion(JPRIME_500, 500)
    assert ei.value.status == 500 and ei.value.engine_message == GRAMMAR
    with pytest.raises(lid.LocalEngineError) as ei:
        lid._extract_completion('{"error": "training lease held"}', 503, 886.0)
    assert ei.value.retry_after_s == 886.0 and "training lease held" in str(ei.value)


def test_an_error_body_behind_a_200_is_still_an_error(lid):
    with pytest.raises(lid.LocalEngineError, match="boom"):
        lid._extract_completion({"error": "boom"}, 200)


def test_a_body_of_neither_shape_is_named_not_keyerrored(lid):
    with pytest.raises(lid.LocalEngineError, match="neither dialect"):
        lid._extract_completion({"id": "x", "object": "chat.completion"}, 200)


def test_well_formed_replies_of_both_dialects_still_parse(lid):
    assert lid._extract_completion({"message": {"content": "hi"}, "eval_count": 3, "prompt_eval_count": 7}) == ("hi", 3, 7)
    assert lid._extract_completion({"choices": [{"message": {"content": "yo"}}],
                                    "usage": {"completion_tokens": 2, "prompt_tokens": 5}}) == ("yo", 2, 5)


def test_error_frames_inside_a_stream_are_surfaced_by_both_parsers(lid):
    sse = lid._parse_stream_line(b'data: {"error": {"message": "decoder crashed"}}')
    nd = lid._parse_stream_line(b'{"error": "decoder crashed"}')
    assert isinstance(sse, lid._StreamError) and sse.message == "decoder crashed"
    assert isinstance(nd, lid._StreamError) and nd.message == "decoder crashed"
    assert lid._parse_stream_line(b'data: {"choices": [{"delta": {"content": "ok"}}]}') == "ok"


def test_engine_errors_are_not_host_faults(lid):
    from backend.core.ouroboros.governance import inference_gateway as ig
    assert not ig.is_infrastructure_fault(lid.LocalEngineError(500, GRAMMAR))


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body

    async def json(self, **kw):
        raise AssertionError("an error body must not be decoded as a completion")


class _Session:
    def __init__(self, resp):
        self.resp = resp

    def post(self, url, **kw):
        return self.resp

    async def close(self):
        pass


def test_the_client_raises_the_engines_message_end_to_end(lid):
    cfg = lid.LocalConfig(**{**lid.LocalConfig.from_env().__dict__})
    client = lid.LocalPrimeClient(cfg, session=_Session(_Resp(500, JPRIME_500)))
    with pytest.raises(lid.LocalEngineError) as ei:
        asyncio.run(client.complete(system="s", user="u", prompt_tokens=10, response_format=None))
    assert ei.value.status == 500 and ei.value.engine_message == GRAMMAR


def test_a_refused_request_keeps_the_body_raise_for_status_used_to_discard(lid):
    cfg = lid.LocalConfig(**{**lid.LocalConfig.from_env().__dict__})
    client = lid.LocalPrimeClient(cfg, session=_Session(_Resp(400, '{"error": "unknown model qwen9:1b"}')))
    with pytest.raises(lid.LocalEngineError, match="unknown model qwen9:1b"):
        asyncio.run(client.complete(system="s", user="u", prompt_tokens=10))
