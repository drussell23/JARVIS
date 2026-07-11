# Slice 4 — Run #15 Causal Set Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the four Run #14 blockers (zero session budget, budget-refusal-as-outage misclassification, dead SENSE leg, intake-WAL on-loop flock) so A1 Run #15 can traverse GENERATE → APPLY → `proven=true`.

**Architecture:** Config/economics fixes land at the isomorphic driver (fail-fast asserts, funded budget, noise-floor); code fixes land at the three diagnosed mechanisms — failure taxonomy in the generator's provider-failure path, off-loop scoped-target resolution in the SENSE lane (same `cooperative_fs_io.offload` substrate as Slice 3), and async intake-WAL variants on the Slice 3 canonical `async_flock_append_line` funnel. No thresholds, watchdogs, or LoopSink semantics change.

**Tech Stack:** Python 3.9+ asyncio, `cooperative_fs_io.offload`, `cross_process_jsonl.async_flock_append_line`, pytest.

**Evidence base:** `docs/superpowers/specs/2026-07-10-run14-autopsy.md` + `.superpowers/sdd/slice4-evidence-pack.md`. Session `bt-iso-1783659655`: 207 ops ingested → 0 APPLY; 26 GENERATE exhaustions all `SessionBudgetPreflightRefused` at `session_remaining=$0.0000`; refusals misclassified → 79-min `dw_global_outage` quarantine + 12 DLQ orphans; TestWatcher SENSE leg dead (7× full-suite `pytest tests/` SIGKILL @180s, scoped resolver wedged in on-loop `pathlib.resolve`); 1049/1065 pool submissions were doc_staleness noise; intake WAL sync flock = 214 LoopSink events (uir.py:1267 + :2173, one self-contending lock).

## Global Constraints

- **Mandate 1 (Root-Cause Only):** No changes to `JARVIS_LOOP_DEADMAN_TIMEOUT_S`, LoopSink thresholds, `ControlPlaneStarvation` thresholds, the ShutdownWatchdog 30s deadline, or the WallClock watchdog isolation invariant (Slice 47 — watchdogs stay blind to app state).
- **Mandate 2 (Architectural Purity):** All new offload boundaries live in `async def` code and route through `cooperative_fs_io.offload(fn, *args, cpu_bound=False)`. No new thread pools, no `time.sleep` on the loop.
- **Mandate 3 (DRY):** `cooperative_fs_io.offload` is the ONLY offload mechanism; async append surfaces route through the Slice 3 canonical `async_flock_append_line` (cross_process_jsonl.py). Sync logic single-sourced — async variants delegate serialization to the existing sync builders, never duplicate them.
- **Mandate 4 (Honest signals):** `WAL.append`'s bool is an HONEST durability signal (wal.py:53-72 docstring contract) — the async variant must preserve exact True/False semantics. Budget-refusal reclassification must not suppress or reword any existing log line consumed by runbooks; it adds a distinct terminal class.
- Every fail-soft path preserves existing semantics: offload failure → the same neutral value the sync path returns (`False`), NEVER an exception into the caller's coroutine.
- `JARVIS_COOPERATIVE_FS_IO_ENABLED=false` (master-off) must degrade every new path to synchronous-inline with byte-identical results — tests must prove it end-to-end.
- All new code: `from __future__ import annotations`, env-var config with sensible defaults, no hardcoded model names, `asyncio.wait_for` never `asyncio.timeout` (Python 3.9).
- Commit style: conventional commits ending `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. `git add` ONLY named files — never `-A`/`.`.
- Ignore space-numbered debris files (`* 2.py` etc.) — never import from or edit them.
- **Deferred out of this slice (do NOT implement):** shutdown-teardown interruptibility (autopsy fix #8 — pressure drops once T1 kills the doc_staleness flood and T4 off-loops the WAL; re-evaluate after Run #15), `mutation_gate.append_ledger` / `multi_prior` / `auto_action_router` conversions (evidence-pack #2-4, default-gated), `file_has_test_coverage` residual re-diagnosis (needs Run #15 cpu_ms data), LoopSink async-window attribution refinement.

---

### Task 1: Iso-run economics + noise floor (driver fail-fast + sensor master gate)

**Files:**
- Modify: `scripts/isomorphic_a1_local.py` (env-composition block — locate `env composed:` log site and the adversary-override dict near it)
- Modify: `backend/core/ouroboros/governance/intake/sensors/doc_staleness_sensor.py` (add master enable gate)
- Test: `tests/governance/test_slice4_iso_economics.py` (new)
- Test: `tests/governance/intake/test_doc_staleness_master_gate.py` (new)

**Interfaces:**
- Consumes: `session_budget_authority._ENV_S2_SESSION_BUDGET = "JARVIS_S2_SESSION_BUDGET_USD"` and `_ENV_BATTLE_COST_CAP = "OUROBOROS_BATTLE_COST_CAP"` (session_budget_authority.py:57-58; env fallback resolution at :546).
- Produces: `doc_staleness_sensor.sensor_enabled() -> bool` (module function, re-read at call time); driver env keys `OUROBOROS_BATTLE_COST_CAP`, `JARVIS_DOC_STALENESS_ENABLED=false`, plus boot-time budget assert.

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/intake/test_doc_staleness_master_gate.py`:

```python
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def sensor_module(monkeypatch):
    import backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor as m
    return m


def test_sensor_enabled_default_true(sensor_module, monkeypatch):
    monkeypatch.delenv("JARVIS_DOC_STALENESS_ENABLED", raising=False)
    assert sensor_module.sensor_enabled() is True


def test_sensor_enabled_false_pins_off(sensor_module, monkeypatch):
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "false")
    assert sensor_module.sensor_enabled() is False


@pytest.mark.asyncio
async def test_fs_event_ignored_when_disabled(sensor_module, monkeypatch):
    """Master-off: the fs.changed handler returns without scanning or
    emitting — the Run #14 flood lane (1049/1065 submissions) is closed."""
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "false")
    sensor = sensor_module.DocStalenessSensor.__new__(sensor_module.DocStalenessSensor)
    called = {"scan": False}

    async def _boom(*a, **k):  # would only run if the gate leaked
        called["scan"] = True

    sensor.scan_once = _boom  # type: ignore[method-assign]

    class _Evt:
        topic = "fs.changed.modified"
        payload = {"path": "README.md"}

    await sensor._on_fs_event(_Evt())
    assert called["scan"] is False


@pytest.mark.asyncio
async def test_scan_once_short_circuits_when_disabled(sensor_module, monkeypatch):
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "false")
    sensor = sensor_module.DocStalenessSensor.__new__(sensor_module.DocStalenessSensor)
    assert await sensor.scan_once() == []
```

Create `tests/governance/test_slice4_iso_economics.py`:

```python
from __future__ import annotations

import re
from pathlib import Path

DRIVER = Path("scripts/isomorphic_a1_local.py").read_text(encoding="utf-8")


def test_driver_composes_funded_cost_cap():
    """The composed child env must carry a non-zero OUROBOROS_BATTLE_COST_CAP.
    Run #14 booted the harness at budget=$0.00 -> every GENERATE preflight
    refused -> APPLY structurally unreachable."""
    assert "OUROBOROS_BATTLE_COST_CAP" in DRIVER
    assert "JARVIS_ISO_SESSION_BUDGET_USD" in DRIVER  # env-tunable, not hardcoded


def test_driver_has_zero_budget_failfast():
    """Driver must abort (not burn 83 min) if the effective budget resolves
    to 0. Assert the guard exists and references the autopsy failure class."""
    assert "budget_failfast" in DRIVER or "ZERO-BUDGET" in DRIVER


def test_driver_pins_doc_staleness_off():
    assert re.search(r"JARVIS_DOC_STALENESS_ENABLED[\"']?\s*[:=]\s*[\"']false", DRIVER)


def test_driver_asserts_offload_substrate_armed():
    """Evidence-pack #5: either cooperative-fs-io or posture wholesale-offload
    being off silently reintroduces on-loop scans. The driver must pin both."""
    assert re.search(r"JARVIS_COOPERATIVE_FS_IO_ENABLED[\"']?\s*[:=]\s*[\"']true", DRIVER)
    assert re.search(r"JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED[\"']?\s*[:=]\s*[\"']true", DRIVER)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intake/test_doc_staleness_master_gate.py tests/governance/test_slice4_iso_economics.py -q`
Expected: FAIL — `sensor_enabled` not defined; driver-source asserts fail.

- [ ] **Step 3: Implement the sensor master gate**

In `doc_staleness_sensor.py`, add beside `webhook_enabled()` (~line 59), following its exact re-read-at-call-time idiom:

```python
def sensor_enabled() -> bool:
    """Master enable for the DocStaleness sensor, re-read at call time.

    ``JARVIS_DOC_STALENESS_ENABLED`` (BOOL, default TRUE). Run #14 autopsy:
    this sensor produced 1049/1065 pool submissions via its ``fs.changed.*``
    subscription (reacting to the session's own file writes), saturating the
    6-worker pool and dominating the shutdown-wedge threads. Iso/A1 soaks pin
    this false; production default is unchanged.
    """
    return os.environ.get(
        "JARVIS_DOC_STALENESS_ENABLED", "true",
    ).strip().lower() not in ("false", "0", "no", "off")
```

Gate BOTH entry lanes (verify exact names by reading the file first; the handler is `_on_fs_event` subscribed at :187, the poll body is `scan_once` at :324):

```python
# top of _on_fs_event:
        if not sensor_enabled():
            return
# top of scan_once:
        if not sensor_enabled():
            return []
```

- [ ] **Step 4: Implement the driver economics block**

In `scripts/isomorphic_a1_local.py`, locate the env-composition dict that produces the `env composed: N keys total, adversary overrides applied` log line. Add (verbatim values; module-level constant near the other `_env_float` reads):

```python
_ISO_SESSION_BUDGET_USD: float = float(
    os.environ.get("JARVIS_ISO_SESSION_BUDGET_USD", "2.00")
)
```

and in the composed-overrides dict:

```python
        # Run #14 autopsy fix #1: the soak child booted with budget=$0.00 ->
        # every GENERATE died at SessionBudgetAuthority preflight
        # (est=$0.10 > remaining=$0.00) with Claude structurally disabled ->
        # APPLY unreachable. Fund the harness cost tracker; keep it bounded.
        "OUROBOROS_BATTLE_COST_CAP": str(_ISO_SESSION_BUDGET_USD),
        # Autopsy fix #3 / noise floor: 1049/1065 submissions were
        # doc_staleness fs.changed reactions to the session's own writes.
        "JARVIS_DOC_STALENESS_ENABLED": "false",
        # Evidence-pack #5: assert the offload substrate is armed — either
        # flag off silently reintroduces the on-loop scan class.
        "JARVIS_COOPERATIVE_FS_IO_ENABLED": "true",
        "JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED": "true",
```

Then add the zero-budget fail-fast IMMEDIATELY after the env dict is fully composed (before the soak child is spawned):

```python
    # ZERO-BUDGET fail-fast (budget_failfast): Run #14 burned 83 min against
    # a structurally-unpassable $0.00 wall. Abort ignition instead.
    _effective_cap = float(_soak_env.get("OUROBOROS_BATTLE_COST_CAP", "0") or "0")
    _s2_budget = float(_soak_env.get("JARVIS_S2_SESSION_BUDGET_USD", "0") or "0")
    if max(_effective_cap, _s2_budget) <= 0.0:
        print("[IsoA1] FATAL ZERO-BUDGET: composed env has no funded session "
              "budget (OUROBOROS_BATTLE_COST_CAP and JARVIS_S2_SESSION_BUDGET_USD "
              "both <= 0) — every GENERATE would preflight-refuse. Aborting.")
        return 2
```

(Adapt the local names `_soak_env`/return-code idiom to the driver's actual composition function — read it first; if composition happens in a helper returning the dict, put the guard at its single call site.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/intake/test_doc_staleness_master_gate.py tests/governance/test_slice4_iso_economics.py -q`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/doc_staleness_sensor.py scripts/isomorphic_a1_local.py tests/governance/intake/test_doc_staleness_master_gate.py tests/governance/test_slice4_iso_economics.py
git commit -m "fix(iso): funded session budget + zero-budget fail-fast + doc_staleness master gate (Slice 4 T1)"
```

---

### Task 2: Budget refusal is not a provider outage (failure taxonomy)

**Files:**
- Modify: `backend/core/ouroboros/governance/session_budget_authority.py` (add classifier helper)
- Modify: `backend/core/ouroboros/governance/race_triage.py` (dual-arm CONFIRMED path, ~:160)
- Modify: `backend/core/ouroboros/governance/candidate_generator.py` (quarantine trigger `upstream_quarantine:dw_global_outage` ~:5854; `[Immortal] ... QUEUE_ONLY` ~:5910)
- Test: `tests/governance/test_slice4_budget_refusal_taxonomy.py` (new)

**Interfaces:**
- Consumes: `SessionBudgetPreflightRefused` (session_budget_authority.py:77).
- Produces: `session_budget_authority.is_budget_refusal(exc: BaseException) -> bool` (walks `__cause__`/`__context__` chains); generator terminal cause string `fallback_skipped:budget_exhausted_non_transient`.

Run #14 evidence: 30 preflight refusals → 27 dual-arm blacklists → global DW quarantine 22:05→23:24 (79 min) → 12 DLQ orphans, plus unbounded `[Immortal]` backoff loops holding worker slots. A LOCAL config gate must not masquerade as a REMOTE provider outage (feedback_observability_over_silent_reroute: surface the real class).

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_slice4_budget_refusal_taxonomy.py`:

```python
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.session_budget_authority import (
    SessionBudgetPreflightRefused,
    is_budget_refusal,
)


def test_direct_refusal_detected():
    exc = SessionBudgetPreflightRefused(
        provider="doubleword", op_id="op-x", estimated_usd=0.10,
        effective_remaining_usd=0.0, session_remaining_usd=0.0,
    )
    assert is_budget_refusal(exc) is True


def test_wrapped_refusal_detected_via_cause_chain():
    inner = SessionBudgetPreflightRefused(
        provider="doubleword", op_id="op-x", estimated_usd=0.10,
        effective_remaining_usd=0.0, session_remaining_usd=0.0,
    )
    try:
        try:
            raise inner
        except SessionBudgetPreflightRefused as e:
            raise RuntimeError("transport wrapper") from e
    except RuntimeError as outer:
        assert is_budget_refusal(outer) is True


def test_ordinary_exception_not_refusal():
    assert is_budget_refusal(TimeoutError("provider timed out")) is False


def test_race_triage_skips_blacklist_on_budget_refusal():
    """Both arms failing on a LOCAL budget gate must not blacklist the model
    (Run #14: 27 spurious dual-arm blacklists -> global quarantine)."""
    from backend.core.ouroboros.governance import race_triage as rt
    refusal = SessionBudgetPreflightRefused(
        provider="doubleword", op_id="op-x", estimated_usd=0.10,
        effective_remaining_usd=0.0, session_remaining_usd=0.0,
    )
    # Exact entry: read race_triage.py's CONFIRMED path (~:160) and call its
    # public triage function with both arm exceptions = refusal; assert the
    # blacklist registry it writes is untouched. The test must construct the
    # real triage inputs (no mocking of race_triage internals) and assert on
    # the same registry `is_blacklisted_for_op` reads
    # (candidate_generator.py:5041 imports it).
    assert rt.is_budget_refusal_pair(refusal, refusal) is True
```

(NOTE to implementer: the 4th test pins the new `race_triage.is_budget_refusal_pair` seam; ALSO extend the existing dual-arm CONFIRMED test file for race_triage — find it via `grep -rl "dual-arm" tests/governance/` — with a case proving no blacklist write occurs when both arms are refusals.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_slice4_budget_refusal_taxonomy.py -q`
Expected: FAIL — `is_budget_refusal` not importable. (If `SessionBudgetPreflightRefused.__init__` uses different kwargs, read session_budget_authority.py:77-110 and match the real constructor — its message format string at :106 shows the fields.)

- [ ] **Step 3: Implement the classifier in session_budget_authority.py**

```python
def is_budget_refusal(exc: BaseException, *, _depth: int = 8) -> bool:
    """True iff *exc* is, or was caused by, a SessionBudgetPreflightRefused.

    Walks ``__cause__``/``__context__`` up to ``_depth`` hops so transport
    wrappers (hedge-race arms wrap provider errors) still classify. Budget
    refusal is a LOCAL config gate — Run #14 counted it as a provider failure,
    blacklisted both arms, and tripped a 79-minute global DW quarantine.
    """
    seen = 0
    cur: BaseException | None = exc
    while cur is not None and seen < _depth:
        if isinstance(cur, SessionBudgetPreflightRefused):
            return True
        cur = cur.__cause__ if cur.__cause__ is not None else cur.__context__
        seen += 1
    return False
```

- [ ] **Step 4: Wire the three consumption points**

(a) `race_triage.py` — read the function containing the `dual-arm failure CONFIRMED` log (~:160). Before the blacklist write, add:

```python
        from backend.core.ouroboros.governance.session_budget_authority import (
            is_budget_refusal,
        )
        if is_budget_refusal_pair(primary_exc, hedge_exc):
            logger.warning(
                "[RaceTriage] dual-arm failure is BUDGET-REFUSAL (local gate, "
                "not provider fault): model=%s — NOT blacklisting, NOT "
                "counting toward outage. op=%s", model_id, op_id,
            )
            return  # no blacklist write, no outage counter
```

with the module-level helper:

```python
def is_budget_refusal_pair(primary_exc: BaseException | None,
                           hedge_exc: BaseException | None) -> bool:
    """Both observed arms (missing arm = None counts as refusal-compatible)
    failed on the local session-budget gate."""
    from backend.core.ouroboros.governance.session_budget_authority import (
        is_budget_refusal,
    )
    arms = [e for e in (primary_exc, hedge_exc) if e is not None]
    return bool(arms) and all(is_budget_refusal(e) for e in arms)
```

(b) `candidate_generator.py` quarantine trigger (~:5854): read the block that emits `upstream_quarantine:dw_global_outage`; guard its entry so refusal-driven exhaustions do not feed the outage detector (the outage gradient must only count provider-shaped failures). Concretely, where the exhaustion cause is recorded, classify first:

```python
                    if is_budget_refusal(exc):
                        exhaustion_cause = "budget_exhausted_non_transient"
                    else:
                        exhaustion_cause = ...  # existing classification
```

and ensure the quarantine/outage accumulator only increments on the non-budget branch. Read the surrounding code to find the accumulator (it produced 23 `UPSTREAM QUARANTINE: global DW out` lines) — the guard belongs at the single point where a failed GENERATE feeds it.

(c) `[Immortal]` QUEUE_ONLY loop (~:5910): the re-attempt loop must terminate immediately when the recorded cause is `budget_exhausted_non_transient` (non-transient: retrying cannot succeed until an operator refunds the session):

```python
                    if exhaustion_cause == "budget_exhausted_non_transient":
                        logger.error(
                            "[Immortal] NON-TRANSIENT budget exhaustion — "
                            "terminating retry loop (fail-fast, op fails "
                            "visibly): op=%s", op_id,
                        )
                        break
```

- [ ] **Step 5: Run tests + the generator/triage guard suites**

Run: `python3 -m pytest tests/governance/test_slice4_budget_refusal_taxonomy.py tests/governance/test_epistemic_feedback*.py -q` (plus the race_triage/dual-arm suite found in Step 1's NOTE)
Expected: ALL PASS; pre-existing quarantine tests (real outages still quarantine) stay green.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/session_budget_authority.py backend/core/ouroboros/governance/race_triage.py backend/core/ouroboros/governance/candidate_generator.py tests/governance/test_slice4_budget_refusal_taxonomy.py
git commit -m "fix(providers): budget refusal is a local gate, not a provider outage (Slice 4 T2)"
```

---

### Task 3: SENSE leg — off-loop scoped-target resolution + event-primary poll derate

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py` (`_resolve_scoped_targets`, ~:836)
- Modify: `backend/core/ouroboros/governance/intent/test_watcher.py` (poll loop; interval env at :129, timeout env at :138)
- Test: `tests/governance/intake/test_slice4_sense_offload.py` (new)

**Interfaces:**
- Consumes: `cooperative_fs_io.offload(fn, /, *args, cpu_bound=False, **kwargs)` and `is_offload_error(result)`; `test_failure_sensor.fs_events_enabled()` (exists — the Gap #4 event-primary gate).
- Produces: `_resolve_scoped_targets` unchanged signature (`async def ... -> Optional[List[str]]`) but all blocking work (the `(repo_root / changed_rel_path).resolve()` at ~:836 AND `TestRunner.resolve_affected_tests`'s internal walk) executes in the offload pool; `TestWatcher._event_primary_derate() -> bool`.

Run #14 evidence: main thread tombstoned inside `_resolve_scoped_targets → pathlib.resolve → _joinrealpath` (83 min); the legacy `intent.test_watcher` swept the FULL `tests/` suite 7× (300s cadence, 180s SIGKILL each, `stdout_bytes=0`) — SENSE never fired. Root causes: (1) resolver blocks the loop; (2) under Gap #4 event-primary architecture, the legacy whole-suite poll is a redundant Strangler-Fig fallback that must derate when the event lane is armed, not compete with it.

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/intake/test_slice4_sense_offload.py`:

```python
from __future__ import annotations

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_resolve_scoped_targets_does_not_block_loop(tmp_path, monkeypatch):
    """The Run #14 tombstone: main thread wedged in pathlib.resolve inside
    _resolve_scoped_targets. Prove the loop keeps ticking (<250ms gaps)
    while resolution runs against a slow filesystem walk."""
    from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
        TestFailureSensor,
    )
    sensor = TestFailureSensor.__new__(TestFailureSensor)
    monkeypatch.setattr(sensor, "_repo_root", lambda: tmp_path, raising=False)

    real_resolve = __import__("pathlib").Path.resolve

    def slow_resolve(self, *a, **k):
        time.sleep(0.4)  # simulated deep _joinrealpath walk
        return real_resolve(self, *a, **k)

    monkeypatch.setattr("pathlib.Path.resolve", slow_resolve)

    gaps: list[float] = []

    async def ticker():
        prev = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    t = asyncio.ensure_future(ticker())
    try:
        await sensor._resolve_scoped_targets("backend/some/module.py")
    finally:
        t.cancel()
    assert gaps, "ticker never ran"
    assert max(gaps) < 0.25, f"loop starved: max gap {max(gaps):.3f}s"


@pytest.mark.asyncio
async def test_resolver_failure_degrades_to_none(monkeypatch, tmp_path):
    """Offload failure -> same neutral value the sync path returns (None),
    never an exception (Global Constraint: fail-soft parity)."""
    from backend.core.ouroboros.governance.intake.sensors import test_failure_sensor as tfs
    sensor = tfs.TestFailureSensor.__new__(tfs.TestFailureSensor)
    monkeypatch.setattr(sensor, "_repo_root", lambda: tmp_path, raising=False)

    async def broken_offload(fn, *a, **k):
        raise RuntimeError("executor down")

    monkeypatch.setattr(tfs, "_offload_fs", broken_offload, raising=False)
    assert await sensor._resolve_scoped_targets("backend/x.py") is None


def test_watcher_derates_when_event_lane_armed(monkeypatch):
    """Gap #4 Strangler Fig: with the event-primary TestFailure lane armed,
    the legacy poller must not run whole-suite sweeps."""
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TESTFAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.delenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", raising=False)
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is True


def test_watcher_polls_when_event_lane_off(monkeypatch):
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TESTFAILURE_FS_EVENTS_ENABLED", "false")
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is False


def test_watcher_escape_hatch(monkeypatch):
    """Operators can force legacy polling even under event-primary."""
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    monkeypatch.setenv("JARVIS_TESTFAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", "true")
    w = TestWatcher.__new__(TestWatcher)
    assert w._event_primary_derate() is False
```

(NOTE to implementer: before writing, `grep -n "fs_events_enabled" backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py` for the REAL env var name the event lane reads — the test above assumes `JARVIS_TESTFAILURE_FS_EVENTS_ENABLED`; use the actual name everywhere, brief-correction rules apply.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intake/test_slice4_sense_offload.py -q`
Expected: FAIL — loop-starve gap > 0.25s; `_offload_fs`/`_event_primary_derate` missing.

- [ ] **Step 3: Implement the sensor offload**

In `test_failure_sensor.py`, add the module-boundary offload helper (posture_observer._offload_signal idiom, the ONLY sanctioned fallback shape — Mandate 3):

```python
async def _offload_fs(fn, /, *args, **kwargs):
    """Route blocking FS work through the cooperative substrate; fail-soft
    to the caller's neutral value at the call site (never raises past it).
    Copied import-fault idiom from posture_observer._offload_signal."""
    try:
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            is_offload_error,
            offload,
        )
    except ImportError:
        import asyncio as _aio
        return await _aio.get_event_loop().run_in_executor(
            None, lambda: fn(*args, **kwargs)
        )
    result = await offload(fn, *args, cpu_bound=False, **kwargs)
    if is_offload_error(result):
        raise RuntimeError(f"offload failed: {result!r}")
    return result
```

Restructure `_resolve_scoped_targets` so every blocking step runs via `_offload_fs`: extract the current body's synchronous work (the `.resolve()` call, the TestRunner resolution — read `TestRunner.resolve_affected_tests`: if it is itself async but internally blocking, call its SYNC underlying mapper in the worker; if none exists, wrap the whole resolution closure) into a module-level sync function `_resolve_scoped_targets_sync(repo_root: Path, changed_rel_path: str) -> Optional[List[str]]` containing the EXISTING logic verbatim (primary mapper → mirror-dir → tier-3 package discovery), then:

```python
    async def _resolve_scoped_targets(self, changed_rel_path: str):
        if not changed_rel_path:
            return None
        try:
            return await _offload_fs(
                _resolve_scoped_targets_sync, self._repo_root(), changed_rel_path,
            )
        except Exception:  # noqa: BLE001 — resolver is best-effort (existing contract)
            return None
```

The docstring contract at :820-831 ("NEVER returns the whole tests/ directory implicitly", fail-safe to None) must survive verbatim — move it onto the sync function.

- [ ] **Step 4: Implement the watcher derate**

In `test_watcher.py`, add to `TestWatcher`:

```python
    def _event_primary_derate(self) -> bool:
        """True when the Gap #4 event-primary TestFailure lane is armed and
        the operator has not forced legacy polling. Run #14: the legacy
        whole-suite poll (300s cadence, 180s timeout) SIGKILLed 7/7 times
        under load and produced zero signals while starving the box the
        event lane needed."""
        from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
            fs_events_enabled,
        )
        forced = os.environ.get(
            "JARVIS_INTENT_POLL_WHEN_EVENT_PRIMARY", "false",
        ).strip().lower() in ("true", "1", "yes", "on")
        return fs_events_enabled() and not forced
```

and consult it at the top of each poll cycle (read the poll loop; insert before `run_pytest` is invoked):

```python
            if self._event_primary_derate():
                logger.debug(
                    "[TestWatcher] event-primary lane armed — skipping "
                    "legacy whole-suite poll (JARVIS_INTENT_POLL_WHEN_"
                    "EVENT_PRIMARY=true to force)."
                )
                continue  # honoring the existing interval sleep
```

- [ ] **Step 5: Run the tests + sensor guard suites**

Run: `python3 -m pytest tests/governance/intake/test_slice4_sense_offload.py tests/governance/intake/test_test_failure_sensor*.py tests/governance/test_posture_observer.py -q`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py backend/core/ouroboros/governance/intent/test_watcher.py tests/governance/intake/test_slice4_sense_offload.py
git commit -m "fix(sense): off-loop scoped-target resolution + event-primary poll derate (Slice 4 T3)"
```

---

### Task 4: Intake WAL async append (the dominant starvation mechanism)

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/wal.py` (add `append_async` / `update_status_async`)
- Modify: `backend/core/ouroboros/governance/intake/unified_intake_router.py:1267` (`self._wal.append(...)` in `_ingest_impl`) and `:2173` (`self._wal.update_status(..., "acked")` in `_dispatch_one`)
- Test: `tests/governance/intake/test_slice4_wal_async.py` (new)

**Interfaces:**
- Consumes: `cross_process_jsonl.async_flock_append_line(path, line, *, timeout_s=None) -> bool` (Slice 3 canonical; NEVER raises; resolves durable path at the funnel).
- Produces: `async def WAL.append_async(entry: WALEntry) -> bool` and `async def WAL.update_status_async(lease_id: str, status: str) -> None` — byte-identical records to their sync twins, same honest-durability bool, same `ValueError` on invalid status (raised BEFORE any await, sync-parity).

Run #14 evidence: 214 LoopSink `flock_append_line kind=sync` events; both call sites share `.jarvis/intake_wal.jsonl` under one flock and self-contend (the 1065 capacity-defer events each also re-park through this lock). This is the exact class Slice 3 killed for the decision trace — same medicine, same funnel.

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/intake/test_slice4_wal_async.py`:

```python
from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry


def _entry(lease="lse-1"):
    return WALEntry(
        lease_id=lease, envelope_dict={"k": "v"}, status="pending",
        ts_monotonic=time.monotonic(), ts_utc="2026-07-10T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_append_async_row_byte_equal_to_sync(tmp_path):
    """Mandate 3/4: async twin delegates to the same record builder —
    prove byte-equal rows (modulo the two timestamps, which the test pins)."""
    sync_wal = WAL(tmp_path / "sync.jsonl")
    async_wal = WAL(tmp_path / "async.jsonl")
    e = _entry()
    assert sync_wal.append(e) is True
    assert await async_wal.append_async(e) is True
    srow = json.loads((tmp_path / "sync.jsonl").read_text().splitlines()[0])
    arow = json.loads((tmp_path / "async.jsonl").read_text().splitlines()[0])
    assert srow == arow


@pytest.mark.asyncio
async def test_update_status_async_tombstone_parity(tmp_path):
    wal = WAL(tmp_path / "w.jsonl")
    wal.append(_entry())
    await wal.update_status_async("lse-1", "acked")
    rows = [json.loads(l) for l in (tmp_path / "w.jsonl").read_text().splitlines()]
    assert rows[-1]["_type"] == "status_update"
    assert rows[-1]["status"] == "acked"
    assert wal.pending_entries() == []


@pytest.mark.asyncio
async def test_update_status_async_invalid_status_raises_before_await(tmp_path):
    """Sync parity: ValueError on invalid status (wal.py:74-79 contract),
    raised synchronously (no partial write)."""
    wal = WAL(tmp_path / "w.jsonl")
    with pytest.raises(ValueError):
        await wal.update_status_async("lse-1", "not-a-status")
    assert not (tmp_path / "w.jsonl").exists() or (tmp_path / "w.jsonl").read_text() == ""


@pytest.mark.asyncio
async def test_append_async_does_not_block_loop_under_lock_contention(tmp_path):
    """The Run #14 mechanism: a held flock must park the WAIT in the pool,
    not on the loop. Hold the lock in a thread; assert loop tick gaps stay
    <250ms while append_async waits/fails honestly."""
    import fcntl
    import threading

    target = tmp_path / "w.jsonl"
    lock_path = target.with_suffix(target.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    release = threading.Timer(1.0, lambda: (fcntl.flock(fd, fcntl.LOCK_UN), fd.close()))
    release.start()

    wal = WAL(target)
    gaps: list[float] = []

    async def ticker():
        prev = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    t = asyncio.ensure_future(ticker())
    try:
        ok = await wal.append_async(_entry())
    finally:
        t.cancel()
        release.cancel()
    assert isinstance(ok, bool)  # honest bool either way
    assert max(gaps) < 0.25, f"loop starved: {max(gaps):.3f}s"


@pytest.mark.asyncio
async def test_master_off_degrades_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")
    wal = WAL(tmp_path / "w.jsonl")
    assert await wal.append_async(_entry()) is True
    row = json.loads((tmp_path / "w.jsonl").read_text().splitlines()[0])
    assert row["lease_id"] == "lse-1"


def test_uir_call_sites_use_async_variants():
    """AST-shape pin: the two Run #14 hot sites must await the async twins
    (fails if the conversion is reverted)."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    ).read_text(encoding="utf-8")
    assert "await self._wal.append_async(" in src
    assert 'await self._wal.update_status_async(envelope.lease_id, "acked")' in src
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intake/test_slice4_wal_async.py -q`
Expected: FAIL — `append_async` not defined. (If the flock sidecar naming differs from `<file>.lock`, read `_acquire_cross_process_lock` in cross_process_jsonl.py and fix the contention test's lock path to the real sidecar; the Slice 3 suite `tests/governance/test_async_flock_append.py` shows the working pattern — mirror it.)

- [ ] **Step 3: Implement the async twins in wal.py**

First extract the record-building from `append`/`update_status` so sync and async share ONE builder each (Mandate 3 — no duplicated serialization):

```python
    def _append_record(self, entry: WALEntry) -> Dict[str, Any]:
        return {
            "v": _WAL_VERSION,
            "lease_id": entry.lease_id,
            "envelope": entry.envelope_dict,
            "status": entry.status,
            "ts_monotonic": entry.ts_monotonic,
            "ts_utc": entry.ts_utc,
        }

    def _status_record(self, lease_id: str, status: str) -> Dict[str, Any]:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_TERMINAL_STATUSES)}, got {status!r}"
            )
        return {
            "v": _WAL_VERSION,
            "lease_id": lease_id,
            "status": status,
            "ts_monotonic": time.monotonic(),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "_type": "status_update",
        }
```

Rewire the existing sync methods through these builders (bodies otherwise unchanged), then add:

```python
    async def append_async(self, entry: WALEntry) -> bool:
        """Async twin of :meth:`append` — same record, same HONEST durability
        bool, but the flock acquire/write/fsync parks in the cooperative
        offload pool instead of on the event loop (Run #14: this call site
        was 214 LoopSink events / the dominant starvation mechanism).
        NEVER raises. Non-reentrancy: do not call while holding
        ``flock_critical_section`` on the same path (bounded timeout ->
        False, never deadlock — same hazard note as async_flock_append_line).
        """
        record = self._append_record(entry)
        return await self._write_line_async(record)

    async def update_status_async(self, lease_id: str, status: str) -> None:
        """Async twin of :meth:`update_status`. ValueError raises BEFORE any
        await (sync parity); the append itself is fail-soft."""
        record = self._status_record(lease_id, status)  # may raise ValueError
        await self._write_line_async(record)

    async def _write_line_async(self, record: Dict[str, Any]) -> bool:
        try:
            line = json.dumps(record, default=str)
        except (TypeError, ValueError):
            return False
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (
                async_flock_append_line,
            )
        except ImportError:
            # Substrate-unavailable rollback — same legacy shape as
            # _write_line's fallback, kept sync (rare, documented).
            return self._write_line(record)
        return bool(await async_flock_append_line(self._path, line))
```

- [ ] **Step 4: Convert the two router call sites**

`unified_intake_router.py:1267` — `_ingest_impl` is `async def`; change `self._wal.append(WALEntry(...))` → `await self._wal.append_async(WALEntry(...))` (preserve how the return bool is currently consumed — read the surrounding lines; the CausalGraphIngestor durability gate must still see the honest bool).
`unified_intake_router.py:2173` — `_dispatch_one` is `async def`; change `self._wal.update_status(envelope.lease_id, "acked")` → `await self._wal.update_status_async(envelope.lease_id, "acked")`. AWAIT in-sequence — ordering on the WAL is an audit property (Mandate 4). Check `:2208`/`:2230` retry/dead-letter siblings: convert them identically if inside `async def` (they are — same method).

- [ ] **Step 5: Run the new suite + WAL/router guard suites**

Run: `python3 -m pytest tests/governance/intake/test_slice4_wal_async.py tests/governance/intake/ -q -k "wal or intake_router or ingest" --timeout=120`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/intake/wal.py backend/core/ouroboros/governance/intake/unified_intake_router.py tests/governance/intake/test_slice4_wal_async.py
git commit -m "perf(intake): WAL append/update_status async twins on the canonical flock funnel (Slice 4 T4)"
```

---

### Task 5: Full regression + Run #15 ignition gate (verification only)

**Files:** none (verification only)

- [ ] **Step 1: Full sprint regression**

Run: `python3 -m pytest tests/governance/intake/test_slice4_wal_async.py tests/governance/intake/test_slice4_sense_offload.py tests/governance/intake/test_doc_staleness_master_gate.py tests/governance/test_slice4_iso_economics.py tests/governance/test_slice4_budget_refusal_taxonomy.py tests/governance/test_async_flock_append.py tests/governance/test_cross_process_jsonl.py tests/governance/test_posture_observer.py -q`
Expected: ALL PASS, 0 failures.

- [ ] **Step 2: Affected-set failure-diff (Slice 3 protocol)**

Build the affected set (test files referencing wal|unified_intake_router|test_failure_sensor|test_watcher|doc_staleness|race_triage|candidate_generator|session_budget), run pristine-branch-worktree vs pristine-base-worktree, `--timeout=60`, strip ANSI BEFORE anchored `grep -E '^(FAILED|ERROR) '`, filter branch-new files from the base side, diff failure sets per chunk. Expected: zero divergence (pre-existing reds identical). Session gotchas that MUST be honored: never run in the main checkout (live `.jarvis` state flips master-flag tests); exclude space-debris `* 2.py` paths.

- [ ] **Step 3: Whole-branch review, then Run #15**

Dispatch the final whole-branch reviewer (same template as Slice 3). After MERGE-READY: ff-merge to main in an OCA-idle window, then ignite Run #15 exactly as Run #14 (`scripts/ignite_a1_soak.py --max-wall-seconds 5000`, Docker up, `.env`-sourced, detached `start_new_session`, NO manual chaos pre-arm) — the driver now self-asserts the funded budget (T1 fail-fast). Success signals, in order: **boot line shows `budget=$<non-zero>`** (new gate) → TestFailureSensor emits a scoped (non-`tests/`-root) pytest run that completes < 180s → GENERATE produces tokens (`tokens>0`) → zero `dw_global_outage` quarantines absent a real outage → `TOOL OUTPUT BEGIN` ≥ 1 → `files_changed>0` → AutoCommit → `a1_verdict.json: proven=true`. LoopSink `flock_append_line` count expected ≈0 (was 214); `ControlPlaneStarvation` expected ⇓ materially (was 590 — hold Slice 3's "≈0" bar only after the intake fix, since Run #14 proved intake WAL was the dominant residual).

---

## Self-Review (completed at write time)

1. **Spec coverage:** Autopsy fix #1 → T1 (funded budget + fail-fast); #2 → T3 (scoped resolution off-loop; the "scope the pytest" outcome falls out of the event lane finally producing signals + legacy sweep derated); #3 → T1 (master gate + iso pin); #4 → deferred (pool headroom — revisit with Run #15 data); #5 → T2; #6 → T2 (Immortal bound); #7 → T4 (dominant) with the rest explicitly deferred; #8 → explicitly deferred with rationale in Global Constraints. Both autopsy-named INDEPENDENT blockers (budget, SENSE) have tasks.
2. **Placeholder scan:** Three verify-first instructions remain by design (real `fs_events_enabled` env name in T3; `SessionBudgetPreflightRefused` constructor kwargs in T2; flock sidecar naming in T4's contention test) — each names the exact file/line to read and the correction rule. No TBDs.
3. **Type consistency:** `append_async(entry: WALEntry) -> bool` / `update_status_async(lease_id: str, status: str) -> None` match their sync twins' shapes; `is_budget_refusal(exc: BaseException) -> bool` used identically in T2's three wiring points; `_offload_fs` follows the Slice 3 `posture_observer._offload_signal` idiom named in Mandate 3.
