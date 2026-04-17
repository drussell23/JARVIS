"""``/resume`` command tests — ResumeScanner + ResumeExecutor + handler wiring.

Strategy: build synthetic per-op ledger files in a tmp dir that match
the real JSONL shape (``{op_id, state, data, timestamp, wall_time}``).
Assert orphan detection, intent extraction, safety gate, and
re-enqueue flow via a mock intake router. No real git / no real
governance stack required — the module's scope is narrow enough that
unit-level coverage is honest.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import time
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.battle_test import resume_command as rc
from backend.core.ouroboros.battle_test.resume_command import (
    OrphanEntry,
    ResumeExecutor,
    ResumePlan,
    ResumeResult,
    ResumeScanner,
    ResumeTarget,
    max_age_s,
    parse_resume_args,
    render_plan,
    resume_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures — synthetic ledger builder
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_RESUME_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _write_ledger(
    dir_: Path,
    op_id: str,
    states: List[tuple],
    *,
    base_wall: float = None,
) -> Path:
    """Write a ledger file for ``op_id`` with ``states`` = list of
    ``(state_name, data_dict)`` tuples in temporal order. Returns the
    path."""
    if base_wall is None:
        base_wall = time.time() - 300.0  # 5min ago by default
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{op_id}.jsonl"
    lines: List[str] = []
    for i, (state, data) in enumerate(states):
        lines.append(json.dumps({
            "op_id": op_id,
            "state": state,
            "data": data,
            "timestamp": float(i),
            "wall_time": base_wall + i,
        }))
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# (1) Env gates + parser
# ---------------------------------------------------------------------------


def test_resume_enabled_default_on():
    assert resume_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off"])
def test_resume_disabled_values(monkeypatch, val):
    monkeypatch.setenv("JARVIS_RESUME_ENABLED", val)
    assert resume_enabled() is False


def test_max_age_default_24h():
    assert max_age_s() == 86400


def test_max_age_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_RESUME_MAX_AGE_S", "3600")
    assert max_age_s() == 3600


def test_max_age_min_clamp(monkeypatch):
    monkeypatch.setenv("JARVIS_RESUME_MAX_AGE_S", "10")
    assert max_age_s() == 60  # clamped to minimum


@pytest.mark.parametrize("raw,expected", [
    ("/resume",                ("latest", "", None)),
    ("/resume list",           ("list", "", None)),
    ("/resume all",            ("all", "", None)),
    ("/resume latest",         ("latest", "", None)),
    ("/resume 019d9368",       ("specific", "019d9368", None)),
    ("resume 019d9368",        ("specific", "019d9368", None)),
])
def test_parse_resume_args(raw, expected):
    assert parse_resume_args(raw) == expected


# ---------------------------------------------------------------------------
# (2) Scanner — orphan detection
# ---------------------------------------------------------------------------


def test_scan_detects_orphan_missing_terminal(tmp_path):
    _write_ledger(
        tmp_path, "op-019d9368-abc-cau",
        [
            ("planned", {"goal": "add feature X", "target_files": ["foo.py"]}),
            ("sandboxing", {"phase": "sandbox"}),
            ("validating", {"syntax_valid": True}),
            # No terminal entry — orphan.
        ],
    )
    scanner = ResumeScanner(ledger_root=tmp_path)
    orphans = scanner.scan_orphans()
    assert len(orphans) == 1
    o = orphans[0]
    assert o.op_id == "op-019d9368-abc-cau"
    assert o.last_state == "validating"
    assert o.goal == "add feature X"
    assert o.target_files == ("foo.py",)


def test_scan_skips_applied_op(tmp_path):
    _write_ledger(
        tmp_path, "op-done-cau",
        [
            ("planned", {"goal": "g", "target_files": ["a.py"]}),
            ("applying", {}),
            ("applied", {"commit": "abc123"}),
        ],
    )
    assert ResumeScanner(ledger_root=tmp_path).scan_orphans() == []


@pytest.mark.parametrize("terminal", ["applied", "failed", "blocked", "rolled_back"])
def test_scan_skips_all_terminal_states(tmp_path, terminal):
    _write_ledger(
        tmp_path, f"op-{terminal}-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]}), (terminal, {})],
    )
    assert ResumeScanner(ledger_root=tmp_path).scan_orphans() == []


def test_scan_accepts_singular_target_file(tmp_path):
    """Some ledger entries use 'target_file' (singular) — must coerce
    to a single-element tuple."""
    _write_ledger(
        tmp_path, "op-sing-cau",
        [("planned", {"goal": "g", "target_file": "solo.py"})],
    )
    orphans = ResumeScanner(ledger_root=tmp_path).scan_orphans()
    assert orphans[0].target_files == ("solo.py",)


def test_scan_newest_orphan_first(tmp_path):
    now = time.time()
    _write_ledger(
        tmp_path, "op-old-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
        base_wall=now - 1000,
    )
    _write_ledger(
        tmp_path, "op-new-cau",
        [("planned", {"goal": "g", "target_files": ["b.py"]})],
        base_wall=now - 10,
    )
    orphans = ResumeScanner(ledger_root=tmp_path).scan_orphans()
    assert [o.op_id for o in orphans] == ["op-new-cau", "op-old-cau"]


def test_scan_missing_dir_returns_empty(tmp_path):
    scanner = ResumeScanner(ledger_root=tmp_path / "does_not_exist")
    assert scanner.scan_orphans() == []


def test_scan_skips_malformed_lines(tmp_path):
    """A corrupt line in the middle must not break subsequent parse."""
    path = tmp_path / "op-mix-cau.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    valid = json.dumps({
        "op_id": "op-mix-cau", "state": "planned",
        "data": {"goal": "g", "target_files": ["a.py"]},
        "timestamp": 0, "wall_time": time.time(),
    })
    corrupt = "{broken json"
    path.write_text("\n".join([valid, corrupt]) + "\n")
    orphans = ResumeScanner(ledger_root=tmp_path).scan_orphans()
    assert len(orphans) == 1
    assert orphans[0].goal == "g"


def test_scan_sets_near_terminal_flag(tmp_path):
    _write_ledger(
        tmp_path, "op-near-cau",
        [
            ("planned", {"goal": "g", "target_files": ["a.py"]}),
            ("gating", {}),
            ("applying", {}),
            # No terminal — died mid-apply.
        ],
    )
    orphans = ResumeScanner(ledger_root=tmp_path).scan_orphans()
    assert orphans[0].last_state == "applying"
    assert orphans[0].is_near_terminal is True


# ---------------------------------------------------------------------------
# (3) Plan — safety gate
# ---------------------------------------------------------------------------


def test_plan_rejects_when_env_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_RESUME_ENABLED", "0")
    _write_ledger(
        tmp_path, "op-x-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan()
    assert plan.has_global_errors
    assert any("disabled" in e.lower() for e in plan.global_errors)


def test_plan_rejects_active_ops(tmp_path):
    _write_ledger(
        tmp_path, "op-active-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
    )
    gls = MagicMock()
    gls._active_ops = {"op-active-cau"}
    plan = ResumeScanner(
        ledger_root=tmp_path, governed_loop_service=gls,
    ).plan()
    assert len(plan.targets) == 1
    t = plan.targets[0]
    assert not t.resumable
    assert any("actively running" in r for r in t.reasons)


def test_plan_rejects_stale_orphans(tmp_path, monkeypatch):
    """Orphan older than JARVIS_RESUME_MAX_AGE_S must be marked unresumable."""
    monkeypatch.setenv("JARVIS_RESUME_MAX_AGE_S", "60")
    _write_ledger(
        tmp_path, "op-stale-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
        base_wall=time.time() - 3600,  # 1h old
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan()
    t = plan.targets[0]
    assert not t.resumable
    assert any("exceeds cutoff" in r for r in t.reasons)


def test_plan_rejects_missing_goal(tmp_path):
    """Orphan without a PLANNED entry cannot be re-synthesized."""
    _write_ledger(
        tmp_path, "op-no-plan-cau",
        [
            ("sandboxing", {"phase": "sandbox"}),
            ("validating", {"syntax_valid": True}),
        ],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan()
    t = plan.targets[0]
    assert not t.resumable
    assert any("no PLANNED" in r for r in t.reasons)


def test_plan_near_terminal_adds_warning(tmp_path):
    _write_ledger(
        tmp_path, "op-apply-cau",
        [
            ("planned", {"goal": "g", "target_files": ["a.py"]}),
            ("applying", {}),
        ],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan()
    t = plan.targets[0]
    assert t.resumable  # near-terminal is advisory, not blocking
    assert any("may already be on disk" in w for w in t.warnings)


def test_plan_specific_requires_prefix(tmp_path):
    _write_ledger(
        tmp_path, "op-any-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan(
        mode="specific", op_id_prefix="",
    )
    assert plan.has_global_errors


def test_plan_specific_matches_prefix(tmp_path):
    _write_ledger(
        tmp_path, "op-019d9368-abc-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
    )
    _write_ledger(
        tmp_path, "op-019d9999-xyz-cau",
        [("planned", {"goal": "g2", "target_files": ["b.py"]})],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan(
        mode="specific", op_id_prefix="019d9368",
    )
    assert not plan.has_global_errors
    assert len(plan.targets) == 1
    assert plan.targets[0].orphan.op_id == "op-019d9368-abc-cau"


def test_plan_latest_mode_returns_single(tmp_path):
    for i in range(3):
        _write_ledger(
            tmp_path, f"op-{i}-cau",
            [("planned", {"goal": f"g{i}", "target_files": ["a.py"]})],
            base_wall=time.time() - 10 * (3 - i),
        )
    plan = ResumeScanner(ledger_root=tmp_path).plan(mode="latest")
    assert len(plan.targets) == 1
    # Newest — op-2 — wins.
    assert plan.targets[0].orphan.op_id == "op-2-cau"


def test_plan_all_mode_returns_every_orphan(tmp_path):
    for i in range(3):
        _write_ledger(
            tmp_path, f"op-{i}-cau",
            [("planned", {"goal": f"g{i}", "target_files": ["a.py"]})],
        )
    plan = ResumeScanner(ledger_root=tmp_path).plan(mode="all")
    assert len(plan.targets) == 3


# ---------------------------------------------------------------------------
# (4) Executor — happy path + skip reasons + lineage
# ---------------------------------------------------------------------------


def _make_mock_router():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    return router


def test_executor_enqueues_and_writes_lineage(tmp_path):
    _write_ledger(
        tmp_path, "op-target-cau",
        [("planned", {"goal": "add feat", "target_files": ["foo.py"]})],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan()
    router = _make_mock_router()

    result = asyncio.run(ResumeExecutor(
        repo_name="jarvis", intake_router=router,
    ).execute(plan))

    assert result.executed is True
    assert len(result.resumed_op_ids) == 1
    assert result.parent_op_ids == ("op-target-cau",)
    router.ingest.assert_called_once()
    # envelope carried the parent op id.
    envelope = router.ingest.call_args.args[0]
    assert envelope.evidence.get("resume_of_op") == "op-target-cau"

    # Lineage entry appended to the orphan ledger.
    ledger_path = tmp_path / "op-target-cau.jsonl"
    last_line = ledger_path.read_text().strip().split("\n")[-1]
    last_entry = json.loads(last_line)
    assert last_entry["state"] == "resumed"
    assert last_entry["data"]["resumed_to_env_id"]


def test_executor_list_mode_does_not_mutate(tmp_path):
    _write_ledger(
        tmp_path, "op-x-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan(mode="list")
    router = _make_mock_router()

    result = asyncio.run(ResumeExecutor(
        repo_name="jarvis", intake_router=router,
    ).execute(plan))

    assert result.executed is False
    router.ingest.assert_not_called()
    # Ledger unchanged (no resumed entry appended).
    ledger = (tmp_path / "op-x-cau.jsonl").read_text()
    assert '"resumed"' not in ledger


def test_executor_skips_unresumable_with_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RESUME_MAX_AGE_S", "60")
    _write_ledger(
        tmp_path, "op-stale-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
        base_wall=time.time() - 3600,
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan(mode="all")
    router = _make_mock_router()

    result = asyncio.run(ResumeExecutor(
        repo_name="jarvis", intake_router=router,
    ).execute(plan))

    assert result.executed is False  # nothing actually ran
    assert len(result.skipped_reasons) == 1
    parent_id, reason = result.skipped_reasons[0]
    assert parent_id == "op-stale-cau"
    assert "cutoff" in reason.lower()


def test_executor_skips_on_ingest_rejection(tmp_path):
    _write_ledger(
        tmp_path, "op-rej-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan()
    router = MagicMock()
    router.ingest = AsyncMock(return_value="backpressure")

    result = asyncio.run(ResumeExecutor(
        repo_name="jarvis", intake_router=router,
    ).execute(plan))

    assert result.executed is False
    assert len(result.skipped_reasons) == 1
    assert "backpressure" in result.skipped_reasons[0][1]


def test_executor_emits_decision_on_success(tmp_path):
    _write_ledger(
        tmp_path, "op-emit-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan()
    router = _make_mock_router()
    comm = MagicMock()
    comm.emit_decision = AsyncMock()

    asyncio.run(ResumeExecutor(
        repo_name="jarvis", intake_router=router, comm=comm,
    ).execute(plan))

    comm.emit_decision.assert_called_once()
    call = comm.emit_decision.call_args.kwargs
    assert call["outcome"] == "resumed"
    assert "resume_of=op-emit-cau" in call["reason_code"]


def test_executor_global_errors_block_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RESUME_ENABLED", "0")
    _write_ledger(
        tmp_path, "op-x-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan()
    router = _make_mock_router()
    result = asyncio.run(ResumeExecutor(
        repo_name="jarvis", intake_router=router,
    ).execute(plan))
    assert result.executed is False
    assert "disabled" in result.error.lower()
    router.ingest.assert_not_called()


# ---------------------------------------------------------------------------
# (5) render_plan — Rich output + plain fallback
# ---------------------------------------------------------------------------


def test_render_plan_rich_contains_key_tokens(tmp_path):
    _write_ledger(
        tmp_path, "op-render-cau",
        [
            ("planned", {"goal": "refactor foo module", "target_files": ["foo.py"]}),
            ("sandboxing", {}),
        ],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan(mode="all")

    from rich.console import Console
    console = Console(record=True, width=160, force_terminal=True)
    console.print(render_plan(plan))
    text = console.export_text()

    assert "/resume" in text
    assert "render" in text  # op short-id
    assert "sandboxing" in text
    assert "refactor foo module" in text
    # Honesty banner always present.
    assert "not preserved" in text.lower()


def test_render_plan_shows_skipped_reasons(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RESUME_MAX_AGE_S", "60")
    _write_ledger(
        tmp_path, "op-old-cau",
        [("planned", {"goal": "g", "target_files": ["a.py"]})],
        base_wall=time.time() - 3600,
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan(mode="all")

    from rich.console import Console
    console = Console(record=True, width=160, force_terminal=True)
    console.print(render_plan(plan))
    text = console.export_text()
    assert "✗" in text or "cutoff" in text.lower()


def test_render_plan_near_terminal_warning(tmp_path):
    _write_ledger(
        tmp_path, "op-near-cau",
        [
            ("planned", {"goal": "g", "target_files": ["a.py"]}),
            ("applying", {}),
        ],
    )
    plan = ResumeScanner(ledger_root=tmp_path).plan(mode="all")

    from rich.console import Console
    console = Console(record=True, width=160, force_terminal=True)
    console.print(render_plan(plan))
    text = console.export_text()
    assert "⚠" in text or "on disk" in text.lower()


# ---------------------------------------------------------------------------
# (6) OrphanEntry helpers
# ---------------------------------------------------------------------------


def test_short_op_id_handles_standard_format():
    o = OrphanEntry(
        op_id="op-019d9368-654b-7612-a031-6507ffde327c-cau",
        ledger_path=Path("/tmp/x"),
        last_state="validating",
        last_wall_time=time.time(),
    )
    assert o.short_op_id == "019d9368"


def test_short_op_id_handles_minimal_format():
    o = OrphanEntry(
        op_id="foo",
        ledger_path=Path("/tmp/x"),
        last_state="validating",
        last_wall_time=time.time(),
    )
    # Degrades to prefix of 10.
    assert o.short_op_id == "foo"


# ---------------------------------------------------------------------------
# (7) AST canaries — handler wiring + module surface
# ---------------------------------------------------------------------------


def _read(parts: tuple) -> str:
    base = Path(__file__).resolve().parent.parent.parent
    return base.joinpath(*parts).read_text(encoding="utf-8")


def test_harness_dispatches_slash_resume():
    src = _read((
        "backend", "core", "ouroboros", "battle_test", "harness.py",
    ))
    assert "_repl_cmd_resume_op" in src
    assert "/resume" in src


def test_harness_registers_boot_orphan_notification():
    src = _read((
        "backend", "core", "ouroboros", "battle_test", "harness.py",
    ))
    assert "_notify_orphaned_ops_at_boot" in src
    # Must be called during boot, not just defined.
    tree = ast.parse(src)
    called = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_notify_orphaned_ops_at_boot"
        for n in ast.walk(tree)
    )
    assert called


def test_resume_module_has_public_surface():
    for name in (
        "ResumeScanner", "ResumeExecutor", "ResumePlan", "ResumeTarget",
        "OrphanEntry", "parse_resume_args", "render_plan",
        "resume_enabled", "max_age_s",
    ):
        assert hasattr(rc, name), f"resume_command.{name} missing"


def test_terminal_states_match_operation_state_enum():
    """Ensure the module's terminal-state set is consistent with the
    ``OperationState`` enum. If governance/ledger.py adds a new terminal
    state, this test fails and forces us to update the scanner."""
    from backend.core.ouroboros.governance.ledger import OperationState
    expected_terminals = {
        OperationState.APPLIED.value,
        OperationState.FAILED.value,
        OperationState.BLOCKED.value,
        OperationState.ROLLED_BACK.value,
    }
    assert expected_terminals == rc._TERMINAL_STATES
