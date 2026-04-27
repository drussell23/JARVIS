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


def test_chars_per_token_default_3_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 11.4.1: dynamic budget formula uses chars/token ratio.
    Default 3.5 matches DW pricing assumption used elsewhere."""
    monkeypatch.delenv(
        "JARVIS_GEN_AST_SLICE_CHARS_PER_TOKEN", raising=False,
    )
    assert pv._gen_ast_slice_chars_per_token() == 3.5


def test_input_budget_ratio_default_0_25(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 11.4.1: 25% of provider context budget reserved for
    file content (rest = output reservation + scaffolding)."""
    monkeypatch.delenv(
        "JARVIS_GEN_AST_SLICE_INPUT_BUDGET_RATIO", raising=False,
    )
    assert pv._gen_ast_slice_input_budget_ratio() == 0.25


def test_input_budget_ratio_clamped_to_unit_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_INPUT_BUDGET_RATIO", "5.0")
    assert pv._gen_ast_slice_input_budget_ratio() == 0.95
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_INPUT_BUDGET_RATIO", "0.0")
    assert pv._gen_ast_slice_input_budget_ratio() == 0.05


def test_target_chars_dw_routes_derive_from_dw_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 11.4.1: target_chars derived dynamically from
    _DW_MAX_TOKENS (not hardcoded). With defaults: 16384 × 0.25 ×
    3.5 / 1 file = 14336 chars."""
    monkeypatch.delenv(
        "JARVIS_GEN_AST_SLICE_INPUT_BUDGET_RATIO", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_GEN_AST_SLICE_CHARS_PER_TOKEN", raising=False,
    )
    target = pv._codegen_target_chars_for_route("background", 1)
    # 16384 × 0.25 × 3.5 = 14336
    assert 12000 <= target <= 16000


def test_target_chars_immediate_route_uses_claude_budget() -> None:
    """Claude routes get a generous budget (200K context)."""
    target = pv._codegen_target_chars_for_route("immediate", 1)
    # 200000 × 0.25 × 3.5 = 175000
    assert target >= 100000


def test_target_chars_scales_inverse_with_num_files() -> None:
    """N target_files share the budget — per-file budget shrinks."""
    one_file = pv._codegen_target_chars_for_route("background", 1)
    four_files = pv._codegen_target_chars_for_route("background", 4)
    assert four_files < one_file
    # Approximately 1/4 the budget (with floor)
    assert four_files * 4 <= one_file + 100  # tolerance for floor


def test_target_chars_floor_2000() -> None:
    """Even with high num_files, never go below 2000 chars per file."""
    target = pv._codegen_target_chars_for_route("background", 1000)
    assert target >= 2000


# ---------------------------------------------------------------------------
# §3 — _ast_outline_python_file behavior
# ---------------------------------------------------------------------------


def _outline_text(result):
    """Slice 11.4.1: _ast_outline_python_file returns
    (outline_str, tier_used, full_n, skel_n) tuple. This helper
    extracts the outline string for tests that only care about
    content."""
    if result is None:
        return None
    return result[0]


def test_outline_emits_summary_marker(large_py_file: Path) -> None:
    content = large_py_file.read_text(encoding="utf-8")
    result = pv._ast_outline_python_file(content, large_py_file)
    outline = _outline_text(result)
    assert outline is not None
    assert "[AST-OUTLINE:" in outline
    # Slice 11.4.1: tier metadata replaces "skeletons" word
    assert "tier=" in outline


def test_outline_returns_tuple(large_py_file: Path) -> None:
    """Slice 11.4.1: return shape is (outline, tier, full_n, skel_n)."""
    content = large_py_file.read_text(encoding="utf-8")
    result = pv._ast_outline_python_file(content, large_py_file)
    assert result is not None
    assert len(result) == 4
    outline, tier, full_n, skel_n = result
    assert isinstance(outline, str)
    assert tier.startswith("tier_")
    assert isinstance(full_n, int)
    assert isinstance(skel_n, int)


def test_outline_module_header_preserved(large_py_file: Path) -> None:
    content = large_py_file.read_text(encoding="utf-8")
    outline = _outline_text(
        pv._ast_outline_python_file(content, large_py_file),
    )
    assert outline is not None
    # Imports retained verbatim (module header chunk is full).
    assert "import os" in outline
    assert "from typing import List" in outline


def test_outline_small_function_full_body(large_py_file: Path) -> None:
    """With NO target_chars constraint, the slicer picks tier 0
    (full bodies) and small functions stay full."""
    content = large_py_file.read_text(encoding="utf-8")
    outline = _outline_text(
        pv._ast_outline_python_file(content, large_py_file),
    )
    assert outline is not None
    assert "def small_top_level" in outline
    assert "return x + 1" in outline


def test_outline_progressive_skeletonization_under_pressure(
    large_py_file: Path,
) -> None:
    """Slice 11.4.1: with a tight target_chars, the slicer must
    progressively skeletonize until the result fits."""
    content = large_py_file.read_text(encoding="utf-8")
    # Set a tight target — 30% of the original size
    tight_target = int(len(content) * 0.3)
    result = pv._ast_outline_python_file(
        content, large_py_file, target_chars=tight_target,
    )
    assert result is not None
    outline, tier, full_n, skel_n = result
    # Should have skeletonized
    assert skel_n > 0
    # Tier reflects the level of skeletonization applied
    assert tier in (
        "tier_2_25pct_skeletons", "tier_3_50pct_skeletons",
        "tier_4_75pct_skeletons", "tier_5_max_skeletal",
    )
    # The outline marker reflects the tier
    assert tier in outline


def test_outline_picks_smallest_tier_that_fits(
    large_py_file: Path,
) -> None:
    """The slicer must NOT over-skeletonize. If tier 0 fits, use
    tier 0. If tier 0 doesn't fit but tier 1 does, use tier 1. Etc."""
    content = large_py_file.read_text(encoding="utf-8")
    # Very generous target — 5x file size (clearly room for tier 0).
    generous = int(len(content) * 5.0)
    result = pv._ast_outline_python_file(
        content, large_py_file, target_chars=generous,
    )
    assert result is not None
    outline, tier, full_n, skel_n = result
    # With a generous target, tier 0 (no skeletonization) wins.
    assert tier == "tier_0_full"
    # And no skeletons applied
    assert skel_n == 0


def test_outline_tier_escalates_under_tighter_target(
    large_py_file: Path,
) -> None:
    """Empirical pin: progressively tighter targets pick higher tiers."""
    content = large_py_file.read_text(encoding="utf-8")
    # Very tight target — 10% of file size; should force tier 4 or 5
    tight = int(len(content) * 0.10)
    result = pv._ast_outline_python_file(
        content, large_py_file, target_chars=tight,
    )
    assert result is not None
    outline, tier, full_n, skel_n = result
    # Aggressive skeletonization
    assert tier in (
        "tier_3_50pct_skeletons", "tier_4_75pct_skeletons",
        "tier_5_max_skeletal",
    )
    # Most functions skeletonized
    assert skel_n >= full_n


def test_outline_returns_none_on_parse_error(broken_py_file: Path) -> None:
    content = broken_py_file.read_text(encoding="utf-8")
    result = pv._ast_outline_python_file(content, broken_py_file)
    assert result is None


def test_outline_returns_none_on_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.py"
    p.write_text("", encoding="utf-8")
    result = pv._ast_outline_python_file("", p)
    # Empty file produces no chunks → fallback
    assert result is None


def test_outline_smaller_than_full_under_target(
    large_py_file: Path,
) -> None:
    """With a tight target_chars, the outline MUST be smaller than
    the original (the whole point)."""
    content = large_py_file.read_text(encoding="utf-8")
    target = int(len(content) * 0.4)
    result = pv._ast_outline_python_file(
        content, large_py_file, target_chars=target,
    )
    assert result is not None
    outline = result[0]
    assert len(outline) < len(content), (
        f"outline ({len(outline)}) must be smaller than full "
        f"({len(content)}) under a tight target_chars constraint"
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
    # Slice 11.4.1: target_symbol carries the tier suffix
    # (e.g. "__codegen_outline__:tier_3_50pct_skeletons")
    assert row["target_symbol"].startswith("__codegen_outline__")
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
    # Either None (parse failed) OR a tuple. Must NOT raise.
    assert result is None or isinstance(result, tuple)


# ---------------------------------------------------------------------------
# §8 — Slice 11.4.1 honest savings_ratio (drop the clamp)
# ---------------------------------------------------------------------------


def test_savings_ratio_negative_when_outline_is_larger() -> None:
    """Slice 11.4.1 honesty pin: a sliced outline LARGER than the
    original surfaces as a NEGATIVE savings ratio. Clamping to 0 hid
    this empirical reality from the operator's ledger."""
    from backend.core.ouroboros.governance.slicing_metrics import (
        SliceMetric,
    )
    metric = SliceMetric(
        file_path="x.py", target_symbol="foo",
        full_chars=1000, sliced_chars=1500,  # outline is LARGER
    )
    assert metric.savings_ratio < 0
    assert metric.savings_ratio == pytest.approx(-0.5, abs=0.01)


def test_savings_ratio_positive_when_outline_is_smaller() -> None:
    from backend.core.ouroboros.governance.slicing_metrics import (
        SliceMetric,
    )
    metric = SliceMetric(
        file_path="x.py", target_symbol="foo",
        full_chars=1000, sliced_chars=200,
    )
    assert metric.savings_ratio == pytest.approx(0.8, abs=0.01)


def test_savings_ratio_zero_when_full_chars_zero() -> None:
    """Defensive: div-by-zero guard."""
    from backend.core.ouroboros.governance.slicing_metrics import (
        SliceMetric,
    )
    metric = SliceMetric(
        file_path="x.py", target_symbol="foo",
        full_chars=0, sliced_chars=0,
    )
    assert metric.savings_ratio == 0.0


def test_dispatcher_records_outline_not_smaller_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 11.4.1: when even max-skeletal tier produces an outline
    >= original size, the dispatcher records a fallback metric AND
    returns None so caller takes legacy truncation path.

    Hardest case to construct deterministically — depends on the
    specific layout of small files where the outline footer +
    chunk-joins exceed the original size. We accept either a
    successful slice (smaller outline) OR a fallback row, but
    REQUIRE that if the dispatcher returned None, it logged the
    reason."""
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SLICING_METRICS_PATH", str(tmp_path / "m.jsonl"),
    )
    monkeypatch.setenv("JARVIS_GEN_AST_SLICE_MIN_CHARS", "200")
    # Build a file ~500 chars with NO skeletonizable content (just
    # imports + many tiny functions) — outline will add the footer +
    # newlines + ChunkType.MODULE_HEADER chunk and likely be larger.
    p = tmp_path / "tiny.py"
    body = '"""Module docstring for fallback test."""\n'
    body += "import os\nimport sys\nimport json\n\n"
    for i in range(8):
        body += f"def fn_{i}() -> int:\n    return {i}\n\n"
    p.write_text(body, encoding="utf-8")
    content = p.read_text(encoding="utf-8")
    result = pv._maybe_ast_outline(
        p, "tiny.py", content, provider_route="background",
    )
    # Either the outline was smaller (slicer succeeded — fine) OR
    # it was larger and dispatcher returned None with a fallback
    # row recorded. EITHER way, a metric row exists.
    metrics_path = tmp_path / "m.jsonl"
    if metrics_path.exists():
        rows = [
            json.loads(line)
            for line in metrics_path.read_text("utf-8").splitlines()
            if line.strip()
        ]
        assert rows, "expected at least one metric row recorded"
        if result is None:
            # Fallback path — verify a fallback_reason was captured
            assert any(
                r.get("fallback_reason") for r in rows
            )
    else:
        # No metrics file written — dispatcher must have returned a
        # successful outline (which records via record_slice). If
        # result is None AND no metrics, that's a contract violation.
        assert result is not None, (
            "dispatcher returned None but did not record a metric row"
        )
