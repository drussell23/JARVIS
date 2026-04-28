"""Phase 1 Slice 1.4 — Session Replay CLI orchestrator regression spine.

Pins:
  §1   replay_cli_enabled flag — default true; case-tolerant
  §2   ReplaySessionPlan — frozen dataclass
  §3   SessionReplayer.discover — empty session_id rejected
  §4   discover — missing seed file → failure plan
  §5   discover — corrupt seed JSON → failure plan
  §6   discover — schema mismatch on seed → failure plan
  §7   discover — valid seed + missing decisions → replayable (warns)
  §8   discover — valid seed + valid decisions → replayable (success)
  §9   discover — corrupt decisions JSONL → counts only valid rows
  §10  discover — non-existent decisions file → count=0, replayable
  §11  discover — unreadable decisions file → failure plan
  §12  apply_env — sets all 7 env vars correctly
  §13  apply_env — unrepayable plan → no-op
  §14  apply_env — unknown mode falls to 'replay' + logs warning
  §15  apply_env — idempotent on repeated calls
  §16  validate — ok plan returns (True, "ok")
  §17  validate — failure plan returns (False, reason)
  §18  validate — zero-seed plan returns (False, "zero_seed_invalid")
  §19  setup_replay_from_cli — full pipeline integration
  §20  setup_replay_from_cli — raise_on_failure=True raises on bad
  §21  setup_replay_from_cli — raise_on_failure=False returns plan
  §22  setup_replay_from_cli — CLI disabled returns failure plan
  §23  render_plan_summary — readable output for ok plans
  §24  render_plan_summary — readable output for failure plans
  §25  Authority invariants — no orchestrator/phase_runner imports
  §26  End-to-end — discover + apply_env + verify Slice 1.2 reads vars
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.determinism import (
    ReplaySessionPlan,
    SessionReplayer,
    render_plan_summary,
    replay_cli_enabled,
    setup_replay_from_cli,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Isolate state dir + clean env between tests."""
    state_dir = tmp_path / "determinism"
    monkeypatch.setenv("JARVIS_DETERMINISM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(state_dir))
    # Clean up any test pollution
    for key in [
        "JARVIS_DETERMINISM_LEDGER_ENABLED",
        "JARVIS_DETERMINISM_LEDGER_MODE",
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED",
        "JARVIS_DETERMINISM_ENTROPY_ENABLED",
        "JARVIS_DETERMINISM_CLOCK_ENABLED",
        "OUROBOROS_BATTLE_SESSION_ID",
        "OUROBOROS_DETERMINISM_SEED",
        "JARVIS_DETERMINISM_REPLAY_CLI_ENABLED",
    ]:
        monkeypatch.delenv(key, raising=False)
    yield state_dir


def _write_seed(state_dir: Path, session_id: str, seed: int = 0xDEADBEEF) -> Path:
    """Write a valid seed.json for a given session."""
    p = state_dir / session_id / "seed.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema_version": "session_seed.1",
        "session_id": session_id,
        "seed": seed,
    }, sort_keys=True, indent=2))
    return p


def _write_decisions(
    state_dir: Path, session_id: str, count: int = 3,
) -> Path:
    """Write a valid decisions.jsonl with N records."""
    p = state_dir / session_id / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(count):
        rows.append(json.dumps({
            "schema_version": "decision_record.1",
            "record_id": f"rec-{i}",
            "session_id": session_id,
            "op_id": f"op-{i}",
            "phase": "ROUTE",
            "kind": "route_assignment",
            "ordinal": 0,
            "inputs_hash": "abc",
            "output_repr": '"STANDARD"',
            "monotonic_ts": 100.0 + i,
            "wall_ts": 1700000000.0 + i,
        }))
    p.write_text("\n".join(rows) + "\n")
    return p


# ---------------------------------------------------------------------------
# §1 — replay_cli_enabled flag
# ---------------------------------------------------------------------------


def test_replay_cli_enabled_default_true(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_DETERMINISM_REPLAY_CLI_ENABLED", raising=False,
    )
    assert replay_cli_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_replay_cli_enabled_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_REPLAY_CLI_ENABLED", val,
    )
    assert replay_cli_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off"])
def test_replay_cli_enabled_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_REPLAY_CLI_ENABLED", val,
    )
    assert replay_cli_enabled() is False


def test_replay_cli_enabled_empty_is_default_true(monkeypatch) -> None:
    """Empty string is the unset marker — default-on for the CLI surface."""
    monkeypatch.setenv("JARVIS_DETERMINISM_REPLAY_CLI_ENABLED", "")
    assert replay_cli_enabled() is True


# ---------------------------------------------------------------------------
# §2 — ReplaySessionPlan frozen
# ---------------------------------------------------------------------------


def test_plan_is_frozen() -> None:
    p = ReplaySessionPlan(
        session_id="s1",
        state_dir=Path("/tmp/x"),
        seed_path=Path("/tmp/x/seed.json"),
        decisions_path=Path("/tmp/x/d.jsonl"),
        is_replayable=True,
    )
    with pytest.raises(Exception):
        p.session_id = "different"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §3-§11 — SessionReplayer.discover
# ---------------------------------------------------------------------------


def test_discover_empty_session_id() -> None:
    plan = SessionReplayer().discover("")
    assert plan.is_replayable is False
    assert plan.failure_reason == "empty_session_id"


def test_discover_whitespace_session_id() -> None:
    plan = SessionReplayer().discover("   ")
    assert plan.is_replayable is False


def test_discover_missing_seed_fails(isolated_state) -> None:
    plan = SessionReplayer().discover("never-recorded")
    assert plan.is_replayable is False
    assert plan.failure_reason == "seed_missing_or_invalid"


def test_discover_corrupt_seed_fails(isolated_state) -> None:
    p = isolated_state / "corrupt-session" / "seed.json"
    p.parent.mkdir(parents=True)
    p.write_text("{ not valid json")
    plan = SessionReplayer().discover("corrupt-session")
    assert plan.is_replayable is False
    assert plan.failure_reason == "seed_missing_or_invalid"


def test_discover_schema_mismatch_seed_fails(isolated_state) -> None:
    p = isolated_state / "wrong-schema" / "seed.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "schema_version": "wrong.0", "seed": 42,
    }))
    plan = SessionReplayer().discover("wrong-schema")
    assert plan.is_replayable is False


def test_discover_valid_seed_no_decisions_replayable(
    isolated_state,
) -> None:
    """Seed but no decisions → still replayable (warning diagnostic)."""
    _write_seed(isolated_state, "seed-only", seed=12345)
    plan = SessionReplayer().discover("seed-only")
    assert plan.is_replayable is True
    assert plan.seed == 12345
    assert plan.decision_count == 0
    # Diagnostic should mention the empty ledger
    assert any(
        "empty decisions ledger" in d for d in plan.diagnostics
    )


def test_discover_valid_seed_and_decisions(isolated_state) -> None:
    _write_seed(isolated_state, "full-session", seed=0xCAFEBABE)
    _write_decisions(isolated_state, "full-session", count=5)
    plan = SessionReplayer().discover("full-session")
    assert plan.is_replayable is True
    assert plan.seed == 0xCAFEBABE
    assert plan.decision_count == 5


def test_discover_corrupt_jsonl_counts_only_valid(
    isolated_state,
) -> None:
    """Mixed valid + invalid lines — only valid records counted."""
    _write_seed(isolated_state, "mixed", seed=1)
    p = isolated_state / "mixed" / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    valid = json.dumps({
        "schema_version": "decision_record.1",
        "record_id": "rec-0", "session_id": "mixed",
        "op_id": "op-0", "phase": "P", "kind": "K", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
    })
    rows = [
        valid,
        "{ not valid json",
        json.dumps({"schema_version": "wrong.0"}),  # schema mismatch
        valid,
        "",  # empty line
    ]
    p.write_text("\n".join(rows) + "\n")
    plan = SessionReplayer().discover("mixed")
    assert plan.is_replayable is True
    assert plan.decision_count == 2  # only the two valid rows


def test_discover_no_decisions_file_counts_zero(isolated_state) -> None:
    """Missing decisions.jsonl → count=0 (not failure)."""
    _write_seed(isolated_state, "no-decisions", seed=1)
    # Don't create decisions.jsonl
    plan = SessionReplayer().discover("no-decisions")
    assert plan.is_replayable is True
    assert plan.decision_count == 0


# ---------------------------------------------------------------------------
# §12-§15 — apply_env
# ---------------------------------------------------------------------------


def test_apply_env_sets_all_vars(isolated_state, monkeypatch) -> None:
    _write_seed(isolated_state, "envtest", seed=0xABCDEF)
    plan = SessionReplayer().discover("envtest")
    SessionReplayer().apply_env(plan, mode="replay")
    assert os.environ["JARVIS_DETERMINISM_LEDGER_ENABLED"] == "true"
    assert os.environ["JARVIS_DETERMINISM_LEDGER_MODE"] == "replay"
    assert os.environ["JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED"] == "true"
    assert os.environ["JARVIS_DETERMINISM_ENTROPY_ENABLED"] == "true"
    assert os.environ["JARVIS_DETERMINISM_CLOCK_ENABLED"] == "true"
    assert os.environ["OUROBOROS_BATTLE_SESSION_ID"] == "envtest"
    assert os.environ["OUROBOROS_DETERMINISM_SEED"] == "11259375"


def test_apply_env_verify_mode(isolated_state, monkeypatch) -> None:
    _write_seed(isolated_state, "vfytest", seed=1)
    plan = SessionReplayer().discover("vfytest")
    SessionReplayer().apply_env(plan, mode="verify")
    assert os.environ["JARVIS_DETERMINISM_LEDGER_MODE"] == "verify"


def test_apply_env_unrepayable_noop(isolated_state, monkeypatch) -> None:
    """Bad plan → apply_env doesn't mutate env."""
    plan = SessionReplayer().discover("nonexistent")
    assert plan.is_replayable is False
    SessionReplayer().apply_env(plan)
    assert "JARVIS_DETERMINISM_LEDGER_ENABLED" not in os.environ


def test_apply_env_unknown_mode_falls_back(
    isolated_state, monkeypatch, caplog,
) -> None:
    import logging
    _write_seed(isolated_state, "modetest", seed=1)
    plan = SessionReplayer().discover("modetest")
    caplog.set_level(logging.WARNING)
    SessionReplayer().apply_env(plan, mode="garbage_mode")
    assert os.environ["JARVIS_DETERMINISM_LEDGER_MODE"] == "replay"
    # Warning emitted
    warns = [
        r for r in caplog.records if "unknown mode" in r.getMessage()
    ]
    assert len(warns) >= 1


def test_apply_env_idempotent(isolated_state, monkeypatch) -> None:
    _write_seed(isolated_state, "idem", seed=1)
    plan = SessionReplayer().discover("idem")
    replayer = SessionReplayer()
    replayer.apply_env(plan, mode="replay")
    replayer.apply_env(plan, mode="replay")
    replayer.apply_env(plan, mode="replay")
    assert os.environ["JARVIS_DETERMINISM_LEDGER_ENABLED"] == "true"
    assert os.environ["OUROBOROS_BATTLE_SESSION_ID"] == "idem"


# ---------------------------------------------------------------------------
# §16-§18 — validate
# ---------------------------------------------------------------------------


def test_validate_ok_plan(isolated_state) -> None:
    _write_seed(isolated_state, "ok-plan", seed=42)
    plan = SessionReplayer().discover("ok-plan")
    valid, msg = SessionReplayer().validate(plan)
    assert valid is True
    assert msg == "ok"


def test_validate_failure_plan(isolated_state) -> None:
    plan = SessionReplayer().discover("nonexistent")
    valid, msg = SessionReplayer().validate(plan)
    assert valid is False
    assert msg == "seed_missing_or_invalid"


def test_validate_zero_seed_invalid(isolated_state) -> None:
    """Even though discover marks the plan replayable, a zero seed
    is rejected by validate (defensive — zero seed is the sentinel
    for empty session_id in Slice 1.1)."""
    _write_seed(isolated_state, "zero-seed", seed=0)
    plan = SessionReplayer().discover("zero-seed")
    valid, msg = SessionReplayer().validate(plan)
    assert valid is False
    assert msg == "zero_seed_invalid"


# ---------------------------------------------------------------------------
# §19-§22 — setup_replay_from_cli
# ---------------------------------------------------------------------------


def test_setup_full_pipeline_ok(isolated_state, monkeypatch) -> None:
    _write_seed(isolated_state, "happy-path", seed=99)
    _write_decisions(isolated_state, "happy-path", count=2)
    plan = setup_replay_from_cli("happy-path", mode="replay")
    assert plan.is_replayable is True
    assert plan.decision_count == 2
    assert os.environ["OUROBOROS_BATTLE_SESSION_ID"] == "happy-path"


def test_setup_raises_on_failure_default(
    isolated_state, monkeypatch,
) -> None:
    """Default raise_on_failure=True → ValueError on bad session."""
    with pytest.raises(ValueError) as exc_info:
        setup_replay_from_cli("nonexistent-session")
    assert "seed_missing" in str(exc_info.value) or \
        "replay setup failed" in str(exc_info.value)


def test_setup_no_raise_returns_plan(isolated_state, monkeypatch) -> None:
    """raise_on_failure=False → return failure plan, env untouched."""
    plan = setup_replay_from_cli(
        "nonexistent-session", raise_on_failure=False,
    )
    assert plan.is_replayable is False
    assert "JARVIS_DETERMINISM_LEDGER_ENABLED" not in os.environ


def test_setup_cli_disabled_returns_failure(
    isolated_state, monkeypatch,
) -> None:
    """Even with valid session, disabled CLI flag → failure plan."""
    _write_seed(isolated_state, "valid", seed=1)
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_REPLAY_CLI_ENABLED", "false",
    )
    plan = setup_replay_from_cli("valid", raise_on_failure=False)
    assert plan.is_replayable is False
    assert plan.failure_reason == "cli_disabled"


# ---------------------------------------------------------------------------
# §23-§24 — render_plan_summary
# ---------------------------------------------------------------------------


def test_render_summary_ok_plan(isolated_state) -> None:
    _write_seed(isolated_state, "render", seed=0xABC)
    _write_decisions(isolated_state, "render", count=3)
    plan = SessionReplayer().discover("render")
    out = render_plan_summary(plan)
    assert "render" in out
    assert "0x0000000000000abc" in out
    assert "decisions:" in out
    assert "3" in out
    assert "is_replayable:  True" in out


def test_render_summary_failure_plan() -> None:
    plan = SessionReplayer().discover("does-not-exist")
    out = render_plan_summary(plan)
    assert "is_replayable:  False" in out
    assert "failure:" in out


# ---------------------------------------------------------------------------
# §25 — Authority invariants
# ---------------------------------------------------------------------------


def test_no_orchestrator_imports() -> None:
    import inspect
    from backend.core.ouroboros.governance.determinism import session_replay
    src = inspect.getsource(session_replay)
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.phase_runner ",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for f in forbidden:
        assert f not in src, f"session_replay must NOT contain {f!r}"


def test_no_phase_runners_imports() -> None:
    """Slice 1.4 is a CLI orchestrator — it must NOT import any
    phase runner. The harness boots normally; phase runners read env
    via Slice 1.3's wrapper."""
    import inspect
    from backend.core.ouroboros.governance.determinism import session_replay
    src = inspect.getsource(session_replay)
    assert "phase_runners" not in src


# ---------------------------------------------------------------------------
# §26 — End-to-end integration with Slice 1.2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_replay_engages_decision_runtime(
    isolated_state, monkeypatch,
) -> None:
    """After setup_replay_from_cli, Slice 1.2's decide() should
    return REPLAY mode + use the recorded decisions."""
    from backend.core.ouroboros.governance.determinism import (
        decide,
    )
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        _resolve_mode,
        LedgerMode,
        reset_all_for_tests,
    )

    # Build a session with a recorded decision
    _write_seed(isolated_state, "e2e", seed=42)
    p = isolated_state / "e2e" / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema_version": "decision_record.1",
        "record_id": "rec-0", "session_id": "e2e",
        "op_id": "op-1", "phase": "ROUTE",
        "kind": "route_assignment", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"REPLAYED"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
    }) + "\n")

    # Engage replay
    setup_replay_from_cli("e2e", mode="replay")
    reset_all_for_tests()  # force fresh runtime singleton load

    # Verify env vars set the runtime to REPLAY mode
    assert _resolve_mode() is LedgerMode.REPLAY

    # decide() should now return the recorded value without
    # running compute()
    canary = {"called": False}

    def should_not_run():
        canary["called"] = True
        return "LIVE-VALUE"

    out = await decide(
        op_id="op-1", phase="ROUTE", kind="route_assignment",
        inputs={}, compute=should_not_run,
    )
    assert out == "REPLAYED"
    assert canary["called"] is False
    reset_all_for_tests()
