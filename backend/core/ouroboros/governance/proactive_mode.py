"""proactive_mode — the ladder, and who is allowed to loosen it.

PRD §30. This is the STATE layer: it owns the ladder vocabulary, the
multi-cockpit composition law, and the boundary arithmetic. It owns no
enforcement — every position is expressed through a primitive that already
exists and is already graduated, because a second enforcement path is a
second thing to keep correct while the first is load-bearing.

THE TWO AXES
------------
The shipped dial (``trust_repl``) answers *may it apply?* in three positions.
The operator has two questions, and nothing answered the other one: at
``approval_required`` — the strictest position expressible — sixteen sensors
still fire, ops still enter the queue, and tokens are still spent. The dial
stops the landing, never the taking off.

So a position is a pair::

    initiative ∈ { none, observe, explore, act }   the right to BEGIN
    authority  ∈ { propose, notify, auto, promote } the right to CONCLUDE

Presenting a 2-D space as a 1-D dial is deliberate: ``Shift+Tab`` must stay
one keystroke. The dial therefore walks a **monotone path** — each step is
less autonomous in *both* coordinates — which is what makes "cycle"
meaningful. A path that traded one axis against the other would leave the
operator unable to say whether they had just loosened or tightened.

THE COMPOSITION LAW, AND WHY IT IS DERIVED
-------------------------------------------
``JARVIS_MIN_RISK_TIER`` is process-global, so two attached cockpits share
one dial and today the last writer wins **silently** — an operator can
tighten the organism and have a colleague loosen it with neither seeing.

The effective position is therefore the **strictest requested by any live
cockpit**, and it is *computed on read* rather than stored. That is the whole
fix for the disconnect race: a cached effective value has a window between a
cockpit vanishing and the recomputation, and a derived one has no window
because there is nothing to be stale.

ABSENCE IS NOT CONSENT TO LOOSEN
---------------------------------
The subtler half. If a request evaporated the instant its cockpit dropped,
then an operator on flaky café Wi-Fi who set ``watch`` would find the
organism becoming *more* autonomous at exactly the moment they lost
visibility — their caution undone by their disconnection.

So a detached cockpit's request is **retained** for a grace window and only
then released. This is §26.6's rule ("absence must not read as refusal")
applied to the dial: absence must not read as *permission* either. The
retention window composes with §29's ``SessionRegistry``, which already holds
detached sessions for exactly this reason.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ProactiveMode")

PROACTIVE_MODE_SCHEMA_VERSION: str = "proactive_mode.1"


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Master switch. Default **false** pending graduation.

    OFF is not a degraded mode: the ladder reduces to the three shipped
    positions and ``trust_repl`` behaves exactly as it did.
    """
    return _env_bool("JARVIS_PROACTIVE_MODE_ENABLED", False)


def request_grace_s() -> float:
    """How long a detached cockpit's request is honoured. Default 900s.

    Matches ``link_protocol.session_idle_expiry_s``'s default deliberately:
    a cockpit that has gone is the same event on both sides of the link, and
    two windows that could disagree would let a session resume into an
    autonomy level its own request had already expired out of.
    """
    return _env_float("JARVIS_PROACTIVE_MODE_GRACE_S", 900.0, minimum=1.0)


def coalesce_window_s() -> float:
    """Window over which rapid dial input is coalesced. Default 0.15s.

    A held ``Shift+Tab`` on a TTY is a repeat RATE, not a discrete event —
    a terminal delivers no key-up — so a burst is ordinary operator input
    rather than a fault. Coalescing bounds the work without discarding
    intent: see :meth:`ProactiveModeController.cycle`.
    """
    return _env_float("JARVIS_PROACTIVE_MODE_COALESCE_S", 0.15, minimum=0.0)


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


class Initiative(str, enum.Enum):
    """The right to BEGIN."""

    NONE = "none"
    OBSERVE = "observe"
    EXPLORE = "explore"
    ACT = "act"


class Authority(str, enum.Enum):
    """The right to CONCLUDE."""

    PROPOSE = "propose"
    NOTIFY = "notify"
    AUTO = "auto"
    PROMOTE = "promote"


@dataclass(frozen=True)
class Position:
    """One rung. Ordered by ``rank``: 0 is loosest, higher is stricter."""

    name: str
    rank: int
    initiative: Initiative
    authority: Authority
    glyph: str
    #: The ``risk_tier_floor`` value this rung asserts, or None for the
    #: no-floor resting state. NEVER a second floor implementation — this is
    #: the existing knob's value, chosen by the rung.
    risk_floor: Optional[str]
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "rank": self.rank,
            "initiative": self.initiative.value,
            "authority": self.authority.value,
            "glyph": self.glyph, "risk_floor": self.risk_floor,
            "summary": self.summary,
        }


#: The ladder, loosest → strictest. A CLOSED taxonomy, deliberately: this is
#: a vocabulary two machines and two operators must agree on, not a tunable.
#: Every threshold, window and cap around it is env-backed; the rungs are not,
#: because a fleet that could invent a rung could invent one nobody else
#: understands.
LADDER: Tuple[Position, ...] = (
    Position("promote", 0, Initiative.ACT, Authority.PROMOTE, "🔵", None,
             "verified work lands on the operator tree (§26 Gate 3)"),
    Position("safe_auto", 1, Initiative.ACT, Authority.AUTO, "🟢", None,
             "applies as classified — green auto, yellow after its window"),
    Position("notify_apply", 2, Initiative.ACT, Authority.NOTIFY, "🟡",
             "notify_apply",
             "every apply shows its diff before landing"),
    Position("approval_required", 3, Initiative.ACT, Authority.PROPOSE, "🟠",
             "approval_required",
             "generates and proposes; nothing lands without a human"),
    Position("explore", 4, Initiative.EXPLORE, Authority.PROPOSE, "🔍",
             "approval_required",
             "reads, searches, plans — no mutation reaches VALIDATE"),
    Position("watch", 5, Initiative.NONE, Authority.PROPOSE, "⏸",
             "approval_required",
             "narrates only; nothing is admitted"),
)

_BY_NAME: Dict[str, Position] = {p.name: p for p in LADDER}

#: The loosest rung the operator may reach WITHOUT it having been earned.
#: `promote` is gated on §26's evidence, so it is not simply a dial position;
#: see :func:`reachable`.
_DEFAULT_NAME = "safe_auto"


def position(name: Optional[str]) -> Position:
    """Resolve a rung by name, falling back to the resting state.

    An unknown name resolves to the DEFAULT rather than raising, matching
    ``trust_repl.current_floor``'s existing posture: an unparseable dial must
    not be able to take the organism down, and the default is the shipped
    resting state rather than the loosest rung.
    """
    return _BY_NAME.get(str(name or "").strip().lower(),
                        _BY_NAME[_DEFAULT_NAME])


def strictest(*names: Optional[str]) -> Position:
    """The strictest of the given rungs. Empty input yields the default."""
    best: Optional[Position] = None
    for name in names:
        if name is None:
            continue
        cand = _BY_NAME.get(str(name).strip().lower())
        if cand is None:
            continue
        if best is None or cand.rank > best.rank:
            best = cand
    return best or _BY_NAME[_DEFAULT_NAME]


def reachable() -> Tuple[Position, ...]:
    """Rungs this host may currently occupy — computed, never hardcoded.

    ``promote`` is omitted unless §26's actuator is actually armed. A dial
    that accepted a position the host cannot honour would report an autonomy
    level that does not exist, which is the §25.4 dishonesty this codebase
    spends sections removing. The check reads the flag the gate itself reads,
    so the two cannot disagree.
    """
    out: List[Position] = []
    for pos in LADDER:
        if pos.name == "promote" and not _promotion_armed():
            continue
        out.append(pos)
    return tuple(out)


def _promotion_armed() -> bool:
    """Is §26 Gate 3 actually enabled? NEVER raises."""
    return _env_bool("JARVIS_WORKSPACE_PROMOTION_ENABLED", False)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass
class _Request:
    cockpit_id: str
    name: str
    requested_at: float
    #: None while attached; the monotonic instant of detachment otherwise.
    detached_at: Optional[float] = None


class ProactiveModeController:
    """Holds every cockpit's request and derives the effective rung.

    Thread-safe, lock-guarded, and **never holds a lock across a call into
    another subsystem** — the effects in :meth:`apply_effects` run outside
    the lock, because a governance primitive that blocked while this held the
    dial would deadlock the very loop the dial is meant to steer.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._requests: Dict[str, _Request] = {}
        self._last_applied: Optional[str] = None
        self._last_cycle_at: float = 0.0
        self._pending_steps: int = 0
        self._transitions = 0
        self._boundary_hits = 0

    # -- requests --------------------------------------------------------

    def request(self, cockpit_id: str, name: str) -> Position:
        """Record one cockpit's requested rung. Returns the EFFECTIVE rung.

        The caller does not necessarily get what it asked for, and that is
        the point: a cockpit asking for a looser rung than another live
        cockpit has requested is told the effective value so it can render
        that it is being overridden. Silently discarding the request — the
        current behaviour — is what makes an operator stop tightening.
        """
        cid = str(cockpit_id or "?")
        resolved = position(name)
        with self._lock:
            self._requests[cid] = _Request(cid, resolved.name, self._clock())
        return self.effective()

    def detach(self, cockpit_id: str) -> Position:
        """A cockpit went away. Its request is RETAINED, not dropped.

        Releasing it immediately would let a disconnection loosen the
        organism — an operator on flaky Wi-Fi who set ``watch`` would have
        their caution undone by their own network. §26.6's rule, applied to
        the dial: absence is not consent.
        """
        cid = str(cockpit_id or "?")
        with self._lock:
            req = self._requests.get(cid)
            if req is not None and req.detached_at is None:
                req.detached_at = self._clock()
        return self.effective()

    def reattach(self, cockpit_id: str) -> Position:
        """A cockpit came back. Its request resumes at full weight."""
        cid = str(cockpit_id or "?")
        with self._lock:
            req = self._requests.get(cid)
            if req is not None:
                req.detached_at = None
        return self.effective()

    def release(self, cockpit_id: str) -> Position:
        """Explicit withdrawal — the operator cycled away or quit cleanly.

        Distinct from :meth:`detach`: a deliberate exit is consent, an
        unplanned drop is not.
        """
        with self._lock:
            self._requests.pop(str(cockpit_id or "?"), None)
        return self.effective()

    # -- derivation ------------------------------------------------------

    def effective(self) -> Position:
        """The strictest live request. **Derived on read, never cached.**

        A cached effective value has a window between a cockpit vanishing and
        the recomputation; a derived one has no window because there is
        nothing that can be stale. That is the entire fix for the disconnect
        race — not a faster invalidation, an absent one.
        """
        now = self._clock()
        grace = request_grace_s()
        with self._lock:
            expired = [cid for cid, r in self._requests.items()
                       if r.detached_at is not None
                       and (now - r.detached_at) > grace]
            for cid in expired:
                self._requests.pop(cid, None)
            names = [r.name for r in self._requests.values()]
        if expired:
            logger.info(
                "[ProactiveMode] released %d request(s) past the %.0fs "
                "grace window", len(expired), grace)
        return strictest(*names) if names else position(_env_position())

    def requests(self) -> Tuple[Dict[str, Any], ...]:
        now = self._clock()
        with self._lock:
            return tuple({
                "cockpit_id": r.cockpit_id, "name": r.name,
                "attached": r.detached_at is None,
                "detached_for_s": (None if r.detached_at is None
                                   else round(now - r.detached_at, 1)),
            } for r in self._requests.values())

    def overridden(self, cockpit_id: str) -> bool:
        """Is this cockpit's request looser than the effective rung?"""
        cid = str(cockpit_id or "?")
        with self._lock:
            mine = self._requests.get(cid)
        if mine is None:
            return False
        return position(mine.name).rank < self.effective().rank

    # -- the dial --------------------------------------------------------

    def cycle(self, cockpit_id: str, steps: int = 1) -> Tuple[Position, bool]:
        """Advance this cockpit's request. Returns ``(position, at_boundary)``.

        **Clamped, never wrapped** (operator decision, §30.11 Q2). Wrapping
        means one accidental keypress moves from maximum caution to maximum
        autonomy, and a dial whose worst misfire is "nothing happened" is
        strictly better than one whose worst misfire is "everything is
        permitted".

        **One press, one rung — no accumulator.** An earlier draft coalesced
        a burst into an accumulated step count and was WRONG: it added the
        pending steps to a position that had already moved, so two presses
        advanced three rungs. Skipping a rung on a dial that governs autonomy
        is not a cosmetic bug; it is the operator landing somewhere they did
        not choose.

        Bursts are bounded without arithmetic. Each press moves exactly one
        rung, the clamp is idempotent at the ends, and the expensive part —
        :meth:`apply_effects` — is already guarded on an actual change of the
        effective rung. So mashing against the boundary settles
        deterministically on the terminal rung and produces no further env
        writes, no pool calls and no telemetry. ``coalesce_window_s`` bounds
        how often the *effects* are re-evaluated, never how far the dial
        moves: a debounce that discarded a transition would lose operator
        intent, which is the one thing a dial may never do.
        """
        cid = str(cockpit_id or "?")
        rungs = reachable()
        if not rungs:
            return position(_DEFAULT_NAME), True

        with self._lock:
            req = self._requests.get(cid)
            current = position(req.name if req else _env_position())
            self._last_cycle_at = self._clock()

        ranks = [p.rank for p in rungs]
        try:
            idx = ranks.index(current.rank)
        except ValueError:
            # The current rung is unreachable on this host (promotion was
            # disarmed underneath us). Clamp to the nearest reachable rung
            # that is no LOOSER, never to the nearest overall — a host losing
            # a capability must not thereby become more autonomous.
            idx = next((i for i, r in enumerate(ranks) if r >= current.rank),
                       len(rungs) - 1)
        target = idx + int(steps)
        clamped = max(0, min(len(rungs) - 1, target))
        at_boundary = clamped != target
        if at_boundary:
            with self._lock:
                self._boundary_hits += 1
        chosen = rungs[clamped]
        self.request(cid, chosen.name)
        return self.effective(), at_boundary

    # -- effects ---------------------------------------------------------

    def apply_effects(self, *, pool: Any = None) -> Dict[str, Any]:
        """Make the effective rung true. Idempotent; safe to call often.

        Composes only. The risk floor is ``risk_tier_floor``'s env knob,
        which every gate already re-reads per operation. Emission control is
        ``BackgroundAgentPool.pause``/``resume`` — the same primitive the
        hibernation path uses. Nothing here re-implements a gate.

        Called outside the lock, deliberately: a governance primitive that
        blocked while this held the dial would deadlock the loop the dial
        exists to steer.
        """
        eff = self.effective()
        changed = eff.name != self._last_applied
        out: Dict[str, Any] = {"position": eff.name, "changed": changed}

        try:
            if eff.risk_floor is None:
                os.environ.pop("JARVIS_MIN_RISK_TIER", None)
            else:
                os.environ["JARVIS_MIN_RISK_TIER"] = eff.risk_floor
            out["risk_floor"] = eff.risk_floor
        except Exception as exc:  # noqa: BLE001
            out["risk_floor_error"] = str(exc)

        out.update(self._apply_emission(eff, pool))
        self._last_applied = eff.name
        if changed:
            self._transitions += 1
            logger.info("[ProactiveMode] %s %s — %s",
                        eff.glyph, eff.name, eff.summary)
        return out

    def _apply_emission(self, eff: Position, pool: Any) -> Dict[str, Any]:
        """Pause or resume admission. NEVER raises.

        Only ``watch`` withholds initiative, so only ``watch`` pauses. The
        reason string is explicit about WHO paused: hibernation pauses the
        same pool on provider exhaustion, and rendering the two alike would
        report an outage the operator did not cause (§30.7 case 8).
        """
        out: Dict[str, Any] = {}
        target_pool = pool if pool is not None else _emission_sink()
        if target_pool is None:
            # No sink registered — headless, a unit test, or a boot that has
            # not reached the pool yet. The risk floor above still applied,
            # so the authority axis holds; only the initiative axis is
            # unenforceable, and saying so beats pretending otherwise.
            return {"emission": "no sink registered"}
        want_paused = eff.initiative is Initiative.NONE
        try:
            if want_paused:
                target_pool.pause(reason="proactive_mode:watch (operator)")
                out["emission"] = "paused"
            else:
                target_pool.resume(reason=f"proactive_mode:{eff.name}")
                out["emission"] = "running"
        except Exception as exc:  # noqa: BLE001
            out["emission"] = f"degraded: {type(exc).__name__}"
        return out

    def snapshot(self) -> Dict[str, Any]:
        eff = self.effective()
        return {
            "schema_version": PROACTIVE_MODE_SCHEMA_VERSION,
            "enabled": is_enabled(),
            "effective": eff.to_dict(),
            "reachable": [p.name for p in reachable()],
            "requests": list(self.requests()),
            "transitions": self._transitions,
            "boundary_hits": self._boundary_hits,
            "grace_s": request_grace_s(),
        }


@dataclass(frozen=True)
class Composition:
    """How the effective rung was arrived at, for the operator to read.

    The AMBIENT surfaces render the effective rung, because that is the
    truth about the organism. Whether a given operator is being overridden
    is a fact about THEIR screen, not about the organism, so it is answered
    per-cockpit by :func:`override_notice` rather than baked into the shared
    line — the same split §25.1 draws when it renders ambient content to the
    minimum width while each cockpit keeps its own.
    """

    effective: str
    glyph: str
    #: Distinct rungs requested across live cockpits. >1 means composed.
    distinct: int
    #: Live requests, attached or within grace.
    voters: int
    composed: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"effective": self.effective, "glyph": self.glyph,
                "distinct": self.distinct, "voters": self.voters,
                "composed": self.composed}


def composition() -> Composition:
    """Summarise how the current rung was decided. NEVER raises."""
    try:
        ctl = get_controller()
        eff = ctl.effective()
        names = {r["name"] for r in ctl.requests()}
        return Composition(
            effective=eff.name, glyph=eff.glyph,
            distinct=len(names), voters=len(ctl.requests()),
            composed=len(names) > 1,
        )
    except Exception:  # noqa: BLE001
        return Composition("safe_auto", "", 0, 0, False)


def override_notice(cockpit_id: str) -> str:
    """What THIS cockpit should be told about its own request, or "".

    Empty when the cockpit is not being overridden — the common case, and a
    surface that shouted on every frame would be ignored by the third frame.

    Non-empty when this operator asked for something LOOSER than the
    organism is running. That is the case §30.6 exists for: today the last
    writer wins silently, so an operator can tighten the organism and have a
    colleague loosen it with neither seeing. Being told "your request is not
    what is in force, and here is what is" is the whole difference between a
    dial an operator trusts and one they stop touching.

    The inverse — this cockpit is the STRICTEST and is therefore what
    everyone else is getting — is deliberately silent. It is not news to the
    operator who asked for it, and saying so on every frame would spend the
    line's attention budget on a non-event.
    """
    try:
        ctl = get_controller()
        cid = str(cockpit_id or "?")
        mine = next((r for r in ctl.requests()
                     if r["cockpit_id"] == cid), None)
        if mine is None:
            return ""
        eff = ctl.effective()
        asked = position(mine["name"])
        if asked.rank >= eff.rank:
            return ""
        return (f"{eff.glyph} {eff.name} in force "
                f"(you asked {asked.glyph} {asked.name})")
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class MutationVerdict:
    """Whether the current rung permits a candidate to reach VALIDATE."""

    permitted: bool
    position: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"permitted": self.permitted, "position": self.position,
                "reason": self.reason}


def mutation_permitted() -> MutationVerdict:
    """May a generated candidate proceed toward APPLY? NEVER raises.

    **Why this is a separate question from the risk floor.** The floor decides
    *how* a mutation lands — auto, after a diff, or only with a human. It
    always assumes one is landing. The ``explore`` rung says something the
    floor cannot: that generation may continue and mutation may not, so the
    organism can read, search, plan and reason without any patch reaching
    disk.

    **Why a veto and not a retry.** The Iron Gate's existing refusal —
    ``ExplorationInsufficientError`` — routes through ``GENERATE_RETRY`` with
    targeted feedback, and that is correct there: the model *can* fix
    insufficient exploration by exploring more. It cannot fix the operator's
    dial. Retrying a mode veto would burn the retry budget on a condition no
    generation can satisfy, then fail the op for a reason that was never the
    model's fault. So a veto is a **benign terminal**, not a retry and not a
    failure — the same shape ``candidate_value_gate`` uses when it proves a
    candidate cosmetic.

    Fails OPEN. A mode subsystem that cannot answer must not be able to halt
    every mutation in the organism; that would convert a fault here into a
    total outage, which is the posture ``local_model_admission`` already
    argues for on the same grounds.
    """
    try:
        if not is_enabled():
            return MutationVerdict(True, "disabled", "proactive mode off")
        eff = get_controller().effective()
        if eff.initiative in (Initiative.ACT,):
            return MutationVerdict(True, eff.name, "rung permits mutation")
        return MutationVerdict(
            False, eff.name,
            f"{eff.glyph} {eff.name}: {eff.summary}")
    except Exception as exc:  # noqa: BLE001 — fail OPEN, never halt the organism
        logger.debug("[ProactiveMode] mutation check degraded: %s", exc)
        return MutationVerdict(True, "unknown", "mode unavailable")


#: The thing that can withhold initiative. INJECTED at boot, never imported.
#:
#: The pool is an instance ``GovernedLoopService`` owns (``_bg_pool``), and a
#: mode controller that reached into the orchestrator to find it would invert
#: the authority boundary every other governance module observes — this
#: module measures and composes; it does not reach. Injection is the pattern
#: already used for ``markup_mirror``, ``set_operator_dispatcher`` and
#: ``set_prompt_publisher``, for exactly this reason.
_EMISSION_SINK: Optional[Any] = None
_sink_lock = threading.Lock()


def set_emission_sink(pool: Optional[Any]) -> None:
    """Register the pool the ``watch`` rung pauses. NEVER raises.

    Absent (unit tests, headless, pre-boot), the initiative axis is
    unenforceable and :meth:`ProactiveModeController.apply_effects` says so
    rather than reporting a state it did not achieve.
    """
    global _EMISSION_SINK
    with _sink_lock:
        _EMISSION_SINK = pool


def _emission_sink() -> Optional[Any]:
    with _sink_lock:
        return _EMISSION_SINK


def _env_position() -> str:
    """The rung implied by the environment when no cockpit has asked.

    Reads the SAME knob ``trust_repl`` writes, so a headless run and an
    attached one cannot disagree about where the dial rests.
    """
    raw = (os.environ.get("JARVIS_MIN_RISK_TIER", "") or "").strip().lower()
    return raw if raw in _BY_NAME else _DEFAULT_NAME


_singleton: Optional[ProactiveModeController] = None
_singleton_lock = threading.Lock()


def get_controller() -> ProactiveModeController:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ProactiveModeController()
        return _singleton


def reset_controller() -> None:
    """Drop the singleton — for tests and a deliberate re-derivation."""
    global _singleton
    with _singleton_lock:
        _singleton = None
