"""BackgroundMonitor — event-streaming wrapper around asyncio subprocesses.

Closes the observability gap between Claude Code (streams stdout events
live) and O+V (polls blocking subprocess.run with timeout). Every line
of stdout/stderr from a subprocess becomes a structured ``MonitorEvent``
that callers can consume incrementally — enabling pattern-match early
exits, live dashboards, and per-line bus subscriptions. Manifesto §8
(Absolute Observability) substrate for the TestRunner migration +
Venom ``Monitor`` tool in later slices.

Security note: this module uses ``asyncio.create_subprocess_exec``,
which is the argv-based (execve-family) variant — NO shell interpretation,
NO injection surface. The command must be passed as a ``Sequence[str]``;
shell-style string invocation is not supported by design.

This module is deliberately isolated from the Venom tool layer. It
depends only on the standard library + an optional ``TrinityEventBus``
reference. The event bus is optional at construction time — when
provided, every emitted event is also published via
``bus.publish_raw(topic, data, persist=False)`` so subscribers see
the same stream without coupling to the subprocess. When ``None``,
the monitor runs as a pure local-observer primitive (fast tests,
no bus setup required).

Usage::

    async with BackgroundMonitor(
        cmd=["pytest", "-x"],
        op_id="op-019xxx",
        ring_capacity=512,
        event_bus=bus,       # optional
    ) as mon:
        async for ev in mon.events():
            if ev.kind == "stdout" and "FAILED" in ev.data:
                break  # early exit — __aexit__ terminates the subprocess
        print(f"exit={mon.exit_code} last {len(mon.ring_snapshot())} lines")

Design decisions:
  * **Line-granular events**: every ``readline()`` produces one
    ``MonitorEvent``.
  * **Interleaved sequence ordering**: stdout + stderr events share a
    single monotonic sequence counter so consumers can reconstruct
    true temporal order.
  * **Backpressure through the queue**: the internal event queue uses
    ``put()`` (blocking), not ``put_nowait()``. If a consumer falls
    behind, the subprocess's stdout pipe fills up.
  * **Graceful shutdown**: ``__aexit__`` sends SIGTERM, waits up to
    ``terminate_grace_s`` (default 2s), then SIGKILL.
  * **Non-UTF8 safety**: decode paths use ``errors="replace"``.
  * **Long-line safety**: ``LimitOverrunError`` (64KB line cap) is
    caught; the reader emits ``truncated=True`` + partial buffer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Deque, List, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)

_DEFAULT_RING_CAPACITY = 1024
_DEFAULT_QUEUE_CAPACITY = 2048
_DEFAULT_TERMINATE_GRACE_S = 2.0


KIND_STDOUT = "stdout"
KIND_STDERR = "stderr"
KIND_EXITED = "exited"
KIND_ERROR = "error"

_VALID_KINDS = frozenset({KIND_STDOUT, KIND_STDERR, KIND_EXITED, KIND_ERROR})


@dataclass(frozen=True)
class MonitorEvent:
    """One streamed event from a BackgroundMonitor.

    Immutable — safe to fan out to multiple consumers. ``data`` carries
    the decoded line WITH the trailing newline stripped; ``line_terminator``
    records which newline variant was present. ``exit_code`` is populated
    only on ``kind == KIND_EXITED`` events.
    """

    kind: str
    op_id: str
    ts_mono: float
    data: str
    sequence: int
    exit_code: Optional[int] = None
    truncated: bool = False
    line_terminator: str = ""


class BackgroundMonitor:
    """Async context manager wrapping ``asyncio.create_subprocess_exec``.

    Argv-based invocation only (no shell). Pass the command as a
    Sequence[str]. See module docstring for full design.
    """

    def __init__(
        self,
        cmd: Sequence[str],
        *,
        op_id: str = "",
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        ring_capacity: int = _DEFAULT_RING_CAPACITY,
        queue_capacity: int = _DEFAULT_QUEUE_CAPACITY,
        terminate_grace_s: float = _DEFAULT_TERMINATE_GRACE_S,
        event_bus: Optional[Any] = None,
        bus_topic_prefix: str = "background_monitor",
    ) -> None:
        if ring_capacity < 1:
            raise ValueError(f"ring_capacity must be >= 1, got {ring_capacity}")
        if queue_capacity < 1:
            raise ValueError(f"queue_capacity must be >= 1, got {queue_capacity}")
        if terminate_grace_s < 0:
            raise ValueError(
                f"terminate_grace_s must be >= 0, got {terminate_grace_s}",
            )
        self._cmd: Tuple[str, ...] = tuple(str(c) for c in cmd)
        self._op_id = str(op_id or "")
        self._cwd = cwd
        self._env = env
        self._ring_capacity = ring_capacity
        self._terminate_grace_s = float(terminate_grace_s)
        self._event_bus = event_bus
        self._bus_topic_prefix = str(bus_topic_prefix or "background_monitor")

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._ring: Deque[MonitorEvent] = deque(maxlen=ring_capacity)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_capacity)
        self._readers: List[asyncio.Task] = []
        self._seq_lock = asyncio.Lock()
        self._sequence: int = 0
        self._exit_code: Optional[int] = None
        self._exited_event_emitted = False
        self._entered = False
        self._exited = False

    @property
    def op_id(self) -> str:
        return self._op_id

    @property
    def cmd(self) -> Tuple[str, ...]:
        return self._cmd

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    def ring_snapshot(self) -> Tuple[MonitorEvent, ...]:
        """Immutable snapshot of the ring buffer in arrival order."""
        return tuple(self._ring)

    async def __aenter__(self) -> "BackgroundMonitor":
        if self._entered:
            raise RuntimeError("BackgroundMonitor is single-use")
        self._entered = True
        try:
            # argv-based spawn (no shell interpretation). Safer than
            # subprocess.run(..., shell=True) — no injection surface.
            self._proc = await asyncio.create_subprocess_exec(
                *self._cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # A monitored tool never reads OUR terminal: an inherited
                # stdin is the blocking read that wedged pytest for 11
                # minutes (test_subprocess_helper, Slice 8).
                stdin=asyncio.subprocess.DEVNULL,
                cwd=self._cwd,
                env=self._env,
                # The context manager OWNS the tool — and therefore whatever
                # the tool spawns. Without its own session there is no way to
                # name those descendants once the leader is gone.
                start_new_session=True,
            )
        except FileNotFoundError:
            self._exited = True
            raise
        except PermissionError:
            self._exited = True
            raise

        try:
            from backend.core.ouroboros.governance.process_session import (  # noqa: PLC0415
                register_session,
            )
            register_session(self._proc.pid, f"monitor:{self._op_id}")
        except Exception:  # noqa: BLE001
            pass

        self._readers = [
            asyncio.create_task(
                self._read_stream(self._proc.stdout, KIND_STDOUT),
                name=f"bgmon-{self._op_id}-stdout",
            ),
            asyncio.create_task(
                self._read_stream(self._proc.stderr, KIND_STDERR),
                name=f"bgmon-{self._op_id}-stderr",
            ),
        ]
        self._readers.append(asyncio.create_task(
            self._await_exit(),
            name=f"bgmon-{self._op_id}-exit",
        ))
        return self

    @property
    def pid(self) -> Optional[int]:
        """Leader pid (= its session id), or ``None`` before spawn."""
        return None if self._proc is None else self._proc.pid

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._exited:
            return
        self._exited = True
        if self._proc is not None and self._proc.returncode is None:
            try:
                if self._terminate_grace_s > 0:
                    self._proc.terminate()
                    try:
                        await asyncio.wait_for(
                            self._proc.wait(),
                            timeout=self._terminate_grace_s,
                        )
                    except asyncio.TimeoutError:
                        self._proc.kill()
                        await self._proc.wait()
                else:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[BackgroundMonitor] shutdown of op_id=%s raised",
                    self._op_id, exc_info=True,
                )

        # Leaving the context ends the tool's lease on the machine — for
        # everything it started, not only for the leader, and whether the
        # leader was killed above or had already exited on its own.
        if self._proc is not None:
            try:
                from backend.core.ouroboros.governance.process_session import (  # noqa: PLC0415
                    reap_session, unregister_session,
                )
                reap_session(self._proc.pid, owner=f"monitor:{self._op_id}")
                unregister_session(self._proc.pid)
            except Exception:  # noqa: BLE001
                logger.debug("[BackgroundMonitor] session reap degraded", exc_info=True)

        # Populate exit_code authoritatively BEFORE cancelling readers —
        # the _await_exit task may be cancelled mid-stride before it
        # reaches its own ``self._exit_code = rc`` assignment. Cancelled
        # monitors must still carry the true subprocess returncode for
        # post-mortem correctness.
        if self._proc is not None and self._exit_code is None:
            self._exit_code = self._proc.returncode

        for t in self._readers:
            if not t.done():
                t.cancel()
        if self._readers:
            await asyncio.gather(*self._readers, return_exceptions=True)

    async def events(self) -> AsyncIterator[MonitorEvent]:
        """Yield MonitorEvents until the subprocess exits + queue drains.

        The final event is always ``kind == KIND_EXITED`` with the
        populated ``exit_code``.
        """
        while True:
            ev = await self._queue.get()
            try:
                yield ev
            finally:
                self._queue.task_done()
            if ev.kind == KIND_EXITED:
                return

    async def _next_sequence(self) -> int:
        async with self._seq_lock:
            self._sequence += 1
            return self._sequence

    async def _emit(self, ev: MonitorEvent) -> None:
        self._ring.append(ev)
        await self._queue.put(ev)
        if self._event_bus is not None:
            topic = f"{self._bus_topic_prefix}.{self._op_id}.{ev.kind}"
            data = {
                "op_id": ev.op_id,
                "kind": ev.kind,
                "ts_mono": ev.ts_mono,
                "data": ev.data,
                "sequence": ev.sequence,
                "exit_code": ev.exit_code,
                "truncated": ev.truncated,
                "line_terminator": ev.line_terminator,
            }
            try:
                await self._event_bus.publish_raw(
                    topic, data, persist=False,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[BackgroundMonitor] bus publish failed op=%s topic=%s",
                    self._op_id, topic, exc_info=True,
                )

    async def _read_stream(
        self,
        stream: Optional[asyncio.StreamReader],
        kind: str,
    ) -> None:
        if stream is None:
            return
        try:
            while True:
                try:
                    raw = await stream.readline()
                except asyncio.LimitOverrunError as exc:
                    try:
                        raw = await stream.readexactly(exc.consumed)
                    except Exception:  # noqa: BLE001
                        raw = b""
                    decoded = raw.decode("utf-8", errors="replace")
                    seq = await self._next_sequence()
                    await self._emit(MonitorEvent(
                        kind=kind,
                        op_id=self._op_id,
                        ts_mono=time.monotonic(),
                        data=decoded,
                        sequence=seq,
                        truncated=True,
                        line_terminator="",
                    ))
                    continue
                if not raw:
                    return  # EOF
                terminator = ""
                if raw.endswith(b"\r\n"):
                    terminator = "\r\n"
                    text_bytes = raw[:-2]
                elif raw.endswith(b"\n"):
                    terminator = "\n"
                    text_bytes = raw[:-1]
                elif raw.endswith(b"\r"):
                    terminator = "\r"
                    text_bytes = raw[:-1]
                else:
                    text_bytes = raw
                decoded = text_bytes.decode("utf-8", errors="replace")
                seq = await self._next_sequence()
                await self._emit(MonitorEvent(
                    kind=kind,
                    op_id=self._op_id,
                    ts_mono=time.monotonic(),
                    data=decoded,
                    sequence=seq,
                    line_terminator=terminator,
                ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[BackgroundMonitor] reader op=%s kind=%s raised %s",
                self._op_id, kind, type(exc).__name__, exc_info=True,
            )
            try:
                seq = await self._next_sequence()
                await self._emit(MonitorEvent(
                    kind=KIND_ERROR,
                    op_id=self._op_id,
                    ts_mono=time.monotonic(),
                    data=f"{type(exc).__name__}: {str(exc)[:256]}",
                    sequence=seq,
                ))
            except Exception:  # noqa: BLE001
                pass

    async def _await_exit(self) -> None:
        """Wait for readers to drain + process to reap, then emit the
        terminal ``exited`` event.

        Orders the ``exited`` event AFTER every stdout/stderr line has
        landed — consumers can trust that a KIND_EXITED marker means
        all output has been enqueued.
        """
        try:
            if self._proc is None:
                return
            # The LEADER's exit is what ends the run. This used to drain the
            # readers first and wait on the process second — and a reader only
            # reaches EOF when EVERY holder of the pipe's write end is gone.
            # A descendant that inherited stdout (a test that spawned
            # ``tail -f``) kept it open after the leader died, so the terminal
            # event never came and the consumer waited on a corpse until some
            # outer timeout: up to the full invocation cap per candidate.
            #
            # The readers run concurrently, so the leader can never block on a
            # full pipe while this waits for it.
            #
            # NOT ``proc.wait()``: asyncio resolves that only after every pipe
            # has closed, so the descendants this is about to clean up would be
            # the very thing preventing it from starting. See ``leader_exited``.
            drain_s = max(self._terminate_grace_s, _DEFAULT_TERMINATE_GRACE_S)
            try:
                from backend.core.ouroboros.governance.process_session import (  # noqa: PLC0415
                    leader_exited, reap_session,
                )
                await leader_exited(self._proc)
                # Reaping the session closes every inherited write end, which
                # is what lets ``wait()`` and the drain below FINISH — and
                # keeps the guarantee that the terminal event follows all of
                # the output.
                reap_session(self._proc.pid, owner=f"monitor:{self._op_id}")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("[BackgroundMonitor] session reap degraded", exc_info=True)

            try:
                # Bounded for the one holder a reap cannot reach: a descendant
                # that left the session with ``setsid``.
                rc = await asyncio.wait_for(self._proc.wait(), timeout=drain_s)
            except asyncio.TimeoutError:
                rc = self._proc.returncode
            except Exception:  # noqa: BLE001
                rc = -1
            self._exit_code = int(rc) if rc is not None else None

            readers = [t for t in self._readers if t is not asyncio.current_task()]
            if readers:
                # Bounded: a descendant that called ``setsid`` has left the
                # session and may still hold the pipe. It does not get to hold
                # the run open as well. Never ZERO, though — with the session
                # reaped EOF is imminent, and a caller's "kill at once" grace
                # must not be read as "discard the trailing output".
                _done, pending = await asyncio.wait(readers, timeout=drain_s)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            if not self._exited_event_emitted:
                self._exited_event_emitted = True
                seq = await self._next_sequence()
                await self._emit(MonitorEvent(
                    kind=KIND_EXITED,
                    op_id=self._op_id,
                    ts_mono=time.monotonic(),
                    data="",
                    sequence=seq,
                    exit_code=self._exit_code,
                ))
        except asyncio.CancelledError:
            if not self._exited_event_emitted:
                self._exited_event_emitted = True
                try:
                    seq = await self._next_sequence()
                    ev = MonitorEvent(
                        kind=KIND_EXITED,
                        op_id=self._op_id,
                        ts_mono=time.monotonic(),
                        data="cancelled",
                        sequence=seq,
                        exit_code=(
                            self._proc.returncode
                            if self._proc is not None else None
                        ),
                    )
                    self._ring.append(ev)
                    try:
                        self._queue.put_nowait(ev)
                    except asyncio.QueueFull:
                        pass
                except Exception:  # noqa: BLE001
                    pass
            raise
