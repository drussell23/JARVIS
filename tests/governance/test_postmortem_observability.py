"""Priority D Slice D1 — Postmortem ledger discoverability spine.

Mirrors the P5 adversarial_observability + P4 metrics_observability
test patterns. Tests the read-only surface that exposes the
determinism-ledger postmortem records via:

  * /postmortems REPL dispatcher (5 subcommands)
  * 4 IDE GET endpoints
  * SSE publisher (best-effort)
  * Distribution computer (the operator-visible signal that surfaces
    Phase 2 hollowness)

Pins:
  §1   Master flag default true (graduated; hot-revert path)
  §2   Master flag empty/whitespace reads as default true
  §3   Master flag false-class disables
  §4   PostmortemReplResult is frozen
  §5   Reader returns [] on missing ledger (defensive)
  §6   Reader skips records of unrelated kinds
  §7   Reader parses output_repr into postmortem dict
  §8   Reader caps tail at limit
  §9   compute_distribution — pure aggregator over rows
  §10  compute_distribution — handles missing _terminal_context
       (Slice 2.4 happy path → defaults to COMPLETE/planned)
  §11  compute_distribution — empty rate computed correctly
  §12  compute_distribution — never raises on garbage rows
  §13  REPL help bypasses master-flag gate (discoverability)
  §14  REPL operational subcommands return DISABLED when off
  §15  REPL recent — happy path
  §16  REPL recent — empty ledger renders friendly message
  §17  REPL recent — bad limit returns BAD_LIMIT
  §18  REPL recent — limit clamped to MAX
  §19  REPL for-op — happy path
  §20  REPL for-op — unknown op returns UNKNOWN_OP
  §21  REPL for-op — bad op_id format returns UNKNOWN_OP
  §22  REPL distribution — happy path
  §23  REPL stats — alias for distribution
  §24  REPL unknown subcommand returns help text
  §25  Distribution renders WARNING when empty_rate >= 70% and
       total >= 20
  §26  ASCII-strict rendering (encode/decode round-trip)
  §27  publish_terminal_postmortem_persisted — never raises on
       broker outage
  §28  publish_terminal_postmortem_persisted — master-off returns None
  §29  Authority invariants — no orchestrator/policy/iron_gate imports
  §30  Public API exposed
  §31  GET endpoint registration — register_postmortem_routes
       attaches all 4 routes to a fake aiohttp app
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.postmortem_observability import (
    EVENT_TYPE_TERMINAL_POSTMORTEM_PERSISTED,
    HISTORY_DEFAULT_N,
    HISTORY_MAX_N,
    MAX_LINES_READ,
    POSTMORTEM_OBSERVABILITY_SCHEMA_VERSION,
    PostmortemDistribution,
    PostmortemReplResult,
    PostmortemReplStatus,
    compute_distribution,
    dispatch_postmortems_command,
    postmortem_observability_enabled,
    publish_terminal_postmortem_persisted,
    register_postmortem_routes,
    render_distribution,
)
from backend.core.ouroboros.governance.postmortem_observability import (
    _read_postmortem_rows,
)


# ===========================================================================
# §1-§3 — Master flag
# ===========================================================================


def test_master_flag_default_true(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED", raising=False,
    )
    assert postmortem_observability_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_reads_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED", val,
    )
    assert postmortem_observability_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_master_flag_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED", val,
    )
    assert postmortem_observability_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_master_flag_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED", val,
    )
    assert postmortem_observability_enabled() is False


# ===========================================================================
# §4 — Frozen schema
# ===========================================================================


def test_repl_result_is_frozen() -> None:
    r = PostmortemReplResult(
        status=PostmortemReplStatus.OK,
        rendered_text="x",
    )
    with pytest.raises(Exception):  # FrozenInstanceError varies by py-version
        r.status = PostmortemReplStatus.DISABLED  # type: ignore[misc]


# ===========================================================================
# §5-§8 — Ledger reader
# ===========================================================================


def _write_ledger(path: Path, records: List[dict]) -> None:
    """Helper — write the records as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _make_postmortem_row(
    *, op_id: str, kind: str = "terminal_postmortem",
    total_claims: int = 0, terminal_phase: str = "POSTMORTEM",
    reason: str = "postmortem", has_blocking: bool = False,
    must_hold_failed: int = 0,
) -> dict:
    """Construct a synthetic ledger row matching the on-disk shape."""
    pm = {
        "op_id": op_id,
        "session_id": "test",
        "started_unix": 1.0,
        "completed_unix": 2.0,
        "total_claims": total_claims,
        "must_hold_count": total_claims,
        "must_hold_failed": must_hold_failed,
        "has_blocking_failures": has_blocking,
        "outcomes": [],
        "_terminal_context": {
            "terminal_phase": terminal_phase,
            "status": "fail" if reason != "planned" else "ok",
            "reason": reason,
            "is_success": reason == "planned",
        },
    }
    return {
        "kind": kind,
        "op_id": op_id,
        "phase": terminal_phase,
        "ordinal": 0,
        "record_id": f"rec-{op_id}",
        "session_id": "test",
        "schema_version": "decision_record.1",
        "inputs_hash": "0" * 64,
        "wall_ts": 100.0,
        "monotonic_ts": 1.0,
        "output_repr": json.dumps(pm),
    }


def test_reader_returns_empty_on_missing_ledger(tmp_path) -> None:
    nonexistent = tmp_path / "missing" / "decisions.jsonl"
    rows = _read_postmortem_rows(nonexistent)
    assert rows == []


def test_reader_skips_unrelated_kinds(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    _write_ledger(path, [
        _make_postmortem_row(op_id="op-1"),
        {"kind": "property_claim", "op_id": "op-1"},
        {"kind": "advisor_verdict", "op_id": "op-1"},
        _make_postmortem_row(op_id="op-2", kind="verification_postmortem"),
    ])
    rows = _read_postmortem_rows(path)
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["terminal_postmortem", "verification_postmortem"]


def test_reader_parses_output_repr(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    _write_ledger(path, [
        _make_postmortem_row(op_id="op-1", total_claims=3),
    ])
    rows = _read_postmortem_rows(path)
    assert len(rows) == 1
    assert "postmortem" in rows[0]
    assert rows[0]["postmortem"]["total_claims"] == 3
    assert rows[0]["postmortem"]["_terminal_context"]["terminal_phase"] == "POSTMORTEM"


def test_reader_caps_tail_at_limit(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    _write_ledger(path, [
        _make_postmortem_row(op_id=f"op-{i}") for i in range(50)
    ])
    rows = _read_postmortem_rows(path, limit=10)
    assert len(rows) == 10
    # Newest-last → tail of the input
    assert rows[-1]["op_id"] == "op-49"
    assert rows[0]["op_id"] == "op-40"


def test_reader_drops_corrupt_lines(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(_make_postmortem_row(op_id="op-1")) + "\n")
        fh.write("{not valid json\n")
        fh.write(json.dumps(_make_postmortem_row(op_id="op-2")) + "\n")
    rows = _read_postmortem_rows(path)
    assert len(rows) == 2


# ===========================================================================
# §9-§12 — Distribution computer
# ===========================================================================


def test_distribution_pure_aggregator() -> None:
    rows = [
        {"kind": "terminal_postmortem", "op_id": "a", "postmortem": {
            "total_claims": 0,
            "_terminal_context": {"terminal_phase": "GENERATE",
                                  "reason": "noop"},
        }},
        {"kind": "terminal_postmortem", "op_id": "b", "postmortem": {
            "total_claims": 3,
            "_terminal_context": {"terminal_phase": "GENERATE",
                                  "reason": "noop"},
        }},
    ]
    dist = compute_distribution(rows)
    assert dist.total == 2
    assert dist.empty_claim_count == 1
    assert dist.empty_claim_rate == 0.5
    assert dist.terminal_phase_histogram == {"GENERATE": 2}
    assert dist.reason_histogram == {"noop": 2}
    assert dist.kind_histogram == {"terminal_postmortem": 2}


def test_distribution_handles_missing_terminal_context() -> None:
    """verification_postmortem records (Slice 2.4 happy path) lack
    the _terminal_context block. Distribution treats that as
    COMPLETE/planned defaults."""
    rows = [
        {"kind": "verification_postmortem", "op_id": "a", "postmortem": {
            "total_claims": 5,
            # No _terminal_context
        }},
    ]
    dist = compute_distribution(rows)
    assert dist.terminal_phase_histogram == {"COMPLETE": 1}
    assert dist.reason_histogram == {"planned": 1}


def test_distribution_empty_rate_correct() -> None:
    # 18/20 empty matches today's soak-#3 reality
    rows = [
        {"kind": "terminal_postmortem", "op_id": f"op-{i}", "postmortem": {
            "total_claims": 0 if i < 18 else 3,
            "_terminal_context": {"terminal_phase": "POSTMORTEM",
                                  "reason": "postmortem"},
        }} for i in range(20)
    ]
    dist = compute_distribution(rows)
    assert dist.total == 20
    assert dist.empty_claim_count == 18
    assert dist.empty_claim_rate == 0.9


def test_distribution_never_raises_on_garbage() -> None:
    rows: List[Any] = [
        None,
        "not a dict",
        42,
        {"kind": "terminal_postmortem", "op_id": "valid", "postmortem":
         {"total_claims": 3}},
        {"kind": "terminal_postmortem", "op_id": "garbage_pm",
         "postmortem": "not a dict"},
        {"kind": "terminal_postmortem"},  # no postmortem at all
    ]
    dist = compute_distribution(rows)  # should not raise
    # total counts every row that's a dict (even with missing/bad postmortem)
    assert dist.total >= 1


# ===========================================================================
# §13-§24 — REPL dispatcher
# ===========================================================================


@pytest.fixture
def populated_ledger(tmp_path):
    """Synthetic ledger with mixed claim density + terminal contexts."""
    path = tmp_path / "decisions.jsonl"
    rows = [
        _make_postmortem_row(
            op_id=f"op-{i}",
            total_claims=0 if i < 7 else 3,  # 7 empty, 3 non-empty
            terminal_phase="GENERATE" if i % 2 == 0 else "POSTMORTEM",
            reason="background_accepted" if i % 2 == 0 else "postmortem",
        )
        for i in range(10)
    ]
    _write_ledger(path, rows)
    return path


def test_help_bypasses_master_flag(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED", "false",
    )
    res = dispatch_postmortems_command(["help"])
    assert res.status is PostmortemReplStatus.OK
    assert "/postmortems" in res.rendered_text
    assert "subcommands" in res.rendered_text


def test_operational_subcommand_disabled(monkeypatch, populated_ledger) -> None:
    monkeypatch.setenv(
        "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED", "false",
    )
    res = dispatch_postmortems_command(
        ["recent"], ledger_path=populated_ledger,
    )
    assert res.status is PostmortemReplStatus.DISABLED
    assert "disabled" in res.rendered_text.lower()


def test_repl_recent_happy_path(populated_ledger) -> None:
    res = dispatch_postmortems_command(
        ["recent"], ledger_path=populated_ledger,
    )
    assert res.status is PostmortemReplStatus.OK
    # Default limit = 10; ledger has 10 rows; all rendered
    assert "[postmortem]" in res.rendered_text


def test_repl_recent_empty_ledger(tmp_path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("")
    res = dispatch_postmortems_command(
        ["recent"], ledger_path=empty,
    )
    assert res.status is PostmortemReplStatus.EMPTY


def test_repl_recent_bad_limit(populated_ledger) -> None:
    res = dispatch_postmortems_command(
        ["recent", "abc"], ledger_path=populated_ledger,
    )
    assert res.status is PostmortemReplStatus.BAD_LIMIT


def test_repl_recent_limit_clamped(populated_ledger) -> None:
    res = dispatch_postmortems_command(
        ["recent", str(HISTORY_MAX_N + 1000)],
        ledger_path=populated_ledger,
    )
    # Shouldn't crash; rows capped to MAX
    assert res.status is PostmortemReplStatus.OK


def test_repl_for_op_happy_path(populated_ledger) -> None:
    res = dispatch_postmortems_command(
        ["for-op", "op-5"], ledger_path=populated_ledger,
    )
    assert res.status is PostmortemReplStatus.OK
    assert "op-5" in res.rendered_text
    assert res.record is not None


def test_repl_for_op_unknown_returns_unknown_op(populated_ledger) -> None:
    res = dispatch_postmortems_command(
        ["for-op", "nonexistent-op"], ledger_path=populated_ledger,
    )
    assert res.status is PostmortemReplStatus.UNKNOWN_OP


def test_repl_for_op_bad_id_format(populated_ledger) -> None:
    res = dispatch_postmortems_command(
        ["for-op", "not valid id with spaces"],
        ledger_path=populated_ledger,
    )
    assert res.status is PostmortemReplStatus.UNKNOWN_OP


def test_repl_distribution_happy_path(populated_ledger) -> None:
    res = dispatch_postmortems_command(
        ["distribution"], ledger_path=populated_ledger,
    )
    assert res.status is PostmortemReplStatus.OK
    assert "distribution" in res.rendered_text.lower()
    # 7/10 = 70% empty → WARNING fires (with total >= 20 floor for warning)
    # Our fixture only has 10, so warning may NOT fire — that's fine


def test_repl_stats_alias_for_distribution(populated_ledger) -> None:
    res_dist = dispatch_postmortems_command(
        ["distribution"], ledger_path=populated_ledger,
    )
    res_stats = dispatch_postmortems_command(
        ["stats"], ledger_path=populated_ledger,
    )
    assert res_dist.rendered_text == res_stats.rendered_text


def test_repl_unknown_subcommand_returns_help(populated_ledger) -> None:
    res = dispatch_postmortems_command(
        ["bogus"], ledger_path=populated_ledger,
    )
    assert res.status is PostmortemReplStatus.UNKNOWN_SUBCOMMAND
    assert "/postmortems" in res.rendered_text


# ===========================================================================
# §25 — Distribution warning text fires above threshold
# ===========================================================================


def test_distribution_warning_fires_above_threshold() -> None:
    rows = [
        {"kind": "terminal_postmortem", "op_id": f"op-{i}", "postmortem": {
            "total_claims": 0 if i < 18 else 3,
            "_terminal_context": {"terminal_phase": "POSTMORTEM",
                                  "reason": "postmortem"},
        }} for i in range(25)
    ]
    dist = compute_distribution(rows)
    text = render_distribution(dist)
    assert "WARNING" in text
    assert "MetaSensor" in text  # remediation hint


def test_distribution_warning_does_not_fire_below_threshold() -> None:
    rows = [
        {"kind": "terminal_postmortem", "op_id": f"op-{i}", "postmortem": {
            "total_claims": 3,  # all healthy
            "_terminal_context": {"terminal_phase": "POSTMORTEM",
                                  "reason": "planned"},
        }} for i in range(25)
    ]
    dist = compute_distribution(rows)
    text = render_distribution(dist)
    assert "WARNING" not in text


# ===========================================================================
# §26 — ASCII-strict rendering
# ===========================================================================


def test_ascii_strict_render() -> None:
    rows = [
        _make_postmortem_row(
            op_id="op-unicode-test", total_claims=0,
            reason="weird unicode: ö ñ ★",
        ),
    ]
    rows[0]["op_id"] = "op-unicode-test"  # rebind
    pm = json.loads(rows[0]["output_repr"])
    pm["_terminal_context"]["reason"] = "weird unicode: ö ñ ★"
    rows[0]["output_repr"] = json.dumps(pm)
    parsed = compute_distribution([
        {**rows[0], "postmortem": pm},
    ])
    text = render_distribution(parsed)
    # ASCII-encodable round-trip — non-ASCII replaced with `?`
    text.encode("ascii")  # MUST NOT raise


# ===========================================================================
# §27-§28 — SSE publisher
# ===========================================================================


def test_publish_never_raises_on_broker_outage() -> None:
    """Even if the broker module is missing, publish returns None."""
    result = publish_terminal_postmortem_persisted(
        op_id="op-test",
        record_id="rec-1",
        terminal_phase="GENERATE",
        total_claims=3,
        has_blocking_failures=False,
        reason="noop",
    )
    # Either None (broker disabled) or an event id — never raises
    assert result is None or isinstance(result, str)


def test_publish_master_off_returns_none(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED", "false",
    )
    result = publish_terminal_postmortem_persisted(
        op_id="op-test",
        record_id="rec-1",
        terminal_phase="GENERATE",
        total_claims=3,
        has_blocking_failures=False,
    )
    assert result is None


# ===========================================================================
# §29 — Authority invariants
# ===========================================================================


def test_no_authority_imports() -> None:
    from backend.core.ouroboros.governance import postmortem_observability
    src = inspect.getsource(postmortem_observability)
    forbidden = (
        "orchestrator", "policy", "iron_gate", "risk_tier",
        "change_engine", "candidate_generator", "gate",
        "semantic_guardian",
    )
    for token in forbidden:
        # Allow string-literal mentions in docstrings / comments;
        # forbid actual imports.
        assert (
            f"from backend.core.ouroboros.governance.{token}" not in src
        ), f"postmortem_observability must not import {token}"
        assert (
            f"import backend.core.ouroboros.governance.{token}" not in src
        ), f"postmortem_observability must not import {token}"


def test_no_write_mode_strings() -> None:
    """Source-grep pin: this module must NEVER open files in write
    mode. It's strictly read-only over the determinism ledger."""
    from backend.core.ouroboros.governance import postmortem_observability
    src = inspect.getsource(postmortem_observability)
    forbidden_modes = (', "w"', ', "a"', ", 'w'", ", 'a'")
    for mode in forbidden_modes:
        assert mode not in src, (
            f"postmortem_observability must not open files in write "
            f"mode (found {mode!r} in source)"
        )


# ===========================================================================
# §30 — Public API
# ===========================================================================


def test_public_api_exposed() -> None:
    from backend.core.ouroboros.governance import postmortem_observability
    expected = {
        "EVENT_TYPE_TERMINAL_POSTMORTEM_PERSISTED",
        "PostmortemDistribution",
        "PostmortemReplResult",
        "PostmortemReplStatus",
        "compute_distribution",
        "dispatch_postmortems_command",
        "postmortem_observability_enabled",
        "publish_terminal_postmortem_persisted",
        "register_postmortem_routes",
    }
    for name in expected:
        assert name in postmortem_observability.__all__, (
            f"{name} missing from __all__"
        )


# ===========================================================================
# §31 — Route registration
# ===========================================================================


def test_register_routes_attaches_4_endpoints() -> None:
    """Verify register_postmortem_routes wires all 4 routes onto a
    fake aiohttp app router."""
    captured = []

    class FakeRouter:
        def add_get(self, path, handler):
            captured.append((path, handler))

    class FakeApp:
        router = FakeRouter()

    register_postmortem_routes(FakeApp())
    paths = [p for p, _ in captured]
    assert "/observability/postmortems" in paths
    assert "/observability/postmortems/recent" in paths
    assert "/observability/postmortems/distribution" in paths
    assert "/observability/postmortems/{op_id}" in paths
