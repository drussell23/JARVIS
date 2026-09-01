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
4. **Device** -- the card must actually be free. ollama holds ~21.8 GiB
   for ``JARVIS_LOCAL_MODEL_KEEP_ALIVE_SECONDS`` (1800) after the last op,
   so a trainer launched the instant a soak ends measures, and fails on,
   whatever is left. We evict, then VERIFY, and refuse if the eviction did
   not take -- never trusting the call.

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
import json
import logging
import os
import shutil
import signal
import time
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
_ENV_EVICT_WAIT = "JARVIS_GRPO_AUTOTRAIN_EVICT_WAIT_S"
_ENV_OLLAMA_URL = "JARVIS_LOCAL_MODEL_BASE_URL"
_ENV_OLLAMA_MODEL = "JARVIS_LOCAL_MODEL_NAME"
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


def _train_cmd() -> Optional[List[str]]:
    """The training entry point.

    Deliberately env-first with NO built-in default beyond the repo's own
    pipeline runner: what "train" means changes with the experiment, and a
    hardcoded argv here would silently pin one.
    """
    raw = (os.getenv(_ENV_TRAIN_CMD) or "").strip()
    if raw:
        return raw.split()
    root, py = _reactor_root(), _reactor_python()
    if not root or not py:
        return None
    script = root / "scripts" / "run_pipeline.py"
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


async def _evict_local_model() -> None:
    """Ask ollama to drop its resident model. Best-effort by design.

    ``keep_alive: 0`` is the documented way to release immediately. We do
    not check the response: the only answer that matters is what the card
    reports afterwards, which the caller polls.
    """
    base = (os.getenv(_ENV_OLLAMA_URL) or "").strip()
    model = (os.getenv(_ENV_OLLAMA_MODEL) or "").strip()
    if not base or not model:
        return
    payload = json.dumps({"model": model, "keep_alive": 0})
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "10", f"{base.rstrip('/')}/api/generate",
            "-d", payload,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15.0)
    except Exception:  # noqa: BLE001
        logger.debug("[AutoTrain] eviction request degraded", exc_info=True)


async def _await_free_card(need_mib: int, wait_s: float) -> Tuple[bool, Optional[int]]:
    """Evict, then poll until the card is actually free enough.

    Returns ``(ok, free_mib)``. ``free_mib is None`` means there is no GPU
    to measure, which is treated as "not our call to make" -- the trainer
    may legitimately be CPU-bound or remote.
    """
    free = await _gpu_free_mib()
    if free is None:
        return True, None
    if free >= need_mib:
        return True, free
    await _evict_local_model()
    deadline = time.monotonic() + max(0.0, wait_s)
    while time.monotonic() < deadline:
        await asyncio.sleep(min(5.0, max(0.5, wait_s / 12.0)))
        free = await _gpu_free_mib()
        if free is None:
            return True, None
        if free >= need_mib:
            return True, free
    return False, free


# ---------------------------------------------------------------------------
# Subprocess with group-kill
# ---------------------------------------------------------------------------

async def _run(
    cmd: Sequence[str],
    *,
    timeout_s: float,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str]:
    """Run a child in its OWN process group; kill the GROUP on every exit.

    ``start_new_session=True`` puts the child in a fresh group so that a
    timeout can reap the whole tree. A trainer forks dataloader workers and
    holds CUDA contexts; terminating only the direct child leaves those
    resident on the GPU, and the NEXT soak then fails to load a model for
    reasons entirely unrelated to itself.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        start_new_session=True,
    )

    def _kill_group(sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace")
    except asyncio.TimeoutError:
        _kill_group(signal.SIGTERM)
        grace = _num(_ENV_KILL_GRACE, 20.0, 1.0, 300.0)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            _kill_group(signal.SIGKILL)
        return 124, f"timeout after {timeout_s:.0f}s; process group reaped"
    except asyncio.CancelledError:
        # Teardown is cancelling us. Do NOT leave a trainer on the card.
        _kill_group(signal.SIGKILL)
        raise
    finally:
        if proc.returncode is None:
            _kill_group(signal.SIGKILL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def maybe_train_after_soak(
    *,
    stop_reason: str,
    session_id: str = "",
) -> Dict[str, Any]:
    """Run one GRPO cycle if -- and only if -- every gate agrees.

    Returns a structured verdict for the caller to log. NEVER raises; the
    only exception that escapes is CancelledError, and only after the child
    has been reaped.
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

    pre = _preflight_cmd()
    if not pre:
        verdict["reason"] = "preflight_command_unresolved"
        return verdict

    try:
        rc, out = await _run(
            pre, timeout_s=_num(_ENV_PREFLIGHT_TIMEOUT, 300.0, 10.0, 3600.0),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        verdict["reason"] = f"preflight_failed:{type(exc).__name__}"
        return verdict

    try:
        verdict["preflight"] = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:  # noqa: BLE001 — report is a bonus, rc is the answer
        verdict["preflight"] = {"raw": out[-400:]}

    if rc == 2:
        # A healthy refusal, not a fault. This is the expected outcome
        # whenever the corpus has no differentiated group.
        verdict["reason"] = "corpus_not_trainable"
        return verdict
    if rc != 0:
        verdict["reason"] = f"preflight_error:rc={rc}"
        return verdict

    need = int(_num(_ENV_FREE_MIB, 24000.0, 0.0, 1_000_000.0))
    ok, free = await _await_free_card(need, _num(_ENV_EVICT_WAIT, 120.0, 0.0, 3600.0))
    verdict["gpu_free_mib"] = free
    if not ok:
        verdict["reason"] = f"gpu_busy:{free}MiB_free_need_{need}"
        return verdict

    cmd = _train_cmd()
    if not cmd:
        verdict["reason"] = "train_command_unresolved"
        return verdict

    started = time.monotonic()
    try:
        rc, out = await _run(
            cmd,
            timeout_s=_num(_ENV_TRAIN_TIMEOUT, 7200.0, 60.0, 86400.0),
            cwd=_reactor_root(),
        )
    except asyncio.CancelledError:
        verdict["reason"] = "cancelled_during_training"
        raise
    except Exception as exc:  # noqa: BLE001
        verdict["reason"] = f"train_launch_failed:{type(exc).__name__}"
        return verdict

    verdict.update({
        "fired": True,
        "reason": "completed" if rc == 0 else f"train_rc={rc}",
        "returncode": rc,
        "duration_s": round(time.monotonic() - started, 1),
        "tail": out[-1200:],
    })
    # Whatever happened, the card must not be left held by our child.
    verdict["gpu_free_mib_after"] = await _gpu_free_mib()
    return verdict


__all__ = ["autotrain_enabled", "maybe_train_after_soak"]
