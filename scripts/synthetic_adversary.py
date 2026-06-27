"""synthetic_adversary.py -- deterministic provider chaos proxy for the Isomorphic
Local Sandbox (Task 3).

PURPOSE
-------
A localhost aiohttp server the harness points providers at via env-URL-swap.
Injects deterministic, programmable chaos per a timeline so failover-trigger
wiring can be exercised in milliseconds for $0 instead of cloud discovery runs.

REUSE (per plan spec -- no duplication)
-----------------------------------------
* FakeClock  (scripts/chaos_injector.py:76)    -- injectable deterministic clock,
  no real sleeps; the adversary reads time ONLY through the injected clock_fn.
* FaultInjector + FaultType  (tests/adversarial/fault_injector.py:56)  -- used
  as boundary-based per-request fault dispatch registry; FailureSource values are
  stored in FaultSpec.params["failure_source"] and consumed via check().
* FailureSource  (topology_sentinel.py:429)    -- the SINGLE source of truth for
  the DW HTTP failure taxonomy; no parallel enum invented here.

ARCHITECTURE
------------
One aiohttp server on a random localhost port, three route prefixes:
  /dw/*      -- DoubleWord (DOUBLEWORD_BASE_URL / JARVIS_AEGIS_URL)
  /prime/*   -- J-Prime  (JARVIS_PRIME_URL)
  /reactor/* -- Reactor  (JARVIS_REACTOR_URL / REACTOR_CORE_API_URL)

Per (route, endpoint) fault schedule: list of _ScheduledFault entries checked
against the FakeClock.  The /dw/models HeavyProbe path and the
/dw/chat/completions generation path are INDEPENDENTLY controllable (by design --
that is exactly the run-#11 condition Task 4 needs to reproduce).

Usage
-----
    from scripts.synthetic_adversary import SyntheticAdversary
    from scripts.chaos_injector import FakeClock
    from backend.core.ouroboros.governance.topology_sentinel import FailureSource

    clock = FakeClock(start=0.0)
    adv = SyntheticAdversary(clock=clock)
    adv.schedule(route="doubleword", endpoint="/chat/completions",
                 fault=FailureSource.LIVE_HTTP_5XX, at=0.0)
    urls = await adv.start()   # {"doubleword": "http://127.0.0.1:PORT/dw", ...}
    os.environ.update(adv.env_overrides())  # {DOUBLEWORD_BASE_URL: ..., ...}
    # ... run providers ...
    await adv.stop()
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Union

# Repo-root bootstrap so the module works both as a script and as an import.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import aiohttp.web  # noqa: E402  (already a dep per plan spec)

# ── reused modules (do NOT duplicate) ──────────────────────────────────────── #
from scripts.chaos_injector import FakeClock  # noqa: E402
from tests.adversarial.fault_injector import (  # noqa: E402
    FaultInjector,
    FaultSpec,
    FaultType,
)

try:
    from backend.core.ouroboros.governance.topology_sentinel import FailureSource
except ImportError:  # fail-soft: tests that mock the import still work
    FailureSource = None  # type: ignore[assignment,misc]

# ──────────────────────────────────────────────────────────────────────────── #

_log = logging.getLogger(__name__)

# How long (seconds) the LIVE_STREAM_STALL handler holds the SSE connection open.
# Set JARVIS_ADVERSARY_STALL_S to a small value in tests to avoid blocking.
_STALL_S: float = float(os.environ.get("JARVIS_ADVERSARY_STALL_S", "30.0"))

# Healthy stub values
_HEALTHY_MODEL_ID = "adversary-stub-model"
_HEALTHY_CHAT_CONTENT = "SyntheticAdversary healthy stub response."
_HEALTHY_COMPLETION_ID = "chatcmpl-adversary-ok"

# Mapping: FailureSource value → nearest FaultType for FaultInjector registration.
# Used only for the FaultInjector boundary-dispatch record; HTTP behaviour is
# driven by the original FailureSource string.
_FS_TO_FT: Dict[str, FaultType] = {
    "live_transport": FaultType.NETWORK_PARTITION,
    "live_http_5xx": FaultType.NETWORK_PARTITION,
    "live_http_429": FaultType.NETWORK_PARTITION,
    "live_parse_error": FaultType.DELAYED_DUPLICATE,
    "live_stream_stall": FaultType.TIMEOUT_AFTER_SUCCESS,
    "heavy_probe_fail": FaultType.NETWORK_PARTITION,
    "light_probe_fail": FaultType.NETWORK_PARTITION,
    "light_probe_timeout": FaultType.TIMEOUT_AFTER_SUCCESS,
    "generation_timeout": FaultType.TIMEOUT_AFTER_SUCCESS,
    "fsm_exhausted": FaultType.NETWORK_PARTITION,
    "local_egress_overweight": FaultType.NETWORK_PARTITION,
}


def _fault_str(fault: Any) -> str:
    """Normalise a FailureSource (or string) to its string value."""
    if hasattr(fault, "value"):
        return str(fault.value)
    return str(fault)


@dataclasses.dataclass
class _ScheduledFault:
    """One scheduled fault for a specific (route, endpoint) pair."""

    route: str        # "doubleword" | "prime" | "reactor"
    endpoint: str     # "/chat/completions" | "/models" | ...
    fault: Any        # FailureSource instance (or string matching its value)
    at: float         # FakeClock time >= at → fault is active
    remaining: Optional[int]  # None = infinite; decremented on each use


class SyntheticAdversary:
    """Localhost aiohttp proxy that injects deterministic provider chaos.

    The /models (HeavyProbe) path and /chat/completions (real-generation) path
    are **independently** controllable via schedule(): each (route, endpoint)
    tuple has its own fault list so probe-healthy + generation-failing is trivial
    to reproduce (the exact run-#11 condition).

    Time is controlled by the injected FakeClock (no real sleeps in fault logic).
    Fault dispatch is routed through FaultInjector (fault_injector.py) so the
    boundary-level record/check mechanism is exercised.
    """

    def __init__(self, *, clock: Optional[FakeClock] = None) -> None:
        # FakeClock (chaos_injector.py:76) -- injectable, no real sleeps
        self._clock: FakeClock = clock if clock is not None else FakeClock()
        self._faults: List[_ScheduledFault] = []
        # FaultInjector (fault_injector.py:56) -- per-request boundary dispatch
        self._injector: FaultInjector = FaultInjector(seed=0)
        self._app: Optional[aiohttp.web.Application] = None
        self._runner: Optional[aiohttp.web.AppRunner] = None
        self._site: Optional[aiohttp.web.TCPSite] = None
        self._port: Optional[int] = None

    # ── public API ──────────────────────────────────────────────────────────── #

    def schedule(
        self,
        *,
        route: str,
        endpoint: str,
        fault: Any,       # FailureSource | str  (FailureSource is the SoT)
        at: float = 0.0,
        count: Optional[int] = None,
    ) -> None:
        """Schedule a deterministic fault for (route, endpoint).

        Args:
            route:    Provider name  -- "doubleword" | "prime" | "reactor".
            endpoint: Path suffix    -- "/chat/completions" | "/models" | ...
            fault:    FailureSource  -- LIVE_TRANSPORT / LIVE_HTTP_5XX /
                      LIVE_HTTP_429 / LIVE_PARSE_ERROR / LIVE_STREAM_STALL
                      (or any other FailureSource value; string accepted too).
            at:       FakeClock time when fault activates (default 0 = instant).
            count:    Max injections; None = unlimited.  After count is exhausted
                      the slot is skipped and subsequent requests see healthy.
        """
        entry = _ScheduledFault(
            route=route,
            endpoint=endpoint,
            fault=fault,
            at=float(at),
            remaining=count,
        )
        self._faults.append(entry)
        _log.debug(
            "[SyntheticAdversary] scheduled %s@%s=%s at=%.1f count=%s",
            route, endpoint, _fault_str(fault), at, count,
        )

    async def start(self) -> Dict[str, str]:
        """Start the adversary server on a random localhost port.

        Returns a URL dict for env-swap:
          {"doubleword": "http://127.0.0.1:PORT/dw",
           "prime":      "http://127.0.0.1:PORT/prime",
           "reactor":    "http://127.0.0.1:PORT/reactor"}
        """
        self._app = self._make_app()
        self._runner = aiohttp.web.AppRunner(
            self._app,
            access_log=None,       # silence per-request logs in tests
        )
        await self._runner.setup()
        self._site = aiohttp.web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # Retrieve the OS-assigned port
        for sock in self._site._server.sockets:  # type: ignore[union-attr]
            self._port = sock.getsockname()[1]
            break
        base = self._base_url()
        _log.info("[SyntheticAdversary] listening on %s", base)
        return {
            "doubleword": f"{base}/dw",
            "prime": f"{base}/prime",
            "reactor": f"{base}/reactor",
        }

    async def stop(self) -> None:
        """Shut down the adversary server."""
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._app = None
        self._site = None
        _log.info("[SyntheticAdversary] stopped")

    def env_overrides(self) -> Dict[str, str]:
        """Return env-var overrides to point providers at this server.

        Raises RuntimeError if start() has not been called.
        """
        if self._port is None:
            raise RuntimeError(
                "SyntheticAdversary.start() must be awaited before env_overrides()"
            )
        base = self._base_url()
        return {
            # DW provider (doubleword_provider.py reads DOUBLEWORD_BASE_URL)
            "DOUBLEWORD_BASE_URL": f"{base}/dw",
            # Aegis proxy (aegis_provider_bridge.py reads JARVIS_AEGIS_URL)
            "JARVIS_AEGIS_URL": f"{base}/dw",
            # J-Prime failover (failover_lifecycle.py writes JARVIS_PRIME_URL)
            "JARVIS_PRIME_URL": f"{base}/prime",
            # Reactor (providers.py reads JARVIS_REACTOR_URL)
            "JARVIS_REACTOR_URL": f"{base}/reactor",
            # Alias used by some consumers
            "REACTOR_CORE_API_URL": f"{base}/reactor",
        }

    # ── internals ───────────────────────────────────────────────────────────── #

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def _make_app(self) -> aiohttp.web.Application:
        app = aiohttp.web.Application()
        r = app.router
        # DW endpoints (the two independently-controllable paths)
        r.add_post("/dw/chat/completions", self._handle_dw_chat)
        r.add_get("/dw/models", self._handle_dw_models)
        # DW batch / file stubs (pass-through healthy or fault)
        r.add_post("/dw/batches", self._handle_dw_batch_create)
        r.add_get("/dw/batches/{batch_id}", self._handle_dw_batch_get)
        r.add_get("/dw/files/{file_id}/content", self._handle_dw_file_content)
        r.add_post("/dw/files", self._handle_dw_files)
        # Prime / Reactor catch-all
        r.add_route("*", "/prime/{path_info:.*}", self._handle_prime)
        r.add_route("*", "/reactor/{path_info:.*}", self._handle_reactor)
        return app

    def _get_active_fault(self, route: str, endpoint: str) -> Optional[Any]:
        """Return the active FailureSource for (route, endpoint), consuming count.

        Implementation note: uses FaultInjector (fault_injector.py) as the
        boundary-based dispatch registry.  When a clock-active fault is found it
        is registered in self._injector under the key "{route}:{endpoint}" and
        immediately consumed via check() -- this exercises the FaultInjector
        register/check contract while the HTTP-level behaviour is driven by the
        returned FailureSource.
        """
        now = self._clock()
        for entry in self._faults:
            if entry.route != route or entry.endpoint != endpoint:
                continue
            if now < entry.at:
                continue
            if entry.remaining is not None and entry.remaining <= 0:
                continue
            # Active: consume one count slot
            if entry.remaining is not None:
                entry.remaining -= 1
            # Register in FaultInjector for boundary-dispatch record/check
            fs_str = _fault_str(entry.fault)
            ft = _FS_TO_FT.get(fs_str, FaultType.NETWORK_PARTITION)
            boundary_key = f"{route}:{endpoint}"
            self._injector.register(
                boundary_key, ft,
                params={"failure_source": fs_str},
                one_shot=True,
            )
            spec: Optional[FaultSpec] = self._injector.check(boundary_key)
            if spec is not None:
                _log.debug(
                    "[SyntheticAdversary] dispatching %s@%s fault=%s",
                    route, endpoint, fs_str,
                )
            return entry.fault
        return None

    async def _apply_fault(
        self,
        request: aiohttp.web.Request,
        fault: Any,
    ) -> Optional[aiohttp.web.Response]:
        """Map FailureSource → HTTP-level behaviour.  Returns Response or None (= healthy).

        FailureSource taxonomy (topology_sentinel.py:429-472):
          LIVE_TRANSPORT    → abort TCP connection (no HTTP response)
          LIVE_HTTP_5XX     → 503 Service Unavailable
          LIVE_HTTP_429     → 429 Too Many Requests + Retry-After
          LIVE_PARSE_ERROR  → 200 with malformed / truncated JSON body
          LIVE_STREAM_STALL → SSE headers sent; tokens never arrive (stall)
        """
        fs = _fault_str(fault)

        if fs == "live_transport":
            # Abort the TCP transport so no HTTP response reaches the client.
            # The client sees ServerDisconnectedError / ClientConnectionError.
            try:
                if request.transport is not None:
                    request.transport.abort()
            except Exception:  # noqa: BLE001
                pass
            # Raising HTTPException stops handler; since the transport is
            # already aborted the error body cannot be written.
            raise aiohttp.web.HTTPServiceUnavailable(
                reason="LIVE_TRANSPORT: connection aborted by adversary"
            )

        if fs == "live_http_5xx":
            return aiohttp.web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({
                    "error": {
                        "message": "Service unavailable (LIVE_HTTP_5XX injected)",
                        "type": "server_error",
                        "code": "service_unavailable",
                    }
                }),
            )

        if fs == "live_http_429":
            return aiohttp.web.Response(
                status=429,
                headers={"Retry-After": "5", "X-RateLimit-Remaining": "0"},
                content_type="application/json",
                text=json.dumps({
                    "error": {
                        "message": "Rate limit exceeded (LIVE_HTTP_429 injected)",
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                    }
                }),
            )

        if fs == "live_parse_error":
            # 200 OK but body is malformed JSON / truncated completion
            return aiohttp.web.Response(
                status=200,
                content_type="application/json",
                text='{"id":"adversary-parse-error","object":"chat.comp',  # truncated
            )

        if fs == "live_stream_stall":
            # Open an SSE connection, send keep-alive comment, then stall.
            # Client will eventually time out (LIVE_STREAM_STALL semantics).
            # JARVIS_ADVERSARY_STALL_S controls how long to hold (default 30s;
            # set small in tests to avoid blocking).
            stall_s = float(os.environ.get("JARVIS_ADVERSARY_STALL_S", str(_STALL_S)))
            resp = aiohttp.web.StreamResponse(
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Adversary-Fault": "live_stream_stall",
                }
            )
            await resp.prepare(request)
            # Send a keep-alive comment so the client knows the connection is
            # open but no tokens follow (distinguishes from immediate close)
            await resp.write(b": adversary-stall keep-alive\n\n")
            try:
                await asyncio.sleep(stall_s)
            except asyncio.CancelledError:
                pass
            return resp

        # Unknown / telemetry-only FailureSource (GENERATION_TIMEOUT etc.) --
        # treat as healthy (fail-soft, don't block the request)
        _log.warning(
            "[SyntheticAdversary] unhandled FailureSource %r -- serving healthy", fs
        )
        return None

    # ── DW handlers ─────────────────────────────────────────────────────────── #

    async def _handle_dw_chat(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.StreamResponse:
        """POST /dw/chat/completions -- real-time generation path (HeavyProbe target)."""
        fault = self._get_active_fault("doubleword", "/chat/completions")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result  # type: ignore[return-value]

        # Healthy: parse body, return well-formed SSE stream (or JSON if stream=false)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        stream = body.get("stream", True)
        model = body.get("model", _HEALTHY_MODEL_ID)

        if not stream:
            return aiohttp.web.Response(  # type: ignore[return-value]
                status=200,
                content_type="application/json",
                text=json.dumps({
                    "id": _HEALTHY_COMPLETION_ID,
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": _HEALTHY_CHAT_CONTENT},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 8,
                        "total_tokens": 16,
                    },
                }),
            )

        resp = aiohttp.web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await resp.prepare(request)
        chunk = {
            "id": _HEALTHY_COMPLETION_ID,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": _HEALTHY_CHAT_CONTENT},
                "finish_reason": None,
            }],
        }
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    async def _handle_dw_models(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """GET /dw/models -- HeavyProbe path (independently controllable)."""
        fault = self._get_active_fault("doubleword", "/models")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result

        # Healthy: well-formed models list
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({
                "object": "list",
                "data": [
                    {
                        "id": _HEALTHY_MODEL_ID,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "adversary-stub",
                    }
                ],
            }),
        )

    async def _handle_dw_batch_create(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        fault = self._get_active_fault("doubleword", "/batches")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"id": "batch_adversary_ok", "status": "in_progress"}),
        )

    async def _handle_dw_batch_get(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        fault = self._get_active_fault("doubleword", "/batches")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        batch_id = request.match_info.get("batch_id", "unknown")
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({
                "id": batch_id,
                "status": "completed",
                "output_file_id": "file_adversary_ok",
            }),
        )

    async def _handle_dw_file_content(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        fault = self._get_active_fault("doubleword", "/files")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/octet-stream",
            body=b'{"choices":[{"message":{"content":"adversary stub"}}]}\n',
        )

    async def _handle_dw_files(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        fault = self._get_active_fault("doubleword", "/files")
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"id": "file_adversary_ok", "object": "file", "purpose": "batch"}),
        )

    # ── Prime / Reactor handlers ─────────────────────────────────────────────── #

    async def _handle_prime(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        path_info = request.match_info.get("path_info", "")
        endpoint = f"/{path_info}".rstrip("/") or "/"
        fault = self._get_active_fault("prime", endpoint)
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"status": "ok", "provider": "prime-adversary-stub"}),
        )

    async def _handle_reactor(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        path_info = request.match_info.get("path_info", "")
        endpoint = f"/{path_info}".rstrip("/") or "/"
        fault = self._get_active_fault("reactor", endpoint)
        if fault is not None:
            result = await self._apply_fault(request, fault)
            if result is not None:
                return result
        return aiohttp.web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"status": "ok", "provider": "reactor-adversary-stub"}),
        )
