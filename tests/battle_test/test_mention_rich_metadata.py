"""Regression spine for §41.3 #10 — rich @-mention metadata.

Substrate tests for `build_mention_metadata` + `format_mention_metadata`
+ the `is_mention_rich_metadata_enabled` gate. Compositional —
extends the existing `repl_input_polish.build_mention_completer`
without parallel state. Data on module constants (kind glyphs,
size units, age units) — no hardcoded magic strings inside the
format functions."""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import repl_input_polish as rip
from backend.core.ouroboros.battle_test.repl_input_polish import (
    MASTER_FLAG_ENV_VAR,
    MENTION_RICH_METADATA_ENV_VAR,
    MentionMetadata,
    _MENTION_AGE_UNITS,
    _MENTION_KIND_GLYPHS,
    _MENTION_SIZE_UNITS,
    _classify_kind,
    _format_age,
    _format_size,
    _glyph_for_kind,
    build_mention_metadata,
    format_mention_metadata,
    is_mention_rich_metadata_enabled,
)


# --- is_mention_rich_metadata_enabled gate --------------------------------


def test_default_true(monkeypatch):
    monkeypatch.delenv(MENTION_RICH_METADATA_ENV_VAR, raising=False)
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    assert is_mention_rich_metadata_enabled() is True


def test_explicit_off(monkeypatch):
    monkeypatch.setenv(MENTION_RICH_METADATA_ENV_VAR, "false")
    assert is_mention_rich_metadata_enabled() is False


def test_off_aliases(monkeypatch):
    for off in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv(MENTION_RICH_METADATA_ENV_VAR, off)
        assert is_mention_rich_metadata_enabled() is False, off


def test_implicitly_off_when_polish_master_off(monkeypatch):
    """Polish master off → no completer at all → rich metadata
    implicitly off. Composes is_polish_enabled."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    monkeypatch.setenv(MENTION_RICH_METADATA_ENV_VAR, "true")
    assert is_mention_rich_metadata_enabled() is False


# --- Data-on-module tables -----------------------------------------------


def test_kind_glyph_table_covers_4_kinds():
    """Bytes-pin: the table data drives the format functions —
    adding a kind requires editing the table, not the format
    code. Tests assert exactly 4 entries for now."""
    kinds = {k for k, _ in _MENTION_KIND_GLYPHS}
    assert kinds == {"dir", "symlink", "file", "missing"}


def test_size_units_sorted_ascending():
    """The walker depends on threshold-ascending order to pick
    the largest-fit-first unit. Defensive pin."""
    thresholds = [t for t, _, _ in _MENTION_SIZE_UNITS]
    assert thresholds == sorted(thresholds)


def test_age_units_sorted_ascending():
    thresholds = [t for t, _, _ in _MENTION_AGE_UNITS]
    assert thresholds == sorted(thresholds)


# --- _format_size ---------------------------------------------------------


def test_format_size_zero():
    assert _format_size(0) == "0 B"


def test_format_size_bytes():
    assert _format_size(512) == "512 B"


def test_format_size_kilobytes():
    assert _format_size(1500) == "1.5 KB"


def test_format_size_megabytes():
    assert _format_size(5 * 1024 * 1024) == "5.0 MB"


def test_format_size_gigabytes():
    assert _format_size(2 * 1024 ** 3) == "2.0 GB"


def test_format_size_negative_returns_dash():
    assert _format_size(-5) == "—"


def test_format_size_garbage_returns_dash():
    assert _format_size("not a number") == "—"  # type: ignore[arg-type]
    assert _format_size(None) == "—"  # type: ignore[arg-type]


# --- _format_age ----------------------------------------------------------


def test_format_age_seconds():
    assert _format_age(5) == "5s"


def test_format_age_minutes():
    assert _format_age(120) == "2m"


def test_format_age_hours():
    assert _format_age(7200) == "2h"


def test_format_age_days():
    assert _format_age(86400 * 3) == "3d"


def test_format_age_weeks():
    assert _format_age(86400 * 14) == "2w"


def test_format_age_negative_returns_dash():
    assert _format_age(-10) == "—"


def test_format_age_garbage_safe():
    assert _format_age("nope") == "—"  # type: ignore[arg-type]
    assert _format_age(None) == "—"  # type: ignore[arg-type]


# --- _classify_kind ------------------------------------------------------


def test_classify_file(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    assert _classify_kind(f) == "file"


def test_classify_dir(tmp_path):
    assert _classify_kind(tmp_path) == "dir"


def test_classify_missing(tmp_path):
    assert _classify_kind(tmp_path / "nope") == "missing"


def test_classify_symlink(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("hi")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink unavailable on this filesystem")
    assert _classify_kind(link) == "symlink"


# --- _glyph_for_kind ------------------------------------------------------


def test_glyph_lookup_known():
    assert _glyph_for_kind("file") == "📄"
    assert _glyph_for_kind("dir") == "📁"
    assert _glyph_for_kind("symlink") == "🔗"


def test_glyph_lookup_unknown_falls_back_to_missing():
    assert _glyph_for_kind("bogus") == "?"


# --- build_mention_metadata ----------------------------------------------


def test_build_metadata_file(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hello world" * 100)  # ~1100 bytes
    meta = build_mention_metadata(
        "doc.txt", base_dir=tmp_path, now_unix=time.time(),
    )
    assert meta.kind == "file"
    assert meta.glyph == "📄"
    assert meta.size_bytes > 0
    assert meta.size_pretty.endswith(("B", "KB"))


def test_build_metadata_dir_has_no_size(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    meta = build_mention_metadata(
        "sub", base_dir=tmp_path,
    )
    assert meta.kind == "dir"
    assert meta.glyph == "📁"
    assert meta.size_pretty == "—"


def test_build_metadata_missing_path(tmp_path):
    meta = build_mention_metadata("nope.txt", base_dir=tmp_path)
    assert meta.kind == "missing"
    assert meta.glyph == "?"
    assert meta.size_pretty == "—"
    assert meta.age_pretty == "—"


def test_build_metadata_empty_text_returns_missing(tmp_path):
    meta = build_mention_metadata("", base_dir=tmp_path)
    assert meta.kind == "missing"


def test_build_metadata_none_returns_missing(tmp_path):
    meta = build_mention_metadata(None, base_dir=tmp_path)
    assert meta.kind == "missing"


def test_build_metadata_garbage_returns_missing(tmp_path):
    meta = build_mention_metadata(42, base_dir=tmp_path)
    assert meta.kind == "missing"


def test_build_metadata_now_unix_drives_age(tmp_path):
    """Pure: pass a now_unix that's exactly 120s after the file's
    mtime → age should report 2m."""
    f = tmp_path / "x.txt"
    f.write_text("hi")
    mtime = f.stat().st_mtime
    meta = build_mention_metadata(
        "x.txt", base_dir=tmp_path, now_unix=mtime + 120,
    )
    assert meta.age_pretty == "2m ago"


def test_build_metadata_age_clamps_to_zero(tmp_path):
    """now_unix BEFORE mtime → age clamps to 0 (NEVER negative)."""
    f = tmp_path / "x.txt"
    f.write_text("hi")
    mtime = f.stat().st_mtime
    meta = build_mention_metadata(
        "x.txt", base_dir=tmp_path, now_unix=mtime - 1000,
    )
    assert meta.age_seconds == 0.0


def test_build_metadata_defaults_base_dir_to_cwd(monkeypatch, tmp_path):
    """No base_dir → uses Path.cwd()."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "in_cwd.txt"
    f.write_text("hi")
    meta = build_mention_metadata("in_cwd.txt")
    assert meta.kind == "file"


# --- format_mention_metadata ---------------------------------------------


def test_format_returns_rich_string_for_file(tmp_path, monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(MENTION_RICH_METADATA_ENV_VAR, "true")
    f = tmp_path / "x.txt"
    f.write_text("hi")
    out = format_mention_metadata(
        "x.txt", base_dir=tmp_path,
        now_unix=f.stat().st_mtime + 60,
    )
    assert "📄" in out
    assert "B" in out  # size unit
    assert "ago" in out


def test_format_returns_fallback_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv(MENTION_RICH_METADATA_ENV_VAR, "false")
    f = tmp_path / "x.txt"
    f.write_text("hi")
    out = format_mention_metadata("x.txt", base_dir=tmp_path)
    assert out == "@mention path"  # legacy default


def test_format_returns_fallback_for_missing_path(tmp_path, monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(MENTION_RICH_METADATA_ENV_VAR, "true")
    out = format_mention_metadata("nope.txt", base_dir=tmp_path)
    assert out == "@mention path"


def test_format_custom_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv(MENTION_RICH_METADATA_ENV_VAR, "false")
    out = format_mention_metadata(
        "x.txt", base_dir=tmp_path, fallback="custom",
    )
    assert out == "custom"


def test_format_dir_shape(tmp_path, monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(MENTION_RICH_METADATA_ENV_VAR, "true")
    sub = tmp_path / "subdir"
    sub.mkdir()
    out = format_mention_metadata(
        "subdir", base_dir=tmp_path,
        now_unix=sub.stat().st_mtime + 1,
    )
    assert "📁" in out
    assert "—" in out  # dirs don't show size


def test_format_never_raises_garbage_input():
    """NEVER raises across any garbage."""
    try:
        out = format_mention_metadata(None)
        assert isinstance(out, str)
        out2 = format_mention_metadata(42)
        assert isinstance(out2, str)
    except Exception:
        pytest.fail("format_mention_metadata raised")


# --- AST pins -------------------------------------------------------------


def test_ast_pin_mention_metadata_symbols_exported():
    src = Path(
        "backend/core/ouroboros/battle_test/repl_input_polish.py"
    ).read_text()
    for name in (
        '"MentionMetadata"',
        '"build_mention_metadata"',
        '"format_mention_metadata"',
        '"is_mention_rich_metadata_enabled"',
        '"MENTION_RICH_METADATA_ENV_VAR"',
    ):
        assert name in src, f"{name} missing from __all__"


def test_ast_pin_completer_uses_format_metadata():
    """Bytes-pin: build_mention_completer's display_meta arg
    composes format_mention_metadata — no legacy hardcoded
    "@mention path" string remains in the completer body."""
    src = Path(
        "backend/core/ouroboros/battle_test/repl_input_polish.py"
    ).read_text()
    # Find the completer definition
    idx = src.find("def build_mention_completer")
    assert idx > 0
    body = src[idx:idx + 3000]
    assert "format_mention_metadata" in body
    # Legacy hardcoded display_meta="@mention path" should NOT
    # appear as a literal arg in the body (it's now in the
    # format helper as the fallback default).
    assert 'display_meta="@mention path"' not in body


def test_ast_pin_kind_glyph_table_is_data():
    """Bytes-pin: _MENTION_KIND_GLYPHS lives at module scope as
    a tuple-of-tuples (data on module). NOT inlined inside the
    format function. Adding a new kind = edit table, not code."""
    src = Path(
        "backend/core/ouroboros/battle_test/repl_input_polish.py"
    ).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_MENTION_KIND_GLYPHS"
            and isinstance(node.value, ast.Tuple)
        ):
            assert len(node.value.elts) >= 4
            return
    pytest.fail("_MENTION_KIND_GLYPHS table not found at module scope")
