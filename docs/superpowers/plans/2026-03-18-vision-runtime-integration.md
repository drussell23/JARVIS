# Vision Runtime Integration — Root Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement.

**Goal:** Integrate vision-action agents into the real JARVIS runtime (not demo scripts), with on-demand GPU management and Reactor-learned step plans.

**Architecture:** Vision tasks flow through the real pipeline: voice → unified_command_processor → plan_and_execute() → ExecutionTierRouter → NativeAppControlAgent/VisualBrowserAgent. GPU VM auto-starts via GCPVMManager when vision is needed. Step decomposition plans are cached in Reactor after first LLM call. Each action is verified via post-action screenshot before proceeding.

**Tech Stack:** GCPVMManager (existing), PrimeClient (existing), CrossRepoExperienceForwarder (existing), AppLibrary (existing), Playwright (existing).

---

## Root Problems (Not Band-Aids)

| Problem | Root Cause | Fix |
|---------|-----------|-----|
| Demo scripts bypass runtime | Agents tested in isolation, not through real pipeline | Wire into unified_command_processor flow |
| GPU costs $640/mo 24/7 | No on-demand lifecycle | GCPVMManager auto-start/stop per vision request |
| Vision loop fails silently | No verification after actions | Post-action screenshot + state verification |
| Step decomposition needs LLM every time | No caching/learning | Reactor stores successful plans, reuses them |
| 12MB screenshots crash Claude | No compression in pipeline | Already fixed — JPEG resize to 1536px |

---

## Task 1: Deploy Models to GPU VM

SSH into jarvis-prime-gpu, install Python venv, download Qwen2.5-7B + LLaVA v1.6, create systemd services for port 8000 (text) and 8001 (vision). Create startup script that auto-starts both servers on VM boot.

## Task 2: On-Demand GPU via GCPVMManager

Modify NativeAppControlAgent and VisualBrowserAgent: before calling send_vision_request(), check if J-Prime is healthy. If not, call GCPVMManager.ensure_static_vm_ready() to auto-start the GPU VM. Add idle-timeout auto-stop (configurable, default 30 min).

## Task 3: Wire Vision Agents Into Real Runtime

Remove demo-script-only code paths. Ensure "Send Zach a WhatsApp message" spoken to JARVIS goes through: unified_command_processor → _try_plan_and_execute() → MultiAgentOrchestrator.plan_and_execute() → PredictivePlanningAgent.plan_to_workflow() → ExecutionTierRouter.decide_tier() → NativeAppControlAgent.execute_task(). No bypassing.

## Task 4: Post-Action Verification Loop

After each action in the vision loop (click, type, etc.), take a new screenshot and send to J-Prime with prompt: "Did the previous action succeed? Is the current step complete?" Only proceed to next step if verified. Retry up to 2x if verification fails.

## Task 5: Reactor-Cached Step Plans

After successful goal decomposition, store the plan in Reactor via CrossRepoExperienceForwarder with experience_type="step_plan_learned". Before calling LLM for decomposition, check if Reactor has a cached plan for a similar goal. Use ChromaDB semantic search on goal text.

## Task 6: End-to-End Integration Test

Test through the real pipeline: speak "Send Zach a message on WhatsApp saying testing with JARVIS" → JARVIS processes through all layers → WhatsApp opens → vision loop executes steps → message sent → Trinity logs experience.

---

## Architecture After Integration

```
Voice: "Send Zach a message on WhatsApp"
     |
unified_command_processor._execute_command_pipeline()
     |
J-Prime classifies: intent=action, domain=unknown
     |
_try_plan_and_execute()  [Wire 1]
     |
MultiAgentOrchestrator.plan_and_execute()  [Wire 2]
     |
PredictivePlanningAgent.plan_to_workflow()
  -> _decompose_goal("send Zach message", "WhatsApp")
     -> Check Reactor cache (ChromaDB) for similar plan  [Task 5]
     -> If miss: J-Prime text (free) decomposes into 6 steps
     -> If J-Prime offline: auto-start GPU VM  [Task 2]
     -> Store plan in Reactor for next time
  -> to_workflow_tasks() converts to WorkflowTasks
     |
ExecutionTierRouter.decide_tier()
  -> WhatsApp installed? YES -> NATIVE_APP
     |
NativeAppControlAgent.execute_task()
  -> For each step:
     1. Take screenshot (compressed JPEG)
     2. Send to J-Prime LLaVA (port 8001)  [Task 2: auto-start if needed]
     3. Execute action (click/type/key)
     4. Verify action succeeded (post-action screenshot)  [Task 4]
     5. Retry if verification fails
  -> Complete: message sent
     |
Trinity experience emission  [Wire 3]
  -> Reactor learns: plan, timing, success/failure per step
     |
JARVIS narrates: "Message sent to Zach on WhatsApp."
```
