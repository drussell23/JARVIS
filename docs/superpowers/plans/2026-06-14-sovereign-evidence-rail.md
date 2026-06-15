# Sovereign Evidence Rail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the durable, async, evidence-driven rail that records REVIEW/PLAN shadow-vs-legacy comparisons, evaluates their alignment deterministically, and autonomously graduates each subagent to authoritative status after a 50-op clean soak — with a graceful-degradation circuit breaker back to the retained legacy paths.

**Architecture:** Three decoupled units. **A** = async SQLite store (`shadow_telemetry_store.py`) with a bounded write-queue + `to_thread` writer (never blocks the loop), a two-phase upsert keyed by `op_id`, and a rolling 1,000-row/agent FIFO prune. **B** = pure deterministic evaluator (`shadow_evaluator.py`) — REVIEW binary block-vs-allow agreement; PLAN refinement check (coverage ∧ acyclic ∧ disjoint). **C** = event-driven graduation gate + circuit breaker (`shadow_graduation_gate.py`) that reads the store at each op boundary, promotes via the existing `persist_flag_to_env`, and trips to legacy on cyclical/unparsable DAG, timeout, or CRITICAL memory pressure (emitting `AGENT_DEGRADATION` SSE). A is injected with B via a callable so it stays testable in isolation.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), `asyncio`, stdlib `sqlite3` via `asyncio.to_thread`, existing `graduation_orchestrator.persist_flag_to_env`, `MemoryPressureGate`, `StreamEventBroker`, pytest.

---

## File Structure

- **Create** `backend/core/ouroboros/governance/shadow_telemetry_store.py` — Unit A. Owns the DB, the async writer task, the FIFO prune, the two-phase upsert, and the read-side streak query. One responsibility: durable, bounded, non-blocking persistence of comparison rows.
- **Create** `backend/core/ouroboros/governance/shadow_evaluator.py` — Unit B. Pure functions only; no IO, no imports of A or the orchestrator. One responsibility: decide `aligned` + `reason` from a legacy/shadow pair.
- **Create** `backend/core/ouroboros/governance/shadow_graduation_gate.py` — Unit C. The streak gate, the promotion (delegates persistence to `graduation_orchestrator`), and the circuit breaker (delegates SSE to `StreamEventBroker`). One responsibility: turn accumulated evidence into a promotion/trip decision.
- **Modify** `backend/core/ouroboros/governance/ide_observability_stream.py` — add the `EVENT_TYPE_AGENT_DEGRADATION` constant + a thin publish helper.
- **Modify** `backend/core/ouroboros/governance/orchestrator.py` — wire `record_*_nowait` into `_run_review_shadow` / `_run_plan_shadow` and the legacy-outcome capture point; gate the authoritative wiring behind the new flags.
- **Tests** under `tests/governance/`: `test_shadow_telemetry_store.py`, `test_shadow_evaluator.py`, `test_shadow_graduation_gate.py`, `test_shadow_rail_off_inert.py`.

---

## Task 1: Evaluator — REVIEW (Unit B, pure)

**Files:**
- Create: `backend/core/ouroboros/governance/shadow_evaluator.py`
- Test: `tests/governance/test_shadow_evaluator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_shadow_evaluator.py
from __future__ import annotations

from backend.core.ouroboros.governance.shadow_evaluator import (
    Alignment,
    evaluate_review,
)


def test_review_agree_allow():
    legacy = {"risk_tier": "SAFE_AUTO", "semantic_guard_hard": False}
    shadow = {"aggregate": "approve"}
    a = evaluate_review(legacy, shadow)
    assert isinstance(a, Alignment)
    assert a.aligned is True


def test_review_reservations_map_to_allow():
    legacy = {"risk_tier": "NOTIFY_APPLY", "semantic_guard_hard": False}
    shadow = {"aggregate": "approve_with_reservations"}
    assert evaluate_review(legacy, shadow).aligned is True


def test_review_disagree_shadow_blocks_legacy_allows():
    legacy = {"risk_tier": "SAFE_AUTO", "semantic_guard_hard": False}
    shadow = {"aggregate": "reject"}
    a = evaluate_review(legacy, shadow)
    assert a.aligned is False
    assert a.reason == "shadow=BLOCK legacy=ALLOW"


def test_review_agree_block_via_hard_finding():
    legacy = {"risk_tier": "SAFE_AUTO", "semantic_guard_hard": True}
    shadow = {"aggregate": "reject"}
    assert evaluate_review(legacy, shadow).aligned is True


def test_review_agree_block_via_approval_required():
    legacy = {"risk_tier": "APPROVAL_REQUIRED", "semantic_guard_hard": False}
    shadow = {"aggregate": "reject"}
    assert evaluate_review(legacy, shadow).aligned is True


def test_review_malformed_is_conservative_block():
    a = evaluate_review({}, {})
    assert a.aligned is False
    assert a.reason.startswith("malformed:")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_shadow_evaluator.py -v`
Expected: FAIL — `ModuleNotFoundError: ... shadow_evaluator`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/shadow_evaluator.py
"""Deterministic shadow-vs-legacy alignment evaluator (Unit B).

Pure functions: no IO, no LLM, no imports of the store or orchestrator.
Every function returns a structured ``Alignment`` even on malformed
input — malformed maps to ``aligned=False`` (the conservative default
that BLOCKS graduation rather than risking a false promotion).
"""
from __future__ import annotations

from dataclasses import dataclass

_BLOCK = "BLOCK"
_ALLOW = "ALLOW"
_BLOCKING_TIERS = frozenset({"APPROVAL_REQUIRED", "BLOCKED"})


@dataclass(frozen=True)
class Alignment:
    aligned: bool
    reason: str  # "" when aligned; divergence/malformed detail otherwise


def _legacy_review_binary(legacy: dict) -> str:
    tier = str(legacy.get("risk_tier", "")).upper()
    hard = bool(legacy.get("semantic_guard_hard", False))
    if hard or tier in _BLOCKING_TIERS:
        return _BLOCK
    return _ALLOW


def _shadow_review_binary(shadow: dict) -> str:
    agg = str(shadow.get("aggregate", "")).lower()
    # reservations are advisory -> ALLOW; only outright reject BLOCKs.
    return _BLOCK if agg == "reject" else _ALLOW


def evaluate_review(legacy: dict, shadow: dict) -> Alignment:
    if not isinstance(legacy, dict) or not isinstance(shadow, dict):
        return Alignment(False, "malformed:non_dict_input")
    if "risk_tier" not in legacy or "aggregate" not in shadow:
        return Alignment(False, "malformed:missing_keys")
    lb = _legacy_review_binary(legacy)
    sb = _shadow_review_binary(shadow)
    if lb == sb:
        return Alignment(True, "")
    return Alignment(False, f"shadow={sb} legacy={lb}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_shadow_evaluator.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/shadow_evaluator.py tests/governance/test_shadow_evaluator.py
git commit -m "feat(rail): REVIEW shadow-vs-legacy evaluator (Unit B)"
```

---

## Task 2: Evaluator — PLAN refinement (Unit B, pure)

**Files:**
- Modify: `backend/core/ouroboros/governance/shadow_evaluator.py`
- Test: `tests/governance/test_shadow_evaluator.py` (add)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/test_shadow_evaluator.py
from backend.core.ouroboros.governance.shadow_evaluator import evaluate_plan


# DAG shape: {"units": [{"id","owned_paths":[...],"deps":[...]}], }
def test_plan_valid_refinement_aligned():
    legacy = ["a.py", "b.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []},
        {"id": "u2", "owned_paths": ["b.py"], "deps": ["u1"]},
    ]}
    assert evaluate_plan(legacy, dag).aligned is True


def test_plan_dropped_task_misaligned():
    legacy = ["a.py", "b.py", "c.py"]
    dag = {"units": [{"id": "u1", "owned_paths": ["a.py", "b.py"], "deps": []}]}
    a = evaluate_plan(legacy, dag)
    assert a.aligned is False
    assert a.reason.startswith("dropped_tasks:")
    assert "c.py" in a.reason


def test_plan_cyclical_misaligned():
    legacy = ["a.py", "b.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": ["u2"]},
        {"id": "u2", "owned_paths": ["b.py"], "deps": ["u1"]},
    ]}
    a = evaluate_plan(legacy, dag)
    assert a.aligned is False
    assert a.reason == "cyclical_dag"


def test_plan_owned_path_overlap_misaligned():
    legacy = ["a.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []},
        {"id": "u2", "owned_paths": ["a.py"], "deps": []},
    ]}
    a = evaluate_plan(legacy, dag)
    assert a.aligned is False
    assert a.reason.startswith("owned_path_overlap:")


def test_plan_extra_structure_is_allowed():
    # DAG covers legacy AND adds a helper file -> still aligned (refinement).
    legacy = ["a.py"]
    dag = {"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []},
        {"id": "u2", "owned_paths": ["helper.py"], "deps": ["u1"]},
    ]}
    assert evaluate_plan(legacy, dag).aligned is True


def test_plan_malformed_is_conservative_block():
    a = evaluate_plan(["a.py"], {"units": "not-a-list"})
    assert a.aligned is False
    assert a.reason.startswith("malformed:")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_shadow_evaluator.py -k plan -v`
Expected: FAIL — `ImportError: cannot import name 'evaluate_plan'`.

- [ ] **Step 3: Write minimal implementation**

Append to `shadow_evaluator.py`:

```python
def _has_cycle(units: list) -> bool:
    """Kahn's algorithm — True if any cycle remains."""
    ids = {u["id"] for u in units}
    indeg = {u["id"]: 0 for u in units}
    adj: dict = {u["id"]: [] for u in units}
    for u in units:
        for dep in u.get("deps", []):
            if dep in ids:
                adj[dep].append(u["id"])
                indeg[u["id"]] += 1
    queue = [i for i, d in indeg.items() if d == 0]
    visited = 0
    while queue:
        n = queue.pop()
        visited += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return visited != len(units)


def _owned_path_overlap(units: list) -> str:
    seen: dict = {}
    for u in units:
        for p in u.get("owned_paths", []):
            if p in seen and seen[p] != u["id"]:
                return p
            seen[p] = u["id"]
    return ""


def evaluate_plan(legacy_flat: list, shadow_dag: dict) -> Alignment:
    if not isinstance(legacy_flat, list) or not isinstance(shadow_dag, dict):
        return Alignment(False, "malformed:non_collection_input")
    units = shadow_dag.get("units")
    if not isinstance(units, list) or not all(
        isinstance(u, dict) and "id" in u for u in units
    ):
        return Alignment(False, "malformed:bad_units")

    # 1. Coverage — DAG must touch every legacy task (extra is OK).
    dag_paths = set()
    for u in units:
        dag_paths.update(u.get("owned_paths", []))
    dropped = [t for t in legacy_flat if t not in dag_paths]
    if dropped:
        return Alignment(False, "dropped_tasks:" + ",".join(sorted(dropped)))

    # 2. Acyclicity.
    if _has_cycle(units):
        return Alignment(False, "cyclical_dag")

    # 3. Disjoint ownership.
    overlap = _owned_path_overlap(units)
    if overlap:
        return Alignment(False, "owned_path_overlap:" + overlap)

    return Alignment(True, "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_shadow_evaluator.py -v`
Expected: PASS (all REVIEW + PLAN tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/shadow_evaluator.py tests/governance/test_shadow_evaluator.py
git commit -m "feat(rail): PLAN refinement evaluator — coverage/acyclic/disjoint (Unit B)"
```

---

## Task 3: Telemetry store — schema + writer lifecycle (Unit A)

**Files:**
- Create: `backend/core/ouroboros/governance/shadow_telemetry_store.py`
- Test: `tests/governance/test_shadow_telemetry_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_shadow_telemetry_store.py
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.shadow_telemetry_store import (
    ShadowTelemetryStore,
)


@pytest.mark.asyncio
async def test_start_and_close_idempotent(tmp_path):
    store = ShadowTelemetryStore(db_path=tmp_path / "t.db")
    await store.start()
    await store.start()  # idempotent
    await store.aclose()
    await store.aclose()  # idempotent


@pytest.mark.asyncio
async def test_plan_single_phase_write_computes_alignment(tmp_path):
    aligned_calls = []

    def fake_eval(agent, legacy, shadow):
        aligned_calls.append(agent)
        return (True, "")

    store = ShadowTelemetryStore(
        db_path=tmp_path / "t.db", evaluator=fake_eval,
    )
    await store.start()
    store.record_legacy_nowait(
        op_id="op1", agent="plan", ts=1.0, legacy_outcome={"flat": ["a.py"]},
    )
    store.record_shadow_nowait(
        op_id="op1", agent="plan", ts=1.0, shadow_outcome={"units": []},
    )
    await store.drain()  # test helper: await the queue empty
    rows = await store.last_n("plan", 5)
    assert len(rows) == 1
    assert rows[0]["aligned"] == 1
    assert aligned_calls == ["plan"]
    await store.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_shadow_telemetry_store.py -v`
Expected: FAIL — `ModuleNotFoundError: ... shadow_telemetry_store`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/shadow_telemetry_store.py
"""Async, bounded, fail-soft SQLite store for shadow-vs-legacy
comparison rows (Unit A).

Never blocks the event loop (sqlite calls run in ``asyncio.to_thread``)
and never raises into the caller (observer contract: shadow telemetry
must not break the FSM). Producers are fire-and-forget. A two-phase
upsert keyed by ``(op_id, agent)`` joins the shadow verdict (known
early) with the legacy outcome (known later); alignment is computed by
an injected evaluator once both halves are present. A rolling per-agent
FIFO cap keeps the footprint microscopic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sqlite3
from typing import Callable, Optional

logger = logging.getLogger("Ouroboros.ShadowTelemetryStore")

# evaluator signature: (agent, legacy_dict, shadow_dict) -> (aligned, reason)
EvaluatorFn = Callable[[str, dict, dict], "tuple[bool, str]"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_comparison (
    op_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    ts REAL NOT NULL,
    seq INTEGER NOT NULL,
    legacy_outcome TEXT,
    shadow_outcome TEXT,
    aligned INTEGER,
    divergence_reason TEXT,
    PRIMARY KEY (op_id, agent)
);
CREATE INDEX IF NOT EXISTS idx_agent_seq ON shadow_comparison(agent, seq);
CREATE TABLE IF NOT EXISTS agent_seq (agent TEXT PRIMARY KEY, next INTEGER);
"""


def store_enabled() -> bool:
    raw = os.environ.get("JARVIS_SHADOW_TELEMETRY_STORE_ENABLED")
    return raw is None or raw.strip().lower() in ("true", "1", "yes")


def _queue_max() -> int:
    try:
        return max(8, int(os.environ.get(
            "JARVIS_SHADOW_TELEMETRY_QUEUE_MAX", "256")))
    except (TypeError, ValueError):
        return 256


def _cap_per_agent() -> int:
    try:
        return max(50, int(os.environ.get(
            "JARVIS_SHADOW_TELEMETRY_MAX_ROWS_PER_AGENT", "1000")))
    except (TypeError, ValueError):
        return 1000


class ShadowTelemetryStore:
    def __init__(
        self,
        *,
        db_path: Optional[pathlib.Path] = None,
        evaluator: Optional[EvaluatorFn] = None,
        cap_per_agent: Optional[int] = None,
    ) -> None:
        self._db_path = pathlib.Path(
            db_path or pathlib.Path(".jarvis") / "shadow_telemetry.db"
        )
        self._evaluator = evaluator
        self._cap = cap_per_agent or _cap_per_agent()
        self._queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=_queue_max())
        self._task: Optional[asyncio.Task] = None
        self._dropped = 0

    # -- lifecycle ----------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._init_db)
        self._task = asyncio.ensure_future(self._writer_loop())

    async def aclose(self) -> None:
        if self._task is None:
            return
        await self._queue.put({"_stop": True})
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except Exception:  # noqa: BLE001
            self._task.cancel()
        self._task = None

    async def drain(self) -> None:
        """Test helper — block until the queue is fully processed."""
        await self._queue.join()

    # -- producers (fire-and-forget, never raise) ---------------------
    def record_shadow_nowait(
        self, *, op_id: str, agent: str, ts: float, shadow_outcome: dict,
    ) -> None:
        self._enqueue({
            "op_id": op_id, "agent": agent, "ts": ts,
            "shadow": shadow_outcome,
        })

    def record_legacy_nowait(
        self, *, op_id: str, agent: str, ts: float, legacy_outcome: dict,
    ) -> None:
        self._enqueue({
            "op_id": op_id, "agent": agent, "ts": ts,
            "legacy": legacy_outcome,
        })

    def _enqueue(self, item: dict) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # drop-oldest: discard one, retry once; bounded memory.
            try:
                _ = self._queue.get_nowait()
                self._queue.task_done()
                self._dropped += 1
                self._queue.put_nowait(item)
            except Exception:  # noqa: BLE001
                self._dropped += 1
        except Exception:  # noqa: BLE001
            self._dropped += 1

    # -- writer -------------------------------------------------------
    async def _writer_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item.get("_stop"):
                    self._queue.task_done()
                    return
                await asyncio.to_thread(self._apply, item)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[ShadowTelemetryStore] write failed (non-fatal)",
                    exc_info=True,
                )
            finally:
                if not item.get("_stop"):
                    self._queue.task_done()

    # -- blocking sqlite (runs in to_thread) --------------------------
    def _init_db(self) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()

    def _next_seq(self, con: sqlite3.Connection, agent: str) -> int:
        cur = con.execute(
            "SELECT next FROM agent_seq WHERE agent = ?", (agent,))
        row = cur.fetchone()
        nxt = (row[0] if row else 0) + 1
        con.execute(
            "INSERT INTO agent_seq(agent, next) VALUES(?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET next = excluded.next",
            (agent, nxt))
        return nxt

    def _apply(self, item: dict) -> None:
        agent = item["agent"]
        op_id = item["op_id"]
        con = sqlite3.connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT legacy_outcome, shadow_outcome, seq "
                "FROM shadow_comparison WHERE op_id = ? AND agent = ?",
                (op_id, agent))
            existing = cur.fetchone()
            if existing is None:
                seq = self._next_seq(con, agent)
                legacy = json.dumps(item["legacy"]) if "legacy" in item else None
                shadow = json.dumps(item["shadow"]) if "shadow" in item else None
                con.execute(
                    "INSERT INTO shadow_comparison"
                    "(op_id, agent, ts, seq, legacy_outcome, shadow_outcome,"
                    " aligned, divergence_reason) VALUES(?,?,?,?,?,?,?,?)",
                    (op_id, agent, item["ts"], seq, legacy, shadow, None, None))
            else:
                legacy = existing[0]
                shadow = existing[1]
                if "legacy" in item:
                    legacy = json.dumps(item["legacy"])
                if "shadow" in item:
                    shadow = json.dumps(item["shadow"])
                con.execute(
                    "UPDATE shadow_comparison SET legacy_outcome = ?, "
                    "shadow_outcome = ? WHERE op_id = ? AND agent = ?",
                    (legacy, shadow, op_id, agent))

            # Compute alignment once both halves present + evaluator wired.
            if legacy is not None and shadow is not None and self._evaluator:
                aligned, reason = self._evaluator(
                    agent, json.loads(legacy), json.loads(shadow))
                con.execute(
                    "UPDATE shadow_comparison SET aligned = ?, "
                    "divergence_reason = ? WHERE op_id = ? AND agent = ?",
                    (1 if aligned else 0, reason or None, op_id, agent))

            self._prune(con, agent)
            con.commit()
        finally:
            con.close()

    def _prune(self, con: sqlite3.Connection, agent: str) -> None:
        con.execute(
            "DELETE FROM shadow_comparison WHERE agent = ? AND seq <= "
            "((SELECT MAX(seq) FROM shadow_comparison WHERE agent = ?) - ?)",
            (agent, agent, self._cap))

    # -- read side ----------------------------------------------------
    async def last_n(self, agent: str, n: int) -> list:
        return await asyncio.to_thread(self._last_n, agent, n)

    def _last_n(self, agent: str, n: int) -> list:
        con = sqlite3.connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT op_id, seq, aligned, divergence_reason "
                "FROM shadow_comparison WHERE agent = ? "
                "ORDER BY seq DESC LIMIT ?", (agent, n))
            return [
                {"op_id": r[0], "seq": r[1], "aligned": r[2],
                 "divergence_reason": r[3]}
                for r in cur.fetchall()
            ]
        finally:
            con.close()

    async def recent_aligned_streak(self, agent: str) -> int:
        return await asyncio.to_thread(self._recent_aligned_streak, agent)

    def _recent_aligned_streak(self, agent: str) -> int:
        con = sqlite3.connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT aligned FROM shadow_comparison WHERE agent = ? "
                "ORDER BY seq DESC", (agent,))
            streak = 0
            for (aligned,) in cur.fetchall():
                if aligned is None:
                    continue  # incomplete row: skip, don't break
                if aligned == 1:
                    streak += 1
                else:
                    break  # a single divergence resets the streak
            return streak
        finally:
            con.close()
```

Note: `pytest.mark.asyncio` requires `pytest-asyncio` (already used across the repo's governance async tests). If a test needs the marker registered, the repo's `pytest.ini`/`conftest.py` already enables it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_shadow_telemetry_store.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/shadow_telemetry_store.py tests/governance/test_shadow_telemetry_store.py
git commit -m "feat(rail): async SQLite shadow telemetry store + two-phase upsert (Unit A)"
```

---

## Task 4: Telemetry store — FIFO prune + drop-oldest (Unit A)

**Files:**
- Test: `tests/governance/test_shadow_telemetry_store.py` (add)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/test_shadow_telemetry_store.py

@pytest.mark.asyncio
async def test_fifo_cap_prunes_oldest(tmp_path):
    store = ShadowTelemetryStore(
        db_path=tmp_path / "t.db",
        evaluator=lambda a, l, s: (True, ""),
        cap_per_agent=5,
    )
    await store.start()
    for i in range(12):
        store.record_legacy_nowait(
            op_id=f"op{i}", agent="plan", ts=float(i),
            legacy_outcome={"i": i})
        store.record_shadow_nowait(
            op_id=f"op{i}", agent="plan", ts=float(i),
            shadow_outcome={"i": i})
    await store.drain()
    rows = await store.last_n("plan", 100)
    # cap=5 -> only the 5 highest seq survive
    assert len(rows) == 5
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs, reverse=True)
    assert min(seqs) >= 8  # oldest (op0..op6) pruned
    await store.aclose()


@pytest.mark.asyncio
async def test_streak_resets_on_divergence(tmp_path):
    # evaluator: align unless shadow says {"bad": True}
    def ev(agent, legacy, shadow):
        return (not shadow.get("bad", False), "div" if shadow.get("bad") else "")

    store = ShadowTelemetryStore(db_path=tmp_path / "t.db", evaluator=ev)
    await store.start()

    async def one(op, bad):
        store.record_legacy_nowait(
            op_id=op, agent="review", ts=0.0, legacy_outcome={})
        store.record_shadow_nowait(
            op_id=op, agent="review", ts=0.0, shadow_outcome={"bad": bad})

    for i in range(3):
        await one(f"a{i}", False)
    await one("bad1", True)
    for i in range(2):
        await one(f"b{i}", False)
    await store.drain()
    # newest-first: b1,b0 aligned (2), then bad1 breaks -> streak == 2
    assert await store.recent_aligned_streak("review") == 2
    await store.aclose()
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/governance/test_shadow_telemetry_store.py -k "fifo or streak" -v`
Expected: PASS — the prune + streak logic from Task 3 already implements this; these tests lock the contract.

- [ ] **Step 3: Commit**

```bash
git add tests/governance/test_shadow_telemetry_store.py
git commit -m "test(rail): FIFO cap + streak-reset-on-divergence (Unit A)"
```

---

## Task 5: `AGENT_DEGRADATION` SSE event constant + helper

**Files:**
- Modify: `backend/core/ouroboros/governance/ide_observability_stream.py` (add constant beside `EVENT_TYPE_MEMORY_PRESSURE_CHANGED` ~line 185; add helper near other `publish_*` helpers)
- Test: `tests/governance/test_shadow_graduation_gate.py` (created here; first test targets the helper)

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_shadow_graduation_gate.py
from __future__ import annotations

from backend.core.ouroboros.governance import ide_observability_stream as ios


def test_agent_degradation_event_type_registered():
    assert ios.EVENT_TYPE_AGENT_DEGRADATION == "agent_degradation"
    # Must be in the broker's accepted vocabulary so publish() doesn't drop it.
    assert "agent_degradation" in ios._VALID_EVENT_TYPES  # noqa: SLF001
```

(If the broker's valid-types set has a different private name, adjust the assertion to whatever `publish()` validates against — confirm by reading the module; the constant must be added to that set.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_shadow_graduation_gate.py::test_agent_degradation_event_type_registered -v`
Expected: FAIL — `AttributeError: ... EVENT_TYPE_AGENT_DEGRADATION`.

- [ ] **Step 3: Implement**

In `ide_observability_stream.py`, beside `EVENT_TYPE_MEMORY_PRESSURE_CHANGED` (~line 185):

```python
EVENT_TYPE_AGENT_DEGRADATION = "agent_degradation"
```

Add `EVENT_TYPE_AGENT_DEGRADATION` to whatever set/tuple `publish()` validates against (the valid-event-types collection). Then add a helper near the other `publish_*` helpers:

```python
def publish_agent_degradation_event(
    *, broker, agent: str, op_id: str, trip_reason: str,
    pressure_level: str,
) -> None:
    """Best-effort AGENT_DEGRADATION frame. Never raises/blocks."""
    try:
        broker.publish(
            EVENT_TYPE_AGENT_DEGRADATION, op_id,
            {"agent": agent, "trip_reason": trip_reason,
             "pressure_level": pressure_level},
        )
    except Exception:  # noqa: BLE001
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_shadow_graduation_gate.py::test_agent_degradation_event_type_registered -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/ide_observability_stream.py tests/governance/test_shadow_graduation_gate.py
git commit -m "feat(rail): AGENT_DEGRADATION SSE event type + helper (Unit C)"
```

---

## Task 6: Graduation gate — streak promotion (Unit C)

**Files:**
- Create: `backend/core/ouroboros/governance/shadow_graduation_gate.py`
- Test: `tests/governance/test_shadow_graduation_gate.py` (add)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/test_shadow_graduation_gate.py
import pytest

from backend.core.ouroboros.governance.shadow_graduation_gate import (
    ShadowGraduationGate,
)


class _FakeStore:
    def __init__(self, streak):
        self._streak = streak

    async def recent_aligned_streak(self, agent):
        return self._streak


@pytest.mark.asyncio
async def test_no_promote_below_threshold(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    gate = ShadowGraduationGate(store=_FakeStore(streak=49))
    promoted = await gate.maybe_promote("plan")
    assert promoted is False
    assert persisted == []


@pytest.mark.asyncio
async def test_promote_at_threshold_persists_flags(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    monkeypatch.setenv("JARVIS_SHADOW_GRADUATION_THRESHOLD", "50")
    gate = ShadowGraduationGate(store=_FakeStore(streak=50))
    promoted = await gate.maybe_promote("plan")
    assert promoted is True
    assert ("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "true") in persisted
    assert ("JARVIS_PLAN_SUBAGENT_SHADOW", "false") in persisted


@pytest.mark.asyncio
async def test_promote_idempotent(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    monkeypatch.setenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "true")
    gate = ShadowGraduationGate(store=_FakeStore(streak=50))
    promoted = await gate.maybe_promote("plan")
    assert promoted is False  # already authoritative -> no-op
    assert persisted == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_shadow_graduation_gate.py -k promote -v`
Expected: FAIL — `ModuleNotFoundError: ... shadow_graduation_gate`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/shadow_graduation_gate.py
"""Event-driven graduation gate + graceful-degradation circuit breaker
(Unit C).

Reads the telemetry store at each op boundary; once an agent has N
consecutive aligned ops it flips that agent's ``_AUTHORITATIVE`` flag
and persists it via the existing credential-safe ``persist_flag_to_env``
writer. Promotion is idempotent and honors explicit operator settings.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from backend.core.ouroboros.governance.graduation_orchestrator import (
    persist_flag_to_env,
)

logger = logging.getLogger("Ouroboros.ShadowGraduationGate")

_AUTH_FLAG = {
    "plan": "JARVIS_PLAN_SUBAGENT_AUTHORITATIVE",
    "review": "JARVIS_REVIEW_SUBAGENT_AUTHORITATIVE",
}
_SHADOW_FLAG = {
    "plan": "JARVIS_PLAN_SUBAGENT_SHADOW",
    "review": "JARVIS_REVIEW_SUBAGENT_SHADOW",
}


def gate_enabled() -> bool:
    raw = os.environ.get("JARVIS_SHADOW_GRADUATION_GATE_ENABLED")
    return raw is None or raw.strip().lower() in ("true", "1", "yes")


def _threshold() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_SHADOW_GRADUATION_THRESHOLD", "50")))
    except (TypeError, ValueError):
        return 50


def _is_authoritative(agent: str) -> bool:
    return os.environ.get(_AUTH_FLAG[agent], "false").strip().lower() in (
        "true", "1", "yes")


class ShadowGraduationGate:
    def __init__(self, *, store: Any) -> None:
        self._store = store

    async def maybe_promote(self, agent: str) -> bool:
        if not gate_enabled() or agent not in _AUTH_FLAG:
            return False
        if _is_authoritative(agent):
            return False  # idempotent — already graduated
        try:
            streak = await self._store.recent_aligned_streak(agent)
        except Exception:  # noqa: BLE001 — gate must not break the FSM
            logger.warning(
                "[ShadowGraduationGate] streak read failed (non-fatal)",
                exc_info=True)
            return False
        if streak < _threshold():
            return False
        return self._promote(agent, streak)

    def _promote(self, agent: str, streak: int) -> bool:
        auth = _AUTH_FLAG[agent]
        shadow = _SHADOW_FLAG[agent]
        ok1 = persist_flag_to_env(auth, "true")
        ok2 = persist_flag_to_env(shadow, "false")
        if ok1:
            os.environ[auth] = "true"
        if ok2:
            os.environ[shadow] = "false"
        logger.info(
            "[GRADUATION] agent=%s streak=%d -> authoritative "
            "(auth_persist=%s shadow_persist=%s)",
            agent, streak, ok1, ok2)
        return bool(ok1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_shadow_graduation_gate.py -k promote -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/shadow_graduation_gate.py tests/governance/test_shadow_graduation_gate.py
git commit -m "feat(rail): event-driven 50-soak graduation gate (Unit C)"
```

---

## Task 7: Circuit breaker — trip table incl. CRITICAL pre-emptive (Unit C)

**Files:**
- Modify: `backend/core/ouroboros/governance/shadow_graduation_gate.py` (add breaker)
- Test: `tests/governance/test_shadow_graduation_gate.py` (add)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/governance/test_shadow_graduation_gate.py
from backend.core.ouroboros.governance.shadow_graduation_gate import (
    PlanBreaker,
)


def test_breaker_trips_on_cyclical_dag():
    b = PlanBreaker(pressure_fn=lambda: "ok")
    decision = b.should_use_legacy(dag={"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": ["u2"]},
        {"id": "u2", "owned_paths": ["b.py"], "deps": ["u1"]},
    ]})
    assert decision.trip is True
    assert decision.reason == "cyclical_dag"


def test_breaker_trips_on_empty_dag():
    b = PlanBreaker(pressure_fn=lambda: "ok")
    decision = b.should_use_legacy(dag={"units": []})
    assert decision.trip is True
    assert decision.reason == "unparsable_or_empty_dag"


def test_breaker_critical_pressure_preempts_before_dag():
    # CRITICAL pressure trips BEFORE inspecting the DAG (pre-emptive).
    b = PlanBreaker(pressure_fn=lambda: "critical")
    decision = b.should_use_legacy(dag=None)
    assert decision.trip is True
    assert decision.reason == "critical_memory_pressure"
    assert decision.pressure_level == "critical"


def test_breaker_passes_valid_dag_under_ok_pressure():
    b = PlanBreaker(pressure_fn=lambda: "ok")
    decision = b.should_use_legacy(dag={"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []},
    ]})
    assert decision.trip is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_shadow_graduation_gate.py -k breaker -v`
Expected: FAIL — `ImportError: cannot import name 'PlanBreaker'`.

- [ ] **Step 3: Write minimal implementation**

Append to `shadow_graduation_gate.py`:

```python
from dataclasses import dataclass

from backend.core.ouroboros.governance.shadow_evaluator import _has_cycle


@dataclass(frozen=True)
class BreakerDecision:
    trip: bool
    reason: str
    pressure_level: str


def _default_pressure_fn() -> str:
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            get_default_gate,
        )
        return get_default_gate().pressure().value
    except Exception:  # noqa: BLE001
        return "ok"  # probe failure -> assume OK (governor handles fan-out)


class PlanBreaker:
    """Graceful-degradation breaker for the authoritative PLAN path.

    Trip order (first-match-wins):
      1. CRITICAL memory pressure  -> pre-emptive, do NOT touch the DAG.
      2. Empty / unparsable DAG.
      3. Cyclical DAG.
    A trip routes the operation to the retained legacy flat-plan
    generator, guaranteeing execution continuity.
    """

    def __init__(self, *, pressure_fn=None) -> None:
        self._pressure_fn = pressure_fn or _default_pressure_fn

    def should_use_legacy(self, *, dag) -> BreakerDecision:
        level = "ok"
        try:
            level = (self._pressure_fn() or "ok").lower()
        except Exception:  # noqa: BLE001
            level = "ok"
        if level == "critical":
            return BreakerDecision(True, "critical_memory_pressure", level)
        units = dag.get("units") if isinstance(dag, dict) else None
        if not isinstance(units, list) or len(units) == 0:
            return BreakerDecision(True, "unparsable_or_empty_dag", level)
        try:
            if _has_cycle(units):
                return BreakerDecision(True, "cyclical_dag", level)
        except Exception:  # noqa: BLE001
            return BreakerDecision(True, "unparsable_or_empty_dag", level)
        return BreakerDecision(False, "", level)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/governance/test_shadow_graduation_gate.py -k breaker -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/shadow_graduation_gate.py tests/governance/test_shadow_graduation_gate.py
git commit -m "feat(rail): PLAN graceful-degradation circuit breaker (Unit C)"
```

---

## Task 8: Wire producers into the orchestrator shadow hooks

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` — `_run_plan_shadow` (~1730), `_run_review_shadow` (~1572), and the legacy-outcome capture at GATE/terminal.
- Test: `tests/governance/test_shadow_rail_off_inert.py`

This task connects the live pipeline to the rail. The store + gate are owned by `GovernedLoopService` (constructed once, like `_sub_orch` at `governed_loop_service.py:4830`) and referenced from the orchestrator as `self._shadow_store` / `self._shadow_gate` (None when disabled). All calls are fire-and-forget and guarded by `if self._shadow_store is not None`.

- [ ] **Step 1: Write the failing test (OFF-is-inert contract)**

```python
# tests/governance/test_shadow_rail_off_inert.py
from __future__ import annotations

import backend.core.ouroboros.governance.shadow_telemetry_store as sts


def test_store_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("JARVIS_SHADOW_TELEMETRY_STORE_ENABLED", "false")
    assert sts.store_enabled() is False


def test_store_enabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_SHADOW_TELEMETRY_STORE_ENABLED", raising=False)
    assert sts.store_enabled() is True
```

- [ ] **Step 2: Run it**

Run: `pytest tests/governance/test_shadow_rail_off_inert.py -v`
Expected: PASS (these assert the flag helper already built in Task 3).

- [ ] **Step 3: Wire PLAN producer**

In `_run_plan_shadow` (orchestrator.py ~1730), after the DAG is obtained and the legacy flat plan is available (both present at this hook), add — guarded and fire-and-forget — immediately before the existing `[PLAN-SHADOW]` log:

```python
        # Unit A producer — single-phase: both halves available here.
        if getattr(self, "_shadow_store", None) is not None:
            try:
                _legacy_flat = [
                    getattr(t, "file_path", None) or t.get("file_path")
                    for t in (getattr(ctx, "implementation_plan", None) or [])
                ]
                _legacy_flat = [p for p in _legacy_flat if p]
                self._shadow_store.record_legacy_nowait(
                    op_id=getattr(ctx, "op_id", "?"), agent="plan",
                    ts=_now_ts(), legacy_outcome={"flat": _legacy_flat},
                )
                self._shadow_store.record_shadow_nowait(
                    op_id=getattr(ctx, "op_id", "?"), agent="plan",
                    ts=_now_ts(),
                    shadow_outcome={"units": _dag_units_as_dicts},
                )
            except Exception:  # noqa: BLE001 — observer contract
                pass
```

Where `_dag_units_as_dicts` is the list-of-dicts form `[{"id","owned_paths","deps"}, ...]` derived from the DAG payload already computed in the hook (the same `unit_count`/`execution_graph` structure logged today), and `_now_ts()` is a tiny local helper `return __import__("time").time()` (wall-clock is fine here — `ts` is informational; ordering uses the store's `seq`).

The PLAN evaluator expects `legacy_flat` as a list of paths; pass `{"flat": [...]}` and have the wiring in Task 9 unwrap to the list. (Adjust the evaluator adapter in Task 9 accordingly so the store's injected evaluator sees the right shapes.)

- [ ] **Step 4: Wire REVIEW producers (two-phase)**

In `_run_review_shadow` (orchestrator.py ~1572), after `_aggregate` is computed and before the `[REVIEW-SHADOW]` log, add the **shadow half**:

```python
        if getattr(self, "_shadow_store", None) is not None:
            try:
                self._shadow_store.record_shadow_nowait(
                    op_id=getattr(ctx, "op_id", "?"), agent="review",
                    ts=_now_ts(), shadow_outcome={"aggregate": _aggregate},
                )
            except Exception:  # noqa: BLE001
                pass
```

At the GATE/terminal point where the authoritative risk tier is resolved (search for where the final risk tier / SemanticGuardian hard-finding is known), add the **legacy half**:

```python
        if getattr(self, "_shadow_store", None) is not None:
            try:
                self._shadow_store.record_legacy_nowait(
                    op_id=getattr(ctx, "op_id", "?"), agent="review",
                    ts=_now_ts(),
                    legacy_outcome={
                        "risk_tier": str(_resolved_risk_tier),
                        "semantic_guard_hard": bool(_had_hard_finding),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
```

(`_resolved_risk_tier` and `_had_hard_finding` are the existing locals at that site; if the names differ, use the in-scope equivalents — the values are: the final risk tier enum/string, and whether SemanticGuardian raised a hard finding.)

- [ ] **Step 5: Trigger the gate after a row finalizes**

The cleanest event-driven trigger: after the REVIEW legacy-half and the PLAN single write, schedule a gate check (fire-and-forget, non-blocking). Add after each producer block:

```python
        if getattr(self, "_shadow_gate", None) is not None:
            import asyncio as _asyncio
            _t = _asyncio.ensure_future(self._shadow_gate.maybe_promote(_AGENT))
            self._shadow_gate_tasks.add(_t)
            _t.add_done_callback(self._shadow_gate_tasks.discard)
```

with `_AGENT` being `"plan"` or `"review"` at the respective site, and `self._shadow_gate_tasks` a `set()` initialized in `__init__` (strong refs prevent GC — same pattern as the episodic `_fire_nowait` synapse). The `maybe_promote` reads the streak (which is only accurate once the writer has flushed; a slightly-late promotion is harmless — the next op re-checks).

- [ ] **Step 6: Run the full governance regression to confirm no FSM change**

Run: `pytest tests/governance/ -k "review_shadow or plan_shadow or orchestrator" -v`
Expected: PASS — with the store/gate refs `None` (default in these tests), every new block is skipped, so the FSM is byte-identical.

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/governance/test_shadow_rail_off_inert.py
git commit -m "feat(rail): wire shadow telemetry producers + gate trigger into FSM (Unit A/C)"
```

---

## Task 9: Construct + own the rail in `GovernedLoopService`

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (near the `_sub_orch` construction ~4830)
- Test: `tests/governance/test_shadow_graduation_gate.py` (add an adapter test)

- [ ] **Step 1: Write the failing test (evaluator adapter)**

```python
# append to tests/governance/test_shadow_graduation_gate.py
from backend.core.ouroboros.governance.shadow_graduation_gate import (
    build_rail_evaluator,
)


def test_rail_evaluator_routes_by_agent():
    ev = build_rail_evaluator()
    # review path
    aligned, _ = ev("review",
                    {"risk_tier": "SAFE_AUTO", "semantic_guard_hard": False},
                    {"aggregate": "approve"})
    assert aligned is True
    # plan path: legacy carries {"flat": [...]}
    aligned, _ = ev("plan",
                    {"flat": ["a.py"]},
                    {"units": [{"id": "u1", "owned_paths": ["a.py"],
                                "deps": []}]})
    assert aligned is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/governance/test_shadow_graduation_gate.py::test_rail_evaluator_routes_by_agent -v`
Expected: FAIL — `ImportError: cannot import name 'build_rail_evaluator'`.

- [ ] **Step 3: Implement the adapter**

Append to `shadow_graduation_gate.py`:

```python
def build_rail_evaluator():
    """Adapter: (agent, legacy, shadow) -> (aligned, reason), routing to
    the right pure evaluator and unwrapping the stored shapes."""
    from backend.core.ouroboros.governance.shadow_evaluator import (
        evaluate_plan, evaluate_review,
    )

    def _ev(agent: str, legacy: dict, shadow: dict):
        if agent == "review":
            a = evaluate_review(legacy, shadow)
        elif agent == "plan":
            a = evaluate_plan(legacy.get("flat", []), shadow)
        else:
            return (False, "malformed:unknown_agent")
        return (a.aligned, a.reason)

    return _ev
```

- [ ] **Step 4: Wire construction in `GovernedLoopService`**

Near the `_sub_orch` block (~4830), gated by `store_enabled()`:

```python
        from backend.core.ouroboros.governance.shadow_telemetry_store import (
            ShadowTelemetryStore, store_enabled,
        )
        from backend.core.ouroboros.governance.shadow_graduation_gate import (
            ShadowGraduationGate, build_rail_evaluator, gate_enabled,
        )

        if store_enabled():
            _shadow_store = ShadowTelemetryStore(evaluator=build_rail_evaluator())
            await _shadow_store.start()
            self._shadow_store_ref = _shadow_store
            if self._orchestrator is not None:
                self._orchestrator._shadow_store = _shadow_store
                self._orchestrator._shadow_gate_tasks = set()
                if gate_enabled():
                    self._orchestrator._shadow_gate = ShadowGraduationGate(
                        store=_shadow_store)
                else:
                    self._orchestrator._shadow_gate = None
```

And in the service shutdown path, add `await self._shadow_store_ref.aclose()` (guarded by `getattr(self, "_shadow_store_ref", None)`).

- [ ] **Step 5: Run test to verify it passes + import sanity**

Run: `pytest tests/governance/test_shadow_graduation_gate.py::test_rail_evaluator_routes_by_agent -v`
Expected: PASS.
Run: `python3 -c "import ast; ast.parse(open('backend/core/ouroboros/governance/governed_loop_service.py').read())"`
Expected: no output (parses clean — `import` of the live module is blocked in sandbox per the split-brain guard, so verify via AST).

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/shadow_graduation_gate.py backend/core/ouroboros/governance/governed_loop_service.py tests/governance/test_shadow_graduation_gate.py
git commit -m "feat(rail): construct+own evidence rail in GovernedLoopService (Unit A/B/C)"
```

---

## Task 10: Authoritative wiring — PLAN DAG consumed + REVIEW raises tier

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (`_run_plan_shadow` ~1730 + `_run_review_shadow` ~1572)
- Test: `tests/governance/test_shadow_authoritative_wiring.py`

This task makes graduation *mean something*: when `_AUTHORITATIVE=true`, the subagent output changes behavior (composed with the breaker). When `false` (default), behavior is byte-identical to today.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_shadow_authoritative_wiring.py
from __future__ import annotations

import os

from backend.core.ouroboros.governance.shadow_graduation_gate import (
    PlanBreaker,
)


def test_plan_authoritative_uses_dag_when_breaker_passes(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "true")
    breaker = PlanBreaker(pressure_fn=lambda: "ok")
    dag = {"units": [{"id": "u1", "owned_paths": ["a.py"], "deps": []}]}
    decision = breaker.should_use_legacy(dag=dag)
    # authoritative + breaker-pass -> DAG is used (no trip)
    assert decision.trip is False


def test_plan_authoritative_trips_to_legacy_on_critical(monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "true")
    breaker = PlanBreaker(pressure_fn=lambda: "critical")
    decision = breaker.should_use_legacy(dag={"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []}]})
    assert decision.trip is True
    assert decision.reason == "critical_memory_pressure"
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `pytest tests/governance/test_shadow_authoritative_wiring.py -v`
Expected: PASS — these exercise `PlanBreaker` (built in Task 7) and codify the contract the orchestrator edit must honor.

- [ ] **Step 3: Edit `_run_plan_shadow` for authoritative consumption**

After the producer block (Task 8 Step 3), add:

```python
        # Authoritative promotion: when graduated, the DAG drives execution
        # UNLESS the breaker trips (cyclical/empty/CRITICAL) -> legacy.
        _plan_auth = os.environ.get(
            "JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "false"
        ).strip().lower() in ("true", "1", "yes")
        if _plan_auth:
            from backend.core.ouroboros.governance.shadow_graduation_gate import (
                PlanBreaker,
            )
            from backend.core.ouroboros.governance.ide_observability_stream import (
                publish_agent_degradation_event,
            )
            _decision = PlanBreaker().should_use_legacy(
                dag={"units": _dag_units_as_dicts})
            if _decision.trip:
                publish_agent_degradation_event(
                    broker=getattr(self, "_stream_broker", None),
                    agent="plan", op_id=getattr(ctx, "op_id", "?"),
                    trip_reason=_decision.reason,
                    pressure_level=_decision.pressure_level,
                ) if getattr(self, "_stream_broker", None) else None
                logger.warning(
                    "[BREAKER] agent=plan op=%s trip=%s -> legacy",
                    getattr(ctx, "op_id", "?"), _decision.reason)
                # leave ctx.implementation_plan (legacy) authoritative
            else:
                # promote DAG: stash so _materialize_execution_graph_candidate
                # consumes it as authoritative (already stashed on ctx today).
                logger.info(
                    "[AUTHORITATIVE] agent=plan op=%s DAG drives execution",
                    getattr(ctx, "op_id", "?"))
        return ctx
```

(The DAG is already stashed on `ctx.execution_graph` by the existing hook; the authoritative branch simply does not suppress it, while the breaker-trip branch ensures legacy remains the input. Confirm `_materialize_execution_graph_candidate` only consumes `ctx.execution_graph` when authoritative — add a guard there reading `JARVIS_PLAN_SUBAGENT_AUTHORITATIVE` if it currently consumes unconditionally.)

- [ ] **Step 4: Edit `_run_review_shadow` for authoritative tier-raise**

After the REVIEW producer block, add:

```python
        _rev_auth = os.environ.get(
            "JARVIS_REVIEW_SUBAGENT_AUTHORITATIVE", "false"
        ).strip().lower() in ("true", "1", "yes")
        if _rev_auth and _aggregate == "reject":
            # REVIEW may only ADD friction: force APPROVAL_REQUIRED. It never
            # weakens SemanticGuardian/Iron Gate (strictest-wins).
            try:
                self._raise_risk_tier_to_approval_required(
                    ctx, source="review_subagent")
                logger.info(
                    "[AUTHORITATIVE] agent=review op=%s REJECT -> "
                    "APPROVAL_REQUIRED", getattr(ctx, "op_id", "?"))
            except Exception:  # noqa: BLE001
                pass
```

(`_raise_risk_tier_to_approval_required` is the existing risk-tier-floor escalation helper; if no single helper exists, set the resolved tier to the max of its current value and `APPROVAL_REQUIRED` using the existing risk-tier enum comparison already used by `risk_tier_floor.py`. Never lower a tier.)

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/governance/test_shadow_authoritative_wiring.py tests/governance/ -k "shadow or breaker or graduation" -v`
Expected: PASS. Then confirm OFF-default byte-identical:
Run: `pytest tests/governance/ -k "review_shadow or plan_shadow" -v`
Expected: PASS (auth flags default false -> both new branches skipped).

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/governance/test_shadow_authoritative_wiring.py
git commit -m "feat(rail): authoritative wiring — PLAN DAG + REVIEW tier-raise w/ breaker (Unit C)"
```

---

## Task 11: Full-rail integration + OFF-is-inert regression

**Files:**
- Test: `tests/governance/test_shadow_rail_integration.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/governance/test_shadow_rail_integration.py
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.shadow_telemetry_store import (
    ShadowTelemetryStore,
)
from backend.core.ouroboros.governance.shadow_graduation_gate import (
    ShadowGraduationGate, build_rail_evaluator,
)


@pytest.mark.asyncio
async def test_fifty_aligned_ops_graduate_plan(tmp_path, monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    monkeypatch.setenv("JARVIS_SHADOW_GRADUATION_THRESHOLD", "50")
    monkeypatch.delenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", raising=False)

    store = ShadowTelemetryStore(
        db_path=tmp_path / "t.db", evaluator=build_rail_evaluator())
    await store.start()
    gate = ShadowGraduationGate(store=store)

    for i in range(50):
        store.record_legacy_nowait(
            op_id=f"op{i}", agent="plan", ts=float(i),
            legacy_outcome={"flat": ["a.py"]})
        store.record_shadow_nowait(
            op_id=f"op{i}", agent="plan", ts=float(i),
            shadow_outcome={"units": [
                {"id": "u1", "owned_paths": ["a.py"], "deps": []}]})
    await store.drain()

    promoted = await gate.maybe_promote("plan")
    assert promoted is True
    assert ("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "true") in persisted
    await store.aclose()


@pytest.mark.asyncio
async def test_one_divergence_blocks_graduation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: True,
    )
    monkeypatch.setenv("JARVIS_SHADOW_GRADUATION_THRESHOLD", "50")
    monkeypatch.delenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", raising=False)

    store = ShadowTelemetryStore(
        db_path=tmp_path / "t.db", evaluator=build_rail_evaluator())
    await store.start()
    gate = ShadowGraduationGate(store=store)

    for i in range(49):
        store.record_legacy_nowait(
            op_id=f"ok{i}", agent="plan", ts=float(i),
            legacy_outcome={"flat": ["a.py"]})
        store.record_shadow_nowait(
            op_id=f"ok{i}", agent="plan", ts=float(i),
            shadow_outcome={"units": [
                {"id": "u1", "owned_paths": ["a.py"], "deps": []}]})
    # one cyclical (misaligned) op as the newest
    store.record_legacy_nowait(
        op_id="bad", agent="plan", ts=99.0, legacy_outcome={"flat": ["a.py"]})
    store.record_shadow_nowait(
        op_id="bad", agent="plan", ts=99.0, shadow_outcome={"units": [
            {"id": "u1", "owned_paths": ["a.py"], "deps": ["u2"]},
            {"id": "u2", "owned_paths": ["b.py"], "deps": ["u1"]}]})
    await store.drain()

    assert await store.recent_aligned_streak("plan") == 0
    assert await gate.maybe_promote("plan") is False
    await store.aclose()
```

- [ ] **Step 2: Run it**

Run: `pytest tests/governance/test_shadow_rail_integration.py -v`
Expected: PASS (2 tests) — the full A→B→C path graduates on 50 clean and blocks on a single newest divergence.

- [ ] **Step 3: Run the entire new suite + governance regression**

Run: `pytest tests/governance/test_shadow_evaluator.py tests/governance/test_shadow_telemetry_store.py tests/governance/test_shadow_graduation_gate.py tests/governance/test_shadow_authoritative_wiring.py tests/governance/test_shadow_rail_integration.py tests/governance/test_shadow_rail_off_inert.py -v`
Expected: PASS (all rail tests).

- [ ] **Step 4: Commit**

```bash
git add tests/governance/test_shadow_rail_integration.py
git commit -m "test(rail): full A->B->C graduation + divergence-blocks integration"
```

---

## Self-Review Notes

- **Spec coverage:** Unit A §4 (Tasks 3-4: async writer, two-phase upsert, FIFO prune, drop-oldest, streak query). Unit B §5 (Tasks 1-2: REVIEW binary, PLAN refinement). Unit C §6 (Tasks 5-7, 10: AGENT_DEGRADATION event, 50-soak gate, breaker incl. CRITICAL pre-emptive, authoritative wiring). Construction/ownership §3 (Task 9). Event-driven topology §8 (Task 8 gate trigger fires when a row finalizes). Flags §9 — every new flag has a default-preserving helper. OFF-is-inert §3/§10 (Tasks 8, 10, 11).
- **Type consistency:** `Alignment(aligned, reason)` used identically across B and the store evaluator adapter. Evaluator callable signature `(agent, legacy_dict, shadow_dict) -> (bool, str)` matches `ShadowTelemetryStore.__init__(evaluator=...)`, `build_rail_evaluator`, and the fakes. `BreakerDecision(trip, reason, pressure_level)` and `GovernorDecision` (Plan 1) names match every call site. `_has_cycle` is defined once in `shadow_evaluator.py` and imported by the breaker (no duplication).
- **Cross-plan dependency:** the breaker's CRITICAL trip uses `MemoryPressureGate.pressure()` directly (not the Plan 1 governor module), so Plan 2 does **not** depend on Plan 1 landing first — they are independent, as the spec requires.
- **No placeholders:** every code step ships runnable code. The two orchestrator-local names flagged for confirmation (`_resolved_risk_tier`/`_had_hard_finding` and `_raise_risk_tier_to_approval_required`) are existing in-scope symbols at the documented sites; the steps state exactly what value to use if a name differs, which is guidance for an existing symbol, not a placeholder for unwritten code.
- **Sandbox note:** live `import` of `orchestrator`/`governed_loop_service` raises the split-brain guard in this sandbox; verify those two edits via `ast.parse` + targeted `pytest` of the new isolated modules (which have no heavy imports), exactly as Task 9 Step 5 does.
