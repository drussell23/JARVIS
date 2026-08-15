"""The organism notices its own dead limbs — without being asked.

`capability_liveness.snapshot()` already answers the question that matters:
of everything O+V declares it can do, what is actually reachable? Run today
it reported **190 severance candidates**, 107 of them with telemetry that has
never fired, including `repair_engine` — the L2 self-repair loop that CLAUDE.md
documents as enabled by default and closing the Ouroboros cycle.

That answer existed all day and nobody had it, because getting it required
somebody to run a script. A self-perception layer that must be invoked is not
self-perception; it is a diagnostic tool that happens to live in the repo.
This makes it autonomic: sampled on a cadence, and severance becomes an
`IntentSignal` the governed loop schedules work against.

Criticality is DERIVED, not listed
-----------------------------------
The obvious implementation hardcodes ``{"repair_engine", "aegis", "cage"}``
and is wrong within a month — a new safety capability is not in the list, and
nothing tells you. Every liveness verdict already carries a ``category``
sourced from the FlagRegistry taxonomy, and 129 of today's 190 candidates are
``safety``. So severity keys on THAT: a severed safety capability is
high-severity because the taxonomy already said it was safety-critical, and a
capability added tomorrow inherits the judgement automatically.

``JARVIS_LIVENESS_CRITICAL_CATEGORIES`` widens the set without code, for the
case where a deployment's idea of critical differs from the taxonomy's.

Firing status is the second axis, and the sharper one
------------------------------------------------------
AST reachability alone over-reports: a symbol dispatched dynamically has no
static call site and is perfectly alive. The verdicts pair it with a
telemetry ``firing`` state, and ``SILENT`` — a channel that has never emitted
— is the signal that a capability is not merely hard to see statically but
has genuinely never run. ``UNKNOWN`` is reported separately rather than
folded into either: a capability with no telemetry at all is a different
problem from one with silent telemetry, and merging them would hide the
observability gap behind a liveness number.

Reuses the memory-hygiene arc
------------------------------
Same lifecycle (``start``/``stop``/``scan_once``/``health``), the same
debounce + per-scan cap + payload-keyed dedup, and the same token bucket the
cage sensor uses. A severance condition is persistent by nature — 190
findings do not resolve between scans — so without dedup this would re-emit
the same backlog every cycle, which is the alert-fatigue failure the cage
sensor was built to avoid.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.sensors.emission_control import (
    TokenBucket,
)

logger = logging.getLogger("Ouroboros.LivenessSensor")

LIVENESS_SENSOR_SCHEMA_VERSION: str = "liveness_sensor.1"

#: Registered in THREE places — `_VALID_SOURCES`, `SignalSource`,
#: `_BACKGROUND_SOURCES`. Missing any one fails silently and differently.
SOURCE = "capability_severance"

__all__ = [
    "LIVENESS_SENSOR_SCHEMA_VERSION",
    "SOURCE",
    "LivenessFinding",
    "LivenessSensor",
    "critical_categories",
    "sensor_enabled",
    "effective_firing",
    "severity_for",
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
    """``JARVIS_LIVENESS_SENSOR_ENABLED`` (default false)."""
    return _flag("JARVIS_LIVENESS_SENSOR_ENABLED", "0")


def critical_categories() -> frozenset:
    """Categories whose severance is HIGH severity. NEVER raises.

    Defaults to ``safety`` — the FlagRegistry category for kill switches and
    gates — because that taxonomy already encodes "this matters" and a second
    hand-kept list would drift from it. Widen with
    ``JARVIS_LIVENESS_CRITICAL_CATEGORIES`` (comma-separated).
    """
    try:
        raw = os.environ.get("JARVIS_LIVENESS_CRITICAL_CATEGORIES", "").strip()
        if raw:
            return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
    except Exception:  # noqa: BLE001
        pass
    return frozenset({"safety"})


def _severed_floor() -> float:
    """Fraction of a capability's callables that must be unreachable.

    ``JARVIS_LIVENESS_SEVERED_FLOOR``. A capability with one unreferenced
    helper is normal; one with most of its surface unreachable is not.
    """
    return _num("JARVIS_LIVENESS_SEVERED_FLOOR", 0.30, 0.0, 1.0)


def _max_emit_per_scan() -> int:
    """``JARVIS_LIVENESS_MAX_EMIT`` — default 2.

    Today's snapshot held 190 candidates. Emitting them would enqueue 190 ops
    of archaeology ahead of every real signal, which is the same flood the
    memory sensor caps at 3.
    """
    return int(_num("JARVIS_LIVENESS_MAX_EMIT", 2, 1, 25))


def _poll_interval_s() -> float:
    """``JARVIS_LIVENESS_POLL_S`` — default 6h.

    Severance changes on the timescale of commits, not seconds, and the
    snapshot walks the whole backend. Sampling it often would cost more than
    the answer is worth.
    """
    return _num("JARVIS_LIVENESS_POLL_S", 21600.0, 60.0, 604800.0)


def _debounce_s() -> float:
    return _num("JARVIS_LIVENESS_DEBOUNCE_S", 60.0, 0.0, 3600.0)


def _bucket_capacity() -> float:
    """Burst allowance. ``JARVIS_LIVENESS_BUCKET_CAPACITY``."""
    return _num("JARVIS_LIVENESS_BUCKET_CAPACITY", 2.0, 1.0, 50.0)


def _bucket_refill_per_s() -> float:
    """Sustained rate. ``JARVIS_LIVENESS_BUCKET_REFILL_PER_S`` — one finding
    an hour. Severance is archaeology; a backlog that took a year to
    accumulate does not need reporting faster than it can be read."""
    return _num("JARVIS_LIVENESS_BUCKET_REFILL_PER_S",
                1.0 / 3600.0, 1.0 / 604800.0, 10.0)


def _new_bucket() -> TokenBucket:
    """Bounds emissions per unit TIME — the same primitive the cage sensor
    uses, for the same reason: the per-scan cap bounds a burst, this bounds a
    sustained drip of distinct findings. The arithmetic is shared; the limits
    stay this sensor's own."""
    return TokenBucket(_bucket_capacity, _bucket_refill_per_s)


@dataclass(frozen=True)
class LivenessFinding:
    """One capability the organism declares and cannot reach."""

    source_file: str
    category: str
    flag: str
    firing: str
    fraction_severed: float
    severed_symbols: Tuple[str, ...]
    severity: str

    @property
    def dedup_key(self) -> str:
        """Keyed on the SYMBOLS, not the file.

        A file whose severed set changes has a genuinely different finding;
        one that merely got edited does not. Keying on the path would
        re-report on every unrelated commit, and keying on the count alone
        would miss a swap of one dead symbol for another.
        """
        return f"{self.source_file}|{self.flag}|{','.join(sorted(self.severed_symbols))}"

    @property
    def rank(self) -> Tuple[int, float]:
        return (0 if self.severity == "high" else 1, -self.fraction_severed)


def effective_firing(source_file: str, firing: str) -> str:
    """AST/telemetry firing state, corrected by the dynamic registry.

    Static reachability cannot see a pub/sub handler, an ``importlib`` load,
    or a ``getattr`` route, so a live capability reads as dead.
    `dynamic_dispatch_registry` records what actually happened at those
    seams, and this is where the two are intersected.

    Only INVOCATION clears a finding. A module that merely REGISTERED asked
    to be called and may never have been — reporting that as alive would let
    the audit confidently clear the exact failure it exists to catch, so it
    is surfaced under its own name instead. ``REGISTERED_NEVER_INVOKED`` is
    a sharper verdict than ``SILENT``, not a softer one: it rules out the
    innocent explanation that nothing ever tried.
    """
    try:
        from backend.core.ouroboros.governance.dynamic_dispatch_registry import (
            FIRING_DYNAMICALLY, REGISTERED_NEVER_INVOKED, dynamic_verdict,
        )
        verdict = dynamic_verdict(source_file)
        if verdict == FIRING_DYNAMICALLY:
            return FIRING_DYNAMICALLY
        if verdict == REGISTERED_NEVER_INVOKED:
            return REGISTERED_NEVER_INVOKED
    except Exception:  # noqa: BLE001 — registry absent -> static verdict stands
        pass
    return str(firing or "UNKNOWN")


def severity_for(category: str, firing: str, fraction: float,
                 source_file: str = "",
                 ledger_backed: Optional[bool] = None) -> str:
    """``high`` / ``low``. NEVER raises.

    HIGH requires BOTH a critical category AND telemetry that has never
    fired. Either alone is weaker evidence than it looks: a safety capability
    with FIRING telemetry is alive whatever the AST says, and a SILENT
    experimental helper is not worth waking anyone for.

    ``FIRING_DYNAMICALLY`` demotes to low — the module demonstrably ran, and
    a static index that could not see the edge is the index's limitation, not
    the module's fault. ``REGISTERED_NEVER_INVOKED`` stays eligible for high:
    it is evidence OF severance, not against it.

    ``ledger_backed`` decides whether a SILENT verdict is PROOF
    -------------------------------------------------------------
    `capability_firing.firing_verdict` says it outright: a ``ledger`` channel
    is reliable evidence-of-work, whereas a ``log``-only channel "is ambiguous
    (an absent log tag may mean 'ran silently' — an observability gap — not
    proven dormancy)". `capability_liveness` acts on that distinction, sorting
    ALIVE capabilities into ``dormant`` (ledger-backed) and
    ``observability_gaps`` (log-only), and publishes ``ledger_backed`` on
    every verdict.

    This function ignored it. Measured against the live snapshot, **11 of the
    12 rows scoring HIGH did so on log-only silence** — the sensor escalating
    capabilities whose dormancy is not merely unproven but *unprovable*. One
    of them was ``JARVIS_L2_ENABLED``: the loop that closes the Ouroboros
    cycle, reported severed because it delegates its bookkeeping to
    ``repair_tree`` and so writes no ``.jsonl`` literal of its own.

    An auditor that cries wolf 11 times out of 12 gets ignored on the twelfth,
    which is the one that mattered. So a SILENT verdict now escalates only
    when it is ledger-backed.

    ``None`` — the row did not carry the field — is treated as NOT proven, for
    the same reason `firing_verdict` refuses to turn "no derivable markers"
    into SILENT: absence of evidence is not evidence of absence, in either
    direction. A finding is still EMITTED at ``low``; nothing is hidden, it
    just stops being an alarm.
    """
    try:
        from backend.core.ouroboros.governance.dynamic_dispatch_registry import (
            FIRING_DYNAMICALLY,
        )
        resolved = effective_firing(source_file, firing) if source_file else (
            str(firing or ""))
        if resolved == FIRING_DYNAMICALLY:
            return "low"
        crit = str(category or "").strip().lower() in critical_categories()
        state = resolved.strip().upper()
        if state == "SILENT" and ledger_backed is not True:
            # Unprovable silence. NOT the same as REGISTERED_NEVER_INVOKED
            # below, which is positive evidence: the module declared itself
            # reachable and still never ran.
            return "low"
        dead = state in ("SILENT", "REGISTERED_NEVER_INVOKED")
        if crit and dead and float(fraction or 0.0) >= _severed_floor():
            return "high"
        return "low"
    except Exception:  # noqa: BLE001
        return "low"


class LivenessSensor:
    """Samples `capability_liveness` and emits severance as intake signals.

    Mirrors `MemoryHygieneSensor`'s lifecycle because that is the contract
    `IntakeLayer` drives; a second sensor shape is a second lifecycle to get
    wrong.
    """

    def __init__(self, repo: str, router: Any, *,
                 project_root: Optional[Any] = None,
                 poll_interval_s: Optional[float] = None) -> None:
        self._repo = repo
        self._router = router
        self._project_root = project_root
        self._poll_interval_s = (poll_interval_s if poll_interval_s is not None
                                 else _poll_interval_s())
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen: Dict[str, float] = {}
        self._bucket = _new_bucket()
        self._scans = 0
        self._emitted = 0
        self._throttled = 0
        self._last_counts: Dict[str, int] = {}

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._running or not sensor_enabled():
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[Liveness] started (poll %.0fs)", self._poll_interval_s)

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
                logger.debug("[Liveness] poll degraded", exc_info=True)

    # -- detection --------------------------------------------------------

    async def collect_findings(self) -> List[LivenessFinding]:
        """Severed capabilities worth reporting. NEVER raises.

        The snapshot walks the whole backend, so it is dispatched off the
        event loop — on a full-screen Application that scan would otherwise
        stall the frame that is meant to be showing its result.
        """
        try:
            from backend.core.ouroboros.governance.capability_liveness import (
                snapshot,
            )
            from backend.core.ouroboros.governance.cooperative_fs_io import (
                is_offload_error, offload,
            )
            result = await offload(snapshot, cpu_bound=False)
            if is_offload_error(result):
                result = snapshot()
            candidates = (result or {}).get("severance_candidates") or []
        except Exception:  # noqa: BLE001
            logger.debug("[Liveness] snapshot unavailable", exc_info=True)
            return []

        floor = _severed_floor()
        out: List[LivenessFinding] = []
        counts: Dict[str, int] = {}
        for row in candidates:
            try:
                source_file = str(row.get("source_file") or "?").split("/")[-1]
                # Correct the STATIC verdict with runtime evidence before
                # anything downstream counts or ranks it — otherwise the
                # health projection reports a severance the sensor has
                # already decided is a false positive.
                firing = effective_firing(
                    source_file, str(row.get("firing") or "UNKNOWN"))
                counts[firing] = counts.get(firing, 0) + 1
                fraction = float(row.get("fraction_severed") or 0.0)
                if fraction < floor:
                    continue
                category = str(row.get("category") or "")
                # `ledger_backed` rides along from the verdict. Computing the
                # distinction upstream and dropping it here is what let 11 of
                # 12 HIGH findings rest on unprovable silence.
                raw_backed = row.get("ledger_backed")
                severity = severity_for(
                    category, firing, fraction, source_file,
                    ledger_backed=(bool(raw_backed)
                                   if raw_backed is not None else None),
                )
                if firing == "FIRING_DYNAMICALLY":
                    # Demonstrably ran. Reporting it would train the operator
                    # to ignore this sensor, which costs more than the finding
                    # is worth.
                    continue
                out.append(LivenessFinding(
                    source_file=source_file,
                    category=category,
                    flag=str(row.get("flag") or ""),
                    firing=firing,
                    fraction_severed=fraction,
                    severed_symbols=tuple(row.get("severed_symbols") or ()),
                    severity=severity,
                ))
            except Exception:  # noqa: BLE001
                continue
        self._last_counts = counts
        out.sort(key=lambda f: f.rank)
        return out

    # -- emission ---------------------------------------------------------

    async def scan_once(self) -> List[LivenessFinding]:
        """Sample, bound, emit. Returns what was FOUND. NEVER raises."""
        if not sensor_enabled():
            return []
        try:
            self._scans += 1
            findings = await self.collect_findings()
            if not findings:
                return []

            fresh = [f for f in findings if f.dedup_key not in self._seen]
            emitted = 0
            for finding in fresh[:_max_emit_per_scan()]:
                if not self._bucket.take():
                    self._throttled += 1
                    break
                if await self._emit(finding):
                    self._seen[finding.dedup_key] = time.time()
                    emitted += 1

            self._emitted += emitted
            logger.info(
                "[Liveness] scan: %d severed (>=%.0f%%), %d fresh, %d emitted "
                "(cap %d) — firing %s",
                len(findings), _severed_floor() * 100, len(fresh), emitted,
                _max_emit_per_scan(), self._last_counts,
            )
            return findings
        except Exception:  # noqa: BLE001
            logger.debug("[Liveness] scan degraded", exc_info=True)
            return []

    async def _emit(self, finding: LivenessFinding) -> bool:
        """One envelope. NEVER raises.

        Urgency is ``low`` even for high-severity findings, matching the cage
        sensor: severance is archaeology, not an incident, and escalating the
        ROUTE would put repo-wide static analysis on the Claude tier. The
        severity rides in the evidence, where an operator surface can show it
        immediately and for free.
        """
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )
            symbols = ", ".join(finding.severed_symbols[:4]) or "(none listed)"
            envelope = make_envelope(
                source=SOURCE,
                description=(
                    f"Capability in {finding.source_file} is "
                    f"{finding.fraction_severed:.0%} unreachable and its "
                    f"telemetry is {finding.firing}: {symbols}. Category "
                    f"{finding.category!r}, flag {finding.flag or '(none)'}. "
                    f"Either wire it to a production caller, or retire it — a "
                    f"capability that is declared and unreachable reads as "
                    f"present from every flag and runs zero times."
                ),
                target_files=(finding.source_file,),
                repo=self._repo,
                confidence=0.55,
                urgency="low",
                evidence={
                    "schema_version": LIVENESS_SENSOR_SCHEMA_VERSION,
                    "sensor": "LivenessSensor",
                    "severity": finding.severity,
                    "category": finding.category,
                    "firing": finding.firing,
                    "fraction_severed": round(finding.fraction_severed, 4),
                    "severed_symbols": list(finding.severed_symbols[:12]),
                    "flag": finding.flag,
                },
                requires_human_ack=False,
            )
            return (await self._router.ingest(envelope)) == "enqueued"
        except Exception:  # noqa: BLE001
            logger.debug("[Liveness] emit failed for %s", finding.source_file,
                         exc_info=True)
            return False

    def health(self) -> Dict[str, Any]:
        """Bounded projection. NEVER raises."""
        return {
            "schema_version": LIVENESS_SENSOR_SCHEMA_VERSION,
            "enabled": sensor_enabled(),
            "running": self._running,
            "scans": self._scans,
            "emitted": self._emitted,
            "throttled": self._throttled,
            "suppressed": len(self._seen),
            "firing_counts": dict(self._last_counts),
            "bucket_tokens": round(self._bucket.tokens, 3),
        }
