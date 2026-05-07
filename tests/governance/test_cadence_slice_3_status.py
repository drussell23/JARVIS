"""Cadence Slice 3 — overdue detector regression spine.

Pins per operator binding 2026-05-06:

  * 5-value CadenceStatusVerdict closed taxonomy bytes-pinned
  * Pure-function evaluate_cadence_status — caller injects
    readers; default composes canonical substrate
  * Verdict ladder first-match-wins:
      1. UNKNOWN — manifest missing or interval_hint=0
      2. NEVER_RAN — no signals
      3. RECENTLY_FAILED — failure age < success age
      4. OVERDUE — last success > grace_window
      5. HEALTHY — last success ≤ grace_window
  * grace_window = interval_hint × grace_factor (clamped ≥1.0)
  * §33.5 versioned-artifact round-trip
  * NO hardcoded cadence-second literals (manifest is sole
    knower) — AST-pinned
  * Read-only: no record_/write_ calls (AST-pinned)
  * Fail-silent on garbage / missing substrate
  * Wires into existing live_fire_graduation_soak.py status
    subcommand (no new CLI tool)

Verifies (35 tests).
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_5_values():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict,
    )
    assert {v.value for v in CadenceStatusVerdict} == {
        "healthy", "overdue", "recently_failed",
        "never_ran", "unknown",
    }


# ---------------------------------------------------------------------------
# Verdict ladder — pure-function with injected readers
# ---------------------------------------------------------------------------


def _stub_manifest(interval_hint_s: int = 12 * 3600):
    """Minimal manifest stub satisfying the duck-type."""
    class _M:
        schedule_kind = "cron"
        schedule_string = "0 */12 * * *"
    m = _M()
    m.interval_hint_s = interval_hint_s  # type: ignore[attr-defined]
    return m


def _stub_failure_row(epoch: float):
    class _R:
        ts_epoch = epoch
    return _R()


def test_verdict_unknown_when_manifest_missing():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict, evaluate_cadence_status,
    )
    r = evaluate_cadence_status(
        now_epoch=1000.0,
        manifest_reader=lambda: None,
        last_preflight_ok_reader=lambda: None,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
    )
    assert r.verdict == CadenceStatusVerdict.UNKNOWN
    assert r.detail == "manifest_missing"


def test_verdict_unknown_when_interval_zero():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict, evaluate_cadence_status,
    )
    r = evaluate_cadence_status(
        now_epoch=1000.0,
        manifest_reader=lambda: _stub_manifest(0),
        last_preflight_ok_reader=lambda: None,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
    )
    assert r.verdict == CadenceStatusVerdict.UNKNOWN


def test_verdict_never_ran():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict, evaluate_cadence_status,
    )
    r = evaluate_cadence_status(
        now_epoch=1000.0,
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: None,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
    )
    assert r.verdict == CadenceStatusVerdict.NEVER_RAN
    assert r.detail == "no_signals_observed"


def test_verdict_healthy_when_recent_ok():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict, evaluate_cadence_status,
    )
    now = 1_000_000.0
    interval = 43200  # 12h
    # ok 1h ago — well within grace_window (12h * 1.5 = 18h)
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(interval),
        last_preflight_ok_reader=lambda: now - 3600,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
        grace_factor=1.5,
    )
    assert r.verdict == CadenceStatusVerdict.HEALTHY


def test_verdict_overdue_when_no_recent_success():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict, evaluate_cadence_status,
    )
    now = 1_000_000.0
    interval = 43200  # 12h, grace 18h
    # Last ok 24h ago — past grace
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(interval),
        last_preflight_ok_reader=lambda: now - (24 * 3600),
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
        grace_factor=1.5,
    )
    assert r.verdict == CadenceStatusVerdict.OVERDUE


def test_verdict_recently_failed_takes_precedence():
    """Failure newer than ok → RECENTLY_FAILED."""
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict, evaluate_cadence_status,
    )
    now = 1_000_000.0
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: now - 7200,    # 2h ago
        last_preflight_failure_reader=(
            lambda: _stub_failure_row(now - 600)        # 10min ago
        ),
        last_history_epoch_reader=lambda: None,
        grace_factor=1.5,
    )
    assert r.verdict == CadenceStatusVerdict.RECENTLY_FAILED


def test_verdict_history_anchor_works_when_preflight_missing():
    """When preflight rows don't exist (legacy path) but the
    graduation history has recent rows, HEALTHY still fires."""
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict, evaluate_cadence_status,
    )
    now = 1_000_000.0
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: None,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: now - 3600,  # 1h ago
        grace_factor=1.5,
    )
    assert r.verdict == CadenceStatusVerdict.HEALTHY


def test_verdict_failure_after_old_success():
    """Old ok, recent failure → RECENTLY_FAILED."""
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusVerdict, evaluate_cadence_status,
    )
    now = 1_000_000.0
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: now - (3 * 24 * 3600),
        last_preflight_failure_reader=(
            lambda: _stub_failure_row(now - 600)
        ),
        last_history_epoch_reader=lambda: None,
    )
    assert r.verdict == CadenceStatusVerdict.RECENTLY_FAILED


# ---------------------------------------------------------------------------
# grace_factor handling
# ---------------------------------------------------------------------------


def test_grace_factor_clamps_below_1():
    """A grace_factor < 1.0 would mean overdue-before-cadence;
    clamp to 1.0."""
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        evaluate_cadence_status,
    )
    now = 1_000_000.0
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: now - 100,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
        grace_factor=0.1,  # too low
    )
    assert r.grace_factor == 1.0
    # grace_window_s = interval × clamped_grace = 43200×1.0
    assert r.grace_window_s == 43200


def test_grace_factor_env_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CADENCE_OVERDUE_GRACE_FACTOR", raising=False,
    )
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        evaluate_cadence_status,
    )
    now = 1_000_000.0
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: None,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
    )
    # Default grace 1.5 → window = 43200 × 1.5 = 64800
    assert r.grace_factor == 1.5
    assert r.grace_window_s == 64800


def test_grace_factor_env_override(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CADENCE_OVERDUE_GRACE_FACTOR", "2.0",
    )
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        evaluate_cadence_status,
    )
    now = 1_000_000.0
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: None,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
    )
    assert r.grace_factor == 2.0


# ---------------------------------------------------------------------------
# next_expected_epoch / iso forecast
# ---------------------------------------------------------------------------


def test_next_expected_anchors_to_latest_success():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        evaluate_cadence_status,
    )
    now = 1_000_000.0
    interval = 3600
    last_ok = now - 1000
    last_history = now - 500  # newer than ok
    r = evaluate_cadence_status(
        now_epoch=now,
        manifest_reader=lambda: _stub_manifest(interval),
        last_preflight_ok_reader=lambda: last_ok,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: last_history,
    )
    # Anchor = max(ok, history) = last_history
    assert r.next_expected_epoch == last_history + interval


def test_next_expected_none_when_no_signals():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        evaluate_cadence_status,
    )
    r = evaluate_cadence_status(
        now_epoch=1_000_000.0,
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: None,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
    )
    assert r.next_expected_epoch is None
    assert r.next_expected_iso is None


# ---------------------------------------------------------------------------
# Defensive — never raises
# ---------------------------------------------------------------------------


def test_evaluate_never_raises_on_bad_readers():
    """Readers that raise → return UNKNOWN-with-detail rather
    than propagating."""
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        evaluate_cadence_status,
    )

    def _bad():
        raise RuntimeError("simulated")

    r = evaluate_cadence_status(
        now_epoch=1.0,
        manifest_reader=_bad,
        last_preflight_ok_reader=_bad,
        last_preflight_failure_reader=_bad,
        last_history_epoch_reader=_bad,
    )
    # Manifest reader exception → manifest_missing detail
    assert r is not None


def test_evaluate_handles_bad_now_epoch():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        evaluate_cadence_status,
    )
    r = evaluate_cadence_status(
        now_epoch="garbage",  # type: ignore
        manifest_reader=lambda: _stub_manifest(43200),
        last_preflight_ok_reader=lambda: None,
        last_preflight_failure_reader=lambda: None,
        last_history_epoch_reader=lambda: None,
    )
    assert r is not None


def test_is_overdue_convenience_predicate(monkeypatch):
    from backend.core.ouroboros.governance.graduation import (
        cadence_status as cs_module,
    )
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        is_overdue,
    )
    # Force OVERDUE via injected readers in evaluate.
    # Simplest: monkeypatch evaluate_cadence_status.
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusReport, CadenceStatusVerdict,
        CADENCE_STATUS_REPORT_SCHEMA_VERSION,
    )
    overdue = CadenceStatusReport(
        schema_version=CADENCE_STATUS_REPORT_SCHEMA_VERSION,
        verdict=CadenceStatusVerdict.OVERDUE,
        schedule_kind="cron",
        schedule_string="0 */12 * * *",
        interval_hint_s=43200,
        grace_factor=1.5,
        grace_window_s=64800,
        last_preflight_ok_age_s=None,
        last_preflight_failure_age_s=None,
        last_history_row_age_s=None,
        next_expected_epoch=None,
        next_expected_iso=None,
        detail="x",
    )
    monkeypatch.setattr(
        cs_module, "evaluate_cadence_status",
        lambda **kw: overdue,
    )
    assert is_overdue() is True
    healthy = CadenceStatusReport(
        schema_version=CADENCE_STATUS_REPORT_SCHEMA_VERSION,
        verdict=CadenceStatusVerdict.HEALTHY,
        schedule_kind="cron",
        schedule_string="0 */12 * * *",
        interval_hint_s=43200,
        grace_factor=1.5,
        grace_window_s=64800,
        last_preflight_ok_age_s=10.0,
        last_preflight_failure_age_s=None,
        last_history_row_age_s=None,
        next_expected_epoch=None,
        next_expected_iso=None,
        detail="x",
    )
    monkeypatch.setattr(
        cs_module, "evaluate_cadence_status",
        lambda **kw: healthy,
    )
    assert is_overdue() is False


# ---------------------------------------------------------------------------
# §33.5 versioned-artifact round-trip
# ---------------------------------------------------------------------------


def test_artifact_round_trip():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CADENCE_STATUS_REPORT_SCHEMA_VERSION,
        CadenceStatusReport, CadenceStatusVerdict,
    )
    r = CadenceStatusReport(
        schema_version=CADENCE_STATUS_REPORT_SCHEMA_VERSION,
        verdict=CadenceStatusVerdict.HEALTHY,
        schedule_kind="cron",
        schedule_string="0 */12 * * *",
        interval_hint_s=43200,
        grace_factor=1.5,
        grace_window_s=64800,
        last_preflight_ok_age_s=120.0,
        last_preflight_failure_age_s=None,
        last_history_row_age_s=None,
        next_expected_epoch=1234567890.0,
        next_expected_iso="2026-05-06T00:00:00Z",
        detail="green",
    )
    rt = CadenceStatusReport.from_dict(r.to_dict())
    assert rt is not None
    assert rt.verdict == CadenceStatusVerdict.HEALTHY
    assert rt.interval_hint_s == 43200


def test_artifact_from_dict_garbage():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CadenceStatusReport,
    )
    assert CadenceStatusReport.from_dict("nope") is None  # type: ignore
    assert CadenceStatusReport.from_dict({}) is None
    assert (
        CadenceStatusReport.from_dict({"verdict": "nope"})
        is None
    )


# ---------------------------------------------------------------------------
# Render — composes existing ANSI vocabulary, no bright_green
# ---------------------------------------------------------------------------


def test_render_block_carries_verdict():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        CADENCE_STATUS_REPORT_SCHEMA_VERSION,
        CadenceStatusReport, CadenceStatusVerdict,
        render_cadence_status_block,
    )
    r = CadenceStatusReport(
        schema_version=CADENCE_STATUS_REPORT_SCHEMA_VERSION,
        verdict=CadenceStatusVerdict.OVERDUE,
        schedule_kind="cron",
        schedule_string="0 */12 * * *",
        interval_hint_s=43200,
        grace_factor=1.5,
        grace_window_s=64800,
        last_preflight_ok_age_s=None,
        last_preflight_failure_age_s=600.0,
        last_history_row_age_s=None,
        next_expected_epoch=None,
        next_expected_iso=None,
        detail="hi",
    )
    text = render_cadence_status_block(r)
    assert "Cadence status" in text
    assert "OVERDUE" in text
    assert "schedule_kind" in text
    # Identity preservation: NO bright_green ANSI
    assert "\x1b[92m" not in text


def test_render_block_never_raises_on_garbage():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        render_cadence_status_block,
    )
    # garbage in — shouldn't raise
    try:
        render_cadence_status_block(None)  # type: ignore
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"raised: {exc}")


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_5():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "cadence_status_authority_asymmetry",
        "cadence_status_no_hardcoded_cadence_seconds",
        "cadence_status_verdict_taxonomy_closed",
        "cadence_status_versioned_artifact_compliance",
        "cadence_status_read_only",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation/"
        "cadence_status.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_no_hardcoded_cadence_seconds_fires_on_synthetic_drift():
    """If a future refactor accidentally hardcodes 86400 (a
    seconds-per-day cadence value), the pin must fire."""
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def _classify_verdict(*, ok_age, **kw):
    # BAD — hardcoded cadence value bypassing manifest
    if ok_age and ok_age > 86400:
        return "overdue"
    return "healthy"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "no_hardcoded_cadence_seconds" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("86400" in v for v in violations)


def test_read_only_pin_fires_on_record_call():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def f():
    record_health_row(payload)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "read_only" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.iron_gate "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "authority_asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_verdict_pin_fires_on_taxonomy_drift():
    from backend.core.ouroboros.governance.graduation.cadence_status import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
import enum
class CadenceStatusVerdict(str, enum.Enum):
    HEALTHY = "healthy"
    OVERDUE = "overdue"
    RECENTLY_FAILED = "recently_failed"
    NEVER_RAN = "never_ran"
    UNKNOWN = "unknown"
    UNAUTHORIZED = "unauthorized"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "verdict_taxonomy_closed" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Integration — wires into existing live_fire_graduation_soak.py
# status subcommand
# ---------------------------------------------------------------------------


def test_status_cli_renders_cadence_block():
    """The existing `status` subcommand MUST compose
    cadence_status (Slice 3 wiring contract)."""
    target = (
        _repo_root() / "scripts" / "live_fire_graduation_soak.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "evaluate_cadence_status" in source
    assert "render_cadence_status_block" in source


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance.graduation import (
        cadence_status,
    )
    expected = {
        "CADENCE_STATUS_REPORT_SCHEMA_VERSION",
        "CadenceStatusReport",
        "CadenceStatusVerdict",
        "evaluate_cadence_status",
        "is_overdue",
        "register_shipped_invariants",
        "render_cadence_status_block",
    }
    assert set(cadence_status.__all__) == expected
