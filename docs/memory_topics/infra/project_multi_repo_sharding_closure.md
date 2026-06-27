---
title: Multi-Repo SemanticIndex/DomainMap Sharding — CLOSED 2026-05-03
modules: [scripts/multi_repo_sharding_closure_verdict.py, backend/core/ouroboros/governance/multi_repo/repo_signature.py, backend/core/ouroboros/governance/semantic_index.py, backend/core/ouroboros/governance/domain_map_memory.py, tests/governance/multi_repo/test_repo_signature.py, tests/governance/test_domain_map_memory.py]
status: historical
source: project_multi_repo_sharding_closure.md
---

# Multi-Repo SemanticIndex/DomainMap Sharding — CLOSED 2026-05-03

4-slice arc closing the Tier 2 #4 gap from the user's roadmap table. Pre-arc state: `multi_repo/{registry,blast_radius,context_builder,repo_pipeline}.py` already shipped (RepoRegistry from Mar 2026, wired into providers + plan + design_prompt + governed_loop_service). But the cognitive substrate -- `SemanticIndex._DEFAULT_INDEX` and `DomainMapStore._default_store` -- were process-wide singletons that **locked to the first-caller's `project_root` and silently ignored subsequent root args**. Multi-repo workspaces (jarvis + prime + reactor-core via env vars `JARVIS_REPO_PATH`/`JARVIS_PRIME_REPO_PATH`/`JARVIS_REACTOR_REPO_PATH`) had:

- All repos crammed into one shared SemanticIndex (corpus + clusters from disparate repos blended)
- DomainMap entries colliding on `centroid_hash8` across unrelated repo boundaries
- Cross-session memory bleeding between repos (Day-N memory for repo A biased by repo B's recent commits)

## Slices shipped

- **Slice A** — New `multi_repo/repo_signature.py` primitive. `compute_repo_signature(path) -> sha256[:8]` of `Path.resolve()` (pure stdlib + pure function — no env reads, no clock, no random, deterministic across runs and processes; tolerates non-existent paths via `Path.resolve()` non-strict mode). `repo_label_for(path, registry)` returns RepoRegistry name when `local_path.resolve()` matches, else dir basename, else `"unknown"`. `register_shipped_invariants()` AST pin (`multi_repo_signature_substrate`) catches: missing functions; `_SIGNATURE_LEN` drift away from 8; determinism break via `time.*`/`random.*`/`monotonic.*` calls inside `compute_repo_signature` body (root-namespace walk, not just leaf attr — caught a real bug in initial implementation). **19 regression tests.**
- **Slice B** — `SemanticIndex._DEFAULT_INDEX: Optional[SemanticIndex]` replaced by `_DEFAULT_INDICES: Dict[str, SemanticIndex]` keyed on shard signature. `get_default_index(project_root)` resolves signature → returns or creates entry. `reset_default_index()` clears all (legacy test-fixture behavior); `reset_default_index(project_root)` clears one shard. Single-repo callers see byte-identical behavior (one signature → one entry). **Verified empirically: same root twice → same instance; distinct roots → distinct instances; per-shard reset isolated.**
- **Slice C** — `DomainMapStore._default_store: Optional[DomainMapStore]` replaced by `_default_stores: Dict[str, DomainMapStore]`. Same pattern. Preserved deferred-init contract (`get_default_store()` no-arg post-construction returns `None` — multi-repo callers MUST disambiguate by passing `project_root`). On-disk layout already per-`<project_root>/.jarvis/domain_map/` so file-system entries also stay separated naturally — only the in-memory store object needed sharding. Updated `domain_map_memory_authority` AST pin allowlist to permit the new `repo_signature` import (extending the existing import-discipline pattern, not bypassing it). **Verified: distinct stores per shard; single-repo identity preserved; per-shard reset; on-disk dirs separated.**
- **Slice D** — Empirical-closure verdict script `scripts/multi_repo_sharding_closure_verdict.py` proving all three contracts in-process. Closure memory entry. MEMORY.md updated.

## Architectural decisions worth remembering

- **No master flag**. The change is strictly additive — single-repo callers see identical behavior (`compute_repo_signature(cwd)` produces one signature → one dict entry == old singleton). Adding a `JARVIS_MULTI_REPO_SHARDING_ENABLED=false` escape hatch would have meant **hardcoding the old broken pathway**, which directly violates the user's "no hardcoding, leverage existing architecture" directive. Tests prove backward compat instead.
- **Deferred-init preserved for `DomainMapStore.get_default_store()` no-arg path**. Pre-shard, no-arg returned the first-set instance (the silent-shared-state bug). Post-shard, no-arg returns `None` always — multi-repo callers MUST disambiguate. Verified all production callers pass `project_root` (the only no-arg `get_default_store()` calls in the codebase point at `posture_observer.get_default_store()`, a different module). Existing test that asserted the old chain-of-identity was updated to use explicit per-shard args + a new assertion that no-arg returns `None`.
- **Path-based signature, not registry-name-based**. RepoRegistry names ("jarvis", "prime", "reactor-core") are configurable env vars and may not always be set (e.g., a fresh checkout without env config). `Path.resolve()` is the authoritative key — works whether RepoRegistry is configured or not, deterministic, collision-resistant. The registry name is just the friendlier label for telemetry (`repo_label_for`), never the shard key.
- **AST root-walk for determinism check** caught a real bug. Initial pin matched `Call.func.attr` (leaf name only). `random.choice(...)` parses as `Call(func=Attribute(value=Name('random'), attr='choice'))` — leaf is `'choice'`, NOT in the banned set. Fixed by walking the Attribute chain to the root `Name` and checking both root and leaf. The substrate test `test_invariant_catches_determinism_break_via_random` failed on first run, exposing the bug before merge.
- **`Path.resolve()` non-strict mode**. Multi-repo registries may point at repo paths that haven't been cloned locally (e.g., a developer's env vars promise prime+reactor but only jarvis is checked out on this machine). The shard key MUST stay stable for the missing repo too — otherwise the singleton dict gets a new entry every call, defeating the cache. `Path.resolve()` with default `strict=False` normalizes syntactically without statting; `OSError` (circular symlinks, the rare edge case) falls back to `os.path.normpath`. NEVER raises.

## Test counts + AST pins

- **322/322 combined sweep** (19 new repo_signature + 91 pre-existing domain_map + 134 semantic_index + 30 cluster_intelligence_slice2_consumers + 47 cluster_intelligence_graduation + 12 semantic_inference_default_flip + 52 semantic_adaptive_embedder + others); zero regressions in semantic+domain_map family
- **1 new AST pin**: `multi_repo_signature_substrate` (target_file=repo_signature.py): functions present + signature length pinned at 8 + determinism guarantee via root-namespace banned-call detection + no exec/eval/compile
- **1 existing AST pin extended**: `domain_map_memory_authority` allowlist now includes `repo_signature` (the only new dependency added by Slice C; pin still bans every other governance import as before)

## Empirical-closure verdict (all in-process, no soak)

```
[PASS] C1 Repo signature deterministic + unique + length=8
       sig(/tmp)=11fe14a5 sig(/tmp)_repeat=11fe14a5 sig(/usr)=894d731f length=8
[PASS] C2 SemanticIndex per-repo isolation + identity + reset
       distinct_roots->distinct_instances=True
       same_root->same_instance=True
       per_shard_reset_isolated=True
[PASS] C3 DomainMapStore per-repo isolation + on-disk separation
       distinct_roots->distinct_instances=True
       same_root->same_instance=True
       on_disk_dirs_separated=True
       none_arg_after_construction_returns_None=True
       per_shard_reset_isolated=True
[PASS] C4 AST pins hold against current source (advisory)
       results=[multi_repo_signature_substrate=PASS, domain_map_memory_authority=PASS]
```

## Reuse contract honored (no duplication)

- Existing `multi_repo/registry.py` reused as the friendly-label resolution source
- Existing `Path.resolve()` + `hashlib.sha256` (pure stdlib) — no new deps
- Existing `_DEFAULT_INDEX_LOCK` + `_default_store_lock` reused for thread safety
- Existing `ShippedCodeInvariant` + `register_shipped_invariants` registration contract reused
- Existing `cross_process_jsonl.flock_critical_section` for DomainMap entries — unchanged (each per-repo store still uses its own per-entry flock)
- DomainMap on-disk layout `<project_root>/.jarvis/domain_map/<centroid_hash8>.json` unchanged — already correctly per-`project_root`, only the in-memory cache needed sharding

## Reverse Russian Doll posture preserved

Outer doll (the substrate) gained correct multi-repo isolation by default. Antivenom scaled proportionally: AST pin enforces shard-key stability (`_SIGNATURE_LEN=8` + determinism); `domain_map_memory_authority` pin extended to whitelist exactly one new module (no broader import surface drift); 19 substrate tests + 322 combined sweep + verdict script for empirical regression detection.

## What this unlocks

The user's table flagged this gap as: "Required for any generalization beyond JARVIS. Point O+V at any Python repo and it works." Pre-arc, pointing O+V at a 2-repo workspace would have collapsed both into one cluster space. Post-arc, each repo gets its own SemanticIndex (per-repo corpus, per-repo clusters, per-repo k-means), its own DomainMap (per-repo cross-session memory), with single-repo behavior unchanged. This is the substrate prerequisite for Tier 3 RSI Pass B graduation work that operates across the trinity (jarvis + prime + reactor-core).

## Files touched

- `backend/core/ouroboros/governance/multi_repo/repo_signature.py` (NEW)
- `backend/core/ouroboros/governance/semantic_index.py` (singleton → dict shard)
- `backend/core/ouroboros/governance/domain_map_memory.py` (singleton → dict shard + AST pin allowlist extension)
- `tests/governance/multi_repo/test_repo_signature.py` (NEW, 19 tests)
- `tests/governance/test_domain_map_memory.py` (one test updated to reflect post-shard semantic + new no-arg-None assertion)
- `scripts/multi_repo_sharding_closure_verdict.py` (NEW)

Closes Tier 2 #4 of the user's roadmap with the structural-then-empirical pattern proven on the ClusterIntelligence-CrossSession + MissionInferrer arcs.
