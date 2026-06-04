"""Slice 27 — Aegis-Unified Auth Bridge & Context-Aware Adaptive Timeout.

Closes two upstream coordination roadblocks surfaced by v20
(bt-2026-05-27-011121):

# Phase 2 — Aegis-unified auth bridge

``DoublewordProvider.prompt_only()`` previously raised
``ValueError("DOUBLEWORD_API_KEY is not set...")`` when self._api_key
was empty — which is the post-scrub state when Aegis is enabled
(Aegis is the secure credential broker that injects the real key
server-side). v20 logs showed this breaking SemanticTriage,
IntentDiscovery, and would silently break Slice 20B json_healer.

Fix: accept Aegis-enabled as a valid credential source. The check
becomes ``not self._api_key AND not aegis_enabled() → raise``.

# Phase 3 — Context-aware adaptive Tier 0 timeout

v20 forensic: 12 EXHAUSTION events ALL with fsm_failure_mode=TIMEOUT
on a 3-model fleet. The static 90s Tier 0 cap (Slice 18c) gave the
same budget to every dispatch regardless of payload size or model
tier. Per operator directive:

    timeout = (base + step_bonus × floor(prompt_chars / step_chars))
              × (heavy_scalar if heavy_model else 1.0)
    timeout = min(timeout, cap)

Defaults: base=60s, step_chars=5000, step_bonus=15s, scalar=1.5,
cap=240s. Heavy model markers: ("397B", "Kimi") — extensible via
JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS env CSV.

Legacy callers that pass only ``provider_route`` (no model_id / no
prompt_chars) get the byte-identical pre-Slice-27 static 90s cap —
no regression in existing behavior.

# Test surface (3 AST pins + 14 spine)
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)
DW_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "doubleword_provider.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_slice27_phase2_aegis_auth_bridge_in_prompt_only() -> None:
    """prompt_only MUST consult aegis.client.is_enabled BEFORE
    raising on missing api_key. Without this, post-scrub callers
    (SemanticTriage / IntentDiscovery / json_healer) break silently."""
    src = DW_FILE.read_text()
    assert "Slice 27 Phase 2" in src, (
        "doubleword_provider missing Slice 27 Phase 2 attribution"
    )
    # AST walk: the prompt_only body must check is_enabled in
    # combination with self._api_key. (Note: ast.unparse strips
    # comments, so the "Slice 27" attribution check uses raw-source
    # within the function's lineno range, not the unparsed body.)
    tree = ast.parse(src, filename=str(DW_FILE))
    src_lines = src.split("\n")
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "prompt_only"
        ):
            body_src = ast.unparse(node)
            # Code-level: is_enabled call + api_key check both present
            code_ok = (
                "is_enabled" in body_src
                and "self._api_key" in body_src
            )
            # Comment-level: Slice 27 attribution present in raw source
            # within function's lineno range (ast.unparse strips comments)
            raw_func_lines = src_lines[node.lineno - 1: node.end_lineno or node.lineno]
            raw_block = "\n".join(raw_func_lines)
            attribution_ok = "Slice 27" in raw_block
            if code_ok and attribution_ok:
                found = True
                break
    assert found, (
        "prompt_only body missing Slice 27 Phase 2 Aegis check — "
        "post-scrub callers will re-break"
    )


def test_ast_pin_slice27_phase3_adaptive_formula_constants() -> None:
    """The 5 adaptive-formula constants MUST be declared as module
    symbols (so operators can grep their env names; so tests can
    reference them; so future refactors can't silently change
    defaults without test failure)."""
    src = CG_FILE.read_text()
    assert "Slice 27 Phase 3" in src, (
        "candidate_generator missing Slice 27 Phase 3 attribution"
    )
    for sym in (
        "_ADAPTIVE_BASE_S_DEFAULT",
        "_ADAPTIVE_STEP_CHARS_DEFAULT",
        "_ADAPTIVE_STEP_BONUS_S_DEFAULT",
        "_ADAPTIVE_HEAVY_SCALAR_DEFAULT",
        "_ADAPTIVE_CAP_S_DEFAULT",
        "_HEAVY_MODEL_DEFAULT_MARKERS",
        "_compute_adaptive_tier0_timeout_s",
        "_is_heavy_model",
    ):
        assert sym in src, (
            f"Slice 27 Phase 3 symbol {sym!r} missing"
        )
    # The 4 env-knob names must all appear (operators must be able to
    # grep them in source).
    for env in (
        "JARVIS_ADAPTIVE_TIER0_BASE_S",
        "JARVIS_ADAPTIVE_TIER0_STEP_CHARS",
        "JARVIS_ADAPTIVE_TIER0_STEP_BONUS_S",
        "JARVIS_ADAPTIVE_TIER0_HEAVY_SCALAR",
        "JARVIS_ADAPTIVE_TIER0_CAP_S",
        "JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS",
    ):
        assert env in src, (
            f"Slice 27 Phase 3 env-knob {env!r} missing — operator "
            "cannot tune without code edit"
        )


def test_ast_pin_legacy_tier0_signature_backwards_compatible() -> None:
    """_tier0_rt_cap_for_route MUST accept (provider_route) as the
    only positional arg — legacy callers (e.g. _compute_tier0_budget
    line 5087) must keep compiling without modification."""
    src = CG_FILE.read_text()
    tree = ast.parse(src, filename=str(CG_FILE))
    found_sig = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_tier0_rt_cap_for_route"
        ):
            # First positional must be provider_route; new kwargs
            # (model_id, prompt_chars) must have defaults
            posonly = node.args.args
            assert len(posonly) >= 1
            assert posonly[0].arg == "provider_route"
            # New kwargs must be keyword-only with defaults
            kwonly_names = [a.arg for a in node.args.kwonlyargs]
            assert "model_id" in kwonly_names, (
                "_tier0_rt_cap_for_route missing model_id kwarg — Slice 27 "
                "adaptive path unreachable"
            )
            assert "prompt_chars" in kwonly_names, (
                "_tier0_rt_cap_for_route missing prompt_chars kwarg"
            )
            found_sig = True
            break
    assert found_sig, "_tier0_rt_cap_for_route not found"


# ──────────────────────────────────────────────────────────────────────
# Phase 2 Aegis-auth spine — 4
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_phase2_prompt_only_raises_when_both_absent(
    monkeypatch,
) -> None:
    """When BOTH api_key absent AND Aegis disabled, prompt_only must
    raise ValueError (the legacy contract for the case where there
    truly is no credential path)."""
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_AEGIS_ENABLED", raising=False)
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    from backend.core.ouroboros.governance import doubleword_provider as dw_mod

    # Build with explicit empty key; Aegis off
    provider = DoublewordProvider(api_key="")
    # Patch is_enabled in the LAZY-IMPORTED module to return False
    # (the import happens INSIDE prompt_only)
    from backend.core.ouroboros.aegis import client as aegis_client_mod
    monkeypatch.setattr(aegis_client_mod, "is_enabled", lambda: False)

    with pytest.raises(ValueError, match="DOUBLEWORD_API_KEY"):
        await provider.prompt_only("test")


@pytest.mark.asyncio
async def test_spine_phase2_prompt_only_proceeds_when_aegis_on(
    monkeypatch,
) -> None:
    """When api_key empty BUT Aegis enabled, prompt_only must NOT
    raise — the empty key is the expected post-scrub state, Aegis
    will inject the real key server-side."""
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )

    provider = DoublewordProvider(api_key="")
    # Force Aegis-enabled
    from backend.core.ouroboros.aegis import client as aegis_client_mod
    monkeypatch.setattr(aegis_client_mod, "is_enabled", lambda: True)

    # Mock the actual batch upload to avoid network — we just need to
    # confirm the ValueError doesn't fire at the early gate.
    mock_upload = mock.AsyncMock(return_value="")  # empty file_id → returns "" cleanly
    monkeypatch.setattr(provider, "_upload_file", mock_upload)
    # _check_budget is a no-op if no batch limits set — but mock it defensively
    monkeypatch.setattr(provider, "_check_budget", lambda: None)
    mock_session = mock.AsyncMock(return_value=mock.MagicMock())
    monkeypatch.setattr(provider, "_get_session", mock_session)

    # Should NOT raise — the gate accepts Aegis as the credential source
    result = await provider.prompt_only("test")
    assert result == ""  # empty because we mocked upload to fail cleanly
    # Upload was attempted (proves we got past the early gate)
    assert mock_upload.called


@pytest.mark.asyncio
async def test_spine_phase2_prompt_only_proceeds_when_key_present_aegis_off(
    monkeypatch,
) -> None:
    """Legacy path: api_key present, Aegis off → proceed as before."""
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "test-key")
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )

    provider = DoublewordProvider(api_key="test-key")
    from backend.core.ouroboros.aegis import client as aegis_client_mod
    monkeypatch.setattr(aegis_client_mod, "is_enabled", lambda: False)

    monkeypatch.setattr(provider, "_upload_file", mock.AsyncMock(return_value=""))
    monkeypatch.setattr(provider, "_check_budget", lambda: None)
    monkeypatch.setattr(provider, "_get_session", mock.AsyncMock(return_value=mock.MagicMock()))

    result = await provider.prompt_only("test")
    assert result == ""


@pytest.mark.asyncio
async def test_spine_phase2_defensive_aegis_import_failure(
    monkeypatch,
) -> None:
    """If the Aegis client module import raises (circular import /
    deployment defect), the gate should treat Aegis as disabled and
    fall back to the api_key check. NEVER propagates the import
    exception to the caller."""
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "test-key")  # key present → no raise
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )

    provider = DoublewordProvider(api_key="test-key")
    monkeypatch.setattr(provider, "_upload_file", mock.AsyncMock(return_value=""))
    monkeypatch.setattr(provider, "_check_budget", lambda: None)
    monkeypatch.setattr(provider, "_get_session", mock.AsyncMock(return_value=mock.MagicMock()))

    # Sabotage the Aegis import by monkey-patching sys.modules so
    # `from backend.core.ouroboros.aegis.client import is_enabled` fails.
    import sys
    monkeypatch.setitem(sys.modules, "backend.core.ouroboros.aegis.client", None)
    # The gate's try/except should handle this → fall through with
    # _aegis_active=False → key present so no raise
    result = await provider.prompt_only("test")
    assert result == ""


# ──────────────────────────────────────────────────────────────────────
# Phase 3 adaptive-timeout spine — 10
# ──────────────────────────────────────────────────────────────────────


def test_spine_phase3_base_no_payload_no_heavy() -> None:
    """0 chars, non-heavy → base value (60s default)."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _compute_adaptive_tier0_timeout_s,
    )
    assert _compute_adaptive_tier0_timeout_s(
        prompt_chars=0, model_id="Qwen/Qwen3.5-35B-A3B-FP8",
    ) == 60.0


def test_spine_phase3_step_bonus_per_5000_chars() -> None:
    """+15s per 5,000 chars step bonus on non-heavy model."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _compute_adaptive_tier0_timeout_s,
    )
    fn = _compute_adaptive_tier0_timeout_s
    mid = "Qwen/Qwen3.5-35B-A3B-FP8"
    # Boundary: just under 5000 → 0 steps → base only
    assert fn(prompt_chars=4999, model_id=mid) == 60.0
    # Exactly 5000 → 1 step → +15
    assert fn(prompt_chars=5000, model_id=mid) == 75.0
    # 10000 → 2 steps → +30
    assert fn(prompt_chars=10000, model_id=mid) == 90.0
    # 14999 → 2 steps → +30 (floor div)
    assert fn(prompt_chars=14999, model_id=mid) == 90.0
    # 15000 → 3 steps → +45
    assert fn(prompt_chars=15000, model_id=mid) == 105.0


def test_spine_phase3_heavy_scalar_applies_to_397b() -> None:
    """1.5× scalar for Qwen-397B."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _compute_adaptive_tier0_timeout_s,
    )
    fn = _compute_adaptive_tier0_timeout_s
    mid = "Qwen/Qwen3.5-397B-A17B-FP8"
    assert fn(prompt_chars=0, model_id=mid) == 90.0       # 60 × 1.5
    assert fn(prompt_chars=10000, model_id=mid) == 135.0  # (60+30) × 1.5
    assert fn(prompt_chars=30000, model_id=mid) == 225.0  # (60+90) × 1.5


def test_spine_phase3_heavy_scalar_applies_to_kimi() -> None:
    """1.5× scalar for Kimi-K2.6."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _compute_adaptive_tier0_timeout_s,
    )
    assert _compute_adaptive_tier0_timeout_s(
        prompt_chars=0, model_id="moonshotai/Kimi-K2.6",
    ) == 90.0
    assert _compute_adaptive_tier0_timeout_s(
        prompt_chars=20000, model_id="moonshotai/Kimi-K2.6",
    ) == 180.0  # (60+60) × 1.5


def test_spine_phase3_hard_cap_at_240s() -> None:
    """50K chars on 397B would compute to 315s but cap clamps to 240s."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _compute_adaptive_tier0_timeout_s,
    )
    fn = _compute_adaptive_tier0_timeout_s
    mid = "Qwen/Qwen3.5-397B-A17B-FP8"
    # 50000 → 10 steps → 60+150=210 → ×1.5 = 315 → capped 240
    assert fn(prompt_chars=50000, model_id=mid) == 240.0
    # Even larger
    assert fn(prompt_chars=1_000_000, model_id=mid) == 240.0


def test_spine_phase3_no_heavy_scalar_on_35b_or_4b() -> None:
    """35B and 4B are NOT in the heavy marker list."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _compute_adaptive_tier0_timeout_s, _is_heavy_model,
    )
    assert _is_heavy_model("Qwen/Qwen3.5-35B-A3B-FP8") is False
    assert _is_heavy_model("Qwen/Qwen3.5-4B") is False
    # 10K chars on 35B → 90s (no scalar)
    assert _compute_adaptive_tier0_timeout_s(
        prompt_chars=10000, model_id="Qwen/Qwen3.5-35B-A3B-FP8",
    ) == 90.0


def test_spine_phase3_env_knobs_override_defaults(monkeypatch) -> None:
    """All 5 env knobs must override their defaults at call time."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_TIER0_BASE_S", "100")
    monkeypatch.setenv("JARVIS_ADAPTIVE_TIER0_STEP_CHARS", "10000")
    monkeypatch.setenv("JARVIS_ADAPTIVE_TIER0_STEP_BONUS_S", "20")
    monkeypatch.setenv("JARVIS_ADAPTIVE_TIER0_HEAVY_SCALAR", "2.0")
    monkeypatch.setenv("JARVIS_ADAPTIVE_TIER0_CAP_S", "500")
    from backend.core.ouroboros.governance.candidate_generator import (
        _compute_adaptive_tier0_timeout_s,
    )
    # 10K chars + heavy model → (100 + 20*1) × 2.0 = 240s
    assert _compute_adaptive_tier0_timeout_s(
        prompt_chars=10000, model_id="Qwen/Qwen3.5-397B-A17B-FP8",
    ) == 240.0


def test_spine_phase3_heavy_marker_env_csv_override(monkeypatch) -> None:
    """Operators can add new heavy markers via CSV env."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS", "MyCustomModel,512B")
    from backend.core.ouroboros.governance.candidate_generator import (
        _is_heavy_model, _heavy_model_markers,
    )
    assert _heavy_model_markers() == ("MyCustomModel", "512B")
    assert _is_heavy_model("vendor/MyCustomModel-XL") is True
    assert _is_heavy_model("Qwen/Qwen3.5-512B-MoE") is True
    # Slice 84 — markers are now ADDITIVE to a param-aware path: a 397B model
    # stays heavy even when it is absent from the custom marker list, because
    # 397B >= the 100B param floor (the v44-v64 regression was exactly a large
    # coder NOT matching a marker and getting the bare 30s TTFT cap). To fully
    # exclude a large model an operator raises JARVIS_HEAVY_MODEL_MIN_PARAMS_B.
    assert _is_heavy_model("Qwen/Qwen3.5-397B-A17B-FP8") is True
    monkeypatch.setenv("JARVIS_HEAVY_MODEL_MIN_PARAMS_B", "500")
    assert _is_heavy_model("Qwen/Qwen3.5-397B-A17B-FP8") is False  # 397 < 500


def test_spine_phase3_legacy_route_only_returns_static_90s(monkeypatch) -> None:
    """The legacy caller pattern (route-only, no kwargs) MUST return
    the static 90s value — preserves Slice 18c byte-identically and
    keeps existing callers at line 5087 working unchanged."""
    monkeypatch.delenv("JARVIS_DW_TIER0_RT_BUDGET_S", raising=False)
    from backend.core.ouroboros.governance.candidate_generator import (
        _tier0_rt_cap_for_route,
    )
    # Legacy callers — no kwargs
    assert _tier0_rt_cap_for_route("standard") == 90.0
    assert _tier0_rt_cap_for_route("complex") == 90.0
    # Non-STANDARD/COMPLEX always returns 30s reflex cap
    assert _tier0_rt_cap_for_route("background") == 30.0
    assert _tier0_rt_cap_for_route("immediate") == 30.0
    assert _tier0_rt_cap_for_route("speculative") == 30.0


def test_spine_phase3_adaptive_path_engaged_when_kwargs_present(monkeypatch) -> None:
    """When EITHER model_id or prompt_chars is provided, route ==
    standard/complex switches to adaptive formula. Non-STANDARD
    routes preserve 30s cap regardless of kwargs (cost-optimization
    semantics)."""
    monkeypatch.delenv("JARVIS_DW_TIER0_RT_BUDGET_S", raising=False)
    from backend.core.ouroboros.governance.candidate_generator import (
        _tier0_rt_cap_for_route,
    )
    # STANDARD + heavy model + 10K chars → adaptive 135s
    assert _tier0_rt_cap_for_route(
        "standard",
        model_id="Qwen/Qwen3.5-397B-A17B-FP8",
        prompt_chars=10000,
    ) == 135.0
    # BACKGROUND + heavy model + 10K chars → still 30s (route override)
    assert _tier0_rt_cap_for_route(
        "background",
        model_id="Qwen/Qwen3.5-397B-A17B-FP8",
        prompt_chars=10000,
    ) == 30.0
    # STANDARD + just prompt_chars (no model_id) → adaptive non-heavy
    # 10K chars → 90s (60+30)
    assert _tier0_rt_cap_for_route(
        "standard", prompt_chars=10000,
    ) == 90.0


def test_spine_phase3_defensive_negative_chars_treated_as_zero() -> None:
    """Misconfigured callers passing negative prompt_chars must NOT
    underflow the formula."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _compute_adaptive_tier0_timeout_s,
    )
    # Should treat negative as 0
    result = _compute_adaptive_tier0_timeout_s(
        prompt_chars=-5000, model_id="Qwen/Qwen3.5-35B-A3B-FP8",
    )
    assert result == 60.0  # base only, no step bonus
