# Slice 5 — SENSE Kill-Chain Elimination (F1–F6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Structurally eliminate the five-link SENSE event-delivery kill chain from the Run #15 autopsy (`docs/superpowers/specs/2026-07-10-run15-autopsy.md`) so a chaos-red test deterministically becomes a mutation-class intake signal before Run #16.

**Architecture:** Fix each link at its own layer: the FileWatchGuard filter gains deep-glob (path-segment) ignore semantics fed from env config (L2 fuel); the TestFailureSensor debounce becomes a set-based accumulator with no eviction (L2); plugin-results consumption gains an mtime-vs-run-initiation staleness gate that can never falsely arm suppression (L3); the FSEventBridge actively self-verifies its pipeline with a sentinel touch and publishes an explicit `fs.watch.ready` bus event + log marker (L1 organism-side, F6 boot WARN); the sensor's existing 600s derate wake gains a zero-event quiet-lane reconcile bounded to git-dirty paths (L4, F6 steady-state); the isomorphic driver gates chaos injection on the WATCH ACTIVE marker and verifies sensor-side evidence after touching, with bounded retry (L1 driver-side).

**Tech Stack:** Python 3.11 (3.9-compatible), asyncio, pytest, watchdog PollingObserver (existing), TrinityEventBus (existing).

## Global Constraints

- `from __future__ import annotations` in every touched file (already present — do not remove).
- Python 3.9 floor: `asyncio.wait_for`, never `asyncio.timeout`.
- Async-first: no blocking calls on the event loop; subprocesses via `asyncio.create_subprocess_exec`.
- Every tunable reads an env var with a sensible default; new flags default **ON** (these are bug fixes) with legacy escape hatches.
- **Mandate 1 (F1):** no naive one-shot `time.sleep()` readiness waits in the driver; readiness is an explicit bridge-published event (`fs.watch.ready` on the bus) whose log marker the driver awaits asynchronously.
- **Mandate 2 (F2/F3):** debounce = dynamic set-based accumulator (no cancel-eviction); ignore globs loaded from env/config with deep (path-segment) matching, not a hardcoded basename patch.
- **Mandate 3 (F5/F6):** reuse `poll_once`/`_resolve_scoped_targets`/the existing `_poll_loop` sleep and the existing bridge callbacks — NO new threads or timer loops.
- **Mandate 4 (F4):** results file must be validated by `st_mtime >= max(last pytest spawn walltime, sensor boot walltime)`; absent/stale/unparseable results NEVER bump `_last_plugin_ts`.
- Preserve T3's anti-storm property: nothing in this slice may reintroduce a whole-suite sweep on a cadence (quiet-lane reconcile is git-dirty-scoped and fires only on a provably silent lane).
- No silent caps: any bounding (path caps, retries) logs what was dropped.
- Branch: `feat/slice5-sense-kill-chain` off local main (`4b630038d1`). Commit per task, conventional commits, Co-Authored-By line per repo convention.

## File Map

- `backend/core/resilience/file_watch_guard.py` — T1: `_should_process` deep-glob semantics (`:1793`).
- `backend/core/ouroboros/governance/intake/fs_event_bridge.py` — T1: env-config ignore globs; T4: sentinel self-verification + `fs.watch.ready` + WATCH ACTIVE/NOT CONFIRMED markers.
- `backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py` — T2: set-accumulator debounce (`_on_fs_event` ~`:840`, `_debounced_pytest_run` ~`:930`, `__init__` `:470`); T3: staleness gate (`_on_test_results_changed` ~`:862`, `_last_plugin_ts` `:787`/`:885`); T5: quiet-lane reconcile (`_poll_loop` `:1166`).
- `backend/core/ouroboros/governance/intent/test_watcher.py` — T3: `last_pytest_spawn_walltime` (`__init__` `:137`, `run_pytest` `:197`).
- `scripts/isomorphic_a1_local.py` — T6: marker-await generalization (`_await_soak_boot` `:1132`), WATCH ACTIVE gate, post-touch evidence verification (`_touch_chaos_files` `:1079`).
- Tests: `tests/governance/test_slice5_deep_ignore_globs.py`, `tests/governance/test_slice5_coalescing_debounce.py`, `tests/governance/test_slice5_results_staleness_gate.py`, `tests/governance/test_slice5_watch_sentinel.py`, `tests/governance/test_slice5_quiet_lane_reconcile.py`, `tests/scripts/test_slice5_driver_watch_gate.py`.

---

### Task 1: F3 — Deep-glob ignore semantics (guard) + env-config globs (bridge)

**Files:**
- Modify: `backend/core/resilience/file_watch_guard.py:1793-1815` (`_should_process`)
- Modify: `backend/core/ouroboros/governance/intake/fs_event_bridge.py:20,56-67` (config build)
- Test: `tests/governance/test_slice5_deep_ignore_globs.py`

**Interfaces:**
- Consumes: `FileWatchConfig.ignore_patterns: List[str]`, `FileEvent(event_type, path, checksum)`.
- Produces: `_should_process` treating any ignore pattern containing `/` as a full-path fnmatch (fnmatch `*` crosses separators = deep glob); `fs_event_bridge._ignore_globs_from_env() -> List[str]` reading `JARVIS_FS_BRIDGE_IGNORE_GLOBS`.

**Background for implementer:** `_should_process` currently fnmatches **basename only**, so directory-shaped ignore entries (`.worktrees/*`, `.git/*`, `__pycache__/*`) match nothing — structurally inert (Run #15 L2 fuel: ~300 `.worktrees/**.py` events reached sensors). The Slice 12J hard-coalesce explicitly relies on ignore_patterns dropping re-included subtrees; this task makes that assumption true. Comments at `:264`, `:929`, `:1514` describe the basename-only behavior — update the ones adjacent to code you touch.

- [ ] **Step 1: Write failing tests**

```python
# tests/governance/test_slice5_deep_ignore_globs.py
"""Slice 5 T1 — deep-glob ignore semantics (Run #15 autopsy L2 fuel, F3)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.resilience.file_watch_guard import (
    FileEvent,
    FileEventType,
    FileWatchConfig,
    FileWatchGuard,
)


def _guard(tmp_path: Path, ignore: list) -> FileWatchGuard:
    cfg = FileWatchConfig(patterns=["*.py", "*.json"], ignore_patterns=ignore)
    return FileWatchGuard(watch_dir=tmp_path, on_event=lambda e: None, config=cfg)


def _ev(p: Path) -> FileEvent:
    return FileEvent(event_type=FileEventType.MODIFIED, path=p, checksum="x")


class TestDeepGlobIgnore:
    def test_slash_pattern_drops_nested_worktree_file(self, tmp_path):
        g = _guard(tmp_path, ["*/.worktrees/*"])
        victim = tmp_path / ".worktrees" / "unit-abc" / "tests" / "test_x.py"
        assert g._should_process(_ev(victim)) is False

    def test_slash_pattern_drops_deeply_nested(self, tmp_path):
        g = _guard(tmp_path, ["*/__pycache__/*"])
        victim = tmp_path / "a" / "b" / "__pycache__" / "c" / "d.py"
        assert g._should_process(_ev(victim)) is False

    def test_source_file_still_processed(self, tmp_path):
        g = _guard(tmp_path, ["*/.worktrees/*", "*/__pycache__/*"])
        keeper = tmp_path / "backend" / "core" / "leaf_predicates.py"
        assert g._should_process(_ev(keeper)) is True

    def test_basename_patterns_unchanged(self, tmp_path):
        g = _guard(tmp_path, ["*.tmp"])
        assert g._should_process(_ev(tmp_path / "x" / "y.tmp")) is False
        assert g._should_process(_ev(tmp_path / "x" / "y.py")) is True


class TestBridgeEnvGlobs:
    def test_default_globs_include_worktrees_deep(self, monkeypatch):
        monkeypatch.delenv("JARVIS_FS_BRIDGE_IGNORE_GLOBS", raising=False)
        from backend.core.ouroboros.governance.intake import fs_event_bridge as m
        globs = m._ignore_globs_from_env()
        assert "*/.worktrees/*" in globs and "*/__pycache__/*" in globs

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FS_BRIDGE_IGNORE_GLOBS", "*/foo/*, */bar/*")
        from backend.core.ouroboros.governance.intake import fs_event_bridge as m
        assert m._ignore_globs_from_env() == ["*/foo/*", "*/bar/*"]

    def test_empty_env_means_legacy_only(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FS_BRIDGE_IGNORE_GLOBS", "")
        from backend.core.ouroboros.governance.intake import fs_event_bridge as m
        assert m._ignore_globs_from_env() == []
```

- [ ] **Step 2: Run to verify failure** — `python3 -m pytest tests/governance/test_slice5_deep_ignore_globs.py -v` — expect FAIL (`_should_process` returns True for nested victims; `_ignore_globs_from_env` undefined).

- [ ] **Step 3: Implement guard change** — in `_should_process` (file_watch_guard.py:1793), replace the ignore loop:

```python
        # Check ignore patterns.
        # Slice 5 T1 (Run #15 autopsy F3): patterns containing "/" are
        # matched against the FULL path string — fnmatch's "*" crosses
        # separators, giving deep-glob semantics ("*/.worktrees/*" drops
        # any path with a .worktrees segment at any depth). Slash-less
        # patterns keep the legacy basename-only behavior byte-identical.
        import fnmatch

        full = str(path)
        for pattern in self.config.ignore_patterns:
            if "/" in pattern:
                if fnmatch.fnmatch(full, pattern):
                    return False
            elif fnmatch.fnmatch(name, pattern):
                return False
```

- [ ] **Step 4: Implement bridge env config** — in fs_event_bridge.py add module helper below `_HEARTBEAT_EVERY_N`:

```python
_DEFAULT_IGNORE_GLOBS = (
    "*/.worktrees/*", "*/__pycache__/*", "*/.git/*", "*/.ouroboros/*",
    "*/node_modules/*", "*/venv/*", "*/.venv/*", "*/*.egg-info/*",
)


def _ignore_globs_from_env() -> List[str]:
    """Deep-glob ignore entries, env-tunable (Slice 5 T1, mandate 2).

    ``JARVIS_FS_BRIDGE_IGNORE_GLOBS`` — comma-separated full-path fnmatch
    globs. Unset -> defaults; set-but-empty -> [] (legacy basename-only).
    """
    raw = os.environ.get("JARVIS_FS_BRIDGE_IGNORE_GLOBS")
    if raw is None:
        return list(_DEFAULT_IGNORE_GLOBS)
    return [g.strip() for g in raw.split(",") if g.strip()]
```

(add `List` to the existing `typing` import) and in `start()` extend the default config's `ignore_patterns` list by `+ _ignore_globs_from_env()` (keep the existing basename entries; the now-inert legacy slash entries `.worktrees/*` etc. may remain — harmless — but append the working deep forms).

- [ ] **Step 5: Run tests green** — same command, expect 7 PASS.
- [ ] **Step 6: Adjacent regression** — `python3 -m pytest tests/governance/test_slice12a_file_watch_overflow_fix.py -q` — expect pre-existing pass set unchanged.
- [ ] **Step 7: Commit** — `git add backend/core/resilience/file_watch_guard.py backend/core/ouroboros/governance/intake/fs_event_bridge.py tests/governance/test_slice5_deep_ignore_globs.py && git commit -m "fix(sense): deep-glob ignore semantics — nested .worktrees/__pycache__ churn never reaches sensors (Slice 5 T1, Run #15 L2 fuel)"`

---

### Task 2: F2 — Set-based accumulator debounce (no eviction)

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py` (`__init__` `:470`, `_on_fs_event` `.py` branch, `_debounced_pytest_run`)
- Test: `tests/governance/test_slice5_coalescing_debounce.py`

**Interfaces:**
- Consumes: `self._resolve_scoped_targets(changed_rel_path) -> Optional[list]` (`:1059`), `self._run_scoped_with_confirmation(targets)`, `dynamic_scoping_enabled()`, `full_suite_fallback_enabled()`, `self._is_recently_hydrated(rel_path)`.
- Produces: `self._pending_changed_paths: Set[str]`; `_debounced_pytest_run(changed_rel_path: str = "")` (signature kept for back-compat — a provided path seeds the set); new helper `async def _resolve_union(self, rel_paths: Sequence[str]) -> list` (T5 reuses this — keep the exact name); module helpers `_debounce_window_s()` (env `JARVIS_TEST_FAILURE_DEBOUNCE_WINDOW_S`, default 2.0) and `_debounce_max_paths()` (env `JARVIS_TEST_FAILURE_DEBOUNCE_MAX_PATHS`, default 32).

**Background:** the current handler cancels the pending debounce task on every `.py` event — last-event-wins evicted the leaf event under the Run #15 worktree burst (L2). New semantics: fixed window — first event opens it, all unique paths accumulate, one run covers the union; paths arriving mid-run re-arm one follow-up window (no loss, bounded chaining).

- [ ] **Step 1: Write failing tests**

```python
# tests/governance/test_slice5_coalescing_debounce.py
"""Slice 5 T2 — set-accumulator debounce, no eviction (Run #15 L2, F2)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


class _StubWatcher:
    poll_interval_s = 30.0

    def __init__(self):
        self.poll_calls: List[Any] = []
        self._failure_streak: dict = {}
        self.last_pytest_spawn_walltime = 0.0

    async def poll_once(self, target_paths=None):
        self.poll_calls.append(tuple(target_paths) if target_paths else None)
        return []


def _sensor(monkeypatch, resolved_map) -> TestFailureSensor:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TEST_FAILURE_DEBOUNCE_WINDOW_S", "0.05")
    s = TestFailureSensor(repo=".", router=SimpleNamespace(), test_watcher=_StubWatcher())
    async def _resolve(rel: str) -> Optional[list]:
        return resolved_map.get(rel)
    monkeypatch.setattr(s, "_resolve_scoped_targets", _resolve)
    s._last_plugin_ts = 0.0  # suppression window disarmed
    s._running = True
    return s


def _event(rel: str):
    return SimpleNamespace(payload={"relative_path": rel, "extension": ".py", "path": rel})


class TestNoEviction:
    @pytest.mark.asyncio
    async def test_burst_does_not_evict_first_path(self, monkeypatch):
        s = _sensor(monkeypatch, {
            "backend/leaf.py": ["tests/test_leaf.py"],
            "wt/a.py": None, "wt/b.py": None,
        })
        await s._on_fs_event(_event("backend/leaf.py"))
        await s._on_fs_event(_event("wt/a.py"))    # must NOT cancel/evict
        await s._on_fs_event(_event("wt/b.py"))
        await asyncio.wait_for(s._debounce_task, timeout=2.0)
        assert any(c and "tests/test_leaf.py" in c for c in s._watcher.poll_calls), (
            "leaf's scoped targets were evicted by the burst — the Run #15 L2 class"
        )

    @pytest.mark.asyncio
    async def test_union_deduped_single_run(self, monkeypatch):
        s = _sensor(monkeypatch, {
            "a.py": ["tests/t1.py", "tests/t2.py"],
            "b.py": ["tests/t2.py", "tests/t3.py"],
        })
        await s._on_fs_event(_event("a.py"))
        await s._on_fs_event(_event("b.py"))
        await asyncio.wait_for(s._debounce_task, timeout=2.0)
        assert len(s._watcher.poll_calls) == 1
        assert sorted(s._watcher.poll_calls[0]) == ["tests/t1.py", "tests/t2.py", "tests/t3.py"]

    @pytest.mark.asyncio
    async def test_mid_run_arrivals_rearm_followup(self, monkeypatch):
        s = _sensor(monkeypatch, {"a.py": ["tests/t1.py"], "late.py": ["tests/t9.py"]})
        await s._on_fs_event(_event("a.py"))
        await asyncio.sleep(0)          # let the window task start
        s._pending_changed_paths.add("late.py")   # simulates arrival mid-run
        await asyncio.wait_for(s._debounce_task, timeout=2.0)
        for _ in range(50):             # follow-up window drains the late path
            if any(c and "tests/t9.py" in c for c in s._watcher.poll_calls):
                break
            await asyncio.sleep(0.05)
        assert any(c and "tests/t9.py" in c for c in s._watcher.poll_calls)

    @pytest.mark.asyncio
    async def test_cap_logs_and_bounds(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_TEST_FAILURE_DEBOUNCE_MAX_PATHS", "2")
        s = _sensor(monkeypatch, {f"p{i}.py": [f"tests/t{i}.py"] for i in range(4)})
        for i in range(4):
            await s._on_fs_event(_event(f"p{i}.py"))
        with caplog.at_level("WARNING"):
            await asyncio.wait_for(s._debounce_task, timeout=2.0)
        assert any("dropped" in r.message for r in caplog.records), "cap must not be silent"
```

- [ ] **Step 2: Verify failure** — `python3 -m pytest tests/governance/test_slice5_coalescing_debounce.py -v` — expect FAIL (first test: leaf targets absent because cancel-chain evicted them).

- [ ] **Step 3: Implement.** `__init__` additions after `self._hydrated_keys`:

```python
        # Slice 5 T2 (Run #15 autopsy L2, F2): set-based debounce
        # accumulator. Events never cancel the pending window — paths
        # aggregate and one scoped run covers the union.
        self._pending_changed_paths: Set[str] = set()
```

(`Set`/`Sequence` into the typing import.) Module helpers next to `_TEST_FAILURE_FALLBACK_INTERVAL_S`:

```python
def _debounce_window_s() -> float:
    return float(os.environ.get("JARVIS_TEST_FAILURE_DEBOUNCE_WINDOW_S", "2.0"))


def _debounce_max_paths() -> int:
    return int(os.environ.get("JARVIS_TEST_FAILURE_DEBOUNCE_MAX_PATHS", "32"))
```

`_on_fs_event` `.py` branch becomes:

```python
        if event.payload.get("extension") != ".py":
            self._fs_events_ignored += 1
            return
        self._fs_events_handled += 1
        self._pending_changed_paths.add(rel_path)
        if self._debounce_task is not None and not self._debounce_task.done():
            return  # window open — accumulate, NEVER cancel/evict (Slice 5 F2)
        self._debounce_task = asyncio.create_task(
            self._debounced_pytest_run(),
            name="test_failure_debounced_run",
        )
```

`_debounced_pytest_run` rewrite (keep suppression + hydration checks; docstring must state fixed-window set semantics):

```python
    async def _debounced_pytest_run(self, changed_rel_path: str = "") -> None:
        """Fixed-window set-accumulator debounce (Slice 5 F2).

        The first .py event opens a ``_debounce_window_s()`` window; every
        event during it adds to ``_pending_changed_paths``. One scoped run
        covers the UNION of resolved targets. Paths arriving mid-run land
        in the (fresh) set and re-arm one follow-up window — nothing is
        evicted (Run #15 L2: last-event-wins cancel dropped the chaos leaf
        under a worktree burst). *changed_rel_path* kept for back-compat:
        a direct caller's path seeds the set.
        """
        try:
            if changed_rel_path:
                self._pending_changed_paths.add(changed_rel_path)
            await asyncio.sleep(_debounce_window_s())
            batch = sorted(self._pending_changed_paths)
            self._pending_changed_paths.clear()
            if not batch:
                return
            if time.monotonic() - self._last_plugin_ts < 10.0:
                logger.debug(
                    "TestFailureSensor: skipping subprocess run — "
                    "plugin results consumed %.1fs ago",
                    time.monotonic() - self._last_plugin_ts,
                )
                return
            if self._watcher is None:
                return
            batch = [p for p in batch if not self._is_recently_hydrated(p)]
            if not batch:
                return
            cap = _debounce_max_paths()
            if len(batch) > cap:
                logger.warning(
                    "TestFailureSensor: debounce batch %d paths > cap %d — "
                    "resolving first %d, dropped: %s",
                    len(batch), cap, cap, batch[cap:],
                )
                batch = batch[:cap]
            if not dynamic_scoping_enabled():
                signals = await self._watcher.poll_once()
                if signals:
                    await self.handle_signals(signals)
                return
            union = await self._resolve_union(batch)
            if union:
                logger.info(
                    "TestFailureSensor: scoped %d test target(s) for %d "
                    "changed path(s): %r",
                    len(union), len(batch), batch,
                )
                await self._run_scoped_with_confirmation(union)
                return
            if not full_suite_fallback_enabled():
                logger.debug(
                    "TestFailureSensor: no scoped targets for %r and "
                    "full-suite fallback disabled — skipping run", batch,
                )
                return
            logger.info(
                "TestFailureSensor: no scoped targets for %r — "
                "JARVIS_TEST_FULL_SUITE_FALLBACK on, running full suite",
                batch,
            )
            signals = await self._watcher.poll_once()
            if signals:
                await self.handle_signals(signals)
        except asyncio.CancelledError:
            pass  # sensor stopping
        except Exception:
            logger.debug("TestFailureSensor: debounced run error", exc_info=True)
        finally:
            if self._pending_changed_paths and self._running:
                self._debounce_task = asyncio.create_task(
                    self._debounced_pytest_run(),
                    name="test_failure_debounced_run",
                )

    async def _resolve_union(self, rel_paths: Sequence[str]) -> list:
        """Resolve each path via the T3 scoped resolver; ordered dedup union."""
        union: list = []
        seen: Set[str] = set()
        for rel in rel_paths:
            targets = await self._resolve_scoped_targets(rel)
            for t in targets or ():
                if t not in seen:
                    seen.add(t)
                    union.append(t)
        return union
```

IMPORTANT: one INFO line in the union branch must keep the substring `test target(s) for` AND include the changed paths — the Slice 5 T6 driver evidence gate greps a single line for `TestFailureSensor` + the chaos filename.

- [ ] **Step 4: Tests green** — the new file, then re-target check: `python3 -m pytest tests/governance/test_dynamic_test_scoping.py -q`. Existing tests that patched the old cancel semantics or called `_debounced_pytest_run("path")` directly must still pass via the seed-the-set back-compat; if any asserts on cancel-eviction itself, re-target it to the accumulator contract (equivalent strength — document in the report which tests were re-targeted and why the old seam was asserting the L2 bug).
- [ ] **Step 5: Commit** — `git commit -m "fix(sense): set-accumulator debounce — burst events aggregate, never evict (Slice 5 T2, Run #15 L2 kill)"` with the two files.

---

### Task 3: F4 — Plugin-results staleness gate (mtime vs run initiation)

**Files:**
- Modify: `backend/core/ouroboros/governance/intent/test_watcher.py` (`__init__` `:137`, `run_pytest` `:197`)
- Modify: `backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py` (`__init__` `:470`, `_on_test_results_changed` ~`:862`, bump at `:885`)
- Test: `tests/governance/test_slice5_results_staleness_gate.py`

**Interfaces:**
- Produces: `TestWatcher.last_pytest_spawn_walltime: float` (0.0 until first spawn; `time.time()` stamped immediately before each spawn in `run_pytest`); sensor `self._boot_walltime: float = time.time()` in `__init__`; `_on_test_results_changed` gates on `os.stat(path).st_mtime >= max(watcher.last_pytest_spawn_walltime, self._boot_walltime) - 1.0` (1s FS-timestamp slack) and bumps `_last_plugin_ts` ONLY after a fresh, parseable read.

**Background:** Run #15 L3 — the sensor consumed the *deleted/stale* `.jarvis/test_results.json` (parse of a missing file → 0 failures) and still bumped `_last_plugin_ts`, arming a 10s suppression window at the exact second the leaf event arrived; proven firing live at 21:25:59. Mandate 4: staleness rejection must be structural.

- [ ] **Step 1: Write failing tests**

```python
# tests/governance/test_slice5_results_staleness_gate.py
"""Slice 5 T3 — plugin-results staleness gate (Run #15 L3, F4)."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


class _StubWatcher:
    poll_interval_s = 30.0
    last_pytest_spawn_walltime = 0.0

    def __init__(self):
        self._failure_streak: dict = {}

    def process_failures(self, failures):
        return []


def _sensor(monkeypatch) -> TestFailureSensor:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    return TestFailureSensor(repo=".", router=SimpleNamespace(), test_watcher=_StubWatcher())


def _results_event(path: str):
    return SimpleNamespace(payload={
        "relative_path": ".jarvis/test_results.json", "path": path, "extension": ".json",
    })


class TestStalenessGate:
    @pytest.mark.asyncio
    async def test_deleted_file_never_arms_suppression(self, monkeypatch, tmp_path):
        s = _sensor(monkeypatch)
        before = s._last_plugin_ts
        await s._on_test_results_changed(_results_event(str(tmp_path / "gone.json")))
        assert s._last_plugin_ts == before, "absent results file armed the L3 window"

    @pytest.mark.asyncio
    async def test_stale_mtime_ignored(self, monkeypatch, tmp_path):
        s = _sensor(monkeypatch)
        f = tmp_path / "test_results.json"
        f.write_text(json.dumps({"failures": []}))
        old = time.time() - 3600
        os.utime(f, (old, old))
        s._watcher.last_pytest_spawn_walltime = time.time()  # run initiated NOW
        before = s._last_plugin_ts
        await s._on_test_results_changed(_results_event(str(f)))
        assert s._last_plugin_ts == before, "results predating the run armed suppression"

    @pytest.mark.asyncio
    async def test_fresh_results_bump(self, monkeypatch, tmp_path):
        s = _sensor(monkeypatch)
        s._boot_walltime = time.time() - 60          # booted a minute ago
        s._watcher.last_pytest_spawn_walltime = time.time() - 30
        f = tmp_path / "test_results.json"
        f.write_text(json.dumps({"failures": []}))    # mtime = now (fresh)
        before = s._last_plugin_ts
        await s._on_test_results_changed(_results_event(str(f)))
        assert s._last_plugin_ts > before


class TestWatcherStamp:
    @pytest.mark.asyncio
    async def test_run_pytest_stamps_spawn_walltime(self, monkeypatch):
        from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
        w = TestWatcher.__new__(TestWatcher)          # attribute contract only
        assert hasattr(TestWatcher, "__init__")
        # AST-level pin: run_pytest source stamps the attribute pre-spawn.
        import inspect
        src = inspect.getsource(TestWatcher.run_pytest)
        assert "last_pytest_spawn_walltime" in src and "time.time()" in src
```

- [ ] **Step 2: Verify failure** — expect the deleted/stale tests to FAIL (current code bumps at `:885` unconditionally) and the AST pin to FAIL.

- [ ] **Step 3: Implement.** `test_watcher.py` `__init__` (`:137` body): add `self.last_pytest_spawn_walltime: float = 0.0` with a comment naming Slice 5 F4. `run_pytest` (`:197`): immediately before the canonical helper spawn call add:

```python
        # Slice 5 F4: results-file freshness floor — a test_results.json
        # older than this stamp is a leftover from a previous run and must
        # never arm the sensor's plugin-suppression window.
        self.last_pytest_spawn_walltime = time.time()
```

Sensor `__init__`: add `self._boot_walltime: float = time.time()`. `_on_test_results_changed` head (before `_parse_results_file`):

```python
        path = event.payload.get("path", "")
        floor_ts = max(
            getattr(self._watcher, "last_pytest_spawn_walltime", 0.0),
            self._boot_walltime,
        ) - 1.0  # 1s slack for coarse FS timestamps
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            logger.debug(
                "TestFailureSensor: results file absent/unreadable (%r) — "
                "ignored, suppression NOT armed (Slice 5 F4)", path,
            )
            return
        if mtime < floor_ts:
            logger.debug(
                "TestFailureSensor: STALE plugin results ignored "
                "(mtime %.1fs before run/boot floor) — suppression NOT armed",
                floor_ts - mtime,
            )
            return
```

and move the `:885` `self._last_plugin_ts = time.monotonic()` so it executes only on this fresh-parse path (it now cannot be reached for absent/stale files). Verify there is no OTHER `_last_plugin_ts` write site (grep; `:787` is the init).

- [ ] **Step 4: Tests green** + `python3 -m pytest tests/governance/test_slice5_coalescing_debounce.py tests/governance/test_dynamic_test_scoping.py -q` (T2 interplay).
- [ ] **Step 5: Commit** — `git commit -m "fix(sense): plugin-results staleness gate — absent/stale test_results.json never arms suppression (Slice 5 T3, Run #15 L3 kill)"`.

---

### Task 4: F1 organism half + F6 boot WARN — bridge sentinel self-verification

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/fs_event_bridge.py`
- Test: `tests/governance/test_slice5_watch_sentinel.py`

**Interfaces:**
- Produces: bus topic `fs.watch.ready` (`data={"elapsed_s": float, "watch_root": str}`, `persist=False`); INFO marker exactly `"[FSEventBridge] WATCH ACTIVE — pipeline verified live (sentinel observed after %.1fs)"`; WARNING marker starting `"[FSEventBridge] WATCH NOT CONFIRMED"`; `FileSystemEventBridge.watch_confirmed: bool` in `get_metrics()`. Env: `JARVIS_FS_BRIDGE_SENTINEL_ENABLED` (default true), `JARVIS_FS_BRIDGE_READY_BUDGET_S` (default 300), `JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S` (default 15). Sentinel file: `<project_root>/.jarvis/fs_watch_sentinel.json` (matched by the `*.json` include pattern; `.jarvis` is NOT in ignore globs — do not add it).
- Consumes: existing `_on_file_event` callback and `self._event_bus.publish_raw`.

**Background:** Run #15 L1 — "Watching" logs at `guard.start()` return but the PollingObserver pipeline delivers ~1 snapshot-pass late (43s start + ~45–120s pass). This task makes the bridge *prove* its own pipeline with a sentinel touch, publish an explicit readiness event (mandate 1), and WARN when unconfirmed (F6 boot half). Fail-soft: a background asyncio task — never blocks `start()` (Progressive Awakening §2). No threads, no timers — one task awaiting an `asyncio.Event` with bounded re-touches.

- [ ] **Step 1: Write failing tests**

```python
# tests/governance/test_slice5_watch_sentinel.py
"""Slice 5 T4 — FSEventBridge sentinel self-verification (Run #15 L1, F1/F6)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.intake.fs_event_bridge import (
    FileSystemEventBridge,
)


class _Bus:
    def __init__(self):
        self.published = []

    async def publish_raw(self, topic, data, persist=False):
        self.published.append((topic, data))


class _Guard:
    """Stands in for FileWatchGuard: start() succeeds; test drives events."""
    is_healthy = True

    def __init__(self, **kw):
        self.on_event = kw.get("on_event")

    async def start(self):
        return True

    async def stop(self):
        return None

    def get_metrics(self):
        return {}


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S", "0.05")
    monkeypatch.setenv("JARVIS_FS_BRIDGE_READY_BUDGET_S", "1.0")
    from backend.core.ouroboros.governance.intake import fs_event_bridge as m
    monkeypatch.setattr(m, "FileWatchGuard", _Guard, raising=False)
    b = FileSystemEventBridge(project_root=tmp_path, event_bus=_Bus())
    return b


def _sentinel_event(b: FileSystemEventBridge):
    p = b._sentinel_path
    return SimpleNamespace(
        event_type=SimpleNamespace(value="modified"), path=p,
        checksum="s", timestamp=0.0, is_directory=False,
    )


class TestSentinel:
    @pytest.mark.asyncio
    async def test_sentinel_observed_publishes_ready_and_marker(self, bridge, caplog, monkeypatch):
        # Patch the guard import inside start()
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        with caplog.at_level("INFO"):
            await bridge.start()
            await bridge._on_file_event(_sentinel_event(bridge))
            await asyncio.wait_for(bridge._verify_task, timeout=2.0)
        topics = [t for t, _ in bridge._event_bus.published]
        assert "fs.watch.ready" in topics
        assert any("WATCH ACTIVE" in r.message for r in caplog.records)
        assert bridge.get_metrics()["watch_confirmed"] is True

    @pytest.mark.asyncio
    async def test_sentinel_events_not_published_downstream(self, bridge, monkeypatch):
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        await bridge.start()
        await bridge._on_file_event(_sentinel_event(bridge))
        fs_topics = [t for t, _ in bridge._event_bus.published if t.startswith("fs.changed")]
        assert fs_topics == [], "liveness probe leaked to sensors"
        await asyncio.wait_for(bridge._verify_task, timeout=2.0)

    @pytest.mark.asyncio
    async def test_budget_exhausted_warns_not_confirmed(self, bridge, caplog, monkeypatch):
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        with caplog.at_level("WARNING"):
            await bridge.start()          # never feed the sentinel event
            await asyncio.wait_for(bridge._verify_task, timeout=5.0)
        assert any("WATCH NOT CONFIRMED" in r.message for r in caplog.records)
        assert bridge.get_metrics()["watch_confirmed"] is False

    @pytest.mark.asyncio
    async def test_master_off_no_task(self, bridge, monkeypatch):
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        monkeypatch.setenv("JARVIS_FS_BRIDGE_SENTINEL_ENABLED", "false")
        await bridge.start()
        assert bridge._verify_task is None
```

(If patching the guard class requires a different seam — e.g. the import inside `start()` resolves from the resilience module — adapt the monkeypatch target, but the four behavioral contracts are non-negotiable.)

- [ ] **Step 2: Verify failure** — attribute `_sentinel_path` missing → AttributeError/FAIL.

- [ ] **Step 3: Implement** in fs_event_bridge.py. Module helpers:

```python
_SENTINEL_BASENAME = "fs_watch_sentinel.json"


def _sentinel_enabled() -> bool:
    return os.environ.get("JARVIS_FS_BRIDGE_SENTINEL_ENABLED", "true").lower() in ("1", "true", "yes")


def _ready_budget_s() -> float:
    return float(os.environ.get("JARVIS_FS_BRIDGE_READY_BUDGET_S", "300"))


def _sentinel_retouch_s() -> float:
    return float(os.environ.get("JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S", "15"))
```

`__init__` additions:

```python
        self._sentinel_path: Path = self._project_root / ".jarvis" / _SENTINEL_BASENAME
        self._sentinel_observed: Optional[Any] = None   # asyncio.Event, armed in start()
        self._verify_task: Optional[Any] = None
        self._watch_confirmed: bool = False
```

`start()` — after the `ok` branch logs "Watching":

```python
        if ok and _sentinel_enabled():
            import asyncio

            self._sentinel_observed = asyncio.Event()
            self._verify_task = asyncio.create_task(
                self._verify_pipeline_live(), name="fs_bridge_watch_verify",
            )
```

`_on_file_event` — FIRST lines of the `try:` body:

```python
            if event.path.name == _SENTINEL_BASENAME:
                if self._sentinel_observed is not None and not self._sentinel_observed.is_set():
                    self._sentinel_observed.set()
                return  # internal liveness probe — never published downstream
```

New method:

```python
    async def _verify_pipeline_live(self) -> None:
        """Prove the watch pipeline delivers events (Slice 5 F1/F6, Run #15 L1).

        Touches a sentinel inside the watch root and awaits its own event.
        Observed  -> publish ``fs.watch.ready`` + the WATCH ACTIVE marker
        (the isomorphic driver gates chaos injection on that marker).
        Budget exhausted -> the WATCH NOT CONFIRMED warning (F6 boot half).
        Fail-soft by construction: background task, never blocks start().
        """
        import asyncio
        import time as _time

        t0 = _time.monotonic()
        budget = _ready_budget_s()
        retouch = _sentinel_retouch_s()
        attempt = 0
        try:
            while (_time.monotonic() - t0) < budget:
                try:
                    self._sentinel_path.parent.mkdir(parents=True, exist_ok=True)
                    # Content NONCE per attempt: the guard checksum-gates
                    # events once a baseline md5 exists (file_watch_guard
                    # :1933), so identical-bytes retouches could be dropped.
                    attempt += 1
                    self._sentinel_path.write_text(
                        '{"probe": "fs-watch-liveness", "attempt": %d}\n' % attempt,
                        encoding="utf-8",
                    )
                except OSError:
                    logger.debug("[FSEventBridge] sentinel touch failed", exc_info=True)
                try:
                    await asyncio.wait_for(self._sentinel_observed.wait(), timeout=retouch)
                except asyncio.TimeoutError:
                    continue  # bounded re-touch; the wait itself is event-driven
                elapsed = _time.monotonic() - t0
                self._watch_confirmed = True
                logger.info(
                    "[FSEventBridge] WATCH ACTIVE — pipeline verified live "
                    "(sentinel observed after %.1fs)", elapsed,
                )
                try:
                    await self._event_bus.publish_raw(
                        topic="fs.watch.ready",
                        data={"elapsed_s": elapsed, "watch_root": str(self._project_root)},
                        persist=False,
                    )
                except Exception:
                    logger.debug("[FSEventBridge] fs.watch.ready publish failed", exc_info=True)
                return
            logger.warning(
                "[FSEventBridge] WATCH NOT CONFIRMED — zero sentinel "
                "observations after %.0fs (events_published=%d); fs.changed "
                "consumers may be blind to changes in this window",
                budget, self._events_published,
            )
        finally:
            try:
                self._sentinel_path.unlink(missing_ok=True)
            except OSError:
                pass
```

`stop()`: cancel `self._verify_task` if pending. `get_metrics()`: add `"watch_confirmed": self._watch_confirmed`.

- [ ] **Step 4: Tests green**; also re-run T1's file (same module touched): `python3 -m pytest tests/governance/test_slice5_deep_ignore_globs.py tests/governance/test_slice5_watch_sentinel.py -v`.
- [ ] **Step 5: Commit** — `git commit -m "feat(sense): FSEventBridge sentinel self-verification + fs.watch.ready + WATCH ACTIVE marker (Slice 5 T4, Run #15 L1/F6)"`.

---

### Task 5: F5 + F6 steady-state — quiet-lane reconcile in the existing derate wake

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py` (`_poll_loop` derate branch `:1187` region)
- Test: `tests/governance/test_slice5_quiet_lane_reconcile.py`

**Interfaces:**
- Consumes: `self._fs_events_handled + self._fs_events_ignored` (T2 counters), `self._resolve_union(...)` (T2), `self._run_scoped_with_confirmation(...)`, existing 600s sleep in the derate branch.
- Produces: `async def _reconcile_quiet_lane(self) -> None`; env `JARVIS_TEST_FAILURE_QUIET_RECONCILE_ENABLED` (default true); INFO line containing `"quiet-lane reconcile"` (F6 steady-state signal).

**Background:** Run #15 L4 — the derate skips `poll_once()` forever while armed; a dropped event is dropped for the session. Fix: on each existing 600s wake, if the sensor saw ZERO fs events across the whole window, reconcile — bounded to git-dirty `.py` files (never the whole suite; preserves T3's anti-storm invariant). In the isomorphic soak the chaos-mutated file is git-dirty, so this alone re-detects a missed leaf event within one window. Mandate 3: piggybacks the existing loop/sleep — no new tasks, threads, or timers.

- [ ] **Step 1: Write failing tests**

```python
# tests/governance/test_slice5_quiet_lane_reconcile.py
"""Slice 5 T5 — quiet-lane reconcile on the derate wake (Run #15 L4, F5/F6)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import List

import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


class _StubWatcher:
    poll_interval_s = 30.0
    last_pytest_spawn_walltime = 0.0

    def __init__(self):
        self.poll_calls: List = []
        self._failure_streak: dict = {}

    async def poll_once(self, target_paths=None):
        self.poll_calls.append(tuple(target_paths) if target_paths else None)
        return []


def _sensor(monkeypatch) -> TestFailureSensor:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    s = TestFailureSensor(repo=".", router=SimpleNamespace(), test_watcher=_StubWatcher())
    s._running = True
    return s


class TestQuietLaneReconcile:
    @pytest.mark.asyncio
    async def test_reconcile_scopes_git_dirty_paths(self, monkeypatch):
        s = _sensor(monkeypatch)

        async def _dirty():
            return ["backend/leaf.py"]

        async def _resolve_union(paths):
            assert paths == ["backend/leaf.py"]
            return ["tests/test_leaf.py"]

        monkeypatch.setattr(s, "_git_dirty_py_paths", _dirty)
        monkeypatch.setattr(s, "_resolve_union", _resolve_union)
        ran: List = []

        async def _scoped(targets):
            ran.append(list(targets))

        monkeypatch.setattr(s, "_run_scoped_with_confirmation", _scoped)
        await s._reconcile_quiet_lane()
        assert ran == [["tests/test_leaf.py"]]

    @pytest.mark.asyncio
    async def test_clean_tree_runs_nothing(self, monkeypatch):
        s = _sensor(monkeypatch)

        async def _dirty():
            return []

        monkeypatch.setattr(s, "_git_dirty_py_paths", _dirty)
        await s._reconcile_quiet_lane()
        assert s._watcher.poll_calls == []      # NEVER whole-suite (T3 invariant)

    @pytest.mark.asyncio
    async def test_derate_wake_triggers_only_on_zero_events(self, monkeypatch):
        s = _sensor(monkeypatch)
        monkeypatch.setenv("JARVIS_TEST_FAILURE_FALLBACK_INTERVAL_S", "0.05")
        import backend.core.ouroboros.governance.intake.sensors.test_failure_sensor as m
        monkeypatch.setattr(m, "_TEST_FAILURE_FALLBACK_INTERVAL_S", 0.05)
        monkeypatch.setattr(s, "_event_primary_derate", lambda: True)
        calls: List = []

        async def _rec():
            calls.append(1)
            s._running = False              # stop the loop after first fire

        monkeypatch.setattr(s, "_reconcile_quiet_lane", _rec)
        await asyncio.wait_for(s._poll_loop(), timeout=5.0)
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_derate_wake_skips_when_events_flowed(self, monkeypatch):
        s = _sensor(monkeypatch)
        import backend.core.ouroboros.governance.intake.sensors.test_failure_sensor as m
        monkeypatch.setattr(m, "_TEST_FAILURE_FALLBACK_INTERVAL_S", 0.05)
        monkeypatch.setattr(s, "_event_primary_derate", lambda: True)
        called: List = []

        async def _rec():
            called.append(1)

        monkeypatch.setattr(s, "_reconcile_quiet_lane", _rec)

        async def _stop_soon():
            await asyncio.sleep(0.02)
            s._fs_events_handled += 1        # lane is alive
            await asyncio.sleep(0.06)
            s._running = False

        await asyncio.wait_for(
            asyncio.gather(s._poll_loop(), _stop_soon()), timeout=5.0,
        )
        assert called == [], "reconcile fired despite live event lane"
```

- [ ] **Step 2: Verify failure** — `_reconcile_quiet_lane`/`_git_dirty_py_paths` undefined.

- [ ] **Step 3: Implement.** Module helper:

```python
def quiet_reconcile_enabled() -> bool:
    return os.environ.get(
        "JARVIS_TEST_FAILURE_QUIET_RECONCILE_ENABLED", "true"
    ).lower() in ("1", "true", "yes")
```

Derate branch of `_poll_loop` becomes:

```python
            if self._event_primary_derate():
                if not self._poll_derate_logged:
                    logger.debug(
                        "[TestFailureSensor] event-primary lane armed — "
                        "skipping legacy whole-suite poll each cycle "
                        "(JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY=true to "
                        "force)."
                    )
                    self._poll_derate_logged = True
                lane_counter = self._fs_events_handled + self._fs_events_ignored
                try:
                    await asyncio.sleep(_TEST_FAILURE_FALLBACK_INTERVAL_S)
                except asyncio.CancelledError:
                    break
                # Slice 5 F5 (Run #15 L4): the derate skip is only safe when
                # the event lane demonstrably delivers. Zero events across a
                # whole fallback window -> one bounded git-dirty-scoped
                # reconcile (NEVER the whole suite — T3 anti-storm holds).
                if (
                    quiet_reconcile_enabled()
                    and self._running
                    and (self._fs_events_handled + self._fs_events_ignored)
                    == lane_counter
                ):
                    logger.info(
                        "TestFailureSensor: event lane delivered ZERO events "
                        "in %.0fs window — quiet-lane reconcile (bounded, "
                        "git-dirty scoped)",
                        _TEST_FAILURE_FALLBACK_INTERVAL_S,
                    )
                    try:
                        await self._reconcile_quiet_lane()
                    except Exception:
                        logger.debug(
                            "TestFailureSensor: quiet-lane reconcile error",
                            exc_info=True,
                        )
                continue
```

New methods:

```python
    async def _git_dirty_py_paths(self) -> list:
        """Tracked-dirty .py files via async git — bounded, never raises."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain=v1", "--untracked-files=no",
                cwd=self._repo,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                return []
        except Exception:
            logger.debug("TestFailureSensor: git dirty scan failed", exc_info=True)
            return []
        dirty: list = []
        for line in out.decode(errors="ignore").splitlines():
            p = line[3:].strip()
            if p.endswith(".py"):
                dirty.append(p)
        return dirty

    async def _reconcile_quiet_lane(self) -> None:
        """One bounded scoped run over git-dirty paths (Slice 5 F5)."""
        dirty = await self._git_dirty_py_paths()
        if not dirty:
            logger.debug(
                "TestFailureSensor: quiet-lane reconcile — tree clean, "
                "nothing to reconcile",
            )
            return
        cap = _debounce_max_paths()
        if len(dirty) > cap:
            logger.warning(
                "TestFailureSensor: reconcile %d dirty paths > cap %d — "
                "first %d only, dropped: %s",
                len(dirty), cap, cap, dirty[cap:],
            )
            dirty = dirty[:cap]
        union = await self._resolve_union(dirty)
        if not union:
            logger.debug(
                "TestFailureSensor: quiet-lane reconcile — no scoped "
                "targets for %r", dirty,
            )
            return
        logger.info(
            "TestFailureSensor: quiet-lane reconcile scoped %d test "
            "target(s) for %d dirty path(s)", len(union), len(dirty),
        )
        await self._run_scoped_with_confirmation(union)
```

- [ ] **Step 4: Tests green** + the T2/T3 slice files + `tests/governance/test_dynamic_test_scoping.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(sense): quiet-lane reconcile on the derate wake — zero-event window triggers one git-dirty-scoped run (Slice 5 T5, Run #15 L4)"`.

---

### Task 6: F1 driver half — WATCH ACTIVE gate + post-touch evidence verification

**Files:**
- Modify: `scripts/isomorphic_a1_local.py` (`:97` markers, `_await_soak_boot` `:1132`, `_touch_chaos_files` call site — search `"run-#12 fix"`)
- Test: `tests/scripts/test_slice5_driver_watch_gate.py`

**Interfaces:**
- Consumes: T4's exact markers `"[FSEventBridge] WATCH ACTIVE"` / `"[FSEventBridge] WATCH NOT CONFIRMED"`; T2's sensor evidence line (contains `TestFailureSensor` and the chaos file's basename).
- Produces: `async def _await_log_predicate(proc, debug_log, predicate, timeout_s, label) -> bool` (generalization; `_await_soak_boot` becomes a delegate); `_WATCH_ACTIVE_MARKER`, `_WATCH_NOT_CONFIRMED_MARKER` consts; env `JARVIS_ISO_WATCH_ACTIVE_BUDGET_S` (default 360), `JARVIS_ISO_CHAOS_EVIDENCE_BUDGET_S` (default 240), `JARVIS_ISO_CHAOS_TOUCH_RETRIES` (default 3), `JARVIS_ISO_REQUIRE_WATCH_ACTIVE` (default true).

**Background:** Run #15 L1 driver-side — run-#12's READY gate proves the subscription, not the pipeline; the touch's "(fires fs.changed.modified)" was never verified. Mandate 1: the readiness signal is the bridge's explicit `fs.watch.ready` bus event; the driver (a separate process) awaits its log-marker projection asynchronously with the existing scan machinery (which is an async poll of the log file — the established pattern from `_await_soak_boot`; NOT a naive one-shot sleep).

- [ ] **Step 1: Write failing tests**

```python
# tests/scripts/test_slice5_driver_watch_gate.py
"""Slice 5 T6 — driver WATCH ACTIVE gate + chaos-evidence verification."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "isomorphic_a1_local",
    Path(__file__).resolve().parents[2] / "scripts" / "isomorphic_a1_local.py",
)
iso = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("isomorphic_a1_local", iso)
_SPEC.loader.exec_module(iso)


class _Proc:
    def poll(self):
        return None


class TestAwaitLogPredicate:
    @pytest.mark.asyncio
    async def test_predicate_found(self, tmp_path):
        log = tmp_path / "debug.log"
        log.write_text("boot...\n[FSEventBridge] WATCH ACTIVE — pipeline verified live (sentinel observed after 92.6s)\n")
        ok = await iso._await_log_predicate(
            _Proc(), str(log), lambda l: iso._WATCH_ACTIVE_MARKER in l,
            timeout_s=2.0, label="watch-active",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self, tmp_path):
        log = tmp_path / "debug.log"
        log.write_text("nothing relevant\n")
        ok = await iso._await_log_predicate(
            _Proc(), str(log), lambda l: "NOPE" in l, timeout_s=0.7, label="x",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_await_soak_boot_delegates(self, tmp_path):
        log = tmp_path / "debug.log"
        log.write_text(iso._TESTWATCHER_READY_MARKER + "\n")
        assert await iso._await_soak_boot(_Proc(), str(log), timeout_s=2.0) is True


class TestEvidencePredicate:
    def test_evidence_line_matches(self):
        line = ("TestFailureSensor: scoped 1 test target(s) for 1 changed "
                "path(s): ['backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py']")
        assert iso._chaos_evidence_predicate(["leaf_predicates.py"])(line) is True

    def test_unrelated_sensor_line_no_match(self):
        line = "TestFailureSensor: scoped 1 test target(s) for 1 changed path(s): ['x.py']"
        assert iso._chaos_evidence_predicate(["leaf_predicates.py"])(line) is False
```

- [ ] **Step 2: Verify failure** — `_await_log_predicate`/`_chaos_evidence_predicate` undefined.

- [ ] **Step 3: Implement.** Constants near `:97`:

```python
_WATCH_ACTIVE_MARKER: str = "[FSEventBridge] WATCH ACTIVE"
_WATCH_NOT_CONFIRMED_MARKER: str = "[FSEventBridge] WATCH NOT CONFIRMED"
```

Generalize the scanner (place next to `_await_soak_boot`; `_await_soak_boot` becomes a two-line delegate passing `lambda l: _TESTWATCHER_READY_MARKER in l` — its docstring's async-loop rationale moves to the new function):

```python
async def _await_log_predicate(
    proc: Any,
    debug_log: str,
    predicate: Any,
    timeout_s: float,
    label: str,
) -> bool:
    """Async-scan debug_log until a line satisfies *predicate*.

    Generalization of the run-#12 READY scan (async loop is load-bearing:
    a blocking sleep here starves the SyntheticAdversary's aiohttp server).
    Returns False on timeout or premature soak exit.
    """
    if proc is None:
        return True  # stub soak
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _log("%s: soak exited prematurely (rc=%s)" % (label, proc.poll()))
            return False
        try:
            with open(debug_log, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if predicate(line):
                        _log("%s: marker observed" % label)
                        return True
        except OSError:
            pass
        await asyncio.sleep(0.5)
    _log("%s: TIMEOUT after %.0fs" % (label, timeout_s))
    return False


def _chaos_evidence_predicate(chaos_basenames: List[str]) -> Any:
    """Line-predicate: the sensor visibly scoped one of the chaos files."""
    def _pred(line: str) -> bool:
        return "TestFailureSensor" in line and any(
            b in line for b in chaos_basenames
        )
    return _pred
```

Main-flow insertion (locate the sequence: `_await_soak_boot(...)` → inject → `_touch_chaos_files(...)`):

1. After boot READY and BEFORE injection:

```python
    watch_budget = _env_float("JARVIS_ISO_WATCH_ACTIVE_BUDGET_S", 360.0)
    _log("STEP await WATCH ACTIVE (bridge sentinel self-verification)")
    watch_ok = await _await_log_predicate(
        proc, debug_log,
        lambda l: _WATCH_ACTIVE_MARKER in l, watch_budget, "watch-active",
    )
    if not watch_ok:
        if os.environ.get("JARVIS_ISO_REQUIRE_WATCH_ACTIVE", "true").lower() in ("1", "true", "yes"):
            _log("FATAL: watch pipeline never confirmed (WATCH ACTIVE absent "
                 "after %.0fs) — aborting BEFORE injection; SENSE would be "
                 "blind (Run #15 L1)" % watch_budget)
            # follow the existing zero-budget abort path: capture failure
            # telemetry + terminate the soak child + return FAILED
            ...  # reuse the sibling abort helper at the T1 zero-budget site
        _log("WARN: WATCH ACTIVE unconfirmed — proceeding (gate disabled)")
```

(The `...` line means: call the same telemetry-capture + teardown helper the T1 zero-budget abort path uses in this file — locate `capture_failure_telemetry` usage and mirror it; do not invent a new abort shape.)

2. After `_touch_chaos_files` returns `touched`:

```python
    basenames = [Path(p).name for p in touched]
    retries = int(_env_float("JARVIS_ISO_CHAOS_TOUCH_RETRIES", 3.0))
    evidence_budget = _env_float("JARVIS_ISO_CHAOS_EVIDENCE_BUDGET_S", 240.0)
    evidence = False
    for attempt in range(1, retries + 1):
        if await _await_log_predicate(
            proc, debug_log, _chaos_evidence_predicate(basenames),
            evidence_budget, "chaos-evidence",
        ):
            _log("run-#15 fix: sensor evidence observed (attempt %d/%d) — "
                 "the touch VERIFIABLY reached SENSE" % (attempt, retries))
            evidence = True
            break
        _log("run-#15 fix: no sensor evidence in %.0fs — re-firing chaos "
             "files WITH CONTENT NONCE (attempt %d/%d)" % (evidence_budget, attempt, retries))
        _refire_chaos_files_with_nonce(touched, attempt)
    if not evidence:
        _log("FATAL: chaos touch produced no sensor evidence after %d "
             "attempts — SENSE blind, aborting (Run #15 L1-L3 class)" % retries)
        ...  # same abort helper as above
```

Add the nonce re-fire helper next to `_touch_chaos_files` — a bare `Path.touch()` changes mtime only, and the guard checksum-gates identical-content events once a baseline md5 exists (`file_watch_guard.py:1933`); the watchdog baseline-snapshot race can additionally absorb an mtime bump entirely (verified: parallel autopsy `2026-07-10-run15-attempt3-autopsy.md` + repro). Re-fires must change bytes:

```python
def _refire_chaos_files_with_nonce(touched: List[str], attempt: int) -> None:
    """Append a nonce comment so the content checksum ACTUALLY changes.

    A bare mtime touch is checksum-gated (guard :1933) and can be absorbed
    by the watchdog baseline race — Run #15's falsified "(fires
    fs.changed.modified)" assumption. The nonce line is driver-owned litter
    on an already-chaos-mutated file; chaos revert restores byte-identical
    original content, discarding it.
    """
    for abs_cf in touched:
        try:
            with open(abs_cf, "a", encoding="utf-8") as fh:
                fh.write("# chaos-refire-nonce-%d\n" % attempt)
            _log("run-#15 fix: nonce-refired %s (attempt %d)" % (abs_cf, attempt))
        except OSError as exc:
            _log("run-#15 fix: nonce refire FAILED for %s: %s" % (abs_cf, exc))
```

Also, immediately BEFORE the injection step, purge any stale plugin-results snapshot so the sensor's plugin lane cannot serve a pre-mutation "0 failures" read (parallel-autopsy fix 1c; T3's mtime gate is the durable belt — this is the harness-side suspender):

```python
    stale_results = os.path.join(repo_root, ".jarvis", "test_results.json")
    try:
        os.unlink(stale_results)
        _log("run-#15 fix: purged stale %s pre-inject" % stale_results)
    except FileNotFoundError:
        pass
```

Update the `_touch_chaos_files` per-file log line: `"run-#12 fix: touched %s (fs.changed.modified pending driver verification)"` — the old "(fires …)" claim was the falsified assumption.

- [ ] **Step 4: Tests green** — `python3 -m pytest tests/scripts/test_slice5_driver_watch_gate.py -v`; plus the driver's existing test spine: `python3 -m pytest tests/scripts/ -q -k "isomorphic or iso"` (pre-existing reds per ledger: test_hybrid_mesh_{readiness,teardown} — stash-verified pre-existing, do not chase).
- [ ] **Step 5: Commit** — `git commit -m "fix(iso): gate chaos injection on WATCH ACTIVE + verify sensor evidence post-touch with bounded retry (Slice 5 T6, Run #15 L1 driver half)"`.

---

### Task 7: L6 — Adversary batch lane must serve a valid full-file repair for the chaos target

**Files:**
- Modify: `scripts/synthetic_adversary.py` (batch response builder — locate via `grep -n "v1_batch" scripts/synthetic_adversary.py`)
- Test: `tests/scripts/test_slice5_adversary_chaos_candidate.py`

**Interfaces:**
- Consumes: `_load_chaos_manifest()` (`scripts/synthetic_adversary.py:461`), manifest fields `target_file` (repo-relative), `original_source` (FULL pre-mutation file text — verified: chaos revert writes the whole file from it), `function`; existing repair-path helpers `_is_repair_prompt`, `_extract_target_file_from_prompt`, `build_repair_completion` (`:576`).
- Produces: the batch lane (`v1_batch_*` responses) serving, for any batched op whose prompt references the manifest's `target_file`, a schema-`2b.1` candidates JSON with `file_path = manifest["target_file"]` and `full_content = manifest["original_source"]` (full length). Non-chaos batched ops keep the existing canned behavior byte-identical.

**Background (Run #15 L6, parallel autopsy finding, controller-verified):** the isomorphic run's ONLY mutation-class op (signed-roadmap seed) was routed to the batch lane, whose canned response carries a fixed ~1306-byte truncated `leaf_predicates.py` payload. Validation correctly rejected it every time — 8× `full_content too short (1306 vs 15183 bytes)` (see `orchestrator.py:5625` region for the validator), 4× `all_candidates_syntax_error`, 2× `file_scope_mismatch` on non-leaf ops. The chaos-repair completion path (`build_repair_completion`) already serves `manifest["original_source"]` correctly — the batch builder never consults the manifest. Without this fix, ACT is untestable in iso mode: no Run #16 can pass the A1 gate even with SENSE fixed.

- [ ] **Step 1: Write failing tests**

```python
# tests/scripts/test_slice5_adversary_chaos_candidate.py
"""Slice 5 T7 — adversary batch lane serves the manifest's full-file repair."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "synthetic_adversary",
    Path(__file__).resolve().parents[2] / "scripts" / "synthetic_adversary.py",
)
adv = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("synthetic_adversary", adv)
_SPEC.loader.exec_module(adv)

_FULL_FILE = "def clamp01(x):\n    return max(0.0, min(1.0, x))\n" + ("# pad\n" * 400)
_MANIFEST = {
    "target_file": "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py",
    "original_source": _FULL_FILE,
    "function": "clamp01",
}


class TestBatchChaosCandidate:
    def test_chaos_targeted_batch_gets_full_file_candidate(self):
        prompt = (
            "Fix the failing test in "
            "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py "
            "(clamp01 regression)."
        )
        body = adv.build_batch_candidates_content(prompt, manifest=_MANIFEST)
        payload = json.loads(body)
        assert payload["schema_version"] == "2b.1"
        cand = payload["candidates"][0]
        assert cand["file_path"] == _MANIFEST["target_file"]
        assert cand["full_content"] == _FULL_FILE, "must be the FULL original file, not a truncation"

    def test_non_chaos_batch_unchanged(self):
        prompt = "Summarize recent TODO debt in backend/voice/."
        body = adv.build_batch_candidates_content(prompt, manifest=_MANIFEST)
        payload = json.loads(body)
        assert payload["candidates"][0].get("full_content") != _FULL_FILE

    def test_no_manifest_keeps_legacy(self):
        prompt = "Fix leaf_predicates.py"
        body = adv.build_batch_candidates_content(prompt, manifest=None)
        json.loads(body)  # legacy canned body must remain parseable JSON
```

(`build_batch_candidates_content(prompt, manifest=...)` is the REQUIRED extraction seam: pull the existing inline canned-batch body construction into this pure module-level function — same name, exact signature — and route the HTTP batch handler through it. If the current builder produces the canned body somewhere other than a single site, consolidate; the HTTP handler behavior for non-chaos prompts must remain byte-identical, proven by capturing one canned body before refactor and asserting equality after.)

- [ ] **Step 2: Verify failure** — `python3 -m pytest tests/scripts/test_slice5_adversary_chaos_candidate.py -v` — FAIL (`build_batch_candidates_content` undefined).
- [ ] **Step 3: Implement** — extract the seam; inside it: if `manifest` is not None and `manifest.get("target_file")` and (`manifest["target_file"]` in prompt or Path(manifest["target_file"]).name in prompt) → return `json.dumps({"schema_version": "2b.1", "candidates": [{"candidate_id": "c1", "file_path": manifest["target_file"], "full_content": manifest["original_source"], "rationale": "Chaos-manifest repair: restore verified original implementation of %s." % manifest.get("function", "target")}]})`; else legacy canned body. The HTTP handler passes `manifest=_load_chaos_manifest()`.
- [ ] **Step 4: Tests green**; also run the adversary's existing spine: `python3 -m pytest tests/scripts/ -q -k "adversary"` (if none exists, note that in the report).
- [ ] **Step 5: Commit** — `git commit -m "fix(iso): adversary batch lane serves manifest full-file repair for chaos-targeted ops (Slice 5 T7, Run #15 L6 — ACT untestable without it)"`.

---

### Task 8: Regression sweep + docs + merge prep

**Files:**
- Modify: `docs/superpowers/specs/2026-07-10-run15-autopsy.md` (append "FIXED BY" line per F#), `.superpowers/sdd/progress.md` (Slice 5 section)

- [ ] **Step 1: Slice regression** — `python3 -m pytest tests/governance/test_slice5_deep_ignore_globs.py tests/governance/test_slice5_coalescing_debounce.py tests/governance/test_slice5_results_staleness_gate.py tests/governance/test_slice5_watch_sentinel.py tests/governance/test_slice5_quiet_lane_reconcile.py tests/scripts/test_slice5_driver_watch_gate.py tests/scripts/test_slice5_adversary_chaos_candidate.py -v` — expect all green.
- [ ] **Step 2: Affected-set sweep** — `python3 -m pytest tests/governance/test_dynamic_test_scoping.py tests/governance/test_slice12a_file_watch_overflow_fix.py tests/test_ouroboros_governance/ -q -k "test_failure or watcher or fs_event or intake" --timeout=60` — failure-set must be byte-identical to a stashed-baseline run (stash → run → pop protocol from Slice 3/4; strip ANSI before diffing FAILED lines).
- [ ] **Step 3: Update autopsy doc** — under each L#/F# add one line: `FIXED: Slice 5 T<n> (<commit subject>)`.
- [ ] **Step 4: Ledger** — append Slice 5 section to `.superpowers/sdd/progress.md` (branch, per-task commits, review verdicts, re-targeted tests with rationale).
- [ ] **Step 5: Commit docs** — `git commit -m "docs(slice5): autopsy fix cross-references + ledger"` — then STOP. Whole-branch review (opus) → OCA-idle ff-merge to local main → Run #16 ignition are conducted by the controller session, not this task.

---

## Self-Review Notes

- Spec coverage: F1 = T4 (organism) + T6 (driver); F2 = T2; F3 = T1; F4 = T3; F5/F6 = T4 (boot WARN) + T5 (steady-state); L6 (parallel-autopsy ACT finding, controller-verified) = T7 — all links covered.
- Reconciled with the parallel autopsy `docs/superpowers/specs/2026-07-10-run15-attempt3-autopsy.md` (commit ebb5b7176f): its checksum-baseline mechanism is verified (guard `:1933` checksum gate + watchdog baseline race) and drove the T4 sentinel nonce + T6 content-nonce re-fire + T6 stale-results purge + new T7.
- Mandate 1: driver awaits the bridge-published event's marker via the established async log-scan; no one-shot sleeps. Mandate 2: set accumulator + env-loaded deep globs. Mandate 3: T5 rides the existing `_poll_loop` sleep and counters; T4 is one asyncio task on the existing loop — zero new threads/timers. Mandate 4: mtime-vs-`max(spawn, boot)` floor; absent/stale never bumps.
- Type consistency: `_resolve_union` defined in T2, consumed in T5; `_debounce_max_paths` defined in T2, consumed in T5; `_WATCH_ACTIVE_MARKER` string in T6 matches T4's log format prefix; `_StubWatcher.last_pytest_spawn_walltime` present in T2/T5 stubs so T3's `getattr` floor works.
- Known risks for reviewers: (1) T2 changes debounce semantics sliding→fixed window — intentional, documented; (2) T4's test seam for FileWatchGuard patching may need adjustment to the real import site (`start()` imports from `backend.core.resilience.file_watch_guard`); (3) T6's abort path must reuse the existing telemetry+teardown helper, not invent one; (4) sensor tests directly instantiate `TestFailureSensor(repo=".", router=...)` — mirror the constructor kwargs actually defined at `:470` (`repo`, `router`, `test_watcher`).
