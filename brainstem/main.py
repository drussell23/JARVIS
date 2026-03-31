"""JARVIS Brainstem — Main entry point.

Boot sequence:
  T+0.0s  Load config, create auth
  T+0.1s  Create sender, HUD
  T+0.3s  Hardware init (AudioBus, Ghost Hands)
  T+2.5s  Vision bridge (lazy, not started unless env says so)
  T+2.5s  Request stream token, connect SSE
  T+3.0s  Start voice intake
  T+3.5s  "JARVIS Online"

Run with: python3 -m brainstem
"""

import asyncio
import logging
import signal
import sys
import time

from brainstem.config import BrainstemConfig
from brainstem.auth import BrainstemAuth
from brainstem.command_sender import CommandSender
from brainstem.sse_consumer import SSEConsumer
from brainstem.action_dispatcher import ActionDispatcher
from brainstem.voice_intake import VoiceIntake
from brainstem.vision_bridge import VisionBridge
from brainstem.hud import HUD
from brainstem.tts import speak

logger = logging.getLogger("jarvis.brainstem")


async def main() -> None:
    boot_start = time.monotonic()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Strip proxy env vars that break Python's urllib/aiohttp.
    # The brainstem inherits https_proxy from the parent process which points
    # to a local proxy (localhost:65403) that can't reach Vercel. Swift's
    # URLSession uses macOS system proxy instead and works fine.
    import os
    for _proxy_key in list(os.environ):
        if _proxy_key.lower() in ("https_proxy", "http_proxy", "all_proxy"):
            logger.info("[Boot] Stripping proxy env: %s=%s", _proxy_key, os.environ[_proxy_key][:30])
            del os.environ[_proxy_key]

    # Phase 1: Config + Auth
    try:
        config = BrainstemConfig.from_env()
    except ValueError as e:
        print(f"[BRAINSTEM] Config error: {e}", file=sys.stderr)
        print("[BRAINSTEM] Required: JARVIS_VERCEL_URL, JARVIS_DEVICE_ID, JARVIS_DEVICE_SECRET", file=sys.stderr)
        sys.exit(1)

    auth = BrainstemAuth(device_id=config.device_id, device_secret=config.device_secret)
    logger.info("[Boot] Config loaded (device=%s)", config.device_id)

    # Phase 2: Create components
    hud = HUD()
    sender = CommandSender(config=config, auth=auth)
    hud.show_status("Booting...")

    # Phase 3: Hardware init (optional — each fails gracefully)
    ghost_hands = None
    audio_bus = None
    stt_engine = None

    try:
        from backend.ghost_hands.yabai_aware_actuator import YabaiAwareActuator
        ghost_hands = YabaiAwareActuator()
        if await ghost_hands.start():
            logger.info("[Boot] Ghost Hands initialized")
        else:
            logger.warning("[Boot] Ghost Hands init returned False")
            ghost_hands = None
    except Exception as e:
        logger.warning("[Boot] Ghost Hands unavailable: %s", e)

    try:
        from backend.audio.audio_bus import AudioBus
        audio_bus = AudioBus()
        await audio_bus.start()
        logger.info("[Boot] AudioBus started")
    except Exception as e:
        logger.warning("[Boot] AudioBus unavailable: %s", e)

    if audio_bus is not None:
        try:
            from backend.voice.streaming_stt import StreamingSTTEngine
            stt_engine = StreamingSTTEngine()
            await stt_engine.start()
            logger.info("[Boot] STT engine started")
        except Exception as e:
            logger.warning("[Boot] STT unavailable: %s", e)

    # Phase 4: Vision bridge (lazy — only auto-starts if env says so)
    vision = VisionBridge()
    if vision.should_auto_activate():
        await vision.activate()

    # Phase 5: Wire dispatcher + consumer + voice
    dispatcher = ActionDispatcher(
        hud=hud,
        ghost_hands=ghost_hands,
        tts_speak=speak,
        jarvis_cu=vision,
    )

    consumer = SSEConsumer(
        config=config,
        auth=auth,
        on_event=dispatcher.dispatch,
    )

    voice = VoiceIntake(
        on_transcript=lambda text: sender.send_command(text=text, priority="realtime"),
    )
    if stt_engine is not None:
        voice.set_stt_engine(stt_engine)

    # Boot complete
    boot_ms = (time.monotonic() - boot_start) * 1000
    hud.show_status(f"JARVIS Online ({boot_ms:.0f}ms)")
    logger.info("[Boot] Complete in %.0fms", boot_ms)

    # Run until shutdown
    shutdown = asyncio.Event()

    def handle_signal(sig: int, _frame: object) -> None:
        logger.info("[Brainstem] Signal %d, shutting down...", sig)
        shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("[Main] Starting SSE consumer + voice intake...")
    try:
        consumer_task = asyncio.create_task(consumer.run(shutdown))
        voice_task = asyncio.create_task(voice.run(shutdown))

        # Surface consumer crashes immediately instead of swallowing them
        async def _watch_consumer() -> None:
            try:
                await consumer_task
            except Exception as exc:
                logger.error("[Main] SSE consumer crashed: %s: %s", type(exc).__name__, exc)
        asyncio.create_task(_watch_consumer())

        await voice_task
    finally:
        hud.show_status("Shutting down...")
        await sender.close()
        await vision.deactivate()
        if audio_bus is not None:
            try:
                await audio_bus.stop()
            except Exception:
                pass
        logger.info("[Brainstem] Shutdown complete")
