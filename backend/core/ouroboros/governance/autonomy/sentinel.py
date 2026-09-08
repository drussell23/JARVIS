"""Autonomous Sentinel Mode — the organism runs; the cockpit observes.

The cockpit was a REPL: a human typed ``/goal sanction``, watched, and cleared
the Iron Gate by hand. That is a copilot. This module is the other shape — the
organism finds its own work, proves it, applies it, and the TUI is a window
onto that rather than a control surface it waits on.

Two questions have to be answered without a human, and they are different
questions, so they are answered separately:

**Is this candidate good enough?** — :func:`spec_compliance_verdict`. Composed
entirely from gates that already exist and already have to pass: the
differential verdict (no regression the candidate caused), SemanticGuardian
(no detections), and the capability assurance (the op ran at the fidelity the
envelope promised). Nothing new is invented to judge code; a fourth opinion
that only the Sentinel consults would be a second definition of "correct".

**Is this op one a machine may approve at all?** — :func:`auto_approval_verdict`.
That is a governance question, not a quality one, and no compliance score may
answer it. It is decided by RISK TIER against an explicit ceiling.

## The tier ceiling, and the floor under it

The operator's model: green and yellow run unattended; orange is theirs to
decide; **red always stops for a human**. So the ceiling is configurable
(``JARVIS_SENTINEL_AUTO_APPROVE_MAX_TIER``, default ``APPROVAL_REQUIRED``) and
the floor is not:

    BLOCKED, Order-2 cognitive self-modification, a recursion-bound breach, or
    any op touching the governance / cage substrate ALWAYS escalates, whatever
    the ceiling says and whatever the compliance score is.

That floor is ``layer4_roadmap_authority.is_safety_operation`` — reused, not
restated, because a second copy of a safety predicate is how the two come to
disagree about the same op.

This module deliberately does NOT call ``may_suppress_approval``: that helper
folds the floor and layer4's own stricter tier policy together, and layer4
classes ``APPROVAL_REQUIRED`` as un-suppressible. Honouring the operator's
ceiling therefore means composing the floor directly and stating the tier
policy here, in the open, rather than routing around a helper whose answer for
orange is fixed. The difference is recorded because a policy difference that
is not visible is a policy difference nobody can audit.

## Fail-closed, everywhere

Every unknown answers NO. An unreadable tier, a missing validation result, an
exception inside the assurance itself — all of them refuse auto-approval and
fall through to the human. The cost of a wrong NO is a prompt; the cost of a
wrong YES is an unreviewed commit.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.Sentinel")

__all__ = [
    "AutoApprovalVerdict",
    "ComplianceVerdict",
    "auto_approval_verdict",
    "sentinel_enabled",
    "spec_compliance_verdict",
    "tier_ceiling",
]

_ENV_SENTINEL = "JARVIS_SENTINEL_MODE_ENABLED"
_ENV_MAX_TIER = "JARVIS_SENTINEL_AUTO_APPROVE_MAX_TIER"

#: Ascending order of autonomy cost. A tier may be auto-approved when its
#: index is <= the ceiling's index. BLOCKED has no index above it by design:
#: it is the floor, not the top of the scale.
_TIER_ORDER: Tuple[str, ...] = (
    "SAFE_AUTO",           # green
    "NOTIFY_APPLY",        # yellow
    "APPROVAL_REQUIRED",   # orange
)
_RED = "BLOCKED"


def sentinel_enabled() -> bool:
    """Whether the organism may approve its own work. Default OFF.

    Unattended application of self-authored code is the single most
    consequential switch in this system, so it is never on by accident — it
    is turned on for a session, deliberately, by the launcher. NEVER raises.
    """
    raw = (os.environ.get(_ENV_SENTINEL, "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def tier_ceiling() -> str:
    """The highest risk tier the Sentinel may auto-approve.

    Defaults to ``APPROVAL_REQUIRED`` (orange) per the operator's model.
    ``BLOCKED`` is never accepted as a ceiling: red is the floor, and a
    configuration that tried to raise the ceiling to it would be asking for
    the one thing no signature or score may buy.
    """
    raw = (os.environ.get(_ENV_MAX_TIER, "") or "").strip().upper()
    if raw in _TIER_ORDER:
        return raw
    if raw == _RED:
        logger.warning(
            "[Sentinel] %s=%s refused — red is the floor, not a ceiling; "
            "falling back to APPROVAL_REQUIRED", _ENV_MAX_TIER, raw,
        )
    elif raw:
        logger.warning(
            "[Sentinel] %s=%r is not a known tier — falling back to "
            "APPROVAL_REQUIRED", _ENV_MAX_TIER, raw,
        )
    return "APPROVAL_REQUIRED"


def _tier_index(tier: Optional[str]) -> int:
    """Index in ``_TIER_ORDER``, or -1 when unknown/red (never auto-approve)."""
    if tier is None:
        return -1
    name = str(tier).strip().upper()
    # Accept an enum-ish "RiskTier.SAFE_AUTO" as well as a bare name.
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    try:
        return _TIER_ORDER.index(name)
    except ValueError:
        return -1


# ---------------------------------------------------------------------------
# Question 1 — is the candidate good enough?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComplianceVerdict:
    """Whether a candidate cleared every gate that already judges quality."""

    ok: bool
    checks: Dict[str, bool] = field(default_factory=dict)
    detail: Dict[str, str] = field(default_factory=dict)

    @property
    def score(self) -> float:
        """Fraction of checks passed. 1.0 is the only value that approves."""
        if not self.checks:
            return 0.0
        return sum(1 for v in self.checks.values() if v) / len(self.checks)

    def render(self) -> str:
        marks = ", ".join(
            f"{'ok' if v else 'FAIL'}:{k}" for k, v in sorted(self.checks.items())
        )
        return f"compliance {self.score * 100:.0f}% [{marks}]"

    def failures(self) -> Tuple[str, ...]:
        return tuple(sorted(k for k, v in self.checks.items() if not v))


def spec_compliance_verdict(
    ctx: Any,
    *,
    validation: Any = None,
    guardian_detections: Optional[Sequence[Any]] = None,
    force_full_content: bool = False,
) -> ComplianceVerdict:
    """Compose the existing gates into one score. NEVER raises.

    ``ok`` requires a PERFECT score. This is not a threshold anyone tuned: a
    partial pass means one of the gates that already has to hold did not, and
    "mostly compliant" is not a state an unattended apply may run in.
    """
    checks: Dict[str, bool] = {}
    detail: Dict[str, str] = {}

    # 1. Validation passed at all.
    try:
        passed = bool(getattr(validation, "passed", False))
    except Exception:  # noqa: BLE001
        passed = False
    checks["validation_passed"] = passed
    detail["validation_passed"] = str(
        getattr(validation, "failure_class", "") or ("ok" if passed else "no result")
    )

    # 2. The differential verdict: zero failures the CANDIDATE caused. Tests
    #    already red at HEAD are the environment's, not this candidate's, and
    #    the differential gate is what separates them.
    try:
        failed_ids = tuple(getattr(validation, "failed_test_ids", ()) or ())
        ambient = tuple(getattr(validation, "ambient_red_tests", ()) or ())
        caused = tuple(t for t in failed_ids if t not in set(ambient))
    except Exception:  # noqa: BLE001
        caused = ("<unreadable>",)
    checks["zero_regressions"] = not caused
    detail["zero_regressions"] = (
        "none" if not caused else f"{len(caused)} caused: {', '.join(caused[:3])}"
    )

    # 3. A validation that proved nothing must never read as a pass — the
    #    vacuous-VALIDATE class (test_total: 0) that once let an empty run
    #    approve itself.
    try:
        total = int(getattr(validation, "test_total", 0) or 0)
    except Exception:  # noqa: BLE001
        total = 0
    checks["validation_non_vacuous"] = total > 0
    detail["validation_non_vacuous"] = f"{total} test(s) ran"

    # 4. SemanticGuardian: zero detections.
    try:
        dets = tuple(guardian_detections or ())
    except Exception:  # noqa: BLE001
        dets = ("<unreadable>",)
    checks["guardian_clean"] = not dets
    detail["guardian_clean"] = (
        "no detections" if not dets else f"{len(dets)} detection(s)"
    )

    # 5. The op ran at the fidelity the envelope promised (capability
    #    assurance) — a candidate produced by a degraded pipeline is not
    #    evidence about the pipeline the operator armed.
    try:
        from backend.core.ouroboros.governance.capability_assurance import (  # noqa: PLC0415
            assert_generation_capability,
        )
        cap = assert_generation_capability(
            ctx, force_full_content=force_full_content,
        )
        # The SAME policy the generation seam applies, for the same reason. A
        # bare `cap.ok` here would simply move the block from GENERATE to
        # approval: the op would generate a usable candidate and then be denied
        # auto-approval for the fidelity it lost, so no self-directed work could
        # ever land while the diff schema stays broken. Fidelity loss is not a
        # RISK signal, and this check gates a risk decision.
        #
        # Only a FATAL degradation -- one that cannot produce a usable
        # candidate -- breaks compliance. A recoverable one is recorded in the
        # detail so the verdict still SAYS what happened.
        checks["capability_intact"] = bool(cap.ok) or cap.degraded_but_usable
        detail["capability_intact"] = (
            cap.reason or "ok" if cap.ok
            else f"degraded_but_usable:{cap.reason}" if cap.degraded_but_usable
            else cap.reason
        )
    except Exception as exc:  # noqa: BLE001
        checks["capability_intact"] = False
        detail["capability_intact"] = f"assurance_unavailable:{type(exc).__name__}"

    return ComplianceVerdict(
        ok=all(checks.values()) and bool(checks), checks=checks, detail=detail,
    )


# ---------------------------------------------------------------------------
# Question 2 — may a machine approve this op at all?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoApprovalVerdict:
    """The single answer the Iron Gate consults before prompting a human."""

    approved: bool
    reason: str
    tier: str = ""
    compliance: Optional[ComplianceVerdict] = None

    def render(self) -> str:
        head = "AUTO-APPROVED" if self.approved else "escalating to human"
        tail = f" — {self.reason}" if self.reason else ""
        score = (
            f" ({self.compliance.render()})" if self.compliance is not None else ""
        )
        return f"{head}{tail}{score}"


def auto_approval_verdict(
    ctx: Any,
    *,
    risk_tier: Optional[str] = None,
    validation: Any = None,
    guardian_detections: Optional[Sequence[Any]] = None,
    force_full_content: bool = False,
    touches_governance: Optional[bool] = None,
    is_order2_rsi: bool = False,
    recursion_exceeded: bool = False,
) -> AutoApprovalVerdict:
    """May this op apply without a human? NEVER raises; unknown means NO."""
    try:
        if not sentinel_enabled():
            return AutoApprovalVerdict(
                False, "sentinel mode is off", str(risk_tier or ""),
            )

        # -- the floor, first, so nothing below can be reasoned around ------
        if touches_governance is None:
            touches_governance = _touches_governance(ctx)
        try:
            from backend.core.ouroboros.governance.layer4_roadmap_authority import (  # noqa: E501,PLC0415
                is_safety_operation,
            )
            # The tier is handled by the ceiling below; here we ask ONLY the
            # structural half of the floor, which no ceiling may lift.
            structural_floor = is_safety_operation(
                risk_tier=None,
                is_order2_rsi=is_order2_rsi,
                recursion_exceeded=recursion_exceeded,
                touches_governance=bool(touches_governance),
            )
        except Exception:  # noqa: BLE001 — cannot read the floor => assume it applies
            structural_floor = True
        if structural_floor:
            return AutoApprovalVerdict(
                False,
                "un-signable floor: governance substrate / Order-2 RSI / "
                "recursion bound — always a human",
                str(risk_tier or ""),
            )

        # -- red is never auto-approved -------------------------------------
        tier_name = str(risk_tier or "").strip().upper()
        if "." in tier_name:
            tier_name = tier_name.rsplit(".", 1)[-1]
        if tier_name == _RED or not tier_name:
            return AutoApprovalVerdict(
                False,
                "red tier (or unreadable tier) always escalates",
                tier_name,
            )

        idx, ceiling = _tier_index(tier_name), tier_ceiling()
        if idx < 0:
            return AutoApprovalVerdict(
                False, f"unknown risk tier {tier_name!r} — escalating", tier_name,
            )
        if idx > _tier_index(ceiling):
            return AutoApprovalVerdict(
                False,
                f"tier {tier_name} exceeds the ceiling {ceiling}",
                tier_name,
            )

        # -- quality, only once the op is eligible at all --------------------
        compliance = spec_compliance_verdict(
            ctx, validation=validation,
            guardian_detections=guardian_detections,
            force_full_content=force_full_content,
        )
        if not compliance.ok:
            return AutoApprovalVerdict(
                False,
                "compliance not perfect: " + ", ".join(compliance.failures()),
                tier_name, compliance,
            )
        return AutoApprovalVerdict(
            True, f"tier {tier_name} <= ceiling {ceiling}, compliance 100%",
            tier_name, compliance,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Sentinel] auto-approval check degraded: %r", exc)
        return AutoApprovalVerdict(
            False, f"assurance_unavailable:{type(exc).__name__}", str(risk_tier or ""),
        )


def _touches_governance(ctx: Any) -> bool:
    """True when any target file is part of the cage the organism runs inside.

    Derived from the module path of the governance package itself rather than
    a written-down list, so a file added to the cage is protected the day it
    exists and nobody has to remember to extend a constant.
    """
    try:
        from backend.core.ouroboros import governance as _gov  # noqa: PLC0415
        anchors = set()
        for entry in list(getattr(_gov, "__path__", []) or []):
            parts = str(entry).replace("\\", "/").rstrip("/").split("/")
            # ".../backend/core/ouroboros/governance" -> the repo-relative tail
            for i in range(len(parts)):
                tail = "/".join(parts[i:])
                if tail.endswith("ouroboros/governance"):
                    anchors.add(tail)
        anchors.add("ouroboros/governance")
        for raw in (getattr(ctx, "target_files", ()) or ()):
            path = str(raw).replace("\\", "/")
            if any(a in path for a in anchors):
                return True
        return False
    except Exception:  # noqa: BLE001 — unknown => treat as governance (fail closed)
        return True
