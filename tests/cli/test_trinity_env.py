"""trinity bootstrap-env — hermetic dependency seal spine.

Proves the native-venv construction + atomic swap without building a real
300-package environment (injected creator/runner seams). Mandate 2: a
failed pip install must leave the existing runtime completely intact.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.ouroboros.cli import trinity_env as env


def _fake_creator(path: Path) -> None:
    """Stand-in for venv.create — makes a bin/python so venv_python()
    resolves, without a real (slow) venv build."""
    (path / "bin").mkdir(parents=True, exist_ok=True)
    (path / "bin" / "python").write_text("#!/bin/sh\n")
    (path / "bin" / "python").chmod(0o755)


class _R:
    def __init__(self, rc=0, err=""): self.returncode = rc; self.stderr = err


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / ".jarvis" / "venv"))
    monkeypatch.setenv("JARVIS_REQUIREMENTS", str(tmp_path / "requirements.txt"))
    (tmp_path / "requirements.txt").write_text("fastapi==1.0\n")
    yield


def test_fresh_bootstrap_creates_hermetic_venv():
    report = env.bootstrap_env(
        creator=_fake_creator, runner=lambda *a, **k: _R(0))
    assert report.ok is True
    assert report.created_fresh is True
    assert report.swapped is False
    assert env.venv_exists()                       # interpreter present
    assert report.python == env.venv_python()


def test_pip_install_targets_the_isolated_interpreter():
    seen = []
    def _runner(argv, **kw):
        seen.append(argv)
        return _R(0)
    env.bootstrap_env(creator=_fake_creator, runner=_runner)
    # The pip install must run FROM the temp venv's own python, not global.
    pip_calls = [c for c in seen if "pip" in c and "install" in c]
    assert pip_calls
    req_call = [c for c in pip_calls if "-r" in c][0]
    assert req_call[0].endswith("/bin/python")     # isolated interpreter
    assert ".tmp" in req_call[0]                    # built in the temp dir


def test_failed_pip_leaves_existing_venv_untouched(tmp_path):
    # Pre-existing GOOD venv with a sentinel file.
    good = env.venv_dir()
    (good / "bin").mkdir(parents=True)
    (good / "bin" / "python").write_text("GOOD")
    (good / "sentinel").write_text("keep-me")

    # pip fails on the rebuild.
    report = env.bootstrap_env(
        creator=_fake_creator, runner=lambda *a, **k: _R(1, "resolution error"))

    assert report.ok is False
    assert "pip install failed" in report.reason
    # Mandate 2: the live venv is completely intact.
    assert (good / "sentinel").read_text() == "keep-me"
    assert (good / "bin" / "python").read_text() == "GOOD"
    # No staging left behind.
    assert not env.venv_tmp_dir().exists()


def test_atomic_swap_replaces_existing_on_success(tmp_path):
    good = env.venv_dir()
    (good / "bin").mkdir(parents=True)
    (good / "bin" / "python").write_text("OLD")
    (good / "old_marker").write_text("x")

    report = env.bootstrap_env(
        creator=_fake_creator, runner=lambda *a, **k: _R(0))

    assert report.ok is True
    assert report.swapped is True                  # replaced, not fresh
    assert env.venv_exists()
    # The OLD tree is gone (swapped out), the new one is in place.
    assert not (good / "old_marker").exists()
    assert (good / "bin" / "python").read_text() != "OLD"
    # No backup/temp residue.
    assert not env.venv_tmp_dir().exists()
    assert not env.venv_backup_dir().exists()


def test_missing_requirements_aborts_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_REQUIREMENTS", str(tmp_path / "nope.txt"))
    report = env.bootstrap_env(creator=_fake_creator, runner=lambda *a, **k: _R(0))
    assert report.ok is False
    assert "requirements not found" in report.reason


def test_venv_exists_false_when_absent():
    assert env.venv_exists() is False              # nothing built yet


def test_uses_native_venv_module_not_bash():
    """Mandate 1: no shell/Docker/conda — the default creator uses the
    stdlib venv module."""
    import inspect
    import re
    src = inspect.getsource(env._default_creator)
    assert "venv.create" in src                    # native module
    full = Path(env.__file__).read_text()
    # No shell/Docker/conda INVOCATIONS — an argv list literal starting
    # with the binary (prose mentioning the ban is fine).
    assert not re.search(r"""\[\s*['"](docker|conda|bash|sh)['"]""", full)
    assert "os.system" not in full
    assert "import docker" not in full
