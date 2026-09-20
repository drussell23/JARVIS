"""Landability triage at intake — ask VALIDATE's question before paying for GENERATE.

## The loop this breaks

A work item names ``backend/api/audio_error_fallback.py``. Nothing covers that
file. The pipeline classifies, routes, plans, spends a full GENERATE on the
local lane, and only then does VALIDATE discover there is no test to judge the
candidate with. It refuses honestly (``no_covering_test``), files the
test-synthesis prerequisite through the goal DAG — and the signer answers
``duplicate_id``, because the previous soak filed the identical pair. The op
fails. The next boot re-emits the same item and the whole sequence repeats.

Measured on bt-2026-09-20-081946: 6 of the 7 ops that reached VALIDATE died
this way, every one with ``A=duplicate_id``. The only op that landed was the
only one whose target had a covering test.

The Sentinel's discovery path never does this: it ranks by liveness and the
``_EligibilityGate`` refuses a goal whose prerequisite has not landed. But
that gate has one caller, and the work-order path is not it.

## What this does

Asks the SAME oracle VALIDATE asks — ``TestRunner.resolve_affected_tests``,
strategies 0-3 including the AST import map — at intake, where the answer
costs a cached lookup instead of a generation. Two components that ask one
question through one function cannot come to disagree about the answer.

* **covered** (or the target IS a test) → ``LANDABLE``; emitted first.
* **a test file that does not exist yet** → ``CREATES_TEST``; emitted next.
  This is the work that unblocks everything else, never deferred.
* **uncovered** → the A→B substitution is filed *now*, through the same
  ``goal_dag`` seam VALIDATE uses after the fact. Once BOTH goals are verifiably
  on the signed roadmap the item is ``DEFERRED``: the roadmap owns the work,
  in an order that can actually finish, and the lane is not spent on it.
* **anything uncertain** → ``UNCOVERED``; emitted last, exactly as before.

## What it deliberately does not do

Shed. An item is only withheld when its work is provably represented on the
roadmap by the dependent goal. Any fault — resolver timeout, unsigned roadmap,
substitution disabled — degrades to the legacy behaviour of emitting the item.
A gate on the intake path that fails closed is an outage, not a safeguard.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, FrozenSet, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LANDABLE = "landable"
CREATES_TEST = "creates_test"
UNCOVERED = "uncovered"
DEFERRED = "deferred"

#: Emission order. Landable work first: it is the only kind that can finish
#: today. Test creation second: it is what makes tomorrow's work landable.
_ORDER = (LANDABLE, CREATES_TEST, UNCOVERED, DEFERRED)

_ENV_ENABLED = "JARVIS_INTAKE_LANDABILITY_GATE_ENABLED"
_ENV_RESOLVE_TIMEOUT_S = "JARVIS_INTAKE_LANDABILITY_RESOLVE_TIMEOUT_S"
# The first resolve builds the AST import map over the whole test tree; every
# later one is a cache hit. Generous, because the alternative being avoided is
# a multi-minute generation, and a timeout merely restores legacy behaviour.
_DEFAULT_RESOLVE_TIMEOUT_S = 120.0

Resolver = Callable[[Tuple[Path, ...]], Awaitable[Sequence[Path]]]


def gate_enabled() -> bool:
    """Master switch, default ON. OFF is byte-identical legacy emission."""
    try:
        raw = os.environ.get(_ENV_ENABLED, "true")
        return raw.strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return True


def _resolve_timeout_s() -> float:
    try:
        value = float(os.environ.get(_ENV_RESOLVE_TIMEOUT_S, "") or 0)
        return value if value > 0 else _DEFAULT_RESOLVE_TIMEOUT_S
    except (TypeError, ValueError):
        return _DEFAULT_RESOLVE_TIMEOUT_S


@dataclass(frozen=True)
class Landability:
    """One work item, judged before any model token is spent on it."""

    state: str
    targets: Tuple[str, ...] = ()
    covering: Tuple[str, ...] = ()
    reason: str = ""

    @property
    def dispatchable(self) -> bool:
        return self.state != DEFERRED

    @property
    def rank(self) -> int:
        """Sort key, lower emits first. Unknown states sort with UNCOVERED."""
        try:
            return _ORDER.index(self.state)
        except ValueError:
            return _ORDER.index(UNCOVERED)


def _is_test_path(posix: PurePosixPath) -> bool:
    return posix.name.startswith("test_") and posix.suffix == ".py"


class LandabilityTriage:
    """Judges work items for one repo. Holds ONE resolver so the AST import
    map is built once per process, not once per item."""

    def __init__(
        self,
        repo_root: Path,
        *,
        resolver: Optional[Resolver] = None,
        roadmap_ids: Optional[Callable[[], FrozenSet[str]]] = None,
    ) -> None:
        self._root = Path(repo_root)
        self._resolver = resolver
        self._roadmap_ids = roadmap_ids

    # -- the oracle --------------------------------------------------------

    def _default_resolver(self) -> Resolver:
        from backend.core.ouroboros.governance.test_runner import (  # noqa: PLC0415
            TestRunner,
        )
        return TestRunner(self._root).resolve_affected_tests

    async def _covering_tests(self, sources: Tuple[Path, ...]) -> Optional[Tuple[str, ...]]:
        """Tests covering *sources*, ``()`` for none, ``None`` for "could not
        ask" — which callers must treat as unknown, never as uncovered."""
        try:
            if self._resolver is None:
                self._resolver = self._default_resolver()
            found = await asyncio.wait_for(
                self._resolver(sources), timeout=_resolve_timeout_s(),
            )
            return tuple(str(p) for p in (found or ()))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — includes TimeoutError
            logger.debug("[Landability] coverage resolve degraded", exc_info=True)
            return None

    # -- the deferral ------------------------------------------------------

    def _ids_on_roadmap(self) -> FrozenSet[str]:
        try:
            if self._roadmap_ids is not None:
                return frozenset(self._roadmap_ids())
            from backend.core.ouroboros.governance.operator_goal_sanction import (  # noqa: PLC0415
                roadmap_goal_ids,
            )
            return roadmap_goal_ids()
        except Exception:  # noqa: BLE001
            logger.debug("[Landability] roadmap id read degraded", exc_info=True)
            return frozenset()

    def _substitute(self, subject: str, description: str) -> Optional[str]:
        """File the A→B pair for *subject*. Returns the reason the item may be
        deferred, or ``None`` when its work is NOT safely on the roadmap."""
        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            operator_goal_sanction as ogs,
        )
        from backend.core.ouroboros.governance.autonomy import (  # noqa: PLC0415
            goal_dag,
        )
        # The signer writes to the roadmap of the tree IT lives in. Filing a
        # path from any other tree would sign a goal about the wrong file.
        if not ogs.governs(self._root):
            return None
        plan = goal_dag.plan_substitution(
            subject_file=subject, original_description=description,
        )
        if plan is None:
            return None
        goal_dag.file_substitution(plan)
        # Trust the signed document, not the filing call's return value: the
        # pair may have been filed by this call, by a previous soak, or half
        # of each. What matters is whether the DEPENDENT — the goal carrying
        # this item's work — is verifiably there, behind its prerequisite.
        ids = self._ids_on_roadmap()
        if plan.goal_a_id in ids and plan.goal_b_id in ids:
            return f"roadmap owns it: {plan.render()}"
        return None

    # -- the verdict -------------------------------------------------------

    async def assess(
        self, targets: Sequence[str], description: str = "",
    ) -> Landability:
        """Judge one item. NEVER raises; every fault answers ``UNCOVERED``,
        which is emitted — i.e. the behaviour before this gate existed."""
        try:
            names = tuple(
                PurePosixPath(str(t).replace("\\", "/"))
                for t in (targets or ()) if str(t or "").strip()
            )
            shown = tuple(str(n) for n in names)
            if not names:
                return Landability(UNCOVERED, shown, reason="no targets")

            tests = [n for n in names if _is_test_path(n)]
            if any((self._root / n).is_file() for n in tests):
                return Landability(LANDABLE, shown, reason="a test is its own cover")

            sources = tuple(
                self._root / n for n in names
                if not _is_test_path(n) and (self._root / n).is_file()
            )
            if sources:
                covering = await self._covering_tests(sources)
                if covering is None:
                    return Landability(UNCOVERED, shown, reason="coverage unknown")
                if covering:
                    return Landability(
                        LANDABLE, shown, covering=covering,
                        reason=f"{len(covering)} covering test file(s)",
                    )
            if tests:
                return Landability(CREATES_TEST, shown, reason="writes a new test")
            if not sources:
                return Landability(UNCOVERED, shown, reason="no existing target")

            subject = next(
                (str(p.relative_to(self._root)) for p in sources if p.suffix == ".py"),
                "",
            )
            if subject:
                why = await asyncio.to_thread(self._substitute, subject, description)
                if why:
                    return Landability(DEFERRED, shown, reason=why)
            return Landability(UNCOVERED, shown, reason="no covering test")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[Landability] assess degraded", exc_info=True)
            return Landability(UNCOVERED, tuple(str(t) for t in (targets or ())),
                               reason="degraded")


__all__ = [
    "CREATES_TEST",
    "DEFERRED",
    "LANDABLE",
    "UNCOVERED",
    "Landability",
    "LandabilityTriage",
    "gate_enabled",
]
