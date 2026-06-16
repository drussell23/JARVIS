"""Unit C — substrate wiring: Sovereign Arbiter + Unified Evidence Row.

Proves the live_fire_soak substrate:
  * captures a bounded-but-large debug.log slice (env-tunable, no hardcoded
    8192) so the self-heal trajectory is parse-able;
  * gates ALL new behavior behind JARVIS_LIVE_FIRE_TELEMETRY_ARBITER_ENABLED
    (default off ⇒ byte-identical: no telemetry field, no parse call);
  * when on, threads harvester Metrics through GraduationContract.arbitrate
    and synthesizes a Unified Evidence Row (legacy classification wrapped in
    harvester trajectory context).

TDD red: written before the wiring + _debug_log_capture_bytes exist.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from backend.core.ouroboros.governance.adaptation import (
    graduation_ledger as _ledger_mod,
)
from backend.core.ouroboros.governance.graduation.live_fire_soak import (
    HarnessStatus,
    LiveFireSoakHarness,
    get_default_harness,
    reset_default_harness,
)

_FULL_RECOVERY_LOG = (
    "phase=GENERATE op=1\n"
    "[LiveFire] candidate FAILED live-fire boot: ImportError\n"
    "routed back failure_class=build op=1\n"
    "GENERATE_RETRY op=1\n"
    "op=1 state=applied phase=COMPLETE\n"
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for k in list(os.environ.keys()):
        if (
            k.startswith("JARVIS_LIVE_FIRE_")
            or k.startswith("JARVIS_GRADUATION_LEDGER_")
        ):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_PATH",
        str(tmp_path / "ledger.jsonl"),
    )
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_HISTORY_PATH",
        str(tmp_path / "history.jsonl"),
    )
    monkeypatch.setenv("JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true")
    _ledger_mod.reset_default_ledger()
    reset_default_harness()
    yield
    _ledger_mod.reset_default_ledger()
    reset_default_harness()


def _runner(summary: Dict[str, Any], debug_text: str = ""):
    def runner(
        *, env, cost_cap_usd, max_wall_seconds, timeout_s, project_root,
    ) -> Tuple[int, Dict[str, Any], str]:
        return (0, summary, debug_text)
    return runner


def _harness() -> LiveFireSoakHarness:
    return get_default_harness()


# --- master-off byte-identical ---------------------------------------------
def test_master_off_no_telemetry_field(monkeypatch):
    # Arbiter flag unset (default off). Even with a recovery trajectory in
    # the log, no telemetry is parsed/attached.
    result = _harness().run_soak(
        flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
        subprocess_runner=_runner(
            {
                "session_id": "bt-off-1",
                "session_outcome": "complete",
                "stop_reason": "ok",
                "failure_class_counts": {},
            },
            _FULL_RECOVERY_LOG,
        ),
    )
    assert result.status == HarnessStatus.OK
    assert result.evidence is not None
    assert getattr(result.evidence, "telemetry", "MISSING") in (None, "MISSING")
    assert "telemetry" not in result.evidence.to_dict()


def test_contract_on_arbiter_off_is_legacy_path(monkeypatch):
    # Contract consultation on, arbiter OFF -> legacy summary-only path,
    # still no telemetry field.
    monkeypatch.setenv("JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", "true")
    result = _harness().run_soak(
        flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
        subprocess_runner=_runner(
            {
                "session_id": "bt-off-2",
                "session_outcome": "complete",
                "stop_reason": "ok",
                "failure_class_counts": {},
            },
            _FULL_RECOVERY_LOG,
        ),
    )
    assert result.evidence is not None
    assert "telemetry" not in result.evidence.to_dict()


# --- master-on: arbiter recovery override ----------------------------------
def _enable_arbiter(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", "true")
    monkeypatch.setenv("JARVIS_LIVE_FIRE_TELEMETRY_ARBITER_ENABLED", "true")


def test_arbiter_recovery_override_promotes_infra_to_clean(monkeypatch):
    _enable_arbiter(monkeypatch)
    # Legacy classifies incomplete_kill as INFRA; metrics prove the system
    # caught + healed -> arbiter overrides to CLEAN.
    result = _harness().run_soak(
        flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
        subprocess_runner=_runner(
            {
                "session_id": "bt-heal-1",
                "session_outcome": "incomplete_kill",
                "stop_reason": "sigterm",
                "failure_class_counts": {},
            },
            _FULL_RECOVERY_LOG,
        ),
    )
    assert result.status == HarnessStatus.OK
    assert result.evidence is not None
    assert result.evidence.outcome == "clean"
    tel = result.evidence.to_dict().get("telemetry")
    assert tel is not None
    assert tel["recovered"] is True
    assert tel["livefire_fired"] == ["ImportError"]
    assert tel["legacy_outcome"] == "infra"
    assert tel["arbiter_changed_outcome"] is True
    # Ledger recorded a clean session.
    progress = _ledger_mod.get_default_ledger().progress(
        "JARVIS_HYPOTHESIS_PROBE_ENABLED",
    )
    assert progress["clean"] == 1


def test_arbiter_anomaly_oom_blocks_clean(monkeypatch):
    _enable_arbiter(monkeypatch)
    log = _FULL_RECOVERY_LOG + "process_memory_cap exceeded MemoryError\n"
    result = _harness().run_soak(
        flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
        subprocess_runner=_runner(
            {
                "session_id": "bt-oom-1",
                "session_outcome": "complete",
                "stop_reason": "ok",
                "failure_class_counts": {},
            },
            log,
        ),
    )
    assert result.evidence is not None
    assert result.evidence.outcome == "infra"  # OOM waiver, never clean
    tel = result.evidence.to_dict().get("telemetry")
    assert tel is not None and tel["oom"] is True


def test_arbiter_clean_run_persists_full_trajectory(monkeypatch):
    _enable_arbiter(monkeypatch)
    result = _harness().run_soak(
        flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
        subprocess_runner=_runner(
            {
                "session_id": "bt-clean-tel",
                "session_outcome": "complete",
                "stop_reason": "ok",
                "failure_class_counts": {},
            },
            _FULL_RECOVERY_LOG,
        ),
    )
    assert result.evidence is not None
    assert result.evidence.outcome == "clean"
    tel = result.evidence.to_dict()["telemetry"]
    assert tel["routed_build"] is True
    assert tel["retried"] is True
    assert tel["recovered"] is True
    assert tel["oom"] is False


# --- capstone wiring -------------------------------------------------------
def test_capstone_downgrades_clean_without_livefire_evidence(monkeypatch):
    _enable_arbiter(monkeypatch)
    # Capstone flag, legacy clean, but NO live-fire in the log -> the
    # Metrics-aware capstone predicate downgrades CLEAN -> RUNNER.
    result = _harness().run_soak(
        flag_name="JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED",
        subprocess_runner=_runner(
            {
                "session_id": "bt-cap-1",
                "session_outcome": "complete",
                "stop_reason": "ok",
                "failure_class_counts": {},
                "strategic_drift": {"total_ops": 4},
            },
            "phase=GENERATE op=1\nop=1 state=applied\n",  # no [LiveFire]
        ),
    )
    assert result.evidence is not None
    assert result.evidence.outcome == "runner"
    assert "contract_metrics_predicate_downgraded" in result.evidence.notes


# --- env-tunable capture bytes ---------------------------------------------
def test_debug_log_capture_bytes_default_and_override_and_bounds(monkeypatch):
    from backend.core.ouroboros.governance.graduation.live_fire_soak import (
        _debug_log_capture_bytes,
    )
    monkeypatch.delenv(
        "JARVIS_LIVE_FIRE_DEBUG_LOG_CAPTURE_BYTES", raising=False,
    )
    default = _debug_log_capture_bytes()
    assert default >= 8192  # at least the legacy tail
    assert default >= 256 * 1024  # generous enough for the trajectory

    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_DEBUG_LOG_CAPTURE_BYTES", "500000",
    )
    assert _debug_log_capture_bytes() == 500000

    # Garbage -> default; absurd low -> clamped to >= 8192.
    monkeypatch.setenv("JARVIS_LIVE_FIRE_DEBUG_LOG_CAPTURE_BYTES", "nope")
    assert _debug_log_capture_bytes() == default
    monkeypatch.setenv("JARVIS_LIVE_FIRE_DEBUG_LOG_CAPTURE_BYTES", "10")
    assert _debug_log_capture_bytes() >= 8192
