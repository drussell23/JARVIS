"""
process_roles - what each JARVIS entry-point process is, declared by itself.

Every ``unified_supervisor.py`` process looks the same from outside: the same
interpreter, the same script, the same checkout. Which of them is THE kernel
is answered by the kernel lock; which of the others are clients of it -- a
viewer attached to its stream, a ``--status``/``--shutdown`` command, a launch
still deciding which it will be, a ``--restart`` launcher following its
restart -- could not be answered at all. So cleanup took them for orphans:
the takeover a booting kernel runs when it takes the lock matched
"unified_supervisor" in their command lines and terminated them (at debug
level), and the viewer following a restart vanished mid-boot, back to the
prompt without a word (2026-10-16 00:47). Killing the venv launcher of a
viewer kills the viewer too: the launcher holds its child in a job.

Each such process now DECLARES its role in a record beside the kernel lock
(``<locks>/roles/<pid>.json``: pid + start time, the identity lock records
use, so a recycled pid is not mistaken for the declarer). The kill guard
(``process_identity``) never signals a live CLIENT -- any role but
``kernel`` -- nor an ancestor of one (its launchers), whatever sweep asks.
A process that becomes the kernel declares that; a kernel that no longer
holds the lock stays eligible for cleanup like before.

The record is withdrawn at exit; one left by a process that died is ignored
(its pid + start time no longer name a live process) and pruned on read.
NEVER raises into its callers.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

KERNEL = "kernel"
LAUNCHING = "launching"
VIEWER = "viewer"
CONTROL = "control"

_declared: Optional[str] = None


def roles_dir() -> Path:
    from backend.core.daemon_handover import KERNEL_LOCKS_DIR
    return KERNEL_LOCKS_DIR / "roles"


def _record_path(pid: int, directory: Optional[Path] = None) -> Path:
    return (directory or roles_dir()) / f"{int(pid)}.json"


def declare_role(role: str, directory: Optional[Path] = None) -> bool:
    """Declare this process's role (replacing an earlier declaration).
    True when the record was written."""
    global _declared
    try:
        from backend.core.ouroboros.governance.durable_io import atomic_write_text
        from backend.core.process_identity import process_started_at
        path = _record_path(os.getpid(), directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({
            "pid": os.getpid(),
            "started_at": process_started_at(os.getpid()),
            "role": role,
            "argv": sys.argv[1:],
        }), sync_dir=False)
        if _declared is None:
            atexit.register(_withdraw, path)
        _declared = role
        return True
    except Exception as exc:  # noqa: BLE001 -- a missing record never stops a launch
        logger.debug("[ProcessRoles] could not declare %s: %s", role, exc)
        return False


def declared_role() -> Optional[str]:
    """The role this process declared, if any."""
    return _declared


def _withdraw(path: Path) -> None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if int(record.get("pid")) == os.getpid():
            path.unlink()
    except (OSError, ValueError, TypeError, AttributeError):
        pass


def live_roles(directory: Optional[Path] = None) -> Dict[int, dict]:
    """``{pid: record}`` for every declarer still running; dead declarers'
    records are removed."""
    from backend.core.process_identity import lock_holder_alive
    found: Dict[int, dict] = {}
    try:
        entries = list((directory or roles_dir()).glob("*.json"))
    except OSError:
        return found
    for entry in entries:
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
            pid = int(record["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if lock_holder_alive(record):
            found[pid] = record
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    return found


def _launchers(pid: int) -> list:
    """*pid*'s ancestors, nearest first (empty without psutil)."""
    try:
        import psutil
        return [parent.pid for parent in psutil.Process(pid).parents()]
    except Exception:  # noqa: BLE001 -- gone, unreadable, or no psutil
        return []


def protected(directory: Optional[Path] = None) -> Dict[int, dict]:
    """``{pid: client record}`` for every process cleanup must spare: each
    live client (any role but ``kernel``) and every launcher above it. A
    recycled ancestor pid can only make this spare more, never less."""
    spared: Dict[int, dict] = {}
    for pid, record in live_roles(directory).items():
        if record.get("role") == KERNEL:
            continue
        spared.setdefault(pid, record)
        for launcher in _launchers(pid):
            spared.setdefault(launcher, record)
    return spared


def client_protecting(pid: int, directory: Optional[Path] = None) -> Optional[dict]:
    """The live client record that protects *pid* -- its own, or that of a
    client it launched -- or None. A kernel's record protects nothing."""
    return protected(directory).get(int(pid))


__all__ = [
    "CONTROL", "KERNEL", "LAUNCHING", "VIEWER", "client_protecting", "declare_role",
    "declared_role", "live_roles", "protected", "roles_dir",
]
