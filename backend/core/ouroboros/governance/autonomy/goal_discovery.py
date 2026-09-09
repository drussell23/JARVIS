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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.GoalDiscovery")

__all__ = [
    "DiscoveredWork",
    "discover",
    "discovery_enabled",
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


def _from_uncovered_modules(repo_root: Path, limit: int) -> List[DiscoveredWork]:
    """Production modules with no test file of the conventional name."""
    out: List[DiscoveredWork] = []
    try:
        tests_root = repo_root / "tests"
        known = {p.name for p in tests_root.rglob("test_*.py")} if tests_root.exists() else set()
        for src in (repo_root / "backend").rglob("*.py"):
            if len(out) >= limit:
                break
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
            out.append(DiscoveredWork(
                target_file=f"tests/test_{src.stem}.py",
                subject_file=rel,
                kind="uncovered_module",
                evidence=f"no tests/**/test_{src.stem}.py exists",
                weight=_KIND_WEIGHT["uncovered_module"],
            ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GoalDiscovery] coverage scan degraded: %r", exc)
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


def _from_roadmap_goals(repo_root: Path, limit: int) -> List[DiscoveredWork]:
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
        # Resolve the roadmap against the repo we were ASKED about.
        #
        # `roadmap_path()` returns a RELATIVE path by default, which resolves
        # against the process cwd — so this source read the live repository's
        # roadmap no matter which `repo_root` it was given, and discovery for
        # one tree returned another tree's work. Harmless while exactly one
        # repo exists; wrong the moment that stops being true, and it broke
        # every existing discovery test that passes a tmp_path.
        _rm = rr.roadmap_path()
        _override = _rm if _rm.is_absolute() else (Path(repo_root) / _rm)
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

        for goal in list(getattr(doc, "goals", ()) or ()):
            if len(out) >= limit:
                break
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
        signed = await asyncio.to_thread(
            _from_roadmap_goals, Path(repo_root), cap,
        )
    except Exception:  # noqa: BLE001
        signed = []

    uncovered: List[DiscoveredWork] = []
    if len(reds) + len(signed) < cap:
        try:
            uncovered = await asyncio.to_thread(
                _from_uncovered_modules, Path(repo_root),
                cap - len(reds) - len(signed),
            )
        except Exception:  # noqa: BLE001
            uncovered = []

    if cooldown is None:
        try:
            from backend.core.ouroboros.governance.autonomy.sentinel_cooldown import (  # noqa: E501,PLC0415
                get_default_ledger,
            )
            cooldown = get_default_ledger()
        except Exception:  # noqa: BLE001
            cooldown = None

    pool = sorted(signed + reds + uncovered, key=lambda w: -w.weight)
    # Work already SATISFIED is not work. Resolved once for the whole pool
    # (one ledger read, one git pass) rather than per candidate.
    settled_ids = await _settled_goal_ids(pool, repo_root=Path(repo_root), settled=settled)
    # The roadmap's dependency edges, read once for the whole pass.
    dag_index = await _dag_index()

    seen: set = set()
    ranked: List[DiscoveredWork] = []
    for item in pool:
        target = item.target_file
        if target in seen or _is_governance(target):
            continue
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
            continue
        # DAG gate. A goal whose prerequisite has not landed must not be
        # scheduled; enforcing it HERE, at selection, is what makes "B never
        # runs before A" structural rather than a check somebody has to
        # remember at execution time. A DEPENDENCY_FAILED goal is dropped
        # outright — a dependent whose prerequisite is unreachable is a queue
        # entry nothing can ever satisfy.
        _dep = _dependency_state(item.goal_id, dag_index, settled_ids, cooldown)
        if _dep is not None and not _dep.runnable:
            logger.info("[GoalDiscovery] %s %s", target, _dep.render())
            continue
        if cooldown is not None:
            try:
                if cooldown.is_cooling(target):
                    logger.info(
                        "[GoalDiscovery] %s is cooling — skipped this pass", target,
                    )
                    continue
            except Exception:  # noqa: BLE001
                pass
        seen.add(target)
        ranked.append(item)
        if len(ranked) >= cap:
            break
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
