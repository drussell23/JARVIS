"""trinity install — Thin-Bundle macOS productization spine.

Mandate 4 (verbatim): simulate a LaunchAgent generation sequence. Assert
the generated .plist XML contains the correct KeepAlive dict, targets the
localized venv path accurately, and that executing the generation
function TWICE does not corrupt the XML structure (idempotency).

Plus: Thin-Bundle (no ML in the .app), TCC Info.plist keys, the
doctor-gate abort (mandate 3), and the resilient port binder.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from backend.core.ouroboros.cli import trinity_installer as inst
from backend.core.ouroboros.cli import port_binder as pb


# ---------------------------------------------------------------------------
# MANDATE 4 — LaunchAgent generation: KeepAlive + venv path + idempotency
# ---------------------------------------------------------------------------

def test_plist_has_keepalive_dict_and_runatload(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / ".jarvis" / "venv"))
    p = inst.build_supervisor_plist()
    assert p["KeepAlive"] == {"SuccessfulExit": False}   # exact dict
    assert p["RunAtLoad"] is True
    assert p["ProcessType"] == "Background"


def test_plist_targets_the_localized_venv_python(tmp_path, monkeypatch):
    venv = tmp_path / ".jarvis" / "venv"
    # The localized venv EXISTS → it is preferred over the fallback.
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setenv("JARVIS_VENV_DIR", str(venv))
    p = inst.build_supervisor_plist()
    prog = p["ProgramArguments"]
    assert prog[0] == str(venv / "bin" / "python")       # localized venv
    assert prog[1].endswith("unified_supervisor.py")
    # env points PATH at the venv bin + PYTHONPATH at the repo
    assert str(venv / "bin") in p["EnvironmentVariables"]["PATH"]


def test_interpreter_adaptive_fallback_when_no_venv(tmp_path, monkeypatch):
    """The real local-install edge case: no localized venv yet → resolve
    to the CURRENTLY-RUNNING interpreter so the daemon is still bootable
    (not a dead path)."""
    import sys
    monkeypatch.delenv("JARVIS_VENV_PYTHON", raising=False)
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "does_not_exist"))
    py, source = inst.resolve_supervisor_python()
    assert source == "current"
    assert py == Path(sys.executable)                    # the working python
    # And the plist targets that working interpreter, not a dead path.
    prog = inst.build_supervisor_plist()["ProgramArguments"]
    assert prog[0] == sys.executable


def test_interpreter_prefers_localized_venv_when_present(tmp_path, monkeypatch):
    venv = tmp_path / ".jarvis" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.delenv("JARVIS_VENV_PYTHON", raising=False)
    monkeypatch.setenv("JARVIS_VENV_DIR", str(venv))
    py, source = inst.resolve_supervisor_python()
    assert source == "localized_venv"
    assert py == venv / "bin" / "python"


def test_write_plist_is_idempotent_and_valid_xml(tmp_path, monkeypatch):
    """MANDATE 4 core: generate twice → byte-identical, still-valid plist
    (a full-document write can never append/corrupt)."""
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))

    path1 = inst.write_supervisor_plist(agents_dir=agents)
    first = path1.read_bytes()
    # Parses as a valid plist with the KeepAlive contract.
    parsed = plistlib.loads(first)
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}

    path2 = inst.write_supervisor_plist(agents_dir=agents)
    second = path2.read_bytes()

    assert path1 == path2                        # one plist, not two
    assert first == second                       # byte-identical — no corruption
    assert plistlib.loads(second)["Label"] == inst.SUPERVISOR_LABEL
    # Exactly ONE plist file in the dir (no duplicate entries).
    assert len(list(agents.glob("*.plist"))) == 1


def test_install_is_idempotent_bootout_before_bootstrap(tmp_path, monkeypatch):
    """Install twice → the prior agent is booted OUT before each load
    (kills orphans), and the launchctl calls are well-formed."""
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))
    calls = []

    def _runner(argv, **kw):
        calls.append(argv)
        class _R:
            returncode = 0
        return _R()

    inst.install_supervisor_agent(agents_dir=agents, runner=_runner)
    inst.install_supervisor_agent(agents_dir=agents, runner=_runner)

    boots = [c for c in calls if "bootout" in c]
    loads = [c for c in calls if "bootstrap" in c]
    assert len(boots) == 2 and len(loads) == 2   # bootout precedes each load
    # Order within a run: bootout then bootstrap.
    assert calls.index(boots[0]) < calls.index(loads[0])
    assert len(list(agents.glob("*.plist"))) == 1  # never duplicated


# ---------------------------------------------------------------------------
# MANDATE 1 — Thin-Bundle: no ML in the .app; TCC keys present
# ---------------------------------------------------------------------------

def test_app_info_plist_has_tcc_usage_strings():
    info = inst.build_app_info_plist()
    assert info["NSMicrophoneUsageDescription"]
    assert info["NSScreenCaptureUsageDescription"]
    assert info["CFBundleIdentifier"] == inst.APP_BUNDLE_ID
    assert info["LSUIElement"] is True           # background agent


def test_app_bundle_is_thin_no_ml_payload(tmp_path):
    app = inst.generate_app_bundle(tmp_path)
    assert app.exists() and app.name.endswith(".app")
    info = app / "Contents" / "Info.plist"
    launcher = app / "Contents" / "MacOS" / "trinity-launch"
    assert info.exists() and launcher.exists()
    # Info.plist is valid + carries TCC.
    parsed = plistlib.loads(info.read_bytes())
    assert "NSMicrophoneUsageDescription" in parsed
    # Thin-Bundle proof: no bundled interpreter / wheels / weights.
    names = [p.name.lower() for p in app.rglob("*")]
    assert not any(n in ("python", "python3") for n in names)
    assert not any(n.endswith((".whl", ".dylib", ".pt", ".onnx", ".bin"))
                   for n in names)
    # The launcher EXECS the venv python — it does not embed it.
    body = launcher.read_text()
    assert "exec" in body and "bin/python" in body
    assert launcher.stat().st_mode & 0o111        # executable bit set


def test_app_bundle_generation_idempotent(tmp_path):
    a1 = inst.generate_app_bundle(tmp_path)
    info1 = (a1 / "Contents" / "Info.plist").read_bytes()
    a2 = inst.generate_app_bundle(tmp_path)
    info2 = (a2 / "Contents" / "Info.plist").read_bytes()
    assert a1 == a2 and info1 == info2            # stable, no corruption


# ---------------------------------------------------------------------------
# MANDATE 3 — doctor gate FIRST; abort on FAIL, generate nothing
# ---------------------------------------------------------------------------

class _FakeStatus:
    def __init__(self, v): self.value = v


class _FakeCheck:
    def __init__(self, name, v): self.name = name; self.status = _FakeStatus(v)


class _FakeReport:
    def __init__(self, ok, results): self.ok = ok; self.results = results


def test_install_aborts_on_doctor_fail_no_assets(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))
    failing = _FakeReport(False, [_FakeCheck("backend-port", "FAIL")])

    report = inst.run_install(
        agents_dir=agents, app_dest=tmp_path / "dist",
        doctor_report=failing,
    )
    assert report.aborted is True
    assert "backend-port" in report.reason
    # NOTHING generated — the abort is before any asset write.
    assert report.plist_path is None
    assert not agents.exists() or not list(agents.glob("*.plist"))
    assert not (tmp_path / "dist").exists()


def test_install_proceeds_when_doctor_ok(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))
    ok = _FakeReport(True, [_FakeCheck("python-runtime", "READY")])

    report = inst.run_install(
        agents_dir=agents, app_dest=tmp_path / "dist",
        doctor_report=ok, runner=lambda *a, **k: type("R", (), {"returncode": 0})(),
    )
    assert report.aborted is False
    assert report.doctor_ok is True
    assert report.plist_path is not None and report.plist_path.exists()
    assert report.app_path is not None and report.app_path.exists()


# ---------------------------------------------------------------------------
# MANDATE 2 — Resilient Startup: port binder distinguishes taken vs stack-down
# ---------------------------------------------------------------------------

def test_port_binder_returns_first_free():
    seen = []

    def _binder(port, host):
        seen.append(port)
        return port if port == 8012 else None    # 8010, 8011 "taken"

    got = pb.resilient_detect_port(8010, 8100, binder=_binder,
                                   sleeper=lambda s: None)
    assert got == 8012
    assert seen[:3] == [8010, 8011, 8012]


def test_port_binder_retries_on_transient_then_succeeds(monkeypatch):
    """A cold-boot stack (EADDRNOTAVAIL) must trigger a bounded retry,
    NOT a silent fallback to an unverified port."""
    import errno
    state = {"pass": 0}
    slept = []

    def _binder(port, host):
        if state["pass"] == 0:
            raise OSError(errno.EADDRNOTAVAIL, "stack not up")
        return port                              # stack up on 2nd pass

    def _sleeper(s):
        slept.append(s)
        state["pass"] += 1

    got = pb.resilient_detect_port(8010, 8100, binder=_binder, sleeper=_sleeper)
    assert got == 8010                           # bound for real, not fallback
    assert slept                                 # it actually backed off


def test_port_binder_no_sleep_when_all_genuinely_taken():
    """All ports EADDRINUSE → fail fast, NO backoff (backoff wouldn't
    help; brute-force waiting is the banned anti-pattern)."""
    slept = []
    got = pb.resilient_detect_port(
        8010, 8012, binder=lambda p, h: None, sleeper=lambda s: slept.append(s))
    assert got == 8010                           # last-resort fallback
    assert slept == []                           # never slept — failed fast


def test_installer_source_thin_no_pyinstaller_torch_bundling():
    """Static guard: the installer must not shell out to a blanket
    py2app/PyInstaller that would embed ML (mandate 1)."""
    src = Path(inst.__file__).read_text()
    assert "PyInstaller" not in src
    assert "py2app" not in src
