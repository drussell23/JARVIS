---
title: DW reasoning_effort=none fix + adaptive Reasoning-Capability Profiler (PR #69633 MERGED, main ae1e1d2009, 2026-06-21)
modules: []
status: merged
source: project_dw_reasoning_capability_profiler.md
---

# DW reasoning_effort=none fix + adaptive Reasoning-Capability Profiler (PR #69633 MERGED, main ae1e1d2009, 2026-06-21)

**Trigger:** Meryem Arik (Co-Founder & CEO, Doubleword) emailed personally (escalation — prior contact was Seb, a DW Core Engineer): "GPT-OSS-120B erroring — you had reasoning_effort:none; for this model you can't disable reasoning. Remove it and it'll be fine. You won't be charged for errored requests. Use support@doubleword.ai for future issues."

**This was the real cause of the gpt-oss-120b DoublewordInfraError** chased for hours during the graduation crucible. Root: `doubleword_provider._reasoning_effort_for` floors effort up to `_dw_model_min_effort(model)`; the static seed `_DEFAULT_DW_MODEL_MIN_EFFORT="deepseek-v4-pro:low"` only covered deepseek (Seb's 2026-06-08 fix) — gpt-oss got NO floor → stayed "none" → DW rejected → error. Confirmed live: `_reasoning_effort_for("trivial","openai/gpt-oss-120b")` returned "none".

**Fix (2 layers, no hardcoded model list, composes existing 3-tier `_dw_model_min_effort` resolver):**
1. **Immediate seed**: `_DEFAULT_DW_MODEL_MIN_EFFORT="deepseek-v4-pro:low,gpt-oss:low"` (substring match covers gpt-oss-120b + gpt-oss-20b). Now trivial→low, complex→medium; never "none".
2. **Adaptive self-learning `dw_reasoning_profile.py` (NEW, mirrors dw_transport_profile)**: `maybe_learn_from_error(model, effort_sent, err_body)` wired at the RT error seam (doubleword_provider ~3269) — when a DW error body matches a reasoning-rejection pattern (env-tunable `JARVIS_DW_REASONING_REJECTION_PATTERNS`, CONSERVATIVE — entitlement/transport/timeout NEVER mis-train), `record_reasoning_floor(model)` persists a monotonic floor to `.jarvis/dw_reasoning_profile.json` (GCS-backed, rehydrates per fork). `_dw_model_min_effort` consults it as the ADAPTIVE tier: **dynamic catalog (DW /v1/models) → learned (errors) → static seed → none**. Master `JARVIS_DW_REASONING_PROFILE_ENABLED` default-true, OFF byte-identical. Self-heals for ANY future reasoning-incapable model DW adds, from ONE error.

43 tests (11 new + 32 Slice 168/169/54/55 reasoning regression). DW is O+V's PRIMARY generation tier — this keeps it clean.

**Relationship note:** CEO-level contact = take seriously. The bug was OURS (we sent the bad param), not DW's. Future issues → support@doubleword.ai. Drafted a warm update email to Meryem (root cause owned + fixed + the adaptive beef-up + a milestone update: O+V autonomously graduated its first self-improvement powered by DW). See [[project_cognitive_graduation_crucible]].
