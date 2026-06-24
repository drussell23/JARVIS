"""Sovereign Command Node write-router -- the biometric-gated
`/authorize-elevation` write-path.

This is a SEPARATE router from the read-only ``ide_observability`` GET
surface (deliberately split by risk: the read feed is authority-free; this
is the ONLY write-path into governance). It mirrors ide_observability's
security posture (loopback binding + rate-limit + no secret leakage) and
ADDS write-specific hardening:

  * Gated OFF by default (``JARVIS_COMMAND_NODE_AUTH_ENABLED``); every
    route 404s when disabled (port scanners see no surface).
  * NO static-token / password auth -- rejected by construction. The only
    credential is a fresh live biometric + a single-use nonce.
  * Loopback-or-TLS only -- the write surface is local-operator-only
    unless terminated behind operator-managed TLS.
  * Rate-limited per client (anti brute-force on the authorize path).
  * Bounded request body (audio cap) -- a malformed/oversized request is
    rejected before it reaches the verifier.

Two routes:
  * ``GET  /command-node/elevation/{pr_id}/challenge``
  * ``POST /command-node/authorize-elevation``

Audio is decoded in-memory only; only its sha256 reaches the audit ledger.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

from backend.core.ouroboros.governance.command_node.biometric_auth_middleware import (  # noqa: E501
    BiometricAuthMiddleware,
    get_default_middleware,
    is_command_node_auth_enabled,
)

logger = logging.getLogger("CommandNode.Router")

COMMAND_NODE_SCHEMA_VERSION = "1.0"

# PR ids: bounded printable token (mirrors ide_observability's op_id class
# but allows the '#'/'/'-free PR-NNN shape used by the elevation queue).
_PR_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_NONCE_RE = re.compile(r"^[0-9a-fA-F]{16,128}$")
_AST_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,256}$")
_BR_HASH_RE = re.compile(r"^[A-Za-z0-9_\-:.]{0,256}$")

# Audio payload hard cap (decoded bytes). Defends the verifier from an
# oversized request. Env-tunable; default 10 MiB (a few seconds of PCM).
_DEFAULT_MAX_AUDIO_BYTES = 10 * 1024 * 1024
# Request body cap (base64 inflates ~4/3) -- a bit above the decoded cap.
_DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024


def _max_audio_bytes() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_COMMAND_NODE_MAX_AUDIO_BYTES",
            str(_DEFAULT_MAX_AUDIO_BYTES),
        )))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AUDIO_BYTES


def _rate_limit_per_min() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_COMMAND_NODE_RATE_LIMIT_PER_MIN", "30",
        )))
    except (TypeError, ValueError):
        return 30


def assert_loopback_or_tls(host: str) -> None:
    """Raise ``ValueError`` if ``host`` would bind non-loopback without
    TLS. The write-path is local-operator-only unless the operator
    explicitly opts into a TLS-terminated bind via
    ``JARVIS_COMMAND_NODE_ALLOW_TLS_BIND=true`` (Phase 3 hardening). By
    default ONLY loopback is allowed."""
    if not isinstance(host, str) or not host.strip():
        raise ValueError(
            "command_node host must be a non-empty loopback address; got "
            + repr(host)
        )
    allowed_loopback = {"127.0.0.1", "::1", "localhost"}
    if host in allowed_loopback:
        return
    allow_tls = os.environ.get(
        "JARVIS_COMMAND_NODE_ALLOW_TLS_BIND", "false",
    ).strip().lower() == "true"
    if allow_tls:
        return
    raise ValueError(
        "command_node write-path refuses non-loopback bind: "
        + repr(host) + " is not allowed. Use 127.0.0.1 / ::1, or set "
        "JARVIS_COMMAND_NODE_ALLOW_TLS_BIND=true behind operator TLS."
    )


# Static-credential header names that are EXPLICITLY rejected by
# construction -- presenting one is a protocol error, not a credential.
_FORBIDDEN_AUTH_HEADERS = (
    "authorization",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "cookie",
)


class CommandNodeRouter:
    """Mounts the biometric write-path routes on a caller-supplied
    aiohttp Application. Holds its own rate tracker (separate trust
    boundary from the read GET surface)."""

    def __init__(
        self,
        *,
        middleware: Optional[BiometricAuthMiddleware] = None,
        resolve_target_repo_fn: Optional[Any] = None,
        voice_verify_fn: Optional[Any] = None,
        approve_fn: Optional[Any] = None,
    ) -> None:
        self._middleware = middleware
        self._resolve_target_repo_fn = resolve_target_repo_fn
        self._voice_verify_fn = voice_verify_fn
        self._approve_fn = approve_fn
        self._rate_tracker: Dict[str, List[float]] = {}

    # --- wiring -----------------------------------------------------------

    def register_routes(self, app: "web.Application") -> None:
        app.router.add_get(
            "/command-node/elevation/{pr_id}/challenge",
            self._handle_challenge,
        )
        app.router.add_post(
            "/command-node/authorize-elevation",
            self._handle_authorize,
        )

    def _mw(self) -> BiometricAuthMiddleware:
        if self._middleware is None:
            self._middleware = get_default_middleware()
        return self._middleware

    # --- helpers ----------------------------------------------------------

    def _client_key(self, request: "web.Request") -> str:
        return str(getattr(request, "remote", "") or "unknown")

    def _check_rate_limit(self, client_key: str) -> bool:
        limit = _rate_limit_per_min()
        now = time.monotonic()
        window_start = now - 60.0
        history = self._rate_tracker.setdefault(client_key, [])
        while history and history[0] < window_start:
            history.pop(0)
        if len(history) >= limit:
            return False
        history.append(now)
        return True

    def _json(self, status: int, payload: Dict[str, Any]) -> Any:
        from aiohttp import web

        if "schema_version" not in payload:
            payload = {"schema_version": COMMAND_NODE_SCHEMA_VERSION, **payload}
        resp = web.json_response(payload, status=status)
        # A governance write-path must never be cached.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _err(self, status: int, reason_code: str) -> Any:
        return self._json(status, {"error": True, "reason_code": reason_code})

    def _gate(self, request: "web.Request") -> Optional[Any]:
        """Shared pre-checks: master-gate (404 when off), rejected static
        credentials, rate limit. Returns an error response or None."""
        if not is_command_node_auth_enabled():
            # 404 -- the surface does not exist when disabled (no signal).
            return self._err(404, "command_node.disabled")
        # NO static-token / password auth -- reject by construction.
        for hdr in _FORBIDDEN_AUTH_HEADERS:
            if request.headers.get(hdr):
                return self._err(400, "command_node.static_credential_rejected")
        if not self._check_rate_limit(self._client_key(request)):
            return self._err(429, "command_node.rate_limited")
        return None

    # --- handlers ---------------------------------------------------------

    async def _handle_challenge(self, request: "web.Request") -> Any:
        """GET /command-node/elevation/{pr_id}/challenge -- mint a
        single-use, TTL-bounded challenge for an elevation.

        Query params (the caller binds the challenge to the AST mutation +
        blast radius of the PR it is reviewing):
          * ``ast_mutation_id`` (required)
          * ``blast_radius_hash`` (optional)
        """
        gate = self._gate(request)
        if gate is not None:
            return gate
        pr_id = request.match_info.get("pr_id", "")
        if not _PR_ID_RE.match(pr_id):
            return self._err(400, "command_node.malformed_pr_id")
        ast_mutation_id = request.query.get("ast_mutation_id", "").strip()
        if not _AST_ID_RE.match(ast_mutation_id):
            return self._err(400, "command_node.malformed_ast_mutation_id")
        blast_radius_hash = request.query.get("blast_radius_hash", "").strip()
        if not _BR_HASH_RE.match(blast_radius_hash):
            return self._err(400, "command_node.malformed_blast_radius_hash")
        try:
            ch = self._mw().issue_challenge(
                pr_id=pr_id,
                ast_mutation_id=ast_mutation_id,
                blast_radius_hash=blast_radius_hash,
            )
        except Exception:  # noqa: BLE001 -- fail-soft, never 500-leak
            logger.error(
                "[CommandNodeRouter] issue_challenge failed", exc_info=True,
            )
            return self._err(503, "command_node.challenge_unavailable")
        return self._json(200, {"challenge": ch.to_public_dict()})

    async def _handle_authorize(self, request: "web.Request") -> Any:
        """POST /command-node/authorize-elevation.

        Body (JSON)::

            {"pr_id": "...", "nonce": "...", "ast_mutation_id": "...",
             "audio_b64": "<base64 audio>", "sample_rate": 16000}

        FAIL-CLOSED: any malformed field / oversized body / verify error
        -> a rejecting verdict (never a stack trace, never AUTHORIZE)."""
        gate = self._gate(request)
        if gate is not None:
            return gate

        # Bounded body read -- reject oversized before parsing.
        max_body = _DEFAULT_MAX_BODY_BYTES
        cl = request.content_length
        if cl is not None and cl > max_body:
            return self._err(413, "command_node.body_too_large")
        try:
            raw = await request.read()
        except Exception:  # noqa: BLE001
            return self._err(400, "command_node.body_read_failed")
        if len(raw) > max_body:
            return self._err(413, "command_node.body_too_large")
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (TypeError, ValueError, UnicodeDecodeError):
            return self._err(400, "command_node.malformed_json")

        pr_id = str(body.get("pr_id", "")).strip()
        nonce = str(body.get("nonce", "")).strip()
        ast_mutation_id = str(body.get("ast_mutation_id", "")).strip()
        if not _PR_ID_RE.match(pr_id):
            return self._err(400, "command_node.malformed_pr_id")
        if not _NONCE_RE.match(nonce):
            return self._err(400, "command_node.malformed_nonce")
        if not _AST_ID_RE.match(ast_mutation_id):
            return self._err(400, "command_node.malformed_ast_mutation_id")

        # Sample rate -- bounded.
        try:
            sample_rate = int(body.get("sample_rate", 16000))
        except (TypeError, ValueError):
            return self._err(400, "command_node.malformed_sample_rate")
        if sample_rate < 8000 or sample_rate > 192000:
            return self._err(400, "command_node.malformed_sample_rate")

        # Decode audio -- in-memory only, hard-capped.
        audio_b64 = body.get("audio_b64", "")
        if not isinstance(audio_b64, str) or not audio_b64:
            return self._err(400, "command_node.missing_audio")
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except Exception:  # noqa: BLE001
            return self._err(400, "command_node.malformed_audio")
        if len(audio) > _max_audio_bytes():
            return self._err(413, "command_node.audio_too_large")

        try:
            result = await self._mw().authorize_elevation(
                pr_id=pr_id,
                nonce=nonce,
                ast_mutation_id=ast_mutation_id,
                audio=audio,
                sample_rate=sample_rate,
                voice_verify_fn=self._voice_verify_fn,
                approve_fn=self._approve_fn,
                resolve_target_repo_fn=self._resolve_target_repo_fn,
            )
        except Exception:  # noqa: BLE001 -- FAIL-CLOSED, never leak
            logger.error(
                "[CommandNodeRouter] authorize_elevation raised -- "
                "fail-closed 200/REJECTED", exc_info=True,
            )
            return self._json(200, {
                "decision": "REJECTED",
                "reason": "fail_closed:router_internal_error",
                "pr_id": pr_id,
                "ast_mutation_id": ast_mutation_id,
            })
        finally:
            # Drop the audio reference promptly -- never persisted.
            audio = b""  # noqa: F841

        status = 200 if result.decision == "AUTHORIZED" else 403
        return self._json(status, result.to_public_dict())


__all__ = [
    "COMMAND_NODE_SCHEMA_VERSION",
    "CommandNodeRouter",
    "assert_loopback_or_tls",
]
