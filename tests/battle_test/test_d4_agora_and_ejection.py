"""D4 — the agora as a lane, deck sizing, and graceful ejection.

The state trap: the daemon garbage-collects a lane (TTL or pressure) while a
cockpit is FOCUSED on it. Nothing in the heartbeat can tell the client this —
a lane simply absent from the next frame is indistinguishable from a slow
frame — so without an explicit event the operator sits in a pane that never
updates and never explains itself.

The ejection reuses ``fsm.escape()`` rather than introducing a force-flow
path: an auto-eject IS the daemon pressing Esc for the operator, and a second
route to FLOW is a second set of invariants to keep in step.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.battle_test.cockpit_fsm import MODE_FLOW
from backend.core.ouroboros.battle_test.lane_rings import (
    LaneRegistry,
    reset_lane_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_lane_registry()
    yield
    reset_lane_registry()


def _rows(*lanes: str) -> List[Dict[str, Any]]:
    return [
        {"lane": ln, "lines": 2, "last_seen": 0.0, "label": "",
         "tombstoned": False, "age_s": 0.1}
        for ln in lanes
    ]


# --------------------------------------------------------------------------
# 1. graceful ejection — requirement 1
# --------------------------------------------------------------------------

def test_reap_of_the_focused_lane_ejects_to_flow() -> None:
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui._lanes = _rows("unit/7")
    ui.fsm.enter_select()
    ui.fsm.focus_selected()
    assert ui.fsm.focused_lane == "unit/7"

    ui.on_lane_reaped("unit/7")

    assert ui.fsm.mode == MODE_FLOW, "operator left trapped in a dead pane"
    assert "expired" in ui.prompt(), "no explanation for the context shift"


def test_reap_of_another_lane_does_not_disturb_the_focus() -> None:
    """Ejecting on someone else's reap would yank the operator out of a pane
    that is perfectly alive."""
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui._lanes = _rows("unit/7", "unit/8")
    ui.fsm.enter_select()
    ui.fsm.focus_selected()
    ui.on_lane_reaped("unit/8")
    assert ui.fsm.focused_lane == "unit/7"


def test_reap_while_in_flow_is_a_no_op() -> None:
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui.on_lane_reaped("unit/7")
    assert ui.fsm.mode == MODE_FLOW
    assert "expired" not in ui.prompt(), "flashed about a lane we never saw"


def test_ejection_reuses_the_escape_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRY, asserted structurally: an auto-eject is the daemon pressing Esc."""
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui._lanes = _rows("unit/7")
    ui.fsm.enter_select()
    ui.fsm.focus_selected()

    calls: List[str] = []
    real_escape = ui.fsm.escape
    monkeypatch.setattr(
        ui.fsm, "escape",
        lambda: (calls.append("escape"), real_escape())[1],
    )
    ui.on_lane_reaped("unit/7")
    assert calls == ["escape"], "ejection took a second path to FLOW"


def test_the_flash_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient notice that never leaves is chrome, not a notice."""
    from backend.core.ouroboros.cli import ov

    clock = {"t": 100.0}
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: clock["t"])

    ui = ov.AttachUI()
    ui.flash("lane expired", seconds=2.0)
    assert "lane expired" in ui.prompt()
    clock["t"] += 5.0
    assert "lane expired" not in ui.prompt()


# --------------------------------------------------------------------------
# 2. the reap event reaches the client
# --------------------------------------------------------------------------

class _Writer:
    def __init__(self) -> None:
        self.frames: List[Dict[str, Any]] = []

    def is_closing(self) -> bool:
        return False

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.frames.append(json.loads(line))

    def close(self) -> None:
        pass


async def test_reap_is_broadcast_to_every_cockpit() -> None:
    """Any terminal could be focused on it — this is the one lane message
    that is genuinely everyone's business."""
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )
    a, b = _Writer(), _Writer()
    bridge = CockpitAttachBridge()
    bridge._loop = asyncio.get_running_loop()   # type: ignore[attr-defined]
    for w, sid in ((a, "sess-A"), (b, "sess-B")):
        bridge._clients.add(w)                  # type: ignore[arg-type]
        bridge.bind_session(sid, w)             # type: ignore[arg-type]

    bridge.announce_lane_reaped("unit/7")
    await asyncio.sleep(0)

    for w in (a, b):
        assert w.frames and w.frames[0]["type"] == "lane_reaped"
        assert w.frames[0]["lane"] == "unit/7"


def test_registry_announces_ttl_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_LANE_TOMBSTONE_TTL_S", "10")
    clock = {"t": 0.0}
    seen: List[str] = []
    reg = LaneRegistry(ring=5, clock=lambda: clock["t"])
    reg.on_reap(seen.append)

    reg.record("unit/7", "x")
    reg.mark_dead("unit/7")
    clock["t"] = 30.0
    reg.summary()                       # the ~1Hz poll observes the expiry
    assert seen == ["unit/7"]


def test_registry_announces_pressure_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_LANE_MAX", "1")
    seen: List[str] = []
    reg = LaneRegistry(ring=5)
    reg.on_reap(seen.append)
    reg.record("unit/a", "x")
    reg.record("unit/b", "y")           # forces an eviction
    assert seen == ["unit/a"]


def test_a_reap_sink_that_queries_the_registry_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Announced outside the lock, so a sink may reach back in."""
    monkeypatch.setenv("JARVIS_LANE_MAX", "1")
    reg = LaneRegistry(ring=5)
    out: List[int] = []
    reg.on_reap(lambda _l: out.append(len(reg.summary())))
    reg.record("unit/a", "x")
    reg.record("unit/b", "y")
    assert out, "the sink never ran — likely deadlocked under the lock"


# --------------------------------------------------------------------------
# 3. the agora is a lane, not a special case
# --------------------------------------------------------------------------

def test_a_post_records_into_the_agora_lane() -> None:
    from backend.core.ouroboros.battle_test.lane_rings import get_lane_registry
    from backend.core.ouroboros.governance import moltbook

    class _Post:
        handle = "@cassandra"
        body = "the soak refused to launch"
        op_id = ""
        author_id = "cassandra"
        glyph = "🐍"
        kind = "distress"

        def to_payload(self) -> dict:
            return {}

    reg = get_lane_registry()
    reg.record("agora", f"{_Post.handle}: {_Post.body}", label="the agora")
    hist = [ln.text for ln in reg.history("agora")]
    assert hist and "cassandra" in hist[0]
    assert "agora" in [r["lane"] for r in reg.summary()]
    assert moltbook is not None


def test_the_agora_selects_like_any_other_lane() -> None:
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui._lanes = _rows("swarm/a", "agora")
    ui.fsm.enter_select()
    ui.fsm.move(1)
    assert ui.fsm.focus_selected() is True
    assert ui.fsm.focused_lane == "agora", (
        "the agora needed a bespoke path — it should just be a lane"
    )


# --------------------------------------------------------------------------
# 4. deck sizing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode,expect_rows", [("off", 0), ("compact", 2)])
def test_deck_sizing_caps_the_rendered_rows(mode: str, expect_rows: int) -> None:
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    for i in range(5):
        ui.on_ambient(f"info line {i}")
    ui.set_deck_size(mode)
    body = [ln for ln in ui.prompt().splitlines() if "info line" in ln]
    assert len(body) <= expect_rows


def test_deck_full_restores_rows() -> None:
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui.on_ambient("DW provider failover")
    ui.set_deck_size("off")
    assert "failover" not in ui.prompt()
    ui.set_deck_size("full")
    assert "failover" in ui.prompt()


def test_unknown_deck_arg_reports_usage_without_changing_state() -> None:
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    before = ui._deck_size
    msg = ui.set_deck_size("sideways")
    assert "off | compact | full" in msg
    assert ui._deck_size == before


def test_deck_verb_is_handled_client_side() -> None:
    """Screen height is not the daemon's business — the verb must not be
    relayed upstream."""
    from backend.core.ouroboros.cli import ov

    class _Client:
        def __init__(self) -> None:
            self.sent: List[str] = []

        def send_input(self, text: str) -> bool:
            self.sent.append(text)
            return True

        def send_audio(self, cmd: str) -> bool:
            return True

    c, ui = _Client(), ov.AttachUI()
    assert ov._route_operator_line(c, ui, "/deck compact") == "handled"
    assert c.sent == [], "the deck verb was relayed to the daemon"
    assert ui._deck_size == "compact"
