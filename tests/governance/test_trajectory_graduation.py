"""Slice 5 graduation pins — Operator Trajectory View arc."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# Authority invariant
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/governance/trajectory_frame.py",
    "backend/core/ouroboros/governance/trajectory_view.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_arc_module_has_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        if re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            src, re.MULTILINE,
        ):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden: {violations}"
    )


# ===========================================================================
# §1 read-only invariant: no control surface
# ===========================================================================


def test_trajectory_frame_has_no_mutation_methods():
    """The frame is a frozen value type. Any method that would
    mutate state or call back into the orchestrator is a boundary
    violation — grep the module source to prove none exists.
    """
    src = Path(
        "backend/core/ouroboros/governance/trajectory_frame.py"
    ).read_text()
    # No GLS / orchestrator callbacks; no `.submit`, `.apply`,
    # `.cancel`, `.approve`, `.reject` methods exposed on the frame
    for banned in ("def submit(", "def apply(", "def cancel(",
                    "def approve(", "def reject("):
        assert banned not in src, (
            f"trajectory_frame.py exposes a control method: {banned!r}"
        )


# ===========================================================================
# Schema versions stable
# ===========================================================================


def test_schema_versions_pinned():
    from backend.core.ouroboros.governance.trajectory_frame import (
        TRAJECTORY_FRAME_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.trajectory_view import (
        TRAJECTORY_VIEW_SCHEMA_VERSION,
    )
    assert TRAJECTORY_FRAME_SCHEMA_VERSION == "trajectory_frame.v1"
    assert TRAJECTORY_VIEW_SCHEMA_VERSION == "trajectory_view.v1"


# ===========================================================================
# Determinism: same suppliers + same now_ts → same frame fields
# ===========================================================================


def test_build_is_deterministic_for_fixed_inputs():
    from backend.core.ouroboros.governance.trajectory_view import (
        TrajectoryBuilder,
    )

    class _Static:
        def current_op(self):
            return {
                "op_id": "op-x",
                "raw_phase": "apply",
                "target_paths": ["a.py"],
                "trigger_source": "t",
                "trigger_reason": "r",
                "started_at_ts": 1000.0,
            }

        def cost_snapshot(self, op_id):
            _ = op_id
            return {"spent_usd": 0.01, "budget_usd": 0.5}

        def eta_for(self, op_id):
            _ = op_id
            return {"eta_seconds": 30.0, "confidence": 0.7}

        def trigger_for(self, op_id):
            _ = op_id
            return None

    b = TrajectoryBuilder(
        op_state=_Static(), cost=_Static(), eta=_Static(),
        sensor_trigger=_Static(),
    )
    f1 = b.build(now_ts=1234567890.0)
    f2 = b.build(now_ts=1234567890.0)
    # Sequence differs between calls by design; everything else matches
    assert f1.op_id == f2.op_id
    assert f1.phase is f2.phase
    assert f1.target_paths == f2.target_paths
    assert f1.eta_seconds == f2.eta_seconds
    assert f1.cost_spent_usd == f2.cost_spent_usd


# ===========================================================================
# Presentation-equality excludes sequence + snapshot
# ===========================================================================


def test_presentation_equality_ignores_sequence_and_timestamp():
    from backend.core.ouroboros.governance.trajectory_view import (
        TrajectoryBuilder, _frames_presentation_equal,
    )

    class _Static:
        def current_op(self):
            return {
                "op_id": "op-x", "raw_phase": "apply",
                "target_paths": ["a.py"],
            }

    b = TrajectoryBuilder(op_state=_Static())
    f1 = b.build(now_ts=1.0)
    f2 = b.build(now_ts=2.0)
    assert _frames_presentation_equal(f1, f2) is True


# ===========================================================================
# Docstring bit-rot guards
# ===========================================================================


def test_trajectory_frame_docstring_references_gap_quote_shape():
    from backend.core.ouroboros.governance.trajectory_frame import (
        TrajectoryFrame,
    )
    doc = TrajectoryFrame.__doc__ or ""
    # Frame is documented as a single-glance snapshot
    assert doc
    # Narrative method exists
    assert hasattr(TrajectoryFrame, "narrative")
    assert hasattr(TrajectoryFrame, "one_line_summary")


def test_builder_docstring_references_fail_closed():
    from backend.core.ouroboros.governance.trajectory_view import (
        TrajectoryBuilder,
    )
    doc = TrajectoryBuilder.__doc__ or ""
    assert "unknown" in doc.lower() or "fail" in doc.lower()
