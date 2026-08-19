"""Can the organism actually complete an operation right now?

THE DEFECT THIS CLOSES
----------------------
The cockpit header printed ``● healthy`` while both provider lanes were at
zero credit, every op was failing, and a GOAL had been orphaned to the DLQ.
The operator's only clue was a small ``⚠ doubleword dry`` token elsewhere on
the line. The header was computed like this::

    state = "HEALTHY"                                # optimistic default
    try:    state = get_reactive_theme().state.value # a UI THEME state
    except: pass                                     # swallow -> stays HEALTHY

Two optimistic defaults stacked on a CATEGORY ERROR. ``UIState`` answers "what
colour should the dot be"; the header rendered that answer as though it meant
"am I able to work". A presentation fact was standing in for a capability
fact -- the same proxy-for-property shape as a path-ban standing in for
archived-implementation identity, or a handler level standing in for audience.

This module answers the capability question, and ONLY that question. It does
not choose a colour. Keeping the two apart is the point: fuse them again and
the next refactor re-creates the bug.

WHAT IS FUSED (all pre-existing readings; no new network calls)
--------------------------------------------------------------
* **lane viability** -- ``provider_liquidity_ledger.any_runway_exhausted()``,
  the same file-backed ledger the status line already samples to render
  ``⚠ … dry``. That token proves the fact was ALREADY KNOWN at render time;
  it simply never reached the badge.
* **daemon heartbeat** -- injected by the caller when it has one.
* **operational telemetry** -- attempted/completed/failed from the session
  summaries ``ov status`` already parses.

Composition is STRICTEST-WINS, the same rule as ThroughputGovernor against
MemoryPressureGate: a capability fact dominates a presentation fact, and a dry
lane dominates an optimistic heartbeat.

ASYMMETRIC HYSTERESIS
---------------------
Degradation is deterministic and immediate: a dry runway is not jitter, it is
a billing state, and waiting to report it would be the whole original defect
in slower motion. Recovery requires a VERIFIED OPERATIONAL SUCCESS -- never a
timer, never merely the absence of the failing signal. A lane that flickers
back to "not exhausted" has proved nothing; an op that COMPLETED has.

That asymmetry is deliberate and matches the profiler's asymmetric EWMA:
cheap to admit trouble, expensive to claim health.

THE DEFAULT IS INVERTED
-----------------------
Unknown resolves to BLOCKED, never HEALTHY. If this module cannot read its
inputs it does not know whether the organism can work, and the honest
rendering of "I do not know" is not a green dot. Same rule as the Advisor's
``provenance=unknown`` and the gateway's ``resident_models() -> None``.

Python 3.9+. Stdlib only at import; every governance read is lazy and
fail-soft.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.CapabilityState")

ENABLED_ENV = "JARVIS_CAPABILITY_STATE_ENABLED"
FAIL_STREAK_ENV = "JARVIS_CAPABILITY_FAIL_STREAK"
TTL_ENV = "JARVIS_CAPABILITY_TTL_S"

_TRUTHY = ("1", "true", "yes", "on")


def capability_state_enabled() -> bool:
    """Master gate. Default ON. OFF restores the legacy theme-derived badge
    exactly, optimistic default included. NEVER raises."""
    try:
        return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def fail_streak_threshold() -> int:
    """Failed ops with ZERO completions before the state degrades. Default 3.

    Not 1: a single failure is an op, not a condition. Not 20: by then the
    operator has watched a paralysed organism for an hour, which is precisely
    the experience this exists to prevent."""
    try:
        return max(1, int(os.environ.get(FAIL_STREAK_ENV, "3")))
    except (TypeError, ValueError):
        return 3


def reading_ttl_s() -> float:
    """How long a fused reading stays fresh. Default 15s."""
    try:
        return max(0.0, float(os.environ.get(TTL_ENV, "15")))
    except (TypeError, ValueError):
        return 15.0


class Capability(str, enum.Enum):
    """What the organism can do — NOT what colour it should be."""

    HEALTHY = "healthy"    # a lane is viable and work has recently completed
    DEGRADED = "degraded"  # work is landing, but failures are present
    BLOCKED = "blocked"    # structurally cannot complete an op right now
    #: Blocked SPECIFICALLY for want of money. Distinguished from BLOCKED
    #: because the two demand different things of the operator: "blocked"
    #: sends them to a log to find out why, "unfunded" names the remedy in
    #: the word itself. Everything that treats BLOCKED as terminal must treat
    #: this identically — see `can_work`.
    UNFUNDED = "unfunded"
    UNKNOWN = "unknown"    # inputs unreadable — renders AS blocked

    @property
    def can_work(self) -> bool:
        return self in (Capability.HEALTHY, Capability.DEGRADED)

    @property
    def is_blocking(self) -> bool:
        """Terminal for dispatch. UNFUNDED is a REASON, not a lesser state —
        anything gating on "can this organism act" must include it."""
        return self in (Capability.BLOCKED, Capability.UNFUNDED,
                        Capability.UNKNOWN)


@dataclass(frozen=True)
class CapabilityReading:
    """A capability verdict carrying every input that produced it."""

    state: Capability
    reason: str
    lanes_dry: bool = False
    dry_provider: str = ""
    heartbeat_ok: Optional[bool] = None
    attempted: int = 0
    completed: int = 0
    failed: int = 0
    #: "fused" when every input was readable; "partial" when some were not;
    #: "unreadable" when none were. A verdict whose provenance is unstated
    #: gets believed as though it were measured.
    provenance: str = "fused"
    held_by_hysteresis: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def badge(self) -> str:
        """The word the header prints. UNKNOWN renders as BLOCKED because a
        state we cannot determine must not be shown as a green dot."""
        return ("blocked" if self.state is Capability.UNKNOWN
                else self.state.value)

    @property
    def is_funding_issue(self) -> bool:
        return self.state is Capability.UNFUNDED

    def render(self) -> str:
        return f"[Capability] {self.badge} — {self.reason}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value, "badge": self.badge,
            "reason": self.reason, "lanes_dry": self.lanes_dry,
            "dry_provider": self.dry_provider, "heartbeat_ok": self.heartbeat_ok,
            "attempted": self.attempted, "completed": self.completed,
            "failed": self.failed, "provenance": self.provenance,
            "held_by_hysteresis": self.held_by_hysteresis, **self.detail,
        }


class CapabilityEvaluator:
    """Fuses the readings and applies the asymmetric hysteresis.

    Process-scoped: the hysteresis needs memory of what it last reported and
    of how many ops had completed when it degraded, so that "recovered" can
    mean "something actually worked since then" rather than "time passed".
    """

    __slots__ = ("_lock", "_cached", "_cached_at", "_degraded_at_completed",
                 "_last_state")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: Optional[CapabilityReading] = None
        self._cached_at: float = 0.0
        #: completed-op count observed when we last degraded. Recovery
        #: requires this to be EXCEEDED -- a strictly greater number is the
        #: only evidence that work has landed since the trouble started.
        self._degraded_at_completed: Optional[int] = None
        self._last_state: Optional[Capability] = None

    # -- inputs, each independently fail-soft ------------------------------

    @staticmethod
    def _read_lanes() -> "tuple":
        """(dry, provider_name, readable) — no new probe, no API call.

        Asks :func:`economic_state.display_liquidity`, the same authority
        `status_line._sample_liquidity` uses, so the badge and the "⚠ … dry"
        token cannot disagree about which lane is out.

        This used to walk `runway_exhausted`, which is a ROUTING predicate: it
        fail-opens once the quota-outage WINDOW lapses, because routing wants a
        topped-up wallet to resume without manual clearing. The badge inherited
        that optimism and reported `healthy — lanes viable` over two accounts
        answering 400 and 402. A lapsed window means time passed, not that
        anyone paid, so the display keeps such a lane DRY.
        """
        try:
            from backend.core.ouroboros.governance import (  # noqa: PLC0415
                provider_liquidity_ledger as pl,
            )
            if not pl.liquidity_ledger_enabled():
                return (False, "", False)
            from backend.core.ouroboros.governance.economic_state import (  # noqa: PLC0415,E501
                display_liquidity,
            )
            view = display_liquidity()
            if not view.get("readable"):
                return (False, "", False)
            dry_lanes = view.get("dry") or []
            # The NAME stays the first dry lane (the badge shows one), while
            # `dry` is the roster-wide answer — so a half-dry fleet still
            # reports the specific lane rather than an arbitrary verdict.
            return (bool(dry_lanes), str(dry_lanes[0]) if dry_lanes else "",
                    True)
        except Exception:  # noqa: BLE001
            return (False, "", False)

    @staticmethod
    def _read_remote() -> "tuple":
        """(state, endpoint, readable) for the remote sovereign host.

        Reads `InferenceGateway.snapshot()`, which is PURE IN-MEMORY -- it
        consults cached breaker state and env, and deliberately does NOT call
        `resident_models()` (the only method that touches the network). That
        matters here: this evaluator runs on the render path, and a capability
        badge that blocked the UI thread on a LAN round-trip would be a worse
        defect than the one it replaces.

        Returns one of:
          * ``absent``      -- no remote configured; single-machine case
          * ``serving``     -- configured and the breaker is not open
          * ``unreachable`` -- breaker OPEN; the LAN is being bypassed
        """
        try:
            from backend.core.ouroboros.governance import (  # noqa: PLC0415
                inference_gateway as ig,
            )
            endpoint = ig.remote_endpoint()
            if not endpoint or not ig.gateway_enabled():
                return ("absent", "", True)
            snap = ig.get_default_gateway().snapshot() or {}
            host = (snap.get("hosts") or {}).get(endpoint) or {}
            state = str(host.get("state") or "")
            if state == ig.HostState.UNREACHABLE.value:
                return ("unreachable", endpoint, True)
            # HEALTHY, DEGRADED, PROBING, or never-contacted all mean the
            # lane is still a candidate. "Never contacted" is deliberately
            # NOT treated as unreachable: a configured fallback that has not
            # been tried yet is unverified, not broken, and reporting BLOCKED
            # would send the operator to buy credits they do not need.
            return ("serving", endpoint, True)
        except Exception:  # noqa: BLE001
            return ("absent", "", False)

    @staticmethod
    def _read_ops() -> "tuple":
        """(attempted, completed, failed, readable) from the most recent
        session summary — the same source `ov status` renders."""
        try:
            from backend.core.ouroboros.governance import (  # noqa: PLC0415
                last_session_summary as lss,
            )
            rec = lss.get_default_summary().load_sync()
            if rec is None:
                return (0, 0, 0, False)
            return (int(getattr(rec, "stats_attempted", 0) or 0),
                    int(getattr(rec, "stats_completed", 0) or 0),
                    int(getattr(rec, "stats_failed", 0) or 0), True)
        except Exception:  # noqa: BLE001
            return (0, 0, 0, False)

    # -- the verdict -------------------------------------------------------

    def evaluate(self, *, heartbeat_ok: Optional[bool] = None,
                 now: Optional[float] = None) -> CapabilityReading:
        """Fuse and decide. NEVER raises."""
        if not capability_state_enabled():
            return CapabilityReading(
                state=Capability.HEALTHY, reason="capability state disabled",
                provenance="disabled")
        try:
            return self._evaluate_inner(heartbeat_ok=heartbeat_ok, now=now)
        except Exception as exc:  # noqa: BLE001
            # Even the failure path refuses to claim health.
            logger.debug("[Capability] degraded", exc_info=True)
            return CapabilityReading(
                state=Capability.UNKNOWN,
                reason=f"capability fusion failed: {type(exc).__name__}",
                provenance="unreadable")

    def _evaluate_inner(self, *, heartbeat_ok: Optional[bool],
                        now: Optional[float]) -> CapabilityReading:
        _now = time.monotonic() if now is None else float(now)
        ttl = reading_ttl_s()
        with self._lock:
            if (ttl > 0.0 and self._cached is not None
                    and (_now - self._cached_at) < ttl):
                return self._cached

        dry, dry_provider, lanes_readable = self._read_lanes()
        attempted, completed, failed, ops_readable = self._read_ops()
        remote_state, remote_endpoint, remote_readable = self._read_remote()
        readable = sum((lanes_readable, ops_readable, remote_readable,
                        heartbeat_ok is not None))
        provenance = ("fused" if readable >= 2
                      else "partial" if readable == 1 else "unreadable")

        state, reason, held = self._decide(
            dry=dry, dry_provider=dry_provider, lanes_readable=lanes_readable,
            heartbeat_ok=heartbeat_ok, attempted=attempted,
            completed=completed, failed=failed, ops_readable=ops_readable,
            remote_state=remote_state, remote_endpoint=remote_endpoint,
        )

        reading = CapabilityReading(
            state=state, reason=reason, lanes_dry=dry,
            dry_provider=dry_provider, heartbeat_ok=heartbeat_ok,
            attempted=attempted, completed=completed, failed=failed,
            provenance=provenance, held_by_hysteresis=held,
            detail={"fail_streak_threshold": fail_streak_threshold(),
                    "remote_state": remote_state,
                    "remote_endpoint": remote_endpoint},
        )
        with self._lock:
            self._cached = reading
            self._cached_at = _now
            changed = self._last_state is not state
            self._last_state = state
            # `is_blocking`, not `is BLOCKED`. Arming the hysteresis mark on
            # the enum member meant that once UNFUNDED existed, a funding
            # degrade no longer armed it — so the "recovery requires a
            # VERIFIED SUCCESS" rule silently stopped applying to the most
            # common degrade there is. Every gate on "can this act" must ask
            # the property, never the member.
            if state.is_blocking and self._degraded_at_completed is None:
                self._degraded_at_completed = completed
            elif state is Capability.HEALTHY:
                self._degraded_at_completed = None
        if changed:
            logger.info("%s", reading.render())
        return reading

    def _decide(self, *, dry: bool, dry_provider: str, lanes_readable: bool,
                heartbeat_ok: Optional[bool], attempted: int, completed: int,
                failed: int, ops_readable: bool,
                remote_state: str = "absent",
                remote_endpoint: str = "") -> "tuple":
        """The fusion rules, strictest-wins. Returns (state, reason, held)."""
        # 1. A dry runway is DETERMINISTIC, not jitter -- it is a billing
        #    state. Degrading immediately is correct; waiting for a streak
        #    would reproduce the original defect in slower motion.
        if dry:
            who = dry_provider or "provider"
            # A DRY PAID LANE IS ONLY BLOCKING IF THERE IS NOWHERE ELSE TO GO.
            #
            # Before the sovereign tier existed, "no runway" and "cannot work"
            # were the same sentence. They are not any more: BACKGROUND and
            # SPECULATIVE can run on the local host, so an exhausted card is a
            # COST condition, not a capability one. Reporting BLOCKED here
            # would send the operator to buy credits while the organism was
            # working perfectly well on its own GPU -- a false alarm that
            # teaches them to distrust the badge, which is how a badge stops
            # being read at all.
            if remote_state == "serving":
                return (Capability.DEGRADED,
                        f"no runway on {who} — running local on "
                        f"{remote_endpoint or 'the sovereign host'}", False)
            if remote_state == "unreachable":
                return (Capability.UNFUNDED,
                        f"{who} is out of credit and {remote_endpoint} is "
                        f"unreachable — add credits, or bring the local host "
                        f"up", False)
            # THE REASON NAMES THE REMEDY.
            #
            # "cannot dispatch work" states the symptom and leaves the
            # operator to discover the cause; the cause is already in hand.
            # And the state word is UNFUNDED rather than BLOCKED because
            # money is the one blocker the organism can never clear itself —
            # the distinction is precisely what tells the operator this is
            # theirs to fix.
            return (Capability.UNFUNDED,
                    f"{who} is out of credit — add credits, or configure a "
                    f"local lane to keep background work moving", False)

        # 2. An explicit heartbeat failure outranks op history: the ops may
        #    simply not have been attempted yet.
        if heartbeat_ok is False:
            return (Capability.BLOCKED, "daemon heartbeat is down", False)

        # 3. HYSTERESIS. Once blocked, only a VERIFIED SUCCESS restores
        #    health -- strictly more completions than when we degraded. The
        #    mere disappearance of the failing signal proves nothing, and a
        #    timer proves less than that.
        with self._lock:
            degraded_mark = self._degraded_at_completed
        if degraded_mark is not None:
            if ops_readable and completed > degraded_mark:
                return (Capability.HEALTHY,
                        f"recovered — {completed - degraded_mark} op(s) "
                        f"completed since the lane came back", False)
            return (Capability.BLOCKED,
                    "lane recovered but no op has completed yet — "
                    "holding blocked until work verifiably lands", True)

        # 4. Nothing readable -> UNKNOWN, which renders as blocked.
        if not ops_readable and not lanes_readable and heartbeat_ok is None:
            return (Capability.UNKNOWN,
                    "no capability signal readable", False)

        # 5. Op telemetry.
        if ops_readable and attempted > 0:
            if completed == 0 and failed >= fail_streak_threshold():
                return (Capability.BLOCKED,
                        f"{failed} of {attempted} recent ops failed, none "
                        f"completed", False)
            if failed > 0:
                return (Capability.DEGRADED,
                        f"{completed}/{attempted} ops completed, "
                        f"{failed} failed", False)
            if completed > 0:
                return (Capability.HEALTHY,
                        f"{completed}/{attempted} ops completed", False)

        # 6. Lanes viable, heartbeat fine, but nothing has been attempted.
        #    That is genuinely healthy-idle, and only reachable when at least
        #    one input was readable (step 4 caught the blind case).
        return (Capability.HEALTHY, "lanes viable, idle", False)

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
            self._cached_at = 0.0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            r = self._cached
        return r.to_dict() if r is not None else {"cached": False}


_SINGLETON: Optional[CapabilityEvaluator] = None
_LOCK = threading.Lock()


def get_default_evaluator() -> CapabilityEvaluator:
    global _SINGLETON
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = CapabilityEvaluator()
        return _SINGLETON


def current_badge(*, heartbeat_ok: Optional[bool] = None) -> str:
    """One-line convenience for the header. NEVER raises."""
    try:
        return get_default_evaluator().evaluate(heartbeat_ok=heartbeat_ok).badge
    except Exception:  # noqa: BLE001
        return "blocked"


def reset_for_tests() -> None:
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None
