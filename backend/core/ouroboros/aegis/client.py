"""AegisClient — JARVIS-side lease management.

Per-process singleton. Reads ``JARVIS_AEGIS_URL`` +
``JARVIS_AEGIS_BOOTSTRAP_PSK`` from env (set by Slice-1 preflight),
maintains a scoped session token, and exposes ``acquire_lease`` /
``redeem_lease`` for the provider modules to wrap their calls.

Lifecycle (Model B):

  Boot once per JARVIS process
    -> POST /session/establish (bootstrap PSK)
    -> cache session token + expiry
    -> auto-refresh by re-acquiring if expired BEFORE first lease attempt
       (Slice 2 caveat: PSK is one-shot, so refresh fails after first
       consumption. Operator should set JARVIS_AEGIS_SESSION_TOKEN_TTL_S
       to cover the whole intended session duration. /session/refresh
       endpoint is a Slice-3 extension.)

  For each provider call
    -> acquire_lease(...) -> lease_token
    -> caller attaches X-JARVIS-Lease header
    -> caller makes the upstream call (routed through Aegis)
    -> redeem_lease(lease_token, actual_cost_usd)

Thread-safety: async-only. Multiple concurrent providers can share the
singleton; an asyncio.Lock guards the session-token refresh path so
two concurrent acquires never both try to establish.

NEVER raises out of the public API. Failures surface as
:class:`AegisClientError` only on operator-actionable misconfiguration
(env unset, daemon unreachable). The provider-call wrapper has to
decide whether to retry or fall back; the client itself does not.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


CLIENT_SCHEMA_VERSION: str = "aegis_client.1"


ENV_AEGIS_URL: str = "JARVIS_AEGIS_URL"
ENV_AEGIS_BOOTSTRAP_PSK: str = "JARVIS_AEGIS_BOOTSTRAP_PSK"

# Session-token refresh safety buffer: refresh ``buffer_s`` before
# actual expiry to avoid race between "valid" check and the upstream
# Aegis call.
_REFRESH_SAFETY_BUFFER_S: float = 60.0

# Per-call timeout when JARVIS talks to the local Aegis daemon.
# Loopback should be <1ms; 5s gives plenty of margin for a daemon
# busy with another stream.
_AEGIS_CALL_TIMEOUT_S: float = 5.0


class AegisClientError(RuntimeError):
    """Raised when AegisClient cannot fulfill a call due to operator-
    actionable misconfiguration (env unset, daemon unreachable, PSK
    invalid, etc.). The message describes the remediation."""


# ---------------------------------------------------------------------------
# Sentinel for "no session yet" / "session expired"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SessionState:
    token: str
    expires_at: float

    def is_usable(self, *, now_s: float) -> bool:
        return now_s + _REFRESH_SAFETY_BUFFER_S < self.expires_at


# ---------------------------------------------------------------------------
# AegisClient
# ---------------------------------------------------------------------------


class AegisClient:
    """Per-process singleton."""

    _instance: "Optional[AegisClient]" = None
    _instance_lock = asyncio.Lock()

    def __init__(
        self,
        *,
        aegis_url: str,
        bootstrap_psk: str,
        call_timeout_s: float = _AEGIS_CALL_TIMEOUT_S,
    ) -> None:
        if not aegis_url.startswith("http://") and not aegis_url.startswith("https://"):
            raise AegisClientError(
                f"aegis_url must be http:// or https://; got {aegis_url!r}"
            )
        self._aegis_url: str = aegis_url.rstrip("/")
        self._bootstrap_psk: str = bootstrap_psk
        self._call_timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(
            total=call_timeout_s,
        )
        self._session_state: Optional[_SessionState] = None
        self._session_lock = asyncio.Lock()
        # We construct an aiohttp.ClientSession lazily on first call so
        # the singleton can be constructed in non-async contexts.
        self._http: Optional[aiohttp.ClientSession] = None

    # -- singleton accessor --------------------------------------------------

    @classmethod
    async def get(cls) -> "AegisClient":
        """Return the per-process singleton. Reads
        ``JARVIS_AEGIS_URL`` + ``JARVIS_AEGIS_BOOTSTRAP_PSK`` from env
        on first call (set by Slice-1 preflight)."""
        async with cls._instance_lock:
            if cls._instance is not None:
                return cls._instance
            aegis_url = os.environ.get(ENV_AEGIS_URL, "").strip()
            psk = os.environ.get(ENV_AEGIS_BOOTSTRAP_PSK, "").strip()
            if not aegis_url:
                raise AegisClientError(
                    f"{ENV_AEGIS_URL} not set — Aegis preflight did not "
                    "complete (was JARVIS_AEGIS_ENABLED=true?)"
                )
            if not psk:
                raise AegisClientError(
                    f"{ENV_AEGIS_BOOTSTRAP_PSK} not set — Aegis preflight "
                    "did not complete"
                )
            cls._instance = cls(aegis_url=aegis_url, bootstrap_psk=psk)
            return cls._instance

    @classmethod
    async def reset_for_tests(cls) -> None:
        """Test isolation helper. Drops the singleton + closes the
        HTTP session if one is open."""
        async with cls._instance_lock:
            inst = cls._instance
            cls._instance = None
        if inst is not None:
            await inst.close()

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    # -- HTTP session lazy init ---------------------------------------------

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=self._call_timeout)
        return self._http

    # -- session-token lifecycle --------------------------------------------

    async def _ensure_session_token(self) -> str:
        """Return a currently-valid session token; establish one if
        needed. Async-safe — only one task at a time runs the
        establish path."""
        now = time.time()
        async with self._session_lock:
            if self._session_state is not None and self._session_state.is_usable(now_s=now):
                return self._session_state.token

            # Establish (or re-establish) — only valid on first use of
            # the PSK. Re-establish after expiry will fail until a
            # /session/refresh endpoint lands (Slice 3+).
            http = await self._ensure_http()
            try:
                async with http.post(
                    f"{self._aegis_url}/session/establish",
                    headers={"Authorization": f"Bearer {self._bootstrap_psk}"},
                ) as resp:
                    if resp.status != 200:
                        body_text = await resp.text()
                        raise AegisClientError(
                            f"/session/establish returned {resp.status}: "
                            f"{body_text}"
                        )
                    body = await resp.json()
            except aiohttp.ClientError as exc:
                raise AegisClientError(
                    f"could not reach Aegis daemon at {self._aegis_url}: {exc}"
                ) from exc

            token = body.get("session_token")
            expires_at = body.get("expires_at")
            if not isinstance(token, str) or not isinstance(expires_at, (int, float)):
                raise AegisClientError(
                    f"/session/establish returned malformed body: {body!r}"
                )
            self._session_state = _SessionState(
                token=token, expires_at=float(expires_at),
            )
            return token

    # -- lease primitives ---------------------------------------------------

    async def acquire_lease(
        self,
        *,
        op_id: str,
        route: str,
        estimated_cost_usd: float,
        causal_lineage_hash: str = "",
    ) -> str:
        """Acquire a fresh lease token. Returns the wire token to
        attach as ``X-JARVIS-Lease`` on the upstream call.

        Raises:
            :class:`AegisClientError` on daemon unreachable / session
            failure / cap-exceeded (caller decides retry vs. surface).
        """
        token = await self._ensure_session_token()
        http = await self._ensure_http()
        body = {
            "op_id": op_id,
            "route": route,
            "estimated_cost_usd": float(estimated_cost_usd),
            "causal_lineage_hash": causal_lineage_hash,
        }
        try:
            async with http.post(
                f"{self._aegis_url}/lease/acquire",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise AegisClientError(
                        f"/lease/acquire returned {resp.status}: {text}"
                    )
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise AegisClientError(
                f"/lease/acquire transport failure: {exc}"
            ) from exc

        if not payload.get("ok"):
            verdict = payload.get("verdict") or {}
            reason = verdict.get("reason") or "unknown"
            raise AegisClientError(
                f"lease denied: reason={reason} "
                f"detail={verdict.get('detail')!r}"
            )
        lease_token = payload.get("lease_token")
        if not isinstance(lease_token, str):
            raise AegisClientError(
                f"/lease/acquire returned malformed body: {payload!r}"
            )
        return lease_token

    async def redeem_lease(
        self,
        *,
        lease_token: str,
        actual_cost_usd: float,
    ) -> None:
        """Notify Aegis that the upstream call completed with
        ``actual_cost_usd`` actually spent. Aegis reconciles the
        reserve and the WAL.

        For Slice 2, this path is OPTIONAL — the streaming forwarder
        in :mod:`aegis.forwarding` ALREADY reconciles via the SSE
        usage parser. Provider modules can omit this call when they
        route through ``/v1/*`` (forwarding handles it). It exists
        for non-streaming / out-of-band reconciliation use cases.

        Never raises on transport failure — daemon-side state is
        authoritative even if the JARVIS-side notification is lost.
        """
        token = await self._ensure_session_token()
        http = await self._ensure_http()
        try:
            async with http.post(
                f"{self._aegis_url}/lease/redeem",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "lease_token": lease_token,
                    "actual_cost_usd": float(actual_cost_usd),
                },
            ) as resp:
                _ = await resp.text()  # drain — we don't care about body for fire-and-forget
        except aiohttp.ClientError as exc:
            logger.warning(
                "[AegisClient] /lease/redeem transport failure (non-fatal): %s",
                exc,
            )

    # -- introspection ------------------------------------------------------

    @property
    def aegis_url(self) -> str:
        return self._aegis_url

    @property
    def has_session(self) -> bool:
        return self._session_state is not None


def is_enabled() -> bool:
    """True iff the JARVIS env shows Aegis preflight completed (both
    ``JARVIS_AEGIS_URL`` and ``JARVIS_AEGIS_BOOTSTRAP_PSK`` set).

    Provider modules call this to decide whether to take the Aegis
    path or the legacy direct-to-upstream path. Cheap — pure env read.
    """
    return bool(
        os.environ.get(ENV_AEGIS_URL, "").strip()
        and os.environ.get(ENV_AEGIS_BOOTSTRAP_PSK, "").strip()
    )


__all__ = [
    "AegisClient",
    "AegisClientError",
    "CLIENT_SCHEMA_VERSION",
    "ENV_AEGIS_BOOTSTRAP_PSK",
    "ENV_AEGIS_URL",
    "is_enabled",
]
