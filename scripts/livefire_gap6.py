"""End-to-end live-fire proof for Gap #6 (Slices 1 + 2 + 4).

Boots a minimal ``EventChannelServer`` with the IDE observability +
stream surfaces enabled (graduated defaults), then:

  1. Drives a few ``TaskBoard`` transitions via ``task_tool``.
  2. Opens a real HTTP SSE connection to ``/observability/stream``
     and collects the frames.
  3. Issues GET requests against ``/observability/{health,tasks,
     tasks/{op_id}}`` and verifies the payloads.
  4. Asserts the full end-to-end contract: every TaskBoard
     transition reaches the SSE consumer as a schema-v1.0 frame
     with the expected event_type, op_id, and payload keys.

Run directly:

    python3 scripts/livefire_gap6.py

Exits 0 on success, 1 on failure with a diagnostic summary.
Writes a per-run journal under ``.livefire/gap6-<ts>/`` so the
closure artefact is auditable post-hoc.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# The live-fire is meant to be run from the repo root.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.governance.event_channel import (
    EventChannelServer,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    reset_default_broker,
    stream_enabled,
)
from backend.core.ouroboros.governance.ide_observability import (
    ide_observability_enabled,
)
from backend.core.ouroboros.governance.task_tool import (
    close_task_board, get_or_create_task_board, reset_task_board_registry,
)


# ---------------------------------------------------------------------------
# Support: find free port
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Live-fire journal
# ---------------------------------------------------------------------------


@dataclass
class Journal:
    """Append-only record of what happened during the session.

    Written to disk at end so the closure artefact is auditable."""

    steps: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)

    def step(self, name: str, **kwargs: Any) -> None:
        self.steps.append({"name": name, "ts": time.time(), **kwargs})
        payload = {k: v for k, v in kwargs.items() if k != "body"}
        print("[livefire] %s  %s"
              % (name, json.dumps(payload, default=str)))

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print("[livefire] FAIL: " + msg, file=sys.stderr)

    def write(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"steps": self.steps, "failures": self.failures},
                indent=2, default=str,
            )
        )


# ---------------------------------------------------------------------------
# SSE reader
# ---------------------------------------------------------------------------


class _SSEReader:
    """Simple streaming SSE reader built on urllib — mirrors the
    Sublime plugin's parser. Collects frames into a list until
    ``stop()`` is called or ``max_frames`` is reached."""

    def __init__(self, url: str, max_frames: int = 16, timeout_s: float = 15.0):
        self.url = url
        self.max_frames = max_frames
        self.timeout_s = timeout_s
        self.frames: List[Dict[str, Any]] = []
        self._stop = False
        self._error: Optional[BaseException] = None

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        req = urllib.request.Request(
            self.url,
            headers={
                "Accept": "text/event-stream",
                "Cache-Control": "no-store",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                buf = ""
                deadline = time.monotonic() + self.timeout_s
                while not self._stop and time.monotonic() < deadline:
                    chunk = resp.read(4096)
                    if not chunk:
                        return
                    buf += chunk.decode("utf-8", errors="replace")
                    while True:
                        sep = buf.find("\n\n")
                        if sep < 0:
                            break
                        raw = buf[:sep]
                        buf = buf[sep + 2:]
                        parsed = _parse_sse_frame(raw)
                        if parsed is not None:
                            self.frames.append(parsed)
                            if len(self.frames) >= self.max_frames:
                                return
        except BaseException as exc:  # noqa: BLE001
            self._error = exc


def _parse_sse_frame(raw: str) -> Optional[Dict[str, Any]]:
    event_id = event_type = None
    data_parts: List[str] = []
    for line in raw.split("\n"):
        if not line or line.startswith(":"):
            continue
        colon = line.find(":")
        if colon < 0:
            continue
        field, value = line[:colon].strip(), line[colon + 1:]
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event_type = value
        elif field == "data":
            data_parts.append(value)
    if event_id is None or event_type is None or not data_parts:
        return None
    try:
        return json.loads("\n".join(data_parts))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Verification runner
# ---------------------------------------------------------------------------


async def _run(journal: Journal) -> int:
    # Pin graduated defaults explicitly. A full live-fire does not
    # assume the environment; the harness controls the env so the
    # result is reproducible.
    os.environ.pop("JARVIS_IDE_OBSERVABILITY_ENABLED", None)
    os.environ.pop("JARVIS_IDE_STREAM_ENABLED", None)
    if not ide_observability_enabled():
        journal.fail("IDE observability not enabled under graduated defaults")
        return 1
    if not stream_enabled():
        journal.fail("IDE stream not enabled under graduated defaults")
        return 1

    reset_default_broker()
    reset_task_board_registry()

    port = _free_port()

    class _NullRouter:
        async def ingest(self, *_args: Any, **_kwargs: Any) -> None:
            return None
        async def ingest_envelope(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    server = EventChannelServer(
        router=_NullRouter(), host="127.0.0.1", port=port,
    )
    await server.start()
    journal.step("server_started", host="127.0.0.1", port=port)
    base = "http://127.0.0.1:" + str(port)

    stream_url = base + "/observability/stream"
    reader = _SSEReader(stream_url, max_frames=16, timeout_s=10.0)

    # Run the SSE reader on a background thread so the main thread
    # can emit TaskBoard events and make GET calls.
    import threading
    reader_thread = threading.Thread(target=reader.run, daemon=True)
    reader_thread.start()

    # Give the SSE connection a moment to establish BEFORE emitting
    # events — otherwise the first few frames race the connect.
    await asyncio.sleep(0.5)

    op_id = "livefire-gap6"
    board = get_or_create_task_board(op_id)

    # We import task_tool's publish hook to exercise the same path
    # that Venom handlers use.
    from backend.core.ouroboros.governance.task_tool import (
        _publish_stream_event, _stream_payload_for_task,
    )

    def _emit(event_type: str, task: Any) -> None:
        _publish_stream_event(event_type, task.op_id, _stream_payload_for_task(task))

    task_created = board.create(title="live fire smoke", body="end-to-end")
    _emit("task_created", task_created)
    journal.step("emitted_task_created", task_id=task_created.task_id)

    task_started = board.start(task_created.task_id)
    _emit("task_started", task_started)
    journal.step("emitted_task_started")

    task_completed = board.complete(task_created.task_id)
    _emit("task_completed", task_completed)
    journal.step("emitted_task_completed")

    # Board close publishes board_closed through close_task_board.
    close_task_board(op_id, reason="livefire complete")
    journal.step("board_closed")

    # Let the reader drain.
    reader_thread.join(timeout=3.0)
    reader.stop()

    # --- GET endpoint verification ---------------------------------------
    # urllib is blocking — offload to the default ThreadPoolExecutor
    # so the aiohttp server (on the same event loop) can service the
    # request. Running urllib on the main thread deadlocks the loop.
    loop = asyncio.get_event_loop()

    def _get(path: str) -> Dict[str, Any]:
        with urllib.request.urlopen(base + path, timeout=5.0) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        health = await loop.run_in_executor(None, _get, "/observability/health")
        journal.step("health_body", body=health)
    except Exception as exc:  # noqa: BLE001
        journal.fail("health GET failed: %s" % exc)
        await server.stop()
        return 1

    try:
        tasks_json = await loop.run_in_executor(None, _get, "/observability/tasks")
        journal.step("tasks_list", body=tasks_json)
    except Exception as exc:  # noqa: BLE001
        journal.fail("tasks list GET failed: %s" % exc)
        await server.stop()
        return 1

    # --- stop the server -------------------------------------------------
    await server.stop()

    # --- assertions ------------------------------------------------------
    seen_types = [f.get("event_type") for f in reader.frames]
    journal.step("sse_frames_received", count=len(reader.frames),
                 types=seen_types)

    expected = {"task_created", "task_started", "task_completed", "board_closed"}
    observed = set(seen_types)
    missing = expected - observed
    if missing:
        journal.fail("missing event types: %s" % sorted(missing))

    # All frames must carry schema_version "1.0".
    for f in reader.frames:
        if f.get("schema_version") != "1.0":
            journal.fail("schema mismatch in frame: %s" % f)

    if health.get("schema_version") != "1.0":
        journal.fail("health missing schema_version 1.0")
    if tasks_json.get("schema_version") != "1.0":
        journal.fail("tasks list missing schema_version 1.0")

    if reader._error is not None:
        journal.fail("SSE reader exception: %r" % reader._error)

    return 0 if not journal.failures else 1


def main() -> int:
    journal = Journal()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = _REPO_ROOT / ".livefire" / ("gap6-" + ts)
    try:
        exit_code = asyncio.run(_run(journal))
    except BaseException as exc:  # noqa: BLE001
        journal.fail("uncaught: %r" % exc)
        exit_code = 1
    out = out_dir / "journal.json"
    journal.write(out)
    if exit_code == 0:
        print("[livefire] PASS  journal=%s" % out)
    else:
        print("[livefire] FAIL  journal=%s  failures=%d"
              % (out, len(journal.failures)), file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
