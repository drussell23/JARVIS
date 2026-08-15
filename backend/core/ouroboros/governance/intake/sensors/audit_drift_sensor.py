"""A floor that moved becomes work — bounded, located, and never the floor.

WHAT THIS CLOSES
----------------
``audit_ratchet`` gave every instrument an accepted floor and a boot
watchdog. The watchdog's whole output is one WARNING line: it tells an
operator who is reading the log, at the moment they are booting, that
something regressed. Nobody was reading it, which is how the instruments
being adapted here got orphaned in the first place.

This is the other half — a regression against an accepted floor becomes an
``IntentEnvelope``, and the organism schedules its own repair. The measurement
already existed; what was missing was the path from *measured* to *acted on*.

WHY IT COULD NOT BE BUILT BEFORE THE FLOORS WERE PINNED
--------------------------------------------------------
On this checkout the two shipped instruments stand at 59 asymmetric modules,
1 orphan, and 983 source-only tests. Adapting them before those were accepted
would have emitted **1,042 ops on the first tick** — the exact flood the
ratchet exists to prevent, arriving through the door built to make it
actionable. The floors were pinned first, and the adapter reads only the
DELTA, so steady state emits nothing at all.

THE DEDUP IS STRUCTURAL, NOT STATISTICAL
-----------------------------------------
The tempting design correlates findings by hashing stack traces and AST
context to guess which ones share a cause. That is a subsystem with its own
failure modes — a collision bundles unrelated regressions, a wide window
swallows genuine separate work, a narrow one lets the flood through anyway —
and it guesses at something the data already states.

A finding key already NAMES a location. One commit that breaks twenty
modules breaks them in a package; one that blinds twenty tests blinds them in
a directory. So findings cluster by their resolved DIRECTORY, which is a fact
about the repository rather than an inference about causation. Twenty
downstream findings become one op that names the locus and carries the
members as evidence — no hashing, no correlation window, nothing to tune.

A WHOLESALE MOVE IS ONE EVENT, NOT N
-------------------------------------
Deleting a rendering surface makes every module it reached asymmetric at
once. Emitting per-locus ops for that would be technically correct and
practically useless: the answer is one human decision (repair, or re-accept
the floor), not forty repairs. So a drift large in PROPORTION to what was
scanned collapses to a single storm envelope carrying the shape and a
sample. The threshold is proportional because the two instruments differ by
three orders of magnitude in population — 12 findings is a catastrophe
against 163 modules and a normal afternoon against 59,834 tests.

THE FLOOR IS NOT AN EDITABLE TARGET
------------------------------------
The cheapest way to make a reachability regression go away is to re-accept
the baseline, and an autonomous loop offered that shortcut would take it —
erasing the instrument to satisfy the instrument. Two things prevent it, and
neither is this module's prose: ``.jarvis/`` is in
``tool_executor._PROTECTED_PATH_SUBSTRINGS``, so no generated patch can write
a baseline; and the instruments themselves are named in the Anti-Venom
sentinel list beside ``semantic_guardian`` and ``risk_engine``, for the same
reason those are there. The envelope says so too, but the guarantee is
structural.

WHAT THIS SENSOR REFUSES TO DO
-------------------------------
* It never runs an audit of its own. Readings come from
  ``audit_ratchet.sweep`` — the same object the boot watchdog logged. Two
  consumers scanning independently could DISAGREE about the repository, and
  the operator would have no way to tell which was stale.
* It never emits on an absent or unreadable baseline. "Not yet measured" is
  not a regression, and treating it as one is how a first boot on a fresh
  checkout would enqueue a thousand ops.
* It never escalates urgency. Every envelope is ``low`` on the BACKGROUND
  route, like the cage and liveness sensors: an instrument regression is a
  chore, and putting repo-wide static analysis on the Claude tier is how a
  diagnostic becomes a budget incident.
* It never emits a finding it could not LOCATE. An op with no target file is
  an op with nothing to act on; the count is reported in ``health()`` instead
  of being dressed up as work.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.sensors.emission_control import (
    TokenBucket, env_flag, env_num,
)

logger = logging.getLogger("Ouroboros.AuditDriftSensor")

AUDIT_DRIFT_SENSOR_SCHEMA_VERSION: str = "audit_drift_sensor.1"

#: Registered in THREE places — `_VALID_SOURCES`, `SignalSource`,
#: `_BACKGROUND_SOURCES`. Missing any one fails silently and differently.
SOURCE = "audit_regression"

__all__ = [
    "AUDIT_DRIFT_SENSOR_SCHEMA_VERSION",
    "SOURCE",
    "AuditDriftSensor",
    "DriftCluster",
    "cluster_drift",
    "locate_finding",
    "reset_for_tests",
    "sensor_enabled",
]


# ---------------------------------------------------------------------------
# Knobs — every one env-driven, read live, clamped
# ---------------------------------------------------------------------------


def sensor_enabled() -> bool:
    """``JARVIS_AUDIT_DRIFT_SENSOR_ENABLED`` (default false).

    Off by default like every sensor that generates autonomous work: the
    graduation decision belongs to an operator who has watched it run, not to
    whoever merged it.
    """
    return env_flag("JARVIS_AUDIT_DRIFT_SENSOR_ENABLED", "0")


def _poll_interval_s() -> float:
    """``JARVIS_AUDIT_DRIFT_POLL_S`` — default 30 minutes.

    The readings come from a shared cache, but a poll that outruns the
    sweep's TTL forces real scans, and the two shipped instruments cost ~41s
    of parsing between them. A floor moves on the timescale of commits.
    """
    return env_num("JARVIS_AUDIT_DRIFT_POLL_S", 1800.0, 60.0, 604800.0)


def _settle_s() -> float:
    """Quiet period after a source change before re-measuring.

    ``JARVIS_AUDIT_DRIFT_SETTLE_S`` — default 5 minutes. FS events make this
    sensor event-primary (Gap #4), but a per-keystroke audit would be a
    41-second scan per save. The settle timer coalesces an editing session
    into ONE measurement, taken once the editor stops.
    """
    return env_num("JARVIS_AUDIT_DRIFT_SETTLE_S", 300.0, 5.0, 86400.0)


def _max_emit_per_scan() -> int:
    """``JARVIS_AUDIT_DRIFT_MAX_EMIT`` — default 2.

    Un-emitted clusters are not lost: the baseline has not moved, so they are
    still new on the next tick. The backlog lives in the floor, which is the
    only place that survives a restart.
    """
    return int(env_num("JARVIS_AUDIT_DRIFT_MAX_EMIT", 2, 1, 25))


def _max_targets() -> int:
    """Target files carried on one envelope. ``JARVIS_AUDIT_DRIFT_MAX_TARGETS``.

    Mirrors ``JARVIS_ATTRIBUTION_MAX_SOURCE_FILES``: past a handful, a target
    list stops scoping an op and starts describing the repository.
    """
    return int(env_num("JARVIS_AUDIT_DRIFT_MAX_TARGETS", 8, 1, 64))


def _max_members_in_evidence() -> int:
    return int(env_num("JARVIS_AUDIT_DRIFT_MAX_MEMBERS", 24, 1, 500))


def _storm_fraction() -> float:
    """Share of the scanned population that reads as wholesale.

    ``JARVIS_AUDIT_DRIFT_STORM_FRACTION`` — default 5%.
    """
    return env_num("JARVIS_AUDIT_DRIFT_STORM_FRACTION", 0.05, 0.0, 1.0)


def _storm_min() -> int:
    """Absolute floor under the proportional test.

    ``JARVIS_AUDIT_DRIFT_STORM_MIN``. Without it, an instrument with a tiny
    population would call three findings a storm.
    """
    return int(env_num("JARVIS_AUDIT_DRIFT_STORM_MIN", 12, 2, 10000))


def _regrowth_factor() -> float:
    """How much worse a known cluster must get to speak again.

    ``JARVIS_AUDIT_DRIFT_REGROWTH_FACTOR`` — default 2.0, the cage sensor's
    rule: a persistent condition reports once, a materially worse one is new
    information.
    """
    return env_num("JARVIS_AUDIT_DRIFT_REGROWTH_FACTOR", 2.0, 1.0, 100.0)


def _bucket_capacity() -> float:
    return env_num("JARVIS_AUDIT_DRIFT_BUCKET_CAPACITY", 2.0, 1.0, 50.0)


def _bucket_refill_per_s() -> float:
    """Default one op per hour sustained. ``JARVIS_AUDIT_DRIFT_BUCKET_REFILL_PER_S``."""
    return env_num("JARVIS_AUDIT_DRIFT_BUCKET_REFILL_PER_S",
                   1.0 / 3600.0, 1.0 / 604800.0, 10.0)


def _project_root(explicit: Optional[str] = None) -> Path:
    try:
        return Path(explicit or os.environ.get("JARVIS_PROJECT_ROOT") or ".")
    except Exception:  # noqa: BLE001
        return Path(".")


# ---------------------------------------------------------------------------
# Locating a finding — repository knowledge, not instrument knowledge
# ---------------------------------------------------------------------------


#: ``{dotted module: repo-relative path}``, built at most once per TTL. Only
#: consulted when the direct path join misses, which on this layout is never
#: — so the expensive walk stays unpaid unless a repository actually needs it.
_module_map: Dict[str, str] = {}
_module_map_at: float = 0.0


def _module_map_ttl_s() -> float:
    """``JARVIS_AUDIT_DRIFT_MODULE_MAP_TTL_S`` — mirrors the attribution
    bridge's 300s, for the same reason: a module map that never expires
    answers tomorrow's question with yesterday's tree."""
    return env_num("JARVIS_AUDIT_DRIFT_MODULE_MAP_TTL_S", 300.0, 0.0, 86400.0)


def _lookup_module_map(module: str, root: Path) -> str:
    """The canonical resolver's answer, TTL-cached. NEVER raises.

    Reuses ``reverse_dep_resolver.build_module_to_path`` — the same walk the
    attribution bridge trusts — rather than growing a second opinion about
    what a dotted name means.
    """
    global _module_map, _module_map_at  # noqa: PLW0603
    try:
        now = time.monotonic()
        if not _module_map or (now - _module_map_at) > _module_map_ttl_s():
            from backend.core.ouroboros.governance.reverse_dep_resolver import (
                build_module_to_path,
            )
            _module_map = build_module_to_path(str(root))
            _module_map_at = now
        return _module_map.get(module, "")
    except Exception:  # noqa: BLE001
        return ""


def locate_finding(key: str, root: Optional[Path] = None) -> str:
    """A finding key → a repo-relative path, or ``""``. NEVER raises.

    The ratchet compares opaque strings and is deliberately ignorant of what
    they mean; an envelope needs a file. This is the one place that gap is
    closed, and it closes it with REPOSITORY knowledge rather than
    per-instrument knowledge, so a third instrument needs no code here:

    * ``tests/x/test_y.py::test_z`` — a node id. The head is the file.
    * ``a.b.c`` — a dotted module. Tried as ``a/b/c.py`` then
      ``a/b/c/__init__.py`` (two stats, no walk), and only if both miss
      through the canonical module map.

    Verified against the filesystem in every branch. A key that resolves to
    nothing returns ``""`` and is counted, never guessed at — the discipline
    the attribution bridge calls ``AttributionUnresolved``.
    """
    try:
        raw = (key or "").strip()
        if not raw:
            return ""
        base = _project_root(str(root) if root is not None else None)
        head = raw.split("::", 1)[0].strip()
        if not head:
            return ""

        # A path the key states outright.
        if head.endswith(".py") or "/" in head:
            candidate = head.lstrip("/")
            if (base / candidate).is_file():
                return candidate
            return ""

        # A dotted module. The join is deterministic and costs two stats;
        # the map is the fallback for layouts where the package root is not
        # the repository root.
        parts = [p for p in head.split(".") if p]
        if not parts:
            return ""
        stem = "/".join(parts)
        for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
            if (base / candidate).is_file():
                return candidate
        mapped = _lookup_module_map(head, base)
        if mapped and (base / mapped).is_file():
            return mapped
        return ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Clustering — one commit's damage is one op
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftCluster:
    """Findings that regressed together, in one place.

    ``storm`` marks the wholesale case, where the members are a SAMPLE rather
    than the whole set: reporting forty paths as an op's targets would claim a
    scope no single change can honestly have.
    """

    instrument: str
    bucket: str
    locus: str
    members: Tuple[str, ...] = ()
    paths: Tuple[str, ...] = ()
    total: int = 0
    scanned: int = 0
    unlocated: int = 0
    storm: bool = False

    @property
    def size(self) -> int:
        """The finding count this cluster speaks for — the sampled total in a
        storm, never the length of the sample."""
        return max(self.total, len(self.members))

    @property
    def identity(self) -> str:
        """Stable across scans, independent of COUNT.

        The count is deliberately excluded: a cluster that grows must be
        recognisable as the same cluster, or the regrowth rule could never
        fire and every scan would re-emit.
        """
        return f"{self.instrument}|{self.bucket}|{self.locus}"

    @property
    def rank(self) -> Tuple[int, int, str]:
        """Storms first, then by weight. An operator's attention is the
        scarce resource, and a wholesale move is the one that cannot wait."""
        return (0 if self.storm else 1, -self.size, self.identity)


def _is_storm(count: int, scanned: int) -> bool:
    """Wholesale, in proportion to what was measured. NEVER raises.

    ``max`` of the two tests rather than ``min``: the absolute floor stops a
    small population from calling three findings a catastrophe, and the
    proportional term stops a large one from calling a normal commit's worth
    of drift the same.
    """
    try:
        floor = max(float(_storm_min()), _storm_fraction() * max(0, int(scanned)))
        return count >= floor
    except Exception:  # noqa: BLE001
        return False


def cluster_drift(instrument: str, drift: Any,
                  root: Optional[Path] = None) -> List[DriftCluster]:
    """One instrument's drift → the ops it justifies. Pure. NEVER raises.

    Silent — returning ``[]`` — for the three states that are not
    regressions: an audit that errored (no reading), an absent baseline (not
    yet measured), and no new findings (the steady state this whole design
    exists to make quiet).
    """
    try:
        if drift is None or getattr(drift, "error", ""):
            return []
        if not getattr(drift, "baseline_exists", False):
            return []
        new = dict(getattr(drift, "new", {}) or {})
        scanned = int(getattr(drift, "scanned", 0) or 0)
    except Exception:  # noqa: BLE001
        return []

    out: List[DriftCluster] = []
    claimed: set = set()
    # Ascending by size so the most SPECIFIC bucket claims a key first. The
    # evidence instrument's `flag_literal` is a strict subset of
    # `source_only`; without this the same test would be reported twice, once
    # under a label that says why it matters and once under one that doesn't.
    for bucket in sorted(new, key=lambda b: (len(new.get(b) or ()), b)):
        keys = [str(k) for k in (new.get(bucket) or ()) if str(k).strip()]
        keys = [k for k in keys if k not in claimed]
        if not keys:
            continue
        claimed.update(keys)

        located: List[Tuple[str, str]] = []
        unlocated = 0
        for key in sorted(set(keys)):
            path = locate_finding(key, root)
            if path:
                located.append((key, path))
            else:
                unlocated += 1
        if not located:
            # Every finding in this bucket is unlocatable. The boot watchdog
            # has already said the floor moved; inventing an op with no file
            # to open would be the louder of the two wrong answers.
            if unlocated:
                logger.info(
                    "[AuditDrift] %s/%s: %d new finding(s), none locatable — "
                    "no op emitted", instrument, bucket, unlocated)
            continue

        if _is_storm(len(keys), scanned):
            members = tuple(k for k, _ in located)
            out.append(DriftCluster(
                instrument=instrument, bucket=bucket, locus="*",
                members=members[:_max_members_in_evidence()],
                paths=tuple(p for _, p in located)[:_max_targets()],
                total=len(keys), scanned=scanned, unlocated=unlocated,
                storm=True))
            continue

        by_locus: Dict[str, List[Tuple[str, str]]] = {}
        for key, path in located:
            by_locus.setdefault(os.path.dirname(path) or ".", []).append(
                (key, path))
        for locus, rows in sorted(by_locus.items()):
            out.append(DriftCluster(
                instrument=instrument, bucket=bucket, locus=locus,
                members=tuple(k for k, _ in rows)[:_max_members_in_evidence()],
                paths=tuple(sorted({p for _, p in rows}))[:_max_targets()],
                total=len(rows), scanned=scanned,
                # Attributed to the first cluster only, so a health projection
                # summing them does not multiply one miss by the locus count.
                unlocated=(unlocated if locus == sorted(by_locus)[0] else 0),
                storm=False))
    return out


# ---------------------------------------------------------------------------
# What the op is asked to do
# ---------------------------------------------------------------------------


_THE_FLOOR_IS_NOT_THE_FIX = (
    "Do NOT edit the accepted baseline under .jarvis/ and do NOT weaken the "
    "audit — moving the floor to match the code erases the only instrument "
    "that can see this class of defect. Both are refused structurally; if the "
    "new state is intended, an operator accepts it with the verb."
)


def _remedy(instrument: str) -> str:
    """What the op should try, in the instrument's own vocabulary.

    Derived from the verb name the ratchet already carries rather than from a
    table of instruments: a third instrument gets a correct instruction with
    no edit here, and a renamed one cannot leave a stale entry behind.
    """
    return (f"Repair the regression at its cause, then confirm with "
            f"`/{instrument}` that the drift is gone. If the new state is "
            f"correct, `/{instrument} accept` is the operator's call, never "
            f"this op's. {_THE_FLOOR_IS_NOT_THE_FIX}")


def _describe(cluster: DriftCluster) -> str:
    """The envelope's prose. Deterministic, never model-written."""
    where = "across the repository" if cluster.storm else f"under {cluster.locus}"
    sample = ", ".join(cluster.members[:5])
    more = (f" (+{cluster.size - min(5, len(cluster.members))} more)"
            if cluster.size > 5 else "")
    head = (
        f"The `{cluster.instrument}` audit regressed against its accepted "
        f"floor: {cluster.size} new `{cluster.bucket}` finding(s) {where}, "
        f"out of {cluster.scanned} scanned. Findings: {sample}{more}."
    )
    if cluster.storm:
        head += (
            " This is a WHOLESALE move — a proportion of the scanned "
            "population large enough that a single change is unlikely to "
            "explain it. Diagnose the common cause (a removed surface, a "
            "renamed entry point, a moved package) before repairing "
            "individual findings."
        )
    return f"{head} {_remedy(cluster.instrument)}"


def _confidence(cluster: DriftCluster) -> float:
    """How sure the finding is that there is something to do.

    A located, clustered regression against a floor a human accepted is
    strong evidence — the measurement is deterministic and the baseline is
    consent. A storm is deliberately LOWER: the finding is certain, but that
    it describes one actionable op is not.
    """
    return 0.45 if cluster.storm else 0.7


# ---------------------------------------------------------------------------
# The sensor
# ---------------------------------------------------------------------------


@dataclass
class _Stats:
    scans: int = 0
    emitted: int = 0
    throttled: int = 0
    suppressed: int = 0
    unlocated: int = 0
    storms: int = 0
    fs_events_handled: int = 0
    fs_events_ignored: int = 0
    settle_runs: int = 0
    last_sweep_fresh: bool = False
    last_sweep_age_s: float = 0.0
    last_instruments: Tuple[str, ...] = ()
    last_error: str = ""


#: Cross-scan memory of what has already been said, keyed by cluster identity
#: → the size it had when last emitted. Module-level so a sensor restarted
#: mid-session does not re-announce the backlog it just announced.
_seen: Dict[str, int] = {}


def reset_for_tests() -> None:
    """Forget what has been emitted. Test-only."""
    _seen.clear()


class AuditDriftSensor:
    """Turns an accepted floor's drift into governed work.

    Lifecycle is ``start`` / ``stop`` / ``scan_once`` / ``health`` — the
    contract ``IntakeLayerService`` already drives for every sensor, because a
    second lifecycle shape is a second thing to get wrong at shutdown.

    Event-primary with a polling floor (Gap #4): a source change arms a
    settle timer, and the poll remains as the backstop for when the FS bridge
    is unavailable. Both funnel into the same ``scan_once``, so there is one
    emission path regardless of what woke it.
    """

    def __init__(self, repo: str, router: Any, *,
                 project_root: Optional[str] = None,
                 poll_interval_s: Optional[float] = None) -> None:
        self._repo = repo
        self._router = router
        self._root = _project_root(project_root)
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._settle_task: Optional[asyncio.Task] = None
        self._bucket = TokenBucket(_bucket_capacity, _bucket_refill_per_s)
        self._stats = _Stats()

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._running or not sensor_enabled():
            return
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"audit_drift_{self._repo}")
        logger.info(
            "[AuditDrift] started (poll %.0fs, settle %.0fs) — emits only what "
            "regressed since the accepted floor",
            self._interval(), _settle_s())

    def stop(self) -> None:
        self._running = False
        for task in (self._task, self._settle_task):
            if task is not None and not task.done():
                task.cancel()
        self._task = None
        self._settle_task = None

    def _interval(self) -> float:
        return (self._poll_interval_s if self._poll_interval_s is not None
                else _poll_interval_s())

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval())
                if self._running:
                    await self.scan_once()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.debug("[AuditDrift] poll degraded", exc_info=True)

    # -- event-primary ----------------------------------------------------

    async def subscribe_to_bus(self, event_bus: Any) -> None:
        """Wake on source changes instead of only on a clock. NEVER raises.

        Called unconditionally by ``IntakeLayerService`` for every sensor
        exposing it; the enablement check lives here so one sensor's decision
        never becomes a special case at the call site.
        """
        if not sensor_enabled():
            return
        try:
            await event_bus.subscribe("fs.changed.*", self._on_fs_event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AuditDrift] FS-event subscription failed: %s — poll "
                "fallback at %.0fs continues", exc, self._interval())
            return
        logger.info(
            "[AuditDrift] subscribed to fs.changed.* — a source change arms a "
            "%.0fs settle timer; poll remains the backstop", _settle_s())

    async def _on_fs_event(self, event: Any) -> None:
        """Arm the settle timer. NEVER raises, never scans inline.

        Scanning here would run a 41-second audit per keystroke. The timer is
        re-armed by each event, so an editing session costs ONE measurement,
        taken once the editing stops — which is also the only moment at which
        the answer would have been stable anyway.
        """
        try:
            payload = getattr(event, "payload", None) or {}
            topic = str(getattr(event, "topic", ""))
            if payload.get("extension") != ".py" or topic.endswith(".deleted"):
                # A deleted file cannot be repaired by an op that opens it;
                # the poll still catches the reachability consequences.
                self._stats.fs_events_ignored += 1
                return
            self._stats.fs_events_handled += 1
            self._arm_settle()
        except Exception:  # noqa: BLE001
            logger.debug("[AuditDrift] fs event degraded", exc_info=True)

    def _arm_settle(self) -> None:
        """(Re)start the quiet timer. NEVER raises."""
        try:
            if self._settle_task is not None and not self._settle_task.done():
                self._settle_task.cancel()
            self._settle_task = asyncio.create_task(
                self._settle_then_scan(), name=f"audit_drift_settle_{self._repo}")
        except RuntimeError:
            # No running loop — nothing to schedule onto. The poll covers it.
            pass
        except Exception:  # noqa: BLE001
            logger.debug("[AuditDrift] settle not armed", exc_info=True)

    async def _settle_then_scan(self) -> None:
        try:
            await asyncio.sleep(_settle_s())
            if not self._running:
                return
            self._stats.settle_runs += 1
            # The tree just changed, so a cached reading describes a
            # repository that no longer exists. This is the one caller that
            # must force.
            await self.scan_once(force=True)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.debug("[AuditDrift] settle scan degraded", exc_info=True)

    # -- detection + emission ---------------------------------------------

    async def collect_clusters(self, *, force: bool = False) -> List[DriftCluster]:
        """The ops this reading justifies, strongest first. NEVER raises."""
        try:
            from backend.core.ouroboros.governance import audit_ratchet
            reading = await audit_ratchet.sweep(force=force)
        except Exception as exc:  # noqa: BLE001
            self._stats.last_error = f"{type(exc).__name__}: {exc}"
            logger.debug("[AuditDrift] sweep unavailable", exc_info=True)
            return []

        drifts = dict(getattr(reading, "drifts", {}) or {})
        self._stats.last_sweep_fresh = bool(getattr(reading, "fresh", False))
        self._stats.last_sweep_age_s = round(float(reading.age_s()), 3)
        self._stats.last_instruments = tuple(sorted(drifts))
        self._stats.last_error = "; ".join(
            f"{name}: {d.error}" for name, d in sorted(drifts.items())
            if getattr(d, "error", ""))

        clusters: List[DriftCluster] = []
        for name, drift in sorted(drifts.items()):
            clusters.extend(cluster_drift(name, drift, self._root))
        clusters.sort(key=lambda c: c.rank)
        return clusters

    async def scan_once(self, *, force: bool = False) -> List[DriftCluster]:
        """Read, cluster, bound, emit. Returns what was FOUND. NEVER raises.

        The return value is the findings, not the emissions: a caller that
        wants to know what the repository looks like must not have that
        answer shaped by how much this sensor was allowed to say.
        """
        if not sensor_enabled():
            return []
        try:
            self._stats.scans += 1
            clusters = await self.collect_clusters(force=force)
            if not clusters:
                return []
            self._stats.unlocated = sum(c.unlocated for c in clusters)
            self._stats.storms = sum(1 for c in clusters if c.storm)

            emitted = 0
            for cluster in clusters:
                if emitted >= _max_emit_per_scan():
                    break
                previous = _seen.get(cluster.identity)
                if (previous is not None
                        and cluster.size < previous * _regrowth_factor()):
                    # Already said, and not materially worse. Silence here is
                    # the feature: the finding is still in the floor's delta
                    # and will speak again if it grows.
                    self._stats.suppressed += 1
                    continue
                if not self._bucket.take():
                    self._stats.throttled += 1
                    logger.debug("[AuditDrift] throttled %s", cluster.identity)
                    break
                if await self._emit(cluster):
                    _seen[cluster.identity] = cluster.size
                    emitted += 1

            self._stats.emitted += emitted
            if emitted or self._stats.unlocated:
                logger.info(
                    "[AuditDrift] %d cluster(s) from %s, %d emitted "
                    "(cap %d, bucket %.2f), %d unlocatable",
                    len(clusters), ",".join(self._stats.last_instruments),
                    emitted, _max_emit_per_scan(), self._bucket.tokens,
                    self._stats.unlocated)
            return clusters
        except Exception:  # noqa: BLE001
            logger.debug("[AuditDrift] scan degraded", exc_info=True)
            return []

    async def _emit(self, cluster: DriftCluster) -> bool:
        """One envelope per cluster. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )
            targets = tuple(cluster.paths[:_max_targets()])
            if not targets:
                # Structurally impossible from `cluster_drift`, which never
                # builds a cluster without a located path. Kept because the
                # envelope contract requires it and an empty tuple would
                # raise inside a fail-soft emitter, turning a missing op into
                # a silent one.
                return False
            envelope = make_envelope(
                source=SOURCE,
                description=_describe(cluster),
                target_files=targets,
                repo=self._repo,
                confidence=_confidence(cluster),
                # LOW, always. An instrument regression is a chore; escalating
                # would put repo-wide static analysis on the Claude tier and
                # make a diagnostic into a budget incident.
                urgency="low",
                evidence={
                    "schema_version": AUDIT_DRIFT_SENSOR_SCHEMA_VERSION,
                    "sensor": "AuditDriftSensor",
                    "instrument": cluster.instrument,
                    "bucket": cluster.bucket,
                    "locus": cluster.locus,
                    # THE dedup axis: `intent_envelope._dedup_key` hashes this
                    # field, so the router's own window recognises a repeat
                    # without this sensor keeping a second, disagreeing
                    # opinion about what "the same finding" means.
                    "signature": cluster.identity,
                    "findings": list(cluster.members),
                    "finding_count": cluster.size,
                    "scanned": cluster.scanned,
                    "unlocated": cluster.unlocated,
                    "storm": cluster.storm,
                    "baseline_is_not_the_fix": True,
                },
                requires_human_ack=False,
            )
            return (await self._router.ingest(envelope)) == "enqueued"
        except Exception:  # noqa: BLE001
            logger.debug("[AuditDrift] emit failed for %s", cluster.identity,
                         exc_info=True)
            return False

    # -- observability ----------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Bounded projection. NEVER raises.

        ``unlocated`` is reported rather than buried: findings this sensor
        cannot turn into work are the one class it silently drops, and a
        silent drop nobody can see is how the instruments being adapted here
        went unnoticed for months.
        """
        return {
            "schema_version": AUDIT_DRIFT_SENSOR_SCHEMA_VERSION,
            "enabled": sensor_enabled(),
            "running": self._running,
            "scans": self._stats.scans,
            "emitted": self._stats.emitted,
            "suppressed": self._stats.suppressed,
            "throttled": self._stats.throttled,
            "unlocated": self._stats.unlocated,
            "storms": self._stats.storms,
            "known_clusters": len(_seen),
            "bucket_tokens": round(self._bucket.tokens, 3),
            "instruments": list(self._stats.last_instruments),
            "sweep_fresh": self._stats.last_sweep_fresh,
            "sweep_age_s": self._stats.last_sweep_age_s,
            "fs_events_handled": self._stats.fs_events_handled,
            "fs_events_ignored": self._stats.fs_events_ignored,
            "settle_runs": self._stats.settle_runs,
            "poll_interval_s": self._interval(),
            "error": self._stats.last_error,
        }
