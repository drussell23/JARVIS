"""
SessionDiff — cross-session delta primitive.
==============================================

Pure-code structural delta between two :class:`SessionRecord` instances.
Inputs are immutable; output is a frozen :class:`SessionDiff` value type.

Scope
-----

* **Inputs only.** The primitive takes two records the caller has
  already resolved (via :class:`SessionIndex`). No filesystem access.
* **Numeric + enum deltas.** Every numeric field (ops_total,
  ops_applied, cost_spent_usd, ...) gets a ``(left, right, delta)``
  triple. stop_reason / ok_outcome / commit_hash get pair-snapshots.
* **Regression classification.** For each numeric field we know the
  "direction": higher ops_applied is improvement, higher cost_spent
  is regression, etc. We surface two bounded tuples so the REPL can
  render a red/green summary without re-implementing the rule table.

Authority boundary
------------------

* §1 read-only — this module only reads fields; records are frozen.
* §7 fail-closed — missing fields degrade to zero, not exceptions.
* No imports from gate / policy / iron_gate / orchestrator modules.
  Pinned by the extension's graduation test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.session_record import SessionRecord

SESSION_DIFF_SCHEMA_VERSION: str = "session_diff.v1"


# Ordered so REPL rendering stays stable.
_NUMERIC_FIELDS: Tuple[str, ...] = (
    "ops_total",
    "ops_applied",
    "ops_verified_pass",
    "ops_verified_total",
    "duration_s",
    "cost_spent_usd",
    "on_disk_bytes",
)

# Fields where "higher value on the right" counts as improvement.
_HIGHER_IS_BETTER: frozenset = frozenset({
    "ops_total",
    "ops_applied",
    "ops_verified_pass",
    "ops_verified_total",
})


@dataclass(frozen=True)
class FieldDelta:
    name: str
    left: Any
    right: Any
    delta: Optional[float]
    regressed: bool
    improved: bool


@dataclass(frozen=True)
class SessionDiff:
    """Structured delta between two :class:`SessionRecord` instances.

    Never raises. Safe to render even when one or both sides had
    ``parse_error=True`` — the delta is still meaningful for the
    subset of fields that parsed.
    """

    left_session_id: str
    right_session_id: str
    left_parse_error: bool
    right_parse_error: bool
    numeric_deltas: Tuple[FieldDelta, ...]
    stop_reason_pair: Tuple[str, str]
    ok_outcome_pair: Tuple[bool, bool]
    commit_hash_pair: Tuple[str, str]
    has_replay_pair: Tuple[bool, bool]
    regressed_fields: Tuple[str, ...]
    improved_fields: Tuple[str, ...]
    schema_version: str = SESSION_DIFF_SCHEMA_VERSION

    def project(self) -> Dict[str, Any]:
        """JSON-safe bounded projection for SSE / HTTP payloads."""
        return {
            "schema_version": self.schema_version,
            "left_session_id": self.left_session_id,
            "right_session_id": self.right_session_id,
            "left_parse_error": self.left_parse_error,
            "right_parse_error": self.right_parse_error,
            "numeric_deltas": [
                {
                    "name": d.name,
                    "left": d.left,
                    "right": d.right,
                    "delta": d.delta,
                    "regressed": d.regressed,
                    "improved": d.improved,
                }
                for d in self.numeric_deltas
            ],
            "stop_reason_pair": list(self.stop_reason_pair),
            "ok_outcome_pair": list(self.ok_outcome_pair),
            "commit_hash_pair": [
                self.commit_hash_pair[0][:10],
                self.commit_hash_pair[1][:10],
            ],
            "has_replay_pair": list(self.has_replay_pair),
            "regressed_fields": list(self.regressed_fields),
            "improved_fields": list(self.improved_fields),
        }


def diff_records(left: SessionRecord, right: SessionRecord) -> SessionDiff:
    """Pure diff function.

    Handles parse-error records gracefully: numeric fields still
    compare (sentinels are zero), but regression classification is
    only applied when both sides have ``parse_error=False``.
    """
    numeric_deltas: List[FieldDelta] = []
    regressed: List[str] = []
    improved: List[str] = []
    both_parsed = not (left.parse_error or right.parse_error)
    for name in _NUMERIC_FIELDS:
        lv = getattr(left, name, 0)
        rv = getattr(right, name, 0)
        try:
            delta: Optional[float] = float(rv) - float(lv)
        except (TypeError, ValueError):
            delta = None
        is_improved = False
        is_regressed = False
        if both_parsed and delta is not None and delta != 0:
            if name in _HIGHER_IS_BETTER:
                is_improved = delta > 0
                is_regressed = delta < 0
            else:
                is_improved = delta < 0
                is_regressed = delta > 0
        if is_improved:
            improved.append(name)
        if is_regressed:
            regressed.append(name)
        numeric_deltas.append(FieldDelta(
            name=name,
            left=lv,
            right=rv,
            delta=delta,
            regressed=is_regressed,
            improved=is_improved,
        ))
    # ok_outcome flip is a separate classification axis — not numeric.
    if both_parsed and left.ok_outcome and not right.ok_outcome:
        regressed.append("ok_outcome")
    elif both_parsed and right.ok_outcome and not left.ok_outcome:
        improved.append("ok_outcome")
    return SessionDiff(
        left_session_id=left.session_id,
        right_session_id=right.session_id,
        left_parse_error=left.parse_error,
        right_parse_error=right.parse_error,
        numeric_deltas=tuple(numeric_deltas),
        stop_reason_pair=(left.stop_reason, right.stop_reason),
        ok_outcome_pair=(left.ok_outcome, right.ok_outcome),
        commit_hash_pair=(left.commit_hash, right.commit_hash),
        has_replay_pair=(left.has_replay_html, right.has_replay_html),
        regressed_fields=tuple(regressed),
        improved_fields=tuple(improved),
    )


def render_session_diff(diff: SessionDiff) -> str:
    """REPL-friendly rendering.

    Format:
        Session diff
          left  : bt-xxx
          right : bt-yyy
          stop  : 'complete' -> 'cost_cap'
          ok    : True -> False
          commit: abc123 -> def456
          numeric:
            ^ ops_applied       12 -> 15  (+3)
            v cost_spent_usd    0.30 -> 0.42  (+0.12)
              ops_total         10 -> 10  (0)
          regressed: cost_spent_usd, ok_outcome
          improved : ops_applied
    """
    lines: List[str] = [
        "  Session diff",
        f"    left  : {diff.left_session_id}",
        f"    right : {diff.right_session_id}",
        (
            f"    stop  : {diff.stop_reason_pair[0]!r} -> "
            f"{diff.stop_reason_pair[1]!r}"
        ),
        (
            f"    ok    : {diff.ok_outcome_pair[0]} -> "
            f"{diff.ok_outcome_pair[1]}"
        ),
        (
            f"    commit: {diff.commit_hash_pair[0][:10]} -> "
            f"{diff.commit_hash_pair[1][:10]}"
        ),
    ]
    if diff.left_parse_error or diff.right_parse_error:
        lines.append(
            f"    parse_error: {diff.left_parse_error} / "
            f"{diff.right_parse_error}"
        )
    lines.append("    numeric:")
    for d in diff.numeric_deltas:
        if d.regressed:
            marker = "v "
        elif d.improved:
            marker = "^ "
        else:
            marker = "  "
        if d.delta is None:
            delta_str = "-"
        elif isinstance(d.delta, float) and d.delta != int(d.delta):
            delta_str = f"{d.delta:+.4g}"
        else:
            delta_str = f"{d.delta:+g}"
        lines.append(
            f"      {marker}{d.name:<20} {d.left} -> {d.right}  "
            f"({delta_str})"
        )
    if diff.regressed_fields:
        lines.append(
            f"    regressed: {', '.join(diff.regressed_fields)}"
        )
    if diff.improved_fields:
        lines.append(
            f"    improved : {', '.join(diff.improved_fields)}"
        )
    return "\n".join(lines)


__all__ = [
    "SESSION_DIFF_SCHEMA_VERSION",
    "FieldDelta",
    "SessionDiff",
    "diff_records",
    "render_session_diff",
]
