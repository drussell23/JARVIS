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

THE LIST IS EVIDENCE, NOT INTUITION
------------------------------------
Every default below came from a StallSampler dump taken while the loop was
provably wedged. Two candidates that look obvious are deliberately ABSENT:

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
)

__all__ = [
    "DEFAULT_PREWARM",
    "PREWARM_SCHEMA_VERSION",
    "prewarm_enabled",
    "prewarm_modules",
    "prewarm_stats",
    "spawn_prewarm",
]

_stats: Dict[str, Any] = {
    "started": False, "done": False, "warmed": [], "failed": [],
    "elapsed_s": 0.0,
}


def prewarm_enabled() -> bool:
    """``JARVIS_PREWARM_ENABLED`` (default true)."""
    return (os.environ.get("JARVIS_PREWARM_ENABLED", "1") or "").strip().lower() \
        not in ("0", "false", "no", "off", "")


def prewarm_modules() -> Tuple[str, ...]:
    """The list, with any env additions. NEVER raises."""
    try:
        extra = (os.environ.get("JARVIS_PREWARM_MODULES") or "").strip()
        added = tuple(m.strip() for m in extra.split(",") if m.strip())
        seen, out = set(), []
        for name in DEFAULT_PREWARM + added:
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


def spawn_prewarm() -> Optional["asyncio.Task"]:
    """Fire the warm as a DETACHED task. NEVER raises, never blocks.

    Detached on purpose: awaiting it here would move the very cost this
    exists to hide onto the caller. The task races the boot and loses
    gracefully — a module the loop reaches first is simply imported there,
    exactly as it is today.
    """
    if not prewarm_enabled():
        return None
    try:
        return asyncio.get_running_loop().create_task(
            _run(), name="jarvis-prewarm")
    except RuntimeError:
        return None                     # no loop — a sync context
    except Exception:  # noqa: BLE001
        logger.debug("[Prewarm] not scheduled", exc_info=True)
        return None
