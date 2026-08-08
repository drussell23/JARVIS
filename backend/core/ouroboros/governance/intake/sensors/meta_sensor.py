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


def meta_sensor_interval_s() -> float:
    """Seconds between dormancy sweeps. Default 1800 (30 min).

    Lives here beside the other knobs rather than at the construction site,
    which is where every other sensor resolves its interval. The difference
    matters: ``MetaSensor.__init__`` already carried ``1800.0`` as a keyword
    default, so an ``os.environ.get(..., "1800")`` written next to the
    constructor call would have made two authorities for one number — the
    defect ``tests/architecture/test_env_default_single_authority.py`` exists
    to catch, reintroduced in the act of wiring the sensor that watches for
    exactly this class of silent inertness.

    Dormancy is a slow signal: it is computed over a rolling window of
    ``empty_postmortem_window()`` postmortems, so sweeping faster than the
    ledger changes costs disk reads and tells nobody anything new. Floored at
    one minute so a mistyped value cannot turn an alarm into a busy-loop.
    """
    raw = os.environ.get("JARVIS_META_SENSOR_INTERVAL_S", "1800")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 1800.0
    return max(60.0, val)


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
    # Only ops that got PAST the claim-capture phase belong in this rate.
    #
    # The first version of this detector divided by every postmortem in the
    # window. Measured on the live ledger the day the alarm was first heard:
    # 3,333 of 5,644 empty records were ops that terminated at CLASSIFY,
    # before PLAN exists to capture anything, and 100% of routed ops with a
    # postmortem HAD captured claims. So the alarm sat at a permanent 71%,
    # named the wrong subsystem, and pointed the operator at wiring that was
    # working — while the real signal (claims captured and then unjudgeable)
    # went unreported for months.
    #
    # `claims_were_applicable` is derived from the enclosing ledger record's
    # phase against the FSM's own canonical ordering. `unknown` provenance is
    # excluded from BOTH numerator and denominator rather than assumed
    # either way.
    applicable = [pm for pm in pms if getattr(pm, "claims_were_applicable", True)]
    if len(applicable) < min_records:
        # Not enough ops reached the capture phase to say anything. Silence
        # here is a measurement, not an all-clear — a window in which almost
        # nothing routes is its own signal, and belongs to a detector that
        # names THAT rather than being smuggled in under this one.
        return None
    empty_count = sum(
        1 for pm in applicable if getattr(pm, "total_claims", 0) == 0
    )
    rate = empty_count / max(1, len(applicable))
    if rate < threshold:
        return None
    skipped = len(pms) - len(applicable)
    return DormancyFinding(
        detector_kind="empty_postmortem_rate",
        severity="p1",
        summary=(
            f"VERIFICATION LOOP IS NOT EXERCISING — "
            f"{empty_count}/{len(applicable)} ({rate:.0%}) of ops that "
            f"reached the claim-capture phase have total_claims=0. "
            f"({skipped} of {len(pms)} records excluded: the op stopped "
            f"before PLAN, where zero claims is correct.) Phase 2 is "
            f"recording terminations but not predictions. Check Priority A "
            f"claim-capture wiring at every PLAN exit."
        ),
        evidence=(
            ("detector_kind", "empty_postmortem_rate"),
            ("empty_count", empty_count),
            ("total_count", len(applicable)),
            ("records_read", len(pms)),
            ("excluded_not_applicable", skipped),
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


def unjudgeable_claim_threshold() -> float:
    """Fraction of evaluated claims returning INSUFFICIENT_EVIDENCE above
    which the loop is judged non-exercising. Default 0.5.

    Lower than the empty-postmortem threshold on purpose. A claim that cannot
    be judged is strictly worse than one that was never made: it costs a
    ledger write, it appears in the count, and it reads as coverage.
    """
    raw = os.environ.get("JARVIS_META_UNJUDGEABLE_CLAIM_THRESHOLD", "0.5")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, val))


def unjudgeable_min_claims() -> int:
    """Minimum evaluated claims before the unjudgeable detector speaks."""
    raw = os.environ.get("JARVIS_META_UNJUDGEABLE_MIN_CLAIMS", "50")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 50
    return max(1, val)


def _evaluate_unjudgeable_claim_rate() -> Optional[DormancyFinding]:
    """Claims are being MADE and cannot be JUDGED.

    The signal that was hiding behind the empty-postmortem alarm. On the
    ledger the day the alarm was first heard: of 18,414 claims recorded at
    COMPLETE, 13,911 (76%) returned INSUFFICIENT_EVIDENCE and **zero ever
    returned FAILED** — across 151,612 claims all-time. Three default claims
    accounted for the insufficiency in exactly equal counts (4,637 each),
    which is the signature of properties attached to every op whose required
    evidence is never supplied:

        provider_route, is_read_only, providers_used
        diff_text
        test_files_pre, test_files_post

    A ``must_hold`` claim that always evaluates INSUFFICIENT is not a safety
    property. It is a comment with a ledger write attached, and because
    INSUFFICIENT is not FAILED it never blocks and never surfaces. The
    postmortem reports ``total_claims`` going up and the verification loop
    reads as healthy while deciding nothing.

    This detector measures whether verdicts are being REACHED, which is the
    question ``empty_postmortem_rate`` was always meant to be asking.

    NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.verification import (
            list_recent_postmortems,
        )
    except Exception:  # noqa: BLE001
        return None
    window = empty_postmortem_window()
    threshold = unjudgeable_claim_threshold()
    min_claims = unjudgeable_min_claims()
    try:
        pms = list_recent_postmortems(limit=window)
    except Exception:  # noqa: BLE001
        return None

    total = sum(int(getattr(pm, "total_claims", 0) or 0) for pm in pms)
    if total < min_claims:
        return None
    insufficient = sum(
        int(getattr(pm, "insufficient_count", 0) or 0) for pm in pms
    )
    errored = sum(int(getattr(pm, "error_count", 0) or 0) for pm in pms)
    undecided = insufficient + errored
    rate = undecided / max(1, total)
    if rate < threshold:
        return None

    # Name the evidence keys actually responsible rather than making the
    # operator re-derive them: the reason strings are already on the
    # outcomes, and a finding that says "76% unjudgeable" without saying
    # WHICH property sends them back to a ledger walk.
    reasons: Dict[str, int] = {}
    for pm in pms:
        for outcome in getattr(pm, "outcomes", ()) or ():
            try:
                verdict = outcome.verdict
                if verdict.verdict.value == "passed":
                    continue
                key = str(verdict.property_name or verdict.kind or "?")
                reasons[key] = reasons.get(key, 0) + 1
            except Exception:  # noqa: BLE001 — malformed outcome, skip
                continue
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    top_repr = "; ".join(f"{name}={count}" for name, count in top) or "n/a"

    return DormancyFinding(
        detector_kind="unjudgeable_claim_rate",
        severity="p1",
        summary=(
            f"CLAIMS ARE MADE BUT NOT JUDGED — {undecided}/{total} "
            f"({rate:.0%}) of recorded claims returned "
            f"INSUFFICIENT_EVIDENCE or evaluator error. A must_hold claim "
            f"that cannot be evaluated never blocks, so the loop reports "
            f"coverage while deciding nothing. Worst properties: {top_repr}"
        ),
        evidence=(
            ("detector_kind", "unjudgeable_claim_rate"),
            ("total_claims", total),
            ("insufficient_count", insufficient),
            ("error_count", errored),
            ("rate", rate),
            ("threshold", threshold),
            ("window", window),
            ("top_properties", top_repr),
            ("remediation", (
                "For each property above, compare its evidence_required "
                "tuple against what the evidence collector actually "
                "supplies at evaluation time. Either the collector must "
                "produce those keys or the property must declare "
                "requirements it can be judged on — a claim nobody can "
                "settle should not carry must_hold severity."
            )),
        ),
        target_files=(
            "backend/core/ouroboros/governance/verification/evidence_collectors.py",
            "backend/core/ouroboros/governance/verification/default_claims.py",
            "backend/core/ouroboros/governance/verification/property_oracle.py",
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
    register_dormancy_detector(
        DormancyDetector(
            detector_kind="unjudgeable_claim_rate",
            severity="p1",
            description=(
                "Fires when >threshold of recorded claims return "
                "INSUFFICIENT_EVIDENCE or evaluator error — the loop is "
                "making predictions it has no evidence to settle, so it "
                "reports coverage while deciding nothing."
            ),
            evaluate=_evaluate_unjudgeable_claim_rate,
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
        poll_interval_s: Optional[float] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        # `None` means "ask the resolver", which is the only place the 1800
        # lives. An explicit value still wins, so every existing caller and
        # test behaves exactly as before.
        self._poll_interval_s = (
            meta_sensor_interval_s() if poll_interval_s is None
            else float(poll_interval_s)
        )
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
        # Event-loop unblocking (2026-07-22): detector evaluate() reads
        # the postmortem ledger from disk (list_recent_postmortems —
        # the bt-2026-07-22-022146 5.0s STUCK_FRAME class). Dispatch
        # each pure-function evaluate to the dedicated advisor-blast
        # executor (Task #88f isolation pool — DRY, no new executor);
        # fail-soft to the legacy on-loop call if the pool is
        # unavailable (import error → sensor still functions).
        try:
            from backend.core.ouroboros.governance.operation_advisor import (  # noqa: E501
                _get_advisor_blast_executor as _blast_pool,
            )
            _pool = _blast_pool()
            _loop = asyncio.get_running_loop()
        except Exception:  # noqa: BLE001 — degraded: evaluate on-loop
            _pool = None
            _loop = None
        for detector in list_dormancy_detectors():
            try:
                if _pool is not None and _loop is not None:
                    finding = await _loop.run_in_executor(
                        _pool, detector.evaluate,
                    )
                else:
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
    "meta_sensor_interval_s",
    "register_dormancy_detector",
    "reset_registry_for_tests",
    "unregister_dormancy_detector",
]
