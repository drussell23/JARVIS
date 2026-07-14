"""epistemic_runners.py — construct the REAL probe/SBT/orange runners the
EpistemicBudget escalation needs, and bridge the empty budget signal to the
Move-5 confidence-probe pipeline using the op's context.

Task #4b. Root cause the audit named: the provider call sites
(``providers.py`` / ``doubleword_provider.py``) call
``epistemic_budget_provider_bridge.attach_to_provider_run(...)`` passing only
``op_id`` / ``route`` / ``risk_tier`` — ``probe_runner`` / ``sbt_runner`` /
``orange_queue`` are left ``None``, and the concrete runners are never
constructed anywhere. So when the budget policy fires ``PROBE_TRIGGERED`` the
hook records ``no_probe_runner_injected`` and moves on: the entire
confidence-drop → probe → belief-update loop resolves to a no-op observer.

The wire is deliberately provider-side, not bridge-side: the bridge's authority
invariants forbid IT from importing the probe pipeline (it must stay
authority-free), so providers inject real runners via the Protocol. The subtle
part is that the ``PROBE_TRIGGERED`` payload is EMPTY (``probe_invocation_kw =
{}``) while :func:`execute_probe_environment` needs an ``AmbiguityContext`` +
resolver. This module closes that impedance gap by capturing the op's scalars
(op_id / target_file / claim / posture) at construction — where they ARE in
scope — and synthesizing the ``AmbiguityContext`` at probe time.

Discipline:
  * Gated by ``JARVIS_EPISTEMIC_RUNNERS_ENABLED`` (default FALSE). This runs on
    the live generation path and adds a bounded probe on confidence collapse,
    so it graduates from a soak, not from author confidence.
  * Fail-soft everywhere — a probe must NEVER raise into the generation path.
  * No new probe machinery: composes the EXISTING ``execute_probe_environment``
    (Move 5) + the shared ``get_default_prober()`` resolver singleton (the same
    resolver the SBT adapter already reuses).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_EPISTEMIC_RUNNERS_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def epistemic_runners_enabled() -> bool:
    """Master gate. Default FALSE — this injects an active probe onto the live
    generation path (bounded latency on confidence collapse), so it is
    graduated from soak evidence, not flipped on author confidence. Explicit
    truthy value opts in. NEVER raises."""
    try:
        return (os.environ.get(_ENV_MASTER, "") or "").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001 — fail-soft: disabled
        return False


class _ProbeRunnerAdapter:
    """Implements ``ProbeRunnerProtocol.run(*, payload)`` by running the real
    Move-5 confidence probe against an ``AmbiguityContext`` synthesized from the
    captured op scalars (the budget payload is empty by design).

    Returns the pipeline's ``ConfidenceCollapseVerdict`` — the tracker records it
    via ``note_probe_completed`` and the caller branches on ``verdict.action``
    (RETRY_WITH_FEEDBACK / ESCALATE_TO_OPERATOR / INCONCLUSIVE). NEVER raises."""

    __slots__ = ("_op_id", "_target_file", "_claim", "_posture", "_prior")

    def __init__(
        self,
        *,
        op_id: str,
        target_file: str = "",
        claim: str = "",
        posture: str = "",
        prior: float = 0.5,
    ) -> None:
        self._op_id = op_id
        self._target_file = target_file
        self._claim = claim
        self._posture = posture
        self._prior = prior

    async def run(self, *, payload: Any) -> Any:
        try:
            from backend.core.ouroboros.governance.verification.confidence_probe_generator import (  # noqa: E501
                AmbiguityContext,
            )
            from backend.core.ouroboros.governance.verification.probe_environment_executor import (  # noqa: E501
                execute_probe_environment,
            )
            from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (  # noqa: E501
                get_default_prober,
            )
        except Exception:  # noqa: BLE001 — infra unavailable → no verdict
            logger.debug("[EpistemicRunners] probe import failed", exc_info=True)
            return None

        try:
            ambiguity = AmbiguityContext(
                op_id=self._op_id,
                target_file=self._target_file,
                claim=self._claim,
                posture=self._posture,
            )
            verdict = await execute_probe_environment(
                monitor=None,  # reset_window is hasattr-guarded downstream
                ambiguity_context=ambiguity,
                op_id=self._op_id,
                prior=self._prior,
                resolver=get_default_prober(),
            )
            logger.info(
                "[EpistemicRunners] probe ran op=%s → %s",
                self._op_id,
                getattr(getattr(verdict, "action", None), "value", verdict),
            )
            return verdict
        except Exception:  # noqa: BLE001 — a probe must never raise into GENERATE
            logger.debug("[EpistemicRunners] probe run swallowed", exc_info=True)
            return None


def build_epistemic_runners(
    *,
    op_id: str,
    target_file: str = "",
    claim: str = "",
    posture: str = "",
) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Return ``(probe_runner, sbt_runner, orange_queue)`` for injection into
    ``attach_to_provider_run``. When the master flag is off, returns
    ``(None, None, None)`` — byte-identical to the pre-Task-4b behavior.

    Currently wires the PRIMARY runner — the confidence probe. ``sbt_runner`` and
    ``orange_queue`` remain ``None`` (the hook handles that path exactly as
    today, recording the skip); their construction requires additional Move-5
    type-mapping (a ``ConvergenceVerdict`` / ``BranchTreeTarget`` the empty
    budget payload does not carry) and is a scoped follow-up. Injecting the probe
    runner alone converts ``PROBE_TRIGGERED`` from a no-op observer into a real
    bounded probe — the audit's headline. NEVER raises."""
    try:
        if not op_id or not epistemic_runners_enabled():
            return (None, None, None)
        probe_runner = _ProbeRunnerAdapter(
            op_id=op_id,
            target_file=target_file or "",
            claim=claim or "",
            posture=posture or "",
        )
        return (probe_runner, None, None)
    except Exception:  # noqa: BLE001 — construction must never break dispatch
        logger.debug("[EpistemicRunners] build swallowed", exc_info=True)
        return (None, None, None)


__all__ = [
    "epistemic_runners_enabled",
    "build_epistemic_runners",
]
