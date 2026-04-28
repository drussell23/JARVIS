"""Phase 2 Slice 2.5 — graduation pin suite.

Pins the graduated state of the four Phase 2 Closed-Loop Self-Verification
master flags. These were introduced default-false in Slices 2.1
(PropertyOracle), 2.2 (RepeatRunner), 2.3 (property_capture) and 2.4
(VerificationPostmortem), then flipped to default-true in Slice 2.5.

The pin suite enforces:

  * defaults are True when env is unset OR empty-string OR whitespace
  * each ``"false"``-class override returns False (hot-revert path)
  * full-revert matrix: flipping one flag off doesn't cross-couple
    any of the others
  * source-level pins: each function literally returns ``True`` from
    the empty-string branch (catches accidental refactor regression)

All four flags can be hot-reverted independently. The flags govern
distinct subsystems with independent rollback authority:

  * JARVIS_VERIFICATION_ORACLE_ENABLED          — Slice 2.1: PropertyOracle
  * JARVIS_VERIFICATION_REPEAT_RUNNER_ENABLED   — Slice 2.2: RepeatRunner
  * JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED — Slice 2.3: claim capture
  * JARVIS_VERIFICATION_POSTMORTEM_ENABLED      — Slice 2.4: postmortem

Pin sections:
  §1   Defaults are True when env unset
  §2   Empty-string env reads as default-True (unset marker)
  §3   Whitespace-only env reads as default-True
  §4   Each ``"false"``-class override returns False
  §5   Full-revert matrix (one-off flip doesn't cross-couple)
  §6   Garbage / unknown values revert to False (strict opt-in)
  §7   Source-level pin: each function has the graduated branch
  §8   Public API surface — graduated readers exposed from package
  §9   Independent subsystem rollback
  §10  Composability: all four flags engaged simultaneously
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.verification import (
    oracle_enabled,
    postmortem_enabled,
    property_capture_enabled,
    repeat_runner_enabled,
)


# ---------------------------------------------------------------------------
# Centralized graduated-flag list — single source of truth
# ---------------------------------------------------------------------------


_GRADUATED_FLAGS = [
    ("JARVIS_VERIFICATION_ORACLE_ENABLED", oracle_enabled),
    ("JARVIS_VERIFICATION_REPEAT_RUNNER_ENABLED", repeat_runner_enabled),
    (
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED",
        property_capture_enabled,
    ),
    ("JARVIS_VERIFICATION_POSTMORTEM_ENABLED", postmortem_enabled),
]


# ===========================================================================
# §1 — Defaults are True when env unset
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_default_is_true_when_env_unset(
    env_name: str, reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delenv → reader returns True. The graduated default."""
    monkeypatch.delenv(env_name, raising=False)
    assert reader() is True, (
        f"Slice 2.5 graduation: {env_name} must default True"
    )


# ===========================================================================
# §2 — Empty string is the unset marker
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_empty_string_reads_as_default_true(
    env_name: str, reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setenv("", "")`` matches delenv. Operators commonly clear via
    shell ``export FOO=`` (which sets to empty string)."""
    monkeypatch.setenv(env_name, "")
    assert reader() is True


# ===========================================================================
# §3 — Whitespace-only reads as default-True
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
@pytest.mark.parametrize("ws", [" ", "  ", "\t", " \t "])
def test_whitespace_reads_as_default_true(
    env_name: str, reader, ws: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.strip()`` collapses whitespace to empty → graduated default."""
    monkeypatch.setenv(env_name, ws)
    assert reader() is True


# ===========================================================================
# §4 — Hot-revert: explicit false-class strings disable
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
@pytest.mark.parametrize("falsy", ["false", "0", "no", "off", "FALSE"])
def test_false_class_string_reverts(
    env_name: str, reader, falsy: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four operator-visible false-class spellings still disable
    the feature post-graduation. Critical for emergency rollback."""
    monkeypatch.setenv(env_name, falsy)
    assert reader() is False, (
        f"{env_name}={falsy!r} should disable the feature"
    )


# ===========================================================================
# §5 — Full-revert matrix
# ===========================================================================


def test_full_revert_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip each flag off in turn; verify ONLY that one flag flips
    while the other three stay True. Catches accidental cross-coupling
    (e.g., a refactor that gates the postmortem behind the oracle flag
    would fail this matrix)."""
    for env_off, _ in _GRADUATED_FLAGS:
        # Reset all to default (delenv) — graduated state
        for env_name, _r in _GRADUATED_FLAGS:
            monkeypatch.delenv(env_name, raising=False)
        # Flip exactly one off
        monkeypatch.setenv(env_off, "false")
        # Verify the flipped one is off, the rest still True
        for env_name, reader in _GRADUATED_FLAGS:
            expected = (env_name != env_off)
            actual = reader()
            assert actual is expected, (
                f"After flipping {env_off}=false, {env_name} reader "
                f"returned {actual} (expected {expected})"
            )


# ===========================================================================
# §6 — Garbage values revert to False (strict opt-in to non-default)
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
@pytest.mark.parametrize("garbage", ["maybe", "unknown", "2", "ENABLED"])
def test_garbage_values_revert_to_false(
    env_name: str, reader, garbage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-graduation: any non-truthy string returned False. Post-
    graduation: empty/whitespace are now True (unset marker), but any
    NON-empty string that isn't in the truthy set still returns False.
    Asymmetric on purpose — operators must explicitly opt-in to
    non-default values via the truthy strings."""
    monkeypatch.setenv(env_name, garbage)
    assert reader() is False, (
        f"{env_name}={garbage!r} should revert to False — only the "
        f"explicit truthy strings + the unset marker yield True"
    )


# ===========================================================================
# §7 — Source-level pins (catches refactor regression)
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_source_has_graduated_branch(env_name: str, reader) -> None:
    """Pin: each flag function has the ``if raw == "": return True``
    graduated branch. Catches accidental revert via refactor."""
    src = inspect.getsource(reader)
    assert 'if raw == ""' in src, (
        f"{reader.__name__} source must contain the graduated empty-"
        f"string branch (Slice 2.5 pin)"
    )
    assert "return True" in src, (
        f"{reader.__name__} source must contain `return True` for the "
        f"graduated default"
    )
    # The function must still consult the truthy set so explicit
    # opt-in values keep working.
    assert '"true"' in src or "'true'" in src, (
        f"{reader.__name__} source must still recognize 'true' as "
        f"explicit truthy value"
    )


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_source_documents_graduated_default(env_name: str, reader) -> None:
    """Each docstring documents the graduated default. Operators
    grepping for "default ``true``" find the four Slice 2.5 flags."""
    src = inspect.getsource(reader)
    has_doc = (
        "default ``true``" in src
        or "graduated in Phase 2 Slice 2.5" in src
    )
    assert has_doc, (
        f"{reader.__name__} docstring must document the graduated "
        f"default (Slice 2.5)"
    )


# ===========================================================================
# §8 — Public API surface
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_reader_is_callable_no_args(env_name: str, reader) -> None:
    """Each flag function is callable with no arguments + returns a
    bool. Stable contract for cross-module consumers."""
    result = reader()
    assert isinstance(result, bool), (
        f"{reader.__name__} must return bool, got {type(result)}"
    )


def test_all_four_flags_exposed_from_package() -> None:
    """``backend.core.ouroboros.governance.verification`` __all__
    exposes the four readers."""
    from backend.core.ouroboros.governance import verification
    assert "oracle_enabled" in verification.__all__
    assert "repeat_runner_enabled" in verification.__all__
    assert "property_capture_enabled" in verification.__all__
    assert "postmortem_enabled" in verification.__all__


# ===========================================================================
# §9 — Independent subsystem rollback
# ===========================================================================


def test_independent_subsystem_rollback(monkeypatch) -> None:
    """Operator can roll back ANY single subsystem without affecting
    the others. Critical for granular debugging in production."""
    # Roll back postmortem, keep others on
    monkeypatch.setenv("JARVIS_VERIFICATION_POSTMORTEM_ENABLED", "false")
    monkeypatch.delenv(
        "JARVIS_VERIFICATION_ORACLE_ENABLED", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_VERIFICATION_REPEAT_RUNNER_ENABLED", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", raising=False,
    )

    assert oracle_enabled() is True             # still on
    assert repeat_runner_enabled() is True      # still on
    assert property_capture_enabled() is True   # still on
    assert postmortem_enabled() is False        # rolled back


# ===========================================================================
# §10 — Composability: all four flags engaged simultaneously
# ===========================================================================


def test_all_flags_engaged_simultaneously(monkeypatch) -> None:
    """Default-on means all four substrates engage by default. Pin
    that no flag silently disables itself when others are off
    (cross-coupling regression check)."""
    for env_name, _ in _GRADUATED_FLAGS:
        monkeypatch.delenv(env_name, raising=False)
    assert oracle_enabled() is True
    assert repeat_runner_enabled() is True
    assert property_capture_enabled() is True
    assert postmortem_enabled() is True
