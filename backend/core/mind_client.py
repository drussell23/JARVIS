"""
MindClient — JARVIS Body's connection to the J-Prime Mind.
==========================================================

Manages HTTP communication with J-Prime's /v1/reason/* endpoints and
maintains an operational level state machine with hysteresis.

Operational Levels
------------------
LEVEL_0 (PRIMARY)  — J-Prime reachable and healthy.  All reasoning requests
                     are forwarded to /v1/reason/select.
LEVEL_1 (DEGRADED) — J-Prime is flaky or slow.  Requests are still attempted
                     but callers should expect occasional None returns.
LEVEL_2 (REFLEX)   — Both J-Prime and the Claude fallback are unavailable.
                     select_brain() returns None immediately; callers must use
                     pure local/reflex logic.

Hysteresis
----------
* Degrade LEVEL_0 → LEVEL_1 : FAILURE_THRESHOLD (3) consecutive call failures.
* Degrade LEVEL_1 → LEVEL_2 : one additional Claude-layer failure while already
                               degraded (caller signals this via
                               _record_claude_failure()).
* Recover any level → LEVEL_0 : RECOVERY_THRESHOLD (3) consecutive successes.
  A single failure anywhere in that streak resets the success counter.

Singleton
---------
Use ``get_mind_client()`` for the process-wide singleton.  Pass explicit
``mind_host``/``mind_port`` only in tests.

Usage
-----
    from backend.core.mind_client import get_mind_client

    client = get_mind_client()
    result = await client.select_brain(command="check email")
    if result is None:
        # J-Prime unavailable — use local reflex
        ...
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants (all overridable via env vars)
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


FAILURE_THRESHOLD: int = _env_int("MIND_CLIENT_FAILURE_THRESHOLD", 3)
RECOVERY_THRESHOLD: int = _env_int("MIND_CLIENT_RECOVERY_THRESHOLD", 3)

_DEFAULT_HOST = "136.113.252.164"
_DEFAULT_PORT = 8000


# ---------------------------------------------------------------------------
# Operational level enum
# ---------------------------------------------------------------------------

class OperationalLevel(str, Enum):
    """Three-tier operational level for the Mind connection."""

    LEVEL_0 = "LEVEL_0_PRIMARY"
    LEVEL_1 = "LEVEL_1_DEGRADED"
    LEVEL_2 = "LEVEL_2_REFLEX"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class _CircuitState(Enum):
    CLOSED = "closed"        # Normal — allow all requests
    OPEN = "open"            # Failing — block requests until cooldown
    HALF_OPEN = "half_open"  # Testing — allow one probe request


class _CircuitBreaker:
    """3-state circuit breaker for the Mind-Body HTTP link.

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures before opening the circuit.
    cooldown_s:
        Seconds to wait in OPEN state before allowing a probe (HALF_OPEN).
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_s: float = 30.0,
    ) -> None:
        self.state = _CircuitState.CLOSED
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._last_failure_time: float = 0.0

    def can_execute(self) -> bool:
        """Return True if a request may proceed.

        Side-effect: transitions OPEN → HALF_OPEN after cooldown elapses.
        """
        if self.state == _CircuitState.CLOSED:
            return True
        if self.state == _CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self._cooldown_s:
                self.state = _CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN: allow exactly one probe request through
        return True

    def record_success(self) -> None:
        """Mark the last call as successful.

        Closes the circuit if it was HALF_OPEN, and resets the failure counter.
        """
        self._failure_count = 0
        if self.state == _CircuitState.HALF_OPEN:
            self.state = _CircuitState.CLOSED

    def record_failure(self) -> None:
        """Mark the last call as failed.

        Opens the circuit once the failure threshold is reached.
        In HALF_OPEN the probe failed, so go straight back to OPEN.
        """
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self.state == _CircuitState.HALF_OPEN:
            # Probe failed — back to fully open
            self.state = _CircuitState.OPEN
        elif self._failure_count >= self._failure_threshold:
            self.state = _CircuitState.OPEN


# ---------------------------------------------------------------------------
# MindClient
# ---------------------------------------------------------------------------

class MindClient:
    """HTTP client for J-Prime /v1/reason/* endpoints with level state machine.

    Parameters
    ----------
    mind_host:
        Hostname or IP of the J-Prime server.  Falls back to
        ``JARVIS_PRIME_HOST`` env var, then the static GCP IP.
    mind_port:
        TCP port of the J-Prime server.  Falls back to
        ``JARVIS_PRIME_PORT`` env var, then 8000.
    """

    def __init__(
        self,
        mind_host: Optional[str] = None,
        mind_port: Optional[int] = None,
    ) -> None:
        # Endpoint resolution — explicit args win, then env, then hardcoded default
        self._host: str = (
            mind_host
            or os.getenv("JARVIS_PRIME_HOST", "")
            or _DEFAULT_HOST
        )
        self._port: int = (
            mind_port
            if mind_port is not None
            else _env_int("JARVIS_PRIME_PORT", _DEFAULT_PORT)
        )
        self._base_url: str = f"http://{self._host}:{self._port}"

        # State machine
        self._level: OperationalLevel = OperationalLevel.LEVEL_0
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0

        # Per-process session identity — useful for J-Prime log correlation
        self._session_id: str = str(uuid.uuid4())

        # Circuit breaker — prevents hammering an unreachable J-Prime
        self._circuit = _CircuitBreaker(
            failure_threshold=int(
                os.getenv("MIND_CLIENT_CIRCUIT_FAILURE_THRESHOLD", "3")
            ),
            cooldown_s=float(
                os.getenv("MIND_CLIENT_CIRCUIT_COOLDOWN_S", "30")
            ),
        )

        # Background health monitor
        self._health_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._health_interval_s: float = float(
            os.getenv("JARVIS_MIND_HEALTH_INTERVAL_S", "30")
        )

        # Lazy aiohttp session (created on first actual HTTP call)
        self._session: Optional[Any] = None  # aiohttp.ClientSession

        logger.debug(
            "[MindClient] Initialised — endpoint=%s session=%s",
            self._base_url,
            self._session_id,
        )

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------

    @property
    def current_level(self) -> OperationalLevel:
        """Current operational level."""
        return self._level

    # ------------------------------------------------------------------
    # State machine helpers (synchronous — no I/O)
    # ------------------------------------------------------------------

    def _record_failure(self) -> None:
        """Record one consecutive failure and possibly degrade the level."""
        self._consecutive_failures += 1
        self._consecutive_successes = 0  # streak broken

        if (
            self._level == OperationalLevel.LEVEL_0
            and self._consecutive_failures >= FAILURE_THRESHOLD
        ):
            self._level = OperationalLevel.LEVEL_1
            logger.warning(
                "[MindClient] Degraded to LEVEL_1 after %d consecutive failures "
                "(endpoint=%s)",
                self._consecutive_failures,
                self._base_url,
            )

    def _record_claude_failure(self) -> None:
        """Signal that the Claude fallback also failed.

        When the caller has already exhausted J-Prime (LEVEL_1) *and* the
        Claude-API safety net has also failed, we drop to LEVEL_2 (reflex
        only).
        """
        if self._level == OperationalLevel.LEVEL_1:
            self._level = OperationalLevel.LEVEL_2
            logger.warning(
                "[MindClient] Degraded to LEVEL_2 — both J-Prime and Claude "
                "fallback unavailable (endpoint=%s)",
                self._base_url,
            )

    def _record_success(self) -> None:
        """Record one consecutive success and possibly recover the level."""
        self._consecutive_successes += 1
        self._consecutive_failures = 0  # streak broken

        if (
            self._level != OperationalLevel.LEVEL_0
            and self._consecutive_successes >= RECOVERY_THRESHOLD
        ):
            previous = self._level
            self._level = OperationalLevel.LEVEL_0
            self._consecutive_successes = 0
            logger.info(
                "[MindClient] Recovered to LEVEL_0 after %d consecutive successes "
                "(was %s, endpoint=%s)",
                RECOVERY_THRESHOLD,
                previous.value,
                self._base_url,
            )

    # ------------------------------------------------------------------
    # HTTP primitives (lazy session, aiohttp imported inside)
    # ------------------------------------------------------------------

    async def _get_session(self) -> Any:  # -> aiohttp.ClientSession
        """Return (or lazily create) the shared aiohttp ClientSession."""
        if self._session is None or self._session.closed:
            import aiohttp  # lazy — never at module level

            timeout = aiohttp.ClientTimeout(
                total=_env_float("MIND_CLIENT_SESSION_TIMEOUT", 60.0)
            )
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "X-MindClient-Session": self._session_id,
                },
                timeout=timeout,
            )
            logger.debug("[MindClient] Created new aiohttp session.")
        return self._session

    async def _http_get(
        self,
        path: str,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Perform a GET request and return the parsed JSON body.

        Raises on any network or HTTP error so callers can record failures.
        """
        import aiohttp  # lazy

        session = await self._get_session()
        url = f"{self._base_url}{path}"
        t = aiohttp.ClientTimeout(total=timeout)
        async with session.get(url, timeout=t) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _http_post(
        self,
        path: str,
        data: Dict[str, Any],
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Perform a POST request with a JSON body and return the parsed JSON response.

        Raises on any network or HTTP error so callers can record failures.
        """
        import aiohttp  # lazy

        session = await self._get_session()
        url = f"{self._base_url}{path}"
        t = aiohttp.ClientTimeout(total=timeout)
        async with session.post(url, json=data, timeout=t) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_health(self) -> Dict[str, Any]:
        """GET /v1/reason/health — returns {status, protocol_version, brains_loaded}.

        Records success/failure in the state machine and circuit breaker.
        Raises the underlying exception on failure (callers may catch it).
        """
        if not self._circuit.can_execute():
            raise RuntimeError(
                f"[MindClient] Circuit OPEN — health check blocked "
                f"(cooldown {self._circuit._cooldown_s}s not elapsed)"
            )
        try:
            result = await self._http_get(
                "/v1/reason/health",
                timeout=_env_float("MIND_CLIENT_HEALTH_TIMEOUT", 10.0),
            )
            self._circuit.record_success()
            self._record_success()
            logger.debug("[MindClient] Health check OK: %s", result.get("status"))
            return result
        except Exception as exc:
            self._circuit.record_failure()
            self._record_failure()
            logger.warning("[MindClient] Health check failed: %s", exc)
            raise

    async def check_protocol_version(self) -> Dict[str, Any]:
        """GET /v1/protocol/version — returns {current_version, min/max_supported, features}.

        Records success/failure in the state machine.
        Raises the underlying exception on failure.
        """
        try:
            result = await self._http_get(
                "/v1/protocol/version",
                timeout=_env_float("MIND_CLIENT_HEALTH_TIMEOUT", 10.0),
            )
            self._record_success()
            return result
        except Exception as exc:
            self._record_failure()
            logger.warning("[MindClient] Protocol version check failed: %s", exc)
            raise

    async def select_brain(
        self,
        command: str,
        task_type: str = "classification",
        context: Optional[Dict[str, Any]] = None,
        deadline_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """POST /v1/reason/select — classify a command and obtain a brain recommendation.

        Returns the full response dict on success, or ``None`` when:
          * The client is at LEVEL_2 (both J-Prime and Claude have failed).
          * Any HTTP or network error occurs (failure recorded; level may degrade).

        Parameters
        ----------
        command:
            The raw user command / utterance to classify.
        task_type:
            Hint to J-Prime about the classification domain (default
            ``"classification"``).
        context:
            Optional free-form context dict forwarded verbatim to J-Prime.
        deadline_ms:
            Optional wall-clock deadline in milliseconds from now.  Forwarded
            to J-Prime so it can short-circuit expensive reasoning paths.
        """
        if self._level == OperationalLevel.LEVEL_2:
            logger.debug(
                "[MindClient] select_brain skipped — at LEVEL_2 (reflex only)."
            )
            return None

        if not self._circuit.can_execute():
            logger.debug(
                "[MindClient] select_brain blocked — circuit %s",
                self._circuit.state.value,
            )
            return None

        payload: Dict[str, Any] = {
            "session_id": self._session_id,
            "command": command,
            "task_type": task_type,
        }
        if context is not None:
            payload["context"] = context
        if deadline_ms is not None:
            payload["deadline_ms"] = deadline_ms

        try:
            result = await self._http_post(
                "/v1/reason/select",
                data=payload,
                timeout=_env_float("MIND_CLIENT_SELECT_TIMEOUT", 30.0),
            )
            self._circuit.record_success()
            self._record_success()
            logger.debug(
                "[MindClient] select_brain OK — status=%s served_mode=%s",
                result.get("status"),
                result.get("served_mode"),
            )
            return result
        except Exception as exc:
            self._circuit.record_failure()
            self._record_failure()
            logger.warning(
                "[MindClient] select_brain failed (command=%r): %s", command, exc
            )
            return None

    async def send_command(
        self,
        command: str,
        context: Optional[Dict[str, Any]] = None,
        deadline_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """POST /v1/reason — send a command to J-Prime for full reasoning.

        Returns a ReasonResponse dict containing ``plan``, ``classification``,
        and metadata on success, or ``None`` when:
          * The client is at LEVEL_2 (both J-Prime and Claude have failed).
          * The circuit breaker is OPEN.
          * Any HTTP or network error occurs (failure recorded; level may degrade).

        Parameters
        ----------
        command:
            The raw user command / utterance to reason about.
        context:
            Optional free-form context dict forwarded verbatim to J-Prime
            (e.g. ``{"speaker": "Derek", "device": "mac"}``).
        deadline_ms:
            Optional wall-clock deadline in milliseconds from now.  Forwarded
            to J-Prime so it can short-circuit expensive reasoning paths.
        """
        if self._level == OperationalLevel.LEVEL_2:
            logger.debug(
                "[MindClient] send_command skipped — at LEVEL_2 (reflex only)."
            )
            return None

        if not self._circuit.can_execute():
            logger.debug(
                "[MindClient] send_command blocked — circuit %s",
                self._circuit.state.value,
            )
            return None

        request_id = str(uuid.uuid4())[:12]
        trace_id = str(uuid.uuid4())[:12]

        payload: Dict[str, Any] = {
            "protocol_version": "1.0.0",
            "request_id": request_id,
            "session_id": self._session_id,
            "trace_id": trace_id,
            "command": command,
            "context": context or {},
        }

        if deadline_ms is not None:
            payload["constraints"] = {"deadline_ms": deadline_ms}

        timeout = _env_float("MIND_CLIENT_REASON_TIMEOUT", 30.0)

        try:
            result = await self._http_post(
                "/v1/reason",
                data=payload,
                timeout=timeout,
            )
            self._circuit.record_success()
            self._record_success()
            logger.debug(
                "[MindClient] send_command OK — status=%s served_mode=%s",
                result.get("status"),
                result.get("served_mode"),
            )
            return result
        except Exception as exc:
            self._circuit.record_failure()
            self._record_failure()
            logger.warning(
                "[MindClient] send_command failed (command=%r): %s", command, exc
            )
            return None

    # ------------------------------------------------------------------
    # Background health monitor
    # ------------------------------------------------------------------

    async def start_health_monitor(self) -> None:
        """Start the background health check task (idempotent).

        The task runs every ``_health_interval_s`` seconds.  It is safe to
        call this method more than once — the second call is a no-op.
        """
        if self._health_task is not None:
            return
        self._health_task = asyncio.create_task(
            self._health_loop(), name="mind_health_monitor"
        )
        logger.info(
            "[MindClient] Health monitor started (interval=%.1fs).",
            self._health_interval_s,
        )

    async def _health_loop(self) -> None:
        """Periodic health check loop — sleeps first, then probes."""
        while True:
            try:
                await asyncio.sleep(self._health_interval_s)
                await self.check_health()
            except asyncio.CancelledError:
                break
            except Exception:
                # check_health already records the failure and logs a warning
                pass

    async def stop_health_monitor(self) -> None:
        """Cancel and await the background health task."""
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
            logger.info("[MindClient] Health monitor stopped.")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Stop the health monitor and close the underlying aiohttp session."""
        await self.stop_health_monitor()
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.debug("[MindClient] Session closed.")


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_mind_client: Optional[MindClient] = None


def get_mind_client() -> MindClient:
    """Return the process-wide MindClient singleton.

    Creates the instance on first call using env vars for configuration.
    Not thread-safe for the creation itself, but creation is idempotent and
    this is expected to be called from async code on a single event loop.
    """
    global _mind_client
    if _mind_client is None:
        _mind_client = MindClient()
        logger.info(
            "[MindClient] Singleton created — endpoint=%s:%s",
            _mind_client._host,
            _mind_client._port,
        )
    return _mind_client
