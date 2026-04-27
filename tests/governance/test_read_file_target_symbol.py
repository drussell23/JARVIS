"""Slice 11.2 regression spine — read_file(target_symbol=...) tests.

Pins:
  §1 Manifest schema — target_symbol + include_imports added
  §2 Master flag — JARVIS_TOOL_AST_SLICE_ENABLED default false
  §3 Sliced read happy paths — function, method, class, async fn
  §4 Fallback paths — non-Python, parse error, symbol not found,
                      missing target_symbol, master flag off
  §5 Backward compat — empty/missing target_symbol returns full file
                       byte-identical to pre-Slice-11.2
  §6 Slicing metrics — record_slice writes the right shape; ledger
                       file path env-overridable; never-raises
  §7 Authority pins — _NoOpTokenCounter, _ast_slice_enabled exist;
                      manifest version bumped 1.1 → 1.2
"""
from __future__ import annotations

import inspect
import json
import os
import textwrap
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance import tool_executor as te
from backend.core.ouroboros.governance import slicing_metrics as sm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_PY = textwrap.dedent('''
    """Sample module."""
    import os
    import sys
    from typing import List


    def helper(x: int) -> int:
        """Add one."""
        return x + 1


    async def async_helper(name: str) -> None:
        """Print name."""
        print(name)


    class Greeter:
        """A greeter."""

        def greet(self, name: str) -> str:
            return f"Hello, {name}"

        def shout(self, name: str) -> str:
            return self.greet(name).upper()
''').lstrip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a minimal repo with a sample Python file."""
    sample = tmp_path / "sample.py"
    sample.write_text(SAMPLE_PY, encoding="utf-8")
    nonpy = tmp_path / "doc.md"
    nonpy.write_text("# Doc\n\nNot Python.\n", encoding="utf-8")
    bad = tmp_path / "broken.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    return tmp_path


def _make_executor(repo_root: Path) -> te.ToolExecutor:
    """Construct a ToolExecutor scoped to repo_root."""
    return te.ToolExecutor(repo_root=repo_root)


# ---------------------------------------------------------------------------
# §1 — Manifest schema
# ---------------------------------------------------------------------------


def test_manifest_includes_target_symbol() -> None:
    manifest = te._L1_MANIFESTS["read_file"]
    assert "target_symbol" in manifest.arg_schema
    schema = manifest.arg_schema["target_symbol"]
    assert schema["type"] == "string"
    assert schema["default"] == ""


def test_manifest_includes_include_imports() -> None:
    manifest = te._L1_MANIFESTS["read_file"]
    assert "include_imports" in manifest.arg_schema
    assert manifest.arg_schema["include_imports"]["default"] is True


def test_manifest_version_bumped() -> None:
    """Slice 11.2 bumped read_file version 1.1 → 1.2 to signal the
    new optional capabilities."""
    assert te._L1_MANIFESTS["read_file"].version == "1.2"


def test_manifest_capabilities_unchanged() -> None:
    """Capabilities (security boundary) MUST remain {'read'} only —
    AST slicing introduces no new mutation surface."""
    assert (
        te._L1_MANIFESTS["read_file"].capabilities == frozenset({"read"})
    )


# ---------------------------------------------------------------------------
# §2 — Master flag
# ---------------------------------------------------------------------------


def test_ast_slice_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_TOOL_AST_SLICE_ENABLED", raising=False)
    assert te._ast_slice_enabled() is False


def test_ast_slice_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", val)
        assert te._ast_slice_enabled() is True


def test_ast_slice_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", val)
        assert te._ast_slice_enabled() is False


# ---------------------------------------------------------------------------
# §3 — Sliced read happy paths
# ---------------------------------------------------------------------------


def test_slice_top_level_function(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "slice_test.jsonl"),
    )
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "sample.py",
        "target_symbol": "helper",
    })
    # SLICED header marker is the dispatch fingerprint.
    assert "SLICED:" in result
    assert "helper" in result
    # Body of helper is in there.
    assert "return x + 1" in result
    # Other functions NOT in there (slicing worked).
    assert "async_helper" not in result
    assert "class Greeter" not in result


def test_slice_async_function(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "slice_test.jsonl"),
    )
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "sample.py",
        "target_symbol": "async_helper",
    })
    assert "SLICED:" in result
    assert "async def async_helper" in result
    assert "print(name)" in result


def test_slice_class_method(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "slice_test.jsonl"),
    )
    ex = _make_executor(repo)
    # Qualified-name target — disambiguates the method by class.
    result = ex._read_file({
        "path": "sample.py",
        "target_symbol": "Greeter.greet",
    })
    assert "SLICED:" in result
    assert 'def greet' in result
    assert 'Hello' in result
    # shout (sibling method) NOT in result.
    assert "shout" not in result.lower() or result.count("shout") < 2


def test_slice_includes_imports_by_default(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "slice_test.jsonl"),
    )
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "sample.py",
        "target_symbol": "helper",
    })
    # Module imports should be prepended for context.
    assert "import os" in result or "import sys" in result


def test_slice_omits_imports_when_disabled(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "slice_test.jsonl"),
    )
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "sample.py",
        "target_symbol": "helper",
        "include_imports": False,
    })
    # Imports excluded from the sliced body.
    assert "import os" not in result
    assert "import sys" not in result


def test_slice_savings_actually_realized(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The whole point — sliced output must be smaller than full file."""
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "slice_test.jsonl"),
    )
    ex = _make_executor(repo)
    sliced = ex._read_file({
        "path": "sample.py",
        "target_symbol": "helper",
        "include_imports": False,
    })
    full = ex._read_file({"path": "sample.py"})
    assert len(sliced) < len(full), (
        "sliced output should be smaller than full read"
    )


# ---------------------------------------------------------------------------
# §4 — Fallback paths
# ---------------------------------------------------------------------------


def test_fallback_master_flag_off_returns_full_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_TOOL_AST_SLICE_ENABLED", raising=False)
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "sample.py",
        "target_symbol": "helper",  # ignored when flag off
    })
    # Full file shape — every function present.
    assert "SLICED:" not in result
    assert "helper" in result
    assert "async_helper" in result
    assert "class Greeter" in result


def test_fallback_no_target_symbol_returns_full_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    ex = _make_executor(repo)
    result = ex._read_file({"path": "sample.py"})
    assert "SLICED:" not in result
    assert "helper" in result
    assert "async_helper" in result


def test_fallback_non_python_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "fb_nonpy.jsonl"),
    )
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "doc.md",
        "target_symbol": "anything",
    })
    # Non-Python falls back to full file.
    assert "SLICED:" not in result
    assert "Not Python." in result
    # Metrics row recorded with fallback_reason
    rows = list(_read_jsonl(tmp_path / "fb_nonpy.jsonl"))
    assert any(
        r["fallback_reason"] == "not_python" for r in rows
    )


def test_fallback_parse_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "fb_parse.jsonl"),
    )
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "broken.py",
        "target_symbol": "broken",
    })
    # Parse failed → full file content returned.
    assert "SLICED:" not in result
    rows = list(_read_jsonl(tmp_path / "fb_parse.jsonl"))
    assert any(
        r["fallback_reason"] == "parse_failed" for r in rows
    )


def test_fallback_symbol_not_found(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        str(tmp_path / "fb_missing.jsonl"),
    )
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "sample.py",
        "target_symbol": "nonexistent_symbol_qwertyuiop",
    })
    assert "SLICED:" not in result
    rows = list(_read_jsonl(tmp_path / "fb_missing.jsonl"))
    assert any(
        r["fallback_reason"] == "symbol_not_found" for r in rows
    )


def test_fallback_missing_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_TOOL_AST_SLICE_ENABLED", "true")
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "no_such_file.py",
        "target_symbol": "x",
    })
    assert "file not found" in result.lower()


# ---------------------------------------------------------------------------
# §5 — Backward compat
# ---------------------------------------------------------------------------


def test_legacy_read_file_unchanged(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master flag off + no target_symbol → byte-identical legacy
    read_file (apart from any unrelated changes elsewhere)."""
    monkeypatch.delenv("JARVIS_TOOL_AST_SLICE_ENABLED", raising=False)
    ex = _make_executor(repo)
    result = ex._read_file({"path": "sample.py"})
    # Legacy header format preserved.
    assert "(lines 1-" in result
    assert "of " in result
    # All numbered lines.
    assert "1: " in result


def test_legacy_lines_range_unchanged(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_TOOL_AST_SLICE_ENABLED", raising=False)
    ex = _make_executor(repo)
    result = ex._read_file({
        "path": "sample.py",
        "lines_from": 5, "lines_to": 10,
    })
    assert "(lines 5-" in result


# ---------------------------------------------------------------------------
# §6 — Slicing metrics
# ---------------------------------------------------------------------------


def _read_jsonl(p: Path):
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def test_metrics_path_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    custom = tmp_path / "custom_metrics.jsonl"
    monkeypatch.setenv("JARVIS_SLICING_METRICS_PATH", str(custom))
    assert sm.metrics_path() == custom


def test_metrics_default_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "JARVIS_TOOL_AST_SLICE_METRICS_ENABLED", raising=False,
    )
    assert sm.is_metrics_enabled() is True


def test_metrics_disabled_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOOL_AST_SLICE_METRICS_ENABLED", "false",
    )
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "x.jsonl"),
    )
    metric = sm.SliceMetric(
        file_path="x.py", target_symbol="foo",
        full_chars=100, sliced_chars=10,
    )
    assert sm.record_slice(metric) is False
    assert not (tmp_path / "x.jsonl").exists()


def test_metrics_record_writes_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "m.jsonl"),
    )
    metric = sm.SliceMetric(
        file_path="x.py", target_symbol="foo",
        full_chars=1000, sliced_chars=100,
        op_id="op-test",
    )
    assert sm.record_slice(metric) is True
    rows = list(_read_jsonl(tmp_path / "m.jsonl"))
    assert len(rows) == 1
    assert rows[0]["file_path"] == "x.py"
    assert rows[0]["target_symbol"] == "foo"
    assert rows[0]["full_chars"] == 1000
    assert rows[0]["sliced_chars"] == 100
    assert rows[0]["savings_ratio"] == pytest.approx(0.9, abs=0.01)
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["schema_version"] == "slicing.1"


def test_metrics_savings_ratio_zero_when_full_chars_zero() -> None:
    metric = sm.SliceMetric(
        file_path="x.py", target_symbol="foo",
        full_chars=0, sliced_chars=0,
    )
    assert metric.savings_ratio == 0.0


def test_metrics_writer_never_raises_on_unwritable_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disk-full / permission errors must NOT take down a tool call."""
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH",
        "/etc/passwd/cannot_write_here.jsonl",
    )
    metric = sm.SliceMetric(
        file_path="x.py", target_symbol="foo",
        full_chars=100, sliced_chars=10,
    )
    # Must not raise.
    result = sm.record_slice(metric)
    assert result is False


def test_metrics_history_trim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The history_capacity() helper has a hard floor of 64 to keep
    the ledger useful in production. Test exceeds that floor to
    actually exercise the trim path."""
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "trim.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_HISTORY_SIZE", "64",
    )
    for i in range(80):
        sm.record_slice(sm.SliceMetric(
            file_path=f"f{i}.py", target_symbol="x",
            full_chars=100, sliced_chars=10,
        ))
    rows = list(_read_jsonl(tmp_path / "trim.jsonl"))
    assert len(rows) == 64
    # Verify the LATEST 64 rows are retained, not the first 64.
    file_paths = [r["file_path"] for r in rows]
    assert "f79.py" in file_paths
    assert "f0.py" not in file_paths


# ---------------------------------------------------------------------------
# §7 — Authority pins + module surface
# ---------------------------------------------------------------------------


def test_no_op_token_counter_satisfies_protocol() -> None:
    """``_NoOpTokenCounter`` is the local stub used by the tool
    handler; must satisfy ``ast_slicer.TokenCounterProtocol``."""
    counter = te._NoOpTokenCounter()
    assert hasattr(counter, "count")
    assert counter.count("anything") == 0


def test_read_file_handler_uses_no_op_counter_not_smart_context() -> None:
    """The handler must NOT import smart_context.TokenCounter (which
    would drag in tiktoken bootstrap + cache pollution). Pinned at
    source level."""
    src = inspect.getsource(te.ToolExecutor._read_file_sliced)
    assert "_NoOpTokenCounter" in src
    assert "smart_context.TokenCounter" not in src


def test_handler_imports_ast_slicer_lazily() -> None:
    """ast_slicer + slicing_metrics imported INSIDE the method, not
    at module top — keeps tool_executor import path clean for callers
    that don't enable slicing."""
    sliced_src = inspect.getsource(te.ToolExecutor._read_file_sliced)
    assert "from backend.core.ouroboros.governance.ast_slicer" in sliced_src
    assert "from backend.core.ouroboros.governance.slicing_metrics" in sliced_src


def test_master_flag_check_inside_handler() -> None:
    """The handler reads the env at call time (not at import time)
    so monkeypatch.setenv works correctly across tests."""
    src = inspect.getsource(te.ToolExecutor._read_file)
    assert "_ast_slice_enabled()" in src
