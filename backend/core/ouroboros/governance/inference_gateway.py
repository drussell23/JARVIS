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
PREFLIGHT_ENV = "JARVIS_GATEWAY_PREFLIGHT_ENABLED"
RESIDENCY_TTL_ENV = "JARVIS_GATEWAY_RESIDENCY_TTL_S"
SWAP_BUDGET_ENV = "JARVIS_GATEWAY_WARM_SWAP_BUDGET_S"

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


def preflight_enabled() -> bool:
    """Master gate for the residency pre-flight. Default ON. NEVER raises."""
    try:
        return os.environ.get(PREFLIGHT_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def residency_ttl_s() -> float:
    """How long a residency reading stays fresh. Default 30s.

    Short, because residency is exactly the thing that changes underneath us:
    another client can request a different model and evict ours. Long enough
    that a burst of ops does not issue a probe each."""
    try:
        return max(0.0, float(os.environ.get(RESIDENCY_TTL_ENV, "30")))
    except (TypeError, ValueError):
        return 30.0


def warm_swap_budget_s() -> float:
    """Wall-clock allowed for a cold load. Default 180s.

    Generous ON PURPOSE and NOT derived from a route budget: this is the
    cost of moving ~19GB across PCIe into VRAM, which has nothing to do with
    how urgent the op is. Measured ~30s for a 1.9GB model on Apple Silicon;
    a 30B on a discrete card is meaningfully longer. The whole point of
    paying it here is that it is paid OUTSIDE the op's generation clock."""
    try:
        return max(1.0, float(os.environ.get(SWAP_BUDGET_ENV, "180")))
    except (TypeError, ValueError):
        return 180.0


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
        self._clients: Dict[Tuple[str, str], Any] = {}
        #: (base_url, model_name) -> (client, profiler). Keyed by BOTH,
        #: because a client is BOUND to a model: its cfg carries model_name
        #: and its profiler is keyed by a physics_key that includes the model.
        #: Keying on base_url alone silently served every later op with
        #: whichever model was requested FIRST -- see the cache-identity note
        #: on `_client_for`.
        #: base_url -> (monotonic_at, resident_models_or_None). None is
        #: "unknowable", which is NOT the same as an empty tuple.
        self._residency: Dict[str, Tuple[float, Optional[Tuple[str, ...]]]] = {}
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
        # CACHE IDENTITY = (endpoint, model). Not the endpoint alone.
        #
        # Found by live-fire, not by 300+ unit tests, because every unit test
        # used one model per gateway. After a warm swap to a second model on
        # the SAME host, an endpoint-only key returned the first model's
        # client for every subsequent dispatch -- so the requested model was
        # silently ignored and its telemetry landed under the wrong physics
        # key. On the deployment this is built for that is: one vision
        # one-off swaps to the 27B, and every BACKGROUND op afterwards
        # quietly runs on it at a third of the throughput, blowing budgets
        # with no error anywhere.
        cache_key = (target.base_url, target.model_name)
        with self._lock:
            hit = self._clients.get(cache_key)
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
            self._clients[cache_key] = pair
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
        if target.is_remote:
            # Pre-flight OUTSIDE the try: a warm swap is not a dispatch
            # attempt, so a slow load must not be classified as a host fault
            # and must not open the breaker.
            await self.ensure_model_resident(target)
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
            self._publish_degraded(target, exc)
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

    # -- pre-flight residency ---------------------------------------------

    async def resident_models(self, base_url: str) -> Optional[Tuple[str, ...]]:
        """Models the remote currently holds IN VRAM, or None if unknowable.

        ``/api/ps`` answers "what is loaded RIGHT NOW", which is a different
        question from ``/api/tags`` ("what is installed") -- the latter is
        already memoised elsewhere in this codebase and would answer the wrong
        question here: a model can be installed and not resident, which is
        precisely the case a warm swap exists to handle.

        None is returned for "could not determine", NEVER an empty tuple. A
        server that does not implement ``/api/ps`` (vLLM, an older ollama)
        must not be read as "nothing is loaded" -- that would make every op
        trigger a needless warm swap. Empty tuple means the endpoint answered
        and genuinely holds nothing.

        Bounded by :func:`probe_timeout_s`; a probe that can hang is a second
        instance of the bug this whole module exists to prevent. NEVER raises.
        """
        try:
            import aiohttp  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None
        url = base_url.rstrip("/") + "/api/ps"
        _t0 = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=probe_timeout_s())
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json(content_type=None)
        except Exception:  # noqa: BLE001 — unreachable, unparseable, absent
            return None
        # THE PROBE IS ALREADY A ROUND TRIP — so it is already an RTT sample,
        # and taking it costs nothing. This seeds the transport profile BEFORE
        # the first stream, so the very first generation is bounded by a
        # deadline that already knows whether the host is 0.4ms away or 90ms
        # away. Without it the first stream on a relayed link would be judged
        # by a LAN-derived budget, which is precisely the false-positive this
        # whole mechanism exists to prevent.
        try:
            from backend.core.ouroboros.governance.transport_profile import (  # noqa: PLC0415,E501
                profile_for as _tprofile,
            )
            _tprofile(base_url).observe((time.monotonic() - _t0) * 1000.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            rows = payload.get("models") or payload.get("data") or []
            names = tuple(
                str(r.get("name") or r.get("model") or "").strip()
                for r in rows if isinstance(r, dict)
            )
            return tuple(n for n in names if n)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _model_matches(wanted: str, resident: Tuple[str, ...]) -> bool:
        """Is *wanted* among *resident*? Tolerates ollama's tag conventions.

        ``qwen3-coder:30b`` and ``qwen3-coder:30b-instruct-q4_K_M`` are the
        same weights to an operator and different strings to a registry, and
        a bare ``qwen3-coder`` is reported as ``qwen3-coder:latest``. An
        over-strict comparison would warm-swap a model that is already
        resident -- burning a cold load to load what is loaded.
        """
        w = (wanted or "").strip().lower()
        if not w:
            return True          # nothing requested -> nothing to verify
        w_base = w.split(":", 1)[0]
        for r in resident:
            r = r.strip().lower()
            if r == w or r.startswith(w + "-"):
                return True
            if r.split(":", 1)[0] == w_base and (":" not in w or w.split(":", 1)[1] in r):
                return True
        return False

    async def ensure_model_resident(self, target: "GatewayTarget") -> Dict[str, Any]:
        """Pre-flight: verify the target model is loaded, warm-swapping if not.

        WHY THIS EXISTS. Ollama loads a model on first use. If the resident
        model is the 30B coder and an op asks for the 27B vision model, that
        first request pays a multi-second-to-multi-minute cold load INSIDE its
        own generation window -- and the route budget was calibrated for
        generation, not for PCIe transfer. The op then fails on a deadline
        that had nothing to do with the model's speed, which is the most
        misleading failure this tier can produce.

        So the swap is performed as an explicit handshake, BEFORE the op's
        clock starts, against a budget of its own (:func:`warm_swap_budget_s`)
        rather than against the route's.

        Composes the EXISTING ``LocalPrimeClient.warmup()``, which already
        forces weights into VRAM with a dedicated cold-start HTTP context.
        Re-implementing it here would be a second cold-load path.

        NEVER raises. Returns a small report for observability.
        """
        out: Dict[str, Any] = {"checked": False, "swapped": False,
                               "resident": None, "reason": ""}
        if not preflight_enabled():
            out["reason"] = "preflight disabled"
            return out
        try:
            now = time.monotonic()
            with self._lock:
                cached = self._residency.get(target.base_url)
            if cached is not None and (now - cached[0]) < residency_ttl_s():
                resident = cached[1]
            else:
                resident = await self.resident_models(target.base_url)
                with self._lock:
                    self._residency[target.base_url] = (now, resident)
            out["checked"] = True
            out["resident"] = list(resident) if resident is not None else None

            if resident is None:
                # Unknowable, not empty. Dispatch anyway: the client's own
                # inter-token watchdog still bounds a slow first token, and
                # swapping on every op would be worse than the problem.
                out["reason"] = "residency unknown — dispatching without a swap"
                return out
            if self._model_matches(target.model_name, resident):
                out["reason"] = "already resident"
                return out

            logger.info(
                "[InferenceGateway] warm swap: %s is resident, op needs %s — "
                "loading before the op clock starts (budget %.0fs)",
                ", ".join(resident) or "<none>", target.model_name,
                warm_swap_budget_s(),
            )
            client, _prof = self._client_for(target)
            ok = await client.warmup(timeout_s=warm_swap_budget_s())
            out["swapped"] = bool(ok)
            out["reason"] = "warm swap completed" if ok else (
                "warm swap did not confirm — dispatching anyway")
            # The reading is now stale either way.
            with self._lock:
                self._residency.pop(target.base_url, None)
            return out
        except Exception as exc:  # noqa: BLE001 — pre-flight must never block dispatch
            out["reason"] = f"preflight degraded: {type(exc).__name__}"
            return out

    def _publish_degraded(self, target: "GatewayTarget",
                          exc: BaseException) -> None:
        """Surface a NON-FATAL network degradation on the existing SSE channel.

        Reuses ``provider_state_changed`` -- already the DEGRADED<->HEALTHY
        signal in this codebase -- rather than minting a new event type. New
        types must be added to the canonical ``_VALID_EVENT_TYPES`` frozenset
        or they are SILENTLY DROPPED, so inventing one here would produce a
        degradation signal that degrades silently.

        Best-effort by construction: the op has already been re-routed to the
        local target by the time this runs, so a failure to publish must not
        turn a handled degradation into an unhandled one. NEVER raises.
        """
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: PLC0415,E501
                publish_provider_state_changed,
            )
            publish_provider_state_changed({
                "provider": "local_tier_remote",
                # The SAME identity the breaker, the residency cache and the
                # client cache are keyed on. A prettier derived host would be
                # a second identity for one endpoint, and an operator could no
                # longer line this event up with `_health_for(base_url)`.
                "endpoint": target.base_url,
                "state": self._health_for(target.base_url).state().value,
                "model": target.model_name,
                "failure_class": getattr(exc, "failure_class",
                                         type(exc).__name__),
                "error": str(exc)[:200],
                "fallback": "local_triage",
                "fatal": False,
            })
        except Exception:  # noqa: BLE001
            logger.debug("[InferenceGateway] degradation publish failed",
                         exc_info=True)

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
