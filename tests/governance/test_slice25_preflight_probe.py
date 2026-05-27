"""Slice 25 — Autonomous Pre-Flight Health & Entitlement Sentinel.

Closes the observability gap surfaced by v18 (bt-2026-05-26-233010):
when the upstream DW tier undergoes a network blackout or per-model
entitlement failure, the dispatch engine blindly churns wall-clock
time burning through every model in the fleet. Slice 25 adds a
boot-time orchestrator that composes the existing HeavyProbe +
dw_entitlement_classifier + PromotionLedger.demote + TopologySentinel
into a single fail-fast preflight.

# Composition (no duplication)

* HeavyProber (existing) — async probe primitive injected via
  ``probe_fn`` parameter for testability.
* dw_entitlement_classifier (existing) — pure-function 4xx classifier.
* PromotionLedger.demote (existing) — augmented with new origin
  constant ``QUARANTINE_ACCOUNT_NOT_ENTITLED``.
* TopologySentinel.report_failure (existing) — Slice 24's structural
  fields carry through.

# Closed verdict taxonomy (AST-pinned)

PreflightVerdict has exactly 5 values:
  ACTIVE / DEMOTED_ENTITLEMENT / DEGRADED_5XX / DEGRADED_TIMEOUT / ERROR_OTHER

# Test surface (3 AST pins + 9 spine)
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PF_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "preflight_probe.py"
)
LEDGER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "dw_promotion_ledger.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_preflight_verdict_taxonomy_closed_at_5() -> None:
    """PreflightVerdict MUST be exactly the 5 documented values.
    Adding a 6th drift kind requires updating this pin + the
    classifier + side-effect router — that friction is intentional."""
    src = PF_FILE.read_text()
    tree = ast.parse(src, filename=str(PF_FILE))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PreflightVerdict":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            found.append(target.id)
    expected = {
        "ACTIVE",
        "DEMOTED_ENTITLEMENT",
        "DEGRADED_5XX",
        "DEGRADED_TIMEOUT",
        "ERROR_OTHER",
    }
    assert set(found) == expected, (
        f"PreflightVerdict taxonomy drift: got {found!r}, expected {expected!r}"
    )


def test_ast_pin_ledger_account_not_entitled_origin_registered() -> None:
    """QUARANTINE_ACCOUNT_NOT_ENTITLED MUST be declared AND included
    in _VALID_QUARANTINE_ORIGINS. Without the latter, demote()
    silently falls back to OPERATOR_DEMOTED and the postmortem
    can't tell entitlement evictions apart from manual demotes."""
    src = LEDGER_FILE.read_text()
    assert 'QUARANTINE_ACCOUNT_NOT_ENTITLED = "account_not_entitled"' in src, (
        "QUARANTINE_ACCOUNT_NOT_ENTITLED constant missing or renamed"
    )
    # Walk the AST to confirm it's in the _VALID_QUARANTINE_ORIGINS frozenset
    tree = ast.parse(src, filename=str(LEDGER_FILE))
    found_in_valid = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_VALID_QUARANTINE_ORIGINS"
        ):
            body_src = ast.unparse(node)
            if "QUARANTINE_ACCOUNT_NOT_ENTITLED" in body_src:
                found_in_valid = True
                break
    assert found_in_valid, (
        "QUARANTINE_ACCOUNT_NOT_ENTITLED not in _VALID_QUARANTINE_ORIGINS "
        "frozenset — demote() will silently swap to OPERATOR_DEMOTED"
    )


def test_ast_pin_composes_existing_substrate_no_duplication() -> None:
    """preflight_probe MUST compose existing primitives — not
    duplicate them. Verify by source-grep that the canonical imports
    are present (lazy imports inside side-effect helpers count;
    they're the operator-binding-compliant composition pattern)."""
    src = PF_FILE.read_text()
    # Must compose, not duplicate
    assert "dw_entitlement_classifier" in src, (
        "preflight_probe must compose dw_entitlement_classifier, "
        "not reimplement 4xx classification"
    )
    assert "QUARANTINE_ACCOUNT_NOT_ENTITLED" in src, (
        "preflight_probe must import the canonical origin constant"
    )
    assert "topology_sentinel" in src, (
        "preflight_probe must compose TopologySentinel.report_failure"
    )
    # Must NOT contain any raw HTTP-classification code (the classifier
    # is the single seam). Source-grep ban on telltale patterns.
    banned_patterns = [
        "blocked by a routing rule",  # marker is the classifier's job
        "contact your administrator",
    ]
    for pat in banned_patterns:
        assert pat not in src, (
            f"preflight_probe contains raw classifier marker {pat!r} — "
            "violates composition: the classifier owns marker matching"
        )


# ──────────────────────────────────────────────────────────────────────
# Spine — 9
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_all_active_returns_clean_report() -> None:
    """All probes succeed → report.active_count == fleet size,
    all_failed=False, no exception."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome,
    )

    async def stub(mid):
        return ProbeOutcome(model_id=mid, success=True, status_code=200, latency_ms=50)

    report = await run_preflight(
        model_ids=("Qwen-397B", "Qwen-35B", "Kimi"),
        probe_fn=stub,
    )
    assert report.active_count == 3
    assert report.demoted_entitlement_count == 0
    assert report.degraded_5xx_count == 0
    assert report.all_failed is False


@pytest.mark.asyncio
async def test_spine_403_entitlement_demotes_model() -> None:
    """403 + entitlement marker → DEMOTED_ENTITLEMENT verdict + ledger.demote
    called with origin=account_not_entitled."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome, PreflightVerdict,
    )

    async def stub(mid):
        if mid == "Qwen-4B":
            return ProbeOutcome(
                model_id=mid,
                success=False,
                status_code=403,
                error_body=(
                    "Real-time access to 'Qwen-4B' is blocked by a "
                    "routing rule. Please contact your administrator."
                ),
            )
        return ProbeOutcome(model_id=mid, success=True, status_code=200, latency_ms=50)

    mock_ledger = mock.MagicMock()
    report = await run_preflight(
        model_ids=("Qwen-397B", "Qwen-4B"),
        probe_fn=stub,
        ledger=mock_ledger,
    )
    assert report.demoted_entitlement_count == 1
    assert report.active_count == 1
    # The model with entitlement block
    blocked = [r for r in report.results if r.verdict is PreflightVerdict.DEMOTED_ENTITLEMENT]
    assert len(blocked) == 1
    assert blocked[0].model_id == "Qwen-4B"
    assert "blocked by a routing rule" in blocked[0].entitlement_marker

    # ledger.demote called with the right origin
    from backend.core.ouroboros.governance.dw_promotion_ledger import (
        QUARANTINE_ACCOUNT_NOT_ENTITLED,
    )
    assert mock_ledger.demote.called, "ledger.demote was not invoked"
    call_args = mock_ledger.demote.call_args
    assert call_args.args[0] == "Qwen-4B"
    assert call_args.kwargs.get("origin") == QUARANTINE_ACCOUNT_NOT_ENTITLED


@pytest.mark.asyncio
async def test_spine_5xx_calls_sentinel_report_failure_with_slice24_fields() -> None:
    """5xx response → DEGRADED_5XX verdict + sentinel.report_failure
    called with the Slice 24 structural fields (status_code,
    response_body, is_terminal=False)."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome, PreflightVerdict,
    )

    async def stub(mid):
        return ProbeOutcome(
            model_id=mid, success=False, status_code=503,
            error_body="upstream unavailable", error_message="provider blackout",
        )

    mock_sentinel = mock.MagicMock()
    report = await run_preflight(
        model_ids=("Qwen-397B",),
        probe_fn=stub,
        sentinel=mock_sentinel,
        halt_on_all_fail=False,
    )
    assert report.degraded_5xx_count == 1
    assert mock_sentinel.report_failure.called
    call_kwargs = mock_sentinel.report_failure.call_args.kwargs
    assert call_kwargs.get("status_code") == 503
    assert call_kwargs.get("response_body") == "upstream unavailable"
    assert call_kwargs.get("is_terminal") is False


@pytest.mark.asyncio
async def test_spine_timeout_calls_sentinel_report_failure_live_transport() -> None:
    """Probe timeout → DEGRADED_TIMEOUT + sentinel.report_failure with
    FailureSource.LIVE_TRANSPORT (not LIVE_HTTP_5XX)."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome,
    )

    async def stub(mid):
        # Simulate timeout by sleeping past the per-model timeout
        await asyncio.sleep(0.5)
        return ProbeOutcome(model_id=mid, success=False, status_code=0)

    mock_sentinel = mock.MagicMock()
    report = await run_preflight(
        model_ids=("Qwen-397B",),
        probe_fn=stub,
        sentinel=mock_sentinel,
        timeout_per_model_s=0.1,
        halt_on_all_fail=False,
    )
    assert report.degraded_timeout_count == 1
    assert mock_sentinel.report_failure.called
    # FailureSource.LIVE_TRANSPORT was used (verify via the second positional arg)
    from backend.core.ouroboros.governance.topology_sentinel import FailureSource
    call_args = mock_sentinel.report_failure.call_args
    assert call_args.args[1] is FailureSource.LIVE_TRANSPORT


@pytest.mark.asyncio
async def test_spine_all_fail_halt_on_all_fail_raises() -> None:
    """When every model fails AND halt_on_all_fail=True (default),
    PreflightAllFailedError raises with the structured report."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome, PreflightAllFailedError,
    )

    async def stub(mid):
        return ProbeOutcome(model_id=mid, success=False, status_code=503)

    with pytest.raises(PreflightAllFailedError) as excinfo:
        await run_preflight(
            model_ids=("Qwen-397B", "Qwen-35B"),
            probe_fn=stub,
        )
    assert excinfo.value.report.all_failed is True
    assert excinfo.value.report.degraded_5xx_count == 2


@pytest.mark.asyncio
async def test_spine_halt_on_all_fail_false_returns_report() -> None:
    """When halt_on_all_fail=False, all-failure returns the report
    instead of raising — caller can branch on report.all_failed."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome,
    )

    async def stub(mid):
        return ProbeOutcome(model_id=mid, success=False, status_code=503)

    report = await run_preflight(
        model_ids=("Qwen-397B",),
        probe_fn=stub,
        halt_on_all_fail=False,
    )
    assert report.all_failed is True
    # Did NOT raise


@pytest.mark.asyncio
async def test_spine_empty_fleet_returns_empty_report_no_raise() -> None:
    """Empty model_ids → empty report, NO raise (caller should not have
    invoked us with empty fleet, but defensive)."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome,
    )

    async def stub(mid):
        raise AssertionError("probe_fn should not be called")

    report = await run_preflight(model_ids=(), probe_fn=stub)
    assert report.total_probed == 0
    assert report.all_failed is False  # all_failed only True when len > 0


@pytest.mark.asyncio
async def test_spine_probe_exception_classified_as_degraded_no_raise() -> None:
    """If probe_fn itself raises, the outer wrapper MUST catch and
    classify as a transport-level failure. The preflight orchestrator
    MUST NOT propagate exceptions from individual probes — that
    would defeat the fail-fast boundary's structured halt."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome, PreflightVerdict,
    )

    async def stub(mid):
        raise RuntimeError("simulated probe substrate failure")

    report = await run_preflight(
        model_ids=("Qwen-397B",),
        probe_fn=stub,
        halt_on_all_fail=False,
    )
    assert report.total_probed == 1
    # status_code=0 + no timeout flag → DEGRADED_5XX (transport)
    result = report.results[0]
    assert result.verdict is PreflightVerdict.DEGRADED_5XX
    assert "probe_raised" in result.diagnostic


@pytest.mark.asyncio
async def test_spine_mixed_outcomes_route_independently() -> None:
    """End-to-end: 4 models, each landing in a different verdict bucket.
    Verifies side-effects route independently (entitlement → ledger,
    5xx/timeout → sentinel, error_other → no side-effect).

    This is the v18 scenario in microcosm: 397B succeeds, 4B is
    entitlement-blocked, 35B is 5xx, Kimi times out."""
    from backend.core.ouroboros.governance.preflight_probe import (
        run_preflight, ProbeOutcome, PreflightVerdict,
    )

    async def stub(mid):
        if mid == "Qwen-397B":
            return ProbeOutcome(model_id=mid, success=True, status_code=200)
        if mid == "Qwen-4B":
            return ProbeOutcome(
                model_id=mid, success=False, status_code=403,
                error_body="blocked by a routing rule",
            )
        if mid == "Qwen-35B":
            return ProbeOutcome(
                model_id=mid, success=False, status_code=502,
                error_message="bad gateway",
            )
        if mid == "Kimi":
            await asyncio.sleep(0.5)  # forces timeout
            return ProbeOutcome(model_id=mid, success=False)
        raise AssertionError(f"unexpected model_id {mid}")

    mock_ledger = mock.MagicMock()
    mock_sentinel = mock.MagicMock()
    report = await run_preflight(
        model_ids=("Qwen-397B", "Qwen-4B", "Qwen-35B", "Kimi"),
        probe_fn=stub,
        ledger=mock_ledger,
        sentinel=mock_sentinel,
        timeout_per_model_s=0.1,
        halt_on_all_fail=False,
    )
    assert report.active_count == 1
    assert report.demoted_entitlement_count == 1
    assert report.degraded_5xx_count == 1
    assert report.degraded_timeout_count == 1
    assert report.all_failed is False  # at least one ACTIVE

    # Ledger demote called exactly once for the entitlement-blocked model
    assert mock_ledger.demote.call_count == 1
    assert mock_ledger.demote.call_args.args[0] == "Qwen-4B"

    # Sentinel report_failure called for BOTH 5xx and timeout (2 calls)
    assert mock_sentinel.report_failure.call_count == 2
    sentinel_model_ids = {
        c.args[0] for c in mock_sentinel.report_failure.call_args_list
    }
    assert sentinel_model_ids == {"Qwen-35B", "Kimi"}
