"""Nine virtual screens named "JARVIS GHOST", and the inference that made them.

Measured on the developer machine before this module existed: nine BetterDisplay
virtual screens, tagIDs 153 through 201, all named "JARVIS GHOST". The design
doc records a historical high-water mark of ~150.

The cause was not a missing check. There WAS a check —
`ensure_ghost_display_exists_async` ran `system_profiler SPDisplaysDataType` and
searched for the name. It is that `system_profiler` cannot see BetterDisplay
virtual screens, so the check answered "absent" with nine of them on the
machine, and the next step created a tenth.

The mandated scenario is `test_it_refuses_to_create_when_the_count_is_unknown`.
Everything else here is detail; that one test is the defect.

THE TESTS TO KEEP
-------------------
`test_a_defined_but_disconnected_ghost_is_reconnected_not_recreated`. This is
the loop that produced nine. `DisplayPressureController` deliberately
disconnects the ghost under memory pressure, so "is it attached?" is the wrong
question — a probe that asks it reports absent for a display the system itself
just detached, forever.

`test_it_detaches_before_discarding`. Learned the expensive way on live
hardware: discarding eight CONNECTED virtual screens removed the definitions and
left all eight framebuffers attached, addressable by nothing, surviving until
BetterDisplay was quit.

`test_it_never_discards_without_an_identifier`. BetterDisplay's own help says an
unidentified discard removes every discardable device with no undo.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.system import ghost_display_reconciler as gdr
from backend.system.ghost_display_reconciler import (
    GhostDisplay,
    GhostDisplayReconciler,
    GhostInventory,
    Presence,
    name_variants,
    parse_identifiers,
    parse_known_display_ids,
)


def _device(tag: str, display_id: str, name: str = "JARVIS GHOST",
            dtype: str = "VirtualScreen") -> Dict[str, Any]:
    d = {"UUID": f"uuid-{tag}", "deviceType": dtype, "displayID": display_id,
         "name": name, "originalName": name, "tagID": tag}
    if dtype == "VirtualScreen":
        d["tagID (VirtualScreen)"] = tag
        d["tagID (Display)"] = str(int(tag) + 50)
    return d


def _identifiers(*devices: Dict[str, Any]) -> str:
    """BetterDisplay's real shape: comma-separated objects, NO outer brackets."""
    return ",".join(json.dumps(d, indent=2) for d in devices)


#: The exact fixture the developer machine presented: nine ghosts plus the
#: built-in, tagIDs ascending in creation order.
_NINE_GHOSTS = _identifiers(
    _device("2", "1", "Built-in Display", "Display"),
    *(_device(t, str(i + 13))
      for i, t in enumerate(["153", "157", "160", "166", "170", "180", "186",
                             "192", "201"])),
)


class _FakeCli:
    """Records every invocation and replays scripted answers."""

    def __init__(self, responses: Optional[Dict[str, Tuple[int, str]]] = None,
                 default: Tuple[int, str] = (0, "")) -> None:
        self.calls: List[Tuple[str, ...]] = []
        self.responses = responses or {}
        self.default = default

    async def __call__(self, *args: str) -> Tuple[int, str]:
        self.calls.append(tuple(args))
        for key, resp in self.responses.items():
            if key in " ".join(args):
                return resp
        return self.default

    def calls_matching(self, needle: str) -> List[Tuple[str, ...]]:
        return [c for c in self.calls if needle in " ".join(c)]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JARVIS_GHOST_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_GHOST_TARGET_COUNT", "1")
    monkeypatch.setenv("JARVIS_GHOST_DISCARD_SURPLUS", "true")
    monkeypatch.setenv("JARVIS_GHOST_MAX_DISCARDS", "16")
    # CoreGraphics is real hardware; pin it so the suite is deterministic.
    monkeypatch.setattr(gdr, "online_display_ids", lambda: None)
    monkeypatch.setattr(gdr, "online_display_count", lambda: None)
    yield


def _rec(cli: _FakeCli) -> GhostDisplayReconciler:
    return GhostDisplayReconciler("JARVIS_GHOST", run_cli=cli)


class TestTheMandatedScenario:
    async def test_it_refuses_to_create_when_the_count_is_unknown(self):
        """THE defect. Silence is not absence, and absence is what creates.

        BetterDisplay answers with prose on stdout and a ZERO exit code when it
        cannot serve a request, so a caller reading only the return code sees
        success and an empty display list — which is indistinguishable from
        "there are none" unless the module refuses to make that inference.
        """
        cli = _FakeCli(default=(0, "Failed. Request timed out. Host app might "
                                   "not be running or is not accepting "
                                   "notifications."))
        rec = _rec(cli)

        inv = await rec.probe()

        assert inv.presence == Presence.UNKNOWN.value
        assert inv.certain is False
        assert inv.defined == 0        # ...but that ZERO must not authorise

        report = await rec.reconcile()
        assert "UNKNOWN" in report["refused"]
        assert report["create_needed"] is False, (
            "an unmeasured count authorised a create — this is the bug that "
            "produced nine displays")
        assert cli.calls_matching("discard") == []
        assert cli.calls_matching("create") == []


class TestTheProbe:
    async def test_it_finds_all_nine_and_excludes_the_built_in(self):
        rec = _rec(_FakeCli(default=(0, _NINE_GHOSTS)))

        inv = await rec.probe()

        assert inv.presence == Presence.PRESENT.value
        assert inv.defined == 9
        assert inv.surplus == 8
        assert all(d.name == "JARVIS GHOST" for d in inv.displays)
        assert "1" not in {d.display_id for d in inv.displays}

    async def test_a_measured_empty_list_is_ABSENT_not_unknown(self):
        rec = _rec(_FakeCli(default=(0, _identifiers(
            _device("2", "1", "Built-in Display", "Display")))))

        inv = await rec.probe()

        assert inv.presence == Presence.ABSENT.value
        assert inv.certain is True      # a measured zero MAY authorise a create

    async def test_no_cli_runner_is_unknown_not_absent(self):
        inv = await GhostDisplayReconciler("JARVIS_GHOST").probe()
        assert inv.presence == Presence.UNKNOWN.value

    async def test_a_nonzero_return_code_is_unknown(self):
        rec = _rec(_FakeCli(default=(1, "")))
        assert (await rec.probe()).presence == Presence.UNKNOWN.value

    async def test_disabled_is_unknown_not_a_free_pass(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GHOST_RECONCILE_ENABLED", "0")
        rec = _rec(_FakeCli(default=(0, _NINE_GHOSTS)))
        inv = await rec.probe()
        assert inv.presence == Presence.UNKNOWN.value

    async def test_garbage_output_is_unknown_rather_than_empty(self):
        rec = _rec(_FakeCli(default=(0, "<html>not json at all</html>")))
        inv = await rec.probe()
        # Parses to nothing, but the CLI did answer — ABSENT is honest here,
        # and what matters is that it NEVER reads as PRESENT.
        assert inv.presence in (Presence.ABSENT.value, Presence.UNKNOWN.value)
        assert inv.defined == 0


class TestConvergence:
    async def test_it_discards_the_surplus_and_keeps_the_oldest(self):
        cli = _FakeCli(default=(0, _NINE_GHOSTS))
        rec = _rec(cli)

        report = await rec.reconcile()

        assert len(report["discarded"]) == 8
        # 153 is the oldest and survives; the newest go first.
        assert "153" not in report["discarded"]
        assert report["discarded"][0] == "201"
        assert set(report["discarded"]) == {
            "157", "160", "166", "170", "180", "186", "192", "201"}

    async def test_it_detaches_before_discarding(self):
        """Discarding a CONNECTED screen orphans its framebuffer.

        Measured on live hardware: eight definitions removed, eight framebuffers
        still attached, BetterDisplay reporting one virtual screen while
        CoreGraphics reported ten displays. Nothing could address them.
        """
        cli = _FakeCli(responses={"-connected": (0, "on")},
                       default=(0, _NINE_GHOSTS))
        rec = _rec(cli)

        await rec.reconcile()

        for tag in ("201", "192", "157"):
            seq = [" ".join(c) for c in cli.calls if tag in " ".join(c)]
            off = next(i for i, c in enumerate(seq) if "connected=off" in c)
            dis = next(i for i, c in enumerate(seq) if "discard" in c)
            assert off < dis, f"tagID {tag} was discarded while still attached"

    async def test_it_never_discards_without_an_identifier(self):
        """An unidentified discard removes EVERY virtual screen, with no undo."""
        cli = _FakeCli(default=(0, _NINE_GHOSTS))
        rec = _rec(cli)

        await rec.reconcile()

        for call in cli.calls_matching("discard"):
            assert any(a.startswith("-tagID=") and len(a) > len("-tagID=")
                       for a in call), f"discard without an identifier: {call}"

    async def test_an_empty_tag_is_refused_outright(self):
        cli = _FakeCli()
        rec = _rec(cli)
        assert await rec._discard(GhostDisplay(tag_id="")) is False
        assert cli.calls_matching("discard") == []

    async def test_a_defined_but_disconnected_ghost_is_reconnected_not_recreated(self):
        """THE loop. The pressure controller detaches; the probe must not
        conclude the display is gone and ask for a replacement."""
        one = _identifiers(_device("153", "13"))
        cli = _FakeCli(responses={"-connected": (0, "off")}, default=(0, one))
        rec = _rec(cli)

        report = await rec.reconcile()

        assert report["reconnected"] == ["153"]
        assert report["create_needed"] is False
        assert cli.calls_matching("create") == []
        assert cli.calls_matching("connected=on")

    async def test_a_connected_single_ghost_is_left_completely_alone(self):
        one = _identifiers(_device("153", "13"))
        cli = _FakeCli(responses={"-connected": (0, "on")}, default=(0, one))

        report = await _rec(cli).reconcile()

        assert report["acted"] is False
        assert report["discarded"] == [] and report["reconnected"] == []
        assert not cli.calls_matching("discard")

    async def test_a_measured_zero_reports_that_a_create_is_needed(self):
        cli = _FakeCli(default=(0, _identifiers(
            _device("2", "1", "Built-in Display", "Display"))))

        report = await _rec(cli).reconcile()

        assert report["create_needed"] is True
        assert report["create_count"] == 1
        # ...and the reconciler does NOT create. Aspect/resolution/registration
        # are the manager's policy; this module only does the arithmetic.
        assert cli.calls_matching("create") == []

    async def test_discard_can_be_disabled_without_losing_the_probe(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GHOST_DISCARD_SURPLUS", "0")
        cli = _FakeCli(default=(0, _NINE_GHOSTS))

        report = await _rec(cli).reconcile()

        assert report["discarded"] == []
        assert cli.calls_matching("discard") == []
        assert report["inventory"]["surplus"] == 8   # still MEASURED

    async def test_the_per_sweep_cap_is_honoured_and_announced(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_GHOST_MAX_DISCARDS", "3")
        cli = _FakeCli(default=(0, _NINE_GHOSTS))

        report = await _rec(cli).reconcile()

        assert len(report["discarded"]) == 3
        assert any("cap" in r.message.lower() or "cap" in str(r.args).lower()
                   for r in caplog.records), "a truncated sweep must say so"

    async def test_a_higher_target_keeps_more(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GHOST_TARGET_COUNT", "3")
        cli = _FakeCli(default=(0, _NINE_GHOSTS))

        report = await _rec(cli).reconcile()

        # Target is a policy, not a constant: 9 - 3 = 6 removed.
        assert len(report["discarded"]) == 6

    async def test_reconcile_is_idempotent(self):
        one = _identifiers(_device("153", "13"))
        cli = _FakeCli(responses={"-connected": (0, "on")}, default=(0, one))
        rec = _rec(cli)

        first = await rec.reconcile()
        second = await rec.reconcile()

        assert first["acted"] is False and second["acted"] is False


class TestSurvivorChoice:
    def test_a_connected_ghost_beats_an_older_detached_one(self):
        """Killing the attached one would strand whatever windows live there."""
        inv = GhostInventory(displays=[
            GhostDisplay(tag_id="100", connected=False),
            GhostDisplay(tag_id="200", connected=True),
        ])
        assert inv.survivor().tag_id == "200"

    def test_otherwise_the_oldest_wins(self):
        """GhostPersistenceManager may hold window state referencing it."""
        inv = GhostInventory(displays=[
            GhostDisplay(tag_id="200", connected=False),
            GhostDisplay(tag_id="100", connected=False),
        ])
        assert inv.survivor().tag_id == "100"

    def test_an_unparseable_tag_sorts_last_rather_than_crashing(self):
        inv = GhostInventory(displays=[
            GhostDisplay(tag_id="oops"), GhostDisplay(tag_id="7")])
        assert inv.survivor().tag_id == "7"

    def test_no_displays_has_no_survivor(self):
        assert GhostInventory().survivor() is None


class TestOrphanedFramebuffers:
    """Attached displays BetterDisplay does not own — invisible until measured."""

    async def test_it_names_them_by_display_id(self, monkeypatch):
        monkeypatch.setattr(gdr, "online_display_ids", lambda: {1, 13, 14, 15})
        cli = _FakeCli(default=(0, _identifiers(
            _device("2", "1", "Built-in Display", "Display"),
            _device("153", "13"))))

        inv = await _rec(cli).probe()

        assert inv.orphans == 2
        assert inv.orphan_display_ids == [14, 15]

    async def test_a_real_external_monitor_is_not_an_orphan(self, monkeypatch):
        """Set difference on displayIDs, not a count comparison."""
        monkeypatch.setattr(gdr, "online_display_ids", lambda: {1, 5, 13})
        cli = _FakeCli(default=(0, _identifiers(
            _device("2", "1", "Built-in Display", "Display"),
            _device("9", "5", "LG UltraFine", "Display"),
            _device("153", "13"))))

        inv = await _rec(cli).probe()

        assert inv.orphans == 0

    async def test_an_unreadable_side_reports_no_orphans_rather_than_all(
            self, monkeypatch):
        """Failing one probe must not indict every display on the machine."""
        monkeypatch.setattr(gdr, "online_display_ids", lambda: None)
        cli = _FakeCli(default=(0, _NINE_GHOSTS))

        inv = await _rec(cli).probe()

        assert inv.orphan_display_ids == []


class TestParsing:
    def test_it_handles_bracketless_comma_separated_objects(self):
        """BetterDisplay's actual output shape."""
        assert len(parse_identifiers(_NINE_GHOSTS, "JARVIS_GHOST")) == 9

    def test_it_prefers_the_virtualscreen_tag_over_the_display_tag(self):
        """Discarding by the Display tag addresses the wrong object."""
        raw = _identifiers(_device("153", "13"))
        assert parse_identifiers(raw, "JARVIS_GHOST")[0].tag_id == "153"

    def test_it_matches_underscores_against_spaces(self):
        """Configured as JARVIS_GHOST; BetterDisplay stores JARVIS GHOST."""
        assert "jarvis ghost" in name_variants("JARVIS_GHOST")
        raw = _identifiers(_device("1", "13", "JARVIS GHOST"))
        assert len(parse_identifiers(raw, "JARVIS_GHOST")) == 1

    def test_it_ignores_virtual_screens_that_are_not_ours(self):
        raw = _identifiers(_device("1", "13", "Someone Elses Screen"))
        assert parse_identifiers(raw, "JARVIS_GHOST") == []

    def test_it_ignores_a_real_display_that_shares_our_name(self):
        """Name alone could reach a monitor an operator labelled similarly."""
        raw = _identifiers(_device("1", "13", "JARVIS GHOST", "Display"))
        assert parse_identifiers(raw, "JARVIS_GHOST") == []

    def test_malformed_input_yields_nothing_rather_than_raising(self):
        for bad in ("", "   ", "{{{", "null", "[1,2,3]"):
            assert parse_identifiers(bad, "JARVIS_GHOST") == []

    def test_known_display_ids_covers_every_device_type(self):
        ids = parse_known_display_ids(_identifiers(
            _device("2", "1", "Built-in Display", "Display"),
            _device("153", "13")))
        assert ids == {1, 13}

    def test_known_display_ids_is_none_when_unreadable(self):
        assert parse_known_display_ids("{{{") is None
        assert parse_known_display_ids("") is None


class TestFailureDetection:
    def test_prose_failure_with_a_zero_exit_code_is_a_failure(self):
        """The trap: BetterDisplay reports refusal on stdout and returns 0."""
        assert gdr._cli_failed("Failed. Request timed out.") is True
        assert gdr._cli_failed("Host app might not be running") is True
        assert gdr._cli_failed("on") is False
        assert gdr._cli_failed("") is False


class TestKnobs:
    def test_the_target_is_clamped(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GHOST_TARGET_COUNT", "9999")
        assert gdr.target_ghost_count() == 4
        monkeypatch.setenv("JARVIS_GHOST_TARGET_COUNT", "not-a-number")
        assert gdr.target_ghost_count() == 1

    def test_stats_never_raise(self):
        s = GhostDisplayReconciler("JARVIS_GHOST").stats()
        assert s["target"] == 1 and "discarded" in s
