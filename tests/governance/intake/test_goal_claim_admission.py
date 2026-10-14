"""Goal admission is ONE atomic step at the intake router.

The boot-time race of 2026-09-07: a checkpoint resume and a fresh roadmap
emission for the same signed goal entered the router together, both read
"no live op", and both were admitted — two ops racing one goal for forty
minutes each. The router now claims the goal under the ledger's flock at
admission (``claim_dispatch``), keeps the claim only for statuses under which
the op exists, and closes it otherwise.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance import goal_reconciliation_ledger as L
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
)

SECRET = "test-roadmap-secret"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(L._ENV_LEDGER_PATH, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(L._ENV_REPO_ROOT, str(tmp_path))
    monkeypatch.setenv("JARVIS_ROADMAP_READER_HMAC_SECRET", SECRET)
    monkeypatch.delenv(L._ENV_ENABLED, raising=False)
    monkeypatch.delenv(L._ENV_INFLIGHT_TTL, raising=False)
    return tmp_path / "ledger.jsonl"


def _goal_env(op_id: str, *, source: str = "roadmap", goal_id: str = "goal-a",
              files=("tests/test_a.py",), requires_human_ack: bool = False):
    return make_envelope(
        source=source, description=f"work for {goal_id} via {op_id}", target_files=tuple(files),
        repo="jarvis", confidence=0.9, urgency="normal",
        evidence={"goal_id": goal_id, "goal_digest": "d" * 8, "signature": op_id},
        requires_human_ack=requires_human_ack, causal_id=op_id,
    )


def _router(tmp_path, **cfg: Any):
    gls = MagicMock(); gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path, dedup_window_s=60.0, **cfg)
    return UnifiedIntakeRouter(gls=gls, config=config)


def _dispatched():
    return [r.op_id for r in L.read_records() if r.event == L.ReconciliationEvent.DISPATCHED.value]


def _terminal():
    return [r.op_id for r in L.read_records() if r.event == L.ReconciliationEvent.TERMINAL.value]


@pytest.mark.asyncio
async def test_concurrent_admissions_of_one_goal_admit_exactly_one(tmp_path, ledger):
    router = _router(tmp_path)
    await router.start()
    try:
        statuses = await asyncio.gather(*(
            router.ingest(_goal_env(f"op-{i}", files=(f"tests/test_{i}.py",))) for i in range(6)
        ))
    finally:
        await router.stop()
    assert sorted(statuses) == ["deduplicated"] * 5 + ["enqueued"]
    assert len(_dispatched()) == 1, "one DISPATCHED row — the claim IS the record"
    assert not router._goal_claims, "the admitted op's claim bookkeeping is consumed by ingest"


@pytest.mark.asyncio
async def test_resume_of_the_holder_is_admitted_and_a_fresh_emission_is_not(tmp_path, ledger, monkeypatch):
    router = _router(tmp_path)
    await router.start()
    try:
        monkeypatch.setenv(L._ENV_SESSION, "bt-old")
        assert await router.ingest(_goal_env("op-old")) == "enqueued"
        monkeypatch.setenv(L._ENV_SESSION, "bt-new")
        # new process: the resume of op-old and a fresh roadmap op enter together
        statuses = await asyncio.gather(
            router.ingest(_goal_env("op-old", source="fsm_resume", files=("tests/test_r.py",))),
            router.ingest(_goal_env("op-fresh", files=("tests/test_f.py",))),
        )
    finally:
        await router.stop()
    assert sorted(statuses) == ["deduplicated", "enqueued"]
    rows = [r for r in L.read_records() if r.event == L.ReconciliationEvent.DISPATCHED.value]
    assert rows[-1].session == "bt-new"


@pytest.mark.asyncio
async def test_a_rejected_op_releases_its_goal(tmp_path, ledger, monkeypatch):
    # backpressure rejects a NON-exempt source once the queue depth reaches the
    # threshold; the depth is pinned so the running dispatcher cannot drain it
    # under the test.
    router = _router(tmp_path, backpressure_threshold=1)
    await router.start()
    try:
        assert await router.ingest(_goal_env("op-1", goal_id="goal-x", files=("tests/x.py",))) == "enqueued"
        monkeypatch.setattr(router, "intake_queue_depth", lambda: 1)
        # queue full → the next goal-bound op is refused by backpressure AFTER it claimed
        status = await router.ingest(_goal_env("op-2", goal_id="goal-y", files=("tests/y.py",)))
        assert status == "backpressure"
        assert "op-2" in _dispatched() and "op-2" in _terminal(), "claimed, then released with a terminal row"
        # the goal is free again for the next emission
        assert (await L.claim_dispatch(goal_id="goal-y", goal_digest_hex="", op_id="op-3")).reason == "claimed_new"
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_parked_and_queued_ops_keep_their_claim(tmp_path, ledger):
    router = _router(tmp_path)
    await router.start()
    try:
        assert await router.ingest(_goal_env("op-ack", goal_id="goal-p", requires_human_ack=True)) == "pending_ack"
        assert "op-ack" in _dispatched() and "op-ack" not in _terminal()
        assert (await L.claim_dispatch(goal_id="goal-p", goal_digest_hex="", op_id="op-dup")).holder_op == "op-ack"
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_an_ingest_exception_after_the_claim_releases_it(tmp_path, ledger, monkeypatch):
    router = _router(tmp_path)
    await router.start()
    try:
        monkeypatch.setattr(router, "_claim_target_files", MagicMock(side_effect=RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            await router.ingest(_goal_env("op-boom", goal_id="goal-e"))
        assert "op-boom" in _terminal()
        assert not router._goal_claims
    finally:
        await router.stop()


# ---- idempotency: a satisfied goal is never built twice (2026-10-13) --------------
#
# One goal landed THREE commits: the WorkOrderSensor's boot emission ran beside
# the Sentinel's dispatch (its envelope stated the goal only inside its
# provenance claim, so it was unbound and never claimed), and after a restart
# the work order was admitted again although the ledger said SATISFIED.


def _git(repo, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo_with_commit(root):
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "the landing")
    return _git(root, "rev-parse", "HEAD")


def _work_order_env(op_id, goal_id="goal-w"):
    """The WorkOrderSensor's shape: the goal only inside the provenance claim."""
    return make_envelope(
        source="roadmap", description="lift the fallback policy", target_files=("backend/w.py",),
        repo="jarvis", confidence=0.95, urgency="normal",
        evidence={"work_order": True, "provenance": {"goal_id": goal_id, "signature": "s"}},
        requires_human_ack=False, causal_id=op_id,
    )


def test_binding_reads_the_provenance_claim_and_prefers_the_top_level():
    assert L.binding_of({"provenance": {"goal_id": "g1"}}) == ("g1", "")
    assert L.binding_of({"goal_id": "top", "goal_digest": "dd",
                         "provenance": {"goal_id": "other"}}) == ("top", "dd")
    assert L.binding_of("junk") == ("", "")


@pytest.mark.asyncio
async def test_a_work_order_claims_its_goal_so_a_second_producer_is_refused(tmp_path, ledger):
    router = _router(tmp_path)
    await router.start()
    try:
        assert await router.ingest(_work_order_env("op-wo")) == "enqueued"
        assert await router.ingest(_goal_env("op-sentinel", goal_id="goal-w",
                                             files=("backend/w2.py",))) == "deduplicated"
    finally:
        await router.stop()
    assert _dispatched() == ["op-wo"]


@pytest.mark.asyncio
async def test_a_satisfied_goal_is_refused_while_its_landing_exists(tmp_path, ledger):
    sha = _repo_with_commit(tmp_path)
    await L.record_landing(goal_id="goal-w", goal_digest_hex="", commit_sha=sha, op_id="op-1")
    await L.record_terminal(goal_id="goal-w", op_id="op-1")
    router = _router(tmp_path)
    await router.start()
    try:
        # the boot-time work order AND a resume of the very op that landed
        assert await router.ingest(_work_order_env("op-after-restart")) == "deduplicated"
        claim = await L.claim_dispatch(goal_id="goal-w", goal_digest_hex="", op_id="op-1")
        assert not claim.claimed and claim.reason == "already_satisfied"
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_work_that_was_undone_is_admitted_again(tmp_path, ledger):
    _repo_with_commit(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "auto")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "landing on an accumulation branch")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    await L.record_landing(goal_id="goal-u", goal_digest_hex="", commit_sha=sha, op_id="op-1")
    await L.record_terminal(goal_id="goal-u", op_id="op-1")
    # an accumulation-branch landing counts as present -- not only main
    assert L.landing_present(sha, cwd=tmp_path) is True
    assert (await L.claim_dispatch(goal_id="goal-u", goal_digest_hex="", op_id="op-2")).reason \
        == "already_satisfied"
    # the boot reaper / a reset removes it: no branch holds the work any more
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "branch", "-q", "-D", "auto")
    assert L.landing_present(sha, cwd=tmp_path) is False
    assert (await L.claim_dispatch(goal_id="goal-u", goal_digest_hex="", op_id="op-3")).claimed


def test_git_that_cannot_answer_leaves_the_ledger_in_charge(tmp_path):
    assert L.landing_present("0" * 40, cwd=tmp_path / "not-a-repo") in (None, False)
    assert L.landing_present("", cwd=tmp_path) is None


@pytest.mark.asyncio
async def test_unbound_sources_never_touch_the_ledger(tmp_path, ledger):
    router = _router(tmp_path)
    await router.start()
    try:
        env = make_envelope(source="backlog", description="fix", target_files=("backend/a.py",), repo="jarvis",
                            confidence=0.8, urgency="normal", evidence={"signature": "s"}, requires_human_ack=False)
        assert await router.ingest(env) == "enqueued"
    finally:
        await router.stop()
    assert not L._ledger_has_rows(ledger)
