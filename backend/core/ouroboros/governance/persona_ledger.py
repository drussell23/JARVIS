"""Who was right, who agrees with whom, and how much of that we can stand behind.

The room needs standing. `@cassandra` clapping back means nothing until you
know whether her calls land; a chemistry number between two personas means
nothing until you know they have disagreed enough times to have a pattern.

Both are DERIVED, never self-reported. Asking an LLM to rate its own
interaction costs a model call, returns a sycophantic number, and has no
ground truth to check against — it is invented data wearing a percentage
sign. The organism already records the only signal that settles an argument:

    GENERATE proposes → REVIEW contests or concurs → VERIFY decides

VERIFY is a test suite. It does not have opinions.

Two numbers, and they are not the same number
----------------------------------------------
* **Calibration** — when this persona took a position, how often did VERIFY
  agree with it? This is being RIGHT.
* **Concordance** — when two personas both took a position, how often was it
  the same one? This is AGREEING.

Conflating them is the trap. A pair with 100% concordance may simply be two
voices that never disagree, which is worth nothing and actively dangerous:
consensus that is never challenged is how a review board stops catching
anything. High concordance is a WARNING, not an achievement.

Which is why rotation keys on calibration alone. Firing for low chemistry
selects for agreement and converges the room on an echo chamber — the
persona who disagrees constantly and is right 70% of the time is the most
valuable one in the building, and a concordance-based cull removes her first.

Provenance, because a number is not evidence
---------------------------------------------
Two samples at 100% is not a track record. Reporting it as one would fire a
persona for being new, or trip an echo-chamber defence on a coincidence.

So every reading carries provenance, following the discipline
`advisor_locality` already established for blast radius: a measurement below
the sample floor is `insufficient`, and an `insufficient` reading is NEUTRAL
— it renders as "—", it never trips the defence, and it never proposes a
rotation. The organism says "I do not know yet" rather than guessing, for the
same reason the advisor refuses to BLOCK on a fabricated blast radius.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.PersonaLedger")

__all__ = [
    "Reading", "PersonaLedger", "ledger_enabled", "sample_floor",
    "echo_threshold", "rotation_threshold", "get_persona_ledger",
]

#: Positions a persona can take on a candidate.
FOR = "for"
AGAINST = "against"

#: Provenance values. `measured` is actionable; `insufficient` never is.
MEASURED = "measured"
INSUFFICIENT = "insufficient"

#: Rolling window per subject. Bounded so a long-lived install measures
#: RECENT behaviour — a persona who was wrong all last month and right all
#: this week should read as improving, not as permanently discredited.
_WINDOW = 50


def ledger_enabled() -> bool:
    """Default ON. Off, no standing is tracked and nothing is derived."""
    return os.environ.get(
        "JARVIS_PERSONA_LEDGER_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def sample_floor() -> int:
    """Positions required before a rate means anything.

    Below this the reading is `insufficient` and inert. Eight is small enough
    to reach in a working day and large enough that one lucky call cannot
    manufacture a reputation.
    """
    return _env_int("JARVIS_PERSONA_SAMPLE_FLOOR", 8, 3, 200)


def echo_threshold() -> float:
    """Concordance above which agreement has stopped being informative."""
    return _env_float("JARVIS_PERSONA_ECHO_THRESHOLD", 0.90, 0.5, 1.0)


def rotation_threshold() -> float:
    """Calibration below which a persona is a candidate for rotation.

    A CANDIDATE, never a decision — rotation is a governed act and goes
    through the gate like any other consequential change.
    """
    return _env_float("JARVIS_PERSONA_ROTATION_THRESHOLD", 0.30, 0.0, 1.0)


class Reading:
    """A rate, its sample count, and whether it can be acted on."""

    __slots__ = ("rate", "samples", "provenance")

    def __init__(self, rate: float, samples: int, provenance: str) -> None:
        self.rate = float(rate)
        self.samples = int(samples)
        self.provenance = provenance

    @property
    def actionable(self) -> bool:
        """Only a MEASURED reading may trip a defence or a gate.

        The same rule `advisor_locality` applies to blast radius: a reading
        the system cannot stand behind is neutral, never authoritative.
        """
        return self.provenance == MEASURED

    @property
    def pct(self) -> str:
        return f"{round(self.rate * 100)}%" if self.actionable else "—"

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Reading {self.pct} n={self.samples} {self.provenance}>"


_INSUFFICIENT = Reading(0.0, 0, INSUFFICIENT)


class PersonaLedger:
    """Derived standing. Records outcomes; decides nothing on its own."""

    def __init__(self, window: int = _WINDOW) -> None:
        self._lock = threading.RLock()
        self._window = max(4, int(window))
        #: persona → deque of bools (did VERIFY agree with their position)
        self._calls: Dict[str, Deque[bool]] = {}
        #: (a, b) sorted → deque of bools (did they take the same side)
        self._pairs: Dict[Tuple[str, str], Deque[bool]] = {}
        #: op_id → {persona: position}, pending VERIFY
        self._open: Dict[str, Dict[str, str]] = {}

    # -- recording ---------------------------------------------------------

    def note_position(self, op_id: str, persona: str, position: str) -> None:
        """A persona took a side on an op. NEVER raises.

        Held open until VERIFY rules. A position with no outcome is not a
        data point — it is an opinion nobody checked.
        """
        try:
            if not ledger_enabled():
                return
            op = str(op_id or "").strip()
            who = str(persona or "").strip()
            side = str(position or "").strip().lower()
            if not op or not who or side not in (FOR, AGAINST):
                return
            with self._lock:
                self._open.setdefault(op, {})[who] = side
        except Exception:  # noqa: BLE001
            logger.debug("[PersonaLedger] note degraded", exc_info=True)

    def settle(self, op_id: str, verified: bool) -> int:
        """VERIFY ruled. Convert open positions into standing.

        Returns the number of positions settled. Concordance is recorded for
        every pair that BOTH took a side — including when they agreed and
        were both wrong, because agreeing wrongly is exactly the pattern the
        echo-chamber defence exists to notice.
        """
        try:
            if not ledger_enabled():
                return 0
            with self._lock:
                positions = self._open.pop(str(op_id or "").strip(), {})
                if not positions:
                    return 0
                truth = FOR if verified else AGAINST
                for who, side in positions.items():
                    self._calls.setdefault(
                        who, deque(maxlen=self._window),
                    ).append(side == truth)
                names = sorted(positions)
                for i, a in enumerate(names):
                    for b in names[i + 1:]:
                        self._pairs.setdefault(
                            (a, b), deque(maxlen=self._window),
                        ).append(positions[a] == positions[b])
                return len(positions)
        except Exception:  # noqa: BLE001
            logger.debug("[PersonaLedger] settle degraded", exc_info=True)
            return 0

    def abandon(self, op_id: str) -> None:
        """The op never reached VERIFY. Discard, never guess an outcome."""
        try:
            with self._lock:
                self._open.pop(str(op_id or "").strip(), None)
        except Exception:  # noqa: BLE001
            pass

    # -- reading -----------------------------------------------------------

    def calibration(self, persona: str) -> Reading:
        """How often VERIFY agreed with this persona. Being RIGHT."""
        return self._rate(self._calls.get(str(persona or "").strip()))

    def concordance(self, a: str, b: str) -> Reading:
        """How often these two took the same side. Being AGREEABLE.

        Not a virtue. A high value means their disagreement has stopped
        carrying information.
        """
        key = tuple(sorted((str(a or "").strip(), str(b or "").strip())))
        return self._rate(self._pairs.get(key))  # type: ignore[arg-type]

    def _rate(self, samples: Optional[Deque[bool]]) -> Reading:
        try:
            if not samples:
                return _INSUFFICIENT
            n = len(samples)
            if n < sample_floor():
                # Honest about not knowing. Two calls at 100% is not a track
                # record, and acting on it would fire someone for being new.
                return Reading(sum(samples) / n, n, INSUFFICIENT)
            return Reading(sum(samples) / n, n, MEASURED)
        except Exception:  # noqa: BLE001
            return _INSUFFICIENT

    # -- what the numbers imply (never what to DO) -------------------------

    def echo_chamber_risk(self) -> List[Tuple[str, str, Reading]]:
        """Pairs whose agreement has stopped being informative.

        Returned rather than acted on. This class derives; the orchestrator
        decides — and the decision (injecting a contrarian reviewer) is a
        behavioural change that belongs where behavioural changes are
        governed.
        """
        try:
            out = []
            with self._lock:
                pairs = list(self._pairs.items())
            for (a, b), samples in pairs:
                reading = self._rate(samples)
                if reading.actionable and reading.rate >= echo_threshold():
                    out.append((a, b, reading))
            return sorted(out, key=lambda row: -row[2].rate)
        except Exception:  # noqa: BLE001
            return []

    def rotation_candidates(self) -> List[Tuple[str, Reading]]:
        """Personas whose calls do not land. CANDIDATES, never decisions.

        Keyed on calibration ALONE. Rotating on concordance would select for
        agreement and converge the room on an echo chamber: the persona who
        disagrees constantly and is right most of the time is the most
        valuable one here, and a chemistry cull removes her first.
        """
        try:
            out = []
            with self._lock:
                calls = list(self._calls.items())
            for who, samples in calls:
                reading = self._rate(samples)
                if reading.actionable and reading.rate <= rotation_threshold():
                    out.append((who, reading))
            return sorted(out, key=lambda row: row[1].rate)
        except Exception:  # noqa: BLE001
            return []

    # -- render ------------------------------------------------------------

    def standing(self, persona: str) -> str:
        """``@cassandra · 7/9 landed`` — or "" while still unproven."""
        try:
            reading = self.calibration(persona)
            if not reading.samples:
                return ""
            landed = round(reading.rate * reading.samples)
            suffix = "" if reading.actionable else " (early)"
            return f"{persona} · {landed}/{reading.samples} landed{suffix}"
        except Exception:  # noqa: BLE001
            return ""

    def chip(self, a: str, b: str) -> str:
        """``⚔ @the-skeptic & @the-pit · tension 94%`` — or "".

        Tension is the INVERSE of concordance, shown only when it is high
        enough to be interesting. A chip that renders "tension 12%" on every
        pair is chrome, and chrome is not read.
        """
        try:
            reading = self.concordance(a, b)
            if not reading.actionable:
                return ""
            tension = 1.0 - reading.rate
            if tension >= 0.35:
                return f"⚔ {a} & {b} · tension {round(tension * 100)}%"
            if reading.rate >= echo_threshold():
                # Named as a warning, not a celebration.
                return f"🤝 {a} & {b} · agreement {reading.pct} (unchallenged)"
            return ""
        except Exception:  # noqa: BLE001
            return ""


_LEDGER: Optional[PersonaLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_persona_ledger() -> PersonaLedger:
    """Process-wide ledger — the producer (phase transitions) and the
    consumer (the TUI chips) sit in different layers with no handle to each
    other, the same reason the plan checklist is a singleton."""
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None:
            _LEDGER = PersonaLedger()
        return _LEDGER


def reset_ledger_for_tests() -> None:
    global _LEDGER
    with _LEDGER_LOCK:
        _LEDGER = None
