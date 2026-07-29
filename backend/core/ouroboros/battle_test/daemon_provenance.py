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
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.Provenance")

__all__ = ["client_binary_warning", "read_provenance", "staleness_line",
           "write_provenance", "env_drift", "env_drift_line"]

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


def _jarvis_env() -> Dict[str, str]:
    """The JARVIS_* environment, for staleness comparison. NEVER raises.

    VALUES are kept, not just keys: "the daemon has JARVIS_VOICE_MUTED=0
    and you have =1" is actionable, while "the daemon has different env"
    is another thing to go and check.
    """
    try:
        import os as _os
        return {
            k: str(v)[:80] for k, v in _os.environ.items()
            if k.startswith("JARVIS_")
        }
    except Exception:  # noqa: BLE001
        return {}


def env_drift(provenance: Optional[Dict[str, Any]] = None) -> List[str]:
    """JARVIS_* settings that differ between this shell and that daemon.

    Returns human-readable differences, most actionable first. Empty when
    they agree — or when the daemon predates this field, which is stated
    rather than guessed at: an older daemon has no `env` key, and claiming
    agreement we cannot verify is how the last three answers went wrong.

    NEVER raises.
    """
    try:
        data = provenance if provenance is not None else read_provenance()
        if not isinstance(data, dict) or "env" not in data:
            return []
        theirs = data.get("env")
        # `env: null` means the daemon recorded NOTHING, not that it ran
        # with an empty environment. Treating the two alike reported every
        # local setting as "absent in the daemon" — a confident answer
        # built on a field that was never written.
        if not isinstance(theirs, dict) or not theirs:
            return []
        mine = _jarvis_env()
        out: List[str] = []
        for key in sorted(set(mine) | set(theirs)):
            a, b = theirs.get(key), mine.get(key)
            if a == b:
                continue
            if a is None:
                out.append(f"{key}={b} is set here, ABSENT in the daemon")
            elif b is None:
                out.append(f"{key}={a} in the daemon, unset here")
            else:
                out.append(f"{key}: daemon={a}, here={b}")
        return out
    except Exception:  # noqa: BLE001
        return []


def write_provenance(path: Optional[Path] = None) -> Optional[Path]:
    """Stamp what this daemon booted from. Called once, at boot."""
    target = path or (_repo_root() / _STAMP)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "pid": os.getpid(),
            "booted_at": time.time(),
            "commit": _git("rev-parse", "HEAD"),
            # The daemon's ENVIRONMENT, as it was at launch.
            #
            # Code staleness was already covered; this is the other half,
            # and it is the half that actually bit: an operator exported
            # JARVIS_VOICE_MUTED=1 and the daemon kept talking, because
            # `export` reaches processes started AFTERWARDS and this one
            # was a day old. Same commit, so no warning fired — the code
            # was current and the environment was not.
            #
            # Only JARVIS_* is captured: everything else is the machine's
            # business, and a full environ dump on disk is a credential
            # leak waiting for a postmortem to read it.
            "env": _jarvis_env(),
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


def env_drift_line(max_shown: int = 3) -> str:
    """One line naming the settings this daemon cannot see. NEVER raises.

    Separate from `staleness_line` because the remedies differ: stale CODE
    wants a restart, a stale ENVIRONMENT wants a restart too — but only
    after the operator knows WHICH setting is being ignored, or they will
    restart and set it in the wrong shell again.
    """
    try:
        drift = env_drift()
        if not drift:
            return ""
        shown = " · ".join(drift[:max_shown])
        more = f" (+{len(drift) - max_shown} more)" if len(drift) > max_shown else ""
        return (f"⚠ this daemon's environment predates your shell — {shown}"
                f"{more} · `export` cannot reach a running process; restart it")
    except Exception:  # noqa: BLE001
        return ""


def _age_threshold_s() -> float:
    """How old is worth mentioning when the commit cannot be compared."""
    try:
        return max(60.0, float(os.environ.get(
            "JARVIS_DAEMON_STALE_AGE_S", "3600") or 3600))
    except (TypeError, ValueError):
        return 3600.0


def client_binary_warning() -> str:
    """One loud line when the RUNNING ov imported code from somewhere
    other than the repo it is operating on, or "".

    The stale-shim trap: a pyenv/pip-installed copy of the package
    shadows the editable install, and the operator watches a GHOST of an
    older interface — old renderers, old bugs, hours lost to fixing what
    is already fixed (the 2026-07-28 classifier-dump screenshot was
    exactly this). Detected by provenance, not by version strings: the
    imported ``backend`` package's filesystem root either IS the repo
    (editable install / in-tree run, incl. worktrees whose env points at
    the main checkout) or it is a copy that can silently rot.

    NEVER raises; unknowable = silent."""
    try:
        import backend as _backend
        code_root = Path(_backend.__file__).resolve().parent.parent
        repo_raw = os.environ.get("JARVIS_REPO_PATH", "").strip()
        repo = Path(repo_raw).resolve() if repo_raw else Path.cwd().resolve()
        code_s, repo_s = str(code_root), str(repo)
        if (
            code_s == repo_s
            or code_s.startswith(repo_s + os.sep)
            or repo_s.startswith(code_s + os.sep)
        ):
            return ""
        return (
            f"⚠ stale ov binary — this process imported code from "
            f"{code_root}, but the repo is {repo}. What you see may be an "
            f"OLD interface. Fix: pip install -e {repo}"
        )
    except Exception:  # noqa: BLE001
        return ""
