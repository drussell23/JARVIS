"""Layer-2 Persona Council Deliberator (Phase 12, Hive Step 3).

The persona council in ``backend/hive/`` was fully built and unit-tested but
triple-severed: no convener on any live path, no real input, and its only
output surface (the HUD relay) defaulted to a no-op. This layer repairs all
three ends AT ONCE, as commentary over the REAL feed:

  * INPUT — a read-only ``HiveAggregator.tap()`` observer: qualifying
    high-signal envelopes (severity-gated, env-tunable) become deliberation
    triggers. Guards kill the nasty edge cases structurally:
      - the FEEDBACK LOOP: ``persona``-subsystem envelopes never re-trigger
        the council (its own speech can't convene it);
      - SYNTHETIC contamination: ``trace_class=synthetic_probe`` frames
        (ov doctor --live) never convene a deliberation;
      - storms: per-(actor, intent) cooldown + an hourly convene budget.
  * DELIBERATION — the REAL ``HiveService`` debate loop (OBSERVE → PROPOSE →
    VALIDATE, three personas, DW-modeled, token-budgeted). Reused, not
    reimplemented. Two spec'd mandates are added at this layer:
      - ACTIVE-SPEAKER MUTEX: one deliberation at a time, ever (an
        ``asyncio.Lock`` around each convene — model spend serializes);
      - the TOKEN-GATED CONSENSUS BRAKE: the council's own reject cap
        (``JARVIS_HIVE_MAX_REJECTS``, default 2 → three proposal turns) and
        per-thread token budget stay authoritative, and the previously
        UNENFORCED debate deadline is finally applied here via
        ``asyncio.wait_for`` (env ``JARVIS_HIVE_COUNCIL_DEADLINE_S``).
  * OUTPUT — ``HudRelayAgent(ipc_send=...)`` (the council's native seam)
    pointed at the Step-2 emission edge: utterances become ``persona``-
    subsystem envelopes in the SAME ``ov hive`` feed, rendered beside the
    real events they deliberate about.

AUTHORITY: none. Consensus is serialized to an ``_AdvisoryLoop`` that mints
an advisory id and emits a frame — it NEVER submits to the real governance
intake. Deliberation is legible thought, not command.

Master ``JARVIS_HIVE_COUNCIL_ENABLED`` — default **false** (Step 3 spec).
Every knob env-tunable; NEVER raises; a council fault can never touch the
feed, the aggregator, or the pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Jarvis.HiveCouncil")

_MASTER_ENV = "JARVIS_HIVE_COUNCIL_ENABLED"


def council_enabled() -> bool:
    return os.environ.get(_MASTER_ENV, "false").strip().lower() == "true"


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "")
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "")
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _trigger_severities() -> Tuple[str, ...]:
    raw = os.environ.get("JARVIS_HIVE_COUNCIL_TRIGGER_SEVERITIES", "warn,error")
    return tuple(s.strip() for s in raw.split(",") if s.strip())


class _AdvisoryLoop:
    """The council's consensus sink — mints an advisory id, emits a feed
    frame, and deliberately NEVER touches the real governance intake."""

    def __init__(self, emit_fn: Any) -> None:
        self._emit = emit_fn

    async def submit(self, ctx: Any, trigger_source: str = "") -> Dict[str, str]:
        advisory_id = f"council-advisory-{uuid.uuid4().hex[:10]}"
        try:
            goal = str(getattr(ctx, "goal", "") or "")[:140]
            self._emit(
                actor_id="persona.council", subsystem="persona",
                intent="consensus_advisory",
                summary=f"council consensus (advisory only): {goal}",
                severity="success", trace_id=advisory_id,
                detail={"trigger_source": trigger_source, "advisory": True},
            )
        except Exception:  # noqa: BLE001
            pass
        return {"op_id": advisory_id}


class CouncilDeliberator:
    """The layer-2 convener. Owns nothing but its own worker; every
    dependency is injectable; every public surface NEVER raises."""

    def __init__(
        self,
        *,
        doubleword: Any = None,
        emit_fn: Any = None,
        state_dir: Optional[Path] = None,
    ) -> None:
        self._dw = doubleword
        self._emit = emit_fn or self._default_emit
        self._state_dir = state_dir
        self._service: Any = None
        self._speaker_mutex = asyncio.Lock()     # Active-Speaker: ONE debate ever
        self._queue: "asyncio.Queue[Any]" = asyncio.Queue(
            maxsize=_env_int("JARVIS_HIVE_COUNCIL_QUEUE_MAX", 8))
        self._worker: Optional[asyncio.Task] = None
        self._running = False
        self._cooldowns: Dict[Tuple[str, str], float] = {}
        self._convene_times: List[float] = []
        self.stats = {"triggers_seen": 0, "triggers_accepted": 0,
                      "convened": 0, "completed": 0, "deadline_hits": 0,
                      "suppressed_feedback": 0, "suppressed_synthetic": 0,
                      "suppressed_cooldown": 0, "suppressed_budget": 0}

    # -- construction helpers ------------------------------------------------

    @staticmethod
    def _default_emit(**kwargs: Any) -> None:
        from backend.api.hive_emitter import hive_emit
        hive_emit(**kwargs)

    def _resolve_dw(self) -> Any:
        """Lazy Doubleword resolution — the SAME provider the persona engine
        was written against (async ``prompt_only``). NEVER raises."""
        if self._dw is not None:
            return self._dw
        try:
            from backend.core.ouroboros.governance.doubleword_provider import (
                DoublewordProvider,
            )
            self._dw = DoublewordProvider()
            return self._dw
        except Exception:  # noqa: BLE001
            logger.debug("[Council] doubleword unavailable", exc_info=True)
            return None

    def _build_service(self) -> Any:
        """Construct the REAL HiveService with the relay seam pointed at the
        Step-2 emission edge. bus=None is safe: subscription only happens in
        ``start()``, which this layer never calls (it convenes directly)."""
        from backend.hive.hive_service import HiveService
        from backend.hive.hud_relay_agent import HudRelayAgent

        dw = self._resolve_dw()
        if dw is None:
            return None
        state_dir = self._state_dir or Path(
            os.environ.get("JARVIS_HIVE_STATE_DIR",
                           str(Path.home() / ".jarvis" / "hive"))) / "council"
        service = HiveService(bus=None, governed_loop=_AdvisoryLoop(self._emit),
                              doubleword=dw, state_dir=state_dir)
        service._relay = HudRelayAgent(ipc_send=self._relay_sink)
        return service

    # -- INPUT: the aggregator tap ------------------------------------------

    def on_envelope(self, env: Any) -> None:
        """Sync tap callback. Filters + enqueues; NEVER raises, NEVER blocks."""
        try:
            self.stats["triggers_seen"] += 1
            if not self._running or not council_enabled():
                return
            if getattr(env, "subsystem", "") == "persona":
                self.stats["suppressed_feedback"] += 1     # its own speech
                return
            detail = getattr(env, "detail", None) or {}
            if detail.get("trace_class") == "synthetic_probe":
                self.stats["suppressed_synthetic"] += 1    # doctor probes
                return
            if getattr(env, "severity", "info") not in _trigger_severities():
                return
            key = (str(getattr(env, "actor_id", "?")),
                   str(getattr(env, "intent", "?")))
            now = time.monotonic()
            cooldown = _env_float("JARVIS_HIVE_COUNCIL_COOLDOWN_S", 600.0)
            if now - self._cooldowns.get(key, -1e12) < cooldown:
                self.stats["suppressed_cooldown"] += 1
                return
            budget = _env_int("JARVIS_HIVE_COUNCIL_MAX_PER_HOUR", 4)
            self._convene_times = [t for t in self._convene_times
                                   if now - t < 3600.0]
            if len(self._convene_times) >= budget:
                self.stats["suppressed_budget"] += 1
                return
            try:
                self._queue.put_nowait(env)
            except asyncio.QueueFull:
                return                                     # storm — drop newest
            self._cooldowns[key] = now
            self._convene_times.append(now)
            self.stats["triggers_accepted"] += 1
        except Exception:  # noqa: BLE001
            pass

    # -- OUTPUT: council envelopes → the Step-2 emission edge ----------------

    async def _relay_sink(self, envelope: Dict[str, Any]) -> None:
        """HudRelayAgent sender: translate council events into persona-
        subsystem feed frames. NEVER raises (relay swallows anyway)."""
        try:
            etype = str(envelope.get("event_type", ""))
            data = envelope.get("data") or {}
            if etype == "persona_reasoning":
                persona = str(data.get("persona", "?"))
                reasoning = str(data.get("reasoning", ""))[:200]
                verdict = data.get("validate_verdict")
                self._emit(
                    actor_id=f"persona.{persona}", subsystem="persona",
                    intent=str(data.get("intent", "reasoning")),
                    summary=(f"{persona}: {reasoning}"
                             + (f" [{verdict}]" if verdict else "")),
                    severity="info",
                    trace_id=str(data.get("thread_id", "council")),
                    detail={"confidence": data.get("confidence"),
                            "model": data.get("model_used"),
                            "tokens": data.get("token_cost"),
                            "principle": data.get("manifesto_principle")},
                )
            elif etype == "thread_lifecycle":
                self._emit(
                    actor_id="persona.council", subsystem="persona",
                    intent="thread_lifecycle",
                    summary=f"deliberation → {data.get('state', '?')}",
                    severity="info",
                    trace_id=str(data.get("thread_id", "council")),
                    detail={k: v for k, v in data.items() if k != "thread_id"},
                )
            # agent_log echoes + cognitive transitions are internal chatter —
            # not projected (the feed already carries the triggering event).
        except Exception:  # noqa: BLE001
            pass

    # -- DELIBERATION --------------------------------------------------------

    async def _convene(self, env: Any) -> None:
        """One bounded deliberation about one real envelope."""
        from backend.hive.thread_models import CognitiveState, ThreadState

        if self._service is None:
            self._service = self._build_service()
            if self._service is None:
                return
        title = str(getattr(env, "action_summary", "") or "feed event")[:120]
        thread = self._service.thread_manager.create_thread(
            title=title,
            trigger_event=(f"{getattr(env, 'subsystem', '?')}:"
                           f"{getattr(env, 'intent', '?')}"),
            cognitive_state=CognitiveState.FLOW,   # thread-carried: no FSM poke
        )
        self._service.thread_manager.transition(
            thread.thread_id, ThreadState.DEBATING)
        self.stats["convened"] += 1
        deadline = _env_float("JARVIS_HIVE_COUNCIL_DEADLINE_S", 240.0)
        try:
            # The previously-UNENFORCED debate deadline, finally applied.
            await asyncio.wait_for(
                self._service._run_debate_round(thread.thread_id),
                timeout=deadline)
            self.stats["completed"] += 1
        except asyncio.TimeoutError:
            self.stats["deadline_hits"] += 1
            self._emit(
                actor_id="persona.council", subsystem="persona",
                intent="thread_lifecycle",
                summary=(f"deliberation timed out at {deadline:.0f}s — "
                         "closed by the consensus brake"),
                severity="warn", trace_id=thread.thread_id,
                detail={"deadline_s": deadline},
            )
        except Exception:  # noqa: BLE001
            logger.debug("[Council] deliberation degraded", exc_info=True)

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                env = await self._queue.get()
                async with self._speaker_mutex:      # ONE deliberation, ever
                    await self._convene(env)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("[Council] worker degraded", exc_info=True)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> bool:
        if not council_enabled():
            return False
        self._running = True
        self._worker = asyncio.create_task(
            self._worker_loop(), name="hive-council-worker")
        logger.info("[Council] layer-2 deliberator up (advisory, "
                    "default-bounded) — persona frames will join ov hive")
        return True

    async def stop(self) -> None:
        self._running = False
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._worker = None


def register_flags(registry: Any) -> None:
    """FlagRegistry declaration (via governance/hive_flags delegate). NEVER
    raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
        for spec in (
            FlagSpec(name=_MASTER_ENV, type=FlagType.BOOL, default=False,
                     category=Category.EXPERIMENTAL,
                     source_file="backend/api/hive_council_layer.py",
                     example="JARVIS_HIVE_COUNCIL_ENABLED=true",
                     description="Layer-2 persona-council deliberator over the hive feed (advisory)"),
            FlagSpec(name="JARVIS_HIVE_COUNCIL_DEADLINE_S", type=FlagType.FLOAT,
                     default=240.0, category=Category.EXPERIMENTAL,
                     source_file="backend/api/hive_council_layer.py",
                     example="JARVIS_HIVE_COUNCIL_DEADLINE_S=120",
                     description="Hard deliberation deadline (the consensus brake's outer bound)"),
            FlagSpec(name="JARVIS_HIVE_COUNCIL_MAX_PER_HOUR", type=FlagType.INT,
                     default=4, category=Category.EXPERIMENTAL,
                     source_file="backend/api/hive_council_layer.py",
                     example="JARVIS_HIVE_COUNCIL_MAX_PER_HOUR=2",
                     description="Hourly convene budget"),
            FlagSpec(name="JARVIS_HIVE_COUNCIL_COOLDOWN_S", type=FlagType.FLOAT,
                     default=600.0, category=Category.EXPERIMENTAL,
                     source_file="backend/api/hive_council_layer.py",
                     example="JARVIS_HIVE_COUNCIL_COOLDOWN_S=300",
                     description="Per-(actor,intent) trigger cooldown"),
        ):
            registry.register(spec)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["CouncilDeliberator", "council_enabled", "register_flags"]
