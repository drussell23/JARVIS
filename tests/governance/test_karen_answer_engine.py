"""Karen Answer Engine — grounded, narrated, policy-routed Q&A spine.

Operator mandate: real answers grounded in the codebase (not
"simulation crap"), live thinking narration in the CLI, and Karen's
voice on top. Tests drive the REAL executor + dispatcher + multiplexer
with the rt_gate lane mocked at its seam.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import karen_answer_engine as kae


# ---------------------------------------------------------------------------
# (1) Grounding pack — evidence, not imagination
# ---------------------------------------------------------------------------


def test_grounding_pack_carries_real_commits():
    pack = kae.build_grounding_pack()
    assert "recent commits:" in pack
    # Real repo evidence — a hash-prefixed oneline subject is present.
    assert any(
        len(ln.split()[0]) >= 7 for ln in pack.splitlines()
        if ln and ln[0].isalnum() and " " in ln
    )


def test_grounding_pack_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_GROUNDING_CAP_CHARS", "500")
    assert len(kae.build_grounding_pack()) <= 500


def test_grounding_pack_never_raises(monkeypatch):
    # Kill every source — pack degrades to empty, never an error.
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")),
    )
    assert isinstance(kae.build_grounding_pack(), str)


# ---------------------------------------------------------------------------
# (2) The provider — narration order + grounding + degrade
# ---------------------------------------------------------------------------


def test_provider_narrates_and_grounds(monkeypatch):
    seen = {}

    async def _fake_gate(prompt, **kw):
        seen["prompt"] = prompt
        seen["kw"] = kw
        return "You last worked on the split-plane attach cockpit."

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.rt_gate.gate_completion",
        _fake_gate,
    )
    progress = []
    p = kae.KarenQueryProvider(progress_sink=progress.append)
    answer = p.query("what did I last work on?")

    assert answer == "You last worked on the split-plane attach cockpit."
    # Thinking narration, in order, in the glyph grammar.
    assert progress[0].startswith("⎿ thinking · gathering")
    assert progress[1].startswith("⎿ thinking · asking claude")
    # The prompt reaching the provider lane is GROUNDED.
    assert "## Organism state (evidence" in seen["prompt"]
    assert "recent commits:" in seen["prompt"]
    assert seen["kw"]["caller_id"] == "karen_chat_answer"


def test_provider_degrades_honestly(monkeypatch):
    async def _dead_gate(prompt, **kw):
        raise RuntimeError("all tiers down")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.rt_gate.gate_completion",
        _dead_gate,
    )
    p = kae.KarenQueryProvider()
    answer = p.query("hello?")
    assert "couldn't reach a provider" in answer
    assert "RuntimeError" in answer               # honest, not a traceback


# ---------------------------------------------------------------------------
# (3) Voice tap — deterministic spoken digest, mounted-only
# ---------------------------------------------------------------------------


def test_spoken_digest_first_sentence_capped():
    long = (
        "O+V is your self-evolving engineering organism. It has eleven "
        "phases and seventeen senses. " + "x" * 300
    )
    d = kae.spoken_digest(long)
    assert d == "O+V is your self-evolving engineering organism."
    assert len(kae.spoken_digest("word " * 200)) <= 140


def test_speak_answer_silent_when_no_duplex():
    # No supervisor / no mounted karen — False, zero noise, zero raise.
    assert kae.speak_answer("anything") is False


def test_speak_answer_submits_when_mounted(monkeypatch):
    spoken = []

    class _Karen:
        def submit_speech(self, line, *a, **k):
            spoken.append(line)

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.comms.duplex."
        "karen_duplex_factory.get_default_karen",
        lambda: _Karen(),
    )
    assert kae.speak_answer("Short answer. Longer detail follows.") is True
    assert spoken == ["Short answer."]


# ---------------------------------------------------------------------------
# (4) End-to-end: mux renders the ANSWER, telemetry stays off the surface
# ---------------------------------------------------------------------------


async def test_full_loop_answer_first_render(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_TEXT_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "true")

    async def _fake_gate(prompt, **kw):
        return "The attach cockpit was the most recent work."

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.rt_gate.gate_completion",
        _fake_gate,
    )
    from backend.core.ouroboros.governance.chat_text_bridge import (
        build_chat_text_multiplexer,
    )
    lines = []
    mux = build_chat_text_multiplexer(print_sink=lines.append)
    assert mux is not None
    task = mux.submit("what was the last thing I worked on?")
    assert task is not None
    result = await task
    assert result is not None

    joined = "\n".join(lines)
    # The ANSWER renders in Karen's voice…
    assert "💭 Karen ▸ The attach cockpit was the most recent work." in joined
    # …the thinking narration streamed live…
    assert "⎿ thinking · gathering organism context" in joined
    assert "⎿ thinking · asking claude" in joined
    # …and the routing telemetry stayed OFF the surface.
    assert "conf=" not in joined
    assert "turn: chat-" not in joined


async def test_logging_stub_keeps_dev_render(monkeypatch):
    """Executor flags OFF → the safe-default logging chain → the legacy
    decision render survives (dev mode: telemetry IS the product)."""
    monkeypatch.setenv("JARVIS_CONVERSATIONAL_MODE_ENABLED", "1")
    monkeypatch.delenv("JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", raising=False)
    from backend.core.ouroboros.governance.chat_text_bridge import (
        build_chat_text_multiplexer,
    )
    lines = []
    mux = build_chat_text_multiplexer(print_sink=lines.append)
    assert mux is not None
    task = mux.submit("what is O+V?")
    await task
    joined = "\n".join(lines)
    assert "logged-claude" in joined               # stub visible in dev mode
    assert "💭 Karen ▸ logged-" not in joined      # never voiced as an answer
