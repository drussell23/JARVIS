# Resource Governor / Anti-Wedge Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Ouroboros runtime biologically aware of its own footprint so the omni soak stops wedging the node at soak-launch — by extending the existing `MemoryPressureGate` and harness watchdog in place (zero duplication), not building anything new.

**Architecture:** Five extensions: (1) a CPU+context-switch dimension on `MemoryPressureGate`; (2) adaptive non-linear watchdog polling in the harness; (3) an allocation-free pre-OOM Death Rattle via a pre-opened fd; (4) governor-gated, jittered sensor poll-loop activation; (5) a macOS-native local diagnostic streamer. Everything is gated default-OFF and byte-identical when off.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), asyncio, psutil (already a dep), faulthandler (stdlib), pytest.

## Global Constraints

- **Python 3.9+** — no `asyncio.timeout`; use `asyncio.wait_for`. Every new file starts with `from __future__ import annotations`.
- **No hardcoded magic numbers** — every threshold/interval is an env var with a sane default, read through `_env_bool/_env_float/_env_int` (gate) or equivalent.
- **Default-OFF, byte-identical-when-off** — master `JARVIS_RESOURCE_GOVERNOR_ENABLED=false` and each sub-flag default false; every modified path early-returns to legacy behavior when its flag is off. Each piece gets a git-HEAD parity test.
- **Authority invariant** — `MemoryPressureGate` must never import any scheduler/sensor/orchestrator/policy module. Consumers pull from the gate; the gate never reaches into them.
- **Allocation-free at redline** — the guaranteed Death-Rattle path uses only a pre-opened int fd + pre-encoded `bytes` literals + `os.write` + `faulthandler.dump_traceback(<int fd>)`. No f-strings/`open()`/`.format`/new containers on the guaranteed path. RSS table is a best-effort tier after the guaranteed dump.
- **Immune layer reads raw signals only** — the thread backstop keeps a fixed aggressive interval; it never depends on the adaptive-interval state it guards.
- **Verified facts (do not re-litigate):** `faulthandler.dump_traceback(file=<int fd>)` works on CPython 3.11.10 (allocation-free). `psutil.swap_memory()` raises `OSError` on the local macOS → use `sysctl vm.swapusage` + `vm_stat`. `psutil.cpu_percent(interval=None)` can read `0.0` on macOS idle → ctx-switch rate is the PRIMARY thrash signal, cpu_pct secondary.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `backend/core/ouroboros/governance/memory_pressure_gate.py` | CPU/ctx-switch dimension + env readers + compose into `pressure()` | Modify |
| `backend/core/ouroboros/battle_test/harness.py` | Adaptive polling; pre-open autopsy fd; `_fire_death_rattle`; wire into cap-fire + thread backstop + redline | Modify |
| `backend/core/ouroboros/governance/intake/intake_layer_service.py` | Governor-gated jittered sensor activation (replace `:1030-1031` loop) | Modify |
| `scripts/resource_blackbox_local.py` | macOS-native local resource streamer for the diagnostic | Create |
| `tests/governance/test_resource_governor_cpu_dim.py` | Gate CPU/ctx dim unit tests | Create |
| `tests/battle_test/test_resource_governor_watchdog.py` | Adaptive polling + Death Rattle unit tests | Create |
| `tests/governance/test_resource_governor_stagger.py` | Gated-stagger unit tests | Create |
| `deploy/ouroboros_omni_prod.env` | Add governor flags (commented OFF until the local proof) | Modify |

---

## Task 1: CPU + context-switch dimension on `MemoryPressureGate`

**Files:**
- Modify: `backend/core/ouroboros/governance/memory_pressure_gate.py` (env block near `:156`, dataclasses near `:214`, gate `__init__` `:424`, new method beside `_process_tree_dim` `:490`)
- Test: `tests/governance/test_resource_governor_cpu_dim.py`

**Interfaces:**
- Consumes: existing `_env_bool/_env_float/_env_int` (`:72-97`), `PressureLevel` (`:193`), `_strictest` (`:208`).
- Produces: `cpu_dim_enabled() -> bool`, `cpu_critical_pct()/cpu_high_pct() -> float`, `ctx_spike_mult() -> float`, `ctx_baseline_halflife_s() -> float`; dataclass `CpuCtxSample(cpu_pct: float, ctx_switches: int, ts: float, ok: bool=True)`; module fn `_sample_cpu_ctx() -> CpuCtxSample`; gate method `_cpu_ctx_dim(self) -> Tuple[PressureLevel, Optional[float], Optional[float]]` returning `(level, cpu_pct, ctx_rate)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_resource_governor_cpu_dim.py
from __future__ import annotations
import importlib, os
import backend.core.ouroboros.governance.memory_pressure_gate as mpg


def _gate_with_samples(samples):
    """Build a gate whose cpu/ctx sampler yields the given sequence."""
    it = iter(samples)
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=60.0, total_bytes=1, available_bytes=1, source="test"))
    g._cpu_ctx_sampler = lambda: next(it)
    return g


def test_ctx_switch_spike_declares_critical_at_60pct_ram(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CTX_SPIKE_MULT", "3.0")
    S = mpg.CpuCtxSample
    # seed(t=0) -> baseline(t=1, rate=1000/s) -> spike(t=2, rate=10000/s)
    g = _gate_with_samples([
        S(cpu_pct=0.0, ctx_switches=0,     ts=0.0),
        S(cpu_pct=0.0, ctx_switches=1000,  ts=1.0),
        S(cpu_pct=0.0, ctx_switches=11000, ts=2.0),
    ])
    assert g._cpu_ctx_dim()[0] == mpg.PressureLevel.OK     # seed
    assert g._cpu_ctx_dim()[0] == mpg.PressureLevel.OK     # baseline set
    assert g._cpu_ctx_dim()[0] == mpg.PressureLevel.CRITICAL  # spike at 60% RAM


def test_cpu_dim_off_is_inert(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", raising=False)
    S = mpg.CpuCtxSample
    g = _gate_with_samples([S(cpu_pct=99.0, ctx_switches=10**9, ts=9.0)])
    assert g._cpu_ctx_dim() == (mpg.PressureLevel.OK, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_resource_governor_cpu_dim.py -q`
Expected: FAIL — `AttributeError: module has no attribute 'CpuCtxSample'` / `_cpu_ctx_dim`.

- [ ] **Step 3: Add env readers** (after `process_high_frac()` block, near `:179`)

```python
# Resource Governor — CPU + context-switch dimension. Master sub-flag
# default-FALSE; off -> _cpu_ctx_dim returns OK -> strictest no-op ->
# byte-identical legacy path. ctx-switch RATE vs rolling EWMA baseline
# is the PRIMARY swap-thrash signal (no hardcoded N); cpu_pct is a
# best-effort secondary (psutil.cpu_percent is noisy/0.0 on macOS).
def cpu_dim_enabled() -> bool:
    return _env_bool("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", False)


def cpu_critical_pct() -> float:
    return _env_float("JARVIS_RESOURCE_GOVERNOR_CPU_CRITICAL_PCT", 95.0, minimum=1.0)


def cpu_high_pct() -> float:
    return _env_float("JARVIS_RESOURCE_GOVERNOR_CPU_HIGH_PCT", 80.0, minimum=1.0)


def ctx_spike_mult() -> float:
    """ctx_rate > baseline * this -> CRITICAL. Default 3.0."""
    return _env_float("JARVIS_RESOURCE_GOVERNOR_CTX_SPIKE_MULT", 3.0, minimum=1.1)


def ctx_baseline_halflife_s() -> float:
    return _env_float("JARVIS_RESOURCE_GOVERNOR_CTX_BASELINE_HALFLIFE_S", 30.0, minimum=1.0)
```

- [ ] **Step 4: Add the `CpuCtxSample` dataclass + sampler** (after `MemoryProbe`, near `:224`)

```python
@dataclass(frozen=True)
class CpuCtxSample:
    cpu_pct: float       # best-effort (0.0 on macOS idle)
    ctx_switches: int    # monotonic counter
    ts: float            # time.monotonic()
    ok: bool = True


def _sample_cpu_ctx() -> CpuCtxSample:
    import time as _t
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=None)          # non-blocking
        ctx = int(psutil.cpu_stats().ctx_switches)       # reliable on macOS
        return CpuCtxSample(cpu_pct=float(cpu), ctx_switches=ctx, ts=_t.monotonic())
    except Exception:  # noqa: BLE001 — fail-open, this dim only advises
        return CpuCtxSample(cpu_pct=0.0, ctx_switches=0, ts=_t.monotonic(), ok=False)
```

- [ ] **Step 5: Seed gate state in `__init__`** (`:430`, after `self._probe_fn = ...`)

```python
        self._cpu_ctx_sampler: Callable[[], "CpuCtxSample"] = _sample_cpu_ctx
        self._last_ctx: Optional[Tuple[int, float]] = None   # (ctx_switches, ts)
        self._ctx_baseline: Optional[float] = None           # EWMA rate /s
```

- [ ] **Step 6: Add `_cpu_ctx_dim`** (immediately after `_process_tree_dim`, near `:535`)

```python
    def _cpu_ctx_dim(
        self,
    ) -> Tuple[PressureLevel, Optional[float], Optional[float]]:
        """Advisory CPU + context-switch pressure dimension.

        ctx-switch RATE (Δctx/Δt) vs a rolling EWMA baseline is the
        PRIMARY swap-thrash signal: a violent spike -> CRITICAL even
        when RAM free% is comfortable (the 60%-RAM swap-storm). cpu_pct
        is a best-effort secondary only. DISABLED/unavailable ->
        (OK, None, None) so the strictest-wins compose is a no-op and
        the legacy path stays byte-identical. Fail-open on any error.
        """
        if not cpu_dim_enabled():
            return PressureLevel.OK, None, None
        try:
            s = self._cpu_ctx_sampler()
            if not s.ok:
                return PressureLevel.OK, None, None
            level = PressureLevel.OK
            ctx_rate: Optional[float] = None
            with self._lock:
                prev = self._last_ctx
                self._last_ctx = (s.ctx_switches, s.ts)
                if prev is not None:
                    dt = s.ts - prev[1]
                    if dt > 0:
                        ctx_rate = max(0.0, (s.ctx_switches - prev[0]) / dt)
                if ctx_rate is not None:
                    if self._ctx_baseline is None:
                        self._ctx_baseline = ctx_rate          # establish
                    else:
                        # Detect spike BEFORE folding it into the baseline
                        # (else the spike inflates the baseline and hides).
                        if ctx_rate > self._ctx_baseline * ctx_spike_mult():
                            level = PressureLevel.CRITICAL
                        dt2 = max(1e-6, s.ts - prev[1])
                        alpha = 1.0 - 0.5 ** (dt2 / max(1e-6, ctx_baseline_halflife_s()))
                        self._ctx_baseline = (
                            (1.0 - alpha) * self._ctx_baseline + alpha * ctx_rate
                        )
            # Best-effort secondary cpu_pct (never the sole trigger).
            if s.cpu_pct >= cpu_critical_pct():
                level = _strictest(level, PressureLevel.CRITICAL)
            elif s.cpu_pct >= cpu_high_pct():
                level = _strictest(level, PressureLevel.HIGH)
            return level, s.cpu_pct, ctx_rate
        except Exception:  # noqa: BLE001 — fail-open
            logger.debug("[MemoryPressureGate] cpu_ctx_dim raised", exc_info=True)
            return PressureLevel.OK, None, None
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_resource_governor_cpu_dim.py -q`
Expected: PASS (2 tests).

- [ ] **Step 8: Commit**

```bash
git add backend/core/ouroboros/governance/memory_pressure_gate.py tests/governance/test_resource_governor_cpu_dim.py
git commit -m "feat(gate): CPU+ctx-switch dimension (ctx-rate vs EWMA baseline, default-OFF)"
```

---

## Task 2: Compose the CPU/ctx dimension into `pressure()`

**Files:**
- Modify: `backend/core/ouroboros/governance/memory_pressure_gate.py:469-474` (`pressure()`)
- Test: `tests/governance/test_resource_governor_cpu_dim.py` (append)

**Interfaces:**
- Consumes: `_cpu_ctx_dim` (Task 1), `_strictest` (`:208`).
- Produces: `pressure()` now strictest-composes free% + process-tree + cpu/ctx.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_pressure_off_is_byte_identical(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=55.0, total_bytes=1, available_bytes=1, source="test"))
    assert g.pressure() == mpg.PressureLevel.OK   # free% only, dims off


def test_pressure_escalates_on_ctx_spike(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    S = mpg.CpuCtxSample
    it = iter([S(0.0, 0, 0.0), S(0.0, 1000, 1.0), S(0.0, 11000, 2.0)])
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=60.0, total_bytes=1, available_bytes=1, source="test"))
    g._cpu_ctx_sampler = lambda: next(it)
    g.pressure(); g.pressure()
    assert g.pressure() == mpg.PressureLevel.CRITICAL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_resource_governor_cpu_dim.py::test_pressure_escalates_on_ctx_spike -q`
Expected: FAIL — `pressure()` returns OK (dim not composed yet).

- [ ] **Step 3: Compose into `pressure()`** (replace lines `:470-474`)

```python
        # Strictest-wins compose across all advisory dimensions. Each
        # disabled dim returns OK so the result == the legacy free-%-only
        # level when every dim is off (byte-identical, AST-pinnable).
        proc_level, _rss, _cap = self._process_tree_dim()
        cpu_level, _cpu, _ctx = self._cpu_ctx_dim()
        return _strictest(_strictest(free_level, proc_level), cpu_level)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_resource_governor_cpu_dim.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/memory_pressure_gate.py tests/governance/test_resource_governor_cpu_dim.py
git commit -m "feat(gate): compose cpu/ctx dim into pressure() (strictest-wins, OFF byte-identical)"
```

---

## Task 3: Adaptive non-linear watchdog interval (pure resolver)

**Files:**
- Modify: `backend/core/ouroboros/battle_test/harness.py` (new module-level env readers + a `_resolve_adaptive_pm_interval` method near `:6526`)
- Test: `tests/battle_test/test_resource_governor_watchdog.py`

**Interfaces:**
- Produces: module fns `rg_adaptive_polling_enabled() -> bool`, `rg_poll_interval_for(level: str) -> float`, `rg_backstop_interval_s() -> float`; harness method `_resolve_adaptive_pm_interval(self, level, fallback: float) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/battle_test/test_resource_governor_watchdog.py
from __future__ import annotations
import backend.core.ouroboros.battle_test.harness as H


def test_adaptive_interval_inversely_proportional(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED", "true")
    assert H.rg_poll_interval_for("ok")       == 10.0
    assert H.rg_poll_interval_for("warn")     == 3.0
    assert H.rg_poll_interval_for("high")     == 0.5
    assert H.rg_poll_interval_for("critical") == 0.2
    # unknown level -> OK interval
    assert H.rg_poll_interval_for("???")      == 10.0


def test_backstop_interval_has_fixed_floor(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_BACKSTOP_INTERVAL_S", raising=False)
    assert H.rg_backstop_interval_s() == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py::test_adaptive_interval_inversely_proportional -q`
Expected: FAIL — `module has no attribute 'rg_poll_interval_for'`.

- [ ] **Step 3: Add the env readers** (module level near the other harness env helpers; place above class `BattleTestHarness`)

```python
def rg_adaptive_polling_enabled() -> bool:
    return os.environ.get(
        "JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED", "false",
    ).strip().lower() in ("1", "true", "yes")


def _rg_envf(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        v = float(raw) if raw else default
    except ValueError:
        v = default
    return max(minimum, v)


def rg_poll_interval_for(level: str) -> float:
    """Adaptive watchdog interval — inversely proportional to pressure.
    As the system nears the event horizon, situational awareness
    accelerates. All env-tunable; no hardcoded constants survive."""
    lvl = (level or "ok").strip().lower()
    if lvl == "critical":
        return _rg_envf("JARVIS_RESOURCE_GOVERNOR_POLL_CRITICAL_S", 0.2, 0.05)
    if lvl == "high":
        return _rg_envf("JARVIS_RESOURCE_GOVERNOR_POLL_HIGH_S", 0.5, 0.05)
    if lvl == "warn":
        return _rg_envf("JARVIS_RESOURCE_GOVERNOR_POLL_WARN_S", 3.0, 0.1)
    return _rg_envf("JARVIS_RESOURCE_GOVERNOR_POLL_OK_S", 10.0, 0.1)


def rg_backstop_interval_s() -> float:
    """Fixed aggressive floor for the starvation-immune thread backstop —
    deliberately NOT adaptive (the immune layer must not depend on the
    state it guards). Mirrors the Watchdog Isolation Invariant."""
    return _rg_envf("JARVIS_RESOURCE_GOVERNOR_BACKSTOP_INTERVAL_S", 1.0, 0.1)
```

- [ ] **Step 4: Add the harness resolver method** (inside `BattleTestHarness`, just above `_monitor_process_memory` at `:6526`)

```python
    def _resolve_adaptive_pm_interval(self, level, fallback: float) -> float:
        """Map a PressureLevel (or None) to an adaptive sleep. Off -> fallback
        (the legacy fixed interval) so the loop is byte-identical when the
        adaptive flag is unset."""
        if not rg_adaptive_polling_enabled():
            return fallback
        lvl = getattr(level, "value", level) if level is not None else "ok"
        return rg_poll_interval_for(str(lvl))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py -q`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/battle_test/harness.py tests/battle_test/test_resource_governor_watchdog.py
git commit -m "feat(harness): adaptive non-linear watchdog interval resolver (default-OFF)"
```

---

## Task 4: Wire adaptive interval into `_monitor_process_memory`

**Files:**
- Modify: `backend/core/ouroboros/battle_test/harness.py:6544-6582` (async loop) and `:6608` (thread backstop interval)
- Test: `tests/battle_test/test_resource_governor_watchdog.py` (append)

**Interfaces:**
- Consumes: `_resolve_adaptive_pm_interval` (Task 3), `rg_backstop_interval_s` (Task 3), `get_default_gate().pressure()`.

- [ ] **Step 1: Write the failing test (append)** — proves the loop computes its next sleep from the gate level.

```python
def test_monitor_uses_adaptive_interval(monkeypatch):
    import asyncio
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED", "true")
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    # Stub the level source + capture the sleeps requested.
    from backend.core.ouroboros.governance import memory_pressure_gate as mpg
    monkeypatch.setattr(mpg.MemoryPressureGate, "pressure",
                        lambda self: mpg.PressureLevel.HIGH)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        raise asyncio.CancelledError  # exit after one iteration

    monkeypatch.setattr(H.asyncio, "sleep", fake_sleep)
    h._probe_process_tree_rss_mb = lambda: 1.0
    asyncio.run(_drive_once(h))
    assert sleeps and sleeps[0] == 0.5   # HIGH -> 0.5s


async def _drive_once(h):
    import asyncio
    try:
        await h._monitor_process_memory(warn_mb=1e9, cap_mb=1e9, interval_s=15.0)
    except asyncio.CancelledError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py::test_monitor_uses_adaptive_interval -q`
Expected: FAIL — first sleep is 15.0 (legacy fixed), not 0.5.

- [ ] **Step 3: Make the async loop sleep adaptively** — replace the `while True:` head of `_monitor_process_memory` (`:6544-6546`):

```python
        _next_interval = interval_s
        while True:
            try:
                await asyncio.sleep(_next_interval)
            except asyncio.CancelledError:
                logger.info(
                    "[ProcessMemoryWatchdog] async monitor cancelled "
                    "(NEVER fired)",
                )
                return
```

Then, immediately after `rss_mb = self._probe_process_tree_rss_mb()` and its `None` guard (`:6554-6556`), insert:

```python
            # Adaptive cadence: accelerate as pressure rises (off -> fixed).
            if rg_adaptive_polling_enabled():
                try:
                    from backend.core.ouroboros.governance.memory_pressure_gate import (  # noqa: E501
                        get_default_gate as _rg_gate,
                    )
                    _next_interval = self._resolve_adaptive_pm_interval(
                        _rg_gate().pressure(), interval_s,
                    )
                except Exception:  # noqa: BLE001
                    _next_interval = interval_s
```

- [ ] **Step 4: Give the thread backstop the fixed floor** — replace `:6608`:

```python
        thread_interval = (
            rg_backstop_interval_s()
            if rg_adaptive_polling_enabled()
            else max(interval_s * 2.0, 10.0)
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/battle_test/harness.py tests/battle_test/test_resource_governor_watchdog.py
git commit -m "feat(harness): wire adaptive cadence into RSS monitor; backstop keeps fixed floor"
```

---

## Task 5: Allocation-free Death Rattle (pre-opened fd)

**Files:**
- Modify: `backend/core/ouroboros/battle_test/harness.py` (module-level `bytes` constants + env reader; `_open_autopsy_fd`/`_fire_death_rattle` methods near `:6470`; call `_open_autopsy_fd()` early in `run()` at `:785`)
- Test: `tests/battle_test/test_resource_governor_watchdog.py` (append)

**Interfaces:**
- Produces: `rg_death_rattle_enabled() -> bool`, `rg_redline_free_pct() -> float`; methods `_open_autopsy_fd(self) -> None`, `_fire_death_rattle(self) -> None`.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_death_rattle_writes_allocation_free_dump(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", "true")
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._open_autopsy_fd()
    assert getattr(h, "_autopsy_fd", None) is not None
    h._fire_death_rattle()
    body = (tmp_path / "pre_oom_autopsy.log").read_text()
    assert "PRE-OOM DEATH RATTLE" in body
    assert "END DEATH RATTLE" in body
    assert ("File" in body or "Thread" in body)  # faulthandler stack present


def test_death_rattle_off_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", raising=False)
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._open_autopsy_fd()           # still pre-opens (cheap, boot-time)
    h._fire_death_rattle()         # but writes nothing when off
    p = tmp_path / "pre_oom_autopsy.log"
    assert (not p.exists()) or p.read_text() == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py::test_death_rattle_writes_allocation_free_dump -q`
Expected: FAIL — `_open_autopsy_fd` undefined.

- [ ] **Step 3: Add module-level pre-encoded bytes + env readers** (near the other `rg_*` helpers)

```python
# Pre-encoded at import — the guaranteed Death-Rattle path must allocate
# NOTHING (an f-string under hard OOM raises MemoryError and dies silent).
_RG_RATTLE_HDR = b"\n=== RESOURCE-GOVERNOR PRE-OOM DEATH RATTLE ===\n"
_RG_RATTLE_STACKS = b"--- thread stacks (faulthandler, allocation-free) ---\n"
_RG_RATTLE_RSS = b"--- process-tree RSS (best-effort, may fail under OOM) ---\n"
_RG_RATTLE_FTR = b"=== END DEATH RATTLE ===\n\n"


def rg_death_rattle_enabled() -> bool:
    return os.environ.get(
        "JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", "false",
    ).strip().lower() in ("1", "true", "yes")


def rg_redline_free_pct() -> float:
    """Fire the rattle+stop when system free% drops below this — tighter
    than the existing 0.75 RSS cap so we dump BEFORE the kernel OOM-kills."""
    return _rg_envf("JARVIS_RESOURCE_GOVERNOR_REDLINE_FREE_PCT", 8.0, 0.5)
```

- [ ] **Step 4: Add the two methods** (inside `BattleTestHarness`, just above `_fire_process_memory_cap` at `:6470`)

```python
    def _open_autopsy_fd(self) -> None:
        """Pre-open a raw fd at boot so the redline dump never needs to
        allocate/open a file when memory is already exhausted."""
        if getattr(self, "_autopsy_fd", None) is not None:
            return
        self._autopsy_fd = None
        try:
            path = self._session_dir / "pre_oom_autopsy.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._autopsy_fd = os.open(
                str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644,
            )
        except Exception:  # noqa: BLE001 — never block boot
            self._autopsy_fd = None

    def _fire_death_rattle(self) -> None:
        """Allocation-free guaranteed dump (header bytes + faulthandler to
        the raw fd) then a best-effort RSS table. Thread-safe: only os.write
        + faulthandler. Off / no-fd -> no-op."""
        if not rg_death_rattle_enabled():
            return
        fd = getattr(self, "_autopsy_fd", None)
        if fd is None:
            return
        try:
            os.write(fd, _RG_RATTLE_HDR)                      # guaranteed
            os.write(fd, _RG_RATTLE_STACKS)
            import faulthandler
            faulthandler.dump_traceback(file=fd, all_threads=True)  # alloc-free
        except Exception:  # noqa: BLE001
            pass
        try:                                                  # best-effort tier
            os.write(fd, _RG_RATTLE_RSS)
            import psutil
            me = psutil.Process()
            for p in [me] + me.children(recursive=True):
                try:
                    rss_mb = int(p.memory_info().rss / 1048576)
                    line = (str(p.pid) + " " + str(rss_mb) + "MB "
                            + (p.name() or "?") + "\n")
                    os.write(fd, line.encode("ascii", "replace"))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        try:
            os.write(fd, _RG_RATTLE_FTR)
            os.fsync(fd)
        except Exception:  # noqa: BLE001
            pass
```

- [ ] **Step 5: Pre-open the fd early in `run()`** — at the top of `BattleTestHarness.run()` (`:785`), after `self._session_dir` is known, add:

```python
        # Resource Governor: pre-open the autopsy fd at the very start of
        # boot so a later redline dump is allocation-free.
        try:
            self._open_autopsy_fd()
        except Exception:  # noqa: BLE001
            pass
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/battle_test/harness.py tests/battle_test/test_resource_governor_watchdog.py
git commit -m "feat(harness): allocation-free pre-OOM Death Rattle via pre-opened fd (default-OFF)"
```

---

## Task 6: Wire the Death Rattle into the redline + cap-fire + backstop

**Files:**
- Modify: `backend/core/ouroboros/battle_test/harness.py:6470-6516` (cap-fire), `:6554+` (redline check in async loop), `:6610-6638` (thread backstop)
- Test: `tests/battle_test/test_resource_governor_watchdog.py` (append)

**Interfaces:**
- Consumes: `_fire_death_rattle` (Task 5), `rg_redline_free_pct` (Task 5), `get_default_gate().probe()`.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_cap_fire_dumps_before_oracle_checkpoint(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", "true")
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._stop_reason = "unknown"
    h._started_at = 0.0
    h._process_memory_event = asyncio.Event()
    order = []
    h._open_autopsy_fd()
    orig = h._fire_death_rattle
    h._fire_death_rattle = lambda: (order.append("rattle"), orig())[1]

    async def fake_ckpt():
        order.append("oracle")

    h._checkpoint_oracle_best_effort = fake_ckpt
    asyncio.run(h._fire_process_memory_cap(99999.0, 1.0))
    assert order[0] == "rattle"            # rattle BEFORE oracle checkpoint
    assert "oracle" in order
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py::test_cap_fire_dumps_before_oracle_checkpoint -q`
Expected: FAIL — rattle never called in cap-fire.

- [ ] **Step 3: Call the rattle first in `_fire_process_memory_cap`** — insert at the very top of the method body (`:6473`, before the `if self._stop_reason ...`):

```python
        # Resource Governor: guaranteed allocation-free autopsy FIRST —
        # before the oracle checkpoint (which allocates and can hang under
        # OOM). No-op when the flag is off.
        self._fire_death_rattle()
```

- [ ] **Step 4: Add the redline fast-trip in the async loop** — after the adaptive-interval block from Task 4 (Task 4 Step 3), add:

```python
            # Redline fast-trip: system free% below the redline fires the
            # rattle+stop BEFORE the RSS cap (catches a fast swap-storm).
            if rg_death_rattle_enabled():
                try:
                    from backend.core.ouroboros.governance.memory_pressure_gate import (  # noqa: E501
                        get_default_gate as _rg_gate2,
                    )
                    _probe = _rg_gate2().probe()
                    if _probe.ok and _probe.free_pct < rg_redline_free_pct():
                        logger.warning(
                            "[ResourceGovernor] REDLINE free=%.1f%% < %.1f%% "
                            "— firing Death Rattle + graceful stop.",
                            _probe.free_pct, rg_redline_free_pct(),
                        )
                        await self._fire_process_memory_cap(rss_mb, cap_mb)
                        return
                except Exception:  # noqa: BLE001
                    pass
```

- [ ] **Step 5: Call the rattle in the thread backstop** — in `_run()` inside `_start_process_memory_hard_deadline_thread`, immediately after the `if rss_mb is None or rss_mb < cap_mb: continue` guard (`:6613`), add:

```python
                self._fire_death_rattle()  # alloc-free; safe off-loop thread
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/battle_test/harness.py tests/battle_test/test_resource_governor_watchdog.py
git commit -m "feat(harness): wire Death Rattle into cap-fire (before oracle), redline trip, backstop"
```

---

## Task 7: Pressure-locked, jittered sensor poll-loop activation

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py:1030-1031` (replace the bare start loop) + module-level env readers
- Test: `tests/governance/test_resource_governor_stagger.py`

**Interfaces:**
- Produces: module fns `rg_stagger_enabled() -> bool`, `_rg_stagger_params() -> tuple` (base_s, jitter_s, hold_poll_s, hold_max_s); method `_gated_stagger_activate(self, sensors) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_resource_governor_stagger.py
from __future__ import annotations
import asyncio
import backend.core.ouroboros.governance.intake.intake_layer_service as ILS


class _FakeSensor:
    def __init__(self, name): self.name = name; self.started = False
    async def start(self): self.started = True


def test_off_path_starts_all_sequentially(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_ENABLED", raising=False)
    svc = ILS.IntakeLayerService.__new__(ILS.IntakeLayerService)
    sensors = [_FakeSensor("a"), _FakeSensor("b")]
    asyncio.run(svc._gated_stagger_activate(sensors))
    assert all(s.started for s in sensors)


def test_high_pressure_holds_then_ignites(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_BASE_MS", "0")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_JITTER_MS", "0")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_HOLD_POLL_S", "0.01")
    from backend.core.ouroboros.governance import memory_pressure_gate as mpg
    levels = iter([mpg.PressureLevel.HIGH, mpg.PressureLevel.HIGH, mpg.PressureLevel.OK])
    monkeypatch.setattr(mpg.MemoryPressureGate, "pressure",
                        lambda self: next(levels, mpg.PressureLevel.OK))
    svc = ILS.IntakeLayerService.__new__(ILS.IntakeLayerService)
    s = _FakeSensor("x")
    asyncio.run(svc._gated_stagger_activate([s]))
    assert s.started   # held during HIGH, ignited once it subsided
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_resource_governor_stagger.py -q`
Expected: FAIL — `_gated_stagger_activate` undefined.

- [ ] **Step 3: Add env readers** (module level in `intake_layer_service.py`, near the top imports)

```python
def rg_stagger_enabled() -> bool:
    import os
    return os.environ.get(
        "JARVIS_RESOURCE_GOVERNOR_STAGGER_ENABLED", "false",
    ).strip().lower() in ("1", "true", "yes")


def _rg_stagger_params():
    import os
    def _f(name, default):
        raw = os.environ.get(name, "").strip()
        try:
            return float(raw) if raw else default
        except ValueError:
            return default
    return (
        _f("JARVIS_RESOURCE_GOVERNOR_STAGGER_BASE_MS", 250.0) / 1000.0,
        _f("JARVIS_RESOURCE_GOVERNOR_STAGGER_JITTER_MS", 250.0) / 1000.0,
        _f("JARVIS_RESOURCE_GOVERNOR_STAGGER_HOLD_POLL_S", 0.5),
        _f("JARVIS_RESOURCE_GOVERNOR_STAGGER_HOLD_MAX_S", 60.0),
    )
```

- [ ] **Step 4: Add the method** (inside `IntakeLayerService`, near the start method)

```python
    async def _gated_stagger_activate(self, sensors) -> None:
        """Ignite sensor poll-loops staggered + pressure-locked. Each sensor
        awaits the MemoryPressureGate: HIGH/CRITICAL -> holding pattern until
        pressure subsides to WARN/OK, then ignite with jittered spacing to
        flatten the boot curve. Direction-safe: sensors PULL from the gate
        (the authority invariant holds). Off -> legacy sequential loop."""
        if not rg_stagger_enabled():
            for sensor in sensors:
                await sensor.start()
            return
        import asyncio as _a
        import random as _r
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            get_default_gate as _gate, PressureLevel as _PL,
        )
        base_s, jitter_s, hold_poll_s, hold_max_s = _rg_stagger_params()
        gate = _gate()
        for sensor in sensors:
            waited = 0.0
            while True:
                try:
                    lvl = gate.pressure()
                except Exception:  # noqa: BLE001
                    lvl = _PL.OK
                if lvl in (_PL.OK, _PL.WARN):
                    break
                if waited >= hold_max_s:
                    logger.warning(
                        "[ResourceGovernor] stagger hold timeout %.0fs — "
                        "igniting %s under pressure=%s (escape hatch).",
                        waited, getattr(sensor, "name", "?"), lvl,
                    )
                    break
                await _a.sleep(hold_poll_s)
                waited += hold_poll_s
            await sensor.start()
            await _a.sleep(base_s + _r.random() * jitter_s)
```

- [ ] **Step 5: Replace the bare loop at `:1030-1031`**

```python
        await self._gated_stagger_activate(self._sensors)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_resource_governor_stagger.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/governance/intake/intake_layer_service.py tests/governance/test_resource_governor_stagger.py
git commit -m "feat(intake): pressure-locked jittered sensor activation (default-OFF, legacy loop preserved)"
```

---

## Task 8: macOS-native local resource streamer

**Files:**
- Create: `scripts/resource_blackbox_local.py`
- Test: `tests/battle_test/test_resource_blackbox_local.py`

**Interfaces:**
- Produces: `sample() -> dict` (keys: `rss_tree_mb`, `free_pct`, `cpu_pct`, `ctx_rate`, `swap_used_mb`, `swap_pageouts`, `disk_free_pct`, `ts`); CLI `python3 scripts/resource_blackbox_local.py [--interval 1.0] [--log PATH]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/battle_test/test_resource_blackbox_local.py
from __future__ import annotations
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location(
    "rbl", pathlib.Path("scripts/resource_blackbox_local.py"))
rbl = importlib.util.module_from_spec(spec); spec.loader.exec_module(rbl)


def test_sample_has_required_keys():
    s = rbl.sample()
    for k in ("rss_tree_mb", "free_pct", "cpu_pct", "ctx_rate",
              "swap_used_mb", "disk_free_pct", "ts"):
        assert k in s
    assert isinstance(s["ts"], float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/battle_test/test_resource_blackbox_local.py -q`
Expected: FAIL — file does not exist.

- [ ] **Step 3: Create the streamer**

```python
# scripts/resource_blackbox_local.py
"""macOS-native local resource black-box for the omni-soak diagnostic.

Streams the death curve (RSS / free% / cpu / ctx-rate / swap / disk) to the
operator's terminal + a tee log, every ~1s. Avoids psutil.swap_memory()
(raises OSError on macOS) — uses native `sysctl vm.swapusage` + `vm_stat`.
"""
from __future__ import annotations
import argparse, os, subprocess, sys, time

_LAST_CTX = {"n": None, "ts": None}


def _vm_swapusage():
    """(used_mb, pageouts) from native macOS `sysctl vm.swapusage` + vm_stat."""
    used_mb, pageouts = 0.0, 0
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "vm.swapusage"], text=True, timeout=2)
        # e.g. "total = 2048.00M  used = 512.00M  free = 1536.00M ..."
        for tok in out.replace("=", " ").split():
            if tok.endswith("M"):
                pass
        parts = out.split("used")
        if len(parts) > 1:
            used_mb = float(parts[1].split("=")[1].split("M")[0].strip())
    except Exception:
        pass
    try:
        vms = subprocess.check_output(["vm_stat"], text=True, timeout=2)
        for line in vms.splitlines():
            if "Pageouts" in line:
                pageouts = int(line.split(":")[1].strip().rstrip("."))
    except Exception:
        pass
    return used_mb, pageouts


def _free_pct():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.available / vm.total * 100.0, 1)
    except Exception:
        return -1.0


def _rss_tree_mb():
    try:
        from backend.core.ouroboros.governance.process_tree_probe import (
            probe_process_tree_rss_mb)
        v = probe_process_tree_rss_mb()
        return round(v, 1) if v else -1.0
    except Exception:
        return -1.0


def _ctx_rate():
    try:
        import psutil
        now = time.monotonic()
        n = int(psutil.cpu_stats().ctx_switches)
        prev_n, prev_ts = _LAST_CTX["n"], _LAST_CTX["ts"]
        _LAST_CTX["n"], _LAST_CTX["ts"] = n, now
        if prev_n is None or now <= (prev_ts or now):
            return 0.0
        return round((n - prev_n) / (now - prev_ts), 1)
    except Exception:
        return -1.0


def _cpu_pct():
    try:
        import psutil
        return psutil.cpu_percent(interval=None)
    except Exception:
        return -1.0


def _disk_free_pct():
    try:
        st = os.statvfs(".")
        return round(st.f_bavail / st.f_blocks * 100.0, 1)
    except Exception:
        return -1.0


def sample() -> dict:
    used_mb, pageouts = _vm_swapusage()
    return {
        "ts": time.time(),
        "rss_tree_mb": _rss_tree_mb(),
        "free_pct": _free_pct(),
        "cpu_pct": _cpu_pct(),
        "ctx_rate": _ctx_rate(),
        "swap_used_mb": used_mb,
        "swap_pageouts": pageouts,
        "disk_free_pct": _disk_free_pct(),
    }


def _fmt(s: dict) -> str:
    return ("rss={rss_tree_mb}MB free={free_pct}% cpu={cpu_pct}% "
            "ctx={ctx_rate}/s swap={swap_used_mb}MB pageouts={swap_pageouts} "
            "disk_free={disk_free_pct}%").format(**s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--log", default="logs/resource_blackbox_local.log")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
    sample()  # seed ctx baseline
    time.sleep(min(0.5, args.interval))
    with open(args.log, "a") as fh:
        while True:
            line = _fmt(sample())
            sys.stdout.write(line + "\n"); sys.stdout.flush()
            fh.write(line + "\n"); fh.flush()
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/battle_test/test_resource_blackbox_local.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/resource_blackbox_local.py tests/battle_test/test_resource_blackbox_local.py
git commit -m "feat(diag): macOS-native local resource black-box streamer (sysctl/vm_stat, no psutil.swap)"
```

---

## Task 9: Full regression sweep + omni env flags

**Files:**
- Modify: `deploy/ouroboros_omni_prod.env` (append governor flags, commented OFF)

- [ ] **Step 1: Run the full new-test suite**

Run: `python3 -m pytest tests/governance/test_resource_governor_cpu_dim.py tests/governance/test_resource_governor_stagger.py tests/battle_test/test_resource_governor_watchdog.py tests/battle_test/test_resource_blackbox_local.py -q`
Expected: PASS (all).

- [ ] **Step 2: Run the existing gate + watchdog regression to prove OFF parity**

Run: `python3 -m pytest tests/governance/ -k "memory_pressure or governor" -q`
Expected: PASS — no pre-existing tests broken (every new path is flag-gated OFF).

- [ ] **Step 3: Append the flags to the omni env (commented until the local proof)**

```bash
# --- Resource Governor / Anti-Wedge (flip ON only after local baseline proof) ---
# export JARVIS_RESOURCE_GOVERNOR_ENABLED=1
# export JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED=1
# export JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED=1
# export JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED=1
# export JARVIS_RESOURCE_GOVERNOR_STAGGER_ENABLED=1
# export JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED=1
```

- [ ] **Step 4: Commit**

```bash
git add deploy/ouroboros_omni_prod.env
git commit -m "chore(omni): stage Resource Governor flags (commented OFF pending local proof)"
```

---

## Task 10: Local diagnostic proof (operator-run, not CI)

This is the protocol from the spec — run by the operator on the 16GB Mac, not an automated test.

- [ ] **Step 1: Baseline (throttle OFF, Death Rattle ON for capture)**

In terminal A: `python3 scripts/resource_blackbox_local.py --interval 1.0`
In terminal B (governor OFF, rattle ON to guarantee capture):

```bash
JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED=1 \
  python3 scripts/ouroboros_battle_test.py --headless --max-wall-seconds 600 -v
```

Observe terminal A's curve at the wedge point + read `.../pre_oom_autopsy.log`. **Confirm the cause** (RAM spike vs ctx/cpu thrash vs disk). Record it.

- [ ] **Step 2: Proof (throttle ON)**

```bash
JARVIS_RESOURCE_GOVERNOR_ENABLED=1 \
JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED=1 \
JARVIS_RESOURCE_GOVERNOR_ADAPTIVE_POLLING_ENABLED=1 \
JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED=1 \
JARVIS_RESOURCE_GOVERNOR_STAGGER_ENABLED=1 \
JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED=1 \
  python3 scripts/ouroboros_battle_test.py --headless --max-wall-seconds 600 -v
```

**Success criterion:** terminal A shows the spike flattened (no redline) and the soak proceeds past the prior wedge point. If it still wedges → read the rattle, identify the resource, add that dimension (same pattern) — no redesign.

- [ ] **Step 3: Record the result** in `memory/` (curve before/after, confirmed cause, whether flattened).

---

## Self-Review

- **Spec coverage:** Piece 1 → Tasks 1-2; Piece 2 → Tasks 3-4; Piece 3 → Tasks 5-6; Piece 4 → Task 7; Piece 5 → Task 8; protocol → Task 10; env staging + regression → Task 9. All spec sections covered.
- **Placeholder scan:** no TBD/TODO; every code step has complete code.
- **Type consistency:** `CpuCtxSample`, `_cpu_ctx_dim` (returns `(level, cpu_pct, ctx_rate)`), `_resolve_adaptive_pm_interval`, `rg_poll_interval_for`, `rg_backstop_interval_s`, `rg_death_rattle_enabled`, `rg_redline_free_pct`, `_fire_death_rattle`, `_open_autopsy_fd`, `_gated_stagger_activate`, `sample()` — names consistent across all tasks.
- **Constraint check:** every modified path early-returns to legacy on flag-off; allocation-free guaranteed path uses only bytes-literals + os.write + faulthandler; immune backstop uses a fixed floor; ctx trigger is a rate-vs-EWMA (no magic N); authority invariant preserved (sensors pull).
