# Ouroboros HUD Pipeline — Program Overview

**Date:** 2026-04-02
**Status:** Active
**Scope:** Wire the full Ouroboros governance pipeline into HUD mode with proper context injection, multi-phase validation, and DaemonNarrator.

## Problem Statement

The 397B brain (Doubleword Qwen3.5) generates valid Python that matches code style. But the pipeline around it is disconnected in HUD mode:

1. CUExecutionSensor counts failures but cannot hand off to the orchestrator (router not wired)
2. Context injection truncates large files, causing blind duplication
3. VALIDATE checks syntax but not semantic duplication
4. DaemonNarrator not wired into the live HUD process

## Sub-Projects (ordered by dependency)

| ID | Name | Type | Depends On |
|----|------|------|------------|
| A | The Severed Nerve | Deterministic infrastructure | None |
| B | The Eyes | Agentic quality | A |
| C | The Immune System | Deterministic guards | A (B improves C) |
| D | The Voice | Observability / UX | A |

Each sub-project has its own spec, acceptance tests, and merge cycle.

## Manifesto Alignment

- **A** = deterministic spinal handoff (Pillar 1: Unified Organism)
- **B** = agentic context quality (Pillar 5: Intelligence-Driven Routing)
- **C** = deterministic guards on agentic output (Pillar 6: Neuroplasticity boundary)
- **D** = absolute observability (Pillar 7)
