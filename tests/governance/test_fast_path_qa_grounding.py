"""``ask_question(grounding=...)`` — the seam the `/btw` lane grounds on.

A side question is asked ABOUT the moment: *"why is THAT slow?"* The
semantic index cannot answer that, because the referent is the live op
and not the repository. So the caller supplies its own context block.

Three properties, and each is a way the naive version goes wrong:

* it reaches the SYSTEM prompt, so it actually grounds the answer;
* it NEVER reaches the stored question, or ``/expand q-N`` re-reads a
  sentence the operator never typed;
* it composes with retrieval instead of replacing it, and lands LAST —
  retrieved snippets describe the repository, grounding describes the
  moment, and where they disagree the moment is the newer fact.

Plus the compatibility property that makes the default safe: omitting it
is byte-identical to the pre-grounding behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.core.ouroboros.governance.fast_path_qa import (
    RETRIEVAL_PATH_HYBRID,
    RETRIEVAL_PATH_RETRIEVAL_ONLY,
    QAVerdict,
    _ENV_BUDGET_USD,
    _ENV_MASTER,
    _ENV_RETRIEVAL_ENABLED,
    _ENV_RETRIEVAL_HIGH_CONFIDENCE,
    _ENV_RETRIEVAL_LOW_CONFIDENCE,
    ask_question,
    reset_cost_today,
    reset_default_qa_store,
    system_prompt,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_BUDGET_USD, "5.0")
    monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, "false")
    reset_default_qa_store()
    reset_cost_today()
    yield


@dataclass(frozen=True)
class _Item:
    text: str
    source: str = "project_doc"


def _provider(answer="answered", cost=0.001):
    captured = {"system": None, "question": None, "calls": 0}

    async def fake(system, question):
        captured["system"] = system
        captured["question"] = question
        captured["calls"] += 1
        return (answer, cost)

    fake.captured = captured  # type: ignore[attr-defined]
    return fake


def _retriever(*results):
    async def fake(query, k, min_score):
        return tuple(results)
    return fake


def _bridge(role, text, source, op_id):
    pass


_GROUND = "Live situation: op-42 is in GENERATE on DoubleWord."


async def test_grounding_reaches_the_system_prompt():
    provider = _provider()
    report = await ask_question(
        "why is that slow?",
        provider_callable=provider,
        bridge_callable=_bridge,
        grounding=_GROUND,
    )
    assert report.verdict is QAVerdict.ANSWERED
    system = provider.captured["system"]  # type: ignore[attr-defined]
    assert _GROUND in system
    assert system.startswith(system_prompt())


async def test_grounding_never_reaches_the_question():
    """The parked q-N must record what the operator actually asked."""
    provider = _provider()
    report = await ask_question(
        "why is that slow?",
        provider_callable=provider,
        bridge_callable=_bridge,
        grounding=_GROUND,
    )
    assert provider.captured["question"] == "why is that slow?"  # type: ignore[attr-defined]
    assert report.artifact is not None
    assert report.artifact.question == "why is that slow?"
    assert _GROUND not in report.artifact.question


async def test_grounding_never_reaches_the_conversation_bridge():
    turns = []

    def _record(role, text, source, op_id):
        turns.append((role, text))

    await ask_question(
        "why is that slow?",
        provider_callable=_provider(),
        bridge_callable=_record,
        grounding=_GROUND,
    )
    assert ("user", "why is that slow?") in turns
    assert all(_GROUND not in text for _role, text in turns)


async def test_omitting_grounding_is_byte_identical(monkeypatch):
    plain = _provider()
    await ask_question(
        "q?", provider_callable=plain, bridge_callable=_bridge,
    )
    empty = _provider()
    await ask_question(
        "q?", provider_callable=empty, bridge_callable=_bridge,
        grounding="",
    )
    assert plain.captured["system"] == empty.captured["system"]  # type: ignore[attr-defined]
    assert plain.captured["system"] == system_prompt()  # type: ignore[attr-defined]


@pytest.mark.parametrize("value", ["   ", "\n\n", None])
async def test_blank_grounding_adds_nothing(value):
    provider = _provider()
    await ask_question(
        "q?", provider_callable=provider, bridge_callable=_bridge,
        grounding=value,
    )
    assert provider.captured["system"] == system_prompt()  # type: ignore[attr-defined]


async def test_grounding_lands_after_retrieved_snippets(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, "true")
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "0.7")
    monkeypatch.setenv(_ENV_RETRIEVAL_LOW_CONFIDENCE, "0.3")
    provider = _provider()
    report = await ask_question(
        "q?",
        provider_callable=provider,
        bridge_callable=_bridge,
        retrieval_callable=_retriever((_Item("REPO CONTEXT"), 0.5)),
        grounding=_GROUND,
    )
    assert report.artifact is not None
    assert report.artifact.retrieval_path == RETRIEVAL_PATH_HYBRID
    system = provider.captured["system"]  # type: ignore[attr-defined]
    # Both present, and the moment comes last.
    assert "REPO CONTEXT" in system and _GROUND in system
    assert system.index("REPO CONTEXT") < system.index(_GROUND)


async def test_the_retrieval_only_path_ignores_grounding(monkeypatch):
    """That path invokes no provider at all. Grounding a call that does
    not happen would be a claim of context the answer does not carry."""
    monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, "true")
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "0.5")
    provider = _provider()
    report = await ask_question(
        "q?",
        provider_callable=provider,
        bridge_callable=_bridge,
        retrieval_callable=_retriever((_Item("definitive"), 0.9)),
        grounding=_GROUND,
    )
    assert report.artifact is not None
    assert report.artifact.retrieval_path == RETRIEVAL_PATH_RETRIEVAL_ONLY
    assert provider.captured["calls"] == 0  # type: ignore[attr-defined]
    assert _GROUND not in report.artifact.answer


async def test_a_non_string_grounding_degrades_rather_than_raises():
    provider = _provider()
    report = await ask_question(
        "q?", provider_callable=provider, bridge_callable=_bridge,
        grounding=object(),  # type: ignore[arg-type]
    )
    # NEVER raises is the substrate's contract; a caller passing garbage
    # loses its context, not its answer.
    assert report.verdict is QAVerdict.ANSWERED


def test_grounding_is_keyword_only_and_defaulted():
    """Positional would reorder an existing caller's arguments; a
    missing default would break every existing call site."""
    import inspect
    param = inspect.signature(ask_question).parameters["grounding"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default == ""
