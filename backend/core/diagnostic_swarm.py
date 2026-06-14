"""Diagnostic Swarm — ephemeral, read-only intelligence for the endorsement
gateway (Spec 2 / Slice 254).

When the Cybernetic Reanimation shadow_guard traps a COMPLEX anomaly
(RESOURCE_PRESSURE / ANOMALY_DETECTED), the kernel must not block to figure out
*why*. Instead the ``SubAgentOrchestrator`` — subscribed to SHADOW_ACTION_TRAPPED
— dynamically spawns an ephemeral, read-only ``DiagnosticSubAgent`` on a hard
TTL. The agent investigates the live system state asynchronously and pipes a
concise root-cause analysis back to the endorsement gateway, so the Host's
``[Endorse Execution? Y/N]`` decision is intelligence-backed, not blind.

Design invariants (kept deliberately strict):
  * **Read-only.** A DiagnosticSubAgent observes; it has NO authority surface
    (no execute / endorse / kill / shed). Diagnosis can never act on the world —
    that authority stays with the Host (Slice 253) behind the shadow shield.
  * **Non-blocking.** ``handle_trap`` schedules the investigation as a background
    task and returns immediately. The trap chokepoint / kernel event loop is
    never awaited on.
  * **Bounded.** Every investigation is wrapped in a hard TTL (``asyncio.wait_for``,
    default 60s) so a hung probe cannot leak; findings are kept in a capacity-
    capped ring.
  * **Fail-soft.** A probe that raises or times out yields a finding with the
    failure status — it never propagates, never breaks the trap path.

Decoupled + duck-typed (no kernel import): unit-testable with fake probes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import backend.core.cybernetic_reanimation as _cr

logger = logging.getLogger("jarvis.diagnostic_swarm")

_ENV_ENABLED = "JARVIS_DIAGNOSTIC_SWARM_ENABLED"
_ENV_TTL_S = "JARVIS_DIAGNOSTIC_AGENT_TTL_S"
_ENV_FINDINGS_MAX = "JARVIS_DIAGNOSTIC_FINDINGS_MAX"
_DEFAULT_TTL_S = 60.0
_DEFAULT_FINDINGS_MAX = 64

# The signal types complex enough to warrant an ephemeral diagnostic (plan §2).
_COMPLEX_SIGNAL_PREFIXES = ("anomaly_detected", "resource_pressure")


def swarm_enabled() -> bool:
    """Master switch (default-TRUE). NEVER raises."""
    try:
        return os.getenv(_ENV_ENABLED, "true").strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False  # fail-safe OFF — the swarm is an enrichment, not a guarantee


def agent_ttl_s() -> float:
    try:
        return max(0.5, float(os.getenv(_ENV_TTL_S, str(_DEFAULT_TTL_S))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_TTL_S


def _findings_max() -> int:
    try:
        return max(1, int(os.getenv(_ENV_FINDINGS_MAX, str(_DEFAULT_FINDINGS_MAX))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_FINDINGS_MAX


def is_complex_trap(payload: Dict[str, Any]) -> bool:
    """True iff the trapped signal is complex enough to delegate a diagnostic."""
    try:
        sig = str(payload.get("triggering_signal", "") or "")
        return any(sig.startswith(p) for p in _COMPLEX_SIGNAL_PREFIXES)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class DiagnosticFinding:
    """An ephemeral agent's root-cause analysis of a trapped action."""
    action_id: str
    op_id: str
    organ: str
    summary: str               # concise, human-facing root-cause analysis
    facts: Dict[str, Any]      # structured evidence (process tree / resource snapshot)
    status: str                # ok | timeout | error
    elapsed_s: float


# ---------------------------------------------------------------------------
# The read-only probe — injectable so tests use fakes and the hot path is safe.
# ---------------------------------------------------------------------------

class DefaultDiagnosticProbe:
    """Read-only system-state probe. Best-effort psutil snapshot (top processes
    by CPU + system memory). NEVER raises — returns whatever it could gather."""

    async def investigate(self, context: Dict[str, Any]) -> Dict[str, Any]:
        facts: Dict[str, Any] = {}
        try:
            import psutil  # local import — optional dep, fail-soft
            facts["cpu_pct"] = psutil.cpu_percent(interval=0.0)
            vm = psutil.virtual_memory()
            facts["mem_pct"] = getattr(vm, "percent", None)
            procs = []
            for p in psutil.process_iter(["pid", "name", "cpu_percent"]):
                try:
                    procs.append(p.info)
                except Exception:  # noqa: BLE001
                    continue
            procs.sort(key=lambda d: (d.get("cpu_percent") or 0.0), reverse=True)
            top = procs[:3]
            facts["top_processes"] = top
            if top:
                t = top[0]
                facts["top_process"] = f"{t.get('name')}(pid={t.get('pid')}) cpu={t.get('cpu_percent')}%"
        except Exception:  # noqa: BLE001
            logger.debug("[Swarm] default probe degraded", exc_info=True)
            facts.setdefault("probe", "unavailable")
        return facts


class DiagnosticSubAgent:
    """An ephemeral, READ-ONLY investigator. Spawned per trapped complex anomaly,
    bounded by a hard TTL, discarded after it returns a single finding. It holds
    no authority surface — it cannot execute, endorse, kill, or shed."""

    def __init__(self, probe: Any = None, ttl_s: Optional[float] = None):
        self._probe = probe if probe is not None else DefaultDiagnosticProbe()
        self._ttl_s = ttl_s

    async def investigate(
        self,
        *,
        action_id: str,
        op_id: str = "",
        organ: str = "",
        intended_action: str = "",
        triggering_signal: str = "",
    ) -> DiagnosticFinding:
        ttl = self._ttl_s if self._ttl_s is not None else agent_ttl_s()
        context = {
            "action_id": action_id, "op_id": op_id, "organ": organ,
            "intended_action": intended_action, "triggering_signal": triggering_signal,
        }
        start = time.monotonic()
        try:
            facts = await asyncio.wait_for(self._probe.investigate(context), timeout=ttl)
            return DiagnosticFinding(
                action_id=action_id, op_id=op_id, organ=organ,
                summary=self._synthesize(context, facts), facts=dict(facts or {}),
                status="ok", elapsed_s=round(time.monotonic() - start, 4),
            )
        except asyncio.TimeoutError:
            return DiagnosticFinding(
                action_id=action_id, op_id=op_id, organ=organ,
                summary=f"diagnostic timed out after {ttl:.0f}s — investigate manually",
                facts={}, status="timeout", elapsed_s=round(time.monotonic() - start, 4),
            )
        except Exception as exc:  # noqa: BLE001 — diagnosis is best-effort
            logger.debug("[Swarm] sub-agent investigate failed", exc_info=True)
            return DiagnosticFinding(
                action_id=action_id, op_id=op_id, organ=organ,
                summary=f"diagnostic error: {str(exc)[:160]}", facts={},
                status="error", elapsed_s=round(time.monotonic() - start, 4),
            )

    @staticmethod
    def _synthesize(context: Dict[str, Any], facts: Dict[str, Any]) -> str:
        """Turn raw evidence into a concise, human-facing root-cause line."""
        parts = []
        top = facts.get("top_process")
        if top:
            parts.append(f"hottest: {top}")
        if facts.get("cpu_pct") is not None:
            parts.append(f"cpu={facts['cpu_pct']}%")
        if facts.get("mem_pct") is not None:
            parts.append(f"mem={facts['mem_pct']}%")
        sig = context.get("triggering_signal") or "?"
        if not parts:
            return f"signal {sig}: no system evidence gathered"
        return f"signal {sig}: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Findings store — bounded + awaitable (the REPL can wait for a fresh diagnosis).
# ---------------------------------------------------------------------------

class DiagnosticFindingsStore:
    """Capacity-capped store keyed by action_id, with a per-key asyncio.Event so a
    consumer can await a finding that is still being computed."""

    def __init__(self, max_size: Optional[int] = None):
        self._max = max_size
        self._findings: "OrderedDict[str, DiagnosticFinding]" = OrderedDict()
        self._events: Dict[str, asyncio.Event] = {}

    def _max_size(self) -> int:
        return self._max if self._max is not None else _findings_max()

    def _event_for(self, action_id: str) -> asyncio.Event:
        ev = self._events.get(action_id)
        if ev is None:
            ev = asyncio.Event()
            self._events[action_id] = ev
        return ev

    def put(self, finding: DiagnosticFinding) -> None:
        self._findings[finding.action_id] = finding
        self._findings.move_to_end(finding.action_id)
        cap = self._max_size()
        while len(self._findings) > cap:
            old_id, _ = self._findings.popitem(last=False)
            self._events.pop(old_id, None)
        self._event_for(finding.action_id).set()

    def get(self, action_id: str) -> Optional[DiagnosticFinding]:
        return self._findings.get(action_id)

    async def wait_for(self, action_id: str, timeout: float) -> Optional[DiagnosticFinding]:
        existing = self._findings.get(action_id)
        if existing is not None:
            return existing
        ev = self._event_for(action_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._findings.get(action_id)

    def reset(self) -> None:
        self._findings.clear()
        self._events.clear()


# ---------------------------------------------------------------------------
# The orchestrator — subscribes to traps, spawns ephemeral agents non-blocking.
# ---------------------------------------------------------------------------

class SubAgentOrchestrator:
    """Subscribes to SHADOW_ACTION_TRAPPED and, for each COMPLEX trapped anomaly,
    spawns an ephemeral DiagnosticSubAgent as a background task (never blocking the
    kernel). Stores each finding keyed by action_id for the endorsement gateway."""

    def __init__(
        self,
        *,
        store: Optional[DiagnosticFindingsStore] = None,
        agent_factory: Optional[Callable[[], DiagnosticSubAgent]] = None,
    ):
        self.store = store if store is not None else DiagnosticFindingsStore()
        self._agent_factory = agent_factory or (lambda: DiagnosticSubAgent())
        self._tasks: "set[asyncio.Task]" = set()  # strong refs — no premature GC

    def handle_trap(self, payload: Dict[str, Any]) -> Optional[asyncio.Task]:
        """NON-BLOCKING. Schedule an ephemeral diagnostic for a complex trapped
        action and return immediately. Returns the scheduled task (or None when
        the swarm is disabled, the trap is not complex, or no loop is running)."""
        if not swarm_enabled() or not is_complex_trap(payload):
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[Swarm] no running loop — cannot spawn diagnostic")
            return None
        task = loop.create_task(self._investigate_and_store(dict(payload)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _investigate_and_store(self, payload: Dict[str, Any]) -> None:
        try:
            agent = self._agent_factory()
            finding = await agent.investigate(
                action_id=str(payload.get("action_id", "")),
                op_id=str(payload.get("op_id", "")),
                organ=str(payload.get("organ_name", "")),
                intended_action=str(payload.get("intended_action", "")),
                triggering_signal=str(payload.get("triggering_signal", "")),
            )
            self.store.put(finding)
        except Exception:  # noqa: BLE001 — a swarm crash must never escape
            logger.debug("[Swarm] investigate_and_store failed", exc_info=True)

    def attach_to_trap_stream(self) -> None:
        """Subscribe to SHADOW_ACTION_TRAPPED (in-process, the decoupled bus)."""
        _cr.register_trap_observer(self.handle_trap)

    def detach(self) -> None:
        _cr.unregister_trap_observer(self.handle_trap)


# ---------------------------------------------------------------------------
# Phase 3 — REPL enrichment: diagnosis FIRST, then the endorsement decision.
# ---------------------------------------------------------------------------

def enriched_endorsement_prompt(
    payload: Dict[str, Any], finding: Optional[DiagnosticFinding],
) -> str:
    """Compose the intelligence-backed endorsement prompt: the ephemeral agent's
    root-cause analysis renders ABOVE the base [Y/N] decision (Slice 253). When no
    finding is available yet, a 'diagnosis pending' note keeps the Host informed
    without blocking the decision."""
    base = _cr.endorsement_prompt_for(payload)
    if finding is None:
        header = "🔬 Diagnosis pending — no analysis yet"
    else:
        header = f"🔬 Diagnosis ({finding.status}): {finding.summary}"
    return f"{header}\n{base}"


async def endorsement_prompt_with_diagnosis(
    orchestrator: SubAgentOrchestrator,
    payload: Dict[str, Any],
    *,
    wait_s: float = 2.0,
) -> str:
    """The endorsement gateway entry point: await the ephemeral agent's finding
    (bounded by ``wait_s``, never the kernel loop), then render the enriched
    prompt. If the diagnosis isn't ready in time, the Host still gets a prompt
    (pending) — the decision is never gated on the swarm."""
    finding: Optional[DiagnosticFinding] = None
    try:
        action_id = str(payload.get("action_id", "") or "")
        if action_id:
            finding = await orchestrator.store.wait_for(action_id, timeout=wait_s)
    except Exception:  # noqa: BLE001
        logger.debug("[Swarm] enrichment wait failed", exc_info=True)
    return enriched_endorsement_prompt(payload, finding)
