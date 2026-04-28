"""Phase 1 Slice 1.3.b — GENERATE phase wiring regression spine.

Pins:
  §1   GENERATE wiring captures provider_selection digest (audit-only)
  §2   Source-level pin: capture_phase_decision is invoked
  §3   Source-level pin: capture happens AFTER generation, not before
  §4   Source-level pin: closure-over-generation pattern (no .generate() re-call)
  §5   Source-level pin: capture wrapper has try/except fallback
  §6   Source-level pin: extra_inputs include provider_route + parallel_gen_used
  §7   Identity adapter sufficient (digest is JSON-friendly primitives)
  §8   Wiring marker present (Slice 1.3.b reference)
  §9   generate_runner imports phase_capture LAZILY (not top-level)
  §10  generate_runner imports cleanly with importlib.reload
  §11  Authority invariant — capture failure does NOT propagate
  §12  End-to-end via captured_phase_decision: provider_selection digest
        round-trips through identity adapter
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


GENERATE_RUNNER_PATH = (
    "backend/core/ouroboros/governance/phase_runners/generate_runner.py"
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


# ---------------------------------------------------------------------------
# §1-§8 — Source-level pins on the wiring
# ---------------------------------------------------------------------------


def _read_runner_source() -> str:
    return open(GENERATE_RUNNER_PATH, encoding="utf-8").read()


def test_generate_wiring_invokes_capture_phase_decision() -> None:
    src = _read_runner_source()
    assert "capture_phase_decision(" in src, (
        "GENERATE runner must invoke capture_phase_decision"
    )


def test_generate_wiring_captures_provider_selection_kind() -> None:
    src = _read_runner_source()
    assert 'kind="provider_selection"' in src, (
        "GENERATE wiring must use kind='provider_selection'"
    )


def test_generate_wiring_uses_phase_generate() -> None:
    src = _read_runner_source()
    assert 'phase="GENERATE"' in src


def test_generate_wiring_captures_after_generation() -> None:
    """The capture call must come AFTER orch._generator.generate(),
    not before. Source ordering pin."""
    src = _read_runner_source()
    gen_idx = src.index("orch._generator.generate(ctx, deadline)")
    capture_idx = src.index('kind="provider_selection"')
    assert capture_idx > gen_idx, (
        "capture must happen AFTER generation completes"
    )


def test_generate_wiring_uses_closure_pattern() -> None:
    """The capture wrapper's compute() reads from the outer
    `generation` variable (closure) — does NOT re-call .generate().
    This proves audit-only semantics."""
    src = _read_runner_source()
    # Find the _digest_compute function definition
    digest_idx = src.index("_digest_compute")
    # Walk forward to next 1500 chars
    after = src[digest_idx:digest_idx + 1500]
    # Should reference `generation` (closure variable), NOT call
    # orch._generator.generate inside the compute closure
    assert "generation" in after
    # The compute closure must not RE-CALL .generate()
    closure_end = after.find("await capture_phase_decision")
    closure_body = after[:closure_end] if closure_end > 0 else after
    assert "._generator.generate(" not in closure_body, (
        "capture compute closure must not re-invoke .generate()"
    )


def test_generate_wiring_has_try_except_fallback() -> None:
    src = _read_runner_source()
    capture_idx = src.index('kind="provider_selection"')
    # Walk backwards to find enclosing try:
    preceding = src[max(0, capture_idx - 4000):capture_idx]
    try_idx = preceding.rfind("try:")
    assert try_idx != -1, (
        "GENERATE capture wrapper must be inside try/except"
    )
    # Walk forward to find except clause
    following = src[capture_idx:capture_idx + 4000]
    except_idx = following.find("except")
    assert except_idx != -1
    except_window = following[except_idx:except_idx + 80]
    assert "Exception" in except_window


def test_generate_wiring_extra_inputs_include_route() -> None:
    """extra_inputs include provider_route + parallel_gen_used so
    canonical inputs encode the upstream routing decision."""
    src = _read_runner_source()
    capture_idx = src.index('kind="provider_selection"')
    # Walk forward to find extra_inputs= block
    following = src[capture_idx:capture_idx + 1500]
    assert "extra_inputs=" in following
    assert "provider_route" in following
    assert "parallel_gen_used" in following


def test_generate_wiring_marker_present() -> None:
    src = _read_runner_source()
    assert "Slice 1.3.b" in src, (
        "GENERATE wiring source must reference Phase 1 Slice 1.3.b"
    )
    assert "audit-only" in src.lower() or "audit_only" in src.lower(), (
        "Wiring must document audit-only semantics in comment"
    )


# ---------------------------------------------------------------------------
# §9-§10 — Lazy import + clean reload
# ---------------------------------------------------------------------------


def test_generate_runner_imports_phase_capture_lazily() -> None:
    """phase_capture import must be INSIDE function body (lazy),
    not at module top level."""
    src = _read_runner_source()
    lines = src.split("\n")
    top_level_imports = [
        ln for ln in lines
        if ln.startswith(
            "from backend.core.ouroboros.governance.determinism.phase_capture"
        )
    ]
    assert top_level_imports == [], (
        "generate_runner must import phase_capture lazily"
    )


def test_generate_runner_imports_cleanly() -> None:
    """generate_runner reloads cleanly even when determinism module
    is partially loaded."""
    from backend.core.ouroboros.governance.phase_runners import (
        generate_runner,
    )
    importlib.reload(generate_runner)
    assert hasattr(generate_runner, "GENERATERunner")


# ---------------------------------------------------------------------------
# §11-§12 — End-to-end behavior via capture_phase_decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_selection_digest_round_trip(isolated) -> None:
    """The digest dict (str/int/bool primitives) round-trips
    through the identity adapter cleanly. Proves the chosen
    digest shape is JSON-friendly without needing a custom adapter."""
    digest = {
        "provider_name": "doubleword-397b",
        "model_id": "Qwen/Qwen3.5-397B-A17B-FP8",
        "candidate_count": 3,
        "is_noop": False,
    }

    out = await capture_phase_decision(
        op_id="op-1", phase="GENERATE", kind="provider_selection",
        compute=lambda: digest,
    )
    assert out == digest


@pytest.mark.asyncio
async def test_provider_selection_audit_record_then_replay(
    isolated, monkeypatch,
) -> None:
    """After RECORD, REPLAY returns the recorded digest. The
    'live' compute path is the closure-over-generation pattern in
    production; here we simulate it with a closure over a fake
    generation result."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")

    fake_gen = type("FakeGen", (), {})()
    fake_gen.provider_name = "claude-sonnet"
    fake_gen.model_id = "claude-sonnet-4-6"
    fake_gen.candidates = [{"file": "a.py"}, {"file": "b.py"}]
    fake_gen.is_noop = False

    async def _digest_compute():
        return {
            "provider_name": fake_gen.provider_name,
            "model_id": fake_gen.model_id,
            "candidate_count": len(fake_gen.candidates),
            "is_noop": fake_gen.is_noop,
        }

    # RECORD pass
    await capture_phase_decision(
        op_id="op-1", phase="GENERATE", kind="provider_selection",
        compute=_digest_compute,
    )

    # REPLAY pass
    reset_runtime_for_tests()
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")

    canary = {"called": False}

    async def _should_not_run():
        canary["called"] = True
        return {"provider_name": "DIFFERENT", "model_id": "wrong",
                "candidate_count": 0, "is_noop": True}

    out = await capture_phase_decision(
        op_id="op-1", phase="GENERATE", kind="provider_selection",
        compute=_should_not_run,
    )
    assert out["provider_name"] == "claude-sonnet"
    assert out["candidate_count"] == 2
    assert canary["called"] is False


# ---------------------------------------------------------------------------
# §13 — Defensive: capture failure doesn't break runner
# ---------------------------------------------------------------------------


def test_capture_failure_does_not_propagate() -> None:
    """The wiring's try/except catches Exception broadly so a broken
    capture wrapper can never break GENERATE. Pin via source."""
    src = _read_runner_source()
    # Find the comment documenting the contract
    assert "best-effort" in src.lower() or "does not propagate" in src.lower()


def test_capture_failure_logs_at_debug() -> None:
    """The fallback path uses logger.debug, NOT logger.warning, so
    flag-off operators don't see capture noise."""
    src = _read_runner_source()
    capture_idx = src.index('kind="provider_selection"')
    # Walk forward to find the except block
    following = src[capture_idx:capture_idx + 4000]
    except_idx = following.find("except Exception")
    assert except_idx != -1
    # The except body should use logger.debug
    body = following[except_idx:except_idx + 500]
    assert "logger.debug" in body, (
        "capture failure must use debug-level logging"
    )
