"""A test that hangs is the TEST's defect when the stack says so.

bt-2026-09-22-201845: three of the first ten ops ended
``validation_infra_failure`` with ``[python:FAIL] 8, in select`` -- the tail of
pytest-timeout's stack dump. Their candidate tests blocked on unmocked real
I/O (``jarvis_reload_manager`` launches JARVIS, ``start_system_parallel``
starts processes); ``migrate_acoustic_features`` hung at collection, past the
per-test cap, and was cut at the 180 s wall with its stack thrown away. Filed
``infra``, each op ended with no repair and the goal came back every soak.

These run REAL pytest subprocesses that really hang: a synthetic dump would
not have contained the ``<frozen runpy>`` frames that the owned-frame rule
used to accept as repo code.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.failure_classifier import FailureClassifier
from backend.core.ouroboros.governance.pytest_traceback import (
    _owned,
    hang_site,
)
from backend.core.ouroboros.governance.repair_sandbox import (
    RepairSandbox,
    SandboxValidationResult,
)
# Module alias, not name imports: pytest collects any `Test*` name it finds
# at module level and warns that these two have constructors.
from backend.core.ouroboros.governance import test_runner as TR

WAITER = '''\
import selectors
import socket
import subprocess


def wait_for_ready():
    a, b = socket.socketpair()
    sel = selectors.DefaultSelector()
    sel.register(a, selectors.EVENT_READ)
    return sel.select()


def spawn_worker(tag):
    return subprocess.Popen(["sleep", tag])
'''

LEAK_TAG = f"{3000 + os.getpid() % 900}.{os.getpid()}"

TESTS = f'''\
from backend.waiter import spawn_worker, wait_for_ready


def test_ready():
    assert wait_for_ready()


def test_worker_then_hang():
    spawn_worker("{LEAK_TAG}")
    assert wait_for_ready()
'''

IMPORT_HANG = '''\
from backend.waiter import wait_for_ready

wait_for_ready()


def test_never():
    pass
'''


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\ntimeout_method = thread\n"
    )
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/waiter.py").write_text(WAITER)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_waiter.py").write_text(TESTS)
    (tmp_path / "tests/test_import_hang.py").write_text(IMPORT_HANG)
    yield tmp_path
    subprocess.run(["pkill", "-f", f"sleep {LEAK_TAG}"], check=False)


def _leaked() -> bool:
    time.sleep(0.3)
    out = subprocess.run(["pgrep", "-f", f"sleep {LEAK_TAG}"], capture_output=True, text=True)
    return bool(out.stdout.strip())


# ---------------------------------------------------------------------------
# Phase 1 — attribution, through real TestRunner subprocesses
# ---------------------------------------------------------------------------

def test_a_per_test_hang_in_repo_code_is_the_tests_fault(repo):
    runner = TR.TestRunner(repo_root=repo, timeout=60, per_test_timeout_s=3)
    result = asyncio.run(runner.run((repo / "tests/test_waiter.py",)))
    assert result.timed_out, "the run was still cut off -- durations must not be learned"
    assert result.hang, "a hang in repo code must be attributed"
    assert "FAILED tests/test_waiter.py::test_" in result.hang
    assert "TestHangError: blocked in select()" in result.hang
    assert "via backend/waiter.py:" in result.hang and "sel.select()" in result.hang


def test_a_collection_hang_cut_at_the_wall_keeps_its_stack(repo):
    """pytest-timeout never arms during collection; the wall cap used to throw
    the output away. dump_stacks + faulthandler keep it."""
    runner = TR.TestRunner(repo_root=repo, timeout=6, per_test_timeout_s=60)
    result = asyncio.run(runner.run((repo / "tests/test_import_hang.py",)))
    assert result.timed_out
    assert "most recent call first" in result.stdout, "the stack was not captured"
    assert result.hang.splitlines()[-1].startswith("ERROR tests/test_import_hang.py - TestHangError")


def test_the_adapter_files_an_attributed_hang_as_test_and_a_bare_one_as_infra(monkeypatch, repo):
    PythonAdapter = TR.PythonAdapter

    def _fake(hang: str):
        async def run(self, test_files, sandbox_dir=None):
            return TR.TestResult(
                passed=False, total=0, failed=0, failed_tests=(), duration_seconds=1.0,
                stdout="cut off", flake_suspected=False, timed_out=True, hang=hang,
            )
        return run

    adapter = PythonAdapter(repo_root=repo)
    monkeypatch.setattr(TR.TestRunner, "run", _fake("FAILED t::x - TestHangError: ..."))
    got = asyncio.run(adapter.run((repo / "tests/test_waiter.py",), None, 30.0, "op-x"))
    assert got.failure_class == "test"
    monkeypatch.setattr(TR.TestRunner, "run", _fake(""))
    got = asyncio.run(adapter.run((repo / "tests/test_waiter.py",), None, 30.0, "op-x"))
    assert got.failure_class == "infra"


def test_a_purely_external_stack_stays_infra(tmp_path):
    dump = (
        "+++++++++++++++++++++++++++++++++++ Timeout ++++++++++++++++++++++++++++++++++++\n"
        "~~~~~~~~~~~~~~~~~~~~~~ Stack of MainThread (1403) ~~~~~~~~~~~~~~~~~~~~~~\n"
        '  File "<frozen runpy>", line 198, in _run_module_as_main\n'
        '  File "/venv/lib/python3.11/site-packages/_pytest/main.py", line 371, in _main\n'
        '  File "/usr/lib/python3.11/selectors.py", line 468, in select\n'
    )
    assert hang_site(dump, repo_root=tmp_path) is None


def test_frozen_pseudo_frames_are_never_repo_code(tmp_path):
    assert not _owned("<frozen runpy>", tmp_path)
    assert not _owned("<string>", tmp_path)


def test_an_ordinary_failure_traceback_is_not_a_hang(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_x.py").write_text("def test_a():\n    assert 0\n")
    out = "tests/test_x.py:2: in test_a\n    assert 0\nE   assert 0\n"
    assert hang_site(out, repo_root=tmp_path) is None


def test_the_verdict_is_read_by_the_classifier_unchanged(repo):
    runner = TR.TestRunner(repo_root=repo, timeout=60, per_test_timeout_s=3)
    result = asyncio.run(runner.run((repo / "tests/test_waiter.py",)))
    got = FailureClassifier().classify(SandboxValidationResult(
        passed=False, stdout=result.stdout, stderr="", returncode=1, duration_s=1.0,
    ))
    assert got.failure_class.value == "test"
    assert any(i.startswith("tests/test_waiter.py::test_") for i in got.failing_test_ids)


# ---------------------------------------------------------------------------
# Phase 3 — nothing the hung test started outlives it
# ---------------------------------------------------------------------------

def test_testrunner_leaves_no_child_behind(repo):
    runner = TR.TestRunner(repo_root=repo, timeout=60, per_test_timeout_s=3)
    asyncio.run(runner.run((repo / "tests/test_waiter.py",)))
    assert not _leaked(), "a child of the hung test survived the run"


@pytest.fixture
def git_repo(repo):
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def test_the_repair_sandbox_reaps_its_session_and_attributes_the_hang(git_repo):
    async def _go():
        async with RepairSandbox(git_repo, 3, mirror_working_tree=False) as sb:
            return await sb.run_tests(("tests/test_waiter.py::test_worker_then_hang",), 3)

    svr = asyncio.run(_go())
    assert not svr.passed
    assert svr.hang_site_key.startswith("backend/waiter.py:"), svr.hang_site_key
    assert "TestHangError" in svr.stdout
    assert not _leaked(), "the L2 sandbox leaked the hung test's child"


# ---------------------------------------------------------------------------
# Phase 2 — the repair loop sees the hang and stops repeating it
# ---------------------------------------------------------------------------

_KEY = "backend/waiter.py:10->select"


class _Provider:
    def __init__(self) -> None:
        self.contexts: List[Any] = []

    async def generate(self, ctx, deadline, *, repair_context=None,
                       hypothesis_seed=None, temperature=None):
        self.contexts.append(repair_context)
        n = len(self.contexts)

        class _R:
            candidates = [{"file_path": "tests/test_waiter.py",
                           "full_content": f"def test_ready():\n    assert {n}\n"}]
            model_id = "stub"
            provider_name = "stub"
        return _R()


def _hang_sandbox(key: str):
    class _Sandbox:
        def __init__(self, repo_root, test_timeout_s):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        def sandbox_root(self):
            return Path("/nonexistent-hang-root")

        async def apply_full_content(self, content, file_path):
            return None

        async def apply_patch(self, unified_diff, file_path):
            return None

        async def run_tests(self, test_targets, timeout_s):
            return SandboxValidationResult(
                passed=False, returncode=-1, duration_s=3.0, stderr="timeout",
                stdout=("=========================== short test summary info "
                        "============================\n"
                        "FAILED tests/test_waiter.py::test_ready - TestHangError: "
                        "blocked in select() via backend/waiter.py:10\n"),
                hang_site_key=key,
            )
    return _Sandbox


class _Ctx:
    op_id = "op-hang"
    target_files = ("tests/test_waiter.py",)
    description = "Write tests for backend/waiter.py"

    class generation:  # noqa: N801
        candidates = [{"file_path": "tests/test_waiter.py",
                       "full_content": "def test_ready():\n    assert 0\n"}]


def _loop(monkeypatch, **env):
    from backend.core.ouroboros.governance.repair_engine import RepairBudget, RepairEngine

    for var in ("JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED", "JARVIS_L2_MULTIFILE_ENABLED",
                "JARVIS_FORWARD_PROGRESS_MAX_REPEATS", "JARVIS_FORWARD_PROGRESS_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    provider = _Provider()
    engine = RepairEngine(
        budget=RepairBudget.from_env(), prime_provider=provider,
        repo_root=Path("."), sandbox_factory=_hang_sandbox(_KEY),
    )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=300)
    return provider, asyncio.run(engine._run_inner(_Ctx(), object(), deadline))


def test_the_same_unmocked_call_twice_stops_the_repair_as_futile(monkeypatch):
    provider, result = _loop(monkeypatch)
    assert result.terminal == "L2_STOPPED"
    assert result.stop_reason == f"hang_repeated:{_KEY}"
    assert len(provider.contexts) == 1, "stopped on the first repeat, not after the budget"


def test_under_a_raised_threshold_the_repeat_is_named_in_the_prompt(monkeypatch):
    provider, result = _loop(monkeypatch, JARVIS_FORWARD_PROGRESS_MAX_REPEATS="3")
    assert result.stop_reason == f"hang_repeated:{_KEY}"
    directive = provider.contexts[1].escalation_directive or ""
    assert "REPEATED HANG" in directive and "backend/waiter.py" in directive
    assert "TestHangError" in provider.contexts[0].failure_summary


def test_hang_repeated_is_a_hard_stop_not_a_redispatch():
    from backend.core.ouroboros.governance.orchestrator import l2_stop_is_hard

    assert l2_stop_is_hard(f"hang_repeated:{_KEY}")
