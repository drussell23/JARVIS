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

Two windows, one discipline
---------------------------
``atexit`` is not the only place an interrupt can land somewhere that cannot
report it. CPython also runs Python code with no caller in ``__del__``
methods, weakref callbacks and GC finalizers; an exception raised there is
*unraisable* — it cannot propagate, so the interpreter prints it and moves
on::

    Exception ignored in: <function WeakValueDictionary.__init__.<locals>.remove>
    Traceback (most recent call last):
      ...
    KeyboardInterrupt:

A signal handler is delivered at an arbitrary bytecode boundary, which means
a Ctrl+C can be delivered *into* one of those finalizers. The handler runs,
chains to ``signal.default_int_handler``, that raises — and the raise happens
with no caller to catch it. Same defect as the ``atexit`` one, one layer
down, and it needs the same answer at the seam the interpreter actually
routes it through: :data:`sys.unraisablehook`.

Narrow on purpose, for the reason stated below about ``sys.excepthook``: the
guard silences ONLY interrupt-class exceptions, and forwards everything else
to whatever hook was already installed. A blanket hook would swallow the
unraisable traceback from a third-party ``__del__``, and there that traceback
is frequently the only evidence a fault occurred at all.
"""
from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "guarded_atexit_register", "run_guarded",
    "install_unraisable_guard", "uninstall_unraisable_guard",
    "unraisable_guard_installed", "suppressed_unraisable_count",
]

#: The discriminator this whole module turns on, named once. ``KeyboardInterrupt``
#: and ``SystemExit`` derive from ``BaseException`` rather than ``Exception``,
#: which is why defensive ``except Exception`` handlers do not stop either of
#: them and why the traceback appears despite the try/except in the source.
_INTERRUPT_CLASS: Tuple[type, ...] = (KeyboardInterrupt, SystemExit)

_UNRAISABLE_LOCK = threading.Lock()
_PREVIOUS_HOOK: Optional[Any] = None
_SUPPRESSED = [0]


def _unraisable_guard_enabled() -> bool:
    """``JARVIS_UNRAISABLE_GUARD_ENABLED`` (default true). NEVER raises."""
    return os.environ.get(
        "JARVIS_UNRAISABLE_GUARD_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def suppressed_unraisable_count() -> int:
    """How many interrupt-class unraisables have been silenced.

    Observable rather than silent: a guard that hides things and keeps no
    count is indistinguishable from a guard that is not working, and the one
    question worth asking later is "did this ever actually fire".
    """
    return int(_SUPPRESSED[0])


def unraisable_guard_installed() -> bool:
    return getattr(sys.unraisablehook, "_ov_unraisable_guard", False) is True


def install_unraisable_guard() -> bool:
    """Chain a hook that silences interrupt-class unraisables. NEVER raises.

    Returns True when the guard is in place (including when it already was).
    Idempotent — the marker attribute is checked rather than a module flag, so
    a re-install after someone else replaced the hook chains correctly instead
    of believing itself already installed.
    """
    global _PREVIOUS_HOOK
    if not _unraisable_guard_enabled():
        return False
    try:
        with _UNRAISABLE_LOCK:
            if unraisable_guard_installed():
                return True
            previous = sys.unraisablehook
            _PREVIOUS_HOOK = previous

            def _hook(unraisable: Any) -> None:
                try:
                    exc_type = getattr(unraisable, "exc_type", None)
                    if (isinstance(exc_type, type)
                            and issubclass(exc_type, _INTERRUPT_CLASS)):
                        # The operator asked to quit. There is no caller, the
                        # process is leaving, and the only thing a traceback
                        # can do here is print over the goodbye.
                        _SUPPRESSED[0] += 1
                        return
                except BaseException:  # noqa: BLE001
                    pass
                # EVERYTHING else goes on to whoever was here first. A real
                # fault in someone's finalizer must still be visible.
                try:
                    previous(unraisable)
                except BaseException:  # noqa: BLE001
                    pass

            _hook._ov_unraisable_guard = True  # type: ignore[attr-defined]
            sys.unraisablehook = _hook
            return True
    except BaseException:  # noqa: BLE001
        return False


def uninstall_unraisable_guard() -> None:
    """Put the previous hook back. NEVER raises.

    Only unwinds OUR hook: if something else installed a hook on top of ours,
    restoring blindly would silently uninstall theirs too.
    """
    global _PREVIOUS_HOOK
    try:
        with _UNRAISABLE_LOCK:
            if not unraisable_guard_installed():
                return
            if _PREVIOUS_HOOK is not None:
                sys.unraisablehook = _PREVIOUS_HOOK
            _PREVIOUS_HOOK = None
    except BaseException:  # noqa: BLE001
        pass


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
