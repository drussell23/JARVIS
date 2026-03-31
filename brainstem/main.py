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

    # Phase 0: Accessibility permissions check — required for pyautogui to post
    # synthetic mouse/keyboard events to macOS. Without this, clicks and keypresses
    # are silently dropped by the OS (no exception, no error — just phantom clicks).
    try:
        import ctypes
        _ax_lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        _ax_lib.AXIsProcessTrusted.restype = ctypes.c_bool
        _ax_trusted = _ax_lib.AXIsProcessTrusted()
        if _ax_trusted:
            logger.info("[Boot] Accessibility permissions: GRANTED — Ghost Hands can control the screen")
        else:
            logger.warning(
                "[Boot] *** ACCESSIBILITY PERMISSIONS NOT GRANTED ***\n"
                "  pyautogui clicks/keypresses will be SILENTLY DROPPED by macOS.\n"
                "  Fix: System Settings → Privacy & Security → Accessibility\n"
                "       Add /opt/homebrew/bin/python3.12 AND Xcode to the list.\n"
                "  Voice commands that require screen interaction WILL NOT EXECUTE."
            )
    except Exception as _ax_exc:
        logger.debug("[Boot] Could not check AX permissions: %s", _ax_exc)

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

    # Inbox reader: HUD forwards action events to brainstem via a file-based
    # inbox at .jarvis/brainstem_inbox.jsonl.  The HUD appends JSON lines;
    # this reader polls for new content, processes it, then truncates the file.
    #
    # This replaces the previous stdin pipe approach which was fundamentally
    # broken: select() on inherited pipe fd 0 hangs forever in Python threads
    # when asyncio's event loop is running kqueue on macOS.
    async def _inbox_reader() -> None:
        """Poll the file-based inbox for events from HUD.

        Uses threading.Queue + run_in_executor instead of asyncio.Queue +
        call_soon_threadsafe.  The latter silently fails when the asyncio
        event loop is busy with SSE/FramePipeline tasks and doesn't
        process the threadsafe callback.
        """
        import json as _json
        import fcntl
        import threading
        import queue as thread_queue

        inbox_path = os.path.join(os.getcwd(), ".jarvis", "brainstem_inbox.jsonl")
        logger.info("[Inbox] Watching %s for events from HUD", inbox_path)

        last_mtime: float = 0.0
        q: thread_queue.Queue[str] = thread_queue.Queue()

        def _poll_thread() -> None:
            """Daemon thread that polls the inbox file for new content."""
            nonlocal last_mtime
            while not shutdown.is_set():
                try:
                    time.sleep(0.1)  # 100ms poll interval

                    if not os.path.exists(inbox_path):
                        continue

                    try:
                        st = os.stat(inbox_path)
                    except OSError:
                        continue

                    # Skip if file hasn't been modified and is empty
                    if st.st_mtime == last_mtime and st.st_size == 0:
                        continue
                    if st.st_size == 0:
                        last_mtime = st.st_mtime
                        continue

                    # Read and truncate under flock
                    lines: list[str] = []
                    try:
                        fd = os.open(inbox_path, os.O_RDWR)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX)
                            content = b""
                            while True:
                                chunk = os.read(fd, 65536)
                                if not chunk:
                                    break
                                content += chunk
                            os.ftruncate(fd, 0)
                            os.lseek(fd, 0, os.SEEK_SET)
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        finally:
                            os.close(fd)

                        last_mtime = time.time()

                        if content:
                            text = content.decode("utf-8", errors="replace")
                            lines = [l.strip() for l in text.splitlines() if l.strip()]
                    except OSError as e:
                        logger.debug("[Inbox] Read error (will retry): %s", e)
                        continue

                    # Dispatch directly in the poll thread using a fresh event
                    # loop. The main asyncio event loop's call_soon_threadsafe
                    # is broken in this macOS subprocess context — neither
                    # asyncio.Queue nor run_in_executor can reliably deliver
                    # data from threads to the main loop.
                    for line_text in lines:
                        try:
                            msg = _json.loads(line_text)
                            event_type = msg.get("event_type", "")
                            data = msg.get("data", {})
                            logger.info("[Inbox] Event from HUD: %s (%d chars)", event_type, len(line_text))
                            # Run dispatch in a dedicated event loop for this thread.
                            # This avoids all call_soon_threadsafe / main-loop issues.
                            _dispatch_loop = asyncio.new_event_loop()
                            try:
                                _dispatch_loop.run_until_complete(
                                    dispatcher.dispatch(event_type, data)
                                )
                            finally:
                                _dispatch_loop.close()
                            logger.info("[Inbox] Dispatch complete for %s", event_type)
                        except _json.JSONDecodeError:
                            logger.debug("[Inbox] Non-JSON line: %s", line_text[:100])
                        except Exception as de:
                            logger.error("[Inbox] Dispatch error: %s", de)

                except Exception as e:
                    logger.warning("[Inbox] Poll thread error: %s", e)
                    time.sleep(1.0)

        thread = threading.Thread(target=_poll_thread, daemon=True, name="inbox-reader")
        thread.start()
        logger.info("[Inbox] Poll thread started (100ms interval, direct dispatch)")

        # The poll thread handles everything — async consumer just keeps task alive
        while not shutdown.is_set():
            await asyncio.sleep(1.0)

    logger.info("[Main] Starting inbox reader + SSE consumer + voice intake...")
    try:
        inbox_task = asyncio.create_task(_inbox_reader())
        consumer_task = asyncio.create_task(consumer.run(shutdown))
        voice_task = asyncio.create_task(voice.run(shutdown))

        # Surface crashes immediately
        async def _watch_tasks() -> None:
            for name, task in [("inbox", inbox_task), ("SSE", consumer_task)]:
                try:
                    await task
                except Exception as exc:
                    logger.error("[Main] %s task crashed: %s: %s", name, type(exc).__name__, exc)
        asyncio.create_task(_watch_tasks())

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
