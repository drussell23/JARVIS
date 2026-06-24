# Sovereign Multi-Agent Swarm — Design Spec (Fleet Commander, Phase 1)

> **Arc.** JARVIS evolves from a solitary organism into a **Central Fleet Commander**: it decomposes a task and delegates parallel sub-goals to *dynamically-defined, ephemeral, sandboxed* worker agents that can talk to each other — all under the existing governance cage. **Date:** 2026-06-24. **Status:** design for review (no implementation until approved).
> **HONEST FRAMING (reuse-first):** the recon found ~60-70% of the "swarm" ALREADY EXISTS. `SubagentScheduler` runs a parallel work-unit DAG with worktree isolation + artifact-return *today*; the GENERAL subagent renders its system prompt + tool allowlist *dynamically*; `ScopedToolBackend` does per-agent tool/mutation caging. This spec EXTENDS that substrate — it does NOT rebuild a parallel agent system. Three narrow genuine gaps (§3/§4/§5) are the real work.

---

## 0. Reuse Map (verified — the swarm substrate that EXISTS)
- **Parallel DAG executor:** `autonomy/subagent_scheduler.py` (`SubagentScheduler`) — executes an `ExecutionGraph` of `WorkUnitSpec` in parallel, dependency-ordered, concurrency-limited, MemoryPressureGate-gated, with worktree isolation per unit. `autonomy/subagent_types.py` (`WorkUnitSpec`, `ExecutionGraph` w/ acyclicity validation, `WorkUnitResult`).
- **Worktree isolation + cleanup:** `worktree_manager.py` — COW `git worktree` per unit, `reap_orphans`, finally-block `cleanup`, ledger-sovereignty ownership. **The fs sandbox already exists.**
- **Per-agent tool/mutation cage:** `scoped_tool_backend.py` (`ScopedToolBackend` + `ScopedToolGate`) — per-instance tool allowlist + `max_mutations` count gate + `state_mirror`. **Already parameterized per dispatch.**
- **Dynamic system prompt (PARTIAL):** `agentic_general_subagent.py` `render_general_system_prompt()` — already templates `goal`, `scope_paths`, `allowed_tools_list`, `max_mutations`, `read_only_mode` at runtime. The GENERAL Semantic Firewall (11 injection detectors, recursion ban, output quarantine, hard-kill) is the per-worker cage.
- **Artifact return:** `WorkUnitResult` → `command_bus.py` (`REPORT_WORK_UNIT_RESULT`) → `MergeCoordinator.merge_repo_patches`. **The return path exists.**
- **Bounded command transport:** `autonomy/command_bus.py` — priority heap + idempotency dedup + TTL + backpressure (L2/L3/L4→L1 today).
- **Event backbone:** `trinity_event_bus.py` — topic pub/sub, correlation/causation, priority, WAL replay (inter-COMPONENT today).
- **Fan-out governance:** `parallel_dispatch.py` (`is_fanout_eligible`) + `SensorGovernor` (global op cap) + `MemoryPressureGate` (RAM-gated fan-out). **The swarm can't run away — already capped.**

**The 3 genuine gaps:** (G1) per-worker *dynamic* system_prompt + tool allowlist for ALL agents (only GENERAL has it; `WorkUnitSpec` lacks the fields). (G2) worker↔worker messaging (ABSENT — everything routes through L1). (G3) an ephemeral *memory* sandbox + GC-on-completion (fs isolation exists; memory/context GC doesn't).

## 1. Goals / Non-Goals
**Goals.** (G1) A `SwarmOrchestrator` that, at decompose time, **dynamically defines each worker's system prompt, tool constraints, and context budget from the sub-goal** — no hardcoded agent-type list. (G2) An async **`AgentMessageBus`** for structured-JSON worker↔worker messaging (artifact handoff, clarification requests) without routing every message through the Commander. (G3) An **Ephemeral Memory Sandbox** per worker: its memory/context is isolated from global Ouroboros state and **garbage-collected on completion** (no token bloat). (G4) Reuse the SubagentScheduler DAG + worktree + artifact-return + the existing governance cage; build thin layers, not a parallel system. (G5) Default-OFF, gated, fail-CLOSED, air-gap-aligned.
**Non-Goals.** NOT rebuilding the parallel DAG executor / worktree isolation / artifact-return (reuse). NOT bypassing the governance cage — every worker obeys the Iron Gate, risk-tier ladder, SensorGovernor, mutation budget, and (for mutating workers) the GENERAL Semantic Firewall. NOT unbounded agent spawning (the SensorGovernor + MemoryPressureGate + concurrency_limit cap it). NOT inter-agent messages with authority (sanitized, advisory — like the conversation_bridge Tier -1).

## 2. Architecture
```
JARVIS = FLEET COMMANDER (the orchestrator)
   │  decompose task → N sub-goals (existing goal_decomposition_planner / DAG)
   ▼
[G1] SwarmOrchestrator  — for each sub-goal, DYNAMICALLY build a WorkerSpec:
   │     {system_prompt (rendered from the sub-goal), allowed_tools, mutation_budget,
   │      context_budget, owned_paths} → a SubagentFactory builds the executor
   ▼  submit as an ExecutionGraph to the EXISTING SubagentScheduler
SubagentScheduler (REUSE) — parallel, dependency-ordered, concurrency+RAM-gated
   │     each worker runs in:  [worktree fs sandbox (REUSE)]  +  [ScopedToolBackend cage (REUSE)]
   │                            + [G3 Ephemeral Memory Sandbox (NEW)]  + [GENERAL firewall if mutating]
   │
   │  [G2] AgentMessageBus (NEW) — workers exchange structured-JSON messages
   │        (artifact handoff / clarification) peer-to-peer, bounded + sanitized
   ▼
WorkUnitResult → CommandBus → MergeCoordinator (REUSE)  → verified artifact to the Commander
   ▼  [G3] on completion: Ephemeral Memory Sandbox GC'd (memory freed, worktree reaped)
```
The Fleet Commander never manually routes every message (G2 is peer-to-peer); workers are ephemeral + sandboxed (G3); their definitions are runtime-dynamic (G1). All on the existing scheduler + cage.

## 3. Guardrail G1 — Dynamic Agent Instantiation (the SwarmOrchestrator)
`autonomy/swarm_orchestrator.py` (new) + extend `WorkUnitSpec`.
- **Extend `WorkUnitSpec`** (additive, backward-compatible — existing fixed-type units unaffected when the new fields are None): `system_prompt_template: str | None`, `allowed_tools: tuple[str,...] | None`, `mutation_budget: int | None`, `context_budget_tokens: int | None`, `worker_role: str | None` (a free-form role label, NOT a hardcoded enum).
- **`SwarmOrchestrator.define_worker(sub_goal) -> WorkerSpec`** — at decompose time, derive each worker's definition PURELY from the sub-goal: render the system prompt (generalize the existing `render_general_system_prompt` into a `render_worker_system_prompt(role, goal, scope, allowed_tools, mutation_budget, read_only)`), select the tool allowlist (the minimal tools the sub-goal needs — read-only by default; mutation tools only if the sub-goal mutates + within budget), set the context/mutation budgets. **No hardcoded agent list** — the role + prompt + tools are computed from the sub-goal's requirements.
- **`SubagentFactory.build(worker_spec) -> Executor`** — construct the worker executor from the spec: a `ScopedToolBackend(allowed_tools=spec.allowed_tools, max_mutations=spec.mutation_budget)` (already parameterized) + the rendered system prompt + the context budget. Reuse the GENERAL executor path (dispatch_general + the Semantic Firewall) as the engine for a dynamically-defined mutating worker; the read-only EXPLORE path for read-only workers. The factory routes by capability (read-only vs mutating), not by a fixed type name.
- Submit the workers as an `ExecutionGraph` to the EXISTING `SubagentScheduler` (no new scheduler). Gated `JARVIS_SWARM_ORCHESTRATOR_ENABLED` (default false).
- **Fail-CLOSED:** a worker's allowed_tools is a strict allowlist (ScopedToolGate rejects anything else, pre-policy); mutation_budget is a hard count gate; an over-broad/unparseable spec → fall back to the minimal read-only worker (never a more-capable one). A dynamically-defined worker can NEVER exceed the cage.

## 4. Guardrail G2 — The Sovereign Inter-Agent Message Bus
`autonomy/agent_message_bus.py` (new). The genuine new transport — reuse the CommandBus's bounded-heap discipline.
- **`@dataclass AgentMessage`** (structured JSON): `msg_id, from_agent, to_agent (or topic), kind (ARTIFACT_HANDOFF | CLARIFICATION_REQUEST | CLARIFICATION_RESPONSE | FINDING | STATUS), payload (bounded JSON), correlation_id, ttl_s, ts`. Schema-versioned.
- **`AgentMessageBus`** — async pub/sub scoped to a swarm graph: `send(msg)`, `subscribe(agent_id) -> queue`, `request(to_agent, payload, timeout) -> response` (clarification round-trip). Reuse the CommandBus patterns: **bounded per-agent inbox (backpressure, drop-oldest + a single lag signal), idempotency dedup, TTL expiry.** Workers exchange artifacts/clarifications DIRECTLY (peer-to-peer) — the Commander is not in the loop for every message.
- **Sanitization (air-gap-aligned, fail-CLOSED):** inter-agent message payloads are UNTRUSTED (a worker could be prompt-injected). Reuse the conversation_bridge Tier -1 sanitizer pattern — a received message is data, NEVER authority: it cannot grant tools, raise a mutation budget, alter another worker's scope, or carry governance directives. Bounded size. A message to a dead/unknown/expired agent → dropped + logged (never blocks the sender). The bus is advisory coordination, not a control channel.
- **Reuse option (chosen):** a dedicated `AgentMessageBus` scoped per-graph (cleaner isolation + GC with the graph) rather than overloading the global TrinityEventBus (which is inter-component + persistent). Mirror the CommandBus bounded-heap; do NOT duplicate its L1-command semantics. Gated `JARVIS_SWARM_MESSAGE_BUS_ENABLED` (default false).

## 5. Guardrail G3 — The Ephemeral Memory Sandbox
`autonomy/ephemeral_memory_sandbox.py` (new) + wire into the scheduler's per-unit lifecycle.
- **`EphemeralMemorySandbox`** — per-worker, attached to the graph execution state: a **bounded** store of the worker's context/conversation turns (bounded deque, `max_turns` + `max_tokens` + per-turn TTL, all env). The worker's memory lives HERE, isolated from the global Ouroboros state (the global state, the SemanticIndex, the conversation_bridge ring are NOT written by a worker — a worker reads a snapshot, writes only to its sandbox).
- **Isolation:** composes with the EXISTING worktree fs-sandbox + the ScopedToolBackend cage — G3 adds the *memory/context* isolation the fs-sandbox lacks. A worker cannot pollute global state: its writes are scoped to its worktree (fs) + its sandbox (memory) + its mutation budget (tools).
- **Garbage collection (the operator constraint):** on worker completion (success OR failure), in the scheduler's existing per-unit `finally`: return the verified artifact (existing WorkUnitResult path), then **GC the sandbox** — clear the bounded store, drop the AgentMessageBus inbox, `WorktreeManager.cleanup` (existing), and an explicit memory release. Token-bloat prevention: the worker's context never accretes into the Commander's window — only the *verified artifact* (the patch + a bounded summary) returns. `sandbox_stats()` for observability.
- **Fail-CLOSED:** the GC runs in `finally` (guaranteed, like the worktree cleanup) — a crashed worker's sandbox is still reaped (compose with `reap_orphans` for SIGKILL/OOM). A sandbox that exceeds `max_tokens` → the worker is bounded (oldest turns evicted) + logged, never an unbounded grow. Gated `JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED` (default false).

## 6. Alignment with the Sovereign Laws (cross-cutting)
- **Fail-CLOSED:** dynamic workers can only ever be LESS capable than the cage (strict tool allowlist + mutation budget; over-broad spec → minimal read-only fallback). Inter-agent messages are sanitized/advisory (never authority). The sandbox GC is `finally`-guaranteed. Any error → the more-restrictive path.
- **Air-Gapped-aligned:** each worker runs in its worktree (fs) + scoped tools + (if mutating) the GENERAL Semantic Firewall (injection detectors, recursion ban, output quarantine, hard-kill). A worker can't reach beyond its sandbox; inter-agent messages can't carry governance authority. The swarm doesn't widen the blast radius — it parallelizes within the existing cage.
- **Governed by the existing ladder:** every worker's output flows through the SAME Iron Gate + risk-tier ladder + SemanticGuardian + VALIDATE/VERIFY as a solo op (the swarm parallelizes generation, not the gates). The SensorGovernor (global op cap) + MemoryPressureGate + concurrency_limit + parallel_dispatch eligibility cap fan-out — a runaway swarm is structurally impossible. Cross-repo workers still hit the Immutable Orange Protocol (a swarm can't merge the Mind/Nerves).
- **Observability (Manifesto §7):** swarm topology + per-worker state + the message bus surface via the existing SSE observability (additive `swarm.*` event types) → the Command Node dashboard can visualize the fleet.
- **Default-OFF + reuse-first:** 3 master flags default false; OFF → today's solo/fixed-subagent behavior byte-identical. New code = `swarm_orchestrator` + `agent_message_bus` + `ephemeral_memory_sandbox` + the `WorkUnitSpec` extension + the `render_worker_system_prompt` generalization. Everything else reused.

## 7. Phasing
1. **Phase 1a — G1 dynamic instantiation:** `WorkUnitSpec` extension + `render_worker_system_prompt` + `SubagentFactory` + `SwarmOrchestrator.define_worker`, submitting to the existing scheduler. Tests. (Bus + sandbox default-off; workers run isolated-but-silent first.)
2. **Phase 1b — G3 ephemeral sandbox:** the memory sandbox + GC-on-completion wired into the scheduler `finally`. Tests.
3. **Phase 1c — G2 agent message bus:** the peer-to-peer structured-JSON bus + sanitization. Tests + a security review (untrusted inter-agent payloads = a real surface).
4. **Phase 1d — observability:** the `swarm.*` SSE events → the Command Node fleet view.
5. Each phase: gated default-OFF, reuse-first, fail-CLOSED, then a soak before any flag flip.

## 8. Tests (per guardrail)
- **G1:** `define_worker` derives prompt/tools/budget from a sub-goal (no hardcoded list); the factory builds a ScopedToolBackend with the spec's allowlist; an over-broad/unparseable spec → minimal read-only fallback (fail-CLOSED); a worker exceeding its tool allowlist / mutation budget → POLICY_DENIED (existing cage); existing fixed-type units unaffected (WorkUnitSpec new fields None → byte-identical).
- **G2:** AgentMessage round-trip (send/subscribe/request); bounded inbox (drop-oldest + lag signal); TTL expiry; dedup; a message to a dead/unknown agent → dropped+logged, sender unblocked; **sanitization: an inter-agent payload cannot grant tools / raise budget / alter scope / carry authority** (the load-bearing security test); per-graph scope (no cross-graph leakage).
- **G3:** sandbox bounded (max_turns/max_tokens evict oldest); worker memory isolated from global state (a worker write doesn't touch the global SemanticIndex/conversation ring); **GC on completion (success AND failure) frees the sandbox + reaps the worktree (finally-guaranteed)**; a crashed worker's sandbox reaped via reap_orphans; only the verified artifact returns (no context bloat into the Commander).
- **Cross-cutting:** master-OFF → byte-identical solo/fixed-subagent behavior; a swarm cannot exceed the SensorGovernor op cap / MemoryPressureGate / concurrency_limit; a cross-repo swarm worker still hits Immutable Orange; every worker output still passes the Iron Gate + risk-tier.

## 9. Open Decisions (for operator review)
1. **Message bus transport:** a dedicated per-graph `AgentMessageBus` (chosen — cleaner GC + isolation) vs. extending the global TrinityEventBus with `agent.*` topics (more reuse, but persistent + cross-component). Confirm the per-graph choice.
2. **Worker engine for mutating dynamic workers:** route through the existing GENERAL executor + Semantic Firewall (chosen — reuses the proven mutation cage) vs. a new executor. Confirm.
3. **Clarification round-trip policy:** allow worker→worker `CLARIFICATION_REQUEST` with a bounded timeout (chosen) — or restrict workers to artifact-handoff only (no live dialogue) for a simpler Phase 1? *Spec assumes bounded clarification allowed.*
4. **First fan-out scale:** the initial `concurrency_limit` / max-workers for a swarm (reuse parallel_dispatch's default 3, or higher)? *Spec assumes the existing governed default.*
