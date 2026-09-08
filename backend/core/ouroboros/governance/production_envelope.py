"""The execution envelope — ONE source of truth for how the organism runs.

A production run needs ~40 environment variables set correctly: background-pool
sizing, generation and pipeline budgets, the validation reserve, sensor
intervals, sibling sampling. Until now those lived as literal ``export`` lines
inside individual bash scripts. ``soak26.sh`` had all of them; the interactive
cockpit launcher had none of them, so a ``/goal sanction`` typed into the
cockpit ran the same pipeline on DEFAULT budgets — which is exactly where the
huge-file production goals kept dying.

Copying the block from one script to the next is not a fix; it is the drift,
one generation later. The values live here instead, and every launcher — soak
or cockpit — derives its environment from this module.

## Derived, not transcribed

The budgets are RELATIONS, not magic numbers, because that is what they
actually are. ``soak26`` ran ``--max-wall-seconds 9000`` and set
``PIPELINE_TIMEOUT_S=4968``, ``GENERATION_TIMEOUT_S=3726``,
``BG_WORKER_OP_TIMEOUT_S=4968``. Those three are one number and two ratios:

    pipeline   = wall * _PIPELINE_WALL_FRACTION      (4968 = 9000 * 0.552)
    generation = pipeline * _GEN_PIPELINE_FRACTION   (3726 = 4968 * 0.75)
    bg_worker  = pipeline                            (a worker's op IS a pipeline)

Change the wall and all three move together and stay consistent. Written as
literals in three scripts, they drift apart the first time someone edits one —
and a generation budget larger than its own pipeline budget is a deadline the
op can never meet, which is invisible until a soak burns on it.

## Profiles

``soak`` is headless and autonomous. ``cockpit`` is the same organism with a
human attached: identical execution budgets — that is the entire point, the
cockpit must be able to do production work — plus the presentation flags and a
bounded approval deadline, because a human at a prompt is a resource with a
timeout and a background worker pool must never block forever on one.

## Operator override always wins

:func:`hydrate` uses ``setdefault`` semantics, the same discipline the boot
exorcism block in ``scripts/ouroboros_battle_test.py`` uses. Anything already
in the environment — a launcher's ``${VAR:-default}``, an operator's
``VAR=x`` prefix, a value from ``.env`` — is left exactly as it is and
reported as an override. The envelope supplies what nobody has spoken for.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.ProductionEnvelope")

__all__ = [
    "PROFILES",
    "ProductionEnvelope",
    "build",
    "export_lines",
    "hydrate",
]

# --- The two ratios that tie the budgets together -------------------------
#: pipeline budget as a fraction of the session wall clock. Below this an op
#: cannot finish inside the session; far above it, one stuck op eats the run.
_PIPELINE_WALL_FRACTION = 0.552
#: generation budget as a fraction of the pipeline budget. The remainder is
#: what VALIDATE / GATE / APPLY / VERIFY get, and they need it.
_GEN_PIPELINE_FRACTION = 0.75

#: Session defaults per profile: (wall_s, idle_timeout_s, cost_cap_usd).
PROFILES: Dict[str, Tuple[int, int, float]] = {
    "soak": (9000, 1800, 0.50),
    "cockpit": (3600, 900, 0.50),
}


def _bg_pool_size() -> int:
    """Concurrent ops the background pool may execute.

    Derived from the LANE. On a paid fleet, concurrency is somebody else's
    capacity problem; on one local GPU it is the whole problem — every extra
    in-flight generation contends for the same card, and the streams come back
    empty rather than slow. Composed from :mod:`local_lane_capacity` so the
    pool and the generator's own semaphore cannot disagree about how much the
    hardware can take. NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.autonomy.local_lane_capacity import (
            resolve_primary_concurrency,
        )
        return max(1, int(resolve_primary_concurrency(cloud_default=6).concurrency))
    except Exception:  # noqa: BLE001 — an unknown lane is a SMALL lane
        return 1


def _fmt(value) -> str:
    """Env values are strings. ``True`` must render as ``true``, not ``True``
    — every reader in this repo lowercases and compares to ``("1","true",...)``,
    and ``"True"`` happens to survive that only by accident of ``.lower()``.
    Be explicit rather than lucky."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


@dataclass(frozen=True)
class ProductionEnvelope:
    """A complete, self-consistent execution environment."""

    profile: str
    wall_s: int
    idle_timeout_s: int
    cost_cap_usd: float
    pipeline_timeout_s: int
    generation_timeout_s: int
    bg_worker_op_timeout_s: int
    approval_deadline_s: int
    values: Dict[str, str] = field(default_factory=dict)

    def as_env(self) -> Dict[str, str]:
        """The variables this envelope declares, name → string value."""
        return dict(self.values)

    def summary(self) -> str:
        """One line an operator can read in a boot banner."""
        return (
            f"envelope[{self.profile}] wall={self.wall_s}s "
            f"pipeline={self.pipeline_timeout_s}s "
            f"generation={self.generation_timeout_s}s "
            f"approval={self.approval_deadline_s}s "
            f"({len(self.values)} vars)"
        )


def build(
    profile: str = "soak",
    *,
    wall_s: Optional[int] = None,
    idle_timeout_s: Optional[int] = None,
    cost_cap_usd: Optional[float] = None,
) -> ProductionEnvelope:
    """Compose the envelope for *profile*, deriving every budget.

    An unknown profile falls back to ``soak`` with a warning rather than
    raising: a launcher typo must not stop the organism booting, but it must
    not be silent either.
    """
    key = (profile or "").strip().lower()
    if key not in PROFILES:
        logger.warning(
            "[ProductionEnvelope] unknown profile %r — falling back to 'soak'",
            profile,
        )
        key = "soak"
    d_wall, d_idle, d_cost = PROFILES[key]
    wall = int(wall_s if wall_s is not None else d_wall)
    idle = int(idle_timeout_s if idle_timeout_s is not None else d_idle)
    cost = float(cost_cap_usd if cost_cap_usd is not None else d_cost)

    pipeline = max(60, int(wall * _PIPELINE_WALL_FRACTION))
    generation = max(30, int(pipeline * _GEN_PIPELINE_FRACTION))
    bg_worker = pipeline
    # A human at a prompt is a resource with a timeout. Bound it BELOW the
    # pipeline budget so the approval gate always expires before the deadline
    # that would kill the op from underneath it — an op shed by its own gate
    # records why; one killed by the pipeline clock just vanishes.
    approval = max(30, int(pipeline * 0.5))

    values: Dict[str, object] = {
        # --- budgets (derived above; never write these as literals) -------
        "JARVIS_PIPELINE_TIMEOUT_S": pipeline,
        "JARVIS_GEN_TIMEOUT_STANDARD_S": generation,
        "JARVIS_GENERATION_TIMEOUT_S": generation,
        "JARVIS_BG_WORKER_OP_TIMEOUT_S": bg_worker,
        "JARVIS_PIPELINE_DEADLINE_AT_START": True,
        "JARVIS_TEST_TIMEOUT_S": 180,
        # --- the background pool that actually runs roadmap ops -----------
        # Sized for the LANE, not copied from soak26. Six workers is right for
        # a hosted fleet and structurally wrong for one local GPU: measured
        # 2026-09-08, four ops entering GENERATE together against a 30B at 32k
        # context each returned `tokens=0 tps=0.0`, recorded as
        # `no_candidates_returned` → `generation_failed`. Thirteen of thirty
        # ops died that way. It reads like a model-quality problem and is not
        # one — the model never ran.
        #
        # The queue stays deep: work should QUEUE, not be refused. What must
        # be bounded is how much of it executes at once.
        "JARVIS_BG_POOL_SIZE": _bg_pool_size(),
        "JARVIS_BG_QUEUE_SIZE": 64,
        "JARVIS_THROUGHPUT_GOVERNOR_ENABLED": False,
        # --- validation reserve -------------------------------------------
        "JARVIS_VALIDATION_RESERVE_ENABLED": True,
        "JARVIS_VALIDATION_RESERVE_COLD_S": 240,
        "JARVIS_VALIDATION_RESERVE_SAFETY": 1.5,
        "JARVIS_VALIDATION_RESERVE_MAX_FRACTION": 0.5,
        # --- watchdog ------------------------------------------------------
        "JARVIS_EXTERNAL_WATCHDOG_STALE_S": 600,
        "JARVIS_EXTERNAL_WATCHDOG_MARGIN_S": 120,
        # --- sibling sampling ----------------------------------------------
        "JARVIS_SIBLING_ENTROPY_ENABLED": True,
        "JARVIS_SIBLING_MAX_RESAMPLE": 1,
        "JARVIS_SIBLING_TEMP_CEILING": 1.15,
        "JARVIS_SIBLING_DIVERSITY_THRESHOLD": 0.999,
        "JARVIS_SIBLING_ESCALATION_MULTIPLIER": 1.0,
        "JARVIS_LOCAL_SIBLING_CANDIDATES": 3,
        "JARVIS_LOCAL_SIBLING_BUDGET_MARGIN": 1.0,
        # --- sensors: the roadmap is the work source; the rest stay quiet --
        "JARVIS_WORK_ORDER_SENSOR_ENABLED": True,
        "JARVIS_WORK_ORDER_INTERVAL_S": 86400,
        "JARVIS_WORK_ORDER_RECENT_N": 41,
        "JARVIS_WORK_ORDER_MAX_ITEMS": 41,
        "JARVIS_WORK_ORDER_DEFAULT_URGENCY": "high",
        "JARVIS_ALLOW_ROADMAP_REVISIT": True,
        "JARVIS_DOC_STALENESS_ENABLED": False,
        "JARVIS_RUNTIME_HEALTH_SENSOR_ENABLED": False,
        "JARVIS_GITHUB_ISSUE_SENSOR_ENABLED": False,
        "JARVIS_OPPORTUNITY_MINER_SENSOR_ENABLED": False,
        "JARVIS_INTENT_TEST_INTERVAL_S": 86400,
        "JARVIS_TODO_SCAN_INTERVAL_S": 86400,
        "JARVIS_EXPLORATION_INTERVAL_S": 86400,
        "JARVIS_INTAKE_BACKLOG_SCAN_INTERVAL_S": 86400,
        "JARVIS_TESTWATCHER_BOOT_HYDRATION_ENABLED": False,
        "JARVIS_TEST_FAILURE_CACHE_FIRST_ENABLED": False,
        # --- PRODUCTION CAPABILITIES ---------------------------------------
        # Shipped, tested, and dormant behind default-FALSE graduation flags.
        # Armed HERE, in the shared base, so the cockpit and the headless soak
        # cannot differ in what the organism can DO — a cockpit that runs a
        # weaker pipeline than the soak is a cockpit whose results mean
        # nothing.
        #
        # The diff schema is the load-bearing one: FALSE means every candidate
        # is a whole-file re-emission, which is precisely where a mid-size
        # model drops a closing docstring quote on a large file. A diff cannot
        # mangle a docstring it never reproduces.
        "JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED": True,
        # The L3 fan-out. Without the master pair, the parent-inheritance fix
        # (ec6bb92c9a) engages only when an authoritative multi-node PLAN DAG
        # drives it; the legacy route stays dead code.
        "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED": True,
        "JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE": True,
        # NOT armed, deliberately, each for a stated reason:
        #   JARVIS_EXPLORATION_LEDGER_ENABLED — its decision path applies score
        #     floors that REFUSE the swarm route; arming it is a regression,
        #     not a capability.
        #   JARVIS_WORKSPACE_PROMOTION_ENABLED — changes where landed work
        #     goes. It belongs to its own change with its own evidence, not to
        #     a capability sweep.
        # --- corpus --------------------------------------------------------
        "JARVIS_TRAJECTORY_RECORDER_ENABLED": True,
        # --- outward-facing: air-gapped in BOTH profiles -------------------
        # Not a cockpit-only concern. The 22 branches escaped from HEADLESS
        # soaks, so the headless profile is exactly the one that needs it.
        "JARVIS_REMOTE_PUSH_AIRGAP": True,
        "JARVIS_ORANGE_PR_ENABLED": False,
        # --- the approval gate (see phase 3) --------------------------------
        "JARVIS_APPROVAL_DEADLINE_S": approval,
    }

    if key == "cockpit":
        values.update({
            # Presentation — the cockpit's own surface. These do not touch
            # execution; the budgets above are identical to the soak's,
            # which is the point of unifying them.
            "JARVIS_OV_PRESENTATION": "cockpit",
            "JARVIS_PRESENTATION_RESTRAINT_ENABLED": True,
            "JARVIS_REPL_COMPLETION_ENABLED": True,
            "JARVIS_REPL_INPUT_POLISH_ENABLED": True,
            "JARVIS_LIVE_STATUS_LINE_ENABLED": True,
            "JARVIS_OP_COLLAPSE_ENABLED": True,
            "JARVIS_TOOL_RENDER_REGISTRY_ENABLED": True,
            "JARVIS_NARRATIVE_INTENT_ENABLED": True,
            "JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED": True,
            "JARVIS_BTW_ENABLED": True,
            "JARVIS_SWARM_ROUTING_ENABLED": True,
            "JARVIS_L2_SYMBOL_SCOPED_ENABLED": True,
            "JARVIS_NOTIFY_APPLY_DELAY_S": 8,
            "JARVIS_REVIEW_TIMEOUT_S": 900,
            # Review branches stay LOCAL. The lane may build them; the
            # air-gap above is what stops them reaching origin.
            "JARVIS_REVIEW_BRANCH_ENABLED": True,
        })

    return ProductionEnvelope(
        profile=key,
        wall_s=wall,
        idle_timeout_s=idle,
        cost_cap_usd=cost,
        pipeline_timeout_s=pipeline,
        generation_timeout_s=generation,
        bg_worker_op_timeout_s=bg_worker,
        approval_deadline_s=approval,
        values={k: _fmt(v) for k, v in values.items()},
    )


def hydrate(
    profile: str = "soak",
    *,
    environ: Optional[Mapping[str, str]] = None,
    wall_s: Optional[int] = None,
    idle_timeout_s: Optional[int] = None,
    cost_cap_usd: Optional[float] = None,
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """Apply the envelope to the process environment. NEVER raises.

    Returns ``(applied, overridden)`` — what this call set, and the names it
    found already spoken for and left alone. Operator intent always wins; the
    envelope fills the silence.
    """
    target = os.environ if environ is None else environ
    envelope = build(
        profile, wall_s=wall_s, idle_timeout_s=idle_timeout_s,
        cost_cap_usd=cost_cap_usd,
    )
    applied: Dict[str, str] = {}
    overridden = []
    for name, value in envelope.as_env().items():
        try:
            if name in target and str(target[name]).strip() != "":
                overridden.append(name)
                continue
            target[name] = value  # type: ignore[index]
            applied[name] = value
        except Exception:  # noqa: BLE001 — a hostile mapping never stops boot
            logger.debug("[ProductionEnvelope] could not set %s", name, exc_info=True)
    logger.info(
        "[ProductionEnvelope] %s — %d applied, %d operator-set (%s)",
        envelope.summary(), len(applied), len(overridden),
        ", ".join(sorted(overridden)[:6]) or "none",
    )
    return applied, tuple(sorted(overridden))


def export_lines(
    profile: str = "soak",
    *,
    wall_s: Optional[int] = None,
    known: Optional[Iterable[str]] = None,
) -> str:
    """The envelope as shell ``export`` lines, for a launcher to ``eval``.

    Uses ``${NAME:-value}`` so a variable already exported by the operator
    survives — the same precedence :func:`hydrate` gives in-process, so bash
    and Python cannot disagree about who wins.
    """
    envelope = build(profile, wall_s=wall_s)
    seen = set(known or ())
    out = [f"# generated by production_envelope.build({profile!r}) — do not edit"]
    for name, value in sorted(envelope.as_env().items()):
        if name in seen:
            continue
        out.append(f'export {name}="${{{name}:-{value}}}"')
    out.append(f'# {envelope.summary()}')
    return "\n".join(out)


def _main(argv=None) -> int:
    """``python -m ...production_envelope --profile cockpit --shell``."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="soak", choices=sorted(PROFILES))
    parser.add_argument("--wall-seconds", type=int, default=None)
    parser.add_argument("--shell", action="store_true",
                        help="emit export lines for eval in a launcher")
    args = parser.parse_args(argv)
    if args.shell:
        print(export_lines(args.profile, wall_s=args.wall_seconds))
    else:
        env = build(args.profile, wall_s=args.wall_seconds)
        print(env.summary())
        for name, value in sorted(env.as_env().items()):
            print(f"{name}={value}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
