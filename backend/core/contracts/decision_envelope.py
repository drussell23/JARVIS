"""DecisionEnvelope — typed wrapper for autonomous reasoning outputs."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class DecisionType(str, Enum):
    EXTRACTION = "extraction"
    SCORING = "scoring"
    POLICY = "policy"
    ACTION = "action"


class DecisionSource(str, Enum):
    JPRIME_V1 = "jprime_v1"
    JPRIME_DEGRADED = "jprime_degraded_fallback"
    HEURISTIC = "heuristic"
    CLOUD_CLAUDE = "cloud_claude"
    LOCAL_PRIME = "local_prime"
    ADAPTIVE = "adaptive"


class OriginComponent(str, Enum):
    EMAIL_TRIAGE_RUNNER = "email_triage.runner"
    EMAIL_TRIAGE_EXTRACTION = "email_triage.extraction"
    EMAIL_TRIAGE_SCORING = "email_triage.scoring"
    EMAIL_TRIAGE_POLICY = "email_triage.policy"
    EMAIL_TRIAGE_LABELER = "email_triage.labels"
    EMAIL_TRIAGE_NOTIFIER = "email_triage.notifications"


@dataclass(frozen=True)
class DecisionEnvelope:
    envelope_id: str
    trace_id: str
    parent_envelope_id: Optional[str]
    decision_type: DecisionType
    source: DecisionSource
    origin_component: OriginComponent
    payload: Dict[str, Any]
    confidence: float
    created_at_epoch: float
    created_at_monotonic: float
    causal_seq: int
    config_version: str
    schema_version: int = 1
    producer_version: str = "1.0.0"
    compat_min_version: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IdempotencyKey:
    key: str

    @classmethod
    def build(cls, decision_type: DecisionType, target_id: str,
              action: str, config_version: str) -> IdempotencyKey:
        raw = f"{decision_type.value}:{target_id}:{action}:{config_version}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return cls(key=digest)


class EnvelopeFactory:
    def __init__(self, clock=None):
        self._clock = clock

    def create(self, trace_id: str, decision_type: DecisionType,
               source: DecisionSource, origin_component: OriginComponent,
               payload: Dict[str, Any], confidence: float, config_version: str,
               parent_envelope_id: Optional[str] = None,
               metadata: Optional[Dict[str, Any]] = None) -> DecisionEnvelope:
        seq = self._clock.tick() if self._clock is not None else 0
        return DecisionEnvelope(
            envelope_id=str(uuid.uuid4()), trace_id=trace_id,
            parent_envelope_id=parent_envelope_id,
            decision_type=decision_type, source=source,
            origin_component=origin_component, payload=payload,
            confidence=confidence, created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(), causal_seq=seq,
            config_version=config_version, metadata=metadata or {},
        )
