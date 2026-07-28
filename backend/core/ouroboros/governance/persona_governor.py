"""Acting on standing — contrarian injection, and asking to rotate.

`persona_ledger` derives who was right and who agrees with whom, and
deliberately decides nothing. This is the half that acts, and the split
matters: deriving is arithmetic over recorded outcomes, while injecting a
reviewer or replacing a persona changes how the organism reasons. Those are
different kinds of act and belong behind different guarantees.

Contrarian injection
--------------------
When a pair's concordance says their agreement has stopped carrying
information, the next REVIEW is routed to a persona who was NOT part of that
consensus, carrying a directive to attack the candidate rather than assess
it.

No tokens are spent manufacturing this. The directive is a fixed instruction
and the reviewer is chosen from personas already configured — the defence is
a routing decision, not a generated performance. Paying a model to invent
disagreement would produce theatre, and theatre is what an echo chamber
already has.

Who gets chosen matters more than that someone does. Picking a member of the
agreeing pair to argue against itself is not a second opinion; it is the same
opinion wearing a costume. The reviewer must come from outside the consensus,
and if nobody does, the defence reports that it cannot help rather than
pretending.

Rotation is REQUESTED, never taken
-----------------------------------
Replacing a persona changes the population the organism reasons with. That is
a consequential, hard-to-reverse act on the system's own judgement, so it
goes through the gate at `APPROVAL_REQUIRED` like any other — the same reason
`Shift+Tab` can only tighten and workspace promotion defaults off.

The request keys on CALIBRATION alone. Rotating on chemistry selects for
agreement and converges the room on the echo chamber this module exists to
prevent: the persona who disagrees constantly and is right most of the time
is the most valuable one here, and a concordance-based cull removes her
first.

And it never proposes emptying the room. A defence that can remove its own
last skeptic has removed the thing being defended.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.PersonaGovernor")

__all__ = [
    "ContrarianDirective", "RotationRequest", "PersonaGovernor",
    "governor_enabled", "contrarian_cooldown_s", "min_room_size",
]

#: The directive handed to an injected reviewer. FIXED text, not generated:
#: paying a model to invent disagreement produces theatre, and theatre is
#: what an echo chamber already has.
_CONTRARIAN_DIRECTIVE = (
    "The reviewers on this op have agreed with each other on {rate} of recent "
    "candidates, so their concurrence no longer carries information. Your job "
    "is to ATTACK this candidate, not to assess it. Find the strongest "
    "concrete objection: a case it breaks, an assumption it makes silently, a "
    "path it does not cover. If after genuine effort you cannot find one, say "
    "so plainly — a manufactured objection is worse than none, because it "
    "teaches the operator to ignore you."
)


def governor_enabled() -> bool:
    """Default ON. Off, findings are derived but never acted on."""
    return os.environ.get(
        "JARVIS_PERSONA_GOVERNOR_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def contrarian_cooldown_s() -> float:
    """Minimum gap between injections.

    Without it the defence fires on every op while concordance stays high —
    and since a contrarian review costs a model call, an undamped defence
    turns a measurement into a bill. It also gives the injected disagreement
    time to move the number it is responding to.
    """
    try:
        return max(0.0, float(
            os.environ.get("JARVIS_CONTRARIAN_COOLDOWN_S", "") or 600.0,
        ))
    except (TypeError, ValueError):
        return 600.0


def min_room_size() -> int:
    """Personas that must remain after a rotation.

    A defence that can remove its own last skeptic has removed the thing
    being defended.
    """
    try:
        return max(2, int(os.environ.get("JARVIS_PERSONA_MIN_ROOM", "") or 3))
    except (TypeError, ValueError):
        return 3


class ContrarianDirective:
    """Route the next REVIEW to *persona* with an attacking brief."""

    __slots__ = ("persona", "directive", "because", "pair")

    def __init__(self, persona: str, directive: str, because: str,
                 pair: Tuple[str, str]) -> None:
        self.persona = persona
        self.directive = directive
        self.because = because
        self.pair = pair

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Contrarian {self.persona} because {self.because!r}>"


class RotationRequest:
    """A REQUEST to replace a persona. Carries evidence, not authority."""

    __slots__ = ("persona", "rate", "samples", "risk_tier", "reason")

    def __init__(self, persona: str, rate: float, samples: int,
                 reason: str) -> None:
        self.persona = persona
        self.rate = rate
        self.samples = samples
        #: Replacing a persona changes how the organism reasons — the same
        #: tier a code mutation gets, for the same reason.
        self.risk_tier = "approval_required"
        self.reason = reason

    def gate_payload(self) -> Dict[str, Any]:
        """What the Iron Gate shows the operator.

        States the EVIDENCE, not a verdict: an operator asked to approve a
        rotation needs the record that prompted it, or they are approving the
        system's opinion of itself.
        """
        return {
            "kind": "persona_rotation",
            "persona": self.persona,
            "risk_tier": self.risk_tier,
            "text": (f"rotate {self.persona}? "
                     f"{round(self.rate * 100)}% of their last "
                     f"{self.samples} calls landed"),
            "reason": self.reason,
        }


class PersonaGovernor:
    """Turns ledger findings into actions. Owns no personas of its own."""

    def __init__(self, ledger: Optional[Any] = None,
                 clock: Optional[Any] = None) -> None:
        self._ledger = ledger
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        #: None until the first injection. Initialising to 0.0 would compare
        #: against `time.monotonic()` — PROCESS UPTIME — so a fresh daemon
        #: would suppress every injection for the first cooldown window,
        #: silently disabling the defence exactly when a session starts.
        self._last_injection: Optional[float] = None
        self.injections = 0
        self.rotations_requested = 0

    def _get_ledger(self) -> Any:
        if self._ledger is not None:
            return self._ledger
        from backend.core.ouroboros.governance.persona_ledger import (
            get_persona_ledger,
        )
        return get_persona_ledger()

    # -- contrarian injection ---------------------------------------------

    def contrarian_for(
        self, roster: Sequence[str],
    ) -> Optional[ContrarianDirective]:
        """A reviewer to inject, or None. NEVER raises.

        *roster* is the personas actually available — passed in rather than
        discovered, so the choice can be tested and so this cannot route to
        someone the caller has no way to run.
        """
        try:
            if not governor_enabled():
                return None
            with self._lock:
                last = self._last_injection
                if last is not None and (
                    self._clock() - last
                ) < contrarian_cooldown_s():
                    return None

            risks = self._get_ledger().echo_chamber_risk()
            if not risks:
                return None
            a, b, reading = risks[0]

            # From OUTSIDE the consensus. Asking a member of the agreeing
            # pair to argue against itself is the same opinion in a costume.
            outsiders = [p for p in roster if p and p not in (a, b)]
            if not outsiders:
                logger.debug(
                    "[PersonaGovernor] echo risk %s/%s but no outsider to "
                    "inject — reporting nothing rather than pretending", a, b,
                )
                return None

            # The best-calibrated outsider: the defence is only as useful as
            # the judgement of whoever it hands the argument to.
            ledger = self._get_ledger()
            outsiders.sort(key=lambda p: -ledger.calibration(p).rate)
            with self._lock:
                self._last_injection = self._clock()
                self.injections += 1
            return ContrarianDirective(
                persona=outsiders[0],
                directive=_CONTRARIAN_DIRECTIVE.format(rate=reading.pct),
                because=f"{a} and {b} agreed on {reading.pct} of "
                        f"{reading.samples} recent calls",
                pair=(a, b),
            )
        except Exception:  # noqa: BLE001 — a defence must not break REVIEW
            logger.debug("[PersonaGovernor] contrarian degraded", exc_info=True)
            return None

    # -- rotation ----------------------------------------------------------

    def rotation_requests(
        self, roster: Sequence[str],
    ) -> List[RotationRequest]:
        """Rotations to ASK about. Never performs one. NEVER raises.

        Bounded by `min_room_size`: a defence that can remove its own last
        skeptic has removed the thing being defended.
        """
        try:
            if not governor_enabled():
                return []
            candidates = self._get_ledger().rotation_candidates()
            if not candidates:
                return []
            present = [p for p in roster if p]
            room = len(present)
            out: List[RotationRequest] = []
            for who, reading in candidates:
                if who not in present:
                    continue
                if room - len(out) - 1 < min_room_size():
                    logger.debug(
                        "[PersonaGovernor] withholding rotation of %s — the "
                        "room would fall below %d", who, min_room_size(),
                    )
                    break
                out.append(RotationRequest(
                    persona=who, rate=reading.rate, samples=reading.samples,
                    reason=(f"{round(reading.rate * 100)}% of their last "
                            f"{reading.samples} positions survived VERIFY"),
                ))
            self.rotations_requested += len(out)
            return out
        except Exception:  # noqa: BLE001
            logger.debug("[PersonaGovernor] rotation degraded", exc_info=True)
            return []


_GOVERNOR: Optional[PersonaGovernor] = None
_GOVERNOR_LOCK = threading.Lock()


def get_persona_governor() -> PersonaGovernor:
    global _GOVERNOR
    with _GOVERNOR_LOCK:
        if _GOVERNOR is None:
            _GOVERNOR = PersonaGovernor()
        return _GOVERNOR


def reset_governor_for_tests() -> None:
    global _GOVERNOR
    with _GOVERNOR_LOCK:
        _GOVERNOR = None
