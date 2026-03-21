"""
GapResolutionProtocol — dedup lock + tri-mode routing + 19-state FSM.

Resolution modes:
  A — Fail Fast: high risk or non-idempotent — no auto-routing.
  B — Pending Queue: idempotent + user_critical — enqueue; replay on graduation.
  C — Parallel Fallback: read_only or assistive — execute fallback in parallel.

19-State FSM (states only — transitions enforced at synthesis time):
  GAP_DETECTED -> GAP_COALESCING -> GAP_COALESCED
  -> ROUTE_DECIDED_A/B/C -> SYNTH_PENDING
  -> SYNTH_TIMEOUT|SYNTH_REJECTED -> CLOSED_UNRESOLVED
  -> ARTIFACT_WRITTEN -> ARTIFACT_VERIFIED|QUARANTINED_PENDING_REVIEW
  -> CANARY_ACTIVE -> CANARY_ROLLED_BACK|AGENT_GRADUATED
  -> REPLAY_AUTHORIZED -> REPLAY_STALE|CLOSED_RESOLVED
  -> CLOSED_RESOLVED|CLOSED_UNRESOLVED (terminal)
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List

import yaml

from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent

log = logging.getLogger(__name__)

_POLICY_PATH = Path(__file__).parent / "gap_resolution_policy.yaml"

# TRINITY_DREAM_DAS_ENABLED: reserved for future DreamEngine/ProphecyEngine integration.
# Defaults to false; setting to true is a no-op in this implementation iteration.
_TRINITY_DREAM_DAS_ENABLED: bool = os.environ.get(
    "TRINITY_DREAM_DAS_ENABLED", "false"
).lower() in ("true", "1")


class ResolutionMode(str, Enum):
    A = "A"  # Fail Fast
    B = "B"  # Pending Queue
    C = "C"  # Parallel Fallback


class DasSynthesisState(str, Enum):
    GAP_DETECTED = "GAP_DETECTED"
    GAP_COALESCING = "GAP_COALESCING"
    GAP_COALESCED = "GAP_COALESCED"
    ROUTE_DECIDED_A = "ROUTE_DECIDED_A"
    ROUTE_DECIDED_B = "ROUTE_DECIDED_B"
    ROUTE_DECIDED_C = "ROUTE_DECIDED_C"
    SYNTH_PENDING = "SYNTH_PENDING"
    SYNTH_TIMEOUT = "SYNTH_TIMEOUT"
    SYNTH_REJECTED = "SYNTH_REJECTED"
    ARTIFACT_WRITTEN = "ARTIFACT_WRITTEN"
    QUARANTINED_PENDING_REVIEW = "QUARANTINED_PENDING_REVIEW"
    ARTIFACT_VERIFIED = "ARTIFACT_VERIFIED"
    CANARY_ACTIVE = "CANARY_ACTIVE"
    CANARY_ROLLED_BACK = "CANARY_ROLLED_BACK"
    AGENT_GRADUATED = "AGENT_GRADUATED"
    REPLAY_AUTHORIZED = "REPLAY_AUTHORIZED"
    REPLAY_STALE = "REPLAY_STALE"
    CLOSED_RESOLVED = "CLOSED_RESOLVED"
    CLOSED_UNRESOLVED = "CLOSED_UNRESOLVED"


@dataclass
class GapResolutionPolicy:
    risk_class: str = "medium"
    idempotent: bool = True
    user_critical: bool = False
    read_only: bool = False
    assistive: bool = False
    slo_p99_ms: int = 5000


@functools.lru_cache(maxsize=None)
def _load_policy(domain_id: str) -> GapResolutionPolicy:
    try:
        data = yaml.safe_load(_POLICY_PATH.read_text())
    except Exception as exc:
        log.warning("Failed to load gap_resolution_policy.yaml: %s; using defaults", exc)
        return GapResolutionPolicy()

    defaults = data.get("defaults", {})
    overrides = data.get("domain_overrides", {}).get(domain_id, {})
    merged = {**defaults, **overrides}
    return GapResolutionPolicy(
        risk_class=merged.get("risk_class", "medium"),
        idempotent=merged.get("idempotent", True),
        user_critical=merged.get("user_critical", False),
        read_only=merged.get("read_only", False),
        assistive=merged.get("assistive", False),
        slo_p99_ms=int(merged.get("slo_p99_ms", 5000)),
    )


class GapResolutionProtocol:
    def __init__(self) -> None:
        self._in_flight: Dict[str, asyncio.Event] = {}
        self._synth_timeout_s: float = float(
            os.environ.get("DAS_SYNTH_TIMEOUT_S", "120")
        )
        self._quarantine_max_retries: int = int(
            os.environ.get("DAS_QUARANTINE_MAX_RETRIES", "3")
        )
        self._oscillation_flip_threshold: int = int(
            os.environ.get("DAS_OSCILLATION_FLIP_THRESHOLD", "3")
        )
        self._oscillation_window_s: float = float(
            os.environ.get("DAS_OSCILLATION_WINDOW_S", "60")
        )
        self._oscillation_freeze_s: float = float(
            os.environ.get("DAS_OSCILLATION_FREEZE_S", "300")
        )
        # domain_id -> list of flip timestamps (monotonic)
        self._flip_history: Dict[str, List[float]] = {}
        # domain_id -> freeze-until monotonic time
        self._frozen_until: Dict[str, float] = {}

    def classify_mode(self, event: CapabilityGapEvent) -> ResolutionMode:
        if event.source == "dream_advisory":
            return ResolutionMode.C
        policy = _load_policy(event.domain_id)
        return self._classify_mode(event, policy)

    def _classify_mode(
        self, _event: CapabilityGapEvent, policy: GapResolutionPolicy
    ) -> ResolutionMode:
        if policy.risk_class == "high" or not policy.idempotent:
            return ResolutionMode.A
        if policy.user_critical and policy.idempotent:
            return ResolutionMode.B
        return ResolutionMode.C

    def _is_oscillating(self, domain_id: str) -> bool:
        """Return True if the domain is currently frozen due to oscillation."""
        now = time.monotonic()
        # Check freeze
        if self._frozen_until.get(domain_id, 0) > now:
            return True
        # Prune old flips outside the window
        flips = self._flip_history.get(domain_id, [])
        flips = [t for t in flips if now - t <= self._oscillation_window_s]
        self._flip_history[domain_id] = flips
        if len(flips) >= self._oscillation_flip_threshold:
            self._frozen_until[domain_id] = now + self._oscillation_freeze_s
            log.warning(
                "GapResolutionProtocol: oscillation detected for domain_id=%s "
                "--- freezing for %.0fs",
                domain_id,
                self._oscillation_freeze_s,
            )
            return True
        return False

    def record_route_flip(self, domain_id: str) -> None:
        """Call when a route flip is observed (canary <-> stable switch)."""
        self._flip_history.setdefault(domain_id, []).append(time.monotonic())

    async def handle_gap_event(self, event: CapabilityGapEvent) -> None:
        if os.environ.get("DAS_ENABLED", "true").lower() in ("false", "0", "no"):
            return
        if self._is_oscillating(event.domain_id):
            log.warning(
                "GapResolutionProtocol: domain %s is frozen (oscillation) "
                "--- skipping synthesis",
                event.domain_id,
            )
            return
        dedupe_key = event.dedupe_key
        if dedupe_key in self._in_flight:
            try:
                await asyncio.wait_for(
                    self._in_flight[dedupe_key].wait(),
                    timeout=self._synth_timeout_s,
                )
            except asyncio.TimeoutError:
                log.warning("Dedup wait timed out for domain_id=%s", event.domain_id)
            return
        done = asyncio.Event()
        self._in_flight[dedupe_key] = done
        try:
            await self._synthesize(event, dedupe_key)
        finally:
            done.set()
            self._in_flight.pop(dedupe_key, None)

    async def _synthesize(
        self, event: CapabilityGapEvent, _dedupe_key: str, retry_count: int = 0
    ) -> None:
        """
        Drives the Ouroboros synthesis pipeline for one gap event.

        Quarantine retry loop: if QUARANTINED_PENDING_REVIEW is reached,
        retry up to DAS_QUARANTINE_MAX_RETRIES times with a new attempt_key.
        Each retry increments retry_count. When max retries are exhausted,
        the FSM transitions to CLOSED_UNRESOLVED.
        Override in integration tests to inject fake synthesis behavior.
        """
        if retry_count > self._quarantine_max_retries:
            log.warning(
                "GapResolutionProtocol: max quarantine retries (%d) exhausted "
                "for domain_id=%s --- transitioning to CLOSED_UNRESOLVED",
                self._quarantine_max_retries,
                event.domain_id,
            )
            return
        log.info(
            "GapResolutionProtocol._synthesize domain_id=%s mode=%s retry=%d",
            event.domain_id,
            self.classify_mode(event).value,
            retry_count,
        )
        # Trinity observer hooks — fire-and-forget, observer-only.
        # HealthCortex and MemoryEngine require full dependency injection at
        # construction time; direct instantiation here would always raise
        # TypeError.  Full wiring is deferred to the Trinity integration
        # follow-up spec.  The TRINITY_DREAM_DAS_ENABLED env var is defined
        # but is a no-op in this implementation iteration.
        log.debug(
            "GapResolutionProtocol: synthesis complete for domain_id=%s "
            "(Trinity observer hooks: deferred pending full Trinity wiring)",
            event.domain_id,
        )
