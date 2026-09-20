"""Keep the training pair when Reactor-Core is not there.

What Reactor-Core actually is
-----------------------------

``ReactorCoreConfig.api_url`` defaults to ``http://localhost:8090``. It is a
LOCAL service, not a cloud endpoint -- nothing in this path was ever going
off-host. On a machine that does not run it the client's ``initialize()``
health check returns False and every ``stream_experience`` returns False in
0.0001s without touching a socket.

So the offline story was already safe. It was not USEFUL: the preference
pair -- the single richest training signal this system produces, a candidate
that failed validation beside the one that fixed it -- was scored, discarded
and forgotten. Arming the emitter against an absent service bought nothing
but a scoring log line.

This sink keeps it. The corpus accumulates on disk whether or not anything
is listening, so the decision to train is one that can be made later against
data that already exists, rather than one that requires having decided
months earlier.

Two costs this also removes
---------------------------

``initialize()`` against a dead ``localhost:8090`` measured **10.61s** --
the client's health check working through its retry ladder. A TCP pre-check
answers the same question in milliseconds, so the ladder is only walked when
something is actually there.

And the write is bounded and rotated for the same reason the reachability
ledger is: an append-only file on a multi-day soak outgrows the disk that
holds it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("Ouroboros.LocalTrajectorySink")

_DEFAULT_PROBE_TIMEOUT_S = 0.25
_DEFAULT_ROTATE_BYTES = 64 * 1024 * 1024
_DEFAULT_KEEP = 3


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        value = float(raw) if raw else default
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        value = int(raw) if raw else default
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def sink_enabled() -> bool:
    """Default ON. Keeping data costs a file; discarding it costs the corpus."""
    raw = (os.environ.get("JARVIS_LOCAL_TRAJECTORY_SINK_ENABLED", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def corpus_path() -> Path:
    """Where pairs accumulate. Beside the other ledgers, not in /tmp."""
    raw = (os.environ.get("JARVIS_DPO_CORPUS_PATH", "") or "").strip()
    if raw:
        return Path(raw)
    root = (os.environ.get("JARVIS_PROJECT_ROOT", "") or "").strip() or "."
    return Path(root) / ".ouroboros" / "dpo_corpus.jsonl"


def probe_timeout_s() -> float:
    return _env_float("JARVIS_REACTOR_PROBE_TIMEOUT_S", _DEFAULT_PROBE_TIMEOUT_S)


@dataclass(frozen=True)
class SinkResult:
    """Where a pair ended up, so "kept" and "lost" are distinguishable."""

    written: bool
    path: str = ""
    reason: str = ""

    def render(self) -> str:
        return f"written={self.written} path={self.path or '-'} reason={self.reason}"


def _host_port(url: str) -> Optional[Tuple[str, int]]:
    """``(host, port)`` from a URL, or ``None`` when it names no TCP target."""
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname
        if not host:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return host, int(port)
    except Exception:  # noqa: BLE001
        return None


def _tcp_open(host: str, port: int, timeout_s: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:  # noqa: BLE001 — refused, unresolved, timed out: all "no"
        return False


async def endpoint_reachable(url: str, *, timeout_s: Optional[float] = None) -> bool:
    """Is anything accepting TCP at *url*? NEVER raises.

    A connect probe, not a health check: it answers "is the socket open" in
    milliseconds, where the client's full ladder took 10.61s to conclude the
    same thing about a dead ``localhost:8090``. The full check still runs --
    this only decides whether it is worth running.

    Offloaded, because ``socket.create_connection`` blocks and this is
    called from the control plane.
    """
    target = _host_port(url)
    if target is None:
        return False
    host, port = target
    try:
        return await asyncio.to_thread(
            _tcp_open, host, port,
            timeout_s if timeout_s is not None else probe_timeout_s(),
        )
    except Exception:  # noqa: BLE001
        return False


def _rotate_if_needed(path: Path) -> None:
    """Bound the corpus the way the reachability ledger is bounded."""
    try:
        ceiling = _env_int("JARVIS_DPO_CORPUS_ROTATE_BYTES", _DEFAULT_ROTATE_BYTES)
        if not path.is_file() or path.stat().st_size < ceiling:
            return
        keep = _env_int("JARVIS_DPO_CORPUS_KEEP", _DEFAULT_KEEP)
        oldest = path.with_suffix(path.suffix + f".{keep}")
        if oldest.exists():
            oldest.unlink()
        for gen in range(keep - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{gen}")
            if src.exists():
                src.rename(path.with_suffix(path.suffix + f".{gen + 1}"))
        path.rename(path.with_suffix(path.suffix + ".1"))
        logger.info("[LocalTrajectorySink] corpus rotated at %d bytes", ceiling)
    except OSError:
        logger.debug("[LocalTrajectorySink] rotation degraded", exc_info=True)


def _write_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


async def write_pair(event: Dict[str, Any], *, path: Optional[Path] = None) -> SinkResult:
    """Append one preference pair to the local corpus. NEVER raises.

    The payload written is whatever the caller hands over -- which on the
    emitter path has ALREADY been through the egress redactor, so a secret
    that would not have been sent is also not stored. Mode 0600: this file
    holds candidate source, and a corpus is not less sensitive for being
    local.
    """
    if not sink_enabled():
        return SinkResult(False, reason="sink_disabled")
    target = path or corpus_path()
    try:
        line = json.dumps(
            {"at": round(time.time(), 3), "event": event},
            sort_keys=True, default=str,
        ) + "\n"
    except Exception as exc:  # noqa: BLE001
        return SinkResult(False, reason=f"unserialisable:{type(exc).__name__}")
    try:
        await asyncio.to_thread(_write_line, target, line)
        return SinkResult(True, path=str(target), reason="local_corpus")
    except Exception as exc:  # noqa: BLE001 — a corpus write never breaks L2
        logger.debug("[LocalTrajectorySink] write degraded", exc_info=True)
        return SinkResult(False, reason=f"{type(exc).__name__}: {exc}")


def corpus_stats(path: Optional[Path] = None) -> Dict[str, Any]:
    """Rows and bytes on disk, for the operator deciding whether there is
    yet enough data to train anything. NEVER raises."""
    target = path or corpus_path()
    try:
        if not target.is_file():
            return {"path": str(target), "exists": False, "rows": 0, "bytes": 0}
        size = target.stat().st_size
        with target.open("rb") as fh:
            rows = sum(1 for _ in fh)
        return {"path": str(target), "exists": True, "rows": rows, "bytes": size}
    except Exception:  # noqa: BLE001
        return {"path": str(target), "exists": False, "rows": 0, "bytes": 0}


__all__ = [
    "SinkResult",
    "corpus_path",
    "corpus_stats",
    "endpoint_reachable",
    "probe_timeout_s",
    "sink_enabled",
    "write_pair",
]
