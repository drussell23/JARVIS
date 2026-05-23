"""Aegis daemon — out-of-process aiohttp app, ephemeral-port loopback.

§43.7 spine: this process runs separately from JARVIS, owns upstream
credentials + HMAC key K + spend WAL. Slice 1 ships the substrate;
no upstream forwarding. AST-pin enforces "no /v1/* routes in Slice 1".

Endpoint surface:

  Slice 1 (always registered):
    * ``GET  /health``             — liveness + bind info + redacted snapshot
    * ``POST /session/establish``  — bootstrap PSK -> scoped session token
    * ``POST /lease/acquire``      — session token -> lease (cap-checked)
    * ``POST /lease/redeem``       — lease + actual cost -> reconciled verdict

  Slice 2 (gated by ``JARVIS_AEGIS_FORWARDING_ENABLED``, default-FALSE
  until Slice 4 graduation):
    * ``POST /v1/messages``           — Anthropic-compat forward (lease-gated)
    * ``POST /v1/chat/completions``   — OpenAI-compat / DW forward (lease-gated)

Process invariants:

  * Binds to ``daemon_bind_host()`` only (default 127.0.0.1).
  * HMAC key K is generated per-boot via ``secrets.token_bytes(32)``,
    held in ``app["_hmac_key"]``, **never** included in any HTTP
    response body, log line, exception message, or repr.
  * Bootstrap PSK is one-shot: after a successful ``/session/establish``
    that consumes it, the PSK is dropped from app state and any future
    request bearing it gets 403. The session-token path becomes the
    only auth surface.
  * No mutable singleton state — every piece lives in ``app[...]``
    so reset-for-tests is a single ``aiohttp.web.Application()``.

Run modes:

  * Library:   ``app = build_app(...); await aiohttp.web._run_app(app)``
  * CLI:       ``python -m backend.core.ouroboros.aegis.daemon
                       --bootstrap-out /tmp/aegis-xxx.json``
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from aiohttp import web

from backend.core.ouroboros.aegis import AEGIS_SCHEMA_VERSION
from backend.core.ouroboros.aegis.bootstrap import (
    BootstrapPayload,
    atomic_write_payload,
    default_expiry,
    mint_bootstrap_psk,
)
from backend.core.ouroboros.aegis.budget_state_machine import (
    BudgetCaps,
    ImmutableBudgetStateMachine,
)
from backend.core.ouroboros.aegis.lease import (
    DEFAULT_LEASE_TTL_S,
    DEFAULT_NONCE_LEDGER_CAPACITY,
    DEFAULT_SESSION_TOKEN_TTL_S,
    Lease,
    NonceLedger,
    SessionToken,
    TokenVerdictKind,
    mint_lease_token,
    mint_session_token,
    validate_lease_token,
    validate_session_token,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App state keys — single seam so the AST pin can grep for them.
# ---------------------------------------------------------------------------

_K_HMAC_KEY = "_hmac_key"
_K_BOOTSTRAP_PSK = "_bootstrap_psk"
_K_PSK_CONSUMED = "_psk_consumed"
_K_ACTIVE_SESSIONS = "_active_sessions"
_K_NONCE_LEDGER = "_nonce_ledger"
_K_BUDGET = "_budget"
_K_LEASE_TTL_S = "_lease_ttl_s"
_K_SESSION_TTL_S = "_session_ttl_s"
_K_BIND_PORT = "_bind_port"
_K_BIND_HOST = "_bind_host"
_K_BOOT_TS = "_boot_ts"
_K_FORWARDING_ENABLED = "_forwarding_enabled"
_K_UPSTREAM_MAP = "_upstream_map"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app(
    *,
    budget: ImmutableBudgetStateMachine,
    bootstrap_psk: str,
    lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
    session_ttl_s: int = DEFAULT_SESSION_TOKEN_TTL_S,
    nonce_ledger_capacity: int = DEFAULT_NONCE_LEDGER_CAPACITY,
    forwarding_enabled: bool = False,
) -> web.Application:
    """Construct the aiohttp Application with all routes wired.

    The HMAC key K is generated here per call so each ``build_app``
    invocation produces an independently-keyed instance — useful for
    tests, mandatory for production (one K per Aegis boot).

    Always-registered routes (Slice 1):
      - GET  /health
      - POST /session/establish
      - POST /lease/acquire
      - POST /lease/redeem

    Forwarding routes (Slice 2, gated by ``forwarding_enabled``):
      - POST /v1/messages           (Anthropic-compat, forwarded)
      - POST /v1/chat/completions   (OpenAI-compat / DW, forwarded)

    Set ``forwarding_enabled=True`` to expose the Slice 2 forwarding
    surface. Default False preserves the Slice 1 dark-substrate
    posture for any caller that constructs the app directly.
    """
    app = web.Application()

    # K is generated INSIDE the factory, never returned, never logged.
    # AST pin confirms K is referenced only via app[_K_HMAC_KEY] in the
    # handlers below.
    app[_K_HMAC_KEY] = secrets.token_bytes(32)
    app[_K_BOOTSTRAP_PSK] = bootstrap_psk
    app[_K_PSK_CONSUMED] = False
    active_sessions: Dict[str, SessionToken] = {}
    app[_K_ACTIVE_SESSIONS] = active_sessions
    app[_K_NONCE_LEDGER] = NonceLedger(capacity=nonce_ledger_capacity)
    app[_K_BUDGET] = budget
    app[_K_LEASE_TTL_S] = int(lease_ttl_s)
    app[_K_SESSION_TTL_S] = int(session_ttl_s)
    app[_K_BIND_HOST] = ""
    app[_K_BIND_PORT] = 0
    app[_K_BOOT_TS] = time.time()
    app[_K_FORWARDING_ENABLED] = bool(forwarding_enabled)

    app.router.add_get("/health", _handle_health)
    app.router.add_post("/session/establish", _handle_session_establish)
    app.router.add_post("/lease/acquire", _handle_lease_acquire)
    app.router.add_post("/lease/redeem", _handle_lease_redeem)

    if forwarding_enabled:
        # Slice 2 — credential confiscation surface. Each route is
        # paired with its upstream descriptor at registration time
        # so the handler can look up wire family / auth scheme /
        # credential env var without re-reading env on the hot path.
        from backend.core.ouroboros.aegis.upstream_registry import (
            snapshot as _upstream_snapshot,
        )
        upstream_map = _upstream_snapshot()
        app[_K_UPSTREAM_MAP] = upstream_map
        for path in upstream_map.keys():
            # Single shared handler — dispatches on request.path so
            # adding upstream paths to the registry is a one-line edit.
            app.router.add_post(path, _handle_forward)

    return app


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _bearer_token(request: web.Request) -> Optional[str]:
    raw = request.headers.get("Authorization", "")
    if not raw or not raw.lower().startswith("bearer "):
        return None
    return raw[7:].strip() or None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _handle_health(request: web.Request) -> web.Response:
    """Liveness + bound port + redacted snapshot.

    NEVER includes the HMAC key, the bootstrap PSK, or any session
    token. Snapshot reflects budget state machine introspection.
    """
    app = request.app
    budget: ImmutableBudgetStateMachine = app[_K_BUDGET]
    snapshot = budget.snapshot()
    body = {
        "ok": True,
        "schema_version": AEGIS_SCHEMA_VERSION,
        "bind_host": app[_K_BIND_HOST],
        "bind_port": app[_K_BIND_PORT],
        "psk_consumed": app[_K_PSK_CONSUMED],
        "active_session_count": len(app[_K_ACTIVE_SESSIONS]),
        "nonce_ledger_size": app[_K_NONCE_LEDGER].size(),
        "boot_ts": app[_K_BOOT_TS],
        "uptime_s": time.time() - app[_K_BOOT_TS],
        "budget": snapshot,
    }
    return web.json_response(body, headers={"Cache-Control": "no-store"})


async def _handle_session_establish(request: web.Request) -> web.Response:
    """Consume the one-shot bootstrap PSK, mint a scoped session token.

    Auth: ``Authorization: Bearer <BOOTSTRAP_PSK>``. The PSK is consumed
    irrevocably on the first successful call. Any subsequent call (with
    the same or any PSK) returns 403.
    """
    app = request.app
    presented = _bearer_token(request)
    if presented is None:
        return web.json_response(
            {"ok": False, "error": "missing_bearer"}, status=401,
        )

    if app[_K_PSK_CONSUMED]:
        # Single-use: any subsequent attempt is rejected without
        # disclosing whether the PSK matched.
        return web.json_response(
            {"ok": False, "error": "psk_already_consumed"}, status=403,
        )

    # Constant-time compare so a timing-attack on the PSK is not possible.
    expected = app[_K_BOOTSTRAP_PSK]
    if not secrets.compare_digest(presented, expected):
        # Do NOT mark consumed on failure — operator may have a
        # legitimate retry. Log at WARNING (no PSK values, just the
        # remote info).
        logger.warning(
            "[AegisDaemon] /session/establish bearer mismatch from %s",
            request.remote,
        )
        return web.json_response(
            {"ok": False, "error": "psk_mismatch"}, status=403,
        )

    # Mint the session token. Mark PSK consumed FIRST so a concurrent
    # second request gets the consumed-path response.
    app[_K_PSK_CONSUMED] = True
    now = time.time()
    K: bytes = app[_K_HMAC_KEY]
    wire, payload = mint_session_token(
        K, now_s=now, ttl_s=app[_K_SESSION_TTL_S],
    )
    app[_K_ACTIVE_SESSIONS][payload.jti] = payload

    # K is not in the response body — only the wire token.
    return web.json_response({
        "ok": True,
        "session_token": wire,
        "issued_at": payload.issued_at,
        "expires_at": payload.expires_at,
        "ttl_s": app[_K_SESSION_TTL_S],
    })


async def _handle_lease_acquire(request: web.Request) -> web.Response:
    """Mint a lease for one upcoming provider call.

    Auth: ``Authorization: Bearer <SESSION_TOKEN>``.

    JSON body:
        {
            "op_id": "<str>",
            "route": "<IMMEDIATE|STANDARD|COMPLEX|BACKGROUND|SPECULATIVE>",
            "estimated_cost_usd": <float>,
            "causal_lineage_hash": "<str>"   # stub for Arc #4
        }

    Returns:
        200 with {ok:true, lease_token, lease:<frozen Lease as dict>,
                  verdict:<BudgetVerdict>}
        OR
        200 with {ok:false, verdict:<BudgetVerdict with reason>}
            — the verdict carries the cap-exceeded reason; not a 500.
        OR
        401/403 on auth failure.
    """
    app = request.app
    K: bytes = app[_K_HMAC_KEY]
    now = time.time()

    presented = _bearer_token(request)
    if presented is None:
        return web.json_response(
            {"ok": False, "error": "missing_bearer"}, status=401,
        )

    verdict = validate_session_token(
        K, presented, now_s=now, active_jti=set(app[_K_ACTIVE_SESSIONS].keys()),
    )
    if verdict.kind is not TokenVerdictKind.VALID:
        return web.json_response(
            {"ok": False, "error": f"session_token_{verdict.kind.value}"},
            status=403,
        )

    try:
        body = await request.json()
    except (ValueError, web.HTTPException):
        return web.json_response(
            {"ok": False, "error": "invalid_json_body"}, status=400,
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"ok": False, "error": "body_not_object"}, status=400,
        )

    try:
        op_id = str(body["op_id"])
        route = str(body["route"])
        estimated = float(body["estimated_cost_usd"])
        lineage = str(body.get("causal_lineage_hash", ""))
    except (KeyError, ValueError, TypeError) as exc:
        return web.json_response(
            {"ok": False, "error": f"invalid_body_field:{exc}"},
            status=400,
        )

    budget: ImmutableBudgetStateMachine = app[_K_BUDGET]
    nonce = secrets.token_urlsafe(16)
    budget_verdict = await budget.admit(
        route=route,
        estimated_cost_usd=estimated,
        lease_nonce=nonce,
        op_id=op_id,
    )

    if not budget_verdict.admitted:
        return web.json_response({
            "ok": False,
            "verdict": budget_verdict.to_dict(),
        })

    # Construct the Lease and mint its wire token. K stays inside
    # mint_lease_token.
    lease_ttl = app[_K_LEASE_TTL_S]
    lease = Lease(
        nonce=nonce,
        op_id=op_id,
        route=route,
        estimated_cost_usd=estimated,
        max_cost_usd=budget_verdict.debit_usd,
        causal_lineage_hash=lineage,
        issued_at=now,
        expires_at=now + float(lease_ttl),
    )
    lease_token = mint_lease_token(K, lease)

    return web.json_response({
        "ok": True,
        "lease_token": lease_token,
        "lease": lease.to_dict(),
        "verdict": budget_verdict.to_dict(),
    })


async def _handle_lease_redeem(request: web.Request) -> web.Response:
    """Mark a lease redeemed and reconcile actual cost against reserve.

    Auth: ``Authorization: Bearer <SESSION_TOKEN>``.

    JSON body:
        {
            "lease_token": "<wire>",
            "actual_cost_usd": <float>
        }

    Slice 1 contract: redemption is a separate explicit step (Slice 2
    will fold it into the /v1/* upstream-forwarding return path).
    Returns the post-reconcile verdict OR a token-verdict-failure
    payload.
    """
    app = request.app
    K: bytes = app[_K_HMAC_KEY]
    now = time.time()

    presented = _bearer_token(request)
    if presented is None:
        return web.json_response(
            {"ok": False, "error": "missing_bearer"}, status=401,
        )

    session_verdict = validate_session_token(
        K, presented, now_s=now, active_jti=set(app[_K_ACTIVE_SESSIONS].keys()),
    )
    if session_verdict.kind is not TokenVerdictKind.VALID:
        return web.json_response(
            {"ok": False, "error": f"session_token_{session_verdict.kind.value}"},
            status=403,
        )

    try:
        body = await request.json()
    except (ValueError, web.HTTPException):
        return web.json_response(
            {"ok": False, "error": "invalid_json_body"}, status=400,
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"ok": False, "error": "body_not_object"}, status=400,
        )
    try:
        lease_token = str(body["lease_token"])
        actual = float(body["actual_cost_usd"])
    except (KeyError, ValueError, TypeError) as exc:
        return web.json_response(
            {"ok": False, "error": f"invalid_body_field:{exc}"},
            status=400,
        )

    lease_verdict = validate_lease_token(
        K, lease_token, now_s=now,
        nonce_ledger=app[_K_NONCE_LEDGER],
    )
    if lease_verdict.kind is not TokenVerdictKind.VALID:
        return web.json_response({
            "ok": False,
            "error": f"lease_token_{lease_verdict.kind.value}",
            "detail": lease_verdict.detail,
        })

    assert lease_verdict.payload is not None
    try:
        lease = Lease.from_dict(lease_verdict.payload)
    except (KeyError, ValueError, TypeError) as exc:
        return web.json_response({
            "ok": False, "error": f"lease_payload_invalid:{exc}",
        })

    budget: ImmutableBudgetStateMachine = app[_K_BUDGET]
    reconcile_verdict = await budget.reconcile(
        lease_nonce=lease.nonce,
        op_id=lease.op_id,
        route=lease.route,
        actual_cost_usd=actual,
    )

    return web.json_response({
        "ok": True,
        "verdict": reconcile_verdict.to_dict(),
        "lease": lease.to_dict(),
    })


# ---------------------------------------------------------------------------
# Slice 2 — upstream forwarding handler
# ---------------------------------------------------------------------------


async def _handle_forward(request: web.Request) -> web.StreamResponse:
    """Slice 2 forwarding entry point.

    Dispatches on ``request.path`` against the upstream registry
    captured at ``build_app`` time (so env overrides apply at boot,
    not per-request). Delegates everything else to
    :func:`forwarding.forward_request`.

    The HMAC key K is read once from app state and passed to the
    forwarder; never exposed in any response body or log line.
    """
    app = request.app
    upstream_map = app.get(_K_UPSTREAM_MAP)
    if upstream_map is None:
        # Defensive: should never happen because the route is only
        # registered when forwarding is enabled. If it does, fail
        # closed.
        return web.json_response(
            {"ok": False, "error": "forwarding_disabled"}, status=503,
        )

    endpoint = upstream_map.get(request.path)
    if endpoint is None:
        return web.json_response(
            {"ok": False, "error": "upstream_path_unregistered"}, status=404,
        )

    # Lazy-import forwarding so the daemon's substrate can be imported
    # by tests without paying the aiohttp.ClientSession startup cost.
    from backend.core.ouroboros.aegis.forwarding import forward_request

    response, _result = await forward_request(
        request=request,
        endpoint=endpoint,
        K=app[_K_HMAC_KEY],
        nonce_ledger=app[_K_NONCE_LEDGER],
        budget=app[_K_BUDGET],
    )
    return response


# ---------------------------------------------------------------------------
# Bind helpers — ephemeral port via socket.bind((host, 0))
# ---------------------------------------------------------------------------


def bind_ephemeral_socket(host: str) -> socket.socket:
    """Create + bind a TCP socket on an ephemeral port on ``host``.

    Returns the socket; caller is responsible for either passing it to
    aiohttp ``_run_app(..., sock=...)`` or closing it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.setblocking(False)
    return sock


# ---------------------------------------------------------------------------
# CLI entry — invoked by the harness via `python -m`.
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aegis-daemon",
        description="Aegis out-of-process egress + budget chokepoint daemon",
    )
    parser.add_argument(
        "--bootstrap-out", type=Path, required=True,
        help="Path to write the one-time bootstrap payload (0600).",
    )
    parser.add_argument(
        "--bind-host", type=str, default="",
        help="Loopback host to bind (default from JARVIS_AEGIS_DAEMON_BIND_HOST or 127.0.0.1).",
    )
    return parser.parse_args(argv)


async def _serve(args: argparse.Namespace) -> None:
    # Lazy-import flags here so the test surface doesn't pay the
    # FlagRegistry-singleton cost when just importing build_app.
    from backend.core.ouroboros.aegis.flags import (
        daemon_bind_host,
        forwarding_enabled,
        hourly_burn_cap_usd,
        lease_expiry_s,
        lease_overrun_multiplier,
        nonce_ledger_capacity,
        register_aegis_flags,
        route_caps_usd,
        session_cap_usd,
        session_token_ttl_s,
        wal_path,
    )

    register_aegis_flags()

    host = args.bind_host or daemon_bind_host()

    caps = BudgetCaps(
        session_cap_usd=session_cap_usd(),
        hourly_burn_cap_usd=hourly_burn_cap_usd(),
        route_caps_usd=route_caps_usd(),
        overrun_multiplier=lease_overrun_multiplier(),
    )
    wal = wal_path()
    wal.parent.mkdir(parents=True, exist_ok=True)
    budget = ImmutableBudgetStateMachine(caps=caps, wal_path=wal)
    budget.replay_for_recovery()
    await budget.record_boot(detail="aegis daemon boot")

    psk = mint_bootstrap_psk()
    app = build_app(
        budget=budget,
        bootstrap_psk=psk,
        lease_ttl_s=lease_expiry_s(),
        session_ttl_s=session_token_ttl_s(),
        nonce_ledger_capacity=nonce_ledger_capacity(),
        forwarding_enabled=forwarding_enabled(),
    )

    sock = bind_ephemeral_socket(host)
    port = sock.getsockname()[1]
    app[_K_BIND_HOST] = host
    app[_K_BIND_PORT] = port

    payload = BootstrapPayload(
        aegis_url=f"http://{host}:{port}",
        bootstrap_psk=psk,
        daemon_pid=os.getpid(),
        expires_at=default_expiry(now_s=time.time()),
    )
    atomic_write_payload(payload, args.bootstrap_out)
    logger.info(
        "[AegisDaemon] bootstrap payload written: %s (pid=%d, url=%s)",
        args.bootstrap_out, os.getpid(), payload.aegis_url,
    )

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()
    logger.info("[AegisDaemon] serving on %s:%d", host, port)

    # Run until cancelled (SIGTERM via signal handler below, or
    # subprocess kill from harness).
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s aegis-daemon %(levelname)s %(message)s",
    )
    args = _parse_args(argv)
    try:
        asyncio.run(_serve(args))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level entry
        logger.exception("[AegisDaemon] fatal: %s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "bind_ephemeral_socket",
    "build_app",
    "main",
]
