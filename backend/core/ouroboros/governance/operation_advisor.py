"""
Operation Advisor — JARVIS-Level Tier 1.

"Sir, I wouldn't recommend that."

Evaluates the WISDOM of an operation before the pipeline executes it.
Not just "can we do this?" but "SHOULD we do this right now?"

Signals: blast radius, test coverage, chronic entropy, time context,
failure streaks, merge freeze, file staleness, concurrent operations.

Decisions: RECOMMEND / CAUTION / ADVISE_AGAINST / BLOCK

Boundary Principle:
  Deterministic: All signals computed via AST, git log, system clock,
  and historical data. No model inference in the judgment itself.
  The advice is injected into the generation prompt as context.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get(
    "JARVIS_ADVISOR_ENABLED", "true"
).lower() in ("true", "1", "yes")
_BLAST_RADIUS_WARN = int(os.environ.get("JARVIS_ADVISOR_BLAST_RADIUS_WARN", "10"))
_FAILURE_STREAK_WARN = int(os.environ.get("JARVIS_ADVISOR_FAILURE_STREAK_WARN", "3"))

# ---------------------------------------------------------------------------
# Blast-radius cache (TTL-bounded memoization)
# ---------------------------------------------------------------------------
#
# Default 60s.  Per-call ``_compute_blast_radius`` scans every Python file in
# the project root and substring-checks the content for target modules — on
# this repo (~29.5k Python files) a cold scan takes ~15s and a warm one
# still takes several seconds.  Without caching, each Advisor call inside the
# orchestrator's CLASSIFY phase paid the full scan, and the call was made on
# the asyncio event loop, starving every other coroutine (16 sensors +
# router dispatch + governed loop) for the duration.  Observed 2026-05-12
# stage-1 wiring soak (session bt-2026-05-13-054721): first CLASSIFY took
# ~12 minutes wall-clock between dispatch and Advisor verdict, subsequent
# ones ~60s each — entirely the filesystem scan, serialized through one
# starved event loop.
#
# TTL is short (60s default) so the cache stays honest under fast-moving
# file changes; longer windows risk acting on stale blast radius.  Most
# ops within a session target similar file sets (sensors re-emit on the
# same hot files), so even a 60s window yields high hit rate.
#
# Cache key: (frozenset(target_files), str(scan_root)) — invariant to
# tuple ordering of target_files (operator binding 2026-04-26: signal
# coalescing must not produce duplicate blast-radius work).
_BLAST_RADIUS_CACHE_TTL_S: float = float(
    os.environ.get("JARVIS_ADVISOR_BLAST_RADIUS_CACHE_TTL_S", "60")
)
# Bounded by op count to keep memory predictable.  16 active sensors × ~3
# unique target_file sets each = ~50 entries typical; pinning to 256
# leaves headroom without unbounded growth.
_BLAST_RADIUS_CACHE_MAX_ENTRIES: int = int(
    os.environ.get("JARVIS_ADVISOR_BLAST_RADIUS_CACHE_MAX_ENTRIES", "256")
)

# Module-level cache — shared across ALL OperationAdvisor instances in
# the process so that per-CLASSIFY-call instantiation (classify_runner
# line ~278; orchestrator line ~1855) doesn't re-pay the cold scan for
# every op.  Stage-1 wiring soak 2026-05-13 (session
# bt-2026-05-13-070956) caught the per-instance trap: my per-instance
# cache was correct in isolation (114,567x speedup demonstrated) but
# wasted in production because each CLASSIFY built a fresh
# OperationAdvisor and lost the cached state.  Advisor verdict latency
# observed at 8m28s for the SWE-Bench-Pro envelope; expected drop to
# seconds with the shared cache.
#
# Cache key includes ``str(scan_root)`` so worktree-aware advisors
# (each with a distinct scan tree) never read each other's results.
# Single threading.Lock guards mutation since the cache is accessed
# from worker threads via asyncio.to_thread.
_BLAST_RADIUS_CACHE_SHARED: "Dict[Tuple[frozenset, str], Tuple[float, int]]" = {}
import threading as _threading  # local alias — keep top-level import block clean
_BLAST_RADIUS_CACHE_LOCK: "_threading.Lock" = _threading.Lock()


# ---------------------------------------------------------------------------
# Oracle-graph blast radius (PR-A 2026-05-13)
# ---------------------------------------------------------------------------
#
# Replaces the ~29.5k-file rglob+read_text scan in _compute_blast_radius
# with a query against the Oracle's pre-built CodeGraph
# (``compute_blast_radius`` BFS over import/call edges).  When the
# graph is loaded and the target file/symbol is found, this is O(degree)
# instead of O(N×avg_file_size) — typically microseconds instead of
# seconds.  Stage-1 wiring soak 2026-05-13 (session
# bt-2026-05-13-075148, even after PR-B isolation) showed that OS-level
# disk contention from 16 sensors + Oracle + DreamEngine reading files
# concurrently was the residual bottleneck; eliminating the rglob
# entirely closes it.
#
# Composition discipline (per operator binding "leverage existing files
# + architecture"): we use TheOracle's PUBLIC ``get_blast_radius()``
# API — the same one CLI ``oracle.py blast`` already exposes.  No
# duplicate BFS; no parallel graph state.  Parity contract: when the
# graph doesn't have the target node (cold graph / new file / Oracle
# disabled), we fall back to the legacy rglob scan byte-identically
# so behavior is conservative under cold-cache.
#
# Master flag (default-FALSE per §33.1 graduation): when off, the
# Oracle path is short-circuited and behavior is byte-identical to
# pre-PR-A.  Operators graduate the flag after parity validation on
# their own data.
ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR: str = (
    "JARVIS_ADVISOR_ORACLE_BLAST_ENABLED"
)


def _advisor_oracle_blast_enabled() -> bool:
    """Master flag for the Oracle-graph blast path."""
    raw = os.environ.get(ADVISOR_ORACLE_BLAST_ENABLED_ENV_VAR, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# Module-level Oracle reference — mirrors the ``_active_goal_tracker``
# pattern in unified_intake_router.py.  GovernedLoopService sets this
# after Oracle initialization; advisor reads it at advise() time.
# Optional[TheOracle], typed as Any to avoid an import cycle.
_active_oracle: "Optional[Any]" = None


def set_active_oracle(oracle: "Optional[Any]") -> None:
    """Register the running Oracle instance with the advisor module.

    Called by GovernedLoopService once the Oracle is initialized.
    When the advisor's blast computation runs, it will check this
    reference; ``None`` keeps the legacy rglob path active.
    """
    global _active_oracle
    _active_oracle = oracle


def _oracle_blast_count(
    oracle: "Any",
    target_files: "Tuple[str, ...]",
) -> "Optional[int]":
    """Query Oracle for the blast radius of ``target_files``.

    Returns the count of unique affected files (direct + transitive
    importers/callers), capped at 50 to match the legacy scan's
    behavior.  Returns ``None`` when:

    - any target file isn't in the graph (cold cache / new file)
    - the graph itself isn't populated (Oracle still initializing)
    - any query raises

    A ``None`` return signals "fall back to legacy" — the advisor
    treats it as a cache miss.  This keeps the Oracle path
    conservative: we only TRUST the Oracle count when EVERY target
    is graph-resolvable.  Heterogeneous mixes (one new file + one
    indexed file) fall through rather than silently undercount.

    Uses TheOracle's PUBLIC ``get_blast_radius`` API (the same
    surface ``oracle.py`` CLI ``blast`` command exposes), composing
    existing architecture rather than duplicating BFS logic.
    """
    # Only treat as resolved when EVERY target file is in the graph.
    # Mixed resolution would silently undercount.
    all_affected_file_paths: set = set()
    for target in target_files:
        # The Oracle API resolves both file paths and symbol names.
        # We prefer the basename without extension (matches how the
        # graph indexes module nodes) but try both forms.
        candidates: "List[str]" = []
        if target.endswith(".py"):
            from pathlib import Path as _Path
            stem = _Path(target).stem
            candidates.append(stem)
            candidates.append(target)
        else:
            candidates.append(target)

        resolved_for_target = False
        for candidate in candidates:
            try:
                blast = oracle.get_blast_radius(candidate)
            except Exception:  # noqa: BLE001
                return None  # Defensive: any Oracle error → legacy fallback
            if blast.risk_level == "unknown":
                continue  # candidate not in graph; try next form
            # Found.  Collect affected NodeIDs → unique file_paths.
            for node_id in blast.directly_affected:
                fp = getattr(node_id, "file_path", "") or ""
                if fp:
                    all_affected_file_paths.add(fp)
            for node_id in blast.transitively_affected:
                fp = getattr(node_id, "file_path", "") or ""
                if fp:
                    all_affected_file_paths.add(fp)
            resolved_for_target = True
            break

        if not resolved_for_target:
            # Target not in graph — abort the Oracle path; legacy
            # rglob still catches name occurrences across the tree.
            return None

    # Cap at 50 to match legacy scan's break threshold (preserves
    # the existing risk-score calibration in advise()).
    return min(len(all_affected_file_paths), 50)


# ---------------------------------------------------------------------------
# Dedicated bounded executor for advisor blast scans (PR-B)
# ---------------------------------------------------------------------------
#
# Background: stage-1 wiring soak 2026-05-13 (session
# bt-2026-05-13-072716) showed that even after asyncio.to_thread + TTL
# cache + module-level shared cache, advisor.advise() in the live
# harness still didn't complete within 360s while the standalone
# benchmark of the same workload finished in 30s (12x slowdown).
# Standalone vs live difference: the live harness has 16 sensors +
# Oracle + DreamEngine + IntakeLayer all dispatching their own
# blocking I/O to asyncio.to_thread, which uses the DEFAULT
# ThreadPoolExecutor.  Advisor's blast scan got queued behind dozens
# of unrelated tasks.
#
# Fix: a dedicated, bounded ThreadPoolExecutor for advisor blast work
# ONLY.  Small max_workers (default 2 — blast scans are CPU-light
# I/O-heavy, and isolation is the point, not parallelism).  Observable
# via logger queue-depth on every dispatch.  Process-wide singleton
# (lazy-init under lock).
#
# This is a workload-isolation fix, not a semantic change.  Legacy
# rglob blast computation runs unchanged inside the dedicated executor
# (PR-A separately replaces the rglob with Oracle-graph BFS).
import concurrent.futures as _futures
import atexit as _atexit
_ADVISOR_BLAST_EXECUTOR_MAX_WORKERS: int = int(
    os.environ.get("JARVIS_ADVISOR_BLAST_EXECUTOR_WORKERS", "2")
)
_ADVISOR_BLAST_EXECUTOR: "Optional[_futures.ThreadPoolExecutor]" = None
_ADVISOR_BLAST_EXECUTOR_INIT_LOCK: "_threading.Lock" = _threading.Lock()


# ---------------------------------------------------------------------------
# Task #88f (2026-05-14) — Advisor-busy signal (public, stable)
# ---------------------------------------------------------------------------
#
# Closes the v14-rev10 graduation-soak blocker: Oracle's
# ``_oracle_index_loop.incremental_update([])`` polls the full 29k-file
# main tree every ~3min and contends with Advisor's blast-radius file
# walks on disk I/O.  The Advisor's dedicated executor IS isolated for
# thread scheduling, but its rglob+read_text scans share the kernel's
# block-device I/O bandwidth with Oracle's main-tree indexing.
#
# This counter is the OFFICIAL "advisor busy" signal that
# ``_oracle_index_loop`` reads to decide whether to yield this poll
# cycle.  Operator binding 2026-05-14: prefer a stable counter over
# ``executor._work_queue.qsize()`` (private, fragile across Python
# versions).  Incremented at the start of every ``advise_async``
# blast scan (inside the executor thread); decremented at the end
# (always, even on exception).  Read by ``get_advisor_busy_count()``
# (public).  Thread-safe via threading.Lock.
#
# The counter measures CONCURRENT BLAST WORK — not pending queue.
# When it's >0, Oracle should yield to avoid disk contention.  When
# it's 0, Oracle is free to poll its bulk indexing.

_ADVISOR_BUSY_COUNT: int = 0
_ADVISOR_BUSY_LOCK: "_threading.Lock" = _threading.Lock()


def get_advisor_busy_count() -> int:
    """Return the count of advise_async() blast scans currently in
    flight on the dedicated executor.

    Stable public surface for cooperative scheduling — callers like
    ``governed_loop_service._oracle_index_loop`` use it to decide
    whether to yield a polling cycle.  Always returns a non-negative
    integer; never raises.
    """
    with _ADVISOR_BUSY_LOCK:
        return _ADVISOR_BUSY_COUNT


def _advisor_busy_incr() -> None:
    """Internal — bump the counter when an advise_async scan starts."""
    global _ADVISOR_BUSY_COUNT
    with _ADVISOR_BUSY_LOCK:
        _ADVISOR_BUSY_COUNT += 1


def _advisor_busy_decr() -> None:
    """Internal — drop the counter when an advise_async scan ends.

    Defensive: never let the counter go negative even on double-decrement
    (would indicate a bug; clamp to 0 to keep the public surface honest).
    """
    global _ADVISOR_BUSY_COUNT
    with _ADVISOR_BUSY_LOCK:
        _ADVISOR_BUSY_COUNT = max(0, _ADVISOR_BUSY_COUNT - 1)


def _get_advisor_blast_executor() -> "_futures.ThreadPoolExecutor":
    """Lazy-init singleton dedicated ThreadPoolExecutor for advisor
    blast scans.  All ``advise_async`` calls dispatch here so advisor
    work never queues behind the default executor's other consumers.
    """
    global _ADVISOR_BLAST_EXECUTOR
    if _ADVISOR_BLAST_EXECUTOR is None:
        with _ADVISOR_BLAST_EXECUTOR_INIT_LOCK:
            if _ADVISOR_BLAST_EXECUTOR is None:
                _ADVISOR_BLAST_EXECUTOR = _futures.ThreadPoolExecutor(
                    max_workers=_ADVISOR_BLAST_EXECUTOR_MAX_WORKERS,
                    thread_name_prefix="advisor-blast",
                )
                _atexit.register(_shutdown_advisor_blast_executor)
                logger.info(
                    "[Advisor] dedicated executor initialized "
                    "(max_workers=%d, thread_prefix=advisor-blast)",
                    _ADVISOR_BLAST_EXECUTOR_MAX_WORKERS,
                )
    return _ADVISOR_BLAST_EXECUTOR


def _shutdown_advisor_blast_executor() -> None:
    """Clean shutdown at process exit — drains pending work then
    closes the pool.  Idempotent: safe to call multiple times."""
    global _ADVISOR_BLAST_EXECUTOR
    with _ADVISOR_BLAST_EXECUTOR_INIT_LOCK:
        if _ADVISOR_BLAST_EXECUTOR is not None:
            try:
                _ADVISOR_BLAST_EXECUTOR.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
            _ADVISOR_BLAST_EXECUTOR = None

# ---------------------------------------------------------------------------
# B.2.0 — Worktree-aware advisory (SWE-Bench-Pro Phase 2 enabling layer +
# permanent improvement for L3 worktree-isolated work and the in-repo L2
# exercise corpus). §33.1 default-FALSE master switch; when ON, the advisor
# scans the per-envelope ``repo_root`` for blast/coverage/staleness/large-file
# signals instead of its constructor-bound ``self._project_root``.
#
# Source-agnostic by design: no envelope.source branch is consulted. The
# override applies whenever the envelope carries a trusted ``repo_root``
# string in its evidence, regardless of which sensor produced it. Per
# operator binding (B.2.0 hardening note 4): blast is computed from the
# actual mutation root — not from a category special-case.
# ---------------------------------------------------------------------------
ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR: str = (
    "JARVIS_ADVISOR_WORKTREE_AWARE_ENABLED"
)
ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR: str = (
    "JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST"
)

# Canonical evidence key (operator binding: pick ONE name, document it,
# don't fork parallel spellings in B.2.1 envelope builder). Mirrored by
# OperationContext.intake_evidence_json schema. Sensors that historically
# stamped ``worktree_path`` continue to do so for telemetry; the advisor
# input is unambiguously ``repo_root``.
EVIDENCE_REPO_ROOT_KEY: str = "repo_root"


# ---------------------------------------------------------------------------
# Read-only intent inference (deterministic keyword scan)
# ---------------------------------------------------------------------------
#
# The orchestrator calls infer_read_only_intent() BEFORE the Advisor so the
# flag can be stamped onto the OperationContext hash chain. The Advisor then
# trusts the flag — not because of the keywords, but because tool_executor
# + orchestrator jointly refuse any mutating tool call / APPLY transition
# whenever ctx.is_read_only is True. The keywords are a soft trigger; the
# enforcement is the mathematical guarantee.

_READ_ONLY_POSITIVE: Tuple[str, ...] = (
    "read-only",
    "read only",
    "readonly",
    "do not mutate",
    "do not write",
    "do not modify",
    "do not change",
    "cartography",
    "architectural mapping",
    "call graph",
    "gap analysis",
    "coupling map",
    "pure-exploration",
    "pure exploration",
    "exploration-only",
    "survey",
    "audit",
    "do not run any tests",
    "do not write any source files",
)

# Mutation verbs — matched as **whole words** (word-boundary regex below).
# Substring-match was used in v1 but tripped on compound words — "dispatch"
# contains "patch", "implementation" contains "implement", etc. — so the
# Trinity cartography task was mis-classified as mutating in the first
# Session-3 run (debug.log bt-2026-04-18-032138).
_READ_ONLY_NEGATIVE: Tuple[str, ...] = (
    "refactor",
    "refactors",
    "refactoring",
    "rewrite",
    "rewrites",
    "rewriting",
    "implement",
    "implements",
    "implementing",
    "fix",
    "fixes",
    "fixing",
    "patch",
    "patches",
    "patching",
    "rename",
    "renames",
    "renaming",
    "replace",
    "replaces",
    "replacing",
    "remove",
    "removes",
    "removing",
    "delete",
    "deletes",
    "deleting",
    "migrate",
    "migrates",
    "migrating",
    "upgrade",
    "upgrades",
    "upgrading",
    # Two-word phrases kept as substring checks below — they can't
    # collide with compound words the way single verbs can.
)

_READ_ONLY_NEGATIVE_PHRASES: Tuple[str, ...] = (
    "add a ",
    "add new ",
    "add an ",
)

# Pre-compile one alternation regex with word boundaries on both sides.
# \b treats "-" as a word boundary in Python re, which is what we want
# for hyphenated verbs like "re-write" if they ever appear.
_READ_ONLY_NEGATIVE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _READ_ONLY_NEGATIVE) + r")\b",
    re.IGNORECASE,
)


def infer_read_only_intent(description: str) -> bool:
    """Return True iff *description* strongly signals a non-mutating op.

    Deterministic keyword scan, no LLM call. Conservative: requires at
    least one positive signal AND no mutation verbs. Mutation verbs are
    matched as **whole words** so "dispatch" / "implementation" / "fixed"
    don't collide with "patch" / "implement" / "fix". False negatives
    remain acceptable (the op proceeds through normal risk gating);
    false positives are structurally clamped — the orchestrator's APPLY
    short-circuit and the policy engine's Rule 0d refuse mutations even
    if a mutating op slipped the classifier.
    """
    if not description:
        return False
    norm = description.lower()
    if not any(kw in norm for kw in _READ_ONLY_POSITIVE):
        return False
    if _READ_ONLY_NEGATIVE_RE.search(norm):
        return False
    if any(phrase in norm for phrase in _READ_ONLY_NEGATIVE_PHRASES):
        return False
    return True


# ---------------------------------------------------------------------------
# Trusted-path resolver — bridges envelope.evidence to advisor.repo_root.
#
# Untrusted-input contract (B.2.0 hardening note 1): the evidence dict is
# operator-influenced data (it flows from sensors, ingest endpoints, and the
# `/attach` REPL path). The advisor must NOT trust an arbitrary path string —
# a hostile or buggy envelope could point ``repo_root`` at ``/etc`` (silently
# making blast=0 globally) or at a symlink that escapes the worktree base.
#
# Validation pipeline (first-failure-wins, NEVER raises):
#   1. master flag ON
#   2. evidence carries ``repo_root`` string + non-empty
#   3. Path resolves (no permission error, no missing-parent ENOENT)
#   4. Resolved path exists + is a directory
#   5. Resolved path is contained under an allowed prefix:
#         a. ``self._project_root`` (covers in-repo worktrees + L3 .worktrees/)
#         b. additional prefixes from
#            ``JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST`` (colon-separated
#            absolute paths; each is itself ``resolve()``-d)
#
# On any failure → returns None → orchestrator falls back to
# ``self._project_root`` (legacy byte-identical behavior).
# ---------------------------------------------------------------------------


def _worktree_aware_enabled() -> bool:
    """Master switch — graduated to default-TRUE 2026-05-13 (§33.1).

    Why graduated: the B.2.0 substrate has been shipped + tested since
    2026-05-12, the path-validation contract (``resolve_envelope_repo_root``)
    rejects every path that doesn't resolve under ``project_root`` or
    a caller-supplied allowlist, and the off-default was the root
    cause of Stage-1 wiring-soak v8–v10 advise() starvation: for a
    6-file SWE-Bench-Pro worktree, the legacy fallback rglob-scanned
    the entire 29.5k-file ``project_root`` because the envelope's
    ``repo_root`` was silently discarded.  The substrate ALREADY
    walks only ``scan_root`` when given a non-None root — the flag
    was the gatekeeper, not the scanner.  Spine pin at
    ``test_operation_advisor_worktree_aware`` asserts the scan-bounding
    contract on a counterfactual fixture so the graduation can't
    silently regress.

    Operators who need the prior default-FALSE behavior can set
    ``JARVIS_ADVISOR_WORKTREE_AWARE_ENABLED=false`` explicitly.  The
    allowlist (``JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST``) remains
    opt-in — only paths under ``project_root`` are accepted without
    additional allowlist entries, so the graduation is safe for the
    default case (envelopes lacking ``repo_root`` or with
    ``repo_root`` under the bound project root).
    """
    raw = os.environ.get(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "").strip().lower()
    # Default-TRUE: unset/empty enables the worktree-aware path.
    # Explicit "false"/"0"/"no"/"off" opts back to the legacy
    # always-scan-project_root behavior.
    if raw in ("false", "0", "no", "off"):
        return False
    return True


def _parse_allowlist_env() -> Tuple[Path, ...]:
    """Parse the colon-separated allowlist env into resolved Paths.
    NEVER raises; invalid entries are skipped with a debug log."""
    raw = os.environ.get(ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR, "").strip()
    if not raw:
        return ()
    out: List[Path] = []
    for entry in raw.split(os.pathsep):
        s = entry.strip()
        if not s:
            continue
        try:
            out.append(Path(s).resolve())
        except (OSError, RuntimeError):
            logger.debug(
                "[Advisor] worktree_root_allowlist: skipping invalid entry %r",
                s,
            )
    return tuple(out)


def _is_under(candidate: Path, parent: Path) -> bool:
    """True iff ``candidate`` is ``parent`` or a descendant of it.

    Uses POSIX-style path comparison on already-resolved Paths (caller
    must ``resolve()`` first to defeat symlink escapes). NEVER raises.
    """
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_envelope_repo_root(
    intake_evidence_json: str,
    *,
    project_root: Path,
    extra_allowlist: Optional[Tuple[Path, ...]] = None,
) -> Optional[Path]:
    """Resolve a per-envelope ``repo_root`` to a trusted absolute Path.

    Parameters
    ----------
    intake_evidence_json:
        The JSON-encoded evidence snapshot from ``OperationContext
        .intake_evidence_json`` (or any source-equivalent string). Empty
        string + malformed JSON + missing key are all silently treated
        as "no override".
    project_root:
        The orchestrator's bound project root. Used both as the legacy
        fallback context AND as the canonical allowed-prefix anchor.
    extra_allowlist:
        Optional caller-supplied extra prefixes (already resolved). When
        ``None`` (default), the env-derived allowlist is consulted.

    Returns
    -------
    Optional[Path]
        Resolved trusted path on success, ``None`` on:
          * master flag OFF
          * evidence missing / not a dict / no ``repo_root`` key
          * path doesn't resolve / doesn't exist / isn't a directory
          * resolved path escapes every allowed prefix

    NEVER raises (mirrors advisor fail-open contract).
    """
    if not _worktree_aware_enabled():
        return None
    if not intake_evidence_json:
        return None
    try:
        evidence = json.loads(intake_evidence_json)
    except (ValueError, TypeError):
        logger.debug(
            "[Advisor] resolve_envelope_repo_root: evidence not valid JSON",
        )
        return None
    if not isinstance(evidence, dict):
        return None
    raw = evidence.get(EVIDENCE_REPO_ROOT_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        # ``resolve(strict=False)`` defeats symlink escapes by canonicalizing
        # the path against the live filesystem. ``strict=True`` would raise
        # on missing components — we want a graceful None, not an exception.
        resolved = Path(raw).resolve(strict=False)
    except (OSError, RuntimeError):
        logger.debug(
            "[Advisor] resolve_envelope_repo_root: Path.resolve raised "
            "for %r",
            raw,
        )
        return None
    try:
        if not resolved.exists() or not resolved.is_dir():
            return None
    except (OSError, PermissionError):
        return None
    try:
        anchor = Path(project_root).resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    allowlist: List[Path] = [anchor]
    extras = (
        extra_allowlist if extra_allowlist is not None
        else _parse_allowlist_env()
    )
    allowlist.extend(extras)
    for parent in allowlist:
        if _is_under(resolved, parent):
            return resolved
    logger.info(
        "[Advisor] resolve_envelope_repo_root: %r rejected — "
        "outside %d allowed prefix(es)",
        str(resolved), len(allowlist),
    )
    return None


# ===========================================================================
# Phase B — envelope repo_root PROMISE status (single canonical seam)
# ===========================================================================
#
# ``resolve_envelope_repo_root`` returns ``None`` for THREE distinct
# situations callers previously could not tell apart — and the silent
# collapse of the third into a byte-identical project_root fallback is
# the contamination root cause (session bt-2026-05-17-002318: a $TMPDIR
# worktree escaped the anchor, the resolver returned None, advisor +
# generator fell back to the JARVIS tree, the model edited the wrong
# repo).  This is the ONE place that classifies the three:
#
#   NO_PROMISE  — feature off OR no ``repo_root`` in evidence.  A
#                 byte-identical project_root fallback is CORRECT here.
#   RESOLVED    — a ``repo_root`` was promised AND resolves under the
#                 anchor.  Use the returned path.
#   REJECTED    — a ``repo_root`` was promised, the worktree-aware
#                 feature is ON, but the path escaped every allowed
#                 prefix.  Isolation promised and broken: callers MUST
#                 fail closed (§1 Boundary / §6 Iron Gate mirror of the
#                 L3 subagent_scheduler / RepairTree discipline — NEVER
#                 fall back to the shared tree).
#
# Both advisor sites (orchestrator.py parallel path + classify_runner.py
# primary production path) compose this, so the "threaded one site but
# not the other" bug class the classify_runner comment documents cannot
# recur — one prefix-math owner (``resolve_envelope_repo_root``), one
# status owner (this).


class RepoRootPromiseStatus(str, Enum):
    """Closed taxonomy for the envelope repo_root promise."""

    NO_PROMISE = "no_promise"   # byte-identical legacy fallback OK
    RESOLVED = "resolved"       # promised + trusted
    REJECTED = "rejected"       # promised + escaped anchor → fail closed


class EnvelopeRepoRootRejected(Exception):
    """A promised isolated ``repo_root`` escaped the advisor anchor.

    Callers convert this into the canonical infra-terminal (advance to
    POSTMORTEM, ``failure_class='infra'``) — they MUST NOT fall back to
    the shared project_root tree.
    """

    def __init__(self, raw_repo_root: str) -> None:
        self.raw_repo_root = raw_repo_root
        super().__init__(
            f"swebp_repo_root_rejected: promised isolated repo_root "
            f"{raw_repo_root!r} escaped the advisor allowed-prefix "
            f"anchor — refusing silent fallback to the shared tree"
        )


def envelope_repo_root_status(
    intake_evidence_json: str,
    *,
    project_root: Path,
    extra_allowlist: Optional[Tuple[Path, ...]] = None,
) -> Tuple["RepoRootPromiseStatus", Optional[Path], str]:
    """Classify the envelope repo_root promise. NEVER raises.

    Returns ``(status, resolved_path_or_None, raw_repo_root_str)``.
    Composes :func:`resolve_envelope_repo_root` verbatim — no parallel
    prefix math.  Feature-off / no-promise collapse to ``NO_PROMISE`` so
    byte-identity holds when the worktree-aware advisor flag is OFF
    (only a promised-AND-rejected path under an enabled feature is
    ``REJECTED``).
    """
    raw = ""
    try:
        if intake_evidence_json:
            _ev = json.loads(intake_evidence_json)
            if isinstance(_ev, dict):
                _r = _ev.get(EVIDENCE_REPO_ROOT_KEY)
                if isinstance(_r, str):
                    raw = _r.strip()
    except (ValueError, TypeError):
        raw = ""

    if not raw:
        return RepoRootPromiseStatus.NO_PROMISE, None, ""

    resolved = resolve_envelope_repo_root(
        intake_evidence_json,
        project_root=project_root,
        extra_allowlist=extra_allowlist,
    )
    if resolved is not None:
        return RepoRootPromiseStatus.RESOLVED, resolved, raw

    if not _worktree_aware_enabled():
        return RepoRootPromiseStatus.NO_PROMISE, None, raw
    return RepoRootPromiseStatus.REJECTED, None, raw


def guard_envelope_repo_root(
    intake_evidence_json: str,
    *,
    project_root: Path,
    extra_allowlist: Optional[Tuple[Path, ...]] = None,
) -> Optional[Path]:
    """Composable fail-closed guard for the two advisor call sites.

    Returns the trusted path (RESOLVED) or ``None`` (NO_PROMISE — caller
    falls back byte-identically, unchanged).  Raises
    :class:`EnvelopeRepoRootRejected` on REJECTED so the caller drives
    the canonical infra-terminal instead of silently editing the shared
    tree.
    """
    status, resolved, raw = envelope_repo_root_status(
        intake_evidence_json,
        project_root=project_root,
        extra_allowlist=extra_allowlist,
    )
    if status is RepoRootPromiseStatus.REJECTED:
        raise EnvelopeRepoRootRejected(raw)
    return resolved


class AdvisoryDecision(str, Enum):
    RECOMMEND = "recommend"            # Proceed normally
    CAUTION = "caution"                # Proceed but inject warnings into prompt
    ADVISE_AGAINST = "advise_against"  # Allow but voice warning
    BLOCK = "block"                    # Refuse (safety-critical only)


@dataclass
class Advisory:
    """The advisor's judgment on an operation."""
    decision: AdvisoryDecision
    reasons: List[str]
    blast_radius: int          # Number of files that import the targets
    test_coverage: float       # 0.0–1.0, % of targets with tests
    chronic_entropy: float     # Domain failure rate from LearningConsolidator
    risk_score: float          # Composite 0.0–1.0
    voice_message: str = ""    # What JARVIS would say


class OperationAdvisor:
    """Evaluates whether an operation SHOULD proceed.

    Called before CLASSIFY — the first thing that happens when an
    IntentEnvelope arrives. The advisor computes a risk score from
    multiple deterministic signals and returns an Advisory.

    The advisory doesn't block the pipeline (except for BLOCK).
    It injects warnings into the generation prompt so the model
    is more careful with risky operations.
    """

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        # Blast-radius memoization — see _BLAST_RADIUS_CACHE_SHARED
        # module-level state for the shared store.  This attribute is
        # an alias to the module-level dict, kept for backward-compat
        # with existing tests that introspect ``self._blast_radius_cache``
        # directly.  Mutation goes through ``_BLAST_RADIUS_CACHE_LOCK``
        # because the dict is accessed from worker threads.
        self._blast_radius_cache = _BLAST_RADIUS_CACHE_SHARED

    async def advise_async(
        self,
        target_files: Tuple[str, ...],
        description: str,
        op_id: str = "",
        is_read_only: bool = False,
        repo_root: Optional[Path] = None,
    ) -> "Advisory":
        """Async wrapper around :meth:`advise` that dispatches through
        the dedicated ``advisor-blast`` ThreadPoolExecutor — NOT the
        default asyncio executor.

        Why: in the live harness, the default executor is contested by
        16 sensors + Oracle + DreamEngine all dispatching their own
        blocking I/O via ``asyncio.to_thread``.  Advisor blast scans
        queued behind that traffic and missed the BG-pool 360s ceiling
        (stage-1 wiring soak 2026-05-13 session bt-2026-05-13-072716).
        Routing advisor work to a dedicated bounded executor
        guarantees isolation.  Queue depth is logged on every
        submission for observability (operator binding 2026-05-13).

        Identical signature + semantics to ``advise`` — callers can
        substitute ``await advisor.advise_async(...)`` for any
        ``advisor.advise(...)`` site they previously wrapped in
        ``asyncio.to_thread``.  AST-pinned at every CLASSIFY call site.
        """
        loop = asyncio.get_running_loop()
        executor = _get_advisor_blast_executor()
        # Observable queue-depth.  ThreadPoolExecutor exposes
        # _work_queue (a queue.SimpleQueue / queue.Queue) for the
        # backlog — qsize() is approximate but good enough for
        # operator visibility.  Falls back silently if the attr
        # disappears across Python versions.
        try:
            _qdepth = executor._work_queue.qsize()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            _qdepth = -1
        if _qdepth > 0:
            logger.info(
                "[Advisor] dispatching op=%s blast scan via dedicated "
                "executor; queue_depth=%d max_workers=%d",
                op_id[:16] if op_id else "-",
                _qdepth, _ADVISOR_BLAST_EXECUTOR_MAX_WORKERS,
            )
        # Task #88f — wrap the blast scan in busy-count tracking.
        # The counter is incremented inside the executor thread (just
        # before advise() runs) and decremented inside ``finally`` to
        # cover all exit paths.  Oracle's ``_oracle_index_loop`` reads
        # ``get_advisor_busy_count()`` to decide whether to yield this
        # poll cycle, avoiding disk-I/O contention with our blast scan.
        def _advise_with_busy_tracking() -> "Advisory":
            _advisor_busy_incr()
            try:
                return self.advise(
                    target_files,
                    description,
                    op_id,
                    is_read_only=is_read_only,
                    repo_root=repo_root,
                )
            finally:
                _advisor_busy_decr()

        # Use a lambda to bind kwargs because run_in_executor doesn't
        # take **kwargs natively.
        return await loop.run_in_executor(
            executor,
            _advise_with_busy_tracking,
        )

    def advise(
        self,
        target_files: Tuple[str, ...],
        description: str,
        op_id: str = "",
        is_read_only: bool = False,
        repo_root: Optional[Path] = None,
    ) -> Advisory:
        """Evaluate an operation and return advisory judgment.

        When ``is_read_only`` is True the Advisor skips blast_radius and
        test_coverage signals — the downstream contract is that tool_executor
        will refuse every mutating tool call and the orchestrator will
        refuse the APPLY transition, so those two signals are mathematically
        unreachable. Stale-file, large-file, time-of-day, and chronic-entropy
        signals still apply because they speak to generation quality, not
        blast radius.

        ``repo_root`` (B.2.0) — when supplied, all filesystem-scanning signals
        (blast radius, test coverage, staleness, large-file) compute against
        this root instead of ``self._project_root``. Callers MUST validate
        the path through :func:`resolve_envelope_repo_root` before passing
        it in. When ``None`` (default) the advisor falls back to its
        constructor-bound project root — byte-identical to pre-B.2.0
        behavior. Source-agnostic: the advisor never branches on which
        sensor produced the envelope (operator binding: blast is root-
        correct, not category-special).
        """
        if not _ENABLED:
            return Advisory(
                decision=AdvisoryDecision.RECOMMEND,
                reasons=["Advisor disabled"], blast_radius=0,
                test_coverage=1.0, chronic_entropy=0.0, risk_score=0.0,
            )

        reasons: List[str] = []
        risk_factors: List[float] = []

        # Signal 1: Blast radius (how many files import the targets)
        # Always computed for observability — surfaced as a reason only
        # for mutating ops.
        blast_radius = self._compute_blast_radius(target_files, root=repo_root)
        if not is_read_only and blast_radius >= _BLAST_RADIUS_WARN:
            reasons.append(
                f"High blast radius: {blast_radius} files import these targets"
            )
            risk_factors.append(min(1.0, blast_radius / 30))

        # Signal 2: Test coverage
        # Same bypass logic — read-only ops don't execute mutations, so
        # coverage of the targets is structurally irrelevant.
        test_coverage = self._compute_test_coverage(target_files, root=repo_root)
        if not is_read_only and test_coverage < 0.5:
            reasons.append(
                f"Low test coverage: {test_coverage:.0%} of targets have tests"
            )
            risk_factors.append(1.0 - test_coverage)

        # Signal 3: Chronic entropy (historical failure rate)
        chronic_entropy = self._get_chronic_entropy(target_files, description)
        if chronic_entropy > 0.5:
            reasons.append(
                f"High chronic entropy: {chronic_entropy:.0%} historical failure rate"
            )
            risk_factors.append(chronic_entropy)

        # Signal 4: Time of day risk
        hour = time.localtime().tm_hour
        if hour >= 2 and hour < 6:
            reasons.append("Late night operation (2-6 AM) — higher error risk")
            risk_factors.append(0.3)

        # Signal 5: File staleness (untouched for long time = riskier)
        stale_files = self._check_staleness(target_files, root=repo_root)
        if stale_files:
            reasons.append(
                f"Stale files (>90 days untouched): {', '.join(stale_files[:3])}"
            )
            risk_factors.append(0.2)

        # Signal 6: Large file risk
        large_files = self._check_large_files(target_files, root=repo_root)
        if large_files:
            reasons.append(
                f"Large files (>500 lines): {', '.join(f'{f}({l}L)' for f, l in large_files[:3])}"
            )
            risk_factors.append(0.2)

        # Compute composite risk score
        risk_score = sum(risk_factors) / max(1, len(risk_factors)) if risk_factors else 0.0
        risk_score = min(1.0, risk_score)

        # Make decision
        if risk_score >= 0.8:
            decision = AdvisoryDecision.ADVISE_AGAINST
        elif risk_score >= 0.5:
            decision = AdvisoryDecision.CAUTION
        elif risk_score >= 0.3:
            decision = AdvisoryDecision.CAUTION
        else:
            decision = AdvisoryDecision.RECOMMEND

        # Special case: block if touching LOCKED trust tier with no tests.
        # Read-only ops bypass this block because the no-mutation contract
        # makes blast radius and coverage unreachable — enforced downstream
        # by tool_executor (mutating tools refused) and orchestrator (APPLY
        # phase short-circuited to COMPLETE).
        if not is_read_only and test_coverage == 0 and blast_radius >= 20:
            decision = AdvisoryDecision.BLOCK
            reasons.append("BLOCKED: Zero test coverage + extreme blast radius")

        # Observability: surface the bypass as a positive reason so the
        # log line and prompt-context both show WHY a high-blast op passed.
        if is_read_only:
            reasons.insert(
                0,
                f"Read-only op: blast_radius={blast_radius}, "
                f"coverage={test_coverage:.0%} bypassed (no-mutation contract)",
            )

        # Build voice message
        voice = self._build_voice_message(decision, reasons, target_files)

        advisory = Advisory(
            decision=decision,
            reasons=reasons,
            blast_radius=blast_radius,
            test_coverage=test_coverage,
            chronic_entropy=chronic_entropy,
            risk_score=round(risk_score, 3),
            voice_message=voice,
        )

        logger.info(
            "[Advisor] %s (risk=%.2f, blast=%d, coverage=%.0f%%, entropy=%.0f%%, "
            "read_only=%s) reasons=%d op=%s",
            decision.value, risk_score, blast_radius,
            test_coverage * 100, chronic_entropy * 100,
            is_read_only, len(reasons), op_id,
        )

        return advisory

    def format_for_prompt(self, advisory: Advisory) -> str:
        """Format advisory for injection into generation prompt."""
        if advisory.decision == AdvisoryDecision.RECOMMEND:
            return ""

        lines = [f"## Operation Advisory: {advisory.decision.value.upper()}"]
        lines.append(f"Risk score: {advisory.risk_score:.0%}")
        for reason in advisory.reasons:
            lines.append(f"- {reason}")

        if advisory.decision == AdvisoryDecision.ADVISE_AGAINST:
            lines.append(
                "\nProceed with EXTREME CAUTION. Minimize changes. "
                "Generate tests alongside any modifications."
            )
        elif advisory.decision == AdvisoryDecision.CAUTION:
            lines.append(
                "\nBe careful with these files. Check for side effects."
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Signal computation (all deterministic)
    # ------------------------------------------------------------------

    def _compute_blast_radius(
        self,
        target_files: Tuple[str, ...],
        *,
        root: Optional[Path] = None,
    ) -> int:
        """Count files that import the targets. AST-based, deterministic.

        ``root`` (B.2.0) — scan tree. Defaults to ``self._project_root`` when
        ``None`` (pre-B.2.0 behavior). Callers MUST validate the override
        path through :func:`resolve_envelope_repo_root` first.

        Results are TTL-memoized per
        (frozenset(target_files), str(scan_root)) — see the
        ``_BLAST_RADIUS_CACHE_TTL_S`` module comment for the reason.  The
        scan reads ~29.5k Python files on the main repo (cold) and dominates
        the wall-clock cost of the Advisor.  Without the cache, every op
        within ``_BLAST_RADIUS_CACHE_TTL_S`` seconds paid the full scan;
        with the cache, repeat calls (signal coalescing on the same target
        files; WAL replay of stuck envelopes) return in microseconds.
        """
        scan_root = root if root is not None else self._project_root

        # Cache lookup — frozenset key makes the cache invariant to
        # target_files tuple ordering (coalesced envelopes can arrive
        # in arbitrary order).  Module-level shared cache: every
        # OperationAdvisor instance reads/writes the same store so
        # per-CLASSIFY-call instantiation doesn't re-pay the cold scan.
        cache_key = (frozenset(target_files), str(scan_root))
        now = time.monotonic()
        with _BLAST_RADIUS_CACHE_LOCK:
            cached = _BLAST_RADIUS_CACHE_SHARED.get(cache_key)
        if cached is not None:
            computed_at, result = cached
            if now - computed_at < _BLAST_RADIUS_CACHE_TTL_S:
                return result
            # Expired — fall through to recompute.

        # PR-A: Oracle-graph blast (gated, fallback to legacy on miss).
        # When the master flag is ON and the running Oracle has graph
        # state covering at least one target, use its pre-built BFS
        # instead of the 29.5k-file rglob scan.  Strict semantics:
        # cold graph / node-not-found / any exception → fall back to
        # legacy.  Cache the result with the same key so downstream
        # paths see a uniform value source.
        if _advisor_oracle_blast_enabled() and _active_oracle is not None:
            try:
                oracle_count = _oracle_blast_count(_active_oracle, target_files)
                if oracle_count is not None:
                    with _BLAST_RADIUS_CACHE_LOCK:
                        _BLAST_RADIUS_CACHE_SHARED[cache_key] = (now, oracle_count)
                        self._evict_blast_radius_cache_if_oversized()
                    return oracle_count
                # else: Oracle didn't have the node → fall through to legacy
            except Exception:  # noqa: BLE001 — defensive: Oracle errors must NEVER block advise
                logger.debug(
                    "[Advisor] Oracle blast query failed for "
                    "target_files=%r — falling back to legacy scan",
                    target_files, exc_info=True,
                )

        target_modules = set()
        for f in target_files:
            if f.endswith(".py"):
                module = f.replace("/", ".").replace(".py", "")
                target_modules.add(module)
                target_modules.add(Path(f).stem)

        if not target_modules:
            with _BLAST_RADIUS_CACHE_LOCK:
                _BLAST_RADIUS_CACHE_SHARED[cache_key] = (now, 0)
                self._evict_blast_radius_cache_if_oversized()
            return 0

        # Slice 12H — bounded legacy fallback. The prior loop ran
        # ``scan_root.rglob("*.py")`` with no scan / wall-clock bound
        # and ``py_file.read_text()`` (unbounded) per file. On the
        # element-web worktree (56K+ files including a generated
        # node_modules tree that the substring "venv"/"__pycache__"
        # skip missed), this was a 5-min sync wedge. LoopDeadman
        # surfaced the stack trace in bt-2026-05-22-215354.
        #
        # The bounded walker now:
        #   * Skips high-cardinality dirs at the directory level
        #     (.git, node_modules, dist, build, .venv, venv,
        #     __pycache__, .next, coverage, ...) using
        #     ``default_skip_dirs()``.
        #   * Enforces ``JARVIS_BLAST_RADIUS_MAX_SCANNED`` (default
        #     20_000) entries scanned ceiling.
        #   * Enforces ``JARVIS_BLAST_RADIUS_TIMEOUT_S`` (default
        #     10.0s) wall-clock ceiling.
        #   * Bounds per-file read to
        #     ``JARVIS_BLAST_RADIUS_MAX_BYTES_PER_FILE`` (default
        #     64 KB) — protects against generated min.js / .map
        #     files even after dir-level skip.
        #
        # On budget exhaustion (timeout / scan-cap hit) the method
        # returns ``JARVIS_BLAST_RADIUS_CONSERVATIVE_CAP`` (default
        # 50, matching the prior in-loop cap). Bias toward
        # CAUTION (high blast radius), never toward false safety.
        from backend.core.ouroboros.governance.bounded_walker import (  # noqa: E501
            blast_radius_conservative_cap,
            blast_radius_max_bytes_per_file,
            blast_radius_max_scanned,
            blast_radius_timeout_s,
            bounded_read_text,
            default_skip_dirs,
            iter_bounded_files,
        )
        _scan_start = time.monotonic()
        _scan_max = blast_radius_max_scanned()
        _scan_timeout_s = blast_radius_timeout_s()
        _per_file_bytes = blast_radius_max_bytes_per_file()
        _conservative_cap = blast_radius_conservative_cap()
        _skip = default_skip_dirs()

        importers = 0
        _files_examined = 0
        _budget_exhausted = False
        for path_str in iter_bounded_files(
            scan_root,
            max_scanned=_scan_max,
            timeout_s=_scan_timeout_s,
            skip_dirs=_skip,
        ):
            # Domain filter: only Python files participate in the
            # importer scan.
            if not path_str.endswith(".py"):
                continue
            _files_examined += 1
            # Bounded read — replaces unbounded read_text. Reads
            # the first _per_file_bytes only; sufficient for
            # import-statement substring matching (imports live in
            # the file head). Defensive against multi-MB generated
            # min.js / build artifacts that slipped past the
            # directory-level skip filter.
            content = bounded_read_text(
                Path(path_str), max_bytes=_per_file_bytes,
            )
            if content is None:
                continue
            if any(mod in content for mod in target_modules):
                importers += 1
            if importers >= _conservative_cap:
                # Reached the historical in-loop cap with the
                # scan budget still intact — same semantic as
                # before but via bounded walker. Not a budget
                # exhaustion; record the actual count.
                break
        else:
            # ``iter_bounded_files`` terminates cleanly on budget
            # exhaustion (returns without yielding). Distinguish:
            # if we didn't hit the importer cap AND the walker
            # stopped, check elapsed against budget. When walker
            # returns due to time / scan caps we lack ground truth
            # on the actual importer count.
            _elapsed_ms = (time.monotonic() - _scan_start) * 1000.0
            if (
                importers < _conservative_cap
                and (
                    _elapsed_ms >= _scan_timeout_s * 1000.0 * 0.95
                    or _files_examined >= _scan_max // 4
                )
            ):
                _budget_exhausted = True

        _elapsed_ms = (time.monotonic() - _scan_start) * 1000.0
        if _budget_exhausted:
            # Conservative return — bias toward caution, NOT false
            # safety. Operator binding: "If the scan budget is
            # exhausted, return a conservative high blast-radius
            # value, e.g. existing cap 50, and log/record
            # 'blast_radius_scan_budget_exhausted'."
            try:
                logger.info(
                    "[Advisor] blast_radius_scan_budget_exhausted "
                    "root=%s targets=%d files_examined=%d "
                    "importers_found_partial=%d elapsed_ms=%.1f "
                    "conservative_cap=%d — returning cap (bias=caution)",
                    str(scan_root), len(target_modules),
                    _files_examined, importers, _elapsed_ms,
                    _conservative_cap,
                )
            except Exception:  # noqa: BLE001
                pass
            importers = _conservative_cap
        else:
            try:
                logger.debug(
                    "[Advisor] blast_radius_legacy_scan_complete "
                    "root=%s targets=%d files_examined=%d "
                    "importers=%d elapsed_ms=%.1f",
                    str(scan_root), len(target_modules),
                    _files_examined, importers, _elapsed_ms,
                )
            except Exception:  # noqa: BLE001
                pass

        # Record + bound the cache.  FIFO eviction is acceptable because the
        # TTL already bounds entry age; max-entries is a defensive memory
        # ceiling not the primary freshness mechanism.
        with _BLAST_RADIUS_CACHE_LOCK:
            _BLAST_RADIUS_CACHE_SHARED[cache_key] = (now, importers)
            self._evict_blast_radius_cache_if_oversized()
        return importers

    def _evict_blast_radius_cache_if_oversized(self) -> None:
        """Drop the oldest entries until the cache fits.

        FIFO eviction (relies on dict insertion order being preserved in
        Python 3.7+).  TTL pruning is implicit at lookup time — expired
        entries get overwritten by the next compute pass with the same key,
        and unused expired entries are evicted only when memory pressure
        (max_entries) forces it.  Acceptable for a 60s-TTL cache.

        MUST be called with ``_BLAST_RADIUS_CACHE_LOCK`` held — the
        eviction races on the shared module-level dict.
        """
        cache = _BLAST_RADIUS_CACHE_SHARED
        while len(cache) > _BLAST_RADIUS_CACHE_MAX_ENTRIES:
            # popitem(last=False) is dict-method on Python 3.7+ via
            # iter(cache).  Use next(iter(...)) for explicit FIFO.
            try:
                oldest = next(iter(cache))
                del cache[oldest]
            except (StopIteration, KeyError):
                break

    def _compute_test_coverage(
        self,
        target_files: Tuple[str, ...],
        *,
        root: Optional[Path] = None,
    ) -> float:
        """Fraction of target files that have corresponding test files.

        ``root`` (B.2.0) — scan tree. Defaults to ``self._project_root`` when
        ``None`` (pre-B.2.0 behavior).
        """
        scan_root = root if root is not None else self._project_root
        if not target_files:
            return 1.0
        py_files = [f for f in target_files if f.endswith(".py") and "test_" not in f]
        if not py_files:
            return 1.0

        covered = 0
        for f in py_files:
            stem = Path(f).stem
            if any((scan_root / "tests" / f"test_{stem}.py").exists()
                   for _ in [1]):
                covered += 1
        return covered / len(py_files)

    def _get_chronic_entropy(
        self, target_files: Tuple[str, ...], description: str,
    ) -> float:
        """Get chronic failure rate from LearningConsolidator."""
        try:
            from backend.core.ouroboros.governance.entropy_calculator import extract_domain_key
            from backend.core.ouroboros.governance.adaptive_learning import LearningConsolidator
            domain = extract_domain_key(target_files, description)
            consolidator = LearningConsolidator()
            rules = consolidator.get_rules_for_domain(domain)
            for rule in rules:
                if rule.rule_type == "common_failure":
                    return rule.confidence
        except Exception:
            pass
        return 0.0

    def _check_staleness(
        self,
        target_files: Tuple[str, ...],
        *,
        root: Optional[Path] = None,
    ) -> List[str]:
        """Find files not modified in 90+ days. Git-free check via mtime.

        ``root`` (B.2.0) — scan tree. Defaults to ``self._project_root`` when
        ``None`` (pre-B.2.0 behavior).
        """
        scan_root = root if root is not None else self._project_root
        stale = []
        cutoff = time.time() - (90 * 86400)
        for f in target_files:
            full = scan_root / f
            if full.exists():
                try:
                    if full.stat().st_mtime < cutoff:
                        stale.append(f)
                except Exception:
                    pass
        return stale

    def _check_large_files(
        self,
        target_files: Tuple[str, ...],
        *,
        root: Optional[Path] = None,
    ) -> List[Tuple[str, int]]:
        """Find files with >500 lines.

        ``root`` (B.2.0) — scan tree. Defaults to ``self._project_root`` when
        ``None`` (pre-B.2.0 behavior).
        """
        scan_root = root if root is not None else self._project_root
        large = []
        for f in target_files:
            full = scan_root / f
            if full.exists() and f.endswith(".py"):
                try:
                    lines = len(full.read_text().split("\n"))
                    if lines > 500:
                        large.append((f, lines))
                except Exception:
                    pass
        return large

    @staticmethod
    def _build_voice_message(
        decision: AdvisoryDecision,
        reasons: List[str],
        target_files: Tuple[str, ...],
    ) -> str:
        """Build JARVIS-style voice message."""
        target = target_files[0] if target_files else "these files"

        if decision == AdvisoryDecision.RECOMMEND:
            return ""
        elif decision == AdvisoryDecision.CAUTION:
            return f"Proceeding with caution on {Path(target).name}. {reasons[0] if reasons else ''}"
        elif decision == AdvisoryDecision.ADVISE_AGAINST:
            return (
                f"I wouldn't recommend modifying {Path(target).name} right now. "
                f"{reasons[0] if reasons else 'The risk profile is elevated.'}"
            )
        elif decision == AdvisoryDecision.BLOCK:
            return (
                f"I'm blocking this operation on {Path(target).name}. "
                f"{reasons[0] if reasons else 'Safety threshold exceeded.'}"
            )
        return ""


# ---------------------------------------------------------------------------
# FlagRegistry self-registration (auto-discovered by §33.3 walker)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Returns count successfully
    registered. NEVER raises."""
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
            name=ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=True,
            description=(
                "B.2.0 master switch — graduated to default-TRUE 2026-05-13 "
                "(§33.1). When ON (default), the "
                "OperationAdvisor consumes a per-envelope ``repo_root`` "
                "string from intake_evidence_json and scans THAT tree for "
                "blast radius / coverage / staleness / large-file signals "
                "instead of the orchestrator's constructor-bound "
                "project_root. Source-agnostic — no branch on "
                "envelope.source. Enabling layer for SWE-Bench-Pro Phase 2 "
                "+ permanent improvement for L3 worktree-isolated work + "
                "the in-repo L2 exercise corpus. Untrusted-input contract "
                "enforced by resolve_envelope_repo_root."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/operation_advisor.py"
            ),
            example="false",
            since="v3.7 Phase 2 Phase B.2.0 (2026-05-12)",
        ),
        FlagSpec(
            name=ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR,
            type=FlagType.STR,
            default="",
            description=(
                "Colon-separated absolute-path prefixes that supplement "
                "the orchestrator's project_root as allowed locations for "
                "envelope-provided ``repo_root`` overrides. Default empty "
                "= project_root only (covers in-repo worktrees + L3 "
                ".worktrees/ + .jarvis/swe_bench_pro/worktrees/). Each "
                "entry is Path.resolve()'d at parse time so symlinks "
                "cannot escape the allowlist after the fact. Entries "
                "outside this allowlist are rejected and the advisor "
                "falls back to the constructor-bound project_root."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/operation_advisor.py"
            ),
            example="/private/tmp/eval-clones:/var/jarvis/scratch",
            since="v3.7 Phase 2 Phase B.2.0 (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Advisor] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR",
    "ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR",
    "EVIDENCE_REPO_ROOT_KEY",
    "Advisory",
    "AdvisoryDecision",
    "OperationAdvisor",
    "infer_read_only_intent",
    "register_flags",
    "resolve_envelope_repo_root",
    "RepoRootPromiseStatus",
    "EnvelopeRepoRootRejected",
    "envelope_repo_root_status",
    "guard_envelope_repo_root",
]
