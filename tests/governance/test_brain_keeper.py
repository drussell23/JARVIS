"""Tests for backend/core/ouroboros/governance/brain_keeper.py (Stage-4 Task 3).

The Body-side KEEPER: persistent token bucket + sustained-absence resurrection
state machine. NO live GCP, NO sleeps -- the clock is injected and the
provision collaborator is a fake.

Proves:
  bucket:
    (a) persistence across instances -- the journal IS the state (2 takes,
        a NEW instance refuses within the window);
    (b) a record OUTSIDE the rolling window is ignored (injected old ts_wall);
    (c) refusal appends NOTHING (deterministic: replaying the same journal
        yields the same refusal, no growth);
  keeper:
    (d) absence-window arithmetic against a fake clock (<= threshold stays
        absent; > threshold resurrects);
    (e) reset-on-discovery (a truthy url clears the window; no provision);
    (f) single-flight (concurrent ticks -> exactly one provision);
    (g) cap_exhausted is TERMINAL (never provisions/retries past it; the
        error is logged exactly once);
    (h) gen minting is monotonic from the manifest;
    (i) record-at-birth (the manifest gains the create record with
        labels+gen BEFORE the provision resolves -- a keeper death
        mid-provision leaves the child on the manifest);
    (j) a failed provision SPENDS the token (VM-factory guard) and returns
        the machine to absent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import brain_keeper as bk
from backend.core.ouroboros.governance import brain_lifecycle as bl


@pytest.fixture()
def bucket_path(tmp_path, monkeypatch):
    path = tmp_path / "manifests" / "resurrect_bucket.jsonl"
    monkeypatch.setenv("JARVIS_RESURRECT_BUCKET_PATH", str(path))
    return path


@pytest.fixture()
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "manifests" / "resource_manifest.jsonl"
    monkeypatch.setenv("JARVIS_RESOURCE_MANIFEST_PATH", str(path))
    return bl.ResourceManifest()


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class _FakeProvisioner:
    """Records every provision call; scriptable result / exception / block."""

    def __init__(self, ok: bool = True, raise_exc: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.ok = ok
        self.raise_exc = raise_exc
        self.started = asyncio.Event()
        self.release: Optional[asyncio.Event] = None

    async def __call__(self, *, node_name: str, labels: Dict[str, str],
                       extra_env: Dict[str, str]) -> Tuple[bool, str]:
        self.calls.append({
            "node_name": node_name, "labels": dict(labels),
            "extra_env": dict(extra_env),
        })
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.raise_exc:
            raise RuntimeError("gcp exploded mid-create")
        return (self.ok, "created:done" if self.ok else "QUOTA_EXCEEDED")


async def _no_brain() -> Optional[str]:
    return None


def _mk_keeper(manifest: Any, bucket: Any, clock: Any,
               provision: Any, *, discover: Any = _no_brain,
               after_s: float = 900.0) -> Any:
    return bk.BrainKeeper(
        discover_fn=discover, provision_fn=provision, manifest=manifest,
        bucket=bucket, resurrect_after_s=after_s, keeper_id="mac-body-keeper",
        clock=clock,
    )


def _journal_lines(path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# (a) Bucket persistence across instances -- the journal IS the state.
# ---------------------------------------------------------------------------


def test_bucket_persists_across_instances(bucket_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "2")
    b1 = bk.PersistentTokenBucket()
    assert b1.try_take(gen=1) is True
    assert b1.try_take(gen=2) is True

    # A brand-new instance (a restarted process) replays the SAME journal.
    b2 = bk.PersistentTokenBucket()
    assert b2.try_take(gen=3) is False, (
        "the cap must be deterministic across process restarts")
    assert len(_journal_lines(bucket_path)) == 2


def test_bucket_take_record_shape(bucket_path) -> None:
    before = time.time()
    bk.PersistentTokenBucket().try_take(gen=7)
    (rec,) = _journal_lines(bucket_path)
    assert rec["gen"] == 7
    assert "ts_utc" in rec
    assert before <= float(rec["ts_wall"]) <= time.time(), (
        "ts_wall must be wall-clock time.time() (survives restarts)")


# ---------------------------------------------------------------------------
# (b) Records outside the rolling window are ignored.
# ---------------------------------------------------------------------------


def test_bucket_ignores_takes_outside_window(bucket_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "2")
    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_WINDOW_S", "3600")
    bucket = bk.PersistentTokenBucket()
    # Two OLD takes (2h ago) injected straight into the journal.
    bucket_path.parent.mkdir(parents=True, exist_ok=True)
    old = time.time() - 7200.0
    with bucket_path.open("a", encoding="utf-8") as fh:
        for gen in (1, 2):
            fh.write(json.dumps(
                {"ts_utc": "old", "ts_wall": old, "gen": gen}) + "\n")

    assert bucket.taken_in_window() == 0, "expired takes must not count"
    assert bucket.try_take(gen=3) is True
    assert bucket.try_take(gen=4) is True
    assert bucket.try_take(gen=5) is False, "in-window takes hit the cap"


# ---------------------------------------------------------------------------
# (c) Refusal appends NOTHING (determinism; no retry/backoff state).
# ---------------------------------------------------------------------------


def test_bucket_refusal_appends_nothing(bucket_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "1")
    bucket = bk.PersistentTokenBucket()
    assert bucket.try_take(gen=1) is True
    for _ in range(5):
        assert bucket.try_take(gen=99) is False
    lines = _journal_lines(bucket_path)
    assert len(lines) == 1, "a refused take must leave NO trace"
    assert lines[0]["gen"] == 1


def test_bucket_tolerates_corrupt_lines(bucket_path) -> None:
    bucket = bk.PersistentTokenBucket()
    assert bucket.try_take(gen=1) is True
    with bucket_path.open("a", encoding="utf-8") as fh:
        fh.write("{not json!!\n\n")
    assert bucket.taken_in_window() == 1, "corrupt lines skipped, not fatal"


# ---------------------------------------------------------------------------
# (d) Absence-window arithmetic (fake clock; no sleeps).
# ---------------------------------------------------------------------------


def test_absence_window_arithmetic(manifest, bucket_path) -> None:
    clock = _FakeClock()
    prov = _FakeProvisioner()
    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov,
                        after_s=900.0)

    assert asyncio.run(keeper.tick()) == "healthy", "no observation yet"
    keeper.note_discovery_result(None)          # absence starts at t
    clock.advance(899.0)
    assert asyncio.run(keeper.tick()) == "absent"
    assert prov.calls == [], "below the threshold: never provision"

    clock.advance(2.0)                          # 901s > 900s: sustained
    assert asyncio.run(keeper.tick()) == "resurrected"
    assert len(prov.calls) == 1

    # Later misses must NOT refresh the start timestamp ('continuously').
    keeper2 = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock,
                         _FakeProvisioner(), after_s=900.0)
    keeper2.note_discovery_result(None)
    clock.advance(500.0)
    keeper2.note_discovery_result(None)         # continue, not restart
    clock.advance(401.0)                        # 901s from the FIRST miss
    assert asyncio.run(keeper2.tick()) == "resurrected"


def test_resurrect_after_s_env_default(manifest, bucket_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_AFTER_S", "60")
    keeper = bk.BrainKeeper(
        discover_fn=_no_brain, provision_fn=_FakeProvisioner(),
        manifest=manifest, bucket=bk.PersistentTokenBucket(),
    )
    assert keeper._resurrect_after_s == 60.0
    assert keeper._keeper_id == "mac-body-keeper", "spec default keeper id"


# ---------------------------------------------------------------------------
# (e) Reset-on-discovery.
# ---------------------------------------------------------------------------


def test_discovery_resets_absence_window(manifest, bucket_path) -> None:
    clock = _FakeClock()
    prov = _FakeProvisioner()
    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov)

    keeper.note_discovery_result(None)
    clock.advance(800.0)
    keeper.note_discovery_result("wss://10.0.0.5:8443/ws")   # brain answered
    clock.advance(5000.0)
    assert asyncio.run(keeper.tick()) == "healthy"
    assert prov.calls == [], "a reset window must never provision"

    # A fresh absence after the reset starts a NEW window.
    keeper.note_discovery_result(None)
    clock.advance(901.0)
    assert asyncio.run(keeper.tick()) == "resurrected"
    assert len(prov.calls) == 1


# ---------------------------------------------------------------------------
# (f) Single-flight: concurrent ticks -> exactly one provision.
# ---------------------------------------------------------------------------


def test_single_flight_concurrent_ticks(manifest, bucket_path) -> None:
    clock = _FakeClock()
    prov = _FakeProvisioner()
    prov.release = asyncio.Event()
    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov)
    keeper.note_discovery_result(None)
    clock.advance(901.0)

    async def scenario() -> None:
        t1 = asyncio.ensure_future(keeper.tick())
        await prov.started.wait()               # first tick is inside provision
        assert await keeper.tick() == "resurrecting", (
            "a concurrent tick must observe the in-flight resurrection")
        assert await keeper.tick() == "resurrecting"
        prov.release.set()
        assert await t1 == "resurrected"

    asyncio.run(scenario())
    assert len(prov.calls) == 1, "single-flight: exactly ONE provision"


# ---------------------------------------------------------------------------
# (g) cap_exhausted is TERMINAL: never retries, error logged exactly once.
# ---------------------------------------------------------------------------


def test_cap_exhausted_terminal_and_logs_once(
        manifest, bucket_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "0")  # always refuse
    clock = _FakeClock()
    prov = _FakeProvisioner()
    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov)
    keeper.note_discovery_result(None)
    clock.advance(901.0)

    with caplog.at_level(logging.ERROR):
        assert asyncio.run(keeper.tick()) == "cap_exhausted"
        for _ in range(5):
            clock.advance(10000.0)
            assert asyncio.run(keeper.tick()) == "cap_exhausted", (
                "TERMINAL: tick must NEVER retry past cap_exhausted")

    assert prov.calls == [], "no provision may follow a refused token"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "the terminal transition logs ERROR exactly once"
    assert "EXHAUSTED" in errors[0].getMessage()

    # Even a recovered brain does not un-exhaust this keeper (state persists;
    # only a NEW keeper process with restored capacity can resurrect).
    keeper.note_discovery_result("wss://10.0.0.5:8443/ws")
    assert asyncio.run(keeper.tick()) == "cap_exhausted"


# ---------------------------------------------------------------------------
# (h) Gen minting is monotonic from the manifest.
# ---------------------------------------------------------------------------


def test_gen_monotonic_from_manifest(manifest, bucket_path) -> None:
    manifest.record_create(kind="instance", name="jarvis-brain-gen3-old",
                           gen=3, keeper_id="mac-body-keeper")
    clock = _FakeClock()
    prov = _FakeProvisioner()
    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov)
    assert keeper.current_gen() == 3

    keeper.note_discovery_result(None)
    clock.advance(901.0)
    assert asyncio.run(keeper.tick()) == "resurrected"

    call = prov.calls[0]
    assert call["node_name"].startswith("jarvis-brain-gen4-")
    assert call["labels"][bl.LABEL_GEN] == "4"
    assert call["extra_env"]["JARVIS_BRAIN_GENERATION"] == "4"
    assert call["extra_env"]["JARVIS_BRAIN_PARENT_NODE"] == call["node_name"]
    assert keeper.current_gen() == 4


# ---------------------------------------------------------------------------
# (i) Record-at-birth: the manifest gains the record with labels+gen.
# ---------------------------------------------------------------------------


def test_record_at_birth_with_labels_and_gen(manifest, bucket_path) -> None:
    clock = _FakeClock()
    prov = _FakeProvisioner()
    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov)
    keeper.note_discovery_result(None)
    clock.advance(901.0)
    asyncio.run(keeper.tick())

    node_name = prov.calls[0]["node_name"]
    fam = manifest.live_family("mac-body-keeper")
    assert [r["name"] for r in fam] == [node_name]
    rec = fam[0]
    assert rec["gen"] == 1
    assert rec["kind"] == "instance"
    assert rec["keeper_id"] == "mac-body-keeper"
    assert rec["labels"][bl.LABEL_OWNER] == "mac-body-keeper"
    assert rec["labels"][bl.LABEL_PARENT] == "mac-body-keeper"
    assert rec["labels"][bl.LABEL_GEN] == "1"


def test_record_at_birth_survives_provision_death(manifest, bucket_path) -> None:
    """A provision that RAISES mid-create (keeper-death proxy) must still
    leave the child on the manifest -- the record is appended BEFORE the
    provision await (the Task-1 record_create semantics)."""
    clock = _FakeClock()
    prov = _FakeProvisioner(raise_exc=True)
    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov)
    keeper.note_discovery_result(None)
    clock.advance(901.0)
    state = asyncio.run(keeper.tick())

    assert state == "absent", "a failed provision returns the machine to absent"
    names = [r["name"] for r in manifest.live_family("mac-body-keeper")]
    assert len(names) == 1 and names[0].startswith("jarvis-brain-gen1-"), (
        "record-at-birth: the child is on the manifest for the next walk")


# ---------------------------------------------------------------------------
# (j) A failed provision SPENDS the token (VM-factory guard).
# ---------------------------------------------------------------------------


def test_failed_provision_spends_token(manifest, bucket_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "2")
    clock = _FakeClock()
    prov = _FakeProvisioner(ok=False)           # clean (False, detail) failure
    bucket = bk.PersistentTokenBucket()
    keeper = _mk_keeper(manifest, bucket, clock, prov)
    keeper.note_discovery_result(None)
    clock.advance(901.0)

    assert asyncio.run(keeper.tick()) == "absent"     # attempt 1: token spent
    assert bucket.taken_in_window() == 1
    assert asyncio.run(keeper.tick()) == "absent"     # attempt 2: token spent
    assert bucket.taken_in_window() == 2
    assert asyncio.run(keeper.tick()) == "cap_exhausted", (
        "failed attempts consume capacity by design (VM-factory guard)")
    assert len(prov.calls) == 2


# ---------------------------------------------------------------------------
# resurrected -> healthy closure via the confirmation probe.
# ---------------------------------------------------------------------------


def test_resurrected_confirms_via_discover_fn(manifest, bucket_path) -> None:
    """The tick after a successful resurrection issues ONE confirmation
    probe through discover_fn; a truthy answer closes resurrected->healthy."""
    clock = _FakeClock()
    prov = _FakeProvisioner()
    probes = {"n": 0}

    async def discover() -> Optional[str]:
        probes["n"] += 1
        return "wss://10.0.0.9:8443/ws"

    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov,
                        discover=discover)
    keeper.note_discovery_result(None)
    clock.advance(901.0)
    assert asyncio.run(keeper.tick()) == "resurrected"
    assert probes["n"] == 0, "the resurrection tick itself never probes"
    assert asyncio.run(keeper.tick()) == "healthy"
    assert probes["n"] == 1
    assert len(prov.calls) == 1, "confirmation probing never re-provisions"


def test_resurrected_missed_probe_rearms_absence_window(
        manifest, bucket_path) -> None:
    """A MISSED confirmation probe (new node still dark) starts a FRESH
    absence window for the new generation -- the machine re-arms and a
    still-dark resurrection can escalate to the next gen, bounded by the
    bucket. The driver's discovery feed can still flip it healthy."""
    clock = _FakeClock()
    prov = _FakeProvisioner()
    keeper = _mk_keeper(manifest, bk.PersistentTokenBucket(), clock, prov,
                        discover=_no_brain)
    keeper.note_discovery_result(None)
    clock.advance(901.0)
    assert asyncio.run(keeper.tick()) == "resurrected"
    assert asyncio.run(keeper.tick()) == "absent", (
        "missed confirm probe -> fresh window, honest absent state")
    assert len(prov.calls) == 1, "the fresh window must not instantly re-fire"

    # The driver's own discovery feed finds the new node -> healthy.
    keeper.note_discovery_result("wss://10.0.0.9:8443/ws")
    assert asyncio.run(keeper.tick()) == "healthy"


# ---------------------------------------------------------------------------
# Review fix IMPORTANT-1: the ENTIRE read-count-decide-append of try_take
# runs inside ONE flock_critical_section acquisition (no cross-process
# TOCTOU), and a lock that cannot be acquired REFUSES (fail-closed).
# ---------------------------------------------------------------------------


def test_try_take_decides_and_appends_under_the_lock(
        bucket_path, monkeypatch) -> None:
    """Recording monkeypatch of flock_critical_section: EVERY try_take
    (grants AND refusals) acquires the section, and the grant's journal
    append happens strictly BETWEEN enter and exit -- i.e. while the real
    lock is held (an unlocked decide + locked append is the TOCTOU the
    review flagged)."""
    from contextlib import contextmanager

    from backend.core.ouroboros.governance import cross_process_jsonl as cpj

    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "1")
    real_section = cpj.flock_critical_section
    events: List[Dict[str, Any]] = []

    def _lines() -> int:
        return len(_journal_lines(bucket_path))

    @contextmanager
    def recording_section(path, **kwargs):
        entry = {"path": str(path), "lines_at_enter": _lines()}
        with real_section(path, **kwargs) as acquired:
            yield acquired
            # Still INSIDE the real lock: the caller's decide+append body
            # has run by now (contextmanager resumes after the with-body).
            entry["lines_before_exit"] = _lines()
        events.append(entry)

    monkeypatch.setattr(cpj, "flock_critical_section", recording_section)
    bucket = bk.PersistentTokenBucket()

    assert bucket.try_take(gen=1) is True
    assert bucket.try_take(gen=2) is False    # refusal path
    assert len(events) == 2, (
        "EVERY take decision (grant and refusal) must run under the section")
    for e in events:
        assert str(bucket_path) in e["path"]
    grant, refusal = events[0], events[1]
    assert grant["lines_at_enter"] == 0 and grant["lines_before_exit"] == 1, (
        "the grant append must land INSIDE the held section")
    assert refusal["lines_at_enter"] == 1 and refusal["lines_before_exit"] == 1, (
        "a refusal must append NOTHING inside the section")


def test_try_take_fail_closed_when_lock_unavailable(
        bucket_path, monkeypatch, caplog) -> None:
    """A take that cannot acquire the lock REFUSES and appends nothing --
    a billing guard that cannot prove capacity must never spend."""
    from contextlib import contextmanager

    from backend.core.ouroboros.governance import cross_process_jsonl as cpj

    @contextmanager
    def never_acquires(path, **kwargs):
        yield False

    monkeypatch.setattr(cpj, "flock_critical_section", never_acquires)
    bucket = bk.PersistentTokenBucket()
    with caplog.at_level(logging.ERROR):
        assert bucket.try_take(gen=1) is False
    assert _journal_lines(bucket_path) == [], "no lock -> no trace"
    assert any("fail-closed" in r.getMessage() for r in caplog.records), (
        "the refused-by-lock path must be LOUD")


def test_try_take_concurrent_instances_never_exceed_capacity(
        bucket_path, monkeypatch) -> None:
    """Two bucket INSTANCES (the zombie-driver-overlapping-fresh-driver
    shape, serialized here by the section's in-process lock tier) racing
    try_take on one journal: exactly ONE grant at capacity 1, exactly one
    journal line -- the decide can never run against a stale count."""
    import threading

    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "1")
    b1 = bk.PersistentTokenBucket()
    b2 = bk.PersistentTokenBucket()
    barrier = threading.Barrier(2)
    results: List[bool] = []
    lock = threading.Lock()

    def race(bucket: Any, gen: int) -> None:
        barrier.wait()
        got = bucket.try_take(gen=gen)
        with lock:
            results.append(got)

    t1 = threading.Thread(target=race, args=(b1, 1))
    t2 = threading.Thread(target=race, args=(b2, 2))
    t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)

    assert sorted(results) == [False, True], (
        "exactly one racer may win the last token: %r" % results)
    assert len(_journal_lines(bucket_path)) == 1, (
        "the cap must never be exceeded by a race")


# ---------------------------------------------------------------------------
# Review fix IMPORTANT-2: grep-enforced gen-minter uniqueness invariant.
# ---------------------------------------------------------------------------


def test_gen_minter_uniqueness_invariant() -> None:
    """Exactly ONE shipped source file may mint generations
    (``max_gen() + 1``), and it must be brain_keeper.py. Two independent
    minters racing the same manifest could stamp duplicate generation
    numbers -- the gen sequence is only monotonic because there is a
    single minter."""
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    pattern = re.compile(r"max_gen\(\)\s*\+\s*1")
    hits: List[str] = []
    for base in ("backend", "scripts"):
        for py in sorted((repo_root / base).rglob("*.py")):
            try:
                text = py.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pattern.search(text):
                hits.append(str(py.relative_to(repo_root)))
    assert hits == [
        "backend/core/ouroboros/governance/brain_keeper.py",
    ], "gen minting must have exactly ONE home: %r" % hits


# ---------------------------------------------------------------------------
# Stage-4 Task-4 (IMPORTANT-2/4): keeper-delivered fence supersession + reap.
# ---------------------------------------------------------------------------


def _record_gen_node(manifest: Any, keeper_id: str, gen: int) -> str:
    """Record a live Brain node owned by the keeper (parent=keeper_id), as
    _resurrect's record-at-birth does."""
    name = "jarvis-brain-gen%d-node" % gen
    manifest.record_create(
        kind="instance", name=name,
        labels={bl.LABEL_OWNER: keeper_id, bl.LABEL_GEN: str(gen)},
        parent=keeper_id, gen=gen, keeper_id=keeper_id,
    )
    return name


def test_supersede_delivers_fence_then_reaps_lower_gens(manifest) -> None:
    """After a new gen N, the keeper delivers console.keeper_heartbeat{gen:N}
    to every LIVE node with gen < N (its self-fence + capture_inflight window),
    then reaps it. The just-minted gen N is NOT superseded."""
    kid = "mac-body-keeper"
    g1 = _record_gen_node(manifest, kid, 1)
    g2 = _record_gen_node(manifest, kid, 2)
    g3 = _record_gen_node(manifest, kid, 3)  # the NEW gen -- must NOT be reaped

    delivered: List[Tuple[str, int]] = []
    reaped: List[str] = []

    async def fake_deliver(node_name: str, gen: int) -> bool:
        delivered.append((node_name, gen))
        return True

    async def fake_delete(name: str, *, zone: Any = None) -> Tuple[bool, str]:
        reaped.append(name)
        return (True, "deleted:200")

    keeper = bk.BrainKeeper(
        discover_fn=_no_brain,
        provision_fn=_FakeProvisioner(),
        manifest=manifest, bucket=bk.PersistentTokenBucket(),
        keeper_id=kid,
        fence_deliver_fn=fake_deliver,
        delete_instance_fn=fake_delete,
        fence_grace_s=0.0,  # no real sleep in tests
    )

    result = asyncio.run(keeper.supersede_old_generations(3))

    assert set(result["superseded"]) == {g1, g2}
    assert g3 not in result["superseded"], "the new gen must never self-reap"
    # The fence heartbeat carries the NEW gen so the old node observes gen>own.
    assert set(delivered) == {(g1, 3), (g2, 3)}
    # Both old nodes reaped after the grace.
    assert set(reaped) == {g1, g2}
    # Manifest converged: the reaped nodes are tombstoned, only g3 remains live.
    live = {r["name"] for r in manifest.live_family(kid)}
    assert live == {g3}


def test_supersede_delivery_failure_falls_through_to_reap(manifest) -> None:
    """An unreachable node (delivery raises/False) MUST still be reaped -- the
    reap is the backstop; delivery never blocks it."""
    kid = "mac-body-keeper"
    g1 = _record_gen_node(manifest, kid, 1)
    _record_gen_node(manifest, kid, 2)  # new gen

    reaped: List[str] = []

    async def failing_deliver(node_name: str, gen: int) -> bool:
        raise RuntimeError("node unreachable")

    async def fake_delete(name: str, *, zone: Any = None) -> Tuple[bool, str]:
        reaped.append(name)
        return (True, "deleted:200")

    keeper = bk.BrainKeeper(
        discover_fn=_no_brain, provision_fn=_FakeProvisioner(),
        manifest=manifest, bucket=bk.PersistentTokenBucket(), keeper_id=kid,
        fence_deliver_fn=failing_deliver, delete_instance_fn=fake_delete,
        fence_grace_s=0.0,
    )

    result = asyncio.run(keeper.supersede_old_generations(2))
    assert result["delivered"] == {g1: False}
    assert reaped == [g1], "a failed delivery must still fall through to reap"


def test_reap_generation_passes_owner_id_for_drift_query(manifest) -> None:
    """IMPORTANT-4 wiring: reap_generation calls teardown_family with
    owner_id=keeper_id so the drift detector queries the keeper-owner label."""
    kid = "mac-body-keeper"
    node = _record_gen_node(manifest, kid, 1)
    query_calls: List[Dict[str, Any]] = []

    async def fake_delete(name: str, *, zone: Any = None) -> Tuple[bool, str]:
        return (True, "deleted:200")

    async def label_query(**kwargs: Any) -> List[Dict[str, Any]]:
        query_calls.append(kwargs)
        return []

    keeper = bk.BrainKeeper(
        discover_fn=_no_brain, provision_fn=_FakeProvisioner(),
        manifest=manifest, bucket=bk.PersistentTokenBucket(), keeper_id=kid,
        delete_instance_fn=fake_delete, label_query_fn=label_query,
    )

    result = asyncio.run(keeper.reap_generation(node))
    assert node in result["deleted"]
    assert query_calls == [
        {"label_key": bl.LABEL_OWNER, "label_value": kid},
    ], "the drift query must key on the keeper-owner label, not the node name"


def test_reap_generation_drift_flags_unowned_grandchild(manifest) -> None:
    """The failover grandchild -- now carrying LABEL_OWNER=keeper -- shows up
    in the keeper-keyed drift query. If the local manifest never recorded it
    (cross-host birth), it is flagged as drift (NOT auto-deleted)."""
    kid = "mac-body-keeper"
    node = _record_gen_node(manifest, kid, 1)

    async def fake_delete(name: str, *, zone: Any = None) -> Tuple[bool, str]:
        return (True, "deleted:200")

    async def label_query(**kwargs: Any) -> List[Dict[str, Any]]:
        return [{"name": "jarvis-prime-failover"}]  # owner=keeper, unrecorded here

    keeper = bk.BrainKeeper(
        discover_fn=_no_brain, provision_fn=_FakeProvisioner(),
        manifest=manifest, bucket=bk.PersistentTokenBucket(), keeper_id=kid,
        delete_instance_fn=fake_delete, label_query_fn=label_query,
    )
    result = asyncio.run(keeper.reap_generation(node))
    assert [d["name"] for d in result["drift"]] == ["jarvis-prime-failover"]


def test_resurrect_success_triggers_supersession(manifest, bucket_path,
                                                  monkeypatch) -> None:
    """End-to-end: a successful resurrection to gen N supersedes the prior
    live gen (delivers the fence + reaps). Proves supersede is wired into
    _resurrect's success branch."""
    monkeypatch.setenv("JARVIS_BRAIN_RESURRECT_MAX_PER_H", "5")
    kid = "mac-body-keeper"
    # A prior live gen-1 node exists on the manifest.
    g1 = _record_gen_node(manifest, kid, 1)

    delivered: List[Tuple[str, int]] = []
    reaped: List[str] = []

    async def fake_deliver(node_name: str, gen: int) -> bool:
        delivered.append((node_name, gen))
        return True

    async def fake_delete(name: str, *, zone: Any = None) -> Tuple[bool, str]:
        reaped.append(name)
        return (True, "deleted:200")

    clock = _FakeClock()
    keeper = bk.BrainKeeper(
        discover_fn=_no_brain, provision_fn=_FakeProvisioner(ok=True),
        manifest=manifest, bucket=bk.PersistentTokenBucket(),
        resurrect_after_s=900.0, keeper_id=kid, clock=clock,
        fence_deliver_fn=fake_deliver, delete_instance_fn=fake_delete,
        fence_grace_s=0.0,
    )
    # Drive the FSM into sustained absence -> resurrection (gen 2).
    keeper.note_discovery_result(None)  # window starts
    clock.advance(901.0)                # exceed resurrect_after_s
    state = asyncio.run(keeper.tick())

    assert state == "resurrected"
    # gen 2 was minted (max_gen was 1) and the old gen-1 node was superseded.
    assert delivered == [(g1, 2)]
    assert reaped == [g1]
