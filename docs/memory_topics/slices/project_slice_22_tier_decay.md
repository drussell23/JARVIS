---
title: Mechanism
modules: []
status: historical
source: project_slice_22_tier_decay.md
---

PR #59077 squash-merged 2026-05-26 at `f7cbeeacde`. Branch `ouroboros/slice-22-dynamic-tier-decay`. Closes structural routing gap surfaced by v16 (`bt-2026-05-26-220930`).

# Mechanism

3 new module-level helpers in `urgency_router.py`:
- `_tier_decay_enabled()` — `JARVIS_TIER_DECAY_ENABLED` (default **TRUE** — load-bearing safety net)
- `_claude_tier_structurally_absent()` — `JARVIS_PROVIDER_CLAUDE_DISABLED` (mirrors Slice 19a contract verbatim)
- `_apply_immediate_tier_decay(reason) -> (route, str)` — post-classification demotion hook

All 4 IMMEDIATE return sites in `classify()` Priority-1 reflex block flow through the decay helper. When Claude absent + decay on, demotes IMMEDIATE → STANDARD with forensic-trail reason `tier_decay:immediate_to_standard:claude_absent:<orig_reason>` and logs §5-attested message at WARNING.

# Why STANDARD not elsewhere

STANDARD is the closest tier with DW as primary (§5). With Claude absent, cascade becomes DW → `fallback_skipped` (Slice 19b's already-validated contract). No new routing arithmetic.

# Why master defaults TRUE

The only failure mode of the decay is the SAME failure mode without it (cascade exhausts at dispatcher). The demotion only fires when `JARVIS_PROVIDER_CLAUDE_DISABLED` is itself opt-in. No graduation period needed.

# What this unlocks

v17 SWE-Bench-Pro op flow:
1. UrgencyRouter classifies IMMEDIATE (test_failure + high urgency)
2. **Slice 22 demotes → STANDARD**
3. Dispatcher → DW (Qwen3.5-397B per trusted_seed)
4. DW produces JSON
5. Parser succeeds OR Slice 20B heal OR Slice 20D drift rotates
6. APPLY → VERIFY → potentially RESOLVED

The healing matrix (Slices 20B/20C/20D + Phase 3) is **NOW REACHABLE**. v17 detonation tests whether reach → RESOLVED.

# Verification

11 tests (3 AST pins + 8 spine). AST pin walks `classify()` AST + asserts zero direct `return ProviderRoute.IMMEDIATE` legacy patterns + 4+ decay helper invocations. §5 attestation pin AST-walks the WARNING call + joins adjacent string literals (Python compile-time concat resilient). 68/68 regression across Slices 18c→22.

# v17 status

Launched 2026-05-26T22:41:35Z (PID 81316) at session `bt-2026-05-26-224138`. Slice 19a + 20A boot signatures confirmed firing. Waiting on SWE-Bench-Pro injection + tier_decay activation.

Related: [[project_slice_21_supervisor_containment]] (predecessor), [[project_slice_20a_self_fallback]] (Claude disable substrate), [[project_slice_20bc_healing_rotation]] (the matrix this unlocks), [[feedback_no_preresult_euphoria]] (v17 graduation bar is APPLY→VERIFY→RESOLVED, not "flags activated").
