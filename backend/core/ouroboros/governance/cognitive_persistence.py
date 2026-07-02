"""Bi-directional cognitive persistence — cures the write-only-organ amnesia.

Write side: distills per-op ToolExecutionRecord failures into merged
CognitiveExperience rows persisted through PersistentIntelligenceManager
(StateCategory.LEARNING). Read side: boot hydration + CONTEXT_EXPANSION
injection as 'Prior Ephemeral Knowledge'. Authority-free; fail-soft.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "cogexp.v1"
KEY_PREFIX = "cogexp:"
_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def cognitive_footprint(model_name: str, num_ctx: Optional[int]) -> str:
    """Same shape as local_inference_director.physics_key: model@ctx-bucket."""
    return "%s@%s" % (model_name, num_ctx if num_ctx else "cpu")


def sanitize_token(raw: str, max_len: int = 64) -> str:
    """Model-derived strings entering future prompts: identifier charset only."""
    return _TOKEN_RE.sub("", str(raw or ""))[:max_len]


class ExperienceKind(str, Enum):
    FAILED_TOOL_PATTERN = "failed_tool_pattern"
    HALLUCINATED_TOOL = "hallucinated_tool"
    DEAD_END_EXPLORATION = "dead_end_exploration"
    GENERATION_FAILURE = "generation_failure"


_OP_RING_CAP = 5


@dataclass
class CognitiveExperience:
    kind: ExperienceKind
    footprint: str
    subject: str          # sanitized tool name / file path stem / phase
    error_class: str      # exception class or reason code, sanitized
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    op_ids: List[str] = field(default_factory=list)

    def key(self) -> str:
        digest = hashlib.sha256(
            f"{self.subject}|{self.error_class}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{KEY_PREFIX}{self.footprint}:{self.kind.value}:{digest}"

    def merge_occurrence(self, op_id: str, ts: float) -> None:
        self.count += 1
        if not self.first_seen:
            self.first_seen = ts
        self.last_seen = max(self.last_seen, ts)
        self.op_ids.append(op_id)
        if len(self.op_ids) > _OP_RING_CAP:
            del self.op_ids[: len(self.op_ids) - _OP_RING_CAP]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind.value,
            "footprint": self.footprint,
            "subject": self.subject,
            "error_class": self.error_class,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "op_ids": list(self.op_ids),
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "CognitiveExperience":
        return cls(
            kind=ExperienceKind(payload["kind"]),
            footprint=str(payload["footprint"]),
            subject=sanitize_token(payload.get("subject", "")),
            error_class=sanitize_token(payload.get("error_class", ""), 96),
            count=int(payload.get("count", 0)),
            first_seen=float(payload.get("first_seen", 0.0)),
            last_seen=float(payload.get("last_seen", 0.0)),
            op_ids=[str(o) for o in payload.get("op_ids", [])][-_OP_RING_CAP:],
        )
