# Domain 1 — Staging 0: Body-local AST Structural Delta Engine

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A Body-local engine that turns a file's before/after revision into a bounded AST **structural delta** (symbols added/removed/re-signatured + import-edge set-diff), never shipping raw diffs or content — the capture half of Domain 1's sensory organ.

**Architecture:** Reuse the Oracle's `CodeStructureVisitor` (`oracle.py:507`) as the sole AST symbol+edge extractor; normalize its `.nodes`/`.edges` into a hashed `SymbolSet`; set-diff two `SymbolSet`s into a `StructuralDelta`; bound the delta by symbol count with overflow-collapse; stamp git/emit lineage. No wire, no graph, no relocation — Staging 0 is pure local computation + tests.

**Tech Stack:** stdlib `ast`, the Oracle `CodeStructureVisitor`/`NodeID`/`NodeData`/`EdgeData`, `cross_process_jsonl` flock for the emit counter.

## Global Constraints (Domain-1 mandates; Staging-0 subset binding here)

- **MANDATE 1 (no-content):** NO field of any Staging-0 type may carry source text or a textual diff. Only: symbol identifiers, signature **hashes**, edge tuples of identifiers, and integer counts. Grep/AST-enforced by a test.
- **MANDATE 3 (DRY):** signature + import-edge parsing is done ONLY by the Oracle `CodeStructureVisitor` — zero re-implemented AST symbol/edge parsing. Reuse `NodeID` for identity.
- Zero hardcoded relationship thresholds (N/A to Staging 0 — no weighting yet — but no magic numbers beyond the explicit env *bound* `JARVIS_CAUSAL_DELTA_MAX_SYMBOLS`).
- `from __future__ import annotations`; py3.9 (`asyncio.wait_for` if any async — Staging 0 is sync); ASCII; fail-soft (a broken/unparseable revision degrades to `file_level_churn`, never raises into the caller); env-resolved bounds; TDD RED→GREEN; commit per task, named files only.
- **Forward constraints (bind later stagings, recorded here so no task drifts):** the timeline reconciler (Staging 4) MUST compute divergence via `git merge-base` against bare mirrors — never commit-message greps; the weighting layer (Staging 3) MUST be numpy/scipy matrix ops with cosine + half-life decay, no static scalar weights or cut-off thresholds; the `BlastRadiusOracle` widest-path traversal (Staging 3) MUST carry explicit cycle detection (deque-worklist + visited, the `reverse_dep_resolver.py:243` pattern) so cross-repo dependency cycles cannot infinite-loop or inflate feedback weight.

## Key facts (scouted, verified)

- `CodeStructureVisitor(repo: str, file_path: str, source: str)` (`oracle.py:519`): construct, `visitor.visit(ast.parse(source))`, then read `visitor.nodes: List[NodeData]` and `visitor.edges: List[Tuple[NodeID, NodeID, EdgeData]]`. It builds a FILE node + CLASS/FUNCTION/METHOD nodes with `NodeData.signature`, `.decorators`, `.base_classes`, `.complexity`, `.source_hash`; and IMPORTS/IMPORTS_FROM/CALLS/INHERITS edges.
- `NodeID(repo, file_path, name, node_type, line_number)` frozen (`oracle.py:346`); `__str__` → `"{repo}:{file_path}:{name}"`. `NodeType` (`oracle.py:316`), `EdgeType` (`oracle.py:329`), `NodeData` (`oracle.py:377`), `EdgeData` carries `edge_type`.
- Import both from `backend.core.ouroboros.oracle`.
- `cross_process_jsonl` (`governance/cross_process_jsonl.py`): `flock_append_line` / `flock_critical_section` for the durable emit counter.

---

### Task 1: `SymbolSet` + `StructuralDelta` model + the set-diff engine

**Files:**
- Create: `backend/core/ouroboros/governance/causal/__init__.py` (empty package marker)
- Create: `backend/core/ouroboros/governance/causal/structural_delta.py`
- Test: `tests/governance/causal/test_structural_delta.py` (+ `tests/governance/causal/__init__.py`)

**Interfaces (Produces):**
```python
SIGNATURE_KINDS = ("class", "function", "method")  # from NodeType

@dataclass(frozen=True)
class SymbolRecord:
    symbol_id: str        # str(NodeID) -- "repo:file:name"
    kind: str             # "class"|"function"|"method"
    signature_hash: str   # sha256(signature + sorted(decorators) + sorted(base_classes))[:16]

@dataclass(frozen=True)
class ImportEdge:
    src_id: str           # str(NodeID) importer
    dst_name: str         # imported dotted name (NOT resolved to a foreign node here)
    edge_kind: str        # "imports"|"imports_from"

@dataclass
class SymbolSet:
    repo: str
    file_path: str
    symbols: Dict[str, SymbolRecord]   # keyed by symbol_id
    import_edges: FrozenSet[ImportEdge]
    parse_ok: bool                     # False -> unparseable revision

def extract_symbol_set(repo: str, file_path: str, source: str) -> SymbolSet:
    """Reuse CodeStructureVisitor. On SyntaxError/any parse failure -> SymbolSet(parse_ok=False, empty)."""

@dataclass
class StructuralDelta:
    repo: str
    file_path: str
    symbols_added: Tuple[SymbolRecord, ...]
    symbols_removed: Tuple[SymbolRecord, ...]
    symbols_resignatured: Tuple[Tuple[str, str, str], ...]  # (symbol_id, old_sig_hash, new_sig_hash)
    import_edges_added: Tuple[ImportEdge, ...]
    import_edges_removed: Tuple[ImportEdge, ...]
    file_level_churn: bool          # True when the delta exceeded the bound OR either revision unparseable
    churn_counts: Dict[str, int]    # {"added":N,"removed":N,"resig":N,"imp_added":N,"imp_removed":N}
    def to_dict(self) -> Dict[str, Any]: ...
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StructuralDelta": ...

def diff_symbol_sets(before: SymbolSet, after: SymbolSet) -> StructuralDelta:
    """Set-diff. Same symbol_id present both sides with differing signature_hash -> resignatured.
    If either side parse_ok=False, or total change count > JARVIS_CAUSAL_DELTA_MAX_SYMBOLS
    (env, default 64) -> collapse to file_level_churn=True with churn_counts populated but the
    per-symbol tuples emptied (bound honored)."""

def compute_file_delta(repo: str, file_path: str, before_source: str, after_source: str) -> StructuralDelta:
    """extract both -> diff. The one public entry."""
```

- [ ] **Step 1: Write the failing tests** — TDD spine (real Oracle visitor, no mocks):
  - a two-symbol file → add one function → `symbols_added` has exactly it, `signature_hash` stable across identical source, others empty.
  - change a function's params (name stable) → `symbols_resignatured` has `(id, old, new)` with differing hashes; NOT in added/removed.
  - remove a class → `symbols_removed`; its methods also removed.
  - add `from x import y` → `import_edges_added` has `ImportEdge(kind="imports_from", dst_name="x.y" or "x")` (match the visitor's real edge shape — read it); remove → `import_edges_removed`.
  - overflow: generate a file with > 64 symbols changed → `file_level_churn=True`, per-symbol tuples empty, `churn_counts` correct.
  - unparseable `after` (SyntaxError) → `file_level_churn=True`, `parse_ok` path, never raises.
  - **NO-CONTENT invariant:** `json.dumps(delta.to_dict())` contains no substring of the source bodies (assert a known unique source token is absent); every signature field is a 16-hex hash.
  - `to_dict`/`from_dict` round-trip equality.
- [ ] **Step 2: RED** — `python3 -m pytest tests/governance/causal/test_structural_delta.py -x -q`.
- [ ] **Step 3: Implement** `structural_delta.py` per the Produces block. `extract_symbol_set` constructs `CodeStructureVisitor`, visits `ast.parse(source)`, maps `.nodes` (filter to class/function/method `NodeType`) → `SymbolRecord` (signature_hash from `NodeData.signature`+decorators+base_classes), `.edges` (filter IMPORTS/IMPORTS_FROM) → `ImportEdge`. Fail-soft parse. `diff_symbol_sets` does the set math + bound.
- [ ] **Step 4: GREEN** — the file suite + a broad `tests/governance/` import-smoke (the new package imports clean).
- [ ] **Step 5: Commit** — `feat(domain1): AST structural-delta engine -- CodeStructureVisitor set-diff, bounded, no-content (Staging 0 Task 1)`.

---

### Task 2: Lineage stamping — `DeltaLineage` + durable `EmitSequence`

**Files:**
- Modify: `backend/core/ouroboros/governance/causal/structural_delta.py` (add lineage types + attach)
- Test: `tests/governance/causal/test_delta_lineage.py`

**Interfaces (Produces):**
```python
@dataclass(frozen=True)
class DeltaLineage:
    repo: str
    head_sha: str
    parent_sha: str
    merge_base: str        # stamped by the caller (Staging-1 sensor reads git); Staging 0 accepts it
    emit_seq: int          # monotonic per-source Lamport counter

class EmitSequence:
    """Durable monotonic counter at JARVIS_CAUSAL_EMIT_SEQ_PATH
    (default <repo>/.jarvis/causal_emit_seq), one line per source repo.
    next(repo) -> int reads-increments-persists under flock_critical_section
    (cross_process_jsonl) so two Body processes never mint the same seq."""
    def __init__(self, path: Optional[str] = None) -> None: ...
    def next(self, repo: str) -> int: ...   # strictly increasing per repo, survives restart

def stamp_delta(delta: StructuralDelta, lineage: DeltaLineage) -> Dict[str, Any]:
    """The publish-ready envelope: {"delta": delta.to_dict(), "lineage": asdict(lineage)}.
    This is exactly what Staging 1 will publish_raw(topic=causal.delta.<repo>). Still NO content."""
```

- [ ] **Step 1: Write the failing tests** — `EmitSequence.next("jarvis")` strictly increases; a NEW instance on the same path continues (persistence, not memory); two repos have independent sequences; concurrent-ish takes (threaded) never duplicate (flock proof, recording-monkeypatch of `flock_critical_section` asserts the take is inside the section). `stamp_delta` envelope round-trips and carries no content (same no-content assertion).
- [ ] **Step 2: RED.** **Step 3: Implement** (reuse `cross_process_jsonl.flock_critical_section` exactly as `brain_keeper.PersistentTokenBucket` does — read that precedent). **Step 4: GREEN.** **Step 5: Commit** — `feat(domain1): durable emit-sequence + delta lineage stamping (Staging 0 Task 2)`.

---

## Self-Review

1. **Spec coverage (Staging 0 = spec Staging 0):** AST set-diff via CodeStructureVisitor (T1), overflow→file_level_churn (T1), SHA/emit_seq lineage stamping (T2), no-content invariant grep-enforced (T1+T2). Wire/graph/weighting/reconciler are later stagings — correctly out.
2. **Placeholder scan:** exact types/envs/signatures; the visitor edge shape is "read it" (a grounded scouting instruction, the implementer binds to the real `EdgeData`).
3. **Type consistency:** `SymbolRecord.signature_hash` (16-hex) used identically in diff + no-content test; `str(NodeID)` is the `symbol_id` everywhere; `stamp_delta` consumes the T1 `StructuralDelta.to_dict()` verbatim.
