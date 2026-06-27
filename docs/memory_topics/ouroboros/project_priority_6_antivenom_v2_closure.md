---
title: Project Priority 6 Antivenom V2 Closure
modules: [backend/core/ouroboros/governance/verification/generative_quorum_gate.py, backend/core/ouroboros/governance/semantic_firewall.py, backend/core/ouroboros/governance/verification/postmortem_recall_consumer.py, backend/core/ouroboros/governance/verification/counterfactual_replay.py, backend/core/ouroboros/governance/flag_registry_seed.py]
status: historical
source: project_priority_6_antivenom_v2_closure.md
---

May 1, 2026: Priority #6 (Antivenom v2) closed via 4-vector bypass-closure arc + 4-directive root-cause structural repair.

**Four §29 bypass vectors closed by V1-V4:**
- V1 (`generative_quorum_gate.py`): BG/SPEC Quine-class — `compute_bg_spec_structural_check` + `BgSpecStructuralCheck` + `JARVIS_BG_SPEC_STRUCTURAL_CHECK_ENABLED`
- V2 (`semantic_firewall.py`): tool-output injection scan — `scan_tool_output` + `ToolOutputScanResult` + `JARVIS_TOOL_OUTPUT_INJECTION_SCAN_ENABLED` + `TOOL_INJECTION_REDACTED`
- V3 (`postmortem_recall_consumer.py`): advisory plausibility — `_CORE_FAILURE_CLASSES` + `_extract_failure_class` + `_advisory_plausibility_enabled` + `JARVIS_KNOWN_FAILURE_CLASSES`
- V4 (`counterfactual_replay.py`): payload laundering — `validate_swap_payload` + `_validate_swap_payload_enabled` + `JARVIS_REPLAY_PAYLOAD_VALIDATION_ENABLED`

**4-directive root-cause structural repair (not workarounds):**

1. **Dynamic Flag Registration**: `_discover_module_provided_flags()` in `flag_registry_seed.py` walks `_FLAG_PROVIDER_PACKAGES` curated tuple via `pkgutil.iter_modules` + `importlib.import_module`, calls module-owned `register_flags(registry)` on any module exposing it. Result: 144 total flags, 5 V1-V4 dynamically discovered — zero seed-file edits.

2. **Native AST Pinning**: Mirror discovery loop `_discover_module_provided_invariants()` in `meta/shipped_code_invariants.py` calls module-owned `register_shipped_invariants()` returning `(name, target, validate, schema_version)` tuples. Result: 52 total invariants, 4 V1-V4 dynamically discovered surface pins, 0 violations.

3. **Phase C MonotonicTighteningVerdict.PASSED stamping on V3**: Already structurally satisfied — `RecurrenceBoost` dataclass defaults `monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED.value`; `compute_recurrence_boosts` constructs without override. Every emitted boost canonically stamps PASSED.

4. **SSE Pin Drift via Property-Based Floor + Registration-Contract Exemption**:
   - 2 SSE vocab tests (W2(4) Slice 4 + W3(7) Slice 7) converted from `== N` exact-equality to `>= floor` additive-only contract — vocabulary may grow as later arcs add events but must never shrink. Floor names: `_SLICE_4_FLOOR=41`, `_SLICE_7_FLOOR=41`.
   - `MODULE_REGISTRATION_CONTRACT_FUNCS = frozenset({"register_flags", "register_shipped_invariants"})` recognized by hot-path purity pins. `_validate_counterfactual_replay_pure_stdlib` + V1/V3 `test_governance_imports_in_allowlist` exclude `ImportFrom` nodes nested under those canonically-named functions. Contract enforced by shared function names (architectural invariant), not allowlist exceptions per module.

**Why:** The hot-path purity pins were correctly enforcing that primitives must be pure-stdlib at runtime — but the module-owned registration pattern needs governance imports at BOOT time only. The structural answer is contract recognition, not pin loosening or companion-file proliferation.

**How to apply:** Future modules adopting the module-owned registration pattern get registry+invariant integration for free by exposing `register_flags(registry) -> int` and `register_shipped_invariants() -> list[tuple]`. No seed-file edits needed; no allowlist edits needed; no companion files needed. Add the package to `_FLAG_PROVIDER_PACKAGES` / `_INVARIANT_PROVIDER_PACKAGES` only if it's in a new package not yet covered.

**Verified clean:**
- All 3 V1/V3/V4 authority pin tests green post-fix (commit 441cdc7bd2)
- Both SSE pin floor tests green (44/44 in fixed file pair)
- Stash-verified: 6 broader-sweep failures (test_review_shadow_hook, test_topology_active_recovery, test_phase_8_temporal_observability, test_semantic_guardian count, test_p4/p5 PermissionError) all pre-existing on clean tree — orthogonal to V1-V4 work

**Commits:** e3871bae0c (dynamic registration substrate), d494b8a685 (V2/V3/V4 invariants + SSE floor edits), 441cdc7bd2 (registration-contract exemption fix).
