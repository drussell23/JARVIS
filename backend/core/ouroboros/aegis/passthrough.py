"""Aegis credential-injecting passthrough — closed allowlist.

For upstream API endpoints that JARVIS needs to reach but that do NOT
burn LLM tokens (file uploads, batch management, model listing). The
passthrough:

  1. Validates the inbound session token (NOT a lease — passthrough
     has no cost, so no lease accounting applies).
  2. Reads the inbound body byte-for-byte (multipart, JSON, or empty
     — Aegis doesn't parse, doesn't re-encode).
  3. Strips ALL JARVIS-side auth + lease + internal headers.
  4. Injects the real upstream credential from env.
  5. Forwards the request (preserving method, query string, Content-Type).
  6. Streams the response back to the client byte-identically.
  7. Records a non-credential audit line for observability.

Binding constraints (Slice 2B-i):

  * **NOT an open proxy.** Only the explicit (method, path) pairs
    declared in :mod:`upstream_registry` with ``kind=PASSTHROUGH``
    are reachable. The daemon registers ONE route per (method, path)
    pair at boot; no dynamic dispatch.
  * **No credential logging.** Outbound auth header value never
    appears in any log line, exception, or response body.
  * **No multipart body logging.** Request body bytes (potentially
    PII / proprietary data) are forwarded but never logged.
  * **Strip-then-inject ordering.** Strip the inbound auth/lease
    BEFORE composing outbound headers — guarantees a no-op header
    set in the gap window between strip and inject.
  * **No token-cost reconciliation.** Passthrough endpoints by
    design do NOT touch the budget state machine; the spend WAL
    is unaffected.
  * **Standard request-body cap** (``JARVIS_AEGIS_MAX_REQUEST_BODY_BYTES``)
    applies — same ceiling as LLM forwarding.

The audit line emitted per passthrough request contains:
``method``, ``aegis_path`` (template), ``request_path`` (concrete),
``upstream_status``, ``upstream_url`` (host+path only — no auth/query),
``content_length`` (response). Operator can grep
``[AegisPassthrough]`` in logs for the full audit trail.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

import aiohttp
from aiohttp import web

from backend.core.ouroboros.aegis.lease import (
    TokenVerdictKind,
    validate_session_token,
)
from backend.core.ouroboros.aegis.upstream_registry import (
    AuthScheme,
    EndpointKind,
    UpstreamEndpoint,
)

logger = logging.getLogger(__name__)


PASSTHROUGH_SCHEMA_VERSION: str = "aegis_passthrough.1"

# Read-size for streaming response pass-through. Matches forwarding.py
# iter_any() shape — preserves TCP chunk boundaries.
_DEFAULT_CONNECT_TIMEOUT_S: float = 10.0
_DEFAULT_SOCK_READ_TIMEOUT_S: float = 30.0
_DEFAULT_OUTBOUND_TOTAL_TIMEOUT_S: float = 600.0  # batch retrieval can be slow

# Headers we ALWAYS strip from inbound before composing outbound.
# Anything carrying JARVIS-side identity or auth must not leak upstream.
_INBOUND_HEADERS_TO_STRIP: Tuple[str, ...] = (
    "authorization",
    "x-jarvis-lease",
    "x-jarvis-session",
    "x-jarvis-causal-lineage",
    "host",
    "content-length",  # aiohttp recomputes on outbound
)


class PassthroughOutcome(str, enum.Enum):
    """Closed 5-value taxonomy of passthrough outcomes."""

    SUCCESS = "success"
    AUTH_MISSING = "auth_missing"
    AUTH_INVALID = "auth_invalid"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    CLIENT_DISCONNECTED = "client_disconnected"


@dataclass(frozen=True)
class PassthroughResult:
    outcome: PassthroughOutcome
    upstream_status: Optional[int]
    response_bytes: int
    detail: Optional[str] = None
    schema_version: str = PASSTHROUGH_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Auth — session token (passthrough does NOT consume leases)
# ---------------------------------------------------------------------------


def _bearer_session(request: web.Request) -> Optional[str]:
    """Extract session token from ``Authorization: Bearer <token>``.

    Passthrough deliberately uses session-token auth (the JARVIS process
    is already proven legitimate via /session/establish). A lease would
    be appropriate if there were cost accounting; passthrough has none.
    """
    raw = request.headers.get("Authorization", "").strip()
    if not raw or not raw.lower().startswith("bearer "):
        return None
    return raw[7:].strip() or None


# ---------------------------------------------------------------------------
# Header composition
# ---------------------------------------------------------------------------


def _max_request_body_bytes() -> int:
    raw = os.environ.get("JARVIS_AEGIS_MAX_REQUEST_BODY_BYTES", "").strip()
    if not raw:
        return 4 * 1024 * 1024
    try:
        return max(1024, int(raw))
    except (TypeError, ValueError):
        return 4 * 1024 * 1024


def _strip_inbound_headers(request: web.Request) -> Dict[str, str]:
    """Build outbound header set: copy inbound, strip JARVIS auth/lease
    plus host/content-length. Returns a fresh dict; never mutates
    request.headers. Credential VALUE is never read here — that lands
    via the upstream_credential injection step that follows."""
    out: Dict[str, str] = {}
    for name, value in request.headers.items():
        if name.lower() in _INBOUND_HEADERS_TO_STRIP:
            continue
        out[name] = value
    return out


def _inject_upstream_credential(
    headers: Dict[str, str], endpoint: UpstreamEndpoint, credential: str,
) -> None:
    """Add the upstream auth header per scheme. Mutates ``headers``."""
    if endpoint.auth_scheme is AuthScheme.HEADER_RAW:
        headers[endpoint.auth_header] = credential
    elif endpoint.auth_scheme is AuthScheme.HEADER_BEARER:
        headers[endpoint.auth_header] = f"Bearer {credential}"
    # Closed enum — no else branch needed.


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def forward_passthrough(
    *,
    request: web.Request,
    endpoint: UpstreamEndpoint,
    K: bytes,
    active_jti: Set[str],
) -> Tuple[web.StreamResponse, PassthroughResult]:
    """Validate session + forward to allowlisted upstream + stream back.

    Returns ``(response, result)``. NEVER raises — error paths build
    a json_response with closed-taxonomy outcome and return.
    """
    now = time.time()

    # --- 1. Endpoint kind invariant (defensive — daemon should never
    # route LLM endpoints here, but fail closed if misconfigured) -----
    if endpoint.kind is not EndpointKind.PASSTHROUGH:
        return (
            web.json_response(
                {"ok": False, "error": "endpoint_kind_mismatch"},
                status=500,
            ),
            PassthroughResult(
                outcome=PassthroughOutcome.UPSTREAM_UNREACHABLE,
                upstream_status=None,
                response_bytes=0,
                detail=f"endpoint {endpoint.aegis_path} is not PASSTHROUGH",
            ),
        )

    # --- 2. Session auth ----------------------------------------------------
    presented = _bearer_session(request)
    if presented is None:
        return (
            web.json_response(
                {"ok": False, "error": "missing_session_bearer"}, status=401,
            ),
            PassthroughResult(
                outcome=PassthroughOutcome.AUTH_MISSING,
                upstream_status=None,
                response_bytes=0,
            ),
        )

    session_verdict = validate_session_token(
        K, presented, now_s=now, active_jti=active_jti,
    )
    if session_verdict.kind is not TokenVerdictKind.VALID:
        return (
            web.json_response(
                {"ok": False, "error": f"session_{session_verdict.kind.value}"},
                status=403,
            ),
            PassthroughResult(
                outcome=PassthroughOutcome.AUTH_INVALID,
                upstream_status=None,
                response_bytes=0,
                detail=session_verdict.detail,
            ),
        )

    # --- 3. Request body (cap-enforced; passthrough never parses) ----------
    try:
        body_bytes = await request.content.read(_max_request_body_bytes())
    except (asyncio.CancelledError, aiohttp.ClientError):
        raise
    except Exception as exc:  # noqa: BLE001
        return (
            web.json_response(
                {"ok": False, "error": "request_body_read_failed"}, status=400,
            ),
            PassthroughResult(
                outcome=PassthroughOutcome.CLIENT_DISCONNECTED,
                upstream_status=None,
                response_bytes=0,
                detail=str(exc),
            ),
        )

    # --- 4. Credential injection (never logged) -----------------------------
    upstream_credential = os.environ.get(endpoint.credential_env_var, "").strip()
    if not upstream_credential:
        return (
            web.json_response({
                "ok": False,
                "error": "upstream_credential_unavailable",
                "detail": (
                    f"env var {endpoint.credential_env_var} is empty in "
                    "aegis daemon"
                ),
            }, status=503),
            PassthroughResult(
                outcome=PassthroughOutcome.UPSTREAM_UNREACHABLE,
                upstream_status=None,
                response_bytes=0,
                detail=f"missing {endpoint.credential_env_var}",
            ),
        )

    outbound_headers = _strip_inbound_headers(request)
    _inject_upstream_credential(outbound_headers, endpoint, upstream_credential)

    # --- 5. Compose outbound URL preserving path + query string ------------
    # request.path is the concrete path (template substituted by aiohttp);
    # query_string preserved so e.g. ?limit=10 on /v1/batches survives.
    upstream_url = endpoint.upstream_url_for(
        request_path=request.path,
        query_string=request.query_string,
    )

    timeout = aiohttp.ClientTimeout(
        connect=_DEFAULT_CONNECT_TIMEOUT_S,
        sock_read=_DEFAULT_SOCK_READ_TIMEOUT_S,
        total=_DEFAULT_OUTBOUND_TOTAL_TIMEOUT_S,
    )

    # --- 6. Open upstream + stream response back ---------------------------
    final_status: Optional[int] = None
    bytes_passed: int = 0
    client_disconnected = False

    async with aiohttp.ClientSession(
        timeout=timeout, headers=outbound_headers,
    ) as session:
        try:
            upstream_resp = await session.request(
                method=request.method,
                url=upstream_url,
                data=body_bytes if body_bytes else None,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return (
                web.json_response({
                    "ok": False,
                    "error": "upstream_unreachable",
                    "detail": str(exc),
                }, status=502),
                PassthroughResult(
                    outcome=PassthroughOutcome.UPSTREAM_UNREACHABLE,
                    upstream_status=None,
                    response_bytes=0,
                    detail=str(exc),
                ),
            )

        final_status = upstream_resp.status

        # Mirror upstream content-type + cache-control. Don't propagate
        # transport-encoding headers (aiohttp manages those outbound).
        passthrough_response_headers: Dict[str, str] = {}
        for hdr in ("Content-Type", "Cache-Control"):
            v = upstream_resp.headers.get(hdr)
            if v:
                passthrough_response_headers[hdr] = v

        client_resp = web.StreamResponse(
            status=final_status,
            headers=passthrough_response_headers,
        )
        client_resp.enable_chunked_encoding()
        await client_resp.prepare(request)

        try:
            async for chunk in upstream_resp.content.iter_any():
                if not chunk:
                    continue
                try:
                    await client_resp.write(chunk)
                    bytes_passed += len(chunk)
                except (ConnectionResetError, aiohttp.ClientConnectionError):
                    client_disconnected = True
                    break
        finally:
            try:
                await client_resp.write_eof()
            except (ConnectionResetError, aiohttp.ClientConnectionError):
                client_disconnected = True

    # --- 7. Audit log (no credentials, no body bytes) ----------------------
    # We log: method, path template + concrete, upstream status, bytes,
    # and the host portion of the upstream URL (no query string, no
    # auth) so operator can see "which DW endpoint was hit".
    upstream_host = endpoint.upstream_base_url
    logger.info(
        "[AegisPassthrough] method=%s template=%s concrete=%s "
        "upstream_host=%s upstream_status=%s bytes=%d disconnected=%s",
        request.method, endpoint.aegis_path, request.path,
        upstream_host, final_status, bytes_passed, client_disconnected,
    )

    outcome = (
        PassthroughOutcome.CLIENT_DISCONNECTED if client_disconnected
        else PassthroughOutcome.SUCCESS
    )
    return client_resp, PassthroughResult(
        outcome=outcome,
        upstream_status=final_status,
        response_bytes=bytes_passed,
        detail=None,
    )


__all__ = [
    "PASSTHROUGH_SCHEMA_VERSION",
    "PassthroughOutcome",
    "PassthroughResult",
    "forward_passthrough",
]
