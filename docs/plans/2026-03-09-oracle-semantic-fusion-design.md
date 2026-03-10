# Oracle Structural + Semantic Fusion Design

**Goal:** Fuse ChromaDB semantic similarity with the existing depth-1 graph neighborhood so J-Prime receives a ranked, dual-section file manifest: structurally connected files from graph edges plus semantically similar code discovered via embedding search.

**Architecture:** Three new components inside `oracle.py` — `OracleSemanticIndex` (ChromaDB + SentenceTransformer), `FileNeighborhood.semantic_support` extension, and `TheOracle.get_fused_neighborhood()` async method. `ContextExpander.expand()` awaits the fused result and renders two labeled sections. Every component degrades independently; neither can block pipeline startup.

**Tech Stack:** ChromaDB `PersistentClient` (v1.0+ API, already in project), `SentenceTransformerProvider` (`all-MiniLM-L6-v2`, 384-dim, reused from `semantic_memory.py`), NetworkX DiGraph (existing oracle graph), Python 3.11 asyncio.

---

## Component 1: OracleSemanticIndex

New class in `oracle.py`, owned by `TheOracle`. Encapsulates the ChromaDB collection and embedding pipeline for code symbols.

**Collection:** `"jarvis_oracle_symbols"` in `~/.jarvis/oracle/chroma/`
**HNSW:** cosine space, `M=16`, `construction_ef=200` (matches project convention)
**Embedding text formula:** `f"{name} {signature or ''} {docstring or ''}"` (trimmed, truncated to 512 chars)
**Indexed node types:** `CLASS`, `FUNCTION`, `METHOD` only — file/import/variable nodes carry no useful semantic content

**Interface:**
```python
async def embed_nodes(nodes: List[NodeData]) -> None   # batch upsert during indexing
async def semantic_search(query: str, k: int) -> List[Tuple[str, float]]  # (repo:file_path, similarity_score)
def is_ready() -> bool                                  # safe to call before search
```

**Fault isolation:** `__init__` never raises — ChromaDB/SentenceTransformer import failures set an internal `_available = False` flag. All public methods return empty results silently when `_available` is False.

---

## Component 2: FileNeighborhood Extension

Add one field to the existing `FileNeighborhood` dataclass:

```python
semantic_support: List[str] = field(default_factory=list)
# Paths discovered via semantic seeding (repo:file_path format, same as structural fields)
```

`to_dict()` includes `semantic_support` if non-empty (already handled by the `if v` guard).
`all_unique_files()` includes `semantic_support` in its union.

---

## Component 3: TheOracle.get_fused_neighborhood()

```python
async def get_fused_neighborhood(
    self,
    file_paths: List[Path],
    query: str,
    k_semantic: int = 5,
) -> FileNeighborhood
```

**Algorithm:**

```
Step 1 — Structural expansion (sync)
  structural_nh = get_file_neighborhood(file_paths)
  structural_set = set(structural_nh.all_unique_files())

Step 2 — Semantic seeds (async)
  raw_seeds = await _semantic_index.semantic_search(query, k=k_semantic)
  # raw_seeds: List[Tuple["repo:file_path", float]] — cosine similarity scores

Step 3 — Seed graph expansion (sync)
  seed_paths = [_resolve_repo_path(s) for s, _ in raw_seeds]
  seed_nh = get_file_neighborhood(seed_paths)

Step 4 — Score all candidates
  For each file in structural_nh.all_unique_files():
    graph_proximity = 1.0    # directly connected to target
    semantic_score  = max(score for seed if seed file == this file, else 0.0)
    recency         = 1.0    # not tracked yet
    final = 0.55 * graph_proximity + 0.35 * semantic_score + 0.10 * recency

  For each file in seed_nh.all_unique_files() NOT in structural_set:
    graph_proximity = 0.5    # connected to a semantic seed, not target
    semantic_score  = score from raw_seeds lookup (0.0 if not a direct seed)
    recency         = 1.0
    final = 0.55 * graph_proximity + 0.35 * semantic_score + 0.10 * recency

  For each direct semantic seed NOT already in structural or seed-derived:
    graph_proximity = 0.5
    semantic_score  = score from raw_seeds
    recency         = 1.0
    final = 0.55 * graph_proximity + 0.35 * semantic_score + 0.10 * recency

Step 5 — Partition
  structural_candidates = files in structural_set, sorted by score desc
  semantic_candidates   = files NOT in structural_set (seed-origin), sorted by score desc

Step 6 — Rebuild FileNeighborhood
  Structural categories (imports, importers, callers, etc.) rebuild from structural_nh,
  each sorted by score descending within category.
  semantic_support = semantic_candidates (flat list, sorted by score desc).
  Token Guardrail applied at rendering layer (MAX_FILES_PER_CATEGORY = 10 per category,
  plus 10 for semantic_support).
```

**Degradation (enforced in `get_fused_neighborhood`):**
- Semantic search throws → log warning, return `structural_nh` unchanged
- Graph fails → return `FileNeighborhood` with only `semantic_support` populated
- Both fail → return empty `FileNeighborhood`

---

## Component 4: ContextExpander Changes

**`expand()` [async]:**
- If oracle available and running: `neighborhood = await oracle.get_fused_neighborhood(target_abs, ctx.description)`
- Falls back to `get_file_neighborhood()` (sync) if `get_fused_neighborhood` not available

**`_render_neighborhood_section(neighborhood)`:**
Renders two distinct labeled sections:

```
Structural file neighborhood (real codebase graph edges):
  Imports:
    - jarvis:backend/core/base.py
    - jarvis:backend/core/utils.py
    ... (and 3 more)
  Callers:
    - jarvis:backend/core/main.py

Semantic support (cross-repo similar code):
    - prime:services/auth_service.py
    - reactor:interfaces/base_handler.py
    ... (and 2 more)
```

**Token Guardrail:** `MAX_FILES_PER_CATEGORY = 10` applied to every structural category AND to `semantic_support`. Always appends `"... (and N more)"` when truncated.

---

## Embedding Lifecycle

| Event | Action |
|---|---|
| `full_index()` completes | `await _semantic_index.embed_nodes(all_nodes_with_content)` |
| `incremental_update(changed_files)` | `await _semantic_index.embed_nodes(nodes_for_changed_files)` |
| Node has no docstring AND no signature | Skip embedding (nothing useful to embed) |
| ChromaDB unavailable at startup | `_semantic_index._available = False`; graph-only mode |

---

## Testing Plan

**New test file:** `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py`

- `TestOracleSemanticIndex` — embed/search round-trip with real SentenceTransformer (or mock)
- `TestGetFusedNeighborhood` — fusion algorithm with mocked semantic_index and graph
- `TestFusedNeighborhoodScoring` — verify score formula and partition logic
- `TestDegradation` — semantic failure → graph-only, graph failure → semantic-only
- `TestContextExpanderFusedRendering` — two-section prompt output, truncation at 10

**Existing tests:** all 681 must continue to pass. `get_file_neighborhood` is unchanged.
