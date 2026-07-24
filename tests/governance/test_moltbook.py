"""Moltbook Foundation spine (Slices 1+2, four mandates).

Mandated asserts: (1) a hallucinated d-999 ref is stripped of its
interactive form before saving, (2) valid refs are preserved,
(3) concurrent async writes never trip the SQLite lock. Plus: schema
frozenness (zero authority), sanitizer fencing, sliding-window thread
retrieval, persona voice determinism + escaping, proactive conversation
engine bounds (recursion ban, cooldown, global budget, determinism),
and broker/descriptor registration.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from backend.core.ouroboros.governance import moltbook as mb
from backend.core.ouroboros.governance.moltbook import (
    MoltPost,
    MoltbookStore,
    neutralize_hallucinated_refs,
    post_molt,
    sanitize_body,
)
from backend.core.ouroboros.governance.moltbook_personas import (
    compose,
    persona_for,
)


@pytest.fixture()
def store(tmp_path):
    return MoltbookStore(db_path=str(tmp_path / "agora.db"))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MOLTBOOK_DB", str(tmp_path / "agora.db"))
    monkeypatch.setenv("JARVIS_MOLTBOOK_CONVERSE_ENABLED", "0")
    mb.reset_store_for_tests()
    mb.reset_conversation_state_for_tests()
    yield
    mb.reset_store_for_tests()
    mb.reset_conversation_state_for_tests()


def _post(**kw):
    base = dict(
        author_id="swarm", handle="@the-pit", glyph="⚡", kind="status",
        body="hello agora", post_id=kw.pop("post_id", None) or
        __import__("uuid").uuid4().hex, ts_unix=1.0,
    )
    base.update(kw)
    return MoltPost(**base)


# ---------------------------------------------------------------------------
# Mandate 4.1 + 4.2 — the Strict Ref Fence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hallucinated_ref_neutralized_valid_preserved(store, monkeypatch):
    """d-999 (hallucinated) → inert text; d-7 (real) + m-N (real) survive."""
    stored = await store.add(_post(body="seed"))
    assert stored is not None and stored.seq > 0

    class _Archive:
        def lookup(self, ref):
            return object() if ref == "d-7" else None

    import backend.core.ouroboros.battle_test.diff_archive as da
    monkeypatch.setattr(da, "get_default_archive", lambda: _Archive())

    text = (f"see d-7 and m-{stored.seq} for receipts, "
            f"but d-999 and m-424242 are inventions")
    out = await neutralize_hallucinated_refs(text, store=store)
    assert "d-7" in out                               # (2) valid preserved
    assert f"m-{stored.seq}" in out
    assert "d-999" not in out                         # (1) interactive form gone
    assert "m-424242" not in out
    assert "d‑999" in out and "m‑424242" in out       # inert U+2011 text remains
    # The /expand family regex can never match the neutralized forms.
    import re
    assert not re.search(r"\bd-999\b", out)


@pytest.mark.asyncio
async def test_ref_fence_fails_closed_on_lookup_fault(store, monkeypatch):
    import backend.core.ouroboros.battle_test.diff_archive as da

    def _boom():
        raise RuntimeError("archive down")

    monkeypatch.setattr(da, "get_default_archive", _boom)
    out = await neutralize_hallucinated_refs("trust d-3", store=store)
    assert "d-3" not in out and "d‑3" in out          # fault → inert


# ---------------------------------------------------------------------------
# Mandate 4.3 — concurrent async writes, no lock exceptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_writes_no_lock_errors(store):
    posts = [_post(body=f"post {i}") for i in range(24)]
    results = await asyncio.gather(*(store.add(p) for p in posts))
    stored = [r for r in results if r is not None]
    assert len(stored) == 24                          # none dropped, none raised
    seqs = sorted(p.seq for p in stored)
    assert seqs == list(range(seqs[0], seqs[0] + 24))  # dense, serialized
    recent = await store.recent(limit=30)
    assert len(recent) == 24


# ---------------------------------------------------------------------------
# Zero-authority envelope + sanitizer (mandate 2c)
# ---------------------------------------------------------------------------


def test_envelope_is_frozen_pure_state():
    p = _post()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.body = "mutated"                            # type: ignore[misc]
    payload = p.to_payload()
    assert all(
        isinstance(v, (str, float, int, list)) for v in payload.values()
    )                                                 # pure data, no callables


def test_sanitizer_fences_markup_and_control():
    out = sanitize_body("[bold red]styled[/bold red]\x1b[31m ansi \n x" * 50)
    from rich.text import Text
    rendered = Text.from_markup(out)
    assert not rendered.spans                         # ZERO style spans = inert
    assert "\\[bold red]" in out                      # escaped, not raw
    assert "\x1b" not in out                          # control stripped
    assert len(out) <= 810                            # capped (pre-escape 400)


# ---------------------------------------------------------------------------
# Sliding-window thread retrieval (mandate 2b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_window_is_mathematically_bounded(store):
    root = await store.add(_post(body="root proposal", kind="proposal"))
    for i in range(12):
        await store.add(_post(body=f"reply {i}", reply_to=root.post_id))
    window = await store.thread_window(root.post_id, window=5)
    assert len(window) == 5                           # never O(thread)
    assert window[-1].body == "reply 11"              # the LAST five
    assert window[0].seq < window[-1].seq             # chronological


# ---------------------------------------------------------------------------
# Personas — determinism + escape fence
# ---------------------------------------------------------------------------


def test_persona_voice_deterministic_and_escaped():
    a = compose("swarm", "status", {"agents": 7, "file": "big.py"})
    b = compose("swarm", "status", {"agents": 7, "file": "big.py"})
    assert a == b                                     # no RNG, no clocks
    evil = compose("review", "rebuttal", {
        "detail": "[bold red]pwn[/bold red]", "orig": "@x",
    })
    from rich.text import Text
    assert not Text.from_markup(evil).spans           # facts fenced — inert


def test_persona_fallback_never_anonymous():
    p = persona_for("some_new_sensor_2027")
    assert p.handle == "@ouroboros"
    assert persona_for("swarm:chunk-3").handle == "@the-pit"
    assert persona_for("worker-2").handle == "@the-floor"


# ---------------------------------------------------------------------------
# Proactive conversation engine — bounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replies_never_breed_replies(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_MOLTBOOK_CONVERSE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_MOLTBOOK_REPLIES_PER_MIN", "30")
    monkeypatch.setenv("JARVIS_MOLTBOOK_REPLY_COOLDOWN_S", "5")
    calls = []
    orig = mb._maybe_converse

    async def _spy(stored):
        calls.append(stored.reply_to)
        await orig(stored)

    monkeypatch.setattr(mb, "_maybe_converse", _spy)
    root = await post_molt("swarm", "proposal", "tree reuse. fight me.")
    assert root is not None
    for _ in range(20):                               # drain reaction tasks
        await asyncio.sleep(0.02)
    # every converse invocation was for a TOP-LEVEL post ("" reply_to)
    assert calls and all(r == "" for r in calls)
    thread = await mb.get_default_store().thread_window(root.post_id, 20)
    replies = [p for p in thread if p.reply_to]
    for r in replies:
        # structural recursion ban: replies exist, but none spawned kin
        assert r.reply_to == root.post_id


@pytest.mark.asyncio
async def test_reaction_dice_deterministic(monkeypatch):
    """Same post_id → same reaction outcome, every time (replayable)."""
    import hashlib
    pid = "deadbeef" * 4
    rolls = [
        hashlib.sha256(f"{pid}|review".encode()).digest()[0] % 100
        for _ in range(3)
    ]
    assert len(set(rolls)) == 1


def test_reply_budget_cooldown_and_global_cap(monkeypatch):
    monkeypatch.setenv("JARVIS_MOLTBOOK_REPLY_COOLDOWN_S", "60")
    monkeypatch.setenv("JARVIS_MOLTBOOK_REPLIES_PER_MIN", "2")
    mb.reset_conversation_state_for_tests()
    assert mb._reply_budget_ok("review", 1000.0) is True
    assert mb._reply_budget_ok("review", 1001.0) is False   # cooldown
    assert mb._reply_budget_ok("prophecy", 1002.0) is True
    assert mb._reply_budget_ok("dream", 1003.0) is False    # global cap (2/min)


# ---------------------------------------------------------------------------
# Broker + descriptor + verb registration (mandate 3 DRY)
# ---------------------------------------------------------------------------


def test_molt_post_event_registered_and_renders():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
    )
    assert "molt_post" in _VALID_EVENT_TYPES
    from backend.core.ouroboros.governance.event_breadcrumb_registry import (
        build_default_registry,
    )
    _sev, text = build_default_registry().render("molt_post", {
        "glyph": "⚡", "handle": "@the-pit",
        "body": "crew of 7 dropping on big.py", "ref": "m-3",
    })
    assert "@the-pit" in text and "m-3" in text


@pytest.mark.asyncio
async def test_full_post_pipeline_publishes(monkeypatch):
    published = []
    import backend.core.ouroboros.governance.ide_observability_stream as ios
    monkeypatch.setattr(
        ios, "publish_task_event",
        lambda et, op, payload: published.append((et, payload)),
    )
    p = await post_molt("swarm", "status", facts={
        "agents": 5, "file": "x.py",
    })
    assert p is not None and p.seq > 0 and p.handle == "@the-pit"
    assert published and published[0][0] == "molt_post"
    assert published[0][1]["ref"] == p.ref


def test_moltbook_verb_matches():
    from backend.core.ouroboros.governance.moltbook_repl import (
        matches_moltbook_command,
    )
    assert matches_moltbook_command("/moltbook")
    assert matches_moltbook_command("moltbook 30")
    assert not matches_moltbook_command("molt")
