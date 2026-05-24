"""Aegis-side upstream forwarder — streaming pass-through with guillotine.

Single seam for ``/v1/messages`` and ``/v1/chat/completions``. The
forwarder:

  1. Validates the inbound ``X-JARVIS-Lease`` token (HMAC + nonce ledger).
  2. Reads the operator-supplied request body byte-for-byte.
  3. Looks up the upstream from the :mod:`upstream_registry`.
  4. Strips JARVIS-side auth and INJECTS the real upstream credential
     (from env, owned by Aegis process only — never seen by JARVIS).
  5. Opens an aiohttp client to upstream and streams the response back
     to the caller chunk-by-chunk (no buffering, no reframing — SSE
     byte-identity preserved per §44.3 reasoning-frames-as-keepalive).
  6. Parses streaming usage events (per-wire-family parser) to maintain
     a running ``(input_tokens, output_tokens)`` total.
  7. After each chunk, recomputes accumulated USD via
     :mod:`aegis.pricing` for ``(route, model)``.
  8. **Guillotine**: if accumulated USD exceeds ``lease.max_cost_usd``,
     the upstream connection is SEVERED (``response.release()`` plus
     ``connector.close()``) and the client stream is closed mid-byte.
     Reconcile fires with the partial usage.
  9. On normal completion: reconcile the budget state machine with the
     final usage; close the WAL row.

This module deliberately does NOT touch:
  - The HMAC key K (lease validation is delegated to ``lease.validate_lease_token``)
  - The bootstrap PSK (this is post-session-establish territory)
  - JARVIS's own provider modules (the wire-pass-through is renderer-blind)

Wire families:
  - ``ANTHROPIC`` SSE — usage in ``message_start`` (input_tokens) +
    ``message_delta`` (output_tokens cumulative).
  - ``OPENAI_COMPAT`` SSE — usage in the final delta (``stream_options
    .include_usage=true`` required by client; non-streaming responses
    include ``usage`` in the JSON body directly).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import aiohttp
from aiohttp import web

from backend.core.ouroboros.aegis.budget_state_machine import (
    ImmutableBudgetStateMachine,
)
from backend.core.ouroboros.aegis.lease import (
    Lease,
    NonceLedger,
    TokenVerdictKind,
    validate_lease_token,
)
from backend.core.ouroboros.aegis.pricing import (
    TokenPrice,
    cost_per_token_usd,
)
from backend.core.ouroboros.aegis.upstream_registry import (
    AuthScheme,
    UpstreamEndpoint,
    WireFamily,
)

logger = logging.getLogger(__name__)


FORWARDING_SCHEMA_VERSION: str = "aegis_forwarding.1"

# Note: we use ``iter_any()`` (not ``iter_chunked(N)``) for upstream
# pass-through. ``iter_any()`` yields whatever's currently in the
# socket buffer without trying to gather more, preserving the TCP-
# chunk boundaries the upstream actually emitted. This is essential
# for two reasons:
#   1. The guillotine — small chunks let cost accounting react fast
#      enough to sever upstream before too many overrun bytes are
#      delivered to the client.
#   2. SSE byte-identity — preserves the §44.3 reasoning-frames-as-
#      keepalive cadence (frame boundaries reach the client as the
#      upstream emitted them, not re-coalesced by our buffering).

# Maximum request body bytes we'll accept from JARVIS to forward. The
# Anthropic + OpenAI APIs cap around 200KB-1MB; we cap at 4MB to be safe.
# Operator can lower via env if they want tighter limits.
_MAX_REQUEST_BODY_BYTES_DEFAULT: int = 4 * 1024 * 1024

# Forwarding-level timeouts. Composes §44 calibrated values:
#   - Connect: 10s (matches DW connect timeout)
#   - Per-chunk: 30s (matches DW SSE stall threshold from §44.6)
#   - Sock-read: same 30s — single source of truth
_DEFAULT_CONNECT_TIMEOUT_S: float = 10.0
_DEFAULT_SOCK_READ_TIMEOUT_S: float = 30.0


# ---------------------------------------------------------------------------
# Wire-family usage parsers — closed dispatch
# ---------------------------------------------------------------------------


@dataclass
class UsageAccumulator:
    """Per-stream running tally. Mutable by design (single-task owner
    is the forwarding handler)."""

    input_tokens: int = 0
    output_tokens: int = 0
    last_observed_at: float = 0.0

    def cost_usd(self, price: TokenPrice) -> float:
        return price.cost_for(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )


def _try_parse_sse_event_block(block: str) -> Optional[Dict[str, Any]]:
    """Extract the ``data: {...}`` JSON payload from an SSE event block.

    SSE event blocks look like:

        event: message_delta
        data: {"type":"message_delta",...}

    or with multi-line data fields. We concatenate ``data:`` lines.
    Returns None if there's no parseable JSON data line.
    """
    data_parts = []
    for line in block.splitlines():
        if line.startswith("data:"):
            data_parts.append(line[len("data:"):].lstrip())
    if not data_parts:
        return None
    joined = "\n".join(data_parts)
    if joined == "[DONE]":
        return {"_sse_done": True}
    try:
        obj = json.loads(joined)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _update_usage_anthropic(usage: UsageAccumulator, event: Dict[str, Any]) -> None:
    """Anthropic SSE: usage appears in message_start (input) +
    message_delta (output, cumulative within stream)."""
    etype = event.get("type")
    if etype == "message_start":
        msg = event.get("message")
        if isinstance(msg, dict):
            u = msg.get("usage")
            if isinstance(u, dict):
                in_t = u.get("input_tokens")
                if isinstance(in_t, int):
                    usage.input_tokens = in_t
                out_t = u.get("output_tokens")
                if isinstance(out_t, int):
                    usage.output_tokens = out_t
    elif etype == "message_delta":
        u = event.get("usage")
        if isinstance(u, dict):
            out_t = u.get("output_tokens")
            if isinstance(out_t, int):
                usage.output_tokens = out_t


def _update_usage_openai_compat(usage: UsageAccumulator, event: Dict[str, Any]) -> None:
    """OpenAI-compat SSE: usage typically in final delta when
    ``stream_options.include_usage=true``. Tolerant of variants:
    some servers emit ``prompt_tokens`` / ``completion_tokens``."""
    u = event.get("usage")
    if not isinstance(u, dict):
        return
    in_t = u.get("input_tokens", u.get("prompt_tokens"))
    out_t = u.get("output_tokens", u.get("completion_tokens"))
    if isinstance(in_t, int):
        usage.input_tokens = in_t
    if isinstance(out_t, int):
        usage.output_tokens = out_t


def _update_usage(family: WireFamily, usage: UsageAccumulator, event: Dict[str, Any]) -> None:
    if family is WireFamily.ANTHROPIC:
        _update_usage_anthropic(usage, event)
    elif family is WireFamily.OPENAI_COMPAT:
        _update_usage_openai_compat(usage, event)
    # Closed taxonomy — no else clause needed (mypy/AST pin protects).


# ---------------------------------------------------------------------------
# Non-streaming response body parser — Slice 2B-i authoritative reconcile
# ---------------------------------------------------------------------------


def _parse_nonstreaming_usage(
    family: WireFamily, body_bytes: bytes,
) -> Optional[tuple[int, int]]:
    """Parse the upstream non-streaming JSON body for usage.

    Returns ``(input_tokens, output_tokens)`` on success, ``None`` on any
    failure (malformed JSON, missing usage field, schema drift). Caller
    treats None as "fall back to pre-flight reserve" — Aegis remains the
    authoritative ledger even when parsing fails (over-account beats
    under-account).

    Anthropic non-streaming shape:
        {"id": "...", "type": "message", ..., "usage": {
            "input_tokens": N, "output_tokens": M,
            "cache_creation_input_tokens": ..., "cache_read_input_tokens": ...
        }}

    OpenAI-compat non-streaming shape:
        {"id": "...", "choices": [...], "usage": {
            "prompt_tokens": N, "completion_tokens": M, "total_tokens": ...
        }}
    """
    if not body_bytes:
        return None
    try:
        obj = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    usage_obj = obj.get("usage")
    if not isinstance(usage_obj, dict):
        return None

    if family is WireFamily.ANTHROPIC:
        in_t = usage_obj.get("input_tokens")
        out_t = usage_obj.get("output_tokens")
    elif family is WireFamily.OPENAI_COMPAT:
        in_t = usage_obj.get("input_tokens", usage_obj.get("prompt_tokens"))
        out_t = usage_obj.get("output_tokens", usage_obj.get("completion_tokens"))
    else:
        return None

    if not isinstance(in_t, int) or not isinstance(out_t, int):
        return None
    if in_t < 0 or out_t < 0:
        return None
    return in_t, out_t


def _max_response_buffer_bytes() -> int:
    """Cap on bytes Aegis buffers from a non-streaming response for
    usage parsing. Bytes still pass through to JARVIS byte-identically;
    this only limits how much Aegis retains in memory for parsing."""
    raw = os.environ.get("JARVIS_AEGIS_MAX_RESPONSE_BUFFER_BYTES", "").strip()
    if not raw:
        return 4 * 1024 * 1024
    try:
        return max(1024, int(raw))
    except (TypeError, ValueError):
        return 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


import enum  # noqa: E402 — local to the module bottom is fine


class ForwardOutcome(str, enum.Enum):
    """Closed 6-value taxonomy of how a forwarding attempt resolved."""

    SUCCESS = "success"
    LEASE_INVALID = "lease_invalid"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    UPSTREAM_ERROR_STATUS = "upstream_error_status"
    BUDGET_GUILLOTINE = "budget_guillotine"
    CLIENT_DISCONNECTED = "client_disconnected"


@dataclass(frozen=True)
class ForwardResult:
    """Frozen forwarding outcome — what to record in WAL + telemetry."""

    outcome: ForwardOutcome
    usage_input_tokens: int
    usage_output_tokens: int
    actual_cost_usd: float
    upstream_status: Optional[int]
    detail: Optional[str] = None
    schema_version: str = FORWARDING_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# The forwarding handler
# ---------------------------------------------------------------------------


def _bearer_lease(request: web.Request) -> Optional[str]:
    """Read the lease token from ``X-JARVIS-Lease`` header (or
    ``Authorization: Lease <token>`` as a fallback). Returns None
    if missing."""
    raw = request.headers.get("X-JARVIS-Lease", "").strip()
    if raw:
        return raw
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("lease "):
        return auth[6:].strip() or None
    return None


def _extract_model(body: Dict[str, Any]) -> str:
    """Best-effort model extraction from request body. Both Anthropic
    and OpenAI-compat schemas use a top-level ``model`` field."""
    m = body.get("model")
    return str(m) if isinstance(m, str) else "unknown"


def _max_request_body_bytes() -> int:
    raw = os.environ.get("JARVIS_AEGIS_MAX_REQUEST_BODY_BYTES", "").strip()
    if not raw:
        return _MAX_REQUEST_BODY_BYTES_DEFAULT
    try:
        v = int(raw)
        return max(1024, v)
    except (TypeError, ValueError):
        return _MAX_REQUEST_BODY_BYTES_DEFAULT


def _is_streaming_request(body: Dict[str, Any]) -> bool:
    """Both wire families use the ``stream: true`` request field to
    request SSE."""
    return bool(body.get("stream", False))


async def forward_request(
    *,
    request: web.Request,
    endpoint: UpstreamEndpoint,
    K: bytes,
    nonce_ledger: NonceLedger,
    budget: ImmutableBudgetStateMachine,
) -> Tuple[web.StreamResponse, ForwardResult]:
    """Validate lease + forward to upstream + stream back to caller.

    Returns ``(response, result)`` — the response object Aegis will
    return to the JARVIS client, and a ForwardResult capturing the
    outcome (used by the WAL + reconcile path).

    NEVER raises — all error paths return a json_response with a
    closed-taxonomy outcome.
    """
    now = time.time()

    # 1. Lease validation -----------------------------------------------------
    presented_lease = _bearer_lease(request)
    if presented_lease is None:
        resp = web.json_response(
            {"ok": False, "error": "missing_lease_header"}, status=401,
        )
        return resp, ForwardResult(
            outcome=ForwardOutcome.LEASE_INVALID,
            usage_input_tokens=0, usage_output_tokens=0,
            actual_cost_usd=0.0, upstream_status=None,
            detail="missing X-JARVIS-Lease header",
        )

    lease_verdict = validate_lease_token(
        K, presented_lease, now_s=now, nonce_ledger=nonce_ledger,
    )
    if lease_verdict.kind is not TokenVerdictKind.VALID:
        resp = web.json_response({
            "ok": False,
            "error": f"lease_{lease_verdict.kind.value}",
            "detail": lease_verdict.detail,
        }, status=403)
        return resp, ForwardResult(
            outcome=ForwardOutcome.LEASE_INVALID,
            usage_input_tokens=0, usage_output_tokens=0,
            actual_cost_usd=0.0, upstream_status=None,
            detail=f"lease_{lease_verdict.kind.value}",
        )

    assert lease_verdict.payload is not None
    try:
        lease = Lease.from_dict(lease_verdict.payload)
    except (KeyError, ValueError, TypeError) as exc:
        resp = web.json_response({
            "ok": False, "error": "lease_payload_malformed",
        }, status=400)
        return resp, ForwardResult(
            outcome=ForwardOutcome.LEASE_INVALID,
            usage_input_tokens=0, usage_output_tokens=0,
            actual_cost_usd=0.0, upstream_status=None,
            detail=str(exc),
        )

    # 2. Request body --------------------------------------------------------
    try:
        body_bytes = await request.content.read(_max_request_body_bytes())
    except (asyncio.CancelledError, aiohttp.ClientError):
        raise
    except Exception as exc:  # noqa: BLE001
        resp = web.json_response({
            "ok": False, "error": "request_body_read_failed",
        }, status=400)
        return resp, ForwardResult(
            outcome=ForwardOutcome.CLIENT_DISCONNECTED,
            usage_input_tokens=0, usage_output_tokens=0,
            actual_cost_usd=0.0, upstream_status=None,
            detail=str(exc),
        )

    try:
        body_obj = json.loads(body_bytes.decode("utf-8"))
        if not isinstance(body_obj, dict):
            raise ValueError("body is not an object")
    except (ValueError, UnicodeDecodeError) as exc:
        resp = web.json_response({
            "ok": False, "error": f"body_parse_failed:{exc}",
        }, status=400)
        return resp, ForwardResult(
            outcome=ForwardOutcome.CLIENT_DISCONNECTED,
            usage_input_tokens=0, usage_output_tokens=0,
            actual_cost_usd=0.0, upstream_status=None,
            detail=str(exc),
        )

    model = _extract_model(body_obj)
    is_streaming = _is_streaming_request(body_obj)
    price = await cost_per_token_usd(route=lease.route, model=model)

    # 3. Credential injection — never logged ---------------------------------
    upstream_credential = os.environ.get(endpoint.credential_env_var, "").strip()
    if not upstream_credential:
        # Aegis daemon was started without credentials in its env — this
        # is operator error (the harness should have passed them at fork).
        resp = web.json_response({
            "ok": False,
            "error": "upstream_credential_unavailable",
            "detail": f"env var {endpoint.credential_env_var} is empty in aegis daemon",
        }, status=503)
        return resp, ForwardResult(
            outcome=ForwardOutcome.UPSTREAM_UNREACHABLE,
            usage_input_tokens=0, usage_output_tokens=0,
            actual_cost_usd=0.0, upstream_status=None,
            detail=f"missing {endpoint.credential_env_var}",
        )

    # Build outbound headers. Start from the inbound (preserves
    # cache-control, anthropic-version, etc.) but strip Host + any
    # JARVIS-side bearer/auth that would leak to upstream.
    outbound_headers: Dict[str, str] = {}
    for name, value in request.headers.items():
        lname = name.lower()
        if lname in ("host", "authorization", "x-jarvis-lease", "content-length"):
            continue
        outbound_headers[name] = value
    if endpoint.auth_scheme is AuthScheme.HEADER_RAW:
        outbound_headers[endpoint.auth_header] = upstream_credential
    elif endpoint.auth_scheme is AuthScheme.HEADER_BEARER:
        outbound_headers[endpoint.auth_header] = f"Bearer {upstream_credential}"

    upstream_url = endpoint.upstream_url_for(
        request_path=request.path, query_string=request.query_string,
    )
    timeout = aiohttp.ClientTimeout(
        connect=_DEFAULT_CONNECT_TIMEOUT_S,
        sock_read=_DEFAULT_SOCK_READ_TIMEOUT_S,
    )

    # 4. Open upstream + stream pass-through ---------------------------------
    usage = UsageAccumulator(last_observed_at=now)
    final_status: Optional[int] = None
    sse_buffer = ""

    # We do not bound the outer session timeout (the per-chunk read
    # timeout is what protects us from stalls; long valid generations
    # need to be allowed to complete).
    async with aiohttp.ClientSession(
        timeout=timeout, headers=outbound_headers,
    ) as session:
        try:
            upstream_resp = await session.post(
                upstream_url, data=body_bytes,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            resp = web.json_response({
                "ok": False,
                "error": "upstream_unreachable",
                "detail": str(exc),
            }, status=502)
            return resp, ForwardResult(
                outcome=ForwardOutcome.UPSTREAM_UNREACHABLE,
                usage_input_tokens=0, usage_output_tokens=0,
                actual_cost_usd=0.0, upstream_status=None,
                detail=str(exc),
            )

        final_status = upstream_resp.status
        # Build the StreamResponse — mirror upstream content-type so
        # SSE / JSON pass through cleanly.
        client_resp = web.StreamResponse(
            status=upstream_resp.status,
            headers={
                "Content-Type": upstream_resp.headers.get(
                    "Content-Type", "application/octet-stream",
                ),
                "Cache-Control": upstream_resp.headers.get(
                    "Cache-Control", "no-store",
                ),
            },
        )
        # Disable aiohttp's response compression — pass-through must
        # not re-encode the body.
        client_resp.enable_chunked_encoding()
        await client_resp.prepare(request)

        guillotine_fired = False
        client_disconnected = False
        # Slice 2B-i: for non-streaming responses, buffer the body
        # (capped) so we can parse `usage` for authoritative reconcile.
        # Bytes are STILL written to the client byte-identically as they
        # arrive — the buffer is a tee for parsing only.
        nonstreaming_buffer: Optional[bytearray] = None
        nonstreaming_cap: int = 0
        if not is_streaming:
            nonstreaming_buffer = bytearray()
            nonstreaming_cap = _max_response_buffer_bytes()
        try:
            async for chunk in upstream_resp.content.iter_any():
                if not chunk:
                    continue

                # Pass-through FIRST — usage accounting must never
                # delay byte delivery to the client.
                try:
                    await client_resp.write(chunk)
                except (ConnectionResetError, aiohttp.ClientConnectionError):
                    client_disconnected = True
                    break

                # SSE usage parsing — only for streaming responses.
                if is_streaming:
                    sse_buffer += chunk.decode("utf-8", errors="replace")
                    # Process complete event blocks (separated by blank line).
                    while "\n\n" in sse_buffer:
                        block, sse_buffer = sse_buffer.split("\n\n", 1)
                        event = _try_parse_sse_event_block(block)
                        if event is None:
                            continue
                        _update_usage(endpoint.wire_family, usage, event)

                    # Guillotine check — recompute after each chunk.
                    current_cost = usage.cost_usd(price)
                    if current_cost > lease.max_cost_usd:
                        guillotine_fired = True
                        logger.warning(
                            "[AegisForward] guillotine fired: actual %.6f > "
                            "max %.6f (in=%d out=%d) lease=%s",
                            current_cost, lease.max_cost_usd,
                            usage.input_tokens, usage.output_tokens,
                            lease.nonce,
                        )
                        # Sever upstream: closing the response releases
                        # the underlying connection from the session pool.
                        upstream_resp.release()
                        break
                else:
                    # Non-streaming: tee into the parse buffer up to cap.
                    # If response exceeds cap, the buffer truncates and
                    # the parse will likely fail → we fall back to the
                    # pre-flight reserve (operator-friendly: over-account).
                    assert nonstreaming_buffer is not None
                    remaining = nonstreaming_cap - len(nonstreaming_buffer)
                    if remaining > 0:
                        nonstreaming_buffer.extend(chunk[:remaining])

        finally:
            # IMPORTANT ordering: parse non-streaming body + reconcile
            # budget BEFORE write_eof. Once write_eof flushes EOF to
            # the client, the consumer (e.g. aiohttp TestClient's
            # ``await resp.read()``) returns and may immediately
            # snapshot the budget. Reconcile must be observable by then.
            _nonstreaming_parse_succeeded = False
            if (
                not is_streaming
                and not client_disconnected
                and final_status is not None
                and 200 <= final_status < 300
                and nonstreaming_buffer is not None
                and len(nonstreaming_buffer) > 0
            ):
                parsed = _parse_nonstreaming_usage(
                    endpoint.wire_family, bytes(nonstreaming_buffer),
                )
                if parsed is not None:
                    in_t, out_t = parsed
                    usage.input_tokens = in_t
                    usage.output_tokens = out_t
                    _nonstreaming_parse_succeeded = True
                else:
                    logger.warning(
                        "[AegisForward] non-streaming usage parse failed "
                        "(body bytes=%d, family=%s, lease=%s); reserve stands",
                        len(nonstreaming_buffer),
                        endpoint.wire_family.value,
                        lease.nonce,
                    )

            # Pre-flush reconcile / reserve-stands decision. Three cases:
            #
            #   1. Non-streaming + parse SUCCEEDED → reconcile with the
            #      parsed actual cost. Must happen BEFORE write_eof so
            #      the budget snapshot is consistent when the client
            #      unblocks on EOF (race-free).
            #
            #   2. Non-streaming + parse FAILED → DO NOT reconcile.
            #      The pre-flight reserve stands until lease expiry.
            #      "Over-account, never silently zero" invariant.
            #
            #   3. Streaming → fall through; the SSE-parser-driven
            #      reconcile happens at function tail (its usage
            #      accumulator is updated per chunk, and the client
            #      unblock on EOF is gradual — no race).
            _reconcile_handled = False
            if not is_streaming and not client_disconnected:
                if _nonstreaming_parse_succeeded:
                    _actual_cost_preflush = usage.cost_usd(price)
                    try:
                        await budget.reconcile(
                            lease_nonce=lease.nonce,
                            op_id=lease.op_id,
                            route=lease.route,
                            actual_cost_usd=_actual_cost_preflush,
                        )
                    except Exception as exc:  # noqa: BLE001 — never fail-loud
                        logger.warning(
                            "[AegisForward] pre-flush reconcile raised: %s", exc,
                        )
                    _reconcile_handled = True
                else:
                    # Parse failed → reserve stands (deliberate).
                    _reconcile_handled = True

            try:
                await client_resp.write_eof()
            except (ConnectionResetError, aiohttp.ClientConnectionError):
                client_disconnected = True

    actual_cost = usage.cost_usd(price)

    if guillotine_fired:
        outcome = ForwardOutcome.BUDGET_GUILLOTINE
        detail = (
            f"actual {actual_cost:.6f} exceeded lease.max_cost_usd "
            f"{lease.max_cost_usd:.6f}"
        )
    elif client_disconnected:
        outcome = ForwardOutcome.CLIENT_DISCONNECTED
        detail = "client closed connection mid-stream"
    elif final_status is not None and final_status >= 400:
        outcome = ForwardOutcome.UPSTREAM_ERROR_STATUS
        detail = f"upstream returned {final_status}"
    else:
        outcome = ForwardOutcome.SUCCESS
        detail = None

    # Reconcile budget — streaming path lands here (non-streaming was
    # already handled pre-flush above, either via reconcile-with-parsed
    # or reserve-stands-on-parse-failure).
    if not _reconcile_handled:
        await budget.reconcile(
            lease_nonce=lease.nonce,
            op_id=lease.op_id,
            route=lease.route,
            actual_cost_usd=actual_cost,
        )

    return client_resp, ForwardResult(
        outcome=outcome,
        usage_input_tokens=usage.input_tokens,
        usage_output_tokens=usage.output_tokens,
        actual_cost_usd=actual_cost,
        upstream_status=final_status,
        detail=detail,
    )


__all__ = [
    "FORWARDING_SCHEMA_VERSION",
    "ForwardOutcome",
    "ForwardResult",
    "UsageAccumulator",
    "forward_request",
]
