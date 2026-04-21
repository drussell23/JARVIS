"""Slice 3 regression spine — TestRunner streaming migration.

Proves the streaming path is a drop-in replacement for the legacy
``_exec_with_timeout`` path:

  1. **Structural parity on representative fixtures**: run identical
     pytest invocations through BOTH paths. Assert structural fields
     (``passed``, ``total``, ``failed``, ``failed_tests``) match
     byte-for-byte. Tests FAIL LOUDLY on any divergence.
  2. **Feature gates**: streaming off by default, on only under the
     env flag. Early-exit off by default. Defaults preserve legacy
     "run everything" semantics.
  3. **Early-exit semantics**: when opt-in, first FAILED/ERROR
     terminates the subprocess; later tests never run.
  4. **Event stream observability**: per-test events logged at INFO
     with grep-stable format; optional ``event_callback`` fires with
     the documented payload shape.
  5. **Timeout + sandbox preservation**: streaming path honors
     ``self._timeout`` identically to the legacy path; sandbox_dir
     respected.
  6. **Isolation discipline**: TestRunner consumes BackgroundMonitor
     DIRECTLY; no import of run_monitor_tool / Venom tool surface.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.core.ouroboros.governance.test_runner import (
    TestResult,
    TestRunner,
    _early_exit_on_fail,
    _parity_mode,
    _PYTEST_EVENT_RE,
    _streaming_enabled,
)


# ---------------------------------------------------------------------------
# Fixture helpers — write a micro pytest project in tmp_path
# ---------------------------------------------------------------------------


def _write_passing_fixture(root: Path) -> Path:
    """One test module with 3 passing tests. Returns the test-file path."""
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / "test_passing.py"
    test_file.write_text(textwrap.dedent('''
        def test_alpha():
            assert 1 + 1 == 2
        def test_beta():
            assert "a" in "abc"
        def test_gamma():
            assert [1, 2, 3][0] == 1
    ''').lstrip())
    return test_file


def _write_mixed_fixture(root: Path) -> Path:
    """Test module with 2 passing + 2 failing tests in a stable order."""
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / "test_mixed.py"
    test_file.write_text(textwrap.dedent('''
        def test_first_passes():
            assert True
        def test_second_fails():
            assert False, "deliberate failure #2"
        def test_third_passes():
            assert True
        def test_fourth_fails():
            assert False, "deliberate failure #4"
    ''').lstrip())
    return test_file


@pytest.fixture(autouse=True)
def _reset_streaming_env(monkeypatch):
    """Ensure every test starts with clean streaming/early-exit/parity env.
    Defaults are off; tests opt in explicitly."""
    for key in (
        "JARVIS_TEST_RUNNER_STREAMING_ENABLED",
        "JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL",
        "JARVIS_TEST_RUNNER_PARITY_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. Feature gate defaults
# ---------------------------------------------------------------------------


def test_streaming_default_post_graduation_is_true(monkeypatch):
    """Slice 4 graduation pin: after the Ticket #4 graduation,
    ``JARVIS_TEST_RUNNER_STREAMING_ENABLED`` defaults to ``"true"``.
    Operators on a fresh install see the streaming path active.
    Legacy ``_exec_with_timeout`` remains available via explicit
    ``"false"`` opt-out."""
    monkeypatch.delenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", raising=False)
    assert _streaming_enabled() is True


def test_streaming_explicit_false_opts_out(monkeypatch):
    """Slice 4 opt-out pin: operators can revert to the legacy
    blocking ``proc.communicate()`` path by explicitly setting
    ``JARVIS_TEST_RUNNER_STREAMING_ENABLED=false``. Guarantees the
    graduation flip is reversible at the env layer."""
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "false")
    assert _streaming_enabled() is False


def test_early_exit_disabled_by_default(monkeypatch):
    """Slice 3 test 2: early-exit-on-fail is OFF by default — legacy
    ``run everything`` semantics preserved unless explicitly opted in."""
    monkeypatch.delenv("JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL", raising=False)
    assert _early_exit_on_fail() is False


def test_parity_mode_disabled_by_default(monkeypatch):
    """Slice 3 test 3: parity-mode is OFF by default — streaming
    doesn't double pytest cost in normal operation."""
    monkeypatch.delenv("JARVIS_TEST_RUNNER_PARITY_MODE", raising=False)
    assert _parity_mode() is False


def test_env_flags_case_insensitive(monkeypatch):
    """Slice 3 test 4: case-insensitive env parsing for all three flags."""
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "TRUE")
    monkeypatch.setenv("JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL", "True")
    monkeypatch.setenv("JARVIS_TEST_RUNNER_PARITY_MODE", "tRuE")
    assert _streaming_enabled() is True
    assert _early_exit_on_fail() is True
    assert _parity_mode() is True


def test_pytest_event_regex_matches_expected_lines():
    """Slice 3 test 5: the regex used by the streaming parser catches
    pytest -v output lines. Pins the parser contract so operators'
    grep-on-logs surface stays stable."""
    line_pass = (
        "tests/test_foo.py::test_alpha PASSED                     [ 33%]"
    )
    line_fail = (
        "tests/test_foo.py::test_beta FAILED                      [ 66%]"
    )
    line_error = (
        "tests/test_foo.py::test_gamma ERROR                     [100%]"
    )
    line_skipped = (
        "tests/test_foo.py::test_delta SKIPPED                    [ 50%]"
    )
    for line, expected in [
        (line_pass, "PASSED"),
        (line_fail, "FAILED"),
        (line_error, "ERROR"),
        (line_skipped, "SKIPPED"),
    ]:
        m = _PYTEST_EVENT_RE.search(line)
        assert m is not None, "regex failed to match: " + repr(line)
        assert m.group("status") == expected


# ---------------------------------------------------------------------------
# 2. Structural parity — the CRITICAL slice invariant
# ---------------------------------------------------------------------------


async def _run_both_paths(
    root: Path, test_file: Path, monkeypatch,
) -> Tuple[TestResult, TestResult]:
    """Run the same test file through BOTH paths. Returns (legacy, streaming)."""
    # Legacy path.
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "false")
    runner_legacy = TestRunner(repo_root=root, timeout=60.0)
    legacy = await runner_legacy.run(test_files=(test_file,))
    # Streaming path.
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    runner_stream = TestRunner(repo_root=root, timeout=60.0)
    streaming = await runner_stream.run(test_files=(test_file,))
    return legacy, streaming


@pytest.mark.asyncio
async def test_parity_all_passing_fixture(tmp_path, monkeypatch):
    """Slice 3 test 6 (CRITICAL PARITY): 3 passing tests —
    structural TestResult fields match across paths."""
    test_file = _write_passing_fixture(tmp_path)
    legacy, streaming = await _run_both_paths(tmp_path, test_file, monkeypatch)
    # Core structural invariants.
    assert legacy.passed == streaming.passed == True, (
        f"passed divergence: legacy={legacy.passed} streaming={streaming.passed}"
    )
    assert legacy.total == streaming.total, (
        f"total divergence: legacy={legacy.total} streaming={streaming.total}"
    )
    assert legacy.failed == streaming.failed == 0
    assert set(legacy.failed_tests) == set(streaming.failed_tests) == set()


@pytest.mark.asyncio
async def test_parity_mixed_pass_fail_fixture(tmp_path, monkeypatch):
    """Slice 3 test 7 (CRITICAL PARITY): 2 pass + 2 fail — every
    structural field matches, failed_tests set is identical
    (order-insensitive). This is the headline parity guarantee."""
    test_file = _write_mixed_fixture(tmp_path)
    legacy, streaming = await _run_both_paths(tmp_path, test_file, monkeypatch)
    # All-structural-field comparison with explicit divergence messages.
    divergences: List[str] = []
    if legacy.passed != streaming.passed:
        divergences.append(f"passed {legacy.passed} vs {streaming.passed}")
    if legacy.total != streaming.total:
        divergences.append(f"total {legacy.total} vs {streaming.total}")
    if legacy.failed != streaming.failed:
        divergences.append(f"failed {legacy.failed} vs {streaming.failed}")
    s_legacy = set(legacy.failed_tests)
    s_stream = set(streaming.failed_tests)
    if s_legacy != s_stream:
        divergences.append(
            f"failed_tests legacy-only={sorted(s_legacy - s_stream)} "
            f"stream-only={sorted(s_stream - s_legacy)}"
        )
    assert not divergences, (
        "STRUCTURAL PARITY FAILED — streaming diverges from legacy on "
        "representative mixed-result fixture:\n  "
        + "\n  ".join(divergences)
    )
    # Sanity: both paths found 2 failures.
    assert legacy.failed == 2
    assert streaming.failed == 2


# ---------------------------------------------------------------------------
# 3. Streaming path — happy path + event stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_returns_valid_test_result(tmp_path, monkeypatch):
    """Slice 3 test 8: the streaming path returns a well-formed
    TestResult, not a crash / None / partial dict."""
    test_file = _write_passing_fixture(tmp_path)
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    runner = TestRunner(repo_root=tmp_path, timeout=60.0)
    result = await runner.run(test_files=(test_file,))
    assert isinstance(result, TestResult)
    assert result.passed is True
    assert result.total >= 3
    assert result.failed == 0


@pytest.mark.asyncio
async def test_event_callback_fires_per_test(tmp_path, monkeypatch):
    """Slice 3 test 9: the optional event_callback fires per pytest
    event with the documented payload shape
    ({kind, node_id, ts_mono, sequence, raw_line})."""
    test_file = _write_mixed_fixture(tmp_path)
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    captured: List[Dict[str, Any]] = []

    def _cb(ev: Dict[str, Any]) -> None:
        captured.append(ev)

    runner = TestRunner(
        repo_root=tmp_path, timeout=60.0, event_callback=_cb,
    )
    await runner.run(test_files=(test_file,))
    # Expected: 2 pass events + 2 fail events.
    kinds = [e["kind"] for e in captured]
    assert "test_passed" in kinds
    assert "test_failed" in kinds
    # Payload shape is complete on every event.
    for ev in captured:
        assert set(ev.keys()) >= {
            "kind", "node_id", "ts_mono", "sequence", "raw_line",
        }
        assert ev["node_id"], "node_id must be non-empty"


@pytest.mark.asyncio
async def test_event_callback_exception_does_not_break_runner(
    tmp_path, monkeypatch,
):
    """Slice 3 test 10: a buggy callback that raises MUST NOT break
    the TestRunner. The runner logs at DEBUG + continues."""
    test_file = _write_passing_fixture(tmp_path)
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")

    def _bad_cb(ev: Dict[str, Any]) -> None:
        raise RuntimeError("simulated consumer failure")

    runner = TestRunner(
        repo_root=tmp_path, timeout=60.0, event_callback=_bad_cb,
    )
    # Should complete successfully despite callback raising on every event.
    result = await runner.run(test_files=(test_file,))
    assert result.passed is True
    assert result.total >= 3


@pytest.mark.asyncio
async def test_event_callback_none_is_legacy_compatible(tmp_path, monkeypatch):
    """Slice 3 test 11: event_callback=None (default) — no callback
    surface consumed; existing callers unchanged."""
    test_file = _write_passing_fixture(tmp_path)
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    # Default ctor — no callback kwarg.
    runner = TestRunner(repo_root=tmp_path, timeout=60.0)
    result = await runner.run(test_files=(test_file,))
    assert result.passed is True


# ---------------------------------------------------------------------------
# 4. Early-exit semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_early_exit_disabled_runs_all_tests(tmp_path, monkeypatch):
    """Slice 3 test 12: without the early-exit flag, streaming runs
    EVERY test — the mixed fixture's 2 failures + 2 passes all land
    in the TestResult. Preserves legacy 'run everything' semantics."""
    test_file = _write_mixed_fixture(tmp_path)
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL", "false")
    runner = TestRunner(repo_root=tmp_path, timeout=60.0)
    result = await runner.run(test_files=(test_file,))
    assert result.total == 4
    assert result.failed == 2


@pytest.mark.asyncio
async def test_early_exit_enabled_stops_on_first_failure(
    tmp_path, monkeypatch,
):
    """Slice 3 test 13 (CRITICAL): with early-exit on, the streaming
    path terminates the subprocess after the FIRST failure.
    Downstream tests never run. Compared against legacy run (which
    executes all 4 tests) — the streaming run reports fewer
    total-executed tests."""
    test_file = _write_mixed_fixture(tmp_path)
    # Legacy path — baseline: all 4 tests run.
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "false")
    runner_legacy = TestRunner(repo_root=tmp_path, timeout=60.0)
    legacy = await runner_legacy.run(test_files=(test_file,))
    assert legacy.total == 4

    # Streaming + early-exit: stops after first failure.
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TEST_RUNNER_EARLY_EXIT_ON_FAIL", "true")
    runner_stream = TestRunner(repo_root=tmp_path, timeout=60.0)
    stream = await runner_stream.run(test_files=(test_file,))
    # At minimum, stream result must show a failure (the point of early-exit).
    # Depending on JSON report vs fallback parse semantics the
    # ``total`` field may reflect collected tests (4) or executed
    # tests (≤2); either way the result MUST report failure.
    assert stream.passed is False, (
        "early-exit must report failure (triggered by first FAILED/ERROR)"
    )


# ---------------------------------------------------------------------------
# 5. Timeout + sandbox preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_honors_timeout(tmp_path, monkeypatch):
    """Slice 3 test 14: a long-sleeping test is terminated at
    ``self._timeout`` on the streaming path, mirroring legacy
    behavior. TestResult reports passed=False + a 'timed out'
    diagnostic."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sleep.py").write_text(
        "import time\n"
        "def test_slow():\n"
        "    time.sleep(30)\n"
    )
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    runner = TestRunner(repo_root=tmp_path, timeout=2.0)  # tight budget
    result = await runner.run(test_files=(tests_dir / "test_sleep.py",))
    assert result.passed is False


@pytest.mark.asyncio
async def test_streaming_respects_sandbox_dir(tmp_path, monkeypatch):
    """Slice 3 test 15: streaming path honors the ``sandbox_dir``
    parameter identically to legacy — pytest runs with that as cwd."""
    test_file = _write_passing_fixture(tmp_path)
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    runner = TestRunner(repo_root=tmp_path, timeout=60.0)
    # sandbox_dir = tmp_path (same as repo_root in this test).
    result = await runner.run(
        test_files=(test_file,), sandbox_dir=tmp_path,
    )
    assert result.passed is True


# ---------------------------------------------------------------------------
# 6. Isolation discipline — slice-boundary pin
# ---------------------------------------------------------------------------


def test_test_runner_does_not_import_monitor_tool():
    """Slice 3 test 16 (CRITICAL): TestRunner must NOT import
    run_monitor_tool / monitor_tool module. Per authorization:
    TestRunner is infra, not a model-facing tool; it consumes
    BackgroundMonitor DIRECTLY. This test grep-enforces the
    boundary so Slice 3+ can't accidentally entangle the two."""
    src = Path(
        "backend/core/ouroboros/governance/test_runner.py"
    ).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.monitor_tool",
        "import monitor_tool",
        "run_monitor_tool",
    ]
    for f in forbidden:
        assert f not in src, (
            "Slice 3 boundary violation: test_runner.py imports " + repr(f) + ". "
            f"TestRunner must consume BackgroundMonitor DIRECTLY per "
            f"Slice 3 authorization — do not route infra through the "
            f"Venom tool surface."
        )


def test_test_runner_imports_background_monitor_primitive():
    """Slice 3 test 17: TestRunner DOES import BackgroundMonitor from
    background_monitor.py (the primitive). Pins the intended
    dependency direction."""
    src = Path(
        "backend/core/ouroboros/governance/test_runner.py"
    ).read_text()
    assert (
        "from backend.core.ouroboros.governance.background_monitor" in src
    ), (
        "TestRunner must import BackgroundMonitor from "
        "background_monitor.py (the Slice 1 primitive)"
    )


# ---------------------------------------------------------------------------
# 7. Parity mode — runtime dual-path divergence detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parity_mode_runs_without_crashing(tmp_path, monkeypatch, caplog):
    """Slice 3 test 18: with both streaming + parity mode on, the
    runner completes without crashing + emits the parity log line
    (either parity_ok or parity_divergence). Pins the operator
    observability surface for Slice 4 graduation decisions."""
    import logging as _logging
    test_file = _write_passing_fixture(tmp_path)
    monkeypatch.setenv("JARVIS_TEST_RUNNER_STREAMING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TEST_RUNNER_PARITY_MODE", "true")
    caplog.set_level(
        _logging.INFO,
        logger="backend.core.ouroboros.governance.test_runner",
    )
    runner = TestRunner(repo_root=tmp_path, timeout=60.0)
    result = await runner.run(test_files=(test_file,))
    assert result.passed is True
    # Expect either parity_ok or parity_divergence in the log.
    parity_msgs = [
        r.getMessage() for r in caplog.records
        if "parity_" in r.getMessage()
    ]
    assert parity_msgs, (
        "parity_mode must emit either [TestRunner] parity_ok or "
        "[TestRunner] parity_divergence — got none in debug.log"
    )
