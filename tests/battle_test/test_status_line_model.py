""""which model am I running" — the cockpit could not say, and always knew.

`op_context.py:304` has carried ``model_id: str = ""`` on every generation
result since it was written ("provider model identifier; empty = not
reported"). `_sample_route_and_provider` read ``provider_name`` beside it and
ignored it, `StatusSnapshot` had no field for it, and the badge rendered
``[std·claude]`` — the LANE, not the brain.

Worse than absent: `_statusline_payload` builds a payload whose docstring says
it "mirrors Claude Code's documented shape — `model`, `workspace`, `cost` …
so a script written for CC runs here unchanged", and it set

    payload["model"] = {"id": snap.provider}

So a CC-compatible status-line script asking for ``.model.id`` got
``"claude"`` — the one key in that payload named for a question it did not
answer. A three-tier cascade makes that actively misleading: ``[std·claude]``
cannot distinguish a sonnet fallback from an opus one, and the operator is
billed differently for each.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.battle_test.status_line import (
    StatusSnapshot, _format_badge, _short_model, _statusline_payload,
)


class TestTheSnapshotCarriesTheModel:
    def test_the_field_exists(self):
        snap = StatusSnapshot(provider="claude", model="claude-sonnet-4-6")
        assert snap.model == "claude-sonnet-4-6"

    def test_it_defaults_empty_rather_than_guessing(self):
        """"not reported" is a real state — a pre-GENERATE op has no model,
        and inventing one would be the same lie the payload told."""
        assert StatusSnapshot().model == ""


class TestTheBadgeShowsIt:
    def test_the_model_joins_route_and_provider(self):
        assert _format_badge("standard", "claude", "claude-sonnet-4-6") == \
            "[std·claude·claude-sonnet-4-6]"

    def test_a_vendor_namespace_is_trimmed(self):
        """``Qwen/Qwen3.5-397B-A17B-FP8-dottxt`` costs more width than the
        rest of the line together. Subtractive only — no abbreviation table,
        because a table is a second place to edit every time a model ships."""
        assert _format_badge("background", "dw",
                             "Qwen/Qwen3.5-397B-A17B-FP8-dottxt") == \
            "[bg·dw·Qwen3.5-397B-A17B]"

    def test_a_model_that_merely_repeats_the_lane_is_suppressed(self):
        """`[std·claude·claude]` is noise, and a badge that repeats itself
        trains the eye to skip it."""
        assert _format_badge("standard", "claude", "claude") == "[std·claude]"

    def test_it_stays_empty_when_nothing_is_known(self):
        assert _format_badge("", "", "") == ""

    def test_the_model_alone_is_enough_to_render(self):
        """Provider unknown, model known — the reverse of today's default, and
        the more useful half."""
        assert "sonnet" in _format_badge("", "", "claude-sonnet-4-6")

    def test_existing_two_arg_callers_still_work(self):
        """`model` is additive and last; every call site that predates it
        keeps its exact output."""
        assert _format_badge("standard", "claude") == "[std·claude]"

    @pytest.mark.parametrize("hostile", [
        None, "", "   ", "/", "a" * 300, "Qwen/", "///x",
    ])
    def test_hostile_model_ids_never_raise(self, hostile):
        assert isinstance(_short_model(hostile), str)  # type: ignore[arg-type]
        assert isinstance(_format_badge("std", "dw", hostile), str)  # type: ignore[arg-type]

    def test_a_long_id_is_bounded(self):
        assert len(_short_model("x" * 200)) <= 28


class TestTheCCPayloadStopsLying:
    def test_model_id_is_the_MODEL(self):
        payload = json.loads(_statusline_payload(
            StatusSnapshot(provider="claude", model="claude-sonnet-4-6")))
        assert payload["model"]["id"] == "claude-sonnet-4-6"

    def test_the_provider_is_still_reported_under_its_own_key(self):
        """Nothing is lost — the lane moves to a key named for it, so a
        consumer wanting either gets the one it asked for."""
        payload = json.loads(_statusline_payload(
            StatusSnapshot(provider="claude", model="claude-sonnet-4-6")))
        assert payload["provider"] == "claude"

    def test_provider_alone_still_populates_model_rather_than_omitting_it(self):
        """Degradation, not silence: before GENERATE reports a model id, the
        lane is the best answer available and a CC script reading `.model.id`
        should get it rather than a missing key."""
        payload = json.loads(_statusline_payload(
            StatusSnapshot(provider="claude", model="")))
        assert payload["model"]["id"] == "claude"

    def test_neither_known_omits_the_key_entirely(self):
        """The payload's own rule: "Keys ov cannot answer are OMITTED rather
        than faked"."""
        payload = json.loads(_statusline_payload(StatusSnapshot()))
        assert "model" not in payload

    def test_the_payload_is_valid_json_under_every_shape(self):
        for snap in (StatusSnapshot(),
                     StatusSnapshot(provider="dw"),
                     StatusSnapshot(model="Qwen/Qwen3.5-397B-A17B-FP8"),
                     StatusSnapshot(provider="dw", model="x" * 500)):
            json.loads(_statusline_payload(snap))


class TestTheSamplerReadsIt:
    def test_it_returns_three_values(self):
        """Arity is the contract: every early return had to widen with it, and
        one that did not would raise on unpack the first time an op had no
        context — the path taken on EVERY idle tick."""
        from backend.core.ouroboros.battle_test.status_line import (
            StatusLineBuilder,
        )
        builder = StatusLineBuilder.__new__(StatusLineBuilder)
        builder._gls = None
        assert builder._sample_route_and_provider("op-x") == ("", "", "")

    def test_a_missing_context_also_returns_three(self):
        from backend.core.ouroboros.battle_test.status_line import (
            StatusLineBuilder,
        )
        builder = StatusLineBuilder.__new__(StatusLineBuilder)
        builder._gls = type("G", (), {"_fsm_contexts": {}})()
        assert builder._sample_route_and_provider("op-missing") == ("", "", "")

    def test_it_reads_model_id_off_the_generation(self):
        from backend.core.ouroboros.battle_test.status_line import (
            StatusLineBuilder,
        )
        gen = type("Gen", (), {"provider_name": "Claude",
                               "model_id": "claude-sonnet-4-6"})()
        ctx = type("Ctx", (), {"provider_route": "STANDARD",
                               "generation": gen})()
        builder = StatusLineBuilder.__new__(StatusLineBuilder)
        builder._gls = type("G", (), {"_fsm_contexts": {"op-1": ctx}})()
        route, provider, model = builder._sample_route_and_provider("op-1")
        assert (route, provider) == ("standard", "claude")
        assert model == "claude-sonnet-4-6", "model_id was ignored again"

    def test_the_model_id_keeps_its_case(self):
        """Provider and route are folded for display; a model id is a
        case-carrying identifier (``Qwen/Qwen3.5-397B-A17B-FP8``) and folding
        it would make the badge disagree with the provider's own logs."""
        from backend.core.ouroboros.battle_test.status_line import (
            StatusLineBuilder,
        )
        gen = type("Gen", (), {"provider_name": "DW",
                               "model_id": "Qwen/Qwen3.5-397B-A17B-FP8"})()
        ctx = type("Ctx", (), {"provider_route": "BACKGROUND",
                               "generation": gen})()
        builder = StatusLineBuilder.__new__(StatusLineBuilder)
        builder._gls = type("G", (), {"_fsm_contexts": {"op-1": ctx}})()
        assert builder._sample_route_and_provider("op-1")[2] == \
            "Qwen/Qwen3.5-397B-A17B-FP8"
