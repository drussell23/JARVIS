"""Sandbox-aware path fallback — preserves Iron Gate, routes around it.

Manifesto §7 (Absolute observability): the Iron Gate is operating correctly
when it blocks writes to restricted host directories. Do NOT lower the shields.
Instead, route telemetry and state to ``.ouroboros/state/sandbox_fallback/``
when the primary path is not writable.

Usage
-----
>>> from .sandbox_paths import sandbox_fallback
>>> log_dir = sandbox_fallback(Path.home() / ".jarvis" / "ops")
>>> # log_dir is either the primary path (if writable) or the fallback.

Semantics
---------
- Exactly one WARNING log line is emitted per distinct primary path, on first
  fallback. Subsequent calls return the cached result silently.
- Thread-safe: guarded by a module-level lock.
- Never raises. Writability probing swallows OSError/PermissionError.
- Master switch: ``JARVIS_SANDBOX_FALLBACK_DISABLED=true`` bypasses fallback
  and returns the primary path unchanged (for tests that need the raw error).
- Fallback root: ``$REPO/.ouroboros/state/sandbox_fallback/`` by default,
  overridable via ``JARVIS_SANDBOX_FALLBACK_ROOT``.
- The fallback path mirrors the relative structure under ``~/.jarvis/`` so
  forensic analysis can still find files (e.g. ``~/.jarvis/ops/2026-04-11.jsonl``
  → ``.ouroboros/state/sandbox_fallback/ops/2026-04-11.jsonl``).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Cache: primary path (as str) → resolved path (primary or fallback).
# Guarantees exactly-one-warning semantics and avoids re-probing.
_RESOLVED: Dict[str, Path] = {}
_LOCK = threading.Lock()


def _env_disabled() -> bool:
    return os.environ.get("JARVIS_SANDBOX_FALLBACK_DISABLED", "false").lower() == "true"


def _repo_root() -> Path:
    """Locate the JARVIS repo root by walking up for ``.ouroboros`` marker.

    Falls back to ``JARVIS_REPO_ROOT`` env var, then to CWD. Cached per-call
    (cheap — a few stat() syscalls).
    """
    explicit = os.environ.get("JARVIS_REPO_ROOT")
    if explicit:
        return Path(explicit).resolve()
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / ".ouroboros").exists():
            return parent
    return Path.cwd().resolve()


def _fallback_root() -> Path:
    override = os.environ.get("JARVIS_SANDBOX_FALLBACK_ROOT")
    if override:
        return Path(override).resolve()
    return _repo_root() / ".ouroboros" / "state" / "sandbox_fallback"


def _relative_to_home_jarvis(path: Path) -> Optional[Path]:
    """Return path relative to ``~/.jarvis`` if it lives there, else None."""
    try:
        return path.relative_to(Path.home() / ".jarvis")
    except (ValueError, RuntimeError):
        return None


def _derive_fallback(primary: Path) -> Path:
    """Map ``primary`` to its sandbox fallback equivalent.

    Files under ``~/.jarvis/X`` land at ``<fallback_root>/X`` so forensic
    search preserves directory shape. Files outside ``~/.jarvis/`` use the
    last 3 path components (enough to avoid collisions in practice).
    """
    rel = _relative_to_home_jarvis(primary)
    if rel is not None:
        return _fallback_root() / rel
    parts = primary.parts[-3:] if len(primary.parts) >= 3 else primary.parts
    # Strip leading "/" to keep joinpath relative.
    safe_parts = tuple(p for p in parts if p not in ("", "/"))
    return _fallback_root().joinpath(*safe_parts)


def _is_writable(path: Path) -> bool:
    """Probe whether we can mkdir + touch under ``path``.

    Treats ``path`` as a directory if it has no file suffix, else as a file
    whose parent we probe. Swallows all OSError/PermissionError.
    """
    target_dir = path.parent if path.suffix else path
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        return False
    sentinel = target_dir / ".sandbox_probe"
    try:
        sentinel.touch(exist_ok=True)
    except (PermissionError, OSError):
        return False
    try:
        sentinel.unlink()
    except OSError:
        pass
    return True


def sandbox_fallback(primary: Path) -> Path:
    """Return a writable path for ``primary``, falling back to the sandbox.

    If ``primary`` is writable, returns it unchanged. Otherwise, returns
    ``<fallback_root>/<relative>`` and emits exactly one WARNING log line.
    Never raises — callers can use the result directly without try/except.

    Thread-safe and idempotent: repeated calls with the same primary path
    return the same resolved path without re-probing.
    """
    if _env_disabled():
        return primary

    key = str(primary)
    with _LOCK:
        cached = _RESOLVED.get(key)
        if cached is not None:
            return cached

        if _is_writable(primary):
            _RESOLVED[key] = primary
            return primary

        fallback = _derive_fallback(primary)
        try:
            fallback_dir = fallback.parent if fallback.suffix else fallback
            fallback_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[SandboxPaths] Fallback mkdir failed for %s -> %s: %s",
                primary, fallback, exc,
            )
        logger.warning(
            "[SandboxPaths] Redirecting %s -> %s (primary not writable; "
            "Iron Gate active)",
            primary, fallback,
        )
        _RESOLVED[key] = fallback
        return fallback


def reset_cache() -> None:
    """Clear the resolution cache. For tests only."""
    with _LOCK:
        _RESOLVED.clear()
