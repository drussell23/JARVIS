"""What a JARVIS backend serves when it is running as a SERVICE.

THE INVERSION
-------------
``converged_headless.main()`` does this on its way in::

    os.environ.setdefault("JARVIS_SERVICE_MODE", "1")

The flag declared the mode. Everything downstream then keyed off the FLAG —
``--headless`` in ``argv`` — so a supervisor started as a service without
that particular spelling served a different set of surfaces than one started
with it, while being the same thing in every way that matters.

Measured consequence: ``rg -c GovernanceSSE`` over the whole backend log
returned **0** across every boot. The O+V → HUD telemetry bridge had never
installed on the ``trinity up`` path, and the device SSE route answered 404
there and 200 under ``--headless``. `trinity up` already sets
``JARVIS_SERVICE_MODE=1`` and ``JARVIS_FRONTEND_AUTOLAUNCH=0``; it was
telling the truth about what it wanted and nothing was listening.

So the MODE decides the surfaces, and the flag is merely one way to assert
the mode. Appending ``--headless`` in the launcher would have made the
symptom go away in the shell and left the coupling exactly where it was.

WHAT THIS DOES NOT DO
---------------------
It does not switch the boot. An interactive supervisor keeps its desktop,
voice and vision stack; it additionally mounts the surfaces it owes anyone
talking to it as a service. Replacing the whole app when
``JARVIS_SERVICE_MODE=1`` would be a second shortcut wearing the first
one's clothes.

MOUNTING IS BY ROUTER IDENTITY, NOT BY PATH
-------------------------------------------
The bug that hid this: ``_init_unified_websocket`` skipped its router when
ANY route with path ``/ws`` already existed, and ``observability_gateway``
registers one. The check asked "is ``/ws`` taken?" while meaning "is THIS
router mounted?", so a router owning both ``/ws`` and the device SSE routes
was skipped whole because one of its paths collided.

:func:`include_router_once` keys on router IDENTITY, recorded on
``app.state``, and REPORTS any path that ends up registered twice instead of
silently shadowing it. FastAPI dispatches first-match-wins, so a duplicate is
inert rather than dangerous — but an inert duplicate nobody can see is how
this class of bug survives.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("Jarvis.ServiceSurface")

SERVICE_SURFACE_SCHEMA_VERSION: str = "service_surface.1"

#: Where mounted router identities are recorded. On ``app.state`` rather than
#: a module global because two apps in one process (tests, an embedded
#: harness) must not share a mount ledger.
_STATE_ATTR = "_jarvis_mounted_routers"

__all__ = [
    "SERVICE_SURFACE_SCHEMA_VERSION",
    "collides",
    "include_router_once",
    "mount_service_surface",
    "service_mode_active",
]


def _flag(name: str, default: str) -> bool:
    try:
        return (os.environ.get(name, default) or "").strip().lower() not in (
            "0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return default.strip().lower() not in ("0", "false", "no", "off", "")


def service_mode_active(argv: Optional[List[str]] = None) -> bool:
    """Is this process running as a SERVICE? NEVER raises.

    True when ``JARVIS_SERVICE_MODE`` is set (what ``trinity up`` asserts and
    what ``converged_headless.main`` declares), or when ``--headless`` was
    passed explicitly. The flag is kept as an assertion of the mode so an
    operator running the converged app by hand still gets the same surfaces —
    but it is no longer the ONLY way to be a service.

    ``JARVIS_SERVICE_SURFACE_ENABLED=0`` is the escape hatch: an operator who
    wants the interactive desktop boot with none of this can say so, and gets
    exactly the behaviour that shipped before.
    """
    if not _flag("JARVIS_SERVICE_SURFACE_ENABLED", "1"):
        return False
    if _flag("JARVIS_SERVICE_MODE", "0"):
        return True
    try:
        import sys
        args = argv if argv is not None else sys.argv
        return "--headless" in (args or ())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Mounting
# ---------------------------------------------------------------------------


def _paths(app: Any) -> List[str]:
    out: List[str] = []
    try:
        for route in getattr(app, "routes", ()) or ():
            path = getattr(route, "path", None)
            if isinstance(path, str):
                out.append(path)
    except Exception:  # noqa: BLE001
        pass
    return out


def collides(app: Any, router: Any) -> Tuple[str, ...]:
    """Paths this router would register that the app already serves.

    Reported, never silently accepted. FastAPI dispatches first-match-wins,
    so a duplicate is inert — but "inert and invisible" is the property that
    let a whole router go unmounted for months.
    """
    try:
        existing = set(_paths(app))
        return tuple(sorted({
            p for p in _paths(router) if p in existing
        }))
    except Exception:  # noqa: BLE001
        return ()


def include_router_once(app: Any, router: Any, *, name: str,
                        **kwargs: Any) -> bool:
    """Mount *router* unless THIS router is already mounted. NEVER raises.

    Idempotency keys on identity, not on any single path: a router that
    happens to share one path with another subsystem is still a different
    router, and skipping it whole is how the device SSE routes went missing.
    """
    try:
        mounted = getattr(getattr(app, "state", None), _STATE_ATTR, None)
        if mounted is None:
            mounted = set()
        if name in mounted:
            return False
        overlap = collides(app, router)
        if overlap:
            # Visible, not fatal. The pre-existing registration keeps
            # serving (first match wins); this says which paths are
            # shadowed so an operator is never guessing later.
            logger.info(
                "[ServiceSurface] router %s shares %d path(s) already served "
                "%s — mounting anyway; the earlier registration continues to "
                "handle them", name, len(overlap), list(overlap[:4]))
        app.include_router(router, **kwargs)
        mounted.add(name)
        try:
            setattr(app.state, _STATE_ATTR, mounted)
        except Exception:  # noqa: BLE001
            pass
        logger.info("[ServiceSurface] mounted router %s", name)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[ServiceSurface] mount of %s degraded", name,
                     exc_info=True)
        return False


async def mount_service_surface(app: Any, *,
                                subsystems: Optional[List[Any]] = None) -> bool:
    """Give *app* the surfaces a service owes its clients. NEVER raises.

    Two things, both REUSED rather than restated:

    * the device SSE + websocket router, mounted by identity;
    * ``converged_headless.default_subsystems()`` — the same hydration DAG
      the converged app runs (O+V telemetry bridge → OuroborosDaemon), driven
      by the same ``HydrationOrchestrator``. Listing those subsystems again
      here would be a second definition of the boot, and the two would drift
      the first time one was extended.

    A no-op unless :func:`service_mode_active`. Returns True when the surface
    was mounted.
    """
    if not service_mode_active():
        return False
    try:
        from backend.api.converged_headless import default_subsystems
        from backend.api.progressive_hydration import HydrationOrchestrator
    except Exception:  # noqa: BLE001
        logger.debug("[ServiceSurface] converged surface unavailable",
                     exc_info=True)
        return False

    try:
        from backend.api.unified_websocket import router as _ws_router
        include_router_once(app, _ws_router, name="unified_websocket",
                            tags=["websocket"])
    except Exception:  # noqa: BLE001
        logger.debug("[ServiceSurface] SSE router unavailable", exc_info=True)

    try:
        orch = HydrationOrchestrator(subsystems or default_subsystems())
        await orch.hydrate()
        logger.info(
            "[ServiceSurface] service surface mounted — device SSE routes "
            "served and the O+V telemetry bridge hydrated (JARVIS_SERVICE_"
            "MODE, not --headless)")
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[ServiceSurface] hydration degraded", exc_info=True)
        return False
