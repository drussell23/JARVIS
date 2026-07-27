"""What code is the running organism actually made of?

An `ov` daemon outlives the terminal that started it — that is the point. The
cost is that an operator attaches to a process whose age and contents are
invisible: it looks identical whether it booted a minute ago or on Saturday.

Observed 2026-07-27: a daemon had been running **36 hours**. Every fix merged
that day was on disk and not in the process. Two rounds of debugging went into
a build that structurally could not contain the fix, and nothing on screen
could have said so.

The daemon stamps what it booted from; the client compares against the repo
now. Neither side guesses, and neither has to remember to ask.

Deliberately advisory. An old daemon is often correct — a long soak SHOULD
keep running while its operator commits unrelated work — so this reports and
never acts. `git` is consulted through a bounded subprocess and every failure
degrades to "unknown", because a provenance banner that can block or slow a
boot is worse than no banner.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.Provenance")

__all__ = ["read_provenance", "staleness_line", "write_provenance"]

_STAMP = ".jarvis/daemon_provenance.json"


def _repo_root() -> Path:
    return Path(os.environ.get("JARVIS_REPO_PATH", ".")).resolve()


def _git(*args: str, root: Optional[Path] = None) -> str:
    """One bounded git call. '' on any failure — never raises, never hangs."""
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=3.0,
            cwd=str(root or _repo_root()),
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def write_provenance(path: Optional[Path] = None) -> Optional[Path]:
    """Stamp what this daemon booted from. Called once, at boot."""
    target = path or (_repo_root() / _STAMP)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "pid": os.getpid(),
            "booted_at": time.time(),
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        }), encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001
        logger.debug("[Provenance] could not stamp", exc_info=True)
        return None


def read_provenance(path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        target = path or (_repo_root() / _STAMP)
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def staleness_line(
    path: Optional[Path] = None, *, root: Optional[Path] = None,
) -> str:
    """One advisory line, or '' when the daemon is current or unknowable.

    Silent on a fresh daemon: a banner that always shows is chrome, and gets
    read past exactly when it finally matters.
    """
    try:
        stamp = read_provenance(path)
        if not stamp:
            return ""
        booted = float(stamp.get("booted_at") or 0)
        commit = str(stamp.get("commit") or "")
        age_s = max(0.0, time.time() - booted) if booted else 0.0

        head = _git("rev-parse", "HEAD", root=root)
        if not head or not commit:
            # Not a git checkout, or git is unavailable. Age alone is still
            # worth saying once it is large — "unknown" beats a confident
            # wrong answer about what code is loaded.
            return (f"⚠ daemon booted {_age(age_s)} ago · commit unknown · "
                    f"`ov restart` if it looks stale"
                    if age_s >= _age_threshold_s() else "")
        if head == commit:
            return ""                       # current; say nothing

        behind = _git("rev-list", "--count", f"{commit}..{head}", root=root)
        detail = f"{behind} commits behind" if behind.isdigit() else "behind HEAD"
        # Name the COMMAND, not the intent. "restart to load current code"
        # told an operator what to want; `ov restart` tells them what to
        # type — and the gap between those two was four steps of `ps`, a
        # transcribed pid, and a `kill`.
        return (f"⚠ daemon booted {_age(age_s)} ago on {commit[:7]} · "
                f"{detail} · run `ov restart` to load current code")
    except Exception:  # noqa: BLE001
        return ""


def _age_threshold_s() -> float:
    """How old is worth mentioning when the commit cannot be compared."""
    try:
        return max(60.0, float(os.environ.get(
            "JARVIS_DAEMON_STALE_AGE_S", "3600") or 3600))
    except (TypeError, ValueError):
        return 3600.0
