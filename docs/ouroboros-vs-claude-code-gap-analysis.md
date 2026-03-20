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

Sources:
- [Hooks reference](https://docs.anthropic.com/en/docs/claude-code/hooks)
- [Settings and permissions](https://docs.anthropic.com/en/docs/claude-code/settings)
- [How Claude remembers your project](https://docs.anthropic.com/en/docs/claude-code/memory)
- [Create custom subagents](https://docs.anthropic.com/en/docs/claude-code/sub-agents)
- [Subagents in the SDK](https://docs.anthropic.com/en/docs/claude-code/sdk/subagents)
- [Agent SDK overview](https://docs.anthropic.com/en/docs/claude-code/sdk)
- [Connect to external tools with MCP](https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-mcp)
- [Configure permissions](https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-permissions)
