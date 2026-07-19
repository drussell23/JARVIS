"""``trinity install`` — Thin-Bundle macOS productization.

Operator authorization 2026-07-19 (Phase 3). Turns the repo + venv into a
native, self-restarting macOS background service with a Gatekeeper-
signable ``.app`` shell — WITHOUT the fatal anti-pattern of bundling
Torch/SpeechBrain into the binary.

Mandate 1 — Thin-Bundle (Root-Cause):
  The generated ``.app`` contains ONLY a tiny launcher executable + an
  ``Info.plist`` carrying the TCC privacy declarations. It does NOT embed
  the Python runtime, ML wheels, or model weights — those live in the
  localized virtualenv (``~/.jarvis/venv``). The bundle stays kilobytes,
  so Gatekeeper signing/notarization never chokes on multi-GB payloads.

Mandate 2 — Architectural Purity:
  * **LaunchAgent → localized venv, idempotent** — the plist's
    ``ProgramArguments`` point at ``~/.jarvis/venv/bin/python``. Install
    is bootout-then-bootstrap: any prior agent is unloaded (killing the
    orphaned daemon) before the fresh plist is atomically written and
    loaded. Running install N times converges to ONE agent, ONE plist,
    zero orphans.
  * **TCC Privacy Handshake** — ``NSMicrophoneUsageDescription`` +
    ``NSScreenCaptureUsageDescription`` are injected into the app
    ``Info.plist`` so macOS shows a consent prompt instead of a silent
    ``SIGKILL``. Denial is fail-soft: ``trinity doctor`` reflects the
    degraded (text-only) state; the daemon never crashes on it.
  * **Resilient Startup** — ``KeepAlive{SuccessfulExit=false}`` +
    ``RunAtLoad=true``. No ``sleep()`` waits: a supervisor that loses a
    boot race exits non-zero and launchd relaunches it; port binding is
    handled natively by the resilient binder.

Mandate 3 — DRY: install runs ``trinity doctor`` (``run_doctor``) as its
FIRST step and ABORTS on any FAIL before generating a single asset. It
reuses ``env_bootstrap`` (paths/venv), ``thin_client`` (repo root, log
rollover), and stdlib ``plistlib`` (same serializer as the ov agent).

Every public entry point NEVER raises.
"""
from __future__ import annotations

import asyncio
import os
import plistlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

# No hardcoding — every identity/path is env-overridable with a default.
SUPERVISOR_LABEL = os.environ.get("JARVIS_SUPERVISOR_LABEL", "com.jarvis.supervisor")
APP_BUNDLE_ID = os.environ.get("JARVIS_APP_BUNDLE_ID", "com.jarvis.trinity")
APP_NAME = os.environ.get("JARVIS_APP_NAME", "Trinity")


def _repo_root() -> Path:
    try:
        from backend.core.ouroboros.cli.thin_client import repo_root
        return repo_root()
    except Exception:
        return Path(__file__).resolve().parents[4]


def localized_python() -> Path:
    """The Thin-Bundle interpreter: the localized venv (``~/.jarvis/venv``)
    the heavy ML deps install into. Env-overridable. This is the path the
    LaunchAgent targets — NOT a machine-specific pyenv shim."""
    override = os.environ.get("JARVIS_VENV_PYTHON")
    if override:
        return Path(os.path.expanduser(override))
    base = os.environ.get("JARVIS_VENV_DIR", "~/.jarvis/venv")
    return Path(os.path.expanduser(base)) / "bin" / "python"


def resolve_supervisor_python() -> Tuple[Path, str]:
    """The interpreter the LaunchAgent should ACTUALLY target — adaptive,
    so a machine without the localized venv still gets a bootable daemon
    (mandate 2 — edge case: local/dev install before the venv is built).

    Precedence: explicit ``JARVIS_VENV_PYTHON`` → the localized venv if it
    exists on disk → the currently-running interpreter (``sys.executable``,
    e.g. the pyenv/conda python that is running trinity right now). Returns
    ``(path, source)`` where source ∈ {``localized_venv``, ``current``}.
    NEVER raises."""
    import sys
    override = os.environ.get("JARVIS_VENV_PYTHON")
    if override:
        p = Path(os.path.expanduser(override))
        return p, ("localized_venv" if "jarvis" in str(p) else "override")
    ideal = localized_python()
    try:
        if ideal.exists():
            return ideal, "localized_venv"
    except Exception:
        pass
    # Fall back to the interpreter that is demonstrably working right now.
    return Path(sys.executable), "current"


def _log_dir() -> Path:
    return _repo_root() / ".jarvis" / "logs"


def launch_agents_dir(agents_dir: Optional[Path] = None) -> Path:
    return agents_dir or (Path.home() / "Library" / "LaunchAgents")


def supervisor_plist_path(agents_dir: Optional[Path] = None) -> Path:
    return launch_agents_dir(agents_dir) / f"{SUPERVISOR_LABEL}.plist"


# ---------------------------------------------------------------------------
# LaunchAgent (mandate 2 — venv-targeted, resilient, idempotent)
# ---------------------------------------------------------------------------

def build_supervisor_plist(*, python: Optional[Path] = None) -> dict:
    """The launchd definition for the resident supervisor. All paths
    resolved at generation time; nothing machine-hardcoded. The
    interpreter is resolved ADAPTIVELY (localized venv when present, else
    the working interpreter) so the generated daemon is always bootable."""
    py = python or resolve_supervisor_python()[0]
    root = _repo_root()
    logs = _log_dir()
    venv_bin = str(py.parent)
    return {
        "Label": SUPERVISOR_LABEL,
        "ProgramArguments": [str(py), str(root / "unified_supervisor.py")],
        "WorkingDirectory": str(root),
        # Resilient Startup (mandate 2): relaunch on crash / lost boot
        # race; RunAtLoad brings it up at login. No sleep()-to-wait.
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "supervisor.out.log"),
        "StandardErrorPath": str(logs / "supervisor.err.log"),
        "EnvironmentVariables": {
            # venv bin first so the localized interpreter + its console
            # scripts win; repo on PYTHONPATH so `backend.*` imports.
            "PATH": f"{venv_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(root),
            "VIRTUAL_ENV": str(py.parent.parent),
            "OUROBOROS_BATTLE_HEADLESS": "1",
        },
    }


def write_supervisor_plist(
    *, agents_dir: Optional[Path] = None, python: Optional[Path] = None,
) -> Path:
    """Atomically (over)write the plist. Idempotent — ``plistlib.dump``
    emits a WHOLE document to a temp sibling then ``os.replace``s it, so
    a second run produces byte-identical XML and can never append/corrupt
    a partial document. NEVER raises."""
    path = supervisor_plist_path(agents_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".plist.tmp")
        with open(tmp, "wb") as fh:
            plistlib.dump(build_supervisor_plist(python=python), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp = path.with_suffix(".plist.tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return path


def install_supervisor_agent(
    *,
    agents_dir: Optional[Path] = None,
    python: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Bootout-then-bootstrap: unload any prior agent (killing its
    daemon) BEFORE writing + loading the fresh plist. Idempotent — N runs
    converge to one agent, zero orphans. NEVER raises."""
    try:
        _log_dir().mkdir(parents=True, exist_ok=True)
        try:
            from backend.core.ouroboros.cli.thin_client import rollover_daemon_log
            rollover_daemon_log(_log_dir() / "supervisor.out.log")
            rollover_daemon_log(_log_dir() / "supervisor.err.log")
        except Exception:
            pass
        uid = os.getuid()
        # 1) Unload any existing instance FIRST (idempotency + no orphan).
        try:
            runner(["launchctl", "bootout", f"gui/{uid}/{SUPERVISOR_LABEL}"],
                   capture_output=True, timeout=10)
        except Exception:
            pass
        # 2) Write the fresh plist atomically.
        path = write_supervisor_plist(agents_dir=agents_dir, python=python)
        # 3) Load it.
        try:
            runner(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                   capture_output=True, timeout=10)
        except Exception:
            return (f"⏺ supervisor agent written to {path} — load it with: "
                    f"launchctl bootstrap gui/$UID {path}")
        return f"⏺ resident supervisor installed ({SUPERVISOR_LABEL}) — {path}"
    except Exception as exc:
        return f"⚠ supervisor install failed: {exc}"


def uninstall_supervisor_agent(
    *,
    agents_dir: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Boot the agent out + remove the plist. Idempotent. NEVER raises."""
    try:
        path = supervisor_plist_path(agents_dir)
        try:
            uid = os.getuid()
            runner(["launchctl", "bootout", f"gui/{uid}/{SUPERVISOR_LABEL}"],
                   capture_output=True, timeout=10)
        except Exception:
            pass
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return (f"⏺ resident supervisor uninstalled ({SUPERVISOR_LABEL})"
                if existed else "⎿ no resident supervisor was installed")
    except Exception as exc:
        return f"⚠ supervisor uninstall failed: {exc}"


# ---------------------------------------------------------------------------
# Thin-Bundle .app (mandate 1 + TCC mandate 2)
# ---------------------------------------------------------------------------

def build_app_info_plist() -> dict:
    """The ``.app`` ``Info.plist`` — carries the TCC usage strings so
    macOS prompts for consent (not a silent SIGKILL). ``LSUIElement``
    keeps the launcher agent-style (no Dock icon)."""
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleExecutable": "trinity-launch",
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": os.environ.get("JARVIS_APP_VERSION", "1.0.0"),
        "CFBundleVersion": os.environ.get("JARVIS_APP_BUILD", "1"),
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,               # background agent, no Dock icon
        "NSHighResolutionCapable": True,
        # ---- TCC Privacy Handshake (mandate 2) ----
        "NSMicrophoneUsageDescription":
            "JARVIS listens for the wake word and your voice commands. "
            "Without microphone access it runs in text-only mode.",
        "NSScreenCaptureUsageDescription":
            "JARVIS observes your screen to provide proactive, context-aware "
            "assistance. Denying this disables visual awareness only.",
        "NSSpeechRecognitionUsageDescription":
            "JARVIS transcribes your spoken commands on-device.",
    }


def _thin_launcher_script() -> str:
    """The ``.app``'s tiny executable: it does NOT contain Python or ML —
    it execs the localized venv's ``trinity`` (mandate 1, Thin-Bundle).
    Resolves the venv at RUN time so the bundle is machine-portable."""
    venv_dir = os.environ.get("JARVIS_VENV_DIR", "$HOME/.jarvis/venv")
    repo = str(_repo_root())
    return (
        "#!/bin/bash\n"
        "# Thin-Bundle launcher — heavy deps live in the venv, not here.\n"
        "# Adaptive interpreter: localized venv → trinity on PATH → any\n"
        "# python3 that can import the package. Never a hard fail.\n"
        f'VENV="{venv_dir}"\n'
        f'REPO="{repo}"\n'
        'cd "$REPO" 2>/dev/null\n'
        'if [ -x "$VENV/bin/python" ]; then\n'
        '  exec "$VENV/bin/python" -m backend.core.ouroboros.cli.'
        'trinity_launcher up\n'
        'elif command -v trinity >/dev/null 2>&1; then\n'
        '  exec trinity up\n'
        'elif command -v python3 >/dev/null 2>&1; then\n'
        '  exec python3 -m backend.core.ouroboros.cli.trinity_launcher up\n'
        'else\n'
        '  osascript -e \'display notification "No interpreter found — run '
        'trinity install" with title "JARVIS"\' 2>/dev/null\n'
        '  exit 1\n'
        'fi\n'
    )


def generate_app_bundle(dest: Path) -> Path:
    """Generate the Thin-Bundle ``AppName.app`` skeleton at ``dest`` (a
    directory). Idempotent — every file is fully overwritten, so a second
    run yields an identical bundle. Returns the ``.app`` path. NEVER
    raises past returning the intended path."""
    app = dest / f"{APP_NAME}.app"
    try:
        contents = app / "Contents"
        macos = contents / "MacOS"
        resources = contents / "Resources"
        for d in (macos, resources):
            d.mkdir(parents=True, exist_ok=True)
        # Info.plist (full-document write → idempotent, never corrupt).
        tmp = contents / "Info.plist.tmp"
        with open(tmp, "wb") as fh:
            plistlib.dump(build_app_info_plist(), fh)
        os.replace(tmp, contents / "Info.plist")
        # PkgInfo
        (contents / "PkgInfo").write_text("APPL????")
        # The thin launcher executable (chmod +x).
        launcher = macos / "trinity-launch"
        launcher.write_text(_thin_launcher_script())
        launcher.chmod(0o755)
    except Exception:
        pass
    return app


# ---------------------------------------------------------------------------
# Orchestration (mandate 3 — doctor gate FIRST)
# ---------------------------------------------------------------------------

@dataclass
class InstallReport:
    aborted: bool = False
    reason: str = ""
    doctor_ok: bool = False
    plist_path: Optional[Path] = None
    app_path: Optional[Path] = None
    messages: List[str] = field(default_factory=list)


def run_install(
    *,
    agents_dir: Optional[Path] = None,
    app_dest: Optional[Path] = None,
    python: Optional[Path] = None,
    runner: Callable[..., Any] = subprocess.run,
    skip_doctor: bool = False,
    doctor_report: Any = None,
) -> InstallReport:
    """Full Thin-Bundle install. MANDATE 3: run ``trinity doctor`` FIRST
    and ABORT on any FAIL before generating a single asset. NEVER
    raises."""
    report = InstallReport()
    try:
        # ---- Gate: environmental preflight must pass (DRY — run_doctor) ----
        if not skip_doctor:
            try:
                from backend.core.ouroboros.cli.trinity_doctor import run_doctor
                dr = doctor_report if doctor_report is not None else \
                    asyncio.run(run_doctor())
                report.doctor_ok = bool(getattr(dr, "ok", False))
                if not report.doctor_ok:
                    fails = [r.name for r in getattr(dr, "results", [])
                             if getattr(r, "status", None)
                             and r.status.value == "FAIL"]
                    report.aborted = True
                    report.reason = (
                        "trinity doctor reported FAIL: "
                        + ", ".join(fails) + " — install aborted, no assets "
                        "generated. Fix the ✗ items and re-run.")
                    return report
            except Exception as exc:
                report.aborted = True
                report.reason = f"doctor gate could not run: {exc}"
                return report
        else:
            report.doctor_ok = True

        # ---- Generate assets (only reached when the gate passed) ----
        report.plist_path = write_supervisor_plist(
            agents_dir=agents_dir, python=python)
        report.messages.append(install_supervisor_agent(
            agents_dir=agents_dir, python=python, runner=runner))
        if app_dest is not None:
            report.app_path = generate_app_bundle(app_dest)
            report.messages.append(
                f"⏺ Thin-Bundle app generated — {report.app_path}")
        return report
    except Exception as exc:
        report.aborted = True
        report.reason = f"install failed: {exc}"
        return report


def installer_main(argv: Optional[List[str]] = None, console=None) -> int:
    """Entry for ``trinity install`` / ``trinity uninstall``. Returns 0 on
    success, 1 on abort/failure. NEVER raises."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    verb = args[0] if args else "install"
    try:
        if console is None:
            from backend.core.ouroboros.ui.theme import build_console
            console = build_console()
        if verb == "uninstall":
            console.print(uninstall_supervisor_agent(), markup=False)
            return 0
        # install: default app dest = repo dist/ (env-overridable)
        app_dest = Path(os.path.expanduser(
            os.environ.get("JARVIS_APP_DEST", str(_repo_root() / "dist"))))
        report = run_install(app_dest=app_dest)
        if report.aborted:
            console.print(f"✗ {report.reason}", markup=False)
            return 1
        for m in report.messages:
            console.print(m, markup=False)
        console.print(
            "⏺ background persistence active — the supervisor now "
            "auto-starts at login and restarts on crash", markup=False)
        return 0
    except Exception as exc:
        try:
            console and console.print(f"✗ install failed: {exc}", markup=False)
        except Exception:
            pass
        return 1


__all__ = [
    "SUPERVISOR_LABEL", "APP_BUNDLE_ID", "APP_NAME",
    "localized_python", "supervisor_plist_path", "build_supervisor_plist",
    "write_supervisor_plist", "install_supervisor_agent",
    "uninstall_supervisor_agent", "build_app_info_plist",
    "generate_app_bundle", "InstallReport", "run_install", "installer_main",
]
