"""§37 Slice 4 — color discipline AST lint pin regression spine.

Pins identity invariant #3 (`green = outcomes only`) per the
operator binding 2026-05-05:

  * Canonical palette constants pinned to exact bytes
    (drift breaks identity invariant silently)
  * Lint scopes to `backend/core/ouroboros/governance/` ONLY
    — battle_test/ legacy code untouched (it has its own
    `chrome_color()` discipline)
  * Docstrings excluded — discipline can be DOCUMENTED in
    English without false-positives
  * Allowlist is operator-bound — every entry has a documented
    rationale
  * Live state passes lint — proves the discipline holds
    against current codebase post-Slice 1+2+3 work
  * Synthetic regressions prove the lint fires on new
    violations

Verifies (16 tests).
"""
from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Canonical palette constants — exact bytes
# ---------------------------------------------------------------------------


def test_outcome_green_bright_ansi_exact_bytes():
    from backend.core.ouroboros.governance.palette import (
        OUTCOME_GREEN_BRIGHT_ANSI,
    )
    assert OUTCOME_GREEN_BRIGHT_ANSI == "\033[92m"


def test_outcome_green_bright_rich_exact_string():
    from backend.core.ouroboros.governance.palette import (
        OUTCOME_GREEN_BRIGHT_RICH,
    )
    assert OUTCOME_GREEN_BRIGHT_RICH == "bright_green"


def test_reset_ansi_exact_bytes():
    from backend.core.ouroboros.governance.palette import (
        RESET_ANSI,
    )
    assert RESET_ANSI == "\033[0m"


def test_canonical_constants_pin_validates_clean():
    """The AST pin for canonical palette constants validates
    against the live source."""
    from backend.core.ouroboros.governance.palette import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/palette.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_canonical_constants_pin_fires_on_drift():
    """If a future edit silently changes the canonical bytes,
    the pin fires."""
    from backend.core.ouroboros.governance.palette import (
        register_shipped_invariants,
    )
    bad_source = '''
OUTCOME_GREEN_BRIGHT_ANSI: str = "wrong-bytes"
OUTCOME_GREEN_BRIGHT_RICH: str = "different_color"
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "outcome_green_canonical" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "OUTCOME_GREEN_BRIGHT_ANSI" in v
        or "OUTCOME_GREEN_BRIGHT_RICH" in v
        for v in violations
    )


def test_canonical_constants_pin_fires_when_constants_missing():
    from backend.core.ouroboros.governance.palette import (
        register_shipped_invariants,
    )
    # Palette without the constants
    bad_source = '''
PALETTE_MODULE_NAME: str = "palette"
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "outcome_green_canonical" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any("missing" in v.lower() for v in violations)


# ---------------------------------------------------------------------------
# Live lint — discipline holds against current codebase
# ---------------------------------------------------------------------------


def test_live_governance_passes_lint():
    """Load-bearing pin: the live `governance/` state MUST be
    clean per the discipline. Every Slice 1-4 work composes
    `OUTCOME_GREEN_BRIGHT_ANSI` from palette.py instead of
    direct literals."""
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    violations = lint_governance_for_bright_green_leaks()
    assert violations == [], (
        f"governance/ has {len(violations)} bright-green "
        f"discipline violations:\n  "
        + "\n  ".join(
            f"{v[0]}:{v[1]}  {v[2]}" for v in violations
        )
    )


def test_my_slice_modules_compliant():
    """Specifically pin that the §37 Slice 1-3 modules I wrote
    earlier comply with the discipline (they should — they use
    regular green `\\033[32m`, NOT bright_green)."""
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    violations = lint_governance_for_bright_green_leaks()
    offenders = {v[0] for v in violations}
    for slice_module in (
        "health_repl.py",
        "listen_repl.py",
        "why_changed_repl.py",
    ):
        for o in offenders:
            assert not o.endswith(slice_module), (
                f"§37 Slice module {slice_module} violates "
                f"the discipline: {o}"
            )


# ---------------------------------------------------------------------------
# Synthetic regressions — lint fires on new violations
# ---------------------------------------------------------------------------


def test_lint_fires_on_synthetic_ansi_violation(tmp_path):
    """A synthetic governance/ file with a direct `\\033[92m`
    literal triggers a violation."""
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    bad_file = tmp_path / "bad_chrome.py"
    bad_file.write_text(
        'BAD_COLOR = "\\033[92m"  # chrome violation\n',
        encoding="utf-8",
    )
    violations = lint_governance_for_bright_green_leaks(
        governance_root=tmp_path,
    )
    assert len(violations) == 1
    assert violations[0][0] == str(bad_file)


def test_lint_fires_on_synthetic_rich_violation(tmp_path):
    """A synthetic governance/ file with `[bright_green]` Rich
    markup triggers a violation."""
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    bad_file = tmp_path / "bad_chrome.py"
    bad_file.write_text(
        'STATUS = "[bright_green]ON[/bright_green]"\n',
        encoding="utf-8",
    )
    violations = lint_governance_for_bright_green_leaks(
        governance_root=tmp_path,
    )
    assert len(violations) == 1


def test_lint_excludes_docstrings(tmp_path):
    """Docstrings can mention `bright_green` for discipline doc
    without triggering false-positives."""
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    file_with_docstring = tmp_path / "ok_doc.py"
    file_with_docstring.write_text(
        '"""Module docstring mentioning bright_green '
        'discipline. No violation."""\n'
        "X = 42\n",
        encoding="utf-8",
    )
    violations = lint_governance_for_bright_green_leaks(
        governance_root=tmp_path,
    )
    assert violations == []


def test_lint_excludes_function_docstrings(tmp_path):
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    file_path = tmp_path / "ok_func_doc.py"
    file_path.write_text(
        "def foo():\n"
        '    """Docstring referencing bright_green chrome '
        'discipline."""\n'
        "    return 42\n",
        encoding="utf-8",
    )
    violations = lint_governance_for_bright_green_leaks(
        governance_root=tmp_path,
    )
    assert violations == []


def test_lint_excludes_class_docstrings(tmp_path):
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    file_path = tmp_path / "ok_class_doc.py"
    file_path.write_text(
        "class Foo:\n"
        '    """Class docstring mentioning bright_green."""\n'
        "    pass\n",
        encoding="utf-8",
    )
    violations = lint_governance_for_bright_green_leaks(
        governance_root=tmp_path,
    )
    assert violations == []


def test_lint_skips_palette_module_itself(tmp_path):
    """The palette module IS the canonical anchor — its own
    constants are exempt."""
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    palette_clone = tmp_path / "palette.py"
    palette_clone.write_text(
        'OUTCOME_GREEN_BRIGHT_ANSI = "\\033[92m"\n'
        'OUTCOME_GREEN_BRIGHT_RICH = "bright_green"\n',
        encoding="utf-8",
    )
    violations = lint_governance_for_bright_green_leaks(
        governance_root=tmp_path,
    )
    assert violations == []


def test_lint_skips_tests_directory(tmp_path):
    """Test fixtures may construct synthetic bad source —
    lint exempts tests/ subdirectory."""
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_something.py"
    test_file.write_text(
        'BAD_COLOR_FIXTURE = "\\033[92m"\n',
        encoding="utf-8",
    )
    violations = lint_governance_for_bright_green_leaks(
        governance_root=tmp_path,
    )
    assert violations == []


def test_lint_handles_unparseable_file_gracefully(tmp_path):
    """A file with syntax errors is skipped, not crashed."""
    from backend.core.ouroboros.governance.palette import (
        lint_governance_for_bright_green_leaks,
    )
    bad_syntax = tmp_path / "bad_syntax.py"
    bad_syntax.write_text(
        "this is not valid python\n!!!\n",
        encoding="utf-8",
    )
    # Should NOT raise
    violations = lint_governance_for_bright_green_leaks(
        governance_root=tmp_path,
    )
    assert violations == []


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def test_allowlist_contains_documented_entries():
    """Every allowlist entry must have a documented rationale
    (verified via grep — surfaces in code review)."""
    from backend.core.ouroboros.governance.palette import (
        _LEGACY_LINT_ALLOWLIST,
    )
    assert isinstance(_LEGACY_LINT_ALLOWLIST, frozenset)
    # Sanity: only known-grandfathered files
    expected = {"observability/multi_op_renderer.py"}
    assert _LEGACY_LINT_ALLOWLIST == expected, (
        "allowlist drift detected — adding new entries is "
        "operator-binding; document rationale before merging"
    )


def test_allowlist_path_actually_exists():
    """The grandfathered file MUST still exist; otherwise the
    allowlist entry is stale + should be removed."""
    from backend.core.ouroboros.governance.palette import (
        _LEGACY_LINT_ALLOWLIST,
    )
    governance_root = (
        _repo_root() / "backend/core/ouroboros/governance"
    )
    for rel_path in _LEGACY_LINT_ALLOWLIST:
        full_path = governance_root / rel_path
        assert full_path.exists(), (
            f"allowlist entry {rel_path!r} no longer exists — "
            f"remove the stale entry"
        )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import palette
    expected = {
        "OUTCOME_GREEN_BRIGHT_ANSI",
        "OUTCOME_GREEN_BRIGHT_RICH",
        "PALETTE_MODULE_NAME",
        "RESET_ANSI",
        "lint_governance_for_bright_green_leaks",
        "register_shipped_invariants",
    }
    assert set(palette.__all__) == expected
