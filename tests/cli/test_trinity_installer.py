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


def test_plist_targets_the_hermetic_venv_strictly(tmp_path, monkeypatch):
    """Deployment immutability: the daemon targets ONLY the hermetic venv
    path — no sys.executable fallback — regardless of on-disk presence."""
    import sys
    venv = tmp_path / ".jarvis" / "venv"
    monkeypatch.delenv("JARVIS_VENV_PYTHON", raising=False)
    monkeypatch.setenv("JARVIS_VENV_DIR", str(venv))
    p = inst.build_supervisor_plist()
    prog = p["ProgramArguments"]
    assert prog[0] == str(venv / "bin" / "python")       # hermetic venv
    assert prog[0] != sys.executable                     # NOT the global one
    assert prog[1].endswith("unified_supervisor.py")
    assert str(venv / "bin") in p["EnvironmentVariables"]["PATH"]


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


def _clean_teardown(**kw):
    return inst.TeardownReport(clean=True, detail="clear")


def test_install_proceeds_when_doctor_ok(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))
    ok = _FakeReport(True, [_FakeCheck("python-runtime", "READY")])

    report = inst.run_install(
        agents_dir=agents, app_dest=tmp_path / "dist",
        doctor_report=ok, runner=lambda *a, **k: type("R", (), {"returncode": 0})(),
        venv_check=lambda: True,                 # hermetic venv present
        teardown_fn=_clean_teardown,             # port already clear
    )
    assert report.aborted is False
    assert report.doctor_ok is True
    assert report.plist_path is not None and report.plist_path.exists()
    assert report.app_path is not None and report.app_path.exists()


def test_install_aborts_when_hermetic_venv_missing(tmp_path, monkeypatch):
    """Deployment immutability: no venv → abort with a bootstrap-env
    directive, NEVER silently fall back to a shared interpreter."""
    ok = _FakeReport(True, [_FakeCheck("python-runtime", "READY")])
    report = inst.run_install(
        agents_dir=tmp_path / "LA", app_dest=tmp_path / "dist",
        doctor_report=ok, venv_check=lambda: False,
    )
    assert report.aborted is True
    assert "bootstrap-env" in report.reason


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


# ---------------------------------------------------------------------------
# MANDATE 4 — Stateful Pre-Flight Teardown: supervisor holds 8010 → SIGTERM
# → port clears → install proceeds without EADDRINUSE
# ---------------------------------------------------------------------------

class _Holder:
    def __init__(self, pid): self.pid = pid; self.command = "python"; self.name = ""


def test_preflight_teardown_identifies_pid_sigterms_and_clears(monkeypatch):
    """The mandate-4 core: a live unified_supervisor holds port 8010. The
    teardown must ID the PID via the doctor's lsof logic, SIGTERM it, watch
    the port clear, and report clean — no EADDRINUSE."""
    import signal
    # Port holder: pid 4242 until it is signalled, then gone.
    state = {"alive": True}
    def _holder(port):
        return _Holder(4242) if state["alive"] else None
    def _sock_holder(path):
        return None
    signals = []
    def _signaller(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGTERM and pid == 4242:
            state["alive"] = False               # process yields on SIGTERM
    slept = []

    rep = inst.preflight_teardown(
        port=8010, socket_path=None,
        holder_fn=_holder, socket_holder_fn=_sock_holder,
        extra_pids_fn=lambda: [],
        signaller=_signaller, sleeper=lambda s: slept.append(s),
    )
    assert 4242 in rep.terminated                # identified + signalled
    assert (4242, signal.SIGTERM) in signals     # graceful SIGTERM first
    assert not rep.escalated                     # yielded → no SIGKILL
    assert rep.clean is True                     # port verified free


def test_preflight_teardown_escalates_to_sigkill_if_stubborn(monkeypatch):
    import signal
    def _holder(port):
        return _Holder(999)                      # NEVER yields
    def _signaller(pid, sig):
        pass
    rep = inst.preflight_teardown(
        port=8010, socket_path=None, holder_fn=_holder,
        socket_holder_fn=lambda p: None, extra_pids_fn=lambda: [],
        signaller=_signaller, sleeper=lambda s: None, max_wait_s=1.0,
        poll_interval_s=0.2)
    assert 999 in rep.terminated
    assert 999 in rep.escalated                  # SIGTERM ignored → SIGKILL
    assert rep.clean is False                    # still held → honest report


def test_install_runs_teardown_before_bootstrap(tmp_path, monkeypatch):
    """Integration: run_install fires the teardown and only bootstraps
    once the port is clean (no EADDRINUSE at launchd load)."""
    import signal
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))
    ok = _FakeReport(True, [_FakeCheck("python-runtime", "READY")])
    state = {"alive": True}
    order = []
    def _holder(port):
        return _Holder(7777) if state["alive"] else None
    def _signaller(pid, sig):
        order.append(("SIGTERM" if sig == signal.SIGTERM else "SIG", pid))
        state["alive"] = False
    def _runner(argv, **kw):
        if "bootstrap" in argv:
            order.append(("bootstrap", 0))
            assert not state["alive"]            # port cleared BEFORE bootstrap
        return type("R", (), {"returncode": 0})()

    report = inst.run_install(
        agents_dir=tmp_path / "LA", doctor_report=ok,
        venv_check=lambda: True, holder_fn=_holder,
        socket_holder_fn=lambda p: None, signaller=_signaller,
        sleeper=lambda s: None, runner=_runner,
    )
    assert report.aborted is False
    # SIGTERM happened before the launchctl bootstrap.
    kinds = [o[0] for o in order]
    assert "SIGTERM" in kinds and "bootstrap" in kinds
    assert kinds.index("SIGTERM") < kinds.index("bootstrap")


def test_install_aborts_if_port_never_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))
    ok = _FakeReport(True, [_FakeCheck("python-runtime", "READY")])
    def _stuck_teardown(**kw):
        return inst.TeardownReport(clean=False, terminated=[9], escalated=[9],
                                   detail="port 8010 STILL contended")
    booted = []
    report = inst.run_install(
        agents_dir=tmp_path / "LA", doctor_report=ok, venv_check=lambda: True,
        teardown_fn=_stuck_teardown,
        runner=lambda *a, **k: booted.append(a) or type("R", (), {"returncode": 0})(),
    )
    assert report.aborted is True
    assert "contended" in report.reason
    # NEVER bootstrapped over a live holder.
    assert not any("bootstrap" in str(b) for b in booted)


# ---------------------------------------------------------------------------
# PHASE 8 — venv↔runtime coherence: daemon enables only installed subsystems
# ---------------------------------------------------------------------------

def test_lean_venv_stamps_subsystems_off(tmp_path, monkeypatch):
    """A lean (core-only) venv → the plist forces voice/vision OFF so the
    daemon never boots into a torch import it doesn't have."""
    venv = tmp_path / ".jarvis" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setenv("JARVIS_VENV_DIR", str(venv))
    # Probe reports torch/speechbrain/cv2 ABSENT (rc=1).
    def _runner(argv, **kw):
        return type("R", (), {"returncode": 1})()
    monkeypatch.setattr(inst.subprocess, "run", _runner)
    env = inst.build_supervisor_plist()["EnvironmentVariables"]
    assert env["JARVIS_AUDIO_BUS_ENABLED"] == "false"
    assert env["JARVIS_VISION_LOOP_ENABLED"] == "false"


def test_full_venv_stamps_subsystems_on(tmp_path, monkeypatch):
    venv = tmp_path / ".jarvis" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    monkeypatch.setenv("JARVIS_VENV_DIR", str(venv))
    def _runner(argv, **kw):
        return type("R", (), {"returncode": 0})()   # all deps present
    monkeypatch.setattr(inst.subprocess, "run", _runner)
    env = inst.build_supervisor_plist()["EnvironmentVariables"]
    assert env["JARVIS_AUDIO_BUS_ENABLED"] == "true"
    assert env["JARVIS_VISION_LOOP_ENABLED"] == "true"


def test_coherence_noop_when_venv_absent(tmp_path, monkeypatch):
    """No venv on disk → no override; .env stands (find_spec never runs)."""
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "gone"))
    env = inst.build_supervisor_plist()["EnvironmentVariables"]
    assert "JARVIS_AUDIO_BUS_ENABLED" not in env      # coherence skipped


def test_venv_has_module_uses_find_spec_not_import():
    """Zero-load: the probe must use importlib find_spec, never `import
    torch` (which would load tensors)."""
    from pathlib import Path
    src = Path(inst.__file__).read_text()
    assert "find_spec" in src
    assert "import torch" not in src


# ---------------------------------------------------------------------------
# PHASE 8b — headless SERVICE MODE daemon + honest bootstrap verification
# ---------------------------------------------------------------------------

def test_plist_boots_headless_service_mode(tmp_path, monkeypatch):
    """The daemon must boot the body HEADLESS: --skip-docker/--skip-gcp +
    JARVIS_SERVICE_MODE (no Chrome splash) + frontend off."""
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))
    p = inst.build_supervisor_plist()
    prog = p["ProgramArguments"]
    assert "--skip-docker" in prog and "--skip-gcp" in prog
    env = p["EnvironmentVariables"]
    assert env["JARVIS_SERVICE_MODE"] == "1"          # no visible Chrome UI
    assert env["JARVIS_FRONTEND_AUTOLAUNCH"] == "0"   # no web frontend
    assert env["JARVIS_ENABLE_SLIM_MODE"]             # lean


def test_install_reports_failure_when_bootstrap_does_not_load(tmp_path, monkeypatch):
    """The silent-success bug: launchctl bootstrap returns non-zero WITHOUT
    raising, and print shows the agent isn't loaded → install must say so,
    not falsely claim success."""
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))

    def _runner(argv, **kw):
        if "bootstrap" in argv:
            return type("R", (), {"returncode": 5, "stderr": "Bootstrap failed: 5"})()
        if "print" in argv:
            return type("R", (), {"returncode": 1, "stderr": ""})()  # NOT loaded
        return type("R", (), {"returncode": 0, "stderr": ""})()

    msg = inst.install_supervisor_agent(agents_dir=tmp_path / "LA", runner=_runner)
    assert "did NOT load" in msg or "not" in msg.lower()
    assert "installed + verified" not in msg           # never a false success


def test_install_confirms_when_agent_verified_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_VENV_DIR", str(tmp_path / "venv"))
    def _runner(argv, **kw):
        return type("R", (), {"returncode": 0, "stderr": ""})()  # all succeed
    msg = inst.install_supervisor_agent(agents_dir=tmp_path / "LA", runner=_runner)
    assert "verified loaded" in msg
