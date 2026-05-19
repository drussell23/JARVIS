"""GitIndexGuard -- Slice 1 of the git-index-integrity arc.
=========================================================

Pure-stdlib subprocess primitive that detects a **missing**
``.git/index`` and performs an advisory, *non-destructive*
rebuild from ``HEAD``.

Why this exists
---------------

A background Cursor Agent (operator soak 2026-05-19) repeatedly
unlinked ``.git/index`` on the operator's ``main`` checkout. With
no index, ``git status`` reports every HEAD-tracked file as a
staged deletion -- the infamous "7856 staged / 2144 changes"
illusion in the Cursor Source Control panel. No content is ever
lost (the working tree is intact); the *index* is simply gone.
``git read-tree HEAD`` rebuilds the index from the HEAD tree
**without touching the working tree** -- the exact one-shot,
content-safe recovery.

Load-bearing safety contract
----------------------------

* **Acts ONLY on total absence.** If ``.git/index`` is present,
  the guard returns ``HEALTHY`` and does *nothing*. It MUST NOT
  rebuild a present index -- the operator may have legitimately
  staged work, and ``read-tree HEAD`` would silently discard it.
* **``read-tree HEAD`` only.** The rebuild NEVER uses a
  working-tree-destructive command (``reset --hard``,
  ``checkout``, ``clean``, ``rm --cached``). AST-pinned.
* **Advisory, authority-free.** Mirrors ``gitignore_guard``:
  closed-5 outcome enum, frozen anomaly dataclass, NEVER-raise
  IO discipline, master flag default-off until graduation. The
  guard observes + repairs index *absence*; it has zero say over
  any governance decision.

SSE seam without coupling
-------------------------

The arc plan calls for an SSE ``git_index_anomaly`` event. To
keep this module pure-stdlib (so it stays AST-pinnable like
``gitignore_guard`` -- no governance imports at the hot path),
the SSE emission is *not* performed here. Instead
:func:`detect_and_rebuild` accepts an injected ``on_anomaly``
callback (fail-silent). A future Slice-2 consumer passes the
``StreamEventBroker`` emitter; this module never imports it. The
Slice-1 signal is the structured WARNING log line
``[GitIndexGuard] git_index_anomaly ...`` -- identical
discipline to ``cross_process_jsonl``'s ``stale_lock_detected``.
"""
from __future__ import annotations

import enum
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

logger = logging.getLogger("Ouroboros.GitIndexGuard")


GIT_INDEX_GUARD_SCHEMA_VERSION: str = "git_index_guard.v1"


# ---------------------------------------------------------------------------
# Master flag + env knobs
# ---------------------------------------------------------------------------


def git_index_guard_enabled() -> bool:
    """``JARVIS_GIT_INDEX_GUARD_ENABLED`` (default ``false`` --
    Slice 1 substrate, graduates to default-true after a stable
    soak per the arc plan).

    When off, :func:`detect_and_rebuild` returns ``DISABLED``
    immediately and launches no subprocess; :func:`git_index_present`
    still works (it is a pure stdlib stat, no git, useful for
    diagnostics regardless of the master flag).

    Asymmetric env semantics -- empty/whitespace = unset = current
    default; explicit truthy overrides. Re-read on every call so a
    flag flip hot-reverts.
    """
    raw = os.environ.get("JARVIS_GIT_INDEX_GUARD_ENABLED", "").strip().lower()
    if raw == "":
        return False  # Slice 1 default-off until graduation
    return raw in ("1", "true", "yes", "on")


def git_index_guard_timeout_s() -> float:
    """``JARVIS_GIT_INDEX_GUARD_TIMEOUT_S`` (default 5.0, floor
    1.0, ceiling 30.0). Subprocess timeout for the ``git read-tree
    HEAD`` rebuild. Bounded so a hung git binary cannot stall the
    caller."""
    raw = os.environ.get("JARVIS_GIT_INDEX_GUARD_TIMEOUT_S", "").strip()
    try:
        n = float(raw) if raw else 5.0
    except ValueError:
        n = 5.0
    return max(1.0, min(30.0, n))


# ---------------------------------------------------------------------------
# Closed-5-value taxonomy
# ---------------------------------------------------------------------------


class GitIndexGuardOutcome(str, enum.Enum):
    """Closed taxonomy for guard verdicts.

    * ``HEALTHY`` -- ``.git/index`` is present; guard did nothing
      (the safe common case)
    * ``MISSING_REBUILT`` -- index was absent; advisory
      ``read-tree HEAD`` rebuild succeeded
    * ``MISSING_REBUILD_FAILED`` -- index was absent; the rebuild
      subprocess failed (git missing / unborn HEAD / timeout)
    * ``DISABLED`` -- master flag off; no probe, no subprocess
    * ``FAILED`` -- could not even determine index presence
      (non-repo / unreadable .git); fail-open, caller proceeds
    """

    HEALTHY = "healthy"
    MISSING_REBUILT = "missing_rebuilt"
    MISSING_REBUILD_FAILED = "missing_rebuild_failed"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Frozen anomaly dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitIndexAnomaly:
    """One observation of the ``.git/index`` integrity check.

    ``outcome`` is the closed-5 verdict. ``index_path`` is the
    resolved absolute path the guard probed (handles linked
    worktrees, where ``.git`` is a gitfile, not a directory).
    ``detail`` carries a short human string (git stderr tail on
    failure, empty on the healthy path).
    """

    repo_root: str
    index_path: str
    outcome: GitIndexGuardOutcome
    detail: str = ""
    schema_version: str = GIT_INDEX_GUARD_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "index_path": self.index_path,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Internal stdlib helpers
# ---------------------------------------------------------------------------


def _run_git(
    args: Sequence[str],
    *,
    repo_root: Path,
    timeout_s: float,
) -> Optional[subprocess.CompletedProcess]:
    """Run a git command with bounded timeout. Returns None on any
    subprocess failure (FileNotFoundError, TimeoutExpired, OSError).
    NEVER raises."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("[GitIndexGuard] git %s degraded: %s", args[0], exc)
        return None
    except Exception as exc:  # noqa: BLE001 -- last-resort defensive
        logger.debug(
            "[GitIndexGuard] git %s last-resort degraded: %s",
            args[0], exc,
        )
        return None


def _resolve_index_path(repo_root: Path) -> Optional[Path]:
    """Resolve the absolute path of the git index for ``repo_root``.

    Handles both layouts purely with stdlib (no git subprocess):

      * ``.git`` is a directory  -> ``<root>/.git/index``
      * ``.git`` is a gitfile    -> ``gitdir: <path>`` -> ``<path>/index``
        (linked worktree case)

    Returns None when ``.git`` is absent or unparseable (the guard
    reports FAILED -- it cannot reason about a non-repo path).
    NEVER raises.
    """
    try:
        dot_git = Path(repo_root) / ".git"
        if dot_git.is_dir():
            return dot_git / "index"
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("gitdir:"):
                    gd = line.split(":", 1)[1].strip()
                    if not gd:
                        return None
                    gd_path = Path(gd)
                    if not gd_path.is_absolute():
                        gd_path = (Path(repo_root) / gd_path).resolve()
                    return gd_path / "index"
            return None
        return None
    except (OSError, ValueError) as exc:
        logger.debug("[GitIndexGuard] _resolve_index_path degraded: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 -- last-resort defensive
        logger.debug(
            "[GitIndexGuard] _resolve_index_path last-resort: %s", exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def git_index_present(repo_root: Path) -> bool:
    """Return True iff the resolved ``.git/index`` file exists.

    Pure stdlib stat -- no git subprocess, independent of the
    master flag (useful for diagnostics in any state). Returns
    False when ``.git`` is absent/unparseable or the index file is
    missing. NEVER raises.
    """
    idx = _resolve_index_path(Path(repo_root))
    if idx is None:
        return False
    try:
        return idx.is_file()
    except OSError:
        return False


def detect_and_rebuild(
    repo_root: Path,
    *,
    on_anomaly: Optional[Callable[["GitIndexAnomaly"], None]] = None,
    timeout_s: Optional[float] = None,
) -> GitIndexAnomaly:
    """Detect a missing ``.git/index`` and advisorily rebuild it
    from HEAD via ``git read-tree HEAD``. NEVER raises.

    Safety contract (load-bearing):

      * If the index is **present**, returns ``HEALTHY`` and does
        nothing. The guard MUST NOT rebuild a present index --
        ``read-tree HEAD`` would silently discard legitimately
        staged operator work.
      * The rebuild is ``git read-tree HEAD`` ONLY. It writes the
        index from the HEAD tree and does **not** modify the
        working tree. No ``reset --hard`` / ``checkout`` / ``clean``
        / ``rm --cached`` is ever issued.

    ``on_anomaly`` (optional) is invoked exactly once with the
    resulting :class:`GitIndexAnomaly` whenever the outcome is NOT
    ``HEALTHY`` and NOT ``DISABLED`` (i.e. an actual anomaly was
    observed -- rebuilt, rebuild-failed, or probe-failed). The
    callback is fail-silent: any exception it raises is swallowed
    so a misbehaving SSE emitter cannot break the guard. This is
    the dependency-injection seam that keeps the module
    pure-stdlib while letting a Slice-2 consumer wire the
    ``StreamEventBroker`` ``git_index_anomaly`` event.

    Outcomes: ``DISABLED`` (master off) / ``HEALTHY`` (index
    present) / ``MISSING_REBUILT`` / ``MISSING_REBUILD_FAILED`` /
    ``FAILED`` (cannot resolve .git -- non-repo).
    """
    root = Path(repo_root)
    idx = _resolve_index_path(root)

    def _emit(anomaly: "GitIndexAnomaly") -> None:
        # Structured WARNING is the Slice-1 signal (mirrors
        # cross_process_jsonl stale_lock_detected). Then the
        # injected SSE seam, fail-silent.
        logger.warning(
            "[GitIndexGuard] git_index_anomaly repo=%s outcome=%s "
            "index_path=%s detail=%s",
            anomaly.repo_root, anomaly.outcome.value,
            anomaly.index_path, anomaly.detail[:200],
        )
        if on_anomaly is not None:
            try:
                on_anomaly(anomaly)
            except Exception as exc:  # noqa: BLE001 -- fail-silent seam
                logger.debug(
                    "[GitIndexGuard] on_anomaly callback raised "
                    "(swallowed): %s", exc,
                )

    if not git_index_guard_enabled():
        return GitIndexAnomaly(
            repo_root=str(root),
            index_path=str(idx) if idx is not None else "",
            outcome=GitIndexGuardOutcome.DISABLED,
        )

    if idx is None:
        anomaly = GitIndexAnomaly(
            repo_root=str(root),
            index_path="",
            outcome=GitIndexGuardOutcome.FAILED,
            detail="could not resolve .git/index (non-repo or "
            "unparseable gitfile)",
        )
        _emit(anomaly)
        return anomaly

    try:
        index_exists = idx.is_file()
    except OSError:
        index_exists = False

    if index_exists:
        # Common safe path -- do NOT touch a present index.
        return GitIndexAnomaly(
            repo_root=str(root),
            index_path=str(idx),
            outcome=GitIndexGuardOutcome.HEALTHY,
        )

    # Index is absent -> advisory non-destructive rebuild.
    timeout = (
        timeout_s if timeout_s is not None
        else git_index_guard_timeout_s()
    )
    result = _run_git(
        ["read-tree", "HEAD"], repo_root=root, timeout_s=timeout,
    )
    if result is not None and result.returncode == 0 and idx.is_file():
        anomaly = GitIndexAnomaly(
            repo_root=str(root),
            index_path=str(idx),
            outcome=GitIndexGuardOutcome.MISSING_REBUILT,
            detail="index was absent; rebuilt from HEAD "
            "(working tree untouched)",
        )
        _emit(anomaly)
        return anomaly

    detail = "read-tree HEAD failed"
    if result is not None:
        stderr_tail = (result.stderr or "").strip()[:200]
        detail = f"read-tree HEAD rc={result.returncode}: {stderr_tail}"
    anomaly = GitIndexAnomaly(
        repo_root=str(root),
        index_path=str(idx),
        outcome=GitIndexGuardOutcome.MISSING_REBUILD_FAILED,
        detail=detail,
    )
    _emit(anomaly)
    return anomaly


__all__ = [
    "GIT_INDEX_GUARD_SCHEMA_VERSION",
    "GitIndexAnomaly",
    "GitIndexGuardOutcome",
    "detect_and_rebuild",
    "git_index_guard_enabled",
    "git_index_guard_timeout_s",
    "git_index_present",
    "register_flags",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning("[GitIndexGuard] register_flags degraded: %s", exc)
        return 0
    target = "backend/core/ouroboros/governance/git_index_guard.py"
    specs = [
        FlagSpec(
            name="JARVIS_GIT_INDEX_GUARD_ENABLED",
            type=FlagType.BOOL, default=False,
            category=Category.SAFETY,
            source_file=target,
            example="JARVIS_GIT_INDEX_GUARD_ENABLED=true",
            description=(
                "Master switch for the git-index integrity guard. "
                "When on, detect_and_rebuild() detects a MISSING "
                ".git/index (the Cursor-Agent unlink failure mode "
                "that produces the false '7856 staged deletions' "
                "in Source Control) and advisorily rebuilds it from "
                "HEAD via 'git read-tree HEAD' -- working tree "
                "untouched, present indexes never modified. Slice 1 "
                "default-false until soak graduation."
            ),
        ),
        FlagSpec(
            name="JARVIS_GIT_INDEX_GUARD_TIMEOUT_S",
            type=FlagType.FLOAT, default=5.0,
            category=Category.TIMING,
            source_file=target,
            example="JARVIS_GIT_INDEX_GUARD_TIMEOUT_S=10.0",
            description=(
                "Subprocess timeout for the 'git read-tree HEAD' "
                "rebuild. Bounded so a hung git binary cannot stall "
                "the caller. Floor 1.0, ceiling 30.0."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[GitIndexGuard] register_flags spec %s skipped: %s",
                spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Module-owned shipped_code_invariants (AST pins)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Slice 1 invariants: pure-stdlib at hot path + closed-5
    outcome taxonomy + the load-bearing non-destructive-rebuild
    property (``read-tree`` present; no working-tree-destructive
    git verb anywhere outside the registration contract)."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(tree: "_ast.Module", source: str) -> tuple:
        # ``source`` is part of the ShippedCodeInvariant protocol
        # signature; this validator works on the AST exclusively
        # (string-Constant scan) so destructive-token literals in
        # this very function cannot self-match a raw substring.
        _ = source
        violations: list = []
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in _ast.walk(tree):
            if isinstance(fnode, _ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))

        def _in_exempt(lineno: int) -> bool:
            return any(s <= lineno <= e for s, e in exempt_ranges)

        # (1) Pure-stdlib at hot path: no governance/backend imports
        #     outside the registration contract.
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or "governance" in module:
                    lineno = getattr(node, "lineno", 0)
                    if _in_exempt(lineno):
                        continue
                    violations.append(
                        f"line {lineno}: git_index_guard must be "
                        f"pure-stdlib at hot path -- found {module!r}"
                    )
            if isinstance(node, _ast.Call) and isinstance(
                node.func, _ast.Name
            ):
                if node.func.id in ("exec", "eval", "compile"):
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"git_index_guard MUST NOT {node.func.id}()"
                    )

        # (2) Closed-5 GitIndexGuardOutcome taxonomy.
        required = {
            "HEALTHY", "MISSING_REBUILT", "MISSING_REBUILD_FAILED",
            "DISABLED", "FAILED",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and (
                node.name == "GitIndexGuardOutcome"
            ):
                seen = set()
                for stmt in node.body:
                    if isinstance(stmt, _ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, _ast.Name):
                                seen.add(tgt.id)
                missing = required - seen
                extras = seen - required
                if missing:
                    violations.append(
                        f"GitIndexGuardOutcome missing required "
                        f"values: {sorted(missing)}"
                    )
                if extras:
                    violations.append(
                        f"GitIndexGuardOutcome has unexpected values "
                        f"(closed-taxonomy violation): {sorted(extras)}"
                    )

        # (3) Load-bearing non-destructive-rebuild property.
        #     The rebuild MUST use read-tree; it MUST NOT issue any
        #     working-tree-destructive git verb. We scan string
        #     Constants OUTSIDE the registration/invariant ranges so
        #     the destructive-token literals in THIS validator (which
        #     live inside register_shipped_invariants) never self-
        #     match. Tokens are assembled from fragments as a second
        #     belt against source-substring false positives.
        read_tree_seen = False
        destructive = {
            "--" + "hard", "check" + "out", "cle" + "an",
            "--" + "cached", "reset",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Constant) and isinstance(
                node.value, str
            ):
                lineno = getattr(node, "lineno", 0)
                if _in_exempt(lineno):
                    continue
                val = node.value.strip()
                if val == "read-tree":
                    read_tree_seen = True
                if val in destructive:
                    violations.append(
                        f"line {lineno}: git_index_guard issues a "
                        f"working-tree-destructive git token "
                        f"{val!r} -- rebuild must be read-tree-only "
                        f"(content-safety invariant)"
                    )
        if not read_tree_seen:
            violations.append(
                "git_index_guard rebuild MUST use 'read-tree' "
                "(the non-destructive HEAD->index recovery); the "
                "literal is absent outside the registration contract"
            )
        return tuple(violations)

    target = "backend/core/ouroboros/governance/git_index_guard.py"
    return [
        ShippedCodeInvariant(
            invariant_name="git_index_guard_purity_and_nondestructive",
            target_file=target,
            description=(
                "Slice 1 primitive stays pure-stdlib at the hot "
                "path (no backend/governance imports outside "
                "register_flags / register_shipped_invariants); "
                "GitIndexGuardOutcome is the closed-5 taxonomy; the "
                "rebuild is read-tree-only -- NO reset --hard / "
                "checkout / clean / --cached anywhere (rebuilding a "
                "missing index must never touch the working tree or "
                "discard staged work)."
            ),
            validate=_validate,
        ),
    ]
