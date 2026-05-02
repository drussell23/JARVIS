"""Q2 Slice 6 — Deterministic state-diff between two DAG records.

Pure-stdlib recursive diff over the free-form ``DecisionRecord.to_dict()``
shape returned by ``handle_dag_record``. Closes the §4 Q2 gap:
operators previously had no first-class way to compare two records
in the CausalityDAG without manual replay.

## What this is

A *projection* — pure read over caller-supplied input dicts.
Returns a frozen ``RecordDiff`` with:

  * Per-leaf ``FieldChange`` records (path, kind, value_a, value_b).
    Closed 3-value ``ChangeKind`` taxonomy: ADDED / REMOVED /
    MODIFIED. UNCHANGED leaves are NOT emitted (would unboundedly
    inflate the diff for large records).
  * Aggregate counts: ``fields_total`` (total leaves traversed) +
    ``fields_changed`` (sum of ADDED + REMOVED + MODIFIED).
  * Path representation: tuple of string segments — JSON-pointer-
    style for nested keys (``("a", "b", "c")`` for ``a.b.c``).
    Lists use the index as a string segment (``("results", "0",
    "ts_ns")`` for ``results[0].ts_ns``).

## Cage discipline

  * **Read-only** — frozen records flow out; no mutation surface.
  * **Bounded** — recursion depth capped by
    ``JARVIS_DAG_DIFF_MAX_DEPTH`` (default 8); leaf count capped
    by ``JARVIS_DAG_DIFF_MAX_LEAVES`` (default 1000). Both protect
    against pathological nested inputs.
  * **NEVER raises** — every public function returns a structured
    ``RecordDiff`` on every code path. ``outcome=FAILED`` carries
    a diagnostic string for caller-visible degradation.
  * **Pure stdlib** — no model inference, no FX dependency, no
    governance-layer imports.

## Authority surface (AST-pinned by Slice 6 graduation)

  * Imports: stdlib ONLY. No governance / orchestrator / iron_gate
    / policy / risk_engine / change_engine / tool_executor /
    providers / candidate_generator / semantic_guardian /
    semantic_firewall / scoped_tool_backend / subagent_scheduler /
    causality_dag / dag_navigation / decision_runtime imports.
  * No filesystem I/O, no subprocess, no env mutation, no network.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


DAG_RECORD_DIFF_SCHEMA_VERSION: str = "dag_record_diff.1"


# ---------------------------------------------------------------------------
# Bounded computation knobs (env-tunable)
# ---------------------------------------------------------------------------


_DEFAULT_MAX_DEPTH: int = 8
_DEFAULT_MAX_LEAVES: int = 1000


def _max_depth() -> int:
    """``JARVIS_DAG_DIFF_MAX_DEPTH`` (default 8, floor 1, ceiling 32).

    Recursion stops at this depth; deeper subtrees are compared as
    opaque values (rendered via ``repr``-truncation). NEVER raises."""
    raw = os.environ.get(
        "JARVIS_DAG_DIFF_MAX_DEPTH", "",
    ).strip()
    if not raw:
        return _DEFAULT_MAX_DEPTH
    try:
        v = int(raw)
        return max(1, min(32, v))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_DEPTH


def _max_leaves() -> int:
    """``JARVIS_DAG_DIFF_MAX_LEAVES`` (default 1000, floor 1,
    ceiling 100_000). Stop emitting changes after this many leaves
    have been classified — the diff result is then truncated and
    flagged. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_DAG_DIFF_MAX_LEAVES", "",
    ).strip()
    if not raw:
        return _DEFAULT_MAX_LEAVES
    try:
        v = int(raw)
        return max(1, min(100_000, v))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_LEAVES


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class ChangeKind(str, enum.Enum):
    """Closed 3-value taxonomy of leaf-level diff outcomes.

    UNCHANGED leaves are NOT emitted — keeping the diff bounded
    over large mostly-unchanged records. Consumers can compute
    ``unchanged = fields_total - fields_changed`` from the
    aggregate counts.

    ``ADDED``    — key/path present in B, absent in A.
    ``REMOVED``  — key/path present in A, absent in B.
    ``MODIFIED`` — key/path present in both; values differ."""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


class DiffOutcome(str, enum.Enum):
    """Closed 5-value outcome taxonomy. Mirror of other Q2
    substrate enums (TopologyOutcome, ConfidencePolicyOutcome).

    ``OK``        — diff computed; ``changes`` populated (possibly
                     empty if the records are identical).
    ``EMPTY``     — both inputs were empty/None; no fields to
                     diff. Distinct from OK so consumers can
                     render "nothing to compare" UX.
    ``TRUNCATED`` — ``fields_changed`` reached ``max_leaves`` cap;
                     diff is partial. Operator should narrow scope.
    ``INVALID``   — non-Mapping input(s); cannot diff.
    ``FAILED``    — defensive sentinel; consumer should render
                     error state."""

    OK = "ok"
    EMPTY = "empty"
    TRUNCATED = "truncated"
    INVALID = "invalid"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Frozen records
# ---------------------------------------------------------------------------


_REPR_MAX_LEN: int = 240


def _safe_repr(value: Any) -> str:
    """Bounded ``repr`` for emission. Truncates to ``_REPR_MAX_LEN``
    chars + ellipsis. NEVER raises."""
    try:
        s = repr(value)
    except Exception:  # noqa: BLE001 — defensive
        s = f"<unrepr:{type(value).__name__}>"
    if len(s) > _REPR_MAX_LEN:
        return s[:_REPR_MAX_LEN] + "..."
    return s


@dataclass(frozen=True)
class FieldChange:
    """One leaf-level diff entry. Frozen so downstream consumers
    cannot mutate audit state."""

    path: Tuple[str, ...]
    kind: ChangeKind
    # ``value_a_repr`` / ``value_b_repr`` are bounded ``repr``
    # strings — keeps the wire payload small + hides un-JSONable
    # types behind a representable form.
    value_a_repr: str = ""
    value_b_repr: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": list(self.path),
            "kind": self.kind.value,
            "value_a_repr": self.value_a_repr,
            "value_b_repr": self.value_b_repr,
        }


@dataclass(frozen=True)
class RecordDiff:
    """Top-level diff result. ``outcome`` is the closed-taxonomy
    discriminant; consumers branch on it before reading
    ``changes``."""

    outcome: DiffOutcome
    record_id_a: str
    record_id_b: str
    changes: Tuple[FieldChange, ...]
    fields_total: int
    fields_changed: int
    detail: str = ""
    schema_version: str = DAG_RECORD_DIFF_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "record_id_a": self.record_id_a,
            "record_id_b": self.record_id_b,
            "changes": [c.to_dict() for c in self.changes],
            "fields_total": self.fields_total,
            "fields_changed": self.fields_changed,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Recursive diff core (private, pure)
# ---------------------------------------------------------------------------


def _diff_walk(
    a: Any, b: Any, path: Tuple[str, ...],
    *,
    depth_remaining: int,
    leaves_emitted: List[int],  # mutable counter passed by ref
    max_leaves: int,
    out: List[FieldChange],
) -> int:
    """Recursive helper. Returns ``fields_total`` traversed below
    this subtree. Mutates ``out`` + ``leaves_emitted[0]``."""
    # Hit the leaf cap → stop emitting (but keep counting traversal).
    if leaves_emitted[0] >= max_leaves:
        return 0

    # Depth cap → treat as opaque values
    if depth_remaining <= 0:
        if a != b and leaves_emitted[0] < max_leaves:
            out.append(FieldChange(
                path=path, kind=ChangeKind.MODIFIED,
                value_a_repr=_safe_repr(a),
                value_b_repr=_safe_repr(b),
            ))
            leaves_emitted[0] += 1
        return 1

    # Both Mappings: recurse on union of keys
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        keys = sorted(set(a.keys()) | set(b.keys()), key=str)
        total = 0
        for k in keys:
            sub_path = path + (str(k),)
            in_a = k in a
            in_b = k in b
            if in_a and not in_b:
                if leaves_emitted[0] < max_leaves:
                    out.append(FieldChange(
                        path=sub_path, kind=ChangeKind.REMOVED,
                        value_a_repr=_safe_repr(a[k]),
                    ))
                    leaves_emitted[0] += 1
                total += 1
            elif in_b and not in_a:
                if leaves_emitted[0] < max_leaves:
                    out.append(FieldChange(
                        path=sub_path, kind=ChangeKind.ADDED,
                        value_b_repr=_safe_repr(b[k]),
                    ))
                    leaves_emitted[0] += 1
                total += 1
            else:
                total += _diff_walk(
                    a[k], b[k], sub_path,
                    depth_remaining=depth_remaining - 1,
                    leaves_emitted=leaves_emitted,
                    max_leaves=max_leaves,
                    out=out,
                )
        return max(1, total)

    # Both lists: recurse element-by-element by index
    if isinstance(a, list) and isinstance(b, list):
        max_len = max(len(a), len(b))
        if max_len == 0:
            return 1
        total = 0
        for i in range(max_len):
            sub_path = path + (str(i),)
            if i < len(a) and i >= len(b):
                if leaves_emitted[0] < max_leaves:
                    out.append(FieldChange(
                        path=sub_path, kind=ChangeKind.REMOVED,
                        value_a_repr=_safe_repr(a[i]),
                    ))
                    leaves_emitted[0] += 1
                total += 1
            elif i >= len(a) and i < len(b):
                if leaves_emitted[0] < max_leaves:
                    out.append(FieldChange(
                        path=sub_path, kind=ChangeKind.ADDED,
                        value_b_repr=_safe_repr(b[i]),
                    ))
                    leaves_emitted[0] += 1
                total += 1
            else:
                total += _diff_walk(
                    a[i], b[i], sub_path,
                    depth_remaining=depth_remaining - 1,
                    leaves_emitted=leaves_emitted,
                    max_leaves=max_leaves,
                    out=out,
                )
        return max(1, total)

    # Type mismatch OR scalar leaf: compare directly
    if a == b:
        return 1
    if leaves_emitted[0] < max_leaves:
        out.append(FieldChange(
            path=path if path else ("$",),
            kind=ChangeKind.MODIFIED,
            value_a_repr=_safe_repr(a),
            value_b_repr=_safe_repr(b),
        ))
        leaves_emitted[0] += 1
    return 1


# ---------------------------------------------------------------------------
# Public: compute_record_diff
# ---------------------------------------------------------------------------


def compute_record_diff(
    *,
    record_a: Any,
    record_b: Any,
    record_id_a: str = "",
    record_id_b: str = "",
    max_depth: Optional[int] = None,
    max_leaves: Optional[int] = None,
) -> RecordDiff:
    """Compute the deterministic state-diff between two records.

    Decision tree:

      1. Both inputs ``None`` / empty Mappings → ``EMPTY``.
      2. Either input is non-Mapping (top-level) → ``INVALID``.
      3. Recurse via ``_diff_walk`` over the union of keys.
      4. If the leaf-emission cap was hit → ``TRUNCATED``.
      5. Otherwise → ``OK``.

    NEVER raises. Returns a ``RecordDiff`` on every code path."""
    eff_depth = (
        max(1, min(32, int(max_depth)))
        if max_depth is not None
        else _max_depth()
    )
    eff_leaves = (
        max(1, min(100_000, int(max_leaves)))
        if max_leaves is not None
        else _max_leaves()
    )
    try:
        # 1. Empty/None → EMPTY
        a_empty = record_a is None or (
            isinstance(record_a, Mapping) and not record_a
        )
        b_empty = record_b is None or (
            isinstance(record_b, Mapping) and not record_b
        )
        if a_empty and b_empty:
            return RecordDiff(
                outcome=DiffOutcome.EMPTY,
                record_id_a=record_id_a,
                record_id_b=record_id_b,
                changes=(), fields_total=0, fields_changed=0,
                detail="both inputs empty",
            )

        # 2. Type check on top-level
        if not (
            (isinstance(record_a, Mapping) or record_a is None)
            and (isinstance(record_b, Mapping) or record_b is None)
        ):
            return RecordDiff(
                outcome=DiffOutcome.INVALID,
                record_id_a=record_id_a,
                record_id_b=record_id_b,
                changes=(), fields_total=0, fields_changed=0,
                detail=(
                    f"non-Mapping input(s): "
                    f"a={type(record_a).__name__} "
                    f"b={type(record_b).__name__}"
                ),
            )

        # 3. Recurse — treat None as empty mapping for traversal
        a_norm: Mapping = record_a if isinstance(record_a, Mapping) else {}
        b_norm: Mapping = record_b if isinstance(record_b, Mapping) else {}

        out: List[FieldChange] = []
        leaves_emitted = [0]
        total = _diff_walk(
            a_norm, b_norm, (),
            depth_remaining=eff_depth,
            leaves_emitted=leaves_emitted,
            max_leaves=eff_leaves,
            out=out,
        )

        # 4. Truncation check — emitted hit the cap
        if leaves_emitted[0] >= eff_leaves:
            return RecordDiff(
                outcome=DiffOutcome.TRUNCATED,
                record_id_a=record_id_a,
                record_id_b=record_id_b,
                changes=tuple(out),
                fields_total=total,
                fields_changed=leaves_emitted[0],
                detail=(
                    f"emitted {leaves_emitted[0]} changes "
                    f"(cap {eff_leaves}); operator should narrow scope"
                ),
            )

        return RecordDiff(
            outcome=DiffOutcome.OK,
            record_id_a=record_id_a,
            record_id_b=record_id_b,
            changes=tuple(out),
            fields_total=total,
            fields_changed=leaves_emitted[0],
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[DAGRecordDiff] compute_record_diff raised: %s", exc,
        )
        return RecordDiff(
            outcome=DiffOutcome.FAILED,
            record_id_a=record_id_a,
            record_id_b=record_id_b,
            changes=(), fields_total=0, fields_changed=0,
            detail=f"compute_failed:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DAG_RECORD_DIFF_SCHEMA_VERSION",
    "ChangeKind",
    "DiffOutcome",
    "FieldChange",
    "RecordDiff",
    "compute_record_diff",
]
