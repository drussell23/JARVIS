"""Cross-Repo Scope Promoter — the ignition wire for the dormant multi-repo Saga mesh.

The full cross-repo orchestration substrate already exists and is wired (RepoRegistry +
`OrchestratorConfig.resolve_repo_roots` + the Oracle's unified jarvis|prime|reactor graph + 2c.1
multi-repo candidate generation + `SagaApplyStrategy` with compensating all-or-nothing rollback +
`CrossRepoVerifier`). It only ever engages when an op's `repo_scope` spans >1 repo
(`cross_repo = len(repo_scope) > 1`) — and **nothing in the live pipeline ever creates such an op**.

This module is that missing trigger. Before GENERATE, it consults the **unified** Oracle graph: if a
fault's dependency cone (deps / dependents / call-chains) crosses from the primary repo into a sibling
(`reactor`/`prime`), it **elevates the op's `repo_scope`** to span both — instantly igniting the native
Saga pipeline — and forces the op to **Orange-tier (APPROVAL_REQUIRED)**, because autonomous mutation
of a second repository is a real blast-radius escalation.

Armor — Topological Cascade Shield (Blast-Radius Governor): before promoting, it measures how *deep*
the cross-boundary blast reaches into the sibling. If that depth exceeds
`JARVIS_MAX_SAGA_CASCADE_DEPTH`, it **shards** the op — capping the sibling's mutable surface to the
immediate boundary-interface files and shielding the sibling's deep internal logic from unintended
rewrites.

Design: pure + injectable + fail-soft (any error → no promotion → single-repo behavior unchanged).
The synchronous lazy-graph queries are offloaded via `asyncio.to_thread` (zero block on the loop).
Gated `JARVIS_CROSS_REPO_PROMOTER_ENABLED` (default OFF → byte-identical single-repo pipeline).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["CrossRepoScopePromoter", "PromotionReport", "promoter_enabled"]


def promoter_enabled() -> bool:
    """``JARVIS_CROSS_REPO_PROMOTER_ENABLED`` (default OFF) — master switch for cross-repo ignition."""
    return os.environ.get("JARVIS_CROSS_REPO_PROMOTER_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _primary_repo() -> str:
    return os.environ.get("JARVIS_PRIMARY_REPO", "jarvis").strip() or "jarvis"


def _max_cascade_depth() -> int:
    """``JARVIS_MAX_SAGA_CASCADE_DEPTH`` (default 2) — boundary blast depth beyond which the op is
    sharded to boundary-interface files only (deep sibling internals shielded)."""
    try:
        return max(1, int(os.environ.get("JARVIS_MAX_SAGA_CASCADE_DEPTH", "2")))
    except ValueError:
        return 2


def _repo_of(node_key: str) -> str:
    """NodeID str is ``repo:file:name`` → the repo segment."""
    parts = str(node_key).split(":")
    return parts[0] if len(parts) >= 3 else ""


def _file_of(node_key: str) -> str:
    parts = str(node_key).split(":")
    return parts[1] if len(parts) >= 3 else str(node_key)


@dataclass
class PromotionReport:
    """Structural-delta record explaining WHY (and how) an op's scope was elevated."""

    promoted: bool = False
    primary_repo: str = "jarvis"
    cross_repos: List[str] = field(default_factory=list)
    boundary_edges: List[Tuple[str, str]] = field(default_factory=list)   # (primary_node, sibling_node)
    boundary_files: List[str] = field(default_factory=list)               # sibling interface files
    elevated_scope: Tuple[str, ...] = ()
    cascade_depth: int = 0
    sharded: bool = False                 # cascade shield engaged
    shielded_internal: List[str] = field(default_factory=list)            # deep sibling nodes shielded
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "promoted": self.promoted, "primary_repo": self.primary_repo,
            "cross_repos": list(self.cross_repos),
            "boundary_edges": [list(e) for e in self.boundary_edges],
            "boundary_files": list(self.boundary_files),
            "elevated_scope": list(self.elevated_scope),
            "cascade_depth": self.cascade_depth, "sharded": self.sharded,
            "shielded_internal": list(self.shielded_internal), "reason": self.reason,
        }

    def render(self) -> str:
        """Human-readable structural-delta visualization (shown before handing to CrossRepoVerifier)."""
        if not self.promoted:
            return ""
        lines = [
            "## CROSS-REPO SCOPE ELEVATION (Saga ignited)",
            f"Primary: {self.primary_repo}  →  Scope: {', '.join(self.elevated_scope)}",
            f"Reason: {self.reason}",
            f"Cross-boundary edges ({len(self.boundary_edges)}):",
        ]
        for a, b in self.boundary_edges[:12]:
            lines.append(f"  {a}  ──▶  {b}")
        if self.boundary_files:
            lines.append(f"Sibling boundary-interface files in scope: {', '.join(self.boundary_files[:12])}")
        if self.sharded:
            lines.append(
                f"⛨ CASCADE SHIELD ENGAGED (depth={self.cascade_depth} > "
                f"{_max_cascade_depth()}): sibling mutation capped to boundary-interface files; "
                f"{len(self.shielded_internal)} deep internal node(s) shielded from rewrite."
            )
        lines.append("Risk tier forced to APPROVAL_REQUIRED (autonomous multi-repo mutation).")
        return "\n".join(lines)


class CrossRepoScopePromoter:
    """Detects cross-boundary lineage and elevates an op to multi-repo scope. Injectable graph + caps."""

    def __init__(self, graph: Any = None, primary_repo: Optional[str] = None,
                 max_cascade_depth: Optional[int] = None) -> None:
        self._graph = graph
        self._primary = primary_repo
        self._max_depth = max_cascade_depth

    # ------------------------------------------------------------------ graph
    def _get_graph(self) -> Optional[Any]:
        if self._graph is not None:
            return self._graph
        try:
            from backend.core.ouroboros.oracle import get_oracle
            g = getattr(get_oracle(), "_graph", None)
            if g is not None and hasattr(g, "compute_blast_radius"):
                self._graph = g
                return g
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CrossRepoPromoter] oracle graph unavailable: %s", exc)
        return None

    # ------------------------------------------------------------------ analysis (sync)
    def analyze(self, target_files: Tuple[str, ...], primary_repo: str) -> PromotionReport:
        """Pure cross-boundary lineage trace over the unified graph. Never raises."""
        report = PromotionReport(primary_repo=primary_repo)
        g = self._get_graph()
        if g is None or not target_files:
            return report
        max_depth = self._max_depth if self._max_depth is not None else _max_cascade_depth()

        # Seed: the primary-repo nodes for the op's target files.
        seeds: List[str] = []
        for f in target_files:
            try:
                for n in (g.find_nodes_in_file(f) or []):
                    k = str(n)
                    if _repo_of(k) in ("", primary_repo):
                        seeds.append(k)
            except Exception:  # noqa: BLE001
                continue
        if not seeds:
            return report

        boundary_edges: List[Tuple[str, str]] = []
        cross_repos: set = set()
        boundary_files: set = set()
        shielded: set = set()
        max_observed_depth = 0

        def _is_cross(key: str) -> bool:
            r = _repo_of(key)
            return bool(r) and r != primary_repo

        for seed in seeds[:8]:                      # bound the seed fan-out
            # Distance-1 boundary: direct deps + dependents that live in a sibling repo.
            try:
                neighbors = list(g.get_dependencies(seed) or []) + list(g.get_dependents(seed) or [])
            except Exception:  # noqa: BLE001
                neighbors = []
            for nb in neighbors:
                k = str(nb)
                if _is_cross(k):
                    cross_repos.add(_repo_of(k))
                    boundary_edges.append((seed, k))
                    boundary_files.add(f"{_repo_of(k)}:{_file_of(k)}")
                    max_observed_depth = max(max_observed_depth, 1)
            # Deeper blast into siblings (transitive) → cascade-shield candidates.
            try:
                blast = g.compute_blast_radius(seed, max_depth=max_depth + 1)
                for n in (getattr(blast, "transitively_affected", set()) or set()):
                    k = str(n)
                    if _is_cross(k):
                        cross_repos.add(_repo_of(k))
                        max_observed_depth = max(max_observed_depth, 2)
                        # transitive sibling node not on the distance-1 boundary → deep internal
                        if not any(k == b for _, b in boundary_edges):
                            shielded.add(k)
            except Exception:  # noqa: BLE001
                continue

        if not cross_repos:
            return report

        report.promoted = True
        report.cross_repos = sorted(cross_repos)
        report.boundary_edges = boundary_edges[:50]
        report.boundary_files = sorted(boundary_files)[:50]
        report.cascade_depth = max_observed_depth
        report.elevated_scope = (primary_repo, *sorted(cross_repos))

        # Cascade shield: deep blast beyond the allowed depth → shard to boundary interface only.
        if max_observed_depth > max_depth and shielded:
            report.sharded = True
            report.shielded_internal = sorted(shielded)[:50]
            report.reason = (
                f"fault cone crosses {primary_repo}→{','.join(report.cross_repos)} boundary; "
                f"blast depth {max_observed_depth} exceeds cap {max_depth} → sharded to boundary files"
            )
        else:
            report.reason = (
                f"fault cone crosses {primary_repo}→{','.join(report.cross_repos)} boundary "
                f"(depth {max_observed_depth}); promoting to coordinated multi-repo saga"
            )
        return report

    # ------------------------------------------------------------------ public (async)
    async def maybe_promote(self, ctx: Any) -> Tuple[Any, Optional[PromotionReport]]:
        """Elevate ``ctx`` to multi-repo scope if its fault cone crosses a repo boundary.

        Returns ``(ctx, report)`` — ctx is elevated (cross_repo=True, Orange-tier) when promoted,
        else unchanged. Self-gates on ``promoter_enabled()``; fail-soft → ``(ctx, None)``."""
        if not promoter_enabled():
            return ctx, None
        try:
            import asyncio
            primary = self._primary or getattr(ctx, "primary_repo", "") or _primary_repo()
            target_files = tuple(getattr(ctx, "target_files", ()) or ())
            report = await asyncio.to_thread(self.analyze, target_files, primary)
            if not report.promoted:
                return ctx, None

            from backend.core.ouroboros.governance.risk_engine import RiskTier
            # Boundary-scoped apply plan: primary first, then siblings (dependency direction).
            apply_plan = report.elevated_scope
            dependency_edges = tuple(
                (primary, r) for r in report.cross_repos
            )
            elevated = ctx.with_cross_repo_promotion(
                repo_scope=report.elevated_scope,
                dependency_edges=dependency_edges,
                apply_plan=apply_plan,
                risk_tier=RiskTier.APPROVAL_REQUIRED,   # immutable Orange-tier for multi-repo
            )
            logger.info(
                "[CrossRepoPromoter] op=%s ELEVATED scope=%s depth=%d sharded=%s edges=%d",
                getattr(ctx, "op_id", "?"), report.elevated_scope, report.cascade_depth,
                report.sharded, len(report.boundary_edges),
            )
            return elevated, report
        except Exception as exc:  # noqa: BLE001 — promotion is additive; never break the pipeline
            logger.debug("[CrossRepoPromoter] promotion skipped (non-fatal): %s", exc)
            return ctx, None
