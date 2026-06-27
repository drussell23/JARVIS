---
title: Project Section 38 11F Capability Constellation
modules: []
status: historical
source: project_section_38_11f_capability_constellation.md
---

May 8 2026: §38.11-F closed end-to-end same-day. **Final §38.11 slice — entire arc now closed in 2 days** (May 7: A+B; May 8: C+D+E+F).

**Substrate**: `governance/capability_constellation.py` (~840 LOC) + `governance/constellation_repl.py` (~330 LOC §33.3 auto-discovered).

**Closed taxonomy**: 5-value `ConstellationBrightness` (RADIANT ⭐ / GLOWING ✦ / DIM · / FAULTING ⚠ / DARK ○) **bytes-pinned 1:1** with canonical `UnifiedGraduationVerdict` via `_VERDICT_VALUE_BRIGHTNESS` map. **No parallel axis taxonomy** — the canonical 8-value `flag_registry.Category` enum IS the constellation axis (per §38.11.5a.5 single-canonical-name discipline; `_CATEGORY_PRINCIPLE_MAP` bytes-pin maps each Category to its Manifesto principle for SSE payload's `linked_principles`).

**Frozen §33.5 versioned artifacts**:
- `ConstellationStar` (flag_name + brightness + graduation_verdict + category + linked_principles + diagnostic + posture_relevance)
- `ConstellationSnapshot` (aggregated_at_unix + stars + by_brightness + by_category + elapsed_s) with `stars_by_brightness(brightness)` filter accessor

**Aggregator** `aggregate_constellation()` composes canonical sources via two-pass union: Pass 1 — flags with registry descriptors get full attribution; Pass 2 — contract-only flags surface as DARK to expose gaps. Stars sorted by `(category, flag_name)` for deterministic rendering. Singleton cache avoids recomputing per-render.

**New SSE event** `EVENT_TYPE_CAPABILITY_CONSTELLATION_UPDATED = "capability_constellation_updated"` registered in canonical `_VALID_EVENT_TYPES` frozenset. Payload carries the §38.11.5a row 6 contracted fields (`flag_name` / `brightness` / `graduation_state` / `linked_principles`) + summary maps; star list bounded to first 50.

**Sub-flag granularity**: master `JARVIS_CAPABILITY_CONSTELLATION_ENABLED` default-FALSE per §33.1 + 2 sub-flags + 1 tunable (refresh interval).

**`/constellation` REPL** (§33.3 auto-discovered): 6 subcommands (panel [N] / refresh / show <flag> / only <brightness> / status / help).

**5 AST pins**: master_default_false / authority_asymmetry / brightness_taxonomy_5_values / composes_canonical_graduation_dashboard / composes_canonical_flag_registry.

**Regression**: 48 new tests + **543/543 cumulative** across full §38.11 arc + canonical sources composed. Real-repo end-to-end smoke detected 389 stars (7 GLOWING + 1 DIM + 381 DARK).

**§38.11 ARC NOW FULLY CLOSED** — 6 substrate modules + 6 §33.3 auto-discovered REPLs (`/organism` / `/continuity` / `/anticipate` / `/introspect` / `/proposals` / `/constellation`) + 6 new SSE event types + ~30 AST pins + ~290 regression tests. ALL 5 §33 catalog patterns invoked across the arc.

**§38.11.5a.5 single-canonical-name discipline honored across entire arc**: ZERO parallel taxonomies, ZERO parallel substrate, ZERO `_v1.py`/`_v2.py` duplicates.

**§38.11.5a.2 row 6 closes §39 #8 "Constellation of capabilities"** structurally — the constellation IS the unified §38.11-F surface; no parallel §39 #8 implementation.

**§38.11 architectural conclusion**: O+V's autonomy is now legible across SIX operator-facing surfaces:
- A: alive indicators (heartbeat + risk light + time-of-presence)
- B: session continuity (graduation ticker + cross-session memory diff)
- C: proactive intervention (banners + pre-fetch indicator)
- D: introspective voice (4-axis NarrativeKind aggregator with DREAM extension)
- E: proactive proposals (4-producer ledger with accept/reject)
- F: capability constellation (flag star-map keyed by Manifesto axes)

The autonomy-aesthetic foundation §38.11 promised — "the path to uniquely professional is not adding more CC parity, it's making O+V's autonomy itself the operator-facing aesthetic" — is now structurally complete.

**NEXT**: §39 Tier-1+ (risk-tier ambient color tint + animated phase-flow ribbon, ~7h) OR autonomous self-development arc (Move 9+ producer-consumer loop closure).
