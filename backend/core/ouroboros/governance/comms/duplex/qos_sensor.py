"""QoS Sensor — Daniel evaluates his OWN interaction quality.

The first of the two closing gaps (operator authorization 2026-07-19):
JARVIS (Daniel) watches where he STRUGGLES to assist the user, and
hands the breakdown to O+V (Karen) as a fix signal — the loop that
makes "O+V improves JARVIS from real usage" literally true.

Mandate 1 — no explicit "you failed" feedback. Frustration is
inferred from IMPLICIT operational deviations:

  * **Rapid prompt repetition** — the user re-sends a
    semantically-equivalent command within
    ``JARVIS_QOS_REPEAT_WINDOW_S`` (30s). Re-asking = the first answer
    missed.
  * **Operational override** — a ``SIGINT`` mid-response, or the user
    seizing an active terminal/audio lease. Interrupting = the
    trajectory was wrong.

On trigger, the preceding context window is bundled into a
``UX_DEGRADATION_EVENT`` and routed — DRY, mandate 3 — through the
EXISTING ``make_envelope(source="performance_regression", …)`` intake
path, so Karen diagnoses a conversational breakdown with the EXACT
logic she uses for a codebase test failure.

Bounded + deduped (a frustration storm is ONE signal); NEVER raises.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.QoSSensor")

UX_DEGRADATION_EVENT = "UX_DEGRADATION_EVENT"


def _repeat_window_s() -> float:
    try:
        return max(3.0, min(300.0, float(os.environ.get(
            "JARVIS_QOS_REPEAT_WINDOW_S", "30",
        ))))
    except (TypeError, ValueError):
        return 30.0


def _cooldown_s() -> float:
    try:
        return max(10.0, float(os.environ.get(
            "JARVIS_QOS_COOLDOWN_S", "120",
        )))
    except (TypeError, ValueError):
        return 120.0


def _semantic_key(command: str) -> str:
    """Semantic-equivalence key: lowercased, punctuation-stripped,
    stop-word-light token set. "fix the bug" ≈ "fix the bug please" ≈
    "can you fix the bug". NEVER raises."""
    try:
        toks = re.findall(r"[a-z0-9]+", str(command or "").lower())
        stop = {"the", "a", "an", "please", "can", "you", "could", "would",
                "to", "for", "me", "my", "i", "it", "this", "that", "do"}
        core = sorted(t for t in toks if t not in stop and len(t) > 1)
        return hashlib.sha256(" ".join(core).encode()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


class QoSSensor:
    """Daniel's self-evaluation. ``emit_signal(envelope)`` is the
    injected intake sink (production: the unified intake router).
    ``context_provider`` returns the recent dialogue for the bundle."""

    def __init__(
        self,
        *,
        emit_signal: Optional[Callable[[Any], None]] = None,
        context_provider: Optional[Callable[[], List[str]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit_signal or (lambda _e: None)
        self._context = context_provider or (lambda: [])
        self._clock = clock
        self._recent: Deque[Tuple[str, float]] = deque(maxlen=32)
        self._last_emit = 0.0
        self.stats: Dict[str, int] = {
            "commands": 0, "repeat_triggers": 0, "override_triggers": 0,
            "emitted": 0, "cooldown_suppressed": 0,
        }

    # ---- the two implicit signals ----

    def observe_command(self, command: str) -> bool:
        """One user command in. Detects rapid semantic repetition.
        Returns True iff a UX_DEGRADATION_EVENT was emitted. NEVER
        raises."""
        try:
            self.stats["commands"] += 1
            key = _semantic_key(command)
            now = self._clock()
            window = _repeat_window_s()
            repeated = any(
                k == key and (now - t) <= window
                for k, t in self._recent
            )
            self._recent.append((key, now))
            if repeated and key:
                self.stats["repeat_triggers"] += 1
                return self._degrade(
                    "rapid_repeat",
                    f"User re-sent a semantically-equivalent command "
                    f"within {int(window)}s — the prior response missed: "
                    f"{str(command)[:120]}",
                )
            return False
        except Exception:  # noqa: BLE001
            return False

    def observe_override(self, kind: str = "sigint") -> bool:
        """An operational override (SIGINT mid-response / lease seizure).
        Returns True iff emitted. NEVER raises."""
        try:
            self.stats["override_triggers"] += 1
            return self._degrade(
                f"override_{kind}",
                f"User issued an operational override ({kind}) mid-"
                f"assistance — the trajectory was wrong.",
            )
        except Exception:  # noqa: BLE001
            return False

    def _degrade(self, cause: str, summary: str) -> bool:
        """Bundle context → UX_DEGRADATION_EVENT → the EXISTING perf-
        regression intake path (mandate 3). Cooldown-guarded so a
        frustration storm is ONE signal. NEVER raises."""
        now = self._clock()
        if (now - self._last_emit) < _cooldown_s():
            self.stats["cooldown_suppressed"] += 1
            return False
        self._last_emit = now
        try:
            context = list(self._context() or [])[-8:]
            from backend.core.ouroboros.governance.intake.intent_envelope import (  # noqa: E501,PLC0415
                make_envelope,
            )
            # DRY: Karen processes a conversational breakdown as a
            # performance_regression — same diagnostic logic as a
            # codebase test failure.
            envelope = make_envelope(
                source="performance_regression",
                description=f"[{UX_DEGRADATION_EVENT}:{cause}] {summary}",
                target_files=(
                    "backend/core/ouroboros/governance/orchestrator.py",
                ),
                repo=os.environ.get("JARVIS_REPO", "."),
                confidence=0.75,
                urgency="normal",
                evidence={
                    "ux_degradation": True,
                    "cause": cause,
                    "dialogue_context": context,
                    "detector": "qos_sensor",
                },
                # A conversational breakdown is an autonomous fix
                # signal — no human ack gate on the SIGNAL (the fix it
                # produces still passes the full Iron Gate downstream).
                requires_human_ack=False,
            )
            self._emit(envelope)
            # DRY: the SAME envelope feeds the preference ledger's
            # attenuation engine — no second telemetry wrapper.
            try:
                from .preference_ledger import get_default_ledger  # noqa: PLC0415
                get_default_ledger().record_frustration(envelope)
            except Exception:  # noqa: BLE001
                pass
            self.stats["emitted"] += 1
            logger.info("[QoS] %s → UX_DEGRADATION_EVENT emitted", cause)
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[QoS] emit degraded", exc_info=True)
            return False


def qos_enabled() -> bool:
    """Master gate — default OFF (§33.1: a surface that emits
    autonomous fix signals from user behavior graduates). NEVER
    raises."""
    return os.environ.get(
        "JARVIS_QOS_SENSOR_ENABLED", "",
    ).strip().lower() in ("1", "true", "yes", "on")


def build_live_qos_sensor(
    *,
    emit_signal: Optional[Callable[[Any], None]] = None,
    context_provider: Optional[Callable[[], List[str]]] = None,
) -> Optional["QoSSensor"]:
    """Mount a QoS sensor for a live input surface, or ``None`` when
    the master gate is down (nothing instantiated). Production
    ``emit_signal`` routes into the unified intake router; the context
    provider pulls recent dialogue from ConversationBridge. NEVER
    raises."""
    try:
        if not qos_enabled():
            return None

        def _default_context() -> List[str]:
            try:
                from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501,PLC0415
                    get_default_bridge,
                )
                snap = get_default_bridge().snapshot()
                turns = getattr(snap, "turns", snap) if snap else []
                return [
                    f"{getattr(t, 'role', '?')}: {getattr(t, 'text', str(t))[:120]}"
                    for t in list(turns)[-8:]
                ]
            except Exception:  # noqa: BLE001
                return []

        return QoSSensor(
            emit_signal=emit_signal,
            context_provider=context_provider or _default_context,
        )
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "QoSSensor",
    "UX_DEGRADATION_EVENT",
    "build_live_qos_sensor",
    "qos_enabled",
]
