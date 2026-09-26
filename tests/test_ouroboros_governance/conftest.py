"""Shared fixtures for Ouroboros governance tests."""

import asyncio

import pytest
from pathlib import Path


class _InertOracle:
    """A deterministic stand-in for TheOracle in GovernedLoopService tests.

    ``GovernedLoopConfig.oracle_enabled`` defaults to True, so every unit test
    that called ``start()`` launched a REAL cold index of this repository —
    thousands of files — and its persistence layer opened an aiosqlite
    connection: a non-daemon thread that loops until closed. Seven tests never
    stopped their service (and every failing test skips its own ``stop()``),
    so the index was abandoned mid-run and the pytest process could not exit
    after its last test: the "10 minute hang" (measured 2026-09-26). Tests of
    the indexer itself patch ``TheOracle`` inside the test, which wins.
    """

    def __init__(self, *_a, **_k) -> None:
        pass

    async def initialize(self) -> None:
        return None

    async def get_metrics(self) -> dict:
        return {"total_nodes": 0}

    async def incremental_update(self, *_a, **_k) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def release_resources(self) -> None:
        return None


#: Whole-repository diagnostics ``start()`` launches in the background, each by
#: its own master flag. Both have their own test suites; a lifecycle test of
#: the loop service exercises neither, and each one's cost landed on the test.
_BOOT_DIAGNOSTICS = {
    # The audit-ratchet boot sweep parses the whole repository
    # (``source_assertion_audit.audit``) on the loop's DEFAULT executor.
    # Closing a test's loop joins that executor: ~23 s of teardown per test
    # (faulthandler dump mid-teardown, 2026-09-26).
    "JARVIS_AUDIT_WATCHDOGS_ENABLED": "false",
    # The invariant-drift observer validates every shipped invariant in the
    # shared PROCESS pool. Python's exit hook waits for in-flight pool work:
    # the process outlived its last test by ~27 s (measured the same day).
    "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED": "false",
}


@pytest.fixture(autouse=True)
def _no_boot_diagnostics(monkeypatch):
    for name, value in _BOOT_DIAGNOSTICS.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def _inert_oracle(monkeypatch):
    from backend.core.ouroboros.governance import governed_loop_service as gls
    monkeypatch.setattr(gls, "TheOracle", _InertOracle)


@pytest.fixture(autouse=True)
def _stop_every_started_service(monkeypatch):
    """Stop, at teardown, every GovernedLoopService a test started — passed or
    failed. A test that fails before its own ``stop()`` otherwise leaves the
    service's background tasks suspended forever in its loop.

    Each service is stopped on the loop it was started on: stopping it on a
    different loop is exactly the cross-loop hazard this repo has hit before.
    A loop that is already closed has no tasks left to stop.
    """
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopService, ServiceState,
    )
    started: list = []
    real_start = GovernedLoopService.start

    async def _tracking_start(self, *a, **k):
        started.append((self, asyncio.get_running_loop()))
        return await real_start(self, *a, **k)

    monkeypatch.setattr(GovernedLoopService, "start", _tracking_start)
    yield
    for svc, loop in started:
        if svc.state is ServiceState.INACTIVE or loop.is_closed():
            continue
        if loop.is_running():
            # Still inside the test's own loop (an async fixture ordering):
            # schedule it there; the loop's teardown awaits pending tasks.
            loop.create_task(svc.stop())
            continue
        loop.run_until_complete(svc.stop())


@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project structure for testing."""
    src = tmp_path / "backend" / "core"
    src.mkdir(parents=True)
    (src / "__init__.py").touch()
    test_file = src / "example.py"
    test_file.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def tmp_ledger_dir(tmp_path):
    """Temporary directory for operation ledger."""
    d = tmp_path / "ledger"
    d.mkdir()
    return d
