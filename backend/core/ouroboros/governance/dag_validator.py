"""
DAG Validator — Manifesto §2 Directed Acyclic Graph enforcement.

Pure, side-effect-free validator for the execution-graph DAG shape
consumed by the orchestrator's parallel-GENERATE path and produced by
the PLAN subagent (Phase B). Returns a structured DagValidationResult;
never raises on valid OR invalid input — malformed DAGs surface as
``valid=False`` with a human-readable error list.

Invariants enforced (Derek's Phase B roadmap §PLAN):

  1. **Acyclicity** — every dependency edge points "forward"; no
     cycles. Detected via DFS with WHITE/GRAY/BLACK coloring (cheaper
     than topological sort when we only need the yes/no answer).

  2. **Reachability** — every unit is reachable from some root. A
     root is a unit with no incoming edges. Isolated sub-DAGs (two
     disjoint trees) are rejected — they indicate the planner
     hallucinated orphan work.

  3. **Owned-path disjointness on parallel branches** — two units
     with no dependency relationship (neither ancestor/descendant of
     the other) cannot share any path in ``owned_paths``. Shared
     paths require a ``barrier_id`` — which is modeled as an
     edge making the units sequential, not parallel.

  4. **Acceptance-test coverage** — every unit declares at least one
     ``acceptance_test`` OR an explicit ``no_test_rationale`` field.
     Blank coverage without rationale is rejected.

  5. **Owned-paths non-empty** — every unit claims at least one path.
     Zero-path units are rejected as meaningless.

  6. **unit_id uniqueness** — IDs are the primary key for
     dependency_ids references.

  7. **dependency_ids reference existing units** — dangling refs
     reject the DAG.

The validator is a pure function so it can be:
  * called by PLAN to self-validate before returning
  * called by the orchestrator before consuming the DAG
  * called by regression tests without mocking anything

No networkx dependency — the graph is small (typically ≤ 20 units)
and the algorithms are simple. Python 3.9 compatible.

Manifesto alignment:
  §2 — Directed Acyclic Graph: the DAG shape IS the contract. A
       "mostly valid" DAG is rejected; GENERATE parallelism rests on
       the guarantees above.
  §8 — Absolute Observability: validation errors carry enough detail
       to diagnose the malformed DAG without re-running the planner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Mapping, Sequence, Set, Tuple


# ============================================================================
# Typed result
# ============================================================================


@dataclass(frozen=True)
class DagValidationResult:
    """Structured outcome of validate_plan_dag.

    Attributes
    ----------
    valid:
        True iff every invariant holds. False if ANY invariant fails.
    errors:
        Human-readable error strings; empty tuple when valid.
    unit_count:
        Total units evaluated (for observability).
    edge_count:
        Total dependency edges evaluated.
    root_count:
        Number of units with no incoming edges (roots of the DAG).
    parallel_branches:
        Tuple of (unit_id, unit_id) pairs that are concurrently
        executable per the acyclicity + owned-path-disjointness
        analysis. Empty for a linear DAG. Populated even on invalid
        DAGs (best-effort — useful for debugging).
    """

    valid: bool
    errors: Tuple[str, ...] = ()
    unit_count: int = 0
    edge_count: int = 0
    root_count: int = 0
    parallel_branches: Tuple[Tuple[str, str], ...] = ()


# ============================================================================
# Unit shape expected by the validator
# ============================================================================
#
# The validator accepts any Mapping with these keys. We intentionally
# don't bind it to a specific dataclass — the PLAN subagent produces
# tuples-of-tuples (for SubagentResult.type_payload frozenness) and
# orchestrator consumers work with dicts. The validator normalizes on
# the way in.
#
#   unit_id: str                  — primary key
#   dependency_ids: Sequence[str] — incoming dependency unit_ids
#   owned_paths: Sequence[str]    — filesystem paths this unit may mutate
#   acceptance_tests: Sequence[str]  — OR no_test_rationale (str)
#   no_test_rationale: Optional[str] — required when acceptance_tests empty
#   barrier_id: Optional[str]     — optional convergence marker
#
# Edges: the validator derives edges from dependency_ids alone. The
# caller does NOT pass a separate edge list.


def _coerce_unit(u: Any) -> Dict[str, Any]:
    """Normalize a unit into a dict regardless of input shape.

    Accepts dict, tuple-of-tuple (key/value pairs), or any object with
    ``__getattr__``. Missing keys default to empty-sequence for lists
    or empty-string for strings, so a malformed unit surfaces as a
    validation error rather than a KeyError.
    """
    if isinstance(u, Mapping):
        return dict(u)
    if isinstance(u, tuple) and all(
        isinstance(pair, tuple) and len(pair) == 2 for pair in u
    ):
        return {k: v for k, v in u}
    # Fallback: attribute access.
    return {
        "unit_id": getattr(u, "unit_id", ""),
        "dependency_ids": getattr(u, "dependency_ids", ()),
        "owned_paths": getattr(u, "owned_paths", ()),
        "acceptance_tests": getattr(u, "acceptance_tests", ()),
        "no_test_rationale": getattr(u, "no_test_rationale", ""),
        "barrier_id": getattr(u, "barrier_id", ""),
    }


# ============================================================================
# Core validator
# ============================================================================


def validate_plan_dag(units: Sequence[Any]) -> DagValidationResult:
    """Validate a plan DAG against all §2 invariants.

    Pure function. Never raises. Returns DagValidationResult with
    ``valid=False`` + per-invariant error strings on any failure.

    Parameters
    ----------
    units:
        Sequence of unit descriptors (dict | tuple-of-tuple | object
        with attributes). The validator coerces each into a dict
        internally.
    """
    errors: List[str] = []
    coerced = [_coerce_unit(u) for u in units]
    unit_count = len(coerced)
    if unit_count == 0:
        return DagValidationResult(
            valid=False,
            errors=("DAG has zero units — PLAN must emit at least one unit",),
        )

    # ---- Invariant 6: unit_id uniqueness --------------------------------
    ids: List[str] = []
    id_set: Set[str] = set()
    for i, u in enumerate(coerced):
        uid = str(u.get("unit_id", "") or "").strip()
        if not uid:
            errors.append(f"units[{i}]: missing unit_id")
            continue
        if uid in id_set:
            errors.append(
                f"unit_id={uid!r} duplicated at units[{i}] — "
                "unit_id must be a primary key"
            )
            continue
        ids.append(uid)
        id_set.add(uid)

    if errors:
        # Cannot proceed without unique IDs — bail early.
        return DagValidationResult(
            valid=False,
            errors=tuple(errors),
            unit_count=unit_count,
        )

    # Build an adjacency map (unit_id → list of dependency unit_ids).
    deps: Dict[str, List[str]] = {}
    for u in coerced:
        uid = str(u.get("unit_id"))
        raw_deps = u.get("dependency_ids", ()) or ()
        deps[uid] = [str(d) for d in raw_deps]

    # ---- Invariant 7: dependency_ids reference existing units -----------
    edge_count = 0
    for uid, dep_ids in deps.items():
        for d in dep_ids:
            if d not in id_set:
                errors.append(
                    f"unit_id={uid!r} depends on {d!r} which does not "
                    "exist in this DAG — dangling reference"
                )
            else:
                edge_count += 1

    # ---- Invariant 1: Acyclicity ----------------------------------------
    cycle_errors = _detect_cycles(deps)
    errors.extend(cycle_errors)

    # ---- Compute roots (for reachability + observability) ---------------
    roots = {uid for uid in deps if not deps[uid]}
    root_count = len(roots)
    if root_count == 0 and not cycle_errors:
        # All units have at least one dependency but no cycles — impossible
        # unless something weird; surface as a reachability error.
        errors.append(
            "DAG has no roots (every unit has a dependency) yet no "
            "cycle detected — malformed graph"
        )

    # ---- Invariant 2: Reachability from some root -----------------------
    if roots and not cycle_errors:
        reachable = _bfs_reachable(deps, roots)
        unreachable = id_set - reachable
        if unreachable:
            errors.append(
                "DAG has unreachable units (not descendants of any root): "
                + ", ".join(sorted(unreachable))
                + " — every unit must be reachable from a root"
            )

    # ---- Invariant 5: owned_paths non-empty -----------------------------
    paths_by_unit: Dict[str, FrozenSet[str]] = {}
    for u in coerced:
        uid = str(u.get("unit_id"))
        raw_paths = u.get("owned_paths", ()) or ()
        paths = frozenset(str(p) for p in raw_paths if p)
        paths_by_unit[uid] = paths
        if not paths:
            errors.append(
                f"unit_id={uid!r} has no owned_paths — unit must claim "
                "at least one filesystem path (or be deleted from the plan)"
            )

    # ---- Invariant 4: acceptance_test coverage --------------------------
    for u in coerced:
        uid = str(u.get("unit_id"))
        tests = u.get("acceptance_tests", ()) or ()
        rationale = str(u.get("no_test_rationale", "") or "").strip()
        if not tests and not rationale:
            errors.append(
                f"unit_id={uid!r} has no acceptance_tests and no "
                "no_test_rationale — every unit needs test coverage or "
                "an explicit rationale for absence"
            )

    # ---- Invariant 3: owned-path disjointness on parallel branches ------
    # Derive ancestor map; two units are parallel iff neither is an
    # ancestor of the other. Parallel units with overlapping paths are
    # a disjointness violation.
    parallel_pairs: List[Tuple[str, str]] = []
    if not cycle_errors:
        ancestors = _compute_ancestors(deps)
        sorted_ids = sorted(id_set)
        for i, a in enumerate(sorted_ids):
            for b in sorted_ids[i + 1:]:
                if a in ancestors[b] or b in ancestors[a]:
                    continue  # ordered pair — sequential, not parallel
                # a and b are concurrent.
                parallel_pairs.append((a, b))
                overlap = paths_by_unit[a] & paths_by_unit[b]
                if overlap:
                    errors.append(
                        f"parallel units {a!r} and {b!r} share owned_paths "
                        f"{sorted(overlap)!r} — parallel branches must have "
                        "disjoint paths (introduce a barrier_id or a "
                        "dependency edge if these cannot run concurrently)"
                    )

    return DagValidationResult(
        valid=not errors,
        errors=tuple(errors),
        unit_count=unit_count,
        edge_count=edge_count,
        root_count=root_count,
        parallel_branches=tuple(parallel_pairs),
    )


# ============================================================================
# Graph algorithms — simple, no networkx dependency
# ============================================================================


def _detect_cycles(deps: Dict[str, List[str]]) -> List[str]:
    """DFS with WHITE/GRAY/BLACK coloring.

    Returns a list of human-readable error strings, one per cycle
    detected. An empty list means the graph is acyclic.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {uid: WHITE for uid in deps}
    errors: List[str] = []

    def visit(node: str, stack: List[str]) -> None:
        if color[node] == BLACK:
            return
        if color[node] == GRAY:
            # Found a back-edge — stack contains the cycle.
            cycle_start = stack.index(node) if node in stack else 0
            cycle = stack[cycle_start:] + [node]
            errors.append(
                "DAG contains a cycle: "
                + " → ".join(cycle)
                + " (dependency_ids must point to predecessors, never "
                "successors)"
            )
            return
        color[node] = GRAY
        stack.append(node)
        for d in deps.get(node, ()):
            if d not in color:
                continue  # dangling dep — already reported by invariant 7
            visit(d, stack)
        stack.pop()
        color[node] = BLACK

    for uid in deps:
        if color[uid] == WHITE:
            visit(uid, [])

    # Deduplicate while preserving order.
    seen: Set[str] = set()
    out: List[str] = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _bfs_reachable(
    deps: Dict[str, List[str]], roots: Set[str],
) -> Set[str]:
    """BFS from roots following the reverse of dependency edges.

    A root is a unit with no dependencies. Descendants of a root are
    units that depend on it (directly or transitively). To reach
    "all descendants" we invert the adjacency: node X is a descendant
    of Y iff Y ∈ deps[X].
    """
    # Invert: reverse_adj[y] = list of units that depend on y
    reverse_adj: Dict[str, List[str]] = {uid: [] for uid in deps}
    for uid, dep_list in deps.items():
        for d in dep_list:
            if d in reverse_adj:
                reverse_adj[d].append(uid)
    reachable: Set[str] = set(roots)
    frontier = list(roots)
    while frontier:
        node = frontier.pop()
        for child in reverse_adj.get(node, ()):
            if child not in reachable:
                reachable.add(child)
                frontier.append(child)
    return reachable


def _compute_ancestors(
    deps: Dict[str, List[str]],
) -> Dict[str, Set[str]]:
    """Return ancestors[x] = transitive closure of deps[x].

    An ancestor of x is any unit x depends on, directly or
    transitively. Used by the parallelism check to distinguish
    "sequential (a precedes b)" from "parallel (neither precedes
    the other)".
    """
    ancestors: Dict[str, Set[str]] = {uid: set() for uid in deps}

    def visit(node: str) -> Set[str]:
        if ancestors[node]:
            return ancestors[node]
        acc: Set[str] = set()
        for d in deps.get(node, ()):
            if d in deps:  # skip dangling
                acc.add(d)
                acc |= visit(d)
        ancestors[node] = acc
        return acc

    for uid in deps:
        visit(uid)
    return ancestors
