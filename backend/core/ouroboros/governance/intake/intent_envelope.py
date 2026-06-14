"""
IntentEnvelope — Canonical contract between sensors and the Unified Intake Router.

Schema version: 2c.1
Every field except ``lease_id`` is immutable once created.
``lease_id`` starts empty and is set by the router at WAL-enqueue time via
``IntentEnvelope.with_lease()``.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from backend.core.ouroboros.governance.operation_id import generate_operation_id

SCHEMA_VERSION = "2c.1"

_VALID_SOURCES = frozenset({
    "architecture",
    "backlog",
    "test_failure",
    "voice_human",
    "ai_miner",
    "capability_gap",
    "runtime_health",
    "exploration",
    "roadmap",
    "cu_execution",
    "intent_discovery",
    # Added 2026-04-12 to stop sensors from lying about their source as
    # "runtime_health" just to satisfy this whitelist. UrgencyRouter then
    # IMMEDIATE-stamped every TODO / doc / issue scan, which burned the
    # Claude budget in bt-2026-04-13-011909 ($0.53 Claude vs $0.002 DW).
    "todo_scanner",
    "doc_staleness",
    "github_issue",
    "performance_regression",
    "cross_repo_drift",
    "security_advisory",
    # Priority B Slice B1 — MetaSensor (degenerate-loop dormancy
    # alarm). Self-issues emitted when O+V's own subsystems silently
    # disable themselves (e.g., empty-postmortem rate > 70%). Stays
    # distinct from runtime_health because the signal is about the
    # cognition-layer, not the OS/runtime-layer.
    "meta_dormancy_alarm",
    "web_intelligence",
    # P1 Slice 3 (2026-04-26) — SelfGoalFormationEngine proposals reach
    # the intake via BacklogSensor's second-source ledger reader. The
    # distinct source ("auto_proposed") lets routers, sensors, and the
    # /backlog auto-proposed REPL surface filter on it without grepping
    # evidence dicts. Auto-proposed envelopes always carry
    # requires_human_ack=True per PRD §9 P1 operator-review tier.
    "auto_proposed",
    # Added 2026-04-18 for VisionSensor (Task 8 of VisionSensor + Visual
    # VERIFY arc). Mirrors ``SignalSource.VISION_SENSOR.value``. See
    # docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md.
    "vision_sensor",
    # Added 2026-05-05 for Phase 9 synthetic workload injection — the
    # honest-source-token approach that closes the headless-cadence
    # zero-ops blocker. Same precedent as the 2026-04-12 comment above
    # ("stop sensors from lying about their source"): synthetic test
    # workload must NEVER masquerade as real `runtime_health` / `ai_miner`
    # / etc. The Phase 9.2 graduation contract + downstream observability
    # filter on this token to distinguish cadence-injected envelopes from
    # production signal traffic. Routes BACKGROUND via UrgencyRouter
    # (low-cost; never burns Claude budget). See PRD §36.5 priority #1
    # + `phase_9_synthetic_workload.py`.
    "cadence_synthetic",
    # Added 2026-05-12 for SWE-Bench-Pro Phase 2 Phase B.2.1 evaluator
    # envelopes (PRD §40.7.9 / §40.7.10-b21). Same honest-source-token
    # precedent: external-benchmark workloads MUST NOT masquerade as
    # `cadence_synthetic` / `ai_miner` / etc. Downstream observability
    # (B.2.0.5 op_lifecycle SSE + IDE consumers + benchmark scorers in
    # Phase C) filter on this token to distinguish benchmark eval traffic
    # from production signal flow. The B.2.1 envelope_builder writes
    # this value via the single-source-of-truth ``ENVELOPE_SOURCE``
    # constant exported by
    # ``backend.core.ouroboros.governance.swe_bench_pro.envelope_builder``
    # — drift between that constant and this frozenset is caught by an
    # AST pin in the B.2.1 spine.
    "swe_bench_pro",
    # Added 2026-06-13 for Slice 239 (Adaptive Test-Sharding). The
    # TestCoverageEnforcer decouples a heavy GOAL's "generate tests for N
    # uncovered files" requirement into a SEPARATE background op instead of
    # inlining it into the primary patch prompt (which blew the deadline —
    # layer 9 of the s235 capstone arc). Honest-source token: decoupled
    # test-coverage work MUST NOT masquerade as `test_failure` / `backlog`.
    # Routes BACKGROUND via routing_override (low-cost; never burns Claude
    # budget); the resulting op still flows the full Iron Gate pipeline.
    "test_coverage",
})
_VALID_URGENCIES = frozenset({"critical", "high", "normal", "low"})

# F2 Slice 2 — allowed values for the optional ``routing_override``
# envelope field. Empty string is the "no override" sentinel (default).
# Non-empty values MUST be one of the five ProviderRoute enum values.
# We duplicate the enum values here rather than importing ProviderRoute
# to avoid an intake → governance.urgency_router import cycle (intent
# envelopes are upstream of routing; nothing in intake should depend
# on routing internals).
_VALID_ROUTING_OVERRIDES = frozenset({
    # Empty string = no override (default).
    # §41.3 #26 Phase 2 — added "informational" to mirror the
    # ProviderRoute closed-5→6 taxonomy expansion.
    "", "immediate", "standard", "complex", "background",
    "speculative", "informational",
})


# Sources whose ops genuinely do NOT know their target files at
# envelope-build time and must localize downstream. ``vision_sensor``:
# "there's a traceback on screen", not "fix file X". ``swe_bench_pro``:
# the authentic SWE-bench task is *localize the bug from the issue
# text* — surfacing the test_patch paths as target_files inverts the
# task (the agent is forbidden to edit tests; the scorer rejects test
# edits as cheating) AND surfacing gold_patch paths would leak the
# solution. So a SWE-bench envelope carries NO target_files; the
# exploration-first Iron Gate forces honest localization. Closed set
# (mirrors _VALID_SOURCES discipline) — additive, AST-pinnable,
# orthogonal to the evidence.user_attachments exemption below.
_EMPTY_TARGET_FILES_EXEMPT_SOURCES = frozenset({
    "vision_sensor",
    "swe_bench_pro",
})


class EnvelopeValidationError(ValueError):
    """Raised when an IntentEnvelope fails schema validation."""


@dataclass(frozen=True)
class IntentEnvelope:
    schema_version: str
    source: str
    description: str
    target_files: Tuple[str, ...]
    repo: str
    confidence: float
    urgency: str
    dedup_key: str
    causal_id: str
    signal_id: str
    idempotency_key: str
    lease_id: str
    evidence: Dict[str, Any]
    requires_human_ack: bool
    submitted_at: float  # time.monotonic()
    # F2 Slice 2 — optional per-envelope routing override. Default "" =
    # not set (pre-F2 byte-identical). When non-empty, MUST be one of
    # ProviderRoute enum values. Additive: SCHEMA_VERSION unchanged
    # because old envelopes + WAL-persisted dicts still parse cleanly
    # via ``from_dict``'s ``.get(..., "")`` fallback.
    routing_override: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EnvelopeValidationError(
                f"schema_version must be {SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )
        if self.source not in _VALID_SOURCES:
            raise EnvelopeValidationError(
                f"source must be one of {sorted(_VALID_SOURCES)}, got {self.source!r}"
            )
        if self.urgency not in _VALID_URGENCIES:
            raise EnvelopeValidationError(
                f"urgency must be one of {sorted(_VALID_URGENCIES)}, got {self.urgency!r}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise EnvelopeValidationError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        # Vision signals genuinely don't know target files at sensor-emit
        # time — the op is "there is a traceback visible on screen", not
        # "fix file X". The same is true for operator-initiated /attach
        # uploads: "reason about this PDF" / "look at this screenshot"
        # has no pre-determined target path. The orchestrator infers
        # actionable targets from evidence downstream for both flows.
        # Other envelopes without either signal type still require a
        # non-empty target_files tuple.
        if not self.target_files:
            _has_user_att = bool(
                (self.evidence or {}).get("user_attachments")
            )
            if (
                self.source not in _EMPTY_TARGET_FILES_EXEMPT_SOURCES
                and not _has_user_att
            ):
                raise EnvelopeValidationError(
                    "target_files must be non-empty (exempt: sources "
                    f"{sorted(_EMPTY_TARGET_FILES_EXEMPT_SOURCES)}, "
                    "or evidence.user_attachments present)"
                )
        # F2 Slice 2: validate routing_override. Empty = no override;
        # non-empty must be a known ProviderRoute value. Invalid values
        # fail fast here rather than silently dropping downstream.
        if self.routing_override not in _VALID_ROUTING_OVERRIDES:
            raise EnvelopeValidationError(
                f"routing_override must be one of "
                f"{sorted(_VALID_ROUTING_OVERRIDES)}, "
                f"got {self.routing_override!r}"
            )

    def with_lease(self, lease_id: str) -> "IntentEnvelope":
        """Return a new envelope with the given lease_id set."""
        return IntentEnvelope(
            schema_version=self.schema_version,
            source=self.source,
            description=self.description,
            target_files=self.target_files,
            repo=self.repo,
            confidence=self.confidence,
            urgency=self.urgency,
            dedup_key=self.dedup_key,
            causal_id=self.causal_id,
            signal_id=self.signal_id,
            idempotency_key=self.idempotency_key,
            lease_id=lease_id,
            evidence=self.evidence,
            requires_human_ack=self.requires_human_ack,
            submitted_at=self.submitted_at,
            routing_override=self.routing_override,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "description": self.description,
            "target_files": list(self.target_files),
            "repo": self.repo,
            "confidence": self.confidence,
            "urgency": self.urgency,
            "dedup_key": self.dedup_key,
            "causal_id": self.causal_id,
            "signal_id": self.signal_id,
            "idempotency_key": self.idempotency_key,
            "lease_id": self.lease_id,
            "evidence": dict(self.evidence),
            "requires_human_ack": self.requires_human_ack,
            "submitted_at": self.submitted_at,
            "routing_override": self.routing_override,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IntentEnvelope":
        try:
            return cls(
                schema_version=d["schema_version"],
                source=d["source"],
                description=d["description"],
                target_files=tuple(d["target_files"]),
                repo=d["repo"],
                confidence=float(d["confidence"]),
                urgency=d["urgency"],
                dedup_key=d["dedup_key"],
                causal_id=d["causal_id"],
                signal_id=d["signal_id"],
                idempotency_key=d["idempotency_key"],
                lease_id=d.get("lease_id", ""),
                evidence=dict(d.get("evidence", {})),
                requires_human_ack=bool(d["requires_human_ack"]),
                submitted_at=float(d["submitted_at"]),
                # F2 Slice 2 — additive. Pre-F2 persisted envelopes
                # omit this field entirely; default "" = no override.
                routing_override=d.get("routing_override", ""),
            )
        except KeyError as exc:
            raise EnvelopeValidationError(f"missing required field: {exc}") from exc


def _dedup_key(source: str, target_files: Tuple[str, ...], evidence: Dict[str, Any]) -> str:
    sig = evidence.get("signature", "")
    raw = f"{source}|{'|'.join(sorted(target_files))}|{sig}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def make_envelope(
    *,
    source: str,
    description: str,
    target_files: Tuple[str, ...],
    repo: str,
    confidence: float,
    urgency: str,
    evidence: Dict[str, Any],
    requires_human_ack: bool,
    causal_id: str = "",
    signal_id: str = "",
    routing_override: str = "",
) -> IntentEnvelope:
    """Create a new IntentEnvelope with auto-generated IDs.

    ``routing_override`` (F2 Slice 2): optional per-envelope provider-
    route hint. Default "" = no override (pre-F2 byte-identical). When
    non-empty, must be one of the 5 ProviderRoute values; the envelope
    constructor validates via ``_VALID_ROUTING_OVERRIDES``.
    """
    sid = signal_id or generate_operation_id("sig")
    cid = causal_id or generate_operation_id("cau")
    ikey = generate_operation_id("ikey")
    dk = _dedup_key(source, tuple(target_files), evidence)
    return IntentEnvelope(
        schema_version=SCHEMA_VERSION,
        source=source,
        description=description,
        target_files=tuple(target_files),
        repo=repo,
        confidence=confidence,
        urgency=urgency,
        dedup_key=dk,
        causal_id=cid,
        signal_id=sid,
        idempotency_key=ikey,
        lease_id="",
        evidence=dict(evidence),
        requires_human_ack=requires_human_ack,
        submitted_at=time.monotonic(),
        routing_override=routing_override,
    )
