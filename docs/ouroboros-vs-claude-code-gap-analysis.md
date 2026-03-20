# Ouroboros vs. Claude Code — Deep Gap Analysis + Disconnected Wires
> Date: 2026-03-20
> Source: Live Claude Code docs (docs.anthropic.com) + full Ouroboros codebase audit

---

## What This Is

Claude Code is a production AI coding agent. Ouroboros is JARVIS's autonomous self-programming governance pipeline. They solve the same core problem — "AI agent that can read and modify code" — from opposite ends:

- **Claude Code**: Reactive harness. Waits for human input. Excels at per-tool interception, permission enforcement, and contextual memory injection.
- **Ouroboros**: Proactive governance. Doesn't wait. Excels at autonomous operation, trust graduation, multi-repo coordination, and self-correction.

The goal here is not to copy Claude Code — it's to identify the harness-layer capabilities CC has that Ouroboros needs to be a safe and controllable autonomous system.

---

## Part 1: What Claude Code Has (Full Technical Picture)

### 1.1 Hook Event System — 18+ Events

CC's hooks fire at every meaningful execution boundary. Shell commands run with access to full tool I/O via stdin/stdout/stderr and exit codes.

```
PreToolUse           — fires BEFORE every tool call (Bash, Read, Edit, Write, etc.)
PostToolUse          — fires AFTER every successful tool call (tool_input + tool_response)
PostToolUseFailure   — fires when a tool errors
SubagentStart        — fires when a subagent is spawned
SubagentStop         — fires when a subagent finishes (includes last_assistant_message)
WorktreeCreate       — fires when a git worktree is created (custom VCS setup)
WorktreeRemove       — fires when a git worktree is removed (custom teardown)
TaskCompleted        — multi-agent team hook (task_id, task_subject, teammate_name, team_name)
TeammateIdle         — teammate coordination (teammate_name, team_name)
PreCompact           — fires before context window compaction
PostCompact          — fires after compaction (with custom_instructions)
Elicitation          — fires when MCP server asks user a structured question
ElicitationResult    — fires after user responds to MCP elicitation
ConfigChange         — fires when settings change
InstructionsLoaded   — fires when CLAUDE.md / skill files are loaded
UserPromptSubmit     — fires when user submits a message
SessionStart         — fires on startup/resume/clear/compact
SessionEnd           — fires when session ends
Stop                 — fires when Claude finishes (has last_assistant_message)
StopFailure          — fires when Claude fails to stop cleanly
```

**Any hook can block/modify the tool call by returning a non-zero exit code.** PreToolUse hooks can veto tool execution before it happens.

### 1.2 Declarative Permission System

Rules are `Tool` or `Tool(specifier)` with glob patterns. Evaluated in order: **deny first, then ask, then allow. First match wins.**

```json
{
  "permissions": {
    "allow": ["Bash(npm run test *)", "Bash(git status)", "Read(~/.zshrc)", "Edit(src/**)"],
    "ask":   ["Bash(git push *)", "Edit(config/**)"],
    "deny":  ["Bash(rm -rf *)", "Bash(curl *)", "Edit(**/.env*)", "Edit(./secrets/**)"]
  }
}
```

**Critical**: Deny rules apply **even in bypassPermissions mode**. They are unconditional hard blocks — equivalent to Ouroboros's `BLOCKED` tier, but user-configurable without code changes.

SDK adds `canUseTool(toolName, toolInput) → Promise<bool | PermissionDecision>` — a runtime programmatic callback that fires for every single tool call.

### 1.3 Memory Hierarchy — 3 Levels

```
~/.claude/CLAUDE.md              # Global: applies to every session everywhere
<project>/CLAUDE.md              # Project: applies when working in this repo (committed)
<project>/.claude/CLAUDE.md      # Local: gitignored personal overrides
```

- **Auto-injected** at every session start, survives `/compact` and context resets
- **Auto-memory**: Claude can write to memory files automatically when it learns from user corrections
- Subagents can maintain their own auto-memory independently
- `settingSources` in SDK controls which levels are loaded

### 1.4 Contextual Skill Injection (Superpowers)

SKILL.md files with frontmatter patterns. When the agent edits a file matching `filePattern` or runs a command matching `bashPattern`, the skill's full instructional content is injected into context. Skills deduplicated per session.

### 1.5 Subagents with Worktree Isolation

- Each subagent: own context window, own system prompt, own tool access, own permissions
- `isolation: worktree` in frontmatter → own git worktree (separate branch, separate files, shared history)
- Worktrees auto-cleaned after completion
- `WorktreeCreate`/`WorktreeRemove` hooks for custom VCS lifecycle
- Multiple subagents run concurrently — only final message returns to parent

### 1.6 Elicitation — Structured Mid-Operation User Input

Via MCP: the AI can ask the user a structured question mid-operation and wait for a typed response. `Elicitation` hook fires with `message`, `requested_schema`, `mcp_server_name`. `ElicitationResult` hook fires after user responds. Hooks can observe, modify, or block the response before it goes back to the MCP server.

### 1.7 Multi-Level Config Inheritance

```
~/.claude/settings.json           # Global defaults
<project>/.claude/settings.json   # Project overrides (committed)
<project>/.claude/settings.local.json  # Local overrides (gitignored)
```

Later levels override earlier ones. Teams share project config; individuals have personal overrides.

---

## Part 2: Ouroboros Strengths (What CC Doesn't Have)

Before gaps: Ouroboros is genuinely more advanced than CC in these areas. Don't lose them.

| Capability | File | Notes |
|---|---|---|
| Trust graduation | `autonomy/graduator.py` | OBSERVE→SUGGEST→GOVERNED→AUTONOMOUS, data-driven |
| Multi-system autonomy gate | `autonomy/gate.py` | CAI + UAE + SAI — checks cognitive load, RAM, screen lock before proceeding |
| Proactive sensors | `intake/sensors/` | Finds bugs and opportunities WITHOUT being asked |
| Multi-repo sagas | `saga/`, `multi_repo/` | Atomic cross-repo operations with blast radius + merge coordination |
| 4-mode degradation | `degradation.py` | FULL_AUTONOMY → REDUCED → READ_ONLY → EMERGENCY_STOP |
| Canary rollout | `canary_controller.py` | Slice-based promotion with rollback rate + p95 latency gate |
| Shadow harness | `shadow_harness.py` | Side-effect-free parallel execution for validation |
| Repair engine | `repair_engine.py` | Auto-classifies failures, attempts repair before escalating |
| Operation ledger + WAL | `ledger.py` + `intake/wal.py` | Durable JSONL audit trail + crash-recovery WAL |
| Preemption FSM | `preemption_fsm.py` | Full LoopState × LoopEvent matrix with durable checkpointing |
| Voice integration | `comms/voice_narrator.py` + `voice_command_sensor.py` | Narrates ops, accepts voice commands |
| MCP outbound client | `mcp_tool_client.py` | Creates GitHub issues/PRs post-operation |
| Model attribution | `model_attribution_recorder.py` | Tracks which model generated which change |
| Patch benchmarker | `patch_benchmarker.py` | Compares multiple candidates |
| Curriculum publisher | `curriculum_publisher.py` | Post-op learning feedback loop |

---

## Part 3: Gaps — What CC Has That Ouroboros Needs

### GAP 1: Per-Tool Hook System (PreToolUse / PostToolUse)
**Severity: CRITICAL**

**What CC has:** Hooks fire before/after EVERY individual tool call. You can block a Bash command before it runs. You can run a formatter after every Edit. You can log every file write to an audit stream.

**What Ouroboros has:** `CommProtocol` fires at operation boundaries: INTENT (before the whole op), HEARTBEAT (phase transitions), DECISION (after everything). No individual tool-call interception.

**The gap:** If J-Prime generates code that calls `rm -rf` as part of a patch, Ouroboros can only block it at the operation level BEFORE generation. There's no way to intercept the actual execution of dangerous commands at the tool-call level and cancel just that tool while allowing the rest.

**How to fill it:**
Add a `ToolCallHook` layer in `SubagentScheduler.execute()` and the `ChangeEngine`. Format:
```python
# governance/tool_hook_registry.py
class ToolCallHookRegistry:
    def register_pre(self, tool_name: str, pattern: str, handler: Callable) -> None: ...
    def register_post(self, tool_name: str, pattern: str, handler: Callable) -> None: ...
    async def run_pre(self, tool_name: str, tool_input: dict) -> HookDecision: ...
    async def run_post(self, tool_name: str, tool_input: dict, result: Any) -> None: ...
```
Wire it into `ChangeEngine.apply()` and `test_runner.py` before each subprocess/file-write call. Load hook registrations from `OUROBOROS_HOOKS_CONFIG` YAML (same structure as CC's hooks format for conceptual parity).

---

### GAP 2: Declarative Permission Rules from Config
**Severity: CRITICAL**

**What CC has:** `allow`/`ask`/`deny` rules as `Tool(glob)` patterns in JSON. User-configurable without code changes. Deny rules are unconditional even in bypass mode.

**What Ouroboros has:** `RiskEngine` with hard-coded Python logic. `OperationProfile` fields (touches_security_surface, blast_radius, etc.). Cannot add "deny edits to `migrations/`" without modifying source code.

**How to fill it:**
Add `PolicyEngine` that reads `~/.jarvis/policy.yaml` + `<repo>/.jarvis/policy.yaml`. Runs before `RiskEngine`. Format:
```yaml
permissions:
  deny:
    - tool: edit
      pattern: "**/.env*"
    - tool: bash
      pattern: "rm -rf *"
  ask:
    - tool: edit
      pattern: "backend/core/**"
  allow:
    - tool: bash
      pattern: "pytest *"
    - tool: edit
      pattern: "tests/**"
```
Integrate in `orchestrator.py` CLASSIFY phase: `PolicyEngine.classify(op_context)` → BLOCKED/APPROVAL_REQUIRED/SAFE_AUTO. Falls through to `RiskEngine` if no match.

---

### GAP 3: OUROBOROS.md Hierarchical Memory Injection
**Severity: HIGH**

**What CC has:** `~/.claude/CLAUDE.md` + `<repo>/CLAUDE.md` + local override. Auto-injected every invocation. Survives compaction. Human-authored, plain Markdown. Zero friction to update.

**What Ouroboros has:** `TheOracle` (semantic code indexer, structural relationships). Context expander adds related files. Neither injects *human-authored instructions* into generation prompts.

**How to fill it:**
New class `ContextMemoryLoader` that reads at each operation start:
1. `~/.jarvis/OUROBOROS.md` — global instructions (always injected)
2. `<repo>/OUROBOROS.md` — project-specific constraints (injected per repo)
3. `<repo>/.jarvis/OUROBOROS.md` — gitignored personal overrides

Add `human_instructions: str` field to `OperationContext`. Both `PrimeProvider._build_codegen_prompt()` and `ClaudeProvider._build_codegen_prompt()` prepend this block before the task description. Two files changed, massive impact on generation quality.

---

### GAP 4: Contextual Skill Injection
**Severity: HIGH**

**What CC has:** SKILL.md files matched by `filePattern`/`bashPattern`. When editing a `.tsx` file, "React best practices" skill auto-injects. Session-deduped.

**What Ouroboros has:** `brain_selection_policy.yaml` routes to backends. No domain-specific instructional content loaded by file type.

**How to fill it:**
Add `SkillRegistry` that loads `<repo>/.jarvis/skills/*.yaml`:
```yaml
# .jarvis/skills/migrations.yaml
name: migration_safety
filePattern: "migrations/**"
instructions: |
  Always create the migration in a transaction.
  Never drop columns in the same migration that removes all usages.
  Always include a rollback method.
```
`ContextExpander` checks `SkillRegistry` for target files and appends matching skill instructions to `OperationContext.human_instructions`. Composes naturally with GAP 3.

---

### GAP 5: Structured Mid-Operation Elicitation
**Severity: MEDIUM-HIGH**

**What CC has:** The AI can pause mid-operation, ask the user a structured question (`Elicitation` event), and wait for a typed response before continuing. Hooks can observe/modify/block the response.

**What Ouroboros has:** `CLIApprovalProvider` allows APPROVE/REJECT. But it's binary — it can't ask "should I use approach A or B?" and accept a typed answer. The `ApprovalProvider` protocol has no `ask_question(question, options)` method.

**How to fill it:**
Extend `ApprovalProvider` protocol with:
```python
async def elicit(
    self,
    request_id: str,
    question: str,
    options: Optional[List[str]] = None,
    schema: Optional[dict] = None,
    timeout_s: float = 300.0,
) -> ElicitationResult: ...
```
Add `CLIElicitationProvider` (stdin/stdout for CLI) and `VoiceElicitationProvider` (asks via TTS, captures voice response). Wire into `orchestrator.py` at the APPROVE phase — if APPROVAL_REQUIRED, optionally elicit clarification before asking for final approval.

---

### GAP 6: Interactive Interrupt Wiring
**Severity: MEDIUM**

**What CC has:** Escape key / Ctrl+C → graceful interrupt of in-progress operation with clean rollback. Hooks (`Stop`, `SubagentStop`) fire with the final message.

**What Ouroboros has:** `PreemptionFsmEngine` and `PreemptionFsmExecutor` — the full FSM machinery for preemption is built. The `LoopEvent.PREEMPT` event is defined and handled. **But nothing actually emits it from user input.**

**How to fill it — this is a wiring job, not a design job:**
```python
# governance/user_signal_bus.py (new, ~30 lines)
class UserSignalBus:
    def __init__(self): self._stop = asyncio.Event()
    def request_stop(self): self._stop.set()
    async def wait_for_stop(self): await self._stop.wait()
```
In `GovernedLoopService.submit()`, wrap the orchestrator call:
```python
stop_task = asyncio.create_task(self._user_signal_bus.wait_for_stop())
op_task = asyncio.create_task(self._orchestrator.run(ctx))
done, _ = await asyncio.wait([stop_task, op_task], return_when=asyncio.FIRST_COMPLETED)
if stop_task in done:
    await self._fsm_executor.advance(LoopEvent.PREEMPT, ctx)
```
Wire `VoiceCommandSensor` to emit `UserSignalBus.request_stop()` when it detects "JARVIS stop" or "JARVIS cancel".

---

### GAP 7: Multi-Level Config Inheritance
**Severity: MEDIUM**

**What CC has:** `~/.claude/settings.json` → `.claude/settings.json` → `.claude/settings.local.json`. Team commits project config; individuals have gitignored local overrides.

**What Ouroboros has:** `GovernedLoopConfig.from_env()` — one level, env vars only.

**How to fill it:**
Extend `GovernedLoopConfig.from_env()`:
1. Load `~/.jarvis/governance.yaml` (global defaults)
2. Load `<repo>/.jarvis/governance.yaml` (project overrides, committed)
3. Load `<repo>/.jarvis/governance.local.yaml` (personal, gitignored, add to .gitignore)
4. Env vars win over all file-based config (current behavior preserved)

Low risk. One method change. High team usability.

---

### GAP 8: Auto-Memory (AI Learns from Corrections)
**Severity: MEDIUM**

**What CC has:** When a user corrects Claude mid-session ("don't use that pattern, use this one"), Claude can update its own memory files automatically. The correction persists to future sessions without the human having to update CLAUDE.md manually.

**What Ouroboros has:** `CurriculumPublisher` — publishes operation outcomes to a learning feedback loop (post-op analytics). `LearningBridge` — records goal+file+error patterns for future operations. Neither responds to *human corrections within an operation*.

**How to fill it:**
When `CLIApprovalProvider.reject()` is called with a reason, extract the correction and append it to `<repo>/OUROBOROS.md` under a `## Auto-Learned Corrections` section. Format:
```markdown
## Auto-Learned Corrections
- 2026-03-20 op:abc-123: Don't use `subprocess.run` in async context — use `asyncio.create_subprocess_exec`
```
This feeds directly into GAP 3's memory injection.

---

### GAP 9: Subagent Git Worktree Isolation
**Severity: LOW-MEDIUM**

**What CC has:** `isolation: worktree` — each subagent gets its own git worktree (own branch, own working tree, shared history). Parallel subagents never conflict at the filesystem level. Auto-cleaned after completion.

**What Ouroboros has:** `SubagentScheduler` (L3) uses `WorkUnitSpec.owned_paths` for logical path ownership (dedup/conflict detection). Validation runs in temp dirs. But actual git worktrees are not created per work unit in the current active code.

**How to fill it:**
In `SubagentScheduler.execute()`, before starting a work unit:
```python
worktree_path = await create_git_worktree(
    repo_root=self._repo_roots[unit.repo],
    branch_name=f"ouroboros/wu-{unit.unit_id}",
)
# Execute in worktree_path, merge back on completion
```
Wire `ToolCallHookRegistry` (GAP 1) to fire worktree create/remove events.

---

### GAP 10: MCP Inbound Server
**Severity: LOW-MEDIUM**

**What CC has:** CC is both MCP client (calls external MCP servers) AND MCP server (external tools can call into CC). IDE extensions and CI systems can submit work to Claude Code via MCP.

**What Ouroboros has:** `mcp_tool_client.py` — outbound-only. Calls GitHub to create issues/PRs. No inbound MCP endpoint.

**How to fill it:**
Add `OuroborosMCPServer` (FastAPI + `mcp` library) that exposes:
```python
@mcp_server.tool("submit_intent")
async def submit_intent(goal: str, target_files: List[str], repo: str = "jarvis") -> dict: ...

@mcp_server.tool("get_operation_status")
async def get_operation_status(op_id: str) -> dict: ...

@mcp_server.tool("approve_operation")
async def approve_operation(request_id: str, approver: str = "mcp_client") -> dict: ...
```
Wire to `IntakeLayerService` intake queue. Makes Ouroboros driveable from any MCP client (IDE, CI, other agents).

---

## Part 4: Disconnected Wires — Files That Exist But Aren't Plugged In

These are the most important findings — existing code that was built but never connected to the live pipeline.

---

### WIRE 1: `shadow_harness.py` — FULLY ORPHANED
**File:** `governance/shadow_harness.py`
**Status:** Built, exported from `__init__.py`, NEVER called by any pipeline code.

**Evidence:**
- `ShadowHarness`, `SideEffectFirewall`, `ShadowResult` only appear in `__init__.py` as re-exports
- `op_context.py:468` has `shadow: Optional[ShadowResult] = None` on OperationContext — a placeholder that is never populated
- Zero imports of `ShadowHarness` in `orchestrator.py` or `governed_loop_service.py`

**What it does:** Runs candidate code in a sandboxed parallel environment with monkey-patched builtins to block filesystem writes and subprocess spawns. Compares output (EXACT / AST / SEMANTIC). Tracks confidence over time, auto-disqualifies candidates with repeat failures.

**Why it matters:** This is the "test the change before applying it" safety layer. Without it, changes go directly from GENERATE → VALIDATE with only AST parsing and test runs for protection.

**Fix:** In `orchestrator.py`, between the GENERATE and GATE phases:
```python
if self._shadow_harness is not None:
    shadow_result = self._shadow_harness.run(candidate_code, expected_output)
    ctx = ctx.with_shadow_result(shadow_result)
    if not shadow_result.passed:
        # Soft-block — surface to GATE for decision, don't hard-fail
        ctx = ctx.with_telemetry(shadow_confidence=shadow_result.confidence)
```
Add `shadow_harness: Optional[ShadowHarness] = None` to `OrchestratorConfig`.

---

### WIRE 2: `tool_use_enabled = False` — CORE FEATURE DISABLED
**File:** `governance/governed_loop_service.py:568`
**Status:** Feature-flagged off. Default env: `JARVIS_GOVERNED_TOOL_USE_ENABLED=false`

**What it does:** When enabled, Ouroboros runs a tool-use loop where the AI can call tools (bash, file read/write, etc.) as first-class actions instead of generating a diff patch. This is how Claude Code fundamentally works.

**Why it matters:** Claude Code is a tool-calling loop at its core. Ouroboros's primary path is LLM-generates-patch. With `tool_use_enabled`, Ouroboros can operate the same way CC does. This is the biggest architectural gap.

**Fix:** Set `JARVIS_GOVERNED_TOOL_USE_ENABLED=true` in `.env`. Then wire GAP 1 (ToolCallHookRegistry) so every tool call in the tool-use loop gets pre/post hooks. Without the hook layer, enabling tool-use means no per-call visibility or blocking.

---

### WIRE 3: `l3_enabled = False` — SubagentScheduler Disabled
**File:** `governance/governed_loop_service.py:575`
**Status:** Feature-flagged off. Default env: `JARVIS_GOVERNED_L3_ENABLED=false`

**What it does:** L3 is the `SubagentScheduler` — parallel execution graph with work unit DAGs, dependency resolution, merge coordination. Multiple files can be worked on simultaneously with proper ordering.

**Fix:** Set `JARVIS_GOVERNED_L3_ENABLED=true` in `.env`. Requires L3 state dir to exist.

---

### WIRE 4: `l4_enabled = False` — Advanced Coordination Disabled
**File:** `governance/governed_loop_service.py:580`
**Status:** Feature-flagged off. Default env: `JARVIS_GOVERNED_L4_ENABLED=false`

**What it does:** L4 is `advanced_coordination.py` — higher-level multi-agent coordination above L3.

**Fix:** Set `JARVIS_GOVERNED_L4_ENABLED=true` in `.env` after L3 is stable.

---

### WIRE 5: `canary_controller.py` Slice Metrics — PARTIALLY WIRED
**File:** `governance/canary_controller.py` + `governed_loop_service.py`
**Status:** Slices are registered and pre-activated at boot. But runtime metric tracking is never updated.

**Evidence:**
- `_register_canary_slices()` calls `register_slice()` and pre-activates state = ACTIVE
- The canary's `SliceMetrics` (operation count, rollback rate, p95 latency, stability window) are never updated after operations complete
- `DomainSlice.state` never transitions from ACTIVE to SUSPENDED based on real runtime data
- The promotion criteria (50 ops, <5% rollback, <120s p95, 72h stability window) exist in code but are never evaluated at runtime

**Fix:** In `GovernedLoopService` after `submit()` resolves, call:
```python
self._stack.canary.record_operation(
    slice_prefix=_infer_canary_slice(ctx.target_files),
    success=(terminal_phase == OperationPhase.COMPLETE),
    duration_s=total_duration_s,
    rollback_occurred=rollback_occurred,
)
```
This makes canary promotion data-driven rather than purely boot-time configured.

---

### WIRE 6: `DegradationController` — Wired in Stack, GLS Doesn't React
**File:** `governance/degradation.py` via `integration.py`
**Status:** `DegradationController` is built and lives in `GovernanceStack`. But `GovernedLoopService` never queries it.

**Evidence:**
- GLS determines ACTIVE/DEGRADED from `self._generator.fsm.state` (FailbackState from provider availability)
- The 4-mode degradation (FULL_AUTONOMY → REDUCED_AUTONOMY → READ_ONLY_PLANNING → EMERGENCY_STOP) is tracked by DegradationController in the stack
- GLS's `_preflight_check()` doesn't check `self._stack.degradation.current_mode`

**Fix:** In `GovernedLoopService._preflight_check()`, add:
```python
deg_mode = getattr(getattr(self._stack, 'degradation', None), 'current_mode', None)
if deg_mode is not None:
    if deg_mode >= DegradationMode.READ_ONLY_PLANNING:
        return _block("degradation:read_only_planning")
    if deg_mode >= DegradationMode.REDUCED_AUTONOMY:
        # Only allow SAFE_AUTO ops in reduced autonomy
        if risk_tier != RiskTier.SAFE_AUTO:
            return _block("degradation:reduced_autonomy_non_safe")
```

---

### WIRE 7: `reasoning_chain_bridge.py` — Conditionally Active
**File:** `governance/reasoning_chain_bridge.py`
**Status:** Conditionally wired. Active only when `get_reasoning_chain_orchestrator()` returns a live instance.

**What it does:** Routes the operation through the reasoning chain orchestrator (DETECT → EXPAND → MIND → COORDINATE) before brain selection. Converts chain decisions into PLAN messages.

**Current state:** Works when the reasoning chain is up. Falls back silently to direct brain selection when not. This is correct behavior but worth knowing — the quality of brain routing depends on whether the reasoning chain is alive.

---

### WIRE 8: `event_bridge.py` — Check Cross-Repo Propagation
**File:** `governance/event_bridge.py`
**Status:** Implements `GovernanceEventMapper` but it's unclear if it's wired into `CommProtocol`'s transport chain.

**What it does:** Maps CommMessages (INTENT/DECISION/POSTMORTEM) to CrossRepoEvents for propagation to PRIME and REACTOR repos.

**Check needed:** Verify `GovernanceEventMapper` is registered as a transport in `CommProtocol` during `_build_comm_protocol()`. If not, cross-repo event propagation is silently missing.

---

## Part 5: Priority Implementation Order

### Tier 1 — Enable existing code (no new design needed)

| Task | File | Effort | Impact |
|---|---|---|---|
| Enable `tool_use_enabled=true` in `.env` | `.env` | 1 line | CRITICAL — core CC parity |
| Enable `l3_enabled=true` in `.env` | `.env` | 1 line | HIGH — parallel subagents |
| Wire shadow_harness into orchestrator VALIDATE phase | `orchestrator.py` | ~20 lines | HIGH — safety net activated |
| Update canary slice metrics post-operation | `governed_loop_service.py` | ~15 lines | MEDIUM — canary math works |
| Wire DegradationController into _preflight_check | `governed_loop_service.py` | ~20 lines | MEDIUM — degradation gates ops |
| Verify EventBridge is in CommProtocol transport chain | `integration.py` | read + 2 lines if missing | MEDIUM — cross-repo propagation |

### Tier 2 — New small files (1 day each)

| Task | New File | Effort | Impact |
|---|---|---|---|
| OUROBOROS.md memory injection | `governance/context_memory_loader.py` | Small | HIGH — human instruction channel |
| Skill injection from .jarvis/skills/ | `governance/skill_registry.py` | Small | HIGH — domain guidance |
| UserSignalBus + FSM wiring | `governance/user_signal_bus.py` | Small | MEDIUM — interactive interrupt |
| Auto-memory from rejection reasons | extend `approval_provider.py` | Small | MEDIUM — learns from corrections |

### Tier 3 — New systems (2-5 days each)

| Task | New File | Effort | Impact |
|---|---|---|---|
| PolicyEngine (declarative permission rules) | `governance/policy_engine.py` | Medium | CRITICAL — user-configurable permissions |
| ToolCallHookRegistry | `governance/tool_hook_registry.py` | Medium | CRITICAL — per-tool interception |
| Multi-level config inheritance | extend `GovernedLoopConfig.from_env()` | Medium | MEDIUM — team usability |
| Elicitation (structured mid-op user input) | extend `approval_provider.py` | Medium | MEDIUM — richer human-AI dialogue |
| Subagent git worktree per work unit | extend `subagent_scheduler.py` | Medium | LOW-MEDIUM — parallel isolation |
| OuroborosMCPServer (inbound MCP) | `governance/mcp_server.py` | Medium | LOW-MEDIUM — external drivability |

---

## Part 6: The Core Insight

Claude Code's harness is built on two foundations:

1. **Interception at every layer** — hooks fire before/after every action; nothing bypasses the hook chain
2. **Human instruction injection** — CLAUDE.md and skills ensure human knowledge enters every generation

Ouroboros has none of #1 (it has operation-level events, not tool-call-level events) and none of #2 (oracle gives code structure, not human instructions).

The good news: Ouroboros's architecture makes both gaps **solvable additions**, not architectural rewrites. The FSM, approval provider, comm protocol, and trust graduation system are all wiring-ready. The missing pieces are the connectors, not the components.

---

## Part 7: Research-Backed Enhancement Opportunities for Ouroboros

Two research papers were analyzed for applicable insights. Neither is a how-to guide for Ouroboros, but both contain specific architectural patterns that map directly to Ouroboros gaps identified in Parts 2–4.

---

### 7.1 From: Khemani (2025) — "Self-Programming AI: Code-Learning Agents for Autonomous Refactoring and Architectural Evolution"

**Source:** Research Square preprint, DOI: 10.21203/rs.3.rs-6688473/v1. High school student project, not peer-reviewed. Architecture is credible; results are small-scale (5 tasks, all <200 LOC).

**SPA Architecture (4 modules in a loop):**

```
Task Specification
    |
    v
[1] Task Planning Module
    GPT-3.5 + function-calling → generates initial code + autogenerated unit tests
    |
    v
[2] Execution & Evaluation Module
    pytest (test pass rate) + coverage.py (branch %) + Radon (cyclomatic complexity) + Pylint (lint count)
    → outputs a JSON metrics bundle
    |
    v
[3] Improvement Module
    LLM reads JSON metrics, identifies root cause of each failure, outputs AST-level patch suggestions
    |
    v
[4] Patch Application Engine
    LibCST (Concrete Syntax Tree) applies surgical patches — no whole-file replacement
    |
    v
    Loop back to [2] until exit criteria: 100% tests pass, ≥80% coverage, complexity ≤10, 0 lint errors
```

---

#### SPA Enhancement 1: Structured Metrics Bundle Between Pipeline Phases (HIGH VALUE)

**What SPA does:** After every evaluation, it produces a JSON document with exact numeric scores for test pass rate, branch coverage, cyclomatic complexity, and lint error count. The Improvement Module reads this JSON directly — the LLM prompt says "coverage is 71%, complexity is 14, 2 lint errors; fix specifically these issues."

**What Ouroboros does now:** `OperationContext.telemetry` carries some metrics but there is no standardized quality dimensions schema passed between VALIDATE and GENERATE. When a generation fails validation, the retry prompt says "validation failed" — it does not say "complexity=14, coverage=71%, 2 lint errors."

**The gap:** Ouroboros's generation retries are information-poor. The retry regenerates without knowing specifically what was wrong with the last attempt.

**Concrete implementation path:**

Add a `QualityMetrics` dataclass to `op_context.py`:

```python
@dataclass
class QualityMetrics:
    test_pass_rate: float = 0.0        # 0.0–1.0
    branch_coverage: float = 0.0      # 0.0–1.0
    cyclomatic_complexity: int = 0     # Radon CC score (target ≤10)
    lint_error_count: int = 0          # Pylint violations
    maintainability_index: float = 0.0 # Radon MI score (target ≥20)
    measured_at: str = ""              # phase name where measured

    def is_convergent(self) -> bool:
        return (
            self.test_pass_rate >= 1.0
            and self.branch_coverage >= 0.80
            and self.cyclomatic_complexity <= 10
            and self.lint_error_count == 0
        )

    def to_prompt_fragment(self) -> str:
        """Returns human-readable quality summary for injection into generation prompt."""
        return (
            f"Previous attempt quality: tests={self.test_pass_rate:.0%}, "
            f"coverage={self.branch_coverage:.0%}, "
            f"complexity={self.cyclomatic_complexity} (target ≤10), "
            f"lint_errors={self.lint_error_count}. "
            f"Focus on improving: {self._worst_dimension()}."
        )

    def _worst_dimension(self) -> str:
        if self.test_pass_rate < 1.0: return "test failures"
        if self.lint_error_count > 0: return "lint violations"
        if self.cyclomatic_complexity > 10: return "cyclomatic complexity"
        if self.branch_coverage < 0.80: return "branch coverage"
        return "none"
```

Add `quality_metrics: Optional[QualityMetrics] = None` to `OperationContext`. Wire it:
- **`verify_provider.py`** runs `radon cc`, `radon mi`, `pylint`, `pytest --cov` post-apply and populates `QualityMetrics`
- **`validate_provider.py`** populates it pre-apply (static analysis before patch lands)
- **`providers.py` `_build_codegen_prompt()`** injects `ctx.quality_metrics.to_prompt_fragment()` into every retry generation prompt

**Result:** On the second generation attempt, the LLM knows exactly what dimension failed and targets it. This is what SPA's Improvement Module does and it's the core reason SPA converges in 1–4 iterations instead of grinding.

---

#### SPA Enhancement 2: Code Quality Gates in VERIFY Exit Criteria (HIGH VALUE)

**What SPA does:** The loop only terminates when ALL four metrics meet threshold simultaneously — 100% tests, ≥80% coverage, complexity ≤10, 0 lint errors. Passing tests alone is not enough.

**What Ouroboros does now:** The `VERIFY` phase checks that tests pass after apply. It does not check coverage, complexity, or lint. An operation can reach `COMPLETE` with a 300-line function with cyclomatic complexity of 35 and zero test coverage of new branches.

**Concrete implementation path:**

In `verify_provider.py`, after running the test suite, run:

```python
import subprocess, json

def _measure_quality(changed_files: list[str]) -> QualityMetrics:
    # Run Radon CC on changed files
    cc_result = subprocess.run(
        ["radon", "cc", "--min", "A", "--json"] + changed_files,
        capture_output=True, text=True
    )
    cc_data = json.loads(cc_result.stdout) if cc_result.stdout else {}
    max_complexity = max(
        (block["complexity"] for file_data in cc_data.values() for block in file_data),
        default=0
    )

    # Run coverage.py on changed modules
    cov_result = subprocess.run(
        ["python", "-m", "pytest", "--cov", "--cov-report=json", "-q"],
        capture_output=True, text=True
    )
    cov_data = json.loads(Path(".coverage.json").read_text()) if Path(".coverage.json").exists() else {}
    coverage = cov_data.get("totals", {}).get("percent_covered_display", 0) / 100

    # Run Pylint
    lint_result = subprocess.run(
        ["pylint", "--output-format=json"] + changed_files,
        capture_output=True, text=True
    )
    lint_issues = len(json.loads(lint_result.stdout)) if lint_result.stdout else 0

    return QualityMetrics(
        cyclomatic_complexity=max_complexity,
        branch_coverage=coverage,
        lint_error_count=lint_issues,
    )
```

In `GovernedLoopService.submit()`, check after VERIFY:
```python
metrics = ctx.quality_metrics
if metrics and not metrics.is_convergent():
    # Don't fail — but log and optionally re-enter GENERATE with quality context
    logger.warning("Operation complete but quality below threshold: %s", metrics.to_prompt_fragment())
    # Trust Graduator gets a "quality_below_threshold" signal — slows promotion
    self._trust_graduator.record_quality_signal(ctx.target_files, metrics)
```

This does not block completion but feeds quality data into trust graduation. Files that repeatedly produce low-quality patches get slower trust promotion — exactly the feedback loop SPA demonstrates.

---

#### SPA Enhancement 3: AST-Awareness in Patch Application (MEDIUM VALUE)

**What SPA does:** LibCST applies patches at the Concrete Syntax Tree level — it knows the difference between adding a function, modifying a loop body, and changing a return statement. It never corrupts surrounding structure because it operates on the AST, not on text lines.

**What Ouroboros does now:** `change_engine.py` applies text-level unified diffs via Python's `difflib` or `patch`. This works reliably for small diffs but can fail on large refactors when context lines don't match exactly.

**Concrete implementation path:**

Add an optional LibCST path in `change_engine.py`:

```python
def apply_patch(self, target_file: Path, diff: str, use_cst: bool = False) -> ApplyResult:
    if use_cst and target_file.suffix == ".py":
        return self._apply_cst_patch(target_file, diff)
    return self._apply_text_patch(target_file, diff)

def _apply_cst_patch(self, target_file: Path, diff: str) -> ApplyResult:
    import libcst as cst
    try:
        source = target_file.read_text()
        tree = cst.parse_module(source)
        # Apply structured modification from diff description
        # ... transform pass ...
        new_source = tree.code
        target_file.write_text(new_source)
        return ApplyResult(success=True, method="cst")
    except cst.ParserSyntaxError as e:
        return ApplyResult(success=False, error=str(e), method="cst_failed")
```

Gate on `JARVIS_CHANGE_ENGINE_CST=true` in `.env`. Start using it for Python refactors where diff context match rate is below 95% (a signal the text patch is fragile).

---

#### SPA Enhancement 4: Convergence Loop with Escape Valve (MEDIUM VALUE)

**What SPA does:** Iterates the generate→evaluate loop until convergence. SPA's limitation is it has no escape valve — it can loop forever on hard cases.

**What Ouroboros has:** `max_generations` config in `GovernedLoopConfig`. But there's no partial-success concept — it either completes or fails.

**What SPA's limitation teaches:** Add a **partial-convergence state** to Ouroboros. If after N iterations quality is improving but hasn't converged, surface a `PARTIAL_COMPLETE` result with the metrics showing progress. The trust graduator treats this differently than a full success or a failure — it's a "promising but not done" signal that warrants a follow-up operation later.

```python
class OperationResult(Enum):
    COMPLETE = "complete"              # All quality gates pass
    PARTIAL_COMPLETE = "partial"       # Tests pass, quality improving, not converged
    FAILED = "failed"                  # Tests don't pass after max iterations
    ROLLED_BACK = "rolled_back"        # Applied then reverted
```

This prevents the system from either silently accepting low-quality patches or hard-failing on code that is correct but needs another pass.

---

### 7.2 From: James, Ene & Lenu (2025) — "Self-Programming Artificial Intelligence: Autonomous Learning and Evolutionary Algorithms"

**Source:** International Journal of Computer Science and Mathematical Theory (IJCSMT), Vol. 11, No. 5, 2025. DOI: 10.56201/ijcsmt.vol.11.no5.2025.pg77.92. Peer-reviewed journal article by Kenule Beeson Saro-Wiwa Polytechnic / Rivers State University, Nigeria.

**What this paper proposes:** A hybrid framework that combines:
- **Reinforcement Learning (PPO)** — agent refines behavior by interacting with environment and receiving rewards/penalties
- **Genetic Programming (GP)** — evolves a population of candidate programs through selection, crossover, and mutation; uses Pareto-front optimization across multiple objectives
- **Neural Architecture Search (NAS)** — automatically finds optimal model architectures without human tuning
- **Meta-Learning (MAML/Reptile)** — "learning to learn"; the system adapts to new tasks with minimal training data

**Experimental results (CartPole-v1, MountainCarContinuous-v0, Multi-Agent Task):**
- CartPole: 95% task completion at 30 iterations (baseline RL: 75%)
- MountainCar: 85% completion at 50 iterations (baseline: 65%)
- Adaptation time: decreased from 50→30 units across 5 iterations (40% faster convergence)
- Computational efficiency: improved from 80%→92% over iterations
- Code complexity: reduced 18% over 100 generations
- Overall resource consumption: ~40% reduction over prolonged usage vs RL-only

---

#### James et al. Enhancement 1: Pareto-Front Brain Selection — Multi-Objective Optimization (HIGH VALUE)

**What the paper does:** GP uses a Pareto-front approach to balance three competing objectives simultaneously: task accuracy, computational efficiency, and code simplicity. No single metric wins — solutions that are Pareto-optimal across all three are preferred.

**What Ouroboros does now:** Brain selection (`brain_selection_policy.yaml`) routes based on compute class and op type. The trust graduator uses a single scalar score. There's no concept of balancing competing objectives when selecting which brain to use for a given operation.

**Why this matters for Ouroboros:** When choosing between J-Prime (fast, powerful, expensive) and Claude (slower, cheaper, different capabilities), the current system routes by capability tier. A Pareto-front approach would choose the brain that is best across the combined dimensions of:
- **Task accuracy** (how well does this brain handle this op type historically?)
- **Latency** (what is the p95 execution time for this brain on this file type?)
- **Cost** (what is the token cost per successful COMPLETE for this brain?)

**Concrete implementation path:**

Add a `BrainPerformanceProfile` to the trust graduator / brain selection system:

```python
@dataclass
class BrainPerformanceProfile:
    brain_id: str
    op_type: str                          # file extension / domain
    historical_accuracy: float            # fraction of ops reaching COMPLETE
    p95_latency_s: float                  # 95th percentile execution time
    cost_per_success: float               # average token cost for successful ops
    sample_count: int                     # how many ops this is based on

    def pareto_score(self, weights: dict[str, float]) -> float:
        """Weighted Pareto score. Higher is better."""
        return (
            weights.get("accuracy", 0.5) * self.historical_accuracy +
            weights.get("speed", 0.3) * (1.0 / max(self.p95_latency_s, 0.1)) +
            weights.get("cost", 0.2) * (1.0 / max(self.cost_per_success, 0.001))
        )
```

Store `BrainPerformanceProfile` per (brain_id, op_type) pair in the existing `GovernanceStack` ledger. After every `COMPLETE` or `FAILED` outcome, update the profile. Brain selection reads profiles and picks the Pareto-optimal brain for the current operation context.

This directly mirrors the paper's tournament selection: multiple candidate brains compete on a fitness function; the winner handles the operation. Over time, Ouroboros routes operations to whichever brain is actually best for that specific file type and operation domain — not just based on compute tier.

---

#### James et al. Enhancement 2: RL-Driven Trust Graduation Feedback Loop (HIGH VALUE)

**What the paper does:** RL agents (PPO) evaluate the programs generated by GP, provide reward/penalty signals, and those signals refine the GP population for the next generation. The RL reward function directly shapes what GP produces. This is a continuous feedback loop — not a batch update.

**What Ouroboros does now:** Trust graduation (`trust_graduator.py`) tracks operation outcomes and promotes/demotes brains based on historical success rates. But there's no reward signal that flows back in real-time to shape the next brain selection or generation prompt. The system learns across sessions, not within a session.

**Why this matters:** The paper shows that RL feedback loops accelerate convergence by 40% (50→30 adaptation time units). Applied to Ouroboros: if the first generation attempt fails validation, the failure signal should immediately influence the second attempt — not just be logged for next-session trust graduation.

**Concrete implementation path:**

Add an `IntraSessionRewardTracker` to `GovernedLoopService`:

```python
class IntraSessionRewardTracker:
    """Within-session RL-style signal accumulation.

    Unlike TrustGraduator (cross-session), this tracks reward signals
    within the current operation's generation loop and adjusts
    generation parameters in real-time.
    """
    def __init__(self):
        self._signals: list[float] = []
        self._brain_rewards: dict[str, list[float]] = defaultdict(list)

    def record(self, brain_id: str, outcome: str, quality: QualityMetrics) -> float:
        """Compute reward signal from operation outcome + quality metrics."""
        reward = 0.0
        if outcome == "COMPLETE":
            reward = 1.0
        elif outcome == "PARTIAL_COMPLETE":
            reward = 0.5 + (quality.test_pass_rate * 0.3) + (quality.branch_coverage * 0.2)
        elif outcome == "FAILED":
            reward = -0.5
        elif outcome == "ROLLED_BACK":
            reward = -1.0

        # Bonus for quality above threshold
        if quality.cyclomatic_complexity <= 10:
            reward += 0.1
        if quality.lint_error_count == 0:
            reward += 0.05

        self._brain_rewards[brain_id].append(reward)
        return reward

    def get_brain_preference(self) -> Optional[str]:
        """Return the brain_id with highest cumulative reward this session."""
        if not self._brain_rewards:
            return None
        return max(self._brain_rewards, key=lambda b: sum(self._brain_rewards[b]))
```

Wire into `GovernedLoopService.submit()`: after each GENERATE→VALIDATE→APPLY→VERIFY cycle within a multi-attempt operation, record the reward. On the next attempt, `get_brain_preference()` steers brain selection toward the best-performing brain seen so far in this session.

This is not a full PPO implementation — it's a lightweight RL-inspired signal that produces the same adaptive effect within a single operation's retry loop.

---

#### James et al. Enhancement 3: Genetic Programming for Policy Evolution (MEDIUM VALUE)

**What the paper does:** GP evolves a population of candidate solutions through:
1. **Tournament selection** — multiple candidates compete; high-fitness ones survive
2. **Crossover** — combine parts of two successful policies to produce offspring
3. **Mutation** — introduce controlled random variation to prevent premature convergence

**What Ouroboros does now:** The `brain_selection_policy.yaml` is a static file. The trust graduation rules are hard-coded Python. Neither evolves automatically based on what's working.

**Directly applicable concept: Evolving Brain Selection Policies**

Rather than a fixed `brain_selection_policy.yaml`, generate multiple candidate routing policies (e.g., "route .py files with complexity >10 to J-Prime", "route .py files with complexity ≤5 to Claude"), evaluate them against historical operation outcomes, and keep the policies with best real-world performance.

This is a 4-phase loop:
```
1. POPULATION: Generate N candidate brain routing policies (vary thresholds, file type rules, compute class weights)
2. TOURNAMENT: Run each candidate policy against a held-out set of recent operations, score by (COMPLETE rate × quality score × cost efficiency)
3. CROSSOVER: Combine thresholds from top-2 policies to produce new candidate
4. MUTATION: Random perturbation of one threshold value (e.g., complexity cutoff 10 → 12)
```

This runs as a background maintenance task (not during active operations) and produces an updated `brain_selection_policy.yaml` when a new policy variant outperforms the current one by >5%.

---

#### James et al. Enhancement 4: Meta-Learning for Fast Task Adaptation (MEDIUM VALUE)

**What the paper does:** MAML (Model-Agnostic Meta-Learning) and Reptile enable the system to adapt to new task types with minimal training data. The system learns "how to learn" new tasks — so when it encounters a new file type or new op domain, it converges faster than starting from scratch.

**What Ouroboros does now:** Each new file type / operation domain starts cold. When Ouroboros first encounters a new codebase (e.g., Rust files, a new API pattern), the trust graduator has no history for that domain and defaults to OBSERVE tier with maximum caution.

**Directly applicable concept: Few-Shot Domain Adaptation**

Implement a `DomainTransferRegistry` that maps known (domain, file_type) patterns to "starter trust profiles":

```python
class DomainTransferRegistry:
    """Meta-learning inspired: transfer trust knowledge from similar domains.

    When encountering a new domain, don't start cold — find the most
    similar known domain and use its trust profile as initialization.
    """
    def __init__(self, ledger: DurableLedgerAdapter):
        self._ledger = ledger

    def get_starter_profile(self, new_domain: str, file_extension: str) -> TrustProfile:
        all_profiles = self._ledger.list_domain_profiles()

        # Find most similar domain by feature overlap
        # Features: file_extension, operation_type, avg_complexity, avg_file_size
        best_match = self._find_similar(new_domain, file_extension, all_profiles)

        if best_match:
            # Transfer profile but halve the confidence — it's a guess
            return best_match.with_confidence_decay(factor=0.5)

        # No similar domain — return cold-start defaults
        return TrustProfile.cold_start()
```

This is the practical application of meta-learning without the computational cost of MAML: instead of gradient-based meta-optimization, use structural similarity to find the closest known domain and warm-start trust graduation from there. The paper shows this reduces convergence from 50→30 iterations; for Ouroboros this means new repos/languages start at SUGGEST tier instead of OBSERVE, saving multiple operation cycles.

---

#### James et al. Enhancement 5: Continuous Performance Monitoring with Adaptation Speed Metric (HIGH VALUE)

**What the paper does:** Tracks four evaluation metrics throughout the system's lifetime:
- Task Completion Rate (% of objectives met)
- **Adaptation Speed** (iterations required to converge on a new task type — this is the key metric)
- Computational Efficiency (resources consumed per operation)
- Code Complexity Reduction (ongoing improvement in generated code quality)

Adaptation speed is tracked across iterations and plotted — it shows whether the system is getting faster at converging over time. In their results, adaptation time dropped from 50→30 units across 5 iterations, a 40% improvement.

**What Ouroboros does now:** `CommProtocol` emits POSTMORTEM events with operation duration and outcome. There's no system-level metric for "how long did it take the system to learn this domain" or "is adaptation speed improving over time?"

**Concrete implementation path:**

Add `AdaptationSpeedTracker` as a `GovernanceStack` component:

```python
class AdaptationSpeedTracker:
    """Measures how quickly the system converges to high-accuracy operation in new domains.

    Adaptation speed = number of operations until the domain reaches
    GOVERNED trust tier. Lower is better. Tracks trend across all domains.
    """
    def record_domain_milestone(self, domain: str, tier_reached: TrustTier, op_count: int):
        ...

    def get_adaptation_trend(self) -> float:
        """Returns slope of adaptation speed over time.
        Negative = getting faster (good). Positive = regression.
        """
        ...

    def emit_telemetry(self, bus: TelemetryBus):
        """Emit adaptation_speed.trend@1.0.0 envelope for TUI SystemPanel."""
        ...
```

Wire into the TUI `SystemPanel` — it's the one metric the paper demonstrates definitively improves with the hybrid approach. If Ouroboros's adaptation speed is trending in the wrong direction, it surfaces immediately in the dashboard.

---

#### James et al. Enhancement 6: Ethical Constraints as Reward Function Components (CRITICAL for Safety)

**What the paper does (Section 5.1-5.3):** Argues that for self-programming AI, ethical considerations must be embedded directly into the reward functions — not added as a post-hoc filter. Specifically:
- Transparency: every system modification must be logged with full traceability
- Safety: fail-safe mechanisms must allow human intervention at any point
- Bias mitigation: regular audits of decision patterns to detect systematic bias
- Accountability: every autonomous decision must be explainable in human-readable terms

The paper explicitly states: *"Ethical constraints were embedded in reward functions to prevent undesirable behavior."*

**Why this is CRITICAL for Ouroboros:** Ouroboros's trust graduation system currently rewards outcomes only. A brain that reaches COMPLETE quickly gets promoted. But a brain that reaches COMPLETE by generating code that bypasses safety checks, works around governance gates, or introduces subtle security regressions would also get promoted — because the reward signal is only "did the operation complete."

**Concrete implementation path — Ethical Reward Shaping:**

Add negative reward signals to `IntraSessionRewardTracker` for ethical violations:

```python
def record_ethical_signals(self, ctx: OperationContext, apply_result: ApplyResult) -> float:
    penalty = 0.0

    # Penalty: patch modifies governance files themselves
    governance_files = ["governed_loop_service.py", "trust_graduator.py", "risk_engine.py", "policy_engine.py"]
    if any(f in str(p) for p in ctx.target_files for f in governance_files):
        penalty -= 0.5  # Self-modification of governance is high-risk

    # Penalty: patch adds new subprocess calls / shell exec patterns (injection risk)
    if _detect_shell_injection_patterns(apply_result.patch_content):
        penalty -= 1.0  # Hard negative — security violation

    # Penalty: patch removes logging or audit trail code
    if _detects_logging_removal(apply_result.patch_content):
        penalty -= 0.3  # Accountability regression

    # Penalty: patch modifies test files to make tests less strict
    if _detects_test_weakening(apply_result.patch_content):
        penalty -= 0.4  # Gaming the quality gate

    return penalty
```

Wire into `RiskEngine` as a pre-APPLY check. If `ethical_penalty < -0.5`, escalate to APPROVAL_REQUIRED regardless of operation's baseline risk tier. This is the paper's "ethical constraints in reward functions" applied to Ouroboros's actual risk surface.

Also add to `TrustGraduator.record_outcome()`:
```python
if ethical_penalty < -0.3:
    self._demotion_signals.append(EthicalViolationSignal(brain_id, op_id, penalty))
    # Two ethical violations → demotion to OBSERVE, regardless of accuracy
```

This implements what the paper calls "regular audits to evaluate fairness and accountability" — except Ouroboros does it continuously on every operation rather than in periodic batch audits.

---

#### James et al. Enhancement 7: NAS Concept Applied to Brain Architecture Selection (LOW-MEDIUM VALUE, Future Work)

**What the paper does:** Neural Architecture Search (NAS) automatically finds optimal model architectures — essentially treating model hyperparameters as a search space and using evolutionary strategies to find the best configuration.

**Applied to Ouroboros:** Ouroboros doesn't train neural networks, but it does configure each brain (context window, temperature, max tokens, system prompt strategy). These hyperparameters are currently static in `brain_selection_policy.yaml`.

**NAS-inspired "Brain Parameter Search":** Treat brain configuration parameters as a search space:
```
Search space per brain:
  temperature: [0.1, 0.3, 0.5, 0.7]
  context_window_fraction: [0.5, 0.75, 1.0]
  system_prompt_strategy: [minimal, standard, full_oracle]
  max_tokens: [512, 1024, 2048, 4096]
```

Run tournament selection across parameter combinations using historical operation outcomes as fitness. This is low-priority because it requires significant data collection before tournament selection is meaningful — but it's the correct long-term path to fully autonomous brain configuration.

---

### 7.3 Cross-Paper Synthesis: What Both Papers Agree On

Both SPA (Khemani) and the hybrid framework (James et al.) independently converge on four principles. These are not coincidences — they reflect what's universally necessary for self-improving AI systems:

**Principle 1: Multi-dimensional exit criteria, not single-metric convergence**
- SPA: 4 metrics must all pass (tests, coverage, complexity, lint)
- James et al.: Pareto-front across task accuracy, computational efficiency, code simplicity
- **Ouroboros action**: Add `QualityMetrics.is_convergent()` as a second gate in VERIFY alongside test pass

**Principle 2: Feedback loops must flow backward to the generation step**
- SPA: JSON metrics bundle drives the improvement module's next prompt
- James et al.: RL reward signals from evaluation shape the next GP generation
- **Ouroboros action**: `IntraSessionRewardTracker` + `QualityMetrics.to_prompt_fragment()` in generation retry prompts

**Principle 3: Population diversity prevents local optima**
- SPA: Acknowledges local-optimum stagnation as a limitation (no escape mechanism)
- James et al.: Genetic mutation introduces controlled variation to prevent premature convergence
- **Ouroboros action**: When a brain is stuck after N retry attempts, inject a mutation-style prompt variation: "Approach this differently — avoid the pattern used in the previous attempt: [summary of previous approach]"

**Principle 4: Ethical constraints must be structural, not advisory**
- SPA: Test quality drives loop quality — shallow tests allow wrong behavior to pass (implicit ethical failure)
- James et al.: "Ethical constraints were embedded in reward functions to prevent undesirable behavior"
- **Ouroboros action**: Ethical penalty signals in `IntraSessionRewardTracker`; ethical violations trigger APPROVAL_REQUIRED regardless of risk tier

---

### 7.4 Implementation Priority for Research-Backed Enhancements

| Enhancement | Paper | Effort | Impact | Files |
|---|---|---|---|---|
| `QualityMetrics` dataclass + prompt injection | Khemani SPA | Small (~50 lines) | HIGH | `op_context.py`, `providers.py` |
| Quality gates in VERIFY (radon, pylint, coverage) | Khemani SPA | Medium (~100 lines) | HIGH | `verify_provider.py` |
| `IntraSessionRewardTracker` | James et al. | Small (~60 lines) | HIGH | `governed_loop_service.py` |
| Ethical reward shaping (penalty signals) | James et al. | Medium (~80 lines) | CRITICAL | `risk_engine.py`, `trust_graduator.py` |
| `AdaptationSpeedTracker` + TUI wiring | James et al. | Small (~40 lines) | MEDIUM | New file + `system_panel.py` |
| `BrainPerformanceProfile` + Pareto selection | James et al. | Medium (~120 lines) | HIGH | `brain_selection_policy.py` |
| `DomainTransferRegistry` (meta-learning) | James et al. | Medium (~100 lines) | MEDIUM | New file + `trust_graduator.py` |
| `PARTIAL_COMPLETE` operation result state | Khemani SPA | Small (~30 lines) | MEDIUM | `op_context.py`, `orchestrator.py` |
| LibCST patch application path | Khemani SPA | Large (~200 lines) | MEDIUM | `change_engine.py` |
| GP-style policy evolution (background task) | James et al. | Large (~300 lines) | LOW-MEDIUM | New file |

---

Sources:

**Claude Code Technical Documentation**
- [Hooks reference](https://docs.anthropic.com/en/docs/claude-code/hooks) — https://docs.anthropic.com/en/docs/claude-code/hooks
- [Settings and permissions](https://docs.anthropic.com/en/docs/claude-code/settings) — https://docs.anthropic.com/en/docs/claude-code/settings
- [How Claude remembers your project](https://docs.anthropic.com/en/docs/claude-code/memory) — https://docs.anthropic.com/en/docs/claude-code/memory
- [Create custom subagents](https://docs.anthropic.com/en/docs/claude-code/sub-agents) — https://docs.anthropic.com/en/docs/claude-code/sub-agents
- [Subagents in the SDK](https://docs.anthropic.com/en/docs/claude-code/sdk/subagents) — https://docs.anthropic.com/en/docs/claude-code/sdk/subagents
- [Agent SDK overview](https://docs.anthropic.com/en/docs/claude-code/sdk) — https://docs.anthropic.com/en/docs/claude-code/sdk
- [Connect to external tools with MCP](https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-mcp) — https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-mcp
- [Configure permissions](https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-permissions) — https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-permissions

**Research Papers**
- Khemani, K. (2025). Self-Programming AI: Code-Learning Agents for Autonomous Refactoring and Architectural Evolution. Research Square (preprint). [https://doi.org/10.21203/rs.3.rs-6688473/v1](https://doi.org/10.21203/rs.3.rs-6688473/v1)
- James, N. H., Ene, D. S., & Lenu, G. F. (2025). Self-Programming Artificial Intelligence: Autonomous Learning and Evolutionary Algorithms. International Journal of Computer Science and Mathematical Theory (IJCSMT), Vol. 11, No. 5, pp. 77–92. [https://doi.org/10.56201/ijcsmt.vol.11.no5.2025.pg77.92](https://doi.org/10.56201/ijcsmt.vol.11.no5.2025.pg77.92)
