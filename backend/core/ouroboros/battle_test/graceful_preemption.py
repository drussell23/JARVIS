"""Graceful Preemption Shield — Spot/SIGTERM anti-corruption matrix.

GCP Spot instances get a **30-second** ``SIGTERM`` notice before forced
termination. The battle-test harness already handles SIGTERM gracefully (sync
partial-``summary.json`` write → shutdown event → async component shutdown), but
that async path can exceed 30s and, more importantly, says nothing about the
**working tree**: if the Ouroboros loop is mid-APPLY (``ChangeEngine`` writing a
source file, or the ``AutoCommitter`` mid-commit) when the SIGKILL lands, the
clone is left with a half-written file or a dangling ``.git/index.lock``.

This module is the SYNCHRONOUS, bounded, corruption-critical front half of the
shield, invoked at the very top of the harness signal handler (before the
existing partial-summary write) so it always completes inside the 30s window:

  1. **Detect** a genuine GCP preemption (metadata server) so the shutdown can be
     tagged ``preempted`` vs. an operator interrupt — purely advisory.
  2. **Halt** the child worker processes (the ``ProcessPoolExecutor`` AST/Oracle
     pool) so no NEW file-touching work starts during teardown.
  3. **Snapshot** any in-flight working-tree changes NON-DESTRUCTIVELY
     (``git stash create -u`` → ``git stash store``) so a partial APPLY is
     recoverable WITHOUT clearing the operator's uncommitted work off disk — the
     snapshot lands as a listable ``[preemption-shield]`` stash entry AND the
     tree stays exactly as it was (a stray ``index.lock`` is cleared first).

     Was ``git stash push -u`` (2026-07-18): ``push`` restores the tree to HEAD
     as an intrinsic side effect, which silently wiped an operator's uncommitted
     + untracked work off disk (recoverable only if they knew to ``git stash
     apply`` the shield entry). ``create``+``store`` preserves the tree in place
     — the data-loss vector is neutralized while recoverability is kept.

Everything is bounded (hard per-step deadlines), gated
(``JARVIS_PREEMPTION_SHIELD_ENABLED``, default true), idempotent (runs once), and
fail-soft (a cleanup step must NEVER raise into the signal path). It deliberately
reuses ``psutil`` (already a harness dependency) and the stdlib ``git`` CLI; it
does NOT reach into the orchestrator op-ledger (same isolation discipline as the
wall-clock watchdog).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import urllib.request
from typing import Optional

_SHIELD_ENV = "JARVIS_PREEMPTION_SHIELD_ENABLED"
_GIT_STASH_ENV = "JARVIS_PREEMPTION_GIT_STASH_ENABLED"
_METADATA_PREEMPTED_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/preempted"
)

# Hard per-step ceilings — the whole shield must fit well inside the 30s Spot
# window with room left for the harness's own partial-summary write + the OS.
_METADATA_TIMEOUT_S = 1.0
_CHILD_TERM_GRACE_S = 3.0
_GIT_STEP_TIMEOUT_S = 8.0

_engaged_once = threading.Lock()
_has_engaged = False


def shield_enabled() -> bool:
    """Master gate (default TRUE). NEVER raises."""
    try:
        return os.environ.get(_SHIELD_ENV, "true").strip().lower() not in (
            "0", "false", "no", "off",
        )
    except Exception:  # noqa: BLE001
        return True


def is_gcp_preemption() -> bool:
    """True iff the GCP metadata server reports this instance is being preempted.

    Probes ``/computeMetadata/v1/instance/preempted`` (returns ``TRUE`` only
    during a Spot preemption). 1s timeout; any failure (not on GCP, no network,
    DNS) → False. Advisory only — the shield runs regardless of the answer."""
    try:
        req = urllib.request.Request(
            _METADATA_PREEMPTED_URL, headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=_METADATA_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8", "replace").strip().upper() == "TRUE"
    except Exception:  # noqa: BLE001 — not on GCP / no metadata / timeout
        return False


def halt_child_workers() -> int:
    """Terminate this process's child workers (the ProcessPoolExecutor / Oracle
    AST pool spawn) so no new file-touching compute starts during teardown.

    SIGTERM → brief grace → SIGKILL stragglers. Returns the count signalled.
    Best-effort + bounded; NEVER raises."""
    try:
        import psutil  # reused harness dependency
    except Exception:  # noqa: BLE001
        return 0
    # Orderly drain FIRST (2026-07-18): registered ProcessPoolExecutors
    # shut down gracefully so their workers exit clean and the
    # multiprocessing resource_tracker unregisters each semaphore
    # exactly once — the KeyError-wall class dies at its source.
    try:
        from backend.core.ouroboros.governance.executor_registry import (
            shutdown_all as _drain_pools,
        )
        _drain_pools()
    except Exception:  # noqa: BLE001
        pass
    try:
        me = psutil.Process()
        kids = me.children(recursive=True)

        def _is_resource_tracker(proc) -> bool:
            # NEVER kill multiprocessing's janitor: killing it forces a
            # relaunch with an empty registry, and every later
            # unregister prints a raw KeyError traceback to stderr.
            # It exits on its own once its pipe closes at process end.
            try:
                return any(
                    "resource_tracker" in part for part in proc.cmdline()
                )
            except Exception:  # noqa: BLE001
                return False

        targets = [c for c in kids if not _is_resource_tracker(c)]
        for c in targets:
            try:
                c.terminate()
            except Exception:  # noqa: BLE001
                pass
        _, alive = psutil.wait_procs(targets, timeout=_CHILD_TERM_GRACE_S)
        for c in alive:
            try:
                c.kill()
            except Exception:  # noqa: BLE001
                pass
        return len(targets)
    except Exception:  # noqa: BLE001
        return 0


def _run_git(repo_root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True, text=True, timeout=_GIT_STEP_TIMEOUT_S,
    )


def _detect_repo_root() -> Optional[str]:
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=_GIT_STEP_TIMEOUT_S,
        )
        root = cp.stdout.strip()
        return root or None
    except Exception:  # noqa: BLE001
        return None


#: Private ref namespace for preemption snapshots. Deliberately NOT ``refs/stash``
#: — see the note in :func:`git_safety_stash`. Listable via
#: ``git for-each-ref refs/jarvis/preemption``; each ref points at a
#: ``git stash create`` commit that ``git stash apply <sha>`` restores.
_PREEMPTION_REF_NS = "refs/jarvis/preemption"


def list_preemption_refs(repo_root: str) -> list:
    """``[(refname, sha), ...]`` newest-first for the private namespace. The
    replacement for ``git stash list`` as the operator's recovery surface.
    Fail-soft: returns ``[]`` on any error."""
    try:
        res = _run_git(
            repo_root, "for-each-ref", "--sort=-refname",
            "--format=%(refname) %(objectname)", _PREEMPTION_REF_NS,
        )
        if res.returncode != 0:
            return []
        out = []
        for line in (res.stdout or "").splitlines():
            parts = line.strip().split()
            if len(parts) == 2:
                out.append((parts[0], parts[1]))
        return out
    except Exception:  # noqa: BLE001
        return []


def _prune_preemption_refs(repo_root: str) -> int:
    """Retain the newest ``JARVIS_PREEMPTION_SNAPSHOT_RETAIN`` (default 20)
    snapshots; delete older refs so the namespace cannot grow without bound
    across a long-lived daemon. Deleting the ref only unpins the commit (GC
    reclaims it later) — it never touches the working tree. Returns the count
    pruned. Fail-soft; NEVER raises."""
    try:
        keep = int(os.environ.get("JARVIS_PREEMPTION_SNAPSHOT_RETAIN", "20"))
    except (TypeError, ValueError):
        keep = 20
    keep = max(1, keep)
    pruned = 0
    try:
        refs = list_preemption_refs(repo_root)
        for refname, _sha in refs[keep:]:
            res = _run_git(repo_root, "update-ref", "-d", refname)
            if res.returncode == 0:
                pruned += 1
    except Exception:  # noqa: BLE001
        pass
    return pruned


def git_safety_stash(repo_root: Optional[str] = None) -> str:
    """NON-DESTRUCTIVELY snapshot in-flight working-tree changes so a partial
    APPLY is recoverable WITHOUT clearing the operator's uncommitted work off
    disk. Clears a stray ``.git/index.lock`` first (a crashed git op leaves one,
    which would block the snapshot). Returns a short status string for telemetry
    (``snapshot:<sha8>`` on success).

    Mechanism (2026-07-18): ``git stash create -u`` writes the dirty + untracked
    delta to a dangling commit and returns its SHA **without touching the working
    tree**; ``git stash store`` then registers that SHA as a listable
    ``[preemption-shield]`` stash entry (still without touching the tree). So the
    snapshot is recoverable via ``git stash list`` / ``apply`` AND the tree is
    left exactly as it was. This replaces ``git stash push -u``, whose intrinsic
    reset-to-HEAD side effect silently wiped uncommitted + untracked work off
    disk. DRY: the ``create -u`` half reuses ``workspace_checkpoint.create_stash_ref``
    (the same helper the FSM atomic-workspace checkpoint uses). Bounded +
    fail-soft; NEVER raises."""
    if os.environ.get(_GIT_STASH_ENV, "true").strip().lower() in ("0", "false", "no", "off"):
        return "stash_disabled"
    try:
        root = repo_root or _detect_repo_root()
        if not root:
            return "no_repo"
        # Clear a stale lock from a git op interrupted by an earlier signal.
        lock = os.path.join(root, ".git", "index.lock")
        try:
            if os.path.isfile(lock):
                os.remove(lock)
        except Exception:  # noqa: BLE001
            pass
        status = _run_git(root, "status", "--porcelain")
        if status.returncode != 0:
            return f"status_failed:{(status.stderr or '').strip()[:60]}"
        if not status.stdout.strip():
            return "tree_clean"
        # NON-DESTRUCTIVE snapshot: create a dangling stash commit (tree stays put).
        try:
            from backend.core.ouroboros.governance.workspace_checkpoint import (
                create_stash_ref,
            )
            sha = create_stash_ref(root)
        except Exception:  # noqa: BLE001 — helper unavailable → degrade to no-snapshot
            sha = None
        if not sha:
            # Tree raced clean, or git could not snapshot — never clear the tree.
            return "snapshot_none"
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        # ── Hazard #70033: NEVER touch the shared stash stack ──────────────
        # `git stash store` pushes onto refs/stash — a SHARED, ORDER-SENSITIVE
        # stack. A human or agent running `git stash pop` gets stash@{0}, so a
        # daemon snapshot landing between their push and their pop silently
        # hands them the DAEMON's tree. Observed 2026-07-24: an agent's pop
        # returned a preemption snapshot and conflicted.
        #
        # The tree was never the problem (create+store are both non-destructive);
        # the shared NAMESPACE was. Anchor snapshots under a private ref
        # namespace instead: the dangling commit is preserved from GC exactly as
        # before, recovery still works (apply_stash_ref takes a RAW SHA and never
        # consults the stack), and refs/stash is left entirely to humans.
        ref = f"{_PREEMPTION_REF_NS}/{ts}-{sha[:8]}"
        store = _run_git(root, "update-ref", ref, sha)
        if store.returncode == 0:
            _prune_preemption_refs(root)
            return f"snapshot:{sha[:8]}"
        # update-ref failed, but the dangling `create` commit still exists + the
        # tree is intact — recovery is still possible by SHA; report it.
        return f"snapshot_unstored:{sha[:8]}"
    except subprocess.TimeoutExpired:
        return "git_timeout"
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}"


def engage(signal_name: Optional[str] = None, repo_root: Optional[str] = None) -> dict:
    """Run the full synchronous shield once. Idempotent (subsequent calls no-op
    with ``{"skipped": "already_engaged"}``). Gated + fail-soft — returns a
    telemetry dict and NEVER raises into the signal handler.

    Order is corruption-first: git-safety BEFORE the (slower) child-halt, so the
    tree is protected even if the halt eats into the budget."""
    global _has_engaged
    if not shield_enabled():
        return {"skipped": "shield_disabled"}
    with _engaged_once:
        if _has_engaged:
            return {"skipped": "already_engaged"}
        _has_engaged = True
    started = time.monotonic()
    preempted = is_gcp_preemption()
    stash = git_safety_stash(repo_root)
    halted = halt_child_workers()
    elapsed = time.monotonic() - started
    result = {
        "signal": signal_name or "?",
        "gcp_preemption": preempted,
        "git_safety": stash,
        "children_halted": halted,
        "elapsed_s": round(elapsed, 3),
    }
    try:
        print(
            f"[PreemptionShield] engaged signal={result['signal']} "
            f"preempted={preempted} git_safety={stash} children_halted={halted} "
            f"elapsed={result['elapsed_s']}s",
            flush=True,
        )
    except Exception:  # noqa: BLE001
        pass
    return result


def _reset_for_tests() -> None:
    """Test hook: clear the idempotency latch."""
    global _has_engaged
    with _engaged_once:
        _has_engaged = False


__all__ = [
    "shield_enabled",
    "is_gcp_preemption",
    "halt_child_workers",
    "git_safety_stash",
    "engage",
]
