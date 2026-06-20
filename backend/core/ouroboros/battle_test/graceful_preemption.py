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
  3. **Stash** any in-flight working-tree changes (``git stash -u``) so a partial
     APPLY can't leave a corrupt tree — the work is fully recoverable from the
     stash on the next boot, and a stray ``index.lock`` is cleared first.

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
    try:
        me = psutil.Process()
        kids = me.children(recursive=True)
        for c in kids:
            try:
                c.terminate()
            except Exception:  # noqa: BLE001
                pass
        _, alive = psutil.wait_procs(kids, timeout=_CHILD_TERM_GRACE_S)
        for c in alive:
            try:
                c.kill()
            except Exception:  # noqa: BLE001
                pass
        return len(kids)
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


def git_safety_stash(repo_root: Optional[str] = None) -> str:
    """Stash in-flight working-tree changes so a partial APPLY can't corrupt the
    tree. Clears a stray ``.git/index.lock`` first (a crashed git op leaves one,
    which would block the stash). Returns a short status string for telemetry.

    ``git stash`` (not commit) is deliberate: it leaves a clean tree, pollutes no
    history, and the in-flight work is fully recoverable via ``git stash list``
    on the next boot. Bounded + fail-soft; NEVER raises."""
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
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        res = _run_git(root, "stash", "push", "-u", "-m", f"[preemption-shield] in-flight {ts}")
        if res.returncode == 0:
            return "stashed"
        return f"stash_failed:{(res.stderr or '').strip()[:60]}"
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
