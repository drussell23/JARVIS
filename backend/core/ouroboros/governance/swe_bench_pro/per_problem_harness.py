"""SWE-Bench-Pro per-problem harness substrate — Phase 2 Phase B.1
(PRD §40.7.9).

Prepares a single :class:`ProblemSpec` for evaluation:

  1. **Lazy repo cache** — clone the upstream repo once per
     repo-url, share across all instances of that repo.  Cache
     directory: ``.jarvis/swe_bench_pro/repo_cache/<repo>/``.
     Subsequent problems against the same repo reuse the cached
     clone (just create a new worktree).

  2. **Per-problem worktree** — isolated working tree at
     ``base_commit`` via ``git worktree add -b swebp/<instance_id>
     <wt_path> <base_commit>`` against the cached repo.

  3. **Test patch application** — apply the SWE-Bench ``test_patch``
     diff into the worktree via canonical safe-subprocess
     ``git apply`` (same composition pattern v3.4 production
     wiring uses).

  4. **Diff capture** — after RepairEngine.run() (wired in Phase B.2)
     produces a fix, capture the produced patch via
     ``git diff <base_commit>..HEAD`` in the worktree.

Phase B.1 ships steps 1-3 + the diff-capture primitive.
Step 4's actual orchestration with RepairEngine is Phase B.2.

Composition discipline (mandate compliance)
-------------------------------------------

  * Authority asymmetry: Phase B.1 imports NO policy substrates
    (orchestrator / iron_gate / change_engine / candidate_generator
    / policy_engine / risk_tier).  RepairEngine import deferred
    to Phase B.2.  AST-pinned in spine.
  * Canonical safe-subprocess composition: every git invocation
    uses ``subprocess.run`` with program+args list (NEVER
    ``shell=True``) dispatched via ``asyncio.to_thread`` so the
    blocking subprocess wait runs in a worker thread and NEVER
    competes with the asyncio event loop for scheduling cycles.
    This is load-bearing: the harness boot hook runs alongside
    16 sensors + GovernedLoop + DreamEngine in the same event
    loop, and ``proc.communicate()`` await scheduling can be
    starved under contention even when the subprocess itself
    finished in milliseconds — the v2 wiring used
    ``create_subprocess_exec + wait_for(proc.communicate(), …)``
    which exhibited a 2500× slowdown vs. standalone timing (60s
    timeout firing on a 0.35s real workload during 2026-05-12
    stage-1 wiring-validation soak).
  * No dependency-surface increase: pure stdlib + canonical
    ``ProblemSpec`` from Phase A.  No new pip deps.
  * Lazy + cached: one clone per repo, reused across all instances.

§7 fail-closed contract
-----------------------

Every public surface NEVER raises (``asyncio.CancelledError``
propagates per orchestrator POSTMORTEM convention).

§33.1 graduation contract
-------------------------

Master flag ``JARVIS_SWE_BENCH_PRO_ENABLED`` (defined in
``dataset_loader.py``; shared with Phase A) defaults FALSE.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
    EvaluatorPhase,
    task_phase,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
    swe_bench_pro_enabled,
)


logger = logging.getLogger("Ouroboros.SWEBenchPro.PerProblemHarness")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


PER_PROBLEM_HARNESS_SCHEMA_VERSION: str = "swe_bench_pro_prepared.v1"


REPO_CACHE_PATH_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH"
WORKTREE_BASE_PATH_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH"
GIT_CLONE_TIMEOUT_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_GIT_CLONE_TIMEOUT_S"
GIT_OP_TIMEOUT_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_GIT_OP_TIMEOUT_S"

_DEFAULT_REPO_CACHE_PATH: str = ".jarvis/swe_bench_pro/repo_cache"
_DEFAULT_WORKTREE_BASE_PATH: str = ".jarvis/swe_bench_pro/worktrees"
_DEFAULT_GIT_CLONE_TIMEOUT_S: int = 600  # 10 min
_DEFAULT_GIT_OP_TIMEOUT_S: int = 60      # 1 min

# Branch prefix for per-problem worktrees — distinct from L3
# unit-* + L2 exercise ouroboros/l2-exercise/* branches.
_BRANCH_PREFIX: str = "swebp/"


# ===========================================================================
# Closed taxonomies (AST bytes-pinned)
# ===========================================================================


class HarnessOutcome(str, enum.Enum):
    """Five canonical outcomes for :func:`prepare_problem`."""

    READY = "ready"
    MASTER_FLAG_OFF = "master_flag_off"
    CLONE_FAILED = "clone_failed"
    # Renamed from the legacy CHECKOUT_FAILED misnomer (Phase C): there
    # is no `git checkout` step — the worktree is created via
    # `git worktree add -b <branch> <path> <commit>`. Every failure this
    # covers (invalid base_commit, orphan branch-exists, path nesting,
    # generic) IS a worktree-creation failure. Mirrors the L3
    # `worktree_create_failed:` precedent. Pure diagnostic label — no
    # consumer switches on it (all check `!= READY`).
    WORKTREE_CREATE_FAILED = "worktree_create_failed"
    TEST_PATCH_FAILED = "test_patch_failed"


class DiffCaptureOutcome(str, enum.Enum):
    """Three canonical outcomes for :func:`capture_produced_patch`."""

    CAPTURED = "captured"
    NO_CHANGES = "no_changes"
    CAPTURE_FAILED = "capture_failed"


# ===========================================================================
# Frozen PreparedProblem dataclass (§33.5 symmetric to_dict/from_dict)
# ===========================================================================


@dataclass(frozen=True)
class PreparedProblem:
    """A SWE-Bench-Pro problem prepared for RepairEngine invocation."""

    problem_instance_id: str
    worktree_path: Path
    base_commit: str
    repo_url: str
    branch_name: str
    target_paths: Tuple[str, ...] = ()
    elapsed_s: float = 0.0
    schema_version: str = PER_PROBLEM_HARNESS_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "problem_instance_id": self.problem_instance_id,
            "worktree_path": str(self.worktree_path),
            "base_commit": self.base_commit,
            "repo_url": self.repo_url,
            "branch_name": self.branch_name,
            "target_paths": list(self.target_paths),
            "elapsed_s": self.elapsed_s,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PreparedProblem":
        return cls(
            schema_version=str(payload.get(
                "schema_version", PER_PROBLEM_HARNESS_SCHEMA_VERSION,
            )),
            problem_instance_id=str(payload["problem_instance_id"]),
            worktree_path=Path(str(payload["worktree_path"])),
            base_commit=str(payload["base_commit"]),
            repo_url=str(payload["repo_url"]),
            branch_name=str(payload["branch_name"]),
            target_paths=tuple(payload.get("target_paths", ())),
            elapsed_s=float(payload.get("elapsed_s", 0.0)),
        )


# ===========================================================================
# Env loaders (NEVER raise)
# ===========================================================================


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw))
    except (ValueError, TypeError):
        logger.warning(
            "[SWEBenchPro] invalid %s=%r — using default %d",
            name, raw, default,
        )
        return default


def repo_cache_path() -> Path:
    raw = os.environ.get(REPO_CACHE_PATH_ENV_VAR, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_REPO_CACHE_PATH)


def worktree_base_path() -> Path:
    raw = os.environ.get(WORKTREE_BASE_PATH_ENV_VAR, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_WORKTREE_BASE_PATH)


def git_clone_timeout_s() -> int:
    return _env_int(GIT_CLONE_TIMEOUT_ENV_VAR, _DEFAULT_GIT_CLONE_TIMEOUT_S)


def git_op_timeout_s() -> int:
    return _env_int(GIT_OP_TIMEOUT_ENV_VAR, _DEFAULT_GIT_OP_TIMEOUT_S)


# ===========================================================================
# Path sanitization
# ===========================================================================


def _sanitize_for_filename(value: str) -> str:
    """Convert an identifier to a filesystem-safe basename.
    Pure function; deterministic; NEVER raises."""
    out_chars: List[str] = []
    for ch in value:
        if ch in ("/", "\\", "\x00", ":", " "):
            out_chars.append("_")
        else:
            out_chars.append(ch)
    return "".join(out_chars) or "_unnamed"


# ===========================================================================
# Canonical safe-subprocess wrapper for git commands
# ===========================================================================


async def _run_git(
    args: List[str],
    *,
    cwd: Optional[Path] = None,
    stdin_input: Optional[bytes] = None,
    timeout_s: Optional[int] = None,
) -> Tuple[int, str, str]:
    """Run a git subprocess off the asyncio event loop.

    Returns ``(returncode, stdout, stderr)``.  The blocking
    ``subprocess.run`` invocation is dispatched via
    ``asyncio.to_thread`` so the wait + pipe drain happens on a
    worker thread, NEVER on the asyncio event loop — the harness
    boot hook coexists with 16 sensors + GovernedLoop + DreamEngine
    in the same loop, and pipe-reader coroutine scheduling can be
    starved under that contention even when the subprocess itself
    finished in milliseconds.  The OS-enforced ``subprocess.run``
    timeout (SIGKILL on expiry) replaces the prior
    ``asyncio.wait_for(proc.communicate(), …)`` await whose timer
    was scheduler-dependent.

    Composition discipline preserved: program + args list (NEVER
    shell-string), explicit cwd, explicit timeout, NEVER raises
    (``asyncio.CancelledError`` propagates from ``to_thread``).
    Returns ``(-1, "", "<error>")`` on construction failure or
    timeout (same shape + ``git_timeout_after_Xs`` diagnostic
    format as the v2 wrapper).
    """
    timeout = timeout_s if timeout_s is not None else git_op_timeout_s()

    def _blocking_call() -> Tuple[int, str, str]:
        try:
            result = subprocess.run(
                ["git", *args],
                input=stdin_input,
                capture_output=True,
                cwd=str(cwd) if cwd is not None else None,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return -1, "", f"git_timeout_after_{timeout}s"
        except Exception as exc:  # noqa: BLE001
            return -1, "", f"{type(exc).__name__}: {exc}"
        return (
            result.returncode if result.returncode is not None else -1,
            result.stdout.decode("utf-8", errors="replace") if result.stdout else "",
            result.stderr.decode("utf-8", errors="replace") if result.stderr else "",
        )

    return await asyncio.to_thread(_blocking_call)


# ===========================================================================
# Repo cache — lazy clone, one per upstream URL
# ===========================================================================


def _cached_repo_path_for(repo_url: str) -> Path:
    cleaned = repo_url
    for prefix in ("https://", "http://", "git@", "ssh://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-len(".git")]
    cleaned = cleaned.replace(":", "/")
    return repo_cache_path() / _sanitize_for_filename(cleaned)


async def _ensure_repo_cached(repo_url: str) -> Optional[Path]:
    """Ensure the upstream repo is cloned into the cache.  Returns
    the cache path on success, None on failure.  Idempotent."""
    if not repo_url.strip():
        logger.warning("[SWEBenchPro] empty repo_url — cannot cache")
        return None
    target = _cached_repo_path_for(repo_url)
    try:
        if target.is_dir() and (target / ".git").is_dir():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            shutil.rmtree(str(target), ignore_errors=True)
        # ``--template=`` (empty string) disables template-hook copying
        # from git's global templates directory. SWE-Bench-Pro clones
        # don't need or want pre-commit / commit-msg / etc. hooks —
        # they're benchmark eval substrates, not contributor checkouts.
        # As a side effect this also unblocks restricted environments
        # where git's global templates dir is non-writable (a real
        # failure mode observed in stage-1 wiring soak 2026-05-12:
        # ``fatal: cannot copy '/opt/homebrew/opt/git/share/git-core/
        # templates/hooks/commit-msg.sample' to ...``). The flag is
        # AST-pinned by the spine to prevent drift.
        rc, _stdout, stderr = await _run_git(
            [
                "clone",
                "--filter=blob:none",
                "--template=",  # AST-pinned: benchmark cleanliness
                repo_url,
                str(target),
            ],
            timeout_s=git_clone_timeout_s(),
        )
        if rc != 0:
            logger.warning(
                "[SWEBenchPro] git clone %r failed rc=%d: %s",
                repo_url, rc, stderr.strip()[:200],
            )
            shutil.rmtree(str(target), ignore_errors=True)
            return None
        return target
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning(
            "[SWEBenchPro] _ensure_repo_cached raised for %r",
            repo_url, exc_info=True,
        )
        return None


# ===========================================================================
# Per-problem worktree creation
# ===========================================================================


def _worktree_path_for(instance_id: str) -> Path:
    # ABSOLUTE (Phase C root-cause fix): `git worktree add` runs with
    # cwd=cached_repo, so a RELATIVE base resolves against the cache dir
    # and nests the worktree INSIDE the cache (the bt-2026-05-17-013346
    # root cause: nested worktree → _apply_test_patch ran in an empty
    # cwd → spurious test_patch_failed + orphan-branch cache poisoning).
    # .resolve() anchors it at the process root — the SAME resolution
    # B1 swe_path_preflight + operation_advisor already apply, so all
    # three agree on one absolute location. Idempotent for an absolute
    # operator override (sandbox-ON TMPDIR case). No hardcoding.
    return (
        worktree_base_path() / _sanitize_for_filename(instance_id)
    ).resolve()


def _branch_name_for(instance_id: str) -> str:
    return f"{_BRANCH_PREFIX}{_sanitize_for_filename(instance_id)}"


async def _create_problem_worktree(
    cached_repo: Path,
    base_commit: str,
    instance_id: str,
) -> Optional[Tuple[Path, str]]:
    wt_path = _worktree_path_for(instance_id)
    branch_name = _branch_name_for(instance_id)
    try:
        # Hardened cleanup (Phase C): rc-check both removals and SURFACE
        # failures — silent orphaning is what poisoned the cache and
        # cost a diagnostic cycle.
        if wt_path.is_dir():
            _rm_rc, _rm_out, _rm_err = await _run_git(
                ["worktree", "remove", "--force", str(wt_path)],
                cwd=cached_repo,
            )
            if _rm_rc != 0:
                logger.warning(
                    "[SWEBenchPro] worktree remove rc=%d for %r: %s",
                    _rm_rc, str(wt_path), _rm_err.strip()[:200],
                )
            shutil.rmtree(str(wt_path), ignore_errors=True)
        _br_rc, _br_out, _br_err = await _run_git(
            ["branch", "-D", branch_name],
            cwd=cached_repo,
        )
        # Canonical orphan reap — mirrors L3
        # WorktreeManager.reap_orphans `git worktree prune`. Clears
        # dangling admin entries that would otherwise make the next
        # `worktree add -b` fail on a phantom path.
        await _run_git(["worktree", "prune"], cwd=cached_repo)
        # A non-zero `branch -D` is benign ONLY if the branch is truly
        # gone (normal first run: branch never existed). If it still
        # exists, the upcoming `worktree add -b` WILL fail — make that
        # loud now instead of silently orphaning.
        _ls_rc, _ls_out, _ls_err = await _run_git(
            ["branch", "--list", branch_name],
            cwd=cached_repo,
        )
        if _ls_out.strip():
            logger.warning(
                "[SWEBenchPro] orphan branch %r persists after cleanup "
                "(branch -D rc=%d) — surfacing; `worktree add` will fail "
                "loudly as WORKTREE_CREATE_FAILED, NOT silently orphan",
                branch_name, _br_rc,
            )
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        rc, _stdout, stderr = await _run_git(
            [
                "worktree", "add", "-b", branch_name,
                str(wt_path), base_commit,
            ],
            cwd=cached_repo,
        )
        if rc != 0:
            logger.warning(
                "[SWEBenchPro] git worktree add failed for %r at "
                "base_commit=%s rc=%d: %s",
                instance_id, base_commit, rc, stderr.strip()[:200],
            )
            return None
        return wt_path, branch_name
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning(
            "[SWEBenchPro] _create_problem_worktree raised for %r",
            instance_id, exc_info=True,
        )
        return None


# ===========================================================================
# Test patch application
# ===========================================================================


async def _apply_test_patch(
    worktree_path: Path,
    test_patch: str,
) -> bool:
    """Apply the SWE-Bench test_patch to the worktree.  An empty
    test_patch is treated as a no-op success."""
    if not test_patch.strip():
        return True
    try:
        rc, _stdout, stderr = await _run_git(
            ["apply", "--index", "-"],
            cwd=worktree_path,
            stdin_input=test_patch.encode("utf-8"),
        )
        if rc != 0:
            logger.warning(
                "[SWEBenchPro] git apply (test_patch) failed in %r "
                "rc=%d: %s",
                str(worktree_path), rc, stderr.strip()[:300],
            )
            return False
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning(
            "[SWEBenchPro] _apply_test_patch raised for %r",
            str(worktree_path), exc_info=True,
        )
        return False


def _extract_target_paths_from_patch(test_patch: str) -> Tuple[str, ...]:
    """Parse +++ b/<path> headers to extract the target paths.
    Pure function; NEVER raises."""
    paths: List[str] = []
    seen: set = set()
    try:
        for line in test_patch.splitlines():
            if line.startswith("+++ b/"):
                p = line[len("+++ b/"):].strip()
                if p and p != "/dev/null" and p not in seen:
                    seen.add(p)
                    paths.append(p)
    except Exception:  # noqa: BLE001
        return ()
    return tuple(paths)


def _strip_test_patch_sections(
    diff_text: str, test_paths: Iterable[str],
) -> str:
    """Drop the diff sections that touch any path in the test-patch manifest.

    Slice 69 — the clean-delta invariant. ``prepare_problem`` applies the
    held-out ``test_patch`` into the worktree BEFORE the model runs, so the
    working-tree capture (``git add -A && git diff --cached base_commit``) also
    sees those test files. The container scorer runs
    ``git apply <test_patch> && git apply <model_patch>`` — and step 2 has NO
    ``|| true`` guard, so a model patch carrying the already-applied test hunks
    HARD-FAILS (``scoring_error``). Stripping the test footprint here leaves
    100% pure model source changes in the captured patch.

    Splits the unified diff on ``diff --git`` section boundaries and removes any
    section whose paths (parsed from the ``diff --git`` header AND the
    ``--- a/`` / ``+++ b/`` lines — same convention as
    ``_extract_target_paths_from_patch``) intersect the manifest. Path matching
    is EXACT on the repo-relative path (never basename), so a source file that
    merely shares a basename with a test survives. Pure; NEVER raises — on any
    error it returns the input unfiltered (capture is fail-safe, not fail-shut).
    """
    test_set = {p.strip() for p in test_paths if p and p.strip()}
    if not test_set or not diff_text:
        return diff_text
    try:
        sections: List[Tuple[set, List[str]]] = []
        cur_lines: List[str] = []
        cur_paths: set = set()

        def _flush() -> None:
            if cur_lines:
                sections.append((set(cur_paths), list(cur_lines)))

        for line in diff_text.splitlines(keepends=True):
            if line.startswith("diff --git "):
                _flush()
                cur_lines = [line]
                cur_paths = set()
                # "diff --git a/<p1> b/<p2>" — parse both (renames differ).
                rest = line[len("diff --git "):].rstrip("\r\n")
                if " b/" in rest:
                    a_part, b_part = rest.split(" b/", 1)
                    if a_part.startswith("a/"):
                        cur_paths.add(a_part[2:].strip())
                    cur_paths.add(b_part.strip())
            else:
                cur_lines.append(line)
                if line.startswith("+++ b/"):
                    cur_paths.add(line[len("+++ b/"):].rstrip("\r\n").strip())
                elif line.startswith("--- a/"):
                    cur_paths.add(line[len("--- a/"):].rstrip("\r\n").strip())
        _flush()

        kept: List[str] = []
        for paths, sec_lines in sections:
            if paths & test_set:
                continue  # contaminating test-patch section — drop it
            kept.extend(sec_lines)
        return "".join(kept)
    except Exception:  # noqa: BLE001 — capture is fail-safe, never fail-shut
        return diff_text


# ===========================================================================
# Public API — prepare_problem
# ===========================================================================


async def prepare_problem(
    problem: ProblemSpec,
) -> Tuple[Optional[PreparedProblem], HarnessOutcome]:
    """Prepare a SWE-Bench-Pro problem for RepairEngine invocation.

    Pipeline: master flag → lazy clone → worktree at base_commit →
    apply test_patch → extract target_paths → return PreparedProblem.

    NEVER raises (``asyncio.CancelledError`` propagates).

    Slice 6 — task-naming completeness: the current asyncio task is
    renamed to ``swe_bench_pro:prepare_problem:<instance_id>`` for
    the duration of the call so the EvaluatorTraceObserver's
    structural probe can identify which inline-await phase a stuck
    task is wedged in. ``task_phase`` is the canonical composing
    primitive (single source of name truth via the EvaluatorPhase
    enum); no hardcoded phase string is allowed at the call site,
    and the Slice 6 AST pin enforces that.
    """
    if not swe_bench_pro_enabled():
        return None, HarnessOutcome.MASTER_FLAG_OFF
    _instance_id = getattr(problem, "instance_id", "") or ""
    async with task_phase(EvaluatorPhase.PREPARE_PROBLEM, _instance_id):
        start = time.monotonic()
        try:
            cached = await _ensure_repo_cached(problem.repo_url)
            if cached is None:
                return None, HarnessOutcome.CLONE_FAILED
            wt_pair = await _create_problem_worktree(
                cached, problem.base_commit, problem.instance_id,
            )
            if wt_pair is None:
                return None, HarnessOutcome.WORKTREE_CREATE_FAILED
            worktree_path, branch_name = wt_pair
            if not await _apply_test_patch(worktree_path, problem.test_patch):
                return None, HarnessOutcome.TEST_PATCH_FAILED
            target_paths = _extract_target_paths_from_patch(problem.test_patch)
            elapsed = time.monotonic() - start
            prepared = PreparedProblem(
                problem_instance_id=problem.instance_id,
                worktree_path=worktree_path,
                base_commit=problem.base_commit,
                repo_url=problem.repo_url,
                branch_name=branch_name,
                target_paths=target_paths,
                elapsed_s=elapsed,
            )
            logger.info(
                "[SWEBenchPro] prepared problem=%r worktree=%r "
                "branch=%r elapsed=%.1fs targets=%d",
                problem.instance_id, str(worktree_path), branch_name,
                elapsed, len(target_paths),
            )
            return prepared, HarnessOutcome.READY
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning(
                "[SWEBenchPro] prepare_problem raised for %r",
                problem.instance_id, exc_info=True,
            )
            return None, HarnessOutcome.WORKTREE_CREATE_FAILED


# ===========================================================================
# Public API — capture_produced_patch (called after RepairEngine
# returns; Phase B.2)
# ===========================================================================


async def capture_produced_patch(
    prepared: PreparedProblem,
) -> Tuple[Optional[str], DiffCaptureOutcome]:
    """Capture the patch the model produced — the full diff vs ``base_commit``
    of the worktree's CURRENT state, with the harness's pre-applied
    ``test_patch`` footprint stripped.

    Slice 68 substrate — APPLY writes the candidate into the worktree's WORKING
    TREE (uncommitted; any commit goes to the separate AutoCommitter workspace),
    so the previous ``git diff base_commit HEAD`` (committed-only) returned
    ``no_changes`` even when the patch was applied. Stage everything (incl. new
    files) then diff the index vs ``base_commit`` — the canonical SWE-bench
    ``git add -A && git diff`` capture — which recovers the patch whether it was
    left in the working tree OR committed, and includes added files. Staging is
    safe: the worktree is ephemeral (``cleanup_prepared`` removes it).

    Slice 69 clean-delta invariant — that capture ALSO sees the held-out
    ``test_patch`` (applied at prepare time). Filter it out via
    ``prepared.target_paths`` so the scorer's
    ``git apply <test_patch> && git apply <model_patch>`` doesn't double-apply
    the test hunks (step 2 has no ``|| true`` guard → ``scoring_error``)."""
    try:
        # Stage all changes (modified + added + deleted) so the diff vs
        # base_commit also sees untracked files. Best-effort: a non-zero rc
        # here is non-fatal — the diff below is authoritative.
        await _run_git(["add", "-A"], cwd=prepared.worktree_path)
        rc, stdout, stderr = await _run_git(
            ["diff", "--cached", prepared.base_commit],
            cwd=prepared.worktree_path,
        )
        if rc != 0:
            logger.warning(
                "[SWEBenchPro] git diff failed for %r rc=%d: %s",
                prepared.problem_instance_id, rc, stderr.strip()[:200],
            )
            return None, DiffCaptureOutcome.CAPTURE_FAILED
        # Strip the pre-applied test_patch footprint (clean-delta invariant).
        cleaned = _strip_test_patch_sections(stdout, prepared.target_paths)
        if not cleaned.strip():
            # Either no change at all, or the ONLY change was the test_patch
            # (the model produced nothing) — both are NO_CHANGES.
            return None, DiffCaptureOutcome.NO_CHANGES
        return cleaned, DiffCaptureOutcome.CAPTURED
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning(
            "[SWEBenchPro] capture_produced_patch raised for %r",
            prepared.problem_instance_id, exc_info=True,
        )
        return None, DiffCaptureOutcome.CAPTURE_FAILED


# ===========================================================================
# Public API — cleanup_prepared (worktree + branch removal)
# ===========================================================================


async def cleanup_prepared(prepared: PreparedProblem) -> bool:
    """Remove the per-problem worktree + branch.  Returns True on
    full success, False if cleanup partially or fully failed."""
    success = True
    try:
        cached = _cached_repo_path_for(prepared.repo_url)
        if cached.is_dir():
            rc, _stdout, _stderr = await _run_git(
                [
                    "worktree", "remove", "--force",
                    str(prepared.worktree_path),
                ],
                cwd=cached,
            )
            if rc != 0:
                success = False
            await _run_git(
                ["branch", "-D", prepared.branch_name],
                cwd=cached,
            )
        if prepared.worktree_path.is_dir():
            shutil.rmtree(str(prepared.worktree_path), ignore_errors=True)
        return success
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug(
            "[SWEBenchPro] cleanup_prepared raised for %r",
            prepared.problem_instance_id, exc_info=True,
        )
        return False


# ===========================================================================
# FlagRegistry self-registration
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration.  Returns count
    successfully registered.  NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=REPO_CACHE_PATH_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_REPO_CACHE_PATH,
            description=(
                "Per-repo clone cache directory for SWE-Bench-Pro "
                "Phase B harness.  One clone per upstream URL, "
                "shared across all instances of that repo.  "
                f"Defaults to {_DEFAULT_REPO_CACHE_PATH}.  "
                "RUNBOOK: a $TMPDIR override is SANDBOX-ON-ONLY (for "
                "envs that block repo-root .git writes).  Under "
                "sandbox-OFF it escapes the operation_advisor "
                "allowed-prefix anchor (project_root) and the "
                "swe-path preflight (B1) REFUSES the spend; the "
                "runtime guard (B2) fails the op infra-closed.  Keep "
                "the default (under repo root) for sandbox-OFF runs."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "per_problem_harness.py"
            ),
            example=_DEFAULT_REPO_CACHE_PATH,
            since="v3.7 Phase 2 Phase B.1 (2026-05-12)",
        ),
        FlagSpec(
            name=WORKTREE_BASE_PATH_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_WORKTREE_BASE_PATH,
            description=(
                "Per-problem worktree base directory.  One worktree "
                "per problem instance, branch-named "
                f"'{_BRANCH_PREFIX}<sanitized-id>'.  Distinct from "
                "L3 'unit-*' branches + L2 exercise branches.  "
                "RUNBOOK: a $TMPDIR override is SANDBOX-ON-ONLY.  "
                "Under sandbox-OFF it escapes the operation_advisor "
                "anchor → B1 swe-path preflight REFUSES the spend, B2 "
                "runtime guard fails the op infra-closed (no shared- "
                "tree fallback).  Keep the default for sandbox-OFF."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "per_problem_harness.py"
            ),
            example=_DEFAULT_WORKTREE_BASE_PATH,
            since="v3.7 Phase 2 Phase B.1 (2026-05-12)",
        ),
        FlagSpec(
            name=GIT_CLONE_TIMEOUT_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_GIT_CLONE_TIMEOUT_S,
            description=(
                "Subprocess timeout (seconds) for the initial "
                "git clone of each upstream repo.  Default "
                f"{_DEFAULT_GIT_CLONE_TIMEOUT_S}s = 10 min."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "per_problem_harness.py"
            ),
            example=str(_DEFAULT_GIT_CLONE_TIMEOUT_S),
            since="v3.7 Phase 2 Phase B.1 (2026-05-12)",
        ),
        FlagSpec(
            name=GIT_OP_TIMEOUT_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_GIT_OP_TIMEOUT_S,
            description=(
                "Subprocess timeout (seconds) for non-clone git "
                "operations (worktree add, apply, diff, branch "
                f"delete).  Default {_DEFAULT_GIT_OP_TIMEOUT_S}s = "
                "1 min."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "per_problem_harness.py"
            ),
            example=str(_DEFAULT_GIT_OP_TIMEOUT_S),
            since="v3.7 Phase 2 Phase B.1 (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SWEBenchPro] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "PER_PROBLEM_HARNESS_SCHEMA_VERSION",
    "REPO_CACHE_PATH_ENV_VAR",
    "WORKTREE_BASE_PATH_ENV_VAR",
    "GIT_CLONE_TIMEOUT_ENV_VAR",
    "GIT_OP_TIMEOUT_ENV_VAR",
    "HarnessOutcome",
    "DiffCaptureOutcome",
    "PreparedProblem",
    "repo_cache_path",
    "worktree_base_path",
    "git_clone_timeout_s",
    "git_op_timeout_s",
    "prepare_problem",
    "capture_produced_patch",
    "cleanup_prepared",
    "register_flags",
]
