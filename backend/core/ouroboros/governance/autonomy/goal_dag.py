"""Goal substitution as a DAG — never as a mutation.

## The problem this exists for

VALIDATE can now answer ``no_covering_test``: strategies 0-3 found no test
related to the changed file, and substituting the whole suite is what this
system spent 450s/candidate learning not to do. But the honest refusal leaves
the work undone, and the op simply fails.

The tempting fix is to widen the running op's scope so it can write the test
too. That is exactly the thing the cage exists to refuse. A goal's
``target_files`` is what its signature attests; an op that writes outside it is
``self_modification_unsanctioned_source``, and this repository has the live
failures to prove the cage means it. Mutating a signed goal to make the failure
go away would not be a fix, it would be a forgery.

## The shape that is legal

Do not touch the running goal. **Shed it, and file two new ones.**

    Goal A  "write tests/test_X.py for backend/X.py"   target: the TEST file
    Goal B  "<the original work on backend/X.py>"      target: the MODULE
            depends_on = (Goal A,)

Both authored through :func:`operator_goal_sanction.author_and_sign_goal` — the
one signer the operator CLI, the ``/goal`` verb and Sentinel discovery already
use. There is deliberately no second signing path: a synthesized goal must be
verifiable in exactly the way a typed one is.

The dependency is carried in ``depends_on``, which ``roadmap_reader`` has
parsed and described as "(advisory)" all along. Putting it in ``GoalSpec.
to_entry`` moves it INSIDE the signed payload, so the edge is attested rather
than advisory: an op cannot acquire a prerequisite it was not signed with, and
cannot shed one either.

## The deadlock guarantee

A dependency graph that can wait forever is a queue that can wedge. The
guarantee here is structural rather than procedural — B is never *scheduled*
until A has landed, so there is no state in which B is running and A is not
done:

* **A satisfied** → B is ``READY``.
* **A neither satisfied nor exhausted** → B is ``BLOCKED``; discovery skips it
  and moves to other work. It costs nothing to leave it blocked.
* **A exhausted** (its target has failed enough times to be in a long cooldown,
  or it is absent from the roadmap entirely) → B is ``DEPENDENCY_FAILED`` and
  is shed, not parked. A prerequisite that cannot be met makes its dependent
  unreachable, and an unreachable goal that stays in the queue is a leak.

"Exhausted" is read from the SAME cooldown ledger the Sentinel already uses for
backoff, so there is no second notion of "this target keeps failing" to drift.

## Invariants

1. **A signed goal is never mutated.** Substitution only ever ADDS goals.
2. **B never runs before A lands.** Enforced at selection, so it holds without
   anyone checking it at execution time.
3. **A dependency that cannot be satisfied fails its dependent.** No goal waits
   on something that will never happen.
4. **Cycles are impossible by construction here** (A is freshly authored and
   depends on nothing), and detected defensively anyway.
5. **Never raises.** A DAG fault degrades to "no substitution", which is the
   behaviour that preceded this module.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.GoalDAG")

__all__ = [
    "BLOCKED",
    "DEPENDENCY_FAILED",
    "READY",
    "DependencyVerdict",
    "SubstitutionPlan",
    "dependency_verdict",
    "file_substitution",
    "plan_substitution",
    "substitution_enabled",
    "test_path_for",
]

READY = "ready"
BLOCKED = "blocked"
DEPENDENCY_FAILED = "dependency_failed"

#: Master switch. Default ON — an honest `no_covering_test` refusal with no
#: follow-up is a dead end, and this is the follow-up.
ENV_ENABLED = "JARVIS_GOAL_SUBSTITUTION_ENABLED"
#: How many consecutive failures make a prerequisite "exhausted" and fail its
#: dependents. Shares the Sentinel's own backoff vocabulary rather than
#: inventing a second threshold.
ENV_EXHAUSTION_FAILURES = "JARVIS_GOAL_DEPENDENCY_EXHAUSTION_FAILURES"
_DEFAULT_EXHAUSTION_FAILURES = 3


def substitution_enabled() -> bool:
    raw = (os.environ.get(ENV_ENABLED, "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _exhaustion_threshold() -> int:
    try:
        raw = (os.environ.get(ENV_EXHAUSTION_FAILURES, "") or "").strip()
        val = int(raw) if raw else _DEFAULT_EXHAUSTION_FAILURES
        return val if val >= 1 else _DEFAULT_EXHAUSTION_FAILURES
    except (TypeError, ValueError):
        return _DEFAULT_EXHAUSTION_FAILURES


# ---------------------------------------------------------------------------
# Naming — derived from the subject, never passed in
# ---------------------------------------------------------------------------

def test_path_for(subject_file: str) -> str:
    """The conventional test path for *subject_file*.

    Derived, never a parameter: a caller-supplied test path is a caller-chosen
    scope, and scope is what the signature attests. Mirrors the convention
    ``TestRunner`` Strategy 1 searches for (``test_<name>.py`` in the nearest
    ``tests/`` directory), so a test authored under this name is the one
    validation will actually find — otherwise Goal A could "succeed" and leave
    Goal B still uncovered.
    """
    p = Path(str(subject_file or ""))
    if not p.name:
        return ""
    name = p.name if p.name.startswith("test_") else f"test_{p.name}"
    # A repo-level `tests/` mirror keeps the path stable regardless of how deep
    # the subject sits, matching what discovery's uncovered-module scan expects.
    return str(Path("tests") / name)


def _slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return out or "unnamed"


def _derive_goal_ids(subject_file: str) -> Tuple[str, str]:
    """``(goal_a_id, goal_b_id)`` — stable and derived from the subject.

    Derived so a re-discovery of the SAME latent problem collides with its own
    prior id, which the signer refuses as a duplicate. That refusal is the
    feature that stops this filing the same pair twice.
    """
    stem = _slug(Path(str(subject_file or "")).stem)
    return (f"ov-dag-testsynth-{stem}", f"ov-dag-repair-{stem}")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubstitutionPlan:
    """Two goals and the edge between them. Pure state; nothing signed yet."""

    subject_file: str
    test_file: str
    goal_a_id: str
    goal_b_id: str
    original_description: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        return (
            f"A={self.goal_a_id} (writes {self.test_file}) "
            f"-> B={self.goal_b_id} (writes {self.subject_file})"
        )


def plan_substitution(
    *, subject_file: str, original_description: str = "",
) -> Optional[SubstitutionPlan]:
    """Plan the A→B pair for a module with no covering test. NEVER raises.

    Returns ``None`` when disabled, when the subject is unusable, or when the
    subject IS a test file (a test with no test is not a coverage deficit).
    """
    try:
        if not substitution_enabled():
            return None
        subject = str(subject_file or "").strip()
        if not subject or not subject.endswith(".py"):
            return None
        if Path(subject).name.startswith("test_"):
            return None
        test_file = test_path_for(subject)
        if not test_file or test_file == subject:
            return None
        a_id, b_id = _derive_goal_ids(subject)
        return SubstitutionPlan(
            subject_file=subject,
            test_file=test_file,
            goal_a_id=a_id,
            goal_b_id=b_id,
            original_description=str(original_description or "").strip(),
        )
    except Exception:  # noqa: BLE001 — planning never breaks the shed path
        logger.debug("[GoalDAG] substitution planning degraded", exc_info=True)
        return None


def _spec_a(plan: SubstitutionPlan) -> Any:
    from backend.core.ouroboros.governance.operator_goal_sanction import (  # noqa: PLC0415
        GoalSpec,
    )
    return GoalSpec(
        goal_id=plan.goal_a_id,
        title=f"[dag] test synthesis: {Path(plan.test_file).name}",
        description=(
            f"`{plan.subject_file}` has no corresponding test module, so a "
            f"change to it cannot be validated. CREATE `{plan.test_file}` "
            "containing focused tests for its public behaviour and edge "
            "cases: an import smoke test, tests for the key public functions, "
            "and the edge cases those functions actually branch on. Read "
            f"`{plan.subject_file}` to derive the tests; do NOT modify it — "
            f"the only file this goal authorises you to write is "
            f"`{plan.test_file}`."
        ),
        target_files=(plan.test_file,),
        success_criteria=(
            f"`{plan.test_file}` exists and passes, and a change to "
            f"`{plan.subject_file}` now resolves to it."
        ),
    )


def _spec_b(plan: SubstitutionPlan) -> Any:
    from backend.core.ouroboros.governance.operator_goal_sanction import (  # noqa: PLC0415
        GoalSpec,
    )
    what = plan.original_description or (
        f"Continue the deferred work on `{plan.subject_file}`."
    )
    return GoalSpec(
        goal_id=plan.goal_b_id,
        title=f"[dag] module repair: {Path(plan.subject_file).name}",
        description=(
            f"{what}\n\nThis goal was deferred because `{plan.subject_file}` "
            f"had no covering test. `{plan.goal_a_id}` writes "
            f"`{plan.test_file}` first; this goal runs only once that has "
            "landed, so the change can be validated against real tests."
        ),
        target_files=(plan.subject_file,),
        # The edge, inside the signed payload.
        depends_on=(plan.goal_a_id,),
    )


def file_substitution(plan: SubstitutionPlan) -> Tuple[Any, Any]:
    """Author and SIGN both goals. Returns ``(result_a, result_b)``.

    Order matters: A is filed first, so that if B's authoring fails the graph
    is left with a runnable prerequisite and no orphaned dependent — the
    failure mode that leaves a queue entry nothing can ever satisfy.

    Composes ``author_and_sign_goal`` exactly as Sentinel discovery does. It
    refuses an unset secret, a duplicate id and an unscoped goal, so a
    re-filing of the same deficit is rejected by the signer rather than needing
    a second guard here. NEVER raises.
    """
    from backend.core.ouroboros.governance import (  # noqa: PLC0415
        operator_goal_sanction as ogs,
    )
    try:
        res_a = ogs.author_and_sign_goal(_spec_a(plan))
        if not getattr(res_a, "ok", False):
            logger.info(
                "[GoalDAG] prerequisite not filed (%s) — NOT filing the "
                "dependent, an orphan would wait forever",
                getattr(res_a, "reason", "?"),
            )
            return res_a, None
        res_b = ogs.author_and_sign_goal(_spec_b(plan))
        logger.warning(
            "[GoalDAG] TestCoverageDeficit substituted: %s [A=%s B=%s]",
            plan.render(),
            getattr(res_a, "reason", "ok"), getattr(res_b, "reason", "ok"),
        )
        return res_a, res_b
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GoalDAG] filing degraded: %r", exc, exc_info=True)
        return None, None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyVerdict:
    """Whether a goal's prerequisites allow it to run."""

    state: str
    goal_id: str = ""
    unmet: Tuple[str, ...] = ()
    failed: Tuple[str, ...] = ()
    reason: str = ""

    @property
    def runnable(self) -> bool:
        return self.state == READY

    def render(self) -> str:
        return (
            f"{self.state} goal={self.goal_id}"
            + (f" unmet={list(self.unmet)}" if self.unmet else "")
            + (f" failed={list(self.failed)}" if self.failed else "")
            + (f" — {self.reason}" if self.reason else "")
        )


def dependency_verdict(
    goal_id: str,
    depends_on: Sequence[str],
    *,
    satisfied: FrozenSet[str],
    exhausted: FrozenSet[str] = frozenset(),
) -> DependencyVerdict:
    """May *goal_id* run, given what has landed and what has given up?

    Parameters
    ----------
    satisfied:
        Goal ids the ledger says are already built — from
        ``goal_reconciliation_ledger.satisfied_goal_ids``. NOT the promotion
        question: an accumulation-branch landing counts, because the test file
        exists on the branch the dependent will also run against.
    exhausted:
        Prerequisites that have failed enough to be considered unreachable.

    NEVER raises: any fault answers READY, i.e. the behaviour before this gate
    existed. A dependency check that fails closed would wedge every goal the
    moment it broke.
    """
    try:
        deps = tuple(
            str(d).strip() for d in (depends_on or ()) if str(d or "").strip()
        )
        gid = str(goal_id or "")
        if not deps:
            return DependencyVerdict(READY, gid, reason="no dependencies")
        if gid and gid in deps:
            # Defensive: a self-edge is a cycle of length one and can never be
            # satisfied. Fail it rather than block forever.
            return DependencyVerdict(
                DEPENDENCY_FAILED, gid, failed=(gid,),
                reason="goal depends on itself",
            )
        failed = tuple(d for d in deps if d in (exhausted or frozenset()))
        if failed:
            return DependencyVerdict(
                DEPENDENCY_FAILED, gid, failed=failed,
                reason="prerequisite exhausted — dependent is unreachable",
            )
        unmet = tuple(d for d in deps if d not in (satisfied or frozenset()))
        if unmet:
            return DependencyVerdict(
                BLOCKED, gid, unmet=unmet,
                reason="prerequisite has not landed yet",
            )
        return DependencyVerdict(READY, gid, reason="all prerequisites landed")
    except Exception:  # noqa: BLE001
        logger.debug("[GoalDAG] dependency verdict degraded", exc_info=True)
        return DependencyVerdict(READY, str(goal_id or ""), reason="degraded")


def exhausted_goal_ids(
    goal_ids: Sequence[str],
    *,
    roadmap_targets: Dict[str, str],
    cooldown: Any = None,
) -> FrozenSet[str]:
    """Prerequisites that can no longer be expected to land. NEVER raises.

    Two ways a prerequisite becomes unreachable:

    * its target has failed consecutively past the exhaustion threshold, read
      from the SAME cooldown ledger the Sentinel uses for backoff — so
      "this keeps failing" has one definition in the system, not two;
    * it is not in the roadmap at all, so nothing will ever dispatch it.
    """
    out = set()
    try:
        threshold = _exhaustion_threshold()
        for gid in goal_ids or ():
            gid = str(gid or "").strip()
            if not gid:
                continue
            target = (roadmap_targets or {}).get(gid)
            if target is None:
                out.add(gid)  # absent from the roadmap: unreachable
                continue
            if cooldown is None:
                continue
            try:
                entry = cooldown.entry_for(target)
                fails = int(getattr(entry, "consecutive_failures", 0) or 0)
            except Exception:  # noqa: BLE001 — no entry is not a failure
                continue
            if fails >= threshold:
                out.add(gid)
    except Exception:  # noqa: BLE001
        logger.debug("[GoalDAG] exhaustion scan degraded", exc_info=True)
    return frozenset(out)
