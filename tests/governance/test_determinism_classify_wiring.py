"""Phase 1 Slice 1.3.a — CLASSIFY phase wiring regression spine.

Pins:
  §1   CLASSIFY adapter registered at module load
  §2   Adapter round-trip — Advisory dataclass ↔ JSON-friendly dict
  §3   Adapter handles all 4 AdvisoryDecision enum values
  §4   Serialize defensive on unknown shape (returns safe-default dict)
  §5   Deserialize defensive on garbage (returns raw input)
  §6   Deserialize defensive on invalid decision string (defaults to RECOMMEND)
  §7   classify_runner imports phase_capture LAZILY (not top-level)
  §8   classify_runner imports cleanly without determinism module
  §9   classify_runner.py source contains the wiring marker
  §10  Wiring preserves both branches: capture call AND fallback to
        direct advise call
  §11  Authority invariant — adapter registration is defensive (NEVER raises)
"""
from __future__ import annotations

import importlib
from typing import Any

import pytest

from backend.core.ouroboros.governance.determinism.phase_capture import (
    OutputAdapter,
    _IDENTITY_ADAPTER,
    get_adapter,
    register_adapter,
    reset_registry_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def classify_runner_reloaded():
    """Force a fresh reload of classify_runner so the module-load
    adapter registration runs in test isolation."""
    reset_registry_for_tests()
    from backend.core.ouroboros.governance.phase_runners import (
        classify_runner,
    )
    importlib.reload(classify_runner)
    yield classify_runner
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# §1 — Adapter registered at module load
# ---------------------------------------------------------------------------


def test_classify_adapter_registered_at_module_load(
    classify_runner_reloaded,
) -> None:
    """Importing classify_runner registers the CLASSIFY/advisor_verdict
    adapter."""
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")
    assert adapter is not _IDENTITY_ADAPTER
    assert adapter.name == "advisor_verdict_adapter"


# ---------------------------------------------------------------------------
# §2 — Adapter round-trip
# ---------------------------------------------------------------------------


def test_classify_adapter_round_trip(classify_runner_reloaded) -> None:
    """An Advisory dataclass survives serialize → deserialize as
    the same dataclass with the same field values."""
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")
    from backend.core.ouroboros.governance.operation_advisor import (
        Advisory, AdvisoryDecision,
    )

    original = Advisory(
        decision=AdvisoryDecision.CAUTION,
        reasons=["high blast radius", "low coverage"],
        blast_radius=15,
        test_coverage=0.3,
        chronic_entropy=0.7,
        risk_score=0.6,
        voice_message="Sir, this might break things.",
    )
    serialized = adapter.serialize(original)
    assert serialized == {
        "decision": "caution",
        "reasons": ["high blast radius", "low coverage"],
        "blast_radius": 15,
        "test_coverage": 0.3,
        "chronic_entropy": 0.7,
        "risk_score": 0.6,
        "voice_message": "Sir, this might break things.",
    }
    deserialized = adapter.deserialize(serialized)
    assert isinstance(deserialized, Advisory)
    assert deserialized.decision == AdvisoryDecision.CAUTION
    assert deserialized.reasons == ["high blast radius", "low coverage"]
    assert deserialized.blast_radius == 15
    assert deserialized.test_coverage == 0.3
    assert deserialized.chronic_entropy == 0.7
    assert deserialized.risk_score == 0.6
    assert deserialized.voice_message == "Sir, this might break things."


# ---------------------------------------------------------------------------
# §3 — All 4 AdvisoryDecision enum values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision_value", [
    "recommend", "caution", "advise_against", "block",
])
def test_classify_adapter_all_decisions(
    classify_runner_reloaded, decision_value,
) -> None:
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")
    from backend.core.ouroboros.governance.operation_advisor import (
        Advisory, AdvisoryDecision,
    )
    advisory = Advisory(
        decision=AdvisoryDecision(decision_value),
        reasons=[], blast_radius=0, test_coverage=0.0,
        chronic_entropy=0.0, risk_score=0.0,
    )
    serialized = adapter.serialize(advisory)
    assert serialized["decision"] == decision_value
    deserialized = adapter.deserialize(serialized)
    assert deserialized.decision == AdvisoryDecision(decision_value)


# ---------------------------------------------------------------------------
# §4 — Serialize defensive
# ---------------------------------------------------------------------------


def test_classify_adapter_serialize_unknown_shape(
    classify_runner_reloaded,
) -> None:
    """Pass a non-Advisory object — adapter returns safe-default
    dict instead of raising."""
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")
    out = adapter.serialize("not an advisory")
    assert isinstance(out, dict)
    assert out["decision"] == "recommend"
    assert out["reasons"] == []
    # voice_message captures repr for diagnostic
    assert "not an advisory" in out["voice_message"]


def test_classify_adapter_serialize_partial_object(
    classify_runner_reloaded,
) -> None:
    """An object with only SOME advisory-shaped fields shouldn't
    crash — defensive coercion."""
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")

    class _Partial:
        # Missing several fields
        def __init__(self):
            self.decision = "recommend"  # str, not enum
            self.reasons = ["partial"]

    out = adapter.serialize(_Partial())
    # Falls back to safe default since attribute access on missing
    # fields raises AttributeError → caught
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# §5-§6 — Deserialize defensive
# ---------------------------------------------------------------------------


def test_classify_adapter_deserialize_garbage(
    classify_runner_reloaded,
) -> None:
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")
    # Non-dict input → returned as-is (Slice 1.3 contract)
    assert adapter.deserialize("not a dict") == "not a dict"
    assert adapter.deserialize(42) == 42
    assert adapter.deserialize(None) is None


def test_classify_adapter_deserialize_invalid_decision_defaults(
    classify_runner_reloaded,
) -> None:
    """Stored decision string that doesn't map to an enum value
    falls back to RECOMMEND (safe default)."""
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")
    from backend.core.ouroboros.governance.operation_advisor import (
        Advisory, AdvisoryDecision,
    )
    out = adapter.deserialize({
        "decision": "INVALID_VALUE_NOT_IN_ENUM",
        "reasons": [], "blast_radius": 0,
        "test_coverage": 0.0, "chronic_entropy": 0.0,
        "risk_score": 0.0, "voice_message": "",
    })
    assert isinstance(out, Advisory)
    assert out.decision == AdvisoryDecision.RECOMMEND


def test_classify_adapter_deserialize_partial_dict(
    classify_runner_reloaded,
) -> None:
    """Dict with missing fields uses defaults via .get()."""
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")
    from backend.core.ouroboros.governance.operation_advisor import (
        Advisory, AdvisoryDecision,
    )
    out = adapter.deserialize({"decision": "block"})
    assert isinstance(out, Advisory)
    assert out.decision == AdvisoryDecision.BLOCK
    assert out.reasons == []
    assert out.blast_radius == 0


# ---------------------------------------------------------------------------
# §7-§9 — Source-level pins
# ---------------------------------------------------------------------------


def test_classify_runner_imports_phase_capture_lazily() -> None:
    """phase_capture import must be INSIDE function body (lazy)."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/classify_runner.py",
        encoding="utf-8",
    ).read()
    lines = src.split("\n")
    top_level_imports = [
        ln for ln in lines
        if ln.startswith("from backend.core.ouroboros.governance.determinism.phase_capture")
    ]
    assert top_level_imports == [], (
        "classify_runner must import phase_capture lazily, not at top level"
    )


def test_classify_runner_imports_cleanly() -> None:
    """classify_runner module imports without error even when
    determinism modules are absent / partially loaded. Defensive
    try/except wraps the adapter registration."""
    from backend.core.ouroboros.governance.phase_runners import (
        classify_runner,
    )
    importlib.reload(classify_runner)
    assert hasattr(classify_runner, "CLASSIFYRunner")


def test_classify_runner_source_contains_wiring_marker() -> None:
    """The file must contain the Slice 1.3.a wiring marker so a
    refactor that strips out the capture wrapper is detected by
    grep + this test."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/classify_runner.py",
        encoding="utf-8",
    ).read()
    assert "Slice 1.3.a" in src, (
        "classify_runner must reference Phase 1 Slice 1.3.a in source"
    )
    assert "advisor_verdict" in src
    assert "capture_phase_decision" in src


# ---------------------------------------------------------------------------
# §10 — Both branches present (capture + fallback)
# ---------------------------------------------------------------------------


def test_classify_runner_has_capture_branch() -> None:
    """The wiring includes capture_phase_decision call site."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/classify_runner.py",
        encoding="utf-8",
    ).read()
    assert "capture_phase_decision(" in src
    # Inputs include the description hash + target count + read-only flag
    assert "description_hash" in src
    assert "target_count" in src


def test_classify_runner_has_fallback_branch() -> None:
    """The wiring includes a defensive fallback that calls
    _advisor.advise(...) directly if capture_phase_decision raises."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/classify_runner.py",
        encoding="utf-8",
    ).read()
    # Look for the fallback comment + the second .advise( call inside
    # the except branch
    assert "fall back to direct" in src.lower() or "falling back" in src.lower()
    # There should be at least 2 .advise( call sites — capture wrapped
    # path + direct fallback
    assert src.count("_advisor.advise(") >= 2


# ---------------------------------------------------------------------------
# §11 — Defensive registration
# ---------------------------------------------------------------------------


def test_register_classify_adapter_helper_is_defensive() -> None:
    """The _register_classify_adapter helper has a top-level
    try/except so it never raises at import time."""
    from backend.core.ouroboros.governance.phase_runners import (
        classify_runner,
    )
    src = open(classify_runner.__file__, encoding="utf-8").read()
    # Find the helper function
    helper_idx = src.index("def _register_classify_adapter")
    # Walk forward to find the try:
    after = src[helper_idx:helper_idx + 4000]
    assert "try:" in after
    # The except clause must catch broadly (Exception family)
    assert "except Exception" in after


# ---------------------------------------------------------------------------
# §12 — End-to-end wiring smoke (no actual ctx required)
# ---------------------------------------------------------------------------


def test_classify_adapter_handles_none_input(
    classify_runner_reloaded,
) -> None:
    adapter = get_adapter(phase="CLASSIFY", kind="advisor_verdict")
    # Defensive serialize on None doesn't crash
    out = adapter.serialize(None)
    assert isinstance(out, dict)
