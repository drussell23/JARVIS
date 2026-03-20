# Live Agent Dashboard (TUI) — Design Spec (Phase C)

> **Date**: 2026-03-20
> **Phase**: C (final, follows Phase A telemetry contract + Phase B agent boot/gate)
> **Sub-project**: 3 of 3
> **Status**: Approved, ready for implementation

---

## Problem

When JARVIS starts, the terminal shows a wall of unstructured log lines. It's impossible to tell at a glance what's working, what's broken, what agents are active, or what's happening when you give a command. You have to grep through logs to understand system state.

## Solution: Textual TUI Dashboard

A Textual-based terminal dashboard that renders **real system state** from the frozen telemetry contract. Four tabs (Pipeline, Agents, System, Faults), a persistent status bar, and real-time updates as commands flow through the reasoning chain.

### Key Constraint: Real Data Only

Every value displayed comes from actual TelemetryEnvelopes emitted by running components. The dashboard never fabricates state, never polls internal module attributes, never imports supervisor internals. It subscribes to the TelemetryBus and renders what it receives. If a component isn't emitting, the dashboard shows "no data" — not a guess.

---

## Architecture

```
TelemetryBus (already running)
    |
    v
BusConsumer (subscribes to "*")
    |
    v routes by event_schema
    |
    +-> PipelinePanel   (reasoning.decision@1.0.0, reasoning.activation@1.0.0)
    +-> AgentsPanel      (scheduler.graph_state@1.0.0, scheduler.unit_state@1.0.0)
    +-> SystemPanel      (lifecycle.transition@1.0.0, lifecycle.health@1.0.0)
    +-> FaultsPanel      (fault.raised@1.0.0, fault.resolved@1.0.0)
    +-> StatusBar        (all events — derives summary from latest state)
```

### File Structure

```
backend/core/tui/
    __init__.py          # Empty, marks package
    app.py               # JarvisDashboard (Textual App), start_dashboard()
    bus_consumer.py      # TelemetryBusConsumer — subscribes, routes to panels
    pipeline_panel.py    # PipelinePanel widget — command trace log
    agents_panel.py      # AgentsPanel widget — agent inventory grid
    system_panel.py      # SystemPanel widget — lifecycle, gate, telemetry stats
    faults_panel.py      # FaultsPanel widget — active/resolved faults
```

### Separation from Supervisor

The supervisor adds ~5 lines at Zone 6.57:
```python
from backend.core.tui.app import start_dashboard
start_dashboard()  # Subscribes to bus, starts Textual in daemon thread
```

The TUI never imports from `unified_supervisor.py`, `unified_command_processor.py`, or any internal module. It only imports from `backend.core.telemetry_contract` (the frozen bus).

---

## Tab: Pipeline (Default)

Shows real-time command flow through the reasoning chain. Each command is a collapsible entry showing every stage.

### Data Source
- `reasoning.decision@1.0.0` — each command's detection/expansion/mind/coordination results
- `reasoning.activation@1.0.0` — gate state changes

### Display Format
```
14:23:05 "start my day" trace_id=abc-123
  DETECT   proactive=true  conf=0.92  signals=[workflow,multi_task]  15ms
  EXPAND   3 intents  conf=0.88  [check email, calendar, slack]     120ms
  MIND[1]  check email     plan_ready  brain=qwen-2.5-7b            850ms
  MIND[2]  check calendar  plan_ready  brain=qwen-2.5-7b            720ms
  MIND[3]  open slack      plan_ready  brain=qwen-2.5-7b            640ms
  COORD    email -> GoogleWorkspaceAgent                             50ms
  COORD    calendar -> GoogleWorkspaceAgent                          45ms
  COORD    slack -> NativeAppControlAgent                            38ms
  DONE     3/3 intents  success=100%  total=2.3s

14:22:41 "what's the weather" trace_id=def-456
  DETECT   proactive=false  conf=0.10  -> passthrough (single-intent)
```

### State Management
- `_commands: Deque[CommandTrace]` — last 50 commands (bounded)
- Each `CommandTrace` aggregates all envelopes sharing the same `trace_id`
- New envelopes append to the matching trace; new trace_ids create new entries
- Display auto-scrolls to newest

---

## Tab: Agents

Shows all Neural Mesh agents and their current state.

### Data Source
- `scheduler.graph_state@1.0.0` — boot-time agent initialization results
- `scheduler.unit_state@1.0.0` — per-agent state changes (idle/busy/error)

### Display Format
```
AGENTS (15/15 initialized)

 CRITICAL (reasoning chain)
  * CoordinatorAgent      idle     tasks: 47   errors: 0
  * PredictivePlanner     busy     tasks: 23   errors: 0
  * ProactiveDetector     idle     detections: 147

 NON-CRITICAL
  * MemoryAgent           idle     queries: 89
  * GoogleWorkspace       busy     tasks: 34   errors: 1
  * SpatialAwareness      idle
  * VisualMonitor         idle     frames: 12,340
  * ErrorAnalyzer         idle
  * ContextTracker        idle
  * PatternRecognition    idle
  * WebSearch             idle
  * GoalInference         idle
  * ActivityRecognition   idle
  * HealthMonitor         idle
  * ComputerUse           idle
```

### State Management
- `_agents: Dict[str, AgentDisplayState]` — keyed by agent name
- Updated from `scheduler.*` envelopes
- Critical agents (in CRITICAL_FOR_REASONING) displayed first
- Status derived from latest envelope, not polled

---

## Tab: System

Shows J-Prime lifecycle, reasoning gate, and telemetry bus health.

### Data Source
- `lifecycle.transition@1.0.0` — J-Prime state changes
- `lifecycle.health@1.0.0` — periodic health probe results
- `reasoning.activation@1.0.0` — gate state changes
- Bus metrics via `get_telemetry_bus().get_metrics()`

### Display Format
```
J-PRIME LIFECYCLE
  State:    READY
  Uptime:   2h 34m (since last READY transition)
  Restarts: 0/5 in window
  Zone:     us-central1-b
  Endpoint: http://136.113.252.164:8000

REASONING GATE
  State:        ACTIVE
  Gate Seq:     47
  Deps:         jprime=HEALTHY coordinator=HEALTHY planner=HEALTHY detector=HEALTHY
  Phase:        FULL_ENABLE (auto-expand)

TELEMETRY BUS
  Emitted:    1,247
  Delivered:  1,245
  Dropped:    0
  Deduped:    2
  Dead-letter: 0
  Queue:      3/1000

RECENT TRANSITIONS
  14:20:01  lifecycle  PROBING -> READY       (ready_for_inference)
  14:20:05  gate       WAITING_DEPS -> READY  (ALL_DEPS_READY)
  14:20:10  gate       READY -> ACTIVE        (ACTIVATION_ARMED)
```

### State Management
- `_lifecycle_state: str` — latest from lifecycle.transition
- `_gate_state: str` — latest from reasoning.activation
- `_transitions: Deque[TransitionDisplay]` — last 20 transitions (both lifecycle + gate)
- Bus metrics read directly from singleton (only exception to "envelopes only" rule — bus metrics are not events, they're operational counters)

---

## Tab: Faults

Shows active and recently resolved faults.

### Data Source
- `fault.raised@1.0.0` — new faults
- `fault.resolved@1.0.0` — fault resolution

### Display Format
```
ACTIVE FAULTS (0)
  (none)

RESOLVED TODAY (2)
  14:18:30  RESOLVED  jprime_lifecycle  connection_refused  auto_recovered  (duration: 45s)
  14:15:02  RESOLVED  coordinator_agent  timeout            auto_recovered  (duration: 12s)
```

### State Management
- `_active_faults: Dict[str, FaultDisplay]` — keyed by event_id of fault.raised
- `_resolved_faults: Deque[FaultDisplay]` — last 20 resolved
- fault.resolved matched to fault.raised via `fault_id` field in payload

---

## Status Bar (Always Visible)

One-line summary across all tabs:

```
J-Prime:READY | Gate:ACTIVE | Agents:15/15 | Faults:0 | Cmds:147 | Bus:1247/0d
```

Updated on every envelope received. Derived from latest state of each domain.

---

## BusConsumer

```python
class TelemetryBusConsumer:
    """Routes TelemetryEnvelopes to dashboard panels."""

    def __init__(self, app: JarvisDashboard):
        self._app = app
        self._routing = {
            "reasoning": app.pipeline_panel,
            "lifecycle": app.system_panel,
            "scheduler": app.agents_panel,
            "recovery": app.faults_panel,
            "fault": app.faults_panel,  # fault.* events
        }

    async def handle(self, envelope: TelemetryEnvelope) -> None:
        # Update status bar (all events)
        self._app.status_bar.update(envelope)

        # Route to panel by partition_key
        schema_domain = envelope.event_schema.split(".")[0]
        panel = self._routing.get(schema_domain)
        if panel:
            panel.update(envelope)
```

---

## Startup: start_dashboard()

```python
def start_dashboard() -> None:
    """Start the TUI dashboard in a daemon thread.

    Subscribes to TelemetryBus and launches the Textual app.
    Non-blocking — returns immediately. If no terminal attached
    (e.g., running as service), silently skips.
    """
    if not sys.stdout.isatty():
        return  # No terminal — skip dashboard

    bus = get_telemetry_bus()
    app = JarvisDashboard()
    consumer = TelemetryBusConsumer(app)
    bus.subscribe("*", consumer.handle)

    thread = threading.Thread(
        target=app.run,
        name="jarvis-tui-dashboard",
        daemon=True,
    )
    thread.start()
```

---

## Textual App Structure

```python
class JarvisDashboard(App):
    CSS = "..."  # Dark theme, monospace, green/blue/amber/red accents

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent():
            with TabPane("Pipeline", id="pipeline"):
                yield PipelinePanel()
            with TabPane("Agents", id="agents"):
                yield AgentsPanel()
            with TabPane("System", id="system"):
                yield SystemPanel()
            with TabPane("Faults", id="faults"):
                yield FaultsPanel()
        yield StatusBar()
```

---

## Testing Strategy

Unit tests for each panel's `update()` method with mock envelopes:
- Feed a `reasoning.decision@1.0.0` envelope -> verify PipelinePanel adds a command trace
- Feed a `lifecycle.transition@1.0.0` envelope -> verify SystemPanel updates lifecycle state
- Feed a `fault.raised@1.0.0` then `fault.resolved@1.0.0` -> verify FaultsPanel tracks lifecycle
- Feed a `scheduler.graph_state@1.0.0` -> verify AgentsPanel populates agent list
- BusConsumer routing: verify correct panel receives each event type

No Textual rendering tests (too brittle). Test the data layer, not the widget rendering.

---

## Files Changed

| File | Change |
|---|---|
| `backend/core/tui/__init__.py` | **NEW** — empty package marker |
| `backend/core/tui/app.py` | **NEW** — JarvisDashboard Textual App, start_dashboard() |
| `backend/core/tui/bus_consumer.py` | **NEW** — TelemetryBusConsumer routing |
| `backend/core/tui/pipeline_panel.py` | **NEW** — PipelinePanel widget |
| `backend/core/tui/agents_panel.py` | **NEW** — AgentsPanel widget |
| `backend/core/tui/system_panel.py` | **NEW** — SystemPanel widget |
| `backend/core/tui/faults_panel.py` | **NEW** — FaultsPanel widget |
| `unified_supervisor.py` | **MODIFY** — Zone 6.57: start_dashboard() (~5 lines) |
| `tests/core/test_tui_panels.py` | **NEW** — Panel data layer tests |

## Acceptance Criteria

1. Dashboard starts when supervisor boots (if terminal attached)
2. Pipeline tab shows real-time command flow with trace_id correlation
3. Agents tab shows all 15 agents with health status from scheduler events
4. System tab shows J-Prime lifecycle + reasoning gate + bus metrics
5. Faults tab tracks raised/resolved faults with duration
6. Status bar always shows one-line summary
7. All displayed data comes from real TelemetryEnvelopes (no mocks, no fabrication)
8. Dashboard never imports supervisor/processor internals
9. Dashboard crash doesn't affect JARVIS operation (daemon thread, fault-isolated)
10. Panels handle missing data gracefully ("no data" not errors)

## Out of Scope

- Mouse interaction (keyboard-only TUI)
- Historical data / persistence (live view only)
- Log viewer tab (logs stay in files)
- Remote dashboard access (local terminal only)

## Dependencies

- `textual` package (pip install textual)
- Phase A telemetry contract (TelemetryBus, TelemetryEnvelope)
- Phase B producers (lifecycle controller, reasoning chain, activation gate)
