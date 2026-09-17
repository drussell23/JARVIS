"""What the INSTALLED Anthropic SDK will actually accept.

## The defect this exists for

Measured in bt-2026-09-09-024244, on the fallback path, 8 seconds after the
local lane hit a latency lockup::

    [ContextExpander] op=op-01a0841b round=1 plan() failed:
        AsyncMessages.create() got an unexpected keyword argument 'temperature'

Context expansion stopped, and the op continued with an unexpanded context. The
call site was not wrong when it was written: ``temperature`` was a documented
parameter of ``messages.create`` for the SDK generation this code was built
against. It was REMOVED from the request surface -- sampling parameters
(``temperature``, ``top_p``, ``top_k``) are rejected on the current model
family, and the Python SDK dropped them from the signature to match.

The same SDK major is behind the sibling warning one line earlier ::

    custom http_client rejected by the anthropic SDK (httpx version drift:
    Expected an instance of `httpx2.AsyncClient` but got <class 'httpx.AsyncClient'>)

Both are one event: the provider was written for an SDK surface that has since
moved.

## Why this is derived rather than a list

Hardcoding ``{"temperature", "top_p", "top_k"}`` here would encode TODAY'S
surface as a constant and leave the next removal to be discovered the same way
-- live, on a fallback path, with the op already in flight. The SDK's own
signature is the authority on what the SDK accepts, so that is what this reads.
A parameter restored in a later release starts flowing again with no edit here;
one removed stops being sent.

## Failure direction

Fail OPEN: if the signature cannot be introspected, kwargs pass through
UNCHANGED. This module is a compatibility shim, and a shim that cannot read the
surface must not start deciding what the request contains -- an unreadable
signature would otherwise strip every parameter and turn a working call into a
malformed one. The SDK raising ``TypeError`` is the status quo it replaces, not
something it can make worse.

Dropping is never silent: every dropped parameter is reported to the caller and
logged once per (method, parameter), because a request whose sampling was
discarded is not the request the caller described.
"""
from __future__ import annotations

import inspect
import logging
import threading
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.Providers")

__all__ = [
    "supported_params",
    "sanitize",
    "reset_cache_for_tests",
]

# One warning per (method, parameter) for the life of the process. The drop is
# structural -- it repeats on every call -- and a per-call warning would bury
# the log it is trying to inform.
_warned: set = set()
_cache: Dict[str, Optional[frozenset]] = {}
_lock = threading.Lock()


def _resolve(method: str) -> Optional[frozenset]:
    """Parameter names ``AsyncMessages.<method>`` accepts.

    ``None`` means "do not filter" -- either the signature could not be read,
    or it declares ``**kwargs`` and therefore accepts anything. NEVER raises.
    """
    try:
        from anthropic.resources.messages import AsyncMessages  # noqa: PLC0415

        fn = getattr(AsyncMessages, method, None)
        if fn is None:
            return None
        params = inspect.signature(fn).parameters
        for p in params.values():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                return None
        return frozenset(n for n in params if n != "self")
    except Exception:  # noqa: BLE001 -- an unreadable surface filters nothing
        logger.debug(
            "[SDKSurface] could not introspect AsyncMessages.%s — passing "
            "kwargs through unfiltered", method, exc_info=True,
        )
        return None


def supported_params(method: str = "create") -> Optional[frozenset]:
    """Cached :func:`_resolve`. The signature cannot change within a process."""
    with _lock:
        if method not in _cache:
            _cache[method] = _resolve(method)
        return _cache[method]


def sanitize(
    kwargs: Mapping[str, Any], *, method: str = "create", model: str = "",
) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    """Return *kwargs* reduced to what the installed SDK accepts.

    Returns ``(sanitized, dropped)``. ``dropped`` is ordered and empty in the
    normal case, so a caller can record that its request was altered -- the
    entropy ladder, for one, must not count a draw as temperature-varied when
    the temperature never reached the wire.
    """
    allowed = supported_params(method)
    if allowed is None:
        return dict(kwargs), ()
    dropped = tuple(k for k in kwargs if k not in allowed)
    if not dropped:
        return dict(kwargs), ()
    for name in dropped:
        key = (method, name)
        with _lock:
            first = key not in _warned
            if first:
                _warned.add(key)
        if first:
            logger.warning(
                "[SDKSurface] the installed anthropic SDK's "
                "AsyncMessages.%s does not accept %r — dropping it from the "
                "request (model=%s). Sampling parameters were removed from the "
                "Messages API request surface; the call now proceeds instead "
                "of raising TypeError, but the model chooses its own sampling.",
                method, name, model or "?",
            )
    return {k: v for k, v in kwargs.items() if k in allowed}, dropped


def reset_cache_for_tests() -> None:
    """Drop the memoised signature + warning state. Tests only."""
    with _lock:
        _cache.clear()
        _warned.clear()
