# Ouroboros Gap Closure: 6 Priorities to Claude-Code Parity + Proactive Trinity

**Date**: 2026-03-19
**Branch**: `feat/ouroboros-gap-closure`
**Scope**: 6 new modules + integration wiring across governance pipeline

---

## Problem Statement

Ouroboros has a production-grade governance pipeline (10 phases, 36K LOC, 1361 tests) but
lacks outward connectivity. It's a powerful engine that mostly talks to itself. Six gaps
prevent it from reaching Claude Code parity and becoming truly proactive in the Trinity
ecosystem (JARVIS/Prime/Reactor):

1. Reasoning chain exists but isn't wired into governance
2. Goal decomposition raises `NotImplementedError`
3. Reactor-Core connection is a one-way file drop with no consumer
4. No external observability (Langfuse/traces)
5. No MCP/external tool integration
6. No flexible scheduling (fixed polling intervals only)

---

## Architecture Overview

All 6 modules plug into existing extension points — no pipeline restructuring needed:

```
                    ┌─────────────────────────────────────┐
                    │        GOVERNANCE PIPELINE           │
  ┌──────────┐     │                                      │     ┌──────────────┐
  │ P6:      │────→│  CLASSIFY → ROUTE → GENERATE → ...   │────→│ P4: Langfuse │
  │ Scheduler│     │     ↑                                │     │ Transport    │
  └──────────┘     │     │ P1: Reasoning                  │     └──────────────┘
  ┌──────────┐     │     │     Chain Bridge               │     ┌──────────────┐
  │ P2: Goal │────→│     │                                │────→│ P5: MCP      │
  │ Decomp.  │     │  IntakeRouter ← Sensors              │     │ Tool Client  │
  └──────────┘     └─────────────────────────────────────┘     └──────────────┘
                              ↕ EventBridge
                    ┌─────────────────────────────────────┐
                    │ P3: Reactor Event Consumer           │
                    │ (bidirectional IPC via shared dir)    │
                    └─────────────────────────────────────┘
```

---

## P1: Wire Reasoning Chain into Governance Pipeline

### Problem
`reasoning_chain_orchestrator.py` (686 lines) is fully built with 3 phases
(SHADOW/SOFT_ENABLE/FULL_ENABLE), ShadowMetrics, ChainTelemetry, and
ChainResult — but zero integration points into the governance pipeline.
Chain telemetry fires directly to Reactor via `asyncio.create_task()`,
bypassing CommProtocol entirely.

### Design

**New file**: `backend/core/ouroboros/governance/reasoning_chain_bridge.py`

```python
class ReasoningChainBridge:
    """Bridges reasoning chain decisions into the governance CommProtocol."""

    def __init__(self, comm: CommProtocol, orchestrator: ReasoningChainOrchestrator):
        self._comm = comm
        self._orchestrator = orchestrator

    async def classify_with_reasoning(
        self, command: str, trace_id: str, deadline: Optional[float] = None
    ) -> Optional[ChainResult]:
        """Run reasoning chain, emit results as PLAN messages."""
        if not self._orchestrator._config.is_active():
            return None

        result = await self._orchestrator.process(
            command=command, context={}, trace_id=trace_id, deadline=deadline
        )
        if result and result.handled:
            await self._comm.emit_plan(
                op_id=trace_id,
                payload={
                    "source": "reasoning_chain",
                    "phase": result.phase,
                    "original_command": command,
                    "expanded_intents": result.expanded_intents,
                    "confidence": result.success_rate,
                    "needs_confirmation": result.needs_confirmation,
                }
            )
        return result
```

**OperationContext addition** — add one field to `op_context.py`:

```python
# In OperationContext frozen dataclass:
reasoning_chain_result: Optional[Dict[str, Any]] = None
```

**Integration point** — in `orchestrator.py` at the CLASSIFY phase (~line 295),
after `emit_intent()`:

```python
# If reasoning chain is active, run it and stamp result onto context
if self._reasoning_bridge:
    chain_result = await self._reasoning_bridge.classify_with_reasoning(
        command=ctx.description, trace_id=ctx.op_id
    )
    if chain_result and chain_result.handled:
        ctx = ctx.advance(
            OperationPhase.ROUTE,
            reasoning_chain_result={
                "expanded_intents": chain_result.expanded_intents,
                "phase": chain_result.phase,
                "success_rate": chain_result.success_rate,
            }
        )
```

**VoiceNarrator hook** — narrate when chain expands intents:

```python
# In voice_narrator.py, add REASONING_CHAIN_EXPANSION handling:
if msg.payload.get("source") == "reasoning_chain":
    intents = msg.payload.get("expanded_intents", [])
    if len(intents) > 1:
        await self._say(f"I'm breaking this into {len(intents)} steps")
```

**Env vars**: Uses existing `JARVIS_REASONING_CHAIN_SHADOW`,
`JARVIS_REASONING_CHAIN_ENABLED`, `JARVIS_REASONING_CHAIN_AUTO_EXPAND`.

**Invariants**:
- J-Prime remains sole planner — bridge classifies and expands, never generates Plans
- Graceful degradation — if chain is inactive or times out, pipeline continues unchanged
- SHADOW mode logs divergence via ShadowMetrics but never alters pipeline flow

---

## P2: Goal Decomposition Engine

### Problem
`engine.py:2476` raises `NotImplementedError("Goal-based improvement not yet implemented")`.
Ouroboros can only process file-level signals from sensors. It can't accept
"improve authentication security" and decompose it into actionable sub-operations.

### Design

**New file**: `backend/core/ouroboros/governance/goal_decomposer.py`

```python
class GoalDecomposer:
    """Decomposes high-level goals into OperationContext objects via TheOracle + reasoning chain."""

    def __init__(
        self,
        oracle: TheOracle,
        reasoning_chain: Optional[ReasoningChainOrchestrator],
        intake_router: UnifiedIntakeRouter,
    ):
        self._oracle = oracle
        self._chain = reasoning_chain
        self._router = intake_router

    async def decompose(self, goal: str, repo: str = "jarvis") -> GoalDecompositionResult:
        """
        1. Use reasoning chain to expand goal into sub-intents
        2. Use Oracle to find target files for each sub-intent via semantic search
        3. Build IntentEnvelopes for each sub-task
        4. Submit to intake router with causal ordering (shared correlation_id)
        """
```

**GoalDecompositionResult**:

```python
@dataclass
class GoalDecompositionResult:
    original_goal: str
    sub_tasks: List[SubTask]      # Each has: intent, target_files, confidence
    correlation_id: str            # Shared saga ID across all sub-tasks
    submitted_count: int           # How many were ingested into router
    skipped_count: int             # How many were below confidence threshold
```

**Algorithm**:

1. **Intent expansion** — If reasoning chain is active, call `orchestrator.process(goal)`
   to get expanded sub-intents. If chain inactive, treat the full goal as a single intent.
2. **File targeting** — For each sub-intent, query `oracle.semantic_search(intent_text)`
   to find relevant files. Use `oracle.get_file_neighborhood()` for blast radius.
3. **Envelope creation** — Build `IntentEnvelope` per sub-task with:
   - `source="goal_decomposer"`
   - `requires_human_ack=True` (all goal-decomposed tasks need approval)
   - `correlation_id` shared across all sub-tasks in this goal
4. **Causal ordering** — If sub-tasks have file dependencies (detected via Oracle graph),
   set `dependency_edges` in OperationContext for topological execution order.
5. **Submission** — Ingest all envelopes via `intake_router.ingest()`.

**Wire into engine.py** — replace `NotImplementedError`:

```python
async def improve_with_goal(goal: str) -> GoalDecompositionResult:
    decomposer = GoalDecomposer(
        oracle=get_oracle(),
        reasoning_chain=get_reasoning_chain_orchestrator(),
        intake_router=get_intake_router(),
    )
    return await decomposer.decompose(goal)
```

**Wire into IntakeLayerService** — add new sensor:

```python
class GoalSensor:
    """Accepts high-level goals from voice commands and API."""
    async def submit_goal(self, goal: str, repo: str = "jarvis") -> str:
        result = await self._decomposer.decompose(goal, repo)
        return result.correlation_id
```

**Safety**: All goal-decomposed operations require `requires_human_ack=True`.
The trust graduator cannot auto-approve goal-sourced operations until AUTONOMOUS tier.

---

## P3: Reactor-Core Bidirectional Event Consumer

### Problem
`CrossRepoEventBus` is a JARVIS-local file loop. It writes JSON to Reactor's
`training/experiences/` directory but nobody reads it. `_on_training_complete()`
is a stub that just logs. The Trinity is missing its nervous system.

### Design

**New file**: `backend/core/ouroboros/governance/reactor_event_consumer.py`

```python
class ReactorEventConsumer:
    """
    Bidirectional event bridge between JARVIS and Reactor-Core.

    JARVIS → Reactor: EXPERIENCE_GENERATED events written to shared event dir
    Reactor → JARVIS: TRAINING_COMPLETE events read from Reactor's outbox dir

    Uses filesystem IPC via a shared directory convention:
      ~/.jarvis/ouroboros/events/          # JARVIS writes here (outbox)
      ~/.jarvis/ouroboros/reactor-inbox/   # Reactor writes here (JARVIS reads)
    """

    def __init__(
        self,
        event_bus: CrossRepoEventBus,
        reactor_inbox: Path,
        poll_interval_s: float = 5.0,
    ):
        self._bus = event_bus
        self._inbox = reactor_inbox
        self._poll_interval = poll_interval_s
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        (self._inbox / "pending").mkdir(exist_ok=True)
        (self._inbox / "processed").mkdir(exist_ok=True)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Poll Reactor's outbox for TRAINING_COMPLETE and other events."""
        while self._running:
            try:
                for event_file in (self._inbox / "pending").glob("*.json"):
                    await self._process_reactor_event(event_file)
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Reactor inbox poll error: %s", e)
                await asyncio.sleep(10.0)

    async def _process_reactor_event(self, event_file: Path) -> None:
        """Process a single event from Reactor's outbox."""
        data = json.loads(await asyncio.to_thread(event_file.read_text))
        event = CrossRepoEvent.from_dict(data)

        # Emit into JARVIS's event bus for handler dispatch
        await self._bus.emit(event)

        # Move to processed
        dest = self._inbox / "processed" / event_file.name
        await asyncio.to_thread(event_file.rename, dest)
```

**Wire `_on_training_complete()`** — replace the stub:

```python
async def _on_training_complete(self, event: CrossRepoEvent) -> None:
    """Handle training complete — notify Prime to reload updated model."""
    self.logger.info("Training completed: %s", event.id)

    model_path = event.payload.get("model_path")
    if model_path:
        # Notify J-Prime to hot-reload the updated model
        prime_connector = self._connectors[RepoType.PRIME]
        if prime_connector.get_state().healthy:
            try:
                await self._prime_client.request_model_reload(model_path)
                logger.info("Requested Prime model reload: %s", model_path)
            except Exception as e:
                logger.warning("Prime model reload request failed: %s", e)
```

**Reactor-side consumer** — create a minimal consumer script in Reactor-Core:

**New file**: `reactor-core/reactor_core/event_consumer.py`

```python
class ReactorEventConsumer:
    """Watches ~/.jarvis/ouroboros/events/ for EXPERIENCE_GENERATED events.
    Processes training experiences and emits TRAINING_COMPLETE to reactor-inbox."""

    def __init__(self, inbox_dir, outbox_dir):
        self._inbox = Path(inbox_dir)    # reads from JARVIS events/pending
        self._outbox = Path(outbox_dir)  # writes to reactor-inbox/pending

    async def process_experience(self, event):
        """Process a training experience and emit completion event."""
        # Store experience for training
        experience = event.payload
        await self._store_experience(experience)

        # Emit TRAINING_COMPLETE back to JARVIS
        completion = CrossRepoEvent(
            type=EventType.TRAINING_COMPLETE,
            source_repo=RepoType.REACTOR,
            target_repo=RepoType.JARVIS,
            payload={"experience_id": event.id, "status": "stored"},
        )
        event_file = self._outbox / "pending" / f"{completion.id}.json"
        event_file.write_text(json.dumps(completion.to_dict()))
```

**Directory convention**:

```
~/.jarvis/ouroboros/
├── events/              # JARVIS outbox (CrossRepoEventBus writes here)
│   ├── pending/
│   ├── processed/
│   └── failed/
└── reactor-inbox/       # Reactor outbox → JARVIS inbox
    ├── pending/         # Reactor writes here, ReactorEventConsumer reads
    └── processed/       # ReactorEventConsumer moves processed events here
```

**Wire into IntakeLayerService.start()** — start ReactorEventConsumer alongside sensors.

---

## P4: Langfuse Observability Transport

### Problem
Governance events are stored locally in JSONL ledger files and in-memory LogTransport.
No external observability — can't visualize pipeline performance, costs, or failure
patterns in a dashboard.

### Design

**New file**: `backend/core/ouroboros/governance/comms/langfuse_transport.py`

```python
class LangfuseTransport:
    """CommProtocol transport that emits governance events to Langfuse.

    Maps the 5-phase CommMessage lifecycle to Langfuse traces:
      INTENT     → trace.start() with operation metadata
      PLAN       → trace.span("plan") with reasoning chain data
      HEARTBEAT  → trace.span("heartbeat") with resource metrics
      DECISION   → trace.span("decision") with outcome + provider
      POSTMORTEM → trace.span("postmortem") with root cause + error
    """

    def __init__(self, langfuse_client=None):
        self._langfuse = langfuse_client or self._create_client()
        self._traces: Dict[str, Any] = {}  # op_id → langfuse trace

    def _create_client(self):
        """Create Langfuse client from env vars."""
        try:
            from langfuse import Langfuse
            return Langfuse(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
                host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
        except ImportError:
            logger.warning("langfuse not installed — LangfuseTransport disabled")
            return None

    async def send(self, msg: CommMessage) -> None:
        if self._langfuse is None:
            return

        if msg.msg_type == MessageType.INTENT:
            # Start a new trace for this operation
            trace = self._langfuse.trace(
                name=f"ouroboros-{msg.op_id}",
                metadata=msg.payload,
                tags=["ouroboros", msg.payload.get("trigger_source", "unknown")],
            )
            self._traces[msg.op_id] = trace

        elif msg.msg_type in (MessageType.PLAN, MessageType.HEARTBEAT,
                               MessageType.DECISION, MessageType.POSTMORTEM):
            trace = self._traces.get(msg.op_id)
            if trace:
                trace.span(
                    name=msg.msg_type.value.lower(),
                    metadata=msg.payload,
                    level="ERROR" if msg.msg_type == MessageType.POSTMORTEM else "DEFAULT",
                )

        # Flush on terminal messages
        if msg.msg_type in (MessageType.DECISION, MessageType.POSTMORTEM):
            self._traces.pop(msg.op_id, None)
            await asyncio.to_thread(self._langfuse.flush)
```

**Wire into supervisor Zone 6.8** — add to transport stack:

```python
transports = [LogTransport(), event_bridge]
if os.getenv("LANGFUSE_PUBLIC_KEY"):
    from backend.core.ouroboros.governance.comms.langfuse_transport import LangfuseTransport
    transports.append(LangfuseTransport())
comm = CommProtocol(transports=transports)
```

**Env vars**: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` (optional).

**Graceful degradation**: If `langfuse` not installed or keys not set, transport is a no-op.

---

## P5: MCP External Tool Integration

### Problem
Ouroboros has no way to interact with external systems. Can't create GitHub issues
for failures, can't send Slack alerts, can't manage deployments.

### Design

**New file**: `backend/core/ouroboros/governance/mcp_tool_client.py`

```python
class GovernanceMCPClient:
    """
    MCP client that connects to external tool servers for governance actions.

    Exposes tools to the pipeline's POSTMORTEM and DECISION phases:
      - github.create_issue — on POSTMORTEM, create issue with failure details
      - notify.alert — on POSTMORTEM, send alert via configured channel
      - github.create_pr — on COMPLETE, create PR for applied changes
    """

    def __init__(self, config: MCPClientConfig):
        self._config = config
        self._servers: Dict[str, MCPServerConnection] = {}

    async def start(self) -> None:
        """Connect to configured MCP servers."""
        for server_name, server_config in self._config.servers.items():
            try:
                conn = await MCPServerConnection.connect(server_config)
                self._servers[server_name] = conn
                logger.info("MCP server connected: %s", server_name)
            except Exception as e:
                logger.warning("MCP server %s failed to connect: %s", server_name, e)

    async def on_postmortem(self, ctx: OperationContext) -> None:
        """React to pipeline failures with external actions."""
        if "github" in self._servers:
            await self._servers["github"].call_tool(
                "create_issue",
                title=f"[Ouroboros] Pipeline failure: {ctx.description[:80]}",
                body=self._format_failure_body(ctx),
                labels=["ouroboros", "automated"],
            )

    async def on_complete(self, ctx: OperationContext, applied_files: List[str]) -> None:
        """React to successful operations with external actions."""
        if not applied_files:
            return
        if "github" in self._servers and self._config.auto_pr:
            await self._servers["github"].call_tool(
                "create_pull_request",
                title=f"[Ouroboros] {ctx.description[:80]}",
                body=self._format_pr_body(ctx, applied_files),
                branch=f"ouroboros/{ctx.op_id[:12]}",
            )
```

**MCPClientConfig** — read from env/yaml:

```python
@dataclass
class MCPClientConfig:
    servers: Dict[str, MCPServerConfig]  # name → {transport, url, command}
    auto_issue: bool = True              # Create GitHub issues on POSTMORTEM
    auto_pr: bool = False                # Create PRs on COMPLETE (opt-in)
    alert_channel: str = ""              # Slack/notification channel

    @classmethod
    def from_env(cls) -> MCPClientConfig:
        config_path = os.getenv("JARVIS_MCP_CONFIG", "")
        if not config_path or not Path(config_path).exists():
            return cls(servers={})
        # Parse YAML config for MCP server definitions
        ...
```

**Wire into orchestrator.py** — hook at terminal phases:

```python
# In _run_pipeline(), after COMPLETE:
if self._mcp_client:
    await self._mcp_client.on_complete(ctx, applied_files)

# In _run_pipeline(), after POSTMORTEM:
if self._mcp_client:
    await self._mcp_client.on_postmortem(ctx)
```

**Config file**: `~/.jarvis/mcp_servers.yaml`

```yaml
servers:
  github:
    transport: stdio
    command: ["npx", "-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
auto_issue: true
auto_pr: false
```

**Safety**: MCP tool calls are fire-and-forget with timeout (10s). Failures logged
but never block the pipeline. `auto_pr` defaults to false — opt-in only.

---

## P6: Flexible ScheduledTriggerSensor

### Problem
Ouroboros only has fixed-interval polling (30s backlog, 300s opportunity mining).
Can't schedule "run a security audit every Sunday at 2 AM" or "check dependency
updates daily."

### Design

**New file**: `backend/core/ouroboros/governance/intake/sensors/scheduled_sensor.py`

```python
class ScheduledTriggerSensor:
    """Fires IntentEnvelopes based on cron expressions from config.

    Config format (YAML):
      schedules:
        - name: security_audit
          cron: "0 2 * * 0"           # Sundays at 2 AM
          goal: "Scan for security vulnerabilities in authentication code"
          repo: jarvis
          requires_human_ack: true

        - name: dependency_check
          cron: "0 8 * * 1"           # Mondays at 8 AM
          goal: "Check for outdated dependencies and suggest updates"
          repo: jarvis
          requires_human_ack: true

        - name: test_coverage_sweep
          cron: "0 3 * * *"           # Daily at 3 AM
          goal: "Find uncovered code paths and generate tests"
          repo: jarvis
          requires_human_ack: false   # auto-submit if trust tier allows
    """

    def __init__(self, config_path: Path, router: UnifiedIntakeRouter):
        self._config_path = config_path
        self._router = router
        self._schedules: List[ScheduleEntry] = []
        self._running = False
        self._check_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._schedules = self._load_config()
        self._running = True
        self._check_task = asyncio.create_task(self._check_loop())

    async def _check_loop(self) -> None:
        """Check every 60s if any schedule should fire."""
        while self._running:
            now = datetime.now()
            for schedule in self._schedules:
                if self._should_fire(schedule, now):
                    await self._fire(schedule)
                    schedule.last_fired = now
            await asyncio.sleep(60.0)

    def _should_fire(self, schedule: ScheduleEntry, now: datetime) -> bool:
        """Check if cron expression matches current time."""
        # Use croniter for cron parsing
        from croniter import croniter
        if schedule.last_fired:
            cron = croniter(schedule.cron, schedule.last_fired)
            next_fire = cron.get_next(datetime)
            return now >= next_fire
        # First run — check if cron matches now
        cron = croniter(schedule.cron, now - timedelta(minutes=1))
        return cron.get_next(datetime) <= now

    async def _fire(self, schedule: ScheduleEntry) -> None:
        """Create and ingest an IntentEnvelope for this schedule."""
        envelope = IntentEnvelope(
            source=f"scheduled:{schedule.name}",
            description=schedule.goal,
            target_files=[],  # Goal decomposer will resolve files
            repo=schedule.repo,
            requires_human_ack=schedule.requires_human_ack,
            confidence=1.0,   # Scheduled = intentional
        )
        await self._router.ingest(envelope)
        logger.info("Scheduled trigger fired: %s", schedule.name)
```

**ScheduleEntry**:

```python
@dataclass
class ScheduleEntry:
    name: str
    cron: str
    goal: str
    repo: str = "jarvis"
    requires_human_ack: bool = True
    enabled: bool = True
    last_fired: Optional[datetime] = None
```

**Config file**: `~/.jarvis/ouroboros/schedules.yaml`

**Wire into IntakeLayerService.start()** — add as 5th sensor type:

```python
# In IntakeLayerService.start():
schedule_config = Path.home() / ".jarvis" / "ouroboros" / "schedules.yaml"
if schedule_config.exists():
    self._scheduled_sensor = ScheduledTriggerSensor(schedule_config, self._router)
    await self._scheduled_sensor.start()
```

**Dependency**: `croniter` (pure Python, no C extensions).

---

## Cross-Cutting Concerns

### Environment Variables (new)

| Variable | Default | Purpose |
|----------|---------|---------|
| `LANGFUSE_PUBLIC_KEY` | (empty) | Enables Langfuse transport |
| `LANGFUSE_SECRET_KEY` | (empty) | Langfuse auth |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse endpoint |
| `JARVIS_MCP_CONFIG` | (empty) | Path to MCP server config YAML |
| `JARVIS_SCHEDULE_CONFIG` | `~/.jarvis/ouroboros/schedules.yaml` | Cron schedule config |
| `JARVIS_REACTOR_INBOX` | `~/.jarvis/ouroboros/reactor-inbox` | Reactor→JARVIS event dir |

### Dependencies (new)

| Package | Version | Purpose | Optional |
|---------|---------|---------|----------|
| `langfuse` | `>=2.0` | Observability traces | Yes — transport is no-op without it |
| `croniter` | `>=1.3` | Cron expression parsing | Yes — scheduler disabled without it |

### Testing Strategy

Each module gets:
1. **Unit tests** — mock all external dependencies (CommProtocol, Oracle, MCP servers)
2. **Integration test** — verify wiring with in-memory transports and test fixtures
3. **Feature flag** — each module can be disabled via env var without affecting others

### File Manifest

**New files** (6):
- `backend/core/ouroboros/governance/reasoning_chain_bridge.py` (~80 lines)
- `backend/core/ouroboros/governance/goal_decomposer.py` (~150 lines)
- `backend/core/ouroboros/governance/reactor_event_consumer.py` (~100 lines)
- `backend/core/ouroboros/governance/comms/langfuse_transport.py` (~90 lines)
- `backend/core/ouroboros/governance/mcp_tool_client.py` (~120 lines)
- `backend/core/ouroboros/governance/intake/sensors/scheduled_sensor.py` (~100 lines)

**Modified files** (5):
- `backend/core/ouroboros/governance/op_context.py` — add `reasoning_chain_result` field
- `backend/core/ouroboros/governance/orchestrator.py` — hook reasoning bridge + MCP client
- `backend/core/ouroboros/governance/intake/intake_layer_service.py` — wire scheduler + reactor consumer
- `backend/core/ouroboros/cross_repo.py` — wire `_on_training_complete()` stub
- `backend/core/ouroboros/engine.py` — replace `NotImplementedError` with GoalDecomposer

**New config files** (1):
- `~/.jarvis/ouroboros/schedules.yaml` (user-created, not checked in)

**Test files** (6):
- One test file per new module in `tests/test_ouroboros_governance/`

---

## Execution Order

P1 and P3 are foundational. P2 depends on P1 (needs reasoning chain for intent expansion).
P4, P5, P6 are independent of each other.

```
P1 (reasoning chain bridge)  ──→  P2 (goal decomposer, uses P1)
P3 (reactor bidirectional)        independent
P4 (langfuse transport)           independent
P5 (MCP tool client)              independent
P6 (scheduled sensor)             independent
```

Parallel execution plan: P1 + P3 + P4 + P5 + P6 in parallel, then P2 after P1 completes.
