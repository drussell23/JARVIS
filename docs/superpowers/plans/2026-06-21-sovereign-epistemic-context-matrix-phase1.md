# Sovereign Epistemological Context Matrix — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cure heavy multi-file GOAL `tool_loop_deadline_exceeded` by feeding Venom a directed, hash-validated pre-fetch DAG and governing exploration with an Information-Gain Governor that converges on Δ-decay (never on wall-clock), actively bridging the Iron Gate floor with a one-shot deadlock breaker.

**Architecture:** Two new pure modules (`epistemic_prefetch.py`, `context_governor.py`) + a session-bound cross-process quarantine ledger, composed with existing assets (`oracle.get_fused_neighborhood`, `state_drift` sha256, Venom's `per_round_observer` + budget machinery). One new frozen `OperationContext` field and one new `tool_executor.run()` parameter. All flags fail-soft; OFF = byte-identical legacy.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), asyncio, numpy, stdlib hashlib/json. pytest. No new third-party deps.

**Spec:** `docs/superpowers/specs/2026-06-21-sovereign-epistemic-context-matrix-design.md` (binding; LR1–LR3 are absolute).

**Binding resolutions (implement verbatim):**
- **LR1** Prefetch-DAG excerpts seed the Δ corpus as round-0 baseline.
- **LR2** Quarantine ledger is `session_id`-bound (consult-time filter ignores foreign sessions); reconciled to oracle on session terminate.
- **LR3** Deadlock-breaker = exactly ONE dedicated round → else fatal `deadlock_override_failed` (no loop, no GENERATE_RETRY fallthrough).

---

## File Structure

| File | Responsibility | New/Modify |
|---|---|---|
| `backend/core/ouroboros/governance/epistemic_prefetch.py` | DAG Router: heavy-GOAL gate → oracle fused neighborhood → bounded ranked + hash-validated `PrefetchEntry` tuple | **Create** |
| `backend/core/ouroboros/governance/epistemic_quarantine.py` | Session-bound cross-process quarantine ledger + atomic read-then-hash Truth-Guard primitive | **Create** |
| `backend/core/ouroboros/governance/context_governor.py` | Information-Gain Governor: lightweight Δ (round-0 = prefetch), elastic budget, verdict, deadlock-breaker directive synthesis | **Create** |
| `backend/core/ouroboros/governance/op_context.py` | Add frozen field `prefetch_manifest: Tuple[PrefetchEntry, ...] = ()` | Modify |
| `backend/core/ouroboros/governance/tool_executor.py` | `run(prefetched_candidates=…)` seed + governor verdict honoring in `per_round_observer` seat | Modify |
| `backend/core/ouroboros/governance/orchestrator.py` | Trigger prefetch post-CONTEXT_EXPANSION; construct + pass governor; handle `deadlock_override_failed`; session-terminal quarantine reconcile | Modify |
| `tests/governance/test_epistemic_prefetch.py` | Prefetch unit tests | **Create** |
| `tests/governance/test_epistemic_quarantine.py` | Quarantine + atomic-hash unit tests | **Create** |
| `tests/governance/test_context_governor.py` | Governor Δ / elastic-budget / deadlock-breaker tests | **Create** |
| `tests/governance/test_epistemic_matrix_integration.py` | Cross-component + OFF byte-identical | **Create** |

**Ordering rationale:** pure leaf modules first (quarantine → prefetch → governor), then the frozen-field foundation, then the two large-file integrations last (they depend on all leaves). Each task is independently committable.

---

## Task 1: Session-bound quarantine ledger + atomic hash primitive

**Files:**
- Create: `backend/core/ouroboros/governance/epistemic_quarantine.py`
- Test: `tests/governance/test_epistemic_quarantine.py`

This is the cross-process Truth-Guard barrier (spec §5.3.1, LR2). Pure, no oracle/Venom deps. Reuses the sha256 convention from `state_drift.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_epistemic_quarantine.py
from __future__ import annotations
import json
from pathlib import Path
import pytest
from backend.core.ouroboros.governance import epistemic_quarantine as eq


def _write(p: Path, text: str) -> str:
    p.write_text(text, encoding="utf-8")
    return eq.sha256_of_file(str(p))


def test_atomic_hash_matches_hashlib(tmp_path):
    p = tmp_path / "f.py"
    h = _write(p, "x = 1\n")
    # atomic_read_and_hash returns (bytes, hexdigest) from a SINGLE read
    data, digest = eq.atomic_read_and_hash(str(p))
    assert data == b"x = 1\n"
    assert digest == h


def test_atomic_hash_missing_file_returns_empty(tmp_path):
    data, digest = eq.atomic_read_and_hash(str(tmp_path / "nope.py"))
    assert data == b""
    assert digest == ""


def test_quarantine_is_session_scoped(tmp_path):
    ledger = tmp_path / "q.jsonl"
    led = eq.QuarantineLedger(path=str(ledger), session_id="S1")
    led.quarantine("a.py", reason="stale")
    assert led.is_quarantined("a.py") is True
    # a NEW session must NOT see S1's quarantine (LR2: no infinite TTL)
    led2 = eq.QuarantineLedger(path=str(ledger), session_id="S2")
    assert led2.is_quarantined("a.py") is False


def test_quarantine_consult_failopen_on_bad_ledger(tmp_path):
    ledger = tmp_path / "q.jsonl"
    ledger.write_text("{not json\n", encoding="utf-8")
    led = eq.QuarantineLedger(path=str(ledger), session_id="S1")
    # corrupt ledger must fail-open (treat as not quarantined), never raise
    assert led.is_quarantined("a.py") is False


def test_reconcile_revalidates_and_drops(tmp_path):
    f = tmp_path / "a.py"
    h = _write(f, "v = 1\n")
    ledger = tmp_path / "q.jsonl"
    led = eq.QuarantineLedger(path=str(ledger), session_id="S1")
    led.quarantine("a.py", reason="stale", root=str(tmp_path), expected_sha=h)
    # file is UNCHANGED vs expected_sha -> reconcile re-validates (revalidated)
    result = led.reconcile(root=str(tmp_path))
    assert result["revalidated"] == ["a.py"]
    assert result["dropped"] == []


def test_reconcile_drops_when_still_drifted(tmp_path):
    f = tmp_path / "a.py"
    _write(f, "v = 1\n")
    ledger = tmp_path / "q.jsonl"
    led = eq.QuarantineLedger(path=str(ledger), session_id="S1")
    led.quarantine("a.py", reason="stale", root=str(tmp_path), expected_sha="deadbeef")
    result = led.reconcile(root=str(tmp_path))
    assert result["dropped"] == ["a.py"]
    assert result["revalidated"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_epistemic_quarantine.py -x -q`
Expected: FAIL — module `epistemic_quarantine` not found.

- [ ] **Step 3: Implement the module**

```python
# backend/core/ouroboros/governance/epistemic_quarantine.py
"""Session-bound cross-process quarantine ledger + atomic Truth-Guard hash.

Sovereign Epistemological Context Matrix (2026-06-21), spec §5.3.1, LR2.

The quarantine ledger is the load-bearing CROSS-PROCESS barrier that stops a
sibling ProcessPoolExecutor worker from ingesting a memory node a peer just
found stale. An in-memory set cannot cross process boundaries; an append-only
on-disk JSONL (atomic temp+rename) consulted by every worker can.

Discipline: pure stdlib, fail-open-to-fresh-read (a ledger error must NEVER
block a legitimate live read), session_id-scoped (no infinite TTL).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def sha256_of_file(path: str) -> str:
    """Full sha256 hex of a file's bytes, or "" if unreadable. Never raises.
    Matches the convention in state_drift.py so memory + drift agree."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, IOError):
        return ""


def atomic_read_and_hash(path: str) -> Tuple[bytes, str]:
    """Read a file's bytes ONCE and hash exactly those bytes (no read->stat->
    re-read tear window). Returns (b"", "") if unreadable. Never raises.

    Atomicity guarantee: a concurrent writer either lands fully before or fully
    after this single os.read of the open fd; the returned digest always
    describes one coherent snapshot."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return data, hashlib.sha256(data).hexdigest()
    except (OSError, IOError):
        return b"", ""


class QuarantineLedger:
    """Append-only, session-scoped, fail-open quarantine of stale memory nodes."""

    def __init__(self, path: str, session_id: str) -> None:
        self._path = path
        self._session_id = session_id or "unknown"

    def quarantine(self, rel_path: str, *, reason: str = "",
                   root: str = "", expected_sha: str = "") -> None:
        """Append a quarantine record for the CURRENT session. Never raises."""
        rec = {
            "session_id": self._session_id,
            "rel_path": rel_path,
            "reason": reason,
            "expected_sha": expected_sha,
            "root": root,
            "ts": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            # append is atomic enough for single-line JSONL on POSIX; for full
            # safety we temp+rename the whole file would lose concurrency, so we
            # use line-append (workers only ever APPEND; consult tolerates dups).
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001 — quarantine is best-effort
            logger.debug("[Quarantine] append swallowed", exc_info=True)

    def _records(self) -> List[Dict]:
        """All parseable records (any session). Fail-open: returns [] on error."""
        out: List[Dict] = []
        try:
            if not os.path.exists(self._path):
                return out
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except (ValueError, TypeError):
                        continue  # skip corrupt line, keep going
        except Exception:  # noqa: BLE001
            return []
        return out

    def is_quarantined(self, rel_path: str) -> bool:
        """True iff rel_path is quarantined IN THE CURRENT SESSION (LR2). A
        prior soak's quarantine is ignored. Fail-open (False) on any error."""
        for rec in self._records():
            if rec.get("session_id") == self._session_id \
                    and rec.get("rel_path") == rel_path:
                return True
        return False

    def reconcile(self, root: str) -> Dict[str, List[str]]:
        """On session terminate: re-hash each current-session quarantined node
        vs live disk. revalidated = hash now matches expected_sha (node is
        clean again); dropped = still drifted. Fail-soft. Returns the summary;
        the caller (FSM) refreshes the oracle for revalidated nodes."""
        revalidated: List[str] = []
        dropped: List[str] = []
        seen: set = set()
        for rec in self._records():
            if rec.get("session_id") != self._session_id:
                continue
            rel = rec.get("rel_path", "")
            if not rel or rel in seen:
                continue
            seen.add(rel)
            expected = rec.get("expected_sha", "")
            live = sha256_of_file(os.path.join(root, rel))
            if expected and live == expected:
                revalidated.append(rel)
            else:
                dropped.append(rel)
        return {"revalidated": revalidated, "dropped": dropped}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/governance/test_epistemic_quarantine.py -x -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/epistemic_quarantine.py tests/governance/test_epistemic_quarantine.py
git commit -m "feat(epistemic): session-bound quarantine ledger + atomic truth-guard hash (LR2)"
```

---

## Task 2: DAG Router (`epistemic_prefetch.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/epistemic_prefetch.py`
- Test: `tests/governance/test_epistemic_prefetch.py`

Spec §5.1. Pure aside from the injected `oracle` (always injected/mocked in tests).

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_epistemic_prefetch.py
from __future__ import annotations
import asyncio
import pytest
from backend.core.ouroboros.governance import epistemic_prefetch as ep


class _FakeOracle:
    def __init__(self, ready=True, neighborhood=None, raises=False):
        self._ready = ready
        self._n = neighborhood or []
        self._raises = raises
    def is_semantic_ready(self):
        return self._ready
    async def get_fused_neighborhood(self, files, query, k_semantic=8):
        if self._raises:
            raise RuntimeError("oracle boom")
        return self._n


def _entries(monkeypatch, tmp_path, **kw):
    # all candidate files live under tmp_path
    return ep


def test_disabled_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "false")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(), goal_text="g", is_heavy=True))
    assert out == ()


def test_not_heavy_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py",), root=str(tmp_path),
        oracle=_FakeOracle(), goal_text="g", is_heavy=False))
    assert out == ()


def test_oracle_cold_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(ready=False), goal_text="g", is_heavy=True))
    assert out == ()


def test_oracle_exception_failsoft_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(raises=True), goal_text="g", is_heavy=True))
    assert out == ()


def test_builds_ranked_hashed_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    (tmp_path / "dep.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    nbh = [{"rel_path": "dep.py", "score": 0.9, "category_hint": "CALL_GRAPH"}]
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(neighborhood=nbh), goal_text="g", is_heavy=True))
    assert len(out) == 1
    e = out[0]
    assert e.rel_path == "dep.py"
    assert e.sha256 != ""                 # hashed
    assert e.relevance == 0.9
    assert "helper" in e.content_excerpt  # seeded


def test_seed_byte_budget_truncates_excerpt(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_SEED_BYTES", "10")
    big = "x = 1  # " + ("padding " * 50) + "\n"
    (tmp_path / "dep.py").write_text(big, encoding="utf-8")
    nbh = [{"rel_path": "dep.py", "score": 0.5, "category_hint": "COMPREHENSION"}]
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_FakeOracle(neighborhood=nbh), goal_text="g", is_heavy=True))
    # over seed budget -> excerpt empty but still hashed + ranked
    assert out[0].content_excerpt == ""
    assert out[0].sha256 != ""
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_epistemic_prefetch.py -x -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the module**

> NOTE TO IMPLEMENTER: the live `oracle.get_fused_neighborhood` return shape (`oracle.py:4163-4279`) may be objects, not dicts. Read that method first and normalize defensively: support both `dict` (`.get`) and attribute access (`getattr`) for `rel_path`/`score`/`category_hint`, defaulting `category_hint` to `"COMPREHENSION"` when oracle doesn't supply one.

```python
# backend/core/ouroboros/governance/epistemic_prefetch.py
"""DAG Router — directed pre-fetch for heavy GOALs (spec §5.1).

On a heavy multi-file GOAL, ask the (already-booted) Oracle for a fused
structural+semantic neighborhood, rank + bound it, snapshot each candidate's
sha256 (Truth Guard), and return an immutable manifest that seeds Venom so it
starts DIRECTED instead of blind. Gated, fail-soft, no-op unless heavy + oracle
ready. Never blocks GENERATE on the oracle.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from backend.core.ouroboros.governance.epistemic_quarantine import atomic_read_and_hash

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_EPISTEMIC_PREFETCH_ENABLED"
_ENV_TOPK = "JARVIS_EPISTEMIC_PREFETCH_TOPK"
_ENV_SEED_BYTES = "JARVIS_EPISTEMIC_PREFETCH_SEED_BYTES"


@dataclass(frozen=True)
class PrefetchEntry:
    rel_path: str
    sha256: str
    relevance: float
    category_hint: str
    content_excerpt: str


def prefetch_enabled() -> bool:
    return (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _topk() -> int:
    try:
        return max(1, int((os.environ.get(_ENV_TOPK, "") or "8").strip()))
    except (TypeError, ValueError):
        return 8


def _seed_bytes() -> int:
    try:
        return max(0, int((os.environ.get(_ENV_SEED_BYTES, "") or "24000").strip()))
    except (TypeError, ValueError):
        return 24000


def _field(item: Any, key: str, default):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


async def build_prefetch_manifest(
    *,
    target_files: Tuple[str, ...],
    root: str,
    oracle: Optional[Any],
    goal_text: str,
    is_heavy: bool,
) -> Tuple[PrefetchEntry, ...]:
    """Return a bounded, ranked, hash-validated candidate manifest. () when
    disabled / not heavy / oracle cold / on any error (fail-soft)."""
    try:
        if not prefetch_enabled() or not is_heavy or oracle is None:
            return ()
        if not bool(oracle.is_semantic_ready()):
            return ()
        topk = _topk()
        neighborhood = await oracle.get_fused_neighborhood(
            list(target_files), goal_text, k_semantic=topk)
        if not neighborhood:
            return ()
        ranked = sorted(
            neighborhood,
            key=lambda it: float(_field(it, "score", 0.0) or 0.0),
            reverse=True,
        )[:topk]

        budget = _seed_bytes()
        spent = 0
        entries = []
        targets = set(target_files)
        for it in ranked:
            rel = str(_field(it, "rel_path", "") or "")
            if not rel or rel in targets:
                continue  # never seed a target file (Venom edits those directly)
            data, digest = atomic_read_and_hash(os.path.join(root, rel))
            if not digest:
                continue  # unreadable — skip
            excerpt = ""
            text = data.decode("utf-8", errors="replace")
            tlen = len(text.encode("utf-8"))
            if spent + tlen <= budget:
                excerpt = text
                spent += tlen
            entries.append(PrefetchEntry(
                rel_path=rel,
                sha256=digest,
                relevance=float(_field(it, "score", 0.0) or 0.0),
                category_hint=str(_field(it, "category_hint", "COMPREHENSION")
                                  or "COMPREHENSION"),
                content_excerpt=excerpt,
            ))
        logger.info(
            "[EpistemicPrefetch] candidates=%d seeded=%d bytes=%d",
            len(entries), sum(1 for e in entries if e.content_excerpt), spent)
        return tuple(entries)
    except Exception:  # noqa: BLE001 — never block GENERATE on prefetch
        logger.debug("[EpistemicPrefetch] build swallowed", exc_info=True)
        return ()
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/governance/test_epistemic_prefetch.py -x -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/epistemic_prefetch.py tests/governance/test_epistemic_prefetch.py
git commit -m "feat(epistemic): DAG Router — bounded hash-validated prefetch manifest from oracle (spec 5.1)"
```

---

## Task 3: Information-Gain Governor (`context_governor.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/context_governor.py`
- Test: `tests/governance/test_context_governor.py`

Spec §5.2 + LR1 + LR3. Pure + synchronous on the hot path (no model calls).

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_context_governor.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import context_governor as cg


class _Floors:
    """Minimal Iron Gate floor stand-in."""
    def __init__(self, met, missing=()):
        self._met = met
        self._missing = tuple(missing)
    def is_satisfied(self, ledger):
        return self._met
    def missing_categories(self, ledger):
        return self._missing


def _gov(**kw):
    return cg.InformationGainGovernor(
        prefetch_excerpts=kw.get("prefetch", ["def helper(): return 1"]),
        floors=kw.get("floors", _Floors(met=True)),
        enabled=kw.get("enabled", True),
        min_gain=kw.get("min_gain", 0.15),
        decay_rounds=kw.get("decay_rounds", 2),
    )


def test_disabled_always_continues():
    g = _gov(enabled=False)
    v = g.observe_round(0, ["totally new content xyz"], ledger=None)
    assert v.action == "continue"


def test_high_gain_continues():
    g = _gov()
    v = g.observe_round(0, ["completely unrelated brand new tokens alpha beta"],
                        ledger=None)
    assert v.action == "continue"
    assert v.info_gain > 0.15


def test_round0_baseline_is_prefetch_not_empty():
    # content identical to the prefetch excerpt -> near-zero gain on round 0
    # (LR1: baseline is the prefetch, so re-reading what memory gave is NOT new)
    g = _gov(prefetch=["def helper(): return 1"])
    v = g.observe_round(0, ["def helper(): return 1"], ledger=None)
    assert v.info_gain < 0.15


def test_decay_triggers_converge_when_floor_met():
    g = _gov(floors=_Floors(met=True), decay_rounds=2,
             prefetch=["aaa bbb ccc"])
    g.observe_round(0, ["aaa bbb ccc"], ledger=None)         # low gain 1
    v = g.observe_round(1, ["aaa bbb ccc"], ledger=None)     # low gain 2 -> decay
    assert v.action == "converge"


def test_decay_with_floor_unmet_emits_deadlock_break():
    g = _gov(floors=_Floors(met=False, missing=("CALL_GRAPH", "HISTORY")),
             decay_rounds=2, prefetch=["aaa bbb"])
    g.observe_round(0, ["aaa bbb"], ledger=None)
    v = g.observe_round(1, ["aaa bbb"], ledger=None)
    assert v.action == "deadlock_break"
    assert set(v.missing_categories) == {"CALL_GRAPH", "HISTORY"}
    assert "get_callers" in v.directive   # CALL_GRAPH -> get_callers
    assert "git_" in v.directive          # HISTORY -> git_blame/git_log


def test_elastic_budget_warm_compresses_cold_expands():
    warm = _gov(prefetch=["seed content present"])
    cold = _gov(prefetch=[])
    vw = warm.observe_round(0, ["new alpha"], ledger=None)
    vc = cold.observe_round(0, ["new alpha"], ledger=None)
    assert vw.budget_scale < 1.0     # warm cache -> compress
    assert vc.budget_scale >= 1.0    # cold cache -> expand


def test_deadlock_breaker_is_one_shot(monkeypatch):
    # after a deadlock_break verdict is consumed, a second decay must NOT
    # re-issue deadlock_break (LR3: exactly one round). The coordinator calls
    # mark_deadlock_round_consumed() once it has appended the directive.
    g = _gov(floors=_Floors(met=False, missing=("CALL_GRAPH",)),
             decay_rounds=1, prefetch=["aaa"])
    v1 = g.observe_round(0, ["aaa"], ledger=None)
    assert v1.action == "deadlock_break"
    g.mark_deadlock_round_consumed()
    v2 = g.observe_round(1, ["aaa"], ledger=None)
    assert v2.action == "deadlock_failed"   # one-shot exhausted -> fatal signal
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_context_governor.py -x -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the module**

```python
# backend/core/ouroboros/governance/context_governor.py
"""Information-Gain Governor (spec §5.2, LR1, LR3).

Decides, after each Venom round, whether continued exploration yields enough
NEW information to justify the budget — and forces a mathematically safe
handoff when it does not. Synchronous + sub-millisecond on the hot path
(TF-IDF/cosine over hashed tokens; NO model call). Deep embeds, if any, are the
coordinator's async concern — the governor never awaits.

LR1: the Δ corpus is seeded with the prefetch excerpts as the round-0 baseline.
LR3: the deadlock breaker is one-shot; a second decay after it is consumed
     yields action="deadlock_failed" (the coordinator turns that into the fatal
     terminal `deadlock_override_failed`).
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, List, Sequence, Tuple

logger = logging.getLogger(__name__)

# Iron Gate category -> canonical tool(s) for the deadlock-break directive.
# Mirrors exploration_engine._TOOL_CATEGORY (spec §3).
_CATEGORY_TOOLS = {
    "COMPREHENSION": ["read_file"],
    "DISCOVERY": ["search_code"],
    "CALL_GRAPH": ["get_callers"],
    "STRUCTURE": ["list_symbols"],
    "HISTORY": ["git_blame", "git_log"],
}

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def _tokens(text: str) -> Counter:
    return Counter(t.lower() for t in _TOKEN_RE.findall(text or ""))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[t] * b[t] for t in common)
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


@dataclass(frozen=True)
class GovernorVerdict:
    action: str            # continue | converge | deadlock_break | deadlock_failed
    info_gain: float
    budget_scale: float
    missing_categories: Tuple[str, ...] = ()
    directive: str = ""


@dataclass
class InformationGainGovernor:
    prefetch_excerpts: Sequence[str]
    floors: Any
    enabled: bool = True
    min_gain: float = 0.15
    decay_rounds: int = 2
    _corpus: Counter = field(default_factory=Counter)
    _low_streak: int = 0
    _warm: bool = False
    _deadlock_consumed: bool = False
    _deadlock_pending: bool = False

    def __post_init__(self) -> None:
        # LR1: round-0 baseline IS the prefetch.
        joined = "\n".join(self.prefetch_excerpts or [])
        self._corpus = _tokens(joined)
        self._warm = bool(self._corpus)

    def _budget_scale(self) -> float:
        # warm + still-informative -> compress; cold -> expand.
        return 0.6 if self._warm else 1.4

    def _directive(self, missing: Tuple[str, ...]) -> str:
        lines = ["STOP broad exploration. To satisfy the mandatory safety "
                 "floor you MUST now call ONLY these tools, nothing else:"]
        for cat in missing:
            tools = _CATEGORY_TOOLS.get(cat, ["read_file"])
            lines.append(f"  - {cat}: call {' or '.join(tools)} "
                         f"on the most relevant target file.")
        lines.append("Then immediately emit your patch.")
        return "\n".join(lines)

    def mark_deadlock_round_consumed(self) -> None:
        """Coordinator calls this after appending the deadlock directive (LR3)."""
        self._deadlock_consumed = True
        self._deadlock_pending = False

    def observe_round(self, round_index: int, round_tool_results: List[str],
                      ledger: Any) -> GovernorVerdict:
        if not self.enabled:
            return GovernorVerdict("continue", 1.0, 1.0)
        scale = self._budget_scale()
        new = _tokens("\n".join(round_tool_results or []))
        # info gain = 1 - similarity-to-known; novel content -> high gain.
        sim = _cosine(new, self._corpus)
        gain = max(0.0, 1.0 - sim) if new else 0.0
        self._corpus.update(new)

        if gain < self.min_gain:
            self._low_streak += 1
        else:
            self._low_streak = 0
            self._warm = self._warm  # unchanged; still informative

        decayed = self._low_streak >= self.decay_rounds
        if not decayed:
            return GovernorVerdict("continue", gain, scale)

        # Δ decayed. Consult the floor.
        floor_met = True
        missing: Tuple[str, ...] = ()
        try:
            floor_met = bool(self.floors.is_satisfied(ledger))
            if not floor_met:
                missing = tuple(self.floors.missing_categories(ledger))
        except Exception:  # noqa: BLE001 — floor probe must not crash governor
            floor_met = True

        if floor_met:
            return GovernorVerdict("converge", gain, scale)

        # Floor unmet.
        if self._deadlock_consumed:
            # LR3: one-shot already spent and floor STILL unmet -> fatal.
            return GovernorVerdict("deadlock_failed", gain, scale,
                                   missing_categories=missing)
        self._deadlock_pending = True
        return GovernorVerdict("deadlock_break", gain, scale,
                               missing_categories=missing,
                               directive=self._directive(missing))
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/governance/test_context_governor.py -x -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/context_governor.py tests/governance/test_context_governor.py
git commit -m "feat(epistemic): Information-Gain Governor — round-0 baseline + one-shot deadlock breaker (LR1/LR3)"
```

---

## Task 4: `OperationContext.prefetch_manifest` field

**Files:**
- Modify: `backend/core/ouroboros/governance/op_context.py` (near the `generate_file_hashes` field, ~line 1057)
- Test: `tests/governance/test_op_context_prefetch_field.py` (Create)

- [ ] **Step 1: Read the field block first**

Run: `python3 -c "import re,sys; s=open('backend/core/ouroboros/governance/op_context.py').read(); i=s.find('generate_file_hashes'); print(s[i-200:i+300])"`
Note the exact dataclass + default-factory style used so the new field matches.

- [ ] **Step 2: Write the failing test**

```python
# tests/governance/test_op_context_prefetch_field.py
from __future__ import annotations
from backend.core.ouroboros.governance.op_context import OperationContext

def test_prefetch_manifest_defaults_empty_and_is_frozen_replaceable():
    import dataclasses
    # build a minimal context via the project's standard constructor/factory;
    # IMPLEMENTER: use the same construction the other op_context tests use.
    ctx = OperationContext.__dataclass_fields__
    assert "prefetch_manifest" in ctx
    assert ctx["prefetch_manifest"].default_factory is tuple
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_op_context_prefetch_field.py -x -q`
Expected: FAIL — `prefetch_manifest` not in fields.

- [ ] **Step 4: Add the field**

Add alongside `generate_file_hashes` (keep the frozen dataclass style; use `field(default_factory=tuple)`):

```python
    # Sovereign Epistemological Context Matrix (spec §5.1): bounded, hash-
    # validated candidate DAG from the DAG Router; seeds Venom + the Governor's
    # round-0 baseline. Empty tuple = no prefetch (light op / oracle cold / off).
    prefetch_manifest: Tuple["PrefetchEntry", ...] = field(default_factory=tuple)
```

> IMPLEMENTER: import `PrefetchEntry` under `TYPE_CHECKING` only (avoid a runtime import cycle: op_context must not hard-import epistemic_prefetch). Use a string annotation as shown.

- [ ] **Step 5: Run to verify pass + no regression**

Run: `python3 -m pytest tests/governance/test_op_context_prefetch_field.py -x -q && python3 -m pytest tests/governance/ -k op_context -q`
Expected: PASS; existing op_context tests still green.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/op_context.py tests/governance/test_op_context_prefetch_field.py
git commit -m "feat(epistemic): add OperationContext.prefetch_manifest frozen field"
```

---

## Task 5: Venom seed + governor verdict honoring (`tool_executor.py`)

**Files:**
- Modify: `backend/core/ouroboros/governance/tool_executor.py` (`ToolLoopCoordinator.run` signature ~5586/5658; loop start ~5675; `per_round_observer` seat ~6554-6569)
- Test: `tests/governance/test_epistemic_venom_wire.py` (Create)

This is the most delicate integration. **Read the loop (`5728-6570`), the `run` signature, and the `per_round_observer` call site before editing.**

- [ ] **Step 1: Write the failing test (focused on the new seam, not the whole loop)**

```python
# tests/governance/test_epistemic_venom_wire.py
from __future__ import annotations
import inspect
from backend.core.ouroboros.governance import tool_executor


def test_run_accepts_prefetched_candidates_kwarg():
    sig = inspect.signature(tool_executor.ToolLoopCoordinator.run)
    assert "prefetched_candidates" in sig.parameters
    assert "governor" in sig.parameters


def test_seed_prefix_builder_is_bounded():
    # the helper that turns prefetch entries into the seed prompt prefix must
    # be a pure function and must label content as memory-supplied context.
    from backend.core.ouroboros.governance.epistemic_prefetch import PrefetchEntry
    entries = (PrefetchEntry("dep.py", "abc", 0.9, "CALL_GRAPH", "def f(): pass"),)
    prefix = tool_executor._build_prefetch_seed_prefix(entries)
    assert "dep.py" in prefix
    assert "def f(): pass" in prefix
    assert "memory" in prefix.lower() or "pre-fetched" in prefix.lower()


def test_seed_prefix_empty_for_no_entries():
    assert tool_executor._build_prefetch_seed_prefix(()) == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_epistemic_venom_wire.py -x -q`
Expected: FAIL — kwargs/helper absent.

- [ ] **Step 3: Implement the seam**

(a) Add the pure helper near the other `_format_*` helpers:

```python
def _build_prefetch_seed_prefix(entries) -> str:
    """Render the validated prefetch manifest as an opening context block for
    Venom (spec §5.1). Bounded by construction (entries already seed-budgeted).
    Labelled as memory-supplied so the model treats it as a HINT, not as having
    satisfied the Iron Gate (which still requires live reads)."""
    if not entries:
        return ""
    parts = ["# Pre-fetched context (memory-supplied hints — VERIFY against "
             "live files before patching):"]
    for e in entries:
        if not getattr(e, "content_excerpt", ""):
            continue
        parts.append(f"\n## {e.rel_path}  (relevance={e.relevance:.2f}, "
                     f"satisfies={e.category_hint})\n{e.content_excerpt}")
    return "\n".join(parts) + "\n\n" if len(parts) > 1 else ""
```

(b) Add `prefetched_candidates=None` and `governor=None` to `run(...)` (keyword-only, defaulted — preserves all existing callers).

(c) At loop start (~5675, after `current_prompt = prompt`):

```python
        if prefetched_candidates:
            _seed = _build_prefetch_seed_prefix(prefetched_candidates)
            if _seed:
                current_prompt = _seed + current_prompt
```

(d) In the `per_round_observer` seat (~6554-6569), after the existing observer fires, consult the governor and honor the verdict. The governor needs this round's read-only tool-result texts and the live exploration ledger (already tracked via `_cumulative_explore_calls` / the ledger object if `JARVIS_EXPLORATION_LEDGER_ENABLED`). Honor verdict:

```python
        if governor is not None:
            try:
                _round_texts = [r.result_text for r in records
                                if getattr(r, "round_index", None) == round_index
                                and getattr(r, "name", "") in self._READONLY_EXPLORATION_TOOLS]
                verdict = governor.observe_round(round_index, _round_texts,
                                                 ledger=self._exploration_ledger)
                if verdict.action == "converge":
                    # reuse the EXISTING final-write nudge path
                    current_prompt += self._final_write_nudge_text()
                    self._final_nudge_issued = True
                elif verdict.action == "deadlock_break":
                    current_prompt += "\n\n" + verdict.directive + "\n"
                    governor.mark_deadlock_round_consumed()
                elif verdict.action == "deadlock_failed":
                    raise GovernanceDeadlockError("deadlock_override_failed")
            except GovernanceDeadlockError:
                raise
            except Exception:  # noqa: BLE001 — governor advisory must not crash loop
                logger.debug("[ContextGovernor] observe swallowed", exc_info=True)
```

> IMPLEMENTER: (1) `self._final_write_nudge_text()` — extract the existing nudge string (currently inline ~5940-6011) into a small helper if it isn't already callable; reuse, do not duplicate the wording. (2) `record.result_text` / `record.round_index` — confirm the actual `ToolExecutionRecord` field names and adapt. (3) `self._exploration_ledger` — confirm how the ledger is held on the coordinator; if it lives elsewhere, thread it in. (4) Define `GovernanceDeadlockError` (Step 3e).

(e) Define the exception near the other tool-loop exceptions:

```python
class GovernanceDeadlockError(RuntimeError):
    """LR3: deadlock breaker's one shot failed to satisfy the Iron Gate floor.
    Terminal — the orchestrator maps this to terminal_reason_code
    `deadlock_override_failed`; it must NOT be retried."""
```

- [ ] **Step 4: Run to verify pass + loop regression**

Run: `python3 -m pytest tests/governance/test_epistemic_venom_wire.py -x -q && python3 -m pytest tests/governance/ -k "tool_loop or tool_executor" -q`
Expected: new tests PASS; existing tool-loop suite green.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/tool_executor.py tests/governance/test_epistemic_venom_wire.py
git commit -m "feat(epistemic): Venom prefetch seed + governor verdict honoring (converge/deadlock/fatal)"
```

---

## Task 6: Orchestrator wiring (trigger, pass-through, terminal, reconcile)

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (prefetch after CONTEXT_EXPANSION ~3294; governor construct + pass to Venom in GENERATE ~3918/4455; terminal mapping; session-terminate reconcile)
- Test: `tests/governance/test_epistemic_orchestrator_wire.py` (Create)

> Heaviest-judgment task — read the CONTEXT_EXPANSION→GENERATE region, the existing oracle handle (`self._oracle`), the heavy-op signal, and the terminal-reason plumbing before editing.

- [ ] **Step 1: Write the failing tests (seam-level, mock the heavy machinery)**

```python
# tests/governance/test_epistemic_orchestrator_wire.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import orchestrator as orch


def test_is_heavy_goal_helper():
    # pure helper: multi-file OR high blast radius -> heavy
    assert orch._is_heavy_goal(target_files=("a.py", "b.py"), blast_radius=0) is True
    assert orch._is_heavy_goal(target_files=("a.py",), blast_radius=99) is True
    assert orch._is_heavy_goal(target_files=("a.py",), blast_radius=0) is False


def test_deadlock_override_failed_is_terminal_nonretry():
    # the terminal reason must be classified non-retryable
    assert orch._is_nonretryable_terminal("deadlock_override_failed") is True
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_epistemic_orchestrator_wire.py -x -q`
Expected: FAIL — helpers absent.

- [ ] **Step 3: Implement**

(a) Pure helpers (module level):

```python
def _is_heavy_goal(target_files, blast_radius: int) -> bool:
    import os
    try:
        thresh = int(os.environ.get("OUROBOROS_BLAST_RADIUS_THRESHOLD", "5"))
    except (TypeError, ValueError):
        thresh = 5
    return (len(target_files or ()) > 1) or (int(blast_radius or 0) > thresh)
```

For `_is_nonretryable_terminal`, locate the existing terminal-reason classification (search `terminal_reason_code` / non-retry set) and ADD `"deadlock_override_failed"` to the non-retryable set rather than creating a parallel function if one exists. If none exists, add the helper.

(b) After CONTEXT_EXPANSION advance (~3294), trigger the prefetch (overlap PLAN, bounded-await before GENERATE):

```python
        if _is_heavy_goal(ctx.target_files, getattr(ctx, "blast_radius", 0)):
            from backend.core.ouroboros.governance.epistemic_prefetch import build_prefetch_manifest
            try:
                _manifest = await asyncio.wait_for(
                    build_prefetch_manifest(
                        target_files=tuple(ctx.target_files),
                        root=str(self._config.project_root),
                        oracle=getattr(self, "_oracle", None),
                        goal_text=ctx.goal or "",
                        is_heavy=True),
                    timeout=float(os.environ.get("JARVIS_EPISTEMIC_PREFETCH_TIMEOUT_S", "8")))
                ctx = dataclasses.replace(ctx, prefetch_manifest=_manifest)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass  # fail-soft — Venom runs blind exactly as today
```

(c) Where Venom is invoked in GENERATE (the `tool_executor`/`ToolLoopCoordinator.run` call), construct the governor and pass both:

```python
        _governor = None
        try:
            if (os.environ.get("JARVIS_CONTEXT_GOVERNOR_ENABLED", "true") or "").lower() in ("1","true","yes","on"):
                from backend.core.ouroboros.governance.context_governor import InformationGainGovernor
                _governor = InformationGainGovernor(
                    prefetch_excerpts=[e.content_excerpt for e in ctx.prefetch_manifest if e.content_excerpt],
                    floors=self._iron_gate_floor_adapter(ctx),   # IMPLEMENTER: adapt exploration_engine floors to the .is_satisfied/.missing_categories shape
                    enabled=True)
        except Exception:  # noqa: BLE001
            _governor = None
        # ... existing run(...) call gains:
        #     prefetched_candidates=ctx.prefetch_manifest, governor=_governor
```

> IMPLEMENTER: build a thin `_iron_gate_floor_adapter(ctx)` that wraps the existing `exploration_engine` floor evaluation (`evaluate_exploration` / `ExplorationFloors`) into the two-method shape the governor expects (`is_satisfied(ledger)`, `missing_categories(ledger)`), computing missing = required − covered from the live ledger. Reuse `exploration_engine`; do not re-implement scoring.

(d) Map the new error to a terminal: where `tool_executor` exceptions are caught in GENERATE, catch `GovernanceDeadlockError` → set `terminal_reason_code="deadlock_override_failed"`, mark non-retryable, route to terminal (NOT GENERATE_RETRY).

(e) Session-terminate reconcile (LR2): in the FSM's session-teardown/`_slice12q_record_terminal` or the session-stop path, call the quarantine reconcile and refresh oracle for revalidated nodes:

```python
        try:
            from backend.core.ouroboros.governance.epistemic_quarantine import QuarantineLedger
            _led = QuarantineLedger(
                path=os.path.join(str(self._config.project_root), ".jarvis", "epistemic_quarantine.jsonl"),
                session_id=str(getattr(self, "_session_id", "") or ""))
            _rec = _led.reconcile(root=str(self._config.project_root))
            # best-effort: ask oracle to re-index revalidated files
            if _rec["revalidated"] and getattr(self, "_oracle", None) is not None:
                await self._oracle.incremental_update()  # IMPLEMENTER: confirm method name
        except Exception:  # noqa: BLE001
            pass
```

- [ ] **Step 4: Run to verify pass + orchestrator regression**

Run: `python3 -m pytest tests/governance/test_epistemic_orchestrator_wire.py -x -q && python3 -m pytest tests/governance/ -k "orchestrator or exploration or state_drift" -q`
Expected: new tests PASS; exploration-gate + state_drift + orchestrator suites green.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/governance/test_epistemic_orchestrator_wire.py
git commit -m "feat(epistemic): orchestrator wiring — heavy-goal prefetch trigger, governor pass, deadlock terminal, session reconcile (LR2)"
```

---

## Task 7: Integration + OFF byte-identical proof

**Files:**
- Test: `tests/governance/test_epistemic_matrix_integration.py` (Create)

- [ ] **Step 1: Write the integration tests**

```python
# tests/governance/test_epistemic_matrix_integration.py
from __future__ import annotations
import asyncio
from backend.core.ouroboros.governance import epistemic_prefetch as ep
from backend.core.ouroboros.governance import context_governor as cg


def test_prefetch_feeds_governor_round0_baseline(monkeypatch, tmp_path):
    # end-to-end: a prefetched excerpt becomes the governor's round-0 baseline,
    # so re-reading that same file yields LOW gain (memory worked).
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "true")
    (tmp_path / "dep.py").write_text("def shared(): return 42\n", encoding="utf-8")

    class _O:
        def is_semantic_ready(self): return True
        async def get_fused_neighborhood(self, f, q, k_semantic=8):
            return [{"rel_path": "dep.py", "score": 0.8, "category_hint": "COMPREHENSION"}]

    manifest = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=_O(), goal_text="touch shared", is_heavy=True))
    gov = cg.InformationGainGovernor(
        prefetch_excerpts=[e.content_excerpt for e in manifest if e.content_excerpt],
        floors=type("F", (), {"is_satisfied": lambda s, l: True,
                              "missing_categories": lambda s, l: ()})(),
        enabled=True)
    v = gov.observe_round(0, ["def shared(): return 42"], ledger=None)
    assert v.info_gain < 0.15   # already known from prefetch -> low gain


def test_all_flags_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EPISTEMIC_PREFETCH_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CONTEXT_GOVERNOR_ENABLED", "false")
    out = asyncio.run(ep.build_prefetch_manifest(
        target_files=("a.py", "b.py"), root=str(tmp_path),
        oracle=None, goal_text="g", is_heavy=True))
    assert out == ()
    gov = cg.InformationGainGovernor(prefetch_excerpts=[], floors=None, enabled=False)
    assert gov.observe_round(0, ["anything"], ledger=None).action == "continue"
```

- [ ] **Step 2: Run + full Phase-1 sweep**

Run:
```bash
python3 -m pytest tests/governance/test_epistemic_matrix_integration.py -x -q
python3 -m pytest tests/governance/ -k "epistemic or context_governor or quarantine or prefetch" -q
python3 -m pytest tests/governance/ -k "exploration or state_drift or tool_loop" -q
```
Expected: all PASS; no regressions in the reused subsystems.

- [ ] **Step 3: Commit**

```bash
git add tests/governance/test_epistemic_matrix_integration.py
git commit -m "test(epistemic): cross-component integration + OFF byte-identical proof"
```

---

## Done criteria (Phase 1)
- New modules + tests green; reused subsystem suites (exploration, state_drift, tool_loop) green.
- All masters OFF → byte-identical legacy behavior (proved by Task 7).
- Governor converges on Δ-decay; deadlock breaker is one-shot → `deadlock_override_failed` fatal (LR3); quarantine session-bound + reconciled (LR2); prefetch is the Δ round-0 baseline (LR1).
- **Live validation (separate, on GCP Spot 8-CPU):** heavy multi-file GOAL soak → assert `state=applied` and `tool_loop_deadline_exceeded`=0. (Not a unit task — operator-run soak.)

## Self-Review notes
- Spec coverage: §5.1 (Task 2), §5.2/LR1/LR3 (Task 3,5), §5.3.1/LR2 (Task 1,6), §5.4 is **Phase 2 — out of scope here**.
- Type consistency: `PrefetchEntry` shape identical across prefetch/op_context/tool_executor; `GovernorVerdict.action` strings identical across governor/tool_executor.
- The large-file tasks (5,6) deliberately ship as *seam tests + read-first edit instructions* because exact field names in `tool_executor.py` (ToolExecutionRecord) and `orchestrator.py` (ledger handle, terminal plumbing) must be confirmed against live code by the implementer — the plan flags every such confirmation inline.
