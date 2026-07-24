"""Bulletproof spine for the Autonomous Wake-and-Execute (AWE) Trigger.

Mandated assertions, all against REAL infra (real StreamEventBroker, real SQLite
on a temp file, real guarded-UPDATE lock) so the fakes cannot mask a buggy
contract:

  (1) the trigger IDLES while the state is DEGRADED,
  (2) a broker emission of HEALTHY (DEGRADED→HEALTHY edge) INSTANTLY fires it,
  (3) the atomic soak lock is ACQUIRED,
  (4) a flap back to DEGRADED then HEALTHY a moment later does NOT fire a second
      parallel swarm (single-launch under flapping), and
  (5) the injected swarm strategy EXECUTES CLEANLY (terminal breadcrumb emitted).

Only the launch_fn is a fake (a counting coroutine) — everything it touches
(broker delivery, edge detection, the SQLite compare-and-swap) is the real code.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from backend.core.ouroboros.governance.awe_trigger import AWETrigger
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_PROVIDER_STATE_CHANGED,
    get_default_broker,
    reset_default_broker,
)
from backend.core.ouroboros.governance.soak_execution_lock import read_soak_lock


async def _wait_for(cond, timeout: float = 2.0) -> None:
    async def _loop() -> None:
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_loop(), timeout)


def _publish(provider: str, state: str, previous_state: str) -> None:
    """Emit a real provider_state_changed frame onto the real broker — the exact
    delivery path the production ProviderStateWatcher uses."""
    get_default_broker().publish(
        EVENT_TYPE_PROVIDER_STATE_CHANGED,
        provider,
        {"provider": provider, "state": state, "previous_state": previous_state},
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "chunk_strategy.db")


def _make_trigger(db_path, launch_fn, crumbs):
    return AWETrigger(
        launch_fn=launch_fn,
        db_factory=lambda: sqlite3.connect(db_path),
        breadcrumb_fn=lambda et, p: crumbs.append((et, dict(p))),
        cooldown_s=3600.0,           # any flap within an hour is suppressed
        run_id_factory=lambda: "run-fixed",
    )


async def test_awe_full_recovery_lifecycle(db_path):
    reset_default_broker()
    launched: list = []
    launch_started = asyncio.Event()

    async def fake_launch(run_id: str):
        launched.append(run_id)
        launch_started.set()
        await asyncio.sleep(0)         # a real detached coroutine that completes
        return "swarm-ok"

    crumbs: list = []
    trig = _make_trigger(db_path, fake_launch, crumbs)
    trig.start()
    try:
        # Ensure the broker subscription is live before we publish (no race).
        await asyncio.wait_for(trig._subscribed.wait(), timeout=2.0)

        # (1) DEGRADED → the trigger idles: no launch, no lock claim.
        _publish("doubleword", "DEGRADED", "UNKNOWN")
        await asyncio.sleep(0.1)
        assert launched == []
        lock = read_soak_lock(sqlite3.connect(db_path))
        assert lock is None or lock.get("claimed", 0) == 0

        # (2) DEGRADED→HEALTHY edge → fires instantly.
        _publish("doubleword", "HEALTHY", "DEGRADED")
        await asyncio.wait_for(launch_started.wait(), timeout=2.0)
        assert launched == ["run-fixed"]

        # (3) the atomic lock was acquired.
        lock = read_soak_lock(sqlite3.connect(db_path))
        assert lock is not None and lock["claimed"] == 1
        assert lock["run_id"] == "run-fixed"

        # (4) a flap (HEALTHY→DEGRADED→HEALTHY) a moment later must NOT re-fire.
        _publish("doubleword", "DEGRADED", "HEALTHY")   # intermediate, not a launch edge
        _publish("doubleword", "HEALTHY", "DEGRADED")   # redundant recovery edge
        await asyncio.sleep(0.15)
        assert launched == ["run-fixed"], "flap must not launch a second swarm"
        assert trig._launch_count == 1
        # The suppression was observable.
        assert any(et == "awe_soak_suppressed" for et, _ in crumbs)

        # (5) the swarm strategy executed cleanly → terminal breadcrumb emitted.
        await _wait_for(lambda: any(et == "awe_soak_complete" for et, _ in crumbs))
        launched_types = [et for et, _ in crumbs]
        assert "awe_soak_launched" in launched_types
        assert "awe_soak_complete" in launched_types
        assert "awe_soak_failed" not in launched_types
    finally:
        await trig.stop()
        reset_default_broker()


async def test_awe_idles_on_non_healthy_states(db_path):
    """A stricter idle proof: UNKNOWN and HEALTHY→HEALTHY (a non-edge) never
    fire, only a true transition into HEALTHY does."""
    reset_default_broker()
    launched: list = []

    async def fake_launch(run_id: str):
        launched.append(run_id)
        return "ok"

    trig = _make_trigger(db_path, fake_launch, [])
    # Drive on_state_event directly for exhaustive edge coverage.
    assert await trig.on_state_event({"state": "DEGRADED", "previous_state": "HEALTHY"}) is False
    assert await trig.on_state_event({"state": "UNKNOWN", "previous_state": "DEGRADED"}) is False
    assert await trig.on_state_event({"state": "HEALTHY", "previous_state": "HEALTHY"}) is False
    assert await trig.on_state_event({}) is False
    assert launched == []
    # The one true edge fires (the soak is DETACHED, so await it to observe).
    assert await trig.on_state_event({"state": "HEALTHY", "previous_state": "DEGRADED"}) is True
    await _wait_for(lambda: launched == ["run-fixed"])
    await trig.stop()


async def test_awe_lock_is_atomic_single_winner(db_path):
    """Two concurrent trigger instances sharing the DB: the guarded UPDATE lets
    exactly ONE win the claim (compare-and-swap), even racing the same edge."""
    reset_default_broker()
    winners: list = []

    def make(idx):
        async def launch(run_id):
            winners.append(idx)
            return idx
        return _make_trigger(db_path, launch, [])

    t1, t2 = make(1), make(2)
    # Fire the same recovery edge at both, concurrently.
    r1, r2 = await asyncio.gather(
        t1.on_state_event({"state": "HEALTHY", "previous_state": "DEGRADED"}),
        t2.on_state_event({"state": "HEALTHY", "previous_state": "DEGRADED"}),
    )
    assert (r1, r2) in [(True, False), (False, True)], "exactly one claim wins"
    # Let the single winner's detached soak run.
    await _wait_for(lambda: len(winners) == 1)
    assert len(winners) == 1
    lock = read_soak_lock(sqlite3.connect(db_path))
    assert lock is not None and lock["claimed"] == 1


# ---------------------------------------------------------------------------
# Gap #2 — the substrate-aware dispatcher.
#
# Before this, AWE's ONLY strategy was a hardcoded battle-test spawn, so a
# queued checkpoint manifest survived the recovery edge untouched: the intent
# stayed `pending` forever while a generic soak ran in its place. These pin the
# routing decision against REAL SQLite + the REAL next_pending_intent query —
# only the terminal launchers are faked, so a regression in the queue schema or
# the ordering surfaces here rather than at 3 AM.
# ---------------------------------------------------------------------------


def _seed_intent(db_path, *, intent_id, manifest_json, target, priority=1):
    """Enqueue via the REAL production writer (not hand-rolled INSERT), so the
    test binds to the shipping schema."""
    from backend.core.ouroboros.governance.soak_intent import enqueue_soak_intent

    conn = sqlite3.connect(db_path)
    try:
        enqueue_soak_intent(
            conn, kind="canary_soak", target=target, priority=priority,
            intent_id=intent_id, manifest_json=manifest_json,
        )
    finally:
        conn.close()


def _dispatcher(db_path, *, fallback, client=object(), crumbs=None, git_status_fn=None):
    from backend.core.ouroboros.governance.awe_trigger import (
        build_manifest_aware_launch_fn,
    )
    return build_manifest_aware_launch_fn(
        db_factory=lambda: sqlite3.connect(db_path),
        client_factory=lambda: client,
        fallback_fn=fallback,
        breadcrumb_fn=(lambda et, p: crumbs.append((et, dict(p)))) if crumbs is not None else None,
        # Pin a CLEAN tree by default. These are routing tests; without this
        # they read the ambient repo porcelain and fail whenever the developer
        # running them happens to have uncommitted work.
        git_status_fn=git_status_fn or (lambda: []),
    )


async def test_pending_manifest_routes_to_run_pending_soak(db_path, monkeypatch):
    """(1) A pending row carrying a manifest routes DIRECTLY to run_pending_soak
    — and carries the intent's `target` as repo_root, because manifest chunk
    file_paths are stored relative to it."""
    import json

    from backend.core.ouroboros.governance import checkpoint_manifest

    manifest = json.dumps({
        "schema_version": 2, "target": "/tmp/canary",
        "pending_chunks": [{"chunk_id": "c1", "file_path": "mathy.py",
                            "symbol": "factorial", "start_line": 4, "end_line": 8}],
        "completed_chunks": [], "quarantined_chunks": [], "chunk_retry_counts": {},
    })
    _seed_intent(db_path, intent_id="canary01", manifest_json=manifest,
                 target="/tmp/canary")

    seen = {}

    async def fake_run_pending_soak(conn, intent_id, *, client, repo_root="."):
        seen["intent_id"] = intent_id
        seen["repo_root"] = repo_root
        seen["client"] = client
        return "manifest-ran"

    monkeypatch.setattr(checkpoint_manifest, "run_pending_soak", fake_run_pending_soak)

    fallback_calls = []

    async def fallback(run_id):
        fallback_calls.append(run_id)
        return "fallback-ran"

    sentinel_client = object()
    crumbs: list = []
    launch = _dispatcher(db_path, fallback=fallback, client=sentinel_client, crumbs=crumbs)

    result = await launch("run-1")

    assert result == "manifest-ran"
    assert fallback_calls == [], "generic soak must NOT run when a manifest is pending"
    assert seen["intent_id"] == "canary01"
    # The load-bearing detail: repo_root is the intent target, NOT the repo root.
    assert seen["repo_root"] == "/tmp/canary"
    assert seen["client"] is sentinel_client
    strategies = [p.get("strategy") for et, p in crumbs if et == "awe_launch_strategy"]
    assert strategies == ["manifest"], "the routing decision must be observable"


async def test_empty_queue_routes_to_subprocess_fallback(db_path):
    """(2) An empty intent queue cleanly routes to the generic battle-test soak."""
    fallback_calls = []

    async def fallback(run_id):
        fallback_calls.append(run_id)
        return "fallback-ran"

    crumbs: list = []
    launch = _dispatcher(db_path, fallback=fallback, crumbs=crumbs)

    result = await launch("run-2")

    assert result == "fallback-ran"
    assert fallback_calls == ["run-2"]
    strategies = [p.get("strategy") for et, p in crumbs if et == "awe_launch_strategy"]
    assert strategies == ["subprocess_fallback"]


async def test_pending_intent_without_manifest_falls_back(db_path):
    """A pending intent with no manifest is real work but not CHECKPOINTED work
    — route it to the generic soak rather than resuming an empty run."""
    _seed_intent(db_path, intent_id="nomanifest", manifest_json=None, target="/tmp/x")

    fallback_calls = []

    async def fallback(run_id):
        fallback_calls.append(run_id)
        return "fallback-ran"

    crumbs: list = []
    launch = _dispatcher(db_path, fallback=fallback, crumbs=crumbs)
    assert await launch("run-3") == "fallback-ran"
    assert fallback_calls == ["run-3"]
    reasons = [p.get("reason") for et, p in crumbs if et == "awe_launch_strategy"]
    assert reasons == ["intent carries no manifest"]


async def test_unresolvable_client_degrades_to_fallback_not_silence(db_path):
    """A manifest we cannot drive in-process must run the generic soak, NOT drop
    the recovery edge on the floor."""
    import json

    _seed_intent(db_path, intent_id="canary02",
                 manifest_json=json.dumps({"schema_version": 2, "pending_chunks": []}),
                 target="/tmp/canary")

    fallback_calls = []

    async def fallback(run_id):
        fallback_calls.append(run_id)
        return "fallback-ran"

    crumbs: list = []
    launch = _dispatcher(db_path, fallback=fallback, client=None, crumbs=crumbs)
    assert await launch("run-4") == "fallback-ran"
    assert fallback_calls == ["run-4"]


async def test_priority_ordering_picks_the_urgent_intent(db_path):
    """next_pending_intent must hand AWE the same intent the supervisor's arm
    gate counted — highest priority (lowest number) first."""
    import json

    from backend.core.ouroboros.governance.soak_intent import next_pending_intent

    m = json.dumps({"schema_version": 2, "pending_chunks": []})
    _seed_intent(db_path, intent_id="low", manifest_json=m, target="/a", priority=9)
    _seed_intent(db_path, intent_id="urgent", manifest_json=m, target="/b", priority=1)

    conn = sqlite3.connect(db_path)
    try:
        got = next_pending_intent(conn)
    finally:
        conn.close()
    assert got is not None
    assert got.intent_id == "urgent"
    assert got.target == "/b"
    assert got.has_manifest is True


# ---------------------------------------------------------------------------
# Clean-tree invariant + the governance-shield ground truth.
#
# NOTE ON THE SHIELD: these assert that `backend/soak_probes/canary/` is
# ALREADY permitted by the self-modification cage — no whitelist was added, and
# none is needed. The cage matches `ouroboros/governance/` and friends by
# substring; the canary matches nothing. The 22 blocked ops in session
# bt-2026-07-24-045323 were sensor-sourced ops whose file sets touched the
# caged surfaces, i.e. the cage doing its job. Pinning this so nobody later
# "fixes" a block that was never blocking the canary.
# ---------------------------------------------------------------------------


def _engine():
    from backend.core.ouroboros.governance.risk_engine import RiskEngine
    return RiskEngine()


def test_canary_paths_are_not_caged_by_self_mod_shield():
    """(2) A file in backend/soak_probes/canary/ must NOT trip the self-mod or
    kernel sentinels — the canary soak is permitted as-is."""
    eng = _engine()
    for path in (
        "backend/soak_probes/canary/mathy.py",
        "backend/soak_probes/canary/strings_util.py",
        "backend/soak_probes/canary/widgets.py",
    ):
        assert not eng._matches_any([path], eng._self_mod_sentinels()), path
        assert not eng._matches_any([path], eng._kernel_sentinels()), path


def test_governance_paths_remain_caged():
    """(1) The negative control: a core governance file DOES trip the shield.
    If this ever goes green-by-passing, the cage has been hollowed out."""
    eng = _engine()
    caged = "backend/core/ouroboros/governance/orchestrator.py"
    assert eng._matches_any([caged], eng._self_mod_sentinels())


def test_self_mod_sentinel_env_hook_is_additive_only():
    """The cage can be WIDENED by env but never narrowed — there is no
    subtraction/whitelist mechanism, by contract."""
    import os

    eng = _engine()
    base = set(eng._self_mod_sentinels())
    os.environ["JARVIS_EXTRA_SELF_MOD_SENTINELS"] = "backend/soak_probes/"
    try:
        widened = set(eng._self_mod_sentinels())
    finally:
        os.environ.pop("JARVIS_EXTRA_SELF_MOD_SENTINELS", None)
    assert base.issubset(widened), "env hook must never remove a sentinel"


async def test_dirty_tree_aborts_the_soak_launch(db_path):
    """(3) A dirty working tree makes AWE refuse — and it refuses by RAISING,
    so the caller releases the execution lock for a later clean-tree edge."""
    import json

    from backend.core.ouroboros.governance.awe_trigger import DirtyTreeRefusal

    _seed_intent(db_path, intent_id="canary03",
                 manifest_json=json.dumps({"schema_version": 2, "pending_chunks": []}),
                 target="/tmp/canary")

    fallback_calls = []

    async def fallback(run_id):
        fallback_calls.append(run_id)
        return "fallback-ran"

    from backend.core.ouroboros.governance.awe_trigger import (
        build_manifest_aware_launch_fn,
    )
    crumbs: list = []
    launch = build_manifest_aware_launch_fn(
        db_factory=lambda: sqlite3.connect(db_path),
        client_factory=lambda: object(),
        fallback_fn=fallback,
        breadcrumb_fn=lambda et, p: crumbs.append((et, dict(p))),
        # Real porcelain shape: XY + space + path.
        git_status_fn=lambda: [" M backend/core/ouroboros/governance/orchestrator.py",
                               "?? scratch.py"],
    )

    with pytest.raises(DirtyTreeRefusal):
        await launch("run-dirty")

    assert fallback_calls == [], "a dirty tree must not fall through to the soak"
    strategies = [p.get("strategy") for et, p in crumbs if et == "awe_launch_strategy"]
    assert strategies == ["refused_dirty_tree"]


async def test_clean_tree_permits_the_launch(db_path):
    """The positive half: an empty porcelain lets dispatch proceed normally."""
    fallback_calls = []

    async def fallback(run_id):
        fallback_calls.append(run_id)
        return "fallback-ran"

    from backend.core.ouroboros.governance.awe_trigger import (
        build_manifest_aware_launch_fn,
    )
    launch = build_manifest_aware_launch_fn(
        db_factory=lambda: sqlite3.connect(db_path),
        fallback_fn=fallback,
        git_status_fn=lambda: [],
    )
    assert await launch("run-clean") == "fallback-ran"


async def test_clean_tree_allowlist_permits_scoped_dirt(db_path, monkeypatch):
    """An operator may permit specific prefixes without disarming the invariant."""
    monkeypatch.setenv("JARVIS_AWE_CLEAN_TREE_ALLOW", "backend/soak_probes/")

    async def fallback(run_id):
        return "fallback-ran"

    from backend.core.ouroboros.governance.awe_trigger import (
        build_manifest_aware_launch_fn,
    )
    launch = build_manifest_aware_launch_fn(
        db_factory=lambda: sqlite3.connect(db_path),
        fallback_fn=fallback,
        git_status_fn=lambda: [" M backend/soak_probes/canary/mathy.py"],
    )
    assert await launch("run-allow") == "fallback-ran"


async def test_clean_tree_invariant_can_be_disarmed(db_path, monkeypatch):
    monkeypatch.setenv("JARVIS_AWE_REQUIRE_CLEAN_TREE", "false")

    async def fallback(run_id):
        return "fallback-ran"

    from backend.core.ouroboros.governance.awe_trigger import (
        build_manifest_aware_launch_fn,
    )
    launch = build_manifest_aware_launch_fn(
        db_factory=lambda: sqlite3.connect(db_path),
        fallback_fn=fallback,
        git_status_fn=lambda: [" M anything.py"],
    )
    assert await launch("run-disarmed") == "fallback-ran"


async def test_refusal_releases_the_lock_for_a_later_edge(db_path):
    """End-to-end through the REAL trigger: a dirty-tree refusal must not wedge
    the execution lock — the whole reason it raises instead of returning."""
    from backend.core.ouroboros.governance.awe_trigger import (
        build_manifest_aware_launch_fn,
    )
    from backend.core.ouroboros.governance.soak_execution_lock import read_soak_lock

    async def _unused_fallback(run_id):   # correct coroutine shape, never called
        raise AssertionError("fallback must not run on a dirty-tree refusal")

    launch = build_manifest_aware_launch_fn(
        db_factory=lambda: sqlite3.connect(db_path),
        fallback_fn=_unused_fallback,
        git_status_fn=lambda: [" M dirty.py"],
    )
    trigger = _make_trigger(db_path, launch, [])
    fired = await trigger.on_state_event(
        {"state": "HEALTHY", "previous_state": "DEGRADED"},
    )
    assert fired is True, "the edge claims the lock before the launcher runs"
    # Let the detached soak coroutine reach its refusal + release.
    await _wait_for(
        lambda: (read_soak_lock(sqlite3.connect(db_path)) or {}).get("claimed") == 0,
        timeout=3.0,
    )
    lock = read_soak_lock(sqlite3.connect(db_path))
    assert lock is not None and lock["claimed"] == 0, "refusal must free the lock"
