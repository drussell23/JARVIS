"""VerdictAuthority -- single source of truth for component/phase status.

Enforces:
- Monotonic severity (can degrade freely, healing requires evidence)
- Epoch-stamped writes (stale epoch rejected)
- Out-of-order rejection (monotonic_ns ordering)
- No raw string overwrites -- all writes go through typed verdicts
"""

from __future__ import annotations

import asyncio
from typing import Dict, Mapping, Optional

from backend.core.root_authority_types import (
    DomainVerdict,
    PhaseVerdict,
    ResourceVerdict,
    SEVERITY_MAP,
)


class VerdictAuthority:
    """Single source of truth for component/phase verdict status.

    All verdict submissions are serialised through an ``asyncio.Lock``
    to guarantee consistent monotonic-severity and epoch-gating checks.

    Reads are lock-free because ``ResourceVerdict`` and ``PhaseVerdict``
    are frozen dataclasses -- the reference swap is atomic in CPython and
    the values are immutable.
    """

    def __init__(self) -> None:
        self._verdicts: Dict[str, ResourceVerdict] = {}
        self._phase_verdicts: Dict[str, PhaseVerdict] = {}
        self._domain_verdicts: Dict[str, DomainVerdict] = {}
        self._current_epoch: int = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Epoch management
    # ------------------------------------------------------------------

    def begin_epoch(self) -> int:
        """Start a new boot epoch.  Stale-epoch verdicts will be rejected.

        P0-5: Synchronises with the unified boot_epoch module so that all
        subsystems share a consistent epoch counter.  Returns the new epoch
        value (also mirrored in boot_epoch.get_epoch()).
        """
        try:
            from backend.core.boot_epoch import advance_epoch
            self._current_epoch = advance_epoch()
        except Exception:
            self._current_epoch += 1
        return self._current_epoch

    @property
    def current_epoch(self) -> int:
        return self._current_epoch

    # ------------------------------------------------------------------
    # Resource verdict submission
    # ------------------------------------------------------------------

    async def submit_verdict(self, name: str, verdict: ResourceVerdict) -> bool:
        """Submit a manager verdict.

        Rejects:
        - Stale epoch (verdict.epoch < current_epoch)
        - Out-of-order (verdict.monotonic_ns < existing.monotonic_ns)
        - Heal without evidence (severity decrease without ``recovery_proof``
          in evidence)

        Returns ``True`` if the verdict was accepted, ``False`` otherwise.
        """
        async with self._lock:
            # Gate 1: stale epoch
            if verdict.epoch < self._current_epoch:
                return False

            existing = self._verdicts.get(name)
            if existing is not None:
                # Gate 2: out-of-order monotonic timestamp
                if existing.monotonic_ns > verdict.monotonic_ns:
                    return False

                # Gate 3: monotonic severity -- healing requires evidence
                existing_severity = SEVERITY_MAP.get(existing.state, 3)
                new_severity = SEVERITY_MAP.get(verdict.state, 3)
                if new_severity < existing_severity:
                    if not verdict.evidence.get("recovery_proof"):
                        return False

            self._verdicts[name] = verdict
            return True

    # ------------------------------------------------------------------
    # Phase verdict submission
    # ------------------------------------------------------------------

    async def submit_phase_verdict(self, verdict: PhaseVerdict) -> bool:
        """Submit an aggregated phase verdict.  Rejects stale epoch."""
        async with self._lock:
            if verdict.epoch < self._current_epoch:
                return False
            self._phase_verdicts[verdict.phase_name] = verdict
            return True

    # ------------------------------------------------------------------
    # Reads (lock-free -- frozen dataclass values)
    # ------------------------------------------------------------------

    def get_component_status(self, name: str) -> Optional[ResourceVerdict]:
        """Read a manager verdict.  Frozen dataclass -- safe without lock."""
        return self._verdicts.get(name)

    def get_phase_status(self, name: str) -> Optional[PhaseVerdict]:
        """Read a phase verdict.  Frozen dataclass -- safe without lock."""
        return self._phase_verdicts.get(name)

    def get_all_verdicts_snapshot(self) -> Mapping[str, ResourceVerdict]:
        """Return consistent point-in-time snapshot.

        Shallow dict copy; values are frozen dataclasses so the snapshot
        is safe to iterate without holding a lock.
        """
        return dict(self._verdicts)

    # ------------------------------------------------------------------
    # P0-3: Cross-repo domain verdict submission and reads
    # ------------------------------------------------------------------

    async def submit_domain_verdict(self, verdict: DomainVerdict) -> bool:
        """Submit a cross-repo state domain verdict.

        Rejects stale-epoch verdicts and out-of-order monotonic timestamps.
        Domain verdicts never enforce monotonic severity (domains may recover
        freely — e.g. routing_target may go DEGRADED then READY again without
        requiring ``recovery_proof``).

        Returns ``True`` if the verdict was accepted.
        """
        async with self._lock:
            if verdict.epoch < self._current_epoch:
                return False
            existing = self._domain_verdicts.get(verdict.domain)
            if existing is not None and existing.monotonic_ns > verdict.monotonic_ns:
                return False
            self._domain_verdicts[verdict.domain] = verdict
            return True

    def get_domain_status(self, domain: str) -> Optional[DomainVerdict]:
        """Read a domain verdict.  Lock-free — frozen dataclass is safe."""
        return self._domain_verdicts.get(domain)

    def get_all_domain_verdicts_snapshot(self) -> Dict[str, DomainVerdict]:
        """Return a consistent point-in-time snapshot of all domain verdicts."""
        return dict(self._domain_verdicts)

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def get_phase_display(self, phase: str) -> Dict[str, str]:
        """Format phase verdict for dashboard/broadcast consumption.

        Replaces the hardcoded ``{"status": "complete"}`` literals that
        previously populated the TUI and memory bus.
        """
        verdict = self._phase_verdicts.get(phase)
        if verdict is None:
            return {"status": "pending"}
        return {
            "status": verdict.state.value,
            "detail": verdict.reason_codes[0].value if verdict.reason_codes else "",
        }
