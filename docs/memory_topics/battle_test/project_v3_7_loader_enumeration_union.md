---
title: Project V3 7 Loader Enumeration Union
modules: [tests/test_smoke.py, backend/core/ouroboros/governance/swe_bench_pro/dataset_loader.py, tests/governance/test_swe_bench_pro_dataset_loader.py]
status: historical
source: project_v3_7_loader_enumeration_union.md
---

May 12 2026 — Phase A `list_cached_problems()` enumeration union fix shipped on branch `ouroboros/swe-bench-pro/loader-enumeration-union`.

## Why this PR exists

Stage-1 wiring-validation soak `bt-2026-05-13-025330` exposed the bug. The SWE-Bench-Pro harness boot hook fired correctly with `JARVIS_SWE_BENCH_PRO_ENABLED=true` and `JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH` pointing at the fixture, but emitted verdict `skipped_no_problems` with the diagnostic:

```
[SWEBenchPro.HarnessInject] master flag ON but no problems available
  (cache empty + no CSV override) - nothing to inject
```

The fixture WAS readable (the loader's `load_problem(id)` could resolve it), but `list_cached_problems()` couldn't see it because it only scanned the cache directory.

Operator framing (2026-05-12):
> `list_cached_problems()` and the harness hook disagreed on what "available problems" means. If `JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH` is a supported config surface, enumeration must include it — otherwise every consumer that trusts `list_cached_problems()` is lied to, and you are forced onto CSV overrides forever (that is the workaround).

## Architectural decisions

**Root problem solved at source — no workaround**:

The shortcut would have been to keep using `INJECT_INSTANCE_IDS` CSV overrides whenever fixture-only config was needed. The operator explicitly rejected that path:
> CSV overrides forever (that is the workaround).

The structural fix: make `list_cached_problems()` honest about ALL sources Phase A's `load_problem()` can resolve from. The function is now the single source of truth — returns `cache_ids ∪ jsonl_instance_ids`. Any consumer that trusted `list_cached_problems()` before the fix is now seeing complete data.

**Composition discipline — `_iter_local_jsonl_records`**:

Operator binding: "Reuse the same path normalization / ID parsing helper `load_problem` uses." The fix extracts the shared scanning logic into `_iter_local_jsonl_records()`, which both `_load_from_local_jsonl` (per-id load) and `list_cached_problems` (enumeration) compose. Single source of truth for local-JSONL parsing.

**Bounded scan**:

Operator binding: "bounded scan — cap lines/bytes if needed." Default cap `_LOCAL_JSONL_MAX_ROWS = 10000` (comfortable headroom over upstream SWE-Bench-Pro 1,865-problem dataset). Cap is read at CALL time (not function-def time) so tests can monkey-patch the module constant without redefining the function.

**Per-source fail-open**:

The cache scan and JSONL scan are each wrapped in independent try/except blocks. A failure in one source produces an empty set from that source — the other source's enumeration still proceeds. This honors the fail-closed contract per-source.

**Dedup semantics**:

Cache and JSONL duplicates collapse to a single instance_id (set union). Callers see each id exactly once. Sorted output preserved.

## Composition discipline — what was deliberately NOT done

- No new env var — the existing `JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH` is the canonical config; the fix just makes enumeration honest
- No new public API — `list_cached_problems()` signature unchanged, behavior just becomes complete
- No mid-tier helper like `list_jsonl_problems()` — single-source-of-truth pattern means callers go through `list_cached_problems()` only
- No JSONL caching/indexing — bounded scan is fast enough (10K lines is sub-100ms; fixture is 1 line is sub-1ms)
- No performance triage bundled in (separate memo filed)

## Stage-1 acceptance bar passed

The focused validator (uncommitted `/tmp` diagnostic per operator binding) exercises the full pipeline end-to-end against real Phase A loader + real B.2.1 envelope builder + canonical IntakeLayerService.ingest_envelope:

```
STEP 1: list_cached_problems()  →  ['jarvis__harness-smoke-001']   ✓ 1.2ms
STEP 2: load_problem('jarvis__harness-smoke-001')  →  loaded         ✓ 3.1ms
STEP 3: maybe_inject_swe_bench_at_boot(stub_intake)  →  INJECTED     ✓ 1.3ms
  envelope.source == 'swe_bench_pro'                                  ✓
  envelope.target_files == ('tests/test_smoke.py',)  [from test_patch] ✓
  envelope.evidence.repo_root == <prepared worktree>  [B.2.0 contract] ✓
```

Note on the sandbox: the validator stubbed `prepare_problem` because this sandbox blocks `git clone --templates` (template-hook copy disallowed). Before the stub was installed, the hook returned `verdict=failed_inject` correctly — fail-closed contract working. In an unrestricted environment the real clone of `octocat/Hello-World` would succeed and the full chain would land verdict=INJECTED naturally.

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/dataset_loader.py` — `_iter_local_jsonl_records` extraction + `list_cached_problems` union semantics + bounded-scan constant
- `tests/governance/test_swe_bench_pro_dataset_loader.py` — 7 new spine tests
- `docs/architecture/OUROBOROS_VENOM_PRD.md` — §40.7.10-union closure paragraph
- `memory/project_intake_layer_start_perf_triage.md` — separate perf triage (do NOT bundle)

## What's next

- **Optional**: one live run with `JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS=jarvis__harness-smoke-001` to prove the CSV override path still works (not as substitute for the structural fix, just to confirm we didn't break the override path)
- **Stage 2**: real benchmark cherry-pick — only after operator decides budget. Requires HF token (currently unset in this shell) + `JARVIS_SWE_BENCH_PRO_HF_DATASET` config. Conditional on stage-1 green.
- **Performance triage**: `IntakeLayerService.start()` 5.5-min cold-start — independent track, not bundled
