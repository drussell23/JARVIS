"""EventSimulator — hermetic in-memory ``fs.changed.*`` injection.

Constructs the EXACT ``fs.changed.*`` event payload that the live
``FileSystemEventBridge`` (``intake/fs_event_bridge.py::_on_file_event``)
emits — same topic, same JSON fields — and publishes it DIRECTLY into a real
:class:`TrinityEventBus` via the genuine ``publish_raw`` path (queue -> worker
-> ``_deliver_event`` -> subscriber handler). No disk mutation is required: the
system "believes" a file just changed and the real
``TestFailureSensor._on_fs_event`` subscription fires exactly as it would on a
live node.

This is NOT a mock of the detection logic — it reuses the real bus and the real
bridge payload schema. The only thing bypassed is the OS file-watch boundary
(FileWatchGuard), which is the cloud/host edge, not the cognitive loop.

The payload shape is kept in lockstep with the bridge by deriving every field
the same way the bridge does:

    topic            = f"fs.changed.{event_type}"            (default "modified")
    payload["path"]            = absolute path string
    payload["relative_path"]   = path relative to repo root (POSIX-ish, str())
    payload["extension"]       = suffix (e.g. ".py")
    payload["checksum"]        = content hash (sha256 hex, like FileWatchGuard)
    payload["is_test_file"]    = tests/ prefix OR test_*/_test.py name
    payload["is_config_file"]  = (.json/.yaml/.yml) AND ".jarvis" in rel
    payload["is_directory"]    = False
    payload["timestamp"]       = wall-clock float
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Optional

__all__ = ["build_fs_changed_payload", "EventSimulator"]


def _compute_checksum(abs_path: Path) -> str:
    """SHA-256 hex of the file's bytes, mirroring FileWatchGuard's verify hash.

    Fail-soft: a missing / unreadable file yields a deterministic empty-hash so
    the simulator can inject an event for a path even in pure-in-memory mode.
    """
    try:
        return hashlib.sha256(abs_path.read_bytes()).hexdigest()
    except OSError:
        return hashlib.sha256(b"").hexdigest()


def build_fs_changed_payload(
    *,
    abs_path: Path,
    repo_root: Path,
    event_type: str = "modified",
    checksum: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> "tuple[str, dict]":
    """Return ``(topic, payload)`` matching the real bridge emission shape.

    Field derivation is byte-for-byte aligned with
    ``fs_event_bridge.FileSystemEventBridge._on_file_event`` so the consumer
    cannot tell a simulated event from a live one.
    """
    abs_path = Path(abs_path)
    repo_root = Path(repo_root)

    topic = f"fs.changed.{event_type}"

    try:
        rel_path = str(abs_path.relative_to(repo_root))
    except ValueError:
        rel_path = str(abs_path)

    extension = abs_path.suffix
    is_test = (
        rel_path.startswith("tests/")
        or abs_path.name.startswith("test_")
        or abs_path.name.endswith("_test.py")
    )
    is_config = (
        extension in (".json", ".yaml", ".yml") and ".jarvis" in rel_path
    )

    payload = {
        "path": str(abs_path),
        "relative_path": rel_path,
        "extension": extension,
        "checksum": checksum if checksum is not None else _compute_checksum(abs_path),
        "is_test_file": is_test,
        "is_config_file": is_config,
        "is_directory": False,
        "timestamp": timestamp if timestamp is not None else time.time(),
    }
    return topic, payload


class EventSimulator:
    """Inject real-shape ``fs.changed.*`` events into a real TrinityEventBus.

    Parameters
    ----------
    event_bus:
        A live (``start()``-ed) :class:`TrinityEventBus` instance.
    repo_root:
        The authoritative repo root used to compute ``relative_path`` — must be
        the SAME ``.git``-anchored root the consumer resolves to (otherwise the
        run-#12 mismatch resurfaces). Pass ``resolve_repo_root(start=...)``.
    """

    def __init__(self, event_bus: Any, repo_root: Path) -> None:
        self._bus = event_bus
        self._repo_root = Path(repo_root)
        self.injected: int = 0

    async def inject_change(
        self,
        abs_path: Path,
        *,
        event_type: str = "modified",
        checksum: Optional[str] = None,
    ) -> str:
        """Publish a single real-shape ``fs.changed.*`` event. Returns event id.

        Routes through the genuine ``publish_raw`` -> priority-queue -> worker
        -> ``_deliver_event`` path, so the real ``TestFailureSensor._on_fs_event``
        handler runs. ``persist=False`` keeps it WAL-free (matches the bridge).
        """
        topic, payload = build_fs_changed_payload(
            abs_path=Path(abs_path),
            repo_root=self._repo_root,
            event_type=event_type,
            checksum=checksum,
        )
        event_id = await self._bus.publish_raw(
            topic=topic,
            data=payload,
            persist=False,
        )
        self.injected += 1
        return event_id
