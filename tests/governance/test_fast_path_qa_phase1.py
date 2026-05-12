"""Regression spine for §41.3 #26 Phase 1 — D2c hybrid retrieval.

Operator-signed 2026-05-11: D2c approved. Phase 1 composes the
canonical :class:`semantic_index.SemanticIndex` (extended with
the new ``top_k_for_text`` method) via 3-tier confidence ladder:

* HIGH (top_score ≥ high_threshold): retrieval-only path, no
  Claude call, $0 cost. retrieval_path = ``retrieval_only``.
* MEDIUM (top_score ≥ low_threshold): hybrid path — snippets
  injected into Claude's system prompt. retrieval_path =
  ``hybrid_grounded``.
* LOW (no retrieval or below low threshold): Phase 0 Claude-
  direct path. retrieval_path = ``claude_direct``.

Operator binding: NO parallel embedder, NO parallel corpus, NO
duplicate cosine math. Composes the canonical
:meth:`SemanticIndex.top_k_for_text` we added — pure read,
NEVER raises.
"""
from __future__ import annotations

import ast
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from backend.core.ouroboros.governance import fast_path_qa as fpq
from backend.core.ouroboros.governance.fast_path_qa import (
    BoundedQAStore,
    QAArtifact,
    QAVerdict,
    RETRIEVAL_PATH_CLAUDE_DIRECT,
    RETRIEVAL_PATH_HYBRID,
    RETRIEVAL_PATH_RETRIEVAL_DISABLED,
    RETRIEVAL_PATH_RETRIEVAL_ONLY,
    _ENV_BUDGET_USD,
    _ENV_MASTER,
    _ENV_RETRIEVAL_ENABLED,
    _ENV_RETRIEVAL_HIGH_CONFIDENCE,
    _ENV_RETRIEVAL_LOW_CONFIDENCE,
    _ENV_RETRIEVAL_TOP_K,
    _RetrievalResult,
    _format_snippets_for_claude_prompt,
    _format_snippets_for_operator_answer,
    _retrieve_context,
    ask_question,
    register_flags,
    register_shipped_invariants,
    reset_cost_today,
    reset_default_qa_store,
    retrieval_enabled,
    retrieval_high_confidence_threshold,
    retrieval_low_confidence_threshold,
    retrieval_top_k,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """Each test runs with master ON, retrieval ON, fresh store
    + cost counter. Thresholds at defaults."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_BUDGET_USD, "5.0")
    monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, "true")
    reset_default_qa_store()
    reset_cost_today()
    yield


@dataclass(frozen=True)
class _FakeCorpusItem:
    text: str
    source: str = "project_doc"


def _make_retriever(*results):
    async def fake(query, k, min_score):
        return tuple(results)
    return fake


def _make_provider(answer="claude answer", cost=0.002):
    captured = {"system": None, "question": None, "calls": 0}

    async def fake(system, question):
        captured["system"] = system
        captured["question"] = question
        captured["calls"] += 1
        return (answer, cost)

    fake.captured = captured  # type: ignore[attr-defined]
    return fake


def _noop_bridge(role, text, source, op_id):
    pass


# --- Env knob accessors -----------------------------------------------------


def test_retrieval_enabled_default_true_when_master_on(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.delenv(_ENV_RETRIEVAL_ENABLED, raising=False)
    assert retrieval_enabled() is True


def test_retrieval_implicitly_off_when_master_off(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, "true")
    assert retrieval_enabled() is False


def test_retrieval_explicit_off(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, "false")
    assert retrieval_enabled() is False


def test_retrieval_off_aliases(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, off)
        assert retrieval_enabled() is False, off


def test_high_confidence_default(monkeypatch):
    monkeypatch.delenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, raising=False)
    assert retrieval_high_confidence_threshold() == 0.55


def test_high_confidence_clamps(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "-5")
    assert retrieval_high_confidence_threshold() == 0.0
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "999")
    assert retrieval_high_confidence_threshold() == 1.0


def test_high_confidence_garbage(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "garbage")
    assert retrieval_high_confidence_threshold() == 0.55


def test_low_confidence_default(monkeypatch):
    monkeypatch.delenv(_ENV_RETRIEVAL_LOW_CONFIDENCE, raising=False)
    assert retrieval_low_confidence_threshold() == 0.30


def test_top_k_default(monkeypatch):
    monkeypatch.delenv(_ENV_RETRIEVAL_TOP_K, raising=False)
    assert retrieval_top_k() == 5


def test_top_k_clamps(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_TOP_K, "0")
    assert retrieval_top_k() == 1
    monkeypatch.setenv(_ENV_RETRIEVAL_TOP_K, "99999")
    assert retrieval_top_k() == 50


# --- _retrieve_context ------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_with_no_results():
    async def empty(q, k, ms):
        return ()
    r = await _retrieve_context("Q?", retrieval_callable=empty)
    assert r.top_score == 0.0
    assert r.snippets == ()
    assert r.item_count == 0


@pytest.mark.asyncio
async def test_retrieve_with_results():
    async def fake(q, k, ms):
        return (
            (_FakeCorpusItem("relevant fact A"), 0.8),
            (_FakeCorpusItem("relevant fact B"), 0.6),
        )
    r = await _retrieve_context("Q?", retrieval_callable=fake)
    assert r.top_score == 0.8
    assert len(r.snippets) == 2
    assert r.snippets[0][1] == "relevant fact A"
    assert r.snippets[0][2] == 0.8


@pytest.mark.asyncio
async def test_retrieve_handles_callable_raise():
    async def crashy(q, k, ms):
        raise RuntimeError("index offline")
    r = await _retrieve_context("Q?", retrieval_callable=crashy)
    assert r.top_score == 0.0
    assert "raised" in r.diagnostic


@pytest.mark.asyncio
async def test_retrieve_skips_items_with_empty_text():
    async def fake(q, k, ms):
        return (
            (_FakeCorpusItem(""), 0.9),
            (_FakeCorpusItem("real text"), 0.5),
        )
    r = await _retrieve_context("Q?", retrieval_callable=fake)
    assert len(r.snippets) == 1
    assert r.snippets[0][1] == "real text"


@pytest.mark.asyncio
async def test_retrieve_skips_malformed_pairs():
    async def fake(q, k, ms):
        return (
            (_FakeCorpusItem("ok"), 0.5),
            "not a tuple",
            (None, "not-a-float"),
        )
    r = await _retrieve_context("Q?", retrieval_callable=fake)
    assert len(r.snippets) == 1


# --- Snippet formatters -----------------------------------------------------


def test_format_claude_prompt_empty():
    assert _format_snippets_for_claude_prompt(()) == ""


def test_format_claude_prompt_includes_snippets():
    out = _format_snippets_for_claude_prompt((
        ("git_commit", "fixed bug X", 0.8),
        ("conversation", "operator asked about Y", 0.6),
    ))
    assert "Relevant project context" in out
    assert "fixed bug X" in out
    assert "operator asked about Y" in out
    assert "git_commit" in out
    assert "0.80" in out


def test_format_operator_answer_empty():
    assert _format_snippets_for_operator_answer(()) == ""


def test_format_operator_answer_no_claude_disclaimer():
    out = _format_snippets_for_operator_answer((
        ("project_doc", "CONTEXT_EXPANSION pulls memory", 0.9),
    ))
    assert "without invoking Claude" in out
    assert "CONTEXT_EXPANSION pulls memory" in out


# --- 3-tier confidence ladder ------------------------------------------------


@pytest.mark.asyncio
async def test_high_confidence_path_retrieval_only(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "0.5")
    retrieve = _make_retriever(
        (_FakeCorpusItem("definitive answer text"), 0.85),
    )
    provider = _make_provider()
    report = await ask_question(
        "What is X?",
        provider_callable=provider,
        bridge_callable=_noop_bridge,
        retrieval_callable=retrieve,
    )
    assert report.verdict is QAVerdict.ANSWERED
    assert report.artifact is not None
    assert report.artifact.retrieval_path == RETRIEVAL_PATH_RETRIEVAL_ONLY
    assert report.artifact.cost_usd == 0.0
    assert "definitive answer text" in report.artifact.answer
    assert "without invoking Claude" in report.artifact.answer
    assert provider.captured["calls"] == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_medium_confidence_path_hybrid_grounded(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "0.7")
    monkeypatch.setenv(_ENV_RETRIEVAL_LOW_CONFIDENCE, "0.3")
    retrieve = _make_retriever(
        (_FakeCorpusItem("context A"), 0.5),
        (_FakeCorpusItem("context B"), 0.4),
    )
    provider = _make_provider("claude grounded answer", cost=0.001)
    report = await ask_question(
        "Q?",
        provider_callable=provider,
        bridge_callable=_noop_bridge,
        retrieval_callable=retrieve,
    )
    assert report.verdict is QAVerdict.ANSWERED
    assert report.artifact is not None
    assert report.artifact.retrieval_path == RETRIEVAL_PATH_HYBRID
    assert report.artifact.answer == "claude grounded answer"
    assert report.artifact.cost_usd == 0.001
    assert provider.captured["calls"] == 1  # type: ignore[attr-defined]
    assert "context A" in provider.captured["system"]  # type: ignore[attr-defined]
    assert "context B" in provider.captured["system"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_low_confidence_path_claude_direct(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_LOW_CONFIDENCE, "0.5")
    retrieve = _make_retriever(
        (_FakeCorpusItem("loose context"), 0.2),
    )
    provider = _make_provider("plain claude answer", cost=0.002)
    report = await ask_question(
        "Q?",
        provider_callable=provider,
        bridge_callable=_noop_bridge,
        retrieval_callable=retrieve,
    )
    assert report.verdict is QAVerdict.ANSWERED
    assert report.artifact is not None
    assert report.artifact.retrieval_path == RETRIEVAL_PATH_CLAUDE_DIRECT
    assert "loose context" not in provider.captured["system"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_empty_retrieval_falls_to_claude_direct():
    retrieve = _make_retriever()
    provider = _make_provider("plain answer")
    report = await ask_question(
        "Q?",
        provider_callable=provider,
        bridge_callable=_noop_bridge,
        retrieval_callable=retrieve,
    )
    assert report.artifact is not None
    assert report.artifact.retrieval_path == RETRIEVAL_PATH_CLAUDE_DIRECT


@pytest.mark.asyncio
async def test_retrieval_disabled_skips_entirely(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, "false")
    provider = _make_provider("plain claude answer")

    async def asserting_retrieve(q, k, ms):
        raise AssertionError("retrieval should NOT be called when sub-flag off")

    report = await ask_question(
        "Q?",
        provider_callable=provider,
        bridge_callable=_noop_bridge,
        retrieval_callable=asserting_retrieve,
    )
    assert report.artifact is not None
    assert (
        report.artifact.retrieval_path
        == RETRIEVAL_PATH_RETRIEVAL_DISABLED
    )
    assert provider.captured["calls"] == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_top_score_recorded_on_artifact(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "0.9")
    monkeypatch.setenv(_ENV_RETRIEVAL_LOW_CONFIDENCE, "0.3")
    retrieve = _make_retriever(
        (_FakeCorpusItem("midweight ctx"), 0.55),
    )
    provider = _make_provider("answer")
    report = await ask_question(
        "Q?",
        provider_callable=provider,
        bridge_callable=_noop_bridge,
        retrieval_callable=retrieve,
    )
    assert report.artifact is not None
    assert report.artifact.top_score == pytest.approx(0.55, abs=0.001)


@pytest.mark.asyncio
async def test_retrieval_only_path_records_assistant_turn():
    captured: List[Tuple[str, str, str, str]] = []

    def recorder(role, text, source, op_id):
        captured.append((role, text, source, op_id))

    retrieve = _make_retriever(
        (_FakeCorpusItem("high-confidence text"), 0.95),
    )
    await ask_question(
        "Q?",
        op_id="op-d4-rt",
        provider_callable=_make_provider(),
        bridge_callable=recorder,
        retrieval_callable=retrieve,
    )
    assert len(captured) == 2
    assert captured[0][0] == "user"
    assert captured[0][2] == "ask_human_q"
    assert captured[1][0] == "assistant"
    assert captured[1][2] == "ask_human_a"


@pytest.mark.asyncio
async def test_artifact_carries_provenance_model():
    retrieve = _make_retriever(
        (_FakeCorpusItem("text"), 0.99),
    )
    report = await ask_question(
        "Q?",
        provider_callable=_make_provider(),
        bridge_callable=_noop_bridge,
        retrieval_callable=retrieve,
    )
    assert report.artifact is not None
    assert report.artifact.model == "semantic_index"


@pytest.mark.asyncio
async def test_retrieval_failure_falls_through_to_phase_0(monkeypatch):
    async def crashy(q, k, ms):
        raise RuntimeError("retrieval system melted")

    provider = _make_provider("claude saved the day")
    report = await ask_question(
        "Q?",
        provider_callable=provider,
        bridge_callable=_noop_bridge,
        retrieval_callable=crashy,
    )
    assert report.verdict is QAVerdict.ANSWERED
    assert report.artifact is not None
    assert (
        report.artifact.retrieval_path == RETRIEVAL_PATH_CLAUDE_DIRECT
    )


# --- semantic_index.top_k_for_text extension -------------------------------


def test_semantic_index_top_k_method_present():
    from backend.core.ouroboros.governance.semantic_index import (
        SemanticIndex,
    )
    assert hasattr(SemanticIndex, "top_k_for_text")


def test_semantic_index_top_k_signature():
    import inspect as _inspect
    from backend.core.ouroboros.governance.semantic_index import (
        SemanticIndex,
    )
    sig = _inspect.signature(SemanticIndex.top_k_for_text)
    params = list(sig.parameters.keys())
    assert "text" in params
    assert "k" in params
    assert "min_score" in params


# --- QAArtifact extensions --------------------------------------------------


def test_artifact_default_retrieval_path():
    store = BoundedQAStore(capacity=5)
    a = store.store(question="q", answer="a")
    assert a.retrieval_path == RETRIEVAL_PATH_CLAUDE_DIRECT
    assert a.top_score == 0.0


def test_artifact_to_dict_carries_new_fields():
    store = BoundedQAStore(capacity=5)
    a = store.store(
        question="q",
        answer="a",
        retrieval_path=RETRIEVAL_PATH_HYBRID,
        top_score=0.65,
    )
    d = a.to_dict()
    assert d["retrieval_path"] == "hybrid_grounded"
    assert d["top_score"] == pytest.approx(0.65)


def test_artifact_top_score_clamps():
    store = BoundedQAStore(capacity=5)
    a = store.store(question="q", answer="a", top_score="not_a_float")
    assert a.top_score == 0.0
    b = store.store(question="q", answer="a", top_score=99.0)
    assert b.top_score == 1.0
    c = store.store(question="q", answer="a", top_score=-99.0)
    assert c.top_score == -1.0


# --- AST pins ---------------------------------------------------------------


def _load_source_tree():
    p = Path("backend/core/ouroboros/governance/fast_path_qa.py")
    src = p.read_text()
    return src, ast.parse(src)


def test_ast_pin_composes_canonical_now_requires_semantic_index():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_composes_canonical_catches_missing_semantic_index():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = '''
import anthropic
from collections import OrderedDict
import threading
# conversation_bridge mentioned
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_composes_canonical_catches_missing_top_k():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = '''
import anthropic
from collections import OrderedDict
import threading
# conversation_bridge mentioned
# semantic_index mentioned
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


# --- FlagRegistry seeds -----------------------------------------------------


def test_register_flags_includes_phase1_seeds():
    class _R:
        def __init__(self):
            self.specs = []
        def register(self, spec):
            self.specs.append(spec)
    r = _R()
    register_flags(r)
    names = [s.name for s in r.specs]
    assert _ENV_RETRIEVAL_ENABLED in names
    assert _ENV_RETRIEVAL_HIGH_CONFIDENCE in names
    assert _ENV_RETRIEVAL_LOW_CONFIDENCE in names
    assert _ENV_RETRIEVAL_TOP_K in names


def test_register_flags_phase1_retrieval_default_true():
    class _R:
        def __init__(self):
            self.specs = []
        def register(self, spec):
            self.specs.append(spec)
    r = _R()
    register_flags(r)
    spec = next(
        s for s in r.specs if s.name == _ENV_RETRIEVAL_ENABLED
    )
    assert spec.default is True


# --- Retrieval path constants -----------------------------------------------


def test_retrieval_path_constants():
    paths = {
        RETRIEVAL_PATH_RETRIEVAL_ONLY,
        RETRIEVAL_PATH_HYBRID,
        RETRIEVAL_PATH_CLAUDE_DIRECT,
        RETRIEVAL_PATH_RETRIEVAL_DISABLED,
    }
    assert len(paths) == 4
    assert "retrieval_only" in paths
    assert "hybrid_grounded" in paths
    assert "claude_direct" in paths
    assert "retrieval_disabled" in paths


# --- Budget interaction -----------------------------------------------------


@pytest.mark.asyncio
async def test_high_confidence_path_costs_zero_against_budget(monkeypatch):
    monkeypatch.setenv(_ENV_BUDGET_USD, "0.01")
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "0.5")
    retrieve = _make_retriever(
        (_FakeCorpusItem("high"), 0.99),
    )
    provider = _make_provider("never_called", cost=100.0)
    for _ in range(3):
        report = await ask_question(
            "Q?",
            provider_callable=provider,
            bridge_callable=_noop_bridge,
            retrieval_callable=retrieve,
        )
        assert report.verdict is QAVerdict.ANSWERED
        assert (
            report.artifact.retrieval_path
            == RETRIEVAL_PATH_RETRIEVAL_ONLY
        )
    assert provider.captured["calls"] == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_hybrid_path_still_records_cost(monkeypatch):
    monkeypatch.setenv(_ENV_RETRIEVAL_HIGH_CONFIDENCE, "0.95")
    monkeypatch.setenv(_ENV_RETRIEVAL_LOW_CONFIDENCE, "0.3")
    retrieve = _make_retriever(
        (_FakeCorpusItem("context"), 0.5),
    )
    provider = _make_provider("grounded answer", cost=0.01)
    report = await ask_question(
        "Q?",
        provider_callable=provider,
        bridge_callable=_noop_bridge,
        retrieval_callable=retrieve,
    )
    assert report.artifact.retrieval_path == RETRIEVAL_PATH_HYBRID
    assert report.artifact.cost_usd == 0.01
    assert fpq.cost_today_usd() == pytest.approx(0.01)
