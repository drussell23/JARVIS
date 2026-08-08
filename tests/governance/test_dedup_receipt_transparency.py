"""A dropped duplicate must say so, in facts something measured.

`router.ingest` answers `"deduplicated"` when a goal's dedup_key was accepted
inside the window. That verdict used to be collapsed into `None` by
`_submit_operator_goal` — the SAME value it returns when intake is
unreachable. Two different realities, one token, so the renderer could not
tell "you already asked for this" from "nothing is listening" and said
"queued — the Backlog sensor will pick it up on its next sweep" about both.

Mechanically true (the goal really was filed), and unactionable: it names
neither the collision nor when the goal becomes runnable again.

This suite pins the repaired chain end to end:

    router.describe_dedup_collision   facts the registry ALREADY holds
      -> harness._dedup_receipt       encoded, never invented
      -> executor.dispatch_backlog    files the safety net, stamps `f`
      -> chat_response_style          the only layer that decides how it reads

and the two properties that make it safe: the backlog safety net still gets
written (a duplicate of a FAILED run must remain recoverable), and the dedup
registry is now bounded.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    DedupCollision,
    IntakeRouterConfig,
    UnifiedIntakeRouter,
    _DEDUP_PRUNE_THRESHOLD,
)


def _router(tmp_path: Path, **cfg: Any) -> UnifiedIntakeRouter:
    gls = MagicMock()
    gls.submit = MagicMock(return_value=None)
    config = IntakeRouterConfig(
        project_root=tmp_path,
        wal_path=tmp_path / ".jarvis" / "intake_wal.jsonl",
        lock_path=tmp_path / ".jarvis" / "intake_router.lock",
        max_queue_size=100,
        **cfg,
    )
    return UnifiedIntakeRouter(gls=gls, config=config)


def _envelope(message: str = "fix the tests") -> Any:
    return make_envelope(
        source="operator_chat", description=message, target_files=(),
        repo="jarvis", confidence=1.0, urgency="high",
        evidence={"signature": message[:64]}, requires_human_ack=False,
    )


# ---------------------------------------------------------------------------
# 1. the router explains itself — from the registry it already keeps
# ---------------------------------------------------------------------------

def test_no_collision_reads_as_no_collision(tmp_path: Path) -> None:
    assert _router(tmp_path).describe_dedup_collision(_envelope()) is None


def test_a_registered_key_reports_its_age_and_window(tmp_path: Path) -> None:
    r = _router(tmp_path, dedup_window_s=600.0)
    env = _envelope()
    r._register_dedup(env)

    c = r.describe_dedup_collision(env)
    assert c is not None
    assert c.dedup_key == env.dedup_key
    assert 0.0 <= c.age_s < 5.0, "just registered — age must be ~0"
    assert c.window_s == 600.0


def test_it_never_invents_a_collision_that_expired(tmp_path: Path) -> None:
    """`_is_duplicate` and this must agree. An entry past its window is not
    why anything dropped, so explaining one would be a fabricated cause."""
    r = _router(tmp_path, dedup_window_s=60.0)
    env = _envelope()
    r._dedup[env.dedup_key] = time.monotonic() - 3600.0   # long expired

    assert r._is_duplicate(env) is False
    assert r.describe_dedup_collision(env) is None


def test_a_disabled_window_explains_nothing(tmp_path: Path) -> None:
    """window <= 0 disables dedup, so nothing can have deduplicated."""
    r = _router(tmp_path, dedup_window_s=0.0)
    env = _envelope()
    r._dedup[env.dedup_key] = time.monotonic()
    assert r._is_duplicate(env) is False
    assert r.describe_dedup_collision(env) is None


def test_describing_is_READ_ONLY(tmp_path: Path) -> None:
    """It must not register, evict, or otherwise become an authority — an
    explanation that mutates the thing it explains is a second tracker."""
    r = _router(tmp_path)
    env = _envelope()

    before = dict(r._dedup)
    assert r.describe_dedup_collision(env) is None
    assert r._dedup == before, "explaining a miss registered a key"

    r._register_dedup(env)
    snapshot = dict(r._dedup)
    for _ in range(5):
        r.describe_dedup_collision(env)
    assert r._dedup == snapshot, "explaining mutated the registry"


def test_the_collision_math_is_sane() -> None:
    c = DedupCollision(dedup_key="a" * 64, age_s=45.0, window_s=600.0)
    assert c.short_hash == "a" * 12
    assert c.dedup_key.startswith(c.short_hash), "hash must be a PREFIX"
    assert c.retry_after_s == 555.0
    # Never negative, even when the age has overrun the window.
    assert DedupCollision("x", age_s=900.0, window_s=600.0).retry_after_s == 0.0


def test_the_hash_is_the_dedup_key_not_a_new_one(tmp_path: Path) -> None:
    """DRY: `sha256(source | target_files | signature)` already exists. A
    second hash would be a second identity for the same collision."""
    r = _router(tmp_path)
    env = _envelope()
    r._register_dedup(env)
    c = r.describe_dedup_collision(env)
    assert c is not None and env.dedup_key.startswith(c.short_hash)


# ---------------------------------------------------------------------------
# 2. the registry is bounded (mandate 4 — no leak)
# ---------------------------------------------------------------------------

def test_the_dedup_registry_no_longer_grows_without_bound(tmp_path: Path) -> None:
    """It was append-only: one entry per distinct key, for the life of the
    process. Every distinct signature an operator ever types, forever."""
    r = _router(tmp_path, dedup_window_s=1.0, voice_dedup_window_s=1.0)

    for i in range(_DEDUP_PRUNE_THRESHOLD + 50):
        e = _envelope(f"goal number {i}")
        r._dedup[e.dedup_key] = time.monotonic() - 3600.0     # all expired
    r._register_dedup(_envelope("the one that triggers the sweep"))

    assert len(r._dedup) < _DEDUP_PRUNE_THRESHOLD, (
        f"registry still holds {len(r._dedup)} entries — the sweep did not run"
    )


def test_pruning_can_never_turn_a_duplicate_into_an_accepted_goal(
    tmp_path: Path,
) -> None:
    """The safety property. Only entries older than the LONGEST window are
    evicted — at which point `_is_duplicate` was already False for them."""
    r = _router(tmp_path, dedup_window_s=600.0, voice_dedup_window_s=300.0)
    live = _envelope("a goal still inside its window")
    r._register_dedup(live)

    for i in range(_DEDUP_PRUNE_THRESHOLD + 10):
        e = _envelope(f"ancient goal {i}")
        r._dedup[e.dedup_key] = time.monotonic() - 100_000.0
    r._register_dedup(_envelope("trigger"))

    assert live.dedup_key in r._dedup, "a LIVE dedup entry was evicted"
    assert r._is_duplicate(live) is True


def test_pruning_never_raises_into_the_ingest_path(tmp_path: Path) -> None:
    """Housekeeping must not be able to drop a signal."""
    r = _router(tmp_path)
    r._dedup = {"junk": "not-a-float"}          # type: ignore[dict-item]
    for i in range(_DEDUP_PRUNE_THRESHOLD + 5):
        r._dedup[f"k{i}"] = time.monotonic()
    r._register_dedup(_envelope())              # must not raise


# ---------------------------------------------------------------------------
# 3. the receipt grammar — one definition, both producers read it
# ---------------------------------------------------------------------------

def test_the_grammar_round_trips() -> None:
    from backend.core.ouroboros.governance.chat_response_style import (
        is_dedup_receipt,
        make_dedup_receipt,
        with_filed,
    )

    r = make_dedup_receipt("a1b2c3d4e5f6", 45.0, 600.0, filed=False)
    assert is_dedup_receipt(r)
    assert r.endswith("f=0")
    assert with_filed(r, True).endswith("f=1")
    assert "a1b2c3d4e5f6" in with_filed(r, True), "the hash survived re-stamping"


def test_a_dedup_receipt_is_not_mistaken_for_a_dispatch() -> None:
    """`op-` is the ONLY shape that means "a worker has it". A collision must
    never satisfy that test or the cockpit says `on it` about a dropped goal."""
    from backend.core.ouroboros.governance.chat_response_style import (
        _dispatched_now,
        _was_discarded,
        is_dedup_receipt,
        make_dedup_receipt,
    )

    r = make_dedup_receipt("abc123", 1.0, 600.0, filed=True)
    assert is_dedup_receipt(r)
    assert not _dispatched_now(r)
    assert not _was_discarded(r)
    for other in ("op-019fa4-jarvis", "chat:chat-abc", "logged-backlog-x", ""):
        assert not is_dedup_receipt(other)


def test_with_filed_leaves_other_receipts_alone() -> None:
    from backend.core.ouroboros.governance.chat_response_style import with_filed

    for other in ("op-019fa4-jarvis", "chat:chat-abc", "logged-backlog-x"):
        assert with_filed(other, True) == other


@pytest.mark.parametrize("junk", [
    "dedup:", "dedup:h=;a=zz;w=;f=", "dedup:garbage", "dedup:a=1;a=2",
])
def test_a_garbled_receipt_renders_rather_than_raising(junk: str) -> None:
    """A formatting fault must never swallow a real response."""
    from backend.core.ouroboros.governance.chat_response_style import (
        compose_reply,
        dedup_lines,
    )

    assert dedup_lines(junk)
    out = compose_reply("backlog_dispatch", receipt=junk)
    assert "Deduplicated" in out


# ---------------------------------------------------------------------------
# 4. what the operator reads
# ---------------------------------------------------------------------------

def _reply(age: float = 45.0, window: float = 600.0, filed: bool = True) -> str:
    from backend.core.ouroboros.governance.chat_response_style import (
        compose_reply,
        make_dedup_receipt,
    )

    return compose_reply(
        "backlog_dispatch",
        receipt=make_dedup_receipt("a1b2c3d4e5f6", age, window, filed=filed),
    )


def test_it_never_says_queued_about_a_dropped_duplicate() -> None:
    """THE defect. "queued — the Backlog sensor will pick it up on its next
    sweep" describes the safety net as if it were the outcome."""
    out = _reply()
    assert "queued" not in out
    assert "next sweep" not in out
    assert "Deduplicated" in out


def test_it_reports_the_measured_age_and_the_retry_horizon() -> None:
    out = _reply(age=45.0, window=600.0)
    assert "45s ago" in out
    assert "9m15s" in out, "the retry horizon is window - age"
    assert "a1b2c3d4e5f6" in out


def test_it_does_NOT_claim_the_earlier_op_is_still_running() -> None:
    """The registry stores ONE fact: when a key was last accepted. An op that
    has since completed or FAILED still collides, so "already in flight" is a
    state nothing measures — the exact fabrication class this arc kills."""
    out = _reply().lower()
    for lie in ("in flight", "in-flight", "still running", "executing now"):
        assert lie not in out, f"claimed {lie!r} without measuring it"
    assert "accepted" in out


def test_the_safety_net_is_reported_only_when_it_exists() -> None:
    assert "filed to the backlog as well" in _reply(filed=True)
    filed_false = _reply(filed=False)
    assert "NOT filed" in filed_false
    assert "filed to the backlog as well" not in filed_false


def test_the_head_line_does_not_describe_the_backlog_write() -> None:
    """`acknowledge()` would say "adding that to the backlog" — true of the
    safety net, wrong as the answer to what the operator asked."""
    out = _reply()
    assert "you have already asked for this" in out
    assert "adding that to the backlog" not in out


def test_the_raw_token_never_reaches_the_operator() -> None:
    out = _reply()
    assert "dedup:h=" not in out and "f=1" not in out


# ---------------------------------------------------------------------------
# 5. the harness mints it — and refuses to invent one
# ---------------------------------------------------------------------------

class _Turn:
    turn_id = "chat-1dc4650228e7"
    session_id = "repl"


def test_the_submitter_returns_a_collision_receipt(tmp_path: Path) -> None:
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness
    from backend.core.ouroboros.governance.chat_response_style import (
        is_dedup_receipt,
    )

    r = _router(tmp_path)
    env = _envelope()
    r._register_dedup(env)

    out = BattleTestHarness._operator_goal_receipt(
        "deduplicated", "op-x-chat", router=r, envelope=env,
    )
    assert out is not None and is_dedup_receipt(out)


def test_an_unexplainable_collision_falls_back_rather_than_fabricating() -> None:
    """A router with no accessor (or an entry that expired between the verdict
    and the read) must yield None -> the goal is FILED. A receipt carrying an
    age nobody measured would be worse than the vague one it replaced."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    class _Old:            # no describe_dedup_collision
        pass

    assert BattleTestHarness._operator_goal_receipt(
        "deduplicated", "op-x", router=_Old(), envelope=_envelope(),
    ) is None
    assert BattleTestHarness._operator_goal_receipt(
        "deduplicated", "op-x", router=None, envelope=None,
    ) is None


@pytest.mark.parametrize("verdict", ["backpressure", "", "nonsense"])
def test_other_refusals_are_unchanged(verdict: str) -> None:
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    assert BattleTestHarness._operator_goal_receipt(verdict, "op-x") is None


@pytest.mark.parametrize("verdict", ["enqueued", "pending_ack"])
def test_acceptance_is_unchanged(verdict: str) -> None:
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    assert BattleTestHarness._operator_goal_receipt(verdict, "op-x") == "op-x"


# ---------------------------------------------------------------------------
# 6. the safety net still gets written (mandate 4)
# ---------------------------------------------------------------------------

def _executor(tmp_path: Path, submit: Any) -> Any:
    from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
        BacklogChatActionExecutor,
    )
    return BacklogChatActionExecutor(tmp_path, submit_now=submit)


def _dedup_token(filed: bool = False) -> str:
    from backend.core.ouroboros.governance.chat_response_style import (
        make_dedup_receipt,
    )
    return make_dedup_receipt("a1b2c3d4e5f6", 45.0, 600.0, filed=filed)


def test_a_deduplicated_goal_is_STILL_filed(tmp_path: Path) -> None:
    """The load-bearing one. The earlier run may have already FAILED; if the
    duplicate is dropped AND unfiled, the operator's goal is simply gone."""
    import json

    out = _executor(tmp_path, lambda _m, _t: _dedup_token()).dispatch_backlog(
        "fix the tests", _Turn(),
    )
    backlog = tmp_path / ".jarvis" / "backlog.json"
    assert backlog.exists(), "the safety net was never written"
    body = json.loads(backlog.read_text())
    rows = body if isinstance(body, list) else body.get("tasks", body)
    assert rows, "backlog.json is empty"
    assert out.endswith("f=1"), "the receipt denies a filing that happened"


def test_a_dispatched_goal_is_NOT_filed(tmp_path: Path) -> None:
    """Unchanged: a real dispatch must not also queue duplicate work."""
    out = _executor(tmp_path, lambda _m, _t: "op-019fa4-chat").dispatch_backlog(
        "fix the tests", _Turn(),
    )
    assert out == "op-019fa4-chat"
    assert not (tmp_path / ".jarvis" / "backlog.json").exists()


def test_a_failed_filing_is_not_reported_as_filed(tmp_path: Path) -> None:
    """`f=0` must mean "no safety net", never "not attempted"."""
    import backend.core.ouroboros.governance.chat_repl_backlog_executor as mod

    ex = _executor(tmp_path, lambda _m, _t: _dedup_token())
    original = mod._append_to_backlog_json
    mod._append_to_backlog_json = lambda *_a, **_k: False
    try:
        out = ex.dispatch_backlog("fix the tests", _Turn())
    finally:
        mod._append_to_backlog_json = original

    assert out.endswith("f=0")
    from backend.core.ouroboros.governance.chat_response_style import compose_reply
    assert "NOT filed" in compose_reply("backlog_dispatch", receipt=out)


def test_the_executor_reads_the_grammar_rather_than_restating_it() -> None:
    """Two copies of a grammar are two grammars, and the second one drifts."""
    import inspect

    import backend.core.ouroboros.governance.chat_repl_backlog_executor as mod

    src = inspect.getsource(mod)
    assert "from backend.core.ouroboros.governance.chat_response_style import" in src
    assert '"dedup:"' not in src and "'dedup:'" not in src, (
        "the executor restated the prefix instead of importing it"
    )


# ---------------------------------------------------------------------------
# 7. the whole chain, against the real router
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_end_to_end_the_second_identical_goal_explains_itself(
    tmp_path: Path,
) -> None:
    """Real router, real verdict, real receipt, real rendering. The first goal
    dispatches; the second — identical, inside the window — comes back naming
    the collision instead of claiming a queue position."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness
    from backend.core.ouroboros.governance.chat_response_style import compose_reply

    r = _router(tmp_path, dedup_window_s=600.0)

    first = _envelope("rebuild the IPC layer")
    assert await r.ingest(first) == "enqueued"
    assert BattleTestHarness._operator_goal_receipt(
        "enqueued", "op-first-chat", router=r, envelope=first,
    ) == "op-first-chat"

    second = _envelope("rebuild the IPC layer")
    assert second.dedup_key == first.dedup_key, "same goal, same key"
    verdict = await r.ingest(second)
    assert verdict == "deduplicated"

    receipt = BattleTestHarness._operator_goal_receipt(
        verdict, "op-second-chat", router=r, envelope=second,
    )
    assert receipt is not None

    reply = compose_reply("backlog_dispatch", receipt=receipt)
    assert "Deduplicated" in reply
    assert "queued" not in reply
    assert "on it" not in reply
    assert second.dedup_key.startswith(
        reply.split("[hash: ")[1].split("]")[0]
    ), "the rendered hash is not this goal's dedup_key"


@pytest.mark.asyncio
async def test_the_submitter_refuses_to_block_the_loop_it_needs(
    tmp_path: Path,
) -> None:
    """Called ON the loop, it must file rather than deadlock.

    `run_coroutine_threadsafe` schedules onto the loop the caller is sitting
    on, so blocking for the result waits for work only the caller could drive:
    the loop wedges for the full timeout and then fails anyway. Caught by the
    four-layer test below before it was guarded — the loop stalled ~3.9s and
    `LoopSink` flagged it.

    Production reaches this through `asyncio.to_thread` and never lands here,
    but "never" was equally true of the envelope contract this same function
    got wrong, so it is enforced rather than assumed.
    """
    import asyncio

    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    router = _router(tmp_path)

    class _GLS:
        _intake_router = router

    h = BattleTestHarness.__new__(BattleTestHarness)
    h._governed_loop_service = _GLS()
    h._operator_goal_loop = asyncio.get_running_loop()

    started = time.monotonic()
    out = h._submit_operator_goal("fix the tests", _Turn())
    elapsed = time.monotonic() - started

    assert out is None, "it claimed a dispatch it could not confirm"
    assert elapsed < 1.0, (
        f"blocked the event loop for {elapsed:.1f}s instead of failing fast"
    )


@pytest.mark.asyncio
async def test_all_four_layers_together(tmp_path: Path) -> None:
    """Router -> harness submitter -> executor -> renderer, no fakes between.

    Each layer is pinned above in isolation; this is the one that would catch
    a seam where two correct layers disagree — the failure mode that let
    `_submit_operator_goal` sit dead behind a green suite for twelve days.
    """
    import json

    from backend.core.ouroboros.battle_test.harness import BattleTestHarness
    from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
        BacklogChatActionExecutor,
    )
    from backend.core.ouroboros.governance.chat_response_style import compose_reply

    import asyncio

    router = _router(tmp_path, dedup_window_s=600.0)

    class _GLS:
        def __init__(self) -> None:
            self._intake_router = router

        def note_operator_op(self, op_id: str) -> None:
            pass

    harness = BattleTestHarness.__new__(BattleTestHarness)
    harness._governed_loop_service = _GLS()
    # The loop the daemon captures when it arms the seam at boot.
    harness._operator_goal_loop = asyncio.get_running_loop()

    executor = BacklogChatActionExecutor(
        tmp_path, submit_now=harness._submit_operator_goal,
    )

    # THE production topology: `chat_text_bridge._run` dispatches the executor
    # through `asyncio.to_thread`, so the submitter runs on a worker thread.
    # Calling it inline here would test a path the daemon never takes — and
    # would deadlock, since the coroutine needs the loop the caller is sitting
    # on (see `test_the_submitter_refuses_to_block_the_loop_it_needs`).
    async def _turn(text: str, turn: Any) -> str:
        return await asyncio.to_thread(executor.dispatch_backlog, text, turn)

    first = await _turn("rebuild the IPC layer", _Turn())
    assert first.startswith("op-"), f"first goal did not dispatch: {first}"
    assert "on it" in compose_reply("backlog_dispatch", receipt=first)

    class _Turn2:
        turn_id = "chat-second-turn"
        session_id = "repl"

    second = await _turn("rebuild the IPC layer", _Turn2())
    assert second.startswith("dedup:"), (
        f"the identical goal did not report a collision: {second}"
    )
    assert second.endswith("f=1"), "the safety net was not written or not stamped"

    # Mandate 4, proven on the real path: the goal survives as a backlog row.
    backlog = tmp_path / ".jarvis" / "backlog.json"
    assert backlog.exists()
    body = json.loads(backlog.read_text())
    rows = body if isinstance(body, list) else body.get("tasks", body)
    assert any("rebuild the IPC layer" in json.dumps(r) for r in rows)

    reply = compose_reply("backlog_dispatch", receipt=second)
    assert "Deduplicated" in reply and "queued" not in reply
    assert "filed to the backlog as well" in reply
