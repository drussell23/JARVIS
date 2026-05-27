"""Slice 29 — Preflight Backoff Daemon (Inside Preflight, Shape A).

Closes the v22 (bt-2026-05-27-034646) fragility: a transient
status=0 transport blip across all 3 trusted DW models terminated
boot via PreflightAllFailedError. True background daemon architectures
wait out transient upstream congestion with exponential backoff.

Per Slice 29 operator directive — Shape A (Daemon Inside Preflight):

  When ``JARVIS_PREFLIGHT_RECOVERY_DAEMON_ENABLED=true`` (default
  TRUE), ``run_boot_preflight`` wraps ``run_preflight`` in an
  exponential-backoff polling loop. On every iteration:

    * If ``active_count >= 1`` → break loop, log recovery, return
    * If ``active_count == 0`` → log heartbeat, sleep backoff,
      double backoff (capped at 300s)

  Operator opt-out: ``=false`` restores byte-identical pre-Slice-29
  fail-fast PreflightAllFailedError behavior.

Operator-attested verbatim log messages (AST-pinned):
  Heartbeat: "[PreflightDaemon] Active provider fleet empty.
             Entering backoff cycle. Next probe attempt in X seconds."
  Recovery:  "[PreflightDaemon] Upstream line recovery confirmed.
             Un-pausing agent pool and executing delayed boot-strap
             component initialization."

# Test surface (3 AST pins + 9 spine)
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PF_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "preflight_probe.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_slice29_substrate_present() -> None:
    """The Slice 29 substrate (helper + 5 env constants + 4 defaults)
    MUST be in place."""
    src = PF_FILE.read_text()
    assert "Slice 29" in src, (
        "preflight_probe missing Slice 29 attribution"
    )
    for sym in (
        "_ENV_DAEMON_ENABLED",
        "_ENV_DAEMON_BASE_S",
        "_ENV_DAEMON_MULTIPLIER",
        "_ENV_DAEMON_MAX_BACKOFF_S",
        "_ENV_DAEMON_MAX_ATTEMPTS",
        "_DAEMON_BASE_BACKOFF_S_DEFAULT",
        "_DAEMON_BACKOFF_MULTIPLIER_DEFAULT",
        "_DAEMON_MAX_BACKOFF_S_DEFAULT",
        "_DAEMON_MAX_ATTEMPTS_DEFAULT",
        "_is_recovery_daemon_enabled",
        "_run_preflight_with_recovery_daemon",
    ):
        assert sym in src, f"Slice 29 symbol {sym!r} missing"
    # Operator-spec defaults
    assert "_DAEMON_BASE_BACKOFF_S_DEFAULT = 30.0" in src
    assert "_DAEMON_BACKOFF_MULTIPLIER_DEFAULT = 2.0" in src
    assert "_DAEMON_MAX_BACKOFF_S_DEFAULT = 300.0" in src


def test_ast_pin_verbatim_heartbeat_and_recovery_messages() -> None:
    """The operator-attested log messages MUST be present verbatim
    inside ``_run_preflight_with_recovery_daemon`` body (AST-walk
    string-literal join handles Python's compile-time concat)."""
    src = PF_FILE.read_text()
    tree = ast.parse(src, filename=str(PF_FILE))
    body_src = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_run_preflight_with_recovery_daemon"
        ):
            body_src = ast.unparse(node)
            break
    assert body_src, (
        "_run_preflight_with_recovery_daemon function not found"
    )
    # Operator-attested verbatim clauses — heartbeat
    heartbeat_required = [
        "Active provider fleet empty",
        "Entering backoff cycle",
        "Next probe attempt in",
    ]
    for clause in heartbeat_required:
        assert clause in body_src, (
            f"Heartbeat message missing operator clause: {clause!r}"
        )
    # Operator-attested verbatim clauses — recovery
    recovery_required = [
        "Upstream line recovery confirmed",
        "Un-pausing agent pool",
        "executing delayed boot-strap component initialization",
    ]
    for clause in recovery_required:
        assert clause in body_src, (
            f"Recovery message missing operator clause: {clause!r}"
        )


def test_ast_pin_run_boot_preflight_dispatches_to_daemon() -> None:
    """``run_boot_preflight`` MUST dispatch to the daemon path when
    ``_is_recovery_daemon_enabled()`` returns True. Without this
    wiring, the daemon substrate is dead code."""
    src = PF_FILE.read_text()
    tree = ast.parse(src, filename=str(PF_FILE))
    body_src = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "run_boot_preflight"
        ):
            body_src = ast.unparse(node)
            break
    assert body_src, "run_boot_preflight not found"
    assert "_is_recovery_daemon_enabled" in body_src
    assert "_run_preflight_with_recovery_daemon" in body_src


# ──────────────────────────────────────────────────────────────────────
# Master flag spine — 2
# ──────────────────────────────────────────────────────────────────────


def test_spine_master_flag_defaults_true(monkeypatch) -> None:
    """Per operator directive ('un-killable background asset'), the
    master flag defaults TRUE so v23+ inherits the daemon behavior
    without needing explicit env setting."""
    monkeypatch.delenv("JARVIS_PREFLIGHT_RECOVERY_DAEMON_ENABLED", raising=False)
    from backend.core.ouroboros.governance.preflight_probe import (
        _is_recovery_daemon_enabled,
    )
    assert _is_recovery_daemon_enabled() is True


def test_spine_master_flag_explicit_off_disables_daemon(monkeypatch) -> None:
    """Operator opt-out: ``=false`` restores legacy fail-fast path."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_RECOVERY_DAEMON_ENABLED", "false")
    from backend.core.ouroboros.governance.preflight_probe import (
        _is_recovery_daemon_enabled,
    )
    assert _is_recovery_daemon_enabled() is False


# ──────────────────────────────────────────────────────────────────────
# Backoff + recovery spine — 7
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_recovery_on_first_attempt_no_backoff(
    monkeypatch, caplog,
) -> None:
    """When the first probe succeeds, daemon returns immediately
    without sleeping. Verifies the fast-path (no upstream blip)."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_BASE_BACKOFF_S", "0.01")
    from backend.core.ouroboros.governance.preflight_probe import (
        _run_preflight_with_recovery_daemon, ProbeOutcome,
    )

    async def stub(mid):
        return ProbeOutcome(model_id=mid, success=True, status_code=200)

    with caplog.at_level(logging.INFO):
        report = await _run_preflight_with_recovery_daemon(
            model_ids=("Qwen-397B",),
            probe_fn=stub,
            ledger=None,
            sentinel=None,
        )

    assert report.active_count == 1
    # NO heartbeat should fire (first attempt succeeded)
    heartbeats = [
        r for r in caplog.records
        if "Active provider fleet empty" in r.getMessage()
    ]
    assert len(heartbeats) == 0, (
        f"Heartbeat fired unexpectedly on first-attempt success: "
        f"{len(heartbeats)}"
    )
    # Recovery message MUST fire
    recoveries = [
        r for r in caplog.records
        if "Upstream line recovery confirmed" in r.getMessage()
    ]
    assert len(recoveries) == 1


@pytest.mark.asyncio
async def test_spine_recovery_after_two_backoff_cycles(
    monkeypatch, caplog,
) -> None:
    """Operator-spec test: simulate transient blackout that clears
    after 2 backoff steps. First 2 attempts fail (heartbeats fire);
    3rd attempt succeeds (recovery fires + returns clean report)."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_BASE_BACKOFF_S", "0.05")
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_MAX_BACKOFF_S", "0.2")
    from backend.core.ouroboros.governance.preflight_probe import (
        _run_preflight_with_recovery_daemon, ProbeOutcome,
    )

    attempt_count = {"n": 0}

    async def stub(mid):
        # Each call to run_preflight calls stub for each model.
        # 1 model × 3 attempts: first 2 attempts fail, 3rd succeeds.
        attempt_count["n"] += 1
        succeed = attempt_count["n"] >= 3
        return ProbeOutcome(
            model_id=mid, success=succeed,
            status_code=200 if succeed else 503,
        )

    with caplog.at_level(logging.INFO):
        report = await _run_preflight_with_recovery_daemon(
            model_ids=("Qwen-397B",),
            probe_fn=stub,
            ledger=None,
            sentinel=None,
        )

    assert report.active_count == 1
    # 2 heartbeats should fire (between attempts 1-2 and 2-3)
    heartbeats = [
        r for r in caplog.records
        if "Active provider fleet empty" in r.getMessage()
    ]
    assert len(heartbeats) == 2, (
        f"Expected 2 heartbeats for 2-cycle recovery, got {len(heartbeats)}"
    )
    # Exactly 1 recovery message
    recoveries = [
        r for r in caplog.records
        if "Upstream line recovery confirmed" in r.getMessage()
    ]
    assert len(recoveries) == 1


@pytest.mark.asyncio
async def test_spine_exponential_backoff_sleep_progression(
    monkeypatch,
) -> None:
    """Backoff doubles per cycle: 30 → 60 → 120 → 240 → 300 (capped)
    at default settings. Verify by spying on asyncio.sleep durations."""
    monkeypatch.delenv("JARVIS_PREFLIGHT_DAEMON_BASE_BACKOFF_S", raising=False)
    monkeypatch.delenv("JARVIS_PREFLIGHT_DAEMON_MULTIPLIER", raising=False)
    monkeypatch.delenv("JARVIS_PREFLIGHT_DAEMON_MAX_BACKOFF_S", raising=False)
    from backend.core.ouroboros.governance.preflight_probe import (
        _run_preflight_with_recovery_daemon, ProbeOutcome,
    )
    # Spy on asyncio.sleep
    sleep_durations: list = []
    original_sleep = asyncio.sleep

    async def spy_sleep(s):
        sleep_durations.append(s)
        await original_sleep(0)  # don't actually sleep

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.preflight_probe.asyncio.sleep",
        spy_sleep,
    )

    attempt_count = {"n": 0}

    async def stub(mid):
        # Fail for 6 attempts, then succeed → 5 backoff cycles
        attempt_count["n"] += 1
        succeed = attempt_count["n"] >= 7
        return ProbeOutcome(
            model_id=mid, success=succeed,
            status_code=200 if succeed else 503,
        )

    await _run_preflight_with_recovery_daemon(
        model_ids=("Qwen-397B",),
        probe_fn=stub,
        ledger=None,
        sentinel=None,
    )

    # Expected backoff sequence (6 sleeps for 6-fail-then-succeed
    # on attempt 7): each failed attempt is followed by a sleep, so
    # 6 failures → 6 sleeps; the 7th attempt succeeds without sleep.
    # Doubling: 30 → 60 → 120 → 240 → 300 (cap) → 300 (still cap)
    expected = [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    assert sleep_durations == expected, (
        f"Backoff progression broken: expected {expected}, got {sleep_durations}"
    )


@pytest.mark.asyncio
async def test_spine_backoff_ceiling_enforced(monkeypatch) -> None:
    """The 300s ceiling MUST clamp — even after 10 doublings,
    backoff stays at max_backoff_s."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_BASE_BACKOFF_S", "100")
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_MAX_BACKOFF_S", "150")
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_BACKOFF_MULTIPLIER", "10.0")
    from backend.core.ouroboros.governance.preflight_probe import (
        _run_preflight_with_recovery_daemon, ProbeOutcome,
    )

    sleep_durations: list = []
    original_sleep = asyncio.sleep

    async def spy_sleep(s):
        sleep_durations.append(s)
        await original_sleep(0)

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.preflight_probe.asyncio.sleep",
        spy_sleep,
    )

    attempt_count = {"n": 0}

    async def stub(mid):
        attempt_count["n"] += 1
        succeed = attempt_count["n"] >= 5
        return ProbeOutcome(
            model_id=mid, success=succeed,
            status_code=200 if succeed else 503,
        )

    await _run_preflight_with_recovery_daemon(
        model_ids=("Qwen-397B",),
        probe_fn=stub, ledger=None, sentinel=None,
    )
    # 4 sleeps; base=100, ×10 → 1000 → cap 150. So:
    # First sleep: 100 (base). Then min(100×10, 150)=150 for all subsequent.
    assert sleep_durations == [100.0, 150.0, 150.0, 150.0], (
        f"Ceiling not enforced: {sleep_durations}"
    )


@pytest.mark.asyncio
async def test_spine_max_attempts_raises_preflight_all_failed(
    monkeypatch,
) -> None:
    """When max_attempts is reached without recovery,
    PreflightAllFailedError raises (operator-configured give-up)."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_BASE_BACKOFF_S", "0.01")
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_MAX_BACKOFF_S", "0.01")
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_MAX_ATTEMPTS", "3")
    from backend.core.ouroboros.governance.preflight_probe import (
        _run_preflight_with_recovery_daemon, ProbeOutcome,
        PreflightAllFailedError,
    )

    async def stub(mid):
        return ProbeOutcome(model_id=mid, success=False, status_code=503)

    with pytest.raises(PreflightAllFailedError) as excinfo:
        await _run_preflight_with_recovery_daemon(
            model_ids=("Qwen-397B",),
            probe_fn=stub, ledger=None, sentinel=None,
        )
    # Report should reflect the final (all-fail) probe outcome
    assert excinfo.value.report.all_failed is True


@pytest.mark.asyncio
async def test_spine_master_flag_off_preserves_legacy_fail_fast(
    monkeypatch,
) -> None:
    """When daemon disabled, ``run_boot_preflight`` invokes
    ``run_preflight`` with halt_on_all_fail=True, which raises
    PreflightAllFailedError on all-fail. Byte-identical pre-Slice-29."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_RECOVERY_DAEMON_ENABLED", "false")
    monkeypatch.setenv("JARVIS_PREFLIGHT_PROBE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_TRUSTED_MODELS", "Qwen-397B")
    monkeypatch.setenv(
        "JARVIS_DW_PROMOTION_LEDGER_PATH",
        "/tmp/claude/test_slice29_legacy_ledger.json",
    )
    # Clean ledger
    from pathlib import Path
    Path("/tmp/claude/test_slice29_legacy_ledger.json").unlink(missing_ok=True)

    from backend.core.ouroboros.governance.preflight_probe import (
        run_boot_preflight, PreflightAllFailedError,
    )

    # Fake DW provider
    fake_provider = mock.MagicMock()
    fake_provider._get_session = mock.AsyncMock(return_value=mock.MagicMock())
    fake_provider._base_url = "https://test.example/v1"
    fake_provider._api_key = "test-key"

    class _FailingProber:
        async def probe(self, *, session, model_id, base_url, api_key, **kw):
            from backend.core.ouroboros.governance.dw_heavy_probe import (
                HeavyProbeResult,
            )
            return HeavyProbeResult(
                model_id=model_id, success=False,
                ttft_ms=10000, total_latency_ms=10000, cost_usd=0.0,
                error="status_503:upstream blackout",
            )

    with pytest.raises(PreflightAllFailedError):
        await run_boot_preflight(
            dw_provider=fake_provider,
            prober_factory=_FailingProber,
        )


@pytest.mark.asyncio
async def test_spine_recovery_log_includes_attempt_count(
    monkeypatch, caplog,
) -> None:
    """Recovery log line MUST carry the attempt counter so postmortem
    can reconstruct how long the upstream blackout persisted."""
    monkeypatch.setenv("JARVIS_PREFLIGHT_DAEMON_BASE_BACKOFF_S", "0.01")
    from backend.core.ouroboros.governance.preflight_probe import (
        _run_preflight_with_recovery_daemon, ProbeOutcome,
    )

    attempt_count = {"n": 0}

    async def stub(mid):
        attempt_count["n"] += 1
        succeed = attempt_count["n"] >= 4  # 3 fails, 4th succeeds
        return ProbeOutcome(
            model_id=mid, success=succeed,
            status_code=200 if succeed else 503,
        )

    with caplog.at_level(logging.INFO):
        await _run_preflight_with_recovery_daemon(
            model_ids=("Qwen-397B",),
            probe_fn=stub, ledger=None, sentinel=None,
        )

    recovery_msgs = [
        r.getMessage() for r in caplog.records
        if "Upstream line recovery confirmed" in r.getMessage()
    ]
    assert len(recovery_msgs) == 1
    # attempt=4 since 3 failed + 4th succeeded
    assert "attempt=4" in recovery_msgs[0], (
        f"Recovery log missing attempt counter: {recovery_msgs[0]!r}"
    )
