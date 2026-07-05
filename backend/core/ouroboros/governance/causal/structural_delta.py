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
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

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


# =============================================================================
# LINEAGE (Task 2) -- durable per-source emit sequence + publish envelope.
# =============================================================================

_EMIT_SEQ_PATH_ENV = "JARVIS_CAUSAL_EMIT_SEQ_PATH"


def _default_emit_seq_path() -> Path:
    """<repo_root>/.jarvis/causal_emit_seq. repo_root is derived from this
    file's location: structural_delta.py -> causal -> governance -> ouroboros
    -> core -> backend -> <repo_root> (parents[5])."""
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / ".jarvis" / "causal_emit_seq"


@dataclass(frozen=True)
class DeltaLineage:
    """Publish-ready provenance for one structural delta. NO source content --
    only SHAs, a repo id, and the monotonic emit sequence.

    ``merge_base`` is stamped by the caller (a Staging-1 sensor reads git);
    Staging 0 accepts whatever it is handed.
    """

    repo: str
    head_sha: str
    parent_sha: str
    merge_base: str
    emit_seq: int


class EmitSequence:
    """Durable, cross-process monotonic counter -- one strictly increasing
    Lamport sequence PER source repo.

    The journal IS the state: every ``next(repo)`` appends one JSONL record
    ``{"repo": R, "seq": N}``; ``next`` replays the journal, takes the max
    ``seq`` seen for ``repo`` (0 if none), increments, and appends the new
    record. Deterministic across process restarts (a fresh instance on the
    same path resumes from the persisted max, not from memory), and repos are
    independent (each maxes over its own records only).

    Atomicity (the ``PersistentTokenBucket`` precedent): the ENTIRE
    read-max-increment-append runs inside ONE ``flock_critical_section``
    acquisition (the canonical ``cross_process_jsonl`` read-modify-write
    helper -- never hand-rolled locking). An unlocked read-max followed by a
    locked append is a cross-process TOCTOU: two racing Body processes could
    both read the same stale max and both mint N+1. The append inside the
    section is caller-owned direct I/O per that helper's contract -- the lock
    is NON-reentrant, so nesting a locking helper inside would self-deadlock.

    Fail-CLOSED (the ONE exception to this module's fail-soft posture): a
    ``next`` that cannot acquire the lock -- or cannot even import the locking
    substrate -- RAISES. A monotonic-uniqueness guarantee that cannot lock
    must never silently hand out a possibly-duplicate seq (mirrors the
    bucket's billing-guard posture: a guard that cannot prove its invariant
    must never proceed).
    """

    def __init__(self, path: Optional[str] = None) -> None:
        if path is not None:
            resolved = Path(path)
        else:
            raw = (os.environ.get(_EMIT_SEQ_PATH_ENV, "") or "").strip()
            resolved = Path(raw) if raw else _default_emit_seq_path()
        self._path = resolved
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # fail-soft: flock_critical_section re-mkdirs on the take path

    @property
    def path(self) -> Path:
        return self._path

    def _durable_path(self) -> Path:
        """The overlay-rerooted journal path (identity on plain hosts),
        resolved at CALL time so the replay read and the locked append always
        target the SAME file ``flock_critical_section`` locks. Fail-soft to
        the raw path."""
        try:
            from backend.core.ouroboros.governance.workspace_resolver import (  # noqa: PLC0415
                resolve_durable_path,
            )

            return Path(resolve_durable_path(self._path))
        except Exception:  # noqa: BLE001
            return self._path

    def _max_seq_for(self, repo: str) -> int:
        """Tolerant journal replay (WAL semantics: corrupt lines skipped).
        Returns the max ``seq`` recorded for ``repo``, or 0 if none."""
        journal = self._durable_path()
        if not journal.exists():
            return 0
        best = 0
        try:
            with journal.open("r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "[EmitSequence] corrupt line %d in %s -- skipping",
                            line_no, self._path,
                        )
                        continue
                    if not isinstance(rec, dict) or rec.get("repo") != repo:
                        continue
                    try:
                        seq = int(rec.get("seq"))
                    except (TypeError, ValueError):
                        continue
                    if seq > best:
                        best = seq
        except OSError as exc:
            logger.warning(
                "[EmitSequence] read failed (%r) -- treating as empty", exc)
            return 0
        return best

    def next(self, repo: str) -> int:
        """Mint the next strictly-increasing seq for ``repo``.

        The WHOLE read-max-increment-append runs under one
        ``flock_critical_section`` acquisition (no cross-process TOCTOU -- see
        the class docstring). Fail-CLOSED: if the locking substrate cannot be
        imported OR the lock cannot be acquired, RAISE -- a uniqueness counter
        must never guess. On success the new record is appended inside the
        held section (caller-owned direct I/O -- the lock is non-reentrant).
        """
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: PLC0415
                flock_critical_section,
            )
        except Exception as exc:  # noqa: BLE001 -- fail-CLOSED, loudly
            logger.error(
                "[EmitSequence] locking substrate unavailable (%r) -- "
                "REFUSING to mint (fail-closed uniqueness guard)", exc,
            )
            raise RuntimeError(
                "EmitSequence: locking substrate unavailable -- cannot mint a "
                "unique seq (fail-closed)"
            ) from exc

        with flock_critical_section(self._path) as locked:
            if not locked:
                logger.error(
                    "[EmitSequence] could not acquire the emit-seq lock for "
                    "%s -- REFUSING to mint (fail-closed: cannot prove "
                    "monotonicity)", self._path,
                )
                raise RuntimeError(
                    "EmitSequence: could not acquire lock for %s -- cannot "
                    "guarantee a unique seq (fail-closed)" % self._path
                )
            new_seq = self._max_seq_for(repo) + 1
            record = {"repo": repo, "seq": new_seq}
            try:
                line = json.dumps(record, default=str)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "EmitSequence: could not serialize record %r" % record
                ) from exc
            try:
                journal = self._durable_path()
                journal.parent.mkdir(parents=True, exist_ok=True)
                with journal.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                logger.error(
                    "[EmitSequence] append failed (%r) -- REFUSING (no record, "
                    "no grant)", exc,
                )
                raise RuntimeError(
                    "EmitSequence: append failed -- seq not persisted "
                    "(fail-closed)"
                ) from exc
        return new_seq


def stamp_delta(delta: StructuralDelta, lineage: DeltaLineage) -> Dict[str, Any]:
    """The publish-ready envelope Staging 1 will ``publish_raw`` on
    ``causal.delta.<repo>``: ``{"delta": delta.to_dict(), "lineage":
    asdict(lineage)}``. Still NO content -- consumes the Task 1
    content-free ``to_dict()`` verbatim."""
    return {"delta": delta.to_dict(), "lineage": asdict(lineage)}
