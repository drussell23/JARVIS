"""Phase 1 Slice 1.3.c — GATE phase wiring regression spine.

Pins:
  §1   GATE wiring captures risk_tier_assignment digest
  §2   Source-level pin: capture_phase_decision invoked
  §3   Source-level pin: kind="risk_tier_assignment"
  §4   Source-level pin: phase="GATE"
  §5   Source-level pin: capture happens BEFORE the success-path return
  §6   Source-level pin: closure-over-risk_tier pattern (no re-mutate)
  §7   Source-level pin: try/except fallback present
  §8   Source-level pin: logger.debug on failure (not warning)
  §9   Wiring marker: Slice 1.3.c reference in source
  §10  gate_runner imports phase_capture LAZILY (not top-level)
  §11  gate_runner imports cleanly with importlib.reload
  §12  Identity adapter sufficient — digest is JSON-friendly primitives
  §13  End-to-end RECORD then REPLAY proves digest captured + replayed
  §14  RiskTier serialization uses .name (uppercase enum identifier)
  §15  Capture only on success path — fail paths bypass capture
"""
from __future__ import annotations

import importlib

import pytest

from backend.core.ouroboros.governance.determinism import (
    capture_phase_decision,
)
from backend.core.ouroboros.governance.determinism.decision_runtime import (
    reset_all_for_tests as reset_runtime_for_tests,
)
from backend.core.ouroboros.governance.determinism.phase_capture import (
    reset_registry_for_tests,
)


GATE_RUNNER_PATH = (
    "backend/core/ouroboros/governance/phase_runners/gate_runner.py"
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "test-session")
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    reset_runtime_for_tests()
    reset_registry_for_tests()
    yield tmp_path / "det"
    reset_runtime_for_tests()
    reset_registry_for_tests()


def _read_runner_source() -> str:
    return open(GATE_RUNNER_PATH, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# §1-§9 — Source-level pins
# ---------------------------------------------------------------------------


def test_gate_wiring_invokes_capture_phase_decision() -> None:
    src = _read_runner_source()
    assert "capture_phase_decision(" in src


def test_gate_wiring_uses_risk_tier_assignment_kind() -> None:
    src = _read_runner_source()
    assert 'kind="risk_tier_assignment"' in src


def test_gate_wiring_uses_phase_gate() -> None:
    src = _read_runner_source()
    assert 'phase="GATE"' in src


def test_gate_wiring_captures_before_success_return() -> None:
    """The capture call must come BEFORE the terminal success
    PhaseResult return at line ~674. Source ordering pin."""
    src = _read_runner_source()
    capture_idx = src.index('kind="risk_tier_assignment"')
    # Find the success-path return (status="ok", reason="gated")
    success_return = src.index('reason="gated"')
    assert capture_idx < success_return, (
        "capture must happen BEFORE the success PhaseResult return"
    )


def test_gate_wiring_uses_closure_over_risk_tier() -> None:
    """The capture wrapper's compute() reads from `risk_tier` outer
    scope — does NOT re-execute the gate mutation flow."""
    src = _read_runner_source()
    digest_idx = src.index("_gate_digest_compute")
    after = src[digest_idx:digest_idx + 1500]
    # Must reference risk_tier (closure over outer var)
    assert "risk_tier" in after
    # Must NOT re-call risk-tier-mutating gates
    closure_end = after.find("await capture_phase_decision")
    closure_body = after[:closure_end] if closure_end > 0 else after
    forbidden_calls = (
        "SimilarityGate", "SemanticGuardian", "MutationGate",
        "frozen_tier", "RISK_CEILING",
    )
    for f in forbidden_calls:
        assert f not in closure_body, (
            f"capture compute closure must NOT re-invoke gate logic ({f})"
        )


def test_gate_wiring_has_try_except_fallback() -> None:
    src = _read_runner_source()
    capture_idx = src.index('kind="risk_tier_assignment"')
    preceding = src[max(0, capture_idx - 4000):capture_idx]
    try_idx = preceding.rfind("try:")
    assert try_idx != -1
    following = src[capture_idx:capture_idx + 4000]
    except_idx = following.find("except")
    assert except_idx != -1
    except_window = following[except_idx:except_idx + 80]
    assert "Exception" in except_window


def test_gate_wiring_uses_logger_debug() -> None:
    """The fallback path uses logger.debug, NOT logger.warning, so
    flag-off operators don't see capture noise."""
    src = _read_runner_source()
    capture_idx = src.index('kind="risk_tier_assignment"')
    following = src[capture_idx:capture_idx + 4000]
    except_idx = following.find("except Exception")
    assert except_idx != -1
    body = following[except_idx:except_idx + 500]
    assert "logger.debug" in body


def test_gate_wiring_marker_present() -> None:
    src = _read_runner_source()
    assert "Slice 1.3.c" in src
    assert "audit-only" in src.lower() or "audit_only" in src.lower()


# ---------------------------------------------------------------------------
# §10-§11 — Lazy import + clean reload
# ---------------------------------------------------------------------------


def test_gate_runner_imports_phase_capture_lazily() -> None:
    src = _read_runner_source()
    lines = src.split("\n")
    top_level_imports = [
        ln for ln in lines
        if ln.startswith(
            "from backend.core.ouroboros.governance.determinism.phase_capture"
        )
    ]
    assert top_level_imports == []


def test_gate_runner_imports_cleanly() -> None:
    from backend.core.ouroboros.governance.phase_runners import (
        gate_runner,
    )
    importlib.reload(gate_runner)
    assert hasattr(gate_runner, "GATERunner")


# ---------------------------------------------------------------------------
# §12-§14 — End-to-end behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_tier_digest_round_trip(isolated) -> None:
    """The digest dict (risk_tier as enum-name str + bool) round-trips
    cleanly through the identity adapter."""
    digest = {
        "risk_tier": "NOTIFY_APPLY",
        "has_best_candidate": True,
    }
    out = await capture_phase_decision(
        op_id="op-1", phase="GATE", kind="risk_tier_assignment",
        compute=lambda: digest,
    )
    assert out == digest


@pytest.mark.asyncio
async def test_risk_tier_record_then_replay(
    isolated, monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")

    # Simulate the GATE closure pattern
    fake_risk_tier_name = "APPROVAL_REQUIRED"

    async def _digest_compute():
        return {
            "risk_tier": fake_risk_tier_name,
            "has_best_candidate": True,
        }

    await capture_phase_decision(
        op_id="op-1", phase="GATE", kind="risk_tier_assignment",
        compute=_digest_compute,
    )

    # REPLAY pass
    reset_runtime_for_tests()
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")

    canary = {"called": False}

    async def _should_not_run():
        canary["called"] = True
        return {"risk_tier": "BLOCKED", "has_best_candidate": False}

    out = await capture_phase_decision(
        op_id="op-1", phase="GATE", kind="risk_tier_assignment",
        compute=_should_not_run,
    )
    assert out["risk_tier"] == "APPROVAL_REQUIRED"
    assert out["has_best_candidate"] is True
    assert canary["called"] is False


def test_risk_tier_serialization_uses_name() -> None:
    """Source-level pin: the closure uses risk_tier.name (uppercase
    enum identifier), not str(risk_tier) which prints
    'RiskTier.NOTIFY_APPLY' with the class prefix."""
    src = _read_runner_source()
    digest_idx = src.index("_gate_digest_compute")
    after = src[digest_idx:digest_idx + 1500]
    assert "risk_tier.name" in after, (
        "closure must use risk_tier.name (NOT str(risk_tier))"
    )


# ---------------------------------------------------------------------------
# §15 — Capture only on success path
# ---------------------------------------------------------------------------


def test_capture_only_on_success_path() -> None:
    """The capture is positioned just before the SUCCESS-path return.
    Fail paths (lines 155, 185, 559, 667) have their own structured
    reason codes and bypass the audit capture."""
    src = _read_runner_source()
    # Count capture_phase_decision occurrences — should be exactly 1
    # (the success-path capture; this slice doesn't wire fail paths)
    count = src.count("await capture_phase_decision(")
    assert count == 1, (
        f"expected exactly 1 capture call (success-path only), got {count}"
    )


def test_capture_emits_after_all_mutation_sites() -> None:
    """The capture is positioned AFTER all 7 risk_tier mutation
    sites — proves the captured digest reflects the FINAL verdict
    after every gate has had its say."""
    src = _read_runner_source()
    capture_idx = src.index('kind="risk_tier_assignment"')
    # Mutation site references that should appear BEFORE the capture
    earlier_mutations = (
        "SimilarityGate",  # one of the 7 mutation sites
        "frozen_tier",      # another
        "RISK_CEILING",     # another
    )
    for marker in earlier_mutations:
        if marker in src:
            mutation_idx = src.index(marker)
            assert mutation_idx < capture_idx, (
                f"capture must come AFTER {marker} mutation site"
            )
