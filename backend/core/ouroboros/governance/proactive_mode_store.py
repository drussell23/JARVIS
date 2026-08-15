"""proactive_mode_store — the dial remembers this checkout. PRD §30.11 Q4.

"This repository is one I let run" is a standing judgement about a
particular working tree, and it is exactly the shape ``UserPreferenceMemory``
already stores for other operator judgements. It does not belong in an
environment variable, which every process inherits and no operator can see,
and it does not belong in the branch, which would carry one person's trust
decision to everyone who checks it out.

THE WORKTREE ILLUSION
---------------------
``git worktree`` gives several directories one ``.git`` backend. Keying the
dial on the git directory would make every worktree share a single position,
so an operator running ``watch`` in a review checkout would silently loosen
the moment a colleague set ``safe_auto`` in a feature worktree — two
cockpits, two intentions, one clobbered file.

So the key is the **working tree**, resolved by ``git rev-parse
--show-toplevel``, which returns the worktree path rather than the common
dir. That is worktree-correct by construction rather than by special-casing:
there is no branch here that says "if a worktree, then…", because the
question was asked in the coordinate system where the answer is already
right.

WHY DEGRADATION IS ALWAYS TO ``watch``
--------------------------------------
Every failure path here — no repository, unreadable state, read-only mount,
a file held by a zombie — ends in ephemeral ``watch``. That is the only
direction that is safe to be wrong in. Degrading to the *last known* dial
would let a filesystem fault re-arm an organism the operator had stood down;
degrading to ``safe_auto`` would let a permissions error grant autonomy.
Failing toward "narrates, initiates nothing" costs an operator one keystroke
and cannot cost them a mutation they did not sanction.

This is the one place in the codebase that deliberately fails CLOSED rather
than open. ``local_model_admission`` fails open because a broken probe must
not disable Tier 2 for a session; the dial fails closed because a broken
disk must not grant authority. The difference is which way the error points.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.ProactiveModeStore")

PROACTIVE_MODE_STORE_SCHEMA_VERSION: str = "proactive_mode_store.1"

STATE_FILENAME = "proactive_mode.json"


def persistence_enabled() -> bool:
    """§30.11 Q4. Default TRUE — the dial remembers this checkout."""
    return (os.environ.get("JARVIS_PROACTIVE_MODE_PERSIST", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def first_contact_default() -> str:
    """§30.11 Q1. The rung with nothing persisted: ``watch``.

    Deliberately NOT the same thing as :func:`proactive_mode.position`'s
    parse fallback. "I cannot read this name" and "nobody has chosen yet"
    are different states, and collapsing them would make an unparseable
    string mean the operator had asked for zero-touch observation.

    ``watch`` because first contact with an unfamiliar checkout is the case
    where the organism knows least and the operator has vouched for nothing.
    §27.4.1.1 bounds a countdown to read-only work, which makes ``explore``
    defensible — but a repository the operator has not looked at should not
    spend tokens before they do.
    """
    return (os.environ.get("JARVIS_PROACTIVE_MODE_FIRST_CONTACT", "watch")
            or "watch").strip().lower()


def resolve_timeout_s() -> float:
    try:
        raw = os.environ.get("JARVIS_PROACTIVE_MODE_RESOLVE_TIMEOUT_S", "")
        return max(0.1, float(raw)) if raw.strip() else 3.0
    except (TypeError, ValueError):
        return 3.0


@dataclass(frozen=True)
class StoreLocation:
    """Where this checkout's dial lives, and whether it can be written."""

    worktree: Optional[Path]
    path: Optional[Path]
    writable: bool
    reason: str

    @property
    def persistent(self) -> bool:
        return self.path is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": PROACTIVE_MODE_STORE_SCHEMA_VERSION,
            "worktree": str(self.worktree) if self.worktree else None,
            "path": str(self.path) if self.path else None,
            "writable": self.writable, "reason": self.reason,
        }


async def _git_toplevel(cwd: Path) -> Optional[Path]:
    """The WORKING TREE root, or None. Async and bounded. NEVER raises.

    ``--show-toplevel`` rather than ``--git-dir`` is the whole worktree fix:
    the former is per-worktree, the latter is shared. Run as a subprocess
    off the loop with an explicit wait bound, because a git call against a
    dead network mount can hang for as long as the mount's own timeout.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--show-toplevel",
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=resolve_timeout_s())
    except (asyncio.TimeoutError, OSError, ValueError):
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        return None
    if proc.returncode != 0:
        return None
    text = (out or b"").decode(errors="replace").strip()
    return Path(text) if text else None


async def locate(cwd: Optional[Path] = None) -> StoreLocation:
    """Where the dial should persist for the current context. NEVER raises.

    Outside a tracked repository the location is non-persistent, and the
    caller degrades to in-memory ``watch``. That is not an error state — a
    scratch directory has no checkout to remember a judgement about.
    """
    if not persistence_enabled():
        return StoreLocation(None, None, False, "persistence disabled")
    base = Path(cwd) if cwd else Path(
        os.environ.get("JARVIS_PROJECT_ROOT", "") or os.getcwd())
    try:
        # DRY: the execution root is the canonical seam for "which tree is
        # authoritative for mutation", and the dial governs mutation. Asking
        # a second question here would let the two disagree about which tree
        # the operator is steering.
        from backend.core.ouroboros.governance.autonomous_workspace import (
            effective_execution_root,
        )
        base = Path(effective_execution_root(base))
    except Exception:  # noqa: BLE001 — the seam is advisory for locating
        pass
    top = await _git_toplevel(base)
    if top is None:
        return StoreLocation(None, None, False, "not a git working tree")
    path = top / ".jarvis" / STATE_FILENAME
    writable, reason = await asyncio.to_thread(_probe_writable, path)
    return StoreLocation(top, path if writable else path, writable, reason)


def _probe_writable(path: Path) -> Any:
    """(writable, reason). Probes by ACTING, never by inspecting.

    ``os.access`` answers a question about permission bits, and the failures
    that matter here are not permission bits: a read-only MOUNT, a full
    disk, an immutable flag, a directory held by a zombie. The only reliable
    test of "can I write here" is a write, so the probe creates and removes
    a temp beside the target.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / f".{STATE_FILENAME}.{os.getpid()}.probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True, "writable"
    except PermissionError as exc:
        return False, f"permission denied: {exc.strerror or exc}"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc.strerror or exc}"


class ProactiveModeStore:
    """Loads and saves the dial for one working tree. Thread-safe.

    Holds no dial of its own — it reads and writes a name. The controller
    remains the only thing that decides a rung, so a store that could not
    reach disk degrades the PERSISTENCE and never the authority.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._location: Optional[StoreLocation] = None
        self._degraded: str = ""

    async def hydrate(self, cwd: Optional[Path] = None) -> str:
        """The rung this checkout last recorded, or the first-contact default.

        Every failure path returns :func:`first_contact_default` — see the
        module docstring for why that direction is the only safe one.
        """
        loc = await locate(cwd)
        with self._lock:
            self._location = loc
            self._degraded = "" if loc.writable else loc.reason
        if not loc.persistent:
            logger.info(
                "[ProactiveMode] no persistent dial (%s) — first-contact "
                "default %s", loc.reason, first_contact_default())
            return first_contact_default()
        try:
            data = json.loads(loc.path.read_text(encoding="utf-8"))
            name = str((data or {}).get("position") or "").strip().lower()
        except FileNotFoundError:
            logger.info(
                "[ProactiveMode] first contact with %s — starting at %s",
                loc.worktree, first_contact_default())
            return first_contact_default()
        except (OSError, ValueError) as exc:
            # Unreadable is NOT "no preference": a corrupt file means the
            # last judgement is unknown, and an unknown judgement must not
            # be resolved in the organism's favour.
            logger.warning(
                "[ProactiveMode] dial state unreadable (%s) — falling back "
                "to %s", exc, first_contact_default())
            return first_contact_default()
        from backend.core.ouroboros.governance.proactive_mode import _BY_NAME
        if name not in _BY_NAME:
            logger.warning(
                "[ProactiveMode] persisted rung %r is not on the ladder — "
                "falling back to %s", name, first_contact_default())
            return first_contact_default()
        logger.info("[ProactiveMode] restored dial %s for %s",
                    name, loc.worktree)
        return name

    async def remember(self, position_name: str) -> bool:
        """Record the dial for this checkout. True when it reached disk.

        A failure is logged ONCE per degradation and then held, because a
        read-only mount does not heal between keystrokes and an operator
        cycling the dial should not receive one warning per press.
        """
        with self._lock:
            loc = self._location
        if loc is None:
            loc = await locate()
            with self._lock:
                self._location = loc
        if not loc.persistent or not loc.writable:
            self._note_degraded(loc.reason)
            return False
        payload = {
            "schema_version": PROACTIVE_MODE_STORE_SCHEMA_VERSION,
            "position": str(position_name),
            "worktree": str(loc.worktree),
        }
        try:
            await asyncio.to_thread(_write_atomic, loc.path, payload)
            with self._lock:
                self._degraded = ""
            return True
        except (PermissionError, OSError) as exc:
            self._note_degraded(f"{type(exc).__name__}: {exc}")
            return False

    def _note_degraded(self, reason: str) -> None:
        with self._lock:
            first = self._degraded != reason
            self._degraded = reason
        if first:
            logger.warning(
                "[ProactiveMode] dial cannot persist (%s) — the position "
                "holds for this session and will not survive a restart",
                reason)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            loc = self._location
            degraded = self._degraded
        out = loc.to_dict() if loc else {"path": None, "writable": False}
        out["degraded"] = degraded
        out["first_contact_default"] = first_contact_default()
        return out


def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Publish the dial durably. Raises for the caller to classify.

    Through ``durable_io.atomic_replace`` so a crash mid-write cannot leave
    a half-written file that the next hydrate reads as corrupt — which
    would silently reset the operator's standing judgement to ``watch``
    and look like the dial forgetting.
    """
    from backend.core.ouroboros.governance.durable_io import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                   encoding="utf-8")
    atomic_replace(tmp, path)


_store: Optional[ProactiveModeStore] = None
_store_lock = threading.Lock()


def get_store() -> ProactiveModeStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ProactiveModeStore()
        return _store


def reset_store() -> None:
    global _store
    with _store_lock:
        _store = None
