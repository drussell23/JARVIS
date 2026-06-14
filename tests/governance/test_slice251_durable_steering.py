"""Slice 251 — Durable Agentic Memory for live steering (cure session-amnesia).

Slice 249 made live steering EPHEMERAL (folded into the current op's prompt only).
This makes a GLOBAL directive temporally durable: classify each absorbed guidance
LOCAL_CORRECTION vs GLOBAL_DIRECTIVE; persist GLOBAL ones into the memory that
ALL future agent inits boot with.

Verify-first scope (corrects the brief):
  * Phase 2's "global memory all future agents boot with" ALREADY EXISTS —
    UserPreferenceMemory persists typed memories and StrategicDirection injects
    them into EVERY future generation prompt. Reuse it; ChromaDB is voice-only /
    not wired to O+V; no new graph.
  * Phase 1's classifier: the codebase idiom is DETERMINISTIC (urgency_router §5
    Tier 0, <1ms, zero-LLM). LOCAL-vs-GLOBAL is a lexical/structural distinction —
    a deterministic classifier is faster + genuinely non-blocking (no "Tiny Prime"
    LLM latency on the hot path).
  * Phase 3: propagation runs out-of-band (fire-and-forget) so the local
    absorption (249) never blocks.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import steering as st
from backend.core.ouroboros.governance.user_preference_memory import (
    UserPreferenceStore, MemoryType,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("JARVIS_STEERING_GLOBAL_PROPAGATION_ENABLED", raising=False)
    st.reset_guidance()
    yield
    st.reset_guidance()


class TestClassifier:
    @pytest.mark.parametrize("text", [
        "always use asynchronous SQLAlchemy sessions",
        "from now on prefer composition over inheritance",
        "never commit secrets to the repo",
        "all request handlers must validate their input",
        "standardize on pytest across the codebase",
        "going forward, use structured logging everywhere",
    ])
    def test_global_directives(self, text):
        assert st.classify_steering_intent(text) == st.INTENT_GLOBAL

    @pytest.mark.parametrize("text", [
        "fix line 45 in this function",
        "update the import here",
        "tweak this one call to pass the timeout",
        "rename the variable on this line",
        "just patch the bug in app.py for now",
    ])
    def test_local_corrections(self, text):
        assert st.classify_steering_intent(text) == st.INTENT_LOCAL

    def test_ambiguous_defaults_local(self):
        # conservative: never pollute global memory on a weak signal
        assert st.classify_steering_intent("hmm look at that") == st.INTENT_LOCAL
        assert st.classify_steering_intent("") == st.INTENT_LOCAL


class TestGate:
    def test_default_true_and_kill_switch(self, monkeypatch):
        assert st.steering_global_propagation_enabled() is True
        monkeypatch.setenv("JARVIS_STEERING_GLOBAL_PROPAGATION_ENABLED", "0")
        assert st.steering_global_propagation_enabled() is False


class TestRecordDirective:
    def test_persists_style_memory_injected_into_future_prompts(self, tmp_path):
        store = UserPreferenceStore(tmp_path)
        mem = store.record_live_steering_directive(
            op_id="op-1",
            directive="always use asynchronous SQLAlchemy sessions",
        )
        assert mem is not None
        assert mem.type == MemoryType.STYLE
        # injected into every future generation prompt via the relevance render
        prompt = store.format_for_prompt(description="add a db query")
        assert "asynchronous SQLAlchemy" in prompt

    def test_empty_directive_is_noop(self, tmp_path):
        store = UserPreferenceStore(tmp_path)
        assert store.record_live_steering_directive(op_id="op", directive="  ") is None


class TestPropagation:
    async def test_global_directive_persists(self, tmp_path):
        store = UserPreferenceStore(tmp_path)
        intent = await st.propagate_directive(
            "op-9", "from now on always validate inputs", store=store,
        )
        assert intent == st.INTENT_GLOBAL
        assert any("validate inputs" in m.description.lower() or
                   "validate inputs" in m.why.lower() or
                   "validate inputs" in m.content.lower()
                   for m in store.list_all())

    async def test_local_correction_does_not_persist(self, tmp_path):
        store = UserPreferenceStore(tmp_path)
        intent = await st.propagate_directive("op-9", "fix line 12 here", store=store)
        assert intent == st.INTENT_LOCAL
        assert store.list_all() == []

    async def test_gate_off_skips_persist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_STEERING_GLOBAL_PROPAGATION_ENABLED", "0")
        store = UserPreferenceStore(tmp_path)
        await st.propagate_directive("op", "always use async", store=store)
        assert store.list_all() == []

    async def test_never_raises(self):
        # a store that explodes must never propagate an exception into the hot
        # path (and we avoid the real default store so no .jarvis pollution)
        class _BrokenStore:
            def record_live_steering_directive(self, **_kw):
                raise RuntimeError("boom")
        await st.propagate_directive("op", "always X", store=_BrokenStore())


class TestToolExecutorWiring:
    def test_absorption_schedules_fire_and_forget_propagation(self):
        from backend.core.ouroboros.governance import tool_executor as te
        src = inspect.getsource(te)
        assert "propagate_directive" in src, "absorption must propagate durable directives"
        # out-of-band: scheduled, not awaited inline (non-blocking)
        assert "create_task" in src


class TestPhase4Integration:
    async def test_global_shift_survives_to_next_agent(self, tmp_path):
        """Inject a GLOBAL architectural shift mid-flight → it is classified
        GLOBAL and written to the persistent memory → a FRESHLY initialized agent
        (new store over the same root) boots with the constraint in its prompt."""
        directive = "always use asynchronous SQLAlchemy sessions"

        # 1) human injects guidance into the running op (Slice 249 channel)
        st.inject_guidance("op-251", directive)
        absorbed = st.consume_guidance("op-251")          # local absorption (non-blocking)
        assert absorbed == directive

        # 2) out-of-band propagation classifies + persists the GLOBAL directive
        agent_a_store = UserPreferenceStore(tmp_path)
        intent = await st.propagate_directive("op-251", absorbed, store=agent_a_store)
        assert intent == st.INTENT_GLOBAL

        # 3) a NEXT agent initialization (fresh store, same project root) boots
        #    with the directive natively in its generation prompt — amnesia cured
        agent_b_store = UserPreferenceStore(tmp_path)
        boot_prompt = agent_b_store.format_for_prompt(description="write a db layer")
        assert "asynchronous SQLAlchemy" in boot_prompt
