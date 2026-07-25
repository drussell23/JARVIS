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
        os.environ.setdefault("JARVIS_ASR_ADMISSION_OPEN", "1")

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
        return True

    async def run(self) -> None:
        """Idle until signalled. The pipeline drives itself from here — this
        coroutine exists only to keep the loop alive and own the shutdown."""
        await self._stop.wait()

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
