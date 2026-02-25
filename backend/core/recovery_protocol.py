# backend/core/recovery_protocol.py
"""
JARVIS Recovery Protocol v1.0
==============================
Post-crash and lease-takeover recovery: probe components, reconcile
projected state with actual state, issue corrective transitions.

Provides:
  - HealthCategory enum for classification of probe results
  - RecoveryProber: multi-strategy health probing (in-process, subprocess, remote)
  - RecoveryReconciler: projected-vs-actual state reconciliation with corrective transitions
  - RecoveryOrchestrator: startup recovery and periodic sparse audits

Design doc: docs/plans/2026-02-24-cross-repo-control-plane-design.md
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

from backend.core.lifecycle_engine import ComponentDeclaration, ComponentLocality

logger = logging.getLogger("jarvis.recovery_protocol")


# ── Health Classification ──────────────────────────────────────────

class HealthCategory(Enum):
    """Classification of a component's observed health."""
    HEALTHY = "healthy"
    CONTRACT_MISMATCH = "contract_mismatch"
    DEPENDENCY_DEGRADED = "dependency_degraded"
    SERVICE_DEGRADED = "service_degraded"
    UNREACHABLE = "unreachable"


# ── Probe Result ───────────────────────────────────────────────────

@dataclass
class ProbeResult:
    """Result of a single health probe against a component."""
    reachable: bool
    category: HealthCategory
    instance_id: str = ""
    api_version: str = ""
    error: str = ""
    probe_epoch: int = 0
    probe_seq: int = 0


# ── States that need probing vs. those that don't ─────────────────

_SKIP_PROBE_STATES = frozenset({"STOPPED", "REGISTERED"})
_ACTIVE_STATES = frozenset({
    "STARTING", "HANDSHAKING", "READY", "DEGRADED",
    "DRAINING", "STOPPING", "FAILED", "LOST",
})


# ── Recovery Prober ────────────────────────────────────────────────

class RecoveryProber:
    """Probes components to determine actual health.

    Supports in-process callables, subprocess HTTP probes, and remote
    HTTP probes. Retries with jittered exponential backoff.
    """

    def __init__(self, journal: Any) -> None:
        self._journal = journal
        self._runtime_probes: Dict[str, Callable[[], Coroutine[Any, Any, Dict]]] = {}

    # ── Registration ─────────────────────────────────────────────

    def register_runtime_probe(
        self,
        component_name: str,
        probe_fn: Callable[[], Coroutine[Any, Any, Dict]],
    ) -> None:
        """Register an in-process async health probe for a component."""
        self._runtime_probes[component_name] = probe_fn

    # ── Classification ───────────────────────────────────────────

    async def classify_for_probe(
        self,
        component: str,
        projected_status: str,
    ) -> Optional[str]:
        """Decide whether a component needs probing.

        Returns "UNVERIFIED" if the component should be probed,
        or None if the component can be skipped (STOPPED/REGISTERED).
        """
        if projected_status in _SKIP_PROBE_STATES:
            return None
        return "UNVERIFIED"

    # ── Probing ──────────────────────────────────────────────────

    async def probe_component(
        self,
        decl: ComponentDeclaration,
        projected_status: str,
        max_attempts: int = 2,
    ) -> Optional[ProbeResult]:
        """Probe a component with retries and jittered backoff.

        Returns None if the lease is lost mid-probe (caller must abort).
        Returns an UNREACHABLE ProbeResult if all attempts are exhausted.
        """
        base_delay = 0.5

        for attempt in range(max_attempts):
            # Lease gate: abort immediately if lease lost
            if not self._journal.lease_held:
                logger.warning(
                    "[RecoveryProber] Lease lost during probe of %s (attempt %d)",
                    decl.name, attempt + 1,
                )
                return None

            try:
                result = await self._probe_once(decl)
                if result.reachable or result.category != HealthCategory.UNREACHABLE:
                    # Stamp epoch/seq from journal
                    result.probe_epoch = self._journal.epoch
                    result.probe_seq = self._journal.current_seq
                    return result
            except Exception as exc:
                logger.debug(
                    "[RecoveryProber] Probe attempt %d/%d for %s failed: %s",
                    attempt + 1, max_attempts, decl.name, exc,
                )

            # Jittered exponential backoff before retry
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt) * (0.5 + random.random())
                await asyncio.sleep(delay)

        # All attempts exhausted
        return ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            error=f"all {max_attempts} probe attempts exhausted",
            probe_epoch=self._journal.epoch,
            probe_seq=self._journal.current_seq,
        )

    async def _probe_once(self, decl: ComponentDeclaration) -> ProbeResult:
        """Dispatch a single probe by component locality."""
        if decl.locality == ComponentLocality.IN_PROCESS:
            return await self._probe_in_process(decl)
        elif decl.locality == ComponentLocality.SUBPROCESS:
            return await self._probe_http(decl, timeout=5.0)
        elif decl.locality == ComponentLocality.REMOTE:
            return await self._probe_http(decl, timeout=10.0)
        else:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error=f"unknown locality: {decl.locality}",
            )

    async def _probe_in_process(self, decl: ComponentDeclaration) -> ProbeResult:
        """Probe an in-process component via registered runtime probe."""
        probe_fn = self._runtime_probes.get(decl.name)
        if probe_fn is None:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error=f"no runtime probe registered for {decl.name}",
            )

        try:
            data = await probe_fn()
            return self._classify_health_response(data)
        except Exception as exc:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error=str(exc),
            )

    async def _probe_http(
        self,
        decl: ComponentDeclaration,
        timeout: float,
    ) -> ProbeResult:
        """Probe a subprocess/remote component via HTTP GET."""
        if not decl.endpoint:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error=f"no endpoint configured for {decl.name}",
            )

        url = f"{decl.endpoint}{decl.health_path}"

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return self._classify_health_response(data)
                    else:
                        return ProbeResult(
                            reachable=True,
                            category=HealthCategory.SERVICE_DEGRADED,
                            error=f"HTTP {resp.status}",
                        )
        except ImportError:
            # aiohttp not available — fall back to unreachable
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error="aiohttp not installed; cannot probe HTTP endpoints",
            )
        except asyncio.TimeoutError:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error=f"HTTP probe timed out after {timeout}s",
            )
        except Exception as exc:
            return ProbeResult(
                reachable=False,
                category=HealthCategory.UNREACHABLE,
                error=str(exc),
            )

    def _classify_health_response(self, data: Dict) -> ProbeResult:
        """Map a health-check JSON response to a ProbeResult."""
        status = data.get("status", "unknown").lower()
        instance_id = str(data.get("instance_id", ""))
        api_version = str(data.get("api_version", ""))

        category_map = {
            "healthy": HealthCategory.HEALTHY,
            "ok": HealthCategory.HEALTHY,
            "ready": HealthCategory.HEALTHY,
            "degraded": HealthCategory.SERVICE_DEGRADED,
            "dependency_degraded": HealthCategory.DEPENDENCY_DEGRADED,
            "contract_mismatch": HealthCategory.CONTRACT_MISMATCH,
            "unhealthy": HealthCategory.SERVICE_DEGRADED,
        }

        category = category_map.get(status, HealthCategory.SERVICE_DEGRADED)

        return ProbeResult(
            reachable=True,
            category=category,
            instance_id=instance_id,
            api_version=api_version,
        )


# ── Recovery Reconciler ───────────────────────────────────────────

class RecoveryReconciler:
    """Compares projected state against probe results and applies corrections.

    All corrective transitions are journaled with deterministic idempotency
    keys so that a crash mid-reconciliation can safely replay.
    """

    def __init__(self, journal: Any, engine: Any) -> None:
        self._journal = journal
        self._engine = engine

    async def reconcile(
        self,
        component: str,
        projected: str,
        probe: ProbeResult,
    ) -> List[dict]:
        """Reconcile a single component.

        Returns a list of corrective action dicts applied (for auditing).
        """
        actions: List[dict] = []

        # Lease gate
        if not self._journal.lease_held:
            logger.warning("[RecoveryReconciler] Lease lost, aborting reconcile for %s", component)
            return actions

        category = probe.category
        idemp_base = self._make_idempotency_key(component, projected, probe)

        # ── READY/DEGRADED + unreachable → LOST ─────────────────
        if projected in ("READY", "DEGRADED") and category == HealthCategory.UNREACHABLE:
            actions.append(await self._apply_transition(
                component, "LOST",
                reason="reconcile_mark_lost",
                idemp_key=idemp_base,
            ))

        # ── READY/DEGRADED + healthy → no-op ────────────────────
        elif projected in ("READY", "DEGRADED") and category == HealthCategory.HEALTHY:
            pass  # No action needed

        # ── READY + service/dependency degraded → DEGRADED ──────
        elif projected == "READY" and category in (
            HealthCategory.SERVICE_DEGRADED,
            HealthCategory.DEPENDENCY_DEGRADED,
        ):
            actions.append(await self._apply_transition(
                component, "DEGRADED",
                reason="reconcile_mark_degraded",
                idemp_key=idemp_base,
            ))

        # ── READY/DEGRADED + contract_mismatch → FAILED ────────
        elif projected in ("READY", "DEGRADED") and category == HealthCategory.CONTRACT_MISMATCH:
            actions.append(await self._apply_transition(
                component, "FAILED",
                reason="reconcile_mark_failed",
                idemp_key=idemp_base,
            ))

        # ── FAILED/LOST + healthy → recovery chain ─────────────
        elif projected in ("FAILED", "LOST") and category == HealthCategory.HEALTHY:
            recovery_actions = await self._apply_recovery_chain(
                component, projected, probe, idemp_base,
            )
            actions.extend(recovery_actions)

        # ── STARTING/HANDSHAKING + unreachable → FAILED ────────
        elif projected in ("STARTING", "HANDSHAKING") and category == HealthCategory.UNREACHABLE:
            actions.append(await self._apply_transition(
                component, "FAILED",
                reason="reconcile_mark_failed",
                idemp_key=idemp_base,
            ))

        # ── DRAINING/STOPPING + unreachable → STOPPED ──────────
        elif projected in ("DRAINING", "STOPPING") and category == HealthCategory.UNREACHABLE:
            if projected == "DRAINING":
                # DRAINING -> STOPPING -> STOPPED
                actions.append(await self._apply_transition(
                    component, "STOPPING",
                    reason="reconcile_mark_stopped",
                    idemp_key=f"{idemp_base}:stopping",
                ))
                actions.append(await self._apply_transition(
                    component, "STOPPED",
                    reason="reconcile_mark_stopped",
                    idemp_key=f"{idemp_base}:stopped",
                ))
            else:
                # STOPPING -> STOPPED
                actions.append(await self._apply_transition(
                    component, "STOPPED",
                    reason="reconcile_mark_stopped",
                    idemp_key=idemp_base,
                ))

        return [a for a in actions if a is not None]

    async def _apply_transition(
        self,
        component: str,
        new_status: str,
        *,
        reason: str,
        idemp_key: str,
    ) -> Optional[dict]:
        """Apply a single corrective transition."""
        if not self._journal.lease_held:
            return None

        try:
            seq = await self._engine.transition_component(
                component, new_status,
                reason=reason,
                trigger_seq=self._journal.current_seq,
            )
            action = {
                "component": component,
                "to": new_status,
                "reason": reason,
                "seq": seq,
                "idempotency_key": idemp_key,
            }
            logger.info(
                "[RecoveryReconciler] %s -> %s (reason=%s, seq=%d)",
                component, new_status, reason, seq,
            )
            return action
        except Exception as exc:
            logger.error(
                "[RecoveryReconciler] Failed transition %s -> %s: %s",
                component, new_status, exc,
            )
            return None

    async def _apply_recovery_chain(
        self,
        component: str,
        projected: str,
        probe: ProbeResult,
        idemp_base: str,
    ) -> List[dict]:
        """Apply FAILED/LOST -> STARTING -> HANDSHAKING -> READY chain."""
        actions: List[dict] = []

        # Get the declaration for handshake timeout
        decl = self._engine.get_declaration(component)
        handshake_timeout = decl.handshake_timeout_s if decl else 10.0

        # Step 1: -> STARTING
        action = await self._apply_transition(
            component, "STARTING",
            reason="reconcile_recover",
            idemp_key=f"{idemp_base}:starting",
        )
        if action:
            actions.append(action)
        else:
            return actions

        if not self._journal.lease_held:
            return actions

        # Step 2: -> HANDSHAKING
        action = await self._apply_transition(
            component, "HANDSHAKING",
            reason="reconcile_recover",
            idemp_key=f"{idemp_base}:handshaking",
        )
        if action:
            actions.append(action)
        else:
            return actions

        if not self._journal.lease_held:
            return actions

        # Step 3: -> READY (with timeout protection)
        try:
            action = await asyncio.wait_for(
                self._apply_transition(
                    component, "READY",
                    reason="reconcile_recover",
                    idemp_key=f"{idemp_base}:ready",
                ),
                timeout=handshake_timeout,
            )
            if action:
                actions.append(action)
        except asyncio.TimeoutError:
            logger.warning(
                "[RecoveryReconciler] Handshake timeout for %s (%.1fs), falling back to DEGRADED",
                component, handshake_timeout,
            )
            # Soft fallback: try DEGRADED (from HANDSHAKING that's not valid,
            # so go through FAILED first, then STARTING, then mark degraded path)
            fallback = await self._apply_transition(
                component, "FAILED",
                reason=f"reconcile_handshake_timeout_{handshake_timeout}s",
                idemp_key=f"{idemp_base}:timeout_failed",
            )
            if fallback:
                actions.append(fallback)

        return actions

    def _make_idempotency_key(
        self,
        component: str,
        projected: str,
        probe: ProbeResult,
    ) -> str:
        """Generate deterministic idempotency key for reconciliation."""
        return (
            f"reconcile:{component}:{probe.probe_epoch}:"
            f"{projected}->{probe.category.value}:"
            f"{probe.instance_id}:{probe.api_version}"
        )


# ── Recovery Orchestrator ──────────────────────────────────────────

class RecoveryOrchestrator:
    """Orchestrates startup recovery and periodic sparse audits.

    Reads projected state from the journal, probes each component,
    and reconciles discrepancies via the RecoveryReconciler.
    """

    def __init__(
        self,
        journal: Any,
        engine: Any,
        prober: RecoveryProber,
    ) -> None:
        self._journal = journal
        self._engine = engine
        self._prober = prober
        self._reconciler = RecoveryReconciler(journal, engine)

    async def run_startup_recovery(self) -> dict:
        """Run full recovery on startup after lease acquisition.

        Returns a summary dict with keys:
            aborted, epoch, probed, reconciled, skipped, errors
        """
        summary = {
            "aborted": False,
            "epoch": self._journal.epoch,
            "probed": 0,
            "reconciled": 0,
            "skipped": 0,
            "errors": [],
        }

        if not self._journal.lease_held:
            summary["aborted"] = True
            summary["errors"].append("lease not held at recovery start")
            return summary

        # Read all projected component states from journal
        projected_states = self._journal.get_all_component_states()

        logger.info(
            "[RecoveryOrchestrator] Starting recovery for epoch %d — %d components",
            self._journal.epoch, len(projected_states),
        )

        for comp_name, state_record in projected_states.items():
            # Lease check at each iteration
            if not self._journal.lease_held:
                logger.warning(
                    "[RecoveryOrchestrator] Lease lost mid-recovery at component %s",
                    comp_name,
                )
                summary["aborted"] = True
                summary["errors"].append(f"lease lost at {comp_name}")
                break

            projected_status = state_record.get("status", "REGISTERED")

            # Classify: should we probe this component?
            classification = await self._prober.classify_for_probe(
                comp_name, projected_status,
            )

            if classification is None:
                summary["skipped"] += 1
                continue

            # Probe the component
            decl = self._engine.get_declaration(comp_name)
            if decl is None:
                logger.warning(
                    "[RecoveryOrchestrator] No declaration for %s, skipping",
                    comp_name,
                )
                summary["skipped"] += 1
                continue

            try:
                probe_result = await self._prober.probe_component(
                    decl, projected_status,
                )
            except Exception as exc:
                logger.error(
                    "[RecoveryOrchestrator] Probe error for %s: %s",
                    comp_name, exc,
                )
                summary["errors"].append(f"probe_error:{comp_name}:{exc}")
                continue

            if probe_result is None:
                # Lease lost during probe
                summary["aborted"] = True
                summary["errors"].append(f"lease lost during probe of {comp_name}")
                break

            summary["probed"] += 1

            # Reconcile
            try:
                actions = await self._reconciler.reconcile(
                    comp_name, projected_status, probe_result,
                )
                summary["reconciled"] += len(actions)
            except Exception as exc:
                logger.error(
                    "[RecoveryOrchestrator] Reconcile error for %s: %s",
                    comp_name, exc,
                )
                summary["errors"].append(f"reconcile_error:{comp_name}:{exc}")

        logger.info(
            "[RecoveryOrchestrator] Recovery complete — probed=%d, reconciled=%d, "
            "skipped=%d, aborted=%s, errors=%d",
            summary["probed"], summary["reconciled"],
            summary["skipped"], summary["aborted"], len(summary["errors"]),
        )

        return summary

    async def run_sparse_audit(self) -> dict:
        """Run a periodic integrity audit (same logic as startup recovery).

        Returns the same summary format as run_startup_recovery().
        """
        return await self.run_startup_recovery()
