"""Cross-client smoke test — Gap #6 end-to-end integration.

Boots a real ``EventChannelServer`` with graduated defaults, then
drives two actual client-library implementations against it:

  1. **Sublime Text client** — imports the plugin's ``jarvis_api``
     module (the same code that runs inside Sublime Text 4's
     embedded Python 3.8 interpreter) and drives its
     ``StreamConsumer`` against the live server.

  2. **VS Code / Cursor extension** — if ``node`` is available and
     the TypeScript sources have been compiled to
     ``extensions/vscode-jarvis/dist/``, runs the compiled
     ``StreamConsumer`` via a small Node helper that forwards
     events back to the harness via stdout.

Both clients are read-only consumers — matching the production
plugins bit-for-bit. The harness emits a handful of TaskBoard
transitions and asserts every registered client observes them.

Exits 0 on success. Writes a per-run journal under
``.livefire/gap6-clients-<ts>/``.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# Sublime plugin is a drop-in package — add its dir to sys.path so
# ``import jarvis_api`` resolves to the plugin's exact module.
_SUBLIME_DIR = _REPO_ROOT / "extensions" / "sublime-jarvis"
if str(_SUBLIME_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBLIME_DIR))

from backend.core.ouroboros.governance.event_channel import (
    EventChannelServer,
)
from backend.core.ouroboros.governance.ide_observability import (
    ide_observability_enabled,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    get_default_broker,
    reset_default_broker,
    stream_enabled,
)
from backend.core.ouroboros.governance.task_tool import (
    close_task_board, get_or_create_task_board, reset_task_board_registry,
    _publish_stream_event, _stream_payload_for_task,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


@dataclass
class Journal:
    steps: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)

    def step(self, name: str, **kwargs: Any) -> None:
        self.steps.append({"name": name, "ts": time.time(), **kwargs})
        trimmed = {k: v for k, v in kwargs.items() if k != "body"}
        print("[clients] %s  %s"
              % (name, json.dumps(trimmed, default=str)))

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print("[clients] FAIL: " + msg, file=sys.stderr)

    def write(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"steps": self.steps, "failures": self.failures},
                indent=2, default=str,
            )
        )


# ---------------------------------------------------------------------------
# Sublime client exercise
# ---------------------------------------------------------------------------


def run_sublime_client(
    endpoint: str,
    collected: List[str],
    connected_event: threading.Event,
    stop_event: threading.Event,
) -> Optional[BaseException]:
    """Drive the Sublime plugin's StreamConsumer against the live
    server. Returns None on success or the exception that stopped
    it."""
    import jarvis_api  # type: ignore[import-not-found]
    consumer = jarvis_api.StreamConsumer(
        endpoint=endpoint,
        auto_reconnect=False,
        timeout_s=10.0,
    )

    def _on_event(ev: Dict[str, Any]) -> None:
        et = ev.get("event_type")
        if isinstance(et, str):
            collected.append(et)

    def _on_state(state: str) -> None:
        if state == jarvis_api.STATE_CONNECTED:
            connected_event.set()

    consumer.on_event(_on_event)
    consumer.on_state(_on_state)
    consumer.start()
    stop_event.wait(timeout=10.0)
    consumer.stop(join_timeout=3.0)
    return None


# ---------------------------------------------------------------------------
# VS Code / Cursor client exercise (if the dist is built)
# ---------------------------------------------------------------------------


_VSCODE_DIR = _REPO_ROOT / "extensions" / "vscode-jarvis"
_VSCODE_DIST = _VSCODE_DIR / "dist" / "api" / "stream.js"


def vscode_dist_available() -> bool:
    return _VSCODE_DIST.is_file()


_VSCODE_DRIVER_JS = r"""
// Minimal Node driver that loads the compiled VS Code extension's
// StreamConsumer, runs it against the provided endpoint, and prints
// each event_type to stdout so the parent Python harness can
// collect them. Exits when it receives SIGTERM.
const path = require('path');
const streamModulePath = path.resolve(process.argv[2]);
const { StreamConsumer } = require(streamModulePath);

const endpoint = process.argv[3];
const consumer = new StreamConsumer({
    endpoint,
    autoReconnect: false,
    reconnectMaxBackoffMs: 1000,
});

consumer.onState((s) => {
    process.stdout.write('STATE ' + s + '\n');
});
consumer.onEvent((frame) => {
    process.stdout.write('EVENT ' + frame.event_type + '\n');
});

process.on('SIGTERM', async () => {
    await consumer.stop();
    process.exit(0);
});

consumer.start();

// Keep the event loop alive; the stream consumer holds its own
// pending IO but Node will exit on empty loop after start() returns
// if we don't register something.
setInterval(() => {}, 60_000);
"""


def run_vscode_client(
    endpoint: str,
    collected: List[str],
    connected_event: threading.Event,
    stop_event: threading.Event,
) -> Optional[BaseException]:
    """Spawn ``node`` against the compiled extension and stream its
    stdout into `collected`."""
    driver_path = _REPO_ROOT / ".livefire" / "tmp_vscode_driver.js"
    driver_path.parent.mkdir(parents=True, exist_ok=True)
    driver_path.write_text(_VSCODE_DRIVER_JS)

    try:
        proc = subprocess.Popen(
            [
                "node",
                str(driver_path),
                str(_VSCODE_DIST),
                endpoint,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return FileNotFoundError("node not installed")

    assert proc.stdout is not None

    def _pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                if line.startswith("STATE "):
                    if line == "STATE connected":
                        connected_event.set()
                elif line.startswith("EVENT "):
                    collected.append(line.split(" ", 1)[1])
        except Exception:  # noqa: BLE001
            pass

    pump_thread = threading.Thread(target=_pump, daemon=True)
    pump_thread.start()

    stop_event.wait(timeout=10.0)
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
    pump_thread.join(timeout=2.0)
    return None


# ---------------------------------------------------------------------------
# Harness main
# ---------------------------------------------------------------------------


async def _run(journal: Journal) -> int:
    os.environ.pop("JARVIS_IDE_OBSERVABILITY_ENABLED", None)
    os.environ.pop("JARVIS_IDE_STREAM_ENABLED", None)
    if not ide_observability_enabled() or not stream_enabled():
        journal.fail("graduated defaults not in effect")
        return 1

    reset_default_broker()
    reset_task_board_registry()

    port = _free_port()

    class _NullRouter:
        async def ingest(self, *_a: Any, **_k: Any) -> None: return None
        async def ingest_envelope(self, *_a: Any, **_k: Any) -> None: return None

    server = EventChannelServer(
        router=_NullRouter(), host="127.0.0.1", port=port,
    )
    await server.start()
    journal.step("server_started", port=port)
    endpoint = "http://127.0.0.1:" + str(port)

    # Start Sublime client.
    sublime_collected: List[str] = []
    sublime_connected = threading.Event()
    sublime_stop = threading.Event()
    sublime_thread = threading.Thread(
        target=run_sublime_client,
        args=(endpoint, sublime_collected, sublime_connected, sublime_stop),
        daemon=True, name="sublime-client",
    )
    sublime_thread.start()

    # Start VS Code client IFF the compiled dist exists.
    vscode_collected: List[str] = []
    vscode_connected = threading.Event()
    vscode_stop = threading.Event()
    vscode_thread: Optional[threading.Thread] = None
    if vscode_dist_available():
        journal.step("vscode_dist_found", path=str(_VSCODE_DIST))
        vscode_thread = threading.Thread(
            target=run_vscode_client,
            args=(endpoint, vscode_collected, vscode_connected, vscode_stop),
            daemon=True, name="vscode-client",
        )
        vscode_thread.start()
    else:
        journal.step("vscode_dist_missing",
                     path=str(_VSCODE_DIST),
                     note="run `npm install && npm run compile` under extensions/vscode-jarvis")

    # Wait for both to connect.
    broker = get_default_broker()
    expected_subs = 2 if vscode_thread is not None else 1
    deadline = time.monotonic() + 10.0
    while broker.subscriber_count < expected_subs and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    journal.step(
        "subscribers_connected",
        count=broker.subscriber_count,
        expected=expected_subs,
    )
    if broker.subscriber_count < expected_subs:
        journal.fail(
            "only %d/%d subscribers connected within 10s"
            % (broker.subscriber_count, expected_subs)
        )

    # Emit a canonical event sequence.
    op_id = "livefire-clients"
    board = get_or_create_task_board(op_id)

    def _emit(event_type: str, task: Any) -> None:
        _publish_stream_event(event_type, task.op_id, _stream_payload_for_task(task))

    t = board.create(title="cross-client smoke", body="")
    _emit("task_created", t)
    await asyncio.sleep(0.05)
    t = board.start(t.task_id)
    _emit("task_started", t)
    await asyncio.sleep(0.05)
    t = board.complete(t.task_id)
    _emit("task_completed", t)
    await asyncio.sleep(0.05)
    close_task_board(op_id, reason="clients smoke complete")
    journal.step("events_emitted", types=[
        "task_created", "task_started", "task_completed", "board_closed",
    ])

    # Let clients drain.
    await asyncio.sleep(1.0)

    # Signal stop + join.
    sublime_stop.set()
    vscode_stop.set()
    sublime_thread.join(timeout=5.0)
    if vscode_thread is not None:
        vscode_thread.join(timeout=5.0)

    await server.stop()

    expected_types = {"task_created", "task_started", "task_completed", "board_closed"}
    sublime_seen = set(sublime_collected)
    journal.step("sublime_summary",
                 events=list(sublime_seen), count=len(sublime_collected))
    if not expected_types.issubset(sublime_seen):
        journal.fail(
            "Sublime client missing events: %s"
            % sorted(expected_types - sublime_seen)
        )

    if vscode_thread is not None:
        vscode_seen = set(vscode_collected)
        journal.step("vscode_summary",
                     events=list(vscode_seen), count=len(vscode_collected))
        if not expected_types.issubset(vscode_seen):
            journal.fail(
                "VS Code client missing events: %s"
                % sorted(expected_types - vscode_seen)
            )

    return 0 if not journal.failures else 1


def main() -> int:
    journal = Journal()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = _REPO_ROOT / ".livefire" / ("gap6-clients-" + ts)
    try:
        exit_code = asyncio.run(_run(journal))
    except BaseException as exc:  # noqa: BLE001
        journal.fail("uncaught: %r" % exc)
        exit_code = 1
    out = out_dir / "journal.json"
    journal.write(out)
    if exit_code == 0:
        print("[clients] PASS  journal=%s" % out)
    else:
        print("[clients] FAIL  journal=%s  failures=%d"
              % (out, len(journal.failures)), file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
