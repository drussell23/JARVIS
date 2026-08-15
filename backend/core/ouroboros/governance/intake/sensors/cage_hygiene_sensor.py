"""Denied cage requests become one aggregated signal — never a flood.

`cage_calibration` learns from what caged workers do, and refuses on principle
to widen a cage from observed demand: a system that grants ``bash`` because a
worker kept asking has built a privilege-escalation ramp out of persistence.
Denials therefore become FINDINGS about the synthesizer's inspection rule.

Until now those findings had no emission surface. That is the open loop this
closes — and closing it naively would open two new holes at once.

Why a raw emitter would be an attack surface
---------------------------------------------
A worker in a retry loop can produce a hundred identical denials in a second.
One signal per denial is a denial-of-service against O+V's own intake: a
hundred ops enqueued ahead of real work, a hundred rows in the operator's
deck, and the one finding that mattered buried in the ninety-nine that
didn't. Worse, it is attacker-REACHABLE — anything that can make a worker ask
for a forbidden tool can make it ask a thousand times.

So the emission path is aggregate-first, and the aggregation is structural
rather than advisory:

* **Cluster** by ``(role, tool)`` over a rolling window. A hundred identical
  denials are ONE cluster carrying ``occurrences=100``. The count is not
  lost; it becomes the WEIGHT, which is strictly more informative than a
  hundred separate signals and costs one.
* **Token bucket** on emission. The aggregator bounds signals per burst; the
  bucket bounds them per unit time. Both are needed — a slow drip of
  *distinct* clusters defeats aggregation alone, and a burst of one cluster
  defeats a bucket alone.
* **Dedup** on cluster identity, so a persistent condition reports once and
  then stays quiet until it changes.

Severity does NOT escalate the route
-------------------------------------
Every envelope here is ``urgency="low"`` on the BACKGROUND route, including
the ones classified suspicious. Escalating urgency for high-risk findings
would look responsible and would hand an attacker a cost-amplification lever:
trigger denials, force IMMEDIATE routing, burn the Claude tier. Severity
rides in the evidence and on the operator surface, where it is visible
immediately and free.

The classifier is DETERMINISTIC, and that is a security decision
-----------------------------------------------------------------
Classifying a denial by asking a model to inspect the invocation context
would mean reading attacker-influenced content to decide whether that content
is an attack — an injection surface guarding against injection. It would also
be unbounded work on an attacker-triggerable path.

So classification is structural, from evidence already held:

* ``subagent_contracts.TOOL_CLASS_MAP`` covers exactly the read-only
  exploration vocabulary. A denied tool ABSENT from it (``bash``,
  ``write_file``, ``web_fetch``) is boundary-crossing by the taxonomy that
  already exists — no new list to maintain or get wrong.
* **Breadth** — distinct tools denied to one role. Asking for one plausible
  neighbour looks like a model guessing; sweeping many looks like probing.

``Benign_Hallucination`` is the read-only, low-breadth case: the synthesizer
under-granted and the RULE should be fixed. ``Suspicious_Injection`` is
boundary-crossing or broad. ``Undetermined`` exists because a classifier
without one lies whenever it is unsure.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.sensors.emission_control import (
    TokenBucket,
)

logger = logging.getLogger("Ouroboros.CageHygieneSensor")

CAGE_HYGIENE_SCHEMA_VERSION: str = "cage_hygiene.1"

#: Registered in THREE places — `_VALID_SOURCES`, `SignalSource`,
#: `_BACKGROUND_SOURCES`. Missing any one fails silently and differently.
SOURCE = "cage_hygiene"

__all__ = [
    "CAGE_HYGIENE_SCHEMA_VERSION",
    "SOURCE",
    "CageHygieneSensor",
    "DenialCluster",
    "classify_cluster",
    "note_denials",
    "reset_for_tests",
    "sensor_enabled",
]


def _flag(name: str, default: str = "1") -> bool:
    try:
        return os.environ.get(name, default).strip().lower() not in (
            "0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return True


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "").strip() or default)))
    except Exception:  # noqa: BLE001
        return default


def sensor_enabled() -> bool:
    """``JARVIS_CAGE_HYGIENE_SENSOR_ENABLED`` (default false)."""
    return _flag("JARVIS_CAGE_HYGIENE_SENSOR_ENABLED", "0")


def _window_s() -> float:
    """Rolling aggregation window. ``JARVIS_CAGE_HYGIENE_WINDOW_S``."""
    return _num("JARVIS_CAGE_HYGIENE_WINDOW_S", 300.0, 1.0, 86400.0)


def _bucket_capacity() -> float:
    """Burst allowance. ``JARVIS_CAGE_HYGIENE_BUCKET_CAPACITY``."""
    return _num("JARVIS_CAGE_HYGIENE_BUCKET_CAPACITY", 3.0, 1.0, 100.0)


def _bucket_refill_per_s() -> float:
    """Sustained emission rate. ``JARVIS_CAGE_HYGIENE_BUCKET_REFILL_PER_S``.

    Default 1/300s — one signal per five minutes sustained. These are
    engineering chores about a synthesis rule; nothing about them is urgent,
    and a rate an operator can actually read is more useful than a complete
    one they stop reading.
    """
    return _num("JARVIS_CAGE_HYGIENE_BUCKET_REFILL_PER_S", 1.0 / 300.0,
                1.0 / 86400.0, 10.0)


def _min_occurrences() -> int:
    """Denials in a cluster before it is worth reporting.

    ``JARVIS_CAGE_HYGIENE_MIN_OCCURRENCES``. One denial is a model guessing
    once; a pattern is a rule being wrong.
    """
    return int(_num("JARVIS_CAGE_HYGIENE_MIN_OCCURRENCES", 3, 1, 1000))


def _suspicious_breadth() -> int:
    """Distinct denied tools for one role that reads as probing.

    ``JARVIS_CAGE_HYGIENE_SUSPICIOUS_BREADTH``.
    """
    return int(_num("JARVIS_CAGE_HYGIENE_SUSPICIOUS_BREADTH", 3, 2, 100))


def _max_clusters() -> int:
    """Hard ceiling on retained clusters. ``JARVIS_CAGE_HYGIENE_MAX_CLUSTERS``.

    The aggregator itself must be bounded: an attacker who cannot flood the
    INTAKE could otherwise flood the aggregator's memory by varying the tool
    name on every call.
    """
    return int(_num("JARVIS_CAGE_HYGIENE_MAX_CLUSTERS", 256, 8, 100000))


def _max_emit_per_scan() -> int:
    return int(_num("JARVIS_CAGE_HYGIENE_MAX_EMIT", 2, 1, 50))


def _debounce_s() -> float:
    return _num("JARVIS_CAGE_HYGIENE_DEBOUNCE_S", 30.0, 0.0, 600.0)


# ---------------------------------------------------------------------------
# Classification — deterministic, from taxonomy that already exists
# ---------------------------------------------------------------------------

BENIGN = "Benign_Hallucination"
SUSPICIOUS = "Suspicious_Injection"
UNDETERMINED = "Undetermined"


def _is_boundary_tool(name: str) -> Optional[bool]:
    """True when *name* is outside the read-only exploration vocabulary.

    Returns None when the taxonomy cannot be consulted — which the caller
    renders as ``Undetermined`` rather than guessing in either direction.
    """
    try:
        from backend.core.ouroboros.governance.subagent_contracts import (
            TOOL_CLASS_MAP,
        )
        return str(name) not in TOOL_CLASS_MAP
    except Exception:  # noqa: BLE001
        return None


@dataclass
class DenialCluster:
    """Denials of one tool to one role, collapsed over the rolling window."""

    role: str
    tool: str
    occurrences: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    #: Every distinct tool denied to this ROLE in the window — the breadth
    #: signal. Held per-cluster so classification needs no second pass.
    role_tool_breadth: int = 1

    @property
    def key(self) -> str:
        return f"{self.role}|{self.tool}"

    @property
    def rate_per_s(self) -> float:
        span = max(1e-6, self.last_seen - self.first_seen)
        return self.occurrences / span


def classify_cluster(cluster: DenialCluster) -> str:
    """``Benign_Hallucination`` / ``Suspicious_Injection`` / ``Undetermined``.

    Pure and total. NEVER raises, never calls a model, never reads
    attacker-supplied text — only the tool NAME (already constrained to the
    dispatch vocabulary) and counts.
    """
    try:
        boundary = _is_boundary_tool(cluster.tool)
        if boundary is None:
            return UNDETERMINED
        if boundary:
            # Outside the read-only vocabulary: shell, filesystem write,
            # network. One such request is worth a human glance.
            return SUSPICIOUS
        if cluster.role_tool_breadth >= _suspicious_breadth():
            # Read-only tools only, but sweeping several of them — that is
            # the shape of probing, not of a model guessing one neighbour.
            return SUSPICIOUS
        return BENIGN
    except Exception:  # noqa: BLE001
        return UNDETERMINED


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


def _new_bucket() -> TokenBucket:
    """This sensor's bucket, over the shared arithmetic.

    Complements aggregation rather than duplicating it: aggregation collapses
    a burst of the SAME cluster, the bucket bounds a drip of DISTINCT ones.
    Either alone leaves the other attack open.

    The limits stay here as this sensor's own env vocabulary; only the
    arithmetic is shared, because a third copy of it had begun to drift from
    the other two.
    """
    return TokenBucket(_bucket_capacity, _bucket_refill_per_s)


# ---------------------------------------------------------------------------
# Aggregator — module-level so the calibration seam can feed it directly
# ---------------------------------------------------------------------------


@dataclass
class _Aggregator:
    clusters: Dict[str, DenialCluster] = field(default_factory=dict)
    #: role -> distinct tools denied to it in the window (the breadth signal)
    role_tools: Dict[str, set] = field(default_factory=dict)
    dropped: int = 0

    def note(self, role: str, tools: Tuple[str, ...], now: float) -> None:
        """Fold denials into their clusters. NEVER raises."""
        try:
            for tool in tools:
                key = f"{role}|{tool}"
                cluster = self.clusters.get(key)
                if cluster is None:
                    if len(self.clusters) >= _max_clusters():
                        # Bounded: an attacker varying the tool name on every
                        # call must not be able to grow this without limit.
                        self.dropped += 1
                        continue
                    cluster = DenialCluster(role=role, tool=tool,
                                            first_seen=now)
                    self.clusters[key] = cluster
                cluster.occurrences += 1
                cluster.last_seen = now
                self.role_tools.setdefault(role, set()).add(tool)
            for cluster in self.clusters.values():
                if cluster.role == role:
                    cluster.role_tool_breadth = len(
                        self.role_tools.get(role, ()) or ()) or 1
        except Exception:  # noqa: BLE001
            logger.debug("[CageHygiene] aggregation degraded", exc_info=True)

    def expire(self, now: float) -> None:
        """Drop clusters whose last denial fell out of the window."""
        try:
            window = _window_s()
            stale = [k for k, c in self.clusters.items()
                     if (now - c.last_seen) > window]
            for key in stale:
                self.clusters.pop(key, None)
            if stale:
                live_roles = {c.role for c in self.clusters.values()}
                for role in list(self.role_tools):
                    if role not in live_roles:
                        self.role_tools.pop(role, None)
        except Exception:  # noqa: BLE001
            pass

    def ripe(self, now: float) -> List[DenialCluster]:
        """Clusters worth reporting, strongest first. NEVER raises."""
        try:
            self.expire(now)
            floor = _min_occurrences()
            ripe = [c for c in self.clusters.values() if c.occurrences >= floor]
            # Suspicious first, then by weight — the operator's attention is
            # the scarce resource this whole module is protecting.
            ripe.sort(key=lambda c: (
                0 if classify_cluster(c) == SUSPICIOUS else 1,
                -c.occurrences, c.key))
            return ripe
        except Exception:  # noqa: BLE001
            return []


_aggregator = _Aggregator()
_bucket = _new_bucket()


def note_denials(signature: str, denied_tools: Tuple[str, ...],
                 count_denied: int = 0) -> None:
    """Fold a worker's denials into the aggregator. NEVER raises.

    Called from `cage_calibration.observe_unit` — the ONE seam that already
    sees every caged worker finish, so no new instrumentation exists to
    fall out of date.
    """
    if not sensor_enabled():
        return
    try:
        role = str(signature or "unknown").split(":", 1)[0]
        tools = tuple(str(t) for t in (denied_tools or ()) if t)
        if count_denied:
            # Budget exhaustion is a denial of the MUTATION capability, not of
            # a named tool. Given its own pseudo-tool so it clusters and
            # classifies alongside the rest instead of needing a parallel path.
            tools = tools + ("<mutation_budget>",)
        if tools:
            _aggregator.note(role, tools, time.time())
    except Exception:  # noqa: BLE001
        logger.debug("[CageHygiene] note_denials degraded", exc_info=True)


def reset_for_tests() -> None:
    """Clear the aggregator and refill the bucket. Test-only."""
    global _aggregator, _bucket  # noqa: PLW0603
    _aggregator = _Aggregator()
    _bucket = _new_bucket()


# ---------------------------------------------------------------------------
# The sensor
# ---------------------------------------------------------------------------


class CageHygieneSensor:
    """Emits aggregated cage findings into `UnifiedIntakeRouter`.

    Mirrors `MemoryHygieneSensor`'s lifecycle — ``start`` / ``stop`` /
    ``scan_once`` / ``health`` — because that is the contract `IntakeLayer`
    already drives, and a second sensor shape is a second lifecycle to get
    wrong.
    """

    def __init__(self, repo: str, router: Any, *,
                 poll_interval_s: Optional[float] = None) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = (poll_interval_s if poll_interval_s is not None
                                 else _window_s())
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen: Dict[str, int] = {}
        self._emitted = 0
        self._scans = 0
        self._throttled = 0

    async def start(self) -> None:
        if self._running or not sensor_enabled():
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[CageHygiene] started (window %.0fs)", self._poll_interval_s)

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval_s)
                if self._running:
                    await self.scan_once()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.debug("[CageHygiene] poll degraded", exc_info=True)

    async def scan_once(self) -> List[DenialCluster]:
        """Aggregate, bound, emit. Returns the ripe clusters. NEVER raises."""
        if not sensor_enabled():
            return []
        try:
            self._scans += 1
            ripe = _aggregator.ripe(time.time())
            if not ripe:
                return []

            emitted = 0
            for cluster in ripe[:_max_emit_per_scan()]:
                # Dedup on cluster identity AND weight: a condition that has
                # not changed reports once. A materially worse one reports
                # again, because "it got ten times worse" is new information.
                previous = self._seen.get(cluster.key)
                if previous is not None and cluster.occurrences < previous * 2:
                    continue
                if not _bucket.take():
                    self._throttled += 1
                    logger.debug("[CageHygiene] throttled %s", cluster.key)
                    break
                if await self._emit(cluster):
                    self._seen[cluster.key] = cluster.occurrences
                    emitted += 1

            self._emitted += emitted
            if emitted:
                logger.info(
                    "[CageHygiene] %d cluster(s) ripe, %d emitted "
                    "(bucket %.2f, dropped %d)",
                    len(ripe), emitted, _bucket.tokens, _aggregator.dropped)
            return ripe
        except Exception:  # noqa: BLE001
            logger.debug("[CageHygiene] scan degraded", exc_info=True)
            return []

    async def _emit(self, cluster: DenialCluster) -> bool:
        """One aggregated envelope. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )
            verdict = classify_cluster(cluster)
            envelope = make_envelope(
                source=SOURCE,
                description=(
                    f"Caged workers of role {cluster.role!r} were denied "
                    f"{cluster.tool!r} {cluster.occurrences} time(s). "
                    f"Classification: {verdict}. The cage is NOT widened from "
                    f"observed demand by design — if this demand is "
                    f"legitimate, fix the inspection rule in "
                    f"worker_synthesizer so the tool is derived for this "
                    f"class of work."
                ),
                target_files=(
                    "backend/core/ouroboros/governance/autonomy/worker_synthesizer.py",
                ),
                repo=self._repo,
                confidence=0.6,
                # LOW for every finding, including suspicious ones. Escalating
                # urgency by severity would hand an attacker a
                # cost-amplification lever: trigger denials, force IMMEDIATE
                # routing, burn the Claude tier. Severity rides in evidence.
                urgency="low",
                evidence={
                    "schema_version": CAGE_HYGIENE_SCHEMA_VERSION,
                    "sensor": "CageHygieneSensor",
                    "role": cluster.role,
                    "tool": cluster.tool,
                    "occurrences": cluster.occurrences,
                    "distinct_tools_for_role": cluster.role_tool_breadth,
                    "rate_per_s": round(cluster.rate_per_s, 4),
                    "classification": verdict,
                    "window_s": _window_s(),
                },
                requires_human_ack=False,
            )
            return (await self._router.ingest(envelope)) == "enqueued"
        except Exception:  # noqa: BLE001
            logger.debug("[CageHygiene] emit failed for %s", cluster.key,
                         exc_info=True)
            return False

    def health(self) -> Dict[str, Any]:
        """Bounded projection. NEVER raises."""
        return {
            "schema_version": CAGE_HYGIENE_SCHEMA_VERSION,
            "enabled": sensor_enabled(),
            "running": self._running,
            "scans": self._scans,
            "emitted": self._emitted,
            "throttled": self._throttled,
            "clusters": len(_aggregator.clusters),
            "clusters_dropped": _aggregator.dropped,
            "bucket_tokens": round(_bucket.tokens, 3),
        }
