"""
Voice.ai WebSocket TTS Provider
================================
R&D spike — isolated implementation of voice.ai's streaming WebSocket protocol.
Does NOT touch any production TTS paths. Designed for sandbox evaluation against
the macOS/Daniel baseline.

Protocol assumptions (verify against voice.ai API docs once access is granted):
  - WebSocket endpoint accepts JSON synthesis requests
  - Server streams binary audio frames as they are generated
  - A final JSON control frame signals end-of-stream
  - HTTP endpoint available for non-streaming / voice listing

Environment variables (all resolved at init — zero hardcoding):
  VOICEAI_API_KEY              Required. API key from voice.ai dashboard.
  VOICEAI_VOICE_ID             Voice ID to use. If empty, server default applies.
  VOICEAI_WS_ENDPOINT          WebSocket TTS endpoint.
                               Default: wss://api.voice.ai/v1/tts/stream
  VOICEAI_HTTP_ENDPOINT        HTTP TTS endpoint (synthesize fallback).
                               Default: https://api.voice.ai/v1/tts
  VOICEAI_VOICES_ENDPOINT      HTTP voices list endpoint.
                               Default: https://api.voice.ai/v1/voices
  VOICEAI_OUTPUT_FORMAT        Audio format token: pcm_16000 | pcm_22050 |
                               mp3_22050 | mp3_44100.
                               Default: pcm_16000
  VOICEAI_MODEL                Model name/ID. Empty string uses server default.
  VOICEAI_CONNECT_TIMEOUT      WebSocket connect timeout in seconds. Default: 5.0
  VOICEAI_SYNTHESIS_TIMEOUT    Max total synthesis time in seconds. Default: 30.0
  VOICEAI_HTTP_FALLBACK        "true" to allow HTTP fallback when WS fails.
                               Default: true
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional

import aiohttp

from .base_tts_engine import BaseTTSEngine, TTSChunk, TTSConfig, TTSEngine, TTSResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal constants — not user-facing config
# ---------------------------------------------------------------------------
_BYTES_PER_SAMPLE_INT16 = 2
_DEFAULT_WS_ENDPOINT = "wss://api.voice.ai/v1/tts/stream"
_DEFAULT_HTTP_ENDPOINT = "https://api.voice.ai/v1/tts"
_DEFAULT_VOICES_ENDPOINT = "https://api.voice.ai/v1/voices"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = True) -> bool:
    raw = os.getenv(key, "true" if default else "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Frozen runtime config — resolved from environment once at initialisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VoiceAIConfig:
    """All voice.ai runtime configuration resolved from environment variables.

    Immutable after construction so that config drift during a session is
    structurally impossible.
    """

    api_key: str
    voice_id: str
    ws_endpoint: str
    http_endpoint: str
    voices_endpoint: str
    output_format: str
    sample_rate: int
    model: str
    connect_timeout: float
    synthesis_timeout: float
    http_fallback: bool

    @classmethod
    def from_env(cls) -> "VoiceAIConfig":
        api_key = _env("VOICEAI_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "VOICEAI_API_KEY is not set. "
                "Obtain a key from voice.ai and export it before initialising VoiceAIProvider."
            )

        output_format = _env("VOICEAI_OUTPUT_FORMAT", "pcm_16000")
        sample_rate = _parse_sample_rate(output_format)

        return cls(
            api_key=api_key,
            voice_id=_env("VOICEAI_VOICE_ID", ""),
            ws_endpoint=_env("VOICEAI_WS_ENDPOINT", _DEFAULT_WS_ENDPOINT),
            http_endpoint=_env("VOICEAI_HTTP_ENDPOINT", _DEFAULT_HTTP_ENDPOINT),
            voices_endpoint=_env("VOICEAI_VOICES_ENDPOINT", _DEFAULT_VOICES_ENDPOINT),
            output_format=output_format,
            sample_rate=sample_rate,
            model=_env("VOICEAI_MODEL", ""),
            connect_timeout=_env_float("VOICEAI_CONNECT_TIMEOUT", 5.0),
            synthesis_timeout=_env_float("VOICEAI_SYNTHESIS_TIMEOUT", 30.0),
            http_fallback=_env_bool("VOICEAI_HTTP_FALLBACK", default=True),
        )

    def auth_headers(self) -> Dict[str, str]:
        """Return HTTP/WebSocket authorization headers."""
        return {"Authorization": f"Bearer {self.api_key}"}

    def build_synthesis_request(self, text: str) -> Dict:
        """Build the JSON payload sent to the voice.ai TTS endpoint.

        NOTE: Verify this schema against voice.ai's official API documentation
        once API access is confirmed.  The fields below follow common conventions
        for streaming TTS providers (ElevenLabs, Cartesia, etc.).
        """
        payload: Dict = {
            "text": text,
            "output_format": self.output_format,
        }
        if self.voice_id:
            payload["voice_id"] = self.voice_id
        if self.model:
            payload["model"] = self.model
        return payload


# ---------------------------------------------------------------------------
# Latency measurement
# ---------------------------------------------------------------------------

@dataclass
class LatencyProbe:
    """Captures precise timing across the streaming pipeline."""

    request_sent_at: float = field(default_factory=time.perf_counter)
    first_byte_at: Optional[float] = None
    last_byte_at: Optional[float] = None

    def mark_first_byte(self) -> None:
        if self.first_byte_at is None:
            self.first_byte_at = time.perf_counter()

    def mark_last_byte(self) -> None:
        self.last_byte_at = time.perf_counter()

    @property
    def ttfb_ms(self) -> Optional[float]:
        if self.first_byte_at is None:
            return None
        return (self.first_byte_at - self.request_sent_at) * 1000

    @property
    def total_ms(self) -> Optional[float]:
        if self.last_byte_at is None:
            return None
        return (self.last_byte_at - self.request_sent_at) * 1000


# ---------------------------------------------------------------------------
# Audio utilities
# ---------------------------------------------------------------------------

def _parse_sample_rate(output_format: str) -> int:
    """Extract sample rate from a format token like 'pcm_16000'."""
    parts = output_format.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return 16000


def _is_mp3_format(output_format: str) -> bool:
    return output_format.lower().startswith("mp3")


def _estimate_duration_ms(audio_bytes: bytes, sample_rate: int, is_mp3: bool) -> float:
    """Best-effort duration estimate without full decode."""
    if is_mp3:
        # Approximate: average MP3 bitrate ~128kbps
        return (len(audio_bytes) / 16000) * 1000
    # PCM int16: 2 bytes per sample, mono
    samples = len(audio_bytes) / _BYTES_PER_SAMPLE_INT16
    return (samples / sample_rate) * 1000


# ---------------------------------------------------------------------------
# Main provider
# ---------------------------------------------------------------------------

class VoiceAIProvider(BaseTTSEngine):
    """Voice.ai WebSocket streaming TTS provider.

    Implements the BaseTTSEngine interface so it can be swapped into any
    slot in the existing TTS fallback chain without modifying callers.

    Streaming path (synthesize_stream):
        Opens a WebSocket connection, sends the synthesis request, and yields
        TTSChunk objects as audio frames arrive from the server.  The first
        chunk is yielded at TTFB (~96ms per voice.ai's benchmark), enabling
        audio playback to begin before synthesis is complete.

    Non-streaming path (synthesize):
        Delegates to synthesize_stream(), collects all chunks, and returns a
        single TTSResult.  Use this when the caller needs the full audio buffer.

    HTTP fallback:
        If the WebSocket connection fails and VOICEAI_HTTP_FALLBACK=true, the
        provider falls back to the HTTP endpoint, which is non-streaming but
        still functional for evaluation purposes.
    """

    def __init__(self, config: TTSConfig) -> None:
        super().__init__(config)
        self._va_config: Optional[VoiceAIConfig] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # BaseTTSEngine interface
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Resolve config from environment and validate connectivity."""
        self._va_config = VoiceAIConfig.from_env()

        # Validate we can reach the HTTP endpoint (lightweight check).
        await self._probe_connectivity()
        self.initialized = True
        logger.info(
            "[VoiceAI] Provider initialized. endpoint=%s voice=%s format=%s",
            self._cfg.ws_endpoint,
            self._cfg.voice_id or "(server default)",
            self._cfg.output_format,
        )

    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize text to speech, returning a complete audio buffer.

        Internally streams via WebSocket and collects all chunks.
        """
        self._assert_initialized()

        audio_chunks: List[bytes] = []
        probe = LatencyProbe()
        chunk_index = 0

        async for chunk in self.synthesize_stream(text):
            audio_chunks.append(chunk.audio_data)
            if chunk_index == 0:
                probe.mark_first_byte()
            chunk_index += 1

        probe.mark_last_byte()

        audio_data = b"".join(audio_chunks)
        is_mp3 = _is_mp3_format(self._cfg.output_format)
        duration_ms = _estimate_duration_ms(audio_data, self._cfg.sample_rate, is_mp3)

        logger.debug(
            "[VoiceAI] synthesize complete. bytes=%d ttfb=%.1fms total=%.1fms",
            len(audio_data),
            probe.ttfb_ms or 0,
            probe.total_ms or 0,
        )

        return TTSResult(
            audio_data=audio_data,
            sample_rate=self._cfg.sample_rate,
            duration_ms=duration_ms,
            latency_ms=probe.ttfb_ms or 0.0,
            engine=TTSEngine.VOICEAI,
            voice=self._cfg.voice_id or "server_default",
            metadata={
                "ttfb_ms": probe.ttfb_ms,
                "total_ms": probe.total_ms,
                "output_format": self._cfg.output_format,
                "model": self._cfg.model,
            },
        )

    async def synthesize_stream(self, text: str) -> AsyncIterator[TTSChunk]:
        """Stream synthesized audio chunks via WebSocket.

        Yields TTSChunk objects as audio frames arrive from the voice.ai server.
        The first chunk is yielded at TTFB, enabling immediate playback.

        Falls back to HTTP synthesis if WebSocket fails and http_fallback is enabled.
        """
        self._assert_initialized()

        if not text or not text.strip():
            return

        try:
            async for chunk in self._ws_stream(text):
                yield chunk
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            if self._cfg.http_fallback:
                logger.warning(
                    "[VoiceAI] WebSocket failed (%s), falling back to HTTP.", exc
                )
                async for chunk in self._http_stream(text):
                    yield chunk
            else:
                raise

    async def get_available_voices(self) -> List[str]:
        """Fetch available voice IDs from voice.ai's voices endpoint."""
        self._assert_initialized()
        session = await self._get_session()
        try:
            async with session.get(
                self._cfg.voices_endpoint,
                headers=self._cfg.auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                # Accommodate both {"voices": [...]} and flat list responses.
                voices_raw = data if isinstance(data, list) else data.get("voices", [])
                return [
                    v.get("voice_id") or v.get("id") or str(v)
                    for v in voices_raw
                    if v
                ]
        except Exception as exc:
            logger.error("[VoiceAI] Failed to fetch voices: %s", exc)
            return []

    async def cleanup(self) -> None:
        """Close the shared aiohttp session."""
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
        self.initialized = False
        logger.info("[VoiceAI] Provider cleaned up.")

    # ------------------------------------------------------------------
    # WebSocket streaming core
    # ------------------------------------------------------------------

    async def _ws_stream(self, text: str) -> AsyncIterator[TTSChunk]:
        """Open a WebSocket connection and yield audio chunks as they arrive.

        Protocol (adjust to match voice.ai's actual API spec):
          1. Connect with Authorization header.
          2. Send JSON synthesis request.
          3. Receive frames:
               - bytes  → audio data, yield as TTSChunk
               - str    → JSON control message; {"type": "end"} signals completion
          4. Close connection after end-of-stream signal or server close.
        """
        session = await self._get_session()
        va = self._cfg
        probe = LatencyProbe()
        chunk_index = 0
        is_mp3 = _is_mp3_format(va.output_format)

        # ws_receive caps the maximum time between consecutive frames,
        # effectively bounding total synthesis time.
        ws_timeout = aiohttp.ClientWSTimeout(
            ws_receive=va.synthesis_timeout,
        )

        async with session.ws_connect(
            va.ws_endpoint,
            headers=va.auth_headers(),
            timeout=ws_timeout,
            heartbeat=20.0,
        ) as ws:
            # Send synthesis request.
            await ws.send_json(va.build_synthesis_request(text))
            probe.request_sent_at = time.perf_counter()

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    probe.mark_first_byte()
                    audio_bytes = msg.data
                    duration_ms = _estimate_duration_ms(
                        audio_bytes, va.sample_rate, is_mp3
                    )
                    yield TTSChunk(
                        audio_data=audio_bytes,
                        chunk_index=chunk_index,
                        is_final=False,
                        sample_rate=va.sample_rate,
                        duration_ms=duration_ms,
                        metadata={
                            "ttfb_ms": probe.ttfb_ms,
                            "output_format": va.output_format,
                        },
                    )
                    chunk_index += 1

                elif msg.type == aiohttp.WSMsgType.TEXT:
                    control = _parse_control_message(msg.data)
                    if control.get("type") == "end":
                        # Server signals end-of-stream.
                        break
                    elif control.get("type") == "error":
                        raise RuntimeError(
                            f"[VoiceAI] Server error: {control.get('message', msg.data)}"
                        )
                    # Any other text frames are informational; log and continue.
                    logger.debug("[VoiceAI] Control frame: %s", control)

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise aiohttp.ClientError(
                        f"[VoiceAI] WebSocket error: {ws.exception()}"
                    )

                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break

        # Emit a terminal chunk so callers that track is_final get a clean signal.
        if chunk_index > 0:
            yield TTSChunk(
                audio_data=b"",
                chunk_index=chunk_index,
                is_final=True,
                sample_rate=va.sample_rate,
                duration_ms=0.0,
                metadata={
                    "ttfb_ms": probe.ttfb_ms,
                    "total_ms": (time.perf_counter() - probe.request_sent_at) * 1000,
                },
            )

        logger.debug(
            "[VoiceAI] WS stream complete. chunks=%d ttfb=%.1fms",
            chunk_index,
            probe.ttfb_ms or 0,
        )

    # ------------------------------------------------------------------
    # HTTP fallback (non-streaming)
    # ------------------------------------------------------------------

    async def _http_stream(self, text: str) -> AsyncIterator[TTSChunk]:
        """HTTP fallback: single request, single audio chunk response.

        Used when WebSocket is unavailable.  No streaming benefit — full
        synthesis completes before the first byte is returned.
        """
        session = await self._get_session()
        va = self._cfg
        probe = LatencyProbe()

        async with session.post(
            va.http_endpoint,
            headers={**va.auth_headers(), "Content-Type": "application/json"},
            json=va.build_synthesis_request(text),
            timeout=aiohttp.ClientTimeout(total=va.synthesis_timeout),
        ) as resp:
            resp.raise_for_status()
            audio_data = await resp.read()

        probe.mark_first_byte()
        probe.mark_last_byte()
        is_mp3 = _is_mp3_format(va.output_format)
        duration_ms = _estimate_duration_ms(audio_data, va.sample_rate, is_mp3)

        logger.debug(
            "[VoiceAI] HTTP fallback complete. bytes=%d ttfb=%.1fms",
            len(audio_data),
            probe.ttfb_ms or 0,
        )

        yield TTSChunk(
            audio_data=audio_data,
            chunk_index=0,
            is_final=True,
            sample_rate=va.sample_rate,
            duration_ms=duration_ms,
            metadata={
                "ttfb_ms": probe.ttfb_ms,
                "total_ms": probe.total_ms,
                "fallback": "http",
                "output_format": va.output_format,
            },
        )

    # ------------------------------------------------------------------
    # Connectivity probe
    # ------------------------------------------------------------------

    async def _probe_connectivity(self) -> None:
        """Lightweight check that the voices endpoint is reachable.

        Raises EnvironmentError if unreachable so initialization fails fast
        rather than silently producing a broken provider.
        """
        session = await self._get_session()
        try:
            async with session.get(
                self._cfg.voices_endpoint,
                headers=self._cfg.auth_headers(),
                timeout=aiohttp.ClientTimeout(total=self._cfg.connect_timeout),
            ) as resp:
                # 401 = reachable but bad key. 200/404 = reachable.
                # Any response means the network path is open.
                if resp.status == 401:
                    raise EnvironmentError(
                        f"[VoiceAI] API key rejected (HTTP 401). "
                        f"Check VOICEAI_API_KEY."
                    )
                logger.debug("[VoiceAI] Connectivity probe: HTTP %d", resp.status)
        except aiohttp.ClientConnectorError as exc:
            raise EnvironmentError(
                f"[VoiceAI] Cannot reach {self._cfg.voices_endpoint}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared aiohttp session, creating it if needed.

        A single persistent session is reused across all requests to
        avoid repeated TCP handshake overhead.
        """
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
                self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _assert_initialized(self) -> None:
        if not self.initialized or self._va_config is None:
            raise RuntimeError(
                "[VoiceAI] Provider not initialized. Call await provider.initialize() first."
            )

    @property
    def _cfg(self) -> VoiceAIConfig:
        """Type-narrowed accessor — only valid after initialize()."""
        assert self._va_config is not None, "VoiceAIProvider not initialized"
        return self._va_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_control_message(raw: str) -> Dict:
    """Parse a JSON control frame from the server, returning {} on failure."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("[VoiceAI] Non-JSON text frame: %s", raw[:200])
        return {}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_voiceai_provider() -> VoiceAIProvider:
    """Construct a VoiceAIProvider from the standard TTSConfig + env vars.

    Call await provider.initialize() before use.

    Example::

        provider = build_voiceai_provider()
        await provider.initialize()

        # Stream chunks:
        async for chunk in provider.synthesize_stream("Hello, Derek."):
            if chunk.audio_data:
                play_audio(chunk.audio_data, chunk.sample_rate)

        # Or collect full buffer:
        result = await provider.synthesize("Hello, Derek.")
        play_audio(result.audio_data, result.sample_rate)

        await provider.cleanup()
    """
    config = TTSConfig(
        name="voiceai",
        engine=TTSEngine.VOICEAI,
        sample_rate=_parse_sample_rate(_env("VOICEAI_OUTPUT_FORMAT", "pcm_16000")),
        api_key=_env("VOICEAI_API_KEY"),
        voice=_env("VOICEAI_VOICE_ID"),
    )
    return VoiceAIProvider(config)
