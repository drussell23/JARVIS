"""Interpreter-exit handlers that cannot spray a traceback on Ctrl+C.

The defect this closes is visible the moment an operator quits ``ov``::

    Error in atexit._run_exitfuncs:
    Traceback (most recent call last):
      ...
    KeyboardInterrupt

``atexit`` handlers run on the main thread during interpreter shutdown, so a
Ctrl+C arriving in that window is delivered *into* whichever handler happens
to be running. Handlers that block — draining a pool, waiting on child
processes — hold that window open long enough to be a real target, and a
second impatient Ctrl+C lands squarely inside one.

The usual ``except Exception`` guard does not help, and its presence is what
makes this surprising: ``KeyboardInterrupt`` and ``SystemExit`` derive from
``BaseException``, not ``Exception``. Handlers written to be defensive are
therefore *exactly* as exposed as handlers that were not, which is why the
traceback appears despite the try/except sitting right there in the source.

Nothing useful can be reported at this point anyway — stderr may already be
torn down, and the process is leaving regardless. The operator asked to quit;
the correct response is to quit, not to explain.

Deliberately NOT a blanket ``sys.excepthook`` or an ``atexit`` monkeypatch:
those would swallow tracebacks from handlers registered by third-party
libraries, where a traceback may be the only evidence of a real fault. This is
opt-in per registration site.
"""
from __future__ import annotations

import atexit
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = ["guarded_atexit_register", "run_guarded"]


def run_guarded(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run ``fn`` so that nothing whatsoever escapes.

    ``BaseException`` is caught on purpose. At interpreter-exit time the
    alternative to swallowing is a traceback printed over the operator's
    prompt, and there is no caller left to handle anything.
    """
    try:
        fn(*args, **kwargs)
    except BaseException:  # noqa: BLE001 — see module docstring
        # Best-effort breadcrumb. Logging itself may be half torn down during
        # shutdown, so even this is guarded; losing the breadcrumb is strictly
        # better than the traceback it replaces.
        try:
            logger.debug(
                "[exit_guard] suppressed exception from exit handler %r",
                getattr(fn, "__qualname__", fn),
                exc_info=True,
            )
        except BaseException:  # noqa: BLE001
            pass


def guarded_atexit_register(
    fn: Callable[..., Any], *args: Any, **kwargs: Any,
) -> Callable[..., Any]:
    """``atexit.register`` for a handler that must never print on the way out.

    Returns the registered wrapper (not ``fn``), which is what
    ``atexit.unregister`` needs if a caller ever wants to withdraw it.
    """
    def _wrapper() -> None:
        run_guarded(fn, *args, **kwargs)

    # Keep the wrapper identifiable in tracebacks and in atexit's own
    # bookkeeping — an anonymous frame is hard to attribute during shutdown.
    try:
        _wrapper.__qualname__ = (
            f"guarded[{getattr(fn, '__qualname__', repr(fn))}]"
        )
    except Exception:  # noqa: BLE001
        pass

    atexit.register(_wrapper)
    return _wrapper
