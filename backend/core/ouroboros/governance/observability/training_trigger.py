"""Close the flywheel: hand a finished soak's corpus to Reactor-Core.

A soak produces trajectories; Reactor-Core turns them into a better model.
Until now a human carried the corpus across that gap. This module is the
carrier, and it is built to REFUSE far more often than it fires.

## Why refusal is the main feature

An automated trainer that runs whenever a soak ends is worse than no
trainer. Measured on this box, the corpus after five soaks held 74 rows and
33 prompts, 19 of them with 2+ responses -- and every one of those 19
groups had a reward spread of exactly 0.0. GRPO drops a flat group, so a
run would have spent an hour of GPU and produced a checkpoint trained on
nothing, indistinguishable at a glance from a successful one. The gates
below exist so that outcome is impossible rather than unlikely.

## The four gates, in order of cheapness

1. **Master flag** -- ``JARVIS_GRPO_AUTOTRAIN_ENABLED``, default FALSE per
   §33.1 shadow-first. Nothing below runs until an operator says so.
2. **Termination class** -- only a GRACEFUL end (wall-clock cap / TTL).
   A crashed or signal-killed session has a corpus of unknown
   completeness, and the flush that makes it complete runs in the same
   teardown this hook is part of.
3. **Corpus** -- delegated to ``scripts/grpo_preflight.py`` in the reactor
   repo, which answers with the TRAINER'S OWN grouping and flatness
   predicate. Exit 2 means "I looked and there is nothing to learn from",
   which is a healthy refusal and is logged as such, not as a fault.
4. **Device** -- handed over by J-Prime's training lease (drain, stop the
   engines, VERIFY the VRAM is free), never by "evict and hope": eviction
   let O+V reload the 30B on top of a trainer on 2026-10-07.

Gates 3 and 4 and the training itself run in the Training Lifecycle Handoff
(``observability.training_handoff``), a detached process; this module keeps
the cheap gates and the helpers both share.

## Why a subprocess and not an import

JARVIS and Reactor-Core are separate repositories with separate
virtualenvs, and the soak-side venv has no torch. A cross-repo import is
impossible; the contract is a command and a JSON document. Same boundary
``REACTOR_GRPO_VERIFY_CMD`` already uses in the other direction.

## Orphan safety

The child is started in its OWN process group (``start_new_session``), and
every exit path -- timeout, cancellation, or a caller that goes away --
kills the GROUP, not just the direct child. A training run spawns
dataloader workers and CUDA contexts; killing only the parent leaves those
holding the GPU, and the next soak then fails to load a model for reasons
that have nothing to do with the next soak. Verified free afterwards.

Nothing here raises. A telemetry-and-training convenience must never be
the reason a session cannot shut down.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# --- env keys (no literals below this block) --------------------------------
_ENV_MASTER = "JARVIS_GRPO_AUTOTRAIN_ENABLED"
_ENV_REACTOR_ROOT = "TRINITY_REACTOR_ROOT"
_ENV_TRAIN_PY = "TRINITY_REACTOR_PYTHON"
_ENV_PREFLIGHT_CMD = "TRINITY_GRPO_PREFLIGHT_CMD"
_ENV_TRAIN_CMD = "TRINITY_GRPO_TRAIN_CMD"
_ENV_GRACEFUL = "JARVIS_GRPO_AUTOTRAIN_GRACEFUL_STOPS"
_ENV_PREFLIGHT_TIMEOUT = "JARVIS_GRPO_AUTOTRAIN_PREFLIGHT_TIMEOUT_S"
_ENV_TRAIN_TIMEOUT = "JARVIS_GRPO_AUTOTRAIN_TIMEOUT_S"
_ENV_FREE_MIB = "JARVIS_GRPO_AUTOTRAIN_MIN_FREE_MIB"
_ENV_KILL_GRACE = "JARVIS_GRPO_AUTOTRAIN_KILL_GRACE_S"

#: Stop reasons that mean "the session ended on purpose". Substring match,
#: because the harness composes them (``wall_clock_cap+atexit_fallback``).
_DEFAULT_GRACEFUL = ("wall_clock_cap", "session_exhausted", "idle_timeout")

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return raw in _TRUTHY if raw else default


def _num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.getenv(name, "") or default)))
    except (TypeError, ValueError):
        return default


def _csv(name: str, default: Sequence[str]) -> Tuple[str, ...]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return tuple(default)
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def autotrain_enabled() -> bool:
    """Master flag. Default FALSE per §33.1 (shadow-first)."""
    return _flag(_ENV_MASTER)


# ---------------------------------------------------------------------------
# Discovery — the same shape as _discover_jprime_endpoint: ask, don't assume
# ---------------------------------------------------------------------------

def _reactor_root() -> Optional[Path]:
    """Locate the reactor repo without hardcoding a path.

    Explicit env wins. Otherwise look for a sibling checkout beside this
    one, which is how the Trinity repos are laid out. Returns None rather
    than guessing, so a missing repo is a clean refusal.
    """
    raw = (os.getenv(_ENV_REACTOR_ROOT) or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if (p / "reactor_core").is_dir() else None
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent.parent / "reactor"
        if (cand / "reactor_core").is_dir():
            return cand
        if (parent / "reactor" / "reactor_core").is_dir():
            return parent / "reactor"
    return None


def _reactor_python() -> Optional[str]:
    """The interpreter that HAS torch. Never this one."""
    raw = (os.getenv(_ENV_TRAIN_PY) or "").strip()
    if raw:
        return raw if Path(raw).exists() else None
    for cand in (
        Path.home() / ".venvs" / "reactor-train" / "bin" / "python",
        Path.home() / ".venvs" / "reactor" / "bin" / "python",
    ):
        if cand.exists():
            return str(cand)
    return shutil.which("python3")


def _preflight_cmd() -> Optional[List[str]]:
    raw = (os.getenv(_ENV_PREFLIGHT_CMD) or "").strip()
    if raw:
        return raw.split()
    root, py = _reactor_root(), _reactor_python()
    if not root or not py:
        return None
    script = root / "scripts" / "grpo_preflight.py"
    return [py, str(script)] if script.exists() else None


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------

async def _gpu_free_mib() -> Optional[int]:
    """Free VRAM per nvidia-smi, or None when there is no GPU to ask."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        first = (out or b"").decode("utf-8", "replace").strip().splitlines()
        return int(first[0].strip()) if first else None
    except Exception:  # noqa: BLE001 — a probe fault is "unknown", not an error
        logger.debug("[AutoTrain] nvidia-smi probe failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Subprocess with group-kill
# ---------------------------------------------------------------------------

async def _run(
    cmd: Sequence[str],
    *,
    timeout_s: float,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    log_path: Optional[Path] = None,
) -> Tuple[int, str]:
    """Run a child in its OWN process group; kill the GROUP on every exit.

    ``log_path`` sends the child's output straight to that file instead of a
    pipe: readable while the child runs, kept when a timeout reaps it, never
    held in this process's memory. A multi-hour trainer otherwise reported
    nothing until it exited and, on a timeout, nothing at all -- the case
    its log is needed most. The returned text is then the file's tail.

    ``start_new_session=True`` puts the child in a fresh group so that a
    timeout can reap the whole tree. A trainer forks dataloader workers and
    holds CUDA contexts; terminating only the direct child leaves those
    resident on the GPU, and the NEXT soak then fails to load a model for
    reasons entirely unrelated to itself.
    """
    sink = None
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        sink = open(log_path, "ab", buffering=0)  # noqa: SIM115 -- closed in finally
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=sink if sink is not None else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **(env or {})},
            start_new_session=True,
        )
    except BaseException:
        if sink is not None:
            sink.close()
        raise

    def _kill_group(sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _tail() -> str:
        return _log_tail(log_path) if log_path is not None else ""

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        text = _tail() if sink is not None else (out or b"").decode("utf-8", "replace")
        return proc.returncode or 0, text
    except asyncio.TimeoutError:
        _kill_group(signal.SIGTERM)
        grace = _num(_ENV_KILL_GRACE, 20.0, 1.0, 300.0)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            _kill_group(signal.SIGKILL)
        head = f"timeout after {timeout_s:.0f}s; process group reaped"
        return 124, f"{head}\n{_tail()}" if sink is not None else head
    except asyncio.CancelledError:
        # Teardown is cancelling us. Do NOT leave a trainer on the card.
        _kill_group(signal.SIGKILL)
        raise
    finally:
        if proc.returncode is None:
            _kill_group(signal.SIGKILL)
        if sink is not None:
            sink.close()


def _log_tail(path: Path, limit: int = 1 << 20) -> str:
    """The last ``limit`` bytes of a child's log (what callers parse)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def maybe_train_after_soak(
    *,
    stop_reason: str,
    session_id: str = "",
    release_within_s: Optional[float] = None,
) -> Dict[str, Any]:
    """REQUEST a training cycle if this ending qualifies. Returns at once.

    The cycle itself -- labeling, corpus gate, the J-Prime lease, GRPO,
    conversion, publish, verification -- is the Training Lifecycle Handoff
    (``observability.training_handoff``), run as its own detached process.
    It used to run HERE, inside the organism's teardown: a 30B cycle is
    hours, so the shutdown deadline was stretched to cover it while the
    independent out-of-process watchdog was not, and on 2026-10-07 that
    watchdog SIGKILLed the organism mid-teardown. Teardown now only asks.
    NEVER raises.
    """
    verdict: Dict[str, Any] = {
        "fired": False, "reason": "", "session_id": session_id,
        "stop_reason": stop_reason,
    }
    if not autotrain_enabled():
        verdict["reason"] = "disabled"
        return verdict
    graceful = _csv(_ENV_GRACEFUL, _DEFAULT_GRACEFUL)
    if not any(g in (stop_reason or "") for g in graceful):
        # A crashed or killed session has a corpus of unknown completeness.
        verdict["reason"] = f"stop_reason_not_graceful:{stop_reason}"
        return verdict
    try:
        from backend.core.ouroboros.governance.observability.training_handoff import (  # noqa: PLC0415
            request_cycle, request_timeout_s, training_yield,
        )
        # Gate 3 -- yield. A cycle costs hours of inference uptime, so it is
        # only worth starting once enough NEW landed evidence (git-proven,
        # newer than what the served adapter learned) has accumulated.
        # Below JARVIS_MIN_TRAINING_BATCH this returns at once: teardown ends
        # and the next boot goes straight back to Sentinel discovery.
        y = await asyncio.wait_for(training_yield(), timeout=request_timeout_s())
        verdict["yield"] = y
        if not y.get("met"):
            verdict["reason"] = (f"below_training_batch:{y.get('unlearned', '?')}<{y.get('threshold')}"
                                 + (f" ({y['error']})" if y.get("error") else ""))
            return verdict
        # This process is the requester and is still ending: the cycle
        # waits for it (bounded by this teardown's own budget), not refuses it.
        out = await asyncio.to_thread(request_cycle, trigger=f"session_end:{session_id}",
                                      requester_pid=os.getpid(), release_within_s=release_within_s)
    except Exception as exc:  # noqa: BLE001
        verdict["reason"] = f"request_failed:{type(exc).__name__}"
        return verdict
    verdict.update({"fired": bool(out.get("requested")), "reason": "requested" if out.get("requested")
                    else str(out.get("reason") or out), "request": out})
    return verdict


__all__ = ["autotrain_enabled", "maybe_train_after_soak"]
