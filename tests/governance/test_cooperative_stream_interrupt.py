"""Cooperative stream cancellation + partial-thought buffer + prefill re-ignition.

Resolves the Event-Loop Monopolization deadlock: a streaming op no longer holds the
graceful shutdown hostage. On the shutdown signal, _complete_streaming raises
GracefulStreamInterruption carrying the exact buffered partial; the checkpointer
saves that partial; window-2 resume injects it as an assistant-message PREFILL so
the 32B resumes typing from the interrupted character (no restart-from-scratch).
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

import backend.core.ouroboros.governance.local_inference_director as lid
import backend.core.ouroboros.governance.fsm_checkpoint as ckpt
import backend.core.ouroboros.governance.cooperative_shutdown as coop


def _cfg(**over):
    return dataclasses.replace(lid.LocalConfig.from_env(), **over)


def _sse(content):
    import json
    return ("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n").encode()


class _Reader:
    def __init__(self, lines, on_chunk=None):
        self.lines = list(lines)
        self.i = 0
        self.on_chunk = on_chunk

    async def readline(self):
        if self.i >= len(self.lines):
            return b""
        ln = self.lines[self.i]
        self.i += 1
        if self.on_chunk:
            self.on_chunk(self.i)
        return ln


class _Resp:
    def __init__(self, reader):
        self.content = reader
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _Sess:
    def __init__(self, reader):
        self._r = reader
        self.posted = []
    def post(self, url, **kw):
        self.posted.append(kw)
        return _Resp(self._r)
    async def close(self):
        pass


# --- 1. cooperative shutdown signal -----------------------------------------

def test_cooperative_shutdown_signal():
    coop.reset()
    assert coop.is_requested() is False
    coop.request("wall_clock_cap")
    assert coop.is_requested() is True
    assert coop.reason() == "wall_clock_cap"
    coop.reset()
    assert coop.is_requested() is False


# --- 1+2. mid-stream interruption raises with the buffered partial ----------

def test_stream_interrupts_and_captures_partial():
    coop.reset()

    # After 2 chunks arrive, request shutdown; the loop must raise on the next check.
    def _after(i):
        if i == 2:
            coop.request("wall_clock_cap")

    lines = [_sse("def foo("), _sse("): retu"), _sse("rn 1"), b"data: [DONE]\n"]
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_Reader(lines, _after)))
    try:
        with pytest.raises(lid.GracefulStreamInterruption) as ei:
            asyncio.run(client.complete(system="s", user="u", prompt_tokens=10, stream=True))
        # Shutdown is requested while chunk 2 is read; the loop appends it, then the
        # next-iteration cooperative check trips -> the buffer preserves EVERYTHING
        # received so far (chunks 1+2), losing nothing.
        assert ei.value.partial == "def foo(): retu"
    finally:
        coop.reset()


def test_no_interruption_when_not_requested():
    coop.reset()
    lines = [_sse("hello"), b"data: [DONE]\n"]
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=_Sess(_Reader(lines)))
    lc = asyncio.run(client.complete(system="s", user="u", prompt_tokens=10, stream=True))
    assert lc.text == "hello"


def test_graceful_interruption_not_l7_recoverable():
    import backend.core.ouroboros.governance.candidate_generator as cg
    assert cg._is_l7_recoverable(lid.GracefulStreamInterruption("x", partial="p")) is False


# --- 2. checkpoint stores the partial + stash/pop side-channel ---------------

def test_checkpoint_partial_roundtrip():
    cp = ckpt.FSMCheckpoint(op_id="op-p", phase="GENERATE", partial_completion="def foo(")
    back = ckpt.FSMCheckpoint.from_json(cp.to_json())
    assert back.partial_completion == "def foo("


def test_partial_stash_pop():
    ckpt.stash_partial("op-x", "half a tool call {")
    assert ckpt.pop_partial("op-x") == "half a tool call {"
    assert ckpt.pop_partial("op-x") == ""   # popped once, gone


def test_capture_reads_stashed_partial():
    from types import SimpleNamespace
    ckpt.stash_partial("op-cap", "interrupted mid-")
    cp = ckpt.capture_from_context(SimpleNamespace(op_id="op-cap", phase="GENERATE"), phase="GENERATE")
    assert cp is not None and cp.partial_completion == "interrupted mid-"


def test_resume_envelope_carries_partial():
    cp = ckpt.FSMCheckpoint(op_id="op-r", phase="GENERATE", partial_completion="return ")
    env = ckpt.build_resume_envelope(cp)
    assert env["partial_completion"] == "return "


# --- 3. prefill re-ignition -------------------------------------------------

def test_prefill_injected_as_assistant_message():
    """A resumed generation injects the saved partial as an assistant prefill so the
    model continues from it; the returned text INCLUDES the prefill + continuation."""
    coop.reset()
    lines = [_sse("rn 42"), b"data: [DONE]\n"]   # model continues "...retu|rn 42"
    sess = _Sess(_Reader(lines))
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=sess)
    lc = asyncio.run(client.complete(system="s", user="u", prompt_tokens=10,
                                     stream=True, prefill="def f(): retu"))
    # request body carried the assistant prefill message
    msgs = sess.posted[0]["json"]["messages"]
    assert msgs[-1] == {"role": "assistant", "content": "def f(): retu"}
    # returned text = prefill + streamed continuation (resumes from the exact char)
    assert lc.text == "def f(): return 42"


def test_client_resume_prefill_attr_used():
    """The dispatch sets client._resume_prefill on a resumed op; complete() consumes
    it (once) when no explicit prefill is passed."""
    coop.reset()
    lines = [_sse("rn 7"), b"data: [DONE]\n"]
    sess = _Sess(_Reader(lines))
    client = lid.LocalPrimeClient(_cfg(num_ctx=8192), session=sess)
    client._resume_prefill = "def g(): retu"
    lc = asyncio.run(client.complete(system="s", user="u", prompt_tokens=10, stream=True))
    assert lc.text == "def g(): return 7"
    assert client._resume_prefill == ""   # consumed once
