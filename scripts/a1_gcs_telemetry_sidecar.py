"""a1_gcs_telemetry_sidecar -- Continuous Async GCS Telemetry Sidecar.

Closes the teardown-race blindspot: when an A1 node is hard-killed by the
watchdog or preempted by GCP, the IAP-SSH *pull* bridge (``a1_telemetry_bridge``)
loses everything after the last drained byte. This sidecar runs ON the node and
*pushes* the growing ``debug.log`` + FSM state to GCS continuously, as immutable
append-only chunks, so the log is reconstructable up to the millisecond the node
died.

Design (zero-duplication):

  * ``AppendOnlyChunkStreamer`` -- pure, dependency-injected, immutable monotonic
    chunking. The ``sink`` is injected; production binds it to the NATIVE GCS
    Vault in ``backend/core/ouroboros/governance/state_persistence_daemon.py``
    (``google-cloud-storage`` SDK, ADC from instance metadata) -- never gsutil,
    never a new GCS client.

The async tail/flush loop and the GCS-Vault sink adapter are driven out in later
units and compose this streamer.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# sink(object_name: str, data: bytes) -> None
ChunkSink = Callable[[str, bytes], None]


def _resolve_gs_uri(target: str):
    """Reuse the canonical ``gs://bucket/prefix`` parser from the State Vault
    (zero-duplication). Returns ``(bucket, prefix)`` or ``None``."""
    from backend.core.ouroboros.governance.state_persistence_daemon import (
        _parse_gs_uri,
    )

    return _parse_gs_uri(target)


def _default_storage_client():
    from google.cloud import storage  # lazy — SDK optional until a real run

    return storage.Client()


def make_gcs_chunk_sink(
    target_uri: str,
    *,
    client_factory: Optional[Callable[[], object]] = None,
) -> Optional[ChunkSink]:
    """Build a chunk ``sink(object_name, data)`` uploading each immutable chunk to
    ``gs://bucket/prefix/<object_name>`` via the native google-cloud-storage SDK
    (ADC). Returns ``None`` when ``target_uri`` is not a ``gs://`` URI (sidecar
    stays disabled). The sink is fail-soft: an upload error is logged, swallowed,
    and never propagated to the soak."""
    try:
        parsed = _resolve_gs_uri(target_uri)
    except Exception as exc:  # noqa: BLE001 — never crash on resolve/import
        logger.warning("[a1-sidecar] gs uri resolve failed: %s", exc)
        return None
    if not parsed:
        return None

    bucket_name, prefix = parsed
    factory = client_factory if client_factory is not None else _default_storage_client
    box: dict = {}

    def sink(object_name: str, data: bytes) -> None:
        try:
            client = box.get("client")
            if client is None:
                client = factory()
                box["client"] = client
            blob_name = "%s/%s" % (prefix, object_name) if prefix else object_name
            client.bucket(bucket_name).blob(blob_name).upload_from_string(data)
        except Exception as exc:  # noqa: BLE001 — fail-soft, never crash the soak
            logger.warning("[a1-sidecar] chunk upload failed (%s): %s", object_name, exc)

    return sink


def flush_tick(path: str, streamer: AppendOnlyChunkStreamer) -> None:
    """Read bytes appended to ``path`` since the streamer's offset and push them.
    Fail-soft: a missing/locked file is a no-op (never raises)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(streamer.offset())
            data = fh.read()
    except Exception as exc:  # noqa: BLE001 — fail-soft
        logger.debug("[a1-sidecar] flush_tick read skipped (%s): %s", path, exc)
        return
    if data:
        streamer.stream_new_bytes(data)


async def run_sidecar(
    path: str,
    streamer: AppendOnlyChunkStreamer,
    *,
    interval_s: float,
    stop_event: "asyncio.Event",
) -> None:
    """Stream ``path`` to the streamer every ``interval_s`` until ``stop_event``
    is set, then do a guaranteed FINAL flush so a hard-killed / preempted node's
    last bytes are captured. Never raises (each tick is fail-soft)."""
    while not stop_event.is_set():
        flush_tick(path, streamer)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
    flush_tick(path, streamer)  # final flush — the dying node's last bytes


def discover_latest_debug_log(sessions_root: str) -> Optional[str]:
    """Return the newest ``bt-*/debug.log`` under ``sessions_root`` (by mtime),
    or ``None``. Lets the decoupled sidecar daemon be spawned BEFORE O+V boots
    with only the sessions root, then attach to the session O+V creates."""
    candidates = glob.glob(os.path.join(sessions_root, "bt-*", "debug.log"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


# --------------------------------------------------------------------------- #
# Decoupled daemon entrypoint -- spawned detached by the chaos harness BEFORE
# O+V boots, fully decoupled from the O+V lifecycle.
# --------------------------------------------------------------------------- #


def build_daemon_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="a1_gcs_telemetry_sidecar",
        description="Decoupled append-only GCS telemetry sidecar daemon.",
    )
    p.add_argument("--sessions-root", required=True,
                   help="Root dir holding bt-*/ session dirs to discover the debug.log.")
    p.add_argument("--gcs-target", required=True,
                   help="gs://bucket/prefix destination for immutable chunks.")
    p.add_argument("--interval", type=float, default=5.0,
                   help="Streaming cadence in seconds (default 5).")
    p.add_argument("--discover-timeout", type=float, default=180.0,
                   help="Seconds to wait for the session debug.log to appear.")
    return p


def run_daemon(argv: Optional[List[str]] = None) -> int:
    """Discover the active session debug.log under --sessions-root and stream it
    to GCS as immutable chunks until terminated. Fail-soft + signal-flushing."""
    import sys
    import time

    # Run-as-script bootstrap: ensure the repo root is importable so the reused
    # state_persistence_daemon GCS Vault (`backend.core...`) resolves. Under
    # pytest the root is already on sys.path; as a detached daemon it is NOT.
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    args = build_daemon_arg_parser().parse_args(argv)
    sink = make_gcs_chunk_sink(args.gcs_target)
    if sink is None:
        logger.warning("[a1-sidecar] no valid gs:// target -> daemon idle exit")
        return 0

    deadline = time.monotonic() + args.discover_timeout
    path = None
    while time.monotonic() < deadline:
        path = discover_latest_debug_log(args.sessions_root)
        if path:
            break
        time.sleep(1.0)
    if not path:
        logger.warning("[a1-sidecar] no session debug.log discovered -> exit")
        return 0

    session_id = os.path.basename(os.path.dirname(path))
    streamer = AppendOnlyChunkStreamer(session_id=session_id, sink=sink)
    sidecar = ManagedSidecar(path, streamer, interval_s=args.interval)
    sidecar.install_signal_handlers()

    async def _go() -> None:
        sidecar.start()
        if sidecar._task is not None:
            await sidecar._task

    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        sidecar.flush_now()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_daemon())


class ManagedSidecar:
    """Managed async daemon bound to the caller's lifecycle.

    ``start()`` launches ``run_sidecar`` as a task; ``aclose()`` signals stop and
    AWAITS the task, which performs a guaranteed final flush -- so the terminal
    'APPLIED' chunk is never lost to a teardown race. ``aclose()`` before
    ``start()`` is a safe no-op."""

    def __init__(
        self,
        path: str,
        streamer: AppendOnlyChunkStreamer,
        *,
        interval_s: float,
    ) -> None:
        self._path = path
        self._streamer = streamer
        self._interval_s = interval_s
        self._stop = asyncio.Event()
        self._task: "Optional[asyncio.Task]" = None

    def start(self) -> "ManagedSidecar":
        self._task = asyncio.ensure_future(
            run_sidecar(
                self._path,
                self._streamer,
                interval_s=self._interval_s,
                stop_event=self._stop,
            )
        )
        return self

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception as exc:  # noqa: BLE001 — teardown must never raise
                logger.warning("[a1-sidecar] managed task close error: %s", exc)
            finally:
                self._task = None

    def flush_now(self) -> None:
        """Synchronous final flush -- safe from a signal handler or an exception
        path at termination (no running event loop required). Reads the remaining
        bytes and pushes them so the terminal 'APPLIED' chunk is never truncated."""
        flush_tick(self._path, self._streamer)

    def install_signal_handlers(self) -> "ManagedSidecar":
        """Intercept SIGINT/SIGTERM: flush_now() before chaining to the prior
        handler. Idempotent; must be called from the main thread. Returns self."""
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            prev = signal.getsignal(sig)

            def handler(signum, frame, _prev=prev):
                try:
                    self.flush_now()
                except Exception as exc:  # noqa: BLE001 — never block teardown
                    logger.warning("[a1-sidecar] signal flush error: %s", exc)
                finally:
                    if callable(_prev) and _prev not in (
                        signal.SIG_DFL,
                        signal.SIG_IGN,
                    ):
                        _prev(signum, frame)

            try:
                signal.signal(sig, handler)
            except (ValueError, OSError) as exc:  # not main thread / unsupported
                logger.warning("[a1-sidecar] signal handler install skipped: %s", exc)
        return self


class AppendOnlyChunkStreamer:
    """Splits an append-only byte stream into immutable, monotonically-indexed
    chunks and pushes each through ``sink`` exactly once. Never overwrites an
    object; a dead node's log is reconstructable by ordering chunk indices.

    Fail-soft: a ``sink`` error is counted and swallowed -- telemetry must never
    crash the soak it observes.
    """

    def __init__(self, *, session_id: str, sink: ChunkSink, index_pad: int = 5) -> None:
        self._session_id = session_id
        self._sink = sink
        self._index_pad = index_pad
        self._index = 0
        self._offset = 0
        self._failed = 0

    def offset(self) -> int:
        """Total bytes consumed from the source stream so far."""
        return self._offset

    def failed_chunks(self) -> int:
        """Count of chunks whose sink push raised (swallowed, fail-soft)."""
        return self._failed

    def _chunk_name(self, index: int) -> str:
        return "%s/chunk_%0*d.log" % (self._session_id, self._index_pad, index)

    def stream_new_bytes(self, data: bytes) -> List[str]:
        """Push ``data`` as the next immutable chunk. Empty input is a no-op.
        Returns the chunk object name(s) emitted."""
        if not data:
            return []
        self._index += 1
        self._offset += len(data)
        name = self._chunk_name(self._index)
        try:
            self._sink(name, data)
        except Exception:  # noqa: BLE001 -- fail-soft: never crash the soak
            self._failed += 1
        return [name]
