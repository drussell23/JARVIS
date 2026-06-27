---
title: Project Section 37 Ux Brutal Review
modules: []
status: historical
source: project_section_37_ux_brutal_review.md
---

§37 captures the operator-driven brutal review of O+V's CLI/UX surface vs Claude Code. Goal: comprehensive parity audit + sequenced roadmap + anti-goal preservation of O+V's autonomous-organism identity.

**Why:** the operator wanted unvarnished critique to ground UX investment decisions. The 41-verb surface + 6 live rendering layers + emoji vocabulary scored well overall (A− happy path / B+ edge cases) but had specific, fixable inconsistencies + 7-9 high-leverage Tier 1 gaps.

**How to apply:** when scoping UX/UI work, consult §37 first. Three rules from the operator binding:
1. **Identity preservation is non-negotiable** — emojis, narrative voice, ouroboros spinner, posture awareness, `/expand` cross-substrate dispatch survive every iteration
2. **Cross-ecosystem is the horizon** — 5 surfaces become load-bearing once J-Prime + Reactor-Core come online; substrate work today must be forward-compatible
3. **Brutal grading is honest grading** — current state is good, not great; closing the named gaps gets to A on both axes

**5-axis grade card**:

| Axis | Grade | One-line defense |
|---|---|---|
| Discoverability | **B** | 41 verbs is a lot; auto-discovered `/help` helps; new operator without docs won't figure out `/expand t-3` from scratch |
| Density | **A** | Multi-surface strategy (status line + narrative + op blocks) avoids wall-of-text |
| State legibility | **A** | Bottom-toolbar + per-op refs + stream metrics + `/status`/`/cost` on demand |
| Navigability | **B** | Auto-history + completion solid; no breadcrumb / op-graph / time-travel |
| Aesthetic identity | **B+** | Emoji vocabulary tight; ouroboros spinner iconic; legacy boot path over-renders + color discipline leaks in chrome |

**Net: A− on happy path / B+ on edge cases.**

**40-feature CC catalog — high-level summary**:
- A. Conversation+chat: 4 features (3 ✅, 1 🟡, 0 ❌, 1 ⛔)
- B. Tool+observability: 7 features (3 ✅, 1 🟡, 3 ❌)
- C. Diff+apply: 5 features (4 ✅, 1 🟡, 1 ❌)
- D. Discoverability+help: 4 features (2 ✅, 1 🟡, 1 ❌)
- E. Mention+completion: 3 features (1 ✅, 2 🟡)
- F. Status+state: 6 features (5 ✅, 1 🟡)
- G. Session+history: 4 features (0 ✅, 1 🟡, 3 ❌) — **biggest gap area**
- H. Plan+reasoning: 5 features (3 ✅, 1 🟡, 1 ❌)
- I. Multi-op+parallel: 4 features (2 ✅, 0 🟡, 2 ❌)
- J. Permissions+safety: 5 features (3 ✅, 0 🟡, 2 ❌)
- K. Skills+workflows: 4 features (1 ✅, 2 🟡, 1 ❌)
- L. Aesthetic+chrome: 7 features (5 ✅, 0 🟡, 1 ❌, 1 ⛔)

**Total: 40 features. ~30 ✅ PRESENT or PARTIAL. ~10 ❌ MISSING. ~3 ⛔ DELIBERATELY-NOT-PORT.** ~75% feature parity with CC; the 25% gap is named + sequenced.

**Tier 1 closures (~20h total, tonight/this-week)** — composes existing substrate, high operator-value:
1. Approaching-budget warning + token-budget meter (~3h)
2. `@mention` file completion via PathCompleter (~2h)
3. `/show-plan` REPL verb (~2h)
4. `/health` (composes 6 unwired autonomy modules) (~1.5h)
5. `/listen` event-stream tail (~2h)
6. Pre-trip circuit-breaker warnings via SSE (~3h)
7. `/why-changed` operator-feedback inline (~1.5h)
8. Color discipline lint pin (~1h)
9. OSC 8 hyperlinks on `/help` and refs (~2h)

**Tier 2 multi-day arcs**:
- `--rerun-from` + `/replay` REPL (~3d Temporal Observability spine)
- Session search via SQLite index (~4-5h)
- Op dependency graph / parallel fan-out canvas (~5h)
- Per-tool confidence indicator (~4h)
- Operation modes (`/plan` `/analyze` `/apply` `/auto`) (~1 slice)
- Per-tool permissions Venom V2 (~2 slices)
- Per-component tool scope Pattern C (~2 slices)

**Tier 3 ecosystem prep** (trigger-gated, fires when J-Prime/Reactor-Core come online):
- Multi-repo status-line composition
- Per-repo posture rendering
- Cross-repo causality DAG (`repo` in record)
- Cross-repo cost aggregation
- Cross-repo flag graduation (`repo` in ledger row)

**6 identity-preservation invariants** (must hold across all UX evolution):
1. Ouroboros spinner is permanent
2. Emoji vocabulary stays bounded (each emoji = ONE meaning)
3. Color discipline (green = outcomes only) — Tier 1 #8 pins this
4. Narrative voice (`💭🗣🤔🔧`) non-negotiable
5. Posture visibility (operator can always see EXPLORE/CONSOLIDATE/HARDEN/MAINTAIN)
6. `/expand <ref>` cross-substrate dispatch preserved (never fork into per-substrate verbs)

**5 anti-goals** (do NOT port from CC):
1. Theme support (low value; identity dilution)
2. Resumable mid-op sessions (atomic-op model is correct)
3. Conversation continuity at the "the conversation is the product" level (we have LSS + SemanticIndex + ConversationBridge — that's enough)
4. Per-message regeneration (our `--rerun-from` is more disciplined)
5. CC's "auto" model selection (UrgencyRouter is more principled)

PRD §37 is the canonical reference. v2.37 → v2.38 with Phase 9 Day 1 graduation + §37 added.
