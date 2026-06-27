---
title: Project Section 38 11C Anticipation Surface
modules: []
status: historical
source: project_section_38_11c_anticipation_surface.md
---

May 8 2026: §38.11-C closed end-to-end same-day per operator authorization. Two surfaces in ONE substrate per §38.11.5a.5 single-canonical-name discipline (matches §38.11-B precedent).

**Substrate**: `governance/anticipation_surface.py` (~640 LOC pure-stdlib) + `governance/anticipate_repl.py` (~250 LOC §33.3 auto-discovered REPL) + 2 lines in canonical `_VALID_EVENT_TYPES` frozenset.

**Closed taxonomies**:
- 4-value `BannerKind` enum (SENSOR_INTERVENTION 🌐 / PROACTIVE_CURIOSITY 🔭 / CAPABILITY_GAP 🧩 / OPPORTUNITY 💡)
- 5-value `PrefetchKind` enum (READ_FILE 📄 / SEARCH_CODE 🔍 / GET_CALLERS 🔗 / GLOB_FILES 🗂 / OTHER •)

**Frozen §33.5 artifacts**: `InterventionBannerEvent` (banner_kind / signal_source / summary / op_id / risk_tier_label / queued_at_unix) + `PrefetchEvent` (op_id / prefetch_kind / tool_name / arg_summary / scheduled_at_unix).

**Composes canonical sources** (5 AST pins enforce):
- `narrative_channel.NarrativeChannel.frames_by_op_kind(NarrativeKind.INTENT, COMMITTED)` — banner prose lookup; module never produces parallel prose
- `ide_observability_stream` broker — single SSE event ring (no parallel publication)

**Two new SSE event types** registered: `EVENT_TYPE_INTERVENTION_BANNER_RAISED` + `EVENT_TYPE_PREFETCH_SCHEDULED`.

**Producer-bridge helpers (§33.2)**: `emit_banner(banner_kind=, signal_source=, summary=, op_id=, risk_tier_label=)` + `emit_prefetch(op_id=, prefetch_kind=, tool_name=, arg_summary=)` lazy-importable from sensors/PlanGenerator/Venom (best-effort, NEVER raises).

**Sub-flag granularity**: master `JARVIS_ANTICIPATION_SURFACE_ENABLED` default-FALSE per §33.1 + 2 sub-flags (BANNERS_ENABLED + PREFETCH_ENABLED) + 2 ring-size knobs.

**`/anticipate` REPL** (§33.3 auto-discovered): 5 subcommands (panel / banners [N] / prefetch [N] / status / help). Help bypasses master gate per §33.3 discoverability invariant.

**Regression**: 52 new tests + 397/397 cumulative across §38.11-A + B + C + canonical sources composed. Real-repo end-to-end smoke: 3 banners + 3 prefetches recorded; composite panel renders all 4 BannerKind glyphs + 3 PrefetchKind glyphs correctly; REPL surfaces all functional.

**§33 patterns invoked**: §33.1 graduation contract / §33.2 producer-bridge (emit_*) / §33.3 naming-cage REPL auto-discovery / §33.5 versioned artifact (2 frozen artifacts).

**NEXT** (§38.11.5a.2 row 4): §38.11-D — Introspective Voice (~9h merged with §39 #9; extends `narrative_channel.NarrativeKind` 6→7 values with new `DREAM` for DreamEngine output; self-correction routes through existing `L2_REPAIR_PROSE`; self-narration through existing `THINKING`+`INTENT`; AST pin update for taxonomy expansion).
