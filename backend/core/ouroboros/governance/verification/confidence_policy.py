"""Gap #2 Slice 1 — Confidence policy substrate primitive.

Pure-stdlib decision primitive for the Confidence Threshold Tuner
arc (Deep Observability §2). Ships the closed-taxonomy vocabulary
+ the total ``compute_policy_diff`` decision function used by the
later slices to gate write proposals routed through
``AdaptationLedger`` + ``MetaAdaptationGovernor``.

Slice 1 is structurally observational: defines + diffs ``Confidence
Policy`` snapshots, classifies each transition as TIGHTEN /
LOOSEN / NO_OP / INVALID, and stamps the canonical
``MonotonicTighteningVerdict`` string from
``adaptation.ledger`` so consumer slices (2-5) can route proposals
through the existing cage without re-deriving the verdict.

## What it does NOT do (later slices)

  * NO ledger writes — Slice 2 registers the surface validator
    that consumes this diff at ``AdaptationLedger.propose``.
  * NO YAML I/O — Slice 3 ships ``adapted_confidence_loader.py``.
  * NO HTTP surface — Slice 4 ships ``ide_policy_router.py``.
  * NO env mutation — the policy reads via the existing accessors
    in ``confidence_monitor.py``; mutations go through Slice 4 +
    MAG, never directly.

## Cage discipline (load-bearing)

Confidence-policy "tightening" is defined per dimension in this
single module so the whole arc has one canonical decision rule:

  ============================  ============  ==================
   dimension                     direction     tighten when ...
  ============================  ============  ==================
   ``floor`` (logprob min)        floor↑       proposed > current
   ``window_k`` (rolling K)       window_k↓    proposed < current
   ``approaching_factor``         factor↑      proposed > current
   ``enforce`` (hard-block bool)  False→True   ON transition only
  ============================  ============  ==================

``approaching_factor`` is intentionally tighten-on-INCREASE
because ``confidence_monitor.py`` evaluates ``APPROACHING_FLOOR``
when ``floor < margin ≤ floor × factor`` — a larger factor widens
the warning band, catching collapses earlier. (factor < 1.0
inverts the semantics; the env accessor floors at 1.0; we mirror
that floor here so an INVALID outcome is structurally raised
rather than silently coerced.)

Any proposal that LOOSENS any one dimension is REJECTED_LOOSEN
even if other dimensions tighten — the cage is conjunctive, never
trades one knob against another. This matches Pass C §4.1: a
proposal must strictly tighten or be no-op on every dimension to
pass the universal cage rule.

## Direct-solve principles

  * **Asynchronous-ready** — pure functions; consumers wrap via
    ``asyncio.to_thread`` if needed. No I/O on the hot path.
  * **Dynamic** — every threshold is env-tunable; nothing is
    hardcoded into the diff logic. Operators can flip the master
    or extend the validator allowlist without code edits.
  * **Adaptive** — diff outcome carries the full per-dimension
    classification so consumers can render targeted UX (which
    knob moved, by how much) rather than a binary pass/fail.
  * **Intelligent** — closed-taxonomy enums for kind + outcome;
    no string comparisons in consumers. Verdict canonical strings
    sourced from ``MonotonicTighteningVerdict`` so cross-surface
    audit queries match by value.
  * **Robust** — every public function NEVER raises. Consumers
    receive a ``PolicyDiff`` with ``outcome=FAILED`` on any
    internal exception; the orchestrator never sees a raise.
  * **No hardcoding** — semantic floors (``approaching_factor >=
    1.0``) sourced from ``confidence_monitor`` accessors; defaults
    routed through the same accessors so Slice 1 stays in sync
    with future env-knob tuning.

## Default-off

``JARVIS_CONFIDENCE_POLICY_ENABLED`` (default ``false`` until
Slice 5 graduation). When off, ``compute_policy_diff`` short-
circuits to ``outcome=DISABLED`` regardless of inputs.

## Authority surface (AST-pinned by Slice 5)

  * Imports: stdlib + ``adaptation.ledger`` (``MonotonicTightening
    Verdict`` only) + ``confidence_monitor`` (env accessors only).
  * MUST NOT import: orchestrator / iron_gate / policy /
    risk_engine / change_engine / tool_executor / providers /
    candidate_generator / semantic_guardian / semantic_firewall /
    scoped_tool_backend / subagent_scheduler.
  * No filesystem I/O, no network, no shell-out, no env
    mutation, no bare eval-family calls.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.adaptation.ledger import (
    MonotonicTighteningVerdict,
)
from backend.core.ouroboros.governance.verification.confidence_monitor import (
    confidence_approaching_factor,
    confidence_floor,
    confidence_monitor_enforce,
    confidence_window_k,
)

logger = logging.getLogger(__name__)


CONFIDENCE_POLICY_SCHEMA_VERSION: str = "confidence_policy.1"


# ---------------------------------------------------------------------------
# Master flag (default-off until Slice 5 graduation)
# ---------------------------------------------------------------------------


def confidence_policy_enabled() -> bool:
    """``JARVIS_CONFIDENCE_POLICY_ENABLED`` (default ``false`` until
    Slice 5). Empty / unset / whitespace = default. Truthy =
    ``1``/``true``/``yes``/``on`` (case-insensitive). Anything else
    = false. NEVER raises."""
    try:
        raw = os.environ.get(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "",
        ).strip().lower()
        if raw == "":
            return False
        return raw in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of policy-change kinds
# ---------------------------------------------------------------------------


class ConfidencePolicyKind(str, enum.Enum):
    """Which dimension a proposal targets. Closed taxonomy:
    consumers MUST handle every value or the AST validator catches
    the omission. Multi-dimension proposals carry the tuple of
    affected kinds in ``PolicyDiff.kinds``.

    ``RAISE_FLOOR``           — ``floor`` increased.
    ``SHRINK_WINDOW``         — ``window_k`` decreased.
    ``WIDEN_APPROACHING``     — ``approaching_factor`` increased
                                 (wider warning band catches
                                 collapses earlier).
    ``ENABLE_ENFORCE``        — ``enforce`` toggled False→True.
    ``DISABLED``              — sentinel for master-off /
                                 short-circuit returns; never
                                 attached to a tightening
                                 proposal."""

    RAISE_FLOOR = "raise_floor"
    SHRINK_WINDOW = "shrink_window"
    WIDEN_APPROACHING = "widen_approaching"
    ENABLE_ENFORCE = "enable_enforce"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of diff outcomes
# ---------------------------------------------------------------------------


class ConfidencePolicyOutcome(str, enum.Enum):
    """Outcome of one ``compute_policy_diff`` invocation. Closed
    taxonomy.

    ``APPLIED``        — diff is a tightening (or no-op); MAY be
                          routed to ``AdaptationLedger.propose``
                          by Slice 2.
    ``REJECTED_LOOSEN`` — at least one dimension would loosen;
                          proposal blocked by the universal cage
                          rule (Pass C §4.1).
    ``INVALID``        — proposal violates a structural floor
                          (e.g. ``approaching_factor < 1.0``,
                          ``floor`` outside [0.0, 1.0],
                          ``window_k < 1``). Never persisted.
    ``DISABLED``       — master flag off; no decision rendered.
    ``FAILED``         — defensive sentinel; consumer should
                          neither apply nor reject — log + drop."""

    APPLIED = "applied"
    REJECTED_LOOSEN = "rejected_loosen"
    INVALID = "invalid"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Structural floors — sourced from confidence_monitor env accessors
# ---------------------------------------------------------------------------
#
# These mirror the floors that the env accessors enforce internally
# (e.g. ``confidence_approaching_factor`` floors at 1.0). Centralising
# them here lets the diff logic raise INVALID structurally rather
# than silently coercing — the operator sees the rejection reason
# instead of receiving a doctored "applied" result with a coerced
# value.

_FLOOR_MIN: float = 0.0
_FLOOR_MAX: float = 1.0
_WINDOW_K_MIN: int = 1
_APPROACHING_FACTOR_MIN: float = 1.0


# ---------------------------------------------------------------------------
# Frozen ConfidencePolicy dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidencePolicy:
    """Snapshot of the four operator-visible confidence knobs.

    Frozen so audit-trail integrity holds when the same instance
    flows through multiple consumers (validator, ledger, SSE
    publisher, IDE webview projection).

    Construction surfaces:
      * ``ConfidencePolicy(...)``           — direct (tests).
      * ``ConfidencePolicy.from_environment()`` — current effective
                                                  state.
      * ``ConfidencePolicy.from_dict(d)``   — JSON / YAML load."""

    floor: float
    window_k: int
    approaching_factor: float
    enforce: bool
    schema_version: str = CONFIDENCE_POLICY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "floor": float(self.floor),
            "window_k": int(self.window_k),
            "approaching_factor": float(self.approaching_factor),
            "enforce": bool(self.enforce),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any],
    ) -> "ConfidencePolicy":
        """Build a policy from a JSON/YAML mapping. Missing fields
        fall through to the env-accessor default. NEVER raises;
        bad fields fall back to the env default for that dimension."""
        try:
            if not isinstance(data, Mapping):
                return cls.from_environment()
            return cls(
                floor=_safe_float(
                    data.get("floor"), default=confidence_floor(),
                ),
                window_k=_safe_int(
                    data.get("window_k"), default=confidence_window_k(),
                ),
                approaching_factor=_safe_float(
                    data.get("approaching_factor"),
                    default=confidence_approaching_factor(),
                ),
                enforce=_safe_bool(
                    data.get("enforce"),
                    default=confidence_monitor_enforce(),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return cls.from_environment()

    @classmethod
    def from_environment(cls) -> "ConfidencePolicy":
        """Snapshot the current env-driven effective policy. Reads
        the same accessors ``ConfidenceMonitor`` uses, so the
        snapshot reflects the live runtime view. NEVER raises;
        each accessor already provides its own defensive default."""
        try:
            return cls(
                floor=confidence_floor(),
                window_k=confidence_window_k(),
                approaching_factor=confidence_approaching_factor(),
                enforce=confidence_monitor_enforce(),
            )
        except Exception:  # noqa: BLE001 — defensive
            # Last-resort hard defaults if every accessor failed
            # simultaneously (extremely unlikely; defensive only).
            return cls(
                floor=0.05,
                window_k=16,
                approaching_factor=1.5,
                enforce=False,
            )

    def state_hash(self) -> str:
        """sha256:<hex64> of the canonical JSON projection. Stable
        across Python invocations (sorted keys, fixed separators).
        Consumed by Slice 2's surface validator to verify proposal
        provenance and by Slice 4 to dedup rapid resubmits."""
        try:
            payload = json.dumps(
                self.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return "sha256:" + hashlib.sha256(payload).hexdigest()
        except Exception:  # noqa: BLE001 — defensive
            return "sha256:" + ("0" * 64)


# ---------------------------------------------------------------------------
# Frozen PolicyDiff outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDiff:
    """Result of a ``compute_policy_diff`` invocation.

    ``kinds`` carries the per-dimension classification — empty
    tuple means no-op (current == proposed). Multi-dimension
    proposals list every kind that moved.

    ``monotonic_tightening_verdict`` is the canonical string from
    ``adaptation.ledger.MonotonicTighteningVerdict`` — Slice 2's
    surface validator forwards this value verbatim to the ledger
    so cross-surface audit queries can match by value."""

    outcome: ConfidencePolicyOutcome
    kinds: Tuple[ConfidencePolicyKind, ...]
    monotonic_tightening_verdict: str
    detail: str
    current_hash: str
    proposed_hash: str
    schema_version: str = CONFIDENCE_POLICY_SCHEMA_VERSION
    per_dimension_detail: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "kinds": [k.value for k in self.kinds],
            "monotonic_tightening_verdict": (
                self.monotonic_tightening_verdict
            ),
            "detail": self.detail,
            "current_hash": self.current_hash,
            "proposed_hash": self.proposed_hash,
            "schema_version": self.schema_version,
            "per_dimension_detail": list(self.per_dimension_detail),
        }


# ---------------------------------------------------------------------------
# Public: compute_policy_diff (total decision function)
# ---------------------------------------------------------------------------


def compute_policy_diff(
    *,
    current: ConfidencePolicy,
    proposed: ConfidencePolicy,
    enabled_override: Optional[bool] = None,
) -> PolicyDiff:
    """Pure decision function. Compares two ``ConfidencePolicy``
    snapshots and returns a ``PolicyDiff`` with:

      * ``outcome``  — closed taxonomy (APPLIED / REJECTED_LOOSEN /
        INVALID / DISABLED / FAILED).
      * ``kinds``    — tuple of every dimension that moved (empty
        for no-op proposals).
      * ``monotonic_tightening_verdict`` — canonical string from
        ``MonotonicTighteningVerdict``. ``PASSED`` on APPLIED;
        ``REJECTED_WOULD_LOOSEN`` on REJECTED_LOOSEN; the failure
        sentinel string on the others.

    Decision tree (top-down, first match wins):

      1. Master flag off → ``DISABLED``.
      2. ``current``/``proposed`` not ``ConfidencePolicy`` →
         ``FAILED``.
      3. Any structural floor violated on ``proposed`` → ``INVALID``.
      4. Any dimension would loosen → ``REJECTED_LOOSEN``.
      5. No dimension moved → ``APPLIED`` with empty ``kinds``
         (no-op proposals are not rejected; they just don't
         materialize a state change).
      6. Otherwise → ``APPLIED`` with the moved dimensions.

    ``enabled_override`` short-circuits the master-flag check —
    intended for tests; production callers leave it ``None``.

    NEVER raises. Returns a ``PolicyDiff`` on every code path."""
    try:
        is_enabled = (
            enabled_override
            if enabled_override is not None
            else confidence_policy_enabled()
        )
        if not is_enabled:
            return PolicyDiff(
                outcome=ConfidencePolicyOutcome.DISABLED,
                kinds=(),
                monotonic_tightening_verdict="disabled",
                detail="master_flag_off",
                current_hash="",
                proposed_hash="",
            )

        if not isinstance(current, ConfidencePolicy):
            return _failed(
                f"current_not_confidence_policy:"
                f"{type(current).__name__}",
            )
        if not isinstance(proposed, ConfidencePolicy):
            return _failed(
                f"proposed_not_confidence_policy:"
                f"{type(proposed).__name__}",
            )

        current_h = current.state_hash()
        proposed_h = proposed.state_hash()

        # 3. Structural floor checks on proposed (current is taken
        # as authoritative — even if it violates a floor we still
        # let proposals tighten away from it).
        invalid_reasons = _validate_structural_floors(proposed)
        if invalid_reasons:
            return PolicyDiff(
                outcome=ConfidencePolicyOutcome.INVALID,
                kinds=(),
                monotonic_tightening_verdict=(
                    "invalid:structural_floor"
                ),
                detail="; ".join(invalid_reasons)[:240],
                current_hash=current_h,
                proposed_hash=proposed_h,
                per_dimension_detail=tuple(invalid_reasons),
            )

        # 4. Per-dimension classification (parallel evaluation;
        # any LOOSEN short-circuits to REJECTED_LOOSEN).
        moved: list = []
        loosen_reasons: list = []
        per_dim: list = []

        floor_kind, floor_detail = _classify_floor(
            current.floor, proposed.floor,
        )
        per_dim.append(floor_detail)
        if floor_kind == _CMP_LOOSEN:
            loosen_reasons.append(floor_detail)
        elif floor_kind == _CMP_TIGHTEN:
            moved.append(ConfidencePolicyKind.RAISE_FLOOR)

        window_kind, window_detail = _classify_window(
            current.window_k, proposed.window_k,
        )
        per_dim.append(window_detail)
        if window_kind == _CMP_LOOSEN:
            loosen_reasons.append(window_detail)
        elif window_kind == _CMP_TIGHTEN:
            moved.append(ConfidencePolicyKind.SHRINK_WINDOW)

        approaching_kind, approaching_detail = _classify_approaching(
            current.approaching_factor,
            proposed.approaching_factor,
        )
        per_dim.append(approaching_detail)
        if approaching_kind == _CMP_LOOSEN:
            loosen_reasons.append(approaching_detail)
        elif approaching_kind == _CMP_TIGHTEN:
            moved.append(ConfidencePolicyKind.WIDEN_APPROACHING)

        enforce_kind, enforce_detail = _classify_enforce(
            current.enforce, proposed.enforce,
        )
        per_dim.append(enforce_detail)
        if enforce_kind == _CMP_LOOSEN:
            loosen_reasons.append(enforce_detail)
        elif enforce_kind == _CMP_TIGHTEN:
            moved.append(ConfidencePolicyKind.ENABLE_ENFORCE)

        # Conjunctive cage rule: ANY loosening dimension blocks
        # the whole proposal. No trade-offs across dimensions.
        if loosen_reasons:
            return PolicyDiff(
                outcome=ConfidencePolicyOutcome.REJECTED_LOOSEN,
                kinds=(),
                monotonic_tightening_verdict=(
                    MonotonicTighteningVerdict
                    .REJECTED_WOULD_LOOSEN.value
                ),
                detail="; ".join(loosen_reasons)[:240],
                current_hash=current_h,
                proposed_hash=proposed_h,
                per_dimension_detail=tuple(per_dim),
            )

        # APPLIED — empty kinds means no-op, populated means
        # tightening. Both are eligible for ledger persistence
        # (operator may want to record a no-op as evidence of
        # an intentional snapshot).
        return PolicyDiff(
            outcome=ConfidencePolicyOutcome.APPLIED,
            kinds=tuple(moved),
            monotonic_tightening_verdict=(
                MonotonicTighteningVerdict.PASSED.value
            ),
            detail=(
                "no_op_snapshot"
                if not moved
                else f"tighten:{','.join(k.value for k in moved)}"
            ),
            current_hash=current_h,
            proposed_hash=proposed_h,
            per_dimension_detail=tuple(per_dim),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[ConfidencePolicy] compute_policy_diff raised: %s",
            exc,
        )
        return _failed(f"compute_failed:{type(exc).__name__}")


# ---------------------------------------------------------------------------
# Internal helpers (closed comparator vocabulary; stdlib only)
# ---------------------------------------------------------------------------


_CMP_TIGHTEN: str = "tighten"
_CMP_LOOSEN: str = "loosen"
_CMP_NO_OP: str = "no_op"


def _failed(detail: str) -> PolicyDiff:
    """Build a FAILED PolicyDiff with the supplied detail string."""
    return PolicyDiff(
        outcome=ConfidencePolicyOutcome.FAILED,
        kinds=(),
        monotonic_tightening_verdict="failed",
        detail=detail[:240],
        current_hash="",
        proposed_hash="",
    )


def _safe_float(value: Any, *, default: float) -> float:
    try:
        v = float(value)
        if not math.isfinite(v):
            return float(default)
        return v
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _safe_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    try:
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        return bool(default)
    except Exception:  # noqa: BLE001 — defensive
        return bool(default)


def _validate_structural_floors(
    p: ConfidencePolicy,
) -> Tuple[str, ...]:
    """Returns a tuple of human-readable violation strings. Empty
    tuple == valid. NEVER raises."""
    out: list = []
    try:
        if not math.isfinite(p.floor):
            out.append("floor:non_finite")
        elif p.floor < _FLOOR_MIN or p.floor > _FLOOR_MAX:
            out.append(
                f"floor:{p.floor}_outside_[{_FLOOR_MIN},{_FLOOR_MAX}]"
            )
        if p.window_k < _WINDOW_K_MIN:
            out.append(
                f"window_k:{p.window_k}_below_{_WINDOW_K_MIN}"
            )
        if not math.isfinite(p.approaching_factor):
            out.append("approaching_factor:non_finite")
        elif p.approaching_factor < _APPROACHING_FACTOR_MIN:
            out.append(
                f"approaching_factor:{p.approaching_factor}_"
                f"below_{_APPROACHING_FACTOR_MIN}"
            )
    except Exception:  # noqa: BLE001 — defensive
        out.append("validation_raised")
    return tuple(out)


def _classify_floor(
    current: float, proposed: float,
) -> Tuple[str, str]:
    """floor↑ = TIGHTEN. NEVER raises."""
    try:
        c = float(current)
        p = float(proposed)
        if math.isclose(c, p, rel_tol=1e-9, abs_tol=1e-12):
            return (_CMP_NO_OP, f"floor:{c}→{p} no_op")
        if p > c:
            return (_CMP_TIGHTEN, f"floor:{c}→{p} tighten")
        return (_CMP_LOOSEN, f"floor:{c}→{p} loosen")
    except Exception:  # noqa: BLE001 — defensive
        return (_CMP_NO_OP, "floor:compare_failed")


def _classify_window(
    current: int, proposed: int,
) -> Tuple[str, str]:
    """window_k↓ = TIGHTEN (smaller window = faster reaction).
    NEVER raises."""
    try:
        c = int(current)
        p = int(proposed)
        if c == p:
            return (_CMP_NO_OP, f"window_k:{c}→{p} no_op")
        if p < c:
            return (_CMP_TIGHTEN, f"window_k:{c}→{p} tighten")
        return (_CMP_LOOSEN, f"window_k:{c}→{p} loosen")
    except Exception:  # noqa: BLE001 — defensive
        return (_CMP_NO_OP, "window_k:compare_failed")


def _classify_approaching(
    current: float, proposed: float,
) -> Tuple[str, str]:
    """approaching_factor↑ = TIGHTEN (wider warning band catches
    collapses earlier — see module docstring). NEVER raises."""
    try:
        c = float(current)
        p = float(proposed)
        if math.isclose(c, p, rel_tol=1e-9, abs_tol=1e-12):
            return (_CMP_NO_OP, f"approaching_factor:{c}→{p} no_op")
        if p > c:
            return (
                _CMP_TIGHTEN,
                f"approaching_factor:{c}→{p} tighten",
            )
        return (
            _CMP_LOOSEN,
            f"approaching_factor:{c}→{p} loosen",
        )
    except Exception:  # noqa: BLE001 — defensive
        return (_CMP_NO_OP, "approaching_factor:compare_failed")


def _classify_enforce(
    current: bool, proposed: bool,
) -> Tuple[str, str]:
    """enforce False→True = TIGHTEN. True→False = LOOSEN. NEVER
    raises."""
    try:
        c = bool(current)
        p = bool(proposed)
        if c == p:
            return (_CMP_NO_OP, f"enforce:{c}→{p} no_op")
        if (not c) and p:
            return (_CMP_TIGHTEN, f"enforce:False→True tighten")
        return (_CMP_LOOSEN, f"enforce:True→False loosen")
    except Exception:  # noqa: BLE001 — defensive
        return (_CMP_NO_OP, "enforce:compare_failed")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CONFIDENCE_POLICY_SCHEMA_VERSION",
    "ConfidencePolicy",
    "ConfidencePolicyKind",
    "ConfidencePolicyOutcome",
    "PolicyDiff",
    "compute_policy_diff",
    "confidence_policy_enabled",
]
