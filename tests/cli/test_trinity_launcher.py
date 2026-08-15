"""trinity launcher spine — the ONE front door."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.cli import trinity_launcher as tl


class _Console:
    def __init__(self): self.lines = []
    def print(self, text, **kw): self.lines.append(str(text))


class TestTrinityRouting:
    def test_help(self, monkeypatch, capsys):
        rc = tl.main(["help"])
        assert rc == 0

    def test_status_runs_active_health_handshake(self, monkeypatch):
        """`trinity status` now delegates to the Active IPC Health
        Handshake (Phase 7) — reports a supervisor health state and exits
        non-zero when not HEALTHY."""
        console = _Console()
        monkeypatch.setattr("backend.core.ouroboros.ui.theme.build_console",
                            lambda: console)
        # Deterministic: inject a DOWN report so the test never depends on
        # whether a real backend happens to be listening on :8010.
        from backend.core.ouroboros.cli import trinity_status as st
        async def _down():
            return st.HealthReport(state=st.Health.DOWN, pid=None,
                                   detail="not running", recommendation="install")
        monkeypatch.setattr(st, "active_health_handshake", lambda **k: _down())
        rc = tl.main(["status"])
        assert rc == 1                                # not HEALTHY
        assert any("supervisor" in l for l in console.lines)

    def test_up_spawns_when_absent(self, monkeypatch):
        console = _Console()
        monkeypatch.setattr("backend.core.ouroboros.ui.theme.build_console",
                            lambda: console)
        monkeypatch.setattr(tl, "_backend_alive", lambda: False)
        spawns = []
        monkeypatch.setattr(tl, "_spawn_backend",
                            lambda: spawns.append(1) or 4242)
        rc = tl.main(["up"])
        assert rc == 0
        assert spawns == [1]                      # ignited
        assert any("detached" in l for l in console.lines)

    def test_up_noop_when_alive(self, monkeypatch):
        console = _Console()
        monkeypatch.setattr("backend.core.ouroboros.ui.theme.build_console",
                            lambda: console)
        monkeypatch.setattr(tl, "_backend_alive", lambda: True)
        spawns = []
        monkeypatch.setattr(tl, "_spawn_backend", lambda: spawns.append(1))
        tl.main(["up"])
        assert spawns == []                       # already awake, no dup
        assert any("already awake" in l for l in console.lines)

    def test_app_launches_native(self, monkeypatch):
        console = _Console()
        monkeypatch.setattr("backend.core.ouroboros.ui.theme.build_console",
                            lambda: console)
        monkeypatch.setattr(tl, "_backend_alive", lambda: True)
        launched = []
        monkeypatch.setattr(tl, "_launch_native_app",
                            lambda c: launched.append(1) or True)
        monkeypatch.setattr(tl, "main",
                            tl.main)  # keep
        # app verb → ensure backend + launch app (then would attach; stub)
        monkeypatch.setattr("backend.core.ouroboros.cli.ov.main",
                            lambda a: 0)
        tl.main(["app"])
        assert launched == [1]

    def test_entry_point_registered(self):
        from pathlib import Path
        pp = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
        assert 'trinity = "backend.core.ouroboros.cli.trinity_launcher:main"' in pp

    def test_no_os_system_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] /
               "backend/core/ouroboros/cli/trinity_launcher.py").read_text()
        assert "os.system(" not in src           # no os.system CALL
        assert "start_new_session=True" in src   # detached, no orphan


class TestInterpreterAuthority:
    """Whoever launches the child decides which Python runs it.

    Observed 2026-08-15: a Python 3.9.6 `.venv` left in the repo in March
    captured every `trinity up`. `_service_python()` correctly selected the
    hermetic 3.11.10 interpreter, `unified_supervisor._ensure_venv_python()`
    re-exec'd out of it into the stale one, and the backend died in 1.9s on
    `ModuleNotFoundError: uuid6`. From outside, `trinity status` could only
    say ZOMBIE/DEADLOCKED — a process that never bound its transports and one
    that wedged are indistinguishable.
    """

    def test_the_service_env_disables_the_childs_own_venv_reexec(self):
        """Two authorities on one question means the loser is whoever ran
        first. The launcher has already chosen, so the child must not."""
        assert tl._service_env().get("JARVIS_SKIP_VENV_CHECK") == "1"

    def test_the_hermetic_interpreter_is_preferred_when_it_exists(
            self, tmp_path, monkeypatch):
        """Executed against a real file on disk, not a mocked predicate:
        `venv_exists` tests for an executable bit, and a fake that answers
        True for a path with no binary would prove nothing."""
        fake = tmp_path / "bin" / "python"
        fake.parent.mkdir(parents=True)
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        monkeypatch.setattr(
            "backend.core.ouroboros.cli.trinity_env.venv_dir",
            lambda: tmp_path)
        assert tl._service_python() == str(fake)

    def test_it_falls_back_to_the_current_interpreter_when_absent(
            self, tmp_path, monkeypatch):
        """A missing hermetic venv is not a reason to refuse to boot."""
        import sys
        monkeypatch.setattr(
            "backend.core.ouroboros.cli.trinity_env.venv_dir",
            lambda: tmp_path / "nope")
        assert tl._service_python() == sys.executable
