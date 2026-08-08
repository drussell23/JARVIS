"""What a session costs, learned from the sessions that already ran.

WHY THIS EXISTS
---------------
The cockpit's cost ceiling was the literal ``"2.50"``, written twice — once for
the detached cold boot and once for the launchd agent. Two copies of a magic
number, and no record anywhere of what it was based on.

It was not a bad guess. This repository holds 772 sessions with a recorded
``cost_total``, and their 95th percentile is $0.77 against a maximum of $5.95,
so a ceiling near $2.50 sits about where a considered one would. That is
precisely the problem: a number that happens to be right, asserted with no
basis, is indistinguishable from one that happens to be wrong. When the
economics change — a cheaper model, a new provider, a quiet week — nothing
tells anyone the constant has drifted away from reality.

So the ceiling is now DERIVED from the sessions that actually spent money, and
it carries the basis it was derived from. A proactive organism should not need
to be told what it can afford; it has been keeping the receipts.

WHAT THE CEILING IS FOR
-----------------------
A safety ceiling, not a forecast. It must be generous enough that an ordinary
productive session is never truncated, and tight enough that a runaway is
caught before it empties an account. That is a high quantile of observed spend
multiplied by headroom — not a mean, which a single expensive session drags,
and not a maximum, which IS the runaway case.

Sessions that cost nothing are excluded from the quantile. Two thirds of the
history spent something; the rest booted, found no work, and exited. Including
them would drag the quantile toward zero and produce a ceiling that stops the
first session that tries to do anything.

READING IS CHEAP, ON PURPOSE
----------------------------
Session directories are named ``bt-YYYY-MM-DD-HHMMSS``, so lexical order IS
chronological order. The newest N are selected by sorting names — no ``stat``
call per directory, and no walking 950 of them at every boot. This runs once,
during a cold boot, and must not become a reason the cockpit feels slow.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("Ouroboros.SessionEconomics")

#: Where sessions record what they spent.
_SESSIONS_ENV = "JARVIS_SESSIONS_DIR"
_SESSIONS_DEFAULT = ".ouroboros/sessions"

#: The quantile of observed spend the ceiling is built on. High rather than
#: central: the ceiling exists to not truncate a normal session, and the
#: median session is far cheaper than the expensive-but-legitimate one.
_QUANTILE_ENV = "JARVIS_COCKPIT_CAP_QUANTILE"
#: Multiplier over that quantile. This is the whole safety margin, and the one
#: number an operator might reasonably want to move.
_HEADROOM_ENV = "JARVIS_COCKPIT_CAP_HEADROOM"
#: How many recent sessions inform the estimate. Bounded so a long-lived
#: repository does not make a cold boot read thousands of files.
_SAMPLE_ENV = "JARVIS_COCKPIT_CAP_SAMPLE"
#: Used ONLY when the history has nothing to say. Reported as unmeasured so it
#: is never mistaken for an observation.
_FALLBACK_ENV = "JARVIS_COCKPIT_CAP_FALLBACK"

#: The smallest ceiling worth booting with. A derived value below this means
#: the history is unrepresentative (a quiet week, a fresh clone), not that the
#: organism should be unable to complete a single operation.
_FLOOR_ENV = "JARVIS_COCKPIT_CAP_FLOOR"


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.debug("[Economics] ignoring malformed %s=%r", name, raw)
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.debug("[Economics] ignoring malformed %s=%r", name, raw)
        return default
    return value if value > 0 else default


def sessions_dir() -> Path:
    return Path(os.environ.get(_SESSIONS_ENV, "") or _SESSIONS_DEFAULT)


def observed_session_costs(limit: Optional[int] = None) -> List[float]:
    """Costs of the most recent sessions that spent anything, newest first.

    Selection is by directory NAME, not mtime: ``bt-YYYY-MM-DD-HHMMSS`` sorts
    lexically in chronological order, so the newest N are found without a stat
    per directory. A malformed or truncated summary is skipped rather than
    treated as a zero — an unreadable session is not a free one.
    """
    limit = limit if limit is not None else _env_int(_SAMPLE_ENV, 200)
    root = sessions_dir()
    try:
        names = sorted(
            (e.name for e in os.scandir(root) if e.is_dir()), reverse=True,
        )[:limit]
    except OSError:
        return []

    costs: List[float] = []
    for name in names:
        try:
            with open(root / name / "summary.json", "r", encoding="utf-8") as fh:
                total = json.load(fh).get("cost_total")
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            value = float(total)
            # Zero-cost sessions booted, found nothing, and exited. They are
            # real sessions but they carry no information about what work
            # costs, and including them drags the quantile toward a ceiling
            # that would stop the first session that tries to do anything.
            if value > 0.0:
                costs.append(value)
    return costs


def _quantile(sorted_values: List[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(q * len(sorted_values))
    return sorted_values[min(max(idx, 0), len(sorted_values) - 1)]


def _censored(ordered: List[float]) -> bool:
    """Is this distribution truncated by the ceiling it is meant to inform?

    A cap does not merely limit spend — it limits the RECORD of spend. Every
    session it stopped is written down at almost exactly the cap, so the
    largest observations cluster on one value. Estimating a new ceiling from
    that tail is circular: it extrapolates from numbers the old ceiling chose.

    The signature is a pile-up, not a maximum. One session near the top is an
    ordinary tail; several within a hair of each other is a wall. Detected
    rather than assumed, because today's history happens to be clean (a single
    session at $2.4956 under a $2.50 cap) and that is exactly the condition
    that will change quietly.
    """
    if len(ordered) < 4:
        return False
    top = ordered[-1]
    if top <= 0:
        return False
    return sum(1 for v in ordered if v >= top * 0.99) > 1


def _aegis_ceiling() -> Optional[float]:
    """The session budget Aegis already enforces, if an operator declared one.

    Read rather than duplicated. Aegis is the existing budget authority in this
    system; a cockpit ceiling that ignored it would be a second budget, and two
    budgets means the effective limit is whichever one nobody remembered.
    """
    try:
        from backend.core.ouroboros.aegis.flags import (
            ENV_AEGIS_SESSION_CAP_USD,
        )
    except Exception:  # noqa: BLE001
        return None
    raw = str(os.environ.get(ENV_AEGIS_SESSION_CAP_USD, "")).strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def derived_cost_cap() -> Tuple[float, str]:
    """The session ceiling and the basis it rests on.

    Returns ``(usd, basis)``. The basis is not decoration — it is the
    difference between "this ceiling reflects 143 sessions of real spend" and
    "nobody has ever measured this", and a surface that shows the number
    without it repeats the exact defect this replaces.
    """
    quantile = min(max(_env_float(_QUANTILE_ENV, 0.95), 0.5), 0.999)
    headroom = _env_float(_HEADROOM_ENV, 3.0)
    floor = _env_float(_FLOOR_ENV, 0.50)
    fallback = _env_float(_FALLBACK_ENV, 2.50)

    costs = observed_session_costs()
    if not costs:
        return (fallback, "unmeasured — no prior session recorded a cost")

    ordered = sorted(costs)
    note = ""
    if _censored(ordered):
        # The tail is the old ceiling's shadow, not evidence. Step the
        # quantile down into the body of the distribution, where the numbers
        # are sessions that ended because they finished rather than because
        # they were stopped — and say so, so the estimate is never mistaken
        # for a reading of the full range.
        quantile = min(quantile, 0.75)
        note = " (tail censored by a prior cap; estimated from the body)"

    base = _quantile(ordered, quantile)
    derived = base * headroom

    # Never propose more than the budget authority that already exists. Aegis
    # enforces a session cap when one is declared; a cockpit ceiling above it
    # would be a number that can never be reached, displayed as though it
    # were the limit.
    aegis = _aegis_ceiling()
    if aegis is not None and derived > aegis:
        return (
            aegis,
            f"clamped to the Aegis session cap ${aegis:.2f}"
            f" (observed would allow ${derived:.2f})",
        )

    if derived < floor:
        # The history is real but unrepresentative — a quiet week, or a repo
        # whose recent work was all free. Reported honestly rather than
        # silently rounded up, so the number and its reason still agree.
        return (
            floor,
            f"floor — {len(costs)} sessions, p{int(quantile * 100)}"
            f"=${base:.2f}, below the floor{note}",
        )

    return (
        derived,
        f"observed — {len(costs)} sessions, p{int(quantile * 100)}"
        f"=${base:.2f} x{headroom:g}{note}",
    )


def cockpit_cost_cap() -> Tuple[str, str]:
    """The ceiling a cold boot should use, as a string, with its basis.

    ONE definition, replacing the literal ``"2.50"`` that appeared in both the
    detached-spawn path and the launchd agent. Two copies of a budget is two
    budgets, and the one that drifts is discovered by an operator wondering
    why the number on screen is not the number they configured.

    An explicit ``JARVIS_COCKPIT_COST_CAP`` always wins and says so: an
    operator who set a ceiling deserves to see their own number, not a
    negotiated one.
    """
    explicit = str(os.environ.get("JARVIS_COCKPIT_COST_CAP", "")).strip()
    if explicit:
        return (explicit, "operator")
    try:
        usd, basis = derived_cost_cap()
        return (f"{usd:.2f}", basis)
    except Exception:  # noqa: BLE001 — a boot must never fail over a budget
        logger.debug("[Economics] derivation failed", exc_info=True)
        return (f"{_env_float(_FALLBACK_ENV, 2.50):.2f}", "unmeasured — error")
