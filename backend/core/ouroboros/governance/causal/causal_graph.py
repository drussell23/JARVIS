"""Domain-1 Staging-2 Task 1 -- the in-memory cross-repo causal graph + fold.

THE GRAPH FOLD. One stamped structural-delta envelope (as Staging-1 delivers
it: ``{"delta": StructuralDelta.to_dict(), "lineage": {...}}``) is folded into a
native-adjacency causal graph in ``apply_delta`` -- O(1) in the delta's symbol
count, with NO traversal on write.

Two load-bearing invariants make Task 4's crash-recovery determinism proof
possible:

  * **Mandate 1 (O(1) fold):** every mutation is a dict upsert / pop against
    ``self._nodes`` plus the two adjacency indexes. ``file_level_churn`` touches
    only the ONE file's node set (``self._by_file``), never a global scan.

  * **Mandate 4 (emit_seq-MONOTONIC, order-independent):** a symbol's state is a
    last-writer-wins register keyed by the per-repo monotonic ``emit_seq``. A
    delta applies to a node ONLY IF the node is new OR ``emit_seq`` strictly
    exceeds the node's ``last_emit_seq``; a stale (lower/equal) delta is a
    no-op. Because ``emit_seq`` is unique-per-repo and a ``symbol_id`` encodes
    its repo, no two distinct deltas can tie on the same symbol -- so folding
    any permutation of a delta SET converges to one canonical state, witnessed
    by ``state_fingerprint()``.

NO intra-repo edges (Oracle owns those -- Task 2). NO WAL (Task 3). This module
is a pure in-memory data structure + fold + deterministic snapshot. Fail-soft: a
malformed envelope returns 0 and is logged, never raised.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from backend.core.ouroboros.governance.causal.structural_delta import (
    StructuralDelta,
)

logger = logging.getLogger(__name__)

_SNAPSHOT_VERSION = 1


@dataclass
class CausalNode:
    """One symbol in the cross-repo causal graph.

    ``imports`` are the declared-import FACTS (dotted names) folded from the
    delta's import edges whose ``src_id`` is this node -- stored, not resolved
    (Staging-3 resolves them cross-repo). ``last_emit_seq`` / ``last_head_sha``
    are the lineage of the newest delta that wrote this node -- the monotonic
    guard's comparison key.
    """

    symbol_id: str
    repo: str
    file_path: str
    kind: str
    signature_hash: str
    imports: FrozenSet[str] = field(default_factory=frozenset)
    last_emit_seq: int = 0
    last_head_sha: str = ""


class CausalGraph:
    """Native-adjacency in-memory causal graph with an O(1) monotonic fold."""

    def __init__(self) -> None:
        self._nodes: Dict[str, CausalNode] = {}
        self._by_repo: Dict[str, Set[str]] = {}
        self._by_file: Dict[Tuple[str, str], Set[str]] = {}

    # ------------------------------------------------------------------ #
    # read surface
    # ------------------------------------------------------------------ #
    def node(self, symbol_id: str) -> Optional[CausalNode]:
        return self._nodes.get(symbol_id)

    def nodes_in_repo(self, repo: str) -> List[CausalNode]:
        ids = self._by_repo.get(repo, set())
        return [self._nodes[s] for s in sorted(ids) if s in self._nodes]

    def node_count(self) -> int:
        return len(self._nodes)

    # ------------------------------------------------------------------ #
    # index-preserving mutation primitives (all O(1))
    # ------------------------------------------------------------------ #
    def _index_add(self, node: CausalNode) -> None:
        self._by_repo.setdefault(node.repo, set()).add(node.symbol_id)
        self._by_file.setdefault((node.repo, node.file_path), set()).add(node.symbol_id)

    def _index_remove(self, node: CausalNode) -> None:
        repo_set = self._by_repo.get(node.repo)
        if repo_set is not None:
            repo_set.discard(node.symbol_id)
            if not repo_set:
                del self._by_repo[node.repo]
        file_key = (node.repo, node.file_path)
        file_set = self._by_file.get(file_key)
        if file_set is not None:
            file_set.discard(node.symbol_id)
            if not file_set:
                del self._by_file[file_key]

    def _put(self, node: CausalNode) -> None:
        """Upsert a node, reconciling adjacency indexes if identity keys moved
        (defensive -- ``symbol_id`` normally pins repo/file)."""
        existing = self._nodes.get(node.symbol_id)
        if existing is not None and (
            existing.repo != node.repo or existing.file_path != node.file_path
        ):
            self._index_remove(existing)
        self._nodes[node.symbol_id] = node
        self._index_add(node)

    def _delete(self, symbol_id: str) -> bool:
        node = self._nodes.pop(symbol_id, None)
        if node is None:
            return False
        self._index_remove(node)
        return True

    # ------------------------------------------------------------------ #
    # the fold
    # ------------------------------------------------------------------ #
    def apply_delta(self, envelope: dict) -> int:
        """Fold one stamped delta envelope. Returns the count of nodes actually
        mutated. O(1) in the delta's symbol count; emit_seq-monotonic per
        symbol; NEVER raises (malformed -> 0, logged)."""
        try:
            if not isinstance(envelope, dict):
                raise TypeError("envelope is not a dict")
            delta = StructuralDelta.from_dict(envelope["delta"])
            lineage = envelope["lineage"]
            if not isinstance(lineage, dict):
                raise TypeError("lineage is not a dict")
            emit_seq = int(lineage["emit_seq"])
            head_sha = str(lineage.get("head_sha", ""))
        except Exception as exc:  # noqa: BLE001 -- fail-soft by contract
            logger.warning("[CausalGraph] malformed envelope (%r) -- ignored", exc)
            return 0

        repo = delta.repo
        file_path = delta.file_path

        # Pre-delta emit_seq per node captured lazily on first touch, so every
        # operation in THIS delta is authorized against the node's state BEFORE
        # the delta started -- keeps a multi-touch delta internally consistent
        # and the whole fold order-independent.
        pre_seq: Dict[str, Optional[int]] = {}

        def _prev(symbol_id: str) -> Optional[int]:
            if symbol_id not in pre_seq:
                node = self._nodes.get(symbol_id)
                pre_seq[symbol_id] = node.last_emit_seq if node is not None else None
            return pre_seq[symbol_id]

        def _wins(symbol_id: str) -> bool:
            prev = _prev(symbol_id)
            return prev is None or emit_seq > prev

        mutated: Set[str] = set()

        # --- symbols_added: full upsert (merge any existing imports) ---------
        for rec in delta.symbols_added:
            sid = rec.symbol_id
            if not _wins(sid):
                continue
            existing = self._nodes.get(sid)
            imports = existing.imports if existing is not None else frozenset()
            self._put(
                CausalNode(
                    symbol_id=sid,
                    repo=repo,
                    file_path=file_path,
                    kind=rec.kind,
                    signature_hash=rec.signature_hash,
                    imports=imports,
                    last_emit_seq=emit_seq,
                    last_head_sha=head_sha,
                )
            )
            mutated.add(sid)

        # --- symbols_resignatured: signature-only update on an existing node -
        # No kind travels with a resignature; if the node is unknown (an
        # out-of-order resignature that precedes its add) we skip -- the
        # highest-seq full add remains the authority, preserving determinism.
        for entry in delta.symbols_resignatured:
            sid, _old_h, new_h = entry
            existing = self._nodes.get(sid)
            if existing is None:
                continue
            if not _wins(sid):
                continue
            self._put(
                CausalNode(
                    symbol_id=sid,
                    repo=repo,
                    file_path=file_path,
                    kind=existing.kind,
                    signature_hash=new_h,
                    imports=existing.imports,
                    last_emit_seq=emit_seq,
                    last_head_sha=head_sha,
                )
            )
            mutated.add(sid)

        # --- symbols_removed: remove only if present AND newer ---------------
        for rec in delta.symbols_removed:
            sid = rec.symbol_id
            if self._nodes.get(sid) is None:
                continue
            if not _wins(sid):
                continue
            if self._delete(sid):
                mutated.add(sid)

        # --- import edges: fold declared-import facts onto the src node ------
        imp_by_src: Dict[str, Tuple[Set[str], Set[str]]] = {}
        for edge in delta.import_edges_added:
            add_names, _ = imp_by_src.setdefault(edge.src_id, (set(), set()))
            add_names.add(edge.dst_name)
        for edge in delta.import_edges_removed:
            _, rem_names = imp_by_src.setdefault(edge.src_id, (set(), set()))
            rem_names.add(edge.dst_name)

        for src_id, (add_names, rem_names) in imp_by_src.items():
            node = self._nodes.get(src_id)
            if node is None:
                continue  # no home node for these import facts yet
            if not _wins(src_id):
                continue
            new_imports = (set(node.imports) | add_names) - rem_names
            self._put(
                CausalNode(
                    symbol_id=node.symbol_id,
                    repo=node.repo,
                    file_path=node.file_path,
                    kind=node.kind,
                    signature_hash=node.signature_hash,
                    imports=frozenset(new_imports),
                    last_emit_seq=emit_seq,
                    last_head_sha=head_sha,
                )
            )
            mutated.add(src_id)

        # --- file_level_churn: coarse lineage bump, THIS file's nodes only ---
        if delta.file_level_churn:
            for sid in list(self._by_file.get((repo, file_path), ())):
                node = self._nodes.get(sid)
                if node is None:
                    continue
                if emit_seq <= node.last_emit_seq:
                    continue
                self._put(
                    CausalNode(
                        symbol_id=node.symbol_id,
                        repo=node.repo,
                        file_path=node.file_path,
                        kind=node.kind,
                        signature_hash=node.signature_hash,
                        imports=node.imports,
                        last_emit_seq=emit_seq,
                        last_head_sha=head_sha,
                    )
                )
                mutated.add(sid)

        return len(mutated)

    # ------------------------------------------------------------------ #
    # deterministic serialization -- the equality witness
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        """Fully deterministic canonical serialization: every node with every
        field, node list sorted by ``symbol_id``, imports as sorted lists."""
        nodes = []
        for sid in sorted(self._nodes):
            n = self._nodes[sid]
            nodes.append(
                {
                    "symbol_id": n.symbol_id,
                    "repo": n.repo,
                    "file_path": n.file_path,
                    "kind": n.kind,
                    "signature_hash": n.signature_hash,
                    "imports": sorted(n.imports),
                    "last_emit_seq": n.last_emit_seq,
                    "last_head_sha": n.last_head_sha,
                }
            )
        return {"version": _SNAPSHOT_VERSION, "nodes": nodes}

    @classmethod
    def from_snapshot(cls, snap: dict) -> "CausalGraph":
        graph = cls()
        for entry in (snap or {}).get("nodes", []):
            node = CausalNode(
                symbol_id=entry["symbol_id"],
                repo=entry["repo"],
                file_path=entry["file_path"],
                kind=entry["kind"],
                signature_hash=entry["signature_hash"],
                imports=frozenset(entry.get("imports", [])),
                last_emit_seq=int(entry.get("last_emit_seq", 0)),
                last_head_sha=str(entry.get("last_head_sha", "")),
            )
            graph._nodes[node.symbol_id] = node
            graph._index_add(node)
        return graph

    def state_fingerprint(self) -> str:
        """sha256 of the canonical snapshot -- the Mandate-4 equality primitive."""
        payload = json.dumps(self.snapshot(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
