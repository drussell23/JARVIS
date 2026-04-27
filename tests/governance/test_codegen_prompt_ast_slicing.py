"""Slice 11.3 regression spine — AST-aware codegen prompt slicing.

Pins:
  §1 Master flag — JARVIS_GEN_AST_SLICE_ENABLED default false +
                   truthy/falsy parsing
  §2 Threshold knobs — JARVIS_GEN_AST_SLICE_MIN_CHARS,
                       JARVIS_GEN_AST_SLICE_FN_MAX_CHARS
  §3 _ast_outline_python_file behavior — outline shape, skeleton
                                          markers, summary footer
  §4 _maybe_ast_outline dispatcher — flag-off short-circuit, small-
                                     file short-circuit, non-Python
                                     short-circuit, parse-failure
                                     fallback
  §5 Slicing-metrics integration — every dispatch records exactly
                                   one row (success or fallback)
  §6 _build_codegen_prompt integration — flag off byte-identical;
                                          flag on injects [AST-SLICED]
                                          marker for large Python
                                          files; small files get
                                          legacy treatment
"""
from __future__ import annotations

import inspect
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance import providers as pv


# ---------------------------------------------------------------------------
# Fixtures — synthetic Python source large enough to trigger slicing
# ---------------------------------------------------------------------------


SMALL_PY = textwrap.dedent('''
    """Small module."""
    def tiny() -> int:
        return 1
''').lstrip()


def _make_large_py(n_methods: int = 30) -> str:
    """Build a Python source ~30 KB with one big class containing many
    methods of variable size."""
    parts: list[str] = []
    parts.append('"""Large module for slicing tests."""')
    parts.append("import os")
    parts.append("import sys")
    parts.append("from typing import List, Dict, Optional")
    parts.append("")
    parts.append("")
    parts.append("def small_top_level(x: int) -> int:")
    parts.append('    """Add one to x."""')
    parts.append("    return x + 1")
    parts.append("")
    parts.append("")
    parts.append("class BigService:")
    parts.append('    """A big class with many methods."""')
    parts.append("")
    for i in range(n_methods):
        parts.append(f"    def method_{i}(self, value: int) -> int:")
        parts.append(f'        """Method number {i}."""')
        # Bloat — intentional padding to push past fn_max_chars
        for j in range(50):
            parts.append(f"        # padding line {j} for method {i}")
            parts.append(f"        intermediate_{j} = value + {j}")
        parts.append(f"        return value + {i}")
        parts.append("")
    return "\n".join(parts)


@pytest.fixture
def small_py_file(tmp_path: Path) -> Path:
    p = tmp_path / "small.py"
    p.write_text(SMALL_PY, encoding="utf-8")
    return p


@pytest.fixture
def large_py_file(tmp_path: Path) -> Path:
    p = tmp_path / "large.py"
    p.write_text(_make_large_py(), encoding="utf-8")
    return p


@pytest.fixture
def non_py_file(tmp_path: Path) -> Path:
    p = tmp_path / "doc.md"
    p.write_text("# Docs\n\nNot Python.\n" * 500, encoding="utf-8")
    return p


@pytest.fixture
def broken_py_file(tmp_path: Path) -> Path:
    p = tmp_path / "broken.py"
    p.write_text("def broken(:\n" * 1000, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_GEN_AST_SLICE_ENABLED", raising=False)
    assert pv._gen_ast_slice_enabled() is False


def test_master_flag_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", val)
        assert pv._gen_ast_slice_enabled() is True


def test_master_flag_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", val)
        assert pv._gen_ast_slice_enabled() is False


# ---------------------------------------------------------------------------
# §2 — Threshold knobs
# ---------------------------------------------------------------------------


def test_min_chars_default_8000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_GEN_AST_SLICE_MIN_CHARS", raising=False)
    assert pv._gen_ast_slice_min_chars() == 8000


def test_min_chars_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_MIN_CHARS", "1000")
    assert pv._gen_ast_slice_min_chars() == 1000


def test_min_chars_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_MIN_CHARS", "garbage")
    assert pv._gen_ast_slice_min_chars() == 8000


def test_fn_max_chars_default_1500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "JARVIS_GEN_AST_SLICE_FN_MAX_CHARS", raising=False,
    )
    assert pv._gen_ast_slice_fn_max_chars() == 1500


def test_fn_max_chars_floor_100(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-function threshold must have a sane minimum so a misconfig
    can't reduce it to ~0 (which would skeleton-everything)."""
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_FN_MAX_CHARS", "1")
    assert pv._gen_ast_slice_fn_max_chars() == 100


# ---------------------------------------------------------------------------
# §3 — _ast_outline_python_file behavior
# ---------------------------------------------------------------------------


def test_outline_emits_summary_marker(large_py_file: Path) -> None:
    content = large_py_file.read_text(encoding="utf-8")
    outline = pv._ast_outline_python_file(content, large_py_file)
    assert outline is not None
    assert "[AST-OUTLINE:" in outline
    assert "skeletons" in outline


def test_outline_module_header_preserved(large_py_file: Path) -> None:
    content = large_py_file.read_text(encoding="utf-8")
    outline = pv._ast_outline_python_file(content, large_py_file)
    assert outline is not None
    # Imports retained verbatim (module header chunk is full).
    assert "import os" in outline
    assert "from typing import List" in outline


def test_outline_small_function_full_body(large_py_file: Path) -> None:
    content = large_py_file.read_text(encoding="utf-8")
    outline = pv._ast_outline_python_file(content, large_py_file)
    assert outline is not None
    # small_top_level is small — should be full body.
    assert "def small_top_level" in outline
    assert "return x + 1" in outline


def test_outline_large_method_skeletonized(large_py_file: Path) -> None:
    content = large_py_file.read_text(encoding="utf-8")
    outline = pv._ast_outline_python_file(content, large_py_file)
    assert outline is not None
    # Each method has 50+ padding lines → exceeds fn_max_chars.
    # Skeleton marker present.
    assert "[AST-SKELETON:" in outline
    # The padding strings (real bodies) should be ABSENT for at least
    # some methods.
    skeleton_count = outline.count("[AST-SKELETON:")
    assert skeleton_count >= 5


def test_outline_returns_none_on_parse_error(broken_py_file: Path) -> None:
    content = broken_py_file.read_text(encoding="utf-8")
    outline = pv._ast_outline_python_file(content, broken_py_file)
    assert outline is None


def test_outline_returns_none_on_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.py"
    p.write_text("", encoding="utf-8")
    outline = pv._ast_outline_python_file("", p)
    # Empty file produces no chunks → fallback
    assert outline is None


def test_outline_smaller_than_full_for_large_file(
    large_py_file: Path,
) -> None:
    """The whole point — outline must reduce char count for large files."""
    content = large_py_file.read_text(encoding="utf-8")
    outline = pv._ast_outline_python_file(content, large_py_file)
    assert outline is not None
    assert len(outline) < len(content), (
        f"outline ({len(outline)}) must be smaller than full "
        f"({len(content)}) for a file with >5 large methods"
    )


# ---------------------------------------------------------------------------
# §4 — _maybe_ast_outline dispatcher
# ---------------------------------------------------------------------------


def test_dispatcher_master_flag_off_returns_none(
    large_py_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_GEN_AST_SLICE_ENABLED", raising=False)
    content = large_py_file.read_text(encoding="utf-8")
    result = pv._maybe_ast_outline(
        large_py_file, "large.py", content,
    )
    assert result is None


def test_dispatcher_non_python_returns_none(
    non_py_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", "true")
    content = non_py_file.read_text(encoding="utf-8")
    result = pv._maybe_ast_outline(
        non_py_file, "doc.md", content,
    )
    assert result is None


def test_dispatcher_small_file_returns_none(
    small_py_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Small files (under min_chars threshold) skip slicing — full
    file is cheap enough to inject."""
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "m.jsonl"),
    )
    content = small_py_file.read_text(encoding="utf-8")
    result = pv._maybe_ast_outline(
        small_py_file, "small.py", content,
    )
    assert result is None


def test_dispatcher_large_python_returns_outline(
    large_py_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "m.jsonl"),
    )
    content = large_py_file.read_text(encoding="utf-8")
    result = pv._maybe_ast_outline(
        large_py_file, "large.py", content,
    )
    assert result is not None
    assert "[AST-OUTLINE:" in result
    assert "[AST-SKELETON:" in result


def test_dispatcher_parse_error_returns_none_records_fallback(
    broken_py_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "m.jsonl"),
    )
    content = broken_py_file.read_text(encoding="utf-8")
    # File is large enough to hit threshold
    assert len(content) > 8000
    result = pv._maybe_ast_outline(
        broken_py_file, "broken.py", content,
    )
    assert result is None
    # Fallback row recorded
    rows = list(_read_jsonl(tmp_path / "m.jsonl"))
    assert any(
        r["fallback_reason"] == "parse_failed_or_empty" for r in rows
    )


# ---------------------------------------------------------------------------
# §5 — Slicing metrics integration
# ---------------------------------------------------------------------------


def _read_jsonl(p: Path):
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def test_dispatcher_records_success_metric(
    large_py_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "m.jsonl"),
    )
    content = large_py_file.read_text(encoding="utf-8")
    result = pv._maybe_ast_outline(
        large_py_file, "large.py", content, op_id="op-test-113",
    )
    assert result is not None
    rows = list(_read_jsonl(tmp_path / "m.jsonl"))
    assert len(rows) == 1
    row = rows[0]
    assert row["target_symbol"] == "__codegen_outline__"
    assert row["outcome"] == "ok"
    assert row["op_id"] == "op-test-113"
    assert row["sliced_chars"] < row["full_chars"]
    assert row["savings_ratio"] > 0


def test_dispatcher_no_metric_for_master_flag_off(
    large_py_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Master-flag-off path doesn't record metrics — would dilute
    the ledger with sub-threshold non-events."""
    monkeypatch.delenv("JARVIS_GEN_AST_SLICE_ENABLED", raising=False)
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "m.jsonl"),
    )
    content = large_py_file.read_text(encoding="utf-8")
    pv._maybe_ast_outline(large_py_file, "large.py", content)
    # Ledger should not exist or have zero rows.
    rows = list(_read_jsonl(tmp_path / "m.jsonl"))
    assert len(rows) == 0


def test_dispatcher_no_metric_for_small_file(
    small_py_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "m.jsonl"),
    )
    content = small_py_file.read_text(encoding="utf-8")
    pv._maybe_ast_outline(small_py_file, "small.py", content)
    rows = list(_read_jsonl(tmp_path / "m.jsonl"))
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# §6 — _build_codegen_prompt integration (source-level pin)
# ---------------------------------------------------------------------------


def test_build_codegen_prompt_imports_dispatcher() -> None:
    """Source-level pin: _build_codegen_prompt must call
    _maybe_ast_outline. Catches a refactor that strips the wiring."""
    src = inspect.getsource(pv._build_codegen_prompt)
    assert "_maybe_ast_outline" in src


def test_build_codegen_prompt_emits_slice_marker_on_outline() -> None:
    src = inspect.getsource(pv._build_codegen_prompt)
    # The header carries [AST-SLICED] marker when slicing fires.
    assert "[AST-SLICED]" in src


def test_outline_branch_precedes_legacy_truncation() -> None:
    """Source-level pin: the AST outline check is BEFORE the legacy
    _read_with_truncation call so sliced output wins when applicable."""
    src = inspect.getsource(pv._build_codegen_prompt)
    outline_idx = src.index("_maybe_ast_outline")
    truncation_idx = src.index("_read_with_truncation")
    assert outline_idx < truncation_idx


def test_outline_marker_only_on_outline_path() -> None:
    """The slice_marker is empty string for the legacy paths (BG +
    default truncation). Pinned by code structure."""
    src = inspect.getsource(pv._build_codegen_prompt)
    # slice_marker = "" for the legacy paths
    assert 'slice_marker = ""' in src


# ---------------------------------------------------------------------------
# §7 — Authority + boundary pins
# ---------------------------------------------------------------------------


def test_outline_function_lazy_imports_ast_slicer() -> None:
    """Lazy import — keeps providers.py module-import path clean for
    callers that don't enable slicing."""
    src = inspect.getsource(pv._ast_outline_python_file)
    assert (
        "from backend.core.ouroboros.governance.ast_slicer import"
        in src
    )


def test_outline_dispatcher_lazy_imports_metrics() -> None:
    src = inspect.getsource(pv._maybe_ast_outline)
    assert (
        "from backend.core.ouroboros.governance.slicing_metrics import"
        in src
    )


def test_outline_function_uses_no_op_token_counter() -> None:
    """Outline doesn't need accurate token counts — uses a local
    NoOp counter to avoid dragging in smart_context.TokenCounter
    bootstrap cost during prompt building."""
    src = inspect.getsource(pv._ast_outline_python_file)
    # Local stub class.
    assert "class _NoOpCounter" in src
    # Doesn't import the heavyweight smart_context counter.
    assert "smart_context.TokenCounter" not in src


def test_outline_function_never_raises() -> None:
    """Defensive smoke: the function returns None on any internal
    failure rather than raising — caller depends on this."""
    # Pass garbage source — must not raise.
    result = pv._ast_outline_python_file(
        "completely \x00 bogus \x01 source", Path("/tmp/fake.py"),
    )
    # Either None (parse failed) OR a string. Must NOT raise.
    assert result is None or isinstance(result, str)
