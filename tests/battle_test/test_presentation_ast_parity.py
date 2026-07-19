"""Design-language enforcement — router behavior + the AST sentinel.

OV_DESIGN_LANGUAGE.md §6: aesthetic consistency across a 1.45M-line
codebase cannot rest on discipline. Part 1 pins the PresentationRouter's
conformance behavior (glyph ration, telemetry recast, density, tier
adaptivity, master-off passthrough). Part 2 is the SENTINEL: an AST
walker over the declared UI-plane modules asserting that no module
bypasses the router — raw ``print()`` calls and unregistered emoji
literals are compile-level failures, not review hopes. The module list
grows additively as surfaces are swept.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import presentation_router as pr

ROOT = Path(__file__).resolve().parents[2]


# ===========================================================================
# Part 1 — router conformance behavior
# ===========================================================================


def test_glyph_ration_is_closed_six():
    assert pr.canonical_glyph_chars() == frozenset("⏺⎿💭🗣⚠🎙")
    assert set(g.value for g in pr.Glyph) == {
        "action", "detail", "voice", "human", "warn", "audio",
    }


def test_scrub_strips_decorative_emoji():
    assert pr.scrub_glyphs("🔥 deploy 🚀 done 🎯") == "deploy done"


def test_scrub_aliases_legacy_liquidity_glyph():
    assert pr.scrub_glyphs("⛲ anthropic dry ~5m") == "⚠ anthropic dry ~5m"


def test_scrub_preserves_ration_and_typography():
    line = "⏺ apply · ✓ verified ⎿ detail ─ 💭 thinking ⚠ hot 🎙 live 🗣 you"
    assert pr.scrub_glyphs(line) == line


def test_scrub_never_raises():
    assert pr.scrub_glyphs(None) == "None"     # degrades to str()
    assert pr.scrub_glyphs("") == ""


def test_telemetry_leak_detection():
    assert pr.looks_like_telemetry("turn=chat-x session=repl") is True
    assert pr.looks_like_telemetry("a normal sentence = fine") is False
    assert pr.looks_like_telemetry("one pair=only") is False


def test_telemetry_recast_to_detail_voice():
    out = pr.recast_telemetry("[chat] turn=chat-x session=repl")
    assert "turn: chat-x" in out
    assert "session: repl" in out
    assert " · " in out
    assert "=" not in out.replace("[chat]", "")


def test_route_line_prefixes_semantic_glyph():
    r = pr.PresentationRouter()
    out = r.route_line("listening", kind=pr.Glyph.AUDIO)
    assert out.startswith("🎙 ") or out.startswith("mic ")


def test_route_line_does_not_double_prefix():
    r = pr.PresentationRouter()
    out = r.route_line("💭 already voiced", kind=pr.Glyph.VOICE)
    assert out.count("💭") == 1


def test_route_block_collapses_blank_runs():
    r = pr.PresentationRouter()
    out = r.route_block("a\n\n\n\nb")
    assert out == "a\n\nb"


def test_master_off_is_byte_identical(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_ROUTER_ENABLED", "0")
    r = pr.PresentationRouter()
    raw = "🔥 x=1 y=2\n\n\n⛲ untouched"
    assert r.route_block(raw) == raw
    assert r.route_line(raw) == raw


def test_ascii_tier_keeps_geometry(monkeypatch):
    """Tier adaptivity: the semantic mark degrades to its ASCII pair
    (theme.mark) — same shape, no unicode requirement."""
    from backend.core.ouroboros.ui import theme
    assert theme.mark("action", unicode=False) == "*"
    assert theme.mark("warn", unicode=False) == "!"
    assert theme.mark("audio", unicode=False) == "mic"
    assert theme.mark("action", unicode=True) == "⏺"


def test_router_never_raises_on_garbage():
    r = pr.PresentationRouter()
    for bad in (None, 42, b"bytes", object()):
        assert isinstance(r.route_line(bad), str)   # type: ignore[arg-type]


# ===========================================================================
# Part 2 — the AST sentinel
# ===========================================================================

#: UI-plane modules under the design-language law. ADDITIVE — sweeping a
#: surface means adding it here so it can never rot back (§7 ledger).
SENTINEL_MODULES = (
    "backend/core/ouroboros/battle_test/presentation_router.py",
    "backend/core/ouroboros/governance/chat_text_bridge.py",
    "backend/core/ouroboros/governance/comms/duplex/audio_state_ipc.py",
    "backend/core/ouroboros/battle_test/status_line.py",
)

#: Per-module emoji grants for glyphs that are DATA (protocol payload,
#: docstrings describing renders) rather than direct UI emission.
_EMOJI_GRANTS = {
    # none currently — grants require a comment in this table saying why
}


def _iter_module_asts():
    for rel in SENTINEL_MODULES:
        path = ROOT / rel
        yield rel, ast.parse(path.read_text()), path.read_text()


def _is_ui_plane_string(s: str) -> bool:
    return any(pr._is_decorative_symbol(ch) for ch in s)


def test_sentinel_no_raw_print_on_ui_plane():
    """Raw ``print()`` bypasses every conformance layer — banned in the
    sentinel modules (they must write through a console/logger/router)."""
    offenders = []
    for rel, tree, _src in _iter_module_asts():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"raw print() on the UI plane: {offenders}"


def test_sentinel_no_unregistered_emoji_literals():
    """String literals in sentinel modules may carry ONLY the rationed
    glyphs + typography (or an explicit grant). Docstrings are exempt
    (they describe, they don't render)."""
    offenders = []
    for rel, tree, _src in _iter_module_asts():
        grants = _EMOJI_GRANTS.get(rel, frozenset())
        docstring_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                ds = ast.get_docstring(node, clean=False)
                if ds and node.body:
                    first = node.body[0]
                    docstring_lines.update(
                        range(first.lineno, (first.end_lineno or first.lineno) + 1)
                    )
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.lineno in docstring_lines:
                    continue
                bad = [
                    ch for ch in node.value
                    if pr._is_decorative_symbol(ch) and ch not in grants
                ]
                if bad:
                    offenders.append(f"{rel}:{node.lineno} {bad!r}")
    assert not offenders, f"unregistered emoji on the UI plane: {offenders}"


def test_sentinel_repl_print_routes_through_router():
    """THE chokepoint pin: the harness funnel must pipe through the
    PresentationRouter — every verb inherits the law through it."""
    src = (ROOT / "backend/core/ouroboros/battle_test/harness.py").read_text()
    body_start = src.index("def _repl_print")
    body = src[body_start:body_start + 1200]
    assert "presentation_router" in body
    assert "route_block" in body


def test_sentinel_status_line_uses_canonical_warn():
    src = (ROOT / "backend/core/ouroboros/battle_test/status_line.py").read_text()
    assert "⛲" not in src            # legacy glyph fully retired at source
    assert "⚠" in src


def test_sentinel_module_list_is_additive_documented():
    """The sweep ledger contract: every sentinel module exists, and the
    design doc references the sentinel by name."""
    for rel in SENTINEL_MODULES:
        assert (ROOT / rel).is_file(), rel
    doc = (ROOT / "docs/architecture/OV_DESIGN_LANGUAGE.md").read_text()
    assert "test_presentation_ast_parity.py" in doc
    assert "PresentationRouter" in doc
