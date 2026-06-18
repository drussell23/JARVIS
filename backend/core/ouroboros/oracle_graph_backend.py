"""Oracle graph traversal seam — interface-segregated backends for graph queries.

Slice 1 of the SQLite-Backed Lazy Traversal arc (`docs/architecture/ORACLE_LAZY_TRAVERSAL_ADD.md`).

Today every blast-radius / call-chain / dependency query runs over the whole in-memory NetworkX
``DiGraph`` (~5 GB for the 29k-file brain). This module introduces the seam that will let those
traversals read SQLite on demand instead — so the full graph becomes *queryable* on a constrained
host without being *resident*. Slice 1 ships the seam + the canonical in-memory backend + the live
differential parity harness; Slice 2 plugs a ``SqliteLazyGraphBackend`` into the harness's shadow
slot.

Design (mirrors ``PersistenceProvider``):
  - ``GraphBackend``           — ABC exposing the ~7 traversal primitives + default algorithms
    (``shortest_path`` / ``simple_cycles`` / ``descendants``) built ON those primitives, so any
    backend works without bespoke algorithm code.
  - ``InMemoryGraphBackend``   — canonical baseline over the live ``CodebaseKnowledgeGraph`` (the
    DEFAULT; byte-identical to the pre-seam path). Overrides the algorithms with NetworkX for speed.
  - ``DualBackendParityHarness`` — a backend façade wrapping a trusted ``primary`` + a candidate
    ``shadow``. Every lookup runs on both; on ANY divergence/shadow-error it captures a rich
    telemetry payload and **autonomously falls back to the primary result** — never crashes the
    control plane. This is how the SQLite backend earns trust in production before it's trusted alone.
  - ``AdaptiveNodeCache``      — bounded LRU (baseline 5,000 nodes) whose ``maxsize`` contracts under
    ``MemoryPressureGate`` pressure. Wired into the SQLite backend in Slice 3; defined+tested here.

The query API is synchronous (see ADD §2), so this whole layer is sync.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

EdgeRow = Tuple[str, Dict[str, Any]]  # (neighbor_key, edge_attrs)


# ---------------------------------------------------------------------------- config (§10)
def lazy_traversal_enabled() -> bool:
    """``JARVIS_ORACLE_LAZY_TRAVERSAL_ENABLED`` — route graph queries through the SQLite-backed lazy
    backend instead of the resident in-memory DiGraph.

    **Graduated default-ON 2026-06-18** — the lazy read path is complete (primitives + find_* family
    + global streaming queries) and parity-proven; in-memory remains the corruption/cold-start
    FALLBACK (build_graph_backend returns it when no db is present). Kill switch ``=0`` restores the
    resident-graph read path."""
    return os.environ.get("JARVIS_ORACLE_LAZY_TRAVERSAL_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def parity_harness_enabled() -> bool:
    """``JARVIS_ORACLE_PARITY_HARNESS_ENABLED`` (default false) — run the candidate backend in the
    shadow slot of the DualBackendParityHarness, differentially verified against the trusted primary
    with autonomous fallback. The safe way to graduate the lazy backend in production."""
    return os.environ.get("JARVIS_ORACLE_PARITY_HARNESS_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def traversal_cache_max() -> int:
    """``JARVIS_ORACLE_TRAVERSAL_CACHE_MAX`` — baseline working-set node-cache size (§10 resolution:
    5,000 nodes). The adaptive contraction loop shrinks below this under host memory pressure."""
    try:
        return max(0, int(os.environ.get("JARVIS_ORACLE_TRAVERSAL_CACHE_MAX", "5000")))
    except (TypeError, ValueError):
        return 5000


def traversal_pressure_probe_interval_s() -> float:
    """``JARVIS_ORACLE_TRAVERSAL_PRESSURE_INTERVAL_S`` — minimum seconds between live
    MemoryPressureGate probes during traversal (Slice 3). Throttles the probe so the sync hot path
    stays fast while still contracting the cache within a couple seconds of host pressure. Default
    2.0s; ``0`` probes every frontier (used by soaks to force immediate contraction)."""
    try:
        return max(0.0, float(os.environ.get("JARVIS_ORACLE_TRAVERSAL_PRESSURE_INTERVAL_S", "2.0")))
    except (TypeError, ValueError):
        return 2.0


# ---------------------------------------------------------------------------- the seam
class GraphBackend(ABC):
    """The traversal seam. Concrete backends implement the primitives; the algorithms below are
    built purely on those primitives so they work over any backend (in-memory, SQLite, future)."""

    # --- primitives (every backend MUST implement) ---
    @abstractmethod
    def contains(self, key: str) -> bool: ...

    @abstractmethod
    def get_node(self, key: str) -> Optional[Dict[str, Any]]:
        """Node attribute dict (the exact shape ``NodeData.to_dict()`` produces), or None."""

    @abstractmethod
    def successors(self, key: str) -> List[EdgeRow]:
        """Outgoing edges: ``[(dst_key, edge_attrs), ...]``."""

    @abstractmethod
    def predecessors(self, key: str) -> List[EdgeRow]:
        """Incoming edges: ``[(src_key, edge_attrs), ...]``."""

    @abstractmethod
    def node_count(self) -> int: ...

    @abstractmethod
    def edge_count(self) -> int: ...

    # --- prefetch hooks (N+1 armor) ---
    # The default algorithms below are LAYER-BY-LAYER BFS so a backend can batch-load an entire
    # frontier in one disk sweep. These hooks are no-ops for the in-memory backend (RAM is already
    # the cache) and a batched ``WHERE src_key IN (...)`` for the SQLite lazy backend.
    def prefetch_successors(self, keys: List[str]) -> None:  # noqa: D401
        """Hint: the caller is about to read ``successors`` for all of ``keys``."""

    def prefetch_predecessors(self, keys: List[str]) -> None:  # noqa: D401
        """Hint: the caller is about to read ``predecessors`` for all of ``keys``."""

    # --- algorithms over the primitives (overridable for native speed) ---
    def successor_keys(self, key: str) -> List[str]:
        return [dst for dst, _ in self.successors(key)]

    def predecessor_keys(self, key: str) -> List[str]:
        return [src for src, _ in self.predecessors(key)]

    def shortest_path(self, source: str, target: str) -> Optional[List[str]]:
        """Unweighted shortest path via LAYER BFS over ``successors`` (same length as
        ``nx.shortest_path`` for an unweighted DiGraph). Prefetches each frontier (N+1 armor)."""
        if source == target:
            return [source] if self.contains(source) else None
        if not self.contains(source) or not self.contains(target):
            return None
        prev: Dict[str, Optional[str]] = {source: None}
        frontier: List[str] = [source]
        while frontier:
            self.prefetch_successors(frontier)        # batch the whole layer in one sweep
            nxt: List[str] = []
            for cur in frontier:
                for s in self.successor_keys(cur):
                    if s in prev:
                        continue
                    prev[s] = cur
                    if s == target:
                        path = [s]
                        while prev[path[-1]] is not None:
                            path.append(prev[path[-1]])  # type: ignore[arg-type]
                        path.reverse()
                        return path
                    nxt.append(s)
            frontier = nxt
        return None

    def descendants(self, key: str, max_depth: Optional[int] = None) -> Set[str]:
        """All nodes reachable from ``key`` (exclusive), optional depth bound. Layer BFS + prefetch."""
        seen: Set[str] = set()
        frontier: List[str] = [key]
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            self.prefetch_successors(frontier)        # batch the whole layer in one sweep
            nxt: List[str] = []
            for n in frontier:
                for s in self.successor_keys(n):
                    if s not in seen and s != key:
                        seen.add(s)
                        nxt.append(s)
            frontier = nxt
            depth += 1
        return seen

    def stream_edges(self, chunk_size: int = 10000):  # noqa: D401
        """Sliding-window cursor over ALL edges as ``(src_key, dst_key)`` — the global-sweep
        primitive (Slice 4). Yields so a global algorithm builds a *key-only* adjacency by streaming
        edges instead of materializing the whole DiGraph (no node attrs, no networkx overhead).
        Default is built on ``all_keys`` + ``successors``; backends with a real cursor override it
        (the SQLite backend streams the edges table via ``fetchmany``)."""
        for k in self.all_keys():
            for dst in self.successor_keys(k):
                yield (k, dst)

    def simple_cycles(self, max_cycles: Optional[int] = None) -> List[List[str]]:
        """Enumerate elementary cycles (global analysis, ADD §6) — STREAMED (Slice 4). Builds a
        key-only adjacency in ONE sliding-window sweep of the edges table (no per-node N+1, no node
        attrs, no full DiGraph), then runs the proven primitive DFS over that adjacency. RAM is
        O(V+E) keys (irreducible for cycle enumeration), NOT O(full graph with attrs). Both backends
        share this impl → parity is exact and bounded. ``max_cycles`` caps enumeration."""
        from collections import defaultdict
        adj: Dict[str, List[str]] = defaultdict(list)
        for src, dst in self.stream_edges():           # one cursor sweep, chunked
            adj[src].append(dst)
        keys = list(adj.keys())
        index = {k: i for i, k in enumerate(keys)}
        cycles: List[List[str]] = []

        def dfs(start: str, node: str, stack: List[str], on_stack: Set[str]) -> None:
            if max_cycles is not None and len(cycles) >= max_cycles:
                return
            stack.append(node)
            on_stack.add(node)
            for nbr in adj.get(node, ()):  # key-only adjacency — no DB round-trip, no attrs
                if index.get(nbr, -1) < index.get(start, 1 << 62):
                    continue  # only consider nodes >= start (avoid duplicate rotations)
                if nbr == start:
                    cycles.append(list(stack))
                    if max_cycles is not None and len(cycles) >= max_cycles:
                        break
                elif nbr not in on_stack:
                    dfs(start, nbr, stack, on_stack)
            stack.pop()
            on_stack.discard(node)

        for k in keys:
            if max_cycles is not None and len(cycles) >= max_cycles:
                break
            dfs(k, k, [], set())
        return cycles

    # --- node lookup primitives (back the find_* family; lazy-correct under Scoper eviction) ---
    def node_id_for(self, key: str):
        """Reconstruct a ``NodeID`` for a key (default: from ``get_node``; backends override for
        speed)."""
        from backend.core.ouroboros.oracle import NodeID
        attrs = self.get_node(key)
        if attrs and attrs.get("node_id"):
            return NodeID.from_dict(attrs["node_id"])
        return None

    def nodes_by_name(self, name: str, fuzzy: bool = False) -> List[str]:
        raise NotImplementedError

    def nodes_by_type(self, node_type_value: str) -> List[str]:
        raise NotImplementedError

    def nodes_in_file(self, file_path: str) -> List[str]:
        raise NotImplementedError

    def nodes_in_repo(self, repo: str) -> List[str]:
        raise NotImplementedError

    def all_keys(self) -> List[str]:
        """All node keys. Default raises — backends that support global scans override it."""
        raise NotImplementedError("backend does not support full key enumeration")


# ---------------------------------------------------------------------------- in-memory baseline
class InMemoryGraphBackend(GraphBackend):
    """Canonical backend over a live ``CodebaseKnowledgeGraph`` (read dynamically, so wholesale
    ``_graph`` replacement on cache load is transparent). Overrides the algorithms with NetworkX."""

    def __init__(self, ckg: Any):
        self._ckg = ckg

    @property
    def _g(self):
        return self._ckg._graph

    def contains(self, key: str) -> bool:
        return key in self._g

    def get_node(self, key: str) -> Optional[Dict[str, Any]]:
        g = self._g
        return dict(g.nodes[key]) if key in g else None

    def successors(self, key: str) -> List[EdgeRow]:
        g = self._g
        if key not in g:
            return []
        return [(dst, dict(g.edges[key, dst])) for dst in g.successors(key)]

    def predecessors(self, key: str) -> List[EdgeRow]:
        g = self._g
        if key not in g:
            return []
        return [(src, dict(g.edges[src, key])) for src in g.predecessors(key)]

    def node_count(self) -> int:
        return self._g.number_of_nodes()

    def edge_count(self) -> int:
        return self._g.number_of_edges()

    def all_keys(self) -> List[str]:
        return list(self._g.nodes)

    def stream_edges(self, chunk_size: int = 10000):
        for u, v in self._g.edges():
            yield (u, v)

    # node lookups (back the find_* family) — read the in-memory indices
    def node_id_for(self, key: str):
        return self._ckg._node_index.get(key)

    def nodes_by_name(self, name: str, fuzzy: bool = False) -> List[str]:
        nl = name.lower()
        out = []
        for k, nid in self._ckg._node_index.items():
            if (nl in nid.name.lower()) if fuzzy else (nid.name == name or nid.name.endswith(f".{name}")):
                out.append(k)
        return out

    def nodes_by_type(self, node_type_value: str) -> List[str]:
        from backend.core.ouroboros.oracle import NodeType
        try:
            return list(self._ckg._type_index.get(NodeType(node_type_value), set()))
        except ValueError:
            return []

    def nodes_in_file(self, file_path: str) -> List[str]:
        return list(self._ckg._file_index.get(file_path, set()))

    def nodes_in_repo(self, repo: str) -> List[str]:
        return list(self._ckg._repo_index.get(repo, set()))

    # native NetworkX override (exact + fast); simple_cycles uses the shared STREAMED impl so the
    # in-memory and SQLite backends are parity-exact AND bounded.
    def shortest_path(self, source: str, target: str) -> Optional[List[str]]:
        import networkx as nx
        g = self._g
        if source not in g or target not in g:
            return None
        try:
            return nx.shortest_path(g, source, target)
        except nx.NetworkXNoPath:
            return None


# ---------------------------------------------------------------------------- live differential harness
class DualBackendParityHarness(GraphBackend):
    """Weaponized live differential guard. Wraps a trusted ``primary`` + candidate ``shadow``; runs
    every read on both, and on ANY divergence or shadow error captures a telemetry payload and
    returns the PRIMARY result — never crashing the control plane. The primary's result is always
    authoritative, so the shadow can be wrong/absent without any availability impact."""

    def __init__(
        self,
        primary: GraphBackend,
        shadow: Optional[GraphBackend] = None,
        *,
        on_divergence: Optional[Callable[[Dict[str, Any]], None]] = None,
        max_records: int = 100,
    ):
        self.primary = primary
        self.shadow = shadow
        self._on_divergence = on_divergence
        self.comparisons = 0
        self.divergences = 0
        self.shadow_errors = 0
        self.records: deque = deque(maxlen=max(1, max_records))
        # per-method latency accumulators (seconds) — the raw execution-delta payload (Phase 3)
        self.latency: Dict[str, Dict[str, float]] = {}

    def _account(self, method: str, primary_s: float, shadow_s: float) -> None:
        m = self.latency.setdefault(method, {"primary_s": 0.0, "shadow_s": 0.0, "n": 0})
        m["primary_s"] += primary_s
        m["shadow_s"] += shadow_s
        m["n"] += 1

    # -- the differential wrapper --
    def _diff(self, method: str, args: tuple, primary_thunk: Callable[[], Any]) -> Any:
        """Time + run the primary (authoritative); if a shadow exists, time + run it, compare with a
        method-aware comparator, and record + fall back on any mismatch/error. Always returns the
        primary's value (the trusted path)."""
        t0 = _now()
        primary_val = primary_thunk()
        primary_s = _now() - t0
        if self.shadow is None:
            return primary_val
        self.comparisons += 1
        t1 = _now()
        try:
            shadow_val = getattr(self.shadow, method)(*args)
            shadow_s = _now() - t1
        except Exception as exc:  # noqa: BLE001 — shadow must never break the query
            self.shadow_errors += 1
            self._account(method, primary_s, _now() - t1)
            self._record(method, args, primary_val, f"<shadow raised: {exc!r}>", kind="shadow_error")
            return primary_val
        self._account(method, primary_s, shadow_s)
        if not _compare(method, primary_val, shadow_val):
            self.divergences += 1
            self._record(method, args, primary_val, shadow_val, kind="divergence")
        return primary_val

    def _record(self, method: str, args: tuple, primary_val: Any, shadow_val: Any, *, kind: str) -> None:
        payload = {
            "kind": kind,
            "method": method,
            "args": [str(a) for a in args],
            "primary": _summarize(primary_val),
            "shadow": _summarize(shadow_val),
        }
        self.records.append(payload)
        logger.warning(
            "[OracleParity] %s on %s%s — primary=%s shadow=%s (autonomous fallback to primary)",
            kind, method, payload["args"], payload["primary"], payload["shadow"],
        )
        if self._on_divergence is not None:
            try:
                self._on_divergence(payload)
            except Exception:  # noqa: BLE001 — telemetry sink must never break the query
                logger.debug("[OracleParity] on_divergence sink failed", exc_info=True)

    # -- primitives (primary authoritative, shadow differentially checked) --
    def contains(self, key: str) -> bool:
        return self._diff("contains", (key,), lambda: self.primary.contains(key))

    def get_node(self, key: str) -> Optional[Dict[str, Any]]:
        return self._diff("get_node", (key,), lambda: self.primary.get_node(key))

    def successors(self, key: str) -> List[EdgeRow]:
        return self._diff("successors", (key,), lambda: self.primary.successors(key))

    def predecessors(self, key: str) -> List[EdgeRow]:
        return self._diff("predecessors", (key,), lambda: self.primary.predecessors(key))

    def node_count(self) -> int:
        return self._diff("node_count", (), lambda: self.primary.node_count())

    def edge_count(self) -> int:
        return self._diff("edge_count", (), lambda: self.primary.edge_count())

    def all_keys(self) -> List[str]:
        return self.primary.all_keys()

    def stream_edges(self, chunk_size: int = 10000):
        return self.primary.stream_edges(chunk_size)

    # node lookups — primary authoritative, shadow differentially checked (order-insensitive)
    def node_id_for(self, key: str):
        return self._diff("node_id_for", (key,), lambda: self.primary.node_id_for(key))

    def nodes_by_name(self, name: str, fuzzy: bool = False) -> List[str]:
        return self._diff("nodes_by_name", (name, fuzzy),
                          lambda: self.primary.nodes_by_name(name, fuzzy))

    def nodes_by_type(self, node_type_value: str) -> List[str]:
        return self._diff("nodes_by_type", (node_type_value,),
                          lambda: self.primary.nodes_by_type(node_type_value))

    def nodes_in_file(self, file_path: str) -> List[str]:
        return self._diff("nodes_in_file", (file_path,),
                          lambda: self.primary.nodes_in_file(file_path))

    def nodes_in_repo(self, repo: str) -> List[str]:
        return self._diff("nodes_in_repo", (repo,), lambda: self.primary.nodes_in_repo(repo))

    # prefetch hints reach BOTH backends (the shadow's batched IN query is the whole point)
    def prefetch_successors(self, keys: List[str]) -> None:
        self.primary.prefetch_successors(keys)
        if self.shadow is not None:
            try:
                self.shadow.prefetch_successors(keys)
            except Exception:  # noqa: BLE001
                pass

    # algorithms: trust the primary's (overridden) impl, differentially check the shadow's
    def shortest_path(self, source: str, target: str) -> Optional[List[str]]:
        return self._diff("shortest_path", (source, target),
                          lambda: self.primary.shortest_path(source, target))

    def simple_cycles(self, max_cycles: Optional[int] = None) -> List[List[str]]:
        return self._diff("simple_cycles", (max_cycles,),
                          lambda: self.primary.simple_cycles(max_cycles))

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "comparisons": self.comparisons,
            "divergences": self.divergences,
            "shadow_errors": self.shadow_errors,
            "records": len(self.records),
        }
        # raw latency deltas (Phase 3 payload): per-method primary vs shadow mean, in ms
        lat = {}
        for m, d in self.latency.items():
            n = max(1, d["n"])
            lat[m] = {
                "n": d["n"],
                "primary_ms": round(1000.0 * d["primary_s"] / n, 4),
                "shadow_ms": round(1000.0 * d["shadow_s"] / n, 4),
            }
        out["latency"] = lat
        return out


def _now() -> float:
    import time
    return time.perf_counter()


def _compare(method: str, a: Any, b: Any) -> bool:
    """Method-aware equality. Most reads are exact (order-insensitive). Two cases are NOT
    exact-comparable because more than one correct answer exists:
      - ``shortest_path``: any path of the same length is equally optimal → compare LENGTH (or both None).
      - ``simple_cycles``: enumeration order/rotation differs by algorithm → compare the SET of cycles
        (each cycle as a frozenset of its nodes)."""
    if method == "shortest_path":
        if a is None or b is None:
            return a is None and b is None
        return len(a) == len(b)
    if method == "simple_cycles":
        try:
            return {frozenset(c) for c in a} == {frozenset(c) for c in b}
        except TypeError:
            return a == b
    return _results_equal(a, b)


def _results_equal(a: Any, b: Any) -> bool:
    """Order-insensitive structural equality for primitive results (edge lists are sets of
    (neighbor, sorted-attr-items); node dicts compared directly)."""
    if isinstance(a, list) and isinstance(b, list):
        try:
            return _norm_edges(a) == _norm_edges(b)
        except (TypeError, ValueError):
            return a == b
    return a == b


def _norm_edges(rows: list):
    norm = set()
    for r in rows:
        if isinstance(r, tuple) and len(r) == 2 and isinstance(r[1], dict):
            norm.add((r[0], tuple(sorted((k, _h(v)) for k, v in r[1].items()))))
        else:
            norm.add(_h(r))
    return norm


def _h(v: Any):
    return tuple(sorted(v.items())) if isinstance(v, dict) else (tuple(v) if isinstance(v, list) else v)


def _summarize(v: Any) -> str:
    if isinstance(v, list):
        return f"list[{len(v)}]"
    if isinstance(v, dict):
        return f"dict{{{','.join(sorted(v)[:4])}}}"
    s = repr(v)
    return s if len(s) <= 80 else s[:77] + "..."


# ---------------------------------------------------------------------------- adaptive working-set cache
class AdaptiveNodeCache:
    """Bounded LRU for traversal working-set nodes (baseline 5,000). ``maxsize`` contracts under
    host memory pressure — the query-side mirror of the index-side Memory Armor. Sync hot path;
    contraction is driven by :meth:`apply_pressure` (a pressure-level string). Wired into the SQLite
    backend in Slice 3; standalone + tested here."""

    _PRESSURE_FRAC = {"ok": 1.0, "warn": 1.0, "high": 0.5, "critical": 0.1}

    def __init__(self, baseline: Optional[int] = None):
        self._baseline = traversal_cache_max() if baseline is None else max(0, baseline)
        self._maxsize = self._baseline
        self._d: "OrderedDict[str, Any]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def get(self, key: str) -> Any:
        if key in self._d:
            self._d.move_to_end(key)
            self.hits += 1
            return self._d[key]
        self.misses += 1
        return None

    def put(self, key: str, value: Any) -> None:
        if self._maxsize <= 0:
            return
        self._d[key] = value
        self._d.move_to_end(key)
        self._trim()

    def apply_pressure(self, level: str) -> int:
        """Recompute ``maxsize`` from the pressure level and evict down to it. Returns evicted count.
        CRITICAL also signals the caller to GC (handled by the caller, which holds gc)."""
        frac = self._PRESSURE_FRAC.get(level, 1.0)
        self._maxsize = int(self._baseline * frac)
        return self._trim()

    def _trim(self) -> int:
        n = 0
        while len(self._d) > self._maxsize:
            self._d.popitem(last=False)  # evict LRU
            self.evictions += 1
            n += 1
        return n

    def __len__(self) -> int:
        return len(self._d)


# ---------------------------------------------------------------------------- SQLite lazy backend
_NODE_COLS = (
    "node_key, repo, file_path, name, node_type, line_number, docstring, signature, decorators,"
    " base_classes, complexity, line_count, last_modified, source_hash"
)


def _row_to_node_attrs(r: tuple) -> Dict[str, Any]:
    """Rebuild the EXACT attr dict shape ``NodeData.to_dict()`` produces (so it's byte-identical to
    the in-memory ``dict(graph.nodes[k])``)."""
    import json
    (node_key, repo, file_path, name, node_type, line_number, docstring, signature,
     decorators, base_classes, complexity, line_count, last_modified, source_hash) = r
    return {
        "node_id": {"repo": repo, "file_path": file_path, "name": name,
                    "node_type": node_type, "line_number": line_number},
        "docstring": docstring,
        "signature": signature,
        "decorators": json.loads(decorators) if decorators else [],
        "base_classes": json.loads(base_classes) if base_classes else [],
        "complexity": complexity,
        "line_count": line_count,
        "last_modified": last_modified,
        "source_hash": source_hash,
    }


def _edge_attrs(edge_type: str, line_number: int, context: str) -> Dict[str, Any]:
    return {"edge_type": edge_type, "line_number": line_number, "context": context}


class SqliteLazyGraphBackend(GraphBackend):
    """On-demand traversal over the canonical ``oracle.db`` — the query-time RAM-wall killer. Reads
    via a sync, thread-safe, READ-ONLY ``sqlite3`` connection over the existing ``idx_edges_src`` /
    ``idx_edges_dst`` / ``idx_nodes_*`` indexes; materializes nodes/edges on demand into adaptive
    caches instead of holding the whole graph resident.

    Predictive prefetch (the N+1 armor): ``prefetch_successors(frontier)`` pulls an ENTIRE BFS layer
    in one ``WHERE src_key IN (...)`` sweep, so a deep recursion costs O(depth) disk round-trips
    instead of O(nodes). Matches the in-memory backend exactly — including stub-node semantics (edge
    endpoints with no ``nodes`` row), so the parity harness sees zero divergence.
    """

    _IN_CHUNK = 800  # under SQLite's ~999 bound-parameter limit

    def __init__(self, db_path: Any, *, busy_timeout_ms: int = 5000,
                 node_cache: Optional[AdaptiveNodeCache] = None, memory_gate: Any = None):
        import threading
        self._db_path = str(db_path)
        self._busy = busy_timeout_ms
        self._lock = threading.Lock()
        self._conn: Any = None
        self._node_cache = node_cache or AdaptiveNodeCache()
        self._succ_cache = AdaptiveNodeCache()   # src_key -> [(dst, edge_attrs)]
        self._pred_cache = AdaptiveNodeCache()   # dst_key -> [(src, edge_attrs)]
        self.query_count = 0                     # disk round-trips (prefetch N+1-armor telemetry)
        # Slice 3 — live memory-pressure governance of the working set. Reuses the shared
        # MemoryPressureGate (same advisory the index armor + scoper use). ``memory_gate`` is
        # injectable for tests; otherwise resolved lazily from the default gate.
        self._gate = memory_gate
        self._gate_resolved = memory_gate is not None
        self._last_probe_t = 0.0
        self.pressure_events = 0                  # times the cache contracted under live pressure
        self.last_pressure = "ok"

    # -- connection (sync, read-only, thread-safe) --
    def _c(self):
        if self._conn is None:
            import sqlite3
            self._conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True, check_same_thread=False,
            )
            self._conn.execute(f"PRAGMA busy_timeout={self._busy}")
            self._conn.execute("PRAGMA query_only=ON")
        return self._conn

    def _q(self, sql: str, params: tuple = ()) -> List[tuple]:
        with self._lock:
            self.query_count += 1
            return list(self._c().execute(sql, params).fetchall())

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def apply_pressure(self, level: str) -> int:
        """Contract all three working-set caches to the pressure ``level`` and, at CRITICAL, flush
        freed dicts back to the OS via GC. Returns total entries evicted. Reusable from an external
        cadence (e.g. the governor tick) as well as the internal throttled probe."""
        evicted = 0
        for c in (self._node_cache, self._succ_cache, self._pred_cache):
            evicted += c.apply_pressure(level)
        if level == "critical":
            import gc
            gc.collect()
        self.last_pressure = level
        return evicted

    # -- live memory-pressure governance (Slice 3) --
    def _resolve_gate(self):
        if not self._gate_resolved:
            self._gate_resolved = True
            try:
                from backend.core.ouroboros.governance.memory_pressure_gate import (
                    get_default_gate, is_enabled,
                )
                self._gate = get_default_gate() if is_enabled() else None
            except Exception:  # noqa: BLE001 — governance is advisory; never break a query
                self._gate = None
        return self._gate

    def _maybe_apply_pressure(self) -> None:
        """Throttled probe (per traversal frontier, time-bounded) of the shared MemoryPressureGate;
        contracts the caches when host RAM is elevated. Sync + cheap; keeps the hot read path fast
        while making the resident working set DYNAMICALLY shrink under pressure instead of growing
        unbounded during a deep recursion. No-op when the gate is disabled/unavailable."""
        gate = self._resolve_gate()
        if gate is None:
            return
        now = _now()
        if now - self._last_probe_t < traversal_pressure_probe_interval_s():
            return
        self._last_probe_t = now
        try:
            level = gate.pressure().value
        except Exception:  # noqa: BLE001
            return
        if level in ("warn", "high", "critical"):
            evicted = self.apply_pressure(level)
            if evicted > 0 or level == "critical":
                self.pressure_events += 1
        else:
            # pressure cleared → restore baseline cache size (adaptive both ways)
            if self.last_pressure != "ok":
                self.apply_pressure("ok")

    # -- primitives (stub-aware for byte-identical parity with the in-memory graph) --
    def _in_edges(self, key: str) -> bool:
        if self._q("SELECT 1 FROM edges WHERE src_key=? LIMIT 1", (key,)):
            return True
        return bool(self._q("SELECT 1 FROM edges WHERE dst_key=? LIMIT 1", (key,)))

    def contains(self, key: str) -> bool:
        if self._succ_cache.get(key) or self._pred_cache.get(key) or self._node_cache.get(key):
            return True
        if self._q("SELECT 1 FROM nodes WHERE node_key=? LIMIT 1", (key,)):
            return True
        return self._in_edges(key)   # stub node (edge endpoint w/o a row) — matches in-memory

    def get_node(self, key: str) -> Optional[Dict[str, Any]]:
        cached = self._node_cache.get(key)
        if cached is not None:
            return dict(cached)
        rows = self._q(f"SELECT {_NODE_COLS} FROM nodes WHERE node_key=?", (key,))
        if rows:
            attrs = _row_to_node_attrs(rows[0])
            self._node_cache.put(key, attrs)
            return dict(attrs)
        # no row: in-memory returns {} for a stub endpoint, None for a truly-absent key
        return {} if self._in_edges(key) else None

    def successors(self, key: str) -> List[EdgeRow]:
        v = self._succ_cache.get(key)
        if v is not None:
            return list(v)
        rows = self._q(
            "SELECT dst_key, edge_type, line_number, context FROM edges WHERE src_key=?", (key,))
        out = [(dst, _edge_attrs(et, ln, ctx)) for dst, et, ln, ctx in rows]
        self._succ_cache.put(key, out)
        return list(out)

    def predecessors(self, key: str) -> List[EdgeRow]:
        v = self._pred_cache.get(key)
        if v is not None:
            return list(v)
        rows = self._q(
            "SELECT src_key, edge_type, line_number, context FROM edges WHERE dst_key=?", (key,))
        out = [(src, _edge_attrs(et, ln, ctx)) for src, et, ln, ctx in rows]
        self._pred_cache.put(key, out)
        return list(out)

    def node_count(self) -> int:
        # distinct union of node rows + edge endpoints == in-memory graph.number_of_nodes() (w/ stubs)
        rows = self._q(
            "SELECT COUNT(*) FROM (SELECT node_key AS k FROM nodes "
            "UNION SELECT src_key FROM edges UNION SELECT dst_key FROM edges)")
        return int(rows[0][0]) if rows else 0

    def edge_count(self) -> int:
        rows = self._q("SELECT COUNT(*) FROM edges")
        return int(rows[0][0]) if rows else 0

    def all_keys(self) -> List[str]:
        seen = set()
        for (k,) in self._q("SELECT node_key FROM nodes"):
            seen.add(k)
        for (s, d) in self._q("SELECT src_key, dst_key FROM edges"):
            seen.add(s); seen.add(d)
        return list(seen)

    # -- global-sweep streaming (Slice 4): sliding-window cursor over the edges table --
    def stream_edges(self, chunk_size: int = 10000):
        with self._lock:
            self.query_count += 1
            cur = self._c().execute("SELECT src_key, dst_key FROM edges")
            while True:
                rows = cur.fetchmany(chunk_size)
                if not rows:
                    break
                for src, dst in rows:
                    yield (src, dst)

    # -- node lookups (back the find_* family; lazy-correct — read SQL, not the partial in-mem index) --
    def node_id_for(self, key: str):
        from backend.core.ouroboros.oracle import NodeID
        attrs = self.get_node(key)
        if attrs and attrs.get("node_id"):
            return NodeID.from_dict(attrs["node_id"])
        return None

    def nodes_by_name(self, name: str, fuzzy: bool = False) -> List[str]:
        if fuzzy:
            rows = self._q("SELECT node_key FROM nodes WHERE lower(name) LIKE ?", (f"%{name.lower()}%",))
        else:
            rows = self._q("SELECT node_key FROM nodes WHERE name = ? OR name LIKE ?",
                           (name, f"%.{name}"))
        return [r[0] for r in rows]

    def nodes_by_type(self, node_type_value: str) -> List[str]:
        return [r[0] for r in self._q(  # idx_nodes_type
            "SELECT node_key FROM nodes WHERE node_type = ?", (node_type_value,))]

    def nodes_in_file(self, file_path: str) -> List[str]:
        return [r[0] for r in self._q(  # idx_nodes_file
            "SELECT node_key FROM nodes WHERE file_path = ?", (file_path,))]

    def nodes_in_repo(self, repo: str) -> List[str]:
        return [r[0] for r in self._q("SELECT node_key FROM nodes WHERE repo = ?", (repo,))]

    # -- predictive batch prefetch (the N+1 armor) --
    def prefetch_successors(self, keys: List[str]) -> None:
        self._prefetch(keys, self._succ_cache, "src_key", "dst_key")

    def prefetch_predecessors(self, keys: List[str]) -> None:
        self._prefetch(keys, self._pred_cache, "dst_key", "src_key")

    def _prefetch(self, keys: List[str], cache: AdaptiveNodeCache, by: str, other: str) -> None:
        # Live memory governance: one throttled gate probe per traversal frontier (the natural,
        # bounded cadence) so a deep recursion contracts the working set instead of growing it.
        self._maybe_apply_pressure()
        want = [k for k in dict.fromkeys(keys) if cache.get(k) is None]  # dedup + skip cached
        if not want:
            return
        from collections import defaultdict
        for i in range(0, len(want), self._IN_CHUNK):
            chunk = want[i:i + self._IN_CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = self._q(
                f"SELECT {by}, {other}, edge_type, line_number, context FROM edges WHERE {by} IN ({ph})",
                tuple(chunk),
            )
            grouped: Dict[str, List[EdgeRow]] = defaultdict(list)
            for k, nbr, et, ln, ctx in rows:
                grouped[k].append((nbr, _edge_attrs(et, ln, ctx)))
            for k in chunk:
                cache.put(k, grouped.get(k, []))   # cache known-empty too (prevents re-query)


# ---------------------------------------------------------------------------- factory
def build_graph_backend(ckg: Any, *, db_path: Any = None) -> GraphBackend:
    """Single decision point for the traversal backend.
      - Lazy OFF (default): ``InMemoryGraphBackend`` — byte-identical to the pre-seam path.
      - Lazy ON + parity ON + a readable ``db_path``: ``DualBackendParityHarness`` with the in-memory
        primary (authoritative) differentially verifying the SQLite lazy shadow — the safe production
        graduation path.
      - Lazy ON + parity OFF + db: the SQLite backend directly (post-graduation).
    Fail-soft: any SQLite-open problem falls back to in-memory."""
    primary = InMemoryGraphBackend(ckg)
    if not lazy_traversal_enabled() or db_path is None:
        return primary
    try:
        import os as _os
        if not _os.path.exists(str(db_path)):
            return primary
        shadow = SqliteLazyGraphBackend(db_path)
        if parity_harness_enabled():
            return DualBackendParityHarness(primary=primary, shadow=shadow)
        return shadow
    except Exception:  # noqa: BLE001 — never let the seam break Oracle construction
        logger.warning("[OracleBackend] lazy backend unavailable — using in-memory", exc_info=True)
        return primary
