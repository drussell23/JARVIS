"""The shutdown handler must be async-signal-safe.

A SIGTERM landing during interpreter finalization segfaulted this process:

    _PyErr_CheckSignalsTstate -> handle_signals -> _Py_HandlePending   (x3)
      -> module_dealloc -> dict_dealloc -> dictkeys_decref -> SIGSEGV at 0x0

Two causes, both inside the handler: it built ``signal.Signals(signum).name``
and called ``logger.info``. Both allocate and touch module dictionaries that
are already being torn down, and both execute enough bytecode to trigger
another signal check — which re-entered the handler, which is the repetition
visible in the trace.

These tests assert the handler's BODY, not its behaviour under a signal,
because the failure only reproduces during finalization and a test that tried
to stage that would be testing CPython rather than us.
"""
from __future__ import annotations

import ast
import inspect
import signal

from backend.core.thread_manager import ExecutorRegistry


def _handler_source() -> str:
    """The nested handler's source, dedented so it parses standalone.

    textwrap.dedent alone is not enough: the comment lines inside the handler
    sit at a different indent than the body, so the common prefix it strips is
    too short. Stripping a fixed 8 columns matches the nesting."""
    import textwrap

    src = inspect.getsource(ExecutorRegistry._register_signal_handlers)
    start = src.index("def signal_handler")
    block = src[start:src.index("# Only register")]
    lines = [ln[8:] if ln.startswith(" " * 8) else ln for ln in block.splitlines()]
    return textwrap.dedent("\n".join(lines))


def test_handler_makes_no_unsafe_calls() -> None:
    """Only the flag may be touched. Anything that allocates, formats, logs or
    imports is forbidden — those are what crashed it."""
    body = ast.parse(_handler_source()).body[0]
    called = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "?"))
        for n in ast.walk(body) if isinstance(n, ast.Call)
    }
    assert called <= {"set"}, f"handler calls unsafe functions: {called - {'set'}}"


def test_handler_never_logs_or_builds_enums() -> None:
    src = _handler_source()
    for forbidden in ("logger", "Signals(", "format(", "f\"", "import "):
        assert forbidden not in src, f"handler contains {forbidden!r}"


def test_handler_is_reentrancy_guarded() -> None:
    """A second signal arriving while the first is flagged must return
    immediately — the repetition in the crash trace was the handler
    re-entering itself."""
    src = _handler_source()
    assert "if self._pending_signal:" in src and "return" in src


def test_flagging_then_draining_round_trips() -> None:
    reg = ExecutorRegistry.__new__(ExecutorRegistry)
    reg._pending_signal = 0

    class _Ev:
        def __init__(self): self.hit = False
        def set(self): self.hit = True
    reg._global_shutdown_event = _Ev()

    # Simulate what the handler does — exactly, with no signal involved.
    reg._pending_signal = int(signal.SIGTERM)
    reg._global_shutdown_event.set()

    assert reg._global_shutdown_event.hit
    assert reg.drain_pending_signal() == int(signal.SIGTERM)
    assert reg.drain_pending_signal() == 0, "drain is not idempotent"


def test_drain_survives_an_unknown_signal_number() -> None:
    reg = ExecutorRegistry.__new__(ExecutorRegistry)
    reg._pending_signal = 9999
    assert reg.drain_pending_signal() == 9999
