"""Epistemic Planner — Autonomous Epistemic Planning (Part C, 2026-07-18).

The stop-condition for human prompting: when O+V sees a gap between the current
codebase (the FRONTIER — where the human's recent commits ended) and the PRD's
A-level criteria (the NORTH STAR — the definition of DONE), it fills its OWN
backlog: a structured multi-step sub-roadmap synthesized as blueprint records
and fed through the EXISTING conception pipeline.

DISTINCT from ``roadmap_synthesizer.py`` (Slice 122): that module drafts
UNSIGNED authority roadmaps (``.jarvis/roadmap.draft.yaml``) for the OPERATOR
to sign — it confers authority. THIS module fills the speculative BACKLOG —
it confers nothing; every step competes through the EV gate + full governance
cage like any dream blueprint.

Design — every step rides existing rails (DRY, no new task database):

  * GAP inputs: ``north_star_context.a_level_criteria()`` (the PRD §6 table,
    structured) × ``git_momentum`` (scope/type momentum + recently-touched
    files — the concrete frontier surface). All deterministic; zero model calls.
  * OUTPUT: real :class:`ImprovementBlueprint` records (the SAME type the
    DreamEngine emits), with DETERMINISTIC ids (sha256 of tree-fingerprint ×
    module × dimension) so re-synthesis is idempotent and the bridge's
    dedup/incubation bounds apply naturally.
  * SUBMISSION: ``ConceptionProposalBridge.route(blueprints, router)`` — the
    same seam dream blueprints use. Each step is EV-scored by the value model
    and COMPETES: high-EV steps route as ``auto_proposed`` envelopes
    immediately; sub-threshold steps land in the IncubationStore (bounded:
    50-cap drop-oldest, 20-attempt retirement) for adaptive re-scoring.
  * GROUNDING: target_files are ONLY .py files that exist on disk right now
    AND were touched by recent commits — a step can never point at fiction.
    No momentum / no criteria / no grounded files → silence over fabrication.

Master ``JARVIS_EPISTEMIC_PLANNER_ENABLED`` **default FALSE** per the §33.1
Graduation Contract (a new autonomous backlog producer arms via env on a soak
gate and graduates only after the empirical contract passes). Fail-soft
everywhere — planning must never break its caller.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

EPISTEMIC_PLANNER_SCHEMA_VERSION = "epistemic_planner.v1"

_TRUTHY = ("1", "true", "yes", "on")

# Conventional-commit type → blueprint category (the blueprint's closed vocab:
# complexity | test_coverage | security | performance | debt).
_TYPE_TO_CATEGORY = {
    "feat": "complexity",
    "fix": "debt",
    "test": "test_coverage",
    "perf": "performance",
    "security": "security",
    "refactor": "debt",
    "docs": "debt",
    "chore": "debt",
}


def epistemic_planner_enabled() -> bool:
    """``JARVIS_EPISTEMIC_PLANNER_ENABLED`` — §33.1 master, **default FALSE**.
    NEVER raises."""
    try:
        return os.environ.get(
            "JARVIS_EPISTEMIC_PLANNER_ENABLED", "",
        ).strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def _max_steps() -> int:
    """``JARVIS_EPISTEMIC_MAX_STEPS`` (default 3, clamp [1, 8]). NEVER raises."""
    try:
        return max(1, min(8, int(os.environ.get("JARVIS_EPISTEMIC_MAX_STEPS", "3"))))
    except (TypeError, ValueError):
        return 3


def _max_files_per_step() -> int:
    """``JARVIS_EPISTEMIC_MAX_FILES_PER_STEP`` (default 2, clamp [1, 4]). NEVER
    raises."""
    try:
        return max(1, min(4, int(
            os.environ.get("JARVIS_EPISTEMIC_MAX_FILES_PER_STEP", "2"),
        )))
    except (TypeError, ValueError):
        return 2


def _step_cost_usd() -> float:
    """``JARVIS_EPISTEMIC_STEP_COST_USD`` (default 0.05) — the conservative
    estimated cost the EV model weighs each step against. NEVER raises."""
    try:
        return max(0.0, float(os.environ.get("JARVIS_EPISTEMIC_STEP_COST_USD", "0.05")))
    except (TypeError, ValueError):
        return 0.05


def _module_of(path: str, depth: int = 4) -> str:
    """Group key: the first *depth* path segments (e.g.
    ``backend/core/ouroboros/governance``). NEVER raises."""
    try:
        return "/".join(Path(path).parts[:depth]) or path
    except Exception:  # noqa: BLE001
        return str(path)


def synthesize_gap_blueprints(
    *,
    momentum: Any,
    recent_files: Sequence[str],
    criteria: Sequence[Tuple[str, str]],
    repo_root: str,
    tree_fingerprint: str,
    policy_hash: str = "",
    now_unix: "Optional[float]" = None,
) -> Tuple[Any, ...]:
    """PURE synthesis: (frontier × A-level criteria) → ≤max_steps
    :class:`ImprovementBlueprint` records. See the module docstring's grounding
    rules. NEVER raises — any fault returns ``()``."""
    try:
        if not criteria or momentum is None:
            return ()
        if getattr(momentum, "is_empty", lambda: True)():
            return ()
        root = Path(repo_root)

        # Group recent EXISTING .py files by module, preserving recency order.
        groups: "dict[str, List[str]]" = {}
        order: "List[str]" = []
        for rel in recent_files or ():
            if not str(rel).endswith(".py"):
                continue
            try:
                if not (root / rel).is_file():
                    continue                      # deleted/renamed → not a target
            except OSError:
                continue
            mod = _module_of(str(rel))
            if mod not in groups:
                groups[mod] = []
                order.append(mod)
            if len(groups[mod]) < _max_files_per_step():
                groups[mod].append(str(rel))
        if not order:
            return ()

        # Dominant conventional-commit type → category hint.
        try:
            top_types = getattr(momentum, "top_types", lambda n=4: [])(4)
            dominant_type = top_types[0][0] if top_types else ""
        except Exception:  # noqa: BLE001
            dominant_type = ""
        category = _TYPE_TO_CATEGORY.get(dominant_type, "debt")

        try:
            subjects = list(getattr(momentum, "latest_subjects", ()) or ())[:2]
        except Exception:  # noqa: BLE001
            subjects = []

        from backend.core.ouroboros.consciousness.types import (  # noqa: PLC0415
            ImprovementBlueprint,
        )
        now = float(now_unix if now_unix is not None else time.time())
        k = min(_max_steps(), len(order), len(criteria))
        steps: "List[Any]" = []
        for i in range(k):
            mod = order[i]
            dimension, criterion = criteria[i % len(criteria)]
            bid = "epistemic-" + hashlib.sha256(
                f"{tree_fingerprint}|{mod}|{dimension}".encode(),
            ).hexdigest()[:16]
            steps.append(ImprovementBlueprint(
                blueprint_id=bid,
                title=(
                    f"Roadmap step {i + 1}/{k}: advance '{dimension}' in {mod}"
                ),
                description=(
                    f"Autonomous sub-roadmap step (gap: frontier vs North Star). "
                    f"A-level criterion not yet met: [{dimension}] {criterion}. "
                    f"The human frontier is active in {mod} "
                    f"(recent: {'; '.join(subjects) or 'n/a'}). Continue that "
                    f"work with a small, concrete change in the target files "
                    f"that measurably advances the criterion. "
                    f"roadmap_lineage={tree_fingerprint[:12]} step={i + 1}/{k} "
                    f"planner={EPISTEMIC_PLANNER_SCHEMA_VERSION}"
                ),
                category=category,
                priority_score=max(0.1, 0.9 - 0.2 * i),
                target_files=tuple(groups[mod]),
                estimated_effort="small",
                estimated_cost_usd=_step_cost_usd(),
                repo="jarvis",
                repo_sha=str(tree_fingerprint),
                computed_at_utc=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now),
                ),
                ttl_hours=24.0,
                model_used=EPISTEMIC_PLANNER_SCHEMA_VERSION,   # deterministic — no model
                policy_hash=str(policy_hash or ""),
                oracle_neighborhood={},
                suggested_approach=(
                    "Smallest change that measurably advances the named "
                    "A-level criterion in the named module; must pass the "
                    "full VALIDATE/GATE cage like any op."
                ),
                risk_assessment=(
                    "Low: synthesized from observed frontier files; competes "
                    "through the EV gate + full governance cage."
                ),
            ))
        return tuple(steps)
    except Exception:  # noqa: BLE001 — synthesis must never break its caller
        logger.debug("[EpistemicPlanner] synthesis degraded", exc_info=True)
        return ()


async def plan_and_route_once(router: Any, *, repo_root: "Optional[str]" = None) -> int:
    """One full planning cycle: gather inputs (async, loop-safe) → synthesize →
    feed through the EXISTING ``ConceptionProposalBridge.route`` (EV-scored:
    high-EV steps route as auto_proposed envelopes, the rest incubate).

    Returns the number of blueprints submitted. 0 when disabled / nothing to
    plan. NEVER raises."""
    try:
        if not epistemic_planner_enabled():
            return 0
        root = str(repo_root or os.environ.get("JARVIS_REPO_ROOT", "."))
        from backend.core.ouroboros.governance.git_momentum import (  # noqa: PLC0415
            compute_recent_files_async,
            compute_recent_momentum_async,
        )
        from backend.core.ouroboros.governance.north_star_context import (  # noqa: PLC0415
            a_level_criteria,
        )
        momentum = await compute_recent_momentum_async(Path(root))
        recent = await compute_recent_files_async(Path(root))
        criteria = a_level_criteria(root)
        # Deterministic tree fingerprint: stable while recent history is
        # unchanged → idempotent ids → bridge dedup/incubation bounds apply.
        fingerprint = hashlib.sha256(
            ("\n".join(recent[:20])).encode(),
        ).hexdigest()[:16]
        steps = synthesize_gap_blueprints(
            momentum=momentum, recent_files=recent, criteria=criteria,
            repo_root=root, tree_fingerprint=fingerprint,
        )
        if not steps:
            return 0
        from backend.core.ouroboros.governance.conception_proposal_bridge import (  # noqa: PLC0415
            get_default_bridge,
        )
        await get_default_bridge().route(list(steps), router)
        logger.info(
            "[EpistemicPlanner] cycle: %d sub-roadmap step(s) -> conception "
            "bridge (EV-scored: route or incubate)", len(steps),
        )
        return len(steps)
    except Exception:  # noqa: BLE001 — autonomous planning must never break intake
        logger.debug("[EpistemicPlanner] cycle degraded", exc_info=True)
        return 0
