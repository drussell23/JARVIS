"""Slice 23 — Autonomous Registry-Driven Sentinel Activation Engine.

Closes the structural bottleneck surfaced by v16/v17: locking dispatch
to a single DW model when an entire trusted-seed fleet sits in the
PromotionLedger. Pre-Slice-23 the sentinel walker required
``JARVIS_TOPOLOGY_SENTINEL_ENABLED=true`` to fire — a per-soak env
flag was the only way to engage the multi-model fleet. Slice 23 makes
activation a structural decision the dispatcher makes at every call
from the active registry state.

# Closed decision matrix (first-match-wins)

1. ``JARVIS_TOPOLOGY_SENTINEL_ENABLED=true``  → ACTIVATE (legacy contract)
2. ``JARVIS_TOPOLOGY_SENTINEL_ENABLED=false`` → DO NOT (operator rollback)
3. ``JARVIS_PROVIDER_CLAUDE_DISABLED=true``   → ACTIVATE (Slice 19a posture)
4. PromotionLedger has ≥2 promoted models for route → ACTIVATE (autonomous)
5. Default → DO NOT (Phase 10 contract preserved)

# Phase 10 graduation contract preservation

``phase10_graduation_contract.py`` AST-pins that the env var DEFAULT
stays false. Slice 23 preserves that literal default — it adds
structural OVERRIDES on top, but the env-var default itself is
unchanged. The Phase 10 pin still passes.

# Test surface (3 AST pins + 9 spine)
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)
P10_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "phase10_graduation_contract.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_slice23_helper_substrate() -> None:
    """The Slice 23 substrate (helper + env-var constants + min-promoted
    threshold) MUST be in place with the canonical names that other
    pins + the dispatch call site depend on."""
    src = CG_FILE.read_text()
    assert "Slice 23" in src, (
        "candidate_generator missing Slice 23 attribution — refactor reverted"
    )
    for name in (
        "_SENTINEL_ENABLED_ENV",
        "_CLAUDE_DISABLED_ENV",
        "_SLICE23_MIN_PROMOTED_FOR_AUTO",
        "_slice23_should_activate_sentinel",
    ):
        assert name in src, (
            f"Slice 23 substrate symbol {name!r} missing"
        )
    # Env-var alignment with Slice 19a contract
    assert '"JARVIS_PROVIDER_CLAUDE_DISABLED"' in src or (
        "'JARVIS_PROVIDER_CLAUDE_DISABLED'" in src
    ), "Slice 23 not aligned with Slice 19a env-var contract"
    assert '"JARVIS_TOPOLOGY_SENTINEL_ENABLED"' in src or (
        "'JARVIS_TOPOLOGY_SENTINEL_ENABLED'" in src
    ), "Slice 23 missing legacy env-var alignment"


def test_ast_pin_dispatch_call_site_uses_helper() -> None:
    """The sentinel-gate call site in ``_generate_dispatch`` MUST call
    ``_slice23_should_activate_sentinel`` rather than the legacy raw
    ``os.environ.get(...)`` env check. Without this, Slice 23 is dead
    code and dispatch is still locked to single-model behavior."""
    src = CG_FILE.read_text()
    tree = ast.parse(src, filename=str(CG_FILE))
    dispatch_body_src = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_generate_dispatch"
        ):
            dispatch_body_src = ast.unparse(node)
            break
    assert dispatch_body_src, "_generate_dispatch not found"
    assert "_slice23_should_activate_sentinel" in dispatch_body_src, (
        "_generate_dispatch does not call _slice23_should_activate_sentinel "
        "— Slice 23 wiring incomplete; dispatch still uses env-only gate"
    )
    # The legacy direct env check pattern MUST be gone from the
    # sentinel gate site. We check that no os.environ.get with
    # JARVIS_TOPOLOGY_SENTINEL_ENABLED followed by a tuple-membership
    # check appears in _generate_dispatch body.
    # The helper itself reads it — that's fine because it's a module-
    # level helper, not inside _generate_dispatch.
    # Substring check: _generate_dispatch should NOT contain the
    # exact legacy pattern.
    legacy_pattern = (
        'os.environ.get(\n            "JARVIS_TOPOLOGY_SENTINEL_ENABLED"'
    )
    assert legacy_pattern not in dispatch_body_src, (
        "_generate_dispatch still contains the legacy direct env read "
        "for JARVIS_TOPOLOGY_SENTINEL_ENABLED — Slice 23 refactor "
        "incomplete; activation gate is not centralized"
    )


def test_ast_pin_phase10_contract_default_preserved() -> None:
    """Phase 10 graduation contract MUST still expect the env DEFAULT
    to be false. Slice 23 adds structural overrides ON TOP of the
    default — it does NOT change the literal env default value.
    Without this preservation, the Phase 10 contract pin would fail.
    """
    src = P10_FILE.read_text()
    # The contract doc must still reference "default-false" semantics
    # (we don't require an exact string; we check that the contract
    # hasn't been silently weakened to "default-true").
    assert "default-false" in src or "default false" in src, (
        "Phase 10 contract no longer documents default-false — "
        "Slice 23 may have inadvertently changed the env default"
    )
    # The contract module must NOT mention auto-activation
    # circumvention — Slice 23 is additive structural overrides,
    # not a bypass of the contract.
    assert "Slice 23" not in src or "preserved" in src.lower() or (
        "additive" in src.lower()
    ), (
        "phase10_graduation_contract.py mentions Slice 23 in a way "
        "that suggests bypass rather than additive override — review"
    )


# ──────────────────────────────────────────────────────────────────────
# Decision-matrix spine — 5 (one per branch)
# ──────────────────────────────────────────────────────────────────────


def test_spine_branch_1_env_explicit_on(monkeypatch) -> None:
    """Branch 1: explicit env-on → ACTIVATE with reason 'env_explicit_on'.
    Legacy contract preserved verbatim — every truthy variant honored."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _slice23_should_activate_sentinel,
    )
    for variant in ("true", "True", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", variant)
        activate, reason = _slice23_should_activate_sentinel("standard")
        assert activate is True, (
            f"Branch 1 failed: env={variant!r} → expected True, got {activate}"
        )
        assert reason == "env_explicit_on", (
            f"Wrong reason: got {reason!r}, expected 'env_explicit_on'"
        )


def test_spine_branch_2_env_explicit_off_wins(monkeypatch) -> None:
    """Branch 2: explicit env-off → DO NOT activate, even when
    Claude is disabled (operator rollback wins over structural conditions)."""
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    from backend.core.ouroboros.governance.candidate_generator import (
        _slice23_should_activate_sentinel,
    )
    for variant in ("false", "False", "0", "no", "off"):
        monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", variant)
        activate, reason = _slice23_should_activate_sentinel("standard")
        assert activate is False, (
            f"Branch 2 failed: env={variant!r} + Claude disabled → "
            f"expected False (rollback wins), got {activate}"
        )
        assert reason == "env_explicit_off", (
            f"Wrong reason: got {reason!r}, expected 'env_explicit_off'"
        )


def test_spine_branch_3_claude_disabled_auto_activates(monkeypatch) -> None:
    """Branch 3: Claude disabled + env unset → ACTIVATE with reason
    'claude_disabled'. This is the core Slice 23 composition with
    Slice 19a — DW fleet IS the only intelligence in this posture."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    from backend.core.ouroboros.governance.candidate_generator import (
        _slice23_should_activate_sentinel,
    )
    activate, reason = _slice23_should_activate_sentinel("standard")
    assert activate is True, (
        "Branch 3 failed: Claude disabled should auto-activate sentinel"
    )
    assert reason == "claude_disabled", (
        f"Wrong reason: got {reason!r}, expected 'claude_disabled'"
    )


def test_spine_branch_4_multi_model_fleet_auto_activates(monkeypatch) -> None:
    """Branch 4: env unset + Claude enabled + multi-model fleet
    (≥2 promoted) → ACTIVATE with reason 'multi_model_fleet'.

    Uses mock.patch on _trusted_seed_dw_models_for_route to simulate
    a 3-model promoted fleet for STANDARD route."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    from backend.core.ouroboros.governance import candidate_generator as cg_mod
    from backend.core.ouroboros.governance import provider_topology as pt_mod

    with mock.patch.object(
        pt_mod,
        "_trusted_seed_dw_models_for_route",
        return_value=("Qwen/Qwen3.5-397B-A17B-FP8", "Qwen/Qwen3.5-35B-A3B-FP8", "moonshotai/Kimi-K2.6"),
    ):
        activate, reason = cg_mod._slice23_should_activate_sentinel("standard")

    assert activate is True, (
        "Branch 4 failed: 3-model promoted fleet should auto-activate"
    )
    assert reason == "multi_model_fleet", (
        f"Wrong reason: got {reason!r}, expected 'multi_model_fleet'"
    )


def test_spine_branch_5_default_off_phase10_preserved(monkeypatch) -> None:
    """Branch 5: env unset + Claude enabled + single-model fleet
    → DO NOT activate (Phase 10 contract preserved for the
    Claude-enabled posture this contract was written about)."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    from backend.core.ouroboros.governance import candidate_generator as cg_mod
    from backend.core.ouroboros.governance import provider_topology as pt_mod

    # 1-model fleet (below the ≥2 threshold)
    with mock.patch.object(
        pt_mod, "_trusted_seed_dw_models_for_route",
        return_value=("Qwen/Qwen3.5-397B-A17B-FP8",),
    ):
        activate, reason = cg_mod._slice23_should_activate_sentinel("standard")

    assert activate is False, (
        "Branch 5 failed: single-model fleet + Claude enabled should NOT "
        "auto-activate (Phase 10 contract preservation)"
    )
    assert reason == "default_off_phase10_contract", (
        f"Wrong reason: got {reason!r}, expected 'default_off_phase10_contract'"
    )


# ──────────────────────────────────────────────────────────────────────
# Defensive spine — 4
# ──────────────────────────────────────────────────────────────────────


def test_spine_trusted_seed_probe_failure_defaults_off(monkeypatch) -> None:
    """When the trusted-seed bridge raises (e.g. circular-import collapse,
    OS error), the helper MUST default to off rather than propagate the
    exception into dispatch. Drift is enhancement, never a gate."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    from backend.core.ouroboros.governance import candidate_generator as cg_mod
    from backend.core.ouroboros.governance import provider_topology as pt_mod

    with mock.patch.object(
        pt_mod, "_trusted_seed_dw_models_for_route",
        side_effect=RuntimeError("simulated bridge failure"),
    ):
        activate, reason = cg_mod._slice23_should_activate_sentinel("standard")

    assert activate is False, (
        "Bridge probe failure should default to off, not crash dispatch"
    )
    assert reason == "trusted_seed_probe_failed", (
        f"Wrong reason: got {reason!r}, expected 'trusted_seed_probe_failed'"
    )


def test_spine_min_promoted_threshold_is_2(monkeypatch) -> None:
    """The threshold for 'multi-model fleet' MUST be ≥2 (not 1 or 3+).
    Single-model fleet → don't auto-activate (single model = nothing to
    rotate to). Two models = the minimum quorum for failover rotation."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    from backend.core.ouroboros.governance import candidate_generator as cg_mod
    from backend.core.ouroboros.governance import provider_topology as pt_mod

    assert cg_mod._SLICE23_MIN_PROMOTED_FOR_AUTO == 2, (
        f"Threshold drift: got {cg_mod._SLICE23_MIN_PROMOTED_FOR_AUTO}, "
        f"expected 2"
    )

    # 0 promoted → don't activate
    with mock.patch.object(
        pt_mod, "_trusted_seed_dw_models_for_route", return_value=(),
    ):
        activate, reason = cg_mod._slice23_should_activate_sentinel("standard")
    assert activate is False
    assert reason == "default_off_phase10_contract"

    # 1 promoted → don't activate
    with mock.patch.object(
        pt_mod, "_trusted_seed_dw_models_for_route",
        return_value=("Qwen/Qwen3.5-397B-A17B-FP8",),
    ):
        activate, reason = cg_mod._slice23_should_activate_sentinel("standard")
    assert activate is False
    assert reason == "default_off_phase10_contract"

    # 2 promoted → ACTIVATE
    with mock.patch.object(
        pt_mod, "_trusted_seed_dw_models_for_route",
        return_value=("Qwen/Qwen3.5-397B-A17B-FP8", "Qwen/Qwen3.5-35B-A3B-FP8"),
    ):
        activate, reason = cg_mod._slice23_should_activate_sentinel("standard")
    assert activate is True
    assert reason == "multi_model_fleet"


def test_spine_precedence_explicit_on_beats_claude_disabled(monkeypatch) -> None:
    """Branch ordering: explicit env-on takes precedence over Claude-disabled
    structural condition. Both produce ACTIVATE but the reason should
    reflect the higher-precedence branch for observability."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    from backend.core.ouroboros.governance.candidate_generator import (
        _slice23_should_activate_sentinel,
    )
    activate, reason = _slice23_should_activate_sentinel("standard")
    assert activate is True
    assert reason == "env_explicit_on", (
        f"Wrong reason ordering: got {reason!r}, expected 'env_explicit_on' "
        f"(higher precedence than 'claude_disabled')"
    )


def test_spine_precedence_claude_disabled_beats_multi_model(monkeypatch) -> None:
    """Branch ordering: Claude-disabled takes precedence over
    multi-model-fleet probe. Both ACTIVATE but the reason reflects
    the higher-precedence branch — also avoids the cost of the
    PromotionLedger probe when Claude-disabled already decided."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    from backend.core.ouroboros.governance import candidate_generator as cg_mod
    from backend.core.ouroboros.governance import provider_topology as pt_mod

    # If precedence is broken and the probe still runs, it returns
    # 3 models — but the helper should never get there.
    probe_called = {"n": 0}

    def _spy_probe(route):
        probe_called["n"] += 1
        return ("Qwen/Qwen3.5-397B-A17B-FP8", "Qwen/Qwen3.5-35B-A3B-FP8", "moonshotai/Kimi-K2.6")

    with mock.patch.object(
        pt_mod, "_trusted_seed_dw_models_for_route", side_effect=_spy_probe,
    ):
        activate, reason = cg_mod._slice23_should_activate_sentinel("standard")

    assert activate is True
    assert reason == "claude_disabled", (
        f"Wrong reason: got {reason!r}, expected 'claude_disabled'"
    )
    assert probe_called["n"] == 0, (
        f"Probe was invoked {probe_called['n']} times — precedence broken; "
        "the Claude-disabled fast path should short-circuit before the probe"
    )
