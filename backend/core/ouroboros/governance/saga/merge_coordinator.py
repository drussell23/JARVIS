"""Deterministic merge coordinator for L3 execution-graph work units."""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Tuple

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    MergeDecision,
    WorkUnitResult,
    WorkUnitState,
)
from backend.core.ouroboros.governance.saga.saga_types import RepoPatch


class MergeCoordinator:
    """Build deterministic merge batches and merge repo patches."""

    def build_barrier_batches(
        self,
        graph: ExecutionGraph,
        results: Dict[str, WorkUnitResult],
    ) -> Tuple[MergeDecision, ...]:
        """Group successful work units into deterministic repo/barrier batches."""
        unit_map = graph.unit_map
        successful = [
            result
            for result in results.values()
            if result.status is WorkUnitState.COMPLETED and result.patch is not None
        ]
        if not successful:
            raise RuntimeError("merge_coordinator:no_successful_work_units")

        grouped: Dict[Tuple[str, str], List[str]] = {}
        for result in successful:
            unit = unit_map[result.unit_id]
            barrier_id = unit.barrier_id or unit.unit_id
            grouped.setdefault((result.repo, barrier_id), []).append(result.unit_id)

        decisions: List[MergeDecision] = []
        for repo, barrier_id in sorted(grouped.keys()):
            unit_ids = tuple(sorted(grouped[(repo, barrier_id)]))
            seen_paths = set()
            conflicts = set()
            for unit_id in unit_ids:
                unit = unit_map[unit_id]
                for path in unit.effective_owned_paths:
                    if path in seen_paths:
                        conflicts.add(unit_id)
                    else:
                        seen_paths.add(path)
            if conflicts:
                raise RuntimeError(
                    f"merge_coordinator:owned_path_conflict:{repo}:{barrier_id}:{sorted(conflicts)}"
                )

            payload = {
                "graph_id": graph.graph_id,
                "repo": repo,
                "barrier_id": barrier_id,
                "merged_unit_ids": unit_ids,
                "skipped_unit_ids": (),
                "conflict_units": (),
            }
            decision_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            decisions.append(
                MergeDecision(
                    graph_id=graph.graph_id,
                    barrier_id=barrier_id,
                    repo=repo,
                    merged_unit_ids=unit_ids,
                    skipped_unit_ids=(),
                    conflict_units=(),
                    decision_hash=decision_hash,
                )
            )

        return tuple(decisions)

    def merge_repo_patches(
        self,
        decisions: Tuple[MergeDecision, ...],
        results: Dict[str, WorkUnitResult],
    ) -> Dict[str, RepoPatch]:
        """Merge work-unit repo patches into a single RepoPatch per repo."""
        if not decisions:
            raise RuntimeError("merge_coordinator:no_decisions")

        merged: Dict[str, RepoPatch] = {}
        for decision in sorted(decisions, key=lambda d: (d.repo, d.barrier_id, d.merged_unit_ids)):
            files = list(merged.get(decision.repo, RepoPatch(repo=decision.repo, files=())).files)
            new_content = list(
                merged.get(decision.repo, RepoPatch(repo=decision.repo, files=())).new_content
            )
            existing_paths = {pf.path for pf in files}
            existing_new_paths = {path for path, _ in new_content}

            for unit_id in decision.merged_unit_ids:
                result = results[unit_id]
                if result.patch is None:
                    continue
                for patched in result.patch.files:
                    if patched.path in existing_paths:
                        raise RuntimeError(
                            f"merge_coordinator:duplicate_file_path:{decision.repo}:{patched.path}"
                        )
                    files.append(patched)
                    existing_paths.add(patched.path)
                for path, content in result.patch.new_content:
                    if path in existing_new_paths:
                        raise RuntimeError(
                            f"merge_coordinator:duplicate_new_content:{decision.repo}:{path}"
                        )
                    new_content.append((path, content))
                    existing_new_paths.add(path)

            merged[decision.repo] = RepoPatch(
                repo=decision.repo,
                files=tuple(files),
                new_content=tuple(new_content),
            )

        return merged
