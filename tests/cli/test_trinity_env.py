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
    assert "not found" in report.reason              # missing req file → clean abort


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


# ---------------------------------------------------------------------------
# PHASE 6 — Configuration-Aware Dependency Sharding
# ---------------------------------------------------------------------------

def _make_shards(tmp_path):
    """Author fake shard files in an isolated dir."""
    d = tmp_path / "deploy" / "requirements"
    d.mkdir(parents=True)
    (d / "requirements-core.txt").write_text("fastapi==1.0\nnumpy==2.0\n")
    (d / "requirements-voice.txt").write_text("torch==2.12.0\nspeechbrain==1.0\n")
    (d / "requirements-vision.txt").write_text("opencv-python==4.10\n")
    return d


@pytest.fixture
def _shards(tmp_path, monkeypatch):
    d = _make_shards(tmp_path)
    monkeypatch.setenv("JARVIS_REQUIREMENTS_DIR", str(d))
    monkeypatch.delenv("JARVIS_REQUIREMENTS", raising=False)  # not single-file
    # Clear ALL toggle aliases so tests set state explicitly.
    for k in ("JARVIS_VOICE_ENABLED", "JARVIS_AUDIO_BUS_ENABLED",
              "JARVIS_VISION_ENABLED", "JARVIS_VISION_LOOP_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    return d


def test_both_toggles_false_excludes_ml_shards(_shards, monkeypatch):
    """MANDATE 4: voice=false + vision=false → the pip command installs
    ONLY core; torch/speechbrain/opencv are strictly excluded."""
    monkeypatch.setenv("JARVIS_VOICE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_VISION_ENABLED", "false")

    files = env.resolve_requirement_files()
    names = [f.name for f in files]
    assert names == ["requirements-core.txt"]            # core ONLY
    assert "requirements-voice.txt" not in names
    assert "requirements-vision.txt" not in names

    # The actual pip argv carries only core — no ML file.
    args = env.build_pip_requirement_args(files)
    joined = " ".join(args)
    assert "requirements-core.txt" in joined
    assert "voice" not in joined and "vision" not in joined


def test_voice_toggle_appends_voice_shard(_shards, monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_VISION_ENABLED", "false")
    names = [f.name for f in env.resolve_requirement_files()]
    assert names == ["requirements-core.txt", "requirements-voice.txt"]
    assert "requirements-vision.txt" not in names


def test_vision_toggle_appends_vision_shard(_shards, monkeypatch):
    monkeypatch.setenv("JARVIS_VISION_ENABLED", "true")
    names = [f.name for f in env.resolve_requirement_files()]
    assert "requirements-vision.txt" in names
    assert "requirements-voice.txt" not in names


def test_audio_bus_alias_activates_voice(_shards, monkeypatch):
    """The REAL .env flag is JARVIS_AUDIO_BUS_ENABLED — it must also
    activate the voice shard (alias coverage)."""
    monkeypatch.setenv("JARVIS_AUDIO_BUS_ENABLED", "true")
    names = [f.name for f in env.resolve_requirement_files()]
    assert "requirements-voice.txt" in names


def test_vision_loop_alias_activates_vision(_shards, monkeypatch):
    monkeypatch.setenv("JARVIS_VISION_LOOP_ENABLED", "true")
    names = [f.name for f in env.resolve_requirement_files()]
    assert "requirements-vision.txt" in names


def test_core_always_present(_shards, monkeypatch):
    # Even with everything off, core is unconditional.
    active = [s.name for s in env.resolve_active_shards()]
    assert "core" in active


def test_bootstrap_uses_only_active_shards_in_pip(_shards, monkeypatch):
    """End-to-end: voice off → the pip subprocess NEVER sees torch's file."""
    monkeypatch.setenv("JARVIS_VOICE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_VISION_ENABLED", "false")
    monkeypatch.setenv("JARVIS_VENV_DIR", str(_shards.parent.parent / ".venv"))
    seen = []

    def _creator(path):
        (path / "bin").mkdir(parents=True, exist_ok=True)
        (path / "bin" / "python").write_text("#!/bin/sh\n")
        (path / "bin" / "python").chmod(0o755)

    def _runner(argv, **kw):
        seen.append(argv)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    rep = env.bootstrap_env(creator=_creator, runner=_runner)
    assert rep.ok is True
    pip_installs = [c for c in seen if "install" in c and "-r" in c]
    assert pip_installs
    argv = pip_installs[0]
    flat = " ".join(argv)
    assert "requirements-core.txt" in flat
    assert "requirements-voice.txt" not in flat      # torch NOT installed
    assert "requirements-vision.txt" not in flat


def test_shard_reuses_doctor_env_true(_shards, monkeypatch):
    """DRY (mandate 3): the shard gate must use the doctor's _env_true —
    same truthy vocabulary (on/yes/1/true)."""
    from backend.core.ouroboros.cli.trinity_doctor import _env_true
    monkeypatch.setenv("JARVIS_VOICE_ENABLED", "on")     # doctor accepts 'on'
    assert _env_true("JARVIS_VOICE_ENABLED") is True
    names = [f.name for f in env.resolve_requirement_files()]
    assert "requirements-voice.txt" in names             # 'on' → active
