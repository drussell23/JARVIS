# Execution Tier Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give JARVIS three distinct execution tiers — API, native app, and browser — with J-Prime intelligently choosing which tier to use per task.

**Architecture:** Four components wired into the existing Neural Mesh: (1) ExecutionTierRouter decides API vs native vs browser per task, (2) NativeAppControlAgent drives installed macOS apps via PyAutoGUI/AppleScript with J-Prime vision, (3) VisualBrowserAgent drives Chrome via Playwright with J-Prime vision, (4) AppInventoryService exposes the existing AppLibrary to all agents. Each component follows BaseNeuralMeshAgent patterns and registers capabilities in the existing AgentRegistry.

**Tech Stack:** Existing: Playwright (installed), PyAutoGUI/pyobjc (installed), AppLibrary (built), BrowsingEngine (built), PrimeRouter (built). New: None required - built on existing infrastructure.

---

## Existing Infrastructure (DO NOT rebuild)

| Component | File | What It Does |
|-----------|------|-------------|
| AppLibrary | `backend/system/app_library.py` | Spotlight-based app scanning, `resolve_app_name_async()`, caching |
| BrowsingEngine | `backend/browsing/browsing_agent.py` | Playwright lifecycle, CDP support, page pooling |
| ComputerUseConnector | `backend/display/computer_use_connector.py` | PyAutoGUI async wrapper, thread pool, circuit breaker |
| PrimeRouter | `backend/core/prime_router.py` | RoutingDecision enum, circuit breakers, deadline propagation |
| BaseNeuralMeshAgent | `backend/neural_mesh/base/base_neural_mesh_agent.py` | Agent base class with publish/request/broadcast/subscribe |

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `backend/neural_mesh/agents/app_inventory_service.py` | Neural Mesh wrapper around AppLibrary. Exposes `app_inventory` capability. |
| `backend/neural_mesh/agents/execution_tier_router.py` | Decides API vs native-app vs browser per task. Uses AppLibrary + heuristics. |
| `backend/neural_mesh/agents/native_app_control_agent.py` | Drives installed macOS apps via PyAutoGUI + J-Prime vision loop. |
| `backend/neural_mesh/agents/visual_browser_agent.py` | Drives Chrome via Playwright + J-Prime vision loop. |
| `tests/integration/test_execution_tiers.py` | Tests for all 4 components. |
| `tests/integration/test_tier_visual_demo.py` | Live visual demo with WhatsApp + Gmail. |

### Modified Files

| File | Change |
|------|--------|
| `backend/neural_mesh/agents/agent_initializer.py` | Register 4 new agents in PRODUCTION_AGENTS |
| `backend/neural_mesh/agents/predictive_planning_agent.py` | Add new capabilities to `_CAPABILITY_ROUTES` |
| `backend/neural_mesh/data_models.py` | Add `TIER_DECISION` MessageType |

---

## Task 1: AppInventoryService

**Files:**
- Create: `backend/neural_mesh/agents/app_inventory_service.py`
- Test: `tests/integration/test_execution_tiers.py`

Thin Neural Mesh wrapper around the existing AppLibrary singleton. Exposes app discovery to all agents via the message bus.

- [ ] Write failing test (check_app, scan_installed actions)
- [ ] Run test to verify it fails
- [ ] Implement AppInventoryService (delegates to AppLibrary)
- [ ] Run test to verify it passes
- [ ] Commit

## Task 2: ExecutionTierRouter

**Files:**
- Create: `backend/neural_mesh/agents/execution_tier_router.py`
- Test: `tests/integration/test_execution_tiers.py` (append)

Decision logic: force_visual -> BROWSER, workspace_service -> API, app_installed -> NATIVE_APP, else -> BROWSER.

- [ ] Write failing tests (gmail->API, whatsapp_installed->NATIVE, whatsapp_not_installed->BROWSER, force_visual->BROWSER, linkedin->BROWSER)
- [ ] Run tests to verify they fail
- [ ] Implement ExecutionTierRouter with ExecutionTier enum and decide_tier()
- [ ] Run tests to verify they pass
- [ ] Commit

## Task 3: NativeAppControlAgent

**Files:**
- Create: `backend/neural_mesh/agents/native_app_control_agent.py`
- Test: `tests/integration/test_execution_tiers.py` (append)

Vision-action loop: activate app -> screenshot -> J-Prime decides action -> PyAutoGUI/AppleScript executes -> repeat.

- [ ] Write failing tests (capabilities, validation, app-not-installed check)
- [ ] Run tests to verify they fail
- [ ] Implement NativeAppControlAgent with vision loop (J-Prime primary, Claude fallback)
- [ ] Run tests to verify they pass
- [ ] Commit

## Task 4: VisualBrowserAgent

**Files:**
- Create: `backend/neural_mesh/agents/visual_browser_agent.py`
- Test: `tests/integration/test_execution_tiers.py` (append)

Same vision-action loop but uses Playwright for Chrome. Launches visible Chrome (not headless) so user sees JARVIS interact.

- [ ] Write failing tests (capabilities, url/goal validation)
- [ ] Run tests to verify they fail
- [ ] Implement VisualBrowserAgent with Playwright + J-Prime vision
- [ ] Run tests to verify they pass
- [ ] Commit

## Task 5: Register and Wire Into Pipeline

**Files:**
- Modify: `backend/neural_mesh/agents/agent_initializer.py`
- Modify: `backend/neural_mesh/agents/predictive_planning_agent.py`
- Modify: `backend/neural_mesh/data_models.py`

- [ ] Add TIER_DECISION MessageType
- [ ] Register 4 new agents in PRODUCTION_AGENTS
- [ ] Add native_app_control and visual_browser capabilities to _CAPABILITY_ROUTES
- [ ] Run full test suite (test_execution_tiers + test_wire_integration)
- [ ] Commit

## Task 6: Live Visual Demo

**Files:**
- Create: `tests/integration/test_tier_visual_demo.py`

Demo: "Send Zach a WhatsApp message" (NATIVE_APP tier) + "Draft email visually in Gmail" (BROWSER tier).

- [ ] Write demo script with tier routing display
- [ ] Test tier decisions without execution first
- [ ] Run full visual demo with real apps
- [ ] Commit

---

## Architecture Summary

```
Voice: "Send Zach a WhatsApp message and draft an email"
     |
PredictivePlanningAgent -> 2 tasks
     |
ExecutionTierRouter decides per task:
  Task 1: WhatsApp installed? YES -> NATIVE_APP
  Task 2: Gmail has API -> API (or BROWSER if force_visual)
     |
MultiAgentOrchestrator dispatches:
  Task 1 -> NativeAppControlAgent
           -> activates WhatsApp.app
           -> screenshot -> J-Prime -> click/type -> repeat
  Task 2 -> GoogleWorkspaceAgent (API)
           OR VisualBrowserAgent (if visual)
     |
Trinity experience -> Reactor learns
```
