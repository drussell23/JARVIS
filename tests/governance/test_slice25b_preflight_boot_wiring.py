"""Slice 25B — Pre-Flight Eager Boot Integration & Graduation.

Wires the Slice 25 substrate into GovernedLoopService boot via 4 phases:

  Phase 1: HeavyProbeResult → ProbeOutcome adapter (build_heavyprobe_adapter
           + _heavyresult_to_outcome + _parse_heavyprobe_error)
  Phase 2: run_boot_preflight one-shot entry point + boot gate insertion
           in _build_components BEFORE BackgroundAgentPool.start
  Phase 3: 403 → ledger.demote + 5xx/timeout → sentinel.report_failure
           + all-fail → PreflightAllFailedError clean halt
  Phase 4: Autonomous activation — is_preflight_enabled returns True when
           JARVIS_PROVIDER_CLAUDE_DISABLED=true (Slice 19a posture
           composition, mirrors Slice 23's branch-3 pattern)

# Test surface (3 AST pins + 10 spine)
"""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PF_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "preflight_probe.py"
)
GLS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "governed_loop_service.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_phase4_autonomous_activation_via_slice19a() -> None:
    """is_preflight_enabled MUST consult JARVIS_PROVIDER_CLAUDE_DISABLED
    as an autonomous activation branch (Slice 19a composition).
    Without this, the operator's "DW-only posture is hard architectural
    requirement" contract is just env-flag hope."""
    src = PF_FILE.read_text()
    assert "Slice 25B Phase 4" in src, (
        "preflight_probe missing Slice 25B Phase 4 attribution"
    )
    # The Slice 19a env var MUST appear inside is_preflight_enabled
    tree = ast.parse(src, filename=str(PF_FILE))
    is_enabled_body = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "is_preflight_enabled"
        ):
            is_enabled_body = ast.unparse(node)
            break
    assert is_enabled_body, "is_preflight_enabled not found"
    assert "JARVIS_PROVIDER_CLAUDE_DISABLED" in is_enabled_body, (
        "is_preflight_enabled missing Slice 19a autonomous activation — "
        "DW-only sessions will silently skip preflight"
    )


def test_ast_pin_boot_gate_inserted_before_bg_pool_start() -> None:
    """The Slice 25B boot gate MUST fire in _build_components BEFORE
    BackgroundAgentPool.start. Without this ordering, workers unblock
    against an unprobed fleet and the entire safety-gate guarantee
    is moot."""
    src = GLS_FILE.read_text()
    assert "Slice 25B Phase 2" in src, (
        "governed_loop_service missing Slice 25B Phase 2 attribution"
    )
    assert "run_boot_preflight" in src, (
        "governed_loop_service missing run_boot_preflight call — wiring "
        "incomplete"
    )
    # Ordering check: the preflight call site must come BEFORE
    # `await self._bg_pool.start()`. We find both positions and
    # assert preflight < bg_pool_start in source order.
    pf_pos = src.find("run_boot_preflight(")
    bg_pos = src.find("await self._bg_pool.start()")
    assert pf_pos > 0 and bg_pos > 0, (
        "could not locate both Slice 25B preflight and bg_pool.start"
    )
    assert pf_pos < bg_pos, (
        "Slice 25B preflight gate is AFTER bg_pool.start — workers "
        "unblock against unprobed fleet; gate is non-functional"
    )


def test_ast_pin_adapter_no_parallel_classifier_reimplementation() -> None:
    """The HeavyProbe adapter MUST NOT re-implement entitlement marker
    matching. The structured ``entitlement_blocked:`` prefix is parsed
    out, and the marker is forwarded as ``error_body`` so the EXISTING
    dw_entitlement_classifier (composed in _classify_outcome) does
    the marker-match on the receiving side. Source-grep ban on the
    classifier's marker strings in the adapter code."""
    src = PF_FILE.read_text()
    # The adapter section starts at "Slice 25B Phase 1"
    phase1_marker = src.find("Slice 25B Phase 1 — HeavyProbe → ProbeOutcome")
    assert phase1_marker > 0, "Phase 1 attribution header missing"
    phase1_block = src[phase1_marker:phase1_marker + 6000]
    # Banned: re-implementing classifier marker logic
    banned = [
        "contact your administrator",  # marker is classifier's job
        "request access",
        "from .dw_entitlement_classifier",  # adapter must not import
    ]
    for pat in banned:
        assert pat not in phase1_block, (
            f"Slice 25B adapter contains banned pattern {pat!r} — "
            "duplicates classifier responsibility"
        )


# ──────────────────────────────────────────────────────────────────────
# Phase 4 spine — 4
# ──────────────────────────────────────────────────────────────────────


def test_spine_phase4_default_off(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_PREFLIGHT_PROBE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    from backend.core.ouroboros.governance.preflight_probe import (
        is_preflight_enabled,
    )
    assert is_preflight_enabled() is False


def test_spine_phase4_claude_disabled_auto_activates(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_PREFLIGHT_PROBE_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    from backend.core.ouroboros.governance.preflight_probe import (
        is_preflight_enabled,
    )
    assert is_preflight_enabled() is True


def test_spine_phase4_explicit_off_beats_claude_disabled(monkeypatch) -> None:
    """Operator rollback wins — explicit off blocks even when Claude
    is disabled. Mirrors Slice 23's branch-2 precedence."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_PROBE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    from backend.core.ouroboros.governance.preflight_probe import (
        is_preflight_enabled,
    )
    assert is_preflight_enabled() is False


def test_spine_phase4_explicit_on_independent_of_claude(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PREFLIGHT_PROBE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    from backend.core.ouroboros.governance.preflight_probe import (
        is_preflight_enabled,
    )
    assert is_preflight_enabled() is True


# ──────────────────────────────────────────────────────────────────────
# Adapter spine — 4
# ──────────────────────────────────────────────────────────────────────


def test_spine_parser_entitlement_blocked() -> None:
    """The Task #86 structured prefix MUST parse status + carry the
    marker into error_body so classifier re-detects it."""
    from backend.core.ouroboros.governance.preflight_probe import (
        _parse_heavyprobe_error,
    )
    sc, body, timeout, msg = _parse_heavyprobe_error(
        "entitlement_blocked:blocked by a routing rule:status_403"
    )
    assert sc == 403
    assert "blocked by a routing rule" in body
    assert timeout is False


def test_spine_parser_status_4xx_5xx() -> None:
    from backend.core.ouroboros.governance.preflight_probe import (
        _parse_heavyprobe_error,
    )
    assert _parse_heavyprobe_error("status_503:upstream timeout") == (
        503, "upstream timeout", False, "status_503:upstream timeout",
    )
    assert _parse_heavyprobe_error("status_404:not found")[0] == 404


def test_spine_parser_ttft_timeout() -> None:
    from backend.core.ouroboros.governance.preflight_probe import (
        _parse_heavyprobe_error,
    )
    sc, body, timeout, msg = _parse_heavyprobe_error("ttft_timeout")
    assert sc == 0
    assert timeout is True


def test_spine_heavyresult_to_outcome_success_path() -> None:
    """HeavyProbeResult.success=True → ProbeOutcome.success=True
    with status_code=200 + ttft_ms forwarded as latency_ms."""
    from backend.core.ouroboros.governance.preflight_probe import (
        _heavyresult_to_outcome, ProbeOutcome,
    )

    @dataclass
    class _FakeHeavyResult:
        model_id: str = "Qwen-397B"
        success: bool = True
        ttft_ms: int = 420
        total_latency_ms: int = 580
        cost_usd: float = 0.00002
        error: str = ""

    outcome = _heavyresult_to_outcome(_FakeHeavyResult())
    assert isinstance(outcome, ProbeOutcome)
    assert outcome.success is True
    assert outcome.status_code == 200
    assert outcome.latency_ms == 420


# ──────────────────────────────────────────────────────────────────────
# Boot-eager entry spine — 2
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_boot_preflight_skips_when_master_off(monkeypatch) -> None:
    """When master flag is off (default), run_boot_preflight returns
    None without invoking any substrate."""
    monkeypatch.delenv("JARVIS_PREFLIGHT_PROBE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    from backend.core.ouroboros.governance.preflight_probe import (
        run_boot_preflight,
    )
    # Pass a stub dw_provider — should never be touched
    fake_provider = mock.MagicMock()
    fake_provider._get_session = mock.AsyncMock(
        side_effect=AssertionError("session should not be acquired"),
    )
    result = await run_boot_preflight(dw_provider=fake_provider)
    assert result is None
    assert not fake_provider._get_session.called


@pytest.mark.asyncio
async def test_spine_boot_preflight_composes_substrate_end_to_end(
    monkeypatch, tmp_path,
) -> None:
    """End-to-end boot probe with injected prober_factory: master flag
    on, fake DW provider, fake prober that returns mixed outcomes
    (1 active, 1 entitlement, 1 5xx). Verifies the report shape +
    side-effects route correctly."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_PROBE_ENABLED", "true")
    # Use a tmpdir ledger so we don't touch real .jarvis state
    monkeypatch.setenv("JARVIS_DW_PROMOTION_LEDGER_PATH", str(tmp_path / "ledger.json"))
    # Seed via the canonical trusted-models env path (Slice 10B substrate)
    monkeypatch.setenv(
        "JARVIS_DW_TRUSTED_MODELS",
        "Qwen-397B,Qwen-4B,Qwen-35B",
    )
    # Reset the default sentinel so we get fresh state
    from backend.core.ouroboros.governance import topology_sentinel as ts_mod
    monkeypatch.setattr(ts_mod, "_default_sentinel", None)

    # Sanity-check: the trusted-seed path actually promoted these models
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger,
    )
    seed_ledger = PromotionLedger()
    seed_ledger.load()
    promoted = seed_ledger.promoted_models()
    assert set(promoted) == {"Qwen-397B", "Qwen-4B", "Qwen-35B"}, (
        f"Trusted-seed didn't promote expected set: got {promoted!r}"
    )

    # Fake DW provider with a session
    fake_provider = mock.MagicMock()
    fake_provider._get_session = mock.AsyncMock(return_value=mock.MagicMock())
    fake_provider._base_url = "https://test.example/v1"
    fake_provider._api_key = "test-key"

    # Fake prober that returns different outcomes per model
    class _FakeProber:
        async def probe(self, *, session, model_id, base_url, api_key, **kw):
            from backend.core.ouroboros.governance.dw_heavy_probe import (
                HeavyProbeResult,
            )
            if model_id == "Qwen-397B":
                return HeavyProbeResult(
                    model_id=model_id, success=True,
                    ttft_ms=300, total_latency_ms=500, cost_usd=0.00002,
                )
            if model_id == "Qwen-4B":
                return HeavyProbeResult(
                    model_id=model_id, success=False,
                    ttft_ms=10000, total_latency_ms=200, cost_usd=0.0,
                    error=(
                        "entitlement_blocked:blocked by a routing rule:"
                        "status_403"
                    ),
                )
            if model_id == "Qwen-35B":
                return HeavyProbeResult(
                    model_id=model_id, success=False,
                    ttft_ms=10000, total_latency_ms=800, cost_usd=0.00002,
                    error="status_503:upstream",
                )

    from backend.core.ouroboros.governance.preflight_probe import (
        run_boot_preflight,
    )
    report = await run_boot_preflight(
        dw_provider=fake_provider,
        prober_factory=_FakeProber,
    )
    assert report is not None
    assert report.active_count == 1
    assert report.demoted_entitlement_count == 1
    assert report.degraded_5xx_count == 1
    assert report.all_failed is False

    # Verify the ledger now reflects the entitlement demotion (read fresh)
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        PromotionLedger, QUARANTINE_ACCOUNT_NOT_ENTITLED,
    )
    check = PromotionLedger()
    check.load()
    assert "Qwen-4B" not in check.promoted_models(), (
        "Qwen-4B should be demoted after entitlement block — "
        "side-effect persistence broken"
    )
    snap = check.snapshot("Qwen-4B")
    assert snap is not None
    assert snap.quarantine_origin == QUARANTINE_ACCOUNT_NOT_ENTITLED, (
        f"Expected origin=account_not_entitled, got {snap.quarantine_origin!r}"
    )
