"""
TelemetryContextualizer — Host-Binding Enforcement for Remote Routes
=====================================================================

Enforces the split-brain invariant:

    For any governable op:  telemetry_host == selector_host == execution_host

Two hard-fail reason codes are emitted (never swallowed):

    BODY_MISMATCH
        The telemetry_host does not match the execution_host for a remote
        route.  Local Mac psutil data must never influence GCP routing.

    TELEMETRY_DISCONNECT
        The remote execution host's telemetry endpoint is unreachable.
        No silent fallback to local psutil.  Caller must abort dispatch.

Usage
-----
    ctx = TelemetryContextualizer()

    # Before dispatching to GCP:
    await ctx.assert_host_binding(
        execution_host="10.0.0.5",   # GCP VM IP
        telemetry_host="10.0.0.5",   # must match or → BODY_MISMATCH
    )

    snap = await ctx.fetch_remote_telemetry("http://10.0.0.5:8000")
    # → RemoteTelemetry(host_id=..., ram_percent=..., ...)
    # → RuntimeError("TELEMETRY_DISCONNECT: ...") on failure
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.TelemetryContextualizer")

# ---------------------------------------------------------------------------
# Public reason codes (referenced by tests and callers)
# ---------------------------------------------------------------------------

REASON_BODY_MISMATCH = "BODY_MISMATCH"
REASON_TELEMETRY_DISCONNECT = "TELEMETRY_DISCONNECT"

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteTelemetry:
    """Telemetry snapshot fetched from the remote execution host."""

    host_id: str
    host_binding: str
    ram_percent: float
    cpu_percent: float
    pressure: str           # PressureLevel name: "NORMAL" | "ELEVATED" | ...
    schema_version: str = "1.0"
    sampled_at_epoch_s: int = 0

    @property
    def pressure_level_name(self) -> str:
        return self.pressure.upper()


# ---------------------------------------------------------------------------
# TelemetryContextualizer
# ---------------------------------------------------------------------------


class TelemetryContextualizer:
    """
    Enforces host-binding invariant for remote route telemetry.

    All methods that contact the remote host can raise:
        RuntimeError("TELEMETRY_DISCONNECT: ...")  — unreachable
        RuntimeError("BODY_MISMATCH: ...")          — host mismatch

    There is NO silent fallback to local psutil in any code path.
    """

    def __init__(self, timeout_s: float = 5.0) -> None:
        self._timeout_s = timeout_s

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_host(raw: str) -> str:
        """Normalize a host string to bare IP/hostname for comparison.

        Handles:
          - URL schemes: "http://10.0.0.5:8000" → "10.0.0.5"
          - Ports:       "10.0.0.5:8000"        → "10.0.0.5"
          - Localhost:   "localhost"              → "127.0.0.1"

        Only the host part is compared; ports are routing metadata, not identity.
        """
        s = raw.strip()
        # Strip URL scheme
        for scheme in ("https://", "http://"):
            if s.startswith(scheme):
                s = s[len(scheme):]
                break
        # Strip path/query after first slash
        s = s.split("/")[0]
        # Strip port
        # Handle IPv6 [::1]:8000 form
        if s.startswith("["):
            bracket_end = s.find("]")
            if bracket_end != -1:
                s = s[1:bracket_end]
        elif ":" in s:
            s = s.rsplit(":", 1)[0]
        # Normalize localhost variants
        if s.lower() in ("localhost", "localhost."):
            s = "127.0.0.1"
        return s.lower()

    async def assert_host_binding(
        self,
        execution_host: str,
        telemetry_host: str,
    ) -> None:
        """Hard-fail with BODY_MISMATCH if execution_host != telemetry_host
        for a remote route.

        Local routes (execution_host == "local") always pass; they have no
        remote/local split ambiguity.

        Host comparison is done after normalization to prevent false
        BODY_MISMATCH from equivalent representations:
          "localhost" == "127.0.0.1"
          "http://10.0.0.5:8000" == "10.0.0.5"
          "10.0.0.5:8080" == "10.0.0.5"

        Parameters
        ----------
        execution_host:
            Where the op will actually execute.  "local" or an IP/hostname.
        telemetry_host:
            Where the telemetry snapshot was collected.  Must equal
            execution_host for any remote route.
        """
        norm_exec = self._normalize_host(execution_host)
        norm_tel = self._normalize_host(telemetry_host)

        if norm_exec == "local":
            return  # local routes: no cross-host invariant applies

        if norm_tel != norm_exec:
            raise RuntimeError(
                f"{REASON_BODY_MISMATCH}: "
                f"execution_host={execution_host!r} (normalized: {norm_exec!r}) "
                f"!= telemetry_host={telemetry_host!r} (normalized: {norm_tel!r}). "
                f"Mac-local telemetry MUST NOT influence GCP routing decisions."
            )

    async def fetch_remote_telemetry(self, base_url: str) -> RemoteTelemetry:
        """Fetch a telemetry snapshot from the remote execution host.

        Raises RuntimeError("TELEMETRY_DISCONNECT: ...") on any network
        failure, timeout, or malformed response.

        There is NO fallback to local psutil.

        Parameters
        ----------
        base_url:
            Base URL of the remote J-Prime server, e.g. "http://10.0.0.5:8000".
        """
        try:
            raw = await self._fetch_remote_telemetry_json(base_url)
            return self._parse_telemetry(raw, base_url)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"{REASON_TELEMETRY_DISCONNECT}: unexpected error fetching "
                f"telemetry from {base_url!r} — {exc}"
            ) from exc

    # -------------------------------------------------------------------------
    # Internal helpers — separated for test patching
    # -------------------------------------------------------------------------

    async def _fetch_remote_telemetry_json(self, base_url: str) -> Dict[str, Any]:
        """HTTP GET {base_url}/v1/telemetry — returns raw dict.

        Raises RuntimeError("TELEMETRY_DISCONNECT: ...") on any failure.
        This method is the single network boundary — tests patch it directly.
        """
        url = f"{base_url.rstrip('/')}/v1/telemetry"
        logger.debug("TelemetryContextualizer: fetching %s", url)

        try:
            import aiohttp  # type: ignore[import]
            return await self._fetch_aiohttp(url, aiohttp)
        except ImportError:
            logger.debug("aiohttp not available; falling back to urllib for %s", url)
            return await self._fetch_urllib(url)

    async def _fetch_aiohttp(self, url: str, aiohttp_mod: Any) -> Dict[str, Any]:
        timeout = aiohttp_mod.ClientTimeout(total=self._timeout_s)
        try:
            async with aiohttp_mod.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise RuntimeError(
                f"{REASON_TELEMETRY_DISCONNECT}: GET {url} failed — {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"{REASON_TELEMETRY_DISCONNECT}: GET {url} error — {exc}"
            ) from exc

    async def _fetch_urllib(self, url: str) -> Dict[str, Any]:
        import asyncio
        import urllib.error
        import urllib.request

        def _blocking() -> Dict[str, Any]:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    return json.loads(resp.read())
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise RuntimeError(
                    f"{REASON_TELEMETRY_DISCONNECT}: GET {url} failed — {exc}"
                ) from exc

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, _blocking)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"{REASON_TELEMETRY_DISCONNECT}: GET {url} unexpected error — {exc}"
            ) from exc

    @staticmethod
    def _parse_telemetry(raw: Dict[str, Any], base_url: str) -> RemoteTelemetry:
        """Parse raw /v1/telemetry response into RemoteTelemetry."""
        try:
            return RemoteTelemetry(
                host_id=str(raw.get("host_id", "")),
                host_binding=str(raw.get("host_binding", base_url)),
                ram_percent=float(raw.get("ram_percent", 0.0)),
                cpu_percent=float(raw.get("cpu_percent", 0.0)),
                pressure=str(raw.get("pressure", "NORMAL")).upper(),
                schema_version=str(raw.get("schema_version", "1.0")),
                sampled_at_epoch_s=int(raw.get("sampled_at_epoch_s", 0)),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError(
                f"{REASON_TELEMETRY_DISCONNECT}: malformed telemetry response "
                f"from {base_url!r} — {exc}"
            ) from exc
