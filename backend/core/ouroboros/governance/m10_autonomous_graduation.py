"""Slice 197 — Autonomous Graduation Contract + Adaptive Synthesis Governor.

The M10 ArchitectureProposer sat frozen behind a static operator-set flag
(§30.5.2: default-false until a proposal-acceptance audit). The deadlock: the
audit needs proposals to audit, and the proposals need the flag. This module
converts the static toggle into OPERATOR-DELEGATED CONDITIONAL AUTHORIZATION:

  * The OPERATOR authorizes the unlock criteria ONCE — by reviewing and
    merging this slice (the operator act that supersedes the §30.5.2 static
    binding). The criteria live here, env-tunable, in the open.
  * The ORGANISM proves the criteria against the durable mmap registry
    (Slice 193 ``.bin`` — exhaustions, hedge stability, control-plane
    starvation profile) and executes the unlock itself, persisting a stamped
    audit artifact.
  * The operator KILL SWITCH IS SUPREME: explicit
    ``JARVIS_M10_ARCH_PROPOSER_ENABLED=0`` beats any autonomous state,
    always (Slice 136 precedent: "Operator =0 precedence honored").
    Revocation is one env var, not a code change.

What this is NOT: self-authorization. The system cannot loosen its own
criteria (they're code the operator reviewed), cannot override the kill
switch, and the ``governance_boundary_gate`` recursion guard is untouched —
proposals that would modify ``governance/`` (including THIS module) still
route ``APPROVAL_REQUIRED``. Grep-pinned in the Slice 197 suite.

Graduation criteria (all must hold, env-tunable):
  * evidence floor — ``hedge_concurrency_dispatches >=
    JARVIS_M10_GRAD_MIN_DISPATCHES`` (default 5): zero traffic proves
    nothing.
  * ``provider_exhaustions <= JARVIS_M10_GRAD_MAX_EXHAUSTIONS`` (default 0).
  * ``hedge_races_abandoned / dispatches <=
    JARVIS_M10_GRAD_MAX_ABANDONED_RATIO`` (default 0.25) — vendor
    containment is holding.
  * ``control_plane_starvation_events <=
    JARVIS_M10_GRAD_MAX_STARVATION_EVENTS`` (default 50) — the loop ticks.

Unlock semantics: STICKY. Once graduated + persisted
(``.jarvis/m10_graduation_state.json``), later metric noise does not
silently re-lock — graduation is a milestone, not an oscillator. Revocation
belongs to the operator (=0).

The Adaptive Synthesis Governor (:func:`effective_cadence_n`) paces proposal
synthesis once unlocked: conserve computational capital when traffic or cost
burn is high, compile aggressively when the organism is idle. Pure function
of static inputs — no ledger coupling (Slice 47 doctrine).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_M10_AUTONOMOUS_GRADUATION_ENABLED"
_ENV_STATE_PATH = "JARVIS_M10_GRADUATION_STATE_PATH"
_DEFAULT_STATE_PATH = ".jarvis/m10_graduation_state.json"

def autonomous_graduation_enabled() -> bool:
    """Master for the autonomous contract (default TRUE — the merged Slice
    197 PR is the operator authorization; kill switch via this flag or the
    supreme JARVIS_M10_ARCH_PROPOSER_ENABLED=0). NEVER raises."""
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _envf(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v >= 0 else default
    except Exception:  # noqa: BLE001
        return default


def _state_path() -> Path:
    raw = os.environ.get(_ENV_STATE_PATH, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_STATE_PATH)


@dataclass(frozen=True)
class GraduationDecision:
    """One evaluation of the graduation criteria against the registry."""

    unlocked: bool
    reason: str
    metrics: Dict[str, int] = field(default_factory=dict)


def evaluate_graduation() -> GraduationDecision:
    """Evaluate the criteria against the live registry; persist the unlock
    (with a stamped metrics snapshot) on pass. NEVER raises."""
    try:
        if not autonomous_graduation_enabled():
            return GraduationDecision(False, "autonomous_graduation_disabled")
        from backend.core.ouroboros.governance.observability_registry import (
            CONTROL_PLANE_STARVATION_EVENTS,
            HEDGE_CONCURRENCY_DISPATCHES,
            HEDGE_RACES_ABANDONED,
            PROVIDER_EXHAUSTIONS,
            get_observability_registry,
        )
        snap = get_observability_registry().snapshot()
        if not snap:
            return GraduationDecision(False, "registry_unavailable")
        dispatches = int(snap.get(HEDGE_CONCURRENCY_DISPATCHES, 0))
        exhaustions = int(snap.get(PROVIDER_EXHAUSTIONS, 0))
        abandoned = int(snap.get(HEDGE_RACES_ABANDONED, 0))
        starvation = int(snap.get(CONTROL_PLANE_STARVATION_EVENTS, 0))

        min_dispatches = int(_envf("JARVIS_M10_GRAD_MIN_DISPATCHES", 5))
        max_exhaustions = int(_envf("JARVIS_M10_GRAD_MAX_EXHAUSTIONS", 0))
        max_abandoned_ratio = _envf("JARVIS_M10_GRAD_MAX_ABANDONED_RATIO", 0.25)
        max_starvation = int(
            _envf("JARVIS_M10_GRAD_MAX_STARVATION_EVENTS", 50)
        )

        if dispatches < min_dispatches:
            return GraduationDecision(
                False,
                f"evidence_floor: dispatches={dispatches} < {min_dispatches}",
                dict(snap),
            )
        if exhaustions > max_exhaustions:
            return GraduationDecision(
                False,
                f"provider_exhaustions={exhaustions} > {max_exhaustions}",
                dict(snap),
            )
        ratio = abandoned / float(dispatches)
        if ratio > max_abandoned_ratio:
            return GraduationDecision(
                False,
                f"abandoned_ratio={ratio:.2f} > {max_abandoned_ratio}",
                dict(snap),
            )
        if starvation > max_starvation:
            return GraduationDecision(
                False,
                f"starvation_events={starvation} > {max_starvation}",
                dict(snap),
            )

        decision = GraduationDecision(
            True,
            "all_criteria_met",
            dict(snap),
        )
        _persist_unlock(decision, {
            "min_dispatches": min_dispatches,
            "max_exhaustions": max_exhaustions,
            "max_abandoned_ratio": max_abandoned_ratio,
            "max_starvation_events": max_starvation,
        })
        return decision
    except Exception as exc:  # noqa: BLE001
        logger.warning("[M10Graduation] evaluation failed soft: %s", exc)
        return GraduationDecision(False, f"evaluation_error:{exc}")


def _persist_unlock(decision: GraduationDecision, criteria: Dict) -> None:
    """Durable audit artifact — the organism's signed-by-metrics unlock
    record. Best-effort; failure to persist doesn't revoke the decision for
    this process (the next process re-proves it)."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": "1.0",
            "unlocked": True,
            "reason": decision.reason,
            "metrics": decision.metrics,
            "criteria": criteria,
            "unlocked_at_unix": time.time(),
        }, indent=2), encoding="utf-8")
        logger.warning(
            "[M10Graduation] AUTONOMOUS GRADUATION: criteria met against the "
            "registry — M10 ArchitectureProposer UNLOCKED (state=%s, "
            "metrics=%s). Operator kill switch: "
            "JARVIS_M10_ARCH_PROPOSER_ENABLED=0",
            path, decision.metrics,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[M10Graduation] state persist failed soft: %s", exc)


def _persisted_unlocked() -> bool:
    try:
        path = _state_path()
        if not path.exists():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        return bool(payload.get("unlocked"))
    except Exception:  # noqa: BLE001
        return False


def is_autonomously_unlocked() -> bool:
    """The predicate ``m10.primitives.m10_arch_proposer_enabled`` consults
    when the operator env is UNSET.

    Sticky-True via the persisted file (``_persisted_unlocked`` short-circuits
    once graduated — later metric noise never re-locks; revocation = operator
    =0). Pre-graduation, re-evaluates fresh on each call (a microsecond mmap
    snapshot read) so ignition fires the MILLISECOND the criteria go healthy —
    no negative-cache lag. evaluate_graduation persists on pass, so the next
    call short-circuits. NEVER raises."""
    try:
        if not autonomous_graduation_enabled():
            return False
        if _persisted_unlocked():
            return True
        return evaluate_graduation().unlocked
    except Exception:  # noqa: BLE001
        return False


def _reset_for_tests() -> None:
    """Test seam — no persistent in-process state to clear now that the
    negative cache is gone; retained for import compatibility and future
    state. NEVER raises."""
    return None


# ---------------------------------------------------------------------------
# Slice 198 — Sovereign Ignition Protocol: cadence ignition + protection arming
# ---------------------------------------------------------------------------

def m10_cadence_ignited() -> bool:
    """The cadence loop ignites WITH the autonomous unlock — a graduated
    proposer that nothing triggers is a dead engine. Consulted by
    ``cadence_runner.cadence_enabled`` when its sub-flag is unset. The
    operator kill switch (``JARVIS_M10_CADENCE_ENABLED=0``) is handled by the
    caller and remains supreme. NEVER raises."""
    try:
        return is_autonomously_unlocked()
    except Exception:  # noqa: BLE001
        return False


def taste_layer_assertion_passes(_assess_probe=None) -> bool:
    """Live assertion for the architectural-taste protection gate: run a
    SYNTHETIC micro-proposal through the master-independent ``assess_file``
    scorer and confirm it returns a real assessment — proving the
    design-quality filter is responsive. Non-blocking, no git, no model.
    Fail-closed: any None/raise → False. ``_assess_probe`` is a test seam."""
    try:
        def _default_probe():
            from backend.core.ouroboros.governance.architectural_taste_layer import (  # noqa: E501
                assess_file,
            )
            # A tiny, well-formed synthetic module — the scorer must produce
            # a verdict for it without touching disk or git.
            return assess_file(
                "slice198_synthetic_probe.py",
                source_override=(
                    "def add(a, b):\n"
                    "    \"\"\"Sum two numbers.\"\"\"\n"
                    "    return a + b\n"
                ),
                siblings_count=1,
            )
        probe = _assess_probe if _assess_probe is not None else _default_probe
        result = probe()
        return result is not None
    except Exception:  # noqa: BLE001
        return False


def _gh_present() -> bool:
    import shutil
    return shutil.which("gh") is not None


def _git_work_tree_with_remote() -> bool:
    import subprocess
    try:
        in_tree = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        if in_tree.returncode != 0 or in_tree.stdout.strip() != "true":
            return False
        remotes = subprocess.run(
            ["git", "remote"], capture_output=True, text=True, timeout=5,
        )
        return remotes.returncode == 0 and bool(remotes.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def orange_pr_assertion_passes(_gh_probe=None, _git_probe=None) -> bool:
    """Live preflight for the orange-PR protection gate: confirm the async
    PR submission line CAN resolve — ``gh`` binary present AND inside a git
    work tree with a remote — WITHOUT pushing and WITHOUT a blocking CLI
    prompt. Fail-closed: a headless/gitless container correctly does NOT arm
    (the honest finding: orange-PR needs a real git+gh host). Probes are test
    seams. NEVER raises."""
    try:
        gh = _gh_probe if _gh_probe is not None else _gh_present
        git = _git_probe if _git_probe is not None else _git_work_tree_with_remote
        return bool(gh()) and bool(git())
    except Exception:  # noqa: BLE001
        return False


def taste_layer_armed() -> bool:
    """Architectural-taste gate arms iff the organism is autonomously
    unlocked AND its synthetic responsiveness assertion passes. Consulted by
    ``architectural_taste_layer.master_enabled`` when the env is unset;
    explicit env wins there (kill switch supreme). NEVER raises."""
    try:
        return is_autonomously_unlocked() and taste_layer_assertion_passes()
    except Exception:  # noqa: BLE001
        return False


def orange_pr_armed() -> bool:
    """Orange-PR gate arms iff autonomously unlocked AND the gh+git preflight
    passes. Consulted by ``orange_pr_reviewer.is_orange_pr_enabled`` when the
    env is unset; explicit env wins (kill switch supreme). NEVER raises."""
    try:
        return is_autonomously_unlocked() and orange_pr_assertion_passes()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Adaptive Synthesis Governor — pacing
# ---------------------------------------------------------------------------

def effective_cadence_n(
    base_n: int,
    dispatch_delta: int,
    cost_burn_ratio: Optional[float] = None,
) -> int:
    """Adapt the M10 cadence (proposals fire every N ops) to the operating
    window. Pure function of static inputs — no ledger coupling.

      * busy (dispatch_delta >= BUSY_DISPATCH_DELTA, default 10) →
        N × BUSY_FACTOR (default 2.0): conserve capital under load.
      * idle (dispatch_delta == 0) → N × IDLE_FACTOR (default 0.5):
        compile patterns aggressively while the organism is quiet.
      * cost_burn_ratio >= COST_CONSERVE_RATIO (default 0.8) → conserve
        regardless of traffic (the budget is the harder constraint).

    Result floored at 1. NEVER raises."""
    try:
        n = max(1, int(base_n))
        delta = max(0, int(dispatch_delta))
        busy_at = _envf("JARVIS_M10_PACING_BUSY_DISPATCH_DELTA", 10.0)
        busy_factor = _envf("JARVIS_M10_PACING_BUSY_FACTOR", 2.0)
        idle_factor = _envf("JARVIS_M10_PACING_IDLE_FACTOR", 0.5)
        conserve_at = _envf("JARVIS_M10_PACING_COST_CONSERVE_RATIO", 0.8)

        factor = 1.0
        if delta >= busy_at:
            factor = max(factor, busy_factor)
        elif delta == 0:
            factor = idle_factor
        if cost_burn_ratio is not None and float(cost_burn_ratio) >= conserve_at:
            factor = max(factor, busy_factor)
        return max(1, int(round(n * factor)))
    except Exception:  # noqa: BLE001
        try:
            return max(1, int(base_n))
        except Exception:  # noqa: BLE001
            return 1
