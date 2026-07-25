"""Persona matrix + phatic fast path — the three mandated assertions.

1. An injected "Hello Karen" transcript intercepts the network call and
   returns a cached string.
2. The AgentRegistry swaps the TTS output profile to Karen.
3. The bypassed turn is written to the mocked conversation history.

Everything here drives the REAL pipeline method. A test that called
``classify`` directly would prove the classifier works and say nothing about
whether the interceptor is on the turn path — which is the failure mode this
codebase has hit repeatedly: a guard with no caller is theatre.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.audio.conversation_pipeline import ConversationPipeline
from backend.voice.agent_persona import AgentPersona
from backend.voice.agent_registry import (
    all_agents,
    route_by_wake_word,
    spec_for,
    voice_profile_for,
)
from backend.voice.phatic_fastpath import acknowledge, classify


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RecordingSession:
    """The mocked conversation history of assertion 3."""

    def __init__(self) -> None:
        self.turns: List[Tuple[str, str]] = []

    def add_turn(self, role: str, content: str) -> None:
        self.turns.append((role, content))

    def get_messages(self) -> List[Dict[str, str]]:
        return [{"role": r, "content": c} for r, c in self.turns]

    def get_context_for_llm(self) -> List[Dict[str, str]]:
        return self.get_messages()


#: The reply the model would give if it were reached. Distinct from every
#: cached acknowledgement, so "which path answered" is visible in the output
#: rather than inferred.
MODEL_REPLY = "__from_the_model__"


class _CountingLLM:
    """Truthy stand-in for the LLM client.

    Counting rather than raising: ``_generate_and_speak_response`` wraps the
    generation branch in a broad ``except`` that logs and continues, so a
    double that raised would be SWALLOWED and the test would pass whether or
    not the interceptor worked. The counter survives that handler."""

    def __init__(self) -> None:
        self.calls = 0


class _WholeTextSplitter:
    """Sentence splitting is not what these assertions are about."""

    async def split(self, stream: Any):
        async for chunk in stream:
            yield chunk


def _pipeline(session: _RecordingSession, llm: _CountingLLM) -> ConversationPipeline:
    """A pipeline with only the collaborators these assertions touch.

    Built with ``__new__`` deliberately: ``__init__`` mounts an audio device,
    an STT model and a TTS engine, none of which the fast path involves, and
    requiring them would make this an integration test of the sound card."""
    p = ConversationPipeline.__new__(ConversationPipeline)
    p._session = session
    p._barge_in = None
    p._llm_client = llm
    p._audio_bus = None            # forces the non-streamed branch
    p._sentence_splitter = _WholeTextSplitter()
    p._system_prompt = "test"
    p._spoken = []

    async def _speak(sentence: str, _cancel: Any) -> None:
        p._spoken.append(sentence)

    async def _stream(*_a: Any, **_kw: Any):
        # The network seam. Entering it at all is what assertion 1 forbids
        # for a greeting — and what the inverse test REQUIRES for a question.
        llm.calls += 1
        yield MODEL_REPLY

    p._speak_sentence = _speak                                  # type: ignore
    p._get_llm_stream = _stream                                 # type: ignore
    return p


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No operator override may decide these outcomes."""
    for key in (
        "JARVIS_VOICE_OV", "JARVIS_VOICE_KAREN", "JARVIS_VOICE_JARVIS",
        "JARVIS_PHATIC_RESPONSES", "JARVIS_PHATIC_MAX_WORDS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JARVIS_PHATIC_FASTPATH", "true")
    monkeypatch.setenv("JARVIS_AGENT_PERSONA", "ov")


# ---------------------------------------------------------------------------
# Assertion 1 — the greeting never reaches the network
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_karen_intercepts_the_network_call() -> None:
    session, llm = _RecordingSession(), _CountingLLM()
    pipe = _pipeline(session, llm)

    await pipe._generate_and_speak_response("Hello Karen")

    assert llm.calls == 0, "the model was called for a pure greeting"
    assert pipe._spoken, "the fast path produced no spoken reply"
    assert pipe._spoken[0].strip(), "the cached reply was empty"
    assert MODEL_REPLY not in pipe._spoken


@pytest.mark.asyncio
async def test_a_real_question_is_not_intercepted() -> None:
    """The inverse, which is the assertion that actually matters.

    A fast path that swallowed questions would pass the test above while
    being catastrophically wrong: answering "what's my disk usage" with
    "I'm here" is a lie, and the bias is explicitly asymmetric against it."""
    session, llm = _RecordingSession(), _CountingLLM()
    pipe = _pipeline(session, llm)

    await pipe._generate_and_speak_response("Karen what is my disk usage")

    assert llm.calls == 1, "a real question was answered from the cache"
    assert pipe._spoken == [MODEL_REPLY]


@pytest.mark.parametrize(
    "utterance",
    ["Hello Karen", "hey karen", "yo karen you around", "Karen?",
     "are you there jarvis", "good morning karen"],
)
def test_phrasings_never_enumerated_still_classify(utterance: str) -> None:
    """Generalisation, not recall: none of these is a stored string."""
    assert classify(utterance, agent_names=["karen", "jarvis", "ov"])


@pytest.mark.parametrize(
    "utterance",
    ["karen what is my disk usage", "hey karen reboot the server",
     "hello karen how is the build", "karen deploy to production",
     "what time is it", "is it the"],
)
def test_content_is_refused(utterance: str) -> None:
    verdict = classify(utterance, agent_names=["karen", "jarvis", "ov"])
    assert not verdict, f"{utterance!r} was swallowed by the cache"


# ---------------------------------------------------------------------------
# Assertion 2 — the registry swaps the TTS output profile
# ---------------------------------------------------------------------------


def test_registry_maps_ov_to_karen_and_jarvis_to_daniel() -> None:
    assert spec_for(AgentPersona.OV).preferred_voice == "Karen"
    assert spec_for(AgentPersona.JARVIS).preferred_voice == "Daniel"
    # KAREN is an ALIAS for the OV agent, not a second agent.
    assert spec_for(AgentPersona.KAREN) is spec_for(AgentPersona.OV)


def test_registry_swaps_the_tts_output_profile() -> None:
    """Assertion 2. Resolution is machine-dependent — a voice may not be
    installed — so what is asserted is that the registry's INTENT reaches the
    resolver, which is the seam under test. Where the voice exists, the
    profile carries it and says the registry chose it."""
    for persona, want in ((AgentPersona.OV, "Karen"), (AgentPersona.JARVIS, "Daniel")):
        profile = voice_profile_for(persona)
        assert profile is not None
        if profile.source == "registry":
            assert profile.voice == want
        else:
            # Not installed on this machine: it must have degraded through the
            # shared chain, never silently become the OTHER agent's voice.
            other = "Daniel" if want == "Karen" else "Karen"
            assert profile.voice != other


@pytest.mark.asyncio
async def test_wake_word_switches_the_active_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Addressing JARVIS in Karen's cockpit hands the turn to JARVIS."""
    session, llm = _RecordingSession(), _CountingLLM()
    pipe = _pipeline(session, llm)
    monkeypatch.setenv("JARVIS_AGENT_PERSONA", "ov")

    assert pipe._route_persona("Hey JARVIS are you there") is True
    assert os.environ["JARVIS_AGENT_PERSONA"] == "jarvis"

    # An unaddressed turn is a CONTINUATION — the agent must not drift.
    assert pipe._route_persona("what is my disk usage") is False
    assert os.environ["JARVIS_AGENT_PERSONA"] == "jarvis"

    assert pipe._route_persona("Karen, you there?") is True
    assert os.environ["JARVIS_AGENT_PERSONA"] == "ov"


def test_first_named_agent_wins() -> None:
    """"Karen, ask JARVIS to reboot" addresses Karen ABOUT JARVIS."""
    assert route_by_wake_word("Karen, ask JARVIS to reboot").persona is AgentPersona.OV
    assert route_by_wake_word("JARVIS, tell Karen to stop").persona is AgentPersona.JARVIS
    assert route_by_wake_word("what is my disk usage") is None


def test_wake_words_are_word_boundaried() -> None:
    """The substring lesson, which cost this codebase a Weather app that
    opened on "the brain is thinking"."""
    assert route_by_wake_word("move it over there") is None
    assert route_by_wake_word("the karenina novel") is None


# ---------------------------------------------------------------------------
# Assertion 3 — the bypassed turn reaches the history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bypassed_turn_is_written_to_history() -> None:
    """Assertion 3.

    A bypassed greeting that vanished would leave the NEXT complex turn
    reasoning about a conversation whose opening it never saw — the model
    would be told the operator's first words were a question that in fact
    followed a greeting it answered."""
    session, llm = _RecordingSession(), _CountingLLM()
    pipe = _pipeline(session, llm)

    await pipe._generate_and_speak_response("Hello Karen")

    assert session.turns, "the bypassed turn left no trace in history"
    role, content = session.turns[-1]
    assert role == "assistant"
    assert content == pipe._spoken[0], (
        "history recorded something other than what was actually said"
    )


@pytest.mark.asyncio
async def test_history_survives_into_the_next_turn() -> None:
    """The point of assertion 3, stated as behaviour: the greeting is still
    visible when the next turn is composed."""
    session, llm = _RecordingSession(), _CountingLLM()
    pipe = _pipeline(session, llm)

    session.add_turn("user", "Hello Karen")
    await pipe._generate_and_speak_response("Hello Karen")

    msgs = session.get_messages()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "Hello Karen"


# ---------------------------------------------------------------------------
# Properties of the cache itself
# ---------------------------------------------------------------------------


def test_acknowledgement_is_deterministic_and_varies_by_utterance() -> None:
    a1 = acknowledge("Hello Karen")
    a2 = acknowledge("Hello Karen")
    assert a1 == a2, "the same greeting must not get a different answer"
    variants = {acknowledge(g) for g in ("hi", "hey karen", "yo", "morning", "you there")}
    assert len(variants) > 1, "every greeting collapsed to one reply"


def test_no_acknowledgement_claims_knowledge_or_action() -> None:
    """The fast path answers being ADDRESSED. If a pool entry ever answered a
    question, a phrasing this classifier accepts would start producing a
    confident lie."""
    from backend.voice.phatic_fastpath import _ACKS

    forbidden = ("i have", "i found", "i ran", "done", "completed", "the answer")
    for ack in _ACKS:
        low = ack.lower()
        assert not any(f in low for f in forbidden), ack


def test_fastpath_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF must send everything to the model — the only honest way to compare
    the two paths on cost and latency."""
    monkeypatch.setenv("JARVIS_PHATIC_FASTPATH", "false")
    session, llm = _RecordingSession(), _CountingLLM()
    pipe = _pipeline(session, llm)

    asyncio.run(pipe._generate_and_speak_response("Hello Karen"))

    assert llm.calls == 1, "the fast path fired with the master switch OFF"
    assert pipe._spoken == [MODEL_REPLY]


# ---------------------------------------------------------------------------
# The lane generalisation
# ---------------------------------------------------------------------------


def test_each_agent_gets_its_own_lane_not_a_shared_one() -> None:
    from backend.core.ouroboros.governance.agent_voice_lane import lane_for

    ov, jarvis = lane_for(AgentPersona.OV), lane_for(AgentPersona.JARVIS)
    assert ov is not jarvis
    assert ov.ledger._path != jarvis.ledger._path
    assert ov.prefix != jarvis.prefix
    # OV keeps the pre-generalisation namespace and file, so measurements
    # already on disk are inherited rather than orphaned.
    assert ov.prefix == "JARVIS_KAREN_VOICE"
    assert ov.ledger._path.name == "karen_voice_lane.json"
    # KAREN is an alias — one lane, or a demotion on one spelling would leave
    # the other still electing the mute model.
    assert lane_for(AgentPersona.KAREN) is ov


def test_lane_knobs_inherit_then_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core.ouroboros.governance import karen_voice_lane as kvl

    monkeypatch.setenv("JARVIS_KAREN_VOICE_TTFT_BUDGET_S", "1.5")
    monkeypatch.delenv("JARVIS_JARVIS_VOICE_TTFT_BUDGET_S", raising=False)
    assert kvl.spoken_ttft_budget_s(prefix="JARVIS_JARVIS_VOICE") == 1.5

    monkeypatch.setenv("JARVIS_JARVIS_VOICE_TTFT_BUDGET_S", "2.4")
    assert kvl.spoken_ttft_budget_s(prefix="JARVIS_JARVIS_VOICE") == 2.4
    assert kvl.spoken_ttft_budget_s() == 1.5, "the override leaked across lanes"


def test_probe_is_bound_to_the_agents_own_identity() -> None:
    """A probe measures TTFT for a real spoken turn, and the prompt is part of
    that measurement — grading JARVIS's models with Karen's prompt measures
    the wrong workload."""
    from backend.core.ouroboros.governance.agent_voice_lane import lane_for

    assert "JARVIS" in lane_for(AgentPersona.JARVIS)._probe_user()
    assert "Karen" in lane_for(AgentPersona.OV)._probe_user()


def test_every_registered_agent_is_complete() -> None:
    """A half-registered agent — wake word but no voice — would answer in
    whatever voice the last turn left behind."""
    for spec in all_agents():
        assert spec.display_name and spec.preferred_voice and spec.wake_words
        assert spec.persona.canonical is spec.persona
