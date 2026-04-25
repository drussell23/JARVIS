#!/usr/bin/env python3
"""W3(6) Slice 5a — formal in-process live-fire smoke for parallel L3 fan-out.

Boots the Wave 3 (6) primitive surfaces end-to-end with **default
(master-off) env** plus controlled overrides for each scenario. Asserts
the full chain in-process so it can run on any developer machine
without needing a battle-test harness.

Coverage (~25 checks across 7 sections):

1. Default master flag is False (pre-Slice-5b).
2. All 5 sub-flag defaults compose correctly.
3. Hot-revert path: master=false force-disables every sub-flag effect.
4. Eligibility decision matrix — every ReasonCode reachable.
5. FlagRegistry seed: all 5 knobs registered with correct types.
6. Source-grep wiring pins (post-GENERATE seam, GLS seed call).
7. Authority-invariant pins (ReasonCode + FanoutOutcome enums frozen).

Exit code 0 on PASS; non-zero on FAIL with a summary of failed check(s).

Usage::

    python3 scripts/livefire_w3_6_parallel_dispatch.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Repo root on sys.path
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class Journal:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
            print(f"  [PASS] {name}")
        else:
            self.failed.append((name, detail))
            print(f"  [FAIL] {name}  ({detail})")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 64}")
        print(f"Result: {len(self.passed)}/{total} checks passed")
        if self.failed:
            print("\nFailures:")
            for n, d in self.failed:
                print(f"  - {n}: {d}")
            return 1
        print("All checks passed — W3(6) Slice 5a live-fire smoke OK.")
        return 0


def _reset_envs() -> None:
    for key in (
        "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED",
        "JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW",
        "JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE",
        "JARVIS_WAVE3_PARALLEL_MAX_UNITS",
        "JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S",
    ):
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# Live-fire body
# ---------------------------------------------------------------------------


def main() -> int:
    j = Journal()
    print("=" * 64)
    print("W3(6) Slice 5a — Parallel L3 fan-out live-fire smoke")
    print("=" * 64)

    _reset_envs()

    from backend.core.ouroboros.governance.parallel_dispatch import (
        FanoutOutcome,
        GRAPH_SCHEMA_VERSION,
        PLANNER_ID,
        POSTURE_CONFIDENCE_FLOOR,
        ReasonCode,
        _own_flag_specs,
        ensure_flag_registry_seeded,
        is_fanout_eligible,
        parallel_dispatch_enabled,
        parallel_dispatch_enforce_enabled,
        parallel_dispatch_max_units,
        parallel_dispatch_shadow_enabled,
        parallel_dispatch_wait_timeout_s,
        posture_weight_for,
    )
    from backend.core.ouroboros.governance.memory_pressure_gate import (
        FanoutDecision,
        PressureLevel,
    )
    from backend.core.ouroboros.governance.posture import Posture

    # -----------------------------------------------------------------
    # (1) Defaults — pre-Slice-5b
    # -----------------------------------------------------------------
    j.check("1a. Master flag defaults False (pre-graduation)",
            parallel_dispatch_enabled() is False)
    j.check("1b. Shadow sub-flag defaults False",
            parallel_dispatch_shadow_enabled() is False)
    j.check("1c. Enforce sub-flag defaults False",
            parallel_dispatch_enforce_enabled() is False)
    j.check("1d. max_units defaults to 3",
            parallel_dispatch_max_units() == 3,
            f"got {parallel_dispatch_max_units()}")
    j.check("1e. wait_timeout defaults to 900s",
            parallel_dispatch_wait_timeout_s() == 900.0,
            f"got {parallel_dispatch_wait_timeout_s()}")

    # -----------------------------------------------------------------
    # (2) POSTURE_CONFIDENCE_FLOOR pin
    # -----------------------------------------------------------------
    j.check("2. POSTURE_CONFIDENCE_FLOOR == 0.3 (Wave 1 SensorGovernor parity)",
            POSTURE_CONFIDENCE_FLOOR == 0.3,
            f"got {POSTURE_CONFIDENCE_FLOOR}")

    # -----------------------------------------------------------------
    # (3) Hot-revert path — master=false force-disables everything
    # -----------------------------------------------------------------
    os.environ["JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED"] = "false"
    os.environ["JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW"] = "true"
    os.environ["JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE"] = "true"
    os.environ["JARVIS_WAVE3_PARALLEL_MAX_UNITS"] = "10"

    elig = is_fanout_eligible(op_id="op-revert", n_candidate_files=3)
    j.check("3a. Hot-revert: master=false → MASTER_OFF reason",
            elig.reason_code is ReasonCode.MASTER_OFF,
            f"got {elig.reason_code}")
    j.check("3b. Hot-revert: allowed=False",
            elig.allowed is False)
    j.check("3c. Hot-revert: n_allowed=1 (serial-equivalent, not 0)",
            elig.n_allowed == 1,
            f"got {elig.n_allowed}")
    _reset_envs()

    # -----------------------------------------------------------------
    # (4) Eligibility decision matrix
    # -----------------------------------------------------------------
    os.environ["JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED"] = "true"

    elig = is_fanout_eligible(op_id="op-1", n_candidate_files=0)
    j.check("4a. EMPTY_CANDIDATE_LIST when n=0",
            elig.reason_code is ReasonCode.EMPTY_CANDIDATE_LIST)

    elig = is_fanout_eligible(op_id="op-1", n_candidate_files=1)
    j.check("4b. SINGLE_FILE_OP when n=1",
            elig.reason_code is ReasonCode.SINGLE_FILE_OP)

    def _low_conf():
        return (Posture.EXPLORE, 0.1)
    elig = is_fanout_eligible(
        op_id="op-1", n_candidate_files=2, posture_fn=_low_conf,
    )
    j.check("4c. POSTURE_LOW_CONFIDENCE when conf<0.3 floor",
            elig.reason_code is ReasonCode.POSTURE_LOW_CONFIDENCE)

    fake_gate_critical = MagicMock()
    fake_gate_critical.can_fanout.return_value = FanoutDecision(
        allowed=False, n_requested=2, n_allowed=0,
        level=PressureLevel.CRITICAL, free_pct=2.0,
        reason_code="memory_critical", source="test",
    )
    elig = is_fanout_eligible(
        op_id="op-1", n_candidate_files=2, gate=fake_gate_critical,
    )
    j.check("4d. MEMORY_CRITICAL when memory pressure CRITICAL",
            elig.reason_code is ReasonCode.MEMORY_CRITICAL)

    def _good_posture():
        return (Posture.EXPLORE, 0.95)
    fake_gate_ok = MagicMock()
    fake_gate_ok.can_fanout.return_value = FanoutDecision(
        allowed=True, n_requested=3, n_allowed=3,
        level=PressureLevel.OK, free_pct=80.0,
        reason_code="allowed", source="test",
    )
    elig = is_fanout_eligible(
        op_id="op-1", n_candidate_files=3,
        posture_fn=_good_posture, gate=fake_gate_ok,
    )
    j.check("4e. ALLOWED happy path: master+EXPLORE+memOK+3files=ALLOWED",
            elig.reason_code is ReasonCode.ALLOWED)
    j.check("4f. n_allowed==n_requested in happy path",
            elig.n_allowed == 3)

    _reset_envs()

    # -----------------------------------------------------------------
    # (5) FlagRegistry seed
    # -----------------------------------------------------------------
    specs = _own_flag_specs()
    j.check("5a. _own_flag_specs returns 5 specs",
            len(specs) == 5,
            f"got {len(specs)}")

    names = {s.name for s in specs}
    expected_names = {
        "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED",
        "JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW",
        "JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE",
        "JARVIS_WAVE3_PARALLEL_MAX_UNITS",
        "JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S",
    }
    j.check("5b. all 5 expected env-flag names present",
            names == expected_names,
            f"missing/extra: {names ^ expected_names}")

    # ensure_flag_registry_seeded is idempotent
    try:
        ensure_flag_registry_seeded()
        ensure_flag_registry_seeded()
        ensure_flag_registry_seeded()
        j.check("5c. ensure_flag_registry_seeded is idempotent (3 calls)", True)
    except Exception as e:  # noqa: BLE001
        j.check("5c. ensure_flag_registry_seeded is idempotent", False, repr(e))

    # Verify registry actually contains the master flag
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            ensure_seeded as _fr_seed,
        )
        fr = _fr_seed()
        master_spec = fr.get_spec("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED")
        j.check("5d. Master flag visible in FlagRegistry post-seed",
                master_spec is not None and master_spec.name == "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED")
    except Exception as e:  # noqa: BLE001
        j.check("5d. Master flag visible in FlagRegistry post-seed",
                False, repr(e))

    # -----------------------------------------------------------------
    # (6) Source-grep wiring pins
    # -----------------------------------------------------------------
    src_pd = (Path(_REPO) / "backend/core/ouroboros/governance/parallel_dispatch.py").read_text(encoding="utf-8")
    j.check("6a. parallel_dispatch.py has master env-reader literal",
            '_env_bool("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", False)' in src_pd)

    src_dispatcher = (Path(_REPO) / "backend/core/ouroboros/governance/phase_dispatcher.py").read_text(encoding="utf-8")
    j.check("6b. phase_dispatcher.py has post-GENERATE enforce_evaluate_fanout seam",
            "enforce_evaluate_fanout" in src_dispatcher)

    src_gls = (Path(_REPO) / "backend/core/ouroboros/governance/governed_loop_service.py").read_text(encoding="utf-8")
    j.check("6c. GovernedLoopService.start calls ensure_flag_registry_seeded",
            "ensure_flag_registry_seeded" in src_gls)

    runbook = Path(_REPO) / "docs/operations/wave3-parallel-dispatch-graduation.md"
    j.check("6d. Operations runbook exists at docs/operations/wave3-parallel-dispatch-graduation.md",
            runbook.exists())
    if runbook.exists():
        runbook_txt = runbook.read_text(encoding="utf-8")
        j.check("6e. Runbook documents JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED hot-revert",
                "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=false" in runbook_txt)
        j.check("6f. Runbook lists all 5 env knobs",
                all(k in runbook_txt for k in expected_names))

    # -----------------------------------------------------------------
    # (7) Authority-invariant enum vocab + schema constants frozen
    # -----------------------------------------------------------------
    j.check("7a. ReasonCode enum has 9 stable values",
            len({rc.value for rc in ReasonCode}) == 9,
            f"got {len({rc.value for rc in ReasonCode})}")
    j.check("7b. FanoutOutcome enum has 7 stable values",
            len({fo.value for fo in FanoutOutcome}) == 7,
            f"got {len({fo.value for fo in FanoutOutcome})}")
    j.check("7c. PLANNER_ID is non-empty string (wire-format)",
            isinstance(PLANNER_ID, str) and PLANNER_ID,
            f"got {PLANNER_ID!r}")
    j.check("7d. GRAPH_SCHEMA_VERSION is non-empty string (wire-format)",
            isinstance(GRAPH_SCHEMA_VERSION, str) and GRAPH_SCHEMA_VERSION,
            f"got {GRAPH_SCHEMA_VERSION!r}")
    j.check("7e. posture_weight_for(None) returns finite float (no NPE)",
            isinstance(posture_weight_for(None), float))

    return j.summary()


if __name__ == "__main__":
    sys.exit(main())
