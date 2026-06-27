---
title: Project V2 94 Venom V2 Slice 5 Flag Seed
modules: [tests/governance/test_permission_decision_archive_flags.py, backend/core/ouroboros/governance/permission_decision_archive.py, tests/governance/test_permission_decision_archive.py, tests/governance/test_tool_permissions_repl.py, tests/governance/test_permission_decision_sse.py, tests/governance/test_ide_observability_tool_permissions.py, tests/governance/test_flag_registry.py]
status: historical
source: project_v2_94_venom_v2_slice_5_flag_seed.md
---

May 10 2026: Slice 5 of the Venom V2 observability arc. Forward-additive on v2.89 (substrate) + v2.90 (REPL) + v2.91 (SSE) + v2.93 (IDE GET).

**Slice scope** — make the two `JARVIS_PERMISSION_ARCHIVE_*` env vars discoverable via the canonical FlagRegistry typed catalog. Slice 6 (PRD §37 Tier 2 #6 row flip) remains.

**Architectural composition**:

1. **§33.3 naming-cage applied to flags** — new module-level `register_flags(registry) -> int` function added to `permission_decision_archive.py`. The canonical `flag_registry_seed._discover_module_provided_flags` walker scans `backend.core.ouroboros.governance` (one of three provider packages) for direct submodules exposing a `register_flags` callable. The new function is picked up zero-edit by the walker — **no additions to `flag_registry_seed.SEED_SPECS` required**. This is the same discipline as Gap #6 Slice 6 REPL auto-discovery via `repl_dispatch_registry`, applied to flags.

2. **Two FlagSpecs** mirroring the canonical pattern from `tool_render_view.register_flags` (Gap #2 Slice 5, 2026-05-04):
   - `JARVIS_PERMISSION_ARCHIVE_ENABLED` — `FlagType.BOOL`, default-`False`, `Category.SAFETY`, source_file pointer + example + since-tag. Master kill switch. Operator-flippable via 3-clean-soak ladder.
   - `JARVIS_PERMISSION_ARCHIVE_SIZE` — `FlagType.INT`, default-`50`, `Category.CAPACITY`, source_file pointer. Bounds [1, 10000] (substrate-enforced, not registry-enforced). Documented in description.

3. **Single-source-of-truth discipline (AST-pinned)** — the function MUST use the canonical `MASTER_FLAG_ENV_VAR` + `ARCHIVE_SIZE_ENV_VAR` module-level constants when building the FlagSpec `name=...` kwargs. Drift to raw string literals would silently desync the env var name from the spec. Test pin asserts the literal `name=MASTER_FLAG_ENV_VAR` (not `name="JARVIS_PERMISSION_ARCHIVE_ENABLED"`) appears in the function source.

4. **Fail-open contract (AST-pinned)** — each `registry.register(spec)` call MUST be wrapped in try/except per the canonical seed pattern. A single bad FlagSpec construction or registry error MUST NOT block the entire `ensure_seeded()` walk. The defensive wrapper mirrors `tool_render_view.register_flags` line-for-line.

5. **Bytes-pinned master default** — the literal `default=False` MUST appear in the master spec construction. Drift to `default=True` would silently graduate the surface without the §33.1 evidence-ladder discipline. Test pin asserts the byte sequence is present.

**10 regression tests** in `tests/governance/test_permission_decision_archive_flags.py`:
- Direct registration (5): installs 2 specs / master shape (BOOL+SAFETY+False) / size shape (INT+CAPACITY+50) / idempotent re-registration / fail-open on broken registry
- Auto-discovery (1): `ensure_seeded()` picks up both specs via canonical walker
- **4 AST pins**: function present (naming-cage hook) / uses canonical env-var constants (single-source-of-truth) / wraps each register() in try/except (fail-open contract) / master default=False bytes-pinned

**Broader regression**: 162 tests green across `test_permission_decision_archive.py` (Slice 1) + `test_tool_permissions_repl.py` (Slice 2) + `test_permission_decision_sse.py` (Slice 3) + `test_ide_observability_tool_permissions.py` (Slice 4) + `test_permission_decision_archive_flags.py` (Slice 5) + `test_flag_registry.py` (canonical). **Zero regression** on the canonical FlagRegistry seed walker.

**Files modified**:
- `backend/core/ouroboros/governance/permission_decision_archive.py` (+~120 LOC: module-level `register_flags(registry) -> int` function appended at module end; `__all__` updated to export it)
- `tests/governance/test_permission_decision_archive_flags.py` (NEW, 10 tests + 4 AST pins)

**Cumulative Venom V2 arc state** (5 slices shipped):
- Slice 1 (v2.89) — substrate ring + producer-bridge + tool_executor seam (31 tests)
- Slice 2 (v2.90) — REPL verb auto-discovered + /expand p-N 5th cross-substrate prefix (31 tests)
- Slice 3 (v2.91) — SSE event registration + producer-bridge to broker (11 tests)
- Slice 4 (v2.93) — IDE GET endpoints + route-order discipline (15 tests)
- Slice 5 (v2.94) — FlagRegistry seed + canonical naming-cage applied to flags (10 tests)
- Total: 98 new regression tests + 23 AST pins across the 5 slices

**Operator binding 2026-05-10 satisfied verbatim**:
- Solved root problem directly — flags need typed catalog membership; substrate-owned register_flags is the canonical seam
- No workarounds — used canonical `tool_render_view.register_flags` pattern + canonical Gap #6 §33.3 naming-cage discipline applied to flags
- No shortcuts — 10 tests + 4 AST pins; default=False byte-pinned
- Composes existing canonical paths: `_discover_module_provided_flags` walker (zero edits to seed file), `FlagSpec` dataclass (no parallel shape), `Category.SAFETY` / `Category.CAPACITY` (canonical 8-slot taxonomy), `FlagType.BOOL` / `FlagType.INT` (canonical 5-slot taxonomy), `MASTER_FLAG_ENV_VAR` / `ARCHIVE_SIZE_ENV_VAR` (canonical substrate constants — single-source-of-truth)
- No hardcoding — env-var names referenced via canonical module constants, not string literals
- No duplication — register_flags is the §33.3 hook; zero parallel flag declaration; zero edits to flag_registry_seed.SEED_SPECS

**Operator-visibility wins (Slice 5)**:
- `/help flags` REPL verb now lists both Venom V2 flags with description + default + category
- `GET /observability/flags` IDE endpoint includes both specs in its bounded projection
- Levenshtein typo detection: operator typing `JARVIS_PERMISSION_ARCIVE_ENABLED` (typo) gets a suggestion to use `JARVIS_PERMISSION_ARCHIVE_ENABLED` (canonical)
- Posture-relevance filter (none assigned — Venom V2 flags are posture-agnostic for now; can be added in a follow-on if HARDEN posture needs critical relevance)

**What's left for Slice 6**: PRD §37 Tier 2 #6 row flip from 🟡 → ✅ + §3.6.3 row update. Pure documentation — no code changes. ~30 min.

**Why this matters for RSI**: the FlagRegistry is the canonical typed catalog that powers `/help flags`, `GET /observability/flags`, and Levenshtein typo detection (the `flag_typo_detected` SSE event). Operators don't have to memorize env var names — the registry teaches them. As Venom V2 callback registrations land in production and the master flag graduates, the registry becomes the operator's interface to the surface. Without Slice 5, the flags exist but are invisible to discovery tooling.

**Hot reload during soak #3**: this slice landed while soak #3 (`bt-2026-05-10-221432`) was actively running under the post-Layer-7 (v2.92) dual-clock watchdog — the soak isn't affected because Slice 5 is a load-time registration that takes effect on the NEXT boot. Phase 9 ladder progress: 2 of 3 clean rows for `JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED` so far; soak #3 still in-flight at this writing.
