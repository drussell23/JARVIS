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


# --- ov awakening Task 9: Mandate 1 made permanent ----------------------
#
# Task 1 split boot-time behavior in scripts/ouroboros_battle_test.py into
# two classes:
#
#   * PURE CEREMONY (``_print_preflight``, the zombie-reap banner) --
#     withheld entirely in COCKPIT mode by being called ONLY from inside
#     ``_run_gated_boot_banners``.
#   * FUNCTIONAL side effects (``_check_api_keys_or_die`` fatal check,
#     ``_reap_zombies`` scan/kill, ``_single_flight_preflight`` conflict
#     rejection) -- these must run unconditionally in BOTH presentation
#     modes, so they live OUTSIDE the gate at their own call sites.
#     ``_reap_zombies``/``_single_flight_preflight`` additionally gate
#     only their own happy-path banner chatter via an explicit ``quiet=``
#     keyword passed at the (ungated) call site -- not via the gate helper.
#
# The brief's literal test (``_single_flight_preflight()`` bare-call count
# outside the gate == 0) does not hold for the real Task 1 shape: that
# function is *never* called from inside the gate at all -- it always runs
# outside it, distinguished instead by requiring an explicit ``quiet=``
# argument. The guard below asserts the invariant that actually holds:
# ceremony-only functions are gate-exclusive; functional-but-gated-banner
# functions always pass ``quiet=`` when called outside the gate; and the
# fatal API-key check is never reachable only from inside the gate.
_SCRIPT = _REPO / "scripts" / "ouroboros_battle_test.py"


def _gate_span(src: str) -> Tuple[int, int]:
    """Return the (start, end) char offsets of _run_gated_boot_banners'
    body within ``src`` -- from its ``def`` line up to (not including)
    the next top-level ``def``."""
    start = src.index("def _run_gated_boot_banners")
    end = src.index("\ndef ", start)
    return start, end


def test_boot_banner_ceremony_only_called_via_gate() -> None:
    """Mandate 1, made permanent: pure-ceremony banners are gate-exclusive;
    functional/fatal boot steps are never gated.

    Would go red if someone re-added an ungated ``_print_preflight()`` call
    to ``main()``'s hot path, or stripped the ``quiet=`` kwarg from an
    outside-the-gate ``_reap_zombies``/``_single_flight_preflight`` call
    (both of which would let COCKPIT's banner-suppression contract leak),
    or moved ``_check_api_keys_or_die()`` inside the gate (which would let
    a presentation mode suppress the no-API-keys fatal exit).
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    gate_start, gate_end = _gate_span(src)

    # 1. _print_preflight() is pure ceremony -- every call site in the
    # file must fall inside the gate body.
    print_preflight_calls = [
        m for m in re.finditer(r"(?<!def )_print_preflight\(\)", src)
    ]
    assert print_preflight_calls, "_print_preflight() is never called at all"
    outside = [
        m for m in print_preflight_calls
        if not (gate_start <= m.start() < gate_end)
    ]
    assert not outside, (
        "_print_preflight() called outside _run_gated_boot_banners at "
        f"offset(s) {[m.start() for m in outside]} -- Mandate 1 violation: "
        "COCKPIT can no longer withhold this banner at the source"
    )

    # 2. _reap_zombies / _single_flight_preflight are FUNCTIONAL and run
    # unconditionally; every call site OUTSIDE the gate body must pass an
    # explicit quiet= kwarg, so only the happy-path banner is suppressed
    # in COCKPIT -- never the scan/kill or conflict-rejection side effects.
    for fn_name in ("_reap_zombies", "_single_flight_preflight"):
        call_rx = re.compile(rf"(?<!def ){re.escape(fn_name)}\(([^)]*)\)")
        outside_calls = [
            m for m in call_rx.finditer(src)
            if not (gate_start <= m.start() < gate_end)
        ]
        assert outside_calls, f"{fn_name} is never called outside the gate"
        for m in outside_calls:
            assert "quiet=" in m.group(1), (
                f"{fn_name} called outside the gate without an explicit "
                f"quiet= kwarg ({m.group(0)!r} at offset {m.start()}) -- "
                "its banner ceremony would leak past Mandate 1"
            )

    # 3. _check_api_keys_or_die is the FATAL preflight -- it must never be
    # reachable only from inside the gate (no presentation mode may
    # suppress the no-API-keys exit).
    assert "_check_api_keys_or_die()" in src
    fatal_calls = [
        m for m in re.finditer(r"(?<!def )_check_api_keys_or_die\(\)", src)
    ]
    assert fatal_calls, "_check_api_keys_or_die() is never called at all"
    assert all(
        not (gate_start <= m.start() < gate_end) for m in fatal_calls
    ), "_check_api_keys_or_die() called from inside the presentation gate"
