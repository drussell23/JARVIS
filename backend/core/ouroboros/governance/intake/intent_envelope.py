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
    "web_intelligence",
    # Added 2026-04-18 for VisionSensor (Task 8 of VisionSensor + Visual
    # VERIFY arc). Mirrors ``SignalSource.VISION_SENSOR.value``. See
    # docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md.
    "vision_sensor",
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
    "", "immediate", "standard", "complex", "background", "speculative",
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
            if self.source != "vision_sensor" and not _has_user_att:
                raise EnvelopeValidationError(
                    "target_files must be non-empty "
                    "(exempt: vision_sensor source, or evidence.user_attachments present)"
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
