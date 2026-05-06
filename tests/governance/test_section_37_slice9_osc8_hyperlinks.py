"""§37 Slice 9 — OSC 8 hyperlink substrate regression spine.

Pins per operator binding 2026-05-05:

  * TERM-aware detection composes Gap #7 Slice 2 real_stdout
    pattern (sys.__stdout__, NOT sys.stdout)
  * Master flag operator opt-out
  * Pure-function wrapper (NEVER raises, no global state)
  * Falls through to plain text on non-supporting terminals
  * /help verb listing wires OSC 8 hyperlinks via auto-derived
    governance/<verb>_repl.py paths
  * AST-pinned authority asymmetry + real_stdout discipline

Verifies (22 tests).
"""
from __future__ import annotations

import ast
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# wrap() — pure-function correctness
# ---------------------------------------------------------------------------


def test_wrap_force_true_renders_osc8():
    from backend.core.ouroboros.governance.osc8 import wrap
    out = wrap("/health", "file:///path", force=True)
    assert "\x1b]8;;file:///path\x1b\\" in out
    assert "/health" in out
    assert out.endswith("\x1b]8;;\x1b\\")


def test_wrap_force_false_returns_plain():
    from backend.core.ouroboros.governance.osc8 import wrap
    assert wrap("/health", "file:///path", force=False) == (
        "/health"
    )


def test_wrap_empty_url_returns_plain():
    from backend.core.ouroboros.governance.osc8 import wrap
    assert wrap("text", "", force=True) == "text"


def test_wrap_none_url_returns_plain():
    from backend.core.ouroboros.governance.osc8 import wrap
    assert wrap(
        "text", None,  # type: ignore
        force=True,
    ) == "text"


def test_wrap_non_string_text_coerces():
    from backend.core.ouroboros.governance.osc8 import wrap
    # Non-string text should NOT crash — coerce to str
    assert "123" in wrap(
        123,  # type: ignore
        "file:///x",
        force=True,
    )


def test_wrap_does_not_raise():
    """Defensive: any input combination MUST NOT raise."""
    from backend.core.ouroboros.governance.osc8 import wrap
    # All these are deliberately bad
    for text, url in [
        (None, None),
        (123, 456),
        ("", ""),
        ("text", None),
    ]:
        try:
            result = wrap(text, url, force=True)  # type: ignore
            assert isinstance(result, str)
        except Exception as exc:
            pytest.fail(f"wrap raised on ({text!r}, {url!r}): {exc}")


# ---------------------------------------------------------------------------
# file_url() — URL construction
# ---------------------------------------------------------------------------


def test_file_url_constructs_absolute():
    from backend.core.ouroboros.governance.osc8 import file_url
    url = file_url("/tmp/test.py")
    assert url.startswith("file://")
    assert "/tmp/test.py" in url


def test_file_url_with_line():
    from backend.core.ouroboros.governance.osc8 import file_url
    url = file_url("/tmp/test.py", line=42)
    assert url.endswith("#L42")


def test_file_url_empty_returns_empty():
    from backend.core.ouroboros.governance.osc8 import file_url
    assert file_url("") == ""


def test_file_url_handles_non_string():
    from backend.core.ouroboros.governance.osc8 import file_url
    # Defensive — should not raise
    assert file_url(None) == ""  # type: ignore


# ---------------------------------------------------------------------------
# is_supported() — composition of master flag + TTY + TERM
# ---------------------------------------------------------------------------


def test_is_supported_master_flag_off(monkeypatch):
    monkeypatch.setenv("JARVIS_OSC8_HYPERLINKS_ENABLED", "false")
    from backend.core.ouroboros.governance.osc8 import (
        is_supported,
    )
    assert is_supported() is False


def test_is_supported_non_tty_returns_false(monkeypatch):
    """When sys.__stdout__ is not a TTY, OSC 8 is suppressed."""
    monkeypatch.setenv("JARVIS_OSC8_HYPERLINKS_ENABLED", "true")
    monkeypatch.setenv("TERM", "xterm-256color")
    from backend.core.ouroboros.governance.osc8 import (
        is_supported,
    )
    # Replace __stdout__ with a non-TTY object
    fake_stdout = StringIO()  # not a TTY
    monkeypatch.setattr(sys, "__stdout__", fake_stdout)
    assert is_supported() is False


def test_is_supported_unknown_term_returns_false(monkeypatch):
    """A TERM not in the known-supporting list returns False
    (conservative)."""
    monkeypatch.setenv("JARVIS_OSC8_HYPERLINKS_ENABLED", "true")
    monkeypatch.setenv("TERM", "unknown-terminal")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)

    class FakeTTY:
        def isatty(self):
            return True

    from backend.core.ouroboros.governance import osc8
    monkeypatch.setattr(sys, "__stdout__", FakeTTY())
    assert osc8.is_supported() is False


def test_is_supported_known_term_returns_true(monkeypatch):
    monkeypatch.setenv("JARVIS_OSC8_HYPERLINKS_ENABLED", "true")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)

    class FakeTTY:
        def isatty(self):
            return True

    from backend.core.ouroboros.governance import osc8
    monkeypatch.setattr(sys, "__stdout__", FakeTTY())
    assert osc8.is_supported() is True


def test_is_supported_known_term_program(monkeypatch):
    """TERM_PROGRAM matching also enables OSC 8."""
    monkeypatch.setenv("JARVIS_OSC8_HYPERLINKS_ENABLED", "true")
    monkeypatch.setenv("TERM", "")
    monkeypatch.setenv("TERM_PROGRAM", "vscode")

    class FakeTTY:
        def isatty(self):
            return True

    from backend.core.ouroboros.governance import osc8
    monkeypatch.setattr(sys, "__stdout__", FakeTTY())
    assert osc8.is_supported() is True


# ---------------------------------------------------------------------------
# /help verb listing wiring
# ---------------------------------------------------------------------------


def test_help_dispatcher_uses_osc8_module():
    """AST regression: help_dispatcher.py MUST import the
    canonical osc8 module."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/help_dispatcher.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "osc8" in source, (
        "help_dispatcher.py MUST wire the canonical osc8 "
        "module (§37 Slice 9 regression)"
    )
    assert "_osc8_wrap" in source or "wrap as" in source, (
        "help_dispatcher.py MUST compose osc8.wrap()"
    )


def test_help_verb_listing_renders_clickable_when_supported():
    """When OSC 8 is force-enabled, the verb listing wraps
    governance/<verb>_repl.py-backed verbs in OSC 8 escapes."""
    from backend.core.ouroboros.governance.help_dispatcher import (
        _list_verbs,
        VerbRegistry,
        VerbSpec,
    )
    reg = VerbRegistry()
    reg.register(VerbSpec(
        name="/health",  # exists at governance/health_repl.py
        one_line="component health dashboard",
        category="observability",
    ))
    reg.register(VerbSpec(
        name="/listen",  # exists at governance/listen_repl.py
        one_line="event stream tail",
        category="observability",
    ))
    # Force OSC 8 rendering for the test
    with patch(
        "backend.core.ouroboros.governance.osc8.is_supported",
        return_value=True,
    ):
        result = _list_verbs(reg)
    assert result.ok is True
    # OSC 8 escape sequence should be present for the
    # auto-derived `<verb>_repl.py` files that exist
    assert "\x1b]8;;" in result.text


def test_help_verb_listing_falls_through_when_unsupported():
    """When OSC 8 isn't supported, the listing renders plain
    text (no escape sequences)."""
    from backend.core.ouroboros.governance.help_dispatcher import (
        _list_verbs,
        VerbRegistry,
        VerbSpec,
    )
    reg = VerbRegistry()
    reg.register(VerbSpec(
        name="/health",
        one_line="component health dashboard",
        category="observability",
    ))
    with patch(
        "backend.core.ouroboros.governance.osc8.is_supported",
        return_value=False,
    ):
        result = _list_verbs(reg)
    assert result.ok is True
    # No OSC 8 escape sequences
    assert "\x1b]8;;" not in result.text
    # Plain verb name preserved
    assert "/health" in result.text


def test_help_verb_listing_skips_nonexistent_repl_files():
    """A verb whose corresponding governance/<verb>_repl.py
    doesn't exist gets plain rendering (no broken hyperlink)."""
    from backend.core.ouroboros.governance.help_dispatcher import (
        _list_verbs,
        VerbRegistry,
        VerbSpec,
    )
    reg = VerbRegistry()
    reg.register(VerbSpec(
        name="/nonexistent_verb_xyz",
        one_line="hypothetical",
        category="general",
    ))
    with patch(
        "backend.core.ouroboros.governance.osc8.is_supported",
        return_value=True,
    ):
        result = _list_verbs(reg)
    # Should NOT contain OSC 8 escape — file doesn't exist
    # so wrap is skipped for this verb
    assert "/nonexistent_verb_xyz" in result.text
    # Either zero escape sequences (none of the verbs match) OR
    # the escape is for a different verb (defensive check —
    # ensure the nonexistent verb's name isn't inside an OSC 8)
    nonexistent_idx = result.text.find("/nonexistent_verb_xyz")
    assert nonexistent_idx >= 0
    # Look for OSC 8 wrapping THIS specific verb — should not
    # be present
    surrounding = result.text[
        max(0, nonexistent_idx - 50):nonexistent_idx + 50
    ]
    assert "\x1b]8;;" not in surrounding


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_2():
    from backend.core.ouroboros.governance.osc8 import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert len(invs) == 2
    names = {i.invariant_name for i in invs}
    assert names == {
        "osc8_uses_real_stdout_isatty",
        "osc8_authority_asymmetry",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.osc8 import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/osc8.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_real_stdout_pin_fires_on_sys_stdout_regression():
    """Synthetic regression: if a future refactor uses
    sys.stdout.isatty() instead of sys.__stdout__, the pin
    fires (Gap #7 Slice 2 discipline)."""
    from backend.core.ouroboros.governance.osc8 import (
        register_shipped_invariants,
    )
    bad_source = '''
import sys

def _real_stdout_isatty():
    return sys.stdout.isatty()
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "real_stdout_isatty" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any("__stdout__" in v for v in violations)


def test_authority_asymmetry_pin_fires_on_forbidden_import():
    from backend.core.ouroboros.governance.osc8 import (
        register_shipped_invariants,
    )
    bad_source = '''
from backend.core.ouroboros.governance.iron_gate import foo
'''
    tree = ast.parse(bad_source)
    invs = register_shipped_invariants()
    pin = next(
        i for i in invs
        if "authority_asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import osc8
    expected = {
        "OSC8_SCHEMA_VERSION",
        "file_url",
        "is_supported",
        "register_shipped_invariants",
        "wrap",
    }
    assert set(osc8.__all__) == expected
