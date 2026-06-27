---
title: Project Vector 9 Mask Discipline Consumer Pin
modules: [tests/governance/test_wave3_hygiene_2026_05_05.py, backend/core/ouroboros/governance/observability/flag_change_emitter.py, backend/core/ouroboros/governance/observability/ide_routes.py, backend/core/ouroboros/governance/observability/sse_bridge.py]
status: historical
source: project_vector_9_mask_discipline_consumer_pin.md
---

May 9 2026: §35 row 🟡 #6 + §3.6.3 #5 both ✅ shipped. Closes Vector #9's broader concern (substrate→consumer chain enforcement) on top of Wave 3 v2.25's substrate closure.

**Audit verified Wave 3 v2.25 substrate closure**:
- `_SENSITIVE_NAME_TOKENS` FrozenSet — 10 bytes-pinned patterns (key/token/secret/password/passwd/pwd/credential/private/auth/session_id)
- `_is_sensitive_flag(name)` — case-insensitive substring match
- `_mask_value(value)` returns `<MASKED:sha256[:8]:len=N>` (None passes through to keep add/remove transitions distinguishable)
- `FlagChangeEvent.to_dict()` masks prev_value + next_value when `_is_sensitive_flag(name)`; surfaces `value_masked: bool` decision audit
- 16 existing regression tests (10 sensitive parametrized + 5 non-sensitive passthrough + None handling + token-set bytes-pin)

**Audit findings** — substrate is structurally clean RIGHT NOW:
- 8 internal access sites in `flag_change_emitter.py` (comparison helpers + property methods + to_dict masking branches; substrate-owned)
- 2 canonical external consumers (zero current violations):
  - `ide_routes.py` calls `d.to_dict()` then double-masks via presence-only `<set>`/`<empty>` markers
  - `sse_bridge.publish_flag_change_event` uses `getattr(event, "prev_value", None)` and pipes through its own `_mask_flag_value` helper before publish

**Closure** — structural AST allowlist pin in `tests/governance/test_wave3_hygiene_2026_05_05.py` (Item 7 section):
- `_walk_flag_value_access_sites(backend_root)` scans `backend/` (excluding venv/__pycache__) for `.prev_value`/`.next_value` Attribute access + `getattr(_, "prev_value"|"next_value")` Call patterns
- Bytes-pinned `_FLAG_VALUE_ACCESS_ALLOWLIST` (2 entries: `flag_change_emitter.py` substrate + `sse_bridge.py` canonical consumer)
- 5 regression tests:
  1. `test_mask_discipline_consumer_chain_pinned` — load-bearing AST scan asserting no out-of-allowlist access
  2. `test_allowlist_files_actually_use_the_fields` — anti-stale: every allowlist entry must still actually access the fields
  3. `test_allowlist_size_pinned` — forces reviewer attention on size change (adding a consumer requires updating BOTH set AND size assertion)
  4. `test_substrate_uses_mask_helper` — defense-in-depth bytes-pin on substrate's `_mask_value` + `_is_sensitive_flag` + `value_masked` field
  5. `test_sse_bridge_pipes_through_mask_helper` — defense-in-depth bytes-pin on canonical consumer's getattr+_mask_flag_value chain

**Stale comment fix**: `ide_routes.py:594` previously said "raw FlagChangeEvent.to_dict() echoes verbatim" (pre-Wave-3 truth). Updated to correctly cite Wave 3 substrate mask + the route's stricter presence-only double-mask as defense-in-depth.

**Test results**: 23/23 mask-discipline (16 existing + 5 new + 2 substrate authority pins) + **1081/1081 cumulative** across §38.11 (A-F) + §39 Tier-1+2+3+4+5+7 + Wave 3 hygiene + scheduler + canonical sources.

**§35 row 🟡 #6 + §3.6.3 row #5** both flipped ✅ Shipped 2026-05-09.

**Architectural discipline**: re-used canonical Python `ast` stdlib (zero new deps), composes the existing Wave 3 substrate (zero parallel masking logic), and pins the existing 2-consumer chain (zero refactor — just lock-in). The fix is structural enforcement, not behavior change.

**NEXT** (autonomy arc remaining):
- Vector #10 AutoCommitter race — Wave 3 v2.25 banner says it CLOSED Item 4 (`invariant_drift_store flock`) but explicitly DEFERRED Item 5 ("vector #10 AutoCommitter race ~1hr focused arc"). Need to verify current status — may genuinely still be open.
- Vector #8 ArtifactContract drift — Wave 3 deferred, multi-hour arc
- Vector #5 cross-session coherence harness — ~1-2 wks empirical validation
- M10 ArchitectureProposer — ~7-10d substrate move (closes weak-form ontogeny gap)
