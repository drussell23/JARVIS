"""
PlanStore — immutable plan persistence keyed by plan_hash.
==========================================================

Plans are serialised to ``{store_dir}/{plan_hash}.json``.  Once written a
file is never overwritten (immutable-append semantics), so the same
``plan_hash`` will always refer to the same logical plan content.

Usage::

    store = PlanStore()                      # default ~/.jarvis/ouroboros/plans
    store.store(plan)                        # no-op if already stored
    plan = store.load("abc123...")           # None if missing or corrupt
    if store.exists("abc123..."):
        ...
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from backend.core.ouroboros.architect.plan import (
    AcceptanceCheck,
    ArchitecturalPlan,
    CheckKind,
    PlanStep,
    StepIntentKind,
)

_log = logging.getLogger(__name__)

_DEFAULT_STORE_DIR = Path.home() / ".jarvis" / "ouroboros" / "plans"


class PlanStore:
    """Immutable plan store keyed by ``plan_hash``.

    Parameters
    ----------
    store_dir:
        Directory where ``{plan_hash}.json`` files are written.
        Defaults to ``~/.jarvis/ouroboros/plans``.
    """

    def __init__(self, store_dir: Optional[Path] = None) -> None:
        self._store_dir: Path = store_dir if store_dir is not None else _DEFAULT_STORE_DIR
        self._store_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store(self, plan: ArchitecturalPlan) -> None:
        """Persist *plan* to disk as ``{plan_hash}.json``.

        If a file for this ``plan_hash`` already exists the call is a silent
        no-op, preserving the immutability guarantee.
        """
        target = self._path_for(plan.plan_hash)
        if target.exists():
            return  # immutable — never overwrite
        payload = _serialize(plan)
        # Write atomically via a temp file in the same directory so that a
        # partial write does not leave a corrupt JSON file behind.
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def load(self, plan_hash: str) -> Optional[ArchitecturalPlan]:
        """Return the :class:`ArchitecturalPlan` stored under *plan_hash*.

        Returns ``None`` if the file does not exist or if deserialisation
        fails (e.g. corrupt JSON).
        """
        target = self._path_for(plan_hash)
        if not target.exists():
            return None
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            return _deserialize(raw)
        except Exception as exc:
            _log.warning("PlanStore: failed to load %s — %s", plan_hash, exc)
            return None

    def exists(self, plan_hash: str) -> bool:
        """Return ``True`` iff a plan with *plan_hash* has been stored."""
        return self._path_for(plan_hash).exists()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path_for(self, plan_hash: str) -> Path:
        return self._store_dir / f"{plan_hash}.json"


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _serialize(plan: ArchitecturalPlan) -> Dict[str, Any]:
    """Convert *plan* to a JSON-serialisable dict.

    * Enum values  → their ``.value`` string
    * Tuples       → lists
    * FrozenSet    → sorted list
    * Optional[int] run_after_step → preserved as ``null`` or int
    """
    return {
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "parent_hypothesis_id": plan.parent_hypothesis_id,
        "parent_hypothesis_fingerprint": plan.parent_hypothesis_fingerprint,
        "title": plan.title,
        "description": plan.description,
        "repos_affected": list(plan.repos_affected),
        "non_goals": list(plan.non_goals),
        "steps": [_serialize_step(s) for s in plan.steps],
        "file_allowlist": sorted(plan.file_allowlist),
        "acceptance_checks": [_serialize_check(c) for c in plan.acceptance_checks],
        "model_used": plan.model_used,
        "created_at": plan.created_at,
        "snapshot_hash": plan.snapshot_hash,
    }


def _serialize_step(step: PlanStep) -> Dict[str, Any]:
    return {
        "step_index": step.step_index,
        "description": step.description,
        "intent_kind": step.intent_kind.value,
        "target_paths": list(step.target_paths),
        "repo": step.repo,
        "ancillary_paths": list(step.ancillary_paths),
        "interface_contracts": list(step.interface_contracts),
        "tests_required": list(step.tests_required),
        "risk_tier_hint": step.risk_tier_hint,
        "depends_on": list(step.depends_on),
    }


def _serialize_check(check: AcceptanceCheck) -> Dict[str, Any]:
    return {
        "check_id": check.check_id,
        "check_kind": check.check_kind.value,
        "command": check.command,
        "expected": check.expected,
        "cwd": check.cwd,
        "timeout_s": check.timeout_s,
        "run_after_step": check.run_after_step,
        "sandbox_required": check.sandbox_required,
    }


# ---------------------------------------------------------------------------
# Deserialisation helpers
# ---------------------------------------------------------------------------


def _deserialize(raw: Dict[str, Any]) -> ArchitecturalPlan:
    """Reconstruct an :class:`ArchitecturalPlan` from a plain dict.

    Performs explicit type reconstruction so that all enums, tuples, and
    frozensets are restored to their proper Python types.
    """
    steps = tuple(_deserialize_step(s) for s in raw["steps"])
    checks = tuple(_deserialize_check(c) for c in raw["acceptance_checks"])

    # file_allowlist is stored as a sorted list; restore as frozenset
    file_allowlist: frozenset[str] = frozenset(raw["file_allowlist"])

    return ArchitecturalPlan(
        plan_id=raw["plan_id"],
        plan_hash=raw["plan_hash"],
        parent_hypothesis_id=raw["parent_hypothesis_id"],
        parent_hypothesis_fingerprint=raw["parent_hypothesis_fingerprint"],
        title=raw["title"],
        description=raw["description"],
        repos_affected=tuple(raw["repos_affected"]),
        non_goals=tuple(raw["non_goals"]),
        steps=steps,
        file_allowlist=file_allowlist,
        acceptance_checks=checks,
        model_used=raw["model_used"],
        created_at=float(raw["created_at"]),
        snapshot_hash=raw["snapshot_hash"],
    )


def _deserialize_step(raw: Dict[str, Any]) -> PlanStep:
    return PlanStep(
        step_index=int(raw["step_index"]),
        description=raw["description"],
        intent_kind=StepIntentKind(raw["intent_kind"]),
        target_paths=tuple(raw["target_paths"]),
        repo=raw["repo"],
        ancillary_paths=tuple(raw.get("ancillary_paths", [])),
        interface_contracts=tuple(raw.get("interface_contracts", [])),
        tests_required=tuple(raw.get("tests_required", [])),
        risk_tier_hint=raw.get("risk_tier_hint", "safe_auto"),
        depends_on=tuple(int(i) for i in raw.get("depends_on", [])),
    )


def _deserialize_check(raw: Dict[str, Any]) -> AcceptanceCheck:
    run_after: Optional[int] = raw.get("run_after_step")
    if run_after is not None:
        run_after = int(run_after)
    return AcceptanceCheck(
        check_id=raw["check_id"],
        check_kind=CheckKind(raw["check_kind"]),
        command=raw["command"],
        expected=raw.get("expected", ""),
        cwd=raw.get("cwd", "."),
        timeout_s=float(raw.get("timeout_s", 120.0)),
        run_after_step=run_after,
        sandbox_required=bool(raw.get("sandbox_required", True)),
    )
