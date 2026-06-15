"""Deterministic shadow-vs-legacy alignment evaluator (Unit B).

Pure functions: no IO, no LLM, no imports of the store or orchestrator.
Every function returns a structured ``Alignment`` even on malformed
input — malformed maps to ``aligned=False`` (the conservative default
that BLOCKS graduation rather than risking a false promotion).
"""
from __future__ import annotations

from dataclasses import dataclass

_BLOCK = "BLOCK"
_ALLOW = "ALLOW"
_BLOCKING_TIERS = frozenset({"APPROVAL_REQUIRED", "BLOCKED"})


@dataclass(frozen=True)
class Alignment:
    aligned: bool
    reason: str  # "" when aligned; divergence/malformed detail otherwise


def _legacy_review_binary(legacy: dict) -> str:
    tier = str(legacy.get("risk_tier", "")).upper()
    hard = bool(legacy.get("semantic_guard_hard", False))
    if hard or tier in _BLOCKING_TIERS:
        return _BLOCK
    return _ALLOW


def _shadow_review_binary(shadow: dict) -> str:
    agg = str(shadow.get("aggregate", "")).lower()
    # reservations are advisory -> ALLOW; only outright reject BLOCKs.
    return _BLOCK if agg == "reject" else _ALLOW


def evaluate_review(legacy: object, shadow: object) -> Alignment:
    if not isinstance(legacy, dict) or not isinstance(shadow, dict):
        return Alignment(False, "malformed:non_dict_input")
    if "risk_tier" not in legacy or "aggregate" not in shadow:
        return Alignment(False, "malformed:missing_keys")
    lb = _legacy_review_binary(legacy)
    sb = _shadow_review_binary(shadow)
    if lb == sb:
        return Alignment(True, "")
    return Alignment(False, f"shadow={sb} legacy={lb}")


def _has_cycle(units: list) -> bool:
    """Kahn's algorithm — True if any cycle remains."""
    ids = {u["id"] for u in units}
    indeg = {u["id"]: 0 for u in units}
    adj: dict = {u["id"]: [] for u in units}
    for u in units:
        for dep in u.get("deps", []):
            if dep in ids:  # deps on external/unknown units are treated as pre-satisfied
                adj[dep].append(u["id"])
                indeg[u["id"]] += 1
    queue = [i for i, d in indeg.items() if d == 0]
    visited = 0
    while queue:
        n = queue.pop()
        visited += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return visited != len(units)


def _owned_path_overlap(units: list) -> str:
    seen: dict = {}
    for u in units:
        for p in u.get("owned_paths", []):
            if p in seen and seen[p] != u["id"]:
                return p
            seen[p] = u["id"]
    return ""


def evaluate_plan(legacy_flat: object, shadow_dag: object) -> Alignment:
    if not isinstance(legacy_flat, list) or not isinstance(shadow_dag, dict):
        return Alignment(False, "malformed:non_collection_input")
    units = shadow_dag.get("units")
    if not isinstance(units, list) or not all(
        isinstance(u, dict) and "id" in u for u in units
    ):
        return Alignment(False, "malformed:bad_units")

    # 1. Coverage — DAG must touch every legacy task (extra is OK).
    dag_paths = set()
    for u in units:
        dag_paths.update(u.get("owned_paths", []))
    dropped = [t for t in legacy_flat if t not in dag_paths]
    if dropped:
        return Alignment(False, "dropped_tasks:" + ",".join(sorted(dropped)))

    # 2. Acyclicity.
    if _has_cycle(units):
        return Alignment(False, "cyclical_dag")

    # 3. Disjoint ownership.
    overlap = _owned_path_overlap(units)
    if overlap:
        return Alignment(False, "owned_path_overlap:" + overlap)

    return Alignment(True, "")
