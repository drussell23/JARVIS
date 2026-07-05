"""Domain-1 Staging-0 -- AST structural-delta engine (CAPTURE half).

Turns a file's before/after revision into a bounded, content-free STRUCTURAL
DELTA: symbols added / removed / re-signatured plus the import-edge set diff.

Mandate 1 (no-content, grep-enforced): NO field carries source text or a
textual diff. Only symbol_ids (``repo:file:name``), 16-hex signature hashes,
edge tuples of identifiers, and integer counts ever cross the boundary.

Mandate 3 (DRY): AST parsing is reused wholesale from the Oracle's
``CodeStructureVisitor`` -- we never re-implement Python parsing here.
"""
from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from backend.core.ouroboros.oracle import (
    CodeStructureVisitor,
    EdgeType,
    NodeType,
)

# Symbol kinds that participate in the delta. FILE/MODULE/VARIABLE/IMPORT/... are
# NOT symbols for our purposes.
SIGNATURE_KINDS = ("class", "function", "method")

_KIND_BY_NODETYPE = {
    NodeType.CLASS: "class",
    NodeType.FUNCTION: "function",
    NodeType.METHOD: "method",
}

_EDGE_KIND_BY_TYPE = {
    EdgeType.IMPORTS: "imports",
    EdgeType.IMPORTS_FROM: "imports_from",
}

DEFAULT_MAX_SYMBOLS = 64
_MAX_SYMBOLS_ENV = "JARVIS_CAUSAL_DELTA_MAX_SYMBOLS"


def _resolve_max_symbols() -> int:
    """Env-resolved change bound. Non-positive / unparseable -> default."""
    raw = os.environ.get(_MAX_SYMBOLS_ENV)
    if raw is None:
        return DEFAULT_MAX_SYMBOLS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SYMBOLS
    return value if value > 0 else DEFAULT_MAX_SYMBOLS


def _signature_hash(
    signature: Optional[str],
    decorators: Optional[List[str]],
    base_classes: Optional[List[str]],
) -> str:
    """Deterministic 16-hex digest. Hash ONLY -- the signature text never
    leaves this function."""
    payload = (
        (signature or "")
        + "|"
        + ",".join(sorted(decorators or []))
        + "|"
        + ",".join(sorted(base_classes or []))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# =============================================================================
# MODEL
# =============================================================================

@dataclass(frozen=True)
class SymbolRecord:
    symbol_id: str        # str(NodeID) -- "repo:file:name"
    kind: str             # "class" | "function" | "method"
    signature_hash: str   # 16-hex sha256(signature + sorted(decorators) + sorted(base_classes))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "kind": self.kind,
            "signature_hash": self.signature_hash,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SymbolRecord":
        return cls(
            symbol_id=d["symbol_id"],
            kind=d["kind"],
            signature_hash=d["signature_hash"],
        )


@dataclass(frozen=True)
class ImportEdge:
    src_id: str           # str(NodeID) importer (the file node)
    dst_name: str         # imported dotted name (NOT resolved to a foreign node here)
    edge_kind: str        # "imports" | "imports_from"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "src_id": self.src_id,
            "dst_name": self.dst_name,
            "edge_kind": self.edge_kind,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ImportEdge":
        return cls(
            src_id=d["src_id"],
            dst_name=d["dst_name"],
            edge_kind=d["edge_kind"],
        )


@dataclass
class SymbolSet:
    repo: str
    file_path: str
    symbols: Dict[str, SymbolRecord]        # keyed by symbol_id
    import_edges: FrozenSet[ImportEdge]
    parse_ok: bool                          # False -> unparseable revision


@dataclass
class StructuralDelta:
    repo: str
    file_path: str
    symbols_added: Tuple[SymbolRecord, ...]
    symbols_removed: Tuple[SymbolRecord, ...]
    symbols_resignatured: Tuple[Tuple[str, str, str], ...]  # (symbol_id, old_hash, new_hash)
    import_edges_added: Tuple[ImportEdge, ...]
    import_edges_removed: Tuple[ImportEdge, ...]
    file_level_churn: bool                  # True when bound exceeded OR a revision unparseable
    churn_counts: Dict[str, int]            # {"added","removed","resig","imp_added","imp_removed"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.repo,
            "file_path": self.file_path,
            "symbols_added": [r.to_dict() for r in self.symbols_added],
            "symbols_removed": [r.to_dict() for r in self.symbols_removed],
            "symbols_resignatured": [list(t) for t in self.symbols_resignatured],
            "import_edges_added": [e.to_dict() for e in self.import_edges_added],
            "import_edges_removed": [e.to_dict() for e in self.import_edges_removed],
            "file_level_churn": self.file_level_churn,
            "churn_counts": dict(self.churn_counts),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StructuralDelta":
        return cls(
            repo=d["repo"],
            file_path=d["file_path"],
            symbols_added=tuple(SymbolRecord.from_dict(x) for x in d.get("symbols_added", [])),
            symbols_removed=tuple(SymbolRecord.from_dict(x) for x in d.get("symbols_removed", [])),
            symbols_resignatured=tuple(
                (t[0], t[1], t[2]) for t in d.get("symbols_resignatured", [])
            ),
            import_edges_added=tuple(ImportEdge.from_dict(x) for x in d.get("import_edges_added", [])),
            import_edges_removed=tuple(ImportEdge.from_dict(x) for x in d.get("import_edges_removed", [])),
            file_level_churn=bool(d["file_level_churn"]),
            churn_counts=dict(d.get("churn_counts", {})),
        )


# =============================================================================
# EXTRACTION
# =============================================================================

def extract_symbol_set(repo: str, file_path: str, source: str) -> SymbolSet:
    """Reuse ``CodeStructureVisitor``. Fail-soft: SyntaxError or ANY parse
    failure -> ``SymbolSet(parse_ok=False, empty)``. Never raises."""
    try:
        tree = ast.parse(source)
        visitor = CodeStructureVisitor(repo, file_path, source)
        visitor.visit(tree)
    except Exception:  # noqa: BLE001 -- fail-soft by contract
        return SymbolSet(
            repo=repo,
            file_path=file_path,
            symbols={},
            import_edges=frozenset(),
            parse_ok=False,
        )

    symbols: Dict[str, SymbolRecord] = {}
    for node_data in visitor.nodes:
        kind = _KIND_BY_NODETYPE.get(node_data.node_id.node_type)
        if kind is None:
            continue
        symbol_id = str(node_data.node_id)
        symbols[symbol_id] = SymbolRecord(
            symbol_id=symbol_id,
            kind=kind,
            signature_hash=_signature_hash(
                node_data.signature,
                node_data.decorators,
                node_data.base_classes,
            ),
        )

    import_edges = set()
    for src_id, dst_id, edge_data in visitor.edges:
        edge_kind = _EDGE_KIND_BY_TYPE.get(edge_data.edge_type)
        if edge_kind is None:
            continue
        import_edges.add(
            ImportEdge(
                src_id=str(src_id),
                dst_name=dst_id.name,  # imported dotted name recorded by the visitor
                edge_kind=edge_kind,
            )
        )

    return SymbolSet(
        repo=repo,
        file_path=file_path,
        symbols=symbols,
        import_edges=frozenset(import_edges),
        parse_ok=True,
    )


# =============================================================================
# SET-DIFF
# =============================================================================

def diff_symbol_sets(before: SymbolSet, after: SymbolSet) -> StructuralDelta:
    """Set-diff two symbol sets. Same symbol_id on both sides with differing
    signature_hash -> resignatured. Collapses to ``file_level_churn`` when
    either side is unparseable OR the total change count exceeds the bound;
    in that case the per-symbol/per-edge tuples are emptied but the real
    ``churn_counts`` survive."""
    repo = after.repo or before.repo
    file_path = after.file_path or before.file_path

    before_syms = before.symbols
    after_syms = after.symbols

    added = tuple(
        after_syms[k] for k in sorted(after_syms) if k not in before_syms
    )
    removed = tuple(
        before_syms[k] for k in sorted(before_syms) if k not in after_syms
    )
    resignatured = tuple(
        (k, before_syms[k].signature_hash, after_syms[k].signature_hash)
        for k in sorted(before_syms)
        if k in after_syms
        and before_syms[k].signature_hash != after_syms[k].signature_hash
    )

    imp_added = tuple(
        sorted(
            after.import_edges - before.import_edges,
            key=lambda e: (e.src_id, e.dst_name, e.edge_kind),
        )
    )
    imp_removed = tuple(
        sorted(
            before.import_edges - after.import_edges,
            key=lambda e: (e.src_id, e.dst_name, e.edge_kind),
        )
    )

    churn_counts = {
        "added": len(added),
        "removed": len(removed),
        "resig": len(resignatured),
        "imp_added": len(imp_added),
        "imp_removed": len(imp_removed),
    }
    total_change = sum(churn_counts.values())

    parse_failure = not before.parse_ok or not after.parse_ok
    over_bound = total_change > _resolve_max_symbols()

    if parse_failure or over_bound:
        return StructuralDelta(
            repo=repo,
            file_path=file_path,
            symbols_added=(),
            symbols_removed=(),
            symbols_resignatured=(),
            import_edges_added=(),
            import_edges_removed=(),
            file_level_churn=True,
            churn_counts=churn_counts,
        )

    return StructuralDelta(
        repo=repo,
        file_path=file_path,
        symbols_added=added,
        symbols_removed=removed,
        symbols_resignatured=resignatured,
        import_edges_added=imp_added,
        import_edges_removed=imp_removed,
        file_level_churn=False,
        churn_counts=churn_counts,
    )


def compute_file_delta(
    repo: str,
    file_path: str,
    before_source: str,
    after_source: str,
) -> StructuralDelta:
    """The one public entry: extract both revisions -> set-diff. Never raises."""
    before = extract_symbol_set(repo, file_path, before_source)
    after = extract_symbol_set(repo, file_path, after_source)
    return diff_symbol_sets(before, after)
