"""One line, above the prompt, when something is wrong.

`ov doctor` proves the whole chain and prints a table. It is the right tool
for "what exactly is broken", and the wrong one for "is anything broken" —
because it only answers when asked, and the operator asks after they have
already lost time to a symptom they could not explain.

So the cockpit carries a single advisory line::

    ✘ hydration severed · run `ov doctor`

A PROJECTION, not a second opinion
----------------------------------
The verdicts come from `ov_doctor._verdicts_from_hydration` — the doctor's own
function, reading the same hydration frame the cockpit already receives at
attach. Nothing is re-probed and no second set of thresholds exists, so the
badge and the doctor cannot disagree about whether the organism is healthy.
Two health surfaces that can contradict each other are worse than one, because
the operator then has to decide which to believe.

That also means this costs nothing: the frame arrives regardless, and the
advisory is derived from bytes already in hand.

One line, worst first
---------------------
There is room for one advisory, so it shows the most severe — SEVERED before
DEGRADED — and says how many others are waiting rather than cycling through
them. A line that changes every second is read as noise.

ABSENT is never shown. An optional surface nobody configured is not a fault,
and reporting it trains the operator to ignore the line, which costs them the
one time it matters.

Saying what to DO
-----------------
Every advisory names its remedy. `✘ hydration severed` alone leaves the
operator exactly where they started; the verb that investigates it is the
whole point of surfacing the problem at all.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.HealthAdvisory")

__all__ = ["HealthAdvisor", "advisory_enabled", "advisories_from_hydration"]


def advisory_enabled() -> bool:
    """Default ON. Off, the cockpit is silent about health until asked."""
    return os.environ.get(
        "JARVIS_HEALTH_ADVISORY_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


#: Severity order. ABSENT and SKIPPED are deliberately missing — see below.
_RANK: Dict[str, int] = {"SEVERED": 2, "DEGRADED": 1}


def _min_rank() -> int:
    """Lowest severity worth one line of the operator's screen.

    SEVERED only, by default. Measured against a real hydration frame, the
    doctor reports DEGRADED for perfectly ordinary conditions — "no fabrics
    block (older daemon?)", "no liquidity data in hydration" — because its
    table has room to explain them and this line does not. Surfacing those
    would put a warning on screen during a completely healthy session, and a
    line that always complains is one the operator stops reading. Then it
    fails exactly when it finally has something true to say.

    `JARVIS_HEALTH_ADVISORY_MIN=degraded` opts into the noisier reading.
    """
    raw = os.environ.get("JARVIS_HEALTH_ADVISORY_MIN", "").strip().lower()
    return 1 if raw in ("degraded", "warn", "all", "1") else 2


def advisories_from_hydration(
    payload: Optional[Dict[str, Any]],
) -> List[Tuple[int, str, str]]:
    """``[(rank, edge, detail), …]`` worst-first. NEVER raises.

    Delegates entirely to the doctor's verdict builder. If that import fails
    the cockpit simply has nothing to say about health, which is the correct
    degradation: inventing a local health check here is exactly how the two
    surfaces would start disagreeing.
    """
    try:
        if not advisory_enabled():
            return []
        from backend.core.ouroboros.cli.ov_doctor import (
            _verdicts_from_hydration,
        )
        out: List[Tuple[int, str, str]] = []
        for verdict in _verdicts_from_hydration(payload) or []:
            state = getattr(getattr(verdict, "state", None), "value", "")
            rank = _RANK.get(str(state), 0)
            if rank < _min_rank():
                # OK is not news. ABSENT is an optional surface nobody
                # configured — reporting it trains the operator to ignore
                # this line, which costs them the one time it matters.
                # SKIPPED is downstream of a severed edge already shown.
                continue
            out.append((rank, str(getattr(verdict, "edge", "")),
                        str(getattr(verdict, "detail", ""))))
        out.sort(key=lambda row: -row[0])
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[HealthAdvisory] derive degraded", exc_info=True)
        return []


class HealthAdvisor:
    """Holds the current advisory and renders one line for it."""

    def __init__(self, remedy: str = "ov doctor") -> None:
        self._rows: List[Tuple[int, str, str]] = []
        self._remedy = str(remedy or "ov doctor")
        #: Set once the operator has seen it and acted; cleared by the next
        #: hydration that still reports a fault, so dismissing is not the same
        #: as fixing.
        self._muted = False

    # -- input -------------------------------------------------------------

    def observe_hydration(self, payload: Any) -> bool:
        """Fold one hydration frame in. True when the advisory CHANGED.

        Returning the change lets the caller repaint only on a transition
        rather than on every heartbeat — the line is stable text, and
        redrawing it at frame rate is how a status line becomes a flicker.
        """
        try:
            rows = advisories_from_hydration(
                payload if isinstance(payload, dict) else None,
            )
            changed = rows != self._rows
            if changed:
                self._rows = rows
                # A NEW fault un-mutes: the operator dismissed the previous
                # one, not this one.
                self._muted = False
            return changed
        except Exception:  # noqa: BLE001
            return False

    def mute(self) -> None:
        """Acknowledge the current advisory without pretending it is fixed."""
        self._muted = True

    # -- state -------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        return not self._rows

    @property
    def worst(self) -> Optional[Tuple[int, str, str]]:
        return self._rows[0] if self._rows else None

    @property
    def count(self) -> int:
        return len(self._rows)

    # -- render ------------------------------------------------------------

    def render(self, width: int = 0) -> str:
        """``✘ hydration severed · run `ov doctor``` or "" when healthy.

        Empty when there is nothing wrong — a health line that is always
        present is chrome, and chrome is not read.
        """
        try:
            if self._muted or not self._rows:
                return ""
            rank, edge, detail = self._rows[0]
            glyph = "✘" if rank >= 2 else "▲"
            # The edge names are numbered for the doctor's table ("3
            # hydration"); the number is a row index there and noise here.
            label = " ".join(str(edge).split()[1:]) or str(edge)
            body = f"{label}: {detail}" if detail else label
            more = f" (+{len(self._rows) - 1})" if len(self._rows) > 1 else ""
            line = f"{glyph} {body}{more} · run `{self._remedy}`"
            if width and len(line) > width > 12:
                # Clip the DETAIL, never the remedy: an advisory that loses
                # its verb is a complaint the operator cannot act on.
                keep = max(0, width - len(f"{glyph} … · run `{self._remedy}`"))
                line = (f"{glyph} {body[:keep].rstrip()}… · "
                        f"run `{self._remedy}`")
            return line
        except Exception:  # noqa: BLE001
            return ""
