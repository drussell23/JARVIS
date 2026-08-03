"""What is TRUE of this machine right now — and what is merely unobserved.

ONE MECHANISM, THREE JOBS
---------------------------
An operator said "unlock my screen" and JARVIS asked for Touch ID. The screen
was locked, so no dialog could be presented; the verdict came back DENIED in
99 milliseconds and the log recorded "operator declined". Nobody declined
anything. `unlock_screen` could never be authorised, by construction:

    a gate whose only answer channel requires the very state the gated
    action exists to produce is a deadlock, not a safeguard.

Three apparently separate problems turn out to be the same question asked
three times — *what is true of the machine right now?*

  * **Preconditions.** "Search for dogs" on a locked Mac cannot work. A person
    would unlock first without being asked to. That is not a special case to
    hardcode; it is an unmet precondition with a known remedy.
  * **Reachability.** Touch ID needs an unlocked screen to draw on. A consent
    channel has preconditions exactly as an action does.
  * **Verification.** After unlocking, the only honest way to know it worked
    is to LOOK. `capability_router` reports EXECUTED when a call did not
    raise, which is a fact about the call, not about the world.

So there is one predicate layer, and the three read from it.

WHY TRUTH IS TERNARY
----------------------
`voice_unlock.objc.server.screen_lock_detector._check_cgsession_locked_via_
ctypes` returns `Optional[bool]` — and the `None` means "the query failed",
which is a genuinely different fact from "the screen is not locked". The
module's own `is_screen_locked()` wrapper collapses that to `False`.

That collapse is the ghost-display defect exactly: a probe that could not see
answered "absent", and an absent reading authorised a create. Here it would
authorise something worse — believing an unlocked screen because CoreGraphics
was unavailable, then typing a password into whatever has focus.

So UNKNOWN is a value, it is never silently narrowed, and:

    **UNKNOWN NEVER SATISFIES A PRECONDITION.**

It also never *refutes* one. An unmet precondition triggers a remedy; an
unknown one triggers a refusal to guess. Those are different outcomes and
this module refuses to conflate them.

WHY PROBES ARE REGISTERED, NOT IMPORTED
-----------------------------------------
The consumer of a predicate must not know which framework answers it.
`system_control` importing `voice_unlock` would invert the layering to read
one boolean, and the next predicate would drag in another subsystem. Probes
register themselves; a predicate nobody probes is UNKNOWN, which is already
the safe answer, so a missing registration degrades rather than breaks.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("JARVIS.WorldState")

WORLD_STATE_SCHEMA_VERSION: str = "world_state.v1"


class Truth(str, enum.Enum):
    """Three-valued truth. UNKNOWN is a fact, not a missing answer."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    @property
    def known(self) -> bool:
        return self is not Truth.UNKNOWN

    def negate(self) -> "Truth":
        """The negation. UNKNOWN negated is still UNKNOWN — not knowing a
        thing tells you exactly as little about its opposite."""
        if self is Truth.TRUE:
            return Truth.FALSE
        if self is Truth.FALSE:
            return Truth.TRUE
        return Truth.UNKNOWN

    @classmethod
    def of(cls, value: Any) -> "Truth":
        """From a probe's ``Optional[bool]``. NEVER raises.

        `None` maps to UNKNOWN rather than FALSE. That single line is the
        whole reason this type exists.
        """
        if value is True:
            return cls.TRUE
        if value is False:
            return cls.FALSE
        return cls.UNKNOWN


#: Predicates that are each other's negation, so ONE probe answers both.
#:
#: Registered beside the probe rather than kept as a policy table: two probes
#: for `screen_locked` and `screen_unlocked` could disagree, and the day they
#: did, the machine would believe the screen was both. A negation cannot
#: disagree with itself.
_ANTONYMS: Dict[str, str] = {}
_ANTONYM_LOCK = threading.Lock()


def canonical(predicate: str) -> Tuple[str, bool]:
    """(base_predicate, negated). NEVER raises.

    Accepts `screen_locked`, `!screen_locked`, `not screen_locked`, and any
    registered antonym such as `screen_unlocked`.
    """
    try:
        p = (predicate or "").strip().lower().replace("-", "_")
        negated = False
        while p.startswith("!") or p.startswith("not_") or p.startswith("not "):
            negated = not negated
            p = p[1:] if p.startswith("!") else p[4:]
            p = p.strip()
        with _ANTONYM_LOCK:
            base = _ANTONYMS.get(p)
        if base:
            return base, (not negated)
        return p, negated
    except Exception:  # noqa: BLE001
        return (predicate or ""), False


def register_antonym(alias: str, base: str) -> None:
    """Declare *alias* to mean "not *base*". Idempotent. NEVER raises."""
    try:
        with _ANTONYM_LOCK:
            _ANTONYMS[(alias or "").strip().lower()] = (base or "").strip().lower()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class Reading:
    """One predicate, observed. Carries HOW it was learned."""

    predicate: str
    truth: str
    #: What answered — a probe name, or "" when nothing could.
    source: str = ""
    #: Why UNKNOWN, when it is. An unexplained UNKNOWN is indistinguishable
    #: from a predicate nobody thought to probe, and an operator can act on
    #: one of those.
    detail: str = ""
    observed_at: float = 0.0
    schema_version: str = WORLD_STATE_SCHEMA_VERSION

    @property
    def is_true(self) -> bool:
        return self.truth == Truth.TRUE.value

    @property
    def is_unknown(self) -> bool:
        return self.truth == Truth.UNKNOWN.value


def probe_ttl_s() -> float:
    """How long a reading may be reused. NEVER raises.

    Deliberately short. A cached world is a remembered world, and the whole
    point of a precondition check is that the machine may have changed since
    the last time anybody looked. Long enough to spare four probes inside one
    utterance; far too short to survive a screen locking.
    """
    try:
        raw = (os.environ.get("JARVIS_WORLD_STATE_TTL_S", "") or "").strip()
        return max(0.0, min(30.0, float(raw))) if raw else 2.0
    except (TypeError, ValueError):
        return 2.0


def probe_timeout_s() -> float:
    """How long a single probe may take. NEVER raises.

    A probe that hangs must become UNKNOWN, not a hung voice command. This is
    on the path between an operator speaking and the machine acting.
    """
    try:
        raw = (os.environ.get("JARVIS_WORLD_STATE_PROBE_TIMEOUT_S", "") or "").strip()
        return max(0.1, min(10.0, float(raw))) if raw else 2.0
    except (TypeError, ValueError):
        return 2.0


class WorldState:
    """The observable state of this machine. NEVER raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probes: Dict[str, Tuple[str, Callable[[], Any]]] = {}
        self._cache: Dict[str, Reading] = {}

    # -- registration ----------------------------------------------------

    def register(self, predicate: str, probe: Callable[[], Any], *,
                 name: str = "") -> None:
        """Register what can answer *predicate*. Idempotent. NEVER raises.

        *probe* returns ``True`` / ``False`` / ``None`` (unknown), sync or
        async. Returning None is a legitimate answer and the reason the whole
        type is ternary — see the module docstring.
        """
        try:
            base, negated = canonical(predicate)
            if negated:
                logger.debug("[WorldState] refusing to register a probe for a "
                             "negated predicate '%s' — register '%s'",
                             predicate, base)
                return
            with self._lock:
                self._probes[base] = (name or getattr(probe, "__name__", "probe"),
                                      probe)
        except Exception:  # noqa: BLE001
            logger.debug("[WorldState] register degraded", exc_info=True)

    def known_predicates(self) -> List[str]:
        with self._lock:
            return sorted(self._probes)

    # -- observation -----------------------------------------------------

    async def read(self, predicate: str, *, fresh: bool = False) -> Reading:
        """Observe one predicate. NEVER raises.

        *fresh* bypasses the cache — used after an action that was SUPPOSED to
        change the world, where a cached answer would confirm the thing we are
        trying to verify.
        """
        base, negated = canonical(predicate)
        try:
            if not fresh:
                cached = self._cached(base)
                if cached is not None:
                    return self._orient(cached, predicate, negated)
            with self._lock:
                entry = self._probes.get(base)
            if entry is None:
                return Reading(predicate=predicate, truth=Truth.UNKNOWN.value,
                               detail=f"nothing probes '{base}'",
                               observed_at=time.time())
            probe_name, probe = entry
            truth, detail = await self._invoke(probe, probe_name)
            reading = Reading(predicate=base, truth=truth.value,
                              source=probe_name, detail=detail,
                              observed_at=time.time())
            with self._lock:
                self._cache[base] = reading
            return self._orient(reading, predicate, negated)
        except Exception as exc:  # noqa: BLE001
            return Reading(predicate=predicate, truth=Truth.UNKNOWN.value,
                           detail=f"{type(exc).__name__}: {exc}",
                           observed_at=time.time())

    def _cached(self, base: str) -> Optional[Reading]:
        try:
            ttl = probe_ttl_s()
            if ttl <= 0.0:
                return None
            with self._lock:
                r = self._cache.get(base)
            if r is None or (time.time() - r.observed_at) > ttl:
                return None
            # NEVER serve a cached UNKNOWN. A failed probe is a transient we
            # want retried, and caching it would turn one CoreGraphics hiccup
            # into a window during which the machine refuses to know anything.
            return None if r.is_unknown else r
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _orient(reading: Reading, asked_as: str, negated: bool) -> Reading:
        """Re-express a base reading in the polarity the caller asked for."""
        if not negated:
            return Reading(predicate=asked_as, truth=reading.truth,
                           source=reading.source, detail=reading.detail,
                           observed_at=reading.observed_at)
        flipped = Truth(reading.truth).negate()
        return Reading(predicate=asked_as, truth=flipped.value,
                       source=reading.source, detail=reading.detail,
                       observed_at=reading.observed_at)

    @staticmethod
    async def _invoke(probe: Callable[[], Any], name: str) -> Tuple[Truth, str]:
        """Run one probe under a timeout. NEVER raises.

        A sync probe runs in a worker thread: `_check_cgsession_locked_via_
        ctypes` is a blocking C call, and the caller here is the event loop
        that also carries the operator's voice.
        """
        try:
            result = probe()
            if inspect.isawaitable(result):
                value = await asyncio.wait_for(result, timeout=probe_timeout_s())
            elif callable(getattr(result, "__call__", None)):
                value = result
            else:
                value = result
            return Truth.of(value), ""
        except asyncio.TimeoutError:
            return Truth.UNKNOWN, f"probe '{name}' timed out"
        except Exception as exc:  # noqa: BLE001
            return Truth.UNKNOWN, f"probe '{name}' failed: {type(exc).__name__}"

    async def satisfies(self, predicate: str, *, fresh: bool = False) -> bool:
        """Whether *predicate* is KNOWN TRUE. NEVER raises.

        The asymmetry is the point: UNKNOWN answers False here and False in
        :meth:`refutes`, so an unobservable world satisfies nothing and
        refutes nothing. A caller that wants "proceed unless proven otherwise"
        has to say so out loud rather than get it by accident.
        """
        return (await self.read(predicate, fresh=fresh)).is_true

    async def refutes(self, predicate: str, *, fresh: bool = False) -> bool:
        """Whether *predicate* is KNOWN FALSE. NEVER raises."""
        r = await self.read(predicate, fresh=fresh)
        return r.truth == Truth.FALSE.value

    async def unmet(self, predicates: Any, *,
                    fresh: bool = False) -> List[Reading]:
        """Every requirement not KNOWN TRUE, observed concurrently.

        NEVER raises. Includes the UNKNOWN ones — a caller must be able to
        tell "the screen is locked" (remediable) from "I cannot see whether
        the screen is locked" (not something to act through).
        """
        try:
            wanted = [p for p in (predicates or []) if p]
            if not wanted:
                return []
            readings = await asyncio.gather(
                *[self.read(p, fresh=fresh) for p in wanted],
                return_exceptions=True)
            out: List[Reading] = []
            for p, r in zip(wanted, readings):
                if isinstance(r, Reading):
                    if not r.is_true:
                        out.append(r)
                else:
                    out.append(Reading(predicate=p, truth=Truth.UNKNOWN.value,
                                       detail="probe raised",
                                       observed_at=time.time()))
            return out
        except Exception:  # noqa: BLE001
            return [Reading(predicate=str(p), truth=Truth.UNKNOWN.value,
                            detail="world state degraded",
                            observed_at=time.time())
                    for p in (predicates or [])]

    def invalidate(self, predicate: str = "") -> None:
        """Forget cached readings. NEVER raises.

        Called after ANY action that touched the machine — not only the ones
        whose declared effects we know about. A capability that changes the
        world in a way nobody declared is exactly the one whose stale reading
        would be believed.
        """
        try:
            with self._lock:
                if not predicate:
                    self._cache.clear()
                else:
                    self._cache.pop(canonical(predicate)[0], None)
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema_version": WORLD_STATE_SCHEMA_VERSION,
                "predicates": sorted(self._probes),
                "cached": {k: v.truth for k, v in self._cache.items()},
                "ttl_s": probe_ttl_s(),
            }


# ── The macOS probes ────────────────────────────────────────────────────────

#: The canonical predicate. `screen_unlocked` is its negation, so one probe
#: answers both and they cannot disagree.
SCREEN_LOCKED = "screen_locked"
SCREEN_UNLOCKED = "screen_unlocked"

# Registered at IMPORT, not inside the singleton factory. `canonical()` is a
# pure function that anything may call — a registry hydrating, a test naming a
# predicate — and if the antonym only existed after the first
# `get_world_state()`, the same string would canonicalise two different ways
# depending on call order. A vocabulary whose meaning depends on when you ask
# is not a vocabulary.
register_antonym(SCREEN_UNLOCKED, SCREEN_LOCKED)


def _probe_screen_locked() -> Optional[bool]:
    """Is the screen locked? ``None`` when we could not tell. NEVER raises.

    COMPOSES the detector module's own `Optional[bool]` primitives instead of
    calling its `is_screen_locked()` wrapper. The wrapper returns `bool` and
    ends in a bare `return False`, so "no method could tell" and "the screen
    is open" come back identical. That collapse is fine for a caller deciding
    whether to show a banner and catastrophic for one deciding whether to type
    a password into whatever currently has focus.

    Same cascade order, same stated invariant — *if any reliable method says
    locked, it is locked* — minus the final narrowing. Three probes rather
    than the wrapper's seven because these three are the ones that answer
    `Optional[bool]`; the rest already express themselves as booleans and
    would smuggle the same collapse back in.

    Measured, and worth knowing: `CGSessionCopyCurrentDictionary` returns NULL
    outside a GUI session, so this reads UNKNOWN from a plain shell and
    answers properly inside the HUD backend, which the Swift app spawns as a
    child of its own GUI session. A test run from a terminal is therefore not
    evidence that the probe is broken.

    ctypes rather than the Quartz bridge because importing Quartz pulls in
    AppKit._metadata, which SIGSEGVs while the CoreAudio IO thread is running
    — and here that thread is always running; it is the microphone.
    """
    saw_unlocked = False
    try:
        from voice_unlock.objc.server import screen_lock_detector as D
    except Exception:  # noqa: BLE001
        logger.debug("[WorldState] lock detector unimportable", exc_info=True)
        return None
    for name in ("_check_cgsession_locked_via_ctypes",
                 "_check_session_locked_via_osascript",
                 "_check_lockscreen_process"):
        try:
            fn = getattr(D, name, None)
            if fn is None:
                continue
            answer = fn()
        except Exception:  # noqa: BLE001 — one blind method never blinds the rest
            logger.debug("[WorldState] %s degraded", name, exc_info=True)
            continue
        if answer is True:
            return True          # any reliable method saying LOCKED wins
        if answer is False:
            saw_unlocked = True
    # UNLOCKED only if something actually SAW it unlocked. All-silent is
    # UNKNOWN, which satisfies nothing — see the module docstring.
    return False if saw_unlocked else None


_WORLD: Optional[WorldState] = None
_WORLD_LOCK = threading.Lock()


def get_world_state() -> WorldState:
    """Process-wide world state, with the macOS probes installed. NEVER raises."""
    global _WORLD
    with _WORLD_LOCK:
        if _WORLD is None:
            w = WorldState()
            try:
                w.register(SCREEN_LOCKED, _probe_screen_locked,
                           name="cgsession_cascade")
            except Exception:  # noqa: BLE001
                logger.debug("[WorldState] probe install degraded", exc_info=True)
            _WORLD = w
        return _WORLD


def reset_world_state() -> None:
    """Testing seam. NEVER raises."""
    global _WORLD
    with _WORLD_LOCK:
        _WORLD = None
