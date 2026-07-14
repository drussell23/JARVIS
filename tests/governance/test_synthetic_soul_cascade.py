"""Task #9 — wire the synthetic-soul learning cascade (Manifesto §4).

Two severed wires in the cross-session / cross-op learning loop:

  PRIMARY — PostmortemRecall on the DEFAULT path. The consumer that reads
  prior-op POSTMORTEM lessons (+ Prior Ephemeral Knowledge) back into the
  next op's decision (`_inject_postmortem_recall_impl` /
  `_inject_prior_knowledge_impl`) was called ONLY in the dead inline CLASSIFY
  branch (orchestrator.py:3812/3819). The shipping extracted `classify_runner`
  never called it, so failure lessons were written to debug.log and never
  read at decision time — postmortem_recall.py's own stated gap. Now wired
  into classify_runner.run between the ConversationBridge and SemanticIndex
  injections (same trust ordering as the inline path).

  COMPLEMENTARY — Episodic long-term recall. `EpisodicLedger.recall` (cosine
  recall over aged-out episodes) had no generation-path consumer — the
  long-term tier was written on every eviction but never consulted. Now
  `render_relevant_context` surfaces the episodes most relevant to the
  current op's intent into the GENERATE prompt (providers.py §5a-bis), and a
  durable text+embedding store lets that long-term memory span sessions (the
  synthetic soul), rehydrated at construction.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import episodic_core as ec
from backend.core.ouroboros.governance.episodic_core import (
    EpisodicLedger,
    render_relevant_context,
    reset_episodic_ledger,
)


# ── deterministic fake embedder (no fastembed dependency) ────────────────
_VOCAB = [
    "database", "migration", "schema", "sql",
    "voice", "audio", "microphone", "wake",
    "auth", "token", "login", "credential",
]


class _FakeEmbedder:
    """Bag-of-keywords vector so cosine similarity is meaningful: texts that
    share domain words are close, cross-domain texts are far."""

    def embed(self, texts):
        out = []
        for t in texts:
            low = (t or "").lower()
            out.append([1.0 if w in low else 0.0 for w in _VOCAB])
        return out


class _NoopBlue:
    """No-op tamper-evident ledger so tests never touch the real .jarvis/."""

    def record(self, **_kw):
        return None


def _ledger(tmp_path, window=2, longterm_max=10):
    return EpisodicLedger(
        window=window,
        longterm_max=longterm_max,
        embedder=_FakeEmbedder(),
        blue_ledger=_NoopBlue(),
        store_path=tmp_path / "episodic_longterm.jsonl",
    )


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_EPISODIC_CORE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_EPISODIC_PERSIST_ENABLED", raising=False)
    reset_episodic_ledger()
    yield
    reset_episodic_ledger()


# ── PRIMARY: PostmortemRecall wire reachability (guard re-severing) ───────

def test_classify_runner_calls_postmortem_recall_and_prior_knowledge():
    """The default path (extracted classify_runner) MUST call both learning-
    recall helpers. This is the exact "wired-but-inert" trap: the helpers
    exist and are tested, but were unreachable on the shipping path."""
    from backend.core.ouroboros.governance.phase_runners import classify_runner
    src = inspect.getsource(classify_runner.CLASSIFYRunner.run)
    assert "_inject_postmortem_recall_impl" in src
    assert "_inject_prior_knowledge_impl" in src
    # And it is AWAITED (the postmortem helper is a coroutine).
    assert "await _inject_postmortem_recall_impl" in src


def test_postmortem_helpers_are_importable_and_shaped():
    """The helpers the wire imports must exist with the expected async/sync
    shape (a rename upstream would silently break the wire otherwise)."""
    from backend.core.ouroboros.governance.orchestrator import (
        _inject_postmortem_recall_impl,
        _inject_prior_knowledge_impl,
    )
    assert inspect.iscoroutinefunction(_inject_postmortem_recall_impl)
    assert not inspect.iscoroutinefunction(_inject_prior_knowledge_impl)


# ── COMPLEMENTARY: episodic long-term recall is now consumed ─────────────

@pytest.mark.asyncio
async def test_recall_surfaces_relevant_aged_out_episodes(tmp_path):
    """Aged-out episodes must be recallable by semantic relevance to a query
    — the wire that makes the long-term tier actionable."""
    led = _ledger(tmp_path)
    # Record enough to push the first episodes out of the window (size 2).
    await led.record(kind="complete", op_id="op1", summary="database migration to new schema")
    await led.record(kind="complete", op_id="op2", summary="voice wake word tuning")
    await led.record(kind="complete", op_id="op3", summary="auth token refresh flow")
    await led.record(kind="complete", op_id="op4", summary="microphone audio capture")
    # op1 + op2 have aged into long-term. Query for a DB-ish new op.
    hits = led.recall_sync("database schema change", k=1)
    assert hits, "long-term recall returned nothing"
    assert hits[0].op_id == "op1"  # the DB episode, not the voice one


@pytest.mark.asyncio
async def test_render_relevant_context_injects_block(tmp_path, monkeypatch):
    """The module-level accessor the GENERATE prompt calls must render the
    relevant episodes, and only when enabled + given a query."""
    led = _ledger(tmp_path)
    monkeypatch.setattr(ec, "get_episodic_ledger", lambda: led)
    for i, s in enumerate([
        "database migration to new schema", "voice wake word tuning",
        "auth token login", "audio microphone capture",
    ]):
        await led.record(kind="complete", op_id=f"op{i}", summary=s)
    block = render_relevant_context("database schema sql")
    assert "Relevant Past Experience" in block
    assert "database migration" in block
    # No query → no block; disabled → no block.
    assert render_relevant_context("") == ""
    monkeypatch.setenv("JARVIS_EPISODIC_CORE_ENABLED", "false")
    assert render_relevant_context("database schema") == ""


# ── COMPLEMENTARY: cross-session persistence (the synthetic soul) ─────────

@pytest.mark.asyncio
async def test_longterm_recall_spans_sessions(tmp_path):
    """A NEW ledger (a fresh 'session') sharing the durable store must
    rehydrate the prior session's long-term episodes and recall them —
    true cross-session learning."""
    store = tmp_path / "episodic_longterm.jsonl"
    # Session 1: accumulate + age out.
    s1 = EpisodicLedger(window=2, longterm_max=10, embedder=_FakeEmbedder(),
                        blue_ledger=_NoopBlue(), store_path=store)
    for i, s in enumerate([
        "database migration schema", "voice wake audio",
        "auth token credential", "microphone audio capture",
    ]):
        await s1.record(kind="complete", op_id=f"s1-op{i}", summary=s)
    assert store.exists(), "durable store was not written"

    # Session 2: brand-new ledger, same store → rehydrated at construction.
    s2 = EpisodicLedger(window=2, longterm_max=10, embedder=_FakeEmbedder(),
                        blue_ledger=_NoopBlue(), store_path=store)
    hits = s2.recall_sync("database sql schema", k=1)
    assert hits, "cross-session recall returned nothing (rehydration failed)"
    assert hits[0].summary.startswith("database")
    assert hits[0].op_id.startswith("s1-")  # from the PRIOR session


@pytest.mark.asyncio
async def test_persistence_disabled_writes_no_store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_EPISODIC_PERSIST_ENABLED", "false")
    led = _ledger(tmp_path)
    for i in range(4):
        await led.record(kind="complete", op_id=f"op{i}", summary=f"thing {i}")
    assert not (tmp_path / "episodic_longterm.jsonl").exists()


@pytest.mark.asyncio
async def test_store_compaction_bounds_the_file(tmp_path):
    """The durable store must not grow unbounded: on rehydration it is
    compacted to at most longterm_max rows."""
    store = tmp_path / "episodic_longterm.jsonl"
    s1 = EpisodicLedger(window=2, longterm_max=3, embedder=_FakeEmbedder(),
                        blue_ledger=_NoopBlue(), store_path=store)
    for i in range(12):  # 10 evictions → 10 appended rows, cap is 3
        await s1.record(kind="complete", op_id=f"op{i}", summary=f"database item {i}")
    rows_before = sum(1 for _ in store.open())
    assert rows_before > 3  # unbounded append during the session
    # New session rehydrates + compacts.
    EpisodicLedger(window=2, longterm_max=3, embedder=_FakeEmbedder(),
                   blue_ledger=_NoopBlue(), store_path=store)
    rows_after = sum(1 for _ in store.open())
    assert rows_after <= 3  # compacted to the retained tail


@pytest.mark.asyncio
async def test_rehydration_survives_corrupt_line(tmp_path):
    """A torn/garbage line in the store must be skipped, not fatal."""
    store = tmp_path / "episodic_longterm.jsonl"
    s1 = EpisodicLedger(window=2, longterm_max=10, embedder=_FakeEmbedder(),
                        blue_ledger=_NoopBlue(), store_path=store)
    for i in range(4):
        await s1.record(kind="complete", op_id=f"op{i}", summary=f"database item {i}")
    with store.open("a") as fh:
        fh.write("{not valid json\n")
    # Must construct + recall without raising.
    s2 = EpisodicLedger(window=2, longterm_max=10, embedder=_FakeEmbedder(),
                        blue_ledger=_NoopBlue(), store_path=store)
    assert s2.recall_sync("database", k=1)  # good rows still recalled
