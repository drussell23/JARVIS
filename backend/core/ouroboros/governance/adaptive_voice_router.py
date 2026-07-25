"""AdaptiveVoiceRouter — one brain for Karen, two execution paths.

The disconnect
--------------
``audio_pipeline_bootstrap`` wires ``ConversationPipeline(llm_client=
self._model_serving)``. That static injection is the whole defect: the spoken
loop is welded to ``UnifiedModelServing``, so the measured DW voice election in
:mod:`karen_voice_lane` reaches typed turns and nothing else. Karen answers a
typed question in ~1s and a *spoken* one on a different brain entirely.

Swapping the variable would just weld it the other way. The pipeline needs a
POLYMORPHIC collaborator — something that satisfies the same
``generate_stream(ModelRequest) -> AsyncIterator[str]`` contract and decides,
per turn, which engine serves it.

Why remote is the DEFAULT, not the optimisation
-----------------------------------------------
On 16GB of unified memory, STT capture, TTS synthesis and local LLM inference
are contending for one pool while the operator is mid-sentence. Local
generation is exactly the wrong thing to schedule at that moment: the memory it
claims is the memory the audio path needs, and the symptom is stutter in the
capture stream — the one artifact a voice interface cannot have. Sending
generation to DW is therefore the resource-preserving choice, not merely the
fast one. Local becomes what it should always have been: the thing that keeps
Karen talking when the network does not.

The failover rule that matters
------------------------------
Failover is only legal BEFORE the first token has been spoken. Once a token has
reached the sentence splitter it is on its way to the speakers, and restarting
on a different model mid-utterance would have Karen say half of one answer and
then all of another. So the router tracks emission: a fault before the first
token fails over silently; a fault after it ends the utterance cleanly. This is
the difference between a resilient voice and a haunted one.

Everything else here is composition: :mod:`karen_voice_lane` elects the model,
``dw_deep_probe`` supplies the SSE dispatch, ``stream_watchdog._extract_token``
parses the deltas, ``doubleword_provider.apply_rt_service_tier`` stamps the RT
tier, ``memory_pressure_gate`` reports local headroom. This module owns a
circuit breaker and a multiplexer, and nothing else.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "").strip() or default))
    except (TypeError, ValueError):
        return int(default)


def router_enabled() -> bool:
    """Master gate. OFF makes the router a transparent pass-through to the
    local engine — i.e. byte-identical to the pre-router pipeline."""
    return os.environ.get(
        "JARVIS_ADAPTIVE_VOICE_ROUTER_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def remote_first() -> bool:
    """Prefer the DW voice lane. Default ON: it preserves local unified memory
    for STT/TTS, which on a 16GB machine is the binding constraint."""
    return os.environ.get(
        "JARVIS_VOICE_ROUTER_REMOTE_FIRST", "true",
    ).strip().lower() in _TRUTHY


def remote_ttft_budget_s() -> float:
    """How long to wait for DW's FIRST token before giving up and going local.

    Distinct from the voice lane's election budget: that one decides who is
    *eligible* to speak, this one decides how long a single turn tolerates
    silence before falling back. Slightly looser, because failing over costs a
    fresh generation and thrashing between engines is worse than one slow
    turn."""
    return max(0.2, _env_float("JARVIS_VOICE_ROUTER_TTFT_BUDGET_S", 3.0))


def remote_total_budget_s() -> float:
    """Ceiling on a whole remote utterance. A stream that stalls mid-sentence
    must not hold the microphone open forever."""
    return max(1.0, _env_float("JARVIS_VOICE_ROUTER_TOTAL_BUDGET_S", 45.0))


def breaker_threshold() -> int:
    """Consecutive remote faults before the circuit opens. >1 so a single
    blip does not exile DW; small so a real outage stops costing every turn
    the full TTFT budget."""
    return max(1, _env_int("JARVIS_VOICE_ROUTER_BREAKER_THRESHOLD", 2))


def breaker_cooldown_s() -> float:
    return max(1.0, _env_float("JARVIS_VOICE_ROUTER_BREAKER_COOLDOWN_S", 60.0))


def pressure_cooldown_divisor() -> float:
    """Under local memory pressure the breaker re-arms sooner.

    Rationale: an open breaker means every turn runs locally, and local is
    precisely what is starving the audio path. Retrying DW earlier is the
    lesser risk — a wasted probe costs one slow turn, whereas sustained local
    inference under pressure costs capture stutter."""
    return max(1.0, _env_float("JARVIS_VOICE_ROUTER_PRESSURE_DIVISOR", 4.0))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class _Breaker:
    """Three-state breaker (closed / open / half-open-by-expiry).

    Deliberately not a shared utility instance: this one is scoped to the voice
    path, so a DW outage that trips the OP-lane breakers does not silently
    change what the operator hears, and vice versa."""

    failures: int = 0
    opened_at: float = 0.0

    def closed(self, *, now: float, pressured: bool = False) -> bool:
        if self.failures < breaker_threshold():
            return True
        cooldown = breaker_cooldown_s()
        if pressured:
            cooldown /= pressure_cooldown_divisor()
        if (now - self.opened_at) >= cooldown:
            return True          # half-open: one trial turn may pass
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def record_failure(self, *, now: float) -> None:
        self.failures += 1
        if self.failures >= breaker_threshold():
            self.opened_at = now


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class AdaptiveVoiceRouter:
    """A drop-in ``llm_client`` that multiplexes remote and local generation.

    Satisfies the exact contract ``ConversationPipeline`` already calls —
    ``generate_stream(request)`` yielding text chunks — so the pipeline needs
    no knowledge that routing exists. Every other attribute delegates to the
    local engine, so anything else reaching for ``llm_client`` (health checks,
    model introspection) behaves as before.
    """

    def __init__(
        self,
        *,
        local: Any = None,
        dispatch: Optional[Callable[[dict], Any]] = None,
        resolve_model: Optional[Callable[[], Optional[str]]] = None,
        pressure_fn: Optional[Callable[[], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._local = local
        self._dispatch = dispatch
        self._resolve_model = resolve_model
        self._pressure_fn = pressure_fn
        self._clock = clock or time.monotonic
        self._breaker = _Breaker()
        self._lock = asyncio.Lock()
        self.last_route: str = "none"      # observability: who served last turn

    # -- delegation ------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Anything not overridden IS the local engine. Keeps the router a
        true drop-in rather than a narrowing wrapper."""
        local = self.__dict__.get("_local")
        if local is None:
            raise AttributeError(name)
        return getattr(local, name)

    # -- decisions -------------------------------------------------------

    def _local_pressured(self) -> bool:
        """Is local memory too tight to be running inference? Advisory only —
        reuses the existing gate rather than probing memory here."""
        if self._pressure_fn is not None:
            try:
                return bool(self._pressure_fn())
            except Exception:  # noqa: BLE001
                return False
        try:
            from backend.core.ouroboros.governance.memory_pressure_gate import (
                PressureLevel, get_default_gate,
            )
            return get_default_gate().pressure() in (
                PressureLevel.HIGH, PressureLevel.CRITICAL,
            )
        except Exception:  # noqa: BLE001
            return False

    def _elected_model(self) -> Optional[str]:
        """The measured voice, via the lane — never re-derived here."""
        if self._resolve_model is not None:
            try:
                return self._resolve_model()
            except Exception:  # noqa: BLE001
                return None
        try:
            from backend.core.ouroboros.governance.karen_voice_lane import (
                ensure_voice_lane_warm, resolve_voice_model,
            )
            model = resolve_voice_model()
            if model is None:
                # Cold lane: learn in the background, serve this turn locally.
                # Speaking through an UNMEASURED remote model is how a spoken
                # turn ends up on the 22-second code brain.
                ensure_voice_lane_warm()
            return model
        except Exception:  # noqa: BLE001
            return None

    def route_for(self, *, model: Optional[str]) -> str:
        """``remote`` | ``local``. Pure given its inputs, so the decision is
        testable without a network or an engine."""
        if not router_enabled() or not remote_first():
            return "local"
        if not model:
            return "local"
        if not self._breaker.closed(
            now=self._clock(), pressured=self._local_pressured(),
        ):
            return "local"
        return "remote"

    # -- payload ---------------------------------------------------------

    @staticmethod
    def _messages_from(request: Any) -> List[Dict[str, str]]:
        """``ModelRequest`` → OpenAI-shaped messages. Tolerates a bare object
        with the same attributes, so tests need no heavyweight import."""
        msgs: List[Dict[str, str]] = []
        system = getattr(request, "system_prompt", None)
        if system:
            msgs.append({"role": "system", "content": str(system)})
        for m in (getattr(request, "messages", None) or ()):
            try:
                role = str(m.get("role") if isinstance(m, dict) else m.role)
                content = str(
                    m.get("content") if isinstance(m, dict) else m.content
                )
            except Exception:  # noqa: BLE001
                continue
            if role and content:
                msgs.append({"role": role, "content": content})
        return msgs

    def build_remote_payload(self, request: Any, model: str) -> dict:
        """Chat-completions body for the elected voice model.

        The RT service tier is stamped through the canonical
        ``apply_rt_service_tier`` seam — without it DW serves the default async
        tier (~66s TTFT), which would make the elected model's measured 1.0s
        meaningless."""
        body = {
            "model": model,
            "messages": self._messages_from(request),
            "max_tokens": int(getattr(request, "max_tokens", 0) or 512),
            "temperature": float(getattr(request, "temperature", 0.7) or 0.7),
            "stream": True,
        }
        try:
            from backend.core.ouroboros.governance.doubleword_provider import (
                apply_rt_service_tier,
            )
            body = apply_rt_service_tier(body, model)
        except Exception:  # noqa: BLE001 — tier stamping is best-effort
            pass
        return body

    # -- remote stream ---------------------------------------------------

    async def _remote_stream(self, request: Any, model: str) -> AsyncIterator[str]:
        """Stream from DW, token by token. Raises on any fault — the caller
        owns the failover decision, because only it knows whether anything has
        been spoken yet."""
        from backend.core.ouroboros.governance.stream_watchdog import (
            _extract_token,
        )
        dispatch = self._dispatch
        if dispatch is None:
            from backend.core.ouroboros.governance.dw_deep_probe import (
                _default_dw_stream_dispatch,
            )
            dispatch = _default_dw_stream_dispatch

        payload = self.build_remote_payload(request, model)
        dispatched = await dispatch(payload)
        readline, resp = (
            dispatched if isinstance(dispatched, tuple) else (dispatched, None)
        )

        deadline = self._clock() + remote_total_budget_s()
        first = True
        try:
            while True:
                # The FIRST token gets the tight budget — that is the silence
                # the operator actually experiences. Later tokens only have to
                # beat the whole-utterance ceiling.
                timeout = (
                    remote_ttft_budget_s() if first
                    else max(0.1, deadline - self._clock())
                )
                line = await asyncio.wait_for(readline(), timeout=timeout)
                if not line:
                    break
                s = (
                    line.decode("utf-8", "replace")
                    if isinstance(line, (bytes, bytearray)) else str(line)
                ).strip()
                if not s.startswith("data:"):
                    continue
                data = s[5:].strip()
                if data == "[DONE]":
                    break
                token = _extract_token(data)
                if token:
                    first = False
                    yield token
                if self._clock() > deadline:
                    break
        finally:
            # A failover leaves a half-read SSE body behind; without this the
            # socket lingers for the length of the abandoned generation.
            if resp is not None:
                try:
                    from backend.core.ouroboros.governance.stream_watchdog import (
                        fast_abort_response,
                    )
                    fast_abort_response(resp)
                except Exception:  # noqa: BLE001
                    pass

    # -- local stream ----------------------------------------------------

    async def _local_stream(self, request: Any) -> AsyncIterator[str]:
        if self._local is None:
            return
        async for chunk in self._local.generate_stream(request):
            yield chunk

    # -- the contract ----------------------------------------------------

    async def generate_stream(self, request: Any) -> AsyncIterator[str]:
        """Yield response text for one spoken turn. NEVER raises.

        Remote first when a measured voice exists and the breaker is closed;
        local otherwise. A remote fault BEFORE the first token fails over
        silently. A remote fault AFTER it ends the utterance — restarting on
        another engine mid-sentence would have Karen say half of one answer and
        then all of another."""
        model = self._elected_model()
        route = self.route_for(model=model)

        if route == "remote" and model:
            emitted = False
            try:
                async for token in self._remote_stream(request, model):
                    emitted = True
                    yield token
                self._breaker.record_success()
                self.last_route = "remote"
                return
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                self._breaker.record_failure(now=self._clock())
                if emitted:
                    # Mid-utterance: stop cleanly. The operator hears a short
                    # answer, not two spliced ones.
                    self.last_route = "remote_truncated"
                    logger.warning(
                        "[VoiceRouter] remote fault after first token "
                        "(%s) — ending utterance", type(exc).__name__,
                    )
                    return
                logger.info(
                    "[VoiceRouter] remote %s failed (%s) — local fallback",
                    model, type(exc).__name__,
                )

        # Local: the fallback, and the only path when nothing was elected.
        if self._local is None:
            self.last_route = "none"
            return
        try:
            async for chunk in self._local_stream(request):
                yield chunk
            self.last_route = "local"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Both engines gone. Karen stays quiet rather than crashing the
            # pipeline: ConversationPipeline treats an empty stream as a failed
            # turn and recovers, whereas an exception here would take the FSM
            # down mid-conversation.
            self.last_route = "failed"
            logger.error("[VoiceRouter] local fallback failed: %r", exc)

    # -- observability ---------------------------------------------------

    def status(self) -> Dict[str, Any]:
        model = None
        try:
            model = self._elected_model()
        except Exception:  # noqa: BLE001
            pass
        return {
            "enabled": router_enabled(),
            "remote_first": remote_first(),
            "elected_model": model,
            "route_next": self.route_for(model=model),
            "last_route": self.last_route,
            "breaker_failures": self._breaker.failures,
            "breaker_open": not self._breaker.closed(now=self._clock()),
            "local_pressured": self._local_pressured(),
            "has_local": self._local is not None,
        }


def build_voice_router(local: Any = None, **kwargs: Any) -> Any:
    """Wrap *local* in a router, or hand it back untouched.

    Returning the bare engine when the router is disabled keeps the OFF path
    byte-identical to the pre-router pipeline — no wrapper, no delegation, no
    behaviour to audit. NEVER raises: a router that cannot be built must not
    cost the operator their voice."""
    try:
        if not router_enabled():
            return local
        return AdaptiveVoiceRouter(local=local, **kwargs)
    except Exception:  # noqa: BLE001
        logger.debug("[VoiceRouter] build degraded", exc_info=True)
        return local


__all__ = [
    "AdaptiveVoiceRouter",
    "build_voice_router",
    "remote_first",
    "remote_ttft_budget_s",
    "router_enabled",
]
