# JARVIS-CU: Local Computer Use + Full Vision Activation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Computer Use system that replaces Claude's CU API with a 3-layer cascade (Accessibility API -> Doubleword 235B -> Claude Vision), powered by the 60fps SHM capture pipeline, with all dormant intelligence modules wired into the live frame stream.

**Architecture:** Voice command -> Claude Vision decomposes into atomic steps -> per-step execution through Accessibility API (deterministic, <5ms) -> Doubleword 235B (visual grounding, ~2s) -> Claude Vision (complex reasoning, fallback). 60fps SHM feed provides continuous verification. Dormant modules (activity recognition, anomaly detection, goal inference, predictive precomputation, intervention decision) observe the 60fps stream and feed context into step execution.

**Tech Stack:** Python 3.9, asyncio, macOS Accessibility API (AXUIElement via ctypes), Doubleword API (Qwen3-VL-235B), Anthropic API (Claude Vision), numpy, SHM ring buffer, Ghost Hands (Playwright/AppleScript/CGEvent)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `backend/vision/jarvis_cu.py` | CREATE | JarvisCU orchestrator: plan + execute + verify |
| `backend/vision/cu_step_executor.py` | CREATE | Per-step 3-layer cascade executor |
| `backend/vision/cu_task_planner.py` | CREATE | Claude Vision task decomposition |
| `backend/vision/vision_activator.py` | CREATE | On-demand vision startup (no env var gate) |
| `backend/vision/intelligence/vision_intelligence_hub.py` | CREATE | Hub wiring 5 dormant modules to 60fps feed |
| `backend/vision/realtime/frame_pipeline.py` | MODIFY | Add frame subscriber system |
| `tests/test_cu_task_planner.py` | CREATE | Task planner tests |
| `tests/test_cu_step_executor.py` | CREATE | Step executor tests |
| `tests/test_jarvis_cu.py` | CREATE | Orchestrator tests |
| `tests/test_vision_intelligence_hub.py` | CREATE | Intelligence hub tests |
| `tests/test_jarvis_cu_e2e.py` | CREATE | End-to-end integration tests |

---

## Task 1: CU Task Planner

Claude Vision decomposes natural language goals into atomic CUStep objects.

**Files:** Create `backend/vision/cu_task_planner.py`, `tests/test_cu_task_planner.py`

See plan body for full implementation code.

## Task 2: CU Step Executor

3-layer cascade: Accessibility API -> Doubleword 235B -> Claude Vision.

**Files:** Create `backend/vision/cu_step_executor.py`, `tests/test_cu_step_executor.py`

## Task 3: JarvisCU Orchestrator

Ties planner + executor together with retry, verification, and telemetry.

**Files:** Create `backend/vision/jarvis_cu.py`, `tests/test_jarvis_cu.py`

## Task 4: Vision Intelligence Hub

Wires 5 dormant modules to 60fps feed with per-module rate limiting.

**Files:** Create `backend/vision/intelligence/vision_intelligence_hub.py`, modify `frame_pipeline.py`

## Task 5: Vision Activator

On-demand vision startup when ACTION commands are detected. No env var gate.

**Files:** Create `backend/vision/vision_activator.py`

## Task 6: End-to-End Integration Tests

Full WhatsApp scenario test with mocked providers.

**Files:** Create `tests/test_jarvis_cu_e2e.py`
