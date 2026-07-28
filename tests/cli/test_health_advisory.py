"""One line, above the prompt, when something is wrong.

`ov doctor` proves the whole chain and prints a table — the right tool for
"what exactly is broken", the wrong one for "is anything broken", because it
only answers when asked, and the operator asks after they have already lost
time to a symptom they could not explain.
"""
from __future__ import annotations

import contextlib
import io

import pytest

from backend.core.ouroboros.cli.health_advisory import (
    HealthAdvisor, advisories_from_hydration,
)

with contextlib.redirect_stderr(io.StringIO()):
    from backend.core.ouroboros.cli.ov import AttachUI

_HEALTHY = {
    "audio": {"state": "OFFLINE"}, "lanes": {}, "fabrics": {},
    "providers": {}, "session": "s-1",
}


def test_an_ORDINARY_session_says_nothing() -> None:
    """Measured against a real frame, the doctor reports DEGRADED for
    perfectly ordinary conditions — "no fabrics block (older daemon?)", "no
    liquidity data" — because its TABLE has room to explain them and this
    line does not. Surfacing those would warn during a completely healthy
    session, and a line that always complains is one nobody reads. Then it
    fails exactly when it finally has something true to say."""
    advisor = HealthAdvisor()
    advisor.observe_hydration(_HEALTHY)
    assert advisor.render() == ""


def test_degraded_can_be_opted_into(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_HEALTH_ADVISORY_MIN", "degraded")
    assert advisories_from_hydration(_HEALTHY), "the noisier reading is gone"


def test_a_severed_edge_surfaces_with_its_remedy() -> None:
    """`✘ hydration severed` alone leaves the operator where they started."""
    advisor = HealthAdvisor()
    advisor.observe_hydration(None)
    line = advisor.render()
    assert line.startswith("✘")
    assert "hydration" in line
    assert "ov doctor" in line, "an advisory with no verb is a complaint"


def test_it_is_a_PROJECTION_of_the_doctors_own_verdicts() -> None:
    """Two health surfaces that can contradict each other are worse than one:
    the operator then has to decide which to believe."""
    import inspect

    from backend.core.ouroboros.cli import health_advisory

    src = inspect.getsource(health_advisory)
    assert "_verdicts_from_hydration" in src
    derived = advisories_from_hydration(None)
    assert derived and derived[0][1].endswith("hydration")


def test_optional_surfaces_are_never_reported() -> None:
    """ABSENT is not a fault. Reporting it trains the operator to ignore the
    line, which costs them the one time it matters."""
    rows = advisories_from_hydration(_HEALTHY)
    assert all(rank > 0 for rank, _e, _d in rows)


def test_the_worst_advisory_wins_and_the_rest_are_counted() -> None:
    """There is room for one. A line that cycles is read as noise."""
    advisor = HealthAdvisor()
    advisor._rows = [(1, "4 fabrics", "impaired"), (2, "3 hydration", "gone")]
    advisor._rows.sort(key=lambda r: -r[0])
    line = advisor.render()
    assert line.startswith("✘") and "hydration" in line
    assert "+1" in line


def test_a_degraded_edge_uses_a_softer_glyph() -> None:
    advisor = HealthAdvisor()
    advisor._rows = [(1, "4 fabrics", "impaired")]
    assert advisor.render().startswith("▲")


def test_severe_still_surfaces_by_default() -> None:
    """Raising the floor must not silence the thing the line exists for."""
    assert advisories_from_hydration(None), "a severed chain went unreported"


def test_clipping_keeps_the_remedy() -> None:
    """An advisory that loses its verb is a complaint the operator cannot
    act on."""
    advisor = HealthAdvisor()
    advisor._rows = [(2, "3 hydration", "x" * 400)]
    line = advisor.render(width=60)
    assert "ov doctor" in line
    assert len(line) <= 70


def test_muting_acknowledges_without_pretending_it_is_fixed() -> None:
    advisor = HealthAdvisor()
    advisor.observe_hydration(None)
    assert advisor.render() != ""
    advisor.mute()
    assert advisor.render() == ""
    assert advisor.healthy is False, "muted is not healthy"


def test_a_NEW_fault_un_mutes() -> None:
    """They dismissed the previous one, not this one."""
    advisor = HealthAdvisor()
    advisor.observe_hydration(None)
    advisor.mute()
    advisor._rows = []
    advisor.observe_hydration({"lanes": {}})
    if advisor.count:
        assert advisor.render() != ""


def test_it_reports_a_CHANGE_so_the_line_does_not_flicker() -> None:
    """Stable text redrawn at frame rate is how a status line becomes a
    flicker."""
    advisor = HealthAdvisor()
    assert advisor.observe_hydration(None) is True
    assert advisor.observe_hydration(None) is False


def test_the_advisory_reaches_the_status_line() -> None:
    ui = AttachUI()
    ui.advisor.observe_hydration(None)
    assert ui.advisor.render() in ui._key_hints()


@pytest.mark.parametrize("junk", [None, {}, "string", 42, []])
def test_junk_cannot_break_the_status_line(junk) -> None:
    ui = AttachUI()
    ui.advisor.observe_hydration(junk)
    assert isinstance(ui.advisor.render(), str)
    assert isinstance(ui._key_hints(), str)


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_HEALTH_ADVISORY_ENABLED", "0")
    assert advisories_from_hydration(None) == []
