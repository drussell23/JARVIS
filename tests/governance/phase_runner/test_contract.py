"""Contract tests for ``PhaseRunner`` / ``PhaseResult``.

Slice 1 of Wave 2 (5) — pin the shape of the abstraction before any
concrete extraction relies on it. These tests fail loudly if:

* The ABC machinery stops rejecting bare instantiation
* ``PhaseResult`` loses its frozen-dataclass guarantee
* The status ``Literal`` drifts
* The schema version is touched silently

Authority invariant: this test module imports nothing from
``candidate_generator`` / ``iron_gate`` / ``change_engine`` / ``gate``
/ ``policy`` / ``risk_tier`` — same discipline as the contract file.
"""
from __future__ import annotations

import dataclasses

import pytest

from backend.core.ouroboros.governance import phase_runner as _pr
from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.core.ouroboros.governance.phase_runner import (
    PHASE_RUNNER_SCHEMA_VERSION,
    PhaseResult,
    PhaseRunner,
    PhaseResultStatus,
)


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


def test_schema_version_is_pinned():
    """Bit-rot guard: bumping the version is deliberate, not accidental."""
    assert PHASE_RUNNER_SCHEMA_VERSION == "1.0"


# ---------------------------------------------------------------------------
# PhaseResult dataclass shape
# ---------------------------------------------------------------------------


def test_phase_result_is_frozen_dataclass():
    """Phase runners hand the dispatcher an immutable result object."""
    assert dataclasses.is_dataclass(PhaseResult)
    assert PhaseResult.__dataclass_params__.frozen is True  # type: ignore[attr-defined]


def test_phase_result_fields_are_stable():
    """The dispatcher will pattern-match on these names; pin them."""
    names = {f.name for f in dataclasses.fields(PhaseResult)}
    assert names == {"next_ctx", "next_phase", "status", "reason", "artifacts"}


def test_phase_result_default_artifacts_is_empty_mapping():
    """Every PhaseResult carries a JSON-serializable bag for §8 audit."""
    pr = PhaseResult(
        next_ctx=None,  # type: ignore[arg-type]
        next_phase=None,
        status="ok",
    )
    assert pr.artifacts == {}
    assert pr.reason is None


def test_phase_result_rejects_mutation():
    """Frozen means frozen — dispatcher can't patch status after the fact."""
    pr = PhaseResult(
        next_ctx=None,  # type: ignore[arg-type]
        next_phase=None,
        status="ok",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        pr.status = "fail"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PhaseResultStatus literal
# ---------------------------------------------------------------------------


def test_phase_result_status_literal_members():
    """Only these four status values should be possible."""
    # typing.get_args returns the Literal members
    from typing import get_args

    members = set(get_args(PhaseResultStatus))
    assert members == {"ok", "retry", "skip", "fail"}


# ---------------------------------------------------------------------------
# PhaseRunner ABC enforcement
# ---------------------------------------------------------------------------


def test_phase_runner_is_abstract():
    """Attempting to instantiate the ABC directly must raise."""
    with pytest.raises(TypeError):
        PhaseRunner()  # type: ignore[abstract]


def test_phase_runner_requires_run():
    """A subclass without ``run`` is still abstract."""

    class Missing(PhaseRunner):  # noqa: D401 — test helper
        phase = OperationPhase.COMPLETE

    with pytest.raises(TypeError):
        Missing()  # type: ignore[abstract]


def test_phase_runner_concrete_subclass_instantiates():
    """Subclass that sets ``phase`` + implements ``run`` works."""

    class Ok(PhaseRunner):
        phase = OperationPhase.COMPLETE

        async def run(self, ctx):  # noqa: D401
            return PhaseResult(
                next_ctx=ctx, next_phase=None, status="ok",
            )

    inst = Ok()
    assert inst.phase == OperationPhase.COMPLETE


# ---------------------------------------------------------------------------
# Authority invariant (grep-pinned from the docstring)
# ---------------------------------------------------------------------------


_BANNED_MODULES = (
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "gate",
    "policy",
    "risk_tier",
)


def test_phase_runner_module_has_no_forbidden_imports():
    """The contract file must not reach into execution-authority modules."""
    import inspect

    src = inspect.getsource(_pr)
    for banned in _BANNED_MODULES:
        # allow docstring mentions (the invariant is literally quoted there);
        # the ban is on actual ``import`` lines.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert banned not in stripped, (
                    f"phase_runner.py must not import {banned}; found: {stripped}"
                )


__all__ = []  # pytest collects by naming convention
