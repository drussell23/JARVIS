"""
body_environment - the Body's venv kept in step with its requirements file.

The venv was installed from its requirements file once, when it was created,
and never again: a package added later (the Google client, 2026-10-14;
Playwright for the visual browser agent, 2026-10-15) reached the file and
never the venv, and each had to be installed by hand. The pre-flight
checklist reported "visual_browser_agent (needs playwright)" while the
requirement sat in a file nothing re-read.

``sync`` compares the requirements' fingerprint (the file and every file it
includes with ``-r`` / ``-c``) with the one recorded in the venv when it was
last installed; on a change it installs with uv, then runs the file's
post-install steps, and only then records the new fingerprint -- a failed
install is retried at the next launch. Both ways of starting JARVIS run it:
``unified_supervisor.py`` before it hands over to the venv, and
``scripts/windows/jarvis.ps1`` after it creates one.

A post-install step is declared in the requirements file, beside the
package that needs it -- a package whose assets live outside pip (a
browser engine) says how to fetch them::

    playwright>=1.40.0
    # post-install: playwright install chromium

The step runs as ``<venv python> -m <step>``.

Standard library only: it runs before the venv's packages are importable.
Settings: JARVIS_BODY_ENV_SYNC=false skips it; JARVIS_UV names uv.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional

_REPO = Path(__file__).resolve().parents[2]

#: The requirements file of each platform whose Body venv is kept here.
#: macOS keeps its own repo-local venv (backend/requirements.txt carries
#: pins this file format cannot express per platform), so it is not synced.
_REQUIREMENTS = {"win32": _REPO / "backend" / "requirements-windows.txt"}

_STAMP = ".jarvis-requirements.sha256"
_INCLUDE = re.compile(r"^\s*-[rc]\s+(\S+)")
_POST_INSTALL = re.compile(r"^\s*#\s*post-install:\s*(.+?)\s*$")


def requirements_file(platform: str = sys.platform) -> Optional[Path]:
    """This platform's requirements file, or None when its venv is not synced here."""
    return next((path for prefix, path in _REQUIREMENTS.items() if platform.startswith(prefix)), None)


def _closure(path: Path, seen: Optional[set] = None) -> List[Path]:
    """*path* and every file it includes, in order, each once."""
    seen = seen if seen is not None else set()
    path = path.resolve()
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    files = [path]
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _INCLUDE.match(line)
        if match:
            files += _closure(path.parent / match.group(1), seen)
    return files


def fingerprint(path: Path) -> str:
    """What the venv must match: the requirements and everything they include."""
    digest = hashlib.sha256()
    for file in _closure(path):
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def post_install_steps(path: Path) -> List[str]:
    """The ``# post-install:`` steps the requirements declare, in order."""
    steps: List[str] = []
    for file in _closure(path):
        for line in file.read_text(encoding="utf-8").splitlines():
            match = _POST_INSTALL.match(line)
            if match and match.group(1) not in steps:
                steps.append(match.group(1))
    return steps


_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared(package: str, platform: str = sys.platform) -> bool:
    """Is *package* (an import or distribution name) in this platform's
    requirements? Then a sync installs it; otherwise it must be added."""
    path = requirements_file(platform)
    if path is None:
        return False
    wanted = _normalized(package)
    for file in _closure(path):
        for line in file.read_text(encoding="utf-8").splitlines():
            match = _NAME.match(line)
            if match and not line.lstrip().startswith(("-", "#")) and _normalized(match.group(1)) == wanted:
                return True
    return False


def _uv() -> Optional[str]:
    """uv, as the launcher finds it: JARVIS_UV, PATH, then uv's own install dir."""
    named = os.environ.get("JARVIS_UV", "").strip()
    if named:
        return named
    found = shutil.which("uv")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or Path.home()) / ".local" / "bin"
    for name in ("uv.exe", "uv"):
        if (home / name).exists():
            return str(home / name)
    return None


def _venv_root(venv_python: Path) -> Path:
    return venv_python.resolve().parent.parent


def in_step(venv_python: Path, platform: str = sys.platform) -> bool:
    """Does the venv already match its requirements?"""
    path = requirements_file(platform)
    if path is None:
        return True
    stamp = _venv_root(venv_python) / _STAMP
    try:
        return stamp.read_text(encoding="utf-8").strip() == fingerprint(path)
    except OSError:
        return False


def sync_command(venv_python: Path) -> str:
    """How an operator brings the venv in step by hand (the checklist's remedy)."""
    return f'"{venv_python}" "{Path(__file__).resolve()}" --sync'


def _run(argv: List[str], say) -> bool:
    say("  $ " + " ".join(argv))
    return subprocess.run(argv).returncode == 0


def sync(venv_python: Path, *, platform: str = sys.platform, say=print) -> bool:
    """Bring *venv_python*'s venv in step with this platform's requirements.

    True when it is in step (already, or now). On failure the fingerprint is
    not recorded, so the next launch tries again; the caller boots anyway --
    an ability whose package is missing is reported by the pre-flight
    checklist, not allowed to stop JARVIS."""
    if os.environ.get("JARVIS_BODY_ENV_SYNC", "true").strip().lower() in ("0", "false", "no", "off"):
        return True
    path = requirements_file(platform)
    if path is None or not path.is_file() or in_step(venv_python, platform):
        return True
    uv = _uv()
    if uv is None:
        say(f"[KERNEL] {path.name} changed, but uv was not found (set JARVIS_UV); "
            f"the venv was not updated")
        return False
    say(f"[KERNEL] {path.name} changed since the venv was installed; installing")
    if not _run([uv, "pip", "install", "--python", str(venv_python), "-r", str(path)], say):
        say("[KERNEL] dependency install failed; JARVIS starts with the venv as it is")
        return False
    for step in post_install_steps(path):
        if not _run([str(venv_python), "-m", *step.split()], say):
            say(f"[KERNEL] post-install step failed: {step}")
            return False
    (_venv_root(venv_python) / _STAMP).write_text(fingerprint(path) + "\n", encoding="utf-8")
    say("[KERNEL] venv in step with its requirements")
    return True


def _main(argv: Iterable[str]) -> int:
    args = list(argv)
    if "--sync" not in args:
        print(f"usage: {Path(sys.executable).name} {Path(__file__).name} --sync")
        return 2
    return 0 if sync(Path(sys.executable)) else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
