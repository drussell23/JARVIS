"""Do not load a local model onto a machine that is about to swap.

NAMING, BECAUSE THIS CODEBASE HAS TWO "MEMORY"S
-------------------------------------------------
`memory_admission.py` already exists and is about REMEMBERED CONTEXT — which
memory topics reached a prompt and why the rest did not. This module is about
RAM. Neither name is wrong and both are load-bearing, so this one says
`local_model` out loud: it guards the act of loading model weights, and
nothing else.

WHAT IT PROTECTS
------------------
Tier 2 now runs LOCALLY — Ollama on the same unified memory as the HUD. On a
16 GB M1 that memory is shared by the GPU, the CoreAudio graph, the vision
pipeline and the model. There is no separate VRAM to spill into, and no
pressure valve except swap.

So a background op deciding to think can take the microphone down. Not
metaphorically: the audio tap runs on a real-time thread,
`HALC_ProxyIOContext :: skipping cycle due to overload` is already in the boot
log at idle, and a swap storm during model load is exactly the condition that
turns a dropped buffer into a severed sentence. The voice path was taken from
15,000 ms to 37 ms; one Tier-2 dispatch that pages the machine gives that back.

A static `num_ctx` cap does not fix this. It is a guess made once, at config
time, about a condition that changes second to second — the same shape of
mistake as a per-soak budget measured against an all-time ledger. What is
needed is a reading taken at the moment of dispatch.

WHY THIS ADDS NO PROBE
------------------------
`memory_pressure_gate` already exists, already graduated (2026-04-21), and
already does the hard part: a stdlib probe cascade (psutil → /proc/meminfo →
`vm_stat` → fallback) behind a four-level enum with env-tunable thresholds.
A second probe here would be a second thing to keep correct, and the day the
two disagreed the machine would hold two opinions about whether it was safe
to allocate.

This is the POLICY layer only: what a generation request should DO about what
the gate already measures.

THE THREE OUTCOMES
--------------------
    OK / WARN     admit unchanged
    HIGH          admit, but PRUNE — shrink the KV footprint before it is
                  allocated, by dropping the oldest droppable context
    CRITICAL      DEFER — do not load, and say so as a normal result

Deferral is a RESULT, not an exception, for the same reason
`OPERATOR_DENIED_EXECUTION` is one: the caller must reason about it, retry it,
or route elsewhere. An exception would be caught by a generic handler and
rendered as a provider fault, tripping failover for a condition that is not a
failure — the machine is fine, it is merely busy.

WHY PRUNING TARGETS THE KV CACHE
----------------------------------
`mmap` keeps model WEIGHTS off the heap: pages load on demand and the OS
evicts them under pressure. The KV cache has no such escape. It is a live
allocation proportional to context length, it cannot be paged out without
destroying the generation, and at 262 K native context it is measured in
gigabytes. The weights look after themselves; the context is the part a
caller can actually give back.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.LocalModelAdmission")

LOCAL_MODEL_ADMISSION_SCHEMA_VERSION: str = "local_model_admission.v1"

#: Fed back verbatim when a request is deferred. A stable, greppable token
#: rather than prose — the same discipline as `capability_router
#: .DENIED_PAYLOAD`, because a reworded sentence reads as a new condition to
#: anything matching on it.
DEFERRED_PAYLOAD: str = "[SYSTEM: DEFERRED_DUE_TO_MEMORY_PRESSURE]"


def admission_enabled() -> bool:
    """Master gate. Default TRUE. NEVER raises.

    Off admits everything unchanged — the behaviour from before Tier 2 was
    local, and safe only while Tier 2 is remote.
    """
    return (os.environ.get("JARVIS_LOCAL_MODEL_ADMISSION_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def prune_floor_messages() -> int:
    """Messages never pruned, counted from the END. NEVER raises.

    The most recent turns are what the model is actually answering. Pruning
    into them does not save memory, it changes the question — and a smaller
    wrong answer is worse than a deferred right one.
    """
    try:
        raw = (os.environ.get("JARVIS_LOCAL_MODEL_PRUNE_FLOOR", "") or "").strip()
        return max(2, min(32, int(raw))) if raw else 6
    except (TypeError, ValueError):
        return 6


def pruned_ctx_fraction() -> float:
    """How much of the requested context survives a HIGH reading."""
    try:
        raw = (os.environ.get("JARVIS_LOCAL_MODEL_PRUNE_FRACTION", "") or "").strip()
        return max(0.1, min(1.0, float(raw))) if raw else 0.35
    except (TypeError, ValueError):
        return 0.35


class Admission(str, enum.Enum):
    """What to do with a generation request, given the machine's state."""

    ADMIT = "admit"
    PRUNE = "prune"
    DEFER = "defer"

    @property
    def proceeds(self) -> bool:
        return self is not Admission.DEFER


@dataclass
class AdmissionDecision:
    """One ruling, carrying the reading that produced it."""

    action: str
    level: str = "unknown"
    free_pct: float = -1.0
    source: str = ""
    #: What the caller should request instead. None = unchanged.
    num_ctx: Optional[int] = None
    pruned: int = 0
    reason: str = ""
    #: For a person, when this reaches one.
    spoken_reason: str = ""
    schema_version: str = LOCAL_MODEL_ADMISSION_SCHEMA_VERSION

    @property
    def proceeds(self) -> bool:
        try:
            return Admission(self.action).proceeds
        except Exception:  # noqa: BLE001 — an unreadable ruling never proceeds
            return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "action": self.action,
            "level": self.level, "free_pct": round(self.free_pct, 2),
            "source": self.source, "num_ctx": self.num_ctx,
            "pruned": self.pruned, "reason": self.reason,
        }


def _read_pressure() -> Tuple[str, float, str]:
    """(level, free_pct, source) from the EXISTING gate. NEVER raises.

    Fails toward OK. A probe that cannot read memory must not itself become a
    reason to refuse work — that would let one broken cascade stage silently
    disable Tier 2 for a whole session, a larger outage than the swap storm it
    was guarding against.
    """
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            get_default_gate,
        )
        gate = get_default_gate()
        probe = gate.probe()
        level = gate.level_for_free_pct(probe.free_pct)
        return (str(getattr(level, "value", level)), float(probe.free_pct),
                str(probe.source or "unknown"))
    except Exception:  # noqa: BLE001
        logger.debug("[LocalModelAdmission] pressure probe unavailable",
                     exc_info=True)
        return ("unknown", -1.0, "unavailable")


def assess(requested_ctx: Optional[int] = None) -> AdmissionDecision:
    """Should this generation be admitted right now? NEVER raises.

    Pure read plus arithmetic — no allocation and no I/O beyond the gate's own
    probe, so it is safe immediately before dispatch on the hot path.
    """
    if not admission_enabled():
        return AdmissionDecision(action=Admission.ADMIT.value,
                                 reason="admission control disabled")
    level, free_pct, source = _read_pressure()

    if level == "critical":
        return AdmissionDecision(
            action=Admission.DEFER.value, level=level, free_pct=free_pct,
            source=source,
            reason=(f"{free_pct:.1f}% memory free — loading a local model now "
                    f"would swap, and the audio graph shares this memory"),
            spoken_reason=("My machine is low on memory, so I've held that "
                           "thought rather than risk the microphone."))

    if level == "high":
        pruned_ctx = (max(1024, int(requested_ctx * pruned_ctx_fraction()))
                      if requested_ctx else None)
        return AdmissionDecision(
            action=Admission.PRUNE.value, level=level, free_pct=free_pct,
            source=source, num_ctx=pruned_ctx,
            reason=(f"{free_pct:.1f}% memory free — shrinking the KV cache "
                    f"rather than refusing the work"))

    return AdmissionDecision(action=Admission.ADMIT.value, level=level,
                             free_pct=free_pct, source=source,
                             reason=f"{free_pct:.1f}% memory free")


def prune_messages(messages: Any, decision: AdmissionDecision) -> Tuple[Any, int]:
    """Drop the oldest droppable messages. Returns (messages, dropped).

    NEVER raises. Acts only on a PRUNE ruling.

    KEEPS THE FIRST AND THE LAST. The first message is almost always the
    system prompt — the instructions that make the output parseable at all,
    and the one whose removal turns a slow answer into a malformed one. The
    last `prune_floor_messages()` are the live question. What sits between is
    history, which is what a shrinking machine can afford to forget.
    """
    try:
        if decision.action != Admission.PRUNE.value:
            return messages, 0
        floor = prune_floor_messages()
        if not isinstance(messages, list) or len(messages) <= floor + 1:
            return messages, 0
        head, tail = messages[:1], messages[-floor:]
        middle = messages[1:-floor]
        keep = int(len(middle) * pruned_ctx_fraction())
        kept = middle[-keep:] if keep else []
        dropped = len(middle) - len(kept)
        if dropped <= 0:
            return messages, 0
        logger.warning(
            "[LocalModelAdmission] pruned %d message(s) of history under %s "
            "pressure (%.1f%% free) — system prompt and last %d turns kept",
            dropped, decision.level, decision.free_pct, floor)
        decision.pruned = dropped
        return head + kept + tail, dropped
    except Exception:  # noqa: BLE001 — never mangle a payload on a fault
        logger.debug("[LocalModelAdmission] prune degraded", exc_info=True)
        return messages, 0


def report(decision: AdmissionDecision, *, what: str = "local Tier 2") -> None:
    """Tell O+V the machine hit its own limits. NEVER raises.

    Routed through `RuntimeHealthSensor.report` — the same push entry point
    `TaskHarvester` and `LoopSentinel` use, through the same `make_envelope`
    and the same intake router. No second metrics path: an organism that
    learns about its hardware through a different pipe than its software will
    eventually reason about them separately.

    Only DEFER and PRUNE are reported. An admitted request is the machine
    working, and a signal for each would drown the intake queue in good news.
    """
    try:
        if decision.action == Admission.ADMIT.value:
            return
        from backend.core.ouroboros.governance.intake.sensors.runtime_health_sensor import (
            HealthFinding, get_runtime_health_sensor,
        )
        finding = HealthFinding(
            category="memory_pressure_admission",
            severity=("high" if decision.action == Admission.DEFER.value
                      else "normal"),
            summary=(f"{what} {decision.action} at {decision.free_pct:.1f}% "
                     f"free memory — {decision.reason}"),
            details=decision.to_dict(),
            target_files=("backend/core/ouroboros/governance/providers.py",),
        )
        sensor = get_runtime_health_sensor()
        if sensor is None:
            # Boot ordering: intake may not exist yet. `TaskHarvester` owns the
            # buffer that flushes on sensor registration, so this rides it
            # rather than keeping a second queue.
            from backend.core.ouroboros.telemetry.task_harvester import (
                TaskFailure, get_task_harvester,
            )
            held = TaskFailure(what=what, summary=finding.summary,
                               severity=finding.severity,
                               target_files=finding.target_files)
            held.as_finding = lambda f=finding: f
            get_task_harvester()._pending.append(held)
            return
        result = sensor.report(finding)
        if result is not None and hasattr(result, "__await__"):
            import asyncio
            try:
                asyncio.get_event_loop().create_task(result)
            except RuntimeError:
                result.close()
    except Exception:  # noqa: BLE001 — telemetry never blocks admission
        logger.debug("[LocalModelAdmission] report degraded", exc_info=True)


def snapshot() -> Dict[str, Any]:
    """Current admission posture, for `/observability`. NEVER raises."""
    level, free_pct, source = _read_pressure()
    return {
        "schema_version": LOCAL_MODEL_ADMISSION_SCHEMA_VERSION,
        "enabled": admission_enabled(),
        "level": level,
        "free_pct": round(free_pct, 2),
        "source": source,
        "prune_floor_messages": prune_floor_messages(),
        "pruned_ctx_fraction": pruned_ctx_fraction(),
    }
