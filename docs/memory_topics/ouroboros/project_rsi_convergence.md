---
title: Project Rsi Convergence
modules: [backend/core/ouroboros/governance/, backend/core/ouroboros/governance/composite_score.py, backend/core/ouroboros/governance/convergence_tracker.py, backend/core/ouroboros/governance/oracle_prescorer.py, backend/core/ouroboros/governance/transition_tracker.py, backend/core/ouroboros/governance/vindication_reflector.py, backend/core/ouroboros/governance/graduation_orchestrator.py]
status: historical
source: project_rsi_convergence.md
---

RSI Convergence Framework added to Ouroboros based on Wenyi Wang's "A Formulation of RSI & Its Possible Efficiency" (UBC, arXiv:1805.06610).

**Why:** Ouroboros lacked a unified score function, convergence monitoring, and adaptive thresholds. Wang's paper provides mathematical grounding that RSI systems can converge in O(log n) steps.

**How to apply:** Architecture doc at `docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md`. Implementation plan at `docs/superpowers/plans/2026-04-06-rsi-convergence-improvements.md`. Plan has 14 tasks, TDD, all code included. Key new files: `composite_score.py`, `convergence_tracker.py`, `oracle_prescorer.py`, `transition_tracker.py`, `vindication_reflector.py` — all in `backend/core/ouroboros/governance/`. Adaptive graduation threshold modifies `graduation_orchestrator.py`. All components are 100% deterministic (no LLM calls).
