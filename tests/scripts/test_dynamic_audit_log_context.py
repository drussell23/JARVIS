"""Dynamic Artifact Context Injection (A1 auditor visibility).

The driver, the organism, and the auditor must all resolve to ONE debug.log via a
single env var (JARVIS_ACTIVE_SESSION_LOG) -- no hardcoded `bt-<ts>`, no `pending`
placeholder. This mathematically guarantees the auditor reads exactly where the
FSM/organism writes its [A1Trace] lines.

Three seams, one contract:
  - driver  (SoakRunner.launch)            -> DICTATES the absolute path into env
  - organism (harness._resolve_active_session_dir) -> WRITES debug.log there
  - auditor (a1_graduation_auditor._resolve_log_file) -> READS that path
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = str((Path(__file__).parent.parent.parent).resolve())
_SCRIPTS_DIR = str((Path(__file__).parent.parent.parent / "scripts").resolve())
for _p in (_REPO_ROOT, _SCRIPTS_DIR, os.path.join(_REPO_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_script(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, name + ".py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Auditor: _resolve_log_file (explicit arg wins; else env; else None)
# ---------------------------------------------------------------------------

def test_auditor_resolve_log_file_prefers_explicit_arg(monkeypatch):
    aud = _load_script("a1_graduation_auditor")
    monkeypatch.setenv("JARVIS_ACTIVE_SESSION_LOG", "/env/path/debug.log")
    assert aud._resolve_log_file("/explicit/debug.log") == "/explicit/debug.log"


def test_auditor_resolve_log_file_falls_back_to_env(monkeypatch):
    aud = _load_script("a1_graduation_auditor")
    monkeypatch.setenv("JARVIS_ACTIVE_SESSION_LOG", "/env/path/debug.log")
    assert aud._resolve_log_file(None) == "/env/path/debug.log"


def test_auditor_resolve_log_file_none_when_neither(monkeypatch):
    aud = _load_script("a1_graduation_auditor")
    monkeypatch.delenv("JARVIS_ACTIVE_SESSION_LOG", raising=False)
    assert aud._resolve_log_file(None) is None


# ---------------------------------------------------------------------------
# Organism: _resolve_active_session_dir honors the injected path
# ---------------------------------------------------------------------------

def test_organism_session_dir_honors_env(monkeypatch):
    from backend.core.ouroboros.battle_test.harness import _resolve_active_session_dir
    monkeypatch.setenv("JARVIS_ACTIVE_SESSION_LOG", "/abs/sessions/bt-iso-9/debug.log")
    got = _resolve_active_session_dir(Path("/default/dir"))
    assert str(got) == "/abs/sessions/bt-iso-9"


def test_organism_session_dir_default_when_unset(monkeypatch):
    from backend.core.ouroboros.battle_test.harness import _resolve_active_session_dir
    monkeypatch.delenv("JARVIS_ACTIVE_SESSION_LOG", raising=False)
    got = _resolve_active_session_dir(Path("/default/dir"))
    assert str(got) == "/default/dir"


# ---------------------------------------------------------------------------
# Driver: SoakRunner.launch DICTATES the path (no discovery, no 'pending')
# ---------------------------------------------------------------------------

class _FakeProc:
    def poll(self):
        return None


def test_driver_dictates_session_log_into_child_env(monkeypatch, tmp_path):
    chaos = _load_script("a1_live_fire_chaos_harness")
    captured = {}

    def _fake_popen(argv, **kw):
        captured["env"] = kw.get("env")
        return _FakeProc()

    monkeypatch.setattr(chaos.subprocess, "Popen", _fake_popen)
    runner = chaos.SoakRunner(repo_root=str(tmp_path), wall_seconds=10)
    env: dict = {}

    handle = runner.launch(env, str(tmp_path / "run"))

    assert handle.debug_log.endswith("debug.log")
    assert "pending" not in handle.debug_log          # no placeholder on the live path
    assert env["JARVIS_ACTIVE_SESSION_LOG"] == handle.debug_log
    # The SAME path is injected into the organism child's env (one memory space).
    assert captured["env"]["JARVIS_ACTIVE_SESSION_LOG"] == handle.debug_log


def test_driver_honors_preset_session_log(monkeypatch, tmp_path):
    chaos = _load_script("a1_live_fire_chaos_harness")
    monkeypatch.setattr(chaos.subprocess, "Popen", lambda argv, **kw: _FakeProc())
    runner = chaos.SoakRunner(repo_root=str(tmp_path), wall_seconds=10)
    preset = str(tmp_path / "custom" / "debug.log")
    env = {"JARVIS_ACTIVE_SESSION_LOG": preset}

    handle = runner.launch(env, str(tmp_path / "run"))

    assert handle.debug_log == preset
