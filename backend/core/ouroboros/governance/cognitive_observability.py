"""Slice 109 — God-Tier Observability Matrix & Voice Ignition.

A *decoupled telemetry router*. It subscribes to the Slice-101 cognitive bus
lifecycle stream (``ouroboros.lifecycle.*``) and fans every event out to two
independent sinks:

  1. **The SSE broker** (``publish_task_event``) as a structured "Why-Snapshot"
     JSON payload — NOT a flat text string. The snapshot carries the decision
     context *at the moment of the decision*: the ``confidence_aura`` band, the
     ``shannon_entropy`` of the domain distribution, the
     ``decision_prior_distribution`` (belief-verdict histogram),
     ``recursion_depth``, and any ``rehearsal_verdict``. The TUI renders this as
     a time-travel decision view ("why did O+V believe what it believed?").

  2. **Karen's voice** (``DaemonNarrator``) for HIGH-severity events only —
     containment breach, graduation-threshold-met, load-shedding-active, and
     post-failure. The voice is gated by ``JARVIS_KAREN_VOICE_ENABLED`` AND the
     live mute state (read from ``KarenConfig``). Mute is enforced *upstream*:
     no TTS is ever queued while muted or while the master flag is off.

Design invariants
-----------------
* **Never on the hot path.** Every read surface (entropy / belief / recursion /
  confidence) is best-effort and individually wrapped — a failing substrate
  degrades the snapshot to a partial dict, never an exception into the bus. The
  bus wrapper double-guards on top of this.
* **Authority-free.** This module only *observes*. It never gates, flips, or
  mutates anything. The SSE publish no-ops when the stream is disabled; the
  voice no-ops when muted.
* **Compose, don't duplicate.** It reuses the existing cognitive bus
  (``cognitive_bus``), SSE broker (``ide_observability_stream``), domain entropy
  engine, belief-revision ledger, recursion tracker, confidence aura tiers, and
  the ``DaemonNarrator`` template surface.

Masters
-------
* ``JARVIS_COGNITIVE_OBSERVABILITY_ENABLED`` — the SSE Why-Snapshot projection.
  Default **TRUE** (read-only projection, §7 "absolute observability"; the SSE
  layer itself remains gated by ``JARVIS_IDE_STREAM_ENABLED``).
* ``JARVIS_KAREN_VOICE_ENABLED`` — the autonomous cognitive narration channel.
  Default **FALSE** (§33.1; audio is opt-in and never a primary interface).
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("ouroboros.cognitive_observability")

WHY_SNAPSHOT_SCHEMA_VERSION = "cognitive_why_snapshot.v1"

_TRUTHY = ("1", "true", "yes", "on")

_ENV_OBSERVABILITY = "JARVIS_COGNITIVE_OBSERVABILITY_ENABLED"
_ENV_VOICE = "JARVIS_KAREN_VOICE_ENABLED"
_ENV_LEDGER_SIZE = "JARVIS_WHY_SNAPSHOT_LEDGER_SIZE"

# High-severity narration → maps a (kind, payload) to a DaemonNarrator event
# type. Only these speak; everything else is SSE-only.
_NARRATOR_CONTAINMENT = "cognitive.containment_breach"
_NARRATOR_GRADUATION = "cognitive.graduation_threshold_met"
_NARRATOR_LOADSHED = "cognitive.load_shedding_active"
_NARRATOR_FAILURE = "cognitive.post_failure"
_NARRATOR_APPLY = "cognitive.post_apply"


# ===========================================================================
# Masters
# ===========================================================================


def _env_truthy(name: str, *, default: bool) -> bool:
    """Read a §33.1-style boolean env flag. NEVER raises."""
    try:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return default


def cognitive_observability_enabled() -> bool:
    """SSE Why-Snapshot projection master — default TRUE (read-only)."""
    return _env_truthy(_ENV_OBSERVABILITY, default=True)


def cognitive_voice_enabled() -> bool:
    """Autonomous cognitive narration master — §33.1 default FALSE."""
    return _env_truthy(_ENV_VOICE, default=False)


# ===========================================================================
# Best-effort read surfaces (compose existing substrates)
# ===========================================================================


def _shannon_entropy() -> Optional[float]:
    """Normalized Shannon entropy of the live domain distribution, in [0, 1].
    ``None`` when the entropy engine is off or unavailable. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.domain_entropy_engine import (
            compute_domain_entropy,
            domain_entropy_engine_enabled,
        )
        if not domain_entropy_engine_enabled():
            return None
        report = compute_domain_entropy()
        val = getattr(report, "normalized_entropy", None)
        return round(float(val), 4) if val is not None else None
    except Exception:  # noqa: BLE001
        return None


def _recursion_depth() -> Optional[int]:
    """Current self-modification recursion depth from the process tracker.
    ``None`` when the gate substrate is unavailable. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.recursion_depth_gate import (
            get_tracker,
        )
        return int(get_tracker().current_depth)
    except Exception:  # noqa: BLE001
        return None


def _decision_prior_distribution() -> Dict[str, int]:
    """The belief-verdict histogram over recent beliefs — the "prior
    distribution" the TUI renders. Empty dict when the belief substrate is off
    or empty. NEVER raises."""
    dist: Dict[str, int] = {}
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (
            evaluate_recent_beliefs,
            master_enabled,
        )
        if not master_enabled():
            return {}
        for report in evaluate_recent_beliefs():
            verdict = getattr(report, "verdict", None)
            key = getattr(verdict, "value", None) or str(verdict or "unknown")
            dist[key] = dist.get(key, 0) + 1
    except Exception:  # noqa: BLE001
        return {}
    return dist


def _confidence_band(score: Optional[float]) -> Optional[str]:
    """Map an op-level confidence score in [0, 1] to a coarse band label
    (``high`` / ``medium`` / ``low``). ``None`` when no score is present.

    Note: we deliberately do NOT reuse ``confidence_aura._tier_for_margin`` —
    that classifies per-token logprob *margins* (a different semantic domain),
    so feeding it an op confidence mislabels it. The op-level band below is the
    honest mapping. NEVER raises."""
    if score is None:
        return None
    try:
        s = float(score)
    except Exception:  # noqa: BLE001
        return None
    if s >= 0.8:
        return "high"
    if s >= 0.5:
        return "medium"
    return "low"


def _coerce_confidence(payload: Mapping[str, Any]) -> Optional[float]:
    for key in ("confidence", "confidence_score", "aura_score"):
        if key in payload and payload[key] is not None:
            try:
                return float(payload[key])
            except Exception:  # noqa: BLE001
                continue
    return None


# ===========================================================================
# The Why-Snapshot
# ===========================================================================


def build_why_snapshot(
    *,
    kind: str,
    op_id: str,
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    """Compose the structured Why-Snapshot for a lifecycle event. PURE w.r.t.
    its inputs except for the best-effort live reads (entropy / belief /
    recursion), each individually guarded. NEVER raises.

    The returned dict is the SSE payload the TUI renders — schema
    ``cognitive_why_snapshot.v1``."""
    confidence = _coerce_confidence(payload)
    why: Dict[str, Any] = {
        "confidence_aura": _confidence_band(confidence),
        "confidence_score": confidence,
        "shannon_entropy": _shannon_entropy(),
        "decision_prior_distribution": _decision_prior_distribution(),
        "recursion_depth": _recursion_depth(),
        "rehearsal_verdict": payload.get("rehearsal_verdict"),
    }
    snapshot: Dict[str, Any] = {
        "schema_version": WHY_SNAPSHOT_SCHEMA_VERSION,
        "op_id": str(op_id or ""),
        "kind": str(kind or ""),
        "phase": str(payload.get("phase") or ""),
        "state": str(payload.get("state") or ""),
        "reason": str(payload.get("reason") or "")[:500],
        "target_files": [str(f) for f in (payload.get("target_files") or ()) if f][:24],
        "risk_tier": str(payload.get("risk_tier") or ""),
        "why": why,
    }
    return snapshot


# ===========================================================================
# Why-Snapshot ledger (time-travel debugging surface for the TUI)
# ===========================================================================


def _ledger_size() -> int:
    try:
        return max(1, int(os.environ.get(_ENV_LEDGER_SIZE, "50")))
    except Exception:  # noqa: BLE001
        return 50


class _WhySnapshotLedger:
    """Bounded in-process ring of the most recent Why-Snapshots, keyed by
    op_id, so the TUI can fetch the decision context for any recent op. Pure
    in-memory; nothing is persisted (the durable record is the SSE stream +
    belief ledger). NEVER raises."""

    __slots__ = ("_by_op", "_max")

    def __init__(self, maxsize: int) -> None:
        self._by_op: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._max = max(1, int(maxsize))

    def record(self, snapshot: Mapping[str, Any]) -> None:
        try:
            op_id = str(snapshot.get("op_id") or "")
            if not op_id:
                return
            if op_id in self._by_op:
                self._by_op.move_to_end(op_id)
            self._by_op[op_id] = dict(snapshot)
            while len(self._by_op) > self._max:
                self._by_op.popitem(last=False)
        except Exception:  # noqa: BLE001
            return

    def for_op(self, op_id: str) -> Optional[Dict[str, Any]]:
        try:
            snap = self._by_op.get(str(op_id or ""))
            return dict(snap) if snap is not None else None
        except Exception:  # noqa: BLE001
            return None

    def recent(self, n: int) -> List[Dict[str, Any]]:
        try:
            items = list(self._by_op.values())[-max(0, int(n)):]
            return [dict(s) for s in items]
        except Exception:  # noqa: BLE001
            return []


_LEDGER = _WhySnapshotLedger(_ledger_size())


def why_snapshot_for_op(op_id: str) -> Optional[Dict[str, Any]]:
    """TUI reader: the Why-Snapshot recorded for *op_id*, or ``None``."""
    return _LEDGER.for_op(op_id)


def recent_why_snapshots(n: int = 10) -> List[Dict[str, Any]]:
    """TUI reader: the *n* most recent Why-Snapshots (oldest → newest)."""
    return _LEDGER.recent(n)


# ===========================================================================
# Sink 1 — SSE Why-Snapshot publish
# ===========================================================================


def publish_why_snapshot(
    *,
    kind: str,
    op_id: str,
    payload: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build a Why-Snapshot, record it to the time-travel ledger, and publish it
    to the SSE broker. Returns the snapshot (for tests/telemetry), or ``None``
    when the observability master is off. NEVER raises."""
    if not cognitive_observability_enabled():
        return None
    try:
        snapshot = build_why_snapshot(kind=kind, op_id=op_id, payload=payload)
    except Exception:  # noqa: BLE001
        return None
    _LEDGER.record(snapshot)
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_COGNITIVE_WHY_SNAPSHOT,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_COGNITIVE_WHY_SNAPSHOT,
            str(op_id or "system::cognitive"),
            snapshot,
        )
    except Exception:  # noqa: BLE001 — SSE is best-effort; ledger already holds it
        logger.debug("[CognitiveObs] SSE publish swallowed", exc_info=True)
    return snapshot


# ===========================================================================
# Sink 2 — Karen's voice (HIGH-severity only, gated + mute-respecting)
# ===========================================================================


def _voice_unmuted() -> bool:
    """Read the live Karen mute state from ``KarenConfig`` (env-backed). True
    only when the master narrator switch is on AND the tool-voice sub-switch is
    on. FAIL-CLOSED: if the config cannot be read, we treat Karen as muted (no
    speech) — silence is the safe default. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.comms.karen_voice import (
            KarenConfig,
        )
        cfg = KarenConfig()
        return bool(getattr(cfg, "master_enabled", False)) and bool(
            getattr(cfg, "enabled", False)
        )
    except Exception:  # noqa: BLE001
        return False


def classify_severity(kind: str, payload: Mapping[str, Any]) -> Tuple[Optional[str], str]:
    """Map a lifecycle (kind, payload) to a (narrator_event_type, severity).

    Severity is one of ``"high"`` / ``"normal"`` / ``"low"``. Only ``"high"``
    events are ever spoken. PURE. NEVER raises."""
    try:
        # Explicit high-severity cognitive signals dominate, regardless of kind.
        if payload.get("containment_breach"):
            return _NARRATOR_CONTAINMENT, "high"
        if payload.get("graduation_threshold_met"):
            return _NARRATOR_GRADUATION, "high"
        if payload.get("load_shedding_active"):
            return _NARRATOR_LOADSHED, "high"

        reason = str(payload.get("reason") or "").lower()
        if "containment" in reason or "breach" in reason:
            return _NARRATOR_CONTAINMENT, "high"

        if kind == "post_failure":
            return _NARRATOR_FAILURE, "high"
        if kind == "post_apply":
            return _NARRATOR_APPLY, "normal"
    except Exception:  # noqa: BLE001
        return None, "low"
    return None, "low"


# Injectable narrator seam (tests override; production resolves lazily).
_NARRATOR: Any = None


def set_narrator_for_test(narrator: Any) -> None:
    """Inject a DaemonNarrator (or test double) for deterministic mock tests."""
    global _NARRATOR
    _NARRATOR = narrator


async def _safe_say_via_karen(message: str, **_kw: Any) -> bool:
    """Default say_fn for the narrator: hand the message to Karen's voice
    channel (``KarenPreambleVoice.speak``), which resolves ``safe_say`` and
    drives macOS ``say`` on a real audio device. NEVER raises; returns False on
    any failure so the narrator records a (harmless) miss."""
    try:
        from backend.core.ouroboros.governance.comms.karen_voice import (
            KarenConfig,
            KarenPreambleVoice,
        )
        voice = KarenPreambleVoice(config=KarenConfig())
        voice.speak(message)
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_narrator() -> Any:
    """Lazily construct the module DaemonNarrator wired to Karen's voice.
    ``enabled`` is True so the narrator's own template/rate machinery runs; the
    AUTHORITATIVE gate (master flag + mute) is enforced before we ever call it.
    NEVER raises (returns None on failure → narration silently skipped)."""
    global _NARRATOR
    if _NARRATOR is not None:
        return _NARRATOR
    try:
        from backend.core.ouroboros.daemon_narrator import DaemonNarrator

        _NARRATOR = DaemonNarrator(
            say_fn=_safe_say_via_karen,
            rate_limit_s=float(os.environ.get("JARVIS_KAREN_COGNITIVE_RATE_S", "30") or 30),
            enabled=True,
            voice=os.environ.get("JARVIS_KAREN_VOICE", "Karen"),
        )
    except Exception:  # noqa: BLE001
        _NARRATOR = None
    return _NARRATOR


async def narrate_event(
    *,
    kind: str,
    op_id: str,
    payload: Mapping[str, Any],
) -> bool:
    """Speak a HIGH-severity cognitive event through Karen, IFF the voice master
    is on AND Karen is unmuted. Returns True iff speech was queued. NEVER raises.

    The gate ordering is load-bearing: master flag → mute state → severity. No
    TTS is queued unless all three pass."""
    # 1. Master flag.
    if not cognitive_voice_enabled():
        return False
    # 2. Live mute state (fail-closed).
    if not _voice_unmuted():
        return False
    # 3. Severity — only high-severity events speak.
    event_type, severity = classify_severity(kind, payload)
    if severity != "high" or event_type is None:
        return False

    narrator = _get_narrator()
    if narrator is None:
        return False

    speak_payload: Dict[str, Any] = {
        "op_id": str(op_id or "an operation"),
        "phase": str(payload.get("phase") or "an unknown phase"),
        "flag": str(payload.get("flag") or payload.get("graduation_flag") or "a capability"),
        "confidence": _confidence_band(_coerce_confidence(payload)) or "unknown confidence",
    }
    try:
        await narrator.on_event(event_type, speak_payload)
        return True
    except Exception:  # noqa: BLE001 — narration is best-effort
        logger.debug("[CognitiveObs] narrate_event swallowed", exc_info=True)
        return False


# ===========================================================================
# Bus subscribers + boot registration
# ===========================================================================


def _unpack(event: Any) -> Tuple[str, str, Mapping[str, Any]]:
    """Extract (kind, op_id, payload) from a TrinityEventBus event. Mirrors the
    ``cognitive_subscribers._event_payload`` contract. NEVER raises."""
    try:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            return "", "", {}
        kind = str(payload.get("lifecycle_kind") or "")
        op_id = str(payload.get("op_id") or "")
        return kind, op_id, payload
    except Exception:  # noqa: BLE001
        return "", "", {}


async def _on_lifecycle_observability(event: Any) -> None:
    """Bus subscriber → Sink 1 (SSE Why-Snapshot). NEVER raises."""
    kind, op_id, payload = _unpack(event)
    if not kind:
        return
    publish_why_snapshot(kind=kind, op_id=op_id, payload=payload)


async def _on_lifecycle_voice(event: Any) -> None:
    """Bus subscriber → Sink 2 (Karen narration). NEVER raises."""
    kind, op_id, payload = _unpack(event)
    if not kind:
        return
    await narrate_event(kind=kind, op_id=op_id, payload=payload)


def build_default_observability_subscribers() -> List[Any]:
    """The two Slice-109 cognitive subscribers (SSE + voice), bound to the
    lifecycle pattern. Returns ``CognitiveSubscriber`` instances. NEVER raises;
    returns ``[]`` if the cognitive-bus module is unavailable."""
    try:
        from backend.core.ouroboros.governance.cognitive_bus import (
            CognitiveSubscriber,
            lifecycle_pattern,
        )
    except Exception:  # noqa: BLE001
        return []
    pattern = lifecycle_pattern()
    return [
        CognitiveSubscriber(
            "cognitive_observability_sse", pattern, _on_lifecycle_observability
        ),
        CognitiveSubscriber(
            "cognitive_observability_voice", pattern, _on_lifecycle_voice
        ),
    ]


async def register_observability(*, bus: Any = None) -> List[str]:
    """Boot entry: subscribe the Slice-109 observability + voice handlers to the
    cognitive bus. Composes ``cognitive_bus.register_cognitive_subscribers`` so
    it inherits the same master-gate + fault isolation. Inert (returns ``[]``)
    when the cognitive bus is off. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cognitive_bus import (
            register_cognitive_subscribers,
        )
    except Exception:  # noqa: BLE001
        return []
    subs = build_default_observability_subscribers()
    if not subs:
        return []
    try:
        return await register_cognitive_subscribers(subs, bus=bus)
    except Exception:  # noqa: BLE001
        return []
