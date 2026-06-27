---
title: Reverse Russian Doll — Pass A Reconciliation (2026-04-26)
modules: [backend/core/ouroboros/intake/sensors/, backend/core/ouroboros/intake/, backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/governance/candidate_generator.py, backend/core/ouroboros/governance/providers.py, backend/core/ouroboros/governance/doubleword_provider.py, backend/core/ouroboros/governance/tool_executor.py, backend/core/ouroboros/governance/auto_committer.py, backend/core/ouroboros/governance/direction_inferrer.py, backend/core/ouroboros/governance/flag_registry.py, backend/core/ouroboros/governance/sensor_governor.py, agentic_explore_subagent.py]
status: merged
source: project_reverse_russian_doll_pass_a.md
---

# Reverse Russian Doll — Pass A Reconciliation (2026-04-26)

Operator-introduced RSI framework articulated 2026-04-26 chat:
- **Zero Order**: AI as exoskeleton (industry default, freezes when human stops typing)
- **First Order (current)**: O+V as autonomic nervous system, expands the body outward
- **Second Order (horizon)**: O+V turns inward, safely rewrites its own cognitive architecture
- **Anti-Venom (constraint)**: Iron Gate / AST validation must scale *proportionally* as the shell expands

Pass A asks: does this framework already live in the doctrine, and where does it map onto the codebase?

## 1. Vocabulary check — the framework is NEW to the doctrine

Grep across `docs/architecture/*.md` for: `russian doll`, `reverse russian`, `zero/first/second order`, `cognitive interior`, `meta-phase`, `self-modify`, `order_[0-2]`. Result: **zero hits for "Order" vocabulary**; only ambient matches for "self-modifying" (TECHNICAL_DOCUMENT lines 15, 1263). The closest existing taxonomies in the four canonical docs are:

| Doc | Taxonomy | Type | Captures Order vocabulary? |
|---|---|---|---|
| `OUROBOROS.md` | 10-phase FSM (CLASSIFY → COMPLETE) | Pipeline phases | No — these are stages of *one operation*, not orders of self-reference |
| `OUROBOROS_VENOM_PRD.md` lines 22-24+ | Phase 1–6 (Self-Reading → Self-Modeling) | Behavioral milestones | Partial — Phase 6 (Self-Modeling) gestures at Second Order but stops short of "self-rewriting cognitive architecture" |
| `RSI_CONVERGENCE_FRAMEWORK.md` | Wang score-monotonic loop | Single optimization loop | No — Wang's framework is *one loop with one score*, not layered orders |
| `JARVIS_LEVEL_OUROBOROS.md` lines 18+ | Tiers 1–7 (Judgment → Autonomous Judgment) | Behavioral enhancements (all Pre-Implementation) | Partial — Tier 7 ("Perhaps I should take control, sir") is strategic autonomy, not cognitive *self-modification* |

**Finding**: the Reverse Russian Doll framework is genuinely new vocabulary. The existing doctrine has *behavioral* taxonomies (what O+V does) and *operational* taxonomies (how O+V runs one cycle); neither captures **what O+V acts upon** — body code (Order 1) vs. its own cognitive code (Order 2). This is a vocabulary contribution worth landing in the PRD.

## 2. Mapping: Orders → existing subsystems (with citations)

### Order 0 — Industry default (exoskeleton)

Not us. Documented as the contrast in `TRINITY_ECOSYSTEM_TECHNICAL_DOCUMENT.md:534` ("Claude Code / OpenClaw / ClawdBot ... Session-scoped, no continuous operation, no sensory layer, single model, cannot self-modify").

### Order 1 — Autonomic O+V (current state)

**Status: SHIPPING.** Maps to:

| Subsystem | Role | Location |
|---|---|---|
| 16 autonomous sensors | Continuous environmental scan | `backend/core/ouroboros/intake/sensors/` |
| `UnifiedIntakeRouter` | Priority queue + WAL persistence | `backend/core/ouroboros/intake/` |
| 11-phase FSM | CLASSIFY → COMPLETE governed loop | `backend/core/ouroboros/governance/orchestrator.py` |
| 3-tier provider cascade | DW 397B → Claude → J-Prime | `candidate_generator.py`, `providers.py`, `doubleword_provider.py` |
| Venom (16 built-in tools + MCP) | Multi-turn tool loop, exploration-first | `tool_executor.py` |
| AutoCommitter | Post-VERIFY structured commit with O+V signature | `auto_committer.py` |
| Wave 1 graduation (2026-04-21) | DirectionInferrer + FlagRegistry + SensorGovernor — system reads its own posture and self-throttles | `direction_inferrer.py`, `flag_registry.py`, `sensor_governor.py` |

**Live-fire proof**: Sessions O (single-file APPLY 2026-04-15), U–W (multi-file APPLY 2026-04-15), Wave 1 graduation soaks (2026-04-21). Per `CLAUDE.md` battle-test ledger: end-to-end autonomous APPLY-to-disk under full complex-route enforcement is proven and graduated.

### Order 2 — Self-cognitive O+V (horizon, partially in motion)

**Status: PREREQUISITES UNDERWAY; STRUCTURAL GAPS REMAIN.** Mapping is more nuanced — the rungs are:

| Capability | Subsystem | Location | Order-2 nature |
|---|---|---|---|
| Cognitive primitives O+V can *use* | Phase B subagents (EXPLORE/REVIEW/PLAN/GENERAL) graduated 2026-04-20 | `agentic_explore_subagent.py`, `agentic_review_subagent.py`, `agentic_plan_subagent.py`, `agentic_general_subagent.py` | **Delegation, not self-modification** — O+V invokes these; it does not generate them |
| Cognitive interior inside a subagent | Phase C Slice 1b GENERAL LLM driver graduated 2026-04-20 | `agentic_general_subagent.py` + `scoped_tool_backend.py` | First "real LLM running inside a subagent shell" — closest existing thing to a cognitive interior, but the shell itself is hand-written |
| Pipeline FSM decomposition | W2(5) PhaseRunner extraction (Slices 1, 2, 3, 4a.1, 4a.2, 4b, 5a all extracted; defaults all false; graduation pending) | `phase_runner/`, `phase_runners/` | **Structural prerequisite for Order 2.** Once graduated, the FSM is pluggable — the door opens for O+V to in principle generate new runners |
| Posture-aware self-regulation | DirectionInferrer + SensorGovernor (Wave 1) | `direction_inferrer.py`, `sensor_governor.py` | System reads its own state and adjusts its own throughput — adjacent to Order 2 but doesn't yet rewrite cognitive *code* |

**Critical distinction missed in chat synthesis**: Phase B subagents are cognitive **delegation**, not cognitive **self-modification**. The framework's Second Order ("O+V rewrites its own cognitive architecture") is not satisfied by O+V *invoking* a subagent — it requires O+V *generating or modifying* the subagent's code/contract/scope. No subsystem in the codebase does this today.

## 3. Anti-Venom mapping — the gates exist, the *adaptivity* doesn't

The framework's claim: as O+V expands the shell, the immune system must scale proportionally. Mapping:

| Anti-Venom layer | Subsystem | Location | Adaptive? |
|---|---|---|---|
| AST validation | `change_engine` AST parser + multi-file coverage gate (Iron Gate 5) | `orchestrator.py` Iron Gate sequence | **Static** — patterns fixed at code-time |
| Exploration-first | Iron Gate 1 (`JARVIS_EXPLORATION_GATE`) + `ExplorationLedger` 5-category diversity scoring | `orchestrator.py` post-GENERATE | **Static** — category floors are env-tuned, not learned |
| ASCII strictness | Iron Gate 2 (`JARVIS_ASCII_GATE`) | `orchestrator.py` post-GENERATE | **Static** — codepoint set fixed |
| Pre-APPLY pattern detection | `SemanticGuardian` (10 AST/regex patterns, ~10ms, zero LLM) | `semantic_guardian.py` | **Static** — patterns hand-written; per-pattern + master kill switches |
| Mutation cage | `ScopedToolBackend._mutations_count` structural COUNT gate (Phase C Epoch 1) | `scoped_tool_backend.py` | **Static cap, not adaptive** — budget set per-invocation, not learned |
| Injection cage | `SemanticFirewall` (11 detectors, 5 credential shapes, recursion ban, hard-kill) | `semantic_firewall.py` | **Static** — detector set hand-written |
| Risk-tier floor | 4-tier ladder + 3 composing knobs (`JARVIS_MIN_RISK_TIER`, `JARVIS_PARANOIA_MODE`, `JARVIS_AUTO_APPLY_QUIET_HOURS`) | `risk_tier_floor.py` | **Static** — strictest-wins composition; thresholds operator-set |
| Isolation | L3 worktree with hard-fail (no silent shared-tree fallback per Manifesto §1) | `subagent_scheduler.py` + `worktree_manager.py` | **Static** — isolation policy is binary |
| Atomic rollback | Multi-file batch snapshots (per-file restore on failure) | `orchestrator.py::_apply_multi_file_candidate` | **Static** — semantics fixed |

**Key finding**: every immune-system layer the framework calls for **already exists as a deterministic gate**. None of them learn. The Reverse Russian Doll thesis ("Anti-Venom must scale proportionally") is the genuinely novel contribution — it does not appear anywhere in the four canonical docs. None of the docs articulate adaptive gate calibration; the PRD treats `MIN_RISK_TIER` and `PARANOIA_MODE` as operator policy levers, not as autonomously tuned constraints.

## 4. Sharpened gap list (the rungs to actual Second Order)

Five gaps, ordered by structural prerequisite:

### Gap 2.1 — Pipeline FSM is not yet pluggable (in motion)

**Where**: `orchestrator.py` still contains the FSM body verbatim; `phase_runner/` extractions exist behind per-slice flags, all defaulting `false`.
**Prerequisite**: W2(5) graduation matrix completes (3 clean sessions per slice).
**Order-2 implication**: until Slice 5b cuts the dispatcher over and graduates, O+V cannot generate or swap runners — the FSM is a hand-written constant. **Per memory `project_wave2_phaserunner_slice4b.md` and `project_wave2_phaserunner_slice5a.md`**, this is on the active roadmap.

### Gap 2.2 — No amendment protocol for governance code

**Where**: `orchestrator.py`, `iron_gate*` sequence in orchestrator, `semantic_firewall.py`, `semantic_guardian.py`, `scoped_tool_backend.py`. These are read-write only by humans today.
**Why deliberate**: avoiding self-soft-corruption of the immune system. There is no Iron Gate equivalent for FSM/gate mutations themselves.
**Order-2 implication**: this is the **hard ceiling** on Second Order. Even if Gap 2.1 closes, O+V can extend the pipeline (new runner classes) but cannot *mutate the existing immune system*. No doctrine doc addresses this gap.

### Gap 2.3 — Anti-Venom is static, not adaptive

**Where**: every entry in §3 above. SemanticGuardian patterns, Iron Gate floors, mutation budgets, risk-tier compositions — all hand-tuned constants.
**What "adaptive" would look like**:
- SemanticGuardian patterns mined from the POSTMORTEM corpus (failure → new detector)
- Iron Gate exploration-floors auto-tightening when a category-bypass is detected
- Mutation budgets calibrated per-Order: Order-1 ops get current budget; Order-2 ops (touching governance code) get a much smaller one
- Risk-tier ladder learning new tiers as new attack surfaces emerge

**Order-2 implication**: this is the framework's load-bearing novel claim. Without it, the shell can expand faster than the cage. **Not in any roadmap doc.**

### Gap 2.4 — Cognitive primitives are fixed, not composable

**Where**: Phase B subagents are EXPLORE/REVIEW/PLAN/GENERAL — four kinds, hand-written contracts (`subagent_contracts.py`).
**Order-2 implication**: O+V cannot design new subagent kinds with their own scope/budget/allowlist. The four kinds were built one slice at a time by human authorship. The PRD's "AdversarialReviewer subagent (P5)" (line 600) is a *fifth hand-written role*, not a generated one.

### Gap 2.5 — Cross-Trinity scope is explicitly OOS

**Where**: `OUROBOROS_VENOM_PRD.md` line 816: *"Trinity (Mind / Soul) integration — assumes JARVIS-side O+V matures first; J-Prime + Reactor Core integration is a separate document."*
**Where (matching)**: `OUROBOROS.md` lines 1366-1368 enumerate the three repos with env-var paths, but operationally O+V patches only JARVIS Body. J-Prime and Reactor are *constraint sources* (J-Prime as code-generation backend; Reactor Core as sandboxed validator), not modification targets.
**Order-2 implication**: the framework's "the core builds an exponentially larger shell around itself, including the entire Trinity Ecosystem" is **architecturally OOS today by operator's own PRD**. Reconciling this requires either:
- Ratifying cross-Trinity patching as the next milestone (and writing the deferred separate document), or
- Narrowing Reverse Russian Doll Order 2 to JARVIS-Body-only, with cross-Trinity reserved for a future Order 3

The framework as written conflates these. Operator decision needed.

## 5. Where the new vocabulary adds clarity vs. where it duplicates

**Genuinely new (worth landing in doctrine)**:
1. **"Order" as a layer-of-self-reference axis.** Phases (PRD) are *operational stages*; Tiers (JARVIS_LEVEL) are *behavioral enhancements*; Orders are *what the system acts upon*. This axis is missing.
2. **"Anti-Venom must scale proportionally"** — adaptive immune system thesis. Not in any doc. Genuine architectural contribution.
3. **Inverse-compression metaphor** ("core carves an exponentially larger shell"). Wang's framework gives score monotonicity; the geometric framing is a complementary intuition pump for non-Wang readers.

**Redundant with existing doctrine (could cite, not re-articulate)**:
1. Order 0 = industry default — already covered in `TECHNICAL_DOCUMENT.md:534` and `JARVIS_vs_INDUSTRY_TIMELINE.md`.
2. Order 1 = autonomic O+V — already richly documented across `OUROBOROS.md` + the 16-sensor architecture.
3. "O+V proactive vs reactive" — covered in `project_ov_vs_claude_code_positioning.md` (operator memory 2026-04-12).

## 6. Recommendation for Pass B

Pass B should design **Gap 2.1 → 2.2 jointly**: the `MetaPhaseRunner` proposal (the prerequisite for any Order-2 capability) **paired with** the governance-code amendment protocol that prevents Gap 2.2 from being silently bypassed. Specifically:

- Define an **Order-2 risk class** above `BLOCKED` for any patch whose target file is in a hardcoded Order-2 manifest (orchestrator, iron_gate sequence, semantic_firewall, semantic_guardian, scoped_tool_backend, all PhaseRunner classes, the manifest itself).
- Order-2 patches require **dual-cage clearance**: standard 11-phase pipeline + a parallel "shadow pipeline" that re-validates against a frozen golden replay of the last 100 successful Order-1 ops (a regression-replay gate).
- An **AST shape validator** for new PhaseRunner subclasses: any generated runner must conform to the ABC contract proven in W2(5) Slice 1, and must pass a verbatim-replay test against a curated golden corpus before its flag can flip.
- A **manifest-amendment protocol**: changes to the Order-2 manifest itself require explicit operator approval (no auto-apply ever — even at SAFE_AUTO).

Pass C (adaptive Anti-Venom, Gap 2.3) is the *next* layer and depends on Pass B existing — you can't grow an adaptive immune system if the system can't mutate its own immune code at all.

## 7. Open operator decisions before Pass B begins

1. **Cross-Trinity scope.** Is Order 2 = "JARVIS Body cognitive code only" or "Trinity-wide cognitive code"? The PRD currently defers Trinity-wide explicitly. Pick one before Pass B starts.
2. **W2(5) graduation sequencing.** Pass B's `MetaPhaseRunner` only makes sense once the PhaseRunner extraction graduates (defaults flip true). Is Pass B blocked on W2(5) Slice 5b operator authorization, or do we draft the design now and gate flipping on W2(5)?
3. **Vocabulary landing.** Should the Reverse Russian Doll vocabulary be added to `OUROBOROS_VENOM_PRD.md` as a new top section (alongside Phase 1–6 roadmap), or kept as a complementary mental model? The vocabulary's Order axis is genuinely orthogonal to the Phase axis — they could coexist.
