"""Slice 3 spine — the Garnish choke, Semantic Human-Routing, @the-human.

Mandated asserts: (1) 3 simultaneous garnish submits process SEQUENTIALLY
(concurrency structurally 1), (2) human input containing "test" routes to
the testing persona, (3) a full queue triggers the deterministic template
fallback. Plus: sliding-window prompt bound, LLM-failure fallback,
per-hour budget, operator pile-on guard, /molt verb end-to-end.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import moltbook as mb
from backend.core.ouroboros.governance import moltbook_garnish as mg
from backend.core.ouroboros.governance.moltbook_garnish import (
    GarnishQueue,
    GarnishRequest,
    garnish_or_template,
)
from backend.core.ouroboros.governance.moltbook_personas import (
    route_human_post,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MOLTBOOK_DB", str(tmp_path / "agora.db"))
    monkeypatch.setenv("JARVIS_MOLTBOOK_CONVERSE_ENABLED", "0")
    monkeypatch.setenv("JARVIS_MOLTBOOK_GARNISH_ENABLED", "1")
    mb.reset_store_for_tests()
    mb.reset_conversation_state_for_tests()
    mg.reset_garnish_for_tests()
    yield
    mg.reset_garnish_for_tests()
    mb.reset_store_for_tests()
    mb.reset_conversation_state_for_tests()


def _req(i: int = 0, **kw):
    base = dict(author_id="review", kind="musing",
                facts={"detail": f"r{i}", "orig": "@x"})
    base.update(kw)
    return GarnishRequest(**base)


# ---------------------------------------------------------------------------
# Mandate 4.1 — three simultaneous submits, sequential processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_simultaneous_garnishes_process_sequentially():
    inflight = {"cur": 0, "max": 0}
    order = []

    async def _slow_llm(prompt):
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        await asyncio.sleep(0.05)
        inflight["cur"] -= 1
        order.append(prompt[-6:])
        return "witty."

    q = GarnishQueue(llm_fn=_slow_llm)
    assert all(q.submit(_req(i)) for i in range(3))   # all admitted
    for _ in range(100):
        await asyncio.sleep(0.02)
        if q.stats["garnished"] == 3:
            break
    assert q.stats["garnished"] == 3                  # all processed
    assert inflight["max"] == 1                       # NEVER concurrent
    assert q.stats["max_inflight"] == 1               # structural proof
    assert len(order) == 3                            # FIFO drained


# ---------------------------------------------------------------------------
# Mandate 4.2 — Semantic Human-Routing
# ---------------------------------------------------------------------------


def test_semantic_router_tags_the_right_resident():
    assert route_human_post("why are the tests failing?") == "test_failure"
    assert route_human_post("what do you think happens next") == "prophecy"
    assert route_human_post("someone review this diff") == "review"
    assert route_human_post("stitch that code back together") == "swarm"
    assert route_human_post("any dreams tonight?") == "dream"
    assert route_human_post("hello everyone") == ""    # no summon
    assert route_human_post(None) == ""                # never raises


# ---------------------------------------------------------------------------
# Mandate 4.3 — full queue → deterministic template fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_queue_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("JARVIS_MOLTBOOK_GARNISH_QUEUE_MAX", "1")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _stuck_llm(prompt):
        started.set()
        await release.wait()
        return "eventually witty"

    q = GarnishQueue(llm_fn=_stuck_llm)
    monkeypatch.setattr(mg, "_QUEUE", q)

    assert q.submit(_req(0)) is True                  # worker takes it
    await asyncio.wait_for(started.wait(), 2.0)
    assert q.submit(_req(1)) is True                  # fills the queue (max 1)
    queued = garnish_or_template(
        "review", "musing", {"detail": "third", "orig": "@x"},
    )
    assert queued is False                            # choke rejected
    assert q.stats["rejected_full"] >= 1
    for _ in range(100):                              # template posted anyway
        await asyncio.sleep(0.02)
        recent = await mb.get_default_store().recent(10)
        if recent:
            break
    assert recent and recent[0].author_id == "review"
    assert "witty" not in recent[0].body              # template, not the LLM
    release.set()


# ---------------------------------------------------------------------------
# Sliding-window prompt bound + failure fallback + budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_carries_only_the_thread_window(monkeypatch):
    monkeypatch.setenv("JARVIS_MOLTBOOK_GARNISH_CONTEXT_WINDOW", "3")
    root = await mb.post_molt("swarm", "proposal", "root idea")
    for i in range(8):
        await mb.post_molt("review", "rebuttal", f"objection {i}",
                           reply_to=root.post_id)
    captured = {}

    async def _llm(prompt):
        captured["prompt"] = prompt
        return "fine."

    q = GarnishQueue(llm_fn=_llm)
    q.submit(_req(thread_root=root.post_id, reply_to=root.post_id))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if captured:
            break
    prompt = captured["prompt"]
    assert "objection 7" in prompt                    # most recent context
    assert "objection 1" not in prompt                # older than window → OUT
    assert prompt.count("objection") == 3             # exactly the window


@pytest.mark.asyncio
async def test_llm_failure_falls_back_never_lost():
    async def _boom(prompt):
        raise RuntimeError("DW down")

    q = GarnishQueue(llm_fn=_boom)
    assert q.submit(_req()) is True
    for _ in range(100):
        await asyncio.sleep(0.02)
        if q.stats["fell_back"] == 1:
            break
    assert q.stats["fell_back"] == 1
    recent = await mb.get_default_store().recent(5)
    assert recent                                     # the post still landed


def test_hourly_budget_rejects(monkeypatch):
    monkeypatch.setenv("JARVIS_MOLTBOOK_GARNISH_PER_HOUR", "2")
    q = GarnishQueue(llm_fn=None)
    assert q._budget_ok(1000.0) and q._budget_ok(1001.0)
    assert q._budget_ok(1002.0) is False              # third in the hour
    assert q._budget_ok(1000.0 + 3601.0) is True      # window rolls


# ---------------------------------------------------------------------------
# @the-human — pile-on guard + /molt verb
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_posts_skip_ambient_dice(monkeypatch):
    monkeypatch.setenv("JARVIS_MOLTBOOK_CONVERSE_ENABLED", "1")
    p = await mb.post_molt("operator", "musing", "quiet thought")
    await asyncio.sleep(0.2)
    thread = await mb.get_default_store().thread_window(p.post_id, 10)
    assert len(thread) == 1                           # no ambient replies


@pytest.mark.asyncio
async def test_molt_verb_posts_and_summons(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mg, "garnish_or_template",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    import backend.core.ouroboros.governance.moltbook_repl as mr
    r = await mr.dispatch_molt_command("/molt why did the tests break?")
    assert r.ok and "@the-human" in r.text
    assert "summoned @first-responder" in r.text
    assert calls and calls[0][0][0] == "test_failure"
    recent = await mb.get_default_store().recent(5)
    assert recent[0].handle == "@the-human"
    assert "tests break" in recent[0].body

    r2 = await mr.dispatch_molt_command("/molt hello everyone")
    assert r2.ok and "summoned" not in r2.text        # no keyword → no summon


def test_molt_verb_never_shadows_moltbook():
    import backend.core.ouroboros.governance.moltbook_repl as mr
    assert mr.matches_molt_command("/molt hi")
    assert not mr.matches_molt_command("/moltbook")
    assert not mr.matches_molt_command("moltbook 5")
    assert mr.matches_moltbook_command("/moltbook")
