"""Slice 202 — Autonomous Strategic Bootstrapper (honest, advisory).

Seeds ``.jarvis/roadmap.yaml`` from the PRD §41.6 north-star objectives so the
organism has a machine-readable statement of its own current goals — WITHOUT
forging the operator's signature. The output is transparently
``signed: false`` / ``authority: advisory``.

Why advisory is honest AND safe: the RoadmapReader is intake-only — its goals
become IntentEnvelopes that flow through the canonical pipeline (Iron Gate /
SemanticGuardian / risk-tier-floor / human approval for anything beyond
SAFE_AUTO). So advisory goals provide DIRECTION, never AUTHORITY. The
signature only attests OPERATOR AUTHORSHIP; an unsigned roadmap makes no such
claim (it is honestly labelled auto-seeded), and grants no auto-approval.

To ELEVATE these goals to signed authenticity (operator authorship), the
operator runs ``strategy_signer`` deliberately. This module never signs and
never imports the signer — a bootstrapper that self-signs would be the
self-authorization anti-pattern the cage forbids (operator = zero-order doll).

Gated ``JARVIS_STRATEGY_BOOTSTRAP_ENABLED`` default-FALSE. NEVER overwrites an
operator-authored ``roadmap.yaml``. Fail-soft.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_STRATEGY_BOOTSTRAP_ENABLED"
_DEFAULT_ROADMAP_REL = ".jarvis/roadmap.yaml"

# The PRD §41.6 Tier-A→B evidence thresholds, as honest north-star goals.
# These are the organism's documented current objectives — grounded, not
# invented. They carry no target_files (they are outcome objectives, not
# file-tasks), so they bias intake DIRECTION without manufacturing fake ops.
_NORTHSTAR_GOALS: List[Dict[str, Any]] = [
    {
        "id": "northstar-m10-proposals-shipped",
        "title": "Ship 10 clean M10 architectural proposals",
        "description": (
            "PRD §41.6 Tier-A→B: accumulate 10 M10 ArchitectureProposer "
            "proposals shipped with no regressions, each operator-reviewed."
        ),
        "priority": "high",
        "success_criteria": "10 M10 proposals merged with zero regressions",
        "depends_on": [],
    },
    {
        "id": "northstar-unsupervised-soak-days",
        "title": "Accumulate 7 continuous unsupervised soak days",
        "description": (
            "PRD §41.6 Tier-A→B: 7 days of continuous unsupervised operation "
            "with the cage holding (no exhaustion storms, no boundary breach)."
        ),
        "priority": "high",
        "success_criteria": "7 unbroken unsupervised days on a durable host",
        "depends_on": [],
    },
    {
        "id": "northstar-ov-signed-commits-audited",
        "title": "30 O+V-signed commits adversarially audited clean",
        "description": (
            "PRD §41.6 Tier-A→B: 30 autonomous O+V-signed commits pass the "
            "adversarial mutation audit with no findings."
        ),
        "priority": "medium",
        "success_criteria": "30 OV-signed commits audited, zero findings",
        "depends_on": [],
    },
    {
        "id": "northstar-cadence-flags-graduated",
        "title": "Graduate 5 cadence flags default-TRUE",
        "description": (
            "PRD §41.6 Tier-A→B: 5 cadence/autonomy flags graduated to "
            "default-TRUE after evidence."
        ),
        "priority": "medium",
        "success_criteria": "5 flags graduated default-TRUE with soak evidence",
        "depends_on": [],
    },
]


def bootstrap_enabled() -> bool:
    """Gate, default FALSE (writes an advisory roadmap). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def extract_northstar_goals() -> List[Dict[str, Any]]:
    """Return the PRD §41.6 Tier-A→B north-star goals. Grounded in the PRD;
    the in-module constants ARE the documented thresholds (a parse of the
    living PRD table would drift with edits — the constants are the stable,
    auditable statement). NEVER raises."""
    try:
        return [dict(g) for g in _NORTHSTAR_GOALS]
    except Exception:  # noqa: BLE001
        return []


def compile_roadmap(
    goals: List[Dict[str, Any]],
    operator_id: str = "ov-auto-seed",
) -> Dict[str, Any]:
    """Wrap goals in the roadmap.yaml schema, transparently UNSIGNED +
    advisory. NEVER raises."""
    try:
        return {
            "version": 1,
            "operator_id": operator_id,
            "source": "prd_section_41.6_auto_seeded",
            "authority": "advisory",
            "signed": False,
            "note": (
                "Auto-seeded advisory roadmap (NOT operator-signed). Goals "
                "provide direction only; every emitted op is still fully "
                "governed. Run strategy_signer to elevate to signed "
                "authenticity, or replace this file with your own goals."
            ),
            "goals": list(goals),
        }
    except Exception:  # noqa: BLE001
        return {"version": 1, "signed": False, "authority": "advisory",
                "source": "prd_auto_seeded", "goals": []}


def _serialize_roadmap(roadmap: Dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(roadmap, sort_keys=False, allow_unicode=True)
    except Exception:  # noqa: BLE001 — PyYAML optional; JSON is valid too
        import json
        return json.dumps(roadmap, indent=2)


def write_roadmap_if_absent(path: Optional[Path] = None) -> Optional[Path]:
    """Write the advisory roadmap iff the target does NOT already exist (an
    operator-authored file is sacrosanct — never overwritten). Returns the
    path on write, else None. NEVER raises."""
    try:
        target = Path(path) if path is not None else Path(_DEFAULT_ROADMAP_REL)
        if target.exists():
            logger.info(
                "[StrategyBootstrap] roadmap already present at %s — not "
                "overwriting (operator file is sacrosanct)", target,
            )
            return None
        roadmap = compile_roadmap(extract_northstar_goals())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_serialize_roadmap(roadmap), encoding="utf-8")
        logger.warning(
            "[StrategyBootstrap] seeded ADVISORY roadmap at %s (signed=false; "
            "direction only, fully governed). Operator: edit goals + run "
            "strategy_signer to elevate to signed authenticity.", target,
        )
        return target
    except Exception as exc:  # noqa: BLE001
        logger.warning("[StrategyBootstrap] write failed soft: %s", exc)
        return None
