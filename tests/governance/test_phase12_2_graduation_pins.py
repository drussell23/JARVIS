"""Phase 12.2 Slice E — graduation pin suite.

Pins the graduated state of the four Phase 12.2 master flags. These
were introduced default-false in Slices A/B/C/D and flipped to
default-true in Slice E. The pin suite enforces:

  * defaults are True when env is unset OR empty-string OR whitespace
  * each ``"false"``-class override returns False (hot-revert path)
  * full-revert matrix: flipping one flag off doesn't cross-couple
    any of the others
  * source-level pins: each function literally returns ``True`` from
    the empty-string branch (catches accidental refactor regression)

All four flags can be hot-reverted independently. The flags govern
distinct subsystems with independent rollback authority:

  * JARVIS_TOPOLOGY_FULL_JITTER_ENABLED   — retry-site backoff
  * JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED — observer record-on-write
  * JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED — promotion + cold-storage
  * JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED   — VRAM-warming probes

Pin sections:
  §1  Defaults are True when env unset
  §2  Empty-string env reads as default-True (unset marker)
  §3  Whitespace-only env reads as default-True
  §4  Each ``"false"``-class override returns False
  §5  Full-revert matrix (one-off flip doesn't cross-couple)
  §6  Garbage / unknown values revert to False (strict opt-in)
  §7  Source-level pin: each function has the graduated branch
  §8  Public API surface — graduated readers callable from module
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.dw_heavy_probe import (
    heavy_probe_enabled,
)
from backend.core.ouroboros.governance.dw_ttft_observer import (
    tracking_enabled,
    ttft_demotion_enabled,
)
from backend.core.ouroboros.governance.full_jitter import (
    full_jitter_enabled,
)


# ---------------------------------------------------------------------------
# Centralized graduated-flag list — single source of truth for the suite
# ---------------------------------------------------------------------------


_GRADUATED_FLAGS = [
    ("JARVIS_TOPOLOGY_FULL_JITTER_ENABLED", full_jitter_enabled),
    ("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", tracking_enabled),
    ("JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED", ttft_demotion_enabled),
    ("JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", heavy_probe_enabled),
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
        f"Slice E graduation: {env_name} must default True"
    )


# ===========================================================================
# §2 — Empty string is the unset marker
# ===========================================================================


@pytest.mark.parametrize("env_name,reader", _GRADUATED_FLAGS)
def test_empty_string_reads_as_default_true(
    env_name: str, reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setenv("", "")`` matches delenv. Operators commonly clear via
    shell ``export FOO=`` (which sets to empty string). Pre-graduation
    "" was a falsy value; post-graduation it's the unset marker."""
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
    (e.g., a refactor that gates the heavy probe behind the TTFT flag
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
        f"string branch (Slice E pin)"
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
    grepping for "default ``true`` —" find the four Slice E flags."""
    src = inspect.getsource(reader)
    # Either the docstring explicitly says "default ``true``" OR
    # references the graduation slice. Allow both since style varies.
    has_doc = (
        "default ``true``" in src
        or "graduated in Phase 12.2 Slice E" in src
    )
    assert has_doc, (
        f"{reader.__name__} docstring must document the graduated "
        f"default (Slice E)"
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


def test_full_jitter_helper_unaffected_by_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``full_jitter_backoff_s`` is a pure function — flag governs
    whether CALLERS use it, not whether the helper itself works.
    Pin: helper still computes a valid uniform delay regardless of
    flag state."""
    from backend.core.ouroboros.governance.full_jitter import (
        full_jitter_backoff_s,
    )
    # Flag off
    monkeypatch.setenv("JARVIS_TOPOLOGY_FULL_JITTER_ENABLED", "false")
    delay = full_jitter_backoff_s(2, base_s=10, cap_s=300)
    assert 0 <= delay <= 40  # min(300, 10*2^2) = 40
    # Flag on (or default)
    monkeypatch.delenv("JARVIS_TOPOLOGY_FULL_JITTER_ENABLED", raising=False)
    delay = full_jitter_backoff_s(2, base_s=10, cap_s=300)
    assert 0 <= delay <= 40
