"""The admission took the brain with it and left nineteen references behind.

``_admit_routing`` was extracted from ``submit()`` in 89a9166e05 so both entry
points would share ONE routing admission. The extraction took
``brain = await self._brain_selector.select(...)`` with it and returned only the
admitted context — but ``submit()``'s body still referenced that local nineteen
times, and TWO of those sit on the unconditional terminal path: the ledger row's
``routing_reason`` and the ``brain_id``/``model_name`` of the terminal events.

So an op that ran the entire pipeline successfully raised
``NameError: name 'brain' is not defined`` at the moment it tried to record what
it had done. The repository's own undefined-name ratchet held
``backend/core/ouroboros/governance`` at zero and reported it as
``19 undefined name(s)`` — the gate was right and the code had drifted.

The fix reads the decision back off the stamp ``_admit_routing`` writes, rather
than returning a second copy of a fact that already has an owner.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import governed_loop_service as GLS
from backend.core.ouroboros.governance.op_context import (
    RoutingIntentTelemetry,
    TelemetryContext,
)

_MODULE = Path(GLS.__file__)


# ---------------------------------------------------------------------------
# The regression itself, guarded by the repository's own gate
# ---------------------------------------------------------------------------

def test_the_module_has_no_undefined_names():
    """Composed from ``ci.lint_gate``, the same analysis CI runs, rather than
    a second implementation of it here — two copies of one rule is how two
    gates come to disagree."""
    from ci.lint_gate import undefined_names

    findings = [f for f in undefined_names(_MODULE) if not f.inert]
    assert not findings, "\n".join(str(f) for f in findings)


def test_submit_binds_brain_before_it_reads_it():
    """The specific shape of the defect: a name loaded in ``submit`` that
    nothing in ``submit`` ever stored."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "submit"
    )
    stores = {
        s.id for s in ast.walk(fn)
        if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Store)
    }
    loads = {
        s.id for s in ast.walk(fn)
        if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load)
    }
    assert "brain" in loads, "the test is guarding a use that no longer exists"
    assert "brain" in stores


def test_the_terminal_path_is_the_one_that_was_broken():
    """Not a hypothetical branch. Both the ledger row and the terminal events
    read the brain on the path every completed op takes."""
    src = inspect.getsource(GLS.GovernedLoopService.submit)
    assert "routing_reason=brain.routing_reason" in src
    assert "brain_id=brain.brain_id" in src


# ---------------------------------------------------------------------------
# The accessor
# ---------------------------------------------------------------------------

def _stamped(**kw):
    """A context object shaped exactly as ``_admit_routing`` leaves one."""
    class _Ctx:
        telemetry = TelemetryContext(
            local_node=None, routing_intent=RoutingIntentTelemetry(**kw),
        )
    return _Ctx()


def test_the_decision_round_trips_off_the_stamp():
    ctx = _stamped(
        expected_provider="GCP_PRIME_SPOT",
        policy_reason="task_gate_heavy",
        brain_id="qwen_coder_32b",
        brain_model="qwen3-coder-ov:30b",
        routing_reason="task_gate_heavy_code",
    )
    decision = GLS._routing_decision_from_context(ctx)
    assert decision.brain_id == "qwen_coder_32b"
    assert decision.model_name == "qwen3-coder-ov:30b"
    assert decision.routing_reason == "task_gate_heavy_code"


def test_the_field_name_translation_is_pinned_to_both_real_dataclasses():
    """The telemetry record calls it ``brain_model``; the call sites mean
    ``model_name``. That translation lives in one accessor, and a rename on
    EITHER side must fail here rather than silently produce empty strings."""
    from backend.core.ouroboros.governance.brain_selector import (
        BrainSelectionResult,
    )

    assert "brain_model" in RoutingIntentTelemetry.__dataclass_fields__
    assert "brain_id" in RoutingIntentTelemetry.__dataclass_fields__
    assert "routing_reason" in RoutingIntentTelemetry.__dataclass_fields__
    # The names the nineteen call sites use, which the view must expose.
    for field in ("brain_id", "model_name", "routing_reason"):
        assert field in BrainSelectionResult.__dataclass_fields__
        assert field in GLS._RoutingDecision.__dataclass_fields__


def test_the_admission_writes_exactly_what_the_accessor_reads():
    """Drift guard across the seam: every field the accessor reads must be one
    ``_admit_routing`` actually stamps."""
    src = inspect.getsource(GLS.GovernedLoopService._admit_routing)
    for written in ("brain_id=brain.brain_id", "brain_model=brain.model_name",
                    "routing_reason=brain.routing_reason"):
        assert written in src


# ---------------------------------------------------------------------------
# Resilience — an unattributed row beats losing a finished op
# ---------------------------------------------------------------------------

def test_a_context_with_no_telemetry_yields_an_empty_decision():
    class _Bare:
        telemetry = None

    decision = GLS._routing_decision_from_context(_Bare())
    assert decision.brain_id == "" and decision.model_name == ""
    assert decision.routing_reason == ""


def test_a_partially_populated_stamp_degrades_field_by_field():
    ctx = _stamped(expected_provider="x", policy_reason="y", brain_id="only_id")
    decision = GLS._routing_decision_from_context(ctx)
    assert decision.brain_id == "only_id"
    assert decision.model_name == ""


@pytest.mark.parametrize("junk", [None, object(), 42, "ctx", [], {}])
def test_the_accessor_never_raises(junk):
    """It runs on the terminal path of every op. Raising there is the exact
    failure it was written to end."""
    assert isinstance(
        GLS._routing_decision_from_context(junk), GLS._RoutingDecision,
    )


def test_the_empty_decision_is_a_single_shared_value():
    """"Unknown routing" is one fact, not one object per call — and it is
    immutable, so no caller can turn a degraded read into a corrupt one."""
    a = GLS._routing_decision_from_context(None)
    b = GLS._routing_decision_from_context(None)
    assert a is b
    with pytest.raises(Exception):
        a.brain_id = "mutated"  # type: ignore[misc]


def test_the_view_is_read_off_the_context_not_reselected():
    """Selecting twice would spend the brain gate twice and could answer
    differently; it also could not cross into `submit_background`'s pool
    worker, which receives only the context."""
    fn = next(
        n for n in ast.walk(ast.parse(_MODULE.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef)
        and n.name == "_routing_decision_from_context"
    )
    # The BODY, not the source: the docstring explains the selection it does
    # not perform, and a substring check over the whole function matched that
    # explanation — the test's own first version failed on its own prose.
    body = "\n".join(
        ast.unparse(stmt) for stmt in fn.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    )
    assert "_brain_selector" not in body
    assert ".select(" not in body
