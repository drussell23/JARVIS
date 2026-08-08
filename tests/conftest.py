"""
Pytest configuration and shared fixtures for JARVIS AI Agent tests.

This file contains:
- Shared fixtures available to all tests
- Test hooks and configuration
- Common test utilities
"""

import asyncio
import pytest
import sys
import os
from pathlib import Path

# Register additional fixture modules
pytest_plugins = [
    "tests.conftest_gmd_ferrari",
    "tests.ouroboros_pytest_plugin",
]

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))


@pytest.fixture(scope="session")
def project_root_path():
    """Return the project root directory path."""
    return project_root


@pytest.fixture(scope="session")
def backend_path():
    """Return the backend directory path."""
    return project_root / "backend"


@pytest.fixture(scope="session")
def frontend_path():
    """Return the frontend directory path."""
    return project_root / "frontend"


@pytest.fixture(scope="function")
def mock_env_vars(monkeypatch):
    """Fixture to set mock environment variables for testing."""
    test_vars = {
        "ANTHROPIC_API_KEY": "test_api_key_placeholder",
        "JARVIS_ENV": "test",
    }
    for key, value in test_vars.items():
        monkeypatch.setenv(key, value)
    return test_vars


@pytest.fixture(scope="function")
def temp_test_dir(tmp_path):
    """Provide a temporary directory for test file operations."""
    return tmp_path


def pytest_configure(config):
    """Configure pytest with custom settings."""
    # Register custom markers
    config.addinivalue_line(
        "markers", "unit: mark test as a unit test"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )
    config.addinivalue_line(
        "markers", "functional: mark test as a functional test"
    )
    config.addinivalue_line(
        "markers", "performance: mark test as a performance test"
    )
    config.addinivalue_line(
        "markers", "e2e: mark test as an end-to-end test"
    )
    config.addinivalue_line(
        "markers", "vision: mark test as vision system related"
    )
    config.addinivalue_line(
        "markers", "voice: mark test as voice system related"
    )
    config.addinivalue_line(
        "markers", "backend: mark test as backend related"
    )
    config.addinivalue_line(
        "markers", "frontend: mark test as frontend related"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers", "api: mark test as requiring API keys"
    )
    config.addinivalue_line(
        "markers", "permissions: mark test as requiring system permissions"
    )


def pytest_collection_modifyitems(config, items):
    """Modify test items during collection."""
    # Add markers based on test location
    for item in items:
        # Add markers based on path
        test_path = str(item.fspath)

        if "/unit/" in test_path:
            item.add_marker(pytest.mark.unit)
        if "/integration/" in test_path:
            item.add_marker(pytest.mark.integration)
        if "/functional/" in test_path:
            item.add_marker(pytest.mark.functional)
        if "/performance/" in test_path:
            item.add_marker(pytest.mark.performance)
            item.add_marker(pytest.mark.slow)
        if "/e2e/" in test_path:
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.slow)

        # Component markers
        if "/vision/" in test_path:
            item.add_marker(pytest.mark.vision)
        if "/voice/" in test_path:
            item.add_marker(pytest.mark.voice)
        if "/backend/" in test_path:
            item.add_marker(pytest.mark.backend)


def pytest_report_header(config):
    """Add custom header to pytest report."""
    return [
        "JARVIS AI Agent Test Suite",
        f"Project Root: {project_root}",
    ]


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Say out loud when the terminal-dependent suite did not run.

    Thirty-one tests — the whole proof that the cockpit answers a keystroke —
    skip when ``pty.openpty()`` raises, which it does in every sandboxed or
    containerised runner. The run then reports green, and the cockpit's
    interactive behaviour gets described as "never verified" while a suite
    proving it sits in the repository passing in 80 seconds on any machine
    with a terminal.

    A skip is an honest answer. A skip nobody can see is not, and this repo has
    already paid for that once: a suite that cannot collect reports no
    failures. So the summary states the count, the consequence, and the way to
    make it fatal.
    """
    try:
        from tests.pty_gate import REQUIRED_ENV_VAR, SKIPPED
    except Exception:  # noqa: BLE001 — a reporting aid must never break a run
        return
    if not SKIPPED:
        return

    write = terminalreporter.write_line
    write("")
    write(f"⚠️  {len(SKIPPED)} terminal-dependent test(s) DID NOT RUN — "
          f"no pseudo-terminal was available.", yellow=True, bold=True)
    write("   The cockpit's interactive behaviour is UNPROVEN by this run: "
          "keybindings, mouse", yellow=True)
    write("   negotiation, alt-screen handling and the slash palette were all "
          "skipped, not passed.", yellow=True)
    write(f"   Make this fatal where a terminal is expected:  "
          f"{REQUIRED_ENV_VAR}=1", yellow=True)
    reasons = {reason for _, reason in SKIPPED}
    for reason in sorted(reasons):
        write(f"   cause: {reason}", yellow=True)
    write("")


@pytest.fixture(autouse=True)
def _isolate_fsm_checkpoint_dir(tmp_path, monkeypatch):
    """Point the FSM suspend/resume checkpoint ledger at a per-test tmp dir.

    The REAL ``.ouroboros/checkpoints`` (the CWD-relative default) can hold
    live PENDING checkpoints between soak windows; any test that starts an
    intake router would otherwise hydrate + CONSUME them (``mark_resumed``
    deletes the file) and re-inject heavy resume ops into the test's router --
    the exact contamination that ate window-2's pending checkpoints and hung
    the intake suite on 2026-07-01. Tests that need a specific dir still
    override via their own ``monkeypatch.setenv`` (applied after this
    autouse fixture).
    """
    monkeypatch.setenv(
        "JARVIS_CHECKPOINT_DIR", str(tmp_path / "_fsm_checkpoints"),
    )
    # Same isolation for the cross-run latency-physics ledger (Amnesia Cure):
    # tests must never read/poison the repo's real measured physics.
    monkeypatch.setenv(
        "JARVIS_LATENCY_LEDGER_PATH", str(tmp_path / "_latency_physics.json"),
    )


@pytest.fixture(autouse=True)
async def _cancel_pending_async_tasks():
    """
    Cancel any tasks left pending after each async test completes.

    Prevents tests that spawn background asyncio tasks (audio capture,
    socket listeners, etc.) from hanging the process indefinitely.
    This is the structural fix for zombie pytest processes.
    """
    yield
    loop = asyncio.get_event_loop()
    tasks = [t for t in asyncio.all_tasks(loop) if not t.done() and t is not asyncio.current_task()]
    if tasks:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _isolate_durable_sensor_state(tmp_path, monkeypatch):
    """Keep test runs out of the operator's real ``.jarvis/`` state.

    `CUExecutionSensor` gained an append-only journal so failure evidence
    survives a crash during governance boot. That made the sensor stateful
    ACROSS PROCESS RUNS — which is the point, and which also means a test run
    starting from the repo root would read and write the operator's live
    journal.

    It is not a hypothetical: the first full run after the journal landed wrote
    29KB of synthetic failures into `.jarvis/cu_failure_journal.jsonl`, and the
    emission cooldowns it recorded then suppressed three unrelated tests in the
    same session. Two distinct harms — polluting real state, and one test
    silently deciding another's outcome.

    Fixed HERE rather than in the sensor: a production module that behaves
    differently under pytest is the kind of shortcut that makes green
    meaningless. Test isolation is the test layer's job.

    PER TEST, not per session. A session-scoped journal was tried first and was
    not enough: the spine tests all use one failure signature, so the emission
    cooldown written by the first still silenced the rest. Durable state has to
    be reset on the same cadence as the singleton it belongs to.

    A test that sets ``JARVIS_CU_JOURNAL_PATH`` itself still wins — `monkeypatch`
    applies in fixture order, and module-level fixtures run after this one — so
    the durability suite can point at its own file and exercise real restarts.
    """
    monkeypatch.setenv(
        "JARVIS_CU_JOURNAL_PATH",
        str(tmp_path / "cu_journal" / "cu_failure_journal.jsonl"),
    )
    # The voice-intent WAL is the same hazard with a different name: it is
    # durable, it lives under `.ouroboros/intents/`, and `unfinished()` is a
    # replay queue — so a test run against the real path could hand the
    # operator's next boot a queue of synthetic commands to resume.
    monkeypatch.setenv(
        "JARVIS_INTENT_JOURNAL_PATH",
        str(tmp_path / "intents" / "journal.jsonl"),
    )
    yield
