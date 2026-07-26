"""The audio plane, on its own — a microphone without a monolith.

Why this exists
---------------
`ov` needed a process that owns CoreAudio, and the only one that did was
``unified_supervisor.py``: 98K lines that boot the websocket router, the legacy
web app, the model-serving stack and everything else, because somewhere inside
all of it a microphone gets opened. Spawning that to hear "hello Karen" is the
wrong shape twice over — it launches a web UI nobody asked for, and it loads a
local model into the same 16GB the audio path is fighting for.

Nothing about the audio plane actually required the monolith. The whole plane
is one already-standalone call:

    AudioBus.get_instance().start()          # CoreAudio capture
    wire_conversation_pipeline(audio_bus=…)  # STT, TTS, turn detection,
                                             # Karen duplex, the audio-state
                                             # UDS broadcaster, lease table,
                                             # mic telemetry → rms_level frames

``wire_conversation_pipeline`` mounts the IPC broadcaster itself (step 3a-ipc),
so this host adds NO plumbing — it supplies a process, a lifecycle, and an
event loop for that call to live in. Every capability the cockpit talks to
(leases, ``wake``, VAD state, amplitude frames) arrives already wired.

Generation is REMOTE-ONLY here, on purpose
------------------------------------------
``llm_client=None`` flows into ``build_voice_router(None)``, giving an
:class:`AdaptiveVoiceRouter` whose local engine is absent. So Karen's spoken
replies go to the elected DW voice and nowhere else. That is not a limitation
of this host, it is its point: the reason to split the audio plane out was to
stop local inference from competing with capture and synthesis for unified
memory. A host that then loaded a local model would have rebuilt the problem it
was extracted to solve.

If DW is unreachable, Karen goes quiet rather than falling back to local — an
honest failure the cockpit surfaces, not a silent memory spike.

Lifecycle
---------
Runs until signalled, then tears the pipeline and bus down in reverse order and
hard-exits via ``os._exit`` — the same Py_FinalizeEx discipline the supervisor
and the battle-test harness already use, for the same reason: a C extension
null-derefs during interpreter finalization, and the work is done by then
anyway.

Single-flight by socket, not by lockfile. A second host that finds the
audio-state socket already served exits 75 immediately, so racing `ov`
instances converge on one owner of the microphone without coordinating —
CoreAudio would refuse the second handle regardless, and failing fast beats
failing deep.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("JARVIS.AudioPlane")

#: Exit code for "another host already owns the microphone". Mirrors the
#: battle-test harness's single-flight convention so operators read one code.
EXIT_ALREADY_RUNNING = 75


def _log_level() -> int:
    raw = os.environ.get("JARVIS_AUDIO_PLANE_LOG", "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO)


def _acquire_exclusive(stack: Any) -> bool:
    """Take the process-lifetime microphone lock. False = someone else has it.

    THE RACE THE SOCKET PROBE CANNOT SEE. Binding happens ~13s into boot
    (faster-whisper loads first), so "is anything listening?" answers NO for a
    13-second window during which a second host can be spawned, pass the same
    probe, and come up beside the first. Observed live: two hosts, every
    utterance transcribed twice, two whisper instances on one microphone.

    A probe asks "has the winner finished?"; a lock asks "is anyone trying?" —
    and only the second question is answerable at t=0. Reuses the canonical
    ``singleton_lock`` (flock, fail-fast, released by the kernel on exit, so a
    SIGKILLed host cannot leave a lock nobody can clear), on its OWN path so
    the audio plane and a battle-test soak never contend.

    Fails OPEN, matching the helper's contract: a substrate breakage must not
    be able to prevent audio from ever starting."""
    try:
        from backend.core.ouroboros.battle_test.singleton_lock import (
            acquire_singleton,
        )
        root = _repo_root()
        result = stack.enter_context(
            acquire_singleton(root, lock_path=root / ".jarvis" / "audio_plane.lock"),
        )
        return bool(getattr(result, "acquired", True))
    except Exception:  # noqa: BLE001
        logger.debug("[AudioPlane] singleton lock unavailable", exc_info=True)
        return True


async def _socket_already_served(timeout: float = 0.5) -> bool:
    """Is a host already listening on the audio-state socket?

    Connect-and-close, reusing the cockpit reflex's probe rather than stat-ing
    a path: a stale socket inode survives SIGKILL, so file presence proves
    nothing. NEVER raises."""
    try:
        from backend.core.ouroboros.cli.audio_daemon_reflex import probe_socket
        return await probe_socket(timeout=timeout)
    except Exception:  # noqa: BLE001
        return False


class AudioPlaneHost:
    """Owns the microphone and the audio-state socket for its lifetime."""

    def __init__(self) -> None:
        self._bus: Any = None
        self._handle: Any = None
        self._stop = asyncio.Event()
        #: Inode of the socket file this host bound. Identity, not liveness —
        #: the only thing that can detect losing an address.
        self._inode: Optional[int] = None
        #: Proximity re-binder. Absent unless the operator armed it.
        self._adaptive: Any = None
        self._adaptive_task: Optional[asyncio.Task] = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> bool:
        """Bring up capture, then the pipeline. False if the plane cannot
        exist — the caller reports honestly and exits rather than idling as a
        process that owns nothing."""
        try:
            from backend.audio.audio_bus import AudioBus
        except Exception as exc:  # noqa: BLE001
            logger.error("[AudioPlane] AudioBus unavailable: %r", exc)
            return False

        try:
            self._bus = AudioBus.get_instance()
            # Bounded: CoreAudio can hang indefinitely when the device is held
            # by another process or TCC has not been granted, and a host stuck
            # in start() is a host the cockpit waits on forever.
            await asyncio.wait_for(
                self._bus.start(),
                timeout=float(os.environ.get("JARVIS_AUDIO_PLANE_BUS_TIMEOUT_S", "30")),
            )
            logger.info("[AudioPlane] AudioBus capturing")
        except asyncio.TimeoutError:
            logger.error(
                "[AudioPlane] AudioBus.start timed out — the microphone is "
                "held by another process, or macOS has not granted this "
                "terminal microphone access (System Settings → Privacy & "
                "Security → Microphone)",
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("[AudioPlane] AudioBus.start failed: %r", exc)
            return False

        # ASR admission. The barrier exists to stop faster-whisper loading
        # DURING a heavy monolith boot, where it would contend for memory with
        # everything else coming up. In a dedicated audio host there is no such
        # boot — this process IS the audio plane, and a plane that cannot
        # transcribe is not one. So the host declares admission through the
        # gate's own documented seam rather than around it.
        #
        # setdefault, not assignment: an operator who has explicitly closed
        # admission keeps it closed.
        # WHOSE ears and whose mouth this process is. Bound BEFORE the
        # pipeline is wired, because TTS engines resolve their voice at
        # construction: bind it afterwards and the singleton is already
        # holding JARVIS's voice.
        #
        # This host is the `ov` cockpit's audio plane, and `ov` is O+V —
        # Karen. Without the declaration, MacOSVoice's British-first selector
        # answered for every agent alike and this cockpit spoke as Daniel.
        try:
            from backend.voice.agent_persona import AgentPersona, bind_persona
            bind_persona(AgentPersona.KAREN)
        except Exception:  # noqa: BLE001 — a nameless voice still speaks
            logger.debug("[AudioPlane] persona bind degraded", exc_info=True)

        os.environ.setdefault("JARVIS_ASR_ADMISSION_OPEN", "1")

        # Warm the voice lane NOW, not at the first turn. This host is
        # remote-only by design, so an unelected lane is not a degraded mode —
        # it is guaranteed silence on the first exchange while the election
        # runs behind it. Boot is the one moment where spending a few probe
        # seconds costs the operator nothing.
        try:
            from backend.core.ouroboros.governance.karen_voice_lane import (
                ensure_voice_lane_warm,
            )
            if ensure_voice_lane_warm():
                logger.info("[AudioPlane] voice lane warming in background")
        except Exception:  # noqa: BLE001
            logger.debug("[AudioPlane] voice-lane warm degraded", exc_info=True)

        try:
            from backend.audio.audio_pipeline_bootstrap import (
                wire_conversation_pipeline,
            )
            # llm_client=None -> AdaptiveVoiceRouter with no local engine:
            # remote-only generation, which is the whole reason this host is
            # separate from the monolith.
            self._handle = await wire_conversation_pipeline(
                audio_bus=self._bus, llm_client=None,
            )
            logger.info("[AudioPlane] conversation pipeline wired")
        except Exception as exc:  # noqa: BLE001
            logger.error("[AudioPlane] pipeline wiring failed: %r", exc)
            return False

        # Honest reporting: a wired pipeline with no IPC surface is a plane the
        # cockpit can never reach, and that must be loud rather than inferred
        # from a wave that does not move.
        if getattr(self._handle, "audio_ipc", None) is None:
            logger.warning(
                "[AudioPlane] no audio-state IPC broadcaster — the cockpit "
                "cannot reach this plane (JARVIS_AUDIO_IPC_ENABLED?)",
            )
        else:
            try:
                from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (  # noqa: E501
                    socket_path,
                )
                logger.info("[AudioPlane] serving %s", socket_path())
            except Exception:  # noqa: BLE001
                pass

        self._wire_adaptive_input()
        return True

    # -- adaptive input --------------------------------------------------

    def _wire_adaptive_input(self) -> None:
        """Arm the proximity re-binder, if the operator asked for it.

        Wired here rather than inside AudioBus because the bus must not decide
        which microphone it is: ranking devices means capturing from them, and
        a bus that probes its own alternatives while running is a bus that can
        contend with itself. The host owns the policy; the bus owns one verb
        (``rebind_input``) and performs it.

        Default OFF. This can open a Continuity handshake and it tears down a
        live CoreAudio stream to do its job — three wedged processes in this
        investigation came from exactly that. NEVER raises."""
        try:
            from backend.audio.acoustic_feedback import set_adaptive_input_sink
            from backend.audio.adaptive_input import (
                CREST_TRIGGER_DB,
                AdaptiveInputManager,
                adaptive_input_enabled,
            )
            if not adaptive_input_enabled():
                return

            manager = AdaptiveInputManager(rebind=self._bus.rebind_input)
            try:
                manager.note_builtin(self._bus._config.input_device)
            except Exception:  # noqa: BLE001
                manager.note_builtin(None)

            # The rejection path already measures every failed capture. That
            # telemetry IS the incumbent's score — reusing it means the manager
            # never opens a second stream on the device in use.
            set_adaptive_input_sink(manager.observe)
            self._adaptive = manager
            self._adaptive_task = asyncio.create_task(self._adaptive_loop())
            logger.info(
                "[AudioPlane] adaptive input armed (crest trigger %.0fdB)",
                CREST_TRIGGER_DB,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[AudioPlane] adaptive input wiring degraded",
                         exc_info=True)

    async def _adaptive_loop(self) -> None:
        """Drive the manager off the audio thread.

        Two jobs, both cheap: ask for a re-evaluation while speech is actually
        happening (the only moment a comparison between microphones is fair),
        and poll the starvation breaker on a freshly bound device."""
        manager = getattr(self, "_adaptive", None)
        if manager is None:
            return
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.25)
                try:
                    await manager.check_liveness()
                    if manager.armed and self._speech_active():
                        await manager.on_speech()
                except Exception:  # noqa: BLE001
                    logger.debug("[AudioPlane] adaptive tick degraded",
                                 exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[AudioPlane] adaptive loop ended", exc_info=True)

    def _speech_active(self) -> bool:
        """Is the operator talking right now? Read from the STT engine that
        already answers this — no second VAD."""
        try:
            stt = getattr(self._handle, "streaming_stt", None)
            return bool(stt is not None and stt.is_speech_active)
        except Exception:  # noqa: BLE001
            return False

    # -- address watchdog ------------------------------------------------

    def _bound_inode(self) -> Optional[int]:
        """Inode of the socket path right now, or None if it is gone."""
        try:
            from backend.core.ouroboros.governance.comms.duplex.audio_state_ipc import (  # noqa: E501
                socket_path,
            )
            return socket_path().stat().st_ino
        except Exception:  # noqa: BLE001
            return None

    async def _address_watchdog(self) -> None:
        """Notice when the host loses its own address, and take it back.

        A unix-domain server can be unbound WITHOUT KNOWING IT. Another
        process unlinks or replaces the path — a stale-socket cleanup, an
        ``rm``, a second host — and the listener goes on serving an orphaned
        inode that no path points to. Every client then gets ECONNREFUSED
        while the server reports perfect health.

        Observed live 2026-07-25: the host was transcribing speech at the same
        moment the cockpit read "no audio plane". Both were telling the truth
        about different inodes.

        Liveness cannot answer this — the process is fine, the pipeline is
        fine, the socket object is fine. Only IDENTITY answers it: is the file
        at my address still the one I bound? So the watchdog compares inodes
        and re-binds when they diverge, which also recovers the case where the
        path was deleted outright.

        Bounded, cheap (one stat per tick), and fail-soft: a watchdog fault
        costs the self-healing, never the audio."""
        try:
            interval = max(1.0, float(
                os.environ.get("JARVIS_AUDIO_PLANE_ADDRESS_CHECK_S", "5"),
            ))
        except (TypeError, ValueError):
            interval = 5.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return                      # stop signalled
            except asyncio.TimeoutError:
                pass
            try:
                now = self._bound_inode()
                if now == self._inode:
                    continue
                logger.warning(
                    "[AudioPlane] lost my address (inode %s -> %s) — "
                    "another process replaced or removed the socket; "
                    "re-binding", self._inode, now,
                )
                if await self._rebind():
                    logger.info("[AudioPlane] address recovered")
                else:
                    # Cannot serve, and holding the singleton lock while
                    # serving nothing would block every replacement. Exit and
                    # let the cockpit's reflex spawn a healthy one.
                    logger.error(
                        "[AudioPlane] could not re-bind — exiting so a "
                        "replacement can take the lock",
                    )
                    self.request_stop("address_lost")
                    return
            except Exception:  # noqa: BLE001
                logger.debug("[AudioPlane] address watchdog degraded", exc_info=True)

    async def _rebind(self) -> bool:
        """Stop and restart just the IPC broadcaster. NEVER raises."""
        try:
            ipc = getattr(self._handle, "audio_ipc", None)
            if ipc is None:
                return False
            try:
                await asyncio.wait_for(ipc.stop(), timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
            if not await ipc.start():
                return False
            self._inode = self._bound_inode()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def run(self) -> None:
        """Idle until signalled, watching that the address stays ours."""
        self._inode = self._bound_inode()
        watchdog = asyncio.get_running_loop().create_task(self._address_watchdog())
        try:
            await self._stop.wait()
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def request_stop(self, reason: str = "") -> None:
        """Signal-handler safe: sets an Event, does no I/O, takes no lock.

        A handler that logged or awaited could deadlock against the very
        subsystem it is trying to stop — the watchdog-isolation discipline the
        harness already documents."""
        self._stop.set()
        self._reason = reason

    async def stop(self) -> None:
        """Reverse-order teardown. NEVER raises — every failure here is one a
        hard exit resolves a moment later anyway."""
        # Detach the telemetry sink FIRST: the manager must not receive a
        # measurement, decide to rebind, and reach for a bus that teardown is
        # already halfway through disposing of.
        task, self._adaptive_task = self._adaptive_task, None
        self._adaptive = None
        try:
            from backend.audio.acoustic_feedback import set_adaptive_input_sink
            set_adaptive_input_sink(None)
        except Exception:  # noqa: BLE001
            pass
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        handle, self._handle = self._handle, None
        if handle is not None:
            for attr in ("stop", "shutdown", "close"):
                fn = getattr(handle, attr, None)
                if callable(fn):
                    try:
                        res = fn()
                        if asyncio.iscoroutine(res):
                            await asyncio.wait_for(res, timeout=10.0)
                        break
                    except Exception:  # noqa: BLE001
                        logger.debug("[AudioPlane] handle.%s degraded", attr)
        bus, self._bus = self._bus, None
        if bus is not None:
            try:
                await asyncio.wait_for(bus.stop(), timeout=10.0)
            except Exception:  # noqa: BLE001
                logger.debug("[AudioPlane] bus stop degraded", exc_info=True)
        logger.info("[AudioPlane] stopped")


async def _amain(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis-audio-plane",
        description=(
            "Own the microphone and serve the audio-state socket. The voice "
            "plane without the monolith: no web app, no local model."
        ),
    )
    parser.add_argument(
        "--force", action="store_true",
        help="start even if another host appears to be serving the socket",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=_log_level(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    import contextlib
    stack = contextlib.ExitStack()
    try:
        # Lock FIRST, then probe. The lock closes the boot window; the probe
        # still catches a host that predates this build (or was started
        # without the lock), so the two guards cover different failures rather
        # than duplicating one.
        if not args.force and not _acquire_exclusive(stack):
            logger.info(
                "[AudioPlane] another host holds the microphone lock — exiting",
            )
            return EXIT_ALREADY_RUNNING
        if not args.force and await _socket_already_served():
            logger.info(
                "[AudioPlane] another host is already serving — exiting",
            )
            return EXIT_ALREADY_RUNNING
        return await _run_host(host=AudioPlaneHost())
    finally:
        stack.close()


async def _run_host(*, host: "AudioPlaneHost") -> int:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, host.request_stop, sig.name)
        except (NotImplementedError, ValueError, RuntimeError):
            pass          # non-main thread / unsupported platform

    if not await host.start():
        await host.stop()
        return 1
    try:
        await host.run()
    finally:
        await host.stop()
    return 0


def main(argv: Optional[list] = None) -> int:
    try:
        return asyncio.run(_amain(argv))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.error("[AudioPlane] fatal: %r", exc)
        return 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    # Importable as a script from anywhere: `ov` spawns this by path, and the
    # spawn's cwd is not guaranteed to be the repo.
    _root = str(_repo_root())
    if _root not in sys.path:
        sys.path.insert(0, _root)
    _rc = main()
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.flush()
        except Exception:  # noqa: BLE001
            pass
    # os._exit, not sys.exit: interpreter finalization null-derefs inside a C
    # extension on this stack (the documented Py_FinalizeEx SIGSEGV class), and
    # every byte of work is already done by here.
    os._exit(int(_rc or 0))
