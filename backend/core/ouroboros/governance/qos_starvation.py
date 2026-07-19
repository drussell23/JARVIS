"""Dynamic QoS Priority Escalation (Aging) — background starvation math.

Operator mandate 2026-07-18: a sovereign OS does not wait for favorable
network weather. When the SensorGovernor throttles an exploration
envelope under liquidity constraints, the envelope is NOT dropped — it
is shunted here with a timestamp, its QoS weight AGES deterministically,
and once it breaches the escalation threshold while provider liquidity
has replenished, the next ingest tick re-admits it under a one-shot
escalation grant that pre-empts the weighted cap.

Design invariants:

  * **No tickers, no sleeps** (mandate 1): aging is a PURE function of
    ``now - starved_at`` evaluated lazily whenever the ledger is
    consulted — the "clock" is the intake loop's own activity.
  * **Middleware, not a task manager** (mandate 3): this module owns
    ONLY the starvation ledger + grant mint; the intake router's
    existing governor seam consumes grants and re-ingests. Liquidity
    truth comes from the existing ProviderLiquidityLedger — no second
    telemetry source.
  * **Anti-inversion ratio** (mandate 4): escalation pledges are capped
    at ``JARVIS_QOS_STARVATION_MAX_RATIO`` (default 0.30) of the
    replenished window's declared tokens — a starved BACKGROUND envelope
    can never crowd out CRITICAL foreground liquidity. The pledge window
    resets on the exhausted→healthy replenishment EDGE (detected lazily,
    again no timer).
  * Master ``JARVIS_QOS_ESCALATION_ENABLED`` — §33.1 default-FALSE;
    graduation soaks arm it. NEVER raises anywhere.

Aging heuristic (env-tunable, no hardcoding):

  linear (default):  weight(t) = 1 + SLOPE * (age_s / TICK_S)
  exp:               weight(t) = GROWTH ** (age_s / TICK_S)

Escalation fires at ``weight >= JARVIS_QOS_ESCALATION_THRESHOLD``
(default 3.0 — with defaults, a starved envelope escalates after
~4 minutes of starvation, liquidity permitting).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def qos_escalation_enabled() -> bool:
    """Master gate — §33.1 default FALSE. NEVER raises."""
    return os.environ.get(
        "JARVIS_QOS_ESCALATION_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def aging_mode() -> str:
    m = os.environ.get("JARVIS_QOS_AGING_MODE", "linear").strip().lower()
    return m if m in ("linear", "exp") else "linear"


def aging_tick_s() -> float:
    return max(1.0, _f("JARVIS_QOS_AGING_TICK_S", 60.0))


def aging_slope() -> float:
    return max(0.0, _f("JARVIS_QOS_AGING_SLOPE", 0.5))


def aging_growth() -> float:
    return max(1.0, _f("JARVIS_QOS_AGING_GROWTH", 1.35))


def escalation_threshold() -> float:
    return max(1.0, _f("JARVIS_QOS_ESCALATION_THRESHOLD", 3.0))


def max_starved() -> int:
    return max(4, _i("JARVIS_QOS_STARVATION_MAX", 32))


def starvation_max_ratio() -> float:
    """Anti-inversion: fraction of the replenished window's declared
    tokens pledgeable to starved background work. Clamped [0.05, 0.9]."""
    return min(0.9, max(0.05, _f("JARVIS_QOS_STARVATION_MAX_RATIO", 0.30)))


def pledge_tokens_estimate() -> int:
    """Deterministic per-escalation token pledge estimate."""
    return max(1_000, _i("JARVIS_QOS_PLEDGE_TOKENS_EST", 20_000))


def grant_ttl_s() -> float:
    return max(5.0, _f("JARVIS_QOS_GRANT_TTL_S", 120.0))


def aged_weight(age_s: float) -> float:
    """The aging heuristic — pure function of starvation age. NEVER
    raises; negative ages clamp to 0."""
    try:
        a = max(0.0, float(age_s)) / aging_tick_s()
        if aging_mode() == "exp":
            return aging_growth() ** a
        return 1.0 + aging_slope() * a
    except Exception:  # noqa: BLE001
        return 1.0


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class StarvedEntry:
    """One shunted envelope + its starvation clock."""

    causal_id: str
    envelope: Any
    starved_at: float
    reason: str
    source: str = ""
    escalations: int = 0

    def weight(self, now: Optional[float] = None) -> float:
        return aged_weight((now or time.time()) - self.starved_at)


@dataclass
class _PledgeWindow:
    """Token-pledge accounting for ONE replenished liquidity window."""

    window_tokens: int = 0
    pledged_tokens: int = 0
    was_exhausted: bool = False


class StarvationLedger:
    """Bounded, thread-safe starvation queue + escalation grant mint.

    All time math is lazy (caller-supplied ``now`` for tests). Dropping
    policy when full: the YOUNGEST entry is refused (the oldest starved
    work is closest to escalation — evicting it would reset the very
    starvation clock this exists to honor).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, StarvedEntry]" = OrderedDict()
        self._grants: Dict[str, float] = {}          # causal_id -> expiry
        self._pledge = _PledgeWindow()
        self.stats: Dict[str, int] = {
            "shunted": 0, "refused_full": 0, "escalated": 0,
            "grants_consumed": 0, "pledge_denied": 0,
        }

    # ---- shunt side (called at the governor deny seam) ----

    def shunt(
        self, envelope: Any, *, reason: str, now: Optional[float] = None,
    ) -> bool:
        """Park a throttled envelope instead of dropping it. Returns
        True when parked. NEVER raises."""
        try:
            if not qos_escalation_enabled():
                return False
            cid = str(getattr(envelope, "causal_id", "") or id(envelope))
            with self._lock:
                if cid in self._entries:
                    return True                      # already starving
                if len(self._entries) >= max_starved():
                    self.stats["refused_full"] += 1
                    return False
                self._entries[cid] = StarvedEntry(
                    causal_id=cid,
                    envelope=envelope,
                    starved_at=float(now if now is not None else time.time()),
                    reason=str(reason),
                    source=str(getattr(envelope, "source", "")),
                )
                self.stats["shunted"] += 1
            logger.info(
                "[QoSStarvation] shunted envelope=%s source=%s reason=%s "
                "(queue=%d)",
                cid[:16], getattr(envelope, "source", "?"), reason,
                len(self._entries),
            )
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[QoSStarvation] shunt degraded", exc_info=True)
            return False

    # ---- liquidity + pledge accounting ----

    def _refresh_pledge_window(self) -> Tuple[bool, int]:
        """Lazily track the exhausted→healthy replenishment EDGE and the
        window's declared token base. Returns (healthy, window_tokens)."""
        try:
            from backend.core.ouroboros.governance.provider_liquidity_ledger import (  # noqa: E501
                _load,
                any_runway_exhausted,
                liquidity,
            )
            exhausted = any_runway_exhausted()
            if exhausted:
                self._pledge.was_exhausted = True
                return (False, 0)
            # Healthy. On the replenishment edge, restart the window with
            # the currently-declared token base.
            tokens_total = 0
            for name in (_load().get("providers") or {}):
                t, _secs = liquidity(name)
                if t:
                    tokens_total += int(t)
            if self._pledge.was_exhausted or self._pledge.window_tokens <= 0:
                self._pledge = _PledgeWindow(
                    window_tokens=tokens_total, pledged_tokens=0,
                    was_exhausted=False,
                )
            return (True, self._pledge.window_tokens)
        except Exception:  # noqa: BLE001
            return (True, 0)   # ledger unknown → healthy, ratio can't bind

    def _pledge_allows(self) -> bool:
        healthy, window = self._refresh_pledge_window()
        if not healthy:
            return False
        if window <= 0:
            return True        # no declared base → the ratio cannot bind
        projected = self._pledge.pledged_tokens + pledge_tokens_estimate()
        if projected > starvation_max_ratio() * window:
            self.stats["pledge_denied"] += 1
            return False
        return True

    # ---- escalation side (called on ingest ticks) ----

    def escalatable(self, now: Optional[float] = None) -> List[StarvedEntry]:
        """Entries whose aged weight breaches the threshold, heaviest
        first. Pure read. NEVER raises."""
        try:
            t = float(now if now is not None else time.time())
            with self._lock:
                rows = [
                    e for e in self._entries.values()
                    if e.weight(t) >= escalation_threshold()
                ]
            rows.sort(key=lambda e: e.weight(t), reverse=True)
            return rows
        except Exception:  # noqa: BLE001
            return []

    def try_escalate(
        self, now: Optional[float] = None,
    ) -> Optional[StarvedEntry]:
        """Pop the heaviest escalatable entry IF liquidity is healthy
        AND the anti-inversion pledge cap permits — minting a one-shot
        grant that lets the re-ingest pre-empt the weighted cap.
        Returns the entry to re-ingest, or None. NEVER raises."""
        try:
            if not qos_escalation_enabled():
                return None
            rows = self.escalatable(now)
            if not rows:
                return None
            if not self._pledge_allows():
                return None
            entry = rows[0]
            t = float(now if now is not None else time.time())
            with self._lock:
                self._entries.pop(entry.causal_id, None)
                self._grants[entry.causal_id] = t + grant_ttl_s()
                self._pledge.pledged_tokens += pledge_tokens_estimate()
                entry.escalations += 1
                self.stats["escalated"] += 1
            logger.info(
                "[QoSStarvation] ESCALATED envelope=%s source=%s "
                "aged_weight=%.2f age_s=%d pledged=%d/%d (ratio=%.2f)",
                entry.causal_id[:16], entry.source, entry.weight(t),
                int(t - entry.starved_at), self._pledge.pledged_tokens,
                self._pledge.window_tokens, starvation_max_ratio(),
            )
            return entry
        except Exception:  # noqa: BLE001
            logger.debug("[QoSStarvation] try_escalate degraded", exc_info=True)
            return None

    def consume_grant(
        self, causal_id: str, now: Optional[float] = None,
    ) -> bool:
        """One-shot grant consumption at the governor seam. NEVER raises."""
        try:
            t = float(now if now is not None else time.time())
            with self._lock:
                expiry = self._grants.pop(str(causal_id), None)
            if expiry is None:
                return False
            if t > expiry:
                return False
            self.stats["grants_consumed"] += 1
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- introspection ----

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._entries)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "depth": len(self._entries),
                "grants_outstanding": len(self._grants),
                "pledged_tokens": self._pledge.pledged_tokens,
                "window_tokens": self._pledge.window_tokens,
                "stats": dict(self.stats),
            }


_DEFAULT_LEDGER: Optional[StarvationLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_default_ledger() -> StarvationLedger:
    global _DEFAULT_LEDGER
    with _LEDGER_LOCK:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = StarvationLedger()
        return _DEFAULT_LEDGER


def reset_default_ledger() -> None:
    """Test hygiene."""
    global _DEFAULT_LEDGER
    with _LEDGER_LOCK:
        _DEFAULT_LEDGER = None


__all__ = [
    "StarvationLedger",
    "StarvedEntry",
    "aged_weight",
    "aging_mode",
    "escalation_threshold",
    "get_default_ledger",
    "pledge_tokens_estimate",
    "qos_escalation_enabled",
    "reset_default_ledger",
    "starvation_max_ratio",
]
