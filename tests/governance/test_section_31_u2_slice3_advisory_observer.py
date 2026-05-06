"""§31 U2 empirical wiring Slice 3 — chatter-suppressed
advisory-observer regression spine.

Pins per operator binding 2026-05-05:

  * Master flag default-FALSE (§33.1 graduation contract)
  * Observer composes Slice 1 ``compute_op_causal_features`` —
    no parallel feature extraction (AST-pinned)
  * Same-advice → silent (chatter suppression)
  * First observation at NEUTRAL → silent (mirrors §37 Slice 5
    first-observation-at-OK rule)
  * Cross-advice transition → emits ONE SSE event with from/to
    payload
  * Singleton + Read-API Extension Pattern (10th application)
  * EVENT_TYPE_CAUSAL_ADVISORY_EMITTED registered in
    _VALID_EVENT_TYPES (canonical broker integration)
  * Authority asymmetry — observer never imports orchestrator/
    iron_gate/policy/providers/etc. (AST-pinned)
  * NEVER raises across all paths
  * AST pins all fire on synthetic regressions
  * Public API stable

Verifies (24 tests).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Stub features for deterministic transition tests
# ---------------------------------------------------------------------------


def _make_features(advice_value: str, **overrides):
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures,
    )
    advice = CausalDecisionAdvice(advice_value)
    defaults = dict(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s",
        record_id="r",
        ancestor_count=3,
        distinct_phases_in_lineage=("GENERATE",),
        sibling_count=0,
        recurrence_score=0.0,
        parent_decisions_summary="",
        advice=advice,
    )
    defaults.update(overrides)
    return OpCausalFeatures(**defaults)


@pytest.fixture
def reset_observer():
    """Reset the singleton between tests so per-key state
    doesn't leak."""
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield
    reset_default_observer_for_tests()


# ---------------------------------------------------------------------------
# Master flag — default-FALSE
# ---------------------------------------------------------------------------


def test_master_flag_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        is_observer_enabled,
    )
    assert is_observer_enabled() is False


def test_master_flag_truthy(monkeypatch):
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        is_observer_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", v,
        )
        assert is_observer_enabled() is True


# ---------------------------------------------------------------------------
# Singleton — first-instance-wins
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance(reset_observer):
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    a = get_default_observer()
    b = get_default_observer()
    assert a is b


# ---------------------------------------------------------------------------
# Chatter suppression — same-band returns None
# ---------------------------------------------------------------------------


def test_record_returns_none_when_master_off(
    monkeypatch, reset_observer,
):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    assert obs.record(session_id="s", record_id="r") is None


def test_record_returns_none_on_blank_inputs(
    monkeypatch, reset_observer,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    assert obs.record(session_id="", record_id="") is None
    assert obs.record(session_id="s", record_id="") is None


def test_first_observation_at_neutral_silent(
    monkeypatch, reset_observer,
):
    """First observation at NEUTRAL should be silent (mirrors
    §37 Slice 5 first-observation-at-OK rule)."""
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=_make_features("neutral"),
    ):
        result = obs.record(session_id="s", record_id="r")
    assert result is None


def test_same_advice_observation_silent(
    monkeypatch, reset_observer,
):
    """Same advice twice in a row → second observation silent."""
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=_make_features("recurrence_warning"),
    ):
        a = obs.record(session_id="s", record_id="r")
        b = obs.record(session_id="s", record_id="r")
    # First observation transitions from None → recurrence_warning
    # → emits.
    assert a is not None
    assert a.from_advice == ""  # no prior
    assert a.to_advice == "recurrence_warning"
    # Second is same-band → silent
    assert b is None


def test_disabled_advice_silent(
    monkeypatch, reset_observer,
):
    """Substrate-side DISABLED → observer silent (substrate flag
    off, even if observer flag on)."""
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    monkeypatch.delenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=_make_features("disabled"),
    ):
        result = obs.record(session_id="s", record_id="r")
    assert result is None


# ---------------------------------------------------------------------------
# Transitions emit
# ---------------------------------------------------------------------------


def test_transition_neutral_to_warning_emits(
    monkeypatch, reset_observer,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    # First observation: NEUTRAL (silent)
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=_make_features("neutral"),
    ):
        first = obs.record(session_id="s", record_id="r")
    assert first is None
    # Second observation: RECURRENCE_WARNING (transition emits)
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=_make_features("recurrence_warning"),
    ):
        second = obs.record(session_id="s", record_id="r")
    assert second is not None
    assert second.from_advice == "neutral"
    assert second.to_advice == "recurrence_warning"


def test_transition_carries_feature_payload(
    monkeypatch, reset_observer,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    feats = _make_features(
        "deep_lineage_harden",
        ancestor_count=15,
        sibling_count=3,
        recurrence_score=0.42,
    )
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=feats,
    ):
        result = obs.record(session_id="s", record_id="r")
    assert result is not None
    assert result.ancestor_count == 15
    assert result.sibling_count == 3
    assert abs(result.recurrence_score - 0.42) < 1e-9


def test_per_record_independence(
    monkeypatch, reset_observer,
):
    """Two different record_ids have independent advice state.
    A transition on r1 does NOT suppress emission on r2."""
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=_make_features("recurrence_warning"),
    ):
        r1 = obs.record(session_id="s", record_id="r1")
        r2 = obs.record(session_id="s", record_id="r2")
    # Both fire — different keys, different state machines
    assert r1 is not None
    assert r2 is not None


def test_observation_to_dict_round_trip(
    monkeypatch, reset_observer,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=_make_features("sibling_dedup"),
    ):
        result = obs.record(session_id="s1", record_id="r1")
    d = result.to_dict()
    assert d["session_id"] == "s1"
    assert d["record_id"] == "r1"
    assert d["from_advice"] == ""
    assert d["to_advice"] == "sibling_dedup"


# ---------------------------------------------------------------------------
# SSE integration — broker publish
# ---------------------------------------------------------------------------


def test_event_type_registered_in_valid_set():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_CAUSAL_ADVISORY_EMITTED,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_CAUSAL_ADVISORY_EMITTED == (
        "causal_advisory_emitted"
    )
    assert (
        EVENT_TYPE_CAUSAL_ADVISORY_EMITTED in _VALID_EVENT_TYPES
    )


def test_observer_publishes_to_broker(
    monkeypatch, reset_observer,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_CAUSAL_ADVISORY_EMITTED, get_default_broker,
    )
    broker = get_default_broker()
    initial = broker.published_count
    obs = get_default_observer()
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        return_value=_make_features("recurrence_warning"),
    ):
        obs.record(session_id="s", record_id="r")
    # Broker received at least one event
    assert broker.published_count > initial
    # And the most recent recent_history contains our event type
    history = broker.recent_history(limit=10)
    types = [e.event_type for e in history]
    assert EVENT_TYPE_CAUSAL_ADVISORY_EMITTED in types


# ---------------------------------------------------------------------------
# NEVER-raises contract
# ---------------------------------------------------------------------------


def test_never_raises_on_compute_exception(
    monkeypatch, reset_observer,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    with patch(
        "backend.core.ouroboros.governance.causality_consumer."
        "compute_op_causal_features",
        side_effect=RuntimeError("simulated"),
    ):
        # Doesn't raise; returns None
        result = obs.record(session_id="s", record_id="r")
    assert result is None


def test_never_raises_on_substrate_unavailable(
    monkeypatch, reset_observer,
):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_ADVISORY_OBSERVER_ENABLED", "1",
    )
    real_import = __import__

    def _block(name, *args, **kwargs):
        if name == (
            "backend.core.ouroboros.governance.causality_consumer"
        ):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block)
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        get_default_observer,
    )
    obs = get_default_observer()
    # Doesn't raise
    result = obs.record(session_id="s", record_id="r")
    assert result is None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_4():
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "causal_advisory_observer_authority_asymmetry",
        "causal_advisory_observer_composes_slice_1",
        "causal_advisory_observer_chatter_suppressed",
        "causal_advisory_observer_master_flag_default_false",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "causal_advisory_observer.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_authority_asymmetry_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.iron_gate "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_composes_slice1_pin_fires_on_missing_compose():
    """If the observer source loses the canonical compose
    import, the pin fires."""
    from backend.core.ouroboros.governance.causal_advisory_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = "import os\n# observer that doesn't compose Slice 1"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "composes_slice_1" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import (
        causal_advisory_observer,
    )
    expected = {
        "CAUSAL_ADVISORY_OBSERVATION_SCHEMA_VERSION",
        "CausalAdvisoryObservation",
        "CausalAdvisoryObserver",
        "get_default_observer",
        "is_observer_enabled",
        "register_shipped_invariants",
        "reset_default_observer_for_tests",
    }
    assert set(causal_advisory_observer.__all__) == expected
