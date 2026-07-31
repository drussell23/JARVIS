"""Memory that reports on itself — the proactive half.

Everything this arc built is PULL. ``route()`` fires when an op reaches
CONTEXT_EXPANSION; ``compose_for_op()`` fires at the same seam; utility
scores are read at the next pull. Memory only ever speaks when spoken to,
which is the right shape for a reactive assistant and the wrong PRIMARY
shape for an organism that self-initiates.

O+V is proactive first. In this codebase that has a precise meaning, not a
vague one: proactive means being a SIGNAL SOURCE — emitting `IntentSignal`
envelopes into `UnifiedIntakeRouter` so the governed loop schedules work
nobody asked for. A background thread that merely recomputes a score is not
proactive; it is a cache warmer.

So memory becomes a sensor. The reactive path is untouched and stays
secondary: an op that wants memory still pulls it at CONTEXT_EXPANSION,
exactly as before.

What memory now has to SAY
---------------------------
This is only possible because the rest of the arc produced the evidence.
Before it, memory knew nothing about itself worth acting on.

``drifted``
    A topic's declared ``modules:`` changed after the topic was written
    (`memory_corpus.Drift`). That is a documentation defect the organism can
    detect and repair itself.

``orphaned``
    Every declared module is gone. The topic describes a shape of the
    codebase that no longer exists.

``unreachable``
    The admission ledger has watched this topic LOSE, repeatedly, and never
    win a slot. Memory nobody can reach is either mis-scoped frontmatter or
    dead weight — and until the ledger existed there was no way to tell the
    difference from "nobody needed it yet".

``suspect``
    `memory_utility` says this topic correlates with ops that FAILED, with
    enough confidence to be worth a look. **Memory reporting its own
    suspected falsity.** Claude Code structurally cannot produce this signal:
    it has no operation outcomes to learn from.

``uncovered``
    A module the organism edits often and has no topic about at all.

Event-primary, per the Gap #4 discipline
-----------------------------------------
Polling would be the easy version. Instead the two real event sources are
used: ``fs.changed.*`` for edits under ``docs/memory_topics``, and the
admission ledger's own listener hook — a routing pass IS the event that
changes what "unreachable" means. The poll loop is the fallback, at a long
interval, not the hot path.

Storms are the obvious failure mode
------------------------------------
This corpus measured 54 drifted topics out of 64 ranked. A sensor that
emitted one op per finding would, at first boot, enqueue fifty ops of
documentation chores and starve everything else — and a single commit
touching 300 topic files (this arc shipped one) would do it again.

So: debounce the FS burst, cap emissions per scan, rank by severity, and
deduplicate on ``(kind, content_hash)``. The hash is what makes the dedup
self-clearing — repairing a topic changes its payload, so the old finding
can never re-fire, and the new payload is only flagged if it is STILL
defective.

Cost
----
These are chores, not emergencies. The source is registered in
``urgency_router._BACKGROUND_SOURCES`` and every envelope carries
``urgency="low"``, so the whole class routes DW-only with no Claude
fallback (~$0.002/op). The whitelist comment in ``intent_envelope`` records
what happens otherwise: sensors that misdeclared their source got
IMMEDIATE-stamped and burned $0.53 of Claude budget on doc scans.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.MemoryHygieneSensor")

MEMORY_HYGIENE_SCHEMA_VERSION: str = "memory_hygiene.1"

#: The source token. Registered in THREE places, and missing any one of them
#: fails differently and silently:
#:   * ``intent_envelope._VALID_SOURCES``  — else every envelope is dropped
#:   * ``intent.signals.SignalSource``     — else typed consumers cannot
#:                                            classify the origin
#:   * ``urgency_router._BACKGROUND_SOURCES`` — else these chores route to
#:                                            Claude and cost 15x
SOURCE = "memory_hygiene"

__all__ = [
    "MEMORY_HYGIENE_SCHEMA_VERSION",
    "SOURCE",
    "HygieneFinding",
    "MemoryHygieneSensor",
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
    """``JARVIS_MEMORY_HYGIENE_SENSOR_ENABLED``.

    Default **false**. A sensor that enqueues autonomous work earns its
    default-on the way every other capability in this codebase does — by
    surviving a soak — not by being new. The reactive path is unaffected
    either way.
    """
    return _flag("JARVIS_MEMORY_HYGIENE_SENSOR_ENABLED", "0")


def _max_emit_per_scan() -> int:
    """``JARVIS_MEMORY_HYGIENE_MAX_EMIT`` — default 3.

    The cold-start bound. This corpus has ~54 drifted topics; emitting them
    all would enqueue fifty chores ahead of every real signal on the first
    boot. Three per scan converges over days instead of flooding in one
    minute, which is the correct pace for hygiene.
    """
    return int(_num("JARVIS_MEMORY_HYGIENE_MAX_EMIT", 3, 1, 50))


def _poll_interval_s() -> float:
    """Fallback cadence. Long, because the FS and ledger events are primary."""
    return _num("JARVIS_MEMORY_HYGIENE_POLL_S", 3600.0, 60.0, 86400.0)


def _debounce_s() -> float:
    return _num("JARVIS_MEMORY_HYGIENE_DEBOUNCE_S", 30.0, 1.0, 600.0)


def _cooldown_s() -> float:
    return _num("JARVIS_MEMORY_HYGIENE_COOLDOWN_S", 900.0, 0.0, 86400.0)


def _min_passes_for_unreachable() -> int:
    """Routing passes a topic must LOSE before "unreachable" is a claim.

    ``JARVIS_MEMORY_HYGIENE_MIN_PASSES``. Without a floor, every topic is
    unreachable after the first op — the same evidence-mass problem
    `memory_utility` solves with a saturating confidence curve, in its
    discrete form.
    """
    return int(_num("JARVIS_MEMORY_HYGIENE_MIN_PASSES", 12, 2, 1000))


def _suspect_floor() -> float:
    """Utility multiplier below which a topic is worth questioning.

    ``JARVIS_MEMORY_HYGIENE_SUSPECT_BELOW``. Paired with a confidence floor
    so a single unlucky op never accuses a topic of being wrong.
    """
    return _num("JARVIS_MEMORY_HYGIENE_SUSPECT_BELOW", 0.85, 0.0, 1.0)


def _suspect_min_confidence() -> float:
    return _num("JARVIS_MEMORY_HYGIENE_SUSPECT_MIN_CONF", 0.5, 0.0, 1.0)


@dataclass(frozen=True)
class HygieneFinding:
    """One defect in the organism's own memory."""

    kind: str
    uri: str
    content_hash: str
    summary: str
    target_files: Tuple[str, ...]
    severity: str
    evidence: Dict[str, Any]

    @property
    def dedup_key(self) -> str:
        """``kind:hash`` — self-clearing.

        Keyed on the PAYLOAD, so repairing a topic produces a new hash and
        the old finding can never re-fire; the repaired text is re-examined
        on its own merits. A path-keyed dedup would suppress the finding
        forever after one failed repair attempt.
        """
        return f"{self.kind}:{self.content_hash}"

    #: Severity ordering for the per-scan cap. Orphaned topics describe a
    #: codebase that is gone; suspect topics may be actively misleading
    #: generation. Both outrank "this could be better documented".
    _RANK = {"orphaned": 0, "suspect": 1, "drifted": 2,
             "unreachable": 3, "uncovered": 4}

    @property
    def rank(self) -> int:
        return self._RANK.get(self.kind, 99)


class MemoryHygieneSensor:
    """Turns memory's self-knowledge into `IntentSignal`s. NEVER raises.

    Mirrors `DocStalenessSensor`'s shape — ``start``/``stop``/
    ``subscribe_to_bus``/``scan_once`` — because that is the contract
    `IntakeLayer` already drives and a second sensor shape would be a second
    lifecycle for the harness to get wrong.
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        *,
        project_root: Optional[Path] = None,
        poll_interval_s: Optional[float] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._project_root = Path(project_root or ".")
        self._poll_interval_s = (poll_interval_s if poll_interval_s is not None
                                 else _poll_interval_s())
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._debounce_task: Optional[asyncio.Task] = None
        self._seen: Dict[str, float] = {}
        self._last_scan_mono = 0.0
        self._emitted = 0
        self._scans = 0

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._running or not sensor_enabled():
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[MemoryHygiene] started (fallback poll %.0fs)",
                    self._poll_interval_s)

    def stop(self) -> None:
        self._running = False
        for task in (self._task, self._debounce_task):
            if task is not None and not task.done():
                task.cancel()
        self._task = None
        self._debounce_task = None

    async def subscribe_to_bus(self, event_bus: Any) -> None:
        """Event-primary wiring. NEVER raises.

        Two real sources, no polling for either:

        * ``fs.changed.*`` — a topic file was edited
        * the admission ledger's listener — a routing pass just changed what
          "unreachable" means for every topic that lost it
        """
        try:
            await event_bus.subscribe("fs.changed.*", self._on_fs_event)
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] fs subscription failed", exc_info=True)
        try:
            from backend.core.ouroboros.governance.memory_admission import (
                get_default_registry,
            )
            # Registry-level, not per-op: ledgers are created lazily per op,
            # so subscribing to one would only ever hear about that op.
            get_default_registry().add_listener(self._on_admission)
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] ledger hook failed", exc_info=True)

    async def _on_fs_event(self, event: Any) -> None:
        """Debounced. A 300-file commit is ONE scan, not 300."""
        try:
            path = str(getattr(event, "path", "") or
                       (event or {}).get("path", "") if isinstance(event, dict)
                       else "")
            if path and "memory_topics" not in path:
                return
            self._schedule_debounced_scan()
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] fs event degraded", exc_info=True)

    def _on_admission(self, payload: Dict[str, Any]) -> None:
        """A routing pass landed. Cheap and synchronous — just schedules."""
        try:
            self._schedule_debounced_scan()
        except Exception:  # noqa: BLE001
            pass

    def _schedule_debounced_scan(self) -> None:
        if self._debounce_task is not None and not self._debounce_task.done():
            return  # a window is already absorbing this burst
        try:
            self._debounce_task = asyncio.create_task(self._debounced_scan())
        except RuntimeError:
            pass  # no running loop (sync context / teardown)

    async def _debounced_scan(self) -> None:
        try:
            await asyncio.sleep(_debounce_s())
            await self._scan_swallow_errors()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] debounced scan degraded", exc_info=True)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval_s)
                if self._running:
                    await self._scan_swallow_errors()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.debug("[MemoryHygiene] poll degraded", exc_info=True)

    async def _scan_swallow_errors(self) -> None:
        try:
            await self.scan_once()
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] scan raised", exc_info=True)

    # -- detection --------------------------------------------------------

    async def collect_findings(self) -> List[HygieneFinding]:
        """Every defect memory can currently see. NEVER raises.

        Separated from emission so the detection half is testable without a
        router, and so a caller can render findings without enqueueing work.
        """
        findings: List[HygieneFinding] = []
        try:
            from backend.core.ouroboros.governance.module_routing import (
                _load_topic_fragments,
            )
            topics, _listing = await _load_topic_fragments(
                self._project_root / "docs" / "memory_topics",
                self._project_root)
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] corpus load failed", exc_info=True)
            return findings

        by_hash = {t.content_hash: t for t in topics}

        for topic in topics:
            if topic.drift == "orphaned":
                findings.append(HygieneFinding(
                    kind="orphaned", uri=topic.uri,
                    content_hash=topic.content_hash,
                    summary=(f"Memory topic '{topic.title}' declares modules "
                             f"that no longer exist. Update its `modules:` "
                             f"frontmatter to the current paths, or retire "
                             f"the topic if the subject is gone."),
                    target_files=(topic.uri,), severity="low",
                    evidence={"declared_modules": list(topic.modules),
                              "drift": topic.drift},
                ))
            elif topic.drift == "drifted":
                findings.append(HygieneFinding(
                    kind="drifted", uri=topic.uri,
                    content_hash=topic.content_hash,
                    summary=(f"Memory topic '{topic.title}' was written before "
                             f"the modules it describes last changed. Re-read "
                             f"those modules and refresh the topic so it "
                             f"still describes what the code does."),
                    target_files=(topic.uri,) + tuple(topic.modules[:4]),
                    severity="low",
                    evidence={"declared_modules": list(topic.modules),
                              "drift": topic.drift},
                ))

        findings.extend(self._admission_findings(by_hash))
        findings.extend(self._utility_findings(by_hash))
        return findings

    def _admission_findings(self, by_hash: Dict[str, Any]) -> List[HygieneFinding]:
        """Topics the ledger has watched lose and never win. NEVER raises."""
        out: List[HygieneFinding] = []
        try:
            from backend.core.ouroboros.governance.memory_admission import (
                get_default_registry,
            )
            registry = get_default_registry()
            with registry._lock:  # noqa: SLF001 — same package, bounded read
                ledgers = list(registry._ledgers.values())

            losses: Dict[str, int] = {}
            wins: Dict[str, int] = {}
            for ledger in ledgers:
                for record in ledger.records():
                    for row in record.rows:
                        bucket = wins if row.admitted else losses
                        bucket[row.content_hash] = bucket.get(
                            row.content_hash, 0) + 1

            floor = _min_passes_for_unreachable()
            for content_hash, lost in losses.items():
                if wins.get(content_hash, 0) > 0 or lost < floor:
                    continue
                topic = by_hash.get(content_hash)
                if topic is None:
                    continue
                out.append(HygieneFinding(
                    kind="unreachable", uri=topic.uri,
                    content_hash=content_hash,
                    summary=(f"Memory topic '{topic.title}' has been "
                             f"considered {lost} times and selected zero "
                             f"times. Its `modules:` frontmatter probably "
                             f"does not match the files ops actually touch — "
                             f"re-scope it, or retire it if it is obsolete."),
                    target_files=(topic.uri,), severity="low",
                    evidence={"considered": lost, "admitted": 0,
                              "declared_modules": list(topic.modules)},
                ))
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] admission scan degraded", exc_info=True)
        return out

    def _utility_findings(self, by_hash: Dict[str, Any]) -> List[HygieneFinding]:
        """Topics that correlate with FAILED ops. NEVER raises.

        The signal Claude Code cannot produce, because it has no operation
        outcomes. Gated on confidence as well as multiplier so one unlucky
        op never accuses a topic of being wrong.
        """
        out: List[HygieneFinding] = []
        try:
            from backend.core.ouroboros.governance.memory_utility import (
                get_store, utility_enabled,
            )
            if not utility_enabled():
                return out
            store = get_store()
            below = _suspect_floor()
            min_conf = _suspect_min_confidence()
            for content_hash in store.hashes():
                reading = store.reading(content_hash)
                if reading.cold or reading.confidence < min_conf:
                    continue
                if reading.multiplier >= below:
                    continue
                topic = by_hash.get(content_hash)
                if topic is None:
                    continue
                out.append(HygieneFinding(
                    kind="suspect", uri=topic.uri, content_hash=content_hash,
                    summary=(f"Memory topic '{topic.title}' has been present "
                             f"in prompts for operations that failed more "
                             f"often than the corpus average "
                             f"(x{reading.multiplier:.2f}, confidence "
                             f"{reading.confidence:.2f}, n={reading.observations}). "
                             f"Verify it is still accurate — correlation is "
                             f"not proof, so confirm against the code before "
                             f"changing anything."),
                    target_files=(topic.uri,), severity="low",
                    evidence={"multiplier": round(reading.multiplier, 4),
                              "confidence": round(reading.confidence, 4),
                              "observations": reading.observations,
                              "corpus_polarity": reading.corpus_polarity},
                ))
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] utility scan degraded", exc_info=True)
        return out

    # -- emission ---------------------------------------------------------

    async def scan_once(self) -> List[HygieneFinding]:
        """Detect, bound, emit. Returns what was FOUND. NEVER raises."""
        if not sensor_enabled():
            return []
        now = time.monotonic()
        cooldown = _cooldown_s()
        if cooldown and (now - self._last_scan_mono) < cooldown:
            return []
        self._last_scan_mono = now
        self._scans += 1

        findings = await self.collect_findings()
        if not findings:
            return []

        fresh = [f for f in findings if f.dedup_key not in self._seen]
        # Severity first, then deterministic by uri — the same corpus must
        # produce the same three ops on every boot, or a restart reshuffles
        # the backlog for no reason.
        fresh.sort(key=lambda f: (f.rank, f.uri))
        cap = _max_emit_per_scan()

        emitted = 0
        for finding in fresh[:cap]:
            if await self._emit(finding):
                self._seen[finding.dedup_key] = time.time()
                emitted += 1

        self._emitted += emitted
        logger.info(
            "[MemoryHygiene] scan: %d finding(s), %d fresh, %d emitted "
            "(cap %d) — %s",
            len(findings), len(fresh), emitted, cap,
            ", ".join(f"{k}={sum(1 for f in findings if f.kind == k)}"
                      for k in ("orphaned", "suspect", "drifted",
                                "unreachable", "uncovered")),
        )
        return findings

    async def _emit(self, finding: HygieneFinding) -> bool:
        """One envelope into the intake router. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )
            envelope = make_envelope(
                source=SOURCE,
                description=finding.summary,
                target_files=finding.target_files,
                repo=self._repo,
                confidence=0.7,
                # Always low: these are chores. The urgency is what keeps the
                # whole class on the BACKGROUND route (DW-only, ~$0.002/op)
                # instead of burning the Claude tier on documentation.
                urgency="low",
                evidence={
                    "schema_version": MEMORY_HYGIENE_SCHEMA_VERSION,
                    "kind": finding.kind,
                    "topic_uri": finding.uri,
                    "content_hash": finding.content_hash,
                    "sensor": "MemoryHygieneSensor",
                    **finding.evidence,
                },
                requires_human_ack=False,
            )
            result = await self._router.ingest(envelope)
            return result == "enqueued"
        except Exception:  # noqa: BLE001
            logger.debug("[MemoryHygiene] emit failed for %s", finding.uri,
                         exc_info=True)
            return False

    def health(self) -> Dict[str, Any]:
        """Bounded projection for `/observability` + `/memory`. NEVER raises."""
        return {
            "schema_version": MEMORY_HYGIENE_SCHEMA_VERSION,
            "enabled": sensor_enabled(),
            "running": self._running,
            "scans": self._scans,
            "emitted": self._emitted,
            "suppressed": len(self._seen),
            "max_emit_per_scan": _max_emit_per_scan(),
        }
