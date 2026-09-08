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
    """One unit of work the repository is asking for, with its evidence."""

    target_file: str
    kind: str
    evidence: str
    symbols: Tuple[str, ...] = ()
    weight: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def goal_id(self) -> str:
        """A stable, readable id derived from the target and the reason.

        Derived rather than random so the SAME latent problem re-discovered
        later collides with its own prior goal id — which the signer refuses
        as a duplicate. That refusal is a feature: it stops the sensor filing
        the same work twice.
        """
        stem = Path(self.target_file).stem[:32]
        slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "target"
        return f"ov-auto-{self.kind.replace('_', '-')}-{slug}"

    def describe(self) -> str:
        """The task text handed to the model."""
        if self.kind == "ambient_red":
            return (
                f"A test is failing at HEAD against `{self.target_file}`: "
                f"{self.evidence}. Repair the PRODUCTION code so the test "
                f"passes. Do not weaken, skip or delete the test — if the test "
                f"is genuinely wrong, decline and say so rather than editing "
                f"the assertion."
            )
        return (
            f"`{self.target_file}` has no corresponding test module. Add "
            f"focused tests covering its public behaviour and edge cases. "
            f"Do not modify the production file."
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


async def _from_ambient_reds(
    repo_root: Path, watcher: Any,
) -> List[DiscoveredWork]:
    """Failing tests at HEAD, via the TestWatcher census. NEVER raises."""
    out: List[DiscoveredWork] = []
    if watcher is None:
        return out
    budget = _census_budget_s()
    try:
        failures, _passed, _skipped = await asyncio.wait_for(
            watcher.run_census(), timeout=budget,
        )
    except asyncio.TimeoutError:
        # Not a fault — a census slower than its budget simply does not get to
        # decide this pass. The coverage source below still answers, so the
        # loop degrades to weaker evidence instead of stalling on the strongest.
        logger.warning(
            "[GoalDiscovery] census exceeded its %.0fs budget — this pass "
            "ranks on coverage evidence only", budget,
        )
        return out
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GoalDiscovery] census unavailable: %r", exc)
        return out
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
            out.append(DiscoveredWork(
                target_file=rel,
                kind="uncovered_module",
                evidence=f"no tests/**/test_{src.stem}.py exists",
                weight=_KIND_WEIGHT["uncovered_module"],
            ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[GoalDiscovery] coverage scan degraded: %r", exc)
    return out


# ---------------------------------------------------------------------------
# The sensor
# ---------------------------------------------------------------------------


async def discover(
    *,
    repo_root: Path,
    watcher: Any = None,
    cooldown: Any = None,
    limit: Optional[int] = None,
) -> Tuple[DiscoveredWork, ...]:
    """Rank the work the repository is asking for. NEVER raises.

    Returns highest-evidence first, with the cage, cooling targets and
    ambiguous test-subjects already removed.
    """
    cap = int(limit or _max_candidates())
    try:
        reds = await _from_ambient_reds(Path(repo_root), watcher)
    except Exception:  # noqa: BLE001
        reds = []
    uncovered: List[DiscoveredWork] = []
    if len(reds) < cap:
        try:
            uncovered = await asyncio.to_thread(
                _from_uncovered_modules, Path(repo_root), cap - len(reds),
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

    seen: set = set()
    ranked: List[DiscoveredWork] = []
    for item in sorted(reds + uncovered, key=lambda w: -w.weight):
        target = item.target_file
        if target in seen or _is_governance(target):
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
        "[GoalDiscovery] %d candidate(s): %d red, %d uncovered",
        len(ranked),
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
