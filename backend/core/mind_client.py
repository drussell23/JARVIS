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

    def update_endpoint(self, host: str, port: int) -> None:
        """Update the J-Prime endpoint (called by JprimeLifecycleController).

        Rebuilds ``_base_url`` so subsequent requests target the new endpoint.
        The aiohttp session and session_id are preserved.
        """
        self._host = host
        self._port = port
        self._base_url = f"http://{host}:{port}"
        logger.info(
            "[MindClient] Endpoint updated to %s (by lifecycle controller)",
            self._base_url,
        )

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
        """Health check via /v1/models (OpenAI-compatible).

        Uses /v1/models instead of /v1/reason/health because llama-cpp-python
        on the GCP golden image doesn't serve the reason endpoint.
        Records success/failure in the state machine and circuit breaker.
        Raises the underlying exception on failure (callers may catch it).
        """
        if not self._circuit.can_execute():
            raise RuntimeError(
                f"[MindClient] Circuit OPEN — health check blocked "
                f"(cooldown {self._circuit._cooldown_s}s not elapsed)"
            )
        import os
        _health_path = os.environ.get("JARVIS_PRIME_HEALTH_ENDPOINT", "/v1/models")
        try:
            result = await self._http_get(
                _health_path,
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
    # Vision frame analysis (L2 path)
    # ------------------------------------------------------------------

    async def send_vision_frame(
        self,
        frame_ref: str,
        target_description: str,
        action_intent: str = "click",
        vision_task_type: str = "ui_element_detection",
        frame_width: int = 1440,
        frame_height: int = 900,
        scale_factor: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        """POST /v1/vision/analyze — send a frame to J-Prime for element detection.

        Returns dict with status, elements (coords + confidence), or None on failure.
        Same circuit breaker and level gating as send_command().
        """
        if self._level == OperationalLevel.LEVEL_2:
            return None

        if not self._circuit.can_execute():
            return None

        import time as _time
        request_id = str(uuid.uuid4())[:12]

        payload: Dict[str, Any] = {
            "request_id": request_id,
            "session_id": self._session_id,
            "trace_id": str(uuid.uuid4())[:12],
            "frame": {
                "artifact_ref": frame_ref,
                "width": frame_width,
                "height": frame_height,
                "scale_factor": scale_factor,
                "captured_at_ms": int(_time.time() * 1000),
                "display_id": 0,
            },
            "task": {
                "type": "find_element",
                "target_description": target_description,
                "action_intent": action_intent,
            },
        }

        timeout = float(os.getenv("MIND_CLIENT_VISION_TIMEOUT", "10"))

        try:
            result = await self._http_post(
                "/v1/vision/analyze",
                data=payload,
                timeout=timeout,
            )
            self._circuit.record_success()
            self._record_success()
            return result
        except Exception as exc:
            self._circuit.record_failure()
            self._record_failure()
            logger.warning("[MindClient] send_vision_frame failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Vision loop reasoning (agentic turn-by-turn)
    # ------------------------------------------------------------------

    def _compress_frame_jpeg(
        self,
        pil_image,
        quality: int = 85,
        max_bytes: int = 500000,
    ) -> dict:
        """Compress PIL Image to JPEG, downscaling if over max_bytes.

        Returns a dict with keys: data (base64), content_type, sha256,
        width, height.  Tries up to 3 downscale passes (each halving
        width+height) before giving up and returning whatever fits.
        """
        import io as _io
        import base64 as _b64
        import hashlib as _hashlib
        from PIL import Image as _Image

        img = pil_image
        buf = _io.BytesIO()
        for _ in range(3):
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            raw = buf.getvalue()
            if len(raw) <= max_bytes:
                break
            w, h = img.size
            img = img.resize((w // 2, h // 2), _Image.LANCZOS)
        else:
            raw = buf.getvalue()

        w, h = img.size
        return {
            "data": _b64.b64encode(raw).decode("ascii"),
            "content_type": "image/jpeg",
            "sha256": _hashlib.sha256(raw).hexdigest(),
            "width": w,
            "height": h,
        }

    def _validate_vision_loop_response(self, result) -> dict:
        """Validate vision.loop.v1 response.

        Returns the result dict if valid, or None if the response is
        structurally malformed.  A response with ``goal_achieved=False``
        and no ``next_action`` is only valid when ``stop_reason`` is
        ``"model_refusal"`` (the model declined to act).
        """
        if not isinstance(result, dict):
            return None
        if "goal_achieved" not in result:
            return None
        if not result.get("goal_achieved") and not result.get("next_action"):
            if result.get("stop_reason") != "model_refusal":
                logger.warning(
                    "[MindClient] Malformed v1: no goal_achieved and no next_action"
                )
                return None
        return result

    async def _claude_vision_fallback(self, v1_payload: dict) -> dict:
        """L3 Claude Vision fallback — real implementation via Anthropic API.

        Sends the screenshot to Claude with the vision.loop.v1 structured
        prompt. Claude sees the screen, reasons about what to do, and returns
        precise coordinates for actions. This is the same capability as
        Claude Computer Use but through our own orchestration.

        Uses tool_use to constrain Claude's response to the v1 schema.
        """
        try:
            import anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                logger.warning("[MindClient] No ANTHROPIC_API_KEY — L3 unavailable")
                return self._vision_error_response("No Anthropic API key configured")

            model = os.environ.get("JARVIS_CLAUDE_VISION_MODEL", "claude-sonnet-4-20250514")
            client = anthropic.AsyncAnthropic(api_key=api_key)

            # Build the prompt from vision.loop.v1 payload
            goal = v1_payload.get("goal", "")
            action_log = v1_payload.get("action_log", [])
            turn = v1_payload.get("turn_number", 1)
            frame_data = v1_payload.get("frame", {})
            frame_b64 = frame_data.get("data", "") if isinstance(frame_data, dict) else ""

            # Build action history text
            history_lines = []
            for entry in action_log:
                t = entry.get("turn", "?")
                act = entry.get("action_type", "?")
                target = entry.get("target", "?")
                result = entry.get("result", "?")
                obs = entry.get("observation", "")
                history_lines.append(f"Turn {t}: {act} '{target}' → {result} ({obs})")
            history_text = "\n".join(history_lines) if history_lines else "(first turn — no prior actions)"

            system_prompt = (
                "You are a vision-guided UI automation agent. You can see the user's screen.\n\n"
                "Your job: look at the screenshot and decide what action to take next to achieve the goal.\n\n"
                "Respond with ONLY valid JSON (no markdown, no explanation):\n"
                "{\n"
                '  "goal_achieved": true/false,\n'
                '  "stop_reason": "goal_satisfied" or null,\n'
                '  "next_action": {\n'
                '    "action_type": "click" or "type" or "scroll",\n'
                '    "target": "description of what to click/type in",\n'
                '    "text": "text to type" or null,\n'
                '    "coords": [x, y] pixel coordinates or null\n'
                '  } or null (if goal achieved),\n'
                '  "reasoning": "one line explanation",\n'
                '  "confidence": 0.0-1.0,\n'
                '  "scene_summary": "brief description of what you see"\n'
                "}\n\n"
                "IMPORTANT:\n"
                "- Return PRECISE pixel coordinates [x, y] for click targets\n"
                "- Look at the actual screen content to find UI elements\n"
                "- If the goal is achieved (e.g., message sent), set goal_achieved=true\n"
                "- action_type must be one of: click, type, scroll\n"
            )

            user_text = (
                f"GOAL: {goal}\n\n"
                f"TURN: {turn}\n\n"
                f"ACTION HISTORY:\n{history_text}\n\n"
                f"Look at the screenshot and tell me what action to take next."
            )

            # Build message content with image
            content = []
            if frame_b64:
                media_type = frame_data.get("content_type", "image/jpeg") if isinstance(frame_data, dict) else "image/jpeg"
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": frame_b64,
                    },
                })
            content.append({"type": "text", "text": user_text})

            timeout = float(os.environ.get("JARVIS_CLAUDE_VISION_TIMEOUT", "15.0"))

            response = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=512,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content}],
                ),
                timeout=timeout,
            )

            # Parse Claude's response
            import json as _json
            raw_text = response.content[0].text if response.content else ""

            # Strip markdown fences if present
            if "```" in raw_text:
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
                raw_text = raw_text.strip()

            result = _json.loads(raw_text)

            # Normalize to v1 schema
            result["schema"] = "vision.loop.v1"
            if result.get("next_action") and not result["next_action"].get("action_id"):
                result["next_action"]["action_id"] = f"claude-{turn}"
            if result.get("next_action") and result["next_action"].get("coords"):
                # Ensure coords are integers
                c = result["next_action"]["coords"]
                if isinstance(c, list) and len(c) >= 2:
                    result["next_action"]["coords"] = [int(c[0]), int(c[1])]

            logger.info(
                "[MindClient] Claude Vision L3: goal_achieved=%s, action=%s, conf=%.2f",
                result.get("goal_achieved"),
                result.get("next_action", {}).get("action_type") if result.get("next_action") else "none",
                result.get("confidence", 0),
            )

            return result

        except asyncio.TimeoutError:
            logger.warning("[MindClient] Claude Vision timed out (%.0fs)", timeout)
            return self._vision_error_response("Claude Vision timed out")
        except _json.JSONDecodeError as e:
            logger.warning("[MindClient] Claude Vision returned invalid JSON: %s", e)
            return self._vision_error_response(f"Invalid JSON from Claude: {e}")
        except Exception as exc:
            logger.warning("[MindClient] Claude Vision fallback error: %s", exc)
            return self._vision_error_response(str(exc))

    @staticmethod
    def _vision_error_response(reason: str) -> dict:
        return {
            "schema": "vision.loop.v1",
            "goal_achieved": False,
            "stop_reason": "error",
            "next_action": None,
            "reasoning": reason,
            "confidence": 0.0,
            "scene_summary": "",
        }

    async def reason_vision_turn(
        self,
        request_id: str,
        session_id: str,
        goal: str,
        action_log: list,
        frame_jpeg_b64: str,
        frame_dims: dict,
        allowed_action_types: list,
        strategy_hints=None,
    ) -> dict:
        """POST /v1/vision/reason_turn — ask J-Prime what to do next.

        Sends the current screen frame plus the full action history and
        receives a vision.loop.v1 decision: the next UI action to take,
        or a signal that the goal has been satisfied (or an error).

        Falls back to ``_claude_vision_fallback`` (L3) on any J-Prime
        failure, and returns a hard-error dict if both tiers are down.

        ``asyncio.shield`` wraps the inner coroutine so that the
        aiohttp session is not cancelled mid-request when the outer
        ``wait_for`` timeout fires.

        Parameters
        ----------
        request_id:
            Caller-supplied trace identifier.
        session_id:
            Vision-loop session UUID (may differ from ``_session_id``).
        goal:
            Natural-language description of the end state to reach.
        action_log:
            List of previously executed actions in this session.
        frame_jpeg_b64:
            Base-64-encoded JPEG of the current screen frame.
        frame_dims:
            Dict with keys ``width``, ``height``, ``scale`` (and any
            additional metadata the caller wants to forward).
        allowed_action_types:
            Constraint list forwarded to J-Prime (e.g. ``["click",
            "type", "scroll"]``).
        strategy_hints:
            Optional free-form hints to guide J-Prime's reasoning.
        """
        payload = {
            "schema": "vision.loop.v1",
            "request_id": request_id,
            "session_id": session_id,
            "goal": goal,
            "turn_number": len(action_log) + 1,
            "max_turns": int(os.getenv("VISION_LOOP_MAX_TURNS", "10")),
            "allowed_action_types": allowed_action_types,
            "strategy_hints": strategy_hints,
            "action_log": action_log,
            "frame": {"data": frame_jpeg_b64, **frame_dims},
        }

        timeout = float(os.getenv("VISION_LOOP_THINK_TIMEOUT_S", "12"))

        try:
            result = await asyncio.wait_for(
                asyncio.shield(
                    self._http_post(
                        "/v1/vision/reason_turn",
                        data=payload,
                        timeout=timeout,
                    )
                ),
                timeout=timeout,
            )
            validated = self._validate_vision_loop_response(result)
            if validated:
                self._circuit.record_success()
                self._record_success()
                return validated
        except asyncio.TimeoutError:
            logger.info(
                "[MindClient] reason_vision_turn timed out (%.1fs)", timeout
            )
        except Exception as exc:
            logger.info(
                "[MindClient] reason_vision_turn L2 failed: %s", exc
            )
            self._circuit.record_failure()
            self._record_failure()

        # L3 fallback — Claude Vision API (stub for now)
        try:
            return await self._claude_vision_fallback(payload)
        except Exception as exc:
            logger.warning("[MindClient] Claude fallback failed: %s", exc)

        return {
            "schema": "vision.loop.v1",
            "goal_achieved": False,
            "stop_reason": "error",
            "next_action": None,
            "reasoning": "Both J-Prime and Claude unavailable",
            "confidence": 0.0,
            "scene_summary": "",
        }

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
