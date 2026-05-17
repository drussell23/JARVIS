"""
SWE-Bench path↔advisor preflight (canonical, single seam)
=========================================================
Boot-time assertion that the effective SWE-Bench worktree base + repo
cache resolve UNDER the operation_advisor allowed-prefix anchor (the
orchestrator project_root).  Runs strictly before the SWE-Bench-Pro
inject (the spend path), beside the provider-readiness gate.

Root cause it closes (session bt-2026-05-17-002318): a ``$TMPDIR``
worktree base escaped the advisor anchor, ``resolve_envelope_repo_root``
returned None, the advisor + generator fell back to the JARVIS tree, the
model edited the wrong repo.  This refuses the spend before a dollar
burns when the configuration would put worktrees outside the anchor.

Composition only — no parallel prefix math:
  * worktree base / cache  : per_problem_harness.worktree_base_path /
                             repo_cache_path  (canonical env knobs)
  * anchor classification  : operation_advisor.envelope_repo_root_status
                             (the single status owner)

NEVER raises.  Default-FALSE master flag → byte-identical no-op.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from backend.core.ouroboros.governance.operation_advisor import (
    EVIDENCE_REPO_ROOT_KEY,
    RepoRootPromiseStatus,
    envelope_repo_root_status,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (  # noqa: E501
    repo_cache_path,
    worktree_base_path,
)

logger = logging.getLogger(__name__)

_MASTER_ENV = "JARVIS_BATTLE_PREFLIGHT_SWE_PATH_ENABLED"


class SwePathVerdict(Enum):
    PROCEED = "proceed"
    PROCEED_DISABLED = "proceed_disabled_by_env"
    PROCEED_FEATURE_OFF = "proceed_advisor_worktree_feature_off"
    REFUSE_OUTSIDE_ANCHOR = "refuse_worktree_base_outside_anchor"


@dataclass
class SwePathReadinessReport:
    timestamp: str
    verdict: str
    under_project_root: bool
    project_root: str
    worktree_base: str
    repo_cache: str
    details: str


def _classify(base: Path, project_root: Path) -> RepoRootPromiseStatus:
    """Classify one base dir via the canonical status owner.

    The base is ensured to exist (mkdir) so ``resolve_envelope_repo_root``'s
    ``exists()/is_dir()`` check is meaningful — a not-yet-created default
    dir must not masquerade as a rejection.
    """
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    evidence = json.dumps({EVIDENCE_REPO_ROOT_KEY: str(base)})
    status, _resolved, _raw = envelope_repo_root_status(
        evidence, project_root=project_root
    )
    return status


async def assess_swe_path_readiness(
    session_dir: str, *, project_root: Path
) -> SwePathVerdict:
    """Assert the SWE-Bench worktree/cache bases sit under the advisor
    anchor. NEVER raises.

    REFUSE_OUTSIDE_ANCHOR iff a base is *promised + rejected* (the
    contamination configuration).  Feature-off → PROCEED_FEATURE_OFF
    (warn; the runtime fail-closed guard is authoritative either way).
    """
    if os.environ.get(_MASTER_ENV, "false").lower() != "true":
        return SwePathVerdict.PROCEED_DISABLED

    report = SwePathReadinessReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        verdict="",
        under_project_root=False,
        project_root="",
        worktree_base="",
        repo_cache="",
        details="",
    )
    try:
        anchor = Path(project_root).resolve(strict=False)
        wt = worktree_base_path().resolve(strict=False)
        rc = repo_cache_path().resolve(strict=False)
        report.project_root = str(anchor)
        report.worktree_base = str(wt)
        report.repo_cache = str(rc)

        statuses = {
            "worktree_base": _classify(wt, anchor),
            "repo_cache": _classify(rc, anchor),
        }
        rejected = [k for k, v in statuses.items()
                    if v is RepoRootPromiseStatus.REJECTED]
        feature_off = any(
            v is RepoRootPromiseStatus.NO_PROMISE for v in statuses.values()
        )

        if rejected:
            report.verdict = SwePathVerdict.REFUSE_OUTSIDE_ANCHOR.value
            report.under_project_root = False
            report.details = (
                f"{rejected} escaped the advisor anchor — refusing "
                f"SWE-Bench inject (would contaminate the shared tree). "
                f"Drop TMPDIR WORKTREE_BASE_PATH/REPO_CACHE_PATH overrides."
            )
            _write_report(session_dir, report)
            logger.warning(
                "[SwePathPreflight] REFUSE — %s outside anchor %s",
                rejected, anchor,
            )
            return SwePathVerdict.REFUSE_OUTSIDE_ANCHOR

        if feature_off:
            report.verdict = SwePathVerdict.PROCEED_FEATURE_OFF.value
            report.under_project_root = True
            report.details = (
                "Advisor worktree-aware feature OFF — boot anchor assert "
                "indeterminate; runtime fail-closed guard authoritative."
            )
            _write_report(session_dir, report)
            logger.info("[SwePathPreflight] feature-off — PROCEED + WARN")
            return SwePathVerdict.PROCEED_FEATURE_OFF

        report.verdict = SwePathVerdict.PROCEED.value
        report.under_project_root = True
        report.details = "Worktree base + repo cache under project_root."
        _write_report(session_dir, report)
        logger.info(
            "[SwePathPreflight] PROCEED — under_project_root=True"
        )
        return SwePathVerdict.PROCEED

    except Exception as e:  # noqa: BLE001 — gate MUST NEVER raise
        logger.error(
            "[SwePathPreflight] internal error: %s — PROCEED+WARN "
            "(runtime guard authoritative)", e,
        )
        report.verdict = SwePathVerdict.PROCEED_FEATURE_OFF.value
        report.under_project_root = True
        report.details = f"Internal exception: {e!s}"
        _write_report(session_dir, report)
        return SwePathVerdict.PROCEED_FEATURE_OFF


def _write_report(session_dir: str, report: SwePathReadinessReport) -> None:
    """Write forensic JSON to the session dir. Never raises."""
    try:
        if not os.path.exists(session_dir):
            os.makedirs(session_dir, exist_ok=True)
        path = os.path.join(session_dir, "swe_path_readiness.json")
        with open(path, "w") as f:
            json.dump(asdict(report), f, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to write swe_path_readiness.json: %s", e)
