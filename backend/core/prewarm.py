"""Pay the first-import tax before anything is waiting on it.

WHY A PRE-WARM AND NOT MORE OFFLOADING
--------------------------------------
The recurring loop blockers were fixed by moving work off the loop. What
remained was a different shape: the FIRST import of a module, executed
wherever the loop happened to reach it. Offloading each of those means
finding every async boundary that might touch a heavy module — whack-a-mole
against an import graph that changes every week.

An import is paid once per process. So it is paid HERE, early, on a worker,
and every later touch is a `sys.modules` hit no matter which coroutine gets
there first. The downstream code is not changed at all, which is the point:
nothing has to remember this exists.

WHY ONE WORKER AND NOT A POOL
------------------------------
`async_offload.import_off_loop` funnels every deferred import through a
SINGLE worker, and its docstring records why in measured detail: CPython
takes a per-module lock, so N threads importing a shared dependency graph
serialize on those locks anyway — with the loop thread queued in among them.
That is how ~2s of work became a 12.38s wedge. Warming in parallel would
recreate exactly that. This dispatches sequentially through the existing
primitive; nothing new is invented here.

TWO TIERS, AND ONLY ONE OF THEM IS EVIDENCE
--------------------------------------------
`DEFAULT_PREWARM` is first-party and evidence-derived: every entry came from
a StallSampler dump taken while the loop was provably wedged.

`DEFAULT_LIBRARY_PREWARM` is third-party and inherited -- it was a hardcoded
list inside `unified_supervisor._prewarm_python_modules`, a SECOND
pre-warmer doing this same job under this same `[Prewarm]` log tag. The two
were consolidated here rather than left to run side by side. That list is
intuition, not measurement, and is kept only because it was already in
production; a name in it has not been shown to block anything.

Two candidates that look obvious are deliberately ABSENT from the evidence
tier:

* ``Quartz`` — never imported in this process. The cursor probe runs an
  `osascript` that spawns its OWN python to import it, so warming it here
  would cost time and change nothing. (That path is separately bounded by
  `bounded_subprocess`.)
* ``torch`` directly — reached through `safetensors.torch`, which is listed;
  importing torch twice by two names is the same graph and the same locks.

``JARVIS_PREWARM_MODULES`` (comma-separated) extends the list without code,
and ``JARVIS_PREWARM_ENABLED=0`` disables it entirely.

FAILURE IS EXPECTED AND FINE
-----------------------------
A module that is not installed on this machine simply is not warmed — the
import that would have failed later fails here instead, in a worker, with
nobody waiting. Nothing raises out of this module, and a warm that cannot
run leaves behaviour exactly as it was.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Jarvis.Prewarm")

PREWARM_SCHEMA_VERSION: str = "prewarm.1"

#: Evidence-derived. Each entry was observed importing ON the event loop in a
#: StallSampler dump, or is the direct dependency that import pulls.
DEFAULT_PREWARM: Tuple[str, ...] = (
    # Caught at `backend/system/__init__.py:21 <module>`, reached from
    # `main._collect_bridge_health` inside a FastAPI health endpoint — an
    # import executing while a request was being served.
    "backend.system",
    # `ai_loader.discover_engines` probes. Warmed at the voice-model
    # initializer too, but that is late; this is the same graph, earlier.
    "safetensors.torch",
    "voice_unlock.ml.quantized_models",
    # `experience_queue._check_reactor_health` constructs this; the class is
    # cached now, the module import is not.
    "backend.core.trinity_ipc",
    # The registry walk `audit_ratchet.registered_ratchets` triggers.
    "backend.core.ouroboros.battle_test.repl_dispatch_registry",
    # Caught at `coding_council/orchestrator.py:273 <module>`, reached from
    # `main.health_check` -> `get_coding_council_health` ->
    # `get_coding_council`, which does a LAZY `from .orchestrator import ...`
    # inside an async function. The dump's innermost frame was
    # `importlib._bootstrap_external._write_atomic` -- the loop was writing a
    # .pyc while an HTTP health request waited. Cold import measured at
    # 325ms. Same shape as `backend.system` above, found the same way: by
    # lowering JARVIS_STALL_SAMPLER_TRIGGER_S below the stall and reading
    # the stack, rather than by adding another instrument.
    "backend.core.coding_council.orchestrator",
    # Caught with the IDENTICAL signature as the entry above -- `_write_atomic`
    # -> `_cache_bytecode` -> `exec_module` -> `agent_initializer.py:25
    # <module>` -- in the run that CONFIRMED the coding_council fix. Cold
    # import measured at 12,929ms, the largest single import cost found in
    # this arc, and it was landing on the event loop.
    #
    # It is placed LAST IN THIS TIER on purpose. The warm is sequential
    # through one worker, so a 13-second entry ahead of the cheap,
    # already-proven modules would delay every one of them. The inherited
    # library tier does follow it and will wait -- acceptable, because those
    # names are unmeasured guesses while this one is a measured 13s that
    # currently lands on the loop. Warming it cannot make anything worse than
    # the status quo, where the same cost is paid by whichever coroutine
    # reaches it first.
    "backend.neural_mesh.agents.agent_initializer",
)

#: Heavy THIRD-PARTY libraries. Moved here verbatim from a hardcoded list
#: inside `unified_supervisor._prewarm_python_modules`, which was a second
#: pre-warmer built for the same job and logging under the same `[Prewarm]`
#: tag -- discovered only because both lines appeared in one boot log and the
#: duplication was momentarily unreadable.
#:
#: Consolidating was not cosmetic. That implementation warmed through
#: `run_in_executor(None, ...)` -- the DEFAULT executor, shared with the 200+
#: `to_thread` sites in this codebase -- which is precisely the arrangement
#: `import_off_loop`'s docstring records as turning ~2s of imports into a
#: 12.38s wedge, because CPython takes a per-module lock and the loop thread
#: ends up queued among the contenders. Routing these through the single
#: serialized import worker removes that hazard, and the names stop being
#: literals buried in a 102K-line file.
DEFAULT_LIBRARY_PREWARM: Tuple[str, ...] = (
    # ML/AI (slowest). `torch` is in the supervisor's original list and is
    # deliberately NOT carried over: this module's docstring already records
    # why -- it is reached through `safetensors.torch` above, and the same
    # graph under two names is the same import and the same per-module locks.
    # Copying it here would have made the docstring lie about its own list.
    "transformers", "numpy", "scipy", "sklearn",
    # Audio/voice
    "librosa", "sounddevice", "pyaudio",
    # Database
    "asyncpg", "sqlalchemy",
    # Web
    "aiohttp", "websockets",
    # System
    "psutil", "watchdog",
)

__all__ = [
    "DEFAULT_PREWARM",
    "PREWARM_SCHEMA_VERSION",
    "prewarm_enabled",
    "prewarm_modules",
    "DEFAULT_LIBRARY_PREWARM",
    "prewarm_result",
    "prewarm_stats",
    "spawn_prewarm",
]

_task: Optional["asyncio.Task"] = None

_stats: Dict[str, Any] = {
    "started": False, "done": False, "warmed": [], "failed": [],
    "elapsed_s": 0.0,
}


def prewarm_enabled() -> bool:
    """``JARVIS_PREWARM_ENABLED`` (default true)."""
    return (os.environ.get("JARVIS_PREWARM_ENABLED", "1") or "").strip().lower() \
        not in ("0", "false", "no", "off", "")


def prewarm_modules() -> Tuple[str, ...]:
    """The full list, with any env additions. NEVER raises.

    First-party evidence entries lead, because each was observed blocking the
    loop and is cheap; the third-party libraries follow. Order matters only
    in that the warm is sequential and may be cut short by shutdown -- the
    things measured to hurt should be paid for first.
    """
    try:
        extra = (os.environ.get("JARVIS_PREWARM_MODULES") or "").strip()
        added = tuple(m.strip() for m in extra.split(",") if m.strip())
        seen, out = set(), []
        for name in DEFAULT_PREWARM + DEFAULT_LIBRARY_PREWARM + added:
            if name not in seen:
                seen.add(name)
                out.append(name)
        return tuple(out)
    except Exception:  # noqa: BLE001
        return DEFAULT_PREWARM


def prewarm_stats() -> Dict[str, Any]:
    """Bounded projection. NEVER raises."""
    return {"schema_version": PREWARM_SCHEMA_VERSION, **_stats,
            "warmed": list(_stats["warmed"]), "failed": list(_stats["failed"])}


async def _run() -> None:
    """Import each module, sequentially, off the loop. NEVER raises."""
    import sys

    names = prewarm_modules()
    t0 = time.monotonic()
    _stats["started"] = True
    warmed: List[str] = []
    failed: List[str] = []
    for name in names:
        if name in sys.modules:
            continue                    # already paid by someone else
        try:
            from backend.core.async_offload import import_off_loop
            await import_off_loop(name)
            warmed.append(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — absent module, bad graph
            failed.append(f"{name}: {type(exc).__name__}")
    _stats["warmed"] = warmed
    _stats["failed"] = failed
    _stats["elapsed_s"] = round(time.monotonic() - t0, 3)
    _stats["done"] = True
    if warmed or failed:
        logger.info(
            "[Prewarm] %d module(s) warmed in %.2fs%s — later imports are "
            "sys.modules hits on whichever coroutine reaches them first",
            len(warmed), _stats["elapsed_s"],
            f", {len(failed)} unavailable" if failed else "")


def prewarm_result() -> Dict[str, Any]:
    """The legacy shape `unified_supervisor._prewarm_python_modules` returned.

    Kept so consolidating the two pre-warmers cannot change what that method
    hands back, even though its single call site discards the value. A
    delegation that quietly alters a return type is how a harmless-looking
    cleanup becomes someone else's bug six months later.
    """
    return {
        "modules_loaded": list(_stats["warmed"]),
        "modules_failed": list(_stats["failed"]),
        "total_time_ms": float(_stats["elapsed_s"]) * 1000.0,
    }


def spawn_prewarm() -> Optional["asyncio.Task"]:
    """Fire the warm as a DETACHED task. NEVER raises, never blocks.

    Detached on purpose: awaiting it here would move the very cost this
    exists to hide onto the caller. The task races the boot and loses
    gracefully — a module the loop reaches first is simply imported there,
    exactly as it is today.
    """
    global _task  # noqa: PLW0603
    if not prewarm_enabled():
        return None
    # SINGLE-FLIGHT. There are now two spawn points -- the detached call at
    # the top of `async_main` and the supervisor's own background-task phase
    # -- and without this the second would re-enter `_run` and race the first
    # through the same import worker. Returning the live task makes the
    # second caller a no-op that still gets something awaitable.
    if _task is not None and not _task.done():
        return _task
    try:
        _task = asyncio.get_running_loop().create_task(
            _run(), name="jarvis-prewarm")
        return _task
    except RuntimeError:
        return None                     # no loop — a sync context
    except Exception:  # noqa: BLE001
        logger.debug("[Prewarm] not scheduled", exc_info=True)
        return None
