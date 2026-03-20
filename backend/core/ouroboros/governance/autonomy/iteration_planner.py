"""Iteration task sourcing and planning for the Autonomy Iteration Mode.

This module provides three public entry points:

- **IterationTaskSource** — hybrid task selection from backlog JSON and the
  ``OpportunityMinerSensor``, with fairness rotation and poison filtering.
- **IterationPlanner** — 4-step planning pipeline that expands files via the
  Oracle, performs dependency analysis, partitions into work units, and
  assembles a validated ``ExecutionGraph``.
- **select_acceptance_tests** — deterministic heuristic mapping from source
  files to their corresponding test files on disk.

All public methods are async-safe. ``IterationPlanner.plan()`` is guaranteed
to return a ``PlannerOutcome``; it never returns ``None`` and never raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.core.ouroboros.governance.autonomy.iteration_types import (
    BlastRadiusPolicy,
    IterationTask,
    PlannedGraphMetadata,
    PlannerOutcome,
    PlannerRejectReason,
    PlanningContext,
    TaskRejectionTracker,
    compute_plan_id,
    compute_task_fingerprint,
)
from backend.core.ouroboros.governance.autonomy.path_utils import (
    PathTraversalError,
    canonicalize_path,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitSpec,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ACCEPTANCE_TESTS_PER_UNIT = 5
_PLANNER_ID = "iteration_planner_v1"
_SCHEMA_VERSION = "3.0"


# ---------------------------------------------------------------------------
# select_acceptance_tests
# ---------------------------------------------------------------------------


def select_acceptance_tests(
    target_files: Tuple[str, ...],
    repo_root: Path,
) -> Tuple[str, ...]:
    """Map source files to acceptance test files that exist on disk.

    Applies three rules in order for each target file:

    - **Rule 1**: ``backend/foo/bar.py`` -> ``tests/test_foo/test_bar.py``
    - **Rule 2**: ``backend/foo/bar.py`` -> ``tests/foo/test_bar.py``
    - **Rule 3**: If no match, search the Rule-1 parent directory for any
      ``test_*.py`` files.

    Results are deduplicated, sorted for determinism, and capped at
    ``_MAX_ACCEPTANCE_TESTS_PER_UNIT``.  Only files that actually exist on
    disk are included.

    Parameters
    ----------
    target_files:
        Tuple of relative source file paths.
    repo_root:
        Absolute path to the repository root.

    Returns
    -------
    Tuple[str, ...]
        Deterministic tuple of relative test file paths.
    """
    found: Set[str] = set()

    for src in target_files:
        src_path = Path(src)
        parts = src_path.parts

        # Determine the sub-path after the top-level directory (e.g. 'backend')
        if len(parts) < 2:
            # Single-component path like "foo.py" — no sub-directory structure
            stem = src_path.stem
            _try_add(repo_root, f"tests/test_{stem}.py", found)
            continue

        # parts = ("backend", "foo", "bar.py") or ("backend", "core", "auth.py")
        sub_parts = parts[1:]  # everything after the top-level dir
        stem = src_path.stem

        # Rule 1: tests/test_{sub_dir}/test_{stem}.py
        if len(sub_parts) >= 2:
            sub_dir = "/".join(f"test_{p}" for p in sub_parts[:-1])
            rule1 = f"tests/{sub_dir}/test_{stem}.py"
        else:
            rule1 = f"tests/test_{sub_parts[0]}" if sub_parts[0] != f"{stem}.py" else f"tests/test_{stem}.py"
            # Adjust for the case: backend/bar.py -> tests/test_bar.py
            rule1 = f"tests/test_{stem}.py"

        matched_specific = _try_add(repo_root, rule1, found)

        # Rule 2: tests/{sub_dir}/test_{stem}.py (without test_ prefix on dir)
        if len(sub_parts) >= 2:
            sub_dir_plain = "/".join(str(p) for p in sub_parts[:-1])
            rule2 = f"tests/{sub_dir_plain}/test_{stem}.py"
        else:
            rule2 = f"tests/test_{stem}.py"

        if not matched_specific:
            matched_specific = _try_add(repo_root, rule2, found)

        # Rule 3: fallback — search parent dir from Rule 1 for test_*.py
        if not matched_specific:
            if len(sub_parts) >= 2:
                parent_dir = repo_root / "tests" / "/".join(
                    f"test_{p}" for p in sub_parts[:-1]
                )
            else:
                parent_dir = repo_root / "tests"

            if parent_dir.is_dir():
                for test_file in sorted(parent_dir.glob("test_*.py")):
                    try:
                        rel = str(test_file.relative_to(repo_root))
                        found.add(rel)
                    except ValueError:
                        continue

    # Deterministic sort, cap at limit
    result = sorted(found)[:_MAX_ACCEPTANCE_TESTS_PER_UNIT]
    return tuple(result)


def _try_add(repo_root: Path, rel_path: str, found: Set[str]) -> bool:
    """Add *rel_path* to *found* if it exists on disk. Return True if added."""
    if (repo_root / rel_path).is_file():
        found.add(rel_path)
        return True
    return False


# ---------------------------------------------------------------------------
# IterationTaskSource
# ---------------------------------------------------------------------------


class IterationTaskSource:
    """Hybrid task source: backlog JSON + opportunity miner, with poison guard.

    Parameters
    ----------
    backlog_path:
        Absolute path to the ``backlog.json`` file.
    miner:
        Optional ``OpportunityMinerSensor`` (or any object with an async
        ``scan_once()`` method returning a list of ``StaticCandidate``).
    rejection_tracker:
        Shared tracker used to skip poisoned tasks.
    """

    def __init__(
        self,
        backlog_path: Path,
        miner: Optional[Any],
        rejection_tracker: TaskRejectionTracker,
    ) -> None:
        self._backlog_path = backlog_path
        self._miner = miner
        self._rejection_tracker = rejection_tracker

    # -- backlog ----------------------------------------------------------

    async def get_backlog_tasks(self) -> List[IterationTask]:
        """Read backlog JSON, filter pending, sort by priority desc, skip poisoned."""
        if not self._backlog_path.is_file():
            return []

        try:
            raw = self._backlog_path.read_text(encoding="utf-8")
            items = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "IterationTaskSource: failed to read backlog %s: %s",
                self._backlog_path, exc,
            )
            return []

        if not isinstance(items, list):
            logger.warning(
                "IterationTaskSource: backlog is not a JSON array: %s",
                self._backlog_path,
            )
            return []

        tasks: List[IterationTask] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            if entry.get("status") != "pending":
                continue
            task_id = str(entry.get("task_id", ""))
            if not task_id:
                continue
            if self._rejection_tracker.is_poisoned(task_id):
                continue

            tasks.append(IterationTask(
                task_id=task_id,
                source="backlog",
                description=str(entry.get("description", "")),
                target_files=tuple(entry.get("target_files", ())),
                repo=str(entry.get("repo", "jarvis")),
                priority=int(entry.get("priority", 0)),
                requires_human_ack=bool(entry.get("requires_human_ack", False)),
                evidence=entry.get("evidence") or {},
            ))

        # Sort by priority descending (stable sort preserves insertion order
        # for equal priorities)
        tasks.sort(key=lambda t: t.priority, reverse=True)
        return tasks

    # -- miner ------------------------------------------------------------

    async def get_miner_tasks(self) -> List[IterationTask]:
        """Run miner scan, convert candidates to IterationTasks, skip poisoned."""
        if self._miner is None:
            return []

        try:
            candidates = await self._miner.scan_once()
        except Exception as exc:
            logger.warning("IterationTaskSource: miner scan failed: %s", exc)
            return []

        tasks: List[IterationTask] = []
        for cand in candidates:
            file_path = getattr(cand, "file_path", "")
            cc = getattr(cand, "cyclomatic_complexity", 0)
            score = getattr(cand, "static_evidence_score", 0.0)

            task_id = f"miner-{hashlib.sha256(file_path.encode()).hexdigest()[:12]}"
            if self._rejection_tracker.is_poisoned(task_id):
                continue

            tasks.append(IterationTask(
                task_id=task_id,
                source="opportunity_miner",
                description=f"High complexity in {file_path} (CC={cc})",
                target_files=(file_path,),
                repo="jarvis",
                priority=int(score * 10),
                requires_human_ack=True,  # AC2: miner tasks always need ack
                evidence={
                    "cyclomatic_complexity": cc,
                    "static_evidence_score": score,
                },
            ))

        return tasks

    # -- hybrid selection -------------------------------------------------

    async def select_task(
        self,
        cycle_count: int,
        fairness_interval: int,
    ) -> Optional[IterationTask]:
        """Select the next task using hybrid fairness rotation.

        Every *fairness_interval* cycles the miner gets priority;
        otherwise backlog is checked first with miner as fallback.

        Parameters
        ----------
        cycle_count:
            Current iteration cycle number (1-based).
        fairness_interval:
            How often (in cycles) the miner gets priority.

        Returns
        -------
        Optional[IterationTask]
            The selected task, or ``None`` if no tasks are available.
        """
        miner_first = (
            fairness_interval > 0
            and cycle_count > 0
            and cycle_count % fairness_interval == 0
        )

        if miner_first:
            primary = await self.get_miner_tasks()
            secondary = await self.get_backlog_tasks()
        else:
            primary = await self.get_backlog_tasks()
            secondary = await self.get_miner_tasks()

        if primary:
            return primary[0]
        if secondary:
            return secondary[0]
        return None


# ---------------------------------------------------------------------------
# IterationPlanner
# ---------------------------------------------------------------------------


class IterationPlanner:
    """4-step planning pipeline: expand -> analyze -> partition -> assemble.

    ``plan()`` is guaranteed to return a ``PlannerOutcome``; it never
    returns ``None`` and never raises an exception to the caller.

    Parameters
    ----------
    oracle:
        Optional ``TheOracle`` instance for semantic search and dependency
        analysis.  When ``None``, the planner falls back to using only
        the task's ``target_files``.
    blast_radius:
        Policy governing maximum scope of a planned iteration.
    rejection_tracker:
        Shared tracker — rejections are recorded for poison tracking.
    repo_root:
        Absolute path to the repository root used for path canonicalization.
    """

    def __init__(
        self,
        oracle: Optional[Any],
        blast_radius: BlastRadiusPolicy,
        rejection_tracker: TaskRejectionTracker,
        repo_root: Path,
    ) -> None:
        self._oracle = oracle
        self._blast_radius = blast_radius
        self._rejection_tracker = rejection_tracker
        self._repo_root = repo_root

    async def plan(
        self,
        task: IterationTask,
        iteration_id: str,
        context: PlanningContext,
    ) -> PlannerOutcome:
        """Execute the 4-step planning pipeline.

        Steps
        -----
        1. **File expansion** — merge Oracle semantic search results with
           ``task.target_files``, deduplicate, canonicalize, cap at blast
           radius.
        2. **Dependency analysis** — if Oracle available, get file
           neighborhood; else treat all files as independent.
        3. **Unit partitioning** — group dependent files into the same work
           unit, independent files into separate units.
        4. **Graph assembly** — build ``ExecutionGraph``, validate DAG,
           check blast radius.

        Returns
        -------
        PlannerOutcome
            Always non-``None``.  ``status`` is ``"accepted"`` or
            ``"rejected"``.
        """
        try:
            return await self._plan_inner(task, iteration_id, context)
        except Exception as exc:
            logger.error(
                "IterationPlanner.plan() unexpected error for task %s: %s",
                task.task_id, exc, exc_info=True,
            )
            reason = PlannerRejectReason.ZERO_ACTIONABLE_UNITS
            self._rejection_tracker.record_rejection(task.task_id, reason)
            return PlannerOutcome(
                status="rejected",
                reject_reason=reason,
                metadata=PlannedGraphMetadata(
                    selection_proof=f"task_id={task.task_id}",
                    expansion_proof="error_during_planning",
                    partition_proof="",
                    reject_reason_code=reason.value,
                    planning_context=context,
                ),
            )

    async def _plan_inner(
        self,
        task: IterationTask,
        iteration_id: str,
        context: PlanningContext,
    ) -> PlannerOutcome:
        """Core planning logic, may raise on unexpected errors."""

        # -- Step 1: File expansion ----------------------------------------
        expanded_files, expansion_proof = await self._expand_files(task)

        # Canonicalize all paths
        canonical_files: List[str] = []
        for f in expanded_files:
            try:
                canonical_files.append(canonicalize_path(f, self._repo_root))
            except PathTraversalError:
                logger.warning("Skipping path outside repo root: %s", f)

        # Deduplicate
        seen: Set[str] = set()
        deduped: List[str] = []
        for f in canonical_files:
            if f not in seen and f != ".":
                seen.add(f)
                deduped.append(f)

        # Cap at blast radius
        max_files = self._blast_radius.max_files_changed
        if len(deduped) > max_files:
            reason = PlannerRejectReason.BLAST_RADIUS_EXCEEDED
            self._rejection_tracker.record_rejection(task.task_id, reason)
            return PlannerOutcome(
                status="rejected",
                reject_reason=reason,
                metadata=PlannedGraphMetadata(
                    selection_proof=f"task_id={task.task_id}",
                    expansion_proof=expansion_proof,
                    partition_proof="",
                    reject_reason_code=reason.value,
                    planning_context=context,
                ),
            )

        if not deduped:
            reason = PlannerRejectReason.ZERO_ACTIONABLE_UNITS
            self._rejection_tracker.record_rejection(task.task_id, reason)
            return PlannerOutcome(
                status="rejected",
                reject_reason=reason,
                metadata=PlannedGraphMetadata(
                    selection_proof=f"task_id={task.task_id}",
                    expansion_proof=expansion_proof,
                    partition_proof="",
                    reject_reason_code=reason.value,
                    planning_context=context,
                ),
            )

        # -- Step 2: Dependency analysis -----------------------------------
        dep_edges = self._analyze_dependencies(deduped)

        # -- Step 3: Unit partitioning -------------------------------------
        groups = self._partition_into_groups(deduped, dep_edges)
        partition_proof = (
            f"groups={len(groups)}, "
            f"files={len(deduped)}, "
            f"edges={len(dep_edges)}"
        )

        # -- Step 4: Graph assembly ----------------------------------------
        fingerprint = compute_task_fingerprint(
            task.description, tuple(sorted(deduped)),
        )
        plan_id = compute_plan_id(
            fingerprint, context.policy_hash, (task.repo,),
        )

        units: List[WorkUnitSpec] = []
        unit_id_map: Dict[str, str] = {}  # file -> unit_id, for dep resolution

        for idx, group_files in enumerate(groups):
            unit_id = f"{plan_id}-u{idx}"
            for f in group_files:
                unit_id_map[f] = unit_id

        # Build dependency_ids between units
        for idx, group_files in enumerate(groups):
            unit_id = f"{plan_id}-u{idx}"
            dep_unit_ids: Set[str] = set()
            for f in group_files:
                for src, dst in dep_edges:
                    if f == src and dst not in group_files:
                        other_uid = unit_id_map.get(dst)
                        if other_uid and other_uid != unit_id:
                            dep_unit_ids.add(other_uid)
                    elif f == dst and src not in group_files:
                        other_uid = unit_id_map.get(src)
                        if other_uid and other_uid != unit_id:
                            dep_unit_ids.add(other_uid)

            target_tuple = tuple(sorted(group_files))
            acceptance = select_acceptance_tests(target_tuple, self._repo_root)

            units.append(WorkUnitSpec(
                unit_id=unit_id,
                repo=task.repo,
                goal=task.description,
                target_files=target_tuple,
                dependency_ids=tuple(sorted(dep_unit_ids)),
                owned_paths=target_tuple,
                max_attempts=2,
                timeout_s=180.0,
                acceptance_tests=acceptance,
            ))

        # Validate by constructing the graph — catches cycles
        try:
            graph = ExecutionGraph(
                graph_id=plan_id,
                op_id=iteration_id,
                planner_id=_PLANNER_ID,
                schema_version=_SCHEMA_VERSION,
                units=tuple(units),
                concurrency_limit=max(1, len(units)),
            )
        except ValueError as exc:
            error_msg = str(exc)
            if "cycle" in error_msg.lower():
                reason = PlannerRejectReason.DAG_CYCLE_DETECTED
            else:
                reason = PlannerRejectReason.ZERO_ACTIONABLE_UNITS
            self._rejection_tracker.record_rejection(task.task_id, reason)
            return PlannerOutcome(
                status="rejected",
                reject_reason=reason,
                metadata=PlannedGraphMetadata(
                    selection_proof=f"task_id={task.task_id}",
                    expansion_proof=expansion_proof,
                    partition_proof=partition_proof,
                    reject_reason_code=reason.value,
                    planning_context=context,
                ),
            )

        # Blast radius check on file count (post-expansion, post-dedup)
        violation = self._blast_radius.check_file_count(len(deduped))
        if violation:
            reason = PlannerRejectReason.BLAST_RADIUS_EXCEEDED
            self._rejection_tracker.record_rejection(task.task_id, reason)
            return PlannerOutcome(
                status="rejected",
                reject_reason=reason,
                metadata=PlannedGraphMetadata(
                    selection_proof=f"task_id={task.task_id}",
                    expansion_proof=expansion_proof,
                    partition_proof=partition_proof,
                    reject_reason_code=reason.value,
                    planning_context=context,
                ),
            )

        metadata = PlannedGraphMetadata(
            selection_proof=f"task_id={task.task_id}, fingerprint={fingerprint[:16]}",
            expansion_proof=expansion_proof,
            partition_proof=partition_proof,
            reject_reason_code=None,
            planning_context=context,
        )

        return PlannerOutcome(
            status="accepted",
            graph=graph,
            metadata=metadata,
        )

    # -- Step 1 helpers ---------------------------------------------------

    async def _expand_files(
        self, task: IterationTask
    ) -> Tuple[List[str], str]:
        """Expand task target files via Oracle semantic search.

        Returns ``(expanded_file_list, expansion_proof_string)``.
        """
        base_files = list(task.target_files)

        if self._oracle is None:
            return base_files, "no_oracle_available; using task.target_files only"

        try:
            search_results = await self._oracle.semantic_search(
                task.description, k=10,
            )
        except Exception as exc:
            logger.warning("Oracle semantic_search failed: %s", exc)
            return base_files, f"oracle_search_error: {exc}"

        oracle_files: List[str] = []
        for key, score in search_results:
            # key format: "repo:relative/path"
            if ":" in key:
                _, rel_path = key.split(":", 1)
            else:
                rel_path = key
            oracle_files.append(rel_path)

        # Merge: task files first, then oracle files
        merged = list(base_files)
        base_set = set(base_files)
        for f in oracle_files:
            if f not in base_set:
                merged.append(f)
                base_set.add(f)

        proof = (
            f"oracle_expanded; "
            f"base_count={len(base_files)}, "
            f"oracle_hits={len(oracle_files)}, "
            f"merged_count={len(merged)}"
        )
        return merged, proof

    # -- Step 2 helpers ---------------------------------------------------

    def _analyze_dependencies(
        self, files: List[str]
    ) -> List[Tuple[str, str]]:
        """Return directed dependency edges between files in the plan.

        Each edge is ``(source_file, dependency_file)`` meaning *source*
        depends on *dependency*.
        """
        if self._oracle is None:
            return []

        try:
            file_paths = [Path(self._repo_root / f) for f in files]
            neighborhood = self._oracle.get_file_neighborhood(file_paths)
        except Exception as exc:
            logger.warning("Oracle get_file_neighborhood failed: %s", exc)
            return []

        file_set = set(files)
        edges: List[Tuple[str, str]] = []

        # imports: files[i] imports neighbor -> edge (files[i], neighbor)
        for imp in getattr(neighborhood, "imports", []):
            rel = _strip_repo_prefix(imp)
            if rel in file_set:
                # Find which of our files imports this
                for f in files:
                    if f != rel:
                        edges.append((f, rel))
                        break

        # importers: neighbor imports files[i] -> edge (neighbor, files[i])
        for imp in getattr(neighborhood, "importers", []):
            rel = _strip_repo_prefix(imp)
            if rel in file_set:
                for f in files:
                    if f != rel:
                        edges.append((rel, f))
                        break

        # Deduplicate edges
        return list(set(edges))

    # -- Step 3 helpers ---------------------------------------------------

    def _partition_into_groups(
        self,
        files: List[str],
        dep_edges: List[Tuple[str, str]],
    ) -> List[List[str]]:
        """Group files into work units based on dependency edges.

        Files connected by dependencies are placed in the same group.
        Independent files get their own group.  Uses union-find for
        efficient grouping.
        """
        # Union-Find
        parent: Dict[str, str] = {f: f for f in files}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for src, dst in dep_edges:
            if src in parent and dst in parent:
                union(src, dst)

        # Collect groups
        groups_map: Dict[str, List[str]] = {}
        for f in files:
            root = find(f)
            groups_map.setdefault(root, []).append(f)

        # Sort files within each group for determinism
        return [sorted(g) for g in sorted(groups_map.values(), key=lambda g: g[0])]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _strip_repo_prefix(path: str) -> str:
    """Strip ``"repo:"`` prefix from an Oracle path key, if present."""
    if ":" in path:
        return path.split(":", 1)[1]
    return path
