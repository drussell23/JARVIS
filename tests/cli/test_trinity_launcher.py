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

    def test_status_reports_backend(self, monkeypatch):
        console = _Console()
        monkeypatch.setattr(tl, "build_console", lambda: console, raising=False)
        monkeypatch.setattr("backend.core.ouroboros.ui.theme.build_console",
                            lambda: console)
        monkeypatch.setattr(tl, "_backend_alive", lambda: False)
        rc = tl.main(["status"])
        assert rc == 0
        assert any("backend" in l for l in console.lines)

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
