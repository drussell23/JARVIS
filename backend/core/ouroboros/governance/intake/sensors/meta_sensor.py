"""Priority B — MetaSensor (degenerate-loop dormancy alarm).

The immune system policing itself. Every other sensor watches the
WORLD for capability gaps; this one watches O+V watching the world,
detecting when subsystems silently disable themselves.

Per PRD §25.5.2: today's three concurrent silent failures (PLAN-skip
nuking claim capture, wall-clock-cap regression, Slices 3a/3c
dormant) all share one signature — the system did not detect that
its own subsystem was silently inert. This sensor closes that gap.

The sensor is designed around a registry of ``DormancyDetector``
specs. Each spec is:
  * Frozen + hashable (replay-stable + safe across threads)
  * Pure-function ``evaluate()`` — same ledger state → same finding
  * Self-contained: declares its own threshold, window size, severity,
    and human-readable summary template

Operators amend the registry by registering additional detectors
from elsewhere; the seed set itself is amend-via-Pass-B governance
(manifest-listed, AST-validated). This keeps the sensor's threshold
discipline operator-controlled rather than buried in code.

Slice B1 ships ONE seed detector — ``empty_postmortem_rate`` — which
is the signal that motivated this whole priority (every postmortem
in soak #3 had ``total_claims=0``). Subsequent slices add the other
PRD-listed signals (TopologySentinel dormancy, wall-clock regression,
sensor mute, phase short-circuit) by registering additional detector
specs at module load.

Authority invariants (AST-pinned by tests):
  * NEVER imports orchestrator / phase_runner / candidate_generator
  * Pure stdlib + verification.* + intake.* read-only consumers
  * NEVER raises out of any public method
  * Read-only over the determinism ledger — never writes back

Master flag ``JARVIS_META_SENSOR_ENABLED`` (default ``false`` until
graduation per CLAUDE.md discipline; flips to default-true after
3 clean sessions).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

logger = logging.getLogger("Ouroboros.MetaSensor")


META_SENSOR_SCHEMA_VERSION = "meta_sensor.1"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def meta_sensor_enabled() -> bool:
    """``JARVIS_META_SENSOR_ENABLED`` (default ``true`` — graduated
    in the F2/F3/C-consumer arc 2026-04-29 alongside the soak that
    validates the empirical loop closure).

    Asymmetric env semantics — empty / whitespace = unset marker =
    graduated default; explicit false-class strings hot-revert.

    When off, ``scan_once()`` short-circuits to an empty list and
    no envelopes are emitted. The detector registry remains
    populated + queryable for operators inspecting the surface.
    Hot-revert: ``export JARVIS_META_SENSOR_ENABLED=false``."""
    raw = os.environ.get("JARVIS_META_SENSOR_ENABLED", "").strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def empty_postmortem_threshold() -> float:
    """Fraction of recent postmortems with ``total_claims=0`` above
    which the detector fires. Default 0.7 (70%) per PRD §25.5.2."""
    raw = os.environ.get(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "0.7",
    )
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.7
    return max(0.0, min(1.0, val))


def empty_postmortem_window() -> int:
    """Rolling-window size for the empty-postmortem detector.
    Default 100 records per PRD §25.5.2."""
    raw = os.environ.get(
        "JARVIS_META_EMPTY_POSTMORTEM_WINDOW", "100",
    )
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 100
    return max(10, val)  # floor at 10 to avoid noise


def empty_postmortem_min_records() -> int:
    """Minimum number of postmortems required before the detector
    emits. Avoids false-fire on a fresh ledger with 1-2 records.
    Default 20 — operators see signal once we have meaningful data."""
    raw = os.environ.get(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "20",
    )
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 20
    return max(1, val)


# ---------------------------------------------------------------------------
# Severity → IntentEnvelope urgency mapping
# ---------------------------------------------------------------------------
#
# IntentEnvelope's canonical urgency set is {critical, high, normal, low}
# (see intent_envelope._VALID_URGENCIES). Our P1/P2/P3 vocabulary mirrors
# the PRD §25.5.2 priority labels; we map them at emit time so the
# envelope contract stays untouched.

_SEVERITY_URGENCY_MAP = {
    "p1": "critical",
    "p2": "high",
    "p3": "normal",
}


def _map_severity_to_urgency(severity: str) -> str:
    """Map P1/P2/P3 (case-insensitive) to canonical urgency. Unknown
    inputs default to ``"normal"`` — never raise on garbage."""
    try:
        key = str(severity).strip().lower()
    except Exception:  # noqa: BLE001
        return "normal"
    return _SEVERITY_URGENCY_MAP.get(key, "normal")


# ---------------------------------------------------------------------------
# DormancyFinding — the output shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DormancyFinding:
    """One degenerate-loop signal. Frozen for ledger-replay-stability
    + hashable for cross-thread sharing.

    Fields
    ------
    detector_kind:
        Stable identifier of the detector that produced this finding
        (e.g., ``"empty_postmortem_rate"``). Used for dedup +
        observability filtering.
    severity:
        ``"p1"`` / ``"p2"`` / ``"p3"`` — maps to envelope urgency.
    summary:
        Human-readable one-liner. Stamped into the IntentEnvelope's
        description so operators see the alarm in /backlog
        auto-proposed with full context.
    evidence:
        Raw signal data (sample size, threshold, observed value).
        Persisted in the envelope's evidence dict for audit.
    target_files:
        Investigation entry-points for the operator — the source
        file(s) to inspect when this alarm fires. Each detector
        populates this with the relevant code path (e.g., the
        empty-postmortem detector points at plan_runner.py because
        that's where Priority A wiring lives). The IntentEnvelope
        contract requires non-empty target_files for non-vision
        sources; the MetaSensor falls back to a sentinel marker
        when a detector omits this field.
    """

    detector_kind: str
    severity: str
    summary: str
    evidence: Tuple[Tuple[str, Any], ...] = field(default_factory=tuple)
    target_files: Tuple[str, ...] = ()
    schema_version: str = META_SENSOR_SCHEMA_VERSION

    def evidence_dict(self) -> Dict[str, Any]:
        try:
            return dict(self.evidence)
        except (TypeError, ValueError):
            return {}


# ---------------------------------------------------------------------------
# DormancyDetector — registry value type
# ---------------------------------------------------------------------------


# Type for the pure evaluator function: returns a DormancyFinding when
# the signal is present, None when healthy. NEVER raises (callers
# wrap in try/except for defense-in-depth).
DetectorEvaluator = Callable[[], Optional[DormancyFinding]]


@dataclass(frozen=True)
class DormancyDetector:
    """One detector spec in the registry. Frozen + hashable.

    Fields
    ------
    detector_kind:
        Stable identifier matching DormancyFinding.detector_kind.
    severity:
        Default severity if the evaluator doesn't override it
        (evaluators MAY synthesize different severities based on
        observed signal magnitude).
    description:
        Human-readable explanation of what this detector watches for.
        Surfaced via /help meta-sensor.
    evaluate:
        Pure async-or-sync function that returns a DormancyFinding
        or None. Read-only against the determinism ledger / posture
        history / governor state.
    """

    detector_kind: str
    severity: str
    description: str
    evaluate: DetectorEvaluator


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: Dict[str, DormancyDetector] = {}
_REGISTRY_LOCK = threading.RLock()


def register_dormancy_detector(
    detector: DormancyDetector, *, overwrite: bool = False,
) -> None:
    """Install a detector. NEVER raises. Idempotent on identical
    re-register; rejects different-callable re-register without
    overwrite=True (defensive)."""
    if not isinstance(detector, DormancyDetector):
        return
    safe_kind = (
        str(detector.detector_kind).strip()
        if detector.detector_kind else ""
    )
    if not safe_kind:
        return
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(safe_kind)
        if existing is not None:
            if existing == detector:
                return  # silent no-op
            if not overwrite:
                logger.info(
                    "[MetaSensor] detector kind=%r already registered",
                    safe_kind,
                )
                return
        _REGISTRY[safe_kind] = detector


def unregister_dormancy_detector(detector_kind: str) -> bool:
    """Remove a detector. Returns True if removed, False if not
    present. NEVER raises."""
    safe_kind = str(detector_kind).strip() if detector_kind else ""
    if not safe_kind:
        return False
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(safe_kind, None) is not None


def list_dormancy_detectors() -> Tuple[DormancyDetector, ...]:
    """Return all registered detectors in stable alphabetical order."""
    with _REGISTRY_LOCK:
        return tuple(_REGISTRY[k] for k in sorted(_REGISTRY.keys()))


def reset_registry_for_tests() -> None:
    """Test isolation."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    _register_seed_detectors()


# ---------------------------------------------------------------------------
# Seed detector — empty_postmortem_rate
# ---------------------------------------------------------------------------


def _evaluate_empty_postmortem_rate() -> Optional[DormancyFinding]:
    """Detect the structural signature that nuked Phase 2 in soak #3:
    most postmortems have ``total_claims=0``, which means PLAN's
    claim-capture path is silently disabled.

    Reads the most recent ``window`` postmortems from the
    determinism ledger via ``list_recent_postmortems`` (Slice B's
    new reader). Computes the fraction with ``total_claims == 0``;
    fires when that fraction crosses ``threshold`` AND the sample
    size is at least ``min_records`` (avoids false-fire on fresh
    sessions).

    NEVER raises — verification module unavailable / ledger
    unreadable / parse errors all return None silently."""
    try:
        from backend.core.ouroboros.governance.verification import (
            list_recent_postmortems,
        )
    except Exception:  # noqa: BLE001 — verification unavailable
        return None
    window = empty_postmortem_window()
    threshold = empty_postmortem_threshold()
    min_records = empty_postmortem_min_records()
    try:
        pms = list_recent_postmortems(limit=window)
    except Exception:  # noqa: BLE001
        return None
    if len(pms) < min_records:
        return None
    empty_count = sum(
        1 for pm in pms if getattr(pm, "total_claims", 0) == 0
    )
    rate = empty_count / max(1, len(pms))
    if rate < threshold:
        return None
    return DormancyFinding(
        detector_kind="empty_postmortem_rate",
        severity="p1",
        summary=(
            f"VERIFICATION LOOP IS NOT EXERCISING — "
            f"{empty_count}/{len(pms)} ({rate:.0%}) of recent "
            f"postmortems have total_claims=0. Phase 2 is recording "
            f"terminations but not predictions. Check Priority A "
            f"claim-capture wiring at every PLAN exit."
        ),
        evidence=(
            ("detector_kind", "empty_postmortem_rate"),
            ("empty_count", empty_count),
            ("total_count", len(pms)),
            ("rate", rate),
            ("threshold", threshold),
            ("window", window),
            ("ledger_path_hint", ".jarvis/determinism/<session>/decisions.jsonl"),
            ("remediation", (
                "Verify JARVIS_DEFAULT_CLAIMS_ENABLED=true; verify "
                "plan_runner.py contains _capture_default_claims_at_-"
                "plan_exit calls preceding every return PhaseResult; "
                "check Slice A2 default-claim registry has seed "
                "specs registered."
            )),
        ),
        # Operator's investigation entry-points — Priority A wiring +
        # registry are the two files that, if regressed, produce this
        # signal.
        target_files=(
            "backend/core/ouroboros/governance/phase_runners/plan_runner.py",
            "backend/core/ouroboros/governance/verification/default_claims.py",
        ),
    )


def _register_seed_detectors() -> None:
    register_dormancy_detector(
        DormancyDetector(
            detector_kind="empty_postmortem_rate",
            severity="p1",
            description=(
                "Fires when >threshold of recent postmortems have "
                "total_claims=0 — the structural signature that "
                "PLAN-time claim capture has silently disabled itself."
            ),
            evaluate=_evaluate_empty_postmortem_rate,
        ),
    )


_register_seed_detectors()


# ---------------------------------------------------------------------------
# MetaSensor — the actual sensor class (follows existing protocol)
# ---------------------------------------------------------------------------


class MetaSensor:
    """Ouroboros intake sensor — degenerate-loop dormancy alarm.

    Follows the implicit sensor protocol (mirrors RuntimeHealthSensor):
      * ``async start()`` — spawn background poll loop
      * ``stop()``        — signal exit
      * ``async scan_once()`` — one detection pass; emits envelopes
        for each finding; dedup via finding.summary
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        poll_interval_s: float = 1800.0,  # 30 min default
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Dedup by (detector_kind, summary) — when severity / sample
        # changes meaningfully the summary text changes too, so
        # re-emission fires.
        self._seen: set = set()
        self._boot_scan_done = False

    async def start(self) -> None:
        if not meta_sensor_enabled():
            logger.info(
                "[MetaSensor] master flag off — start() is a no-op",
            )
            return
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"meta_sensor_{self._repo}",
        )
        logger.info(
            "[MetaSensor] started repo=%s poll_interval=%ds detectors=%d",
            self._repo, self._poll_interval_s,
            len(list_dormancy_detectors()),
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[MetaSensor] stopped repo=%s", self._repo)

    async def _poll_loop(self) -> None:
        if not self._boot_scan_done:
            await asyncio.sleep(60.0)  # let other zones boot
            self._boot_scan_done = True
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — never break the loop
                logger.exception("[MetaSensor] scan error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> List[DormancyFinding]:
        """Run every registered detector. Emit envelopes for new
        findings. NEVER raises."""
        if not meta_sensor_enabled():
            return []
        findings: List[DormancyFinding] = []
        for detector in list_dormancy_detectors():
            try:
                finding = detector.evaluate()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[MetaSensor] detector %s raised — skipped",
                    detector.detector_kind, exc_info=True,
                )
                continue
            if finding is None:
                continue
            findings.append(finding)
        # Emit envelopes for new findings only
        emitted = 0
        for finding in findings:
            dedup_key = f"{finding.detector_kind}::{finding.summary}"
            if dedup_key in self._seen:
                continue
            self._seen.add(dedup_key)
            # Map our P1/P2/P3 vocabulary to the IntentEnvelope's
            # canonical urgency set (critical/high/normal/low). Keep
            # the raw severity in evidence so /backlog history retains
            # full fidelity.
            urgency = _map_severity_to_urgency(finding.severity)
            ev = finding.evidence_dict()
            ev.setdefault("dormancy_severity", finding.severity)
            try:
                from backend.core.ouroboros.governance.intake.intent_envelope import (
                    make_envelope,
                )
                # Detector specifies target_files (the operator's
                # investigation entry-points). Falls back to a sentinel
                # marker when a detector omits the field — the
                # IntentEnvelope contract requires non-empty.
                tgt = finding.target_files or (
                    f"<meta_dormancy:{finding.detector_kind}>",
                )
                envelope = make_envelope(
                    source="meta_dormancy_alarm",
                    description=finding.summary,
                    target_files=tgt,
                    repo=self._repo,
                    confidence=1.0,  # deterministic — same ledger → same finding
                    urgency=urgency,
                    evidence=ev,
                    requires_human_ack=True,  # operator-review tier
                )
                result = await self._router.ingest(envelope)
                if result == "enqueued":
                    emitted += 1
                    logger.info(
                        "[MetaSensor] emitted: detector=%s severity=%s",
                        finding.detector_kind, finding.severity,
                    )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[MetaSensor] failed to emit finding kind=%s",
                    finding.detector_kind, exc_info=True,
                )
                continue
        if findings:
            logger.info(
                "[MetaSensor] scan complete: detectors_fired=%d emitted=%d",
                len(findings), emitted,
            )
        return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "META_SENSOR_SCHEMA_VERSION",
    "DormancyDetector",
    "DormancyFinding",
    "MetaSensor",
    "empty_postmortem_min_records",
    "empty_postmortem_threshold",
    "empty_postmortem_window",
    "list_dormancy_detectors",
    "meta_sensor_enabled",
    "register_dormancy_detector",
    "reset_registry_for_tests",
    "unregister_dormancy_detector",
]
