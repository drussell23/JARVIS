"""SerpentFlow /mutation command wiring tests.

Coverage:
  * Command is visible in /help.
  * /mutation with no args prints usage.
  * /mutation <missing_file> prints error.
  * /mutation <src> with no test matches prints error.
  * /mutation <src> -- <tests> dispatches to run_mutation_test with
    the explicit test paths.
  * _discover_tests_for finds Session-W-style test_<stem>*.py files.
  * AST canary: the dispatch loop has a branch that routes ``/mutation``
    into _handle_mutation (locks against silent refactor deletion).
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow


def _mk_cli(tmp_path):
    """Build an isolated SerpentREPL without starting the prompt loop."""
    flow = SerpentFlow(session_id="test", cost_cap_usd=1.0)
    # The REPL class is nested inside serpent_flow module — grab by name.
    import backend.core.ouroboros.battle_test.serpent_flow as sf
    repl_cls = None
    for name in dir(sf):
        obj = getattr(sf, name)
        if inspect.isclass(obj) and hasattr(obj, "_handle_mutation"):
            repl_cls = obj
            break
    assert repl_cls is not None, "could not locate REPL class"
    repl = repl_cls(flow=flow)
    return repl, flow


@pytest.mark.asyncio
async def test_mutation_usage_on_empty_args(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repl, flow = _mk_cli(tmp_path)
    printed = []
    flow.console.print = lambda *args, **kwargs: printed.append(
        " ".join(str(a) for a in args)
    )
    await repl._handle_mutation("/mutation")
    combined = "\n".join(printed)
    assert "Usage" in combined
    assert "/mutation" in combined


@pytest.mark.asyncio
async def test_mutation_missing_source_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repl, flow = _mk_cli(tmp_path)
    printed = []
    flow.console.print = lambda *args, **kwargs: printed.append(
        " ".join(str(a) for a in args)
    )
    await repl._handle_mutation("/mutation nonexistent_file.py")
    combined = "\n".join(printed)
    assert "not found" in combined.lower()


@pytest.mark.asyncio
async def test_mutation_no_test_discovery_hits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "lonely.py"
    src.write_text("def f(): return 1\n")
    (tmp_path / "tests").mkdir()
    repl, flow = _mk_cli(tmp_path)
    printed = []
    flow.console.print = lambda *args, **kwargs: printed.append(
        " ".join(str(a) for a in args)
    )
    await repl._handle_mutation("/mutation lonely.py")
    combined = "\n".join(printed)
    assert "No test files" in combined


@pytest.mark.asyncio
async def test_mutation_explicit_test_paths_dispatched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "sut.py"
    tst = tmp_path / "test_sut.py"
    src.write_text("def f(x): return x + 1\n")
    tst.write_text(
        "def test_noop():\n    assert True\n"
    )
    repl, flow = _mk_cli(tmp_path)
    printed = []
    flow.console.print = lambda *args, **kwargs: printed.append(
        " ".join(str(a) for a in args)
    )
    fake_result = MagicMock()
    fake_result.to_json.return_value = {"score": 0.95}
    with patch(
        "backend.core.ouroboros.governance.mutation_tester.run_mutation_test",
        return_value=fake_result,
    ) as mock_run, patch(
        "backend.core.ouroboros.governance.mutation_tester.render_console_report",
        return_value="mocked report",
    ):
        await repl._handle_mutation(f"/mutation sut.py -- test_sut.py")
    assert mock_run.called, "run_mutation_test was not invoked"
    call = mock_run.call_args
    assert call.kwargs["test_files"] == [Path("test_sut.py")]
    combined = "\n".join(printed)
    assert "mocked report" in combined


def test_discover_tests_for_session_w_pattern(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_test_failure_sensor.py").write_text("")
    (tmp_path / "tests" / "test_test_failure_sensor_dedup.py").write_text("")
    (tmp_path / "tests" / "test_test_failure_sensor_ttl.py").write_text("")
    (tmp_path / "tests" / "test_unrelated.py").write_text("")
    repl, _ = _mk_cli(tmp_path)
    discovered = repl._discover_tests_for(Path("test_failure_sensor.py"))
    names = sorted(p.name for p in discovered)
    assert names == [
        "test_test_failure_sensor.py",
        "test_test_failure_sensor_dedup.py",
        "test_test_failure_sensor_ttl.py",
    ]


def test_dispatch_loop_routes_slash_mutation():
    """AST canary — the dispatch loop must contain a branch that routes
    ``/mutation`` into ``_handle_mutation``. Catches silent refactors
    that would drop the wiring.
    """
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text(encoding="utf-8")
    # Scan for the exact dispatch snippet
    assert 'line.startswith("/mutation")' in src
    assert "await self._handle_mutation(line)" in src


def test_help_mentions_mutation_command():
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text(encoding="utf-8")
    # Help entry line exists.
    assert "/mutation <src>" in src
    assert "meta-test" in src.lower()


@pytest.mark.asyncio
async def test_survivors_only_renders_each_survivor_once(tmp_path, monkeypatch, caplog):
    """`/mutation --survivors-only` must emit one printed line + one
    structured log record per survivor. Zero-survivor runs emit a
    single 'all caught' marker."""
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "sut.py"
    tst = tmp_path / "test_sut.py"
    src.write_text("def f(x): return x + 1\n")
    tst.write_text("def test_pass(): assert True\n")

    from backend.core.ouroboros.governance.mutation_tester import (
        Mutant, MutantOutcome, MutationResult,
    )
    fake_mut = Mutant(
        op="bool_flip", source_file="sut.py",
        line=42, col=8, original="True", mutated="False",
        patched_src="",
    )
    fake_out = MutantOutcome(
        mutant=fake_mut, caught=False,
        reason="survived", duration_s=0.1,
    )
    fake_result = MutationResult(
        source_file="sut.py",
        total_mutants=5, caught=4, survived=1,
        score=0.80, grade="B",
        survivors=(fake_out,),
    )
    repl, flow = _mk_cli(tmp_path)
    printed = []
    flow.console.print = lambda *args, **kwargs: printed.append(
        " ".join(str(a) for a in args)
    )
    with patch(
        "backend.core.ouroboros.governance.mutation_tester.run_mutation_test",
        return_value=fake_result,
    ):
        with caplog.at_level("INFO", logger="Ouroboros.MutationTelemetry"):
            await repl._handle_mutation(
                "/mutation --survivors-only sut.py -- test_sut.py"
            )
    combined = "\n".join(printed)
    assert "SURVIVED" in combined
    assert "bool_flip" in combined
    # Structured telemetry: one INFO record carrying all the fields a
    # downstream consumer needs to route the bypass.
    tel_records = [
        r for r in caplog.records
        if r.name == "Ouroboros.MutationTelemetry"
    ]
    assert len(tel_records) == 1
    msg = tel_records[0].getMessage()
    assert "line=42" in msg
    assert "op=bool_flip" in msg
    assert "'True'" in msg


@pytest.mark.asyncio
async def test_survivors_only_emits_clean_marker_when_no_survivors(
    tmp_path, monkeypatch, caplog,
):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "sut.py"
    tst = tmp_path / "test_sut.py"
    src.write_text("def f(): return 1\n")
    tst.write_text("def test_x(): assert True\n")

    from backend.core.ouroboros.governance.mutation_tester import (
        MutationResult,
    )
    clean_result = MutationResult(
        source_file="sut.py",
        total_mutants=3, caught=3, survived=0,
        score=1.0, grade="A",
        survivors=(),
    )
    repl, flow = _mk_cli(tmp_path)
    printed = []
    flow.console.print = lambda *args, **kwargs: printed.append(
        " ".join(str(a) for a in args)
    )
    with patch(
        "backend.core.ouroboros.governance.mutation_tester.run_mutation_test",
        return_value=clean_result,
    ):
        with caplog.at_level("INFO", logger="Ouroboros.MutationTelemetry"):
            await repl._handle_mutation(
                "/mutation --survivors-only sut.py -- test_sut.py"
            )
    combined = "\n".join(printed)
    assert "No survivors" in combined
    tel_records = [
        r for r in caplog.records
        if r.name == "Ouroboros.MutationTelemetry"
    ]
    assert len(tel_records) == 1
    assert "survivors=0" in tel_records[0].getMessage()
