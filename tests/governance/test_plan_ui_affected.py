"""Regression spine for the ``ui_affected`` field on plan.1 schema.

Task 5 of the VisionSensor + Visual VERIFY implementation plan. Pins the
D2 decision in the spec: structured ``target_files`` classification is
the primary authoritative signal; prose keywords in the ``approach`` text
are the secondary fallback, consulted **only** when the structured
signal is absent or ambiguous.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§VERIFY Extension → Trigger conditions.

This file exercises:

* ``classify_ui_affected`` in isolation — pure deterministic function.
* ``PlanResult.ui_affected`` default + constructor + ``skipped_result``.
* End-to-end stamping through ``PlanGenerator.generate_plan`` via a
  stub generator that feeds a canned JSON response through the real
  parse + stamping path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.plan_generator import (
    _CLASSIFIABLE_EXTENSIONS,
    _FRONTEND_EXTENSIONS,
    PlanGenerator,
    PlanResult,
    classify_ui_affected,
)


# ---------------------------------------------------------------------------
# classify_ui_affected — Primary path (frontend extensions)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "src/Button.tsx",
    "components/Card.jsx",
    "app/App.vue",
    "pages/Home.svelte",
    "styles/main.css",
    "styles/_mixins.scss",
    "public/index.html",
    "legacy/form.htm",
    "deeply/nested/components/header/Nav.tsx",
    "Windows\\path\\widget.tsx",     # normalise backslashes
    "mixed/case.TSX",                 # case-insensitive extension
])
def test_classify_primary_frontend_extension_true(path):
    assert classify_ui_affected((path,)) is True


def test_classify_primary_any_frontend_file_wins_in_mixed_set():
    # Backend + frontend in the same target_files → True (any frontend wins)
    assert classify_ui_affected(
        ("backend/server.py", "src/Button.tsx"),
    ) is True


def test_classify_primary_frontend_wins_even_when_approach_is_backend_flavoured():
    # Structured signal authoritative — prose ignored when target_files
    # has any classifiable entry.
    assert classify_ui_affected(
        ("src/Button.tsx",),
        approach="refactor the database migration runner",
    ) is True


# ---------------------------------------------------------------------------
# classify_ui_affected — Structured-negative path (classifiable, no frontend)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "backend/server.py",
    "cmd/main.go",
    "src/lib.rs",
    "app/Handler.java",
    "Service.kt",
    "backend/util.scala",
    "src/Program.cs",
    "lib/tool.rb",
    "app/index.php",
    "native/mod.swift",
    "legacy/helper.m",
    "bin/script.sh".replace(".sh", ".lua"),  # just making a lua path
])
def test_classify_structured_negative_backend_only_is_false(path):
    # Backend-only target → False even with keyword in approach
    assert classify_ui_affected(
        (path,),
        approach="render a styled component with layout in viewport",
    ) is False


def test_classify_structured_negative_backend_only_no_approach_false():
    assert classify_ui_affected(("backend/server.py",)) is False


def test_classify_ts_and_js_are_classifiable_but_not_frontend():
    # .ts / .js alone should classify as "non-frontend classifiable" —
    # they pull the structured-negative branch so prose is ignored.
    assert classify_ui_affected(
        ("src/helpers.ts",),
        approach="restyle the component",
    ) is False
    assert classify_ui_affected(
        ("src/utils.js",),
        approach="add a viewport-aware layout",
    ) is False


# ---------------------------------------------------------------------------
# classify_ui_affected — Secondary fallback (empty OR unclassifiable only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyword_phrase", [
    "UI polish on the header",
    "render the latest payload",
    "restyle the header component",    # hits via 'component'
    "add a viewport-aware layout",     # hits viewport AND layout
    "tweak the style",                 # hits style
])
def test_classify_empty_target_files_keyword_match_triggers(keyword_phrase):
    assert classify_ui_affected((), approach=keyword_phrase) is True


def test_classify_empty_target_files_no_keyword_false():
    assert classify_ui_affected((), approach="fix the database migration") is False


def test_classify_empty_target_files_empty_approach_false():
    assert classify_ui_affected((), approach="") is False
    assert classify_ui_affected(()) is False


@pytest.mark.parametrize("unclassifiable_path", [
    "docs/plan.md",
    "config/.gitignore",
    "data/report.csv",
    "README.md",
    ".env.example",
    "archive.tar.gz",
])
def test_classify_unclassifiable_only_falls_through_to_keyword_check(unclassifiable_path):
    # All unclassifiable → secondary keyword check fires on approach.
    assert classify_ui_affected(
        (unclassifiable_path,),
        approach="restyle the header component",
    ) is True
    assert classify_ui_affected(
        (unclassifiable_path,),
        approach="bump the database schema version",
    ) is False


def test_classify_mixed_unclassifiable_and_classifiable_respects_structured_signal():
    # docs/plan.md (unclassifiable) + backend/server.py (classifiable non-frontend)
    # → structured signal is present → False; prose ignored.
    assert classify_ui_affected(
        ("docs/plan.md", "backend/server.py"),
        approach="restyle the header component",  # keyword ignored
    ) is False


# ---------------------------------------------------------------------------
# classify_ui_affected — Keyword regex semantics
# ---------------------------------------------------------------------------


def test_classify_keyword_case_insensitive():
    assert classify_ui_affected((), approach="UI") is True
    assert classify_ui_affected((), approach="ui") is True
    assert classify_ui_affected((), approach="Ui") is True
    assert classify_ui_affected((), approach="LAYOUT") is True


def test_classify_keyword_word_boundary_rejects_substring():
    # 'restyle' does NOT contain the word 'style' at a word boundary,
    # so the 'style' keyword alone cannot fire on it.
    assert classify_ui_affected((), approach="restyle only") is False
    # 'rendered' likewise does not fire 'render' (different words that
    # happen to share a prefix are OK — word boundary enforced).
    assert classify_ui_affected((), approach="rendered finally") is False


def test_classify_keyword_punctuation_boundaries_ok():
    # Comma, period, parens all count as word boundaries.
    assert classify_ui_affected((), approach="Fix the layout.") is True
    assert classify_ui_affected((), approach="(UI) is broken") is True
    assert classify_ui_affected((), approach="style,typography") is True


def test_classify_approach_none_or_empty_safe():
    # None-ish approach must not crash — default arg is empty string.
    assert classify_ui_affected(()) is False


# ---------------------------------------------------------------------------
# Extension helpers — constants pinned
# ---------------------------------------------------------------------------


def test_frontend_extensions_set_is_pinned():
    assert _FRONTEND_EXTENSIONS == frozenset(
        {".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html", ".htm"}
    )


def test_frontend_extensions_are_subset_of_classifiable():
    assert _FRONTEND_EXTENSIONS <= _CLASSIFIABLE_EXTENSIONS


# ---------------------------------------------------------------------------
# PlanResult — field default + constructor + skipped_result
# ---------------------------------------------------------------------------


def test_plan_result_default_ui_affected_false():
    r = PlanResult()
    assert r.ui_affected is False


def test_plan_result_constructor_accepts_ui_affected_true():
    r = PlanResult(ui_affected=True)
    assert r.ui_affected is True


def test_plan_result_skipped_result_ui_affected_false():
    # skipped_result always constructs with the default — Visual VERIFY's
    # primary trigger will re-run classify_ui_affected on ctx.target_files
    # directly when the plan is skipped, so this default is correct.
    r = PlanResult.skipped_result("trivial_op")
    assert r.ui_affected is False
    assert r.skipped is True


def test_plan_result_slots_include_ui_affected():
    # Regression guard — if someone removes the slot the field silently
    # disappears at runtime (AttributeError on first read).
    assert "ui_affected" in PlanResult.__slots__


# ---------------------------------------------------------------------------
# End-to-end stamping via PlanGenerator.generate_plan
# ---------------------------------------------------------------------------


class _StubGenerator:
    """Minimal stub that feeds a canned JSON response back through
    ``PlanGenerator._parse_plan_response`` + ``classify_ui_affected``.
    """

    def __init__(self, response: str) -> None:
        self._response = response

    async def plan(self, prompt: str, deadline: datetime) -> str:
        return self._response


def _make_ctx(target_files=("backend/x.py", "backend/y.py"), description="Long enough operation description that defeats the trivial-skip cut-off threshold of 200 characters by repeating useful context about what needs to change across multiple files so the planner does not early-return."):
    return OperationContext.create(
        target_files=target_files,
        description=description,
    )


def _canned_plan_json(approach: str = "implement feature") -> str:
    return (
        '{'
        '"schema_version": "plan.1",'
        f'"approach": "{approach}",'
        '"complexity": "moderate",'
        '"ordered_changes": [],'
        '"risk_factors": [],'
        '"test_strategy": "",'
        '"architectural_notes": ""'
        '}'
    )


@pytest.mark.asyncio
async def test_generate_plan_stamps_ui_affected_from_target_files(tmp_path: Path):
    gen = PlanGenerator(_StubGenerator(_canned_plan_json()), tmp_path)
    ctx = _make_ctx(target_files=("src/Button.tsx", "src/Card.tsx"))
    result = await gen.generate_plan(ctx, datetime.now(tz=timezone.utc) + timedelta(minutes=1))
    assert result.ui_affected is True


@pytest.mark.asyncio
async def test_generate_plan_stamps_ui_affected_false_for_backend(tmp_path: Path):
    gen = PlanGenerator(
        _StubGenerator(_canned_plan_json(approach="restyle the header component")),
        tmp_path,
    )
    # Backend target → structured-negative path → prose ignored.
    ctx = _make_ctx(target_files=("backend/server.py", "backend/util.py"))
    result = await gen.generate_plan(ctx, datetime.now(tz=timezone.utc) + timedelta(minutes=1))
    assert result.ui_affected is False


@pytest.mark.asyncio
async def test_generate_plan_skipped_result_preserves_default_false(tmp_path: Path):
    gen = PlanGenerator(_StubGenerator(_canned_plan_json()), tmp_path)
    # Trivial op — single file + short description → planning is skipped
    ctx = OperationContext.create(
        target_files=("src/Button.tsx",),
        description="trivial UI tweak",
    )
    result = await gen.generate_plan(ctx, datetime.now(tz=timezone.utc) + timedelta(minutes=1))
    assert result.skipped is True
    # On the skipped path, the parser never ran — ui_affected stays at
    # the default. Visual VERIFY's primary trigger (Task 17) will re-run
    # classify_ui_affected on ctx.target_files directly.
    assert result.ui_affected is False


@pytest.mark.asyncio
async def test_generate_plan_stamp_survives_malformed_approach(tmp_path: Path):
    """Even an empty-approach plan still gets stamped correctly."""
    empty_approach_json = _canned_plan_json(approach="")
    gen = PlanGenerator(_StubGenerator(empty_approach_json), tmp_path)
    ctx = _make_ctx(target_files=("src/Button.tsx",))
    result = await gen.generate_plan(ctx, datetime.now(tz=timezone.utc) + timedelta(minutes=1))
    assert result.ui_affected is True  # primary still wins
