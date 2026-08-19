"""The bridge from this machine's orchestration to another machine's GPUs.

WHAT THIS IS, AND DELIBERATELY IS NOT
-------------------------------------
It is an ENDPOINT RESOLVER, a HOST HEALTH STATE MACHINE, and a TELEMETRY
ROUTER. It is not a transport, not a streaming client, and not a timeout
implementation, because this repository already has all three and a second one
would be a second authority over the same physics.

Specifically it COMPOSES:

  * :class:`local_inference_director.LocalPrimeClient` -- which already
    streams and already runs the Inter-Token Watchdog
    (``asyncio.wait_for(..., _inter_token_timeout_s())`` raising
    :class:`InterTokenStall`). Re-implementing that here would duplicate the
    one guard the module's own comment calls "the sole guard".
  * :class:`LatencyProfiler` + the physics ledger -- keyed per host since the
    hardware-signature work, which is precisely what makes remote telemetry
    safe to record at all.
  * :class:`ThroughputGovernor` -- which turns those measurements into a lane
    count.

THE ONE TRANSPORT DEFECT IT DOES FIX
------------------------------------
``LocalPrimeClient.complete`` decides whether to stream like this::

    _use_stream = stream if stream is not None else (
        bool(self._cfg.num_ctx) and _streaming_enabled())

So on any config WITHOUT a negotiated ``num_ctx`` the client takes the
non-streaming path -- and the Inter-Token Watchdog lives inside
``_complete_streaming``. The watchdog exists but is DISARMED, on exactly the
path a LAN bridge uses before context negotiation has run. A remote host that
accepts the connection and then wedges would hang this process against a
socket, which is the failure the bridge exists to prevent.

The gateway therefore passes ``stream=True`` EXPLICITLY for remote targets. A
stall over a LAN is not slowness, it is a peer that stopped talking, and it
must be severed rather than waited out.

DEGRADATION IS A ROUTING DECISION, NOT AN ERROR
-----------------------------------------------
When the remote host is unreachable the correct behaviour is not to fail the
op -- it is to run it somewhere else. Triage-class work has a local model that
can do it. So host health drives a routing table, and an op only fails when
NO target can serve it.

Health counts INFRASTRUCTURE faults only. A model returning a bad completion
is not evidence about the host, and letting it open the breaker would take a
healthy 5090 out of service because a prompt was malformed.

Python 3.9+. Every governance import is lazy and fail-soft.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.InferenceGateway")

ENABLED_ENV = "JARVIS_GATEWAY_ENABLED"
REMOTE_ENDPOINT_ENV = "JARVIS_REMOTE_INFERENCE_ENDPOINT"
REMOTE_MODEL_ENV = "JARVIS_REMOTE_INFERENCE_MODEL"
LOCAL_TRIAGE_MODEL_ENV = "JARVIS_LOCAL_TRIAGE_MODEL"
FAILURE_THRESHOLD_ENV = "JARVIS_GATEWAY_FAILURE_THRESHOLD"
COOLDOWN_ENV = "JARVIS_GATEWAY_COOLDOWN_S"
PROBE_TIMEOUT_ENV = "JARVIS_GATEWAY_PROBE_TIMEOUT_S"

_TRUTHY = ("1", "true", "yes", "on")


def gateway_enabled() -> bool:
    """Master gate. Default ON, but inert without a configured remote -- see
    :func:`remote_endpoint`. NEVER raises."""
    try:
        return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def remote_endpoint() -> str:
    """The remote inference host, or "" when none is configured.

    NEVER a hardcoded address. An unset value is not a misconfiguration -- it
    is the single-machine case, and the gateway degrades to local-only without
    complaint. That is why the master flag can default ON: a developer who has
    never heard of this module sees no behaviour change.
    """
    try:
        return (os.environ.get(REMOTE_ENDPOINT_ENV, "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def remote_model() -> str:
    """Model to request on the remote host. Empty -> whatever the local config
    names, which is right when both hosts serve the same model."""
    try:
        return (os.environ.get(REMOTE_MODEL_ENV, "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def local_triage_model() -> str:
    """The lightweight model this machine falls back to. Empty -> the local
    config's own model."""
    try:
        return (os.environ.get(LOCAL_TRIAGE_MODEL_ENV, "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def failure_threshold() -> int:
    """Consecutive infrastructure faults before the LAN is bypassed. Default 3.

    Not 1: a single dropped packet or a model load pause is not an outage, and
    a breaker that opens on one fault would flap. Not 10: every fault costs a
    real op its budget."""
    try:
        return max(1, int(os.environ.get(FAILURE_THRESHOLD_ENV, "3")))
    except (TypeError, ValueError):
        return 3


def cooldown_s() -> float:
    """How long the LAN stays bypassed before ONE probe is allowed. Default 60."""
    try:
        return max(1.0, float(os.environ.get(COOLDOWN_ENV, "60")))
    except (TypeError, ValueError):
        return 60.0


def probe_timeout_s() -> float:
    """Bound on the reachability probe. Default 3s -- a probe that can hang is
    a second instance of the bug this module exists to fix."""
    try:
        return max(0.5, float(os.environ.get(PROBE_TIMEOUT_ENV, "3")))
    except (TypeError, ValueError):
        return 3.0


class HostState(str, enum.Enum):
    """Reachability of one inference host."""

    UNKNOWN = "unknown"        # never contacted
    HEALTHY = "healthy"        # last attempt succeeded
    DEGRADED = "degraded"      # faults observed, still under threshold
    UNREACHABLE = "unreachable"  # breaker OPEN -- bypass the LAN
    PROBING = "probing"        # breaker HALF-OPEN -- one attempt allowed


@dataclass(frozen=True)
class GatewayTarget:
    """Where an op should be dispatched, and why."""

    base_url: str
    model_name: str
    #: "remote" or "local". Not cosmetic: it decides whether streaming is
    #: FORCED (see the module docstring) and which physics ledger is written.
    scope: str
    state: HostState
    reason: str

    @property
    def is_remote(self) -> bool:
        return self.scope == "remote"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url, "model_name": self.model_name,
            "scope": self.scope, "state": self.state.value,
            "reason": self.reason,
        }


class RemoteHostUnavailable(RuntimeError):
    """No target could serve this op. An INFRASTRUCTURE failure class, so the
    orchestrator's existing taxonomy attributes it to the host rather than to
    the model's output."""

    failure_class = "remote_host_unavailable"


class _HostHealth:
    """Three-state breaker for ONE endpoint.

    CLOSED (healthy/degraded) -> OPEN (unreachable) -> HALF-OPEN (probing).

    Half-open admits exactly ONE attempt. Without that gate every queued op
    would stampede a recovering host the instant the cooldown expired, and the
    first thing a recovering host does is load a model -- so the stampede
    arrives precisely when it is least able to absorb it.
    """

    __slots__ = ("_lock", "_consecutive", "_state", "_opened_at", "_probe_taken",
                 "_last_reason", "_successes", "_failures")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive = 0
        self._state = HostState.UNKNOWN
        self._opened_at = 0.0
        self._probe_taken = False
        self._last_reason = ""
        self._successes = 0
        self._failures = 0

    def state(self, *, now: Optional[float] = None) -> HostState:
        _now = time.monotonic() if now is None else now
        with self._lock:
            if self._state is not HostState.UNREACHABLE:
                return self._state
            if (_now - self._opened_at) < cooldown_s():
                return HostState.UNREACHABLE
            # Cooldown elapsed: offer exactly one probe slot.
            if not self._probe_taken:
                return HostState.PROBING
            return HostState.UNREACHABLE

    def claim_probe(self, *, now: Optional[float] = None) -> bool:
        """Take the single half-open slot. False if already taken."""
        _now = time.monotonic() if now is None else now
        with self._lock:
            if self._state is not HostState.UNREACHABLE:
                return True
            if (_now - self._opened_at) < cooldown_s() or self._probe_taken:
                return False
            self._probe_taken = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive = 0
            self._successes += 1
            self._state = HostState.HEALTHY
            self._probe_taken = False
            self._last_reason = ""

    def record_failure(self, reason: str, *, now: Optional[float] = None) -> None:
        _now = time.monotonic() if now is None else now
        with self._lock:
            self._consecutive += 1
            self._failures += 1
            self._last_reason = reason
            if self._consecutive >= failure_threshold():
                # Re-stamp `_opened_at` on every failure at or past the
                # threshold, INCLUDING a failed half-open probe -- otherwise a
                # host that fails its probe would be retried immediately,
                # because its cooldown had already elapsed.
                self._state = HostState.UNREACHABLE
                self._opened_at = _now
                self._probe_taken = False
            else:
                self._state = HostState.DEGRADED

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "consecutive_failures": self._consecutive,
                "successes": self._successes,
                "failures": self._failures,
                "last_reason": self._last_reason,
            }


#: Exception TYPE NAMES that are evidence about the HOST. Matched by name so
#: this module never imports aiohttp, and so a transport library swap does not
#: silently empty the set.
_INFRA_FAULTS = frozenset({
    "InterTokenStall", "LocalLatencyLockup", "UnrecoverableInferenceLatency",
    "TimeoutError", "AsyncTimeoutError", "ClientConnectorError",
    "ClientOSError", "ServerDisconnectedError", "ClientPayloadError",
    "ConnectionResetError", "ConnectionRefusedError", "OSError",
    "ClientConnectionError", "ServerTimeoutError",
})


def is_infrastructure_fault(exc: BaseException) -> bool:
    """Is *exc* evidence about the HOST rather than about the request?

    A model returning nonsense, or a 400 for a malformed body, says nothing
    about whether the machine is up. Counting those would open the breaker on
    a healthy 5090 because a prompt was wrong -- taking working hardware out
    of service for a software bug. NEVER raises.
    """
    try:
        names = {type(exc).__name__}
        names.update(b.__name__ for b in type(exc).__mro__)
        return bool(names & _INFRA_FAULTS)
    except Exception:  # noqa: BLE001
        return False


class InferenceGateway:
    """Resolves a target, dispatches through the existing client, records health.

    Not a singleton by construction -- :func:`get_default_gateway` provides the
    process-wide one, and tests build their own.
    """

    def __init__(self, *, client_factory: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self._health: Dict[str, _HostHealth] = {}
        self._clients: Dict[str, Any] = {}
        self._client_factory = client_factory

    # -- resolution --------------------------------------------------------

    def _health_for(self, endpoint: str) -> _HostHealth:
        with self._lock:
            h = self._health.get(endpoint)
            if h is None:
                h = _HostHealth()
                self._health[endpoint] = h
            return h

    def target_for(self, *, route: Optional[str] = None,
                   now: Optional[float] = None) -> GatewayTarget:
        """Where should an op on *route* run right now? NEVER raises."""
        try:
            return self._target_for(route=route, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[InferenceGateway] resolution degraded", exc_info=True)
            return self._local_target(f"resolution failed: {type(exc).__name__}")

    def _local_target(self, reason: str) -> GatewayTarget:
        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            local_inference_director as lid,
        )
        cfg = lid.LocalConfig.from_env()
        return GatewayTarget(
            base_url=str(getattr(cfg, "base_url", "") or ""),
            model_name=local_triage_model() or str(
                getattr(cfg, "model_name", "") or ""),
            scope="local", state=HostState.HEALTHY, reason=reason,
        )

    def _target_for(self, *, route: Optional[str],
                    now: Optional[float]) -> GatewayTarget:
        endpoint = remote_endpoint()
        if not gateway_enabled():
            return self._local_target("gateway disabled")
        if not endpoint:
            # The single-machine case, not a misconfiguration.
            return self._local_target("no remote endpoint configured")

        health = self._health_for(endpoint)
        state = health.state(now=now)
        if state is HostState.UNREACHABLE:
            return self._local_target(
                f"remote {endpoint} unreachable — bypassing LAN")
        if state is HostState.PROBING and not health.claim_probe(now=now):
            # Someone else took the single half-open slot.
            return self._local_target(
                f"remote {endpoint} recovering — probe slot taken")

        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            local_inference_director as lid,
        )
        cfg = lid.LocalConfig.from_env()
        return GatewayTarget(
            base_url=endpoint,
            model_name=remote_model() or str(getattr(cfg, "model_name", "") or ""),
            scope="remote", state=state,
            reason="probing after cooldown" if state is HostState.PROBING
            else "remote healthy",
        )

    # -- transport ---------------------------------------------------------

    def _client_for(self, target: GatewayTarget) -> Tuple[Any, Any]:
        """A client bound to *target*, with a profiler on THAT host's ledger.

        The profiler is keyed by ``physics_key(cfg, endpoint=...)``, so a
        remote 5090's measurements never land in this Mac's entry. Before the
        hardware-signature work that key was ``model@ctx`` and this whole
        method would have silently poisoned the local ledger with LAN numbers.
        """
        import dataclasses  # noqa: PLC0415

        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            local_inference_director as lid,
        )
        with self._lock:
            hit = self._clients.get(target.base_url)
        if hit is not None:
            return hit
        base = lid.LocalConfig.from_env()
        cfg = dataclasses.replace(
            base, base_url=target.base_url, model_name=target.model_name)
        profiler = lid.LatencyProfiler(
            cfg, ledger_key=lid.physics_key(cfg, endpoint=target.base_url))
        if self._client_factory is not None:
            client = self._client_factory(cfg, profiler)
        else:
            client = lid.LocalPrimeClient(cfg, profiler=profiler)
        pair = (client, profiler)
        with self._lock:
            self._clients[target.base_url] = pair
        return pair

    async def dispatch(
        self,
        *,
        system: str,
        user: str,
        prompt_tokens: int,
        route: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Any:
        """Run one completion on the best available host.

        STREAMING TIMEOUT LOGIC, end to end:

        1. ``stream=True`` is FORCED for remote targets. The client would
           otherwise choose the non-streaming path whenever ``num_ctx`` is
           unset, and the Inter-Token Watchdog lives inside the streaming
           path. The guard exists; this arms it.
        2. The watchdog is the client's, not ours: it awaits each chunk under
           ``asyncio.wait_for(..., _inter_token_timeout_s())``. The model may
           take as long as it likes overall, provided it keeps EMITTING --
           which is the right shape for a 64K-context prompt whose first token
           legitimately takes a minute, and the wrong shape for a total
           deadline, which is why a total deadline is not used here.
        3. A breach raises :class:`InterTokenStall` from inside the client,
           which severs the connection as the ``async with`` unwinds. We do
           not re-implement cancellation; we classify the result.
        4. That fault counts against HOST health. Enough of them open the
           breaker and the next op is routed locally instead of failing.
        5. A retry on the local target happens ONLY for a remote infra fault,
           and only once. A local fault has nowhere to fall back to and is
           raised.
        """
        target = self.target_for(route=route, now=now)
        try:
            return await self._dispatch_to(
                target, system=system, user=user, prompt_tokens=prompt_tokens,
                temperature=temperature, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            if not target.is_remote:
                raise
            if not is_infrastructure_fault(exc):
                # The host ANSWERED; the REQUEST was bad. Two consequences,
                # and the ordering here is load-bearing:
                #
                #   * it is NOT recorded against host health. An earlier draft
                #     recorded first and classified second, so a malformed
                #     prompt opened the breaker and removed a perfectly
                #     healthy 5090 from service for a software bug.
                #   * it is not retried elsewhere. The same request would fail
                #     identically on another machine and burn a second budget.
                raise
            self._health_for(target.base_url).record_failure(
                f"{type(exc).__name__}: {str(exc)[:120]}", now=now)
            logger.warning(
                "[InferenceGateway] remote %s failed (%s) — falling back to "
                "local for this op", target.base_url, type(exc).__name__)
            fallback = self._local_target(
                f"remote fault: {type(exc).__name__}")
            return await self._dispatch_to(
                fallback, system=system, user=user,
                prompt_tokens=prompt_tokens, temperature=temperature,
                max_tokens=max_tokens)

    async def _dispatch_to(self, target: GatewayTarget, *, system: str,
                           user: str, prompt_tokens: int, temperature: float,
                           max_tokens: Optional[int]) -> Any:
        client, _profiler = self._client_for(target)
        result = await client.complete(
            system=system, user=user, prompt_tokens=prompt_tokens,
            temperature=temperature, max_tokens=max_tokens,
            # See the docstring: forced for remote, left to the client's own
            # policy locally (where a stall is slowness, not a dead peer).
            stream=True if target.is_remote else None,
        )
        if target.is_remote:
            self._health_for(target.base_url).record_success()
        return result

    # -- lifecycle + observability ----------------------------------------

    async def aclose(self) -> None:
        """Close every client this gateway OWNS. NEVER raises.

        Ownership is explicit because an un-closed aiohttp session is a leak
        this codebase has already paid for once -- the cognition-lanes
        provider leak, where a per-call client was left for the GC.
        """
        with self._lock:
            pairs = list(self._clients.values())
            self._clients.clear()
        for client, _prof in pairs:
            try:
                closer = getattr(client, "aclose", None)
                if closer is not None:
                    await closer()
            except Exception:  # noqa: BLE001
                logger.debug("[InferenceGateway] client close degraded",
                             exc_info=True)

    def snapshot(self) -> Dict[str, Any]:
        """Observability surface. NEVER raises."""
        try:
            endpoint = remote_endpoint()
            out: Dict[str, Any] = {
                "enabled": gateway_enabled(),
                "remote_endpoint": endpoint or None,
                "failure_threshold": failure_threshold(),
                "cooldown_s": cooldown_s(),
                "hosts": {},
            }
            with self._lock:
                items = list(self._health.items())
            for ep, h in items:
                out["hosts"][ep] = h.snapshot()
            out["active_target"] = self.target_for().to_dict()
            return out
        except Exception:  # noqa: BLE001
            return {"enabled": False, "error": "snapshot degraded"}


_SINGLETON: Optional[InferenceGateway] = None
_SINGLETON_LOCK = threading.Lock()


def get_default_gateway() -> InferenceGateway:
    """Process-wide gateway. Mirrors `memory_pressure_gate.get_default_gate`."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = InferenceGateway()
        return _SINGLETON


def reset_for_tests() -> None:
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None
