"""audit_ratchet — an instrument's findings, and what changed since you looked.

FOUR INSTRUMENTS, ONE RATCHET
-----------------------------
This repo measures its own darkness from several angles: which surfaces reach
a module (``surface_reachability``), how many tests can only ever pass
(``source_assertion_audit``), and others. Every one of them needs the same
four things, and none of them is about what the instrument measures:

    an ACCEPTED baseline    what the operator has already seen and allowed
    DRIFT                   what is new since then, and what got fixed
    ACCEPT                  pin the current state as the new floor
    a boot WATCHDOG         say so, once, when the number gets worse

``reach_repl`` grew all four inline. Copying them for the second instrument
would be ~130 lines of duplicated persistence, degradation policy and
comparison logic — and the copy would drift, because the two would be fixed
separately. So the ratchet is the abstraction and the instrument is the
parameter.

WHY THE FLOOR RATCHETS RATHER THAN ALARMS
-----------------------------------------
A repo-wide instrument that reports its whole standing list on every boot
gets muted within a week, and a muted instrument is worse than none: it looks
like coverage. The ratchet reports only the DELTA against a state a human
accepted, so steady state is silent by construction and a regression is the
only thing that speaks.

Absence of a baseline reports NOTHING rather than everything. A first run on
a fresh checkout has not regressed — it has not yet been measured — and
flooding an operator with the standing list is how the last four detectors
taught people to ignore them.

A CORRUPT BASELINE IS ABSENT, NOT EMPTY
---------------------------------------
Reading an unparseable baseline as ``{}`` would report every standing finding
as newly regressed — a hundred false positives arriving exactly when someone
is already recovering from a disk fault. Absent is the only safe reading.

DECLARING A RATCHET MOUNTS ITS WATCHDOG
---------------------------------------
A ``*_repl`` module that exposes a module-level ``RATCHET`` has its watchdog
run at boot by :func:`run_registered_watchdogs`, discovered through the same
``repl_dispatch_registry`` that routes the verb itself. That is deliberate:
``reach_repl``
shipped a correct ``run_watchdog`` with **zero production callers** — the
ratchet built to catch unmounted features was itself unmounted. Making the
declaration the registration is the only fix that does not depend on somebody
remembering.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (Any, Awaitable, Callable, Dict, Iterable, List, Mapping,
                    Optional, Tuple)

logger = logging.getLogger("Ouroboros.AuditRatchet")

AUDIT_RATCHET_SCHEMA_VERSION: str = "audit_ratchet.1"


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_num(name: str, default: float, lo: float, hi: float) -> float:
    """A clamped numeric knob. NEVER raises."""
    try:
        return min(hi, max(lo, float((os.environ.get(name) or "").strip()
                                     or default)))
    except Exception:  # noqa: BLE001
        return min(hi, max(lo, default))


@dataclass(frozen=True)
class Instrument:
    """What a ratchet needs to know about a measurement, and nothing else.

    The ratchet never learns what a finding MEANS — it compares sets of
    stable string keys. That is what lets one implementation serve a
    reachability graph and a test-quality scan without either leaking into
    it, and what will let the third instrument arrive as a declaration.
    """

    #: Stable, lowercase. Names the baseline file and the env overrides, so
    #: renaming an instrument is a deliberate act with a visible consequence
    #: rather than a silent orphaning of everyone's accepted state.
    name: str
    #: The audit itself, already off the event loop. Async because every
    #: instrument here parses hundreds of files, and Principle 3 has no
    #: exception for diagnostics.
    run: Callable[[], Awaitable[Any]]
    #: reading → {bucket: [stable key, …]}. Buckets are compared
    #: independently so "a new orphan" and "a newly asymmetric module" stay
    #: distinguishable in the report.
    findings: Callable[[Any], Mapping[str, Iterable[str]]]
    #: reading → how much was looked at. Context for the operator; never
    #: compared, because a scan that grew is not a regression.
    scanned: Callable[[Any], int] = lambda _r: 0
    #: Where this instrument's baseline has always lived. Carried on the
    #: instrument rather than derived, so adopting the ratchet does not
    #: orphan a baseline an operator already accepted.
    baseline_filename: str = ""
    #: Reads a baseline written BEFORE this instrument adopted the ratchet.
    #: An instrument that predates the ratchet has an on-disk format the
    #: ratchet cannot know, and reading it as unrecognised would silently
    #: discard a floor the operator accepted — they would be told "no
    #: baseline recorded" and every standing finding would return as new.
    #: Owned by the instrument because it is the instrument's own history;
    #: the ratchet stays ignorant of what any bucket means.
    legacy_buckets: Optional[
        Callable[[Dict[str, Any]], Mapping[str, Iterable[str]]]] = None

    @property
    def env_prefix(self) -> str:
        return f"JARVIS_{self.name.upper()}"


@dataclass(frozen=True)
class Drift:
    """What changed since the baseline. Bucket-wise, never flattened."""

    new: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    resolved: Tuple[str, ...] = ()
    baseline_exists: bool = True
    scanned: int = 0
    error: str = ""

    @property
    def regressed(self) -> bool:
        return any(bool(v) for v in self.new.values())

    def bucket(self, name: str) -> Tuple[str, ...]:
        return tuple(self.new.get(name, ()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": AUDIT_RATCHET_SCHEMA_VERSION,
            "new": {k: list(v) for k, v in self.new.items()},
            "resolved": list(self.resolved),
            "baseline_exists": self.baseline_exists,
            "scanned": self.scanned, "regressed": self.regressed,
            "error": self.error,
        }


class AuditRatchet:
    """One instrument's accepted floor. NEVER raises out of any method."""

    def __init__(self, instrument: Instrument) -> None:
        self.instrument = instrument

    # -- location ---------------------------------------------------------

    def baseline_path(self) -> Path:
        """Per-REPOSITORY, never global.

        An accepted floor is a judgement about one checkout's code, and a
        shared file would carry one repo's allowances into another — the
        same reasoning that keeps the proactive-mode dial per-worktree.
        """
        override = os.environ.get(f"{self.instrument.env_prefix}_BASELINE_PATH")
        if override:
            return Path(override)
        root = Path(os.environ.get("JARVIS_PROJECT_ROOT", "."))
        name = (self.instrument.baseline_filename
                or f"{self.instrument.name}_baseline.json")
        return root / ".jarvis" / name

    def watchdog_enabled(self) -> bool:
        """Default TRUE — silent at steady state, so it costs nothing."""
        return _env_flag(f"{self.instrument.env_prefix}_WATCHDOG_ENABLED")

    # -- state ------------------------------------------------------------

    def _load(self) -> Optional[Dict[str, Any]]:
        """The accepted state, or None. Corrupt reads as ABSENT."""
        try:
            data = json.loads(self.baseline_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    async def accept(self) -> Dict[str, Any]:
        """Pin the current state as the floor. NEVER raises.

        Written through ``durable_io.atomic_replace``: a baseline half-written
        by a crash reads as corrupt on the next boot, which degrades to "no
        baseline" and floods the operator exactly when they are already
        recovering from something.
        """
        try:
            reading = await self.instrument.run()
            buckets = {k: sorted(set(v))
                       for k, v in self.instrument.findings(reading).items()}
            payload: Dict[str, Any] = {
                "schema_version": AUDIT_RATCHET_SCHEMA_VERSION,
                "instrument": self.instrument.name,
                "buckets": buckets,
                "scanned": self.instrument.scanned(reading),
            }
            await asyncio.to_thread(_write_atomic, self.baseline_path(),
                                    payload)
            logger.info("[%s] baseline accepted — %s across %d scanned",
                        self.instrument.name,
                        ", ".join(f"{k} {len(v)}" for k, v in buckets.items())
                        or "nothing",
                        payload["scanned"])
            return payload
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] baseline write failed: %s",
                         self.instrument.name, exc, exc_info=True)
            return {"error": f"{type(exc).__name__}: {exc}"}

    async def drift(self) -> Drift:
        """What regressed since the baseline. NEVER raises."""
        try:
            reading = await self.instrument.run()
        except Exception as exc:  # noqa: BLE001
            return Drift(error=f"{type(exc).__name__}: {exc}")
        try:
            now = {k: set(v)
                   for k, v in self.instrument.findings(reading).items()}
            scanned = self.instrument.scanned(reading)
        except Exception as exc:  # noqa: BLE001
            return Drift(error=f"{type(exc).__name__}: {exc}")

        base = self._load()
        if base is None:
            return Drift(baseline_exists=False, scanned=scanned)
        was = base.get("buckets")
        if not isinstance(was, dict) and self.instrument.legacy_buckets:
            try:
                converted = self.instrument.legacy_buckets(base)
                was = {k: list(v) for k, v in converted.items()}
            except Exception:  # noqa: BLE001
                was = None
        if not isinstance(was, dict):
            # A baseline in a shape neither this ratchet nor the instrument
            # understands is not a baseline. Treated as absent for the same
            # reason a corrupt one is — inventing an empty floor
            # manufactures a regression per standing finding.
            return Drift(baseline_exists=False, scanned=scanned)

        new: Dict[str, Tuple[str, ...]] = {}
        was_map: Dict[str, Any] = was
        resolved: List[str] = []
        for bucket, keys in now.items():
            before = set(was_map.get(bucket) or ())
            new[bucket] = tuple(sorted(keys - before))
            resolved.extend(sorted(before - keys))
        # A bucket the baseline knows and this reading does not is entirely
        # resolved — dropping it here would silently forget a fix.
        for bucket, keys in was_map.items():
            if bucket not in now:
                resolved.extend(sorted(set(keys or ())))
        return Drift(new=new, resolved=tuple(sorted(set(resolved))),
                     baseline_exists=True, scanned=scanned)

    async def watchdog(self) -> Drift:
        """Boot-time drift check. NEVER raises. Silent at steady state."""
        if not self.watchdog_enabled():
            return Drift(baseline_exists=False)
        result = await self.drift()
        name = self.instrument.name
        if result.error:
            logger.debug("[%s] watchdog could not audit: %s", name,
                         result.error)
        elif not result.baseline_exists:
            logger.info(
                "[%s] no baseline — run `/%s accept` to pin the current "
                "state, then this reports only what changes", name, name)
        elif result.regressed:
            # WARNING, not INFO: these instruments exist because capabilities
            # keep shipping unreachable, and that has now happened six times.
            logger.warning(
                "[%s] REGRESSED since the accepted baseline — %s", name,
                "; ".join(f"new {k}: {', '.join(v)}"
                          for k, v in result.new.items() if v))
        return result


def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
    from backend.core.ouroboros.governance.durable_io import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                   encoding="utf-8")
    atomic_replace(tmp, path)


# ---------------------------------------------------------------------------
# Boot mount — declaring a ratchet registers its watchdog
# ---------------------------------------------------------------------------


def registered_ratchets() -> List[AuditRatchet]:
    """Every ratchet a mounted verb declares. NEVER raises.

    Asked of ``repl_dispatch_registry`` — THE verb table the daemon actually
    routes to — rather than of a list here or of a second discovery pass. A
    verb opts in by exposing a module-level ``RATCHET``: one declaration,
    beside the ``__verb_help__`` the same registry already reads.

    The registry has primed itself by the time this runs, so the modules are
    already imported; this reads an attribute off each one and imports
    nothing new.
    """
    import sys

    out: List[AuditRatchet] = []
    try:
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            _VERB_TO_DISPATCHER,
            prime_registry,
        )
        prime_registry()
    except Exception:  # noqa: BLE001
        logger.debug("[AuditRatchet] verb registry unavailable", exc_info=True)
        return out
    seen = set()
    for _verb, fn in sorted(_VERB_TO_DISPATCHER.items()):
        name = getattr(fn, "__module__", "") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        ratchet = getattr(sys.modules.get(name), "RATCHET", None)
        if isinstance(ratchet, AuditRatchet):
            out.append(ratchet)
    return out


def watchdogs_enabled() -> bool:
    """Master switch for the whole boot sweep. Default TRUE."""
    return _env_flag("JARVIS_AUDIT_WATCHDOGS_ENABLED")


async def run_registered_watchdogs() -> Dict[str, Drift]:
    """Run every declared watchdog concurrently. NEVER raises.

    Concurrent because these are independent scans and serialising them
    would make the slowest instrument set the boot cost for all of them.
    Each already runs its own audit off the loop, so this composes rather
    than nests threads.

    ``return_exceptions``: one instrument that explodes must not cancel the
    others. An audit is a diagnostic, and a diagnostic that can take down
    the boot it reports on is a liability rather than an asset.
    """
    if not watchdogs_enabled():
        return {}
    # `registered_ratchets` -> `prime_registry` -> `import_module` for every
    # `*_repl` module in the package. StallSampler caught THIS call on the
    # wedged main loop: a discovery walk is import work, and import work on
    # the loop is a stall no `async def` around it can prevent. Offloaded to
    # the house pool; fail-open to inline so a missing helper cannot silence
    # every watchdog.
    try:
        from backend.core.async_offload import call_off_loop
        ratchets = await call_off_loop(registered_ratchets)
    except Exception:  # noqa: BLE001
        ratchets = registered_ratchets()
    if not ratchets:
        return {}
    results = await asyncio.gather(
        *(r.watchdog() for r in ratchets), return_exceptions=True)
    out: Dict[str, Drift] = {}
    for ratchet, result in zip(ratchets, results):
        if isinstance(result, BaseException):
            logger.debug("[AuditRatchet] %s watchdog raised: %s",
                         ratchet.instrument.name, result)
            out[ratchet.instrument.name] = Drift(
                error=f"{type(result).__name__}: {result}")
        else:
            out[ratchet.instrument.name] = result
    return out


def spawn_registered_watchdogs() -> Optional["asyncio.Task"]:
    """Fire the sweep as a background task. NEVER raises, never blocks.

    The boot seam calls this and moves on. A watchdog that delayed startup
    by the length of its own scan would be traded away the first time
    somebody profiled boot — and then the instrument is gone again.

    Goes through :func:`sweep` rather than straight to
    :func:`run_registered_watchdogs`, so the reading this produces is the
    same object every other consumer sees. Boot is the earliest and
    cheapest moment to take it; anything that asks in the following TTL
    gets it for free instead of paying for the scans a second time.
    """
    if not watchdogs_enabled():
        return None
    try:
        return asyncio.get_running_loop().create_task(sweep())
    except RuntimeError:
        # No loop — a synchronous context (a script, a test collection).
        # Not an error: there is simply nothing to schedule onto.
        return None
    except Exception:  # noqa: BLE001
        logger.debug("[AuditRatchet] watchdog sweep not scheduled",
                     exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The shared reading — one scan, N consumers
# ---------------------------------------------------------------------------


AUDIT_SWEEP_SCHEMA_VERSION: str = "audit_sweep.1"


@dataclass(frozen=True)
class Sweep:
    """Every declared instrument's drift, from ONE pass.

    Exists because the reading is expensive and there is now more than one
    consumer: the boot watchdog logs it, and ``audit_drift_sensor`` turns it
    into work. Measured on this checkout the two shipped instruments cost
    ~12s and ~29s of parsing; a second consumer that re-ran them would not
    merely double the cost, it would make two consumers capable of DISAGREEING
    about the same repository — the operator told one thing by the log and
    another by the deck, with no way to tell which reading was stale.
    """

    drifts: Mapping[str, Drift] = field(default_factory=dict)
    #: ``time.monotonic()`` when this reading completed. Monotonic because
    #: freshness is an interval, and a wall clock that steps backwards over
    #: an NTP correction would make a cached reading immortal.
    completed_at: float = 0.0
    #: True when this call performed the scan; False when it was served from
    #: the cache or joined one already in flight. Reported, never inferred —
    #: a consumer that thinks it forced a scan and did not would draw
    #: conclusions from a reading of a repository that has since changed.
    fresh: bool = False

    @property
    def regressed(self) -> bool:
        return any(d.regressed for d in self.drifts.values())

    def age_s(self, now: Optional[float] = None) -> float:
        if not self.completed_at:
            return float("inf")
        return max(0.0, (time.monotonic() if now is None else now)
                   - self.completed_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": AUDIT_SWEEP_SCHEMA_VERSION,
            "drifts": {k: v.to_dict() for k, v in self.drifts.items()},
            "regressed": self.regressed,
            "age_s": round(self.age_s(), 3),
            "fresh": self.fresh,
        }


def sweep_ttl_s() -> float:
    """How long a reading stays serviceable. ``JARVIS_AUDIT_SWEEP_TTL_S``.

    Default 15 minutes: long enough that boot and the first sensor scan share
    one pass, short enough that a consumer never reasons about a repository
    an hour out of date. ``0`` disables reuse entirely.
    """
    return _env_num("JARVIS_AUDIT_SWEEP_TTL_S", 900.0, 0.0, 86400.0)


#: The cached reading and the scan currently producing one. Module-level
#: because the point is to be shared across every caller in the process.
_last_sweep: Optional[Sweep] = None
_inflight: Optional["asyncio.Future"] = None


def _inflight_for_this_loop() -> Optional["asyncio.Future"]:
    """The in-flight scan, IF it belongs to the running loop.

    A future from another loop can never complete for this caller — awaiting
    it deadlocks. Tests create a fresh loop per case and daemons re-enter on
    reconnect, so this is a live condition rather than a theoretical one.
    """
    fut = _inflight
    if fut is None or fut.done():
        return None
    try:
        return fut if fut.get_loop() is asyncio.get_running_loop() else None
    except Exception:  # noqa: BLE001
        return None


async def sweep(*, force: bool = False,
                max_age_s: Optional[float] = None) -> Sweep:
    """ONE reading of every declared instrument, shared. NEVER raises.

    Three callers, one scan:

    * **cache** — a reading younger than the TTL is returned as-is. The boot
      watchdog pays for the scans; a sensor waking up inside the window is
      free.
    * **single flight** — a caller arriving while a scan is running JOINS it
      rather than starting a second. Without this, boot and a sensor tick
      landing together would run four audits concurrently over the same
      files, and the loop would feel it.
    * **force** — an operator asking a verb for the current state means
      *current*, and must not be answered from a cache they cannot see.

    Joined callers ``shield`` the shared scan: one consumer being cancelled
    (a REPL Ctrl-C, a sensor shutting down) must not cancel a reading every
    other consumer is waiting on, and the cache still fills for whoever asks
    next.
    """
    if not watchdogs_enabled():
        return Sweep()
    try:
        ttl = sweep_ttl_s() if max_age_s is None else max(0.0, float(max_age_s))
    except Exception:  # noqa: BLE001
        ttl = sweep_ttl_s()

    if not force:
        cached = _last_sweep
        if cached is not None and cached.completed_at and cached.age_s() <= ttl:
            # Re-stamped rather than returned as-is: the first consumer took
            # a live reading and every later one did not, and a cached object
            # that still claims ``fresh`` would let the second consumer report
            # the repository's state as of a scan it never ran.
            return replace(cached, fresh=False)
        joined = _inflight_for_this_loop()
        if joined is not None:
            try:
                return await asyncio.shield(joined)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                return Sweep()

    return await _start_sweep()


async def _start_sweep() -> Sweep:
    """Own the scan for this loop, and let everyone else join it."""
    global _inflight  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    task = loop.create_task(_sweep_once())
    _inflight = task
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The scan keeps running and still fills the cache — the work is
        # already paid for, and discarding it would make a cancelled REPL
        # command cost the next consumer a fresh 40 seconds.
        raise
    except Exception:  # noqa: BLE001
        return Sweep()
    finally:
        if _inflight is task and task.done():
            _inflight = None


async def _sweep_once() -> Sweep:
    """Run every watchdog and cache the result. NEVER raises.

    Deliberately does NOT clear ``_inflight``: two overlapping scans (a
    forced one over a cadence one) would otherwise have the first to finish
    unregister the second, and a third caller would start a needless fourth
    audit. Registration is cleared by whoever owns it, and a finished future
    left behind is inert — :func:`_inflight_for_this_loop` refuses a done
    one.
    """
    global _last_sweep  # noqa: PLW0603
    try:
        drifts = await run_registered_watchdogs()
    except Exception as exc:  # noqa: BLE001 — a diagnostic never propagates
        logger.debug("[AuditRatchet] sweep failed: %s", exc, exc_info=True)
        drifts = {}
    result = Sweep(drifts=drifts, completed_at=time.monotonic(), fresh=True)
    _last_sweep = result
    return result


def last_sweep() -> Optional[Sweep]:
    """The cached reading, or None. Does NOT scan — for health projections
    and operator surfaces that must stay free to call."""
    return _last_sweep


def reset_sweep_cache() -> None:
    """Forget the cached reading and any in-flight scan. Test-only."""
    global _inflight, _last_sweep  # noqa: PLW0603
    _last_sweep = None
    _inflight = None


__all__ = [
    "AUDIT_RATCHET_SCHEMA_VERSION",
    "AUDIT_SWEEP_SCHEMA_VERSION",
    "AuditRatchet",
    "Drift",
    "Instrument",
    "Sweep",
    "last_sweep",
    "registered_ratchets",
    "reset_sweep_cache",
    "run_registered_watchdogs",
    "spawn_registered_watchdogs",
    "sweep",
    "sweep_ttl_s",
    "watchdogs_enabled",
]
