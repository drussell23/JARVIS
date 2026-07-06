"""Interpreter-resolution spine — pytest spawns must NEVER use bare "python3".

Root cause (run a1-brain-20260705-233225, GCP Brain node): TestWatcher's
``run_pytest`` spawned ``argv=["python3", "-m", "pytest", ...]``. Under the
systemd-run unit's minimal PATH, ``python3`` resolved to ``/usr/bin/python3``
(no pytest) -> every run died at bootstrap with the 41-byte
``/usr/bin/python3: No module named pytest`` -> ``parse_pytest_output`` found
zero FAILED lines -> zero signals emitted -> the A1 blast=1 injection vector
NEVER became an op. The organism was blind to test failures the entire
session. Classic passes-local-fails-remote: the Mac's PATH python3 happens to
carry pytest; the node's does not.

Fix: ``resolve_python_bin()`` in the Slice-9 canonical pytest helper —
``JARVIS_PYTHON_BIN`` env override wins (existing precedent:
hybrid_teammate_executor / isolated_agent_worker), else ``sys.executable``
(the running interpreter — structurally guaranteed to carry the organism's
own test deps), bare "python3" only as a last resort for embedded/frozen
interpreters where ``sys.executable`` is empty.

Three A1-critical spawn sites are pinned here:
  * intent/test_watcher.py  (TestFailure sensor detection)
  * test_runner.py          (post-APPLY VERIFY)
  * tool_executor.py        (Venom run_tests tool)
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance import test_subprocess_helper as tsh


# ---------------------------------------------------------------------------
# 1. resolve_python_bin() unit behavior
# ---------------------------------------------------------------------------


class TestResolvePythonBin:
    def test_default_is_sys_executable(self, monkeypatch):
        monkeypatch.delenv("JARVIS_PYTHON_BIN", raising=False)
        assert tsh.resolve_python_bin() == sys.executable

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PYTHON_BIN", "/opt/custom/bin/python3")
        assert tsh.resolve_python_bin() == "/opt/custom/bin/python3"

    def test_blank_env_falls_through_to_sys_executable(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PYTHON_BIN", "   ")
        assert tsh.resolve_python_bin() == sys.executable

    def test_empty_sys_executable_falls_back_to_python3(self, monkeypatch):
        monkeypatch.delenv("JARVIS_PYTHON_BIN", raising=False)
        with mock.patch.object(tsh.sys, "executable", ""):
            assert tsh.resolve_python_bin() == "python3"


# ---------------------------------------------------------------------------
# 2. TestWatcher.run_pytest spawns the RESOLVED interpreter
# ---------------------------------------------------------------------------


def test_test_watcher_run_pytest_uses_resolved_interpreter(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_PYTHON_BIN", raising=False)
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher

    captured: dict = {}

    async def _fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        result = mock.Mock()
        result.timed_out = False
        result.stdout = ""
        result.returncode = 0
        return result

    monkeypatch.setattr(tsh, "run_pytest_subprocess", _fake_run)
    w = TestWatcher(
        repo="jarvis",
        test_dir=str(tmp_path / "tests"),
        repo_path=str(tmp_path),
    )
    asyncio.run(w.run_pytest())
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][0] != "python3"
    assert captured["argv"][1:3] == ["-m", "pytest"]


# ---------------------------------------------------------------------------
# 3. Source-level pin — the bug class may not reappear at the three
#    A1-critical spawn sites. Bare "python3" adjacent to a pytest spawn is
#    the exact 41-byte failure mode; new spawn sites must compose
#    resolve_python_bin().
# ---------------------------------------------------------------------------

_GOV = Path(__file__).resolve().parents[2] / "backend" / "core" / "ouroboros" / "governance"

_CRITICAL_MODULES = (
    _GOV / "intent" / "test_watcher.py",
    _GOV / "test_runner.py",
    _GOV / "tool_executor.py",
)

# argv-list opening with the bare literal: ["python3", "-m", "pytest"
_BARE_SPAWN_RE = re.compile(r"\[\s*\"python3\"\s*,\s*\"-m\"\s*,\s*\"pytest\"")


@pytest.mark.parametrize("module_path", _CRITICAL_MODULES, ids=lambda p: p.name)
def test_no_bare_python3_pytest_spawn(module_path):
    source = module_path.read_text(encoding="utf-8")
    hits = _BARE_SPAWN_RE.findall(source)
    assert not hits, (
        f"{module_path.name} spawns pytest via bare 'python3' — resolves "
        "PATH-dependently (41-byte 'No module named pytest' on minimal-PATH "
        "nodes). Use test_subprocess_helper.resolve_python_bin()."
    )
