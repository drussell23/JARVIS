# Live Agent Dashboard (TUI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify and close the Live Agent Dashboard implementation — all source files and supervisor wiring already exist; this plan runs the test suite, fixes the one remaining display gap (bus metrics in System tab), and confirms all 10 spec acceptance criteria.

**Architecture:** All 7 TUI files, the full test suite, and Zone 6.57 supervisor wiring are already in place. The data layer (PipelineData, AgentsData, SystemData, FaultsData, StatusBarData, TelemetryBusConsumer) is complete. The only gap is that `_refresh_system()` in `app.py` does not yet render the `TELEMETRY BUS` stats block the spec requires. This plan is verification-first, gap-close second.

**Tech Stack:** Python 3, `textual>=0.47.0` (already in `requirements.txt`), `pytest`

**Spec:** `docs/superpowers/specs/2026-03-20-live-agent-dashboard-design.md`

---

## Current Status

| File | Status |
|---|---|
| `backend/core/tui/__init__.py` | COMPLETE |
| `backend/core/tui/pipeline_panel.py` | COMPLETE |
| `backend/core/tui/agents_panel.py` | COMPLETE |
| `backend/core/tui/system_panel.py` | COMPLETE |
| `backend/core/tui/faults_panel.py` | COMPLETE |
| `backend/core/tui/bus_consumer.py` | COMPLETE |
| `backend/core/tui/app.py` | **GAP: missing TELEMETRY BUS display block** |
| `tests/core/test_tui_panels.py` | COMPLETE (34 tests) |
| `unified_supervisor.py:75063-75074` | COMPLETE (Zone 6.57 wired) |

---

### Task 1: Verify Existing Tests Pass

**Files:**
- Test: `tests/core/test_tui_panels.py`

- [ ] **Step 1: Run the full TUI test suite**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/core/test_tui_panels.py -v
```

Expected: all 34 tests PASS. If any fail, stop and fix before proceeding.

- [ ] **Step 2: Confirm import chain is clean**

```bash
python3 -c "
from backend.core.tui.app import JarvisDashboard, start_dashboard
from backend.core.tui.bus_consumer import TelemetryBusConsumer, StatusBarData
from backend.core.tui.pipeline_panel import PipelineData, CommandTrace
from backend.core.tui.agents_panel import AgentsData, AgentEntry
from backend.core.tui.system_panel import SystemData, TransitionEntry
from backend.core.tui.faults_panel import FaultsData, FaultEntry
app = JarvisDashboard()
assert app.pipeline_data and app.agents_data and app.system_data and app.faults_data
print('All TUI symbols imported and instantiated OK')
"
```

Expected: `All TUI symbols imported and instantiated OK`

- [ ] **Step 3: Confirm no supervisor reverse-imports exist**

The spec requires: "The TUI never imports from `unified_supervisor.py`, `unified_command_processor.py`, or any internal module."

```bash
grep -rn "unified_supervisor\|unified_command_processor" backend/core/tui/
```

Expected: zero matches. If any appear, remove them.

---

### Task 2: Add Bus Metrics Block to System Tab

**What the spec requires (Section: Tab System):**

```
TELEMETRY BUS
  Emitted:    1,247
  Delivered:  1,245
  Dropped:    0
  Deduped:    2
  Dead-letter: 0
  Queue:      3/1000
```

The spec explicitly permits reading bus metrics from the singleton as the only exception to the "envelopes only" rule: "Bus metrics read directly from singleton (only exception to 'envelopes only' rule — bus metrics are not events, they're operational counters."

**Files:**
- Modify: `backend/core/tui/app.py` — `_refresh_system()` method

- [ ] **Step 1: Read app.py to confirm line numbers haven't shifted**

```bash
grep -n "_refresh_system\|RECENT TRANSITIONS\|TELEMETRY BUS" backend/core/tui/app.py
```

Confirm `_refresh_system` exists and note the line where `RECENT TRANSITIONS` is written. The bus metrics block goes between the REASONING GATE section and the RECENT TRANSITIONS section.

- [ ] **Step 2: Edit `_refresh_system` to add bus metrics**

In `backend/core/tui/app.py`, the full `_refresh_system` method should become:

```python
    def _refresh_system(self) -> None:
        if not self._system_log:
            return
        count = len(self.system_data.recent_transitions)
        if count == self._last_system_count and count > 0:
            return
        self._last_system_count = count
        log = self._system_log
        log.clear()
        lc = self.system_data.lifecycle_state
        lc_color = "green" if lc == "READY" else "yellow" if lc == "DEGRADED" else "red"
        log.write("[bold]J-PRIME LIFECYCLE[/]")
        log.write(f"  State:    [{lc_color}]{lc}[/]")
        log.write(f"  Restarts: {self.system_data.lifecycle_restarts}")
        log.write("")
        gs = self.system_data.gate_state
        gs_color = "green" if gs == "ACTIVE" else "yellow" if gs == "DEGRADED" else "red" if gs in ("BLOCKED", "TERMINAL") else "dim"
        log.write("[bold]REASONING GATE[/]")
        log.write(f"  State:    [{gs_color}]{gs}[/]")
        log.write(f"  Sequence: {self.system_data.gate_sequence}")
        if self.system_data.gate_deps:
            deps_str = "  ".join(f"{k}={v}" for k, v in self.system_data.gate_deps.items())
            log.write(f"  Deps:     {deps_str}")
        log.write("")
        # --- TELEMETRY BUS (spec: only exception to envelopes-only rule) ---
        try:
            from backend.core.telemetry_contract import get_telemetry_bus
            m = get_telemetry_bus().get_metrics()
            log.write("[bold]TELEMETRY BUS[/]")
            log.write(f"  Emitted:     {m.get('emitted', 0):,}")
            log.write(f"  Delivered:   {m.get('delivered', 0):,}")
            log.write(f"  Dropped:     {m.get('dropped', 0):,}")
            log.write(f"  Deduped:     {m.get('deduped', 0):,}")
            log.write(f"  Dead-letter: {m.get('dead_letter', 0):,}")
            log.write(f"  Queue:       {m.get('queue_size', 0):,}")
        except Exception:
            log.write("[dim]TELEMETRY BUS  (unavailable)[/]")
        log.write("")
        log.write("[bold]RECENT TRANSITIONS[/]")
        for t in list(self.system_data.recent_transitions)[-10:]:
            ts = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
            log.write(f"  {ts}  {t.domain:<10} {t.from_state} -> {t.to_state}  ({t.trigger})")
```

- [ ] **Step 3: Run tests to confirm nothing broke**

```bash
python3 -m pytest tests/core/test_tui_panels.py -v
```

Expected: all 34 tests PASS. (The bus metrics block is in the rendering layer; rendering is not tested per spec — "No Textual rendering tests (too brittle). Test the data layer, not the widget rendering.")

- [ ] **Step 4: Verify import doesn't create circular dependency**

```bash
python3 -c "from backend.core.tui.app import JarvisDashboard; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add backend/core/tui/app.py
git commit -m "feat(tui): add telemetry bus metrics to system tab display

Closes final gap vs spec AC-4. The TELEMETRY BUS block reads from
get_telemetry_bus().get_metrics() — the spec's explicit exception to
the envelopes-only rule (bus metrics are operational counters, not
events)."
```

---

### Task 3: Spec Acceptance Criteria Audit

All 10 acceptance criteria from the spec. Verify each, then commit.

- [ ] **AC 1: Dashboard starts when supervisor boots (if terminal attached)**

```bash
grep -n "Zone 6.57\|start_dashboard" unified_supervisor.py | head -10
```

Expected: Zone 6.57 block with `from backend.core.tui.app import start_dashboard` at line ~75067.

- [ ] **AC 2: Pipeline tab shows real-time command flow with trace_id correlation**

Confirmed by `TestPipelineData` tests. `CommandTrace.trace_id` is populated from `envelope.trace_id`.

- [ ] **AC 3: Agents tab shows all agents with health status from scheduler events**

Confirmed by `TestAgentsData` tests. No hardcoded agent count — `total_agents` comes from `scheduler.graph_state` payload.

```bash
grep -n "hardcod\|== 15\|= 15" backend/core/tui/app.py
```

Expected: zero matches for agent count.

- [ ] **AC 4: System tab shows J-Prime lifecycle + reasoning gate + bus metrics**

Closed by Task 2. Confirmed by inspection of `_refresh_system()`.

- [ ] **AC 5: Faults tab tracks raised/resolved faults with duration**

Confirmed by `TestFaultsData::test_fault_resolved`. `FaultEntry.duration_ms` populated from `fault.resolved` payload's `duration_ms` field.

- [ ] **AC 6: Status bar always shows one-line summary**

Confirmed by `TestStatusBarData::test_to_string`. The `StatusBar.refresh_display()` is called on every 1-second tick in `_refresh_panels()`.

- [ ] **AC 7 + AC 8: All data from real TelemetryEnvelopes; no supervisor internals imported**

```bash
grep -rn "unified_supervisor\|unified_command_processor" backend/core/tui/
```

Expected: zero matches.

- [ ] **AC 9: Dashboard crash doesn't affect JARVIS (daemon thread, fault-isolated)**

```bash
grep -n "daemon=True" backend/core/tui/app.py
grep -n "except Exception" unified_supervisor.py | grep -A1 "6.57\|tui"
```

Expected: `daemon=True` in `threading.Thread(...)` call; outer `except Exception` in Zone 6.57 wrapping the entire `start_dashboard()` call.

- [ ] **AC 10: Panels handle missing data gracefully (no bare key access)**

```bash
grep -rn 'p\["' backend/core/tui/
```

Expected: zero matches. All payload access uses `p.get("key", default)`.

- [ ] **Commit acceptance audit**

```bash
git commit --allow-empty -m "chore(tui): all 10 spec acceptance criteria verified"
```

---

### Task 4: Final Regression Check

- [ ] **Step 1: Run full TUI test suite**

```bash
python3 -m pytest tests/core/test_tui_panels.py -v --tb=short
```

Expected: 34 PASSED.

- [ ] **Step 2: Quick smoke of import chain one final time**

```bash
python3 -c "
from backend.core.tui.app import JarvisDashboard, start_dashboard
from unittest.mock import patch
import sys
with patch.object(sys.stdout, 'isatty', return_value=False):
    result = start_dashboard()
assert result is None
print('start_dashboard() returns None without TTY — OK')
"
```

Expected: `start_dashboard() returns None without TTY — OK`

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "feat(tui): Live Agent Dashboard (Phase C) — complete

All 7 TUI source files + test suite (34 tests) + Zone 6.57 supervisor
wiring verified. All 10 acceptance criteria pass. textual>=0.47.0
dependency confirmed in requirements.txt.

Closes: Live Agent Dashboard design spec (2026-03-20)"
```

---

## YAGNI Guard — Out of Scope per Spec

Do NOT implement these:
- Mouse interaction (keyboard-only TUI)
- Historical data persistence (live view only)
- Log viewer tab (logs stay in files)
- Remote dashboard access (local terminal only)
- Per-MIND-request detail rows in pipeline tab (only aggregate counts stored)
