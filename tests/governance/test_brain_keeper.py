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
