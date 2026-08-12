"""rehearsal_tier — what the organism does when every lane is dry.

The failure this addresses
--------------------------

Observed in ``bt-2026-08-11-230412``: DoubleWord returned 402, the
economic classifier caught it, ``LiquidityLedger`` opened a 1800s outage,
and the failback cascade ran anyway — for every subsequent op. Eight
GENERATE attempts, each re-walking the full tier chain (primary probe →
fallback semaphore acquisition → timeout budget → breaker consult) before
arriving at the conclusion the ledger had already recorded minutes
earlier.

Nothing was broken. The breaker classified correctly, the cascade failed
over correctly, and every tier really was dead. The defect is that
**detection and consumption were disconnected**: the outage was known,
and each op rediscovered it from scratch at full cost.

That is the root cause addressed here — not "there is no fallback". A
fallback that fabricates work would be worse than the thrash.

What a rehearsal is, and is not
-------------------------------

**It produces no code.** There is no synthetic patch, no generated diff,
no invented rationale. A tier that manufactured a candidate would put
fabricated work into the ledger whose entire value is that it records
only what actually happened — the defect class this codebase keeps
finding (a measurement presented as evidence of something it never
touched).

A rehearsal is an *honest terminal*: the op ran, generation was
structurally impossible, and that fact is recorded with its evidence —
which providers were exhausted, which cause fired, and when the outage is
expected to lift. The op is then replayable, by token, once credit
returns.

So the value delivered is exactly:

  * ops stop paying the full cascade cost to rediscover a known outage
  * the transcript gains real records of a real condition
  * nothing anywhere can mistake a rehearsal for work

Structurally incapable of lying
-------------------------------

:class:`RehearsalOutcome` carries no file content and no diff — not by
convention but by shape; there is no field to put them in.
``provenance`` is a frozen constant, ``mutates_disk`` and
``eligible_for_commit`` are read-only ``False``. An AST pin
(:func:`register_shipped_invariants`) proves the module never imports
``change_engine``, ``auto_committer`` or any mutation surface, so a later
refactor cannot quietly grant it teeth.

Adaptive, not scheduled
-----------------------

The suppression window is **derived**, never configured: it comes from
the outage TTL the quarantine layer already computed for that route. A
second timeout here would be a second authority for "how long is this
outage", and the shorter of the two would silently win. When the ledger
says the outage lifted, rehearsal stops on the next consult with no timer
to expire and no state to reconcile.

Authority boundary
------------------

* §1 deterministic — a predicate over state other layers already own
* §5 Tier 0 — no LLM, no network, no I/O
* §7 fail-closed — any doubt returns NOT_ENGAGED, so the normal
  exhaustion path runs unchanged. Rehearsal never becomes the reason an
  op failed to be attempted.
* §8 observable — every outcome carries the evidence for its own verdict
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.RehearsalTier")


__all__ = [
    "MASTER_FLAG_ENV_VAR",
    "PROVENANCE",
    "RehearsalDisposition",
    "RehearsalOutcome",
    "RehearsalTier",
    "get_rehearsal_tier",
    "is_economic_exhaustion",
    "register_shipped_invariants",
    "rehearsal_enabled",
    "reset_rehearsal_tier_for_tests",
]


REHEARSAL_SCHEMA_VERSION: str = "rehearsal_tier.v1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_REHEARSAL_TIER_ENABLED"

#: Frozen. Every surface that renders an outcome reads this rather than
#: choosing its own word, so "rehearsal" cannot drift into "generated"
#: at one call site while staying honest at another.
PROVENANCE: str = "rehearsal"

#: Causes from ``_raise_exhausted``'s report that mean "no amount of
#: retrying will help, because this is a billing state". Distinct from
#: transient causes (timeout, deadline, breaker-open), where retrying is
#: exactly right and rehearsal must NOT engage.
_ECONOMIC_TOKENS: Tuple[str, ...] = (
    "402", "payment", "quota", "insufficient", "credit", "billing",
    "economic", "entitlement", "403",
)

#: Failure modes that are transient by nature. Listed so a cause string
#: containing an economic token for an unrelated reason (a 402 in a URL,
#: a message quoting an earlier error) cannot promote a timeout into an
#: outage.
_TRANSIENT_TOKENS: Tuple[str, ...] = (
    "timeout", "deadline", "cancelled", "canceled", "breaker_open",
    "queue_only_dispatch",
)


def rehearsal_enabled() -> bool:
    """Read :data:`MASTER_FLAG_ENV_VAR`. **Default false.**

    Off, this module is inert and the exhaustion path behaves exactly as
    it does today — which is correct behaviour, merely expensive. Turning
    a cost optimisation on by default would change failure semantics for
    every operator who never asked. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "false")
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ===========================================================================
# Classification — reusing the report the raiser already builds
# ===========================================================================


def is_economic_exhaustion(report: Optional[Mapping[str, Any]]) -> bool:
    """True iff this exhaustion is a BILLING state, not a transient one.

    Reads the structured ``exhaustion_report`` that ``_raise_exhausted``
    already attaches to its ``RuntimeError`` — no second classifier, and
    no re-parsing of log lines.

    Transient wins ties. A report mentioning both a timeout and a quota is
    ambiguous, and treating ambiguity as economic would suppress
    generation that might have succeeded; treating it as transient only
    costs the cascade we would have paid anyway. Fail-closed means
    *toward retrying*. NEVER raises."""
    if not report:
        return False
    try:
        blob = " ".join(
            f"{k}={v}" for k, v in report.items()
            if not isinstance(v, (dict, list, tuple))
        ).lower()
    except Exception:  # noqa: BLE001
        return False
    if any(tok in blob for tok in _TRANSIENT_TOKENS):
        return False
    return any(tok in blob for tok in _ECONOMIC_TOKENS)


# ===========================================================================
# Closed taxonomy
# ===========================================================================


class RehearsalDisposition(str, enum.Enum):
    """Why the tier did or did not engage. Closed and specific: "rehearsal
    did not happen" is not actionable, while "the outage was transient" and
    "the flag is off" call for different responses."""

    NOT_ENGAGED_DISABLED = "not_engaged_disabled"
    NOT_ENGAGED_TRANSIENT = "not_engaged_transient"
    NOT_ENGAGED_NO_EVIDENCE = "not_engaged_no_evidence"
    REHEARSED = "rehearsed"

    @property
    def engaged(self) -> bool:
        return self is RehearsalDisposition.REHEARSED


@dataclass(frozen=True)
class RehearsalOutcome:
    """An honest terminal for an op that could not be generated.

    Note what is ABSENT and cannot be added without changing this class:
    there is no ``content``, no ``diff``, no ``files`` payload. The
    outcome has nowhere to put fabricated work.
    """

    op_id: str
    disposition: RehearsalDisposition
    reason: str = ""
    #: Providers the cascade actually tried, from the raiser's own report.
    exhausted_providers: Tuple[str, ...] = ()
    #: The op's targets, carried so a replay knows what it was for.
    target_files: Tuple[str, ...] = ()
    #: Monotonic seconds until the outage is expected to lift, DERIVED from
    #: the quarantine's own TTL. ``0`` = unknown, never a guess.
    suppressed_for_s: float = 0.0
    #: Opaque handle for re-submitting this op when credit returns.
    replay_token: str = ""
    schema_version: str = REHEARSAL_SCHEMA_VERSION

    @property
    def provenance(self) -> str:
        """Frozen. Not a field, so it cannot be constructed as anything
        else."""
        return PROVENANCE

    @property
    def mutates_disk(self) -> bool:
        """Always False. A rehearsal has no bytes to write."""
        return False

    @property
    def eligible_for_commit(self) -> bool:
        """Always False. Nothing was produced that could be committed, and
        an AutoCommit of a rehearsal would be a commit of nothing wearing
        the signature of work."""
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "disposition": self.disposition.value,
            "provenance": self.provenance,
            "reason": self.reason,
            "exhausted_providers": list(self.exhausted_providers),
            "target_files": list(self.target_files),
            "suppressed_for_s": round(self.suppressed_for_s, 1),
            "replay_token": self.replay_token,
            "mutates_disk": self.mutates_disk,
            "eligible_for_commit": self.eligible_for_commit,
            "schema_version": self.schema_version,
        }


# ===========================================================================
# The tier
# ===========================================================================


@dataclass
class _RouteState:
    """What is known about one route's outage. Derived, never scheduled."""

    engaged_at: float = 0.0
    ttl_s: float = 0.0
    rehearsals: int = 0
    last_cause: str = ""

    def remaining(self, now: float) -> float:
        if self.ttl_s <= 0:
            return 0.0
        return max(0.0, (self.engaged_at + self.ttl_s) - now)


class RehearsalTier:
    """Terminal tier consulted when the cascade has nowhere left to go."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._routes: Dict[str, _RouteState] = {}
        self._counters: Dict[str, int] = {
            "consults": 0, "rehearsed": 0, "declined_transient": 0,
            "declined_disabled": 0, "declined_no_evidence": 0,
        }

    # ---- the single entry point ---------------------------------------

    def consult(
        self,
        op_id: str,
        *,
        report: Optional[Mapping[str, Any]] = None,
        target_files: Sequence[str] = (),
        route: str = "",
    ) -> RehearsalOutcome:
        """Decide whether this exhaustion becomes a rehearsal. NEVER raises.

        Fail-closed toward the STATUS QUO: every uncertain path returns a
        NOT_ENGAGED disposition, and the caller then raises exactly as it
        does today. This tier can make an op cheaper; it can never make an
        op fail that would otherwise have been attempted.
        """
        op = str(op_id or "")
        with self._lock:
            self._counters["consults"] += 1

        try:
            if not rehearsal_enabled():
                return self._decline(
                    op, RehearsalDisposition.NOT_ENGAGED_DISABLED,
                    "master flag off", target_files,
                )
            if not report:
                return self._decline(
                    op, RehearsalDisposition.NOT_ENGAGED_NO_EVIDENCE,
                    "no exhaustion report to classify", target_files,
                )
            if not is_economic_exhaustion(report):
                return self._decline(
                    op, RehearsalDisposition.NOT_ENGAGED_TRANSIENT,
                    "exhaustion is retryable, not economic", target_files,
                )

            cause = str(report.get("cause", "") or "")
            rt = str(route or report.get("route", "") or "default")
            now = time.monotonic()
            ttl = self._resolve_ttl(rt)

            with self._lock:
                state = self._routes.get(rt)
                if state is None:
                    state = self._routes[rt] = _RouteState(
                        engaged_at=now, ttl_s=ttl,
                    )
                elif state.ttl_s > 0 and state.remaining(now) <= 0:
                    # A KNOWN window elapsed — this is a new outage
                    # episode, so the evidence starts over.
                    #
                    # The guard is `ttl_s > 0` deliberately. An UNKNOWN
                    # TTL also reports remaining()==0, and treating that
                    # as "expired" would rebuild the state on every
                    # consult, discarding the count of how many ops the
                    # outage has cost — the one number that makes the
                    # thrash visible. Unknown means we cannot say when it
                    # lifts, which is a reason to keep accumulating, not
                    # to forget.
                    state = self._routes[rt] = _RouteState(
                        engaged_at=now, ttl_s=ttl,
                    )
                elif ttl > 0:
                    # The quarantine's TTL is the ONE authority; adopt a
                    # refreshed value rather than holding a stale copy.
                    state.ttl_s = ttl
                state.rehearsals += 1
                state.last_cause = cause
                self._counters["rehearsed"] += 1
                remaining = state.remaining(now)

            outcome = RehearsalOutcome(
                op_id=op,
                disposition=RehearsalDisposition.REHEARSED,
                reason=f"all providers economically exhausted ({cause})"
                       if cause else "all providers economically exhausted",
                exhausted_providers=self._providers_from(report),
                target_files=tuple(str(f) for f in (target_files or ())),
                suppressed_for_s=remaining,
                replay_token=f"{PROVENANCE}:{op}",
            )
            logger.warning(
                "[RehearsalTier] op=%s REHEARSED — generation structurally "
                "impossible (%s); no code produced, op replayable via %s; "
                "route=%s outage lifts in %.0fs",
                op, outcome.reason, outcome.replay_token, rt, remaining,
            )
            return outcome
        except Exception:  # noqa: BLE001
            logger.debug("[RehearsalTier] consult degraded", exc_info=True)
            return self._decline(
                op, RehearsalDisposition.NOT_ENGAGED_NO_EVIDENCE,
                "consult failed", target_files,
            )

    # ---- internals -----------------------------------------------------

    def _decline(
        self, op_id: str, disposition: RehearsalDisposition,
        reason: str, target_files: Sequence[str],
    ) -> RehearsalOutcome:
        key = {
            RehearsalDisposition.NOT_ENGAGED_DISABLED: "declined_disabled",
            RehearsalDisposition.NOT_ENGAGED_TRANSIENT: "declined_transient",
            RehearsalDisposition.NOT_ENGAGED_NO_EVIDENCE:
                "declined_no_evidence",
        }.get(disposition)
        if key:
            with self._lock:
                self._counters[key] += 1
        return RehearsalOutcome(
            op_id=op_id, disposition=disposition, reason=reason,
            target_files=tuple(str(f) for f in (target_files or ())),
        )

    @staticmethod
    def _resolve_ttl(route: str) -> float:
        """The outage TTL, DERIVED from the layer that already computed it.

        Returns ``0.0`` when unknown — which surfaces as "unknown", never
        as a default duration. A fabricated TTL would make the tier state
        a lift-time it never measured, which is the same dishonesty as a
        fabricated candidate in a smaller box."""
        try:
            from backend.core.ouroboros.governance.provider_quarantine import (
                get_provider_health_gradient,
            )
            q = get_provider_health_gradient()
            for attr in ("outage_ttl_s", "quarantine_ttl_s", "ttl_s"):
                val = getattr(q, attr, None)
                if callable(val):
                    val = val(route)
                if isinstance(val, (int, float)) and val > 0:
                    return float(val)
        except Exception:  # noqa: BLE001
            logger.debug("[RehearsalTier] ttl resolve degraded", exc_info=True)
        return 0.0

    @staticmethod
    def _providers_from(report: Mapping[str, Any]) -> Tuple[str, ...]:
        names = []
        for key in ("tier0_name", "primary_name", "fallback_name"):
            val = str(report.get(key, "") or "").strip()
            if val and val not in ("?", "none"):
                names.append(val)
        return tuple(names)

    # ---- observability --------------------------------------------------

    def snapshot_stats(self) -> Dict[str, Any]:
        """NEVER raises."""
        try:
            now = time.monotonic()
            with self._lock:
                return {
                    "schema_version": REHEARSAL_SCHEMA_VERSION,
                    "enabled": rehearsal_enabled(),
                    "counters": dict(self._counters),
                    "routes": {
                        r: {
                            "rehearsals": s.rehearsals,
                            "remaining_s": round(s.remaining(now), 1),
                            "last_cause": s.last_cause,
                        }
                        for r, s in self._routes.items()
                    },
                }
        except Exception:  # noqa: BLE001
            return {"schema_version": REHEARSAL_SCHEMA_VERSION}


_DEFAULT: Optional[RehearsalTier] = None
_DEFAULT_LOCK = threading.Lock()


def get_rehearsal_tier() -> RehearsalTier:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = RehearsalTier()
        return _DEFAULT


def reset_rehearsal_tier_for_tests() -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


# ===========================================================================
# Authority invariant
# ===========================================================================


def register_shipped_invariants() -> list:
    """Pin that a rehearsal can never grow teeth. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    import ast as _ast

    _MUTATION_SURFACES = (
        "change_engine", "auto_committer", "orchestrator", "iron_gate",
        "policy_engine", "repair_engine", "worktree_manager",
    )

    def _validate_no_mutation_surface(tree, _source) -> tuple:
        del _source
        violations = []
        for node in _ast.walk(tree):
            mod = ""
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, _ast.Import):
                mod = ",".join(a.name for a in node.names)
            for banned in _MUTATION_SURFACES:
                if banned and banned in mod:
                    violations.append(
                        f"rehearsal_tier imports {banned!r} — a rehearsal "
                        f"produces no bytes; reaching a mutation surface is "
                        f"how it would stop being one"
                    )
        return tuple(violations)

    def _validate_outcome_carries_no_content(tree, _source) -> tuple:
        del _source
        banned_fields = {"content", "full_content", "diff", "diff_text",
                         "patch", "files"}
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and node.name == "RehearsalOutcome":
                for stmt in node.body:
                    if isinstance(stmt, _ast.AnnAssign) and isinstance(
                        stmt.target, _ast.Name,
                    ):
                        if stmt.target.id in banned_fields:
                            violations.append(
                                f"RehearsalOutcome gained a {stmt.target.id!r} "
                                f"field — the outcome must have NOWHERE to put "
                                f"fabricated work"
                            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="rehearsal_tier_has_no_mutation_surface",
            target_file=(
                "backend/core/ouroboros/governance/rehearsal_tier.py"
            ),
            description=(
                "The rehearsal tier must never import a surface that can "
                "write to disk, commit, or drive the loop."
            ),
            validate=_validate_no_mutation_surface,
        ),
        ShippedCodeInvariant(
            invariant_name="rehearsal_outcome_cannot_carry_work",
            target_file=(
                "backend/core/ouroboros/governance/rehearsal_tier.py"
            ),
            description=(
                "BUG-FIX REGRESSION PIN: RehearsalOutcome must have no field "
                "capable of holding generated content. Honesty by shape, not "
                "by convention."
            ),
            validate=_validate_outcome_carries_no_content,
        ),
    ]
