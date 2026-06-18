"""Repair Context Bridge — Slice 3: the Sovereign Structural Validation Gate (the enforce).

The zero-regression shield. Slice 2 *steers* the model into the dependency cone; this gate *enforces*
that a candidate patch does not introduce a structural regression the failing test won't catch — a new
import/call cycle, a severed live execution path, or a broken intra-file interface contract — **before**
the candidate is flushed to the sandbox or signed by AutoCommitter.

Three deterministic, zero-LLM proofs run on an **isolated transactional delta** of the candidate:

1. **Acyclicity Guard** (hard) — a dependency cycle present in the post-delta cone but absent pre-delta.
2. **Path Reachability Matrix** (§3.1) — a modification that flips a downstream component from
   reachable-from-a-live-root (active test / entry script) to unreachable. Dead-only severance is
   authorized as valid structural pruning (ACCEPT + non-blocking telemetry), never a false reject.
3. **Boundary Invariant Verification** (soft) — a changed intra-file symbol signature whose un-modified
   in-cone callers would no longer type/arity-match.

**Blindspot Armor (isolation + thread-safety):** the candidate is parsed off-process via
``analyze_python_source_for_oracle`` (its own interpreter — never touches the live backend). The
what-if graph is a *local* ``_DeltaGraph`` built per-invocation from a read-only cone snapshot; it is
never written back to the ``SqliteLazyGraphBackend``. Concurrent repair iterations therefore cannot
pollute each other's simulation or contend on a shared mutable graph.

**Feedback (Phase 3):** a violation does not throw — it compiles a structured ``DivergenceSignature``
with the exact AST coordinates (the closed loop / the severed edge path / the symbol + old→new
signature), hashed via ``failure_classifier.patch_signature_hash``, and routes it back into the L2
iterate-feedback loop so the next generation is a mathematically targeted correction.

Gated ``JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED`` (default OFF → L2 byte-identical). Fail-soft: any
error → ACCEPT (the immune system is never *weakened* by the thing meant to strengthen it).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DivergenceSignature",
    "StructuralPrune",
    "StructuralVerdict",
    "ConeReader",
    "StructuralValidationGate",
    "gate_enabled",
]

_HARD = "hard"
_SOFT = "soft"


# --------------------------------------------------------------------------- flags
def gate_enabled() -> bool:
    """``JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED`` (default ON — graduated 2026-06-18) — master for the
    structural gate. Kill-switch: set the env to ``false`` to disable."""
    return os.environ.get("JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _severity(env_name: str, default: str) -> str:
    """Per-check severity knob (soft→hard graduation discipline). ``hard`` blocks + feeds back;
    ``soft`` records + feeds back as advisory but does not block on its own."""
    val = os.environ.get(env_name, "").strip().lower()
    return val if val in (_HARD, _SOFT) else default


def _soft_blocks() -> bool:
    """``JARVIS_REPAIR_STRUCTURAL_SOFT_BLOCKS`` — when on, soft divergences also block (post-soak)."""
    return os.environ.get("JARVIS_REPAIR_STRUCTURAL_SOFT_BLOCKS", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


# --------------------------------------------------------------------------- payloads
@dataclass
class DivergenceSignature:
    """Structured, AST-coordinate-precise description of a structural violation (Phase 3)."""

    kind: str                                  # new_cycle | severed_reachability | boundary_signature_mismatch
    severity: str                              # hard | soft
    detail: str
    coordinates: Dict[str, Any] = field(default_factory=dict)
    signature_hash: str = ""

    def to_feedback(self) -> str:
        """Render targeted correction guidance for the next L2 generation step."""
        head = f"[STRUCTURAL VIOLATION: {self.kind} ({self.severity})] {self.detail}"
        coord = self.coordinates or {}
        if self.kind == "new_cycle":
            loop = " → ".join(coord.get("cycle", []))
            return (
                f"{head}\nYour patch closes a dependency cycle: {loop}.\n"
                "Break this loop — remove or invert one edge in the cycle so the graph stays acyclic."
            )
        if self.kind == "severed_reachability":
            path = " → ".join(coord.get("broken_path", []))
            lost = ", ".join(coord.get("unreachable", []))
            return (
                f"{head}\nYour patch severs the only live execution path to: {lost}.\n"
                f"Broken route (was reachable from {coord.get('root', 'a live root')}): {path}.\n"
                "Preserve a call/import edge that keeps these components reachable, or move their "
                "logic so the active test path still reaches them."
            )
        if self.kind == "boundary_signature_mismatch":
            return (
                f"{head}\nSymbol `{coord.get('symbol', '?')}` changed signature "
                f"`{coord.get('old_signature', '?')}` → `{coord.get('new_signature', '?')}`, "
                f"but in-cone callers still use the old contract: "
                f"{', '.join(coord.get('callers', []))}.\n"
                "Either keep the signature backward-compatible or update every caller in the same patch."
            )
        return head


@dataclass
class StructuralPrune:
    """An authorized severance of dead/orphaned-only structure (telemetry, never blocks). §3.1."""

    severed_edge: Tuple[str, str]
    reason: str = "dead_only_subgraph"


@dataclass
class StructuralVerdict:
    accepted: bool
    divergences: Tuple[DivergenceSignature, ...] = ()
    prunes: Tuple[StructuralPrune, ...] = ()
    analyzed: bool = True                       # False → could not simulate (fail-soft ACCEPT)
    note: str = ""

    def blocking(self) -> Tuple[DivergenceSignature, ...]:
        soft_blocks = _soft_blocks()
        return tuple(d for d in self.divergences if d.severity == _HARD or soft_blocks)

    def feedback(self) -> str:
        """Concatenated targeted feedback for every divergence (Phase 3 routing payload)."""
        return "\n\n".join(d.to_feedback() for d in self.divergences)

    def telemetry(self) -> str:
        kinds = ",".join(sorted({d.kind for d in self.divergences})) or "none"
        return (
            f"[StructuralGate] accepted={self.accepted} analyzed={self.analyzed} "
            f"divergences={len(self.divergences)} kinds={kinds} prunes={len(self.prunes)}"
        )


# --------------------------------------------------------------------------- cone reader
class ConeReader(Protocol):
    """Read-only snapshot of the pre-fix cone structure. Production impl wraps the Oracle backend;
    tests inject a fake. NEVER mutates the backend."""

    def cone_edges(self) -> List[Tuple[str, str, str]]: ...   # (src_key, dst_key, edge_type)
    def node_signature(self, key: str) -> Optional[str]: ...
    def roots(self) -> List[str]: ...                          # entry-point reachability roots


# --------------------------------------------------------------------------- isolated delta graph
class _DeltaGraph:
    """An isolated, in-memory directed graph for what-if simulation. Local to a single gate call —
    no shared state, thread-safe by construction (the Blindspot Armor)."""

    def __init__(self) -> None:
        self._adj: Dict[str, Set[str]] = {}
        self._nodes: Set[str] = set()

    def add_edge(self, src: str, dst: str) -> None:
        self._nodes.add(src)
        self._nodes.add(dst)
        self._adj.setdefault(src, set()).add(dst)

    def successors(self, key: str) -> Set[str]:
        return self._adj.get(key, set())

    def reachable(self, roots: List[str]) -> Set[str]:
        """BFS reachability closure from the root set."""
        seen: Set[str] = set()
        frontier = [r for r in roots if r in self._nodes]
        while frontier:
            n = frontier.pop()
            if n in seen:
                continue
            seen.add(n)
            frontier.extend(self._adj.get(n, set()) - seen)
        return seen

    def cycles(self) -> List[List[str]]:
        """Return non-trivial cycles (DFS-based; the cone is small). Each cycle is a node list."""
        WHITE, GREY, BLACK = 0, 1, 2
        color: Dict[str, int] = {n: WHITE for n in self._nodes}
        stack: List[str] = []
        found: List[List[str]] = []
        seen_keys: Set[frozenset] = set()

        def _dfs(u: str) -> None:
            color[u] = GREY
            stack.append(u)
            for v in self._adj.get(u, set()):
                if color.get(v, WHITE) == GREY and v in stack:        # back-edge → cycle
                    cyc = stack[stack.index(v):]
                    key = frozenset(cyc)
                    if len(cyc) > 1 and key not in seen_keys:
                        seen_keys.add(key)
                        found.append(list(cyc))
                elif color.get(v, WHITE) == WHITE:
                    _dfs(v)
            stack.pop()
            color[u] = BLACK

        for node in list(self._nodes):
            if color.get(node, WHITE) == WHITE:
                _dfs(node)
        return found


# --------------------------------------------------------------------------- gate
class StructuralValidationGate:
    """Deterministic pre-flight structural delta validator. Composes the existing Oracle parser +
    graph primitives; the only new logic is the isolated delta + the three proofs + the structured
    feedback compiler.

    Parameters
    ----------
    analyzer:
        Async ``analyze_python_source_for_oracle``-shaped callable (injectable for tests). Default
        resolves the real off-process analyzer.
    """

    def __init__(self, analyzer: Optional[Any] = None) -> None:
        self._analyzer = analyzer

    # ------------------------------------------------------------------ analyze
    async def _analyze_candidate(
        self, candidate_source: str, file_path: str, repo_name: str,
    ) -> Optional[Tuple[List[Tuple[str, str, str]], Dict[str, Optional[str]]]]:
        """Off-process parse of the candidate → ``(edge list, {node_key: signature})``. None on any
        non-OK outcome (fail-soft → caller ACCEPTs). Never touches the live backend (its own
        interpreter via the process pool — the Blindspot Armor)."""
        analyzer = self._analyzer
        if analyzer is None:
            from backend.core.ouroboros.governance.ast_compile_helper import (
                analyze_python_source_for_oracle,
            )
            analyzer = analyze_python_source_for_oracle
        try:
            result = await analyzer(
                "structural_validation_gate.validate",
                candidate_source,
                filename=file_path or "<candidate>",
                repo_name=repo_name,
                relative_path=file_path,
            )
        except Exception as exc:  # noqa: BLE001 — analysis is best-effort
            logger.debug("[StructuralGate] candidate analyze failed: %s", exc)
            return None
        if getattr(result, "outcome", None) is None or str(getattr(result.outcome, "value", result.outcome)) != "ok":
            return None
        edges: List[Tuple[str, str, str]] = []
        for e in getattr(result, "edges", ()) or ():
            try:
                src, dst, data = e
                etype = getattr(getattr(data, "edge_type", None), "value", "") or ""
                edges.append((str(src), str(dst), str(etype)))
            except Exception:  # noqa: BLE001 — skip malformed edge tuples
                continue
        sigs: Dict[str, Optional[str]] = {}
        for nd in getattr(result, "nodes", ()) or ():
            try:
                nid = getattr(nd, "node_id", None)
                if nid is not None:
                    sigs[str(nid)] = getattr(nd, "signature", None)
            except Exception:  # noqa: BLE001
                continue
        return edges, sigs

    # ------------------------------------------------------------------ delta build
    @staticmethod
    def _build_delta(
        pre_edges: List[Tuple[str, str, str]],
        new_file_edges: List[Tuple[str, str, str]],
        file_path: str,
    ) -> Tuple["_DeltaGraph", "_DeltaGraph", List[Tuple[str, str]]]:
        """Construct the isolated pre-delta and post-delta graphs + the list of edges the patch
        removes. The patch replaces *all* edges originating in the changed file."""
        def _file_of(key: str) -> str:
            parts = key.split(":")
            return parts[1] if len(parts) >= 3 else key

        pre = _DeltaGraph()
        post = _DeltaGraph()
        removed: List[Tuple[str, str]] = []
        for src, dst, _t in pre_edges:
            pre.add_edge(src, dst)
            if _file_of(src) == file_path:        # edge owned by the changed file → dropped by patch
                removed.append((src, dst))
            else:
                post.add_edge(src, dst)
        for src, dst, _t in new_file_edges:       # candidate's replacement edges for the file
            post.add_edge(src, dst)
        return pre, post, removed

    # ------------------------------------------------------------------ proofs
    @staticmethod
    def _check_acyclicity(
        pre: "_DeltaGraph", post: "_DeltaGraph",
    ) -> List[DivergenceSignature]:
        pre_cycles = {frozenset(c) for c in pre.cycles()}
        out: List[DivergenceSignature] = []
        for cyc in post.cycles():
            if frozenset(cyc) not in pre_cycles:        # cycle absent pre-fix → introduced
                out.append(DivergenceSignature(
                    kind="new_cycle",
                    severity=_severity("JARVIS_REPAIR_STRUCT_CYCLE_SEVERITY", _HARD),
                    detail="patch introduces a dependency cycle absent before the fix",
                    coordinates={"cycle": list(cyc)},
                ))
        return out

    @staticmethod
    def _check_reachability(
        pre: "_DeltaGraph", post: "_DeltaGraph", roots: List[str],
        removed: List[Tuple[str, str]],
    ) -> Tuple[List[DivergenceSignature], List[StructuralPrune]]:
        """§3.1 Dynamic Reachability Matrix: REJECT iff a node flips reachable-from-root → unreachable.
        Severance of already-dead structure → authorized prune (telemetry, non-blocking)."""
        if not roots:
            return [], []
        reach_pre = pre.reachable(roots)
        reach_post = post.reachable(roots)
        severed = reach_pre - reach_post              # lost live reachability — the regression set
        divergences: List[DivergenceSignature] = []
        prunes: List[StructuralPrune] = []
        if severed:
            divergences.append(DivergenceSignature(
                kind="severed_reachability",
                severity=_severity("JARVIS_REPAIR_STRUCT_REACH_SEVERITY", _HARD),
                detail=f"patch makes {len(severed)} live component(s) unreachable from a system root",
                coordinates={
                    "root": next((r for r in roots if r in reach_pre), roots[0]),
                    "unreachable": sorted(severed),
                    "broken_path": [e for pair in removed for e in pair if e in severed][:6],
                },
            ))
        # Pruning telemetry: removed edges whose endpoint was already dead pre-fix.
        for src, dst in removed:
            if dst not in reach_pre and dst not in reach_post:
                prunes.append(StructuralPrune(severed_edge=(src, dst)))
        return divergences, prunes

    @staticmethod
    def _check_boundary(
        candidate_signatures: Dict[str, Optional[str]],
        reader: ConeReader,
        pre_edges: List[Tuple[str, str, str]],
    ) -> List[DivergenceSignature]:
        """Intra-file interface contract: a changed symbol signature whose in-cone callers
        (incoming CALLS edges) still rely on the old contract."""
        out: List[DivergenceSignature] = []
        for sym_key, new_sig in candidate_signatures.items():
            old_sig = reader.node_signature(sym_key)
            if old_sig is None or new_sig is None or old_sig == new_sig:
                continue
            callers = sorted({
                src for src, dst, etype in pre_edges
                if dst == sym_key and etype in ("calls", "imports", "imports_from")
            })
            if callers:
                out.append(DivergenceSignature(
                    kind="boundary_signature_mismatch",
                    severity=_severity("JARVIS_REPAIR_STRUCT_BOUNDARY_SEVERITY", _SOFT),
                    detail="changed symbol signature with un-updated in-cone callers",
                    coordinates={
                        "symbol": sym_key,
                        "old_signature": old_sig,
                        "new_signature": new_sig,
                        "callers": callers[:10],
                    },
                ))
        return out

    # ------------------------------------------------------------------ public
    async def validate(
        self,
        *,
        candidate_source: str,
        candidate_diff: str = "",
        file_path: str,
        repo_name: str,
        reader: ConeReader,
        candidate_signatures: Optional[Dict[str, Optional[str]]] = None,
    ) -> StructuralVerdict:
        """Run the isolated delta simulation + three proofs. ACCEPT/REJECT verdict + structured
        feedback. Self-gates on ``gate_enabled()``; fail-soft → ACCEPT on any inability to simulate."""
        if not gate_enabled():
            return StructuralVerdict(accepted=True, analyzed=False, note="gate_disabled")
        if not candidate_source or not file_path:
            return StructuralVerdict(accepted=True, analyzed=False, note="no_full_source")
        try:
            analyzed = await self._analyze_candidate(candidate_source, file_path, repo_name)
            if analyzed is None:
                return StructuralVerdict(accepted=True, analyzed=False, note="analyze_unavailable")
            new_file_edges, analyzed_sigs = analyzed
            sigs = candidate_signatures if candidate_signatures is not None else analyzed_sigs
            pre_edges = list(reader.cone_edges() or [])
            pre, post, removed = self._build_delta(pre_edges, new_file_edges, file_path)

            divergences: List[DivergenceSignature] = []
            prunes: List[StructuralPrune] = []
            divergences += self._check_acyclicity(pre, post)
            reach_div, reach_prunes = self._check_reachability(pre, post, reader.roots(), removed)
            divergences += reach_div
            prunes += reach_prunes
            if sigs:
                divergences += self._check_boundary(sigs, reader, pre_edges)

            # Hash each divergence by the candidate diff + its kind/coords for a stable signature.
            from backend.core.ouroboros.governance.failure_classifier import patch_signature_hash
            for d in divergences:
                d.signature_hash = patch_signature_hash(
                    f"{candidate_diff or candidate_source}\n{d.kind}\n{sorted(d.coordinates.items()) if d.coordinates else ''}"
                )

            verdict = StructuralVerdict(
                accepted=True, divergences=tuple(divergences), prunes=tuple(prunes), analyzed=True,
            )
            verdict.accepted = not verdict.blocking()
            return verdict
        except Exception as exc:  # noqa: BLE001 — gate must never break L2
            logger.debug("[StructuralGate] validate failed (non-fatal, ACCEPT): %s", exc)
            return StructuralVerdict(accepted=True, analyzed=False, note=f"error:{type(exc).__name__}")


# --------------------------------------------------------------------------- production cone reader
class OracleConeReader:
    """Read-only cone snapshot backed by the Oracle CKG. Reads cone-scoped edges + signatures +
    derives reachability roots from the graph (active tests / call-chain sources) — never mutates."""

    def __init__(self, graph: Any, cone: Any, failing_tests: Tuple[str, ...] = ()) -> None:
        self._g = graph
        self._cone = cone
        self._failing_tests = failing_tests

    def _cone_keys(self) -> List[str]:
        c = self._cone
        keys: List[str] = []
        for attr in ("fault_keys", "dependents", "dependencies"):
            keys.extend(getattr(c, attr, []) or [])
        return list(dict.fromkeys(keys))

    def cone_edges(self) -> List[Tuple[str, str, str]]:
        edges: List[Tuple[str, str, str]] = []
        seen: Set[Tuple[str, str, str]] = set()
        for key in self._cone_keys():
            try:
                for tgt, data in (self._g.get_edges_from(key) or []):
                    t = (str(key), str(tgt), str(data.get("edge_type", "")))
                    if t not in seen:
                        seen.add(t); edges.append(t)
                for src, data in (self._g.get_edges_to(key) or []):
                    t = (str(src), str(key), str(data.get("edge_type", "")))
                    if t not in seen:
                        seen.add(t); edges.append(t)
            except Exception:  # noqa: BLE001 — read-only best-effort
                continue
        return edges

    def node_signature(self, key: str) -> Optional[str]:
        try:
            node = self._g.get_node(key)
            return (node or {}).get("signature")
        except Exception:  # noqa: BLE001
            return None

    def roots(self) -> List[str]:
        """Reachability roots derived from the graph (no hardcoding): failing-test nodes + the
        sources of the cone's causal call-chains."""
        roots: List[str] = []
        for tid in self._failing_tests:
            name = tid.split("::")[-1].split("[")[0]
            try:
                for n in (self._g.find_nodes_by_name(name) or [])[:1]:
                    roots.append(str(n))
            except Exception:  # noqa: BLE001
                continue
        for chain in getattr(self._cone, "call_chain", []) or []:
            head = chain.split(" → ")[0].strip()
            if head:
                roots.append(head)
        return list(dict.fromkeys(roots))
