"""
Intent Signal Data Model
========================

Foundational data types for JARVIS's Intent Engine (Layer 1 of autonomous
self-development).  Every detected anomaly -- test failure, stack trace,
git analysis -- is captured as an :class:`IntentSignal` and de-duplicated
via :class:`DedupTracker` before entering the governance pipeline.

Key design decisions:

* **Frozen dataclass** -- signals are immutable once created, ensuring
  thread-safety and auditability.
* **Deterministic dedup_key** -- SHA-256 hash of (repo + sorted files +
  evidence signature), so identical root causes collapse regardless of
  description wording, confidence, or source channel.
* **Monotonic cooldown** -- :class:`DedupTracker` uses ``time.monotonic()``
  to avoid wall-clock skew issues.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple, TypedDict

from backend.core.ouroboros.governance.operation_id import generate_operation_id


# ---------------------------------------------------------------------------
# SignalSource — canonical source identifiers for IntentSignal envelopes
# ---------------------------------------------------------------------------
#
# StrEnum-style (3.9-compatible via ``str, Enum`` multi-inheritance —
# mirrors the ``SagaStepStatus`` pattern in ``op_context.py``). Existing
# consumers compare ``signal.source`` against string literals; this enum
# gives new typed code (VisionSensor, risk_tier_floor, threat-model
# tests) a discoverable type without forcing migration of every existing
# ``source = "..."`` call site.
#
# Only VISION_SENSOR is pinned here today. Other canonical source names
# (``"test_failure"``, ``"voice_human"``, ``"ai_miner"`` etc. — see the
# comment on ``OperationContext.signal_source``) remain string literals
# at their call sites; migrating them is NOT in scope for Task 3.


class SignalSource(str, Enum):
    """Canonical source identifiers for :class:`IntentSignal` envelopes.

    ``SignalSource.VISION_SENSOR == "vision_sensor"`` is ``True`` (StrEnum
    semantics), so string-based consumers and typed consumers interoperate
    transparently.
    """

    VISION_SENSOR = "vision_sensor"


# ---------------------------------------------------------------------------
# VisionSignalEvidence — schema v1 evidence payload for vision-originated
# signals
# ---------------------------------------------------------------------------
#
# Spec: docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md
#   §Sensor Contract → IntentSignal schema extension
#   §Invariant I1 — every vision-originated op MUST carry this evidence;
#   missing any field = op rejected at CLASSIFY, not silently nulled.


_VISION_SIGNAL_SCHEMA_VERSION = 1
_VISION_FRAME_HASH_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_VISION_VALID_VERDICTS: frozenset = frozenset(
    {"bug_visible", "error_visible", "ok", "unclear"}
)
_VISION_VALID_SEVERITIES: frozenset = frozenset({"info", "warning", "error"})
_VISION_OCR_SNIPPET_MAX_LEN = 256
_VISION_CLASSIFIER_MODEL_MAX_LEN = 128
_VISION_APP_ID_MAX_LEN = 256
_VISION_DETERMINISTIC_MATCHES_MAX = 16  # cap total regex-hit tags per signal


class VisionSignalEvidence(TypedDict):
    """Schema v1 evidence payload carried on the ``"vision_signal"`` key of
    :attr:`IntentSignal.evidence` for any envelope produced by
    ``VisionSensor``.

    Every field is mandatory (I1). ``app_id`` and ``window_id`` are the
    only nullable fields; all others must be populated or validation
    raises.

    See ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
    §Sensor Contract for the semantic definition of each field.
    """

    schema_version: int                      # 1
    frame_hash: str                          # dhash, 16 lowercase hex chars
    frame_ts: float                          # monotonic capture time
    frame_path: str                          # absolute local path
    app_id: Optional[str]                    # macOS bundle id, may be None
    window_id: Optional[int]                 # CGWindow ID, may be None
    classifier_verdict: str                  # bug_visible | error_visible | ok | unclear
    classifier_model: str                    # "deterministic" | "qwen3-vl-235b" | ...
    classifier_confidence: float             # [0.0, 1.0]
    deterministic_matches: Tuple[str, ...]   # regex pattern names that hit
    ocr_snippet: str                         # first 256 chars, sanitized
    severity: str                            # info | warning | error


def validate_vision_signal_evidence(evidence: Any) -> None:
    """Raise ``ValueError`` if *evidence* violates the VisionSignalEvidence
    schema v1.

    Pure function. Called at CLASSIFY intake to enforce I1 and by
    :func:`build_vision_signal_evidence` at emission time.

    Every validation check raises with a specific field reference so the
    caller can attribute rejection precisely.
    """
    if not isinstance(evidence, dict):
        raise ValueError(
            f"vision_signal evidence must be dict; got {type(evidence).__name__}"
        )
    required = {
        "schema_version",
        "frame_hash",
        "frame_ts",
        "frame_path",
        "app_id",
        "window_id",
        "classifier_verdict",
        "classifier_model",
        "classifier_confidence",
        "deterministic_matches",
        "ocr_snippet",
        "severity",
    }
    missing = required - set(evidence.keys())
    if missing:
        raise ValueError(
            f"vision_signal evidence missing required fields: {sorted(missing)}"
        )

    if evidence["schema_version"] != _VISION_SIGNAL_SCHEMA_VERSION:
        raise ValueError(
            f"vision_signal schema_version must be {_VISION_SIGNAL_SCHEMA_VERSION}; "
            f"got {evidence['schema_version']!r}"
        )

    frame_hash = evidence["frame_hash"]
    if not isinstance(frame_hash, str) or not _VISION_FRAME_HASH_PATTERN.match(frame_hash):
        raise ValueError(
            f"vision_signal frame_hash must be 16 lowercase hex chars; "
            f"got {frame_hash!r}"
        )

    frame_ts = evidence["frame_ts"]
    if not isinstance(frame_ts, (int, float)) or isinstance(frame_ts, bool) or frame_ts < 0:
        raise ValueError(
            f"vision_signal frame_ts must be non-negative float; got {frame_ts!r}"
        )

    frame_path = evidence["frame_path"]
    if not isinstance(frame_path, str) or not frame_path:
        raise ValueError("vision_signal frame_path must be non-empty string")
    if not os.path.isabs(frame_path):
        raise ValueError(
            f"vision_signal frame_path must be absolute; got {frame_path!r}"
        )

    app_id = evidence["app_id"]
    if app_id is not None:
        if not isinstance(app_id, str) or not app_id:
            raise ValueError("vision_signal app_id must be None or non-empty string")
        if len(app_id) > _VISION_APP_ID_MAX_LEN:
            raise ValueError(
                f"vision_signal app_id exceeds {_VISION_APP_ID_MAX_LEN} chars"
            )

    window_id = evidence["window_id"]
    if window_id is not None:
        # bool is a subclass of int in Python — reject it explicitly so
        # ``True`` / ``False`` can't sneak into window_id.
        if isinstance(window_id, bool) or not isinstance(window_id, int):
            raise ValueError(
                f"vision_signal window_id must be None or int; got {window_id!r}"
            )
        if window_id < 0:
            raise ValueError(
                f"vision_signal window_id must be non-negative; got {window_id!r}"
            )

    verdict = evidence["classifier_verdict"]
    if verdict not in _VISION_VALID_VERDICTS:
        raise ValueError(
            f"vision_signal classifier_verdict must be one of "
            f"{sorted(_VISION_VALID_VERDICTS)}; got {verdict!r}"
        )

    model = evidence["classifier_model"]
    if not isinstance(model, str) or not model:
        raise ValueError("vision_signal classifier_model must be non-empty string")
    if len(model) > _VISION_CLASSIFIER_MODEL_MAX_LEN:
        raise ValueError(
            f"vision_signal classifier_model exceeds "
            f"{_VISION_CLASSIFIER_MODEL_MAX_LEN} chars"
        )

    confidence = evidence["classifier_confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not (0.0 <= float(confidence) <= 1.0)
    ):
        raise ValueError(
            f"vision_signal classifier_confidence must be in [0.0, 1.0]; "
            f"got {confidence!r}"
        )

    matches = evidence["deterministic_matches"]
    if not isinstance(matches, tuple):
        raise ValueError(
            f"vision_signal deterministic_matches must be tuple; "
            f"got {type(matches).__name__}"
        )
    if len(matches) > _VISION_DETERMINISTIC_MATCHES_MAX:
        raise ValueError(
            f"vision_signal deterministic_matches exceeds "
            f"{_VISION_DETERMINISTIC_MATCHES_MAX} entries"
        )
    for i, m in enumerate(matches):
        if not isinstance(m, str) or not m:
            raise ValueError(
                f"vision_signal deterministic_matches[{i}] must be non-empty "
                f"string; got {m!r}"
            )

    snippet = evidence["ocr_snippet"]
    if not isinstance(snippet, str):
        raise ValueError(
            f"vision_signal ocr_snippet must be string; "
            f"got {type(snippet).__name__}"
        )
    if len(snippet) > _VISION_OCR_SNIPPET_MAX_LEN:
        raise ValueError(
            f"vision_signal ocr_snippet exceeds {_VISION_OCR_SNIPPET_MAX_LEN} chars"
        )

    severity = evidence["severity"]
    if severity not in _VISION_VALID_SEVERITIES:
        raise ValueError(
            f"vision_signal severity must be one of "
            f"{sorted(_VISION_VALID_SEVERITIES)}; got {severity!r}"
        )


def build_vision_signal_evidence(
    *,
    frame_hash: str,
    frame_ts: float,
    frame_path: str,
    classifier_verdict: str,
    classifier_model: str,
    classifier_confidence: float,
    deterministic_matches: Tuple[str, ...] = (),
    ocr_snippet: str = "",
    severity: str = "info",
    app_id: Optional[str] = None,
    window_id: Optional[int] = None,
) -> VisionSignalEvidence:
    """Build + validate a :class:`VisionSignalEvidence` payload in one step.

    Canonical constructor used by ``VisionSensor`` at emission time.
    Stamps ``schema_version=1`` and runs the full validator — callers
    cannot emit a malformed payload through this function.

    Raises
    ------
    ValueError
        If any field fails the schema v1 invariants.
    """
    evidence: VisionSignalEvidence = {
        "schema_version": _VISION_SIGNAL_SCHEMA_VERSION,
        "frame_hash": frame_hash,
        "frame_ts": frame_ts,
        "frame_path": frame_path,
        "app_id": app_id,
        "window_id": window_id,
        "classifier_verdict": classifier_verdict,
        "classifier_model": classifier_model,
        "classifier_confidence": classifier_confidence,
        "deterministic_matches": tuple(deterministic_matches),
        "ocr_snippet": ocr_snippet,
        "severity": severity,
    }
    validate_vision_signal_evidence(evidence)
    return evidence


# ---------------------------------------------------------------------------
# IntentSignal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentSignal:
    """Immutable record of a detected anomaly that may require autonomous action.

    Parameters
    ----------
    source:
        Channel that produced the signal.  One of ``"intent:test_failure"``,
        ``"intent:stack_trace"``, ``"intent:git_analysis"``, etc.
    target_files:
        Tuple of file paths implicated by the signal.
    repo:
        Repository origin (``"jarvis"``, ``"prime"``, ``"reactor-core"``).
    description:
        Human-readable summary of what was detected.
    evidence:
        Arbitrary evidence dict.  **Must** contain a ``"signature"`` key used
        for deduplication (e.g. ``"ValueError:module:42"``).
    confidence:
        Model or heuristic confidence in the signal, 0.0 -- 1.0.
    stable:
        ``True`` when the signal has met stability criteria (e.g. reproduced
        across multiple runs).
    signal_id:
        Auto-generated unique identifier via :func:`generate_operation_id`.
    timestamp:
        Auto-generated UTC creation time.
    """

    source: str
    target_files: Tuple[str, ...]
    repo: str
    description: str
    evidence: Dict[str, Any]
    confidence: float
    stable: bool

    # Auto-generated fields
    signal_id: str = field(default_factory=lambda: generate_operation_id("sig"))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    @property
    def dedup_key(self) -> str:
        """Deterministic dedup key: SHA-256 of (repo + sorted files + signature).

        Truncated to 16 hex characters.  Two signals with the same repo,
        target files, and evidence signature will always produce the same key,
        regardless of description, confidence, source channel, or timestamps.
        """
        sorted_files = tuple(sorted(self.target_files))
        signature = self.evidence.get("signature", "")
        raw = f"{self.repo}|{'|'.join(sorted_files)}|{signature}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return digest[:16]


# ---------------------------------------------------------------------------
# DedupTracker
# ---------------------------------------------------------------------------


class DedupTracker:
    """Tracks recently seen signal dedup keys to suppress duplicates.

    Uses ``time.monotonic()`` internally so cooldown is immune to
    wall-clock adjustments.

    Parameters
    ----------
    cooldown_s:
        Minimum seconds between accepting two signals with the same
        ``dedup_key``.  Default 300 s (5 minutes).
    """

    def __init__(self, cooldown_s: float = 300.0) -> None:
        self._cooldown_s = cooldown_s
        self._seen: Dict[str, float] = {}  # dedup_key -> monotonic timestamp

    def is_new(self, signal: IntentSignal) -> bool:
        """Return ``True`` if *signal* has not been seen within the cooldown.

        If ``True`` is returned the signal's dedup key is recorded with the
        current monotonic time (i.e. calling ``is_new`` both checks **and**
        registers the signal).
        """
        key = signal.dedup_key
        now = time.monotonic()
        last_seen = self._seen.get(key)

        if last_seen is not None and (now - last_seen) < self._cooldown_s:
            return False

        self._seen[key] = now
        return True

    def clear(self) -> None:
        """Reset all tracked dedup keys."""
        self._seen.clear()
