"""The native ``/api/chat`` transport, and why the default moved to it.

Measured on ollama 0.33.2 with curl, 2026-09-02, against qwen3-coder:30b:

    /v1/chat/completions   seed=7 -> 7da4b520af   seed=7 -> 6f75291498
    /api/chat              seed=7 -> 74f9a91dfc   seed=7 -> 74f9a91dfc
    /v1  options.top_k=1   -> 93e3f6958f, 2c36a271df   (greedy, yet varied)
    /api options.top_k=1   -> 712bb859ae, 712bb859ae   (greedy, identical)

The OpenAI-compatible layer drops the ``options`` block. Every sampler
field the entropy ladder sets -- ``top_k``, ``repeat_penalty``, ``seed`` --
rides ``options``, so on ``/v1`` the ladder reached the wire and not the
sampler: soak bt-2026-09-02-220948 logged 56 distinct sampling points and 13
byte-identical redraws. Only ``temperature`` and ``top_p`` (OpenAI-spelled)
ever bit, which is the temperature-only knob the ladder comment warns cannot
widen a truncated tail.

These tests pin the native dialect end to end: the URL, every field's
native spelling, the NDJSON stream parser as a clean sibling of the SSE
one, the shape-dispatched reply reader, and -- because a transport that
looks wired and is not is the whole history of this arc -- the request
BODY a sibling draw actually posts.
"""
from __future__ import annotations

import json

import pytest

LID = "backend.core.ouroboros.governance.local_inference_director"
ENT = "backend.core.ouroboros.governance.sibling_entropy"


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p


class _RecordingSession:
    def __init__(self, payload=None):
        self._p = payload
        self.posts = []
        self.closed = False

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return _FakeResp(self._p)

    async def close(self):
        self.closed = True

    @property
    def last(self):
        assert self.posts, "nothing posted"
        return self.posts[-1]


@pytest.fixture()
def lid(monkeypatch):
    import importlib
    mod = importlib.import_module(LID)
    monkeypatch.setenv("JARVIS_LOCAL_STREAMING_ENABLED", "0")
    monkeypatch.delenv("JARVIS_LOCAL_TRANSPORT", raising=False)
    monkeypatch.setattr(mod, "_SCHEMA_UNSUPPORTED", set())
    monkeypatch.setattr(mod, "_REASONING_UNSUPPORTED", set())
    return mod


@pytest.fixture()
def ent():
    import importlib
    return importlib.import_module(ENT)


def _cfg(lid, **over):
    return lid.LocalConfig(**{**lid.LocalConfig.from_env().__dict__, **over})


NATIVE_REPLY = {
    "model": "m", "message": {"role": "assistant", "content": "native ok"},
    "done": True, "eval_count": 9, "prompt_eval_count": 41,
}
OPENAI_REPLY = {
    "choices": [{"message": {"content": "openai ok"}}],
    "usage": {"completion_tokens": 9, "prompt_tokens": 41},
}


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------

def test_native_is_the_default_and_the_url_follows(lid):
    cfg = _cfg(lid)
    assert lid.is_native_transport(cfg)
    assert lid.chat_endpoint(cfg).endswith("/api/chat")


def test_openai_stays_selectable(lid, monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_TRANSPORT", "openai")
    cfg = lid.LocalConfig.from_env()
    assert not lid.is_native_transport(cfg)
    assert lid.chat_endpoint(cfg).endswith("/v1/chat/completions")


def test_a_typo_selects_the_working_dialect_not_the_leaky_one(lid, monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_TRANSPORT", "opneai")
    assert lid.LocalConfig.from_env().transport == "native"


# ---------------------------------------------------------------------------
# One builder, one translation seam
# ---------------------------------------------------------------------------

def test_spelling_seam_moves_scalars_into_options_on_native(lid):
    body = {"model": "m", "messages": [], "temperature": 0.7, "max_tokens": 77,
            "top_p": 0.9, "seed": 5, "stream_options": {"include_usage": True},
            "options": {"num_ctx": 4096, "top_p": 0.9, "seed": 5, "top_k": 60}}
    out = lid._spell_for_transport(body, _cfg(lid))
    assert out is body
    assert "temperature" not in body and "max_tokens" not in body
    assert "top_p" not in body and "seed" not in body
    assert "stream_options" not in body
    assert body["options"]["temperature"] == pytest.approx(0.7)
    assert body["options"]["num_predict"] == 77
    # nothing that was already native-spelled is disturbed
    assert body["options"]["num_ctx"] == 4096
    assert body["options"]["top_k"] == 60 and body["options"]["seed"] == 5


def test_spelling_seam_is_a_noop_on_openai(lid):
    body = {"model": "m", "temperature": 0.7, "max_tokens": 77, "top_p": 0.9}
    before = json.dumps(body, sort_keys=True)
    lid._spell_for_transport(body, _cfg(lid, transport="openai"))
    assert json.dumps(body, sort_keys=True) == before


def test_spelling_seam_repairs_a_non_dict_options(lid):
    body = {"temperature": 0.5, "options": "corrupt"}
    lid._spell_for_transport(body, _cfg(lid))
    assert body["options"] == {"temperature": 0.5}


# ---------------------------------------------------------------------------
# Field spellings ride the SAME appliers
# ---------------------------------------------------------------------------

def test_schema_constraint_is_spelled_format_on_native(lid, monkeypatch):
    monkeypatch.setattr(lid, "_resolve_response_schema",
                        lambda: {"type": "object", "properties": {}})
    body = {}
    assert lid._apply_response_format(body, _cfg(lid)) == "json_schema"
    assert body["format"] == {"type": "object", "properties": {}}
    assert "response_format" not in body


def test_json_object_rung_is_spelled_json_on_native(lid, monkeypatch):
    monkeypatch.setattr(lid, "_resolve_response_schema", lambda: None)
    body = {}
    assert lid._apply_response_format(body, _cfg(lid)) == "json_object"
    assert body["format"] == "json"


def test_degrade_rewrites_the_native_spelling(lid, monkeypatch):
    monkeypatch.setattr(lid, "_resolve_response_schema",
                        lambda: {"type": "object"})
    cfg = _cfg(lid)
    body = {}
    lid._apply_response_format(body, cfg)
    assert lid._degrade_response_format(body, cfg) is True
    assert body["format"] == "json"
    assert lid._degrade_response_format(body, cfg) is False   # idempotent


def test_reasoning_effort_none_is_think_false_on_native(lid, monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_REASONING_EFFORT", "none")
    body = {}
    assert lid._apply_reasoning_effort(body, _cfg(lid)) == "none"
    assert body["think"] is False and "reasoning_effort" not in body
    assert lid._degrade_reasoning_effort(body, _cfg(lid)) is True
    assert "think" not in body


# ---------------------------------------------------------------------------
# The NDJSON parser: a clean sibling of _parse_sse_delta, same contract
# ---------------------------------------------------------------------------

def _nd(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def test_ndjson_content_frame(lid):
    assert lid._parse_ndjson_delta(_nd({"message": {"content": "ab"}, "done": False})) == "ab"


def test_ndjson_terminal_frame_carries_the_engine_counts(lid):
    got = lid._parse_ndjson_delta(_nd({"message": {"content": ""}, "done": True,
                                       "eval_count": 12, "prompt_eval_count": 300}))
    assert isinstance(got, lid._SSEUsage)
    assert got.completion_tokens == 12 and got.prompt_tokens == 300


def test_ndjson_terminal_frame_without_counts_is_done(lid):
    assert lid._parse_ndjson_delta(_nd({"message": {"content": ""}, "done": True})) is lid._SSE_DONE


def test_ndjson_trailing_fragment_on_the_last_frame_is_not_lost(lid):
    """A last fragment AND done:true -> the fragment; EOF ends the loop."""
    assert lid._parse_ndjson_delta(_nd({"message": {"content": "!"}, "done": True,
                                       "eval_count": 3})) == "!"


@pytest.mark.parametrize("junk", [b"", b"\n", b"not json", b"[1,2]", b"data: {}", b"{bad"])
def test_ndjson_never_raises(lid, junk):
    assert lid._parse_ndjson_delta(junk) is None


def test_the_loop_is_dialect_blind(lid):
    """One read loop, two wire formats, dispatched on the line's first byte."""
    sse = b'data: {"choices":[{"delta":{"content":"s"}}]}\n'
    nd = _nd({"message": {"content": "n"}, "done": False})
    assert lid._parse_stream_line(sse) == "s"
    assert lid._parse_stream_line(nd) == "n"
    assert lid._parse_stream_line(b"data: [DONE]\n") is lid._SSE_DONE


# ---------------------------------------------------------------------------
# The reply reader dispatches on SHAPE
# ---------------------------------------------------------------------------

def test_extract_completion_reads_both_shapes(lid):
    assert lid._extract_completion(NATIVE_REPLY) == ("native ok", 9, 41)
    assert lid._extract_completion(OPENAI_REPLY) == ("openai ok", 9, 41)


def test_extract_completion_missing_counts_are_zero_not_errors(lid):
    assert lid._extract_completion({"message": {"content": "x"}}) == ("x", 0, 0)


# ---------------------------------------------------------------------------
# End to end: what a sibling draw actually POSTS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_native_body_carries_the_whole_sampling_point_in_options(lid, ent):
    """THE regression. On /v1 these fields reached the wire and not the sampler."""
    sess = _RecordingSession(NATIVE_REPLY)
    client = lid.LocalPrimeClient(_cfg(lid), session=sess)
    point = ent.sampling_for(3, op_id="op-native")
    out = await client.complete(system="<s/>", user="<u/>", prompt_tokens=10,
                                max_tokens=64, sampling=point)
    url, kw = sess.last
    body = kw["json"]
    assert url.endswith("/api/chat")
    opts = body["options"]
    assert opts["top_k"] == int(point.top_k)
    assert opts["repeat_penalty"] == pytest.approx(float(point.repeat_penalty))
    assert opts["top_p"] == pytest.approx(float(point.top_p))
    assert opts["seed"] == int(point.seed)
    assert opts["temperature"] == pytest.approx(0.2)
    assert opts["num_predict"] == 64
    for absent in ("temperature", "max_tokens", "top_p", "seed", "stream_options"):
        assert absent not in body, absent
    # The native route streams unless told not to; a JSON read of an
    # x-ndjson reply is the failure the first live call produced.
    assert body["stream"] is False
    # the native reply was read natively, with MEASURED counts
    assert out.text == "native ok"
    assert out.output_tokens == 9 and out.prompt_tokens == 41
    assert out.tokens_estimated is False


@pytest.mark.asyncio
async def test_openai_dialect_keeps_both_spellings(lid, ent):
    """The OpenAI route is byte-identical to before the native transport."""
    sess = _RecordingSession(OPENAI_REPLY)
    client = lid.LocalPrimeClient(_cfg(lid, transport="openai"), session=sess)
    point = ent.sampling_for(2, op_id="op-openai")
    await client.complete(system="<s/>", user="<u/>", prompt_tokens=10, sampling=point)
    url, kw = sess.last
    body = kw["json"]
    assert url.endswith("/v1/chat/completions")
    assert body["top_p"] == pytest.approx(float(point.top_p))
    assert body["seed"] == int(point.seed)
    assert body["temperature"] == pytest.approx(0.2)
    assert body["options"]["top_k"] == int(point.top_k)


@pytest.mark.asyncio
async def test_warmup_speaks_the_native_dialect(lid):
    sess = _RecordingSession(NATIVE_REPLY)
    client = lid.LocalPrimeClient(_cfg(lid), session=sess)
    assert await client.warmup(timeout_s=5.0) is True
    url, kw = sess.last
    assert url.endswith("/api/chat")
    assert kw["json"]["options"]["num_predict"] == 1
    assert kw["json"]["options"]["temperature"] == 0.0
    assert "max_tokens" not in kw["json"]


# ---------------------------------------------------------------------------
# Native STREAMING end to end: NDJSON frames through the real read loop
# ---------------------------------------------------------------------------

class _NDReader:
    def __init__(self, lines):
        self.lines = list(lines)

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""


class _StreamResp:
    def __init__(self, reader):
        self.content = reader
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _StreamSess:
    def __init__(self, lines):
        self._lines = lines
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return _StreamResp(_NDReader(self._lines))

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_native_stream_assembles_text_and_measures_tokens(lid, monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_STREAMING_ENABLED", "1")
    lines = [
        _nd({"message": {"content": "def "}, "done": False}),
        _nd({"message": {"content": "f():"}, "done": False}),
        _nd({"message": {"content": ""}, "done": True,
             "eval_count": 4, "prompt_eval_count": 20}),
    ]
    sess = _StreamSess(lines)
    client = lid.LocalPrimeClient(_cfg(lid, num_ctx=4096), session=sess)
    out = await client.complete(system="<s/>", user="<u/>", prompt_tokens=10, stream=True)
    assert out.text == "def f():"
    assert out.output_tokens == 4 and out.prompt_tokens == 20
    assert out.tokens_estimated is False
    url, kw = sess.posts[-1]
    assert url.endswith("/api/chat")
    assert kw["json"]["stream"] is True
    assert "stream_options" not in kw["json"]
