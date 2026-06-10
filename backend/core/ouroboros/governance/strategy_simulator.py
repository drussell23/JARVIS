"""Slice 203 — Strategic Simulation Sandbox (propose-don't-dispose).

The organism reads its OWN telemetry (the observability registry), identifies
recurring operational deficiencies, maps each to a candidate remediation goal,
ranks them by a heuristic fitness, writes the top few into a NON-authoritative
draft (``.jarvis/roadmap.draft.yaml``), and bundles them into an
operator-review PR. The operator reviews and — if they approve — runs
``strategy_signer`` to elevate the draft into the SIGNED authoritative
roadmap. The organism PROPOSES; the operator DISPOSES + signs (Slice 202 line
honored — no self-signing, no writing the active roadmap).

HONEST FRAMING (do not oversell):
  * "fitness" is a heuristic PRIORITIZATION — ``severity × frequency ÷
    effort`` — NOT a predictive ROI simulation. The organism cannot truly
    simulate the future value of an upgrade it has not built. The function
    ranks observed pain against rough effort; that is its honest scope.
  * goals come from a CURATED deficiency→remediation catalog: the organism
    maps observed telemetry pain to known remediation goals; it does not
    invent novel architecture from scratch.
  * the simulator writes ONLY the ``.draft`` file and NEVER the active
    ``roadmap.yaml``, and NEVER computes a signature.

Gated ``JARVIS_STRATEGY_SIMULATOR_ENABLED`` default-FALSE. Fail-soft.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_STRATEGY_SIMULATOR_ENABLED"
_DEFAULT_DRAFT_REL = ".jarvis/roadmap.draft.yaml"
_DEFAULT_MARKER_REL = ".jarvis/.strategy_proposal_marker"


def simulator_enabled() -> bool:
    """Gate, default FALSE (proposes a PR). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _envf(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v >= 0 else default
    except Exception:  # noqa: BLE001
        return default


# Curated deficiency catalog: telemetry signal → remediation goal template.
# ``effort`` is a rough relative cost estimate (1=cheap … 10=major); ``floor``
# is the count above which the deficiency is flagged; ``per`` normalizes
# frequency (None → absolute count).
_CATALOG = [
    {
        "kind": "provider_exhaustion",
        "counter": "provider_exhaustions",
        "floor": 1, "effort": 5.0, "priority": "high",
        "title": "Harden provider resilience (eliminate all_providers_exhausted)",
        "description": (
            "Telemetry shows ops fully exhausting every provider tier. Goal: "
            "drive provider_exhaustions to 0 via deeper failover / backoff / "
            "additional fallback capacity."
        ),
        "success_criteria": "provider_exhaustions == 0 across a 24h window",
    },
    {
        "kind": "control_plane_starvation",
        "counter": "control_plane_starvation_events",
        "floor": 5, "effort": 6.0, "priority": "high",
        "title": "Eliminate control-plane starvation (event-loop wedges)",
        "description": (
            "Recurring ControlPlaneStarvation lag events — the main asyncio "
            "loop is being starved by sync CPU work. Goal: move the blocking "
            "work off-thread / chunk it so the loop ticks within threshold."
        ),
        "success_criteria": "control_plane_starvation_events < 5 / 24h",
    },
    {
        "kind": "vendor_instability",
        "counter": "hedge_races_abandoned",
        "floor": 2, "effort": 4.0, "priority": "medium",
        "title": "Reduce abandoned hedge races (dual-arm vendor failures)",
        "description": (
            "Races dying with no winner indicate dual-arm vendor failures. "
            "Goal: lower the abandoned-race rate via earlier rotation / a "
            "third transport arm / better model-health prediction."
        ),
        "success_criteria": "abandoned-race ratio < 5% of dispatches",
    },
]


def analyze_deficiencies(
    snapshot: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Map a registry snapshot to flagged deficiencies (count over floor).
    ``severity`` scales with how far over the floor the count sits;
    ``frequency`` is normalized against dispatches when available. NEVER
    raises."""
    try:
        if not snapshot or not isinstance(snapshot, dict):
            return []
        dispatches = float(snapshot.get("hedge_concurrency_dispatches", 0) or 0)
        out: List[Dict[str, Any]] = []
        for entry in _CATALOG:
            count = float(snapshot.get(entry["counter"], 0) or 0)
            if count < entry["floor"]:
                continue
            severity = min(10.0, 1.0 + count / max(1.0, float(entry["floor"])))
            frequency = (count / dispatches) if dispatches > 0 else count
            out.append({
                "kind": entry["kind"],
                "counter": entry["counter"],
                "count": count,
                "severity": severity,
                "frequency": frequency,
                "effort": float(entry["effort"]),
                "template": entry,
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def compute_fitness(metrics: Dict[str, Any]) -> float:
    """Heuristic prioritization: ``(severity × (1 + frequency) × Wu) / (effort
    × Wc)``. NOT a predictive ROI — a ranking of observed pain vs rough
    effort. NEVER raises / never divides by zero."""
    try:
        wu = _envf("JARVIS_STRATEGY_W_IMPACT", 1.0)
        wc = _envf("JARVIS_STRATEGY_W_EFFORT", 1.0)
        severity = max(0.0, float(metrics.get("severity", 0.0)))
        frequency = max(0.0, float(metrics.get("frequency", 0.0)))
        effort = max(0.0, float(metrics.get("effort", 1.0)))
        impact = severity * (1.0 + frequency) * wu
        denom = (effort * wc) + 1.0  # +1 guards divide-by-zero + tempers
        return round(impact / denom, 4)
    except Exception:  # noqa: BLE001
        return 0.0


def synthesize_goals(
    snapshot: Optional[Dict[str, Any]], top_n: int = 6,
) -> List[Dict[str, Any]]:
    """Deficiencies → fitness-ranked roadmap goal dicts (top N). NEVER
    raises."""
    try:
        scored = []
        for d in analyze_deficiencies(snapshot):
            fit = compute_fitness(d)
            tpl = d["template"]
            scored.append((fit, {
                "id": f"sim-{d['kind']}",
                "title": tpl["title"],
                "description": tpl["description"],
                "priority": tpl["priority"],
                "success_criteria": tpl["success_criteria"],
                "depends_on": [],
                "fitness": fit,
                "evidence": {
                    "counter": d["counter"], "count": d["count"],
                    "severity": round(d["severity"], 2),
                },
            }))
        scored.sort(key=lambda t: -t[0])
        return [g for _, g in scored[:max(1, int(top_n))]] if scored else []
    except Exception:  # noqa: BLE001
        return []


def compile_draft(goals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap goals in the draft-roadmap schema — transparently a PROPOSAL,
    never authoritative, never signed. NEVER raises."""
    try:
        return {
            "version": 1,
            "operator_id": "ov-strategy-simulator",
            "source": "strategy_simulation_telemetry_driven",
            "authority": "draft",
            "signed": False,
            "note": (
                "DRAFT proposal synthesized by the strategy simulator from "
                "live registry telemetry. NOT authoritative and NOT signed. "
                "Review the bundled PR; run strategy_signer to elevate the "
                "goals you approve into the signed roadmap.yaml."
            ),
            "goals": list(goals),
        }
    except Exception:  # noqa: BLE001
        return {"version": 1, "signed": False, "authority": "draft", "goals": []}


def _serialize(draft: Dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(draft, sort_keys=False, allow_unicode=True)
    except Exception:  # noqa: BLE001
        return json.dumps(draft, indent=2)


def write_draft(
    goals: List[Dict[str, Any]], path: Optional[Path] = None,
) -> Optional[Path]:
    """Write the volatile draft (overwritable scratchpad) — ONLY ever the
    ``.draft`` file, NEVER the active roadmap.yaml. Returns path or None.
    NEVER raises."""
    try:
        target = Path(path) if path is not None else Path(_DEFAULT_DRAFT_REL)
        if "roadmap.draft" not in target.name and not str(target).endswith(
            ".draft.yaml",
        ):
            # refuse to write anything that isn't explicitly a draft file
            target = target.with_name("roadmap.draft.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_serialize(compile_draft(goals)), encoding="utf-8")
        return target
    except Exception as exc:  # noqa: BLE001
        logger.debug("[StrategySim] draft write failed soft: %s", exc)
        return None


def _draft_fingerprint(goals: List[Dict[str, Any]]) -> str:
    try:
        ids = sorted(g.get("id", "") for g in goals)
        return hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


def _render_pr_body(goals: List[Dict[str, Any]]) -> str:
    lines = [
        "## [Ouroboros Strategic Proposal] Project Horizon Upgrades",
        "",
        "The organism analyzed its own telemetry (observability registry) and "
        "proposes the following remediation goals, ranked by a **heuristic** "
        "fitness (severity × frequency ÷ effort — a prioritization, not a "
        "predictive ROI). **DO-NOT-AUTO-MERGE** — this is a proposal for your "
        "review.",
        "",
        "| # | Goal | Priority | Fitness | Evidence |",
        "|---|------|----------|---------|----------|",
    ]
    for i, g in enumerate(goals, 1):
        ev = g.get("evidence", {})
        lines.append(
            f"| {i} | {g.get('title','')} | {g.get('priority','')} | "
            f"{g.get('fitness','')} | {ev.get('counter','')}={ev.get('count','')} |"
        )
    lines += [
        "",
        "### To accept",
        "Review the goals above. To elevate the ones you approve into the "
        "signed authoritative roadmap, run:",
        "```",
        "python3 -m backend.core.ouroboros.governance.strategy_signer "
        ".jarvis/roadmap.yaml",
        "```",
        "(after copying the goals you want from `.jarvis/roadmap.draft.yaml`).",
    ]
    return "\n".join(lines)


async def propose_via_pr(
    snapshot: Optional[Dict[str, Any]],
    pr_creator: Optional[Callable] = None,
    draft_path: Optional[Path] = None,
    marker_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Synthesize goals → write draft → bundle into ONE operator-review PR.
    Deduped: if the draft fingerprint matches the last proposal's marker, no
    new PR is opened (prevents restart/cadence spam). NEVER signs, NEVER
    writes the active roadmap. NEVER raises."""
    try:
        if not simulator_enabled():
            return None
        goals = synthesize_goals(snapshot)
        if not goals:
            return None
        write_draft(goals, path=draft_path)

        fingerprint = _draft_fingerprint(goals)
        marker = Path(marker_path) if marker_path is not None \
            else Path(_DEFAULT_MARKER_REL)
        try:
            if marker.exists() and marker.read_text(
                encoding="utf-8",
            ).strip() == fingerprint:
                logger.info(
                    "[StrategySim] draft unchanged (fp=%s) — no new PR",
                    fingerprint,
                )
                return None
        except Exception:  # noqa: BLE001
            pass

        creator = pr_creator or _default_pr_creator
        result = await creator(
            "strategy-proposal",
            "[Ouroboros Strategic Proposal] Project Horizon Upgrades",
            [(".jarvis/roadmap.draft.yaml", _serialize(compile_draft(goals)))],
            evidence={"strategy_proposal": True, "body": _render_pr_body(goals)},
        )
        if result is None:
            return None
        pr_url = getattr(result, "pr_url", None) or getattr(
            result, "url", None,
        ) or str(result)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(fingerprint, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "[StrategySim] strategic proposal PR opened for operator review: "
            "%s (goals=%d, fp=%s)", pr_url, len(goals), fingerprint,
        )
        return {"pr_url": pr_url, "goals": len(goals), "fingerprint": fingerprint}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[StrategySim] propose failed soft: %s", exc)
        return None


async def _default_pr_creator(
    op_id: str, description: str, files: List, **kwargs: Any,
) -> Optional[Any]:
    from pathlib import Path as _P
    from backend.core.ouroboros.governance.orange_pr_reviewer import (
        OrangePRReviewer,
    )
    root = _P(os.environ.get("JARVIS_AUTO_COMMIT_WORKSPACE", "").strip() or "/app")
    if not (root / ".git").exists():
        root = _P.cwd()
    reviewer = OrangePRReviewer(project_root=root)
    return await reviewer.create_review_pr(
        op_id=op_id, description=description, files=files,
        evidence=kwargs.get("evidence"), risk_tier_name="APPROVAL_REQUIRED",
    )
