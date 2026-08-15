"""``trinity`` — the ONE front door to the whole ecosystem.

Operator authorization 2026-07-19. The scatter (`ov`, `jarvis`,
`python3 unified_supervisor.py`, JARVIS-Apple) collapses into a single
command that starts the tri-partite organism cleanly:

    trinity            boot the backend (Mind+Body) + attach the ov cockpit
    trinity up         start the backend detached; return the prompt
    trinity app        also launch the native JARVIS-Apple product surface
    trinity doctor     validate the environment before boot (ports, ghost
                       sockets, config-gated deps + models)
    trinity status     what's running (backend / sockets / native app)
    trinity down       graceful stop of the resident backend
    trinity help       usage

Root-cause discipline (mandate 1, carried from Phase 0): `.env` loads
once at the top; external processes are managed via
``asyncio.create_subprocess_exec`` / ``subprocess.Popen`` with
``start_new_session`` — never ``os.system``, never an orphaned child.
Thin surface: stdlib + the existing thin-client stack only.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

_APP_NAME = "JARVIS"                       # `open -a JARVIS` (the .app)
_HELP = """trinity — the JARVIS + O+V ecosystem, one command

  trinity           boot the backend + attach the ov cockpit (default)
  trinity up        start the backend detached; keep the terminal free
  trinity app       launch the native JARVIS-Apple product surface too
  trinity doctor    validate the environment before boot
  trinity bootstrap-env  build the hermetic ~/.jarvis/venv (isolated deps)
  trinity install   install background persistence (LaunchAgent + .app);
                    runs `doctor` + pre-flight teardown; needs the venv
  trinity uninstall remove the background service
  trinity status     what's running (backend / sockets / native app)
  trinity down       gracefully stop the resident backend
  trinity help       this message

Config lives in .env (loaded automatically). The native app pairs to
http://<this-mac>:8010.
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _load_env() -> None:
    try:
        from backend.core.env_bootstrap import load_env_once
        load_env_once()
    except Exception:
        pass


def _backend_socket() -> Path:
    """The single source of truth for the attach socket path (DRY —
    reuse the cockpit contract; fall back only if it can't import)."""
    try:
        from backend.core.ouroboros.battle_test.cockpit_attach import (
            attach_socket_path,
        )
        return attach_socket_path()
    except Exception:
        return _repo_root() / ".jarvis" / "cockpit_attach.sock"


def _backend_alive() -> bool:
    """Is the SUPERVISOR up? NEVER raises.

    Probes what the supervisor itself owns — its pid and its HTTP port —
    and deliberately NOT the cockpit attach socket.

    That socket used to be a fine proxy: one process owned the governed loop
    and the body, so any transport answering meant the backend was up. Since
    `ov` took ownership of the loop it owns that socket too, and this probe
    read it as evidence of a supervisor that was not running: `trinity up`
    printed "organism already awake" and started nothing, while
    `trinity status` — which looks at all three signals — correctly said
    ``partial readiness (pid=None, tcp=refused, uds=live)``.

    Two daemons cannot share one liveness signal. This asks the question the
    caller actually means: is there a supervisor to talk to.
    """
    try:
        from backend.core.ouroboros.cli.thin_client import probe_http
        from backend.core.ouroboros.cli.trinity_status import (
            supervisor_pid, _health_port, _strict_timeout,
        )
        if supervisor_pid() is not None:
            return True
        return asyncio.run(
            probe_http("127.0.0.1", _health_port(), _strict_timeout())
        ) == "live"
    except Exception:
        return False


def _service_env() -> dict:
    """Env for a HEADLESS service boot of the JARVIS body — no visible
    Chrome loading experience (JARVIS-Apple is the face), no web frontend,
    SLIM. Prefers the hermetic venv interpreter."""
    env = dict(os.environ)
    env.setdefault("JARVIS_ENABLE_SLIM_MODE", "SLIM")
    env["JARVIS_SERVICE_MODE"] = "1"
    env["JARVIS_FRONTEND_AUTOLAUNCH"] = "0"
    env["OUROBOROS_BATTLE_HEADLESS"] = "1"
    # `_service_python` has ALREADY chosen the interpreter, and the child
    # would otherwise choose a different one: `unified_supervisor.
    # _ensure_venv_python()` re-execs into the first `venv/` or `.venv/` it
    # finds beside the script, discarding what it was launched with. That
    # mechanism predates this launcher — it exists to rescue a bare
    # `python3 unified_supervisor.py`, which is exactly the case where
    # nobody has chosen — and two authorities on one question means the
    # loser is whichever ran first.
    #
    # Observed 2026-08-15: a Python 3.9.6 `.venv` left in the repo in March
    # captured every `trinity up`. The supervisor re-exec'd into it and died
    # in 1.9s on `ModuleNotFoundError: uuid6`, while the interpreter this
    # function had selected could import everything. `trinity status` then
    # reported ZOMBIE/DEADLOCKED, because from outside a process that never
    # bound its transports and one that wedged look identical.
    env["JARVIS_SKIP_VENV_CHECK"] = "1"
    return env


def _service_python() -> str:
    """The hermetic venv interpreter if present, else the current one."""
    try:
        from backend.core.ouroboros.cli.trinity_env import venv_python, venv_exists
        if venv_exists():
            return str(venv_python())
    except Exception:
        pass
    return sys.executable


def _spawn_backend() -> Optional[int]:
    """Start the JARVIS body detached in HEADLESS SERVICE MODE (no splash /
    Chrome / Docker / GCP), logs to .jarvis/logs/. Returns pid or None.
    NEVER raises."""
    try:
        log_dir = _repo_root() / ".jarvis" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / "trinity-backend.log"
        with open(log, "ab") as sink:
            proc = subprocess.Popen(
                [_service_python(), str(_repo_root() / "unified_supervisor.py"),
                 "--skip-docker", "--skip-gcp"],
                cwd=str(_repo_root()),
                stdout=sink, stderr=sink, stdin=subprocess.DEVNULL,
                start_new_session=True, env=_service_env(),
            )
        return proc.pid
    except Exception:
        return None


def _launch_native_app(console) -> bool:
    """`open -a JARVIS` — launch the native product surface. Returns
    True on success. NEVER raises."""
    try:
        r = subprocess.run(
            ["open", "-a", os.environ.get("JARVIS_APP_NAME", _APP_NAME)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            console.print("⏺ JARVIS-Apple launched — pairs to "
                          "http://localhost:8010", markup=False)
            return True
        console.print(
            "⎿ native app not found (build JARVIS-Apple in Xcode first, "
            "or set JARVIS_APP_NAME)", markup=False,
        )
        return False
    except Exception:
        return False


def _status(console) -> int:
    """Active IPC Health Handshake (Phase 7): PID + live TCP/UDS probe →
    HEALTHY / ZOMBIE / DOWN. Detects a live-but-deadlocked daemon that a
    plain PID check would falsely report as running."""
    from backend.core.ouroboros.cli.trinity_status import status_main
    return status_main(console)


def _down(console) -> int:
    """Stop the resident backend by attaching + sending shutdown, or
    signalling the lock holder. NEVER a blind kill."""
    try:
        lock = _repo_root() / ".jarvis" / "intake_router.lock"
        if not _backend_alive() and not lock.exists():
            console.print("⎿ backend not running", markup=False)
            return 0
        import json as _json
        import signal as _sig
        try:
            pid = int(_json.loads(lock.read_text()).get("pid", 0))
        except Exception:
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, _sig.SIGTERM)
                console.print(f"⏺ backend stopping (pid {pid}, SIGTERM)",
                              markup=False)
                return 0
            except ProcessLookupError:
                console.print("⎿ backend already gone", markup=False)
                return 0
        console.print("⎿ could not resolve the backend pid", markup=False)
        return 1
    except Exception:
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    _load_env()
    from backend.core.ouroboros.ui.theme import build_console
    console = build_console()
    args = list(sys.argv[1:] if argv is None else argv)
    verb = args[0] if args else "cockpit"

    if verb in ("help", "--help", "-h"):
        console.print(_HELP, markup=False, highlight=False)
        return 0
    if verb == "doctor":
        from backend.core.ouroboros.cli.trinity_doctor import doctor_main
        return doctor_main(console)
    if verb == "bootstrap-env":
        from backend.core.ouroboros.cli.trinity_env import env_main
        return env_main([verb], console)
    if verb in ("install", "uninstall"):
        from backend.core.ouroboros.cli.trinity_installer import installer_main
        return installer_main([verb], console)
    if verb in ("release", "build"):
        from backend.core.ouroboros.cli.trinity_release import release_main
        return release_main([verb], console)
    if verb == "status":
        return _status(console)
    if verb == "down":
        return _down(console)

    # up / app / cockpit all ensure the backend is live first.
    if not _backend_alive():
        console.print("⏺ igniting the organism (unified_supervisor)…",
                      markup=False)
        pid = _spawn_backend()
        if pid is None:
            console.print("⚠ backend ignition failed — see "
                          ".jarvis/logs/trinity-backend.log", markup=False)
            return 1
    else:
        console.print("⏺ organism already awake", markup=False)

    if verb == "app":
        _launch_native_app(console)

    if verb == "up":
        console.print("⏺ backend running detached. `trinity` to attach, "
                      "`trinity status` to check, `trinity down` to stop.",
                      markup=False)
        return 0

    # Default (cockpit): hand off to the ov thin-client attach — it
    # zero-trust-probes, cold-boots if needed, and renders the live
    # cockpit. ONE command, the full experience.
    try:
        from backend.core.ouroboros.cli.ov import main as ov_main
        return ov_main([])
    except Exception:
        console.print("⎿ backend up; run `ov` to attach the cockpit",
                      markup=False)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
