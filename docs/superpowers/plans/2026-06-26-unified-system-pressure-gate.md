# Unified System Pressure Gate — Disk I/O & Capacity Dimension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing Resource Governor with a **Disk I/O + Capacity dimension** so disk-fill / IOPS-thrash (massive ephemeral swarm worktrees filling the e2-standard-8 boot disk) throttles DAG fan-out and fires the pre-OOM Death Rattle before a `No space left on device` kernel panic — woven into the existing gate, zero duplication.

**Architecture:** Add a fourth advisory dimension (`_disk_dim`) to `MemoryPressureGate` mirroring the existing `_cpu_ctx_dim`: capacity via `disk_usage` free-% + absolute-GB floor, IOPS-thrash via `disk_io_counters` bytes/sec rate vs a rolling EWMA baseline (the exact ctx-switch-rate pattern). Compose into **both** `pressure()` and `can_fanout()` via `_strictest`. The disk-CRITICAL redline rides the **already-built** `pressure()==CRITICAL` death-rattle trip (final-review fix) — so disk-full halts gracefully with the same allocation-free autopsy.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), psutil (`disk_usage` + `disk_io_counters`), `os.statvfs` fallback, pytest.

## Global Constraints

- **Python 3.9+**; `from __future__ import annotations` in any new file.
- **No hardcoded magic numbers** — every threshold/interval is an env var with a sane default via the gate's existing `_env_bool/_env_float/_env_int`.
- **Default-OFF, byte-identical-when-off** — gated by the umbrella `JARVIS_RESOURCE_GOVERNOR_ENABLED` (OR) plus a per-piece `JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED`; `_disk_dim()` returns `(OK, None, None)` when off so `_strictest` is a no-op and `pressure()`/`can_fanout()` are byte-identical. Per-piece git-HEAD parity test.
- **Authority invariant** — `memory_pressure_gate.py` imports no scheduler/sensor/orchestrator. Consumers pull.
- **Fail-open** — any probe error → `(OK, None, None)`; the dimension only ever *advises* a clamp. The ProcessMemoryWatchdog + disk redline remain the hard stops.
- **IOPS must be None-safe** — `psutil.disk_io_counters()` returns `None` in many VM/container contexts (verified it works on the dev Mac, but the Linux node may differ). When `io_bytes is None`, the IOPS sub-signal is skipped and only the capacity sub-signal contributes.
- **Verified facts:** on this macOS, `psutil.disk_usage(".")` works (`.percent` is USED%), `psutil.disk_io_counters()` works (cumulative `read_bytes`/`write_bytes`), `os.statvfs` works. `disk_io_counters()` can be `None` elsewhere → handle it.
- **No rename / no API break** — keep the class `MemoryPressureGate` (it has many live callers: subagent_scheduler, parallel_dispatch, oracle, reactor_daemon_supervisor, REPL, SSE, 13 test files). Add a module-level alias `SystemPressureGate = MemoryPressureGate` for the "Unified System Pressure Gate" semantics without churn.

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `backend/core/ouroboros/governance/memory_pressure_gate.py` | `_disk_dim` + capacity/IOPS sub-signals + env readers + state; compose into `pressure()` & `can_fanout()`; `SystemPressureGate` alias | Modify |
| `backend/core/ouroboros/battle_test/harness.py` | Disk-specific redline label + absolute-GB redline; disk-free line in the Death-Rattle best-effort autopsy | Modify |
| `scripts/resource_blackbox_local.py` | Add disk IO-rate to the stream (already has `disk_free_pct`) | Modify |
| `scripts/run_wedge_diagnostics.py` | Parse peak IO-rate + min disk-free; add to the verdict matrix | Modify |
| `tests/governance/test_resource_governor_disk_dim.py` | Disk dimension unit tests | Create |
| `tests/battle_test/test_resource_governor_watchdog.py` | Disk redline test (append) | Modify |
| `deploy/ouroboros_omni_prod.env` | Add disk flags (commented, under the umbrella) | Modify |

---

## Task D1: Disk dimension in `MemoryPressureGate` (capacity + IOPS), composed into pressure() & can_fanout()

**Files:**
- Modify: `memory_pressure_gate.py` — env readers (near `cpu_dim_enabled` block), `DiskSample` + `_sample_disk` (near `CpuCtxSample`), gate `__init__` state, `_disk_dim` + `_disk_capacity_level` (after `_cpu_ctx_dim`), compose in `pressure()` and `can_fanout()`, add `SystemPressureGate` alias near `get_default_gate`.
- Test: `tests/governance/test_resource_governor_disk_dim.py`

**Interfaces:**
- Produces: `disk_dim_enabled()`, `disk_warn_free_pct()/disk_high_free_pct()/disk_critical_free_pct()`, `disk_critical_free_gb()` (None=unset), `disk_io_spike_mult()`, `disk_io_baseline_halflife_s()`, `disk_watch_path()`; `DiskSample(free_pct, free_gb, io_bytes: Optional[int], ts, ok=True)`; `_sample_disk() -> DiskSample`; `_disk_dim(self) -> Tuple[PressureLevel, Optional[float], Optional[float]]` returning `(level, free_pct, io_rate)`; `_disk_capacity_level(self, free_pct, free_gb) -> PressureLevel`; module alias `SystemPressureGate`.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_resource_governor_disk_dim.py
from __future__ import annotations
import backend.core.ouroboros.governance.memory_pressure_gate as mpg


def _gate(disk_samples, free_probe_pct=60.0):
    it = iter(disk_samples)
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=free_probe_pct, total_bytes=1, available_bytes=1, source="test"))
    g._disk_sampler = lambda: next(it)
    return g


def test_capacity_critical_below_free_pct(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    D = mpg.DiskSample
    g = _gate([D(free_pct=3.0, free_gb=40.0, io_bytes=None, ts=1.0)])
    assert g._disk_dim()[0] == mpg.PressureLevel.CRITICAL   # 3% < 5% default


def test_capacity_gb_floor_overrides_pct(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_CRITICAL_FREE_GB", "10")
    D = mpg.DiskSample
    # 50% free but only 8GB left → CRITICAL via the absolute floor
    g = _gate([D(free_pct=50.0, free_gb=8.0, io_bytes=None, ts=1.0)])
    assert g._disk_dim()[0] == mpg.PressureLevel.CRITICAL


def test_iops_spike_declares_critical(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_IO_SPIKE_MULT", "3.0")
    D = mpg.DiskSample
    # plenty of capacity (free 80%); IO rate seeds then spikes 10x
    g = _gate([
        D(free_pct=80.0, free_gb=400.0, io_bytes=0,         ts=0.0),
        D(free_pct=80.0, free_gb=400.0, io_bytes=1_000_000, ts=1.0),   # baseline 1MB/s
        D(free_pct=80.0, free_gb=400.0, io_bytes=21_000_000, ts=2.0),  # 20MB/s spike
    ])
    assert g._disk_dim()[0] == mpg.PressureLevel.OK    # seed
    assert g._disk_dim()[0] == mpg.PressureLevel.OK    # baseline
    assert g._disk_dim()[0] == mpg.PressureLevel.CRITICAL  # spike


def test_iops_none_safe_capacity_still_works(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    D = mpg.DiskSample
    g = _gate([D(free_pct=80.0, free_gb=400.0, io_bytes=None, ts=1.0)])
    assert g._disk_dim()[0] == mpg.PressureLevel.OK   # no IO counters → no crash


def test_disk_dim_off_is_inert(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_ENABLED", raising=False)
    D = mpg.DiskSample
    g = _gate([D(free_pct=0.5, free_gb=1.0, io_bytes=10**12, ts=1.0)])
    assert g._disk_dim() == (mpg.PressureLevel.OK, None, None)


def test_pressure_and_canfanout_compose_disk(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    D = mpg.DiskSample
    g = _gate([D(free_pct=2.0, free_gb=20.0, io_bytes=None, ts=1.0)] * 4)
    assert g.pressure() == mpg.PressureLevel.CRITICAL
    dec = g.can_fanout(8)
    assert dec.level == mpg.PressureLevel.CRITICAL
    assert dec.n_allowed <= 1   # clamped at CRITICAL


def test_system_pressure_gate_alias():
    assert mpg.SystemPressureGate is mpg.MemoryPressureGate
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_resource_governor_disk_dim.py -q`
Expected: FAIL — `DiskSample` / `_disk_dim` / `SystemPressureGate` undefined.

- [ ] **Step 3: Add env readers** (after the cpu/ctx env-reader block)

```python
# Resource Governor — Disk I/O + Capacity dimension. Capacity free-% (and an
# optional absolute-GB floor) + IOPS-thrash RATE vs a rolling EWMA baseline
# (the ctx-switch-rate pattern). Off / umbrella-off -> _disk_dim returns OK ->
# strictest no-op -> byte-identical.
def disk_dim_enabled() -> bool:
    return resource_governor_master_enabled() or _env_bool(
        "JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", False)


def disk_watch_path() -> str:
    return os.environ.get("JARVIS_RESOURCE_GOVERNOR_DISK_PATH", ".").strip() or "."


def disk_warn_free_pct() -> float:
    return _env_float("JARVIS_RESOURCE_GOVERNOR_DISK_WARN_FREE_PCT", 20.0, minimum=0.5)


def disk_high_free_pct() -> float:
    return _env_float("JARVIS_RESOURCE_GOVERNOR_DISK_HIGH_FREE_PCT", 10.0, minimum=0.5)


def disk_critical_free_pct() -> float:
    return _env_float("JARVIS_RESOURCE_GOVERNOR_DISK_CRITICAL_FREE_PCT", 5.0, minimum=0.1)


def disk_critical_free_gb() -> Optional[float]:
    """Absolute free-GB floor (None=unset). Strictest wins vs the free-%."""
    raw = os.environ.get("JARVIS_RESOURCE_GOVERNOR_DISK_CRITICAL_FREE_GB", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def disk_io_spike_mult() -> float:
    return _env_float("JARVIS_RESOURCE_GOVERNOR_DISK_IO_SPIKE_MULT", 3.0, minimum=1.1)


def disk_io_baseline_halflife_s() -> float:
    return _env_float("JARVIS_RESOURCE_GOVERNOR_DISK_IO_BASELINE_HALFLIFE_S", 30.0, minimum=1.0)
```

- [ ] **Step 4: Add `DiskSample` + `_sample_disk`** (after `CpuCtxSample`/`_sample_cpu_ctx`)

```python
@dataclass(frozen=True)
class DiskSample:
    free_pct: float          # % free on the watched path
    free_gb: float
    io_bytes: Optional[int]  # cumulative read+write; None when counters absent
    ts: float
    ok: bool = True


def _sample_disk() -> DiskSample:
    import time as _t
    try:
        import psutil
        path = disk_watch_path()
        du = psutil.disk_usage(path)
        free_pct = max(0.0, 100.0 - float(du.percent))   # psutil.percent is USED%
        free_gb = float(du.free) / (1024.0 ** 3)
        io_bytes: Optional[int] = None
        try:
            io = psutil.disk_io_counters()
            if io is not None:
                io_bytes = int(io.read_bytes) + int(io.write_bytes)
        except Exception:  # noqa: BLE001 — None-safe: counters unavailable on VM
            io_bytes = None
        return DiskSample(free_pct=free_pct, free_gb=free_gb,
                          io_bytes=io_bytes, ts=_t.monotonic())
    except Exception:  # noqa: BLE001 — fail-open
        return DiskSample(free_pct=0.0, free_gb=0.0, io_bytes=None,
                          ts=_t.monotonic(), ok=False)
```

- [ ] **Step 5: Seed gate state in `__init__`** (after the cpu/ctx state)

```python
        self._disk_sampler: Callable[[], "DiskSample"] = _sample_disk
        self._last_disk_io: Optional[Tuple[int, float]] = None  # (io_bytes, ts)
        self._disk_io_baseline: Optional[float] = None          # EWMA bytes/s
```

- [ ] **Step 6: Add `_disk_capacity_level` + `_disk_dim`** (after `_cpu_ctx_dim`)

```python
    def _disk_capacity_level(self, free_pct: float, free_gb: float) -> PressureLevel:
        crit_gb = disk_critical_free_gb()
        if crit_gb is not None and free_gb < crit_gb:
            return PressureLevel.CRITICAL
        if free_pct < disk_critical_free_pct():
            return PressureLevel.CRITICAL
        if free_pct < disk_high_free_pct():
            return PressureLevel.HIGH
        if free_pct < disk_warn_free_pct():
            return PressureLevel.WARN
        return PressureLevel.OK

    def _disk_dim(
        self,
    ) -> Tuple[PressureLevel, Optional[float], Optional[float]]:
        """Advisory disk pressure: capacity (free-% + optional GB floor) and
        IOPS-thrash (bytes/sec RATE vs rolling EWMA baseline — the ctx-rate
        pattern). DISABLED/unavailable -> (OK, None, None) so the strictest
        compose is a no-op (byte-identical). Fail-open. IOPS is None-safe
        (counters absent on many VMs -> only capacity contributes)."""
        if not disk_dim_enabled():
            return PressureLevel.OK, None, None
        try:
            s = self._disk_sampler()
            if not s.ok:
                return PressureLevel.OK, None, None
            level = self._disk_capacity_level(s.free_pct, s.free_gb)
            io_rate: Optional[float] = None
            with self._lock:
                prev = self._last_disk_io
                if s.io_bytes is not None:
                    self._last_disk_io = (s.io_bytes, s.ts)
                    if prev is not None:
                        dt = s.ts - prev[1]
                        if dt > 0:
                            io_rate = max(0.0, (s.io_bytes - prev[0]) / dt)
                            if self._disk_io_baseline is None:
                                self._disk_io_baseline = io_rate
                            else:
                                if io_rate > self._disk_io_baseline * disk_io_spike_mult():
                                    level = _strictest(level, PressureLevel.CRITICAL)
                                alpha = 1.0 - 0.5 ** (dt / max(1e-6, disk_io_baseline_halflife_s()))
                                self._disk_io_baseline = (
                                    (1.0 - alpha) * self._disk_io_baseline + alpha * io_rate
                                )
            return level, s.free_pct, io_rate
        except Exception:  # noqa: BLE001 — fail-open
            logger.debug("[MemoryPressureGate] disk_dim raised", exc_info=True)
            return PressureLevel.OK, None, None
```

- [ ] **Step 7: Compose into `pressure()`** — extend the compose line:

```python
        proc_level, _rss, _cap = self._process_tree_dim()
        cpu_level, _cpu, _ctx = self._cpu_ctx_dim()
        disk_level, _dfree, _io = self._disk_dim()
        return _strictest(_strictest(_strictest(free_level, proc_level), cpu_level), disk_level)
```

- [ ] **Step 8: Compose into `can_fanout()`** — after the existing cpu compose line:

```python
        disk_level, _dfree2, _io2 = self._disk_dim()
        level = _strictest(level, disk_level)
```

- [ ] **Step 9: Add the alias** (near `get_default_gate`)

```python
# "Unified System Pressure Gate" semantics without an API-breaking rename:
# the gate now composes memory + process-tree + cpu/ctx + disk dimensions.
SystemPressureGate = MemoryPressureGate
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_resource_governor_disk_dim.py -q`
Expected: PASS (7 tests).

- [ ] **Step 11: Commit**

```bash
git add backend/core/ouroboros/governance/memory_pressure_gate.py tests/governance/test_resource_governor_disk_dim.py
git commit -m "feat(gate): disk I/O + capacity dimension (free%/GB floor + IOPS-rate EWMA), composed into pressure()+can_fanout(); SystemPressureGate alias (default-OFF)"
```

---

## Task D2: Disk-aware redline label + absolute-GB redline + autopsy disk line

**Files:**
- Modify: `harness.py` — the redline block in `_monitor_process_memory` (label disk vs RAM); `_fire_death_rattle` best-effort tier (add a disk-free line)
- Test: `tests/battle_test/test_resource_governor_watchdog.py` (append)

**Interfaces:**
- Consumes: `get_default_gate().pressure()` (now disk-aware), `_disk_dim`/`_sample_disk` via the gate.

**Note:** Because the final-review fix already trips the redline on `pressure()==CRITICAL`, and Task D1 composes disk into `pressure()`, a disk-free-below-5% condition *already* fires the Death Rattle. This task only (a) makes the stop_reason/log distinguish a disk redline from a RAM redline, and (b) adds disk free to the autopsy snapshot.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_redline_trips_on_disk_critical_with_disk_label(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DEATH_RATTLE_ENABLED", "1")
    from backend.core.ouroboros.governance import memory_pressure_gate as mpg
    # gate reports CRITICAL and a low disk free%; RAM probe is healthy
    monkeypatch.setattr(mpg.MemoryPressureGate, "pressure",
                        lambda self: mpg.PressureLevel.CRITICAL)
    monkeypatch.setattr(mpg.MemoryPressureGate, "probe",
                        lambda self: mpg.MemoryProbe(free_pct=60.0, total_bytes=1,
                                                     available_bytes=1, source="test"))
    monkeypatch.setattr(mpg.MemoryPressureGate, "_disk_dim",
                        lambda self: (mpg.PressureLevel.CRITICAL, 3.0, None))
    h = H.BattleTestHarness.__new__(H.BattleTestHarness)
    h._session_dir = tmp_path
    h._stop_reason = "unknown"
    h._started_at = 0.0
    h._process_memory_event = asyncio.Event()
    h._open_autopsy_fd()
    fired = {"cap": False}

    async def fake_cap(rss, cap):
        fired["cap"] = True
    h._fire_process_memory_cap = fake_cap

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(H.asyncio, "sleep", fake_sleep)
    h._probe_process_tree_rss_mb = lambda: 1.0
    try:
        asyncio.run(h._monitor_process_memory(1e9, 1e9, 15.0))
    except asyncio.CancelledError:
        pass
    assert fired["cap"] is True
    assert h._stop_reason == "resource_governor_disk_redline"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py::test_redline_trips_on_disk_critical_with_disk_label -q`
Expected: FAIL — stop_reason is `resource_governor_redline` (generic), not the disk label.

- [ ] **Step 3: Make the redline label disk vs RAM** — in the redline block, replace the trigger/label logic so a disk-CRITICAL is named distinctly:

```python
                    _gate = _rg_gate2()
                    _probe = _gate.probe()
                    _disk_level, _disk_free, _io = _gate._disk_dim()
                    _disk_crit = (_disk_level == PressureLevel.CRITICAL)
                    _crit = (_gate.pressure() == PressureLevel.CRITICAL)
                    if _disk_crit or _crit or (_probe.ok and _probe.free_pct < rg_redline_free_pct()):
                        self._stop_reason = (
                            "resource_governor_disk_redline" if _disk_crit
                            else "resource_governor_redline"
                        )
                        logger.warning(
                            "[ResourceGovernor] REDLINE disk_crit=%s crit=%s "
                            "free=%.1f%% disk_free=%s — Death Rattle + stop.",
                            _disk_crit, _crit, _probe.free_pct, _disk_free,
                        )
                        await self._fire_process_memory_cap(rss_mb, cap_mb)
                        return
```

- [ ] **Step 4: Add disk free to the Death-Rattle best-effort autopsy** — in `_fire_death_rattle`, inside the best-effort (psutil) tier, before/after the RSS table, add a disk line (best-effort, allocation tolerated in this tier):

```python
            try:
                import psutil as _ps
                _du = _ps.disk_usage(".")
                _dl = ("disk free%=" + str(round(100.0 - _du.percent, 1))
                       + " free_gb=" + str(round(_du.free / 1024**3, 1)) + "\n")
                os.write(fd, _dl.encode("ascii", "replace"))
            except Exception:
                pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/battle_test/test_resource_governor_watchdog.py -q`
Expected: PASS (existing + new).

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/battle_test/harness.py tests/battle_test/test_resource_governor_watchdog.py
git commit -m "feat(harness): disk-aware redline (distinct stop_reason) + disk free in Death Rattle autopsy"
```

---

## Task D3: Disk visibility in the local streamer + diagnostic verdict matrix

**Files:**
- Modify: `scripts/resource_blackbox_local.py` — add `io_rate` (read+write bytes/sec) to `sample()` (None-safe), include in the printed line
- Modify: `scripts/run_wedge_diagnostics.py` — parse `peak_io_rate` + `min_disk_free`, add both rows to the matrix
- Test: `tests/battle_test/test_resource_blackbox_local.py` + `tests/battle_test/test_run_wedge_diagnostics.py` (append)

- [ ] **Step 1: Write the failing tests (append)**

```python
# in test_resource_blackbox_local.py
def test_sample_has_disk_io_keys():
    s = rbl.sample()
    assert "io_rate" in s and "disk_free_pct" in s
```
```python
# in test_run_wedge_diagnostics.py
def test_peaks_capture_disk_io():
    lines = ["rss=10.0MB free=50.0% cpu=1.0% ctx=1.0/s swap=0.0MB pageouts=0 "
             "disk_free=4.0% io_rate=52428800.0/s"]
    p = rwd.parse_blackbox_peaks(lines)
    assert p["min_disk_free_pct"] == 4.0
    assert p["peak_io_rate"] == 52428800.0
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/battle_test/test_resource_blackbox_local.py::test_sample_has_disk_io_keys tests/battle_test/test_run_wedge_diagnostics.py::test_peaks_capture_disk_io -q`
Expected: FAIL — `io_rate` key / `min_disk_free_pct` missing.

- [ ] **Step 3: Add `io_rate` to the streamer** — in `resource_blackbox_local.py` add an `_io_rate()` helper (delta of `psutil.disk_io_counters().read_bytes+write_bytes`, None-safe → -1.0, same `_LAST_*` cached-sample idiom as `_ctx_rate`), include `"io_rate"` in `sample()` and `io_rate={io_rate}/s` in `_fmt`.

```python
_LAST_IO = {"n": None, "ts": None}


def _io_rate():
    try:
        import psutil
        now = time.monotonic()
        io = psutil.disk_io_counters()
        if io is None:
            return -1.0
        n = int(io.read_bytes) + int(io.write_bytes)
        prev_n, prev_ts = _LAST_IO["n"], _LAST_IO["ts"]
        _LAST_IO["n"], _LAST_IO["ts"] = n, now
        if prev_n is None or now <= (prev_ts or now):
            return 0.0
        return round((n - prev_n) / (now - prev_ts), 1)
    except Exception:
        return -1.0
```
Add `"io_rate": _io_rate()` to the `sample()` dict and `... disk_free={disk_free_pct}% io_rate={io_rate}/s` to `_fmt`.

- [ ] **Step 4: Parse disk peaks in the harness** — in `run_wedge_diagnostics.py` add regexes `_DISKFREE_RE = re.compile(r"disk_free=([-\d.]+)%")` and `_IORATE_RE = re.compile(r"io_rate=([-\d.]+)/s")`, init `peak["min_disk_free_pct"]=100.0` and `peak["peak_io_rate"]=0.0`, update them in the loop (ignore -1 sentinels), and add two rows to `render_matrix`:

```python
        row("min disk free (%)", p1.get("min_disk_free_pct", 100.0), p2.get("min_disk_free_pct", 100.0)),
        row("peak disk IO (B/s)", p1.get("peak_io_rate", 0.0), p2.get("peak_io_rate", 0.0)),
```

- [ ] **Step 5: Run the streamer + harness tests**

Run: `python3 -m pytest tests/battle_test/test_resource_blackbox_local.py tests/battle_test/test_run_wedge_diagnostics.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/resource_blackbox_local.py scripts/run_wedge_diagnostics.py tests/battle_test/test_resource_blackbox_local.py tests/battle_test/test_run_wedge_diagnostics.py
git commit -m "feat(diag): disk IO-rate + disk-free in streamer and wedge verdict matrix"
```

---

## Task D4: Regression sweep + omni env flags

**Files:** Modify `deploy/ouroboros_omni_prod.env`

- [ ] **Step 1: Full feature suite**

Run: `python3 -m pytest tests/governance/test_resource_governor_cpu_dim.py tests/governance/test_resource_governor_disk_dim.py tests/governance/test_resource_governor_stagger.py tests/battle_test/test_resource_governor_watchdog.py tests/battle_test/test_resource_blackbox_local.py tests/battle_test/test_run_wedge_diagnostics.py -q`
Expected: PASS (all).

- [ ] **Step 2: OFF-parity regression**

Run: `python3 -m pytest tests/governance/ -k "memory_pressure or governor" -q`
Expected: no NEW failures vs the 618 green baseline (pre-existing collection errors OK).

- [ ] **Step 3: Append disk flags (commented) to the omni env**

```bash
# --- Resource Governor disk dimension (rides the umbrella flag) ---
# export JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED=1
# export JARVIS_RESOURCE_GOVERNOR_DISK_CRITICAL_FREE_PCT=5
# export JARVIS_RESOURCE_GOVERNOR_DISK_CRITICAL_FREE_GB=5
# export JARVIS_RESOURCE_GOVERNOR_DISK_IO_SPIKE_MULT=3.0
```
(Note: `JARVIS_RESOURCE_GOVERNOR_ENABLED=1` already enables the disk dim via the umbrella; these are for granular control.)

- [ ] **Step 4: Commit**

```bash
git add deploy/ouroboros_omni_prod.env
git commit -m "chore(omni): stage Resource Governor disk-dimension flags (commented, under umbrella)"
```

---

## Self-Review

- **Spec coverage:** Disk I/O & Capacity dimension → D1 (capacity free%/GB + IOPS-rate EWMA, composed into pressure()+can_fanout()); IOPS thrash detection → D1 (bytes/sec rate vs EWMA, mirrors ctx); Ephemeral Space Redline → D2 (rides the existing CRITICAL redline; disk-distinct stop_reason + autopsy disk line; the 5%-free default maps to CRITICAL); Zero Duplication → no new gate class (alias only), reuses `_strictest`/redline/death-rattle. Streamer+matrix disk visibility → D3.
- **Placeholder scan:** none; every step has complete code.
- **Type consistency:** `DiskSample`, `_disk_dim`→`(level, free_pct, io_rate)`, `_disk_capacity_level`, `disk_*` env readers, `SystemPressureGate` alias, `_io_rate`/`io_rate` key, `min_disk_free_pct`/`peak_io_rate` — consistent across tasks.
- **Constraint check:** umbrella+sub OR gating; `_disk_dim` OK when off (byte-identical); fail-open; IOPS None-safe; no rename (alias); composed into BOTH pressure() and can_fanout() (the final-review lesson).
