"""The boot panel announced a tool-round ceiling the engine did not use.

Found by sweeping for environment variables read with CONFLICTING defaults —
the same class as the cost ceiling written twice, and the same shape as every
other defect this session: a surface asserting a value it did not get from the
authority.

    engine   governed_loop_service.py   default 15
    display  presentation_restraint.py  default 10

With the variable unset — the DEFAULT case, so the common one — the panel told
the operator the safety ceiling was 10 while the loop allowed 15.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.governance.governed_loop_service import (  # noqa: E402
    MAX_TOOL_ROUNDS_ENV,
    configured_max_tool_rounds,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(MAX_TOOL_ROUNDS_ENV, raising=False)


def test_the_unset_default_is_the_engines(monkeypatch) -> None:
    """15, not 10. The disagreement lived entirely in the default."""
    assert configured_max_tool_rounds() == 15


def test_an_operator_value_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv(MAX_TOOL_ROUNDS_ENV, "7")
    assert configured_max_tool_rounds() == 7


@pytest.mark.parametrize("bad", ["abc", "", "  ", "0", "-3", "1.5"])
def test_malformed_values_fall_back_rather_than_crash(monkeypatch, bad) -> None:
    """A tool loop must not fail to start because a knob was fat-fingered, and
    a ceiling of zero is not a ceiling."""
    monkeypatch.setenv(MAX_TOOL_ROUNDS_ENV, bad)
    assert configured_max_tool_rounds() == 15


def test_the_engine_builds_its_config_from_the_resolver() -> None:
    """Structural. If the engine kept its own inline int(), the two could drift
    apart again silently — which is exactly how this started."""
    import inspect
    from backend.core.ouroboros.governance import governed_loop_service

    src = inspect.getsource(governed_loop_service)
    assert "max_tool_rounds=configured_max_tool_rounds()" in src
    # The literal default must exist in exactly one place: the resolver.
    assert src.count(f'"{MAX_TOOL_ROUNDS_ENV}"') == 1, (
        "the variable is resolved in more than one place again"
    )


def test_the_display_reads_the_authority_and_does_not_re_derive() -> None:
    """The panel must not resolve the variable itself. A display that
    re-derives a limit is a second authority, and the operator only ever sees
    the second one."""
    import inspect
    from backend.core.ouroboros.battle_test import presentation_restraint

    src = inspect.getsource(presentation_restraint)
    assert "configured_max_tool_rounds" in src, "the panel is not reading it"
    assert MAX_TOOL_ROUNDS_ENV not in src, (
        "the panel still resolves the variable independently"
    )


def test_the_panel_omits_the_line_rather_than_guessing() -> None:
    """An unmeasured ceiling is not a ceiling. If the authority cannot be
    imported the line is dropped, never printed with a fallback number."""
    import inspect
    from backend.core.ouroboros.battle_test import presentation_restraint

    src = inspect.getsource(presentation_restraint)
    idx = src.index("configured_max_tool_rounds")
    window = src[idx:idx + 400]
    assert "except Exception" in window and "pass" in window
