---
title: What it adds
modules: []
status: historical
source: project_slice_20bc_healing_rotation.md
---

PR #59074 squash-merged 2026-05-26 at `dfc064e24d`. Branch `ouroboros/slice-20bc-healing-rotation`. Combined arc closes 3 of 4 v15 failure shapes from `bt-2026-05-26-184355`; item #4 (RESOLVED verdict) is the v16 graduation bar.

# What it adds

**Slice 20B** — `json_healer.py` (519 LOC): last-resort LLM repair via Qwen3.5-35B-A3B-FP8 AFTER `providers._repair_json()` regex sweep exhausts. Zero-governance via `DoublewordProvider.prompt_only`. Immutable AST-pinned operator-attested system prompt. 30s/8192tok/64KiB bounds. `heal_and_retry_parse()` composition helper wired into DW provider's 3 `_parse_generation_response` sites via new `_parse_with_heal()` method. Master flag `JARVIS_JSON_HEAL_LLM_ENABLED` default FALSE.

**Slice 20C** — `schema_drift_tracker.py` (359 LOC): op-scoped per-(op_id, model_id) tracker. Closed 3-value `DriftType` enum (JSON_PARSE_ERROR_AFTER_HEAL / SCHEMA_ID_HALLUCINATION / ZERO_CANDIDATE_RETURN). Bounded rings (10 events/op × 256 ops). Master flag `JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED` default FALSE → `has_drifted()` short-circuits False in hot path. Wired into `candidate_generator._generate_dispatch` sentinel walk AFTER OPEN/TERMINAL_OPEN check, with `skipped_drift` classifier token.

**Phase 3** — `providers._build_lean_codegen_prompt` appends operator-attested zero-candidate prohibition on STANDARD/COMPLEX routes when `JARVIS_DW_ZERO_CANDIDATE_PROHIBITION_ENABLED=true`. BG/SPEC excluded (no Venom).

# Wiring map

- JSON_PARSE_ERROR_AFTER_HEAL drift: recorded in `heal_and_retry_parse` after heal+retry fails
- ZERO_CANDIDATE_RETURN drift: recorded in dispatch success branch when `result.candidates=()` and `not is_noop`
- SCHEMA_ID_HALLUCINATION drift: substrate ready, parser-level wiring at `providers.py:4091` deferred to follow-up slice

# Discipline

- All 3 masters default FALSE — operator enables in v16 runbook per `feedback_no_preresult_euphoria.md`
- No duplication: heal LAYERS on top of existing `_repair_json` regex sweep
- Acyclic substrate: `json_healer` takes callable, doesn't import provider
- 18 tests (4 AST pins + 14 spine), all green; 47/47 regression across Slices 18c-20C

# v16 detonation runbook

```bash
JARVIS_PROVIDER_CLAUDE_DISABLED=true               # Slice 20A (on main)
JARVIS_JSON_HEAL_LLM_ENABLED=true                  # Slice 20B
JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED=true          # Slice 20C
JARVIS_DW_ZERO_CANDIDATE_PROHIBITION_ENABLED=true  # Phase 3
```

# Graduation bar (v16 soak)

At least ONE candidate flows APPLY → VERIFY → RESOLVED on pure-DW. Until that artifact lands, this is methodology validation, NOT capability measurement.

Related: [[project_slice_20a_self_fallback]] (predecessor — eliminated double-bind), [[project_predictive_provider_resilience]] (broader arc), [[feedback_no_preresult_euphoria]] (graduation discipline).
