# Hierarchical Context Routing (Memory) Implementation Plan

**Goal:** Solve memory bloat via AST-bound context routing (not deletion). Repo-native `docs/memory_topics/` becomes the source of truth for architectural-decision memory; an O+V hook injects ONLY the topics relevant to the module under work; my Claude harness index becomes a lean pointer.

**Architecture:** Reuse existing Trinity infra — `crawl_memory`/`_fragment_from_file` (markdown load), `_embedder_factory`/`_cosine` (semantic rank), `oracle.compute_blast_radius`/`get_fused_neighborhood` (AST-bound related-modules signal), inject via `ctx.with_strategic_memory_context`. New `module_routing.py` lazily imports these (no reverse deps). Gated default-OFF.

## Global Constraints
- **Zero duplication:** no new markdown parser / embedder / search index. Reuse the recon'd utilities (lazy import).
- **AST-bound, not string-match:** routing signal = the op's target files → Oracle dependency/blast-radius → related modules → topic relevance (semantic + structural), NOT filename string-matching.
- **Repo-native dominance:** `docs/memory_topics/*.md` is the source of truth; the Claude harness index is a lean pointer (no full rationale duplicated locally if it lives in the repo).
- **Authority-free / advisory:** like StrategicDirection — injects prompt text only, fail-silent, no execution authority, gated default-OFF (`JARVIS_MEMORY_ROUTING_ENABLED`).
- Python 3.9+, `from __future__ import annotations`, async-first, no hardcoded models.

---

### Task MEM-1: `module_routing.py` — the AST-bound topic router (core, gated, TDD)
**Files:** Create `backend/core/ouroboros/governance/module_routing.py`; Test `tests/governance/test_module_routing.py`; a few fixture topics under `docs/memory_topics/_fixtures/` (or tmp) for tests.
**Produces:** `class ModuleContextRouter` with `route(target_files: list[str], query: str, *, max_topics: int = 3, token_budget: int = 2000) -> RoutedContext` (returns selected topic fragments + a rendered prompt section string). `routing_enabled() -> bool` (reads `JARVIS_MEMORY_ROUTING_ENABLED`, default false).
**Design:**
1. Load topic fragments from `docs/memory_topics/` via `roadmap.source_crawlers.crawl_memory`-style loading (reuse `_fragment_from_file`); each topic carries title/summary/mtime/uri + a `modules:`/`topics:` frontmatter tag if present.
2. AST-bound candidate set: given `target_files`, call `oracle.compute_blast_radius` / `get_fused_neighborhood` (lazy import, fail-soft → empty) to get related module names; map related modules → candidate topic files (via topic frontmatter `modules:` tags + path heuristics).
3. Semantic rank: embed the query + candidate topic summaries via `_embedder_factory`/`_cosine` (reuse), rank, take top `max_topics` within `token_budget`.
4. Render a `## Relevant Architecture Memory` prompt section (fail-silent, advisory).
**Tests:** topic loading from a fixture dir; AST signal maps target file → related topics (mock oracle); semantic rank orders by relevance (mock/real embedder fallback); token-budget cap; `routing_enabled()` default false → `route` returns empty; fail-soft when oracle/embedder unavailable.

### Task MEM-2: Wire MEM-1 into CONTEXT_EXPANSION (gated, TDD)
**Files:** Modify the CONTEXT_EXPANSION seam (`orchestrator.py` ~:3507-3527 / `phase_runners/context_expansion_runner.py`); Test extends existing context-expansion tests.
**Design:** after the existing `strategic_memory_prompt` assembly, if `routing_enabled()`, call `ModuleContextRouter().route(ctx.target_files, ctx.description)` and append its section via `ctx.with_strategic_memory_context(strategic_memory_prompt=existing + "\n\n" + routed.section)`. Fail-soft (router error → skip, never break the pipeline). Default-OFF byte-identical.
**Tests:** flag on → routed section appended to the context; flag off → byte-identical to today; router exception → pipeline unaffected.

### Task MEM-3: Migrate architectural memory into `docs/memory_topics/`
**Files:** Create `docs/memory_topics/<domain>/<topic>.md` (grouped: ouroboros/, swarm/, sovereign/, providers/, memory/, infra/); Create `docs/memory_topics/INDEX.md` (repo-side index).
**Design:** Migrate the ARCHITECTURAL topic files (the `project_*.md` rationale) from the Claude harness memory at `/Users/djrussell23/.claude/projects/-Users-djrussell23-Documents-repos-JARVIS-AI-Agent/memory/` into the repo, grouped by domain, each with a short frontmatter (`title`, `modules:` the key source files it concerns, `status`). SELECTIVE: architectural decisions + engineering rationale migrate; pure session-ops/working-style notes (`feedback_*.md` about how I work) stay in the harness. Preserve rationale verbatim where it's architectural. Build `INDEX.md` mapping topic → modules. (This is judgment work — a subagent reads the harness memory dir and proposes the grouping; controller reviews.)

### Task MEM-4: Refactor the Claude harness index → lean pointer (outside repo, not in PR)
**Files:** `/Users/djrussell23/.claude/projects/-Users-djrussell23-Documents-repos-JARVIS-AI-Agent/memory/MEMORY.md` → lean <15KB.
**Design:** each line becomes a terse pointer: `topic — one-line hook → docs/memory_topics/<domain>/<file>.md`. Full rationale lives in the repo topic file (repo-native dominance). Keep harness-only `feedback_*` working-style entries local. Target <15KB so the harness loads the whole index untruncated.
