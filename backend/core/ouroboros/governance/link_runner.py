"""link_runner — opens the socket, supervises the pumps, survives the drop.

This is the last piece: everything below it is tested logic with no I/O, and
everything above it is an operator typing a command. Its whole job is to hold
a connection open, run three coroutines against it, and do the right thing
when one of them fails.

SUPERVISION, AND WHY IT IS A GROUP
-----------------------------------
Three pumps share one socket: writes, heartbeat, reads. They are not
independent — a dead peer detected by the heartbeat means the reader will
block forever, and a reader that hits EOF means the writer is shouting into a
closed pipe. So they are supervised as a **group**: the first to fail cancels
the others, the connection is torn down once, and the session parks.

Running them as unsupervised tasks is the failure mode this shape exists to
prevent. A heartbeat task that raises ``ConnectionError`` into nobody's
``await`` becomes a "Task exception was never retrieved" warning at garbage
collection, minutes later, while the reader sits on a socket that will never
speak again — the half-open hang wearing a different hat.

RECONNECTION IS NOT A LOOP AROUND CONNECT
------------------------------------------
It is a loop around **park → wait → resume**. The distinction matters: a
retry loop that re-runs ``connect`` builds new state each time, and that is
precisely what makes fifty reconnects expensive. Here the session, its
ledger, its clock and its outbox are untouched by a disconnection; only the
socket is replaced. Fifty reconnects cost fifty handshakes.

The wait between attempts comes from :func:`link_protocol.backoff_delay_s`
and the decision to attempt at all from :class:`link_protocol.FlapBreaker` —
neither is re-implemented here.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import random
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance import link_protocol as proto
from backend.core.ouroboros.governance import link_session as sess
from backend.core.ouroboros.governance import link_transport as tx

logger = logging.getLogger("Ouroboros.LinkRunner")

LINK_RUNNER_SCHEMA_VERSION: str = "link_runner.1"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def read_idle_grace() -> float:
    """Multiplier on the liveness deadline before a read is called stalled.

    The reader's timeout must be LOOSER than the heartbeat's, or the reader
    declares death first and the RTT estimator — the thing that actually
    knows what this path costs — never gets consulted.
    """
    return _env_float("JARVIS_LINK_READ_GRACE", 3.0, minimum=1.1)


class LinkRunner:
    """Holds one session across however many sockets it takes."""

    def __init__(
        self,
        loop: sess.LinkSessionLoop,
        *,
        connector: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.loop = loop
        #: Injected so tests drive a full cycle over an in-memory pair, and
        #: so a re-key supplies a fresh SSL context without this class
        #: knowing certificates exist.
        self._connector = connector
        self._stopping = asyncio.Event()
        self._connections = 0
        self._last_error: str = ""

    # -- one connection --------------------------------------------------

    async def _serve_connection(self, reader: Any, writer: Any) -> None:
        """Run the three pumps until one fails. Always tears down once."""
        self._connections += 1
        self.loop.on_established()
        pumps = [
            asyncio.create_task(self.loop.pump_writes(writer),
                                name="link-writes"),
            asyncio.create_task(self.loop.pump_heartbeat(writer),
                                name="link-heartbeat"),
            asyncio.create_task(self._pump_reads(reader),
                                name="link-reads"),
        ]
        try:
            done, pending = await asyncio.wait(
                pumps, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            for task in pumps:
                task.cancel()
            # Await the cancellations. A cancelled task that is never awaited
            # surfaces minutes later as "Task exception was never retrieved",
            # attributed to garbage collection rather than to the disconnect
            # that caused it.
            await asyncio.gather(*pumps, return_exceptions=True)
            with contextlib.suppress(Exception):
                writer.close()

    async def _pump_reads(self, reader: Any) -> None:
        """Read frames until EOF or a stall. Dispatch is the session's.

        The timeout is derived from the liveness deadline rather than fixed,
        and multiplied by a grace factor so the heartbeat — which measures
        the path — is the component that declares death.
        """
        while True:
            timeout = max(
                tx.heartbeat_interval_s(),
                self.loop.liveness.rtt.deadline_s()) * read_idle_grace()
            record = await tx.read_frame(
                reader, limits=self.loop._limits or tx.negotiate({}, {}),
                timeout_s=timeout)
            if record is None:
                raise ConnectionError("peer closed the connection")
            if record.get("kind") == "__rejected__":
                # A corrupt frame is not a dead link. The CRC caught it, the
                # counter records it, and the stream resynchronises on the
                # next newline — tearing the session down would turn one bad
                # frame into a reconnect storm.
                continue
            if record.get("kind") == tx.KIND_HEARTBEAT:
                self.loop.liveness.note_inbound()
                self.loop.queue.put_high({
                    "kind": tx.KIND_HEARTBEAT_ACK,
                    "seq": self.loop.next_seq(),
                    "lamport": (self.loop._session.clock.tick()
                                if self.loop._session else 0),
                    "node_id": self.loop.config.node_id,
                    "sent_mono": record.get("sent_mono"),
                })
                continue
            if record.get("kind") == tx.KIND_HEARTBEAT_ACK:
                sent = record.get("sent_mono")
                if isinstance(sent, (int, float)):
                    self.loop.liveness.note_ack(float(sent))
                else:
                    self.loop.liveness.note_inbound()
                continue
            self.loop.dispatch(record)

    # -- the outer cycle -------------------------------------------------

    async def run(self, *, max_cycles: Optional[int] = None) -> None:
        """park → wait → resume, until stopped.

        ``max_cycles`` exists for tests and for a bounded one-shot; unset, it
        runs until :meth:`stop`.
        """
        cycles = 0
        while not self._stopping.is_set():
            if max_cycles is not None and cycles >= max_cycles:
                return
            cycles += 1

            verdict = self.loop.breaker.admit(self.loop.config.node_id)
            if not verdict.admitted:
                await self._sleep_or_stop(verdict.retry_after_s)
                continue

            try:
                if self._connector is None:
                    raise ConnectionError("no connector configured")
                reader, writer = await self._connector()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"connect: {type(exc).__name__}: {exc}"
                self.loop.park(self._last_error)
                await self._sleep_or_stop(self._backoff())
                continue

            try:
                await self._serve_connection(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.info("[LinkRunner] connection ended — %s",
                            self._last_error)
            self.loop.park(self._last_error or "connection ended")
            await self._sleep_or_stop(self._backoff())

    def _backoff(self) -> float:
        """Backoff with jitter. Jitter is drawn HERE, not inside the pure
        helper, so the schedule stays reproducible under test."""
        return proto.backoff_delay_s(
            self.loop._attempt + 1, jitter=random.uniform(-0.3, 0.3))

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Wait, but wake immediately on stop.

        A bare ``sleep`` would make shutdown take up to a full backoff
        interval — thirty seconds of an operator watching a process that has
        been told to exit.
        """
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    def stop(self) -> None:
        self._stopping.set()

    def snapshot(self) -> Dict[str, Any]:
        out = self.loop.snapshot()
        out.update({
            "runner_schema": LINK_RUNNER_SCHEMA_VERSION,
            "connections": self._connections,
            "last_error": self._last_error,
            "stopping": self._stopping.is_set(),
        })
        return out


# ---------------------------------------------------------------------------
# Connectors — the only place a socket is actually opened
# ---------------------------------------------------------------------------


def tls_connector(host: str, port: int) -> Callable[[], Any]:
    """A dialer for the Body. Builds a FRESH SSL context per attempt.

    Fresh per attempt is what makes re-keying free: rotated material is
    picked up by the next reconnection with no separate code path, because
    the context is constructed at dial time rather than held for the process
    lifetime.
    """
    async def _dial() -> Tuple[Any, Any]:
        ctx = tx.build_ssl_context(server_side=False)
        if ctx is None:
            raise ConnectionError(
                "link mTLS material missing — refusing to dial "
                "unauthenticated (see `ov link --issue-certs`)")
        return await asyncio.open_connection(host, port, ssl=ctx,
                                             server_hostname=host)
    return _dial


async def serve_link(
    loop: sess.LinkSessionLoop, *, host: Optional[str] = None,
    port: Optional[int] = None,
) -> Any:
    """Bind and accept for the Engine. Returns the asyncio Server.

    Binds where configuration says, defaulting to loopback. A caller that
    wants the tailnet address supplies it deliberately; nothing here widens
    the surface on its own.
    """
    ctx = tx.build_ssl_context(server_side=True)
    if ctx is None:
        raise ConnectionError(
            "link mTLS material missing — refusing to serve unauthenticated "
            "(see `ov link --issue-certs`)")
    runner = LinkRunner(loop)

    async def _on_client(reader: Any, writer: Any) -> None:
        peer = writer.get_extra_info("peername")
        verdict = loop.breaker.admit(str(peer))
        if not verdict.admitted:
            logger.warning("[LinkRunner] refusing %s — %s (retry in %.1fs)",
                           peer, verdict.reason, verdict.retry_after_s)
            with contextlib.suppress(Exception):
                writer.close()
            return
        logger.info("[LinkRunner] peer connected: %s", peer)
        try:
            await runner._serve_connection(reader, writer)
        except Exception as exc:  # noqa: BLE001
            logger.info("[LinkRunner] peer %s ended: %s", peer, exc)
        finally:
            loop.park("peer disconnected")

    bind = host or tx.bind_host()
    listen = tx.bind_port() if port is None else port
    try:
        return await asyncio.start_server(_on_client, bind, listen, ssl=ctx)
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
            raise
        # An ungraceful termination leaves the previous socket in TIME_WAIT.
        # asyncio sets SO_REUSEADDR on POSIX but deliberately NOT on Windows,
        # where that option permits hijacking an active listener rather than
        # merely reclaiming a dead one — so the Engine is exactly the host
        # where this is reachable.
        #
        # Not retried in a loop: if another Engine is genuinely running,
        # silently waiting for it to exit would be indistinguishable from a
        # hang, and stealing the port would be worse. The operator is told
        # what holds it and how to check.
        remedy = (
            f"lsof -nP -iTCP:{listen} -sTCP:LISTEN"
            if os.name != "nt"
            else f'netstat -ano | findstr ":{listen}"')
        raise ConnectionError(
            f"cannot bind {bind}:{listen} — {exc.strerror}. Either an Engine "
            f"is already running, or a previous one left the socket in "
            f"TIME_WAIT. Check with:  {remedy}\n"
            f"Set JARVIS_LINK_PORT to a different port, or wait for the "
            f"kernel to release it."
        ) from exc
