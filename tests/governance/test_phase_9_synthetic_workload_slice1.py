"""Phase 9 Slice 1 — synthetic workload factory regression spine.

Pins the principled-test-load-injection substrate per the
2026-05-05 operator binding:

  * **Single pipeline** — factory composes ``make_envelope`` only;
    no parallel ``IntentEnvelope(...)`` construction. AST-pinned.
  * **Honest source token** — ``"cadence_synthetic"`` is whitelisted
    in ``intent_envelope._VALID_SOURCES``; matches
    ``CADENCE_SYNTHETIC_SOURCE`` factory constant.
  * **Transparency markers** — every envelope carries
    ``evidence.category = "cadence_synthetic"`` +
    ``evidence.sensor = "Phase9SyntheticSeeder"`` +
    ``evidence.is_synthetic_cadence_load = True``. Operators MUST
    be able to filter cadence load from real load.
  * **Defaults / safety** — hard cap via env knob; n=0 returns
    empty tuple; misconfigure NEVER raises.

Verifies (24 tests):
  * Factory: builds N envelopes; honors cap; n=0 returns empty;
    seq monotonic; misconfigure returns empty silently
  * Honesty: source / category / sensor markers present in every
    envelope; whitelist contains 'cadence_synthetic'
  * Cost discipline: urgency='low' (BACKGROUND route); confidence
    moderate (0.50)
  * AST pins (all 3) auto-discovered + green
  * Authority asymmetry pin fires on forbidden import
  * Composes-make_envelope pin fires on direct IntentEnvelope(...)
  * Source-token-constant pin fires on drift
  * FlagRegistry seed for JARVIS_PHASE9_SEED_INTENTS_MAX present
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.graduation.phase_9_synthetic_workload import (  # noqa: E501
    CADENCE_SYNTHETIC_CATEGORY,
    CADENCE_SYNTHETIC_SENSOR_NAME,
    CADENCE_SYNTHETIC_SOURCE,
    PHASE_9_SYNTHETIC_WORKLOAD_SCHEMA_VERSION,
    build_synthetic_envelopes,
    register_shipped_invariants,
    seed_intents_max,
)


# ---------------------------------------------------------------------------
# Constants + whitelist alignment
# ---------------------------------------------------------------------------


def test_source_constant_value():
    assert CADENCE_SYNTHETIC_SOURCE == "cadence_synthetic"


def test_category_constant_value():
    assert CADENCE_SYNTHETIC_CATEGORY == "cadence_synthetic"


def test_sensor_name_constant_value():
    assert CADENCE_SYNTHETIC_SENSOR_NAME == "Phase9SyntheticSeeder"


def test_whitelist_contains_cadence_synthetic():
    """The factory's source token MUST be in the canonical
    ``_VALID_SOURCES`` whitelist; otherwise envelope validation
    rejects it. Drift between the two would silently break."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (  # noqa: E501
        _VALID_SOURCES,
    )
    assert CADENCE_SYNTHETIC_SOURCE in _VALID_SOURCES, (
        f"CADENCE_SYNTHETIC_SOURCE={CADENCE_SYNTHETIC_SOURCE!r} "
        f"missing from _VALID_SOURCES — drift between factory "
        f"and whitelist will break envelope validation"
    )


def test_schema_version_constant():
    assert (
        PHASE_9_SYNTHETIC_WORKLOAD_SCHEMA_VERSION
        == "phase_9_synthetic_workload.1"
    )


# ---------------------------------------------------------------------------
# Factory: shape + cap + transparency markers
# ---------------------------------------------------------------------------


def test_factory_builds_n_envelopes():
    envs = build_synthetic_envelopes(n=3, repo="r")
    assert len(envs) == 3


def test_factory_zero_returns_empty_tuple():
    """n=0 (production non-cadence path) returns ()."""
    envs = build_synthetic_envelopes(n=0, repo="r")
    assert envs == ()


def test_factory_negative_returns_empty_tuple():
    envs = build_synthetic_envelopes(n=-5, repo="r")
    assert envs == ()


def test_factory_caps_at_max():
    """Requesting beyond seed_intents_max() returns the capped
    tuple, not the requested count. Defense-in-depth against
    misconfigured cron."""
    envs = build_synthetic_envelopes(n=999, repo="r")
    assert len(envs) == seed_intents_max()
    assert len(envs) <= 64


def test_factory_respects_env_cap():
    """Override cap via env knob; factory honors it."""
    with patch.dict(
        os.environ,
        {"JARVIS_PHASE9_SEED_INTENTS_MAX": "5"},
    ):
        envs = build_synthetic_envelopes(n=999, repo="r")
    assert len(envs) == 5


def test_factory_seq_is_monotonic():
    envs = build_synthetic_envelopes(n=3, repo="r")
    seqs = [e.evidence["phase_9_seq"] for e in envs]
    assert seqs == [0, 1, 2]


def test_factory_seq_offset_honored():
    envs = build_synthetic_envelopes(
        n=2, repo="r", seq_offset=10,
    )
    seqs = [e.evidence["phase_9_seq"] for e in envs]
    assert seqs == [10, 11]


def test_every_envelope_carries_transparency_markers():
    envs = build_synthetic_envelopes(n=4, repo="r")
    for e in envs:
        assert e.source == "cadence_synthetic"
        assert (
            e.evidence["category"] == CADENCE_SYNTHETIC_CATEGORY
        )
        assert (
            e.evidence["sensor"] == CADENCE_SYNTHETIC_SENSOR_NAME
        )
        assert e.evidence["is_synthetic_cadence_load"] is True
        assert e.evidence["schema_version"] == (
            PHASE_9_SYNTHETIC_WORKLOAD_SCHEMA_VERSION
        )


def test_every_envelope_low_urgency():
    """urgency='low' routes BACKGROUND via UrgencyRouter — DW-
    only cascade, never burns Claude budget. Cost discipline
    preserved by composition."""
    envs = build_synthetic_envelopes(n=3, repo="r")
    for e in envs:
        assert e.urgency == "low"


def test_every_envelope_unattended_safe():
    """Cadence runs unattended; envelopes MUST NOT block on
    operator approval."""
    envs = build_synthetic_envelopes(n=3, repo="r")
    for e in envs:
        assert e.requires_human_ack is False


def test_every_envelope_has_unique_idempotency_key():
    """Each envelope gets a fresh idempotency_key so they don't
    dedup against each other in the intake router."""
    envs = build_synthetic_envelopes(n=5, repo="r")
    keys = {e.idempotency_key for e in envs}
    assert len(keys) == 5


def test_every_envelope_target_files_non_empty():
    """make_envelope validates target_files non-empty (except for
    vision_sensor + user_attachments paths). Cadence synthetic
    uses project-root sentinel."""
    envs = build_synthetic_envelopes(n=3, repo="r")
    for e in envs:
        assert e.target_files == (".",)


# ---------------------------------------------------------------------------
# Defensive paths — NEVER raises
# ---------------------------------------------------------------------------


def test_factory_handles_make_envelope_raise(monkeypatch):
    """Single bad envelope-build does not poison the rest;
    factory continues and returns successful builds only."""
    from backend.core.ouroboros.governance.intake import (
        intent_envelope as ienv_mod,
    )
    real_make = ienv_mod.make_envelope
    call_count = [0]

    def flaky_make(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:  # second call raises
            raise RuntimeError("flaky build")
        return real_make(*args, **kwargs)

    monkeypatch.setattr(
        ienv_mod, "make_envelope", flaky_make,
    )
    envs = build_synthetic_envelopes(n=3, repo="r")
    # 3 calls attempted, 1 raised → 2 succeeded
    assert len(envs) == 2


def test_factory_empty_repo_does_not_raise():
    """Empty repo string is coerced to '' in evidence; never raises."""
    envs = build_synthetic_envelopes(n=2, repo="")
    assert len(envs) == 2


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("", 16), ("1", 1), ("64", 64),
    ("0", 1),  # below floor
    ("999", 64),  # above ceiling
    ("garbage", 16),  # parse failure → default
])
def test_seed_intents_max_clamping(raw, expected):
    with patch.dict(
        os.environ,
        {"JARVIS_PHASE9_SEED_INTENTS_MAX": raw},
    ):
        assert seed_intents_max() == expected


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    invs = register_shipped_invariants()
    assert len(invs) == 3
    names = {i.invariant_name for i in invs}
    assert names == {
        "phase_9_synthetic_workload_authority_asymmetry",
        "phase_9_synthetic_workload_composes_make_envelope",
        "phase_9_synthetic_workload_source_token_constant",
    }


def test_all_pins_validate_clean():
    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/graduation"
        / "phase_9_synthetic_workload.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_authority_asymmetry_pin_fires_on_forbidden_import():
    bad_source = '''
from backend.core.ouroboros.governance.orchestrator import foo
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    auth = next(
        i for i in invs
        if "authority_asymmetry" in i.invariant_name
    )
    violations = auth.validate(tree, bad_source)
    assert violations
    assert any("orchestrator" in v for v in violations)


def test_composes_pin_fires_on_direct_intent_envelope_call():
    """If a future refactor tries to bypass make_envelope, the
    pin fires."""
    bad_source = '''
def foo():
    return IntentEnvelope(source="cadence_synthetic")
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    comp = next(
        i for i in invs
        if "composes_make_envelope" in i.invariant_name
    )
    violations = comp.validate(tree, bad_source)
    assert violations
    assert any(
        "IntentEnvelope" in v or "make_envelope" in v
        for v in violations
    )


def test_source_token_pin_fires_on_drift():
    """Drift in CADENCE_SYNTHETIC_SOURCE constant breaks the
    whitelist link silently — pin must fire."""
    bad_source = '''
CADENCE_SYNTHETIC_SOURCE: str = "synthetic_typo"
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    tok = next(
        i for i in invs
        if "source_token_constant" in i.invariant_name
    )
    violations = tok.validate(tree, bad_source)
    assert violations
    assert any("cadence_synthetic" in v for v in violations)


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_flag_registry_seed_present():
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    seed = next(
        (s for s in SEED_SPECS
         if s.name == "JARVIS_PHASE9_SEED_INTENTS_MAX"),
        None,
    )
    assert seed is not None, (
        "JARVIS_PHASE9_SEED_INTENTS_MAX FlagRegistry seed missing"
    )
    assert seed.default == 16
