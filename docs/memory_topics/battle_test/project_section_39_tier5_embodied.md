---
title: Project Section 39 Tier5 Embodied
modules: [backend/core/ouroboros/governance/architecture_viz.py, backend/core/ouroboros/governance/confidence_aura.py, backend/core/ouroboros/governance/attention_mirror.py, backend/core/ouroboros/governance/procedural_portrait.py, backend/core/ouroboros/governance/embodied_repl.py]
status: historical
source: project_section_39_tier5_embodied.md
---

May 9 2026: §39 Tier-5 closed end-to-end same-day. 4 read-only embodied surfaces + 1 combined REPL.

**Surface #5 — Architecture viz** `architecture_viz.py` (~470 LOC):
- Closed 8-value `OrganismZone` enum bytes-pinned to CLAUDE.md §1 zone numbering (Z0_BOOT through Z7_CONSCIOUSNESS)
- Bytes-pinned `_ACTIVITY_TO_ZONE` map for canonical activity_radar drift detection
- Composes canonical `activity_radar.aggregate_activity()` — ZERO parallel category aggregation
- Renders nested-box ASCII viz with active/idle pulse glyphs per zone
- 4 AST pins

**Surface #15 — Confidence aura** `confidence_aura.py` (~510 LOC):
- Closed 4-value `ConfidenceTier` (CERTAIN █ / CONFIDENT ▓ / UNCERTAIN ▒ / SCATTERED ░)
- Bytes-pinned `_MARGIN_THRESHOLDS = ((4.0, CERTAIN), (2.0, CONFIDENT), (0.5, UNCERTAIN))` natural-log probability margins
- Pure `_tier_for_margin` bucketing — None/NaN/non-finite → SCATTERED
- Composes canonical `ConfidenceTrace.tokens` + `ConfidenceToken.margin_top1_top2()` — ZERO parallel logprob math (AST pin enforces)
- Renders summary line + per-token tinted glyph strip with Rich background tints (on green/cyan/yellow/red)
- 4 AST pins

**Surface #16 — Attention mirror** `attention_mirror.py` (~560 LOC):
- Closed 4-value `AttentionFocus` (READING 📖 / SEARCHING 🔍 / THINKING 🤔 / IDLE ⋯)
- Composes canonical SSE broker `recent_history()` (filtered by tool_call_started + payload-keyword heuristic) + canonical `narrative_channel.find_by_kind(THINKING + TOOL_PREAMBLE)` for active BUFFERING frames
- Window clamped 5..300s (default 30s); primary focus by recency
- Renders "looking at" mirror with up to 6 most-recent items
- 4 AST pins

**Surface #17 — Procedural ASCII portrait** `procedural_portrait.py` (~580 LOC):
- Closed 3-value `PortraitMode` (AT_REST / WORKING / ALERT) composed from canonical mood + posture + heartbeat
- `_mode_for_inputs` first-match-wins: emergency/struggling OR harden → ALERT / neutral+maintain → AT_REST / else WORKING
- Bytes-pinned per-mode glyph catalogs (`_EYE_GLYPHS_AT_REST` / `_EYE_GLYPHS_WORKING` / `_EYE_GLYPHS_ALERT` + matching mouth catalogs)
- **Deterministic** face: `_seed = sha256(mode|mood|posture)[:8]`; `_pick_glyph` picks via `pool[int(sha256(seed|slot)[:8],16) % len(pool)]` — same inputs ALWAYS yield same face
- Composes canonical `polish_bundle.compute_mood + format_heartbeat` + `posture_palette.read_current_posture_safe`
- 3-line ASCII face frame with Rich title + seed footer
- 4 AST pins

**Combined `/embodied` REPL** `embodied_repl.py` (§33.3 auto-discovered): 7 subcommands (arch / aura / attention / portrait / all / status / help); `all` stacks the 3 always-renderable surfaces (aura skipped — requires per-op trace input).

**Four new SSE events** registered in canonical `_VALID_EVENT_TYPES` frozenset.

**Sub-flag granularity**: 4 separate masters all default-FALSE per §33.1.

**Regression**: 69 new tests + **919/919 cumulative** across §38.11 (A-F) + §39 Tier-1 + Tier-2 + Tier-3 + Tier-4 + Tier-5.

**§38.11.5a.5 single-canonical-name discipline honored**: arch reuses canonical activity_radar; aura reuses canonical ConfidenceTrace + margin_top1_top2 (ZERO parallel logprob math); attention reuses canonical broker + narrative_channel; portrait reuses canonical polish_bundle + posture_palette. Five NEW closed taxonomies (`OrganismZone` 8 + `ConfidenceTier` 4 + `AttentionFocus` 4 + `PortraitMode` 3 + 3 bytes-pinned glyph catalogs) — all AST-pinned.

**§33 patterns invoked**: §33.1 graduation contract / §33.3 naming-cage REPL / §33.5 versioned artifact (5 frozen artifacts).

**End-to-end smoke**: arch viz 8-zone box renders correctly; aura buckets 3 synthetic tokens to CERTAIN/UNCERTAIN/SCATTERED with Rich tints; attention IDLE state handled correctly; portrait produces deterministically-different ASCII faces for working/alert/at_rest modes.

**§39.5 sequencing status**: Tier 5 ✅ SHIPPED. Now **17 substrate modules + 12 §33.3 REPLs + 17 SSE event types + 73+ AST pins** across §38.11 + §39 (Tier 1+2+3+4+5).

**NEXT**: Tier-7 audio (#20 phase orchestra, ~3h smallest); Tier-6 trigger-gated until J-Prime + Reactor-Core online; OR autonomous self-development arc (Vector #11 ~2h cheapest, M10 ~7-10d substrate move).
