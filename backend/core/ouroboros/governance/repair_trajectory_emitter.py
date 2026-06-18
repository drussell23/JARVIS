"""RepairTrajectoryEmitter — Phase 1 of the Self-Correction & DPO Alignment Engine.

O+V's L2 self-repair loop produces the single richest training signal a coding agent can: a
**preference pair** — the candidate that FAILED validation (rejected) and the converged, test-passing
candidate that fixed it (chosen). This module serializes those trajectories and streams them to
Reactor-Core's experience pipeline, where the DPO pair generator + LoRA trainer consume them.

**Scoped to the DoubleWord stability goal:** each trajectory is labeled with its `provider` (the
generator that produced the rejected candidate). DW is the cheap primary provider but less stable than
Claude — DW-labeled `(failed → fixed)` pairs are exactly the data needed to train a local critic /
stabilizer for DW's specific structural failure modes (malformed JSON, non-diff output, schema drift),
so O+V can keep DW primary and avoid the expensive Claude fallback.

Honest scope: this emits the *data*; Reactor-Core does the training; the critic (preflight_critic.py)
is the consumer. Network I/O is fire-and-forget on a background task — never blocks the repair loop —
and fail-soft (any error is swallowed). Gated ``JARVIS_REPAIR_TRAJECTORY_EMIT_ENABLED`` (default OFF —
opt-in, since it ships data to an external service).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["RepairTrajectoryEmitter", "emitter_enabled", "build_dpo_trajectory"]


def emitter_enabled() -> bool:
    """``JARVIS_REPAIR_TRAJECTORY_EMIT_ENABLED`` (default OFF) — opt-in; streams trajectory data to
    Reactor-Core. OFF → no remote stream (the repair loop is byte-identical)."""
    return os.environ.get("JARVIS_REPAIR_TRAJECTORY_EMIT_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def critic_learn_enabled() -> bool:
    """``JARVIS_PREFLIGHT_CRITIC_LEARN_ENABLED`` (default OFF) — feed converged trajectories to the
    M1-native online critic so it ACCUMULATES on-device (decoupled from gating: learn first, then the
    critic auto-graduates to gating once warm + accurate). Cheap + fail-soft."""
    return os.environ.get("JARVIS_PREFLIGHT_CRITIC_LEARN_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _content(candidate: Any) -> str:
    """Best-effort full source of a candidate dict (full_content, else first multi-file entry)."""
    if not isinstance(candidate, dict):
        return ""
    fc = candidate.get("full_content")
    if isinstance(fc, str) and fc:
        return fc
    files = candidate.get("files")
    if isinstance(files, list) and files and isinstance(files[0], dict):
        return str(files[0].get("full_content", "") or "")
    return ""


def _provider_of(result: Any) -> str:
    """Provider attribution for the trajectory — the generator behind the chosen fix. Prefers the
    converged iteration's provider_name, falls back to the summary. '' if unknown."""
    try:
        iters = getattr(result, "iterations", ()) or ()
        for rec in reversed(iters):
            p = getattr(rec, "provider_name", "") or ""
            if p:
                return p
        summary = getattr(result, "summary", {}) or {}
        return str(summary.get("provider_name", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def build_dpo_trajectory(ctx: Any, result: Any) -> Optional[Dict[str, Any]]:
    """Pure: compile a converged L2 repair into a provider-labeled DPO/correction ExperienceEvent.

    rejected = the candidate that failed validation and triggered L2 (``ctx.generation.candidates[0]``);
    chosen = the converged, test-passing candidate (``result.candidate``). Returns ``None`` unless this
    is a genuine converged pair with both code states present (no hollow/empty trajectories)."""
    if getattr(result, "terminal", "") != "L2_CONVERGED":
        return None
    chosen = _content(getattr(result, "candidate", None))
    rejected = ""
    try:
        gen = getattr(ctx, "generation", None)
        cands = getattr(gen, "candidates", None) if gen is not None else None
        if cands:
            rejected = _content(cands[0])
    except Exception:  # noqa: BLE001
        rejected = ""
    if not chosen or not rejected or chosen == rejected:
        return None

    provider = _provider_of(result)
    iters = getattr(result, "iterations", ()) or ()
    # DivergenceSignature metrics from the validation gates (structural failure fingerprints).
    divergence_kinds: List[str] = []
    for rec in iters:
        fc = getattr(rec, "failure_class", "") or ""
        if fc:
            divergence_kinds.append(fc)

    file_path = ""
    cand = getattr(result, "candidate", None)
    if isinstance(cand, dict):
        file_path = str(cand.get("file_path", "") or "")

    # Canonical ExperienceEvent (correction shape) — original/corrected map to DPO rejected/chosen.
    return {
        "event_type": "correction",
        "source": "jarvis_body",
        "task_type": "l2_repair",
        "outcome": "success",
        "model_id": provider or "unknown",
        "provider": provider,                       # DW-stability labeling
        "user_input": f"Repair the failing candidate for {file_path or 'the target'}.",
        "assistant_output": chosen,
        "original_response": rejected,              # DPO rejected
        "corrected_response": chosen,               # DPO chosen
        "metadata": {
            "op_id": getattr(ctx, "op_id", ""),
            "file_path": file_path,
            "iterations": len(iters),
            "divergence_kinds": divergence_kinds,
            "stop_reason": getattr(result, "stop_reason", None),
        },
    }


class RepairTrajectoryEmitter:
    """Fire-and-forget emitter of converged L2 repair trajectories to Reactor-Core. Never blocks the
    repair loop; fail-soft. Injectable client for tests."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from backend.clients.reactor_core_client import ReactorCoreClient, ReactorCoreConfig
            self._client = ReactorCoreClient(ReactorCoreConfig())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[TrajectoryEmitter] reactor client unavailable: %s", exc)
        return self._client

    async def _send(self, event: Dict[str, Any]) -> bool:
        client = self._get_client()
        if client is None:
            return False
        try:
            init = getattr(client, "initialize", None)
            if init is not None:
                await init()
            ok = await client.stream_experience(event)
            logger.info("[TrajectoryEmitter] streamed l2_repair DPO pair provider=%s ok=%s",
                        event.get("provider", "?"), ok)
            return bool(ok)
        except Exception as exc:  # noqa: BLE001 — emission is best-effort
            logger.debug("[TrajectoryEmitter] stream failed (non-fatal): %s", exc)
            return False
        finally:
            for m in ("close", "disconnect"):
                fn = getattr(client, m, None)
                if fn is not None:
                    try:
                        await fn()
                    except Exception:  # noqa: BLE001
                        pass
                    break

    def _learn_local(self, event: Dict[str, Any]) -> None:
        """Feed the converged trajectory to the M1-native online critic (on-device, milliseconds).
        Offloaded to a thread (Metal/CPU executor ring) so the control bus sees zero latency."""
        try:
            from backend.core.ouroboros.governance.preflight_critic import get_default_critic
            critic = get_default_critic()
            rejected = event.get("original_response", "") or ""
            chosen = event.get("corrected_response", "") or ""
            file_path = (event.get("metadata", {}) or {}).get("file_path", "") or ""
            graph = None
            try:
                from backend.core.ouroboros.oracle import get_oracle
                graph = getattr(get_oracle(), "_graph", None)
            except Exception:  # noqa: BLE001
                graph = None
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                t = loop.create_task(critic.alearn_pair(rejected, chosen, file_path, graph))
                _PENDING.add(t); t.add_done_callback(_PENDING.discard)
            else:
                critic.learn_pair(rejected, chosen, file_path, graph)
        except Exception as exc:  # noqa: BLE001 — learning is best-effort
            logger.debug("[TrajectoryEmitter] local critic learn skipped: %s", exc)

    def emit(self, ctx: Any, result: Any) -> bool:
        """Build the trajectory once, then route it: (a) feed the local M1 online critic if learning is
        on, (b) fire-and-forget stream to Reactor-Core if remote emit is on. Gated + fail-soft; safe
        from the repair loop's terminal path. Returns True if either path ran."""
        if not (emitter_enabled() or critic_learn_enabled()):
            return False
        try:
            event = build_dpo_trajectory(ctx, result)
            if event is None:
                return False
            did = False
            if critic_learn_enabled():
                self._learn_local(event)
                did = True
            if emitter_enabled():
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    task = loop.create_task(self._send(event))
                    _PENDING.add(task)
                    task.add_done_callback(_PENDING.discard)
                else:
                    asyncio.run(self._send(event))
                did = True
            return did
        except Exception as exc:  # noqa: BLE001 — emission must never break L2
            logger.debug("[TrajectoryEmitter] emit skipped (non-fatal): %s", exc)
            return False


# Strong refs for in-flight fire-and-forget tasks (no-GC).
_PENDING: "set" = set()
