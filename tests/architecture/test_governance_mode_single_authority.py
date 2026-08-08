"""One governance mode, one authority — and never the optimistic one.

Found by the conflicting-defaults pin, and the worst instance it reported:

    integration.py:150     "sandbox"   ← the constructor, which validates
    remote_status.py:103   "sandbox"   ← what external observers are TOLD
    health_cortex.py:455   "governed"  ← the health report

With nothing configured, the health cortex claimed the organism was under
governance while the governance stack itself was in sandbox. `"safe"` is an
alias for SANDBOX in `_MODE_MAP`, which settles which direction is
conservative: the health surface was the optimistic one.

Underneath the mismatched defaults was a defect the defaults hid.
``GovernanceConfig.from_env_and_args`` resolves a CLI argument BEFORE the
environment, and both reporters read the environment — so a process started
with ``--governance-mode governed`` was reported as "sandbox" by the status
endpoint, with nothing anywhere disagreeing. Matching the defaults would not
have fixed that; only a queryable authority does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.governance.integration import (  # noqa: E402
    _DEFAULT_GOVERNANCE_MODE,
    _MODE_MAP,
    GOVERNANCE_MODE_ENV,
    configured_governance_mode,
    set_active_governance_mode,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    set_active_governance_mode(None)
    monkeypatch.delenv(GOVERNANCE_MODE_ENV, raising=False)
    yield
    set_active_governance_mode(None)


# ---------------------------------------------------------------------------
# the safety property
# ---------------------------------------------------------------------------

def test_the_fallback_is_the_conservative_mode() -> None:
    """The direction matters more than the value. Over-claiming governance is
    the one error a reader cannot walk back."""
    assert configured_governance_mode() == "sandbox"
    assert _MODE_MAP["safe"] is _MODE_MAP["sandbox"], (
        "'safe' aliasing SANDBOX is what makes sandbox the conservative "
        "fallback — if that changes, this default must be revisited"
    )


def test_an_unknown_value_is_not_repeated_back(monkeypatch) -> None:
    """The constructor REJECTS a value outside the map, so a reporter must not
    present one as live — it would describe a process that cannot exist."""
    monkeypatch.setenv(GOVERNANCE_MODE_ENV, "yolo")
    assert configured_governance_mode() == _DEFAULT_GOVERNANCE_MODE


@pytest.mark.parametrize("value", sorted(_MODE_MAP))
def test_every_valid_mode_round_trips(monkeypatch, value) -> None:
    monkeypatch.setenv(GOVERNANCE_MODE_ENV, value)
    assert configured_governance_mode() == value


def test_case_and_whitespace_do_not_defeat_it(monkeypatch) -> None:
    monkeypatch.setenv(GOVERNANCE_MODE_ENV, "  GOVERNED  ")
    assert configured_governance_mode() == "governed"


# ---------------------------------------------------------------------------
# the defect the defaults were hiding
# ---------------------------------------------------------------------------

def test_a_resolved_config_outranks_the_environment(monkeypatch) -> None:
    """THE reason matching the defaults would not have been enough.

    `from_env_and_args` honours a CLI argument first, so a run started with
    `--governance-mode governed` leaves the env unset. Every reporter read the
    env, so the status endpoint announced "sandbox" for a GOVERNED process.
    """
    monkeypatch.delenv(GOVERNANCE_MODE_ENV, raising=False)
    set_active_governance_mode("governed")
    assert configured_governance_mode() == "governed"


def test_a_published_mode_beats_a_contradicting_environment(monkeypatch) -> None:
    monkeypatch.setenv(GOVERNANCE_MODE_ENV, "sandbox")
    set_active_governance_mode("governed")
    assert configured_governance_mode() == "governed", (
        "the mode a config RESOLVED to outranks the raw environment"
    )


def test_a_published_junk_mode_is_ignored() -> None:
    """Publishing is not a bypass of the vocabulary."""
    set_active_governance_mode("not-a-mode")
    assert configured_governance_mode() == _DEFAULT_GOVERNANCE_MODE


def test_the_constructor_publishes_what_it_resolved() -> None:
    import inspect
    from backend.core.ouroboros.governance import integration

    src = inspect.getsource(integration.GovernanceConfig.from_env_and_args)
    assert "set_active_governance_mode(mode_str)" in src, (
        "the constructor resolves a mode and never publishes it, so every "
        "reporter is back to guessing"
    )


# ---------------------------------------------------------------------------
# the reporters
# ---------------------------------------------------------------------------

def test_the_status_endpoint_reads_the_authority() -> None:
    import inspect
    from backend.core.ouroboros.governance import remote_status

    src = inspect.getsource(remote_status)
    assert "configured_governance_mode" in src
    assert f'"{GOVERNANCE_MODE_ENV}"' not in src, (
        "the status endpoint still resolves the variable itself"
    )


def test_the_health_cortex_reads_the_authority() -> None:
    import inspect
    from backend.core.ouroboros.consciousness import health_cortex

    src = inspect.getsource(health_cortex)
    assert "configured_governance_mode" in src
    assert f'"{GOVERNANCE_MODE_ENV}"' not in src, (
        "the health cortex still resolves the variable itself"
    )


def test_no_reporter_falls_back_to_governed() -> None:
    """Both degrade to sandbox when the authority is unreachable. A surface
    that guesses "governed" while unable to reach governance is asserting the
    single most misleading thing it could."""
    import inspect
    from backend.core.ouroboros.consciousness import health_cortex
    from backend.core.ouroboros.governance import remote_status

    for module in (remote_status, health_cortex):
        src = inspect.getsource(module)
        idx = src.index("configured_governance_mode")
        window = src[idx:idx + 500]
        assert '"governed"' not in window, (
            f"{module.__name__} falls back to the optimistic mode"
        )
        assert '"sandbox"' in window, (
            f"{module.__name__} has no conservative fallback"
        )


def test_the_status_endpoint_reports_a_published_mode(monkeypatch) -> None:
    """End to end on the value an external observer actually receives."""
    from backend.core.ouroboros.governance.remote_status import _governance_mode

    monkeypatch.delenv(GOVERNANCE_MODE_ENV, raising=False)
    set_active_governance_mode("governed")
    assert _governance_mode() == "governed"
    set_active_governance_mode(None)
    assert _governance_mode() == "sandbox"
