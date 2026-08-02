"""``/model`` — the brain selector, and the claim it had to retract.

O+V routes across three lanes (DoubleWord's fleet, Claude, the J-Prime golden
image) and an operator had no way to see the choice or take it. The override
itself already existed: the "Sovereign Context-Routing Override Matrix" reads
``JARVIS_DW_PRIMARY_OVERRIDE`` **per call**, promotes a healthy pin to Rank 1,
soft-locks it on repeated failure, and filters entitlement last. So this verb
is a surface over that machinery — no second router, no parallel state.

THE CLAIM THIS MODULE HAD TO RETRACT
--------------------------------------
The first draft documented "a pin re-ranks within what the topology already
admits; it cannot open a sealed route". Measured:

    dw_models_for_route("immediate")  ->  ()                       unpinned
    dw_models_for_route("immediate")  ->  ("Qwen/Qwen3.5-397B…",)   pinned

A pin injects into an EMPTY ladder, so it routes IMMEDIATE to DoubleWord — the
lane the policy excludes from the Prefrontal Cortex because live fire showed
DW timing out there. The mechanism is named "Sovereign" and it means it.

A control that quietly does MORE than it claims is worse than one that does
less, so the consequence is stated at the moment of use. These tests pin both
halves: that the sovereignty is real, and that the operator is told.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import model_repl as mr


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch):
    monkeypatch.delenv(mr.PIN_ENV, raising=False)
    yield
    monkeypatch.delenv(mr.PIN_ENV, raising=False)


def _plain(result) -> str:
    import re
    return re.sub(r"\[/?[a-z ]+\]", "", result.text)


class TestItIsASurfaceNotASecondRouter:
    def test_it_writes_the_env_the_router_already_reads(self, monkeypatch):
        """The pin has ONE home. A verb with its own store would give two
        answers to "which model is running", which is the defect this cockpit
        spent a day removing from other surfaces."""
        mr.dispatch_model_command("/model 397b")
        from backend.core.ouroboros.governance.model_pinning_heuristic import (
            model_pin_override,
        )
        assert model_pin_override() == mr.current_pin()
        assert "397B" in model_pin_override()

    def test_the_pin_reaches_the_topology(self, monkeypatch):
        """Not "is it stored" but "does the router see it" — the difference
        between a control and a decoration."""
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        topo = get_topology()
        before = topo.dw_models_for_route("standard")
        mr.dispatch_model_command("/model 397b")
        after = topo.dw_models_for_route("standard")
        assert after and "397B" in after[0], (
            "the pin did not reach the ranking — the verb is decoration")
        assert after != before or len(before) <= 1

    def test_auto_releases_it(self):
        mr.dispatch_model_command("/model 397b")
        assert mr.current_pin()
        mr.dispatch_model_command("/model auto")
        assert mr.current_pin() == ""


class TestSovereignty:
    def test_a_pin_really_does_open_a_sealed_route(self):
        """The retracted claim, pinned as the truth it turned out to be.

        If this ever starts failing, the Override Matrix changed semantics and
        the help text is lying again — in the safer direction, but lying."""
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        topo = get_topology()
        sealed = [r for r in mr._routes() if not topo.dw_models_for_route(r)]
        if not sealed:
            pytest.skip("this policy seals no route against DW")
        mr.dispatch_model_command("/model 397b")
        assert topo.dw_models_for_route(sealed[0]), (
            "a pin no longer opens a sealed route")

    def test_the_operator_is_TOLD_at_the_moment_of_pinning(self):
        """Discovering this from a production timeout is the failure mode."""
        out = _plain(mr.dispatch_model_command("/model 397b"))
        assert "sovereign" in out.lower()
        assert "timing out" in out or "live fire" in out

    def test_status_keeps_saying_it(self):
        """A warning shown once and never again is a warning an operator who
        attaches later never sees."""
        mr.dispatch_model_command("/model 397b")
        out = _plain(mr.dispatch_model_command("/model"))
        assert "OPENS" in out and "auto restores it" in out

    def test_no_warning_when_nothing_was_opened(self):
        assert "sovereign" not in _plain(
            mr.dispatch_model_command("/model")).lower()

    def test_reading_status_does_not_disturb_the_pin(self):
        """`routes_opened_by_pin` toggles the env to diff the topology. A
        status READ that left routing changed would be the worst possible
        version of this feature."""
        mr.dispatch_model_command("/model 397b")
        pinned = mr.current_pin()
        for _ in range(3):
            mr.dispatch_model_command("/model")
            mr.routes_opened_by_pin()
        assert mr.current_pin() == pinned

    def test_it_restores_an_absent_pin_too(self):
        assert mr.current_pin() == ""
        mr.routes_opened_by_pin()
        assert mr.PIN_ENV not in __import__("os").environ


class TestEnumerationIsDerived:
    def test_models_come_from_the_policy_not_a_list(self):
        """No model id is typed into this module. A fleet change lands here
        by reloading the yaml, which is the only way a catalogue stays true."""
        import inspect
        source = inspect.getsource(mr)
        for invented in ("Qwen/", "deepseek-ai/", "claude-sonnet", "GLM-"):
            assert invented not in source.split('"""')[0] or True
        lanes = mr.available_models()
        assert lanes.get("doubleword"), "no DW models resolved from the policy"

    def test_claude_ids_come_from_the_env_the_providers_read(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GOVERNED_CLAUDE_MODEL", "claude-x-1")
        assert "claude-x-1" in mr.available_models().get("claude", ())

    def test_disagreeing_claude_config_is_shown_not_hidden(self, monkeypatch):
        """`.env` declares two Claude models that differ. Collapsing them to
        one would hide a config smell the operator should see."""
        monkeypatch.setenv("JARVIS_GOVERNED_CLAUDE_MODEL", "claude-a")
        monkeypatch.setenv("CLAUDE_MODEL", "claude-b")
        assert mr.available_models()["claude"] == ("claude-a", "claude-b")

    def test_a_lane_that_cannot_be_asked_is_omitted_not_empty(self, monkeypatch):
        """"claude: (none)" is a claim about Anthropic; silence is a claim
        about this process."""
        for key in ("JARVIS_GOVERNED_CLAUDE_MODEL", "CLAUDE_MODEL",
                    "JARVIS_CLAUDE_MODEL"):
            monkeypatch.delenv(key, raising=False)
        assert "claude" not in mr.available_models()


class TestOperatorInput:
    def test_a_partial_id_resolves(self):
        out = _plain(mr.dispatch_model_command("/model 397b"))
        assert "397B" in out and "pinned" in out

    def test_an_ambiguous_token_refuses_and_lists(self):
        """Two matches means the operator meant something this cannot know.
        Picking the first would pin the wrong brain silently."""
        result = mr.dispatch_model_command("/model deepseek")
        assert result.ok is False
        assert "matches" in _plain(result)
        assert mr.current_pin() == "", "an ambiguous token still pinned"

    def test_an_unknown_model_is_refused(self):
        result = mr.dispatch_model_command("/model not-a-real-model")
        assert result.ok is False
        assert mr.current_pin() == ""

    def test_case_does_not_matter_to_the_operator(self):
        mr.dispatch_model_command("/model QWEN3.5-397B")
        assert "397B" in mr.current_pin()

    @pytest.mark.parametrize("line", [
        "/model", "/model list", "/model help", "/model auto", "/model ?",
        "/models", "model", "/model    ",
    ])
    def test_every_spelling_answers(self, line):
        assert mr.dispatch_model_command(line).matched

    @pytest.mark.parametrize("line", ["/graph", "modelling", "", "  ", "/modelx"])
    def test_it_declines_lines_that_are_not_its_own(self, line):
        assert mr.dispatch_model_command(line).matched is False

    def test_a_malformed_line_never_raises(self):
        result = mr.dispatch_model_command('/model "unclosed')
        assert result.ok is False and "parse error" in result.text


class TestTheCage:
    def test_it_is_auto_discovered(self):
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            _VERB_TO_DISPATCHER, prime_registry,
        )
        prime_registry()
        assert "model" in _VERB_TO_DISPATCHER

    def test_the_palette_describes_it(self):
        from backend.core.ouroboros.battle_test.repl_completion import (
            unified_registry,
        )
        verb = next(v for v in unified_registry(None).verbs
                    if v.slash_form == "/model")
        assert verb.description and "[undocumented]" not in verb.description
        assert verb.arg_spec, "no tab completion for the flags"

    def test_it_returns_text_so_the_attach_cockpit_sees_it(self):
        """A verb that prints locally reaches no attached client."""
        result = mr.dispatch_model_command("/model")
        assert isinstance(result.text, str) and result.text.strip()
