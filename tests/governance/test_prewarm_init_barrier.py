"""Pre-Flight Init Barrier -- regression spine (Omni-Soak v5/v6 fix).

THE BUG (captured live): the JIT pre-warm was wired into the DRAIN path
(``dispatch_ready_bundles -> prewarm_window``), but ops exit via the OTHER
path -- ``_flush_aged_ops`` -- which runs its OWN cold disjointness check and
flushes them to legacy ("no disjoint sibling found") BEFORE the drain's
pre-warm ever fires. ``pre-warming oracle`` telemetry NEVER appeared (0 lines).
Root flaw: pre-warming is an INITIALIZATION event, not a runtime-loop event.

THE FIX: the GovernedLoopService ``start()`` sequence ingests
``oracle_prewarm.json`` and warms the SHARED Oracle handle as a BLOCKING boot
step BEFORE the drain/flush loops are scheduled. The timers cannot start until
the brain is fully loaded.

This spine pins (all proofs use a MOCKED / dilated asyncio clock -- NO real
sleeps; the barrier is gated on a controllable :class:`asyncio.Event`):

(a) BARRIER HOLDS: the drain/flush tasks are NOT scheduled until
    ``ingest_prewarm_payload`` returns (an ingest that awaits a controllable
    event -> assert no drain task exists until it resolves).
(b) FIRST-TICK BUNDLE (the v5/v6 fix): after the barrier warms the Oracle, 3
    disjoint-file ops -> on the FIRST ``_flush_aged_ops`` tick the Oracle is
    ALREADY warm -> they are DISJOINT (would BUNDLE allowed=true n=3), NONE
    age out to legacy ("no disjoint sibling found").
(c) OFF byte-identical: master flag off -> no ingest, warmed=0, no payload read.
(d) MISSING / MISMATCH payload -> fail-soft boot proceeds + the runtime JIT
    (``ensure_file_indexed``) is still reachable as the cold-miss fallback.
(e) REUSE: the barrier drives the Oracle's EXISTING ``ingest_prewarm_payload``
    + the EXISTING ``ensure_file_indexed`` JIT -- no new ingester / Oracle.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import meta_goal_wiring as mgw
from backend.core.ouroboros.governance.meta_goal_wiring import (
    ingest_prewarm_barrier,
    oracle_prewarm_payload_path,
)


# ---------------------------------------------------------------------------
# Test doubles -- reuse the real ingest/JIT CONTRACT, not the AST parser.
# ---------------------------------------------------------------------------


class _Node:
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


class _BarrierOracle:
    """A fake Oracle exposing the EXISTING contract the barrier reuses:

    - ``ingest_prewarm_payload(path)`` -- SYNC, SHA256-validates each target
      against the live file, warms ``_file_index`` (mirrors the real Oracle).
    - ``ensure_file_indexed(fp)`` -- the runtime JIT cold-miss fallback.
    - ``find_nodes_in_file(fp)`` -- the sync collision probe surface.

    Starts COLD (``_file_index`` empty). Optionally an ``ingest_gate`` event
    lets a test HOLD the (off-loop) ingest mid-flight to prove the barrier
    blocks scheduling.
    """

    def __init__(self, *, ingest_gate=None) -> None:
        self._file_index = {}
        self._ingest_calls = 0
        self._jit_calls = 0
        self._ingest_gate = ingest_gate

    # -- EXISTING ingest contract the barrier reuses (SYNC, blocking) --------
    def ingest_prewarm_payload(self, path: str) -> int:
        self._ingest_calls += 1
        if self._ingest_gate is not None:
            # Block this off-loop thread until the test releases it -> proves
            # the barrier's await has NOT returned, so nothing downstream was
            # scheduled yet. ``wait()`` is a threading.Event (cross-thread).
            self._ingest_gate.wait()
        try:
            p = Path(path)
            if not p.exists():
                return 0
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return 0
        targets = data.get("targets") if isinstance(data, dict) else None
        if not isinstance(targets, list) or not targets:
            return 0
        verified = []
        for entry in targets:
            fp = str(entry.get("file_path") or "").strip()
            want = str(entry.get("sha256") or "").strip().lower()
            live = Path(fp)
            if not fp or not want or not live.exists():
                return 0
            got = hashlib.sha256(live.read_bytes()).hexdigest()
            if got != want:
                return 0  # mismatch -> DISCARD entire payload, fall to JIT.
            verified.append(fp)
        for fp in verified:
            self._file_index[fp] = {f"{fp}::node"}
        return len(verified)

    # -- EXISTING runtime JIT (cold-miss fallback) --------------------------
    async def ensure_file_indexed(self, file_path, *, _visited=None, _depth=0):
        self._jit_calls += 1
        self._file_index[file_path] = {f"{file_path}::node"}
        return True

    # -- sync collision probe surface ---------------------------------------
    def find_nodes_in_file(self, file_path):
        return [_Node(file_path) for _ in self._file_index.get(file_path, ())]


class _Host:
    """Minimal GLS-shaped host: only ``_oracle`` is read by the barrier."""

    def __init__(self, oracle) -> None:
        self._oracle = oracle


def _write_payload(tmp_path: Path, files) -> Path:
    """Write a SHA256-valid oracle_prewarm.json over real on-disk files."""
    targets = []
    for name in files:
        f = tmp_path / name
        f.write_text(f"# {name}\nX = 1\n", encoding="utf-8")
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        targets.append({"file_path": str(f), "sha256": sha, "coupled": []})
    payload = tmp_path / "oracle_prewarm.json"
    payload.write_text(json.dumps({"targets": targets}), encoding="utf-8")
    return payload


@pytest.fixture(autouse=True)
def _enable_self_warming(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", "true")
    yield


# ---------------------------------------------------------------------------
# (a) BARRIER HOLDS -- drain/flush not scheduled until ingest returns.
#     Time-dilated: the ingest blocks on a controllable event; we assert the
#     barrier coroutine has NOT completed (and thus nothing after it ran)
#     until the event is released. No real sleeps.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barrier_blocks_scheduling_until_ingest_returns(tmp_path, monkeypatch):
    import threading

    gate = threading.Event()
    oracle = _BarrierOracle(ingest_gate=gate)
    payload = _write_payload(tmp_path, ["a.py", "b.py", "c.py"])
    monkeypatch.setenv("JARVIS_ORACLE_PREWARM_PAYLOAD_PATH", str(payload))
    host = _Host(oracle)

    # Stand-in for "schedule the drain/flush loops" -- it must NOT run until
    # the barrier's await returns (the barrier is awaited BEFORE scheduling).
    scheduled = {"drain": False}

    async def boot():
        await ingest_prewarm_barrier(host)
        scheduled["drain"] = True  # only reachable AFTER the barrier returns.

    task = asyncio.create_task(boot())

    # Drain the event loop WITHOUT releasing the ingest gate. The barrier is
    # parked in asyncio.to_thread waiting on the (still-closed) gate -> the
    # post-barrier scheduling line is UNREACHABLE.
    for _ in range(5):
        await asyncio.sleep(0)
    assert scheduled["drain"] is False, "drain scheduled before ingest returned"
    assert oracle._file_index == {}, "Oracle warmed before ingest released"
    assert not task.done()

    # Release the ingest -> the barrier completes -> THEN scheduling proceeds.
    gate.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert scheduled["drain"] is True
    assert oracle._ingest_calls == 1
    # The barrier warmed the SHARED Oracle handle for the chaos targets.
    assert set(oracle._file_index) == {
        str(tmp_path / "a.py"),
        str(tmp_path / "b.py"),
        str(tmp_path / "c.py"),
    }


# ---------------------------------------------------------------------------
# (b) FIRST-TICK BUNDLE (the v5/v6 fix) -- post-barrier, an IMMEDIATE
#     _flush_aged_ops tick finds DISJOINT (Oracle already warm), so 3 disjoint
#     ops would BUNDLE (allowed=true n=3) and NONE age out to legacy.
#     Time-dilated: ops are offered already-aged (offered_at far in the past)
#     so the FIRST tick is the age-out moment -- and yet they are not flushed,
#     because the warm Oracle proves them disjoint -> they bundle/drain.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_barrier_first_tick_bundles_none_age_out(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.collision_matrix import (
        build_collision_matrix,
        partition_parallel_safe,
        CollisionVerdict,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        WorkUnitSpec,
    )

    oracle = _BarrierOracle()  # no gate -> ingest returns immediately.
    payload = _write_payload(tmp_path, ["a.py", "b.py", "c.py"])
    monkeypatch.setenv("JARVIS_ORACLE_PREWARM_PAYLOAD_PATH", str(payload))
    host = _Host(oracle)

    # COLD before the barrier: the partition would COLLIDE on every pair.
    assert oracle._file_index == {}

    # THE BARRIER: blocking boot ingest BEFORE any drain/flush tick.
    warmed = await ingest_prewarm_barrier(host)
    assert warmed == 3
    fa, fb, fc = (str(tmp_path / n) for n in ("a.py", "b.py", "c.py"))
    assert set(oracle._file_index) == {fa, fb, fc}

    # FIRST tick: the partition (what _flush_aged_ops/drain both read) sees the
    # ALREADY-warm shared Oracle -> DISJOINT, not "no disjoint sibling".
    units = [
        WorkUnitSpec(unit_id="u1", repo="jarvis", goal="g", target_files=(fa,)),
        WorkUnitSpec(unit_id="u2", repo="jarvis", goal="g", target_files=(fb,)),
        WorkUnitSpec(unit_id="u3", repo="jarvis", goal="g", target_files=(fc,)),
    ]
    matrix = build_collision_matrix(units, oracle=oracle)
    assert matrix.verdict("u1", "u2") is CollisionVerdict.DISJOINT
    assert matrix.verdict("u1", "u3") is CollisionVerdict.DISJOINT
    assert matrix.verdict("u2", "u3") is CollisionVerdict.DISJOINT

    parallel, sequential = partition_parallel_safe(units, oracle=oracle, matrix=matrix)
    # allowed=true n=3: ONE fan-out group of all 3 -> NONE forced serial / aged.
    assert len(parallel) == 1 and len(parallel[0]) == 3
    assert sequential == []
    # No runtime JIT was needed -- the barrier did the warming up front.
    assert oracle._jit_calls == 0


# ---------------------------------------------------------------------------
# (c) OFF byte-identical -- master flag off -> NO ingest, NO warm.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_flag_no_ingest_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", "false")
    oracle = _BarrierOracle()
    payload = _write_payload(tmp_path, ["a.py", "b.py"])
    monkeypatch.setenv("JARVIS_ORACLE_PREWARM_PAYLOAD_PATH", str(payload))
    host = _Host(oracle)

    warmed = await ingest_prewarm_barrier(host)
    assert warmed == 0
    assert oracle._ingest_calls == 0  # payload never even read (OFF).
    assert oracle._file_index == {}


# ---------------------------------------------------------------------------
# (d) MISSING payload -> fail-soft boot proceeds + runtime JIT still available.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_payload_failsoft_boot_then_jit_fallback(tmp_path, monkeypatch):
    oracle = _BarrierOracle()
    monkeypatch.setenv(
        "JARVIS_ORACLE_PREWARM_PAYLOAD_PATH", str(tmp_path / "does_not_exist.json"),
    )
    host = _Host(oracle)

    warmed = await ingest_prewarm_barrier(host)
    assert warmed == 0  # missing payload -> graceful no-op (boot proceeds).
    assert oracle._file_index == {}

    # The runtime JIT (ensure_file_indexed) is STILL reachable as the cold-miss
    # fallback for files the barrier did not pre-warm.
    ok = await oracle.ensure_file_indexed("z.py")
    assert ok is True
    assert "z.py" in oracle._file_index
    assert oracle._jit_calls == 1


# ---------------------------------------------------------------------------
# (d') SHA256 MISMATCH -> entire payload discarded -> fail-soft + JIT fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sha_mismatch_discards_payload_failsoft(tmp_path, monkeypatch):
    oracle = _BarrierOracle()
    payload = _write_payload(tmp_path, ["a.py", "b.py"])
    # Mutate a live target AFTER the payload was hashed -> sha mismatch.
    (tmp_path / "a.py").write_text("# tampered\nY = 2\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_ORACLE_PREWARM_PAYLOAD_PATH", str(payload))
    host = _Host(oracle)

    warmed = await ingest_prewarm_barrier(host)
    assert warmed == 0  # mismatch -> discard entire payload (boot proceeds).
    assert oracle._file_index == {}
    # JIT fallback intact.
    assert await oracle.ensure_file_indexed("a.py") is True


# ---------------------------------------------------------------------------
# (e) REUSE -- no oracle / no ingest method -> graceful 0 (no new ingester).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_oracle_handle_graceful(monkeypatch):
    host = _Host(None)
    assert await ingest_prewarm_barrier(host) == 0


def test_payload_path_default_and_override(monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_PREWARM_PAYLOAD_PATH", raising=False)
    assert oracle_prewarm_payload_path().endswith("oracle_prewarm.json")
    monkeypatch.setenv("JARVIS_ORACLE_PREWARM_PAYLOAD_PATH", "/custom/p.json")
    assert oracle_prewarm_payload_path() == "/custom/p.json"
