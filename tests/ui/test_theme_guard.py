"""Structural guard: no literal styling may regress into the presentation layer.

This is the enforcement mechanism for the Restrained Mono migration (spec
2026-07-06 §8, mandate #4). It scans the ``ui/`` package plus every migrated
presentation consumer for banned patterns:

  * legacy Rich color markup  -- ``[bold cyan]`` / ``[bright_green]`` / ``[dim]``
  * raw ANSI CSI escapes       -- ``\\x1b[`` (OSC title ``\\x1b]`` is allowed)
  * hardcoded frames           -- ``"─" * 52`` and friends

``theme.py`` is the single exempt source of truth -- it legitimately defines
the concrete color values and box glyphs the tokens resolve to. Consumers must
reference semantic tokens (``[accent]``, ``[muted]`` ...) instead, which this
guard does not match.

If this test fails, do NOT add a runtime stripper -- physically replace the
literal with a token / primitive at the source (mandate #1).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

_REPO = Path(__file__).resolve().parents[2]
_UI_DIR = _REPO / "backend" / "core" / "ouroboros" / "ui"
_BATTLE = _REPO / "backend" / "core" / "ouroboros" / "battle_test"

# theme.py is the token source-of-truth (holds #3AAFA9, "cyan", box glyphs).
_EXEMPT = {"theme.py"}

# Presentation consumers migrated to the theme. Every listed file is held to
# the no-literal-styling contract forever. diff_preview uses portable concrete
# styles (theme.styles()) so its returned renderable is themed on any console.
#
# serpent_flow.boot_banner: its ACTIVE (Restrained Mono) path delegates to the
# guarded render_minimal_welcome/render_organism above; its own log line was
# migrated. The dense multi-section path below the restraint guard is the
# intentional JARVIS_PRESENTATION_RESTRAINT_ENABLED=false rollback (old look by
# design) and is deliberately NOT scanned.
_MIGRATED_CONSUMERS = [
    _BATTLE / "presentation_restraint.py",
    _BATTLE / "status_line.py",
    _BATTLE / "live_status_line.py",
    _BATTLE / "diff_preview.py",
    _BATTLE / "diff_display.py",
    _BATTLE / "tool_render_view.py",
    _BATTLE / "boot_timing.py",
]

_BANNED = {
    "rich_color_markup": re.compile(
        r"\[/?(?:bold |dim |italic |underline |blink |reverse )*"
        r"(?:bright_)?(?:black|red|green|yellow|blue|magenta|cyan|white|gr[ae]y\d*)\b"
    ),
    "dim_markup": re.compile(r"\[/?dim\b"),
    # The keyword form: style="bold cyan" / border_style="dim" (Rich Text/Panel).
    "style_kw_color": re.compile(
        r"""(?:border_)?style\s*=\s*["'](?:bold |dim |italic |underline )*"""
        r"""(?:bright_)?(?:black|red|green|yellow|blue|magenta|cyan|white|gr[ae]y\d*)\b"""
    ),
    "style_kw_dim": re.compile(r"""(?:border_)?style\s*=\s*["']dim\b"""),
    "raw_ansi_csi": re.compile(r"\\x1b\["),
    "hardcoded_frame": re.compile(
        r"""["'][─═]["']\s*\*|["'][=\-]["']\s*\*\s*\d{2,}"""
    ),
}


def _scan(path: Path) -> List[Tuple[int, str, str]]:
    """Return (lineno, pattern_name, line) offenders in a file."""
    hits: List[Tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return hits
    for i, line in enumerate(text.splitlines(), start=1):
        for name, rx in _BANNED.items():
            if rx.search(line):
                hits.append((i, name, line.strip()))
    return hits


def _ui_files() -> List[Path]:
    return [
        p for p in _UI_DIR.glob("*.py")
        if p.name not in _EXEMPT and "__pycache__" not in str(p)
    ]


def test_ui_package_has_no_literal_styling() -> None:
    offenders = {}
    for path in _ui_files():
        hits = _scan(path)
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"literal styling in ui/: {offenders}"


def test_migrated_consumers_have_no_literal_styling() -> None:
    offenders = {}
    for path in _MIGRATED_CONSUMERS:
        hits = _scan(path)
        if hits:
            offenders[str(path.relative_to(_REPO))] = hits
    assert not offenders, f"literal styling regressed: {offenders}"


def test_boot_banner_active_path_log_line_migrated() -> None:
    """boot_banner's Restrained Mono (active) path delegates to the guarded
    render_minimal_welcome/render_organism; its own log line must use the muted
    token — not the legacy ``[dim]`` + emoji form. Positive assertion, robust to
    the 8000-line REPL. The dense multi-section path below the restraint guard
    is the intentional PRESENTATION_RESTRAINT=false rollback and is not scanned.
    """
    sf = (_BATTLE / "serpent_flow.py").read_text(encoding="utf-8")
    assert "[muted]{log_path}[/muted]" in sf
