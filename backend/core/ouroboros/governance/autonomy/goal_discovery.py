"""Goal Discovery Sensor — the organism finds its own work.

Until now every unit of sanctioned work began with a human typing
``/goal sanction``. That makes the organism a copilot: it can execute a task
perfectly and cannot notice one. This sensor is the other half — it reads the
repository's own distress signals and turns them into signed, scoped goals with
nobody at the keyboard.

## Where work comes from — evidence, never invention

A goal the organism made up is a goal nobody can verify. Every candidate here
traces to a FACT already recorded by machinery that exists for other reasons:

* **Ambient reds** — tests failing at HEAD, from the TestWatcher census. A red
  test is the least ambiguous work item in the repository: something is stated
  to be true, and is not.
* **Uncovered production modules** — a source file with no corresponding test
  file. Untested code is not a defect, so these rank BELOW reds; it is a
  standing invitation rather than an alarm.

Ranking is by evidence strength, and it is deliberate: a repository with one
red test and four hundred uncovered modules should fix the red test.

## What it refuses to look at

Three exclusions, each structural rather than a list someone maintains:

* the **governance substrate** — the cage the organism runs inside. Discovering
  work there would let it propose edits to its own brakes, and while the
  Sentinel floor would still force those to a human, the honest place to stop
  is before synthesising the goal at all;
* anything **cooling** (:mod:`sentinel_cooldown`) — the memory that stops the
  loop re-attacking a target it just failed;
* **tests themselves** as repair targets, when the red is in a test file whose
  subject can be identified — the fix belongs in the code under test, and a
  loop that edits assertions until they pass is not an engineer.

## Authority is inherited, never minted

:func:`synthesize_and_sign` composes ``operator_goal_sanction.
author_and_sign_goal`` — the ONE signer the operator CLI and the ``/goal``
verb already use. There is no second signing path, so a synthesized goal is
signed, scoped and verifiable in exactly the way a typed one is, and the cage
re-derives its authority from the document rather than trusting this module.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import (
    Any, Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple,
)

logger = logging.getLogger("Ouroboros.GoalDiscovery")

__all__ = [
    "DiscoveredWork",
    "discover",
    "discovery_enabled",
    "is_dispatchable",
    "synthesize_and_sign",
]

_ENV_ENABLED = "JARVIS_GOAL_DISCOVERY_ENABLED"
_ENV_MAX_CANDIDATES = "JARVIS_GOAL_DISCOVERY_MAX_CANDIDATES"
_ENV_CENSUS_BUDGET = "JARVIS_GOAL_DISCOVERY_CENSUS_BUDGET_S"

#: Evidence strength. A failing test outranks an absence of tests, always.
_KIND_WEIGHT: Dict[str, float] = {
    "ambient_red": 1.0,
    "uncovered_module": 0.4,
}


def discovery_enabled() -> bool:
    """Whether the organism may author its own goals. Default OFF. NEVER raises."""
    raw = (os.environ.get(_ENV_ENABLED, "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _max_candidates() -> int:
    try:
        raw = (os.environ.get(_ENV_MAX_CANDIDATES, "") or "").strip()
        return max(1, int(raw)) if raw else 8
    except (TypeError, ValueError):
        return 8


def _census_budget_s() -> float:
    """How long DISCOVERY may take before it gives up on the census.

    The census runs the repository's test suite. On a large tree that is
    minutes, and on a wedged one it is forever — and an unbounded await here
    would hang the entire autonomous loop on its cheapest step, which is the
    exact failure mode every other part of this system was built to refuse.

    Derived, not chosen: discovery is overhead against the work it finds, so
    it gets a fraction of one pipeline budget. A census that cannot finish in
    that time is not a census the loop can use, and the coverage source still
    answers.
    """
    try:
        raw = (os.environ.get(_ENV_CENSUS_BUDGET, "") or "").strip()
        if raw:
            return max(10.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        pipeline = float((os.environ.get("JARVIS_PIPELINE_TIMEOUT_S", "") or "0").strip())
    except (TypeError, ValueError):
        pipeline = 0.0
    if pipeline > 0:
        return max(30.0, pipeline / 8.0)
    return 300.0


@dataclass(frozen=True)
class DiscoveredWork:
    """One unit of work the repository is asking for, with its evidence.

    ``target_file`` is the file that will be WRITTEN, which is not always the
    file the evidence points at. A signed goal's scope is what the cage checks
    an edit against, so declaring the subject instead of the target is refused
    as ``self_modification_unsanctioned_source`` — the op tries to write a path
    its own mandate never covered. Found live: an "add tests for X.py" goal
    declared ``X.py`` and then tried to create ``tests/test_X.py``.
    """

    target_file: str
    kind: str
    evidence: str
    symbols: Tuple[str, ...] = ()
    weight: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)
    #: The file the EVIDENCE is about, when it differs from what gets written
    #: (an uncovered module is the subject; the new test file is the target).
    subject_file: str = ""
    #: The id of an ALREADY-SIGNED roadmap goal this work came from.
    #:
    #: Empty for work discovered from evidence, which derives its id. Set only
    #: by the roadmap source, where the goal already has an identity that the
    #: ledger, the DAG edges and the signer's duplicate guard all key on.
    declared_goal_id: str = ""

    @property
    def goal_id(self) -> str:
        """A stable, readable id derived from the target and the reason.

        Derived rather than random so the SAME latent problem re-discovered
        later collides with its own prior goal id — which the signer refuses
        as a duplicate. That refusal is a feature: it stops the sensor filing
        the same work twice.

        EXCEPT when the work came from an already-signed roadmap goal, which
        carries its own id. Deriving one there would mint a second identity for
        a goal that already has one, and every id-keyed contract in the system
        — the reconciliation ledger, the DAG's ``depends_on`` edges, the
        duplicate-id guard in the signer — would then be looking at the wrong
        name for the same work.
        """
        if self.declared_goal_id:
            return self.declared_goal_id
        # Keyed on the SUBJECT, not the target: "tests for X" is the same work
        # however the test file ends up named, and the id is what stops the
        # sensor filing it twice.
        stem = Path(self.subject_file or self.target_file).stem[:32]
        slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "target"
        return f"ov-auto-{self.kind.replace('_', '-')}-{slug}"

    def describe(self) -> str:
        """The task text handed to the model."""
        if self.kind == "roadmap_goal":
            # A signed goal already CARRIES its task text, written and attested
            # when it was authored. Re-deriving one here would hand the model a
            # different instruction than the one the signature covers — and for
            # a DAG's repair half, the derived text would be the "write tests
            # for X" template, i.e. exactly the wrong job.
            return str(self.detail.get("description") or self.evidence or "")
        if self.kind == "ambient_red":
            return (
                f"A test is failing at HEAD against `{self.target_file}`: "
                f"{self.evidence}. Repair the PRODUCTION code so the test "
                f"passes. Do not weaken, skip or delete the test — if the test "
                f"is genuinely wrong, decline and say so rather than editing "
                f"the assertion."
            )
        subject = self.subject_file or self.target_file
        return (
            f"`{subject}` has no corresponding test module. CREATE "
            f"`{self.target_file}` containing focused tests for its public "
            f"behaviour and edge cases: an import smoke test, tests for the "
            f"key public functions, and the edge cases those functions "
            f"actually branch on. Read `{subject}` to derive the tests; do "
            f"NOT modify it — the only file this goal authorises you to write "
            f"is `{self.target_file}`."
        )


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def _is_governance(path: str) -> bool:
    """The cage the organism runs inside — never a discovery target."""
    p = str(path).replace("\\", "/")
    return "ouroboros/governance" in p or "ouroboros/battle_test" in p


def _is_test_file(path: str) -> bool:
    name = Path(str(path)).name
    return name.startswith("test_") or name.endswith("_test.py")


def _subject_of_test(test_path: str, repo_root: Path) -> Optional[str]:
    """The production module a test file is about, when it can be identified.

    ``tests/governance/test_foo.py`` -> the ``foo.py`` that exists under
    ``backend/``. Returns None when the mapping is ambiguous, because guessing
    a target is how an autonomous loop edits the wrong file with confidence.
    """
    try:
        stem = Path(test_path).stem
        if not stem.startswith("test_"):
            return None
        subject = stem[len("test_"):]
        if not subject:
            return None
        matches = [
            p for p in (repo_root / "backend").rglob(f"{subject}.py")
            if not _is_test_file(str(p))
        ]
        if len(matches) != 1:
            return None                       # ambiguous -> refuse to guess
        return str(matches[0].relative_to(repo_root)).replace("\\", "/")
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _from_ambient_reds(
    repo_root: Path, census: Any, watcher: Any,
) -> List[DiscoveredWork]:
    """Failing tests at HEAD, READ from the census store. NEVER raises.

    Deliberately synchronous and instant. It used to ``await
    watcher.run_census()``, which runs pytest subprocesses that do not
    cooperate with the event loop — so the Sentinel's first pass sat behind
    minutes of test execution, and an ``asyncio.wait_for`` around it could not
    help (a timeout only fires at an await boundary; blocking work inside runs
    to completion regardless).

    Now the census is an eventual-consistency store this READS. Fresh data
    sharpens the ranking; its absence costs precision for one pass and nothing
    else. A refresh is kicked off in the background and awaited by nobody.
    """
    out: List[DiscoveredWork] = []
    if census is None:
        return out
    snapshot = None
    try:
        snapshot = census.snapshot()
        # Ask for a refresh whenever the data is stale. Returns immediately;
        # single-flight and back-off live in the store, not here.
        census.ensure_refreshing(watcher)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GoalDiscovery] census store unavailable: %r", exc)
        return out
    if snapshot is None:
        logger.info(
            "[GoalDiscovery] no fresh census — ranking on coverage evidence "
            "this pass (a refresh runs in the background)",
        )
        return out
    failures = snapshot.failures
    for failure in failures or ():
        try:
            test_path = str(getattr(failure, "file_path", "") or "")
            test_id = str(getattr(failure, "test_id", "") or "")
            error = str(getattr(failure, "error_text", "") or "")[:200]
            if not test_path:
                continue
            # The fix belongs in the code under test, never in the assertion.
            subject = _subject_of_test(test_path, repo_root)
            if subject is None:
                logger.debug(
                    "[GoalDiscovery] red %s has no unambiguous subject — skipped",
                    test_id,
                )
                continue
            out.append(DiscoveredWork(
                target_file=subject,
                kind="ambient_red",
                evidence=f"{test_id}: {error}" if error else test_id,
                weight=_KIND_WEIGHT["ambient_red"],
                detail={"test_id": test_id, "test_file": test_path},
            ))
        except Exception:  # noqa: BLE001
            continue
    return out


def _iter_uncovered_modules(repo_root: Path) -> Iterator[DiscoveredWork]:
    """Production modules with no test file of the conventional name.

    A GENERATOR, and that is the whole point. Every source in this module is
    now UNCAPPED at collection, because a cap applied before the pass's filters
    have run decides WHICH work is eligible rather than how much of it a pass
    takes on — the defect measured live on the roadmap source (48 passes at
    ``0 candidate(s)``, see ``_from_roadmap_goals``).

    The roadmap could simply be collected whole: the document is bounded. A
    filesystem walk is not, and enumerating every module on every pass is the
    expensive tier this loop exists to avoid. Yielding lazily gets both
    properties at once — the ranking loop pulls exactly as far as it needs, so
    truncation happens AFTER the filters, and the walk still stops early.

    Raises nothing: a walk that breaks mid-iteration yields what it had.
    """
    try:
        tests_root = repo_root / "tests"
        known = {p.name for p in tests_root.rglob("test_*.py")} if tests_root.exists() else set()
        for src in (repo_root / "backend").rglob("*.py"):
            rel = str(src.relative_to(repo_root)).replace("\\", "/")
            if _is_test_file(rel) or _is_governance(rel):
                continue
            if src.name == "__init__.py":
                continue
            if f"test_{src.stem}.py" in known:
                continue
            try:
                if src.stat().st_size < 512:      # a stub is not a gap
                    continue
            except OSError:
                continue
            # The TARGET is the test file to be created — the goal's scope
            # must name what gets WRITTEN, or the cage refuses the op as
            # unsanctioned when it tries to create a path outside its mandate.
            yield DiscoveredWork(
                target_file=f"tests/test_{src.stem}.py",
                subject_file=rel,
                kind="uncovered_module",
                evidence=f"no tests/**/test_{src.stem}.py exists",
                weight=_KIND_WEIGHT["uncovered_module"],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GoalDiscovery] coverage scan degraded: %r", exc)


def _from_uncovered_modules(repo_root: Path, limit: int) -> List[DiscoveredWork]:
    """The first *limit* uncovered modules, as a list.

    Retained as the named seam the tests and any direct caller already use;
    ``discover`` consumes :func:`_iter_uncovered_modules` directly so the cap
    lands after filtering rather than before it.
    """
    out: List[DiscoveredWork] = []
    for item in _iter_uncovered_modules(repo_root):
        if len(out) >= limit:
            break
        out.append(item)
    return out


def _priority_weight(priority: Any) -> float:
    """Evidence weight for an already-SIGNED goal, from its own priority.

    Derived from ``_KIND_WEIGHT``, never a new literal: a signed goal is work
    the operator (or the organism, through the operator's signer) has already
    committed to, so it starts at the strongest evidence the table knows and is
    scaled DOWN by its own declared priority. If the weight table is retuned,
    this moves with it.

    The scale is the priority enum's own ordering, so adding a tier to
    ``GoalPriority`` cannot leave this stale.
    """
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (  # noqa: PLC0415
            GoalPriority,
        )
        order = list(GoalPriority)                      # critical..low
        raw = str(getattr(priority, "value", priority) or "").strip().lower()
        idx = next((i for i, p in enumerate(order) if p.value == raw), None)
        top = max(_KIND_WEIGHT.values())
        if idx is None:
            return top * 0.5
        # critical -> top, low -> top/len(order); linear in the declared rank.
        return top * (len(order) - idx) / len(order)
    except Exception:  # noqa: BLE001
        return max(_KIND_WEIGHT.values()) * 0.5


def _from_roadmap_goals(repo_root: Path) -> List[DiscoveredWork]:
    """Signed roadmap goals that nothing has dispatched yet.

    ## Why this source has to exist

    Discovery had exactly two sources — ``ambient_red`` and
    ``uncovered_module`` — and both scan the FILESYSTEM. Neither can surface a
    goal by id. So the substitution DAG filed 14 correctly-signed goals in one
    session (7 A→B pairs, edges intact, scopes disjoint) and the ledger
    recorded ZERO dispatches of any of them: a correct graph nothing reads.
    ``_roadmap_goal`` in the harness is a LOOKUP that resolves scope for a goal
    somebody already chose; it does not enumerate.

    That also made the DAG gate look like it was working when it was merely
    never consulted — ``blocked=0, dependency_failed=0`` because no candidate
    with edges ever reached it.

    ## What it emits

    One ``DiscoveredWork`` per signed goal, carrying the goal's OWN id, so
    every id-keyed contract downstream — the reconciliation ledger, the DAG's
    ``depends_on`` edges, the settled filter — refers to the same work by the
    same name.

    Deliberately does NOT filter on satisfaction, dependencies or cooldown:
    those are the ranking loop's job and are already implemented there. A
    second copy of that logic here is exactly the duplication that lets two
    filters disagree.

    Reads through ``roadmap_reader.read_roadmap`` — the same reader the CAGE
    consults — so a goal this surfaces is a goal the cage will accept, and an
    unsigned or tampered document yields nothing rather than unverified work.
    """
    out: List[DiscoveredWork] = []
    try:
        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            roadmap_reader as rr,
        )
        # Resolve the roadmap against the AUTHORITATIVE tree.
        #
        # Two wrong answers were possible here and the first fix hit the other
        # one. `roadmap_path()` is RELATIVE by default, so resolving it against
        # the process cwd made this read the live repo's roadmap whatever
        # `repo_root` said (breaking every test that passes a tmp_path).
        # Resolving it against `repo_root` alone then made it INVISIBLE in
        # production: an op runs inside `.worktrees/<session>/`, `.jarvis/` is
        # gitignored, and so the worktree has no roadmap at all — measured live
        # as `0 candidate(s): 0 signed` while the same call returned 26 from
        # the main clone.
        #
        # The roadmap is a property of the REPOSITORY, not of whichever
        # worktree an op happens to execute in, and reading it is a READ.
        # `authoritative_repo_root` is the seam this codebase already uses for
        # exactly that distinction — reads from the authoritative tree, writes
        # to the worktree — so coverage lookups, blast-radius scans, test
        # discovery and now goal discovery all resolve the same way.
        _rm = rr.roadmap_path()
        if _rm.is_absolute():
            _override = _rm
        else:
            try:
                from backend.core.ouroboros.governance.execution_context import (  # noqa: E501,PLC0415
                    authoritative_repo_root,
                )
                _base = authoritative_repo_root(Path(repo_root))
            except Exception:  # noqa: BLE001 — fail-soft to the given root
                _base = Path(repo_root)
            _override = _base / _rm
        verdict, doc, diag = rr.read_roadmap(path_override=_override)
        if doc is None:
            if verdict is not None and str(getattr(verdict, "value", verdict)) not in (
                "no_roadmap",
            ):
                logger.info(
                    "[GoalDiscovery] roadmap unusable (%s) — no signed goals "
                    "this pass: %s", verdict, str(diag)[:120],
                )
            return out

        # NO truncation by document order.
        #
        # `limit` is a cap on how much work a PASS may take on, and the ranking
        # loop applies it after weight-sorting — so imposing it here as a
        # collection cap silently decides WHICH goals are eligible before any
        # filter or weight has run.
        #
        # For a filesystem scan that is harmless: order is arbitrary. The
        # roadmap is append-only, so its order is the opposite of arbitrary —
        # the NEWEST goals sit at the end, and a cap applied here removes
        # exactly the work that was most recently filed. Measured live: the
        # Sentinel passes no limit, so the cap is `_max_candidates()` = 8, the
        # first 8 roadmap entries are all old goals that are governance-refused,
        # satisfied or cooling, and every DAG goal was truncated away unseen —
        # `0 candidate(s): 0 signed` for 48 consecutive passes while the same
        # call with limit=40 returned 8 signed.
        #
        # The document is already bounded by `roadmap_reader.max_goals()`, so
        # collecting all of it is cheap and finite. Let the ranking loop pick
        # the BEST `limit`, not the FIRST-LISTED `limit`.
        for goal in list(getattr(doc, "goals", ()) or ()):
            gid = str(getattr(goal, "goal_id", "") or "").strip()
            files = tuple(str(f) for f in (getattr(goal, "target_files", ()) or ()))
            if not gid or not files:
                continue
            target = files[0].replace("\\", "/")
            # The un-signable floor still applies: a signature cannot authorise
            # the organism to rewrite its own governance.
            if _is_governance(target):
                logger.info(
                    "[GoalDiscovery] %s targets governance — refused at "
                    "discovery, signature or not", gid,
                )
                continue
            out.append(DiscoveredWork(
                target_file=target,
                subject_file=target,
                kind="roadmap_goal",
                evidence=(
                    str(getattr(goal, "title", "") or gid)[:200]
                ),
                weight=_priority_weight(getattr(goal, "priority", None)),
                declared_goal_id=gid,
                detail={
                    "depends_on": list(getattr(goal, "depends_on", ()) or ()),
                    "priority": str(
                        getattr(getattr(goal, "priority", None), "value", "") or ""
                    ),
                    # The SIGNED task text, carried verbatim. `describe()`
                    # returns this rather than deriving one, so the model is
                    # handed the instruction the signature actually covers.
                    "description": str(
                        getattr(goal, "description", "")
                        or getattr(goal, "title", "") or ""
                    ),
                },
            ))
    except Exception as exc:  # noqa: BLE001 — a source may never break a pass
        logger.warning("[GoalDiscovery] roadmap scan degraded: %r", exc)
    return out


# ---------------------------------------------------------------------------
# The sensor
# ---------------------------------------------------------------------------


async def _settled_goal_ids(
    pool: Sequence["DiscoveredWork"], *, repo_root: Path, settled: Any = None,
) -> frozenset:
    """The goal ids in *pool* the repository has already SATISFIED.

    Composes ``goal_reconciliation_ledger.satisfied_goal_ids`` — one ledger
    read for the whole pool, and deliberately NOT ``reconcile``, which asks
    whether a goal's commit is reachable from ``landing_ref``. That is the
    PROMOTION question: autonomous work lands on an ``ouroboros/auto/<session>``
    accumulation branch by design, so reconcile correctly answers ACTIVE until
    an operator merges, and scheduling on it would re-select finished work
    forever. "Have I already built this" is the scheduling question, and it is
    a fact about the ledger alone.

    *settled* is an injection seam mirroring ``cooldown``: any object exposing
    ``satisfied_goal_ids(ids)``. ``None`` resolves the real ledger.

    NEVER raises: an unreadable ledger yields an empty set, which is exactly
    the behaviour this function replaces.
    """
    try:
        goal_ids = []
        for item in pool or ():
            gid = str(getattr(item, "goal_id", "") or "").strip()
            if gid and gid not in goal_ids:
                goal_ids.append(gid)
        if not goal_ids:
            return frozenset()

        oracle = settled
        if oracle is None:
            from backend.core.ouroboros.governance import (  # noqa: PLC0415
                goal_reconciliation_ledger as grl,
            )
            oracle = grl
        # Off the loop: the ledger read is file I/O, and discovery's whole
        # contract is that a pass completes at the speed of the cheap tier.
        return frozenset(
            await asyncio.to_thread(oracle.satisfied_goal_ids, goal_ids)
        )
    except Exception:  # noqa: BLE001 — suppression must never break discovery
        logger.debug("[GoalDiscovery] satisfaction filter degraded", exc_info=True)
        return frozenset()


async def _dag_index() -> Dict[str, Dict[str, Any]]:
    """``{goal_id: {"depends_on": (...), "target": str}}`` from the roadmap.

    One read per discovery pass, off the event loop. The roadmap is the
    authority on edges because that is where the SIGNATURE covers them —
    reading them from anywhere else would be trusting an unattested copy.
    NEVER raises; an unreadable roadmap yields an empty index, i.e. no gating.
    """
    def _read() -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        try:
            from backend.core.ouroboros.governance import (  # noqa: PLC0415
                roadmap_reader as rr,
            )
            _verdict, doc, _diag = rr.read_roadmap()
            for goal in list(getattr(doc, "goals", ()) or ()):
                gid = str(getattr(goal, "goal_id", "") or getattr(goal, "id", "") or "")
                if not gid:
                    continue
                files = tuple(getattr(goal, "target_files", ()) or ())
                out[gid] = {
                    "depends_on": tuple(getattr(goal, "depends_on", ()) or ()),
                    "target": str(files[0]) if files else "",
                }
        except Exception:  # noqa: BLE001
            logger.debug("[GoalDiscovery] roadmap DAG index degraded", exc_info=True)
        return out

    try:
        return await asyncio.to_thread(_read)
    except Exception:  # noqa: BLE001
        return {}


def _dependency_state(
    goal_id: str,
    dag_index: Dict[str, Dict[str, Any]],
    settled_ids: Any,
    cooldown: Any,
) -> Any:
    """The DAG verdict for *goal_id*, or ``None`` when it has no edges.
    NEVER raises — a gate that fails closed would wedge every goal."""
    try:
        entry = (dag_index or {}).get(str(goal_id or ""))
        if not entry:
            return None
        deps = tuple(entry.get("depends_on") or ())
        if not deps:
            return None
        from backend.core.ouroboros.governance.autonomy import (  # noqa: PLC0415
            goal_dag,
        )
        targets = {
            gid: str((meta or {}).get("target") or "")
            for gid, meta in (dag_index or {}).items()
        }
        exhausted = goal_dag.exhausted_goal_ids(
            deps, roadmap_targets=targets, cooldown=cooldown,
        )
        return goal_dag.dependency_verdict(
            goal_id, deps, satisfied=frozenset(settled_ids or ()),
            exhausted=exhausted,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[GoalDiscovery] dependency gate degraded", exc_info=True)
        return None


#: Liveness tiers. Higher sorts first. Ordinals, not weights — they express an
#: ORDER ("prefer work that can finish"), and mixing them into the evidence
#: weight would make one number answer two unrelated questions.
LIVENESS_LANDABLE = 3      # target exists AND has a covering test
LIVENESS_CREATES_TEST = 2  # target is a test file this goal will write
LIVENESS_NO_TEST = 1       # target exists, nothing covers it — sheds at VALIDATE
LIVENESS_DEAD = 0          # target absent and not a test — nothing to edit


def _covering_test_stems(repo_root: Path) -> FrozenSet[str]:
    """Every module stem that has a ``tests/**/test_<stem>.py`` covering it.

    Built ONCE per pass and handed to the ranker, because ``sorted`` calls its
    key per element and an ``rglob`` of the test tree per candidate would turn
    one walk into N — a filesystem pass on the Sentinel's critical path, which
    is the same mistake that put the census there.

    Uses the same ``test_<stem>.py`` convention the coverage sensor uses to
    call a module uncovered, so "covered" means the same thing to the sorter
    as it does to the sensor that filed the goal.
    """
    try:
        tests_root = repo_root / "tests"
        if not tests_root.is_dir():
            return frozenset()
        return frozenset(
            p.stem[5:] for p in tests_root.rglob("test_*.py") if p.is_file()
        )
    except Exception:  # noqa: BLE001 — an unreadable tree just means no cover
        return frozenset()


def _liveness_rank(
    work: "DiscoveredWork",
    repo_root: Path,
    covering_stems: FrozenSet[str] = frozenset(),
) -> int:
    """How far this work can actually get through the pipeline.

    ## The starvation this fixes

    Every roadmap goal carries the same evidence weight — measured on the live
    queue, all 27 of them at 0.75 — so ``sorted(key=-weight)`` was a stable
    sort over equal keys and fell through to DOCUMENT ORDER. The roadmap is
    append-only and the Sentinel takes ``candidates[0]``, so work was consumed
    strictly oldest-first, and the cap of 8 was a window onto the OLDEST eight
    goals rather than the best eight.

    Measured effect on that queue: the one landable production-file goal
    (``backend/api/sse_contract.py``) sat at index 26 of 27 — permanently
    outside the cap, which is why soak after soak produced nothing — and ranks
    to index 4. The seven goals whose target exists with nothing covering it
    (they shed at VALIDATE) sink to the tail instead of interleaving.

    ## Ranked, never shed

    The instinct is to drop goals whose target file does not exist. That would
    delete the DAG's entire test-synthesis half, which exists precisely to
    CREATE those files and is the only thing that can unblock the repair goals
    waiting behind them. Ranking starves nothing: landable work floats, dead
    work sinks, and a goal that becomes landable later rises on its own.

    ## What it deliberately does NOT read

    The DAG. Dependency state is already the ``_EligibilityGate``'s job, and it
    REFUSES a blocked goal rather than deprioritising it. Re-deriving it here
    would be a second copy of the rules living next to a source — the exact
    duplication this module's gate was extracted to prevent, and the way two
    filters come to disagree.

    NEVER raises: an unrankable pass ranks everything equal, which is exactly
    today's behaviour.
    """
    try:
        target = str(getattr(work, "target_file", "") or "")
        if not target:
            return LIVENESS_DEAD
        # Separators normalised BEFORE the existence check, not just the name
        # check: the roadmap is authored from both trees, and a backslash path
        # is one path component on POSIX, so `is_file()` would say no and a
        # landable goal would rank as one that creates its own target.
        name = PurePosixPath(target.replace("\\", "/"))
        is_test = name.name.startswith("test_")
        if not (repo_root / name).is_file():
            # A test file this goal is going to WRITE is live work; a source
            # file that is simply absent is not something to edit.
            return LIVENESS_CREATES_TEST if is_test else LIVENESS_DEAD
        if is_test:
            return LIVENESS_LANDABLE        # a test IS its own cover
        if str(getattr(work, "kind", "")) == "ambient_red":
            # A failing test IS the covering test, and it has already proven
            # it exercises this file. Asking the `test_<stem>.py` convention
            # about it would demote hard evidence to L1 over a NAMING
            # question, sinking a real red below speculative test-writing.
            return LIVENESS_LANDABLE
        return (
            LIVENESS_LANDABLE if name.stem in covering_stems else LIVENESS_NO_TEST
        )
    except Exception:  # noqa: BLE001 — ranking never breaks a pass
        return LIVENESS_NO_TEST


def is_dispatchable(work: "DiscoveredWork", repo_root: Path) -> bool:
    """Whether spending an op on *work* could produce a write at all.

    The dispatcher's contract, named here so the Sentinel does not reach into
    the ranker's internals. False ONLY for :data:`LIVENESS_DEAD` — a target
    that does not exist and is not a test file this goal would create. Every
    other tier is dispatchable; being unlikely to land is not the same as
    having nothing to edit, and deciding THAT is the pipeline's job.
    """
    return _liveness_rank(work, repo_root) > LIVENESS_DEAD


class _EligibilityGate:
    """The pass's one filter. Every source is judged by this and nothing else.

    Extracted from ``discover``'s ranking loop so the coverage walk can be
    truncated AFTER filtering instead of before it. A second copy of these
    rules living next to a source is the duplication that lets two filters
    disagree — the reason ``_from_roadmap_goals`` deliberately filters nothing.

    Two entry points, because the checks have different costs:

    * :meth:`cheap_ok` — dedupe, the governance cage and the cooldown. Pure
      in-memory; safe to run against a lazy walk.
    * :meth:`admit` — everything in ``cheap_ok`` plus the two id-keyed
      questions that need a resolved ``settled_ids`` set. Marks the target seen
      ON ACCEPTANCE ONLY, so a candidate dropped for satisfaction never
      shadows a later one.
    """

    def __init__(self, *, dag_index: Dict[str, Dict[str, Any]], cooldown: Any) -> None:
        self._dag_index = dag_index
        self._cooldown = cooldown
        self.seen: set = set()

    def _basic_ok(self, item: "DiscoveredWork") -> bool:
        """Dedupe and the governance cage. No ledger, no cooldown."""
        target = item.target_file
        return not (target in self.seen or _is_governance(target))

    def _cooling(self, item: "DiscoveredWork") -> bool:
        target = item.target_file
        if self._cooldown is None:
            return False
        try:
            if self._cooldown.is_cooling(target):
                logger.info(
                    "[GoalDiscovery] %s is cooling — skipped this pass", target,
                )
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def cheap_ok(self, item: "DiscoveredWork") -> bool:
        """The ledger-free half of the gate — everything :meth:`admit` can
        decide without a settled set. A PRE-filter for the lazy walk only; the
        authoritative order lives in :meth:`admit`. NEVER raises."""
        return self._basic_ok(item) and not self._cooling(item)

    def admit(self, item: "DiscoveredWork", settled_ids: frozenset) -> bool:
        """Whether this pass may take *item* on. NEVER raises.

        Order is load-bearing, not incidental: SETTLEMENT is asked before
        cooling. A landing CLEARS its own target's cooldown, so after a
        successful op the cooldown is not a brake at all — settlement is the
        only thing that stops re-selection, and it must also be the reason
        REPORTED, or a landed goal that happens to be cooling is logged as
        merely cooling and the re-discovery spin looks like back-pressure
        instead of the bug it is.
        """
        if not self._basic_ok(item):
            return False
        target = item.target_file
        # THE re-discovery spin. Discovery reads the WORKING TREE; autonomous
        # work commits to an accumulation branch. So a landed goal is still
        # "uncovered" here, gets re-selected, and the only brake -- the
        # cooldown -- had just been CLEARED by its own success.
        #
        # Measured, bt-2026-09-08-225144: after
        # ov-auto-uncovered-module-apply-emergency-cpu-fix landed at 16:14:41
        # (sha bb575e9b28, recorded SATISFIED), passes 8-36 re-dispatched it
        # 29 times in 8 seconds. Each repeat was correctly refused downstream
        # (`fc=duplication`), so nothing corrupt was written -- but it consumed
        # the discoverable targets and left the last 17 minutes of the session
        # idle.
        #
        # The ledger's own docstring already called itself "the SAME one that
        # stops a satisfied goal being re-dispatched". It was, at the point of
        # SCORING a dispatch that had already happened. Nothing asked it before
        # choosing. This is that question, asked first.
        if item.goal_id and item.goal_id in settled_ids:
            logger.info(
                "[GoalDiscovery] %s already SATISFIED (%s) — not re-selecting "
                "work that has landed", target, item.goal_id,
            )
            return False
        # DAG gate. A goal whose prerequisite has not landed must not be
        # scheduled; enforcing it HERE, at selection, is what makes "B never
        # runs before A" structural rather than a check somebody has to
        # remember at execution time. A DEPENDENCY_FAILED goal is dropped
        # outright — a dependent whose prerequisite is unreachable is a queue
        # entry nothing can ever satisfy.
        _dep = _dependency_state(
            item.goal_id, self._dag_index, settled_ids, self._cooldown,
        )
        if _dep is not None and not _dep.runnable:
            logger.info("[GoalDiscovery] %s %s", target, _dep.render())
            return False
        if self._cooling(item):
            return False
        self.seen.add(target)
        return True


def _take_cheap(
    walk: Iterator["DiscoveredWork"], gate: "_EligibilityGate", limit: int,
) -> List["DiscoveredWork"]:
    """Pull up to *limit* items off *walk* that clear the gate's cheap half.

    Blocking by design — the walk is filesystem I/O, so ``discover`` runs this
    in a thread. Resumable: the generator keeps its position, so a batch whose
    members are later dropped for satisfaction is followed by the NEXT
    candidates rather than by the same ones.

    Intra-batch dedupe is its own set because the gate marks a target seen only
    when it is accepted, and two modules in different packages can share a stem
    (``backend/a/foo.py`` and ``backend/b/foo.py`` both name
    ``tests/test_foo.py``).
    """
    out: List["DiscoveredWork"] = []
    pending: set = set()
    if limit <= 0:
        return out
    for item in walk:
        if item.target_file in pending:
            continue
        if not gate.cheap_ok(item):
            continue
        pending.add(item.target_file)
        out.append(item)
        if len(out) >= limit:
            break
    return out


async def discover(
    *,
    repo_root: Path,
    watcher: Any = None,
    cooldown: Any = None,
    census: Any = None,
    limit: Optional[int] = None,
    settled: Any = None,
) -> Tuple[DiscoveredWork, ...]:
    """Rank the work the repository is asking for. NEVER raises.

    Dual-tier by design. Tier 1 is CHEAP and always available — a filesystem
    walk, executed off the event loop. Tier 2 is the census, read from a store
    that may or may not have fresh data. Nothing here ever waits on a pytest
    subprocess, so a pass completes at the speed of the cheap tier no matter
    what the test suite is doing.

    Returns highest-evidence first, with the cage, cooling targets and
    ambiguous test-subjects already removed.
    """
    cap = int(limit or _max_candidates())
    if census is None and watcher is not None:
        try:
            from backend.core.ouroboros.governance.autonomy.census_store import (  # noqa: E501,PLC0415
                get_default_store,
            )
            census = get_default_store()
        except Exception:  # noqa: BLE001
            census = None
    try:
        # Synchronous and instant — a store read, not a census run.
        reds = _from_ambient_reds(Path(repo_root), census, watcher)
    except Exception:  # noqa: BLE001
        reds = []
    # Signed goals FIRST: work the operator's signer has already sanctioned
    # outranks work discovered from evidence, and it is also the cheapest to
    # resolve — one document read, no filesystem walk.
    signed: List[DiscoveredWork] = []
    try:
        # Deliberately NOT capped here — see `_from_roadmap_goals`. The
        # document is already bounded; the ranking loop applies `cap` after
        # weighting, so the pass takes the best work rather than the
        # first-listed work.
        signed = await asyncio.to_thread(
            _from_roadmap_goals, Path(repo_root),
        )
    except Exception:  # noqa: BLE001
        signed = []

    if cooldown is None:
        try:
            from backend.core.ouroboros.governance.autonomy.sentinel_cooldown import (  # noqa: E501,PLC0415
                get_default_ledger,
            )
            cooldown = get_default_ledger()
        except Exception:  # noqa: BLE001
            cooldown = None

    # Both document sources are collected WHOLE; the filesystem walk stays
    # lazy. Nothing is truncated before the filters run.
    # LIVENESS OUTRANKS EVIDENCE. Every roadmap goal carries the same `high`
    # weight, so the old `key=-weight` was a stable sort over equal keys and
    # silently degraded to DOCUMENT ORDER on an append-only roadmap — with the
    # Sentinel taking `candidates[0]`, the oldest unsatisfiable goals held the
    # head of the queue and newly authored landable work sat past the cap.
    # Weight still breaks ties, so within a liveness tier the ranking is
    # unchanged. The walk is one pass, off the event loop, reused by every key.
    covering = await asyncio.to_thread(_covering_test_stems, Path(repo_root))
    root = Path(repo_root)
    # Ranked ONCE and carried, not recomputed for the sort and again for the
    # census: each rank costs a `stat`, and a queue-wide double stat every pass
    # is the kind of cost that arrives unnoticed on the critical path.
    scored = [(_liveness_rank(w, root, covering), w) for w in signed + reds]
    scored.sort(key=lambda rw: (-rw[0], -rw[1].weight))
    documented = [w for _, w in scored]
    if scored:
        # WARNING, like the Sentinel's own pass breadcrumbs and for the same
        # reason: a headless soak's log carries WARNING and above, so an INFO
        # census of the queue is invisible in exactly the run that needs it.
        # One line per pass, and a pass is minutes.
        logger.warning(
            "[GoalDiscovery] liveness census (L3 landable → L0 dead): %s | head=%s",
            " ".join(
                f"L{r}={n}"
                for r, n in sorted(Counter(r for r, _ in scored).items(), reverse=True)
            ),
            documented[0].target_file,
        )
    # The roadmap's dependency edges, read once for the whole pass.
    dag_index = await _dag_index()
    gate = _EligibilityGate(dag_index=dag_index, cooldown=cooldown)
    ranked: List[DiscoveredWork] = []

    async def _rank(batch: Sequence[DiscoveredWork]) -> None:
        """Admit as much of *batch* as the cap still allows.

        One batched ledger read per call — ``satisfied_goal_ids`` answers for a
        whole group, so asking it per candidate would turn one file read into
        N. Splitting the pass into a few batches is the price of never
        truncating before the filter, and a ledger read is not a git pass.
        """
        if not batch or len(ranked) >= cap:
            return
        settled_ids = await _settled_goal_ids(
            batch, repo_root=Path(repo_root), settled=settled,
        )
        for item in batch:
            if len(ranked) >= cap:
                return
            if gate.admit(item, settled_ids):
                ranked.append(item)

    # The cheap tier's weight is FIXED for every item it yields, so the global
    # ranking is exact without materialising the walk: everything that outranks
    # an uncovered module is considered first, the walk fills whatever the cap
    # still has room for, and the lower-weight tail follows.
    _uncovered_weight = _KIND_WEIGHT["uncovered_module"]
    await _rank([w for w in documented if w.weight >= _uncovered_weight])

    # THE BUDGET DEFECT, second instance — same class as the roadmap cap, and
    # it survived that fix. The coverage scan used to be sized
    # `cap - len(reds)` and skipped entirely when `len(reds) >= cap`, both read
    # from the RAW red count. Reds are filtered downstream (satisfied, cooling,
    # dependency-blocked) and several failing tests routinely collapse onto one
    # subject file, so a pass could hold `cap` reds, admit one, and still
    # refuse to look at the cheap tier — the same "decide eligibility before
    # filtering" mistake, one source over.
    #
    # The shortfall is now measured from candidates that SURVIVED the gate, and
    # the walk is pulled until that shortfall is filled or the tree is
    # exhausted. `_take_cheap` runs off the loop: it is filesystem I/O.
    if len(ranked) < cap:
        # `iter()` is load-bearing, not decoration: the batches must resume
        # where the previous one stopped. A source that returns a re-iterable
        # (a list — which is exactly what a test double supplies) would
        # otherwise hand back the same candidates every round, and a batch the
        # ledger prunes without marking anything seen would never terminate.
        walk = iter(_iter_uncovered_modules(Path(repo_root)))
        while len(ranked) < cap:
            try:
                batch = await asyncio.to_thread(
                    _take_cheap, walk, gate, cap - len(ranked),
                )
            except Exception:  # noqa: BLE001 — a source may never break a pass
                break
            if not batch:
                break
            await _rank(batch)

    await _rank([w for w in documented if w.weight < _uncovered_weight])
    logger.info(
        "[GoalDiscovery] %d candidate(s): %d signed, %d red, %d uncovered",
        len(ranked),
        sum(1 for r in ranked if r.kind == "roadmap_goal"),
        sum(1 for r in ranked if r.kind == "ambient_red"),
        sum(1 for r in ranked if r.kind == "uncovered_module"),
    )
    return tuple(ranked)


def synthesize_and_sign(work: DiscoveredWork, **kwargs) -> Any:
    """Turn discovered work into a SIGNED roadmap goal. NEVER raises.

    Composes ``operator_goal_sanction.author_and_sign_goal`` — the one signer
    the operator CLI and the ``/goal`` verb already use. There is deliberately
    no second signing path: a synthesized goal must be verifiable in exactly
    the way a typed one is, and the cage re-derives authority from the signed
    document rather than trusting this module.
    """
    try:
        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            operator_goal_sanction as ogs,
        )
        if not discovery_enabled():
            return ogs.SanctionResult(
                False, goal_id=work.goal_id, reason="discovery_disabled",
                detail=f"{_ENV_ENABLED} is not set",
            )
        spec = ogs.GoalSpec(
            goal_id=work.goal_id,
            title=f"[auto] {work.kind}: {Path(work.target_file).name}",
            description=work.describe(),
            target_files=ogs.normalize_target_files((work.target_file,)),
            target_symbols=tuple(work.symbols),
            success_criteria=(
                "the named test passes and no previously-passing test regresses"
                if work.kind == "ambient_red"
                else "new tests cover the module's public behaviour and pass"
            ),
            note=f"synthesized by the Goal Discovery Sensor; evidence: {work.evidence}"[:400],
        )
        return ogs.author_and_sign_goal(spec, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GoalDiscovery] synthesis degraded: %r", exc)
        try:
            from backend.core.ouroboros.governance import operator_goal_sanction as ogs
            return ogs.SanctionResult(
                False, goal_id=work.goal_id,
                reason=f"synthesis_failed:{type(exc).__name__}", detail=str(exc)[:200],
            )
        except Exception:  # noqa: BLE001
            return None
