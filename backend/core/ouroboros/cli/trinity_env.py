"""``trinity bootstrap-env`` — the hermetic dependency seal.

Operator authorization 2026-07-19 (Phase 5). Deployment immutability: the
24/7 daemon must run from an ISOLATED interpreter whose dependency graph
no other project can perturb. A global ``pip install`` elsewhere must not
be able to drift — and eventually crash — the resident supervisor.

Mandate 1 — Root-Cause: the environment is built with Python's NATIVE
``venv`` module invoked programmatically (``venv.create(..., with_pip=
True)``) + a ``pip install -r requirements.txt`` subprocess. No bash
wrapper, no Docker, no Anaconda.

Mandate 2 — Atomic Venv Swapping: a rebuild NEVER mutates the live venv
in place. It is constructed in ``~/.jarvis/venv.tmp``; only after ``pip``
succeeds is it swapped into place with ``os.replace`` (atomic rename).
A failed ``pip install`` leaves the existing runtime completely
untouched — the daemon can never be caught with a half-installed venv.

Injectable ``creator`` / ``runner`` seams make the whole flow unit-
testable without building a real 300-package environment. Every public
entry point NEVER raises.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional


def venv_dir() -> Path:
    """The hermetic venv root (``~/.jarvis/venv``), env-overridable."""
    base = os.environ.get("JARVIS_VENV_DIR", "~/.jarvis/venv")
    return Path(os.path.expanduser(base))


def venv_tmp_dir() -> Path:
    """The staging root for an atomic rebuild (``~/.jarvis/venv.tmp``)."""
    d = venv_dir()
    return d.with_name(d.name + ".tmp")


def venv_backup_dir() -> Path:
    d = venv_dir()
    return d.with_name(d.name + ".old")


def venv_python(base: Optional[Path] = None) -> Path:
    return (base or venv_dir()) / "bin" / "python"


def venv_exists(base: Optional[Path] = None) -> bool:
    """True only when the hermetic interpreter is actually present +
    executable. NEVER raises."""
    try:
        p = venv_python(base)
        return p.exists() and os.access(p, os.X_OK)
    except Exception:  # noqa: BLE001
        return False


def requirements_path() -> Path:
    override = os.environ.get("JARVIS_REQUIREMENTS")
    if override:
        return Path(os.path.expanduser(override))
    try:
        from backend.core.ouroboros.cli.thin_client import repo_root
        return repo_root() / "requirements.txt"
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[4] / "requirements.txt"


# ---------------------------------------------------------------------------
# Configuration-Aware Dependency Sharding (Phase 6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Shard:
    """One dependency shard. ``toggles`` are the ``.env`` flags that
    activate it (enabled if ANY is true); ``always`` shards install
    unconditionally. No hardcoding — filenames + toggles are declarative."""
    name: str
    filename: str
    toggles: tuple = ()
    always: bool = False


#: The dependency graph, sharded by subsystem. Voice/vision carry the
#: heavy aarch64-compile-risk ML; core is the lightweight control plane +
#: semantic stack. Toggle aliases cover BOTH the mandate's names and the
#: real ``.env`` flags (voice ↔ audio-bus, vision ↔ vision-loop).
_SHARDS = (
    Shard("core", "requirements-core.txt", always=True),
    Shard("voice", "requirements-voice.txt",
          toggles=("JARVIS_VOICE_ENABLED", "JARVIS_AUDIO_BUS_ENABLED")),
    Shard("vision", "requirements-vision.txt",
          toggles=("JARVIS_VISION_ENABLED", "JARVIS_VISION_LOOP_ENABLED")),
)


def shard_dir() -> Path:
    """Directory holding the sharded requirement files. Env-overridable."""
    override = os.environ.get("JARVIS_REQUIREMENTS_DIR")
    if override:
        return Path(os.path.expanduser(override))
    try:
        from backend.core.ouroboros.cli.thin_client import repo_root
        base = repo_root()
    except Exception:  # noqa: BLE001
        base = Path(__file__).resolve().parents[4]
    return base / "deploy" / "requirements"


def _shard_enabled(shard: Shard) -> bool:
    """Is this shard active? Core is always on; a subsystem shard is on
    when ANY of its toggles is asserted. DRY (mandate 3): reuses the
    doctor's exact boolean evaluation ``_env_true``."""
    if shard.always:
        return True
    try:
        from backend.core.ouroboros.cli.trinity_doctor import _env_true
    except Exception:  # noqa: BLE001
        def _env_true(name: str) -> bool:   # fail-safe mirror
            return os.environ.get(name, "").strip().lower() in (
                "1", "true", "yes", "on")
    return any(_env_true(t) for t in shard.toggles)


def resolve_active_shards() -> List[Shard]:
    """The shards this configuration activates (mandate 2 — config-aware).
    NEVER raises."""
    try:
        return [s for s in _SHARDS if _shard_enabled(s)]
    except Exception:  # noqa: BLE001
        return [s for s in _SHARDS if s.always]


def resolve_requirement_files() -> List[Path]:
    """Existing on-disk requirement files for every active shard, in
    install order (core first). Skips a shard whose file is absent so a
    partial checkout never hard-fails. NEVER raises."""
    out: List[Path] = []
    d = shard_dir()
    for s in resolve_active_shards():
        p = d / s.filename
        if p.exists():
            out.append(p)
    return out


def build_pip_requirement_args(files: List[Path]) -> List[str]:
    """Flatten requirement files into ``-r f1 -r f2 …`` pip arguments.
    The ML shards are simply ABSENT from this list when their subsystem
    is toggled off — that is the payload optimization (mandate 4)."""
    args: List[str] = []
    for f in files:
        args.extend(["-r", str(f)])
    return args


def _default_creator(path: Path) -> None:
    """Native venv construction (mandate 1)."""
    import venv
    venv.create(str(path), with_pip=True, clear=True)


@dataclass
class BootstrapReport:
    ok: bool = False
    reason: str = ""
    python: Optional[Path] = None
    swapped: bool = False           # replaced a pre-existing venv atomically
    created_fresh: bool = False
    messages: List[str] = field(default_factory=list)


def bootstrap_env(
    *,
    creator: Optional[Callable[[Path], None]] = None,
    runner: Callable[..., Any] = subprocess.run,
    requirements: Optional[Path] = None,
    upgrade_pip: bool = True,
) -> BootstrapReport:
    """Build ``~/.jarvis/venv`` hermetically with an atomic swap. NEVER
    raises."""
    rep = BootstrapReport()
    creator = creator or _default_creator
    final = venv_dir()
    tmp = venv_tmp_dir()
    backup = venv_backup_dir()
    try:
        # Mandate 2: parse .env FIRST so shard toggles are authoritative.
        try:
            from backend.core.env_bootstrap import load_env_once
            load_env_once()
        except Exception:  # noqa: BLE001
            pass

        # ---- Resolve the install payload (config-aware sharding) ----
        # Precedence: explicit ``requirements`` arg / JARVIS_REQUIREMENTS
        # single-file override (back-compat) → else the toggled shards.
        req_files: List[Path] = []
        if requirements is not None:
            req_files = [requirements]
        elif os.environ.get("JARVIS_REQUIREMENTS"):
            req_files = [requirements_path()]
        else:
            req_files = resolve_requirement_files()
        if not req_files:
            rep.reason = (f"no requirement shards found in {shard_dir()} — "
                          "run the migration or set JARVIS_REQUIREMENTS")
            return rep
        missing = [str(f) for f in req_files if not f.exists()]
        if missing:
            rep.reason = f"requirement file(s) not found: {', '.join(missing)}"
            return rep
        active = [s.name for s in resolve_active_shards()]
        rep.messages.append(
            f"⏺ install payload: shards={active} "
            f"({len(req_files)} requirement file(s))")
        # Clean any stale staging from a prior crashed run.
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.parent.mkdir(parents=True, exist_ok=True)

        # 1) Native venv into the TEMP dir (never the live path).
        creator(tmp)
        tmp_py = venv_python(tmp)
        if not tmp_py.exists():
            rep.reason = "venv creation did not produce an interpreter"
            shutil.rmtree(tmp, ignore_errors=True)
            return rep

        # 2) pip install into the isolated interpreter — ONLY the active
        # shards (mandate 2/4: toggled-off ML is never in the argv).
        if upgrade_pip:
            runner([str(tmp_py), "-m", "pip", "install", "--upgrade", "pip"],
                   capture_output=True, text=True, timeout=600)
        pip_req_args = build_pip_requirement_args(req_files)
        r = runner([str(tmp_py), "-m", "pip", "install", *pip_req_args],
                   capture_output=True, text=True, timeout=3600)
        if getattr(r, "returncode", 1) != 0:
            # Mandate 2: pip failed → discard TEMP, live venv UNTOUCHED.
            shutil.rmtree(tmp, ignore_errors=True)
            rep.reason = ("pip install failed — existing runtime left "
                          "intact: " + (getattr(r, "stderr", "") or "")[-400:])
            return rep

        # 3) Atomic swap (mandate 2). os.replace can't overwrite a non-
        # empty dir, so move the live venv aside FIRST (atomic rename),
        # then move TEMP into place (atomic rename), then drop the backup.
        rep.created_fresh = not final.exists()
        if final.exists():
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            os.replace(final, backup)       # live → backup (atomic)
            try:
                os.replace(tmp, final)      # new → live (atomic)
            except Exception:
                # Extremely rare: restore the backup so we never end up
                # with NO venv.
                os.replace(backup, final)
                shutil.rmtree(tmp, ignore_errors=True)
                rep.reason = "atomic swap failed — restored previous venv"
                return rep
            shutil.rmtree(backup, ignore_errors=True)
            rep.swapped = True
        else:
            os.replace(tmp, final)          # first install (atomic)

        rep.ok = True
        rep.python = venv_python(final)
        rep.messages.append(
            f"⏺ hermetic venv ready at {final} "
            + ("(atomically swapped)" if rep.swapped else "(fresh)"))
        return rep
    except Exception as exc:  # noqa: BLE001
        # Fail closed — never leave the live venv in a partial state.
        try:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        rep.reason = f"bootstrap-env failed: {exc}"
        return rep


def env_main(argv: Optional[List[str]] = None, console=None) -> int:
    """Entry for ``trinity bootstrap-env``. Returns 0 on a ready venv.
    NEVER raises."""
    try:
        if console is None:
            from backend.core.ouroboros.ui.theme import build_console
            console = build_console()
        # Parse .env so the shard preview reflects the real toggles.
        try:
            from backend.core.env_bootstrap import load_env_once
            load_env_once()
        except Exception:  # noqa: BLE001
            pass
        shards = [s.name for s in resolve_active_shards()]
        skipped = [s.name for s in _SHARDS if s.name not in shards]
        console.print(
            f"⏺ building hermetic venv at {venv_dir()} — shards: {shards}"
            + (f" (skipping {skipped} — toggled off)" if skipped else "")
            + ". Core installs always; heavy ML only when its subsystem is "
            "enabled. This can take several minutes…", markup=False)
        rep = bootstrap_env()
        for m in rep.messages:
            console.print(m, markup=False)
        if not rep.ok:
            console.print(f"✗ bootstrap-env failed: {rep.reason}", markup=False)
            return 1
        console.print(
            f"⏺ hermetic seal complete — the daemon will run from {rep.python}. "
            "Now run `trinity install`.", markup=False)
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            console and console.print(f"✗ bootstrap-env failed: {exc}",
                                      markup=False)
        except Exception:  # noqa: BLE001
            pass
        return 1


__all__ = [
    "venv_dir", "venv_tmp_dir", "venv_backup_dir", "venv_python",
    "venv_exists", "requirements_path", "bootstrap_env", "BootstrapReport",
    "env_main", "Shard", "shard_dir", "resolve_active_shards",
    "resolve_requirement_files", "build_pip_requirement_args",
]
