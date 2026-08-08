"""The idle breadcrumb must render the state it was handed, not re-assert one.

An operator watched a real boot and read `IDLE · $0.00/$2.50` for two minutes
while four genuine test failures sat queued. The sampler had already worked out
the truth — 17 sensors armed, then 4 signals queued — and every word of it was
discarded at two seams:

  * `format_idle_breadcrumb` opened with the literal ``["IDLE"]`` and took no
    detail argument at all;
  * `_statusline_payload` carried ``phase`` and not ``phase_detail``.

So `IDLE · 17 sensors` could never have reached a screen regardless of whether
intake was wired, and the two possible causes were indistinguishable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.battle_test.presentation_restraint import (  # noqa: E402
    format_idle_breadcrumb,
)
from backend.core.ouroboros.battle_test.status_line import (  # noqa: E402
    StatusSnapshot,
    _format_plain,
    _statusline_payload,
)


# ---------------------------------------------------------------------------
# the breadcrumb
# ---------------------------------------------------------------------------

def test_the_breadcrumb_renders_the_detail_it_is_given() -> None:
    crumb = format_idle_breadcrumb(detail="17 sensors")
    assert crumb.startswith("IDLE · 17 sensors"), crumb


def test_idle_without_detail_is_unchanged() -> None:
    """The historical shape, for every caller that has nothing to add."""
    assert format_idle_breadcrumb(cost_spent=0.04, cost_budget=0.5) == (
        "IDLE · $0.04/$0.50"
    )


def test_detail_precedes_the_other_fields() -> None:
    """Why the organism is idle outranks how much it has spent being idle."""
    crumb = format_idle_breadcrumb(
        detail="17 sensors", cost_spent=0.0, cost_budget=2.5, op_id="abc-sig",
    )
    assert crumb.index("17 sensors") < crumb.index("$0.00")


@pytest.mark.parametrize("bad", [None, 0, [], {}])
def test_a_non_string_detail_is_ignored_not_rendered(bad) -> None:
    assert format_idle_breadcrumb(detail=bad) == "IDLE"   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the seam that dropped it
# ---------------------------------------------------------------------------

def test_the_plain_renderer_forwards_phase_detail() -> None:
    """THE regression. `_format_plain` took the breadcrumb path for IDLE and
    called it without the detail, so the sampler's answer was thrown away one
    line after it was computed."""
    out = _format_plain(
        StatusSnapshot(phase="IDLE", phase_detail="17 sensors",
                       cost_spent_usd=0.0, cost_budget_usd=2.5),
        compact=False,
    )
    assert "17 sensors" in out, out


def test_a_queued_phase_is_not_swallowed_by_the_idle_path() -> None:
    """QUEUED is not IDLE and must not be routed through the idle breadcrumb —
    that branch exists to compress the *quiet* state."""
    out = _format_plain(
        StatusSnapshot(phase="QUEUED", phase_detail="4 signals"),
        compact=False,
    )
    assert "QUEUED" in out
    assert "4 signals" in out


# ---------------------------------------------------------------------------
# the payload that dropped it
# ---------------------------------------------------------------------------

def test_the_payload_carries_the_qualifier_with_the_phase() -> None:
    """"IDLE" and "IDLE, 17 sensors armed" are different claims. A payload
    carrying only the first forces every downstream surface to render the
    ambiguous one."""
    payload = json.loads(_statusline_payload(
        StatusSnapshot(phase="IDLE", phase_detail="17 sensors"),
    ))
    assert payload.get("phase") == "IDLE"
    assert payload.get("phase_detail") == "17 sensors"


def test_the_payload_omits_an_empty_qualifier() -> None:
    """Absent, not empty-string: a consumer testing truthiness and one testing
    presence must reach the same conclusion."""
    payload = json.loads(_statusline_payload(StatusSnapshot(phase="IDLE")))
    assert "phase_detail" not in payload


def test_the_payload_still_defaults_phase() -> None:
    payload = json.loads(_statusline_payload(StatusSnapshot(phase="")))
    assert payload.get("phase") == "IDLE"
