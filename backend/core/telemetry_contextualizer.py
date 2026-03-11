"""TelemetryContextualizer — Routes resource telemetry to the correct source.

Remote route → J-Prime /capability endpoint (authoritative for GCP resources).
Local route  → local psutil/MLX queries (Mac-side only).

HARD FAIL semantics:
  If route=REMOTE and the remote source is unreachable, raises
  TelemetryDisconnectError.  There is NO silent fallback to local telemetry.
  Callers that want local data must explicitly request route=LOCAL.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("JARVIS.TelemetryContextualizer")


class TelemetryRoute(Enum):
    LOCAL = "local"
    REMOTE = "remote"


class TelemetryDisconnectError(Exception):
    """Raised when the remote telemetry source is unreachable.

    Attributes
    ----------
    reason_code : str
        Always ``"TELEMETRY_DISCONNECT"``.
    """

    def __init__(self, message: str, reason_code: str = "TELEMETRY_DISCONNECT") -> None:
        super().__init__(f"TELEMETRY_DISCONNECT: {message}")
        self.reason_code = reason_code


@dataclass(frozen=True)
class ResourceState:
    """Telemetry snapshot from either local or remote source."""

    available_ram_gb: float
    total_ram_gb: float
    cpu_pressure: float
    source: str              # "local" | "remote"
    endpoint: Optional[str] = None   # populated for remote source


class LocalTelemetrySource:
    """Protocol for synchronous local resource queries (psutil/MLX)."""

    def get_resource_state(self) -> ResourceState:  # pragma: no cover
        raise NotImplementedError


class RemoteTelemetrySource:
    """Protocol for async remote (J-Prime) resource queries."""

    async def get_resource_state(self) -> ResourceState:  # pragma: no cover
        raise NotImplementedError


class TelemetryContextualizer:
    """
    Routes resource telemetry to the correct source based on TelemetryRoute.

    - REMOTE route → RemoteTelemetrySource (J-Prime capability endpoint)
    - LOCAL route  → LocalTelemetrySource (psutil/MLX)

    Remote disconnect = HARD FAIL (TelemetryDisconnectError).
    No local fallback is ever performed on remote failure.
    """

    def __init__(
        self,
        local_source: LocalTelemetrySource,
        remote_source: RemoteTelemetrySource,
    ) -> None:
        self._local = local_source
        self._remote = remote_source

    async def get_resource_state(self, route: TelemetryRoute) -> ResourceState:
        """Return resource state from the appropriate source.

        Parameters
        ----------
        route:
            TelemetryRoute.LOCAL  → local psutil/MLX (synchronous).
            TelemetryRoute.REMOTE → J-Prime capability endpoint (async).

        Raises
        ------
        TelemetryDisconnectError
            When route=REMOTE and remote source is unreachable.  Never raised
            for route=LOCAL.
        """
        if route == TelemetryRoute.LOCAL:
            return self._local.get_resource_state()

        # route == REMOTE — hard fail on any error, no local fallback
        try:
            return await self._remote.get_resource_state()
        except TelemetryDisconnectError:
            raise
        except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
            raise TelemetryDisconnectError(
                f"Remote telemetry unreachable: {exc}"
            ) from exc
        except Exception as exc:
            raise TelemetryDisconnectError(
                f"Remote telemetry error: {exc}"
            ) from exc
