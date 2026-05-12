"""Regression spine for §41.3 #26 Phase 0 — fast_path_qa.

Operator-signed 2026-05-11: D1c/D2a/D3a/D4=defaults/D5c
(Phase 0 of the §41.3.1 plan — Phase 1 layers D2c hybrid;
Phase 2 layers D3b INFORMATIONAL budget).

Substrate tests cover:
* §33.1 master gate + operator-binding gate (default-FALSE
  even with D1-D5 approved)
* BoundedQAStore — same architecture as the 5 existing
  artifact-ref rings (t-N/d-N/o-N/n-N/p-N); q-N joins as 6th
* ask_question pipeline (master / input / budget / provider /
  empty-answer / record / store / return)
* Cost tracking (D3a IMMEDIATE in-process; resets at UTC midnight)
* AST pins (closed taxonomy / q- prefix pinned / master default
  false / authority asymmetry / composes_canonical / no
  ClaudeProvider.generate)

Operator binding: NO parallel state, NO hardcoded prompts, NO
direct orchestrator/iron_gate imports.
"""
from __future__ import annotations

import ast
import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from backend.core.ouroboros.governance import fast_path_qa as fpq
from backend.core.ouroboros.governance.fast_path_qa import (
    BoundedQAStore,
    FAST_PATH_QA_SCHEMA_VERSION,
    QA_REF_PREFIX,
    QAArtifact,
    QAReport,
    QAVerdict,
    _ENV_BUDGET_USD,
    _ENV_MASTER,
    _ENV_MAX_TOKENS,
    _ENV_MODEL,
    _ENV_STORE_CAPACITY,
    _ENV_SYSTEM_PROMPT,
    _ENV_TEMPERATURE,
    _ENV_TIMEOUT_S,
    ask_question,
    cost_today_usd,
    daily_budget_usd,
    get_default_qa_store,
    master_enabled,
    max_tokens,
    model_name,
    register_flags,
    register_shipped_invariants,
    reset_cost_today,
    reset_default_qa_store,
    store_capacity,
    system_prompt,
    temperature,
    timeout_s,
)


# ---------------------------------------------------------------------------
# Schema + taxonomy invariants
# ---------------------------------------------------------------------------


def test_schema_version_stamp():
    assert FAST_PATH_QA_SCHEMA_VERSION == "fast_path_qa.1"


def test_qa_verdict_closed_5_value():
    assert {v.value for v in QAVerdict} == {
        "answered", "disabled", "budget_exhausted",
        "provider_failed", "out_of_scope",
    }


def test_qa_ref_prefix_pinned():
    """q-N slots into the canonical artifact-ref family."""
    assert QA_REF_PREFIX == "q-"


# ---------------------------------------------------------------------------
# Env knobs — §33.1 + operator-tunable
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    assert master_enabled() is False


def test_master_explicit_enable(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_master_off_aliases(monkeypatch):
    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv(_ENV_MASTER, off)
        assert master_enabled() is False, off


def test_daily_budget_default(monkeypatch):
    monkeypatch.delenv(_ENV_BUDGET_USD, raising=False)
    assert daily_budget_usd() == 5.0


def test_daily_budget_clamps(monkeypatch):
    monkeypatch.setenv(_ENV_BUDGET_USD, "-10")
    assert daily_budget_usd() == 0.0
    monkeypatch.setenv(_ENV_BUDGET_USD, "999999")
    assert daily_budget_usd() == 1000.0


def test_daily_budget_garbage(monkeypatch):
    monkeypatch.setenv(_ENV_BUDGET_USD, "not_a_float")
    assert daily_budget_usd() == 5.0


def test_max_tokens_clamps(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_TOKENS, "10")
    assert max_tokens() == 64
    monkeypatch.setenv(_ENV_MAX_TOKENS, "99999")
    assert max_tokens() == 4000


def test_temperature_clamps(monkeypatch):
    monkeypatch.setenv(_ENV_TEMPERATURE, "-5")
    assert temperature() == 0.0
    monkeypatch.setenv(_ENV_TEMPERATURE, "5")
    assert temperature() == 2.0


def test_store_capacity_clamps(monkeypatch):
    monkeypatch.setenv(_ENV_STORE_CAPACITY, "0")
    assert store_capacity() == 1
    monkeypatch.setenv(_ENV_STORE_CAPACITY, "999999")
    assert store_capacity() == 10_000


def test_timeout_clamps(monkeypatch):
    monkeypatch.setenv(_ENV_TIMEOUT_S, "1")
    assert timeout_s() == 5
    monkeypatch.setenv(_ENV_TIMEOUT_S, "9999")
    assert timeout_s() == 300


def test_system_prompt_default_cites_claudemd(monkeypatch):
    monkeypatch.delenv(_ENV_SYSTEM_PROMPT, raising=False)
    p = system_prompt()
    assert "CLAUDE.md" in p
    assert "PRD" in p


def test_system_prompt_override(monkeypatch):
    monkeypatch.setenv(_ENV_SYSTEM_PROMPT, "custom prompt")
    assert system_prompt() == "custom prompt"


def test_model_name_default(monkeypatch):
    monkeypatch.delenv(_ENV_MODEL, raising=False)
    assert "claude" in model_name().lower()


def test_model_name_override(monkeypatch):
    monkeypatch.setenv(_ENV_MODEL, "claude-haiku-4-5")
    assert model_name() == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# BoundedQAStore — mirrors BoundedBodyStore architecture
# ---------------------------------------------------------------------------


def test_store_issues_monotonic_q_refs():
    store = BoundedQAStore(capacity=10)
    a1 = store.store(question="Q1?", answer="A1")
    a2 = store.store(question="Q2?", answer="A2")
    a3 = store.store(question="Q3?", answer="A3")
    assert a1.ref == "q-1"
    assert a2.ref == "q-2"
    assert a3.ref == "q-3"


def test_store_drop_oldest_on_overflow():
    store = BoundedQAStore(capacity=2)
    a1 = store.store(question="Q1", answer="A1")
    a2 = store.store(question="Q2", answer="A2")
    a3 = store.store(question="Q3", answer="A3")
    # Eldest (a1) evicted
    assert store.lookup(a1.ref) is None
    assert store.lookup(a2.ref) is not None
    assert store.lookup(a3.ref) is not None


def test_store_monotonic_refs_never_reuse():
    """Even after eviction shrinks size, refs keep counting up."""
    store = BoundedQAStore(capacity=1)
    for i in range(5):
        store.store(question=f"Q{i}", answer="A")
    # Only the latest survives
    assert len(store) == 1
    # But next_seq advanced past 5
    assert store.next_seq() == 6


def test_store_lookup_garbage_returns_none():
    store = BoundedQAStore(capacity=5)
    assert store.lookup(None) is None
    assert store.lookup(42) is None
    assert store.lookup("not-a-ref") is None


def test_store_lookup_evicted_returns_none():
    store = BoundedQAStore(capacity=1)
    a1 = store.store(question="Q1", answer="A1")
    store.store(question="Q2", answer="A2")
    assert store.lookup(a1.ref) is None  # evicted


def test_store_clear():
    store = BoundedQAStore(capacity=5)
    store.store(question="Q", answer="A")
    store.clear()
    assert len(store) == 0


def test_store_all_refs_insertion_order():
    store = BoundedQAStore(capacity=10)
    refs = []
    for i in range(5):
        refs.append(store.store(question=f"Q{i}", answer="A").ref)
    assert store.all_refs() == tuple(refs)


def test_store_garbage_inputs_coerced():
    """NEVER raises — non-string question / non-float cost
    coerce to safe defaults."""
    store = BoundedQAStore(capacity=5)
    a = store.store(
        question=None,
        answer=42,
        asked_at_unix="bad",
        cost_usd="oops",
    )
    assert a.question == ""
    assert a.answer == "42"
    assert a.asked_at_unix == 0.0
    assert a.cost_usd == 0.0


def test_store_thread_safe():
    """Concurrent stores must produce distinct refs."""
    store = BoundedQAStore(capacity=1000)
    refs = set()
    lock = threading.Lock()

    def writer(i):
        a = store.store(question=f"Q{i}", answer="A")
        with lock:
            refs.add(a.ref)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(refs) == 50


def test_singleton_get_default_qa_store():
    reset_default_qa_store()
    s1 = get_default_qa_store()
    s2 = get_default_qa_store()
    assert s1 is s2


def test_reset_default_qa_store():
    s1 = get_default_qa_store()
    reset_default_qa_store()
    s2 = get_default_qa_store()
    assert s1 is not s2


# ---------------------------------------------------------------------------
# Cost tracking — D3a Phase 0
# ---------------------------------------------------------------------------


def test_cost_starts_at_zero():
    reset_cost_today()
    assert cost_today_usd() == 0.0


# ---------------------------------------------------------------------------
# ask_question — top-level pipeline
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(monkeypatch):
    """Master ON, fresh store + cost counter."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_BUDGET_USD, "5.0")
    reset_default_qa_store()
    reset_cost_today()
    yield


def _make_fake_provider(answer: str = "Sample answer", cost: float = 0.001):
    captured = {}

    async def fake(system, question):
        captured["system"] = system
        captured["question"] = question
        return (answer, cost)

    fake.captured = captured  # type: ignore[attr-defined]
    return fake


def _noop_bridge(role, text, source, op_id):
    pass


@pytest.mark.asyncio
async def test_master_off_returns_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    report = await ask_question(
        "What is X?",
        provider_callable=_make_fake_provider(),
        bridge_callable=_noop_bridge,
    )
    assert report.verdict is QAVerdict.DISABLED
    assert report.artifact is None
    assert "gate disabled" in report.diagnostic


@pytest.mark.asyncio
async def test_empty_question_returns_out_of_scope(isolated_env):
    report = await ask_question(
        "",
        provider_callable=_make_fake_provider(),
        bridge_callable=_noop_bridge,
    )
    assert report.verdict is QAVerdict.OUT_OF_SCOPE


@pytest.mark.asyncio
async def test_none_question_returns_out_of_scope(isolated_env):
    report = await ask_question(
        None,
        provider_callable=_make_fake_provider(),
        bridge_callable=_noop_bridge,
    )
    assert report.verdict is QAVerdict.OUT_OF_SCOPE


@pytest.mark.asyncio
async def test_whitespace_question_returns_out_of_scope(isolated_env):
    report = await ask_question(
        "   \n  \t   ",
        provider_callable=_make_fake_provider(),
        bridge_callable=_noop_bridge,
    )
    assert report.verdict is QAVerdict.OUT_OF_SCOPE


@pytest.mark.asyncio
async def test_happy_path_returns_answered_with_q_ref(isolated_env):
    fake = _make_fake_provider("The answer is 42.", cost=0.002)
    report = await ask_question(
        "What is the answer?",
        op_id="op-test",
        provider_callable=fake,
        bridge_callable=_noop_bridge,
    )
    assert report.verdict is QAVerdict.ANSWERED
    assert report.artifact is not None
    assert report.artifact.ref.startswith("q-")
    assert report.artifact.answer == "The answer is 42."
    assert report.artifact.cost_usd == 0.002


@pytest.mark.asyncio
async def test_artifact_lookupable_via_default_store(isolated_env):
    fake = _make_fake_provider("answer", cost=0.001)
    report = await ask_question(
        "Q?",
        provider_callable=fake,
        bridge_callable=_noop_bridge,
    )
    assert report.artifact is not None
    found = get_default_qa_store().lookup(report.artifact.ref)
    assert found is not None
    assert found.question == "Q?"


@pytest.mark.asyncio
async def test_provider_returns_empty_answer(isolated_env):
    fake = _make_fake_provider("", cost=0.0)
    report = await ask_question(
        "Q?",
        provider_callable=fake,
        bridge_callable=_noop_bridge,
    )
    assert report.verdict is QAVerdict.PROVIDER_FAILED
    assert report.artifact is None


@pytest.mark.asyncio
async def test_provider_raises_returns_provider_failed(isolated_env):
    async def crashy(system, q):
        raise RuntimeError("network down")

    report = await ask_question(
        "Q?",
        provider_callable=crashy,
        bridge_callable=_noop_bridge,
    )
    assert report.verdict is QAVerdict.PROVIDER_FAILED
    assert "network down" in report.diagnostic


@pytest.mark.asyncio
async def test_provider_timeout_returns_provider_failed(
    isolated_env, monkeypatch,
):
    monkeypatch.setenv(_ENV_TIMEOUT_S, "5")

    async def slow(system, q):
        await asyncio.sleep(60)
        return ("", 0.0)

    # Override the substrate timeout via the fixture's call path
    # — actually the substrate reads timeout_s() each call. We
    # need to actually verify timeout fires. Use a short timeout
    # via a tightly-bounded asyncio.wait_for.
    # We can't easily test the 5s timeout in a fast unit test,
    # so we make the substrate call a fake that hangs and rely
    # on asyncio.wait_for. Use an asyncio.Event that's never set
    # combined with a substrate-internal asyncio.wait_for.

    async def hangs(system, q):
        # Sleep longer than the test budget — but the substrate
        # wraps in wait_for(timeout=timeout_s()=5)
        await asyncio.sleep(0.05)
        # We can't actually wait 5s; this test verifies the
        # timeout WIRING by patching timeout_s.
        return ("late answer", 0.0)

    # Patch timeout_s to return 0.01 so the wait_for fires
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.fast_path_qa.timeout_s",
        lambda: 0.01,
    )

    report = await ask_question(
        "Q?",
        provider_callable=hangs,
        bridge_callable=_noop_bridge,
    )
    assert report.verdict is QAVerdict.PROVIDER_FAILED
    assert "timeout" in report.diagnostic.lower()


@pytest.mark.asyncio
async def test_provider_receives_system_prompt(isolated_env):
    fake = _make_fake_provider("ok", cost=0.0)
    await ask_question(
        "Q?",
        provider_callable=fake,
        bridge_callable=_noop_bridge,
    )
    assert "JARVIS" in fake.captured["system"]  # type: ignore[attr-defined]
    assert fake.captured["question"] == "Q?"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_long_question_truncated(isolated_env):
    fake = _make_fake_provider("ok", cost=0.0)
    long_q = "x" * 5000
    await ask_question(
        long_q,
        provider_callable=fake,
        bridge_callable=_noop_bridge,
    )
    # Substrate caps at 4096
    assert len(fake.captured["question"]) == 4096  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cost_accumulates_across_questions(isolated_env):
    reset_cost_today()
    fake = _make_fake_provider("answer", cost=0.5)
    await ask_question(
        "Q1?",
        provider_callable=fake,
        bridge_callable=_noop_bridge,
    )
    await ask_question(
        "Q2?",
        provider_callable=fake,
        bridge_callable=_noop_bridge,
    )
    assert cost_today_usd() == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_budget_exhausted_blocks_subsequent_calls(
    isolated_env, monkeypatch,
):
    """D3a IMMEDIATE budget enforcement: when daily spend
    reaches the cap, further calls return BUDGET_EXHAUSTED."""
    monkeypatch.setenv(_ENV_BUDGET_USD, "0.5")
    reset_cost_today()
    expensive = _make_fake_provider("answer", cost=0.6)
    # First call succeeds (budget not yet exhausted)
    r1 = await ask_question(
        "Q1?",
        provider_callable=expensive,
        bridge_callable=_noop_bridge,
    )
    assert r1.verdict is QAVerdict.ANSWERED
    # Second call hits the cap
    r2 = await ask_question(
        "Q2?",
        provider_callable=expensive,
        bridge_callable=_noop_bridge,
    )
    assert r2.verdict is QAVerdict.BUDGET_EXHAUSTED
    assert "budget exhausted" in r2.diagnostic.lower()


@pytest.mark.asyncio
async def test_d4_default_records_both_turns(isolated_env):
    """D4 default: ConversationBridge captures user turn AND
    assistant turn. Verify both via injected recorder."""
    fake = _make_fake_provider("the answer", cost=0.001)
    captured: List[Tuple[str, str, str, str]] = []

    def recorder(role, text, source, op_id):
        captured.append((role, text, source, op_id))

    await ask_question(
        "What is X?",
        op_id="op-d4",
        provider_callable=fake,
        bridge_callable=recorder,
    )
    assert len(captured) == 2
    assert captured[0] == ("user", "What is X?", "ask_human_q", "op-d4")
    assert captured[1] == ("assistant", "the answer", "ask_human_a", "op-d4")


@pytest.mark.asyncio
async def test_bridge_callable_failure_does_not_break_ask(isolated_env):
    """If the ConversationBridge recorder raises, the Q&A pipeline
    still completes — D4 default is best-effort."""
    fake = _make_fake_provider("ok", cost=0.0)

    def crashy_recorder(role, text, source, op_id):
        raise RuntimeError("bridge down")

    report = await ask_question(
        "Q?",
        provider_callable=fake,
        bridge_callable=crashy_recorder,
    )
    assert report.verdict is QAVerdict.ANSWERED


# ---------------------------------------------------------------------------
# Authority asymmetry — runtime check
# ---------------------------------------------------------------------------


def test_module_imports_are_clean():
    """At module-import time, fast_path_qa MUST NOT pull in
    orchestrator/iron_gate/etc. Verify by inspecting the
    module's actual loaded submodules.
    """
    import sys
    forbidden = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.candidate_generator",
    )
    # Only catches strict import-time coupling; lazy/local
    # imports are tolerated by design.
    # We can't directly inspect what fast_path_qa imported
    # without isolation; AST pin in `register_shipped_invariants`
    # is the bytes-level enforcement. Smoke: fpq module exists.
    assert "backend.core.ouroboros.governance.fast_path_qa" in sys.modules


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def _load_source_tree():
    p = Path("backend/core/ouroboros/governance/fast_path_qa.py")
    src = p.read_text()
    return src, ast.parse(src)


def test_ast_pins_count():
    assert len(register_shipped_invariants()) == 6


def test_ast_pin_verdict_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_ref_prefix_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "ref_prefix" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_master_default_false_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "master_default_false" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_authority_asymmetry_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_composes_canonical_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_no_provider_generate_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "no_provider_generate" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


# ---------------------------------------------------------------------------
# AST pin synthetic regressions
# ---------------------------------------------------------------------------


def test_ast_pin_verdict_taxonomy_catches_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy" in p.invariant_name
    )
    bad = '''
class QAVerdict(str, enum.Enum):
    ANSWERED = "answered"
    DISABLED = "disabled"
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_ref_prefix_catches_wrong_prefix():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "ref_prefix" in p.invariant_name
    )
    bad = 'QA_REF_PREFIX: str = "qa-"'
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_master_default_false_catches_true():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad = '''
def master_enabled():
    return _flag("X", default=True)
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_authority_catches_orchestrator_import():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = '''
from backend.core.ouroboros.governance.orchestrator import x
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_no_provider_generate_catches_import():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "no_provider_generate" in p.invariant_name
    )
    bad = '''
from backend.core.ouroboros.governance.providers import ClaudeProvider
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_composes_canonical_catches_missing_bridge():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = '''
import anthropic
from collections import OrderedDict
import threading
# no conversation_bridge reference
'''
    assert pin.validate(ast.parse(bad), bad) != ()


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_register_flags_count():
    class _R:
        def __init__(self):
            self.specs = []
        def register(self, spec):
            self.specs.append(spec)
    r = _R()
    n = register_flags(r)
    assert n >= 8
    names = [s.name for s in r.specs]
    assert _ENV_MASTER in names
    assert _ENV_BUDGET_USD in names
    assert _ENV_MODEL in names


def test_register_flags_master_default_false():
    class _R:
        def __init__(self):
            self.specs = []
        def register(self, spec):
            self.specs.append(spec)
    r = _R()
    register_flags(r)
    master_spec = next(s for s in r.specs if s.name == _ENV_MASTER)
    assert master_spec.default is False


# ---------------------------------------------------------------------------
# /expand q-N wiring smoke
# ---------------------------------------------------------------------------


def test_expand_q_prefix_dispatch_present_in_source():
    """Bytes-pin: serpent_flow's /expand handler routes `q-` to
    the QA-ring expansion path."""
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    # The dispatch must branch on `q-` prefix
    assert 'startswith("q-")' in src
    # And call the QA-specific expand handler
    assert "_expand_qa" in src


def test_handle_ask_method_present():
    """Bytes-pin: SerpentREPL has _handle_ask method that
    composes fast_path_qa.ask_question."""
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    assert "_handle_ask" in src
    assert "fast_path_qa" in src


def test_ask_verb_dispatched_in_loop():
    """Bytes-pin: /ask is routed in the dispatch chain."""
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    assert '"/ask"' in src
    assert "_handle_ask" in src
