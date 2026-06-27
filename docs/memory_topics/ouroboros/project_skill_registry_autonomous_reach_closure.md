---
title: SkillRegistry-AutonomousReach — CLOSED 2026-05-02
modules: [backend/core/ouroboros/governance/skill_manifest.py, backend/core/ouroboros/governance/skill_catalog.py, tests/governance/test_skill_manifest.py, tests/governance/test_skill_catalog.py, tests/governance/test_skill_graduation.py, backend/core/ouroboros/governance/skill_trigger.py, backend/core/ouroboros/governance/skill_observer.py, backend/core/ouroboros/governance/skill_venom_bridge.py, backend/core/ouroboros/governance/tool_executor.py]
status: merged
source: project_skill_registry_autonomous_reach_closure.md
---

# SkillRegistry-AutonomousReach — CLOSED 2026-05-02

5-slice arc closing the Skills/slash-commands ⚠️ row in the CC-parity table by ADDING the **autonomous-trigger reach** — the third surface beyond CC's operator+model. CC Skills are reactive (operator types `/<name>` OR model reaches for the tool); O+V Skills add a third reach (`AUTONOMOUS`) where the SkillObserver fires skills on TrinityEventBus signal preconditions without operator typing or model prompting. **Same composable verb surface, three reaches.**

## What was already there (avoided duplicating)

- `skill_manifest.py` (528 LOC) — SkillManifest dataclass with name/desc/trigger/entrypoint/permissions/version + YAML loader + arg validation
- `skill_catalog.py` (838 LOC) — SkillCatalog (authority-gated registration: OPERATOR/ORCHESTRATOR; MODEL refused) + SkillInvoker (resolve+run) + SkillMarketplace (filesystem discovery) + `/skills` REPL dispatcher
- `test_skill_manifest.py` + `test_skill_catalog.py` + `test_skill_graduation.py` (124 tests)

The existing arc covered **operator + filesystem marketplace + invoker pipeline + REPL**. What was missing: `reach` declaration on the manifest, structured `trigger_specs`, autonomous trigger surface, Venom tool-surface bridge.

## Slices shipped (additive on top of existing arc)

- **Slice 1** — `skill_trigger.py` (pure-stdlib decision primitive). 3 closed-5 enums (SkillReach / SkillTriggerKind / SkillOutcome), 3 frozen dataclasses (SkillTriggerSpec / SkillInvocation / SkillResult), total `compute_should_fire` NEVER-raises decision function, strict-dialect validators (`parse_reach` / `parse_trigger_kind` / `parse_trigger_spec_mapping`). Additive backward-compat fields on existing SkillManifest: `reach: SkillReach = OPERATOR_PLUS_MODEL` + `trigger_specs: Tuple[...] = ()`.
- **Slice 2** — `SkillCatalog.triggers_for_signal` kind-keyed lookup index (O(K), race-resilient via lock-domain snapshot). Promoted `_spec_matches_invocation` → public `spec_matches_invocation` so catalog reuses the SAME predicate the decision function uses (zero parallel decision paths).
- **Slice 3** — `skill_observer.py` async bridge. TrinityEventBus → catalog narrow → decision → SkillInvoker. Per-spec subscription with closure capturing `(qname, spec_index, kind)` — zero hardcoded topic→kind table; operators declare in YAML. Hot-reload via `catalog.on_change` listener. Bounded concurrency via `asyncio.Semaphore` + opt-in dedup (`spec.dedup_key_template`) + sliding-window rate limit. Defensive try/except at every async boundary; `asyncio.CancelledError` propagates.
- **Slice 4** — `skill_venom_bridge.py` model surface. 3 surgical additive edits to `tool_executor.py` mirroring MCP's `mcp_*` pattern: Rule 0 amendment (skill__* exempt from immediate DENY), Rule 0c-skill (catalog-gated ALLOW), backend dispatch (skill__* → `dispatch_skill_tool` → `(ok, output, error)` tuple → ToolResult). The bridge MUST NOT import `tool_executor` (AST-pinned) — zero circular dep via the tuple return.
- **Slice 5** — graduation: 3 master flags flipped false→true; 3 modules own register_flags (8 FlagRegistry seeds); 3 modules own register_shipped_invariants (4 AST pins); SSE `EVENT_TYPE_SKILL_INVOKED` + `publish_skill_invocation` helper; observer fires SSE on every fire-or-skip decision (full lifecycle observability); operator escape hatches preserved (explicit "false" overrides graduated default).

## Tests + counts

- **421/421 combined Skills arc** (124 existing + 297 new across Slices 1-5)
- 8 FlagRegistry seeds (3 trigger + 3 observer + 2 bridge)
- 4 AST pins (skill_trigger pure-stdlib + 5-value taxonomies; skill_observer authority + bounded-concurrency; skill_venom_bridge authority with explicit `tool_executor` forbidden)
- New SSE event type `skill_invoked` (closed-4 SKIP_REASON vocab in payload)

## Bugs caught + fixed by the test spines

- **Slice 1**: `reach_includes(OPERATOR_PLUS_MODEL, OPERATOR_PLUS_MODEL)` returned False (set-of-self). Without fix every EXPLICIT_INVOCATION against an OPERATOR_PLUS_MODEL skill silently routed to SKIPPED_DISABLED — entire common-case dead. Fixed: OPERATOR_PLUS_MODEL is set containing OPERATOR + MODEL + itself.
- **Slice 1**: `parse_trigger_spec_mapping` lost spec-list path context when re-raising kind-parse errors — wrapped to `trigger_specs[N].kind: ...`.
- **Slice 3**: dedup was always-on via structural-fingerprint fallback, swallowing distinct events with identical payload shapes before rate-limit could engage. Made opt-in via `spec.dedup_key_template` (non-empty).

## Deferred to post-Slice-5 follow-ups

- `/skills` REPL command extension to surface trigger-index counts + observer subscription state
- 4 GET routes (`/observability/skills`, `/skills/<qname>`, `/skills/observer-state`, `/skills/trigger-index`)
- Orchestrator wire-up to inject `render_skill_tool_block()` into GENERATE prompt automatically (Slice 4b — currently caller-driven)
- Singleton `get_default_observer()` boot wire-up at SerpentFlow init (mirrors PostureObserver Slice 5b pattern)

## Reverse Russian Doll posture

O+V's outermost doll gains the proactive autonomous-trigger surface (the cognitive primitive CC has no analogue for). Antivenom scales proportionally:
- Closed-5 taxonomies AST-pinned at the primitive
- Reuse contracts AST-pinned (skill_trigger pure-stdlib + sync; skill_observer authority allowlist + bounded-concurrency; skill_venom_bridge MUST NOT import tool_executor)
- Per-surface escape hatches via explicit env false
- Defensive try/except at every async boundary
- asyncio.CancelledError propagates everywhere
- SSE publish failure cannot stall the observer
- Catalog narrows; compute_should_fire decides — single source of truth for fire/skip, no parallel decision paths

Closes Cognitive Delta gap #1 from the post-PlanFalsificationDetector assessment (B+ overall, A- after Move 6 graduation).
