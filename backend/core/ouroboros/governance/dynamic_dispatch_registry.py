"""What AST cannot see: modules reached by dispatch rather than by name.

Static reachability under-reports by construction. A pub/sub handler, an
``importlib`` load, a ``getattr`` route — none leaves an edge a caller index
can follow, so a live capability reads as dead. `capability_liveness` says so
itself: its verdict reason ends "may be dynamically dispatched — a review
candidate, not proven dead."

This is the other half of that sentence.

REGISTERED is not FIRING, and conflating them hides the bug
------------------------------------------------------------
The tempting design records a breadcrumb when a module subscribes or is
loaded, then treats anything in the registry as alive. That would be worse
than no registry at all.

Subscribing proves the module was IMPORTED and asked to be called. It proves
nothing about whether it ever was. A handler that registers at boot and never
receives an event is exactly the failure being hunted — and under
"registered ⇒ alive" it would flip from ``SILENT, investigate`` to
``dynamically dispatched, ignore``, which is the audit confidently clearing
the one case it exists to catch.

So two facts are tracked separately and they are not interchangeable:

``register(module)``
    "I am reachable by dispatch." Written at subscribe/load time.

``note_invocation(module)``
    "I actually ran." Written when the handler fires.

Only the second earns ``FIRING_DYNAMICALLY``. The first earns
``REGISTERED_NEVER_INVOKED`` — a verdict with no static equivalent, and
arguably the sharpest one available: the module declared itself reachable and
still never ran, which is a stronger claim than "no caller found" because it
rules out the innocent explanation.

Attribute-routed calls are a STATIC gap, not a dynamic one
-----------------------------------------------------------
Worth recording where this does NOT apply. ``repair_engine`` appeared 40%
severed, and the cause was not dynamic dispatch: its entry point is called at
``orchestrator.py:13014`` as
``self._config.repair_engine.run(ctx, ...)``. That is a perfectly static
call, invisible only to an index keyed on bare symbol names. A runtime
breadcrumb would have "fixed" that verdict while leaving the caller index
just as blind for every other injected dependency.

Cheap enough to leave on
-------------------------
Two dict writes under a lock, no I/O, bounded by
``JARVIS_DYNAMIC_DISPATCH_MAX_MODULES``. A breadcrumb that cost anything
would be turned off, and a breadcrumb that is off records nothing.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import functools
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.DynamicDispatch")

DYNAMIC_DISPATCH_SCHEMA_VERSION: str = "dynamic_dispatch.1"

#: Verdicts this registry can contribute. Deliberately three, not two.
FIRING_DYNAMICALLY = "FIRING_DYNAMICALLY"
REGISTERED_NEVER_INVOKED = "REGISTERED_NEVER_INVOKED"
UNSEEN = "UNSEEN"

__all__ = [
    "DYNAMIC_DISPATCH_SCHEMA_VERSION",
    "FIRING_DYNAMICALLY",
    "REGISTERED_NEVER_INVOKED",
    "UNSEEN",
    "DispatchRecord",
    "dynamic_verdict",
    "dynamically_dispatched",
    "flush_interval_s",
    "note_invocation",
    "read_ledger",
    "register",
    "registry_enabled",
    "reset_for_tests",
    "snapshot",
]


def registry_enabled() -> bool:
    """``JARVIS_DYNAMIC_DISPATCH_REGISTRY_ENABLED`` (default true).

    Default-ON because the cost is two dict writes and the alternative is an
    auditor that keeps calling live pub/sub handlers dead. OFF makes every
    lookup return ``UNSEEN``, which is the pre-registry behaviour exactly.
    """
    try:
        return os.environ.get(
            "JARVIS_DYNAMIC_DISPATCH_REGISTRY_ENABLED", "1",
        ).strip().lower() not in ("0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return True


def _max_modules() -> int:
    try:
        return max(16, min(100000, int(os.environ.get(
            "JARVIS_DYNAMIC_DISPATCH_MAX_MODULES", "4096") or 4096)))
    except (TypeError, ValueError):
        return 4096


@dataclass
class DispatchRecord:
    """One module's dynamic-reachability facts."""

    module: str
    registered_at: float = 0.0
    registrations: int = 0
    invocations: int = 0
    last_invoked_at: float = 0.0
    channels: Tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        if self.invocations > 0:
            return FIRING_DYNAMICALLY
        if self.registrations > 0:
            return REGISTERED_NEVER_INVOKED
        return UNSEEN


_records: Dict[str, DispatchRecord] = {}
_lock = threading.RLock()
_dropped = 0


def reset_for_tests() -> None:
    """Clear every breadcrumb. Test-only."""
    global _records, _dropped, _last_flush_mono  # noqa: PLW0603
    with _lock:
        _records = {}
        _dropped = 0
        _last_flush_mono = 0.0


def _normalise(module: Any) -> str:
    """A stable module key. NEVER raises.

    Accepts a module object, a dotted path, or a bare name, and reduces to
    the basename — which is what `capability_liveness` reports in
    ``source_file``. Keying on anything else means the intersection silently
    matches nothing, which would look exactly like "no dynamic evidence".
    """
    try:
        name = getattr(module, "__name__", None) or str(module or "")
        name = name.strip().replace("\\", "/")
        # Directories FIRST, then the extension, then a dotted package path.
        # Doing the extension first and splitting only on "." left
        # "governance/repair_engine.py" as "governance/repair_engine" — a key
        # that matches nothing, which is indistinguishable from "no dynamic
        # evidence" and would quietly re-report a live module as dead.
        name = name.rsplit("/", 1)[-1]
        if name.endswith(".py"):
            name = name[:-3]
        return name.rsplit(".", 1)[-1] if "." in name else name
    except Exception:  # noqa: BLE001
        return ""


def register(module: Any, *, channel: str = "") -> None:
    """Record "reachable by dispatch". NEVER raises.

    Called at subscribe/plugin-load time. This does NOT assert the module
    ran — see :func:`note_invocation`.
    """
    global _dropped  # noqa: PLW0603
    if not registry_enabled():
        return
    try:
        key = _normalise(module)
        if not key:
            return
        with _lock:
            rec = _records.get(key)
            if rec is None:
                if len(_records) >= _max_modules():
                    _dropped += 1
                    return
                rec = DispatchRecord(module=key, registered_at=time.time())
                _records[key] = rec
            rec.registrations += 1
            if channel and channel not in rec.channels:
                rec.channels = rec.channels + (str(channel),)
    except Exception:  # noqa: BLE001
        logger.debug("[DynamicDispatch] register degraded", exc_info=True)


#: Where the registry writes itself down, and why it must.
#:
#: An in-memory registry can only be READ from inside its own process, and a
#: new reader can only be installed by RESTARTING that process. Both halves bit
#: on the same day: a soak ran for three and a half hours holding a fully
#: populated registry, `/liveness` was written to show it, and the verb could
#: not be delivered to the daemon because the daemon predates the module. The
#: only way to observe the organism was to kill it.
#:
#: So it leaves evidence on disk. Same principle as ``__firing_ledgers__``
#: earlier in this file's history — durable, dated work is the only kind that
#: survives the process that did it — and the same canonical writer every
#: other ledger here uses (`cross_process_jsonl.flock_append_line`, flock-
#: serialized, never raises). A `.jarvis/**/*.jsonl` stem is also exactly what
#: `capability_firing` already treats as evidence-of-work, so the registry's
#: own liveness becomes provable by the mechanism it exists to serve.
_LEDGER_PATH = ".jarvis/ouroboros/dynamic_dispatch.jsonl"
_last_flush_mono: float = 0.0


def flush_interval_s() -> float:
    """``JARVIS_DYNAMIC_DISPATCH_FLUSH_S`` (default 30). ``0`` disables.

    A debounce, not a schedule: the write happens on the invocation that
    crosses the interval, so there is no timer to start, no task to own, and
    nothing to go inert when a caller forgets to drive it.
    """
    try:
        return max(0.0, float(os.environ.get(
            "JARVIS_DYNAMIC_DISPATCH_FLUSH_S", "30") or 30))
    except (TypeError, ValueError):
        return 30.0


def _maybe_flush() -> None:
    """Append one snapshot line if the debounce has elapsed. NEVER raises.

    Called from the invocation path, which is the honest "something happened"
    signal — registration alone proves nothing, exactly as the verdicts above
    insist. Deliberately NOT on the hot path's critical section: the lock is
    released first, the interval check is a monotonic compare, and any failure
    (lock contention, read-only fs, missing fcntl) is swallowed. Observability
    that can stall delivery is worse than no observability.
    """
    global _last_flush_mono  # noqa: PLW0603
    try:
        interval = flush_interval_s()
        if interval <= 0.0:
            return
        now = time.monotonic()
        if (now - _last_flush_mono) < interval:
            return
        _last_flush_mono = now
        payload = snapshot()
        payload["written_at_unix"] = time.time()
        payload["pid"] = os.getpid()
        import json as _json
        from pathlib import Path as _Path
        from backend.core.ouroboros.governance.cross_process_jsonl import (
            flock_append_line,
        )
        flock_append_line(
            _Path(_LEDGER_PATH),
            _json.dumps(payload, separators=(",", ":"), sort_keys=True),
            timeout_s=0.25,      # never wait on a reader
        )
    except Exception:  # noqa: BLE001
        logger.debug("[DynamicDispatch] flush degraded", exc_info=True)


def read_ledger(path: str = "") -> Optional[Dict[str, Any]]:
    """The NEWEST persisted snapshot, or None. NEVER raises.

    This is what makes the registry readable from a process that did not
    produce it — a fresh `ov` client, a post-mortem, a different machine
    reading a synced tree.
    """
    try:
        from pathlib import Path as _Path
        import json as _json
        target = _Path(path or _LEDGER_PATH)
        if not target.is_file():
            return None
        newest = None
        # Tail-read: the file is append-only and the last complete line wins.
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    newest = _json.loads(raw)
                except ValueError:
                    continue      # a torn line is skipped, never fatal
        return newest if isinstance(newest, dict) else None
    except Exception:  # noqa: BLE001
        return None


def note_invocation(module: Any, *, channel: str = "") -> None:
    """Record "actually ran". NEVER raises.

    The ONLY thing that earns ``FIRING_DYNAMICALLY``.
    """
    global _dropped  # noqa: PLW0603
    if not registry_enabled():
        return
    try:
        key = _normalise(module)
        if not key:
            return
        now = time.time()
        with _lock:
            rec = _records.get(key)
            if rec is None:
                if len(_records) >= _max_modules():
                    _dropped += 1
                    return
                rec = DispatchRecord(module=key, registered_at=now)
                _records[key] = rec
            rec.invocations += 1
            rec.last_invoked_at = now
            if channel and channel not in rec.channels:
                rec.channels = rec.channels + (str(channel),)
        # OUTSIDE the lock: the flush takes a file lock, and holding the
        # registry lock across it would let a slow disk serialise every
        # producer on the bus.
        _maybe_flush()
    except Exception:  # noqa: BLE001
        logger.debug("[DynamicDispatch] invocation degraded", exc_info=True)


def dynamic_verdict(module: Any) -> str:
    """``FIRING_DYNAMICALLY`` / ``REGISTERED_NEVER_INVOKED`` / ``UNSEEN``.

    Pure lookup. NEVER raises. ``UNSEEN`` when the registry is off, so a
    disabled registry cannot silently vouch for anything.
    """
    if not registry_enabled():
        return UNSEEN
    try:
        key = _normalise(module)
        with _lock:
            rec = _records.get(key)
        return rec.verdict if rec is not None else UNSEEN
    except Exception:  # noqa: BLE001
        return UNSEEN


def snapshot() -> Dict[str, Any]:
    """Bounded projection for observability surfaces. NEVER raises."""
    try:
        with _lock:
            rows = [
                {"module": r.module, "verdict": r.verdict,
                 "registrations": r.registrations,
                 "invocations": r.invocations,
                 "channels": list(r.channels)}
                for r in _records.values()
            ]
            dropped = _dropped
        rows.sort(key=lambda r: (r["verdict"], r["module"]))
        return {
            "schema_version": DYNAMIC_DISPATCH_SCHEMA_VERSION,
            "enabled": registry_enabled(),
            "tracked": len(rows),
            "dropped": dropped,
            "firing": sum(1 for r in rows if r["verdict"] == FIRING_DYNAMICALLY),
            "registered_never_invoked": sum(
                1 for r in rows if r["verdict"] == REGISTERED_NEVER_INVOKED),
            "rows": rows[:200],
        }
    except Exception:  # noqa: BLE001
        return {"schema_version": DYNAMIC_DISPATCH_SCHEMA_VERSION,
                "enabled": False, "tracked": 0, "rows": []}


def dynamically_dispatched(
    fn: Optional[Callable] = None, *, channel: str = "",
) -> Callable:
    """Mark a handler as dispatch-reached, and record when it FIRES.

    Registers at decoration time (import) and notes an invocation on every
    call, so one decorator produces both facts and they cannot drift apart.
    Wrapping is transparent for sync and async handlers alike; a breadcrumb
    that changed a handler's contract would not survive contact with the
    codebase.

    Usage::

        @dynamically_dispatched(channel="fs.changed")
        async def _on_fs_event(event): ...
    """
    def _decorate(func: Callable) -> Callable:
        module = getattr(func, "__module__", "") or ""
        register(module, channel=channel)

        try:
            import asyncio
            is_async = asyncio.iscoroutinefunction(func)
        except Exception:  # noqa: BLE001
            is_async = False

        if is_async:
            @functools.wraps(func)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                note_invocation(module, channel=channel)
                return await func(*args, **kwargs)
            return _async_wrapper

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            note_invocation(module, channel=channel)
            return func(*args, **kwargs)
        return _sync_wrapper

    if fn is not None:          # bare @dynamically_dispatched
        return _decorate(fn)
    return _decorate            # @dynamically_dispatched(channel=...)
