"""Keep a test-owned watchdog from killing the test runner.

``BoundedShutdownWatchdog`` exists to hard-``os._exit(75)`` a harness whose
shutdown has wedged. ``BattleTestHarness.__init__`` constructs one with the
default ``exit_fn`` — i.e. the real ``os._exit`` — and it runs on a daemon
thread. Ten test files build a harness; none of them stop it.

So any test that arms a harness's watchdog leaves a live thread holding a
deadline. Nothing fails at the time. Minutes later, mid-way through an
unrelated file, the deadline elapses and ``os._exit(75)`` takes the entire
pytest process down with it — no summary, no failure list, no traceback
pointing anywhere near the test that armed it.

That is what made ``pytest tests/battle_test/`` unrunnable as a directory
while every file passed on its own, and why the crash appeared to move
around: it lands wherever the clock happens to be.

The fix is scoped to the DEFAULT. A test that explicitly passes ``exit_fn``
is exercising the exit contract deliberately and is left exactly as it was;
only the implicit ``os._exit`` — which no test ever asked for — is replaced
with a recorder. Production is untouched: this lives in conftest.
"""
from __future__ import annotations

from typing import Any, List, Tuple

import pytest


@pytest.fixture(autouse=True)
def _never_hard_exit_the_test_runner(monkeypatch: pytest.MonkeyPatch):
    """Neutralise implicit ``os._exit`` in shutdown watchdogs; stop threads."""
    try:
        from backend.core.ouroboros.battle_test import shutdown_watchdog as sw
    except Exception:  # noqa: BLE001 — module absent, nothing to guard
        yield
        return

    cls = getattr(sw, "BoundedShutdownWatchdog", None)
    if cls is None:
        yield
        return

    recorded: List[Tuple[Any, int]] = []
    created: List[Any] = []
    original_init = cls.__init__

    def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        if "exit_fn" not in kwargs:
            # Not a silent no-op: record it, so a test that wants to assert
            # "the watchdog fired" can still read it off the instance rather
            # than discovering it as a dead test session.
            kwargs["exit_fn"] = lambda code: recorded.append((self, code))
        original_init(self, *args, **kwargs)
        created.append(self)
        try:
            self._test_recorded_exits = recorded  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    monkeypatch.setattr(cls, "__init__", _patched_init)
    try:
        yield
    finally:
        # Release the daemon threads. stop() is documented for exactly this:
        # "only needed when a test constructs a watchdog and wants to release
        # the thread before the test ends."
        for wdg in created:
            for method in ("disarm", "stop"):
                try:
                    getattr(wdg, method)()
                except Exception:  # noqa: BLE001
                    pass
