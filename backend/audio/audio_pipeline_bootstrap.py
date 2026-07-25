"""
Audio Pipeline Bootstrap (Composition Layer)
=============================================

Two-phase factory that creates, wires, and manages the lifecycle of all
real-time voice conversation components.

Phase 1 (early): AudioBus — called before narrator, fast (~1s)
Phase 2 (late):  ConversationPipeline — called after Intelligence, heavier (~5s)

Usage in supervisor:
    # Phase 1 (early startup)
    audio_bus = await audio_pipeline_bootstrap.start_audio_bus()

    # Phase 2 (after Intelligence phase provides LLM client)
    handle = await audio_pipeline_bootstrap.wire_conversation_pipeline(
        audio_bus=audio_bus,
        llm_client=model_serving,
        speech_state=speech_state_manager,
    )

    # Shutdown
    await audio_pipeline_bootstrap.shutdown(handle)
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def _can_start_streaming_stt_now() -> tuple[bool, str]:
    """Check startup ASR admission before loading faster-whisper."""
    if os.getenv("JARVIS_ASR_ADMISSION_FORCE_OPEN", "").lower() in ("1", "true", "yes", "on"):
        return True, "forced_open"
    if os.getenv("JARVIS_ASR_ADMISSION_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
        return True, "admission_disabled"
    if os.getenv("JARVIS_ASR_ADMISSION_OPEN", "").lower() in ("1", "true", "yes", "on"):
        return True, "admitted"
    if os.getenv("JARVIS_STARTUP_COMPLETE", "").lower() == "true":
        return True, "startup_complete"
    return False, os.getenv("JARVIS_ASR_ADMISSION_REASON", "startup_barrier")


@dataclass
class PipelineHandle:
    """Lifecycle handle returned by wire_conversation_pipeline()."""
    audio_bus: object = None
    streaming_stt: object = None
    turn_detector: object = None
    barge_in: object = None
    tts_engine: object = None
    conversation_pipeline: object = None
    mode_dispatcher: object = None
    health_task: Optional[asyncio.Task] = None
    _bargein_vad_consumer: object = None  # stored for unregister on shutdown
    _mic_telemetry_task: Optional["asyncio.Task"] = None
    karen: object = None  # Sprint 3: full-duplex control layer (env-gated mount)
    voice_build: object = None  # Sprint 4: voice->build bridge (env-gated mount)
    audio_ipc: object = None  # 2026-07-18: audio-state UDS broadcaster (ov CLI subscribes)

    def get_status(self) -> dict:
        """Aggregate status from all components."""
        status = {}
        for name in [
            "audio_bus", "streaming_stt", "turn_detector",
            "barge_in", "conversation_pipeline", "mode_dispatcher",
        ]:
            comp = getattr(self, name, None)
            if comp is not None and hasattr(comp, "get_status"):
                try:
                    status[name] = comp.get_status()
                except Exception as e:
                    status[name] = {"error": str(e)}
            else:
                status[name] = None
        return status


async def start_audio_bus(timeout: float = 5.0):
    """
    Phase 1: Start AudioBus. Called early, before narrator.

    Creates AudioBus singleton + FullDuplexDevice + AEC.
    Returns the AudioBus instance (or None on failure).
    """
    try:
        from backend.audio.audio_bus import AudioBus
        bus = AudioBus.get_instance()
        # Shield to prevent singleton half-init on timeout (see MEMORY.md)
        await asyncio.wait_for(asyncio.shield(bus.start()), timeout=timeout)
        logger.info("[Bootstrap] AudioBus started (Phase 1)")
        return bus
    except asyncio.TimeoutError:
        logger.warning(f"[Bootstrap] AudioBus start timed out ({timeout}s)")
        return None
    except Exception as e:
        logger.warning(f"[Bootstrap] AudioBus start failed: {e}")
        return None


async def wire_conversation_pipeline(
    audio_bus,
    llm_client=None,
    speech_state=None,
    stt_timeout: float = 10.0,
    tts_timeout: float = 15.0,
) -> PipelineHandle:
    """
    Phase 2: Wire all conversation pipeline components.

    Called after Intelligence phase so llm_client (UnifiedModelServing) is
    available. Each sub-component is independently optional — partial
    wiring is OK (degraded mode).

    Returns a PipelineHandle for lifecycle management.
    """
    handle = PipelineHandle(audio_bus=audio_bus)
    loop = asyncio.get_running_loop()

    # 1. TTS singleton
    try:
        from backend.voice.engines.unified_tts_engine import get_tts_engine
        handle.tts_engine = await asyncio.wait_for(
            get_tts_engine(), timeout=tts_timeout,
        )
        logger.info("[Bootstrap] TTS singleton ready")
    except Exception as e:
        logger.warning(f"[Bootstrap] TTS init skipped: {e}")

    # 2. StreamingSTT — register as AudioBus mic consumer
    try:
        stt_allowed, stt_reason = _can_start_streaming_stt_now()
        if not stt_allowed:
            logger.info(
                "[Bootstrap] StreamingSTT deferred by admission gate: %s",
                stt_reason,
            )
            return handle
        from backend.voice.streaming_stt import StreamingSTTEngine
        handle.streaming_stt = StreamingSTTEngine()
        await asyncio.wait_for(handle.streaming_stt.start(), timeout=stt_timeout)

        if audio_bus is not None:
            audio_bus.register_mic_consumer(handle.streaming_stt.on_audio_frame)
            logger.info("[Bootstrap] StreamingSTT registered on AudioBus")
        else:
            logger.info("[Bootstrap] StreamingSTT started (no AudioBus)")
    except Exception as e:
        logger.warning(f"[Bootstrap] StreamingSTT init skipped: {e}")
        handle.streaming_stt = None

    # 3. TurnDetector + BargeInController
    try:
        from backend.audio.turn_detector import TurnDetector
        from backend.audio.barge_in_controller import BargeInController

        handle.turn_detector = TurnDetector()

        handle.barge_in = BargeInController()
        handle.barge_in.set_loop(loop)

        if audio_bus is not None:
            handle.barge_in.set_audio_bus(audio_bus)
            # Register barge-in VAD callback as AudioBus mic consumer.
            handle._bargein_vad_consumer = _create_bargein_vad_consumer(
                handle.barge_in,
                # DRY (mandate #3): the SAME per-frame VAD decision drives both the
                # legacy barge-in controller and Karen's arbiter — no second loop.
                # handle.karen resolves at frame-time (mounted just below).
                karen_vad=lambda s: (
                    (
                        handle.karen.on_vad(s)
                        if handle.karen is not None else None
                    ),
                    # Same per-frame VAD decision feeds the audio-state
                    # IPC (edge-coalesced inside publish_vad) — the ov
                    # cockpit sees VAD_ACTIVE/INACTIVE with no second
                    # VAD loop. Late-bound like handle.karen.
                    (
                        handle.audio_ipc.publish_vad(bool(s))
                        if handle.audio_ipc is not None else None
                    ),
                )[0],
            )
            audio_bus.register_mic_consumer(handle._bargein_vad_consumer)
            logger.info("[Bootstrap] BargeInController registered on AudioBus")

        if speech_state is not None:
            handle.barge_in.set_speech_state(speech_state)

    except Exception as e:
        logger.warning(f"[Bootstrap] TurnDetector/BargeIn skipped: {e}")

    # 3a-ipc. Audio-state IPC broadcaster (operator-signed 2026-07-18):
    #     the supervisor owns the audio hardware plane; the ov cockpit
    #     subscribes to STATE over this UDS instead of binding audio.
    #     Read-only telemetry export — a bind failure never touches the
    #     pipeline. VAD edges publish through the SAME per-frame decision
    #     that drives barge-in + Karen (DRY — no second VAD loop).
    try:
        from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (
            AudioStateBroadcaster,
            audio_ipc_enabled,
        )
        if audio_ipc_enabled():
            # Tri-State broker lease seam (operator-authorized
            # 2026-07-18): a remote `wake` (ov attach → daemon broker)
            # arms the SUPERVISOR-owned duplex; disarm stops it. The
            # closure late-binds `handle` (karen mounts at 3b, AFTER
            # this) and lazy-builds over the SAME factory when voice
            # wasn't pre-mounted — sovereignty never leaves this
            # process; the broadcaster only relays the verb.
            def _wire_interruption_reporter() -> None:
                # Semantic Interruption Awareness: barge-in/flush cuts
                # route the truncation estimate into ConversationBridge
                # so the next GENERATE knows the narration was cut off.
                try:
                    from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
                        record_barge_in,
                    )
                    if handle.karen is not None:
                        handle.karen.arbiter.on_interruption = record_barge_in
                except Exception:
                    pass

            async def _on_lease_change(armed: bool) -> None:
                try:
                    if armed:
                        if handle.karen is None and handle.tts_engine is not None:
                            from backend.core.ouroboros.governance.comms.duplex.karen_duplex_factory import (  # noqa: E501
                                build_karen_duplex,
                                set_default_karen,
                            )
                            handle.karen = build_karen_duplex(handle.tts_engine)
                            set_default_karen(handle.karen)
                            logger.info(
                                "[Bootstrap] Karen duplex lazy-mounted on lease",
                            )
                        if handle.karen is not None:
                            # Hardware Topology Survival: the arbiter's
                            # stream-fault reporter routes into the
                            # broker's fail-safe (revoke + disarm +
                            # HW_FAULT broadcast). Late-bound so a
                            # lazy-mounted duplex is covered too.
                            try:
                                handle.karen.arbiter.on_hardware_fault = (
                                    lambda exc: handle.audio_ipc.publish_hardware_fault(str(exc))  # noqa: E501
                                    if handle.audio_ipc is not None else None
                                )
                            except Exception:
                                pass
                            _wire_interruption_reporter()
                            await handle.karen.start()
                            logger.info("[Bootstrap] audio lease ARMED")
                    else:
                        if handle.karen is not None:
                            await handle.karen.stop()
                            logger.info(
                                "[Bootstrap] audio lease DISARMED (fail-safe)",
                            )
                except Exception:
                    logger.warning(
                        "[Bootstrap] lease arm/disarm degraded", exc_info=True,
                    )

            def _on_flush() -> None:
                # TTS interruption (ducking): instantaneous outbound
                # halt on the arbiter's own flush seam. Sync + inline.
                try:
                    if handle.karen is not None:
                        handle.karen.arbiter.flush()
                except Exception:
                    logger.debug(
                        "[Bootstrap] lease flush degraded", exc_info=True,
                    )

            handle.audio_ipc = AudioStateBroadcaster(
                on_lease_change=_on_lease_change,
                on_flush=_on_flush,
            )
            if not await handle.audio_ipc.start():
                handle.audio_ipc = None
            else:
                logger.info("[Bootstrap] Audio-state IPC broadcaster mounted")
            # Ambient Phase 2 (2026-07-19): NSWorkspace wake observer →
            # conditional Daniel briefing with the Coffee-Shop guard.
            # Dormant without pyobjc; gated JARVIS_AMBIENT_WAKE_ENABLED
            # (default off — §33.1 graduation contract for a surface
            # that SPEAKS unprompted).
            try:
                if os.getenv(
                    "JARVIS_AMBIENT_WAKE_ENABLED", "",
                ).strip().lower() in ("1", "true", "yes", "on"):
                    from backend.core.ouroboros.governance.comms.duplex.ambient import (  # noqa: E501
                        SystemWakeObserver,
                    )
                    handle.wake_observer = SystemWakeObserver()
                    if handle.wake_observer.start():
                        logger.info(
                            "[Bootstrap] ambient wake observer armed",
                        )
            except Exception:
                logger.debug(
                    "[Bootstrap] ambient wake observer skipped",
                    exc_info=True,
                )
            # PASSIVE_SENTRY final mount (2026-07-19): the total gate
            # lives INSIDE mount_passive_sentry — when
            # JARVIS_PASSIVE_SENTRY_ENABLED is down, zero sentry
            # imports/threads/deques/runloops are instantiated.
            try:
                from backend.core.ouroboros.governance.comms.duplex.sentry_bootstrap import (  # noqa: E501
                    mount_passive_sentry,
                )
                handle.passive_sentry = mount_passive_sentry(
                    broadcaster=handle.audio_ipc,
                    mic_register=lambda cb: audio_bus.register_mic_consumer(cb),
                )
            except Exception:
                handle.passive_sentry = None
                logger.debug(
                    "[Bootstrap] passive sentry mount skipped", exc_info=True,
                )
        else:
            handle.audio_ipc = None
    except Exception as e:
        handle.audio_ipc = None
        logger.warning(f"[Bootstrap] audio-state IPC skipped: {e}")

    # 3a-tel. Mic amplitude telemetry → ov cockpit (the data plane).
    #
    # The control plane (VAD edges) already crosses this UDS; this adds the
    # AMPLITUDE so the cockpit's Braille oscilloscope can draw a live waveform
    # instead of a binary talking/not-talking light.
    #
    # Thread-safety is the whole design constraint. AudioBus invokes mic
    # consumers on the PortAudio C thread, which must never block and cannot
    # touch the asyncio loop. So the callback does one O(1) thing — hand a
    # zero-copy view to a latest-wins mailbox — and this task, running on the
    # orchestrator's own loop, does the RMS and the socket write. The two
    # sides never share a lock the audio thread could wait on.
    #
    # Reuses the SAME mailbox the broadcast tap already provides (DRY: no
    # second ring buffer) and the SAME UDS the VAD edges use (no second
    # socket). Fully fail-soft: any fault here leaves capture untouched.
    try:
        from backend.audio.mic_telemetry_bridge import (
            bridge_enabled, ensure_attached, pump_once,
        )
        if bridge_enabled() and audio_bus is not None:
            _tel_bridge = ensure_attached(server=handle.audio_ipc)
            if _tel_bridge is not None:
                _tel_interval = 1.0 / max(1.0, float(
                    os.environ.get("JARVIS_MIC_TELEMETRY_FPS", "20"),
                ))

                async def _mic_telemetry_loop() -> None:
                    """Drain the mailbox onto the UDS at the telemetry rate.

                    Sleeps a fixed tick rather than awaiting a signal: the
                    mailbox is latest-wins, so there is nothing to wake for —
                    an empty drain is a no-op and a full one is one RMS. Never
                    raises out; a telemetry fault must not end the pipeline."""
                    while True:
                        try:
                            pump_once(handle.audio_ipc, plane="user")
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001
                            pass
                        await asyncio.sleep(_tel_interval)

                handle._mic_telemetry_task = loop.create_task(
                    _mic_telemetry_loop(),
                )
                logger.info(
                    "[Bootstrap] Mic telemetry bridged to ov cockpit "
                    "(%.0f FPS, lossy valve)", 1.0 / _tel_interval,
                )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Bootstrap] Mic telemetry skipped: {e}")

    # 3b. Karen full-duplex control layer (Sprint 3) — env-gated adaptive mount.
    #     Default OFF: the pipeline pays zero voice-allocation overhead and its
    #     lifecycle is byte-identical to before (mandate #2). When enabled, the
    #     arbiter speaks through the real streaming TTS and the shared barge-in
    #     VAD (above) drives it. Fault-isolated — a mount failure never touches
    #     the FSM (mandate #4).
    if os.getenv("JARVIS_KAREN_VOICE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from backend.core.ouroboros.governance.comms.duplex.karen_duplex_factory import (
                build_karen_duplex,
                set_default_karen,
            )
            handle.karen = build_karen_duplex(handle.tts_engine)
            await handle.karen.start()
            set_default_karen(handle.karen)
            # Hardware Topology Survival — same fault route as the
            # lease-armed path (a pre-mounted duplex is equally exposed
            # to device vanish).
            try:
                if handle.audio_ipc is not None:
                    handle.karen.arbiter.on_hardware_fault = (
                        lambda exc: handle.audio_ipc.publish_hardware_fault(str(exc))  # noqa: E501
                        if handle.audio_ipc is not None else None
                    )
            except Exception:
                pass
            try:
                from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
                    record_barge_in as _rbi,
                )
                handle.karen.arbiter.on_interruption = _rbi
            except Exception:
                pass
            logger.info("[Bootstrap] Karen full-duplex control layer mounted")
        except Exception as e:
            handle.karen = None
            try:
                from backend.core.ouroboros.governance.comms.duplex.karen_duplex_factory import (
                    set_default_karen,
                )
                set_default_karen(None)
            except Exception:
                pass
            logger.warning(f"[Bootstrap] Karen duplex mount skipped: {e}")

    # 4. ConversationPipeline
    try:
        from backend.audio.conversation_pipeline import ConversationPipeline

        handle.conversation_pipeline = ConversationPipeline(
            audio_bus=audio_bus,
            streaming_stt=handle.streaming_stt,
            turn_detector=handle.turn_detector,
            barge_in=handle.barge_in,
            tts_engine=handle.tts_engine,
            llm_client=llm_client,
        )
        logger.info("[Bootstrap] ConversationPipeline created")
    except Exception as e:
        logger.warning(f"[Bootstrap] ConversationPipeline init skipped: {e}")

    # 4b. Karen voice->build bridge (Sprint 4) — env-gated adaptive mount.
    #     Default OFF: zero allocation/behavior change on the pipeline (mandate #2).
    #     When enabled, completed conversation turns are forked (not replaced) into
    #     the voice->build classify+route path — the LLM chat reply is untouched
    #     (DRY, mandate #3). Fault-isolated — a mount failure never touches the FSM.
    if os.getenv("JARVIS_KAREN_VOICE_BUILD_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from backend.core.ouroboros.governance.comms.voice_build.bridge import VoiceBuildBridge
            # Lazy — resolves the sensor per-call via get_default_voice_sensor()
            # inside the bridge, so mount order relative to IntakeLayerService
            # publishing the sensor no longer matters (Sprint 4 review fix).
            handle.voice_build = VoiceBuildBridge()
            # Fork completed turns into voice->build (the LLM chat path is untouched — DRY).
            if handle.conversation_pipeline is not None:
                handle.conversation_pipeline._on_turn_text = handle.voice_build.on_final_transcript
            logger.info("[Bootstrap] Karen voice->build bridge mounted")
        except Exception as e:
            handle.voice_build = None
            logger.warning(f"[Bootstrap] voice->build mount skipped: {e}")

    # 4c. Audio-state IPC transcript + speech hooks (2026-07-18).
    #     Composes — never replaces — the existing forks:
    #       * user turns: chained _on_turn_text wrapper publishes the
    #         final transcript to the IPC THEN forwards to whatever fork
    #         was already installed (voice->build stays untouched);
    #       * Karen lines: submit_speech is wrapped to publish the line
    #         (role=karen) + TTS_GENERATING before the arbiter enqueue.
    #     Fault-isolated: hook failures degrade silently; the audio
    #     pipeline is byte-identical when the broadcaster is absent.
    if handle.audio_ipc is not None:
        try:
            _ipc = handle.audio_ipc
            if handle.conversation_pipeline is not None:
                _prev_fork = getattr(
                    handle.conversation_pipeline, "_on_turn_text", None,
                )

                async def _ipc_turn_fork(text, *a, **kw):
                    try:
                        _ipc.publish_transcript("user", str(text), final=True)
                    except Exception:  # noqa: BLE001
                        pass
                    if _prev_fork is not None:
                        return await _prev_fork(text, *a, **kw)
                    return None

                handle.conversation_pipeline._on_turn_text = _ipc_turn_fork
            if handle.karen is not None:
                _prev_submit = handle.karen.submit_speech

                def _ipc_submit_speech(text, *a, **kw):
                    try:
                        _ipc.publish_transcript("karen", str(text), final=True)
                        _ipc.publish_event("TTS_GENERATING")
                    except Exception:  # noqa: BLE001
                        pass
                    return _prev_submit(text, *a, **kw)

                handle.karen.submit_speech = _ipc_submit_speech
            logger.info("[Bootstrap] audio-state IPC transcript hooks wired")
        except Exception as e:
            logger.warning(f"[Bootstrap] audio-state IPC hooks skipped: {e}")

    # 5. ModeDispatcher
    try:
        from backend.audio.mode_dispatcher import ModeDispatcher

        handle.mode_dispatcher = ModeDispatcher(
            conversation_pipeline=handle.conversation_pipeline,
            speech_state=speech_state,
        )
        # Wire AudioBus and TTS for biometric mode + speaker verification
        if audio_bus is not None:
            handle.mode_dispatcher.set_audio_bus(audio_bus)
        if handle.tts_engine is not None:
            handle.mode_dispatcher.set_tts_engine(handle.tts_engine)
        await handle.mode_dispatcher.start()
        logger.info("[Bootstrap] ModeDispatcher started")
    except Exception as e:
        logger.warning(f"[Bootstrap] ModeDispatcher init skipped: {e}")

    # 6. Register ModeDispatcher transcript hook on voice communicator
    try:
        from backend.agi_os.realtime_voice_communicator import (
            get_voice_communicator,
        )
        _communicator = await get_voice_communicator()
        if _communicator is not None and handle.mode_dispatcher is not None:
            if hasattr(_communicator, "register_transcript_hook"):
                _communicator.register_transcript_hook(
                    handle.mode_dispatcher.handle_transcript
                )
                logger.info("[Bootstrap] ModeDispatcher registered as transcript hook")
            else:
                logger.debug("[Bootstrap] Voice communicator lacks register_transcript_hook")
    except Exception as e:
        logger.debug(f"[Bootstrap] Transcript hook registration skipped: {e}")

    return handle


def _create_bargein_vad_consumer(barge_in, karen_vad=None):
    """
    Create a mic consumer callback that runs energy-based VAD and feeds
    results to the BargeInController.

    ``karen_vad`` (optional): a sync ``(is_speech: bool) -> None`` sink that
    receives the SAME per-frame VAD decision, so one energy analysis serves both
    the legacy barge-in controller and the Karen duplex arbiter (DRY, mandate #3
    — no second frame loop).

    Runs in the audio thread — must be fast and non-blocking.
    """
    _energy_threshold = float(os.getenv("JARVIS_BARGEIN_ENERGY_THRESHOLD", "0.01"))

    def _on_frame(frame: np.ndarray) -> None:
        if frame.size == 0:
            return
        energy = float(np.sqrt(np.mean(frame ** 2)))
        is_speech = energy > _energy_threshold
        barge_in.on_vad_speech_detected(is_speech)
        if karen_vad is not None:
            try:
                karen_vad(is_speech)   # audio-thread-safe marshal lives inside
            except Exception:
                pass

    return _on_frame


async def shutdown(handle: PipelineHandle) -> None:
    """
    Shutdown all components in reverse order.
    Each step has a timeout to prevent shutdown stall.
    """
    _timeout = 5.0

    # 0. Karen full-duplex control layer (Sprint 3) — stop first so its arbiter
    #    run loop + VAD bridge tear down before the audio components they use.
    if getattr(handle, "karen", None) is not None:
        try:
            await asyncio.wait_for(handle.karen.stop(), timeout=_timeout)
        except Exception as e:
            logger.debug(f"[Bootstrap] Karen duplex stop error: {e}")
        try:
            from backend.core.ouroboros.governance.comms.duplex.karen_duplex_factory import (
                set_default_karen,
            )
            set_default_karen(None)
        except Exception:
            pass

    # 0b. Karen voice->build bridge (Sprint 4) — drop the ref, no async teardown needed.
    handle.voice_build = None

    # 0c. Audio-state IPC broadcaster — close clients + unlink the socket
    #     so a later boot never sees a stale sock file.
    if getattr(handle, "audio_ipc", None) is not None:
        try:
            await asyncio.wait_for(handle.audio_ipc.stop(), timeout=_timeout)
        except Exception as e:
            logger.debug(f"[Bootstrap] audio-state IPC stop error: {e}")
        handle.audio_ipc = None

    # 1. ModeDispatcher
    if handle.mode_dispatcher is not None:
        try:
            await asyncio.wait_for(handle.mode_dispatcher.stop(), timeout=_timeout)
        except Exception as e:
            logger.debug(f"[Bootstrap] ModeDispatcher stop error: {e}")

    # 1b. Stop speaker verification (unregisters its AudioBus consumer)
    if handle.mode_dispatcher is not None:
        try:
            if hasattr(handle.mode_dispatcher, '_stop_speaker_verification'):
                await asyncio.wait_for(
                    handle.mode_dispatcher._stop_speaker_verification(),
                    timeout=_timeout,
                )
        except Exception:
            pass

    # 2. BargeInController — disable before pipeline teardown to prevent
    #    call_soon_threadsafe on a closing event loop from the audio thread.
    if handle.barge_in is not None:
        try:
            handle.barge_in.enabled = False
            handle.barge_in.set_loop(None)
        except Exception:
            pass

    # 2a-tel. Stop mic telemetry first — it reads the bus and the IPC server,
    # both of which are torn down below.
    if handle._mic_telemetry_task is not None:
        try:
            handle._mic_telemetry_task.cancel()
        except Exception:
            pass
        handle._mic_telemetry_task = None
    try:
        from backend.audio.mic_telemetry_bridge import reset_bridge
        reset_bridge()          # unregisters the mic consumer
    except Exception:
        pass

    # 2b. Unregister barge-in VAD consumer from AudioBus
    if handle._bargein_vad_consumer is not None and handle.audio_bus is not None:
        try:
            handle.audio_bus.unregister_mic_consumer(handle._bargein_vad_consumer)
        except Exception:
            pass

    # 3. ConversationPipeline
    if handle.conversation_pipeline is not None:
        try:
            await asyncio.wait_for(
                handle.conversation_pipeline.end_session(), timeout=_timeout,
            )
        except Exception as e:
            logger.debug(f"[Bootstrap] ConversationPipeline end error: {e}")

    # 3. StreamingSTT — unregister from AudioBus first
    if handle.streaming_stt is not None:
        if handle.audio_bus is not None:
            try:
                handle.audio_bus.unregister_mic_consumer(
                    handle.streaming_stt.on_audio_frame
                )
            except Exception:
                pass
        try:
            await asyncio.wait_for(handle.streaming_stt.stop(), timeout=_timeout)
        except Exception as e:
            logger.debug(f"[Bootstrap] StreamingSTT stop error: {e}")

    # 4. Cancel health task
    if handle.health_task is not None:
        handle.health_task.cancel()
        try:
            await handle.health_task
        except (asyncio.CancelledError, Exception):
            pass

    # Note: AudioBus is NOT stopped here — it has its own lifecycle
    # managed by the supervisor (started in Phase 1, stopped at shutdown).

    logger.info("[Bootstrap] Audio pipeline shutdown complete")
