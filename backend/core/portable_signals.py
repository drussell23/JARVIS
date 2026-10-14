"""
portable_signals - install a shutdown signal handler on any OS's event loop.

``loop.add_signal_handler`` exists only on POSIX loops; on Windows it raises
NotImplementedError, which killed the loading server at startup and was
caught and re-implemented in eight places, some calling the loop-side handler
straight from signal context. Here: the loop's own mechanism where it exists,
else ``signal.signal`` whose handler only schedules the callback on the loop
(``call_soon_threadsafe``) -- the callback always runs ON the loop.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Callable, Iterable, List

logger = logging.getLogger(__name__)


def install_loop_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    signals: Iterable[signal.Signals],
    callback: Callable[[signal.Signals], None],
) -> List[signal.Signals]:
    """Run ``callback(sig)`` on *loop* when any of *signals* arrives.

    Returns the signals actually wired. A signal this process may not handle
    (not the main thread, unsupported on the OS) is skipped and logged, never
    raised: a missing handler must not stop the program that asked for one.
    """
    wired: List[signal.Signals] = []
    for sig in signals:
        try:
            loop.add_signal_handler(sig, callback, sig)
            wired.append(sig)
            continue
        except NotImplementedError:
            pass
        except (ValueError, RuntimeError, OSError) as exc:
            logger.debug("[PortableSignals] %s not wired on loop: %s", sig, exc)
            continue
        try:
            signal.signal(sig, lambda _n, _f, s=sig: loop.call_soon_threadsafe(callback, s))
            wired.append(sig)
        except (ValueError, OSError) as exc:
            logger.debug("[PortableSignals] %s not wired: %s", sig, exc)
    return wired


__all__ = ["install_loop_signal_handlers"]
