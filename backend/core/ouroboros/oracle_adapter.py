"""Slice 113 — Unified async Oracle adapter (the wiring layer).

Slice 112 built the process-isolated Oracle (``AsyncOracleProxy``) + the
in-process ``TheOracle``, but they don't share a call signature: the in-process
Oracle's ``get_metrics`` / ``get_context_for_improvement`` are SYNC, the proxy's
are ASYNC + may return ``OracleNotReady``. This module gives the engine ONE
``await``-able interface over EITHER backend, selected by
``JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED`` — so the FSM/GLS call sites are
identical regardless of topology, and the OFF path stays byte-identical to the
legacy in-process behavior.

Degradation convention (load-bearing for call-site simplicity): the convenience
methods return **safe degraded values** rather than the raw ``OracleNotReady``
sentinel —

  * ``get_metrics()``                  → ``dict`` ({} while hydrating/crashed)
  * ``get_context_for_improvement()``  → ``dict`` ({} while hydrating/crashed)
  * ``incremental_update()``           → ``None`` (no-op while not ready)

so a call site just ``await``s and its existing empty-dict handling Just Works.
``is_ready`` + the structured ``OracleNotReady`` remain available for callers
that want to branch explicitly.

Composes: ``TheOracle`` (+ its ``OracleReadiness`` for the in-process ready
gate), ``AsyncOracleProxy`` (Slice 112), ``oracle_ipc.process_isolation_enabled``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("ouroboros.oracle_adapter")

_TRUTHY = ("1", "true", "yes", "on")


def _env_truthy(name: str) -> bool:
    try:
        return (os.environ.get(name, "") or "").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


class InProcessOracleAdapter:
    """Wraps the in-process ``TheOracle`` behind the unified async interface.
    Preserves the existing deferred-init behavior (non-blocking boot; readiness
    composed from ``OracleReadiness``). Default path — byte-identical effect to
    legacy, just reached through ``await``."""

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle
        self._init_task: Optional["asyncio.Task"] = None

    async def start(self) -> None:
        """Kick off initialization. Deferred (non-blocking) by default — the
        graph hydrates in the background; queries degrade to {} until ready.
        ``JARVIS_ORACLE_BLOCK_BOOT`` forces the legacy synchronous-await."""
        if _env_truthy("JARVIS_ORACLE_BLOCK_BOOT"):
            await self._oracle.initialize()
            return
        self._init_task = asyncio.ensure_future(self._safe_init())

    async def _safe_init(self) -> None:
        try:
            await self._oracle.initialize()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — readiness records the failure
            logger.warning("[OracleAdapter] in-process init failed (non-fatal): %s", exc)

    @property
    def is_ready(self) -> bool:
        """Graph-scope readiness. Best-effort: if the Oracle exposes no
        ``OracleReadiness`` primitive, assume ready (legacy oracles were always
        treated as usable once constructed). NEVER raises."""
        try:
            from backend.core.ouroboros.oracle_readiness import OracleReadinessScope
            readiness = getattr(self._oracle, "_readiness", None)
            if readiness is None:
                return True
            return bool(readiness.is_ready(OracleReadinessScope.GRAPH))
        except Exception:  # noqa: BLE001
            return True

    async def get_metrics(self) -> dict:
        try:
            return dict(self._oracle.get_metrics()) if self.is_ready else {}
        except Exception:  # noqa: BLE001
            return {}

    async def get_context_for_improvement(self, target: Any, max_depth: int = 2) -> dict:
        try:
            if not self.is_ready:
                return {}
            res = self._oracle.get_context_for_improvement(target, max_depth=max_depth)
            return res if isinstance(res, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def incremental_update(self, changed_files: Any = None) -> None:
        try:
            if self.is_ready:
                await self._oracle.incremental_update(changed_files)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[OracleAdapter] incremental_update swallowed: %s", exc)

    async def shutdown(self) -> None:
        if self._init_task is not None:
            self._init_task.cancel()
            try:
                await self._init_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await self._oracle.shutdown()
        except Exception:  # noqa: BLE001
            pass

    # Escape hatch for callers that need the raw underlying Oracle (e.g.
    # governance-stack wiring that predates the adapter). Read-only.
    @property
    def raw(self) -> Any:
        return self._oracle

    def __getattr__(self, name: str) -> Any:
        """Transparent delegation: any method NOT overridden above (e.g.
        ``find_nodes_in_file``, ``get_callers``, graph traversal) passes
        straight through to the wrapped in-process Oracle, so the adapter is a
        drop-in for every existing consumer — only the 5 engine call sites that
        opt into the async interface change. (Dunder/private names are NOT
        delegated, so this never shadows ``self._oracle`` lookups.)"""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._oracle, name)


class IsolatedOracleAdapter:
    """Wraps the Slice-112 ``AsyncOracleProxy`` behind the unified interface,
    normalizing the structured ``OracleNotReady`` sentinel into the safe
    degraded values the call sites expect."""

    def __init__(self, proxy: Any) -> None:
        self._proxy = proxy

    async def start(self) -> None:
        await self._proxy.start()

    @property
    def is_ready(self) -> bool:
        try:
            return bool(self._proxy.is_ready)
        except Exception:  # noqa: BLE001
            return False

    async def get_metrics(self) -> dict:
        r = await self._proxy.get_metrics()
        return r if isinstance(r, dict) else {}

    async def get_context_for_improvement(self, target: Any, max_depth: int = 2) -> dict:
        r = await self._proxy.get_context_for_improvement(target, max_depth=max_depth)
        return r if isinstance(r, dict) else {}

    async def incremental_update(self, changed_files: Any = None) -> None:
        # Returns OracleNotReady while hydrating — tolerated (best-effort no-op).
        await self._proxy.incremental_update(changed_files)

    async def shutdown(self) -> None:
        await self._proxy.shutdown()

    @property
    def raw(self) -> Any:
        return self._proxy

    def __getattr__(self, name: str) -> Any:
        """Under process isolation the graph lives in another process, so only
        the IPC'd surface is reachable. A non-IPC method (e.g. in-process graph
        traversal) cannot be served — raise a CLEAR error rather than silently
        delegating to a proxy that lacks it. Consumers needing those degrade."""
        if name.startswith("_"):
            raise AttributeError(name)
        raise AttributeError(
            f"{name!r} is not available on the process-isolated Oracle "
            f"(only the IPC surface is reachable; consumer should degrade)"
        )


def make_oracle_adapter(
    *,
    narrator: Any = None,
    oracle: Any = None,
    proxy: Any = None,
) -> Any:
    """Factory: return the isolated-process adapter when
    ``JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED`` is set, else the in-process
    adapter. ``oracle`` / ``proxy`` injectable for tests. NEVER raises — falls
    back to in-process on any error."""
    try:
        from backend.core.ouroboros.oracle_ipc import (
            AsyncOracleProxy,
            process_isolation_enabled,
        )
        if process_isolation_enabled():
            return IsolatedOracleAdapter(proxy or AsyncOracleProxy(narrator=narrator))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OracleAdapter] isolation path unavailable, using in-process: %s", exc)
    if oracle is None:
        from backend.core.ouroboros.oracle import TheOracle
        oracle = TheOracle()
    return InProcessOracleAdapter(oracle)
