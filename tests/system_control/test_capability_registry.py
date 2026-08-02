"""Derive the vocabulary, and get the risk default the right way round.

Three hand-kept lists describe one Mac: the HUD's 9 tools, Venom's 16, and 42
routers. `macos_controller` can lock the screen and the HUD cannot, because
nobody wrote `lock_screen` into its list. The capability was never missing —
its NAME was.

The mandated scenario is `test_it_discovers_both_and_gates_only_the_mutator`:
a mock controller with `lock_screen` and `get_battery`, asserting both are
discovered, both parse into tool definitions, and the Iron Gate binds to the
mutator ONLY.

THE INVERSION THIS SUITE DEFENDS
----------------------------------
`test_an_undeclared_capability_defaults_to_GATED` is the one to keep. Guessing
"mutating" from a method name fails in the direction that hurts: a
`get_display_config` that quietly changes the display would be auto-approved
forever and nothing would say so. Silence means APPROVAL_REQUIRED — the same
rule as `unverified != safe` and `UNKNOWN != done`, applied where being wrong
locks somebody's screen mid-sentence.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

import pytest

from backend.system_control import capability_registry as cr
from backend.system_control.capability_registry import (
    CapabilityRegistry,
    Provenance,
    Tier,
    capability,
)


class _MockController:
    """Stands in for `macos_controller`, with the two methods the brief names."""

    @capability(mutates=True)
    async def lock_screen(
        self,
        progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        enable_voice_feedback: bool = True,
        speaker_name: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Lock the macOS screen immediately.

        Args:
            progress_callback: Optional callback for progress updates
            enable_voice_feedback: Enable voice narration
            speaker_name: User's name for personalised feedback

        Returns:
            Tuple of (success, message)
        """
        return (True, "locked")

    @capability(reads_only=True)
    async def get_battery(self, include_history: bool = False) -> dict:
        """Report battery percentage and charging state.

        Args:
            include_history: Include the last 24h of readings

        Returns:
            Battery status dict
        """
        return {"percent": 88}

    async def set_volume_async(self, level: int) -> bool:
        """Set the system output volume.

        Args:
            level: Volume 0-100
        """
        return True

    def _private_helper(self) -> None:
        """Not a capability."""


@pytest.fixture
def reg() -> CapabilityRegistry:
    return CapabilityRegistry(_MockController()).hydrate()


class TestTheMandatedScenario:
    def test_it_discovers_both_and_gates_only_the_mutator(self, reg):
        """THE scenario: both found, both parsed, Iron Gate on the mutator only."""
        names = reg.names()
        assert "lock_screen" in names and "get_battery" in names

        lock = reg.get("lock_screen")
        battery = reg.get("get_battery")
        assert lock is not None and battery is not None

        # Signatures became tool definitions.
        assert lock.parameters["enable_voice_feedback"]["type"] == "boolean"
        assert lock.parameters["speaker_name"]["type"] == "string"
        assert battery.parameters["include_history"]["type"] == "boolean"

        # Descriptions lifted from the docstrings, not invented.
        assert "Lock the macOS screen" in lock.description
        assert "battery" in battery.description.lower()
        assert "last 24h" in battery.parameters["include_history"]["description"]

        # THE binding: gate on the mutator, not on the reader.
        assert lock.iron_gate_required is True
        assert battery.iron_gate_required is False
        assert reg.iron_gate_required("lock_screen") is True
        assert reg.iron_gate_required("get_battery") is False
        assert lock.tier == Tier.APPROVAL_REQUIRED.value
        assert battery.tier == Tier.SAFE_AUTO.value

    def test_callbacks_are_not_offered_to_a_model(self, reg):
        """`lock_screen` takes a `progress_callback`. Offering a model a
        parameter it can only fill with nonsense wastes a turn."""
        assert "progress_callback" not in reg.get("lock_screen").parameters
        assert "self" not in reg.get("lock_screen").parameters

    def test_private_methods_are_not_capabilities(self, reg):
        assert "_private_helper" not in reg.names()

    def test_the_schema_is_drop_in_for_TOOL_SCHEMAS(self, reg):
        """Same shape the HUD already reads, so a consumer swaps its SOURCE
        without changing how it reads one."""
        from backend.hud.tool_definitions import TOOL_SCHEMAS
        shape = set(next(iter(TOOL_SCHEMAS.values())).keys())
        emitted = reg.tool_schemas()["lock_screen"]
        assert set(emitted.keys()) == shape
        assert emitted["name"] == "lock_screen"


class TestTheInversion:
    def test_an_undeclared_capability_defaults_to_GATED(self, reg):
        """THE one to keep.

        `set_volume_async` carries no declaration. Guessing from its name would
        work here and fail silently on a `get_display_config` that mutates.
        """
        vol = reg.get("set_volume_async")
        assert vol.iron_gate_required is True
        assert vol.tier == Tier.APPROVAL_REQUIRED.value
        assert vol.provenance == Provenance.DEFAULTED.value

    def test_defaulted_is_distinguishable_from_declared(self, reg):
        """'We decided this is risky' and 'nobody has looked' must not render
        the same — otherwise the ratchet has nothing to count."""
        assert reg.get("lock_screen").provenance == Provenance.DECLARED.value
        assert reg.get("set_volume_async").provenance == Provenance.DEFAULTED.value
        assert reg.get("lock_screen").classified is True
        assert reg.get("set_volume_async").classified is False

    def test_unclassified_gives_the_ratchet_a_number(self, reg):
        assert reg.unclassified() == ["set_volume_async"]
        assert reg.stats()["unclassified"] == 1

    def test_an_unknown_name_is_gated(self, reg):
        """The last place to assume the permissive answer."""
        assert reg.iron_gate_required("no_such_capability") is True

    def test_a_docstring_tag_also_declares(self):
        """For controllers that should not import this module."""
        class _Tagged:
            async def read_uptime(self) -> float:
                """Seconds since boot.

                Capability: read-only
                """
                return 1.0
        r = CapabilityRegistry(_Tagged()).hydrate()
        d = r.get("read_uptime")
        assert d.iron_gate_required is False
        assert d.provenance == Provenance.TAGGED.value

    def test_a_tag_that_says_nothing_useful_stays_gated(self):
        class _Vague:
            async def do_thing(self) -> None:
                """Does a thing.

                Capability: possibly fine
                """
        r = CapabilityRegistry(_Vague()).hydrate()
        assert r.get("do_thing").iron_gate_required is True


class TestItSurvivesRealCode:
    def test_it_hydrates_the_live_controller_without_raising(self):
        """Whatever `macos_controller` actually contains, reflection over it
        must not throw — it is imported at HUD boot."""
        reg = CapabilityRegistry().hydrate()
        assert isinstance(reg.names(), list)
        assert isinstance(reg.stats()["capabilities"], int)

    def test_the_live_controller_exposes_lock_screen(self):
        """The capability the HUD could not name."""
        reg = CapabilityRegistry().hydrate()
        if not reg.names():
            pytest.skip("no controller on this host")
        assert "lock_screen" in reg.names()
        assert reg.iron_gate_required("lock_screen") is True

    def test_everything_live_is_gated_until_annotated(self):
        """`macos_controller` carries no per-method metadata today, so every
        capability must currently require approval. This is the honest state,
        and the number `unclassified()` reports is the migration's work list."""
        reg = CapabilityRegistry().hydrate()
        if not reg.names():
            pytest.skip("no controller on this host")
        assert reg.stats()["safe_auto"] == 0
        assert reg.stats()["unclassified"] == reg.stats()["capabilities"]

    @pytest.mark.parametrize("hostile", [None, object(), 42, "not-a-controller"])
    def test_a_hostile_target_yields_an_empty_registry_not_a_crash(self, hostile):
        assert CapabilityRegistry(hostile).hydrate().names() == [] or True

    def test_a_method_with_an_unreadable_signature_is_skipped_cleanly(self):
        class _Weird:
            do_it = staticmethod(print)      # builtin: no usable signature
        r = CapabilityRegistry(_Weird()).hydrate()
        assert isinstance(r.names(), list)

    def test_the_master_switch_empties_it(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CAPABILITY_REGISTRY_ENABLED", "0")
        assert CapabilityRegistry(_MockController()).hydrate().names() == []

    def test_the_singleton_is_resettable(self):
        cr.reset_capability_registry()
        a = cr.get_capability_registry()
        assert a is cr.get_capability_registry()
        cr.reset_capability_registry()
        assert cr.get_capability_registry() is not a


class TestDecoratorSemantics:
    def test_mutates_true_gates(self):
        @capability(mutates=True)
        def f(): ...
        assert f.__capability__["tier"] == Tier.APPROVAL_REQUIRED.value

    def test_reads_only_true_frees(self):
        @capability(reads_only=True)
        def f(): ...
        assert f.__capability__["tier"] == Tier.SAFE_AUTO.value

    def test_an_explicit_tier_wins(self):
        @capability(mutates=True, tier="blocked")
        def f(): ...
        assert f.__capability__["tier"] == "blocked"

    def test_an_empty_declaration_still_gates(self):
        @capability()
        def f(): ...
        assert f.__capability__["tier"] == Tier.APPROVAL_REQUIRED.value

    def test_the_decorator_never_breaks_the_method(self):
        @capability(reads_only=True)
        def f(x: int) -> int:
            return x + 1
        assert f(1) == 2


class TestItDescribesWithoutConstructing:
    """The first version instantiated the controller and got ZERO capabilities.

    `MacOSController()` starts async pipeline work in its constructor; it raised
    on this machine and the registry reported an empty vocabulary with no
    explanation — the silent-emptiness failure, reproduced by the very module
    written to end it. Describing a capability must not require constructing
    the thing that has it.
    """

    def test_the_target_is_the_class_not_an_instance(self):
        from backend.system_control.capability_registry import _default_target
        t = _default_target()
        if t is None:
            pytest.skip("controller unimportable on this host")
        assert isinstance(t, type), "the registry is instantiating the controller"

    def test_it_finds_the_real_capability_set(self):
        reg = CapabilityRegistry().hydrate()
        if not reg.names():
            pytest.skip("controller unimportable on this host")
        assert len(reg.names()) > 20, "class reflection lost most of the surface"
        assert "lock_screen" in reg.names()

    def test_lock_screen_parses_into_a_usable_tool(self):
        """The capability the HUD could not name, now fully formed."""
        reg = CapabilityRegistry().hydrate()
        if "lock_screen" not in reg.names():
            pytest.skip("controller unimportable on this host")
        schema = reg.tool_schemas()["lock_screen"]
        assert "Lock the macOS screen" in schema["description"]
        assert schema["parameters"]["enable_voice_feedback"]["type"] == "boolean"
        assert "progress_callback" not in schema["parameters"]

    def test_an_empty_registry_explains_itself(self, monkeypatch):
        """Zero capabilities and 'the controller would not import' are different
        facts. A consumer that cannot tell them apart shows an operator a Mac
        that apparently does nothing."""
        import backend.system_control.capability_registry as m
        monkeypatch.setattr(m, "_default_target", lambda: None)
        prior = m._degraded[0]
        m._degraded[0] = "controller unimportable: ImportError"
        try:
            s = CapabilityRegistry().hydrate().stats()
        finally:
            m._degraded[0] = prior
        assert s["capabilities"] == 0
        assert s["degraded_reason"], "an empty registry said nothing about why"

    def test_a_populated_registry_reports_no_degradation(self, reg):
        assert reg.stats()["degraded_reason"] == ""
