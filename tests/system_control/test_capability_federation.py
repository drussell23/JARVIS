"""Many subsystems, one vocabulary — and the HUD can finally name them.

`capability_registry` answered "what can this Mac do" for ONE controller. The
same question over the whole organism had three more answers the HUD could reach
none of: multi-space intelligence, video multi-space intelligence, and ghost
touch. Roughly 11,000 lines of working capability whose NAMES were missing.

The mandated scenario is `test_the_three_subsystems_reach_the_hud`: after
hydration, a `space.`, a `video.` and a `touch.` capability are all nameable by
the model AND accepted by the Iron Gate's name check — because a capability the
gate rejects is not reachable no matter how well it was discovered.

THE TESTS TO KEEP
-------------------
`test_a_provider_with_a_required_dependency_is_buildable`. `VideoStreamCapture`
takes a required `vision_analyzer`, so the no-arg fallback raised `TypeError`
and the entire video namespace described perfectly and constructed never — the
worst available shape, since the model is offered a tool that fails every call.

`test_a_name_two_providers_claim_is_refused`. Arrival order must never decide
which subsystem a caller reached. A conflict that resolves silently is a routing
bug that only manifests as the wrong subsystem doing the right thing.

`test_an_unhydrated_namespace_is_not_an_empty_one`. UNKNOWN is not EMPTY. A
surface that renders them the same shows an operator a Mac that apparently
cannot see its own screens.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.system_control import capability_federation as cf
from backend.system_control.capability_federation import (
    CapabilityFederation,
    ProviderSpec,
    Readiness,
    discover,
    split,
)
from backend.system_control.capability_registry import Tier


@pytest.fixture(autouse=True)
def _clean():
    cf.reset_federation()
    cf.reset_bindings()
    yield
    cf.reset_federation()
    cf.reset_bindings()


# ---------------------------------------------------------------------------
# Fixture providers, written to a temp tree so discovery is exercised for real.
# ---------------------------------------------------------------------------

_PROVIDER_A = '''
class Streamer:
    """A fake video subsystem.

    Capability-Namespace: fakevideo
    """

    def __init__(self, vision_analyzer):
        self.vision_analyzer = vision_analyzer
        self.running = False

    async def start_streaming(self) -> bool:
        """Begin capturing.

        Capability: session-start, release=stop_streaming
        """
        self.running = True
        return True

    async def stop_streaming(self):
        """Stop capturing.

        Capability: session-end
        """
        self.running = False

    def get_metrics(self) -> dict:
        """Streaming metrics.

        Capability: read-only
        """
        return {"running": self.running}

    def cleanup(self):
        """Lifecycle plumbing that is NOT a capability."""
'''

_PROVIDER_B = '''
class Toucher:
    """A fake automation subsystem sharing a namespace.

    Capability-Namespace: faketouch
    """

    def __init__(self):
        self.started = False

    async def start(self) -> bool:
        """Start the toucher.

        Capability: session-start, release=stop_toucher, as=start_toucher
        """
        self.started = True
        return True

    async def stop(self):
        """Stop the toucher.

        Capability: session-end, as=stop_toucher
        """
        self.started = False

    def list_tasks(self) -> list:
        """Toucher's tasks — the name Watcher also claims.

        Capability: read-only
        """
        return []


class Watcher:
    """A second provider in the same namespace.

    Capability-Namespace: faketouch
    """

    async def start(self) -> bool:
        """Start watching. Exports as `start` because it declared no alias —
        which is exactly why it does NOT collide with Toucher's aliased start.

        Capability: session-start, release=stop_watcher
        """
        return True

    async def stop_watcher(self):
        """Stop watching.

        Capability: session-end
        """

    def list_tasks(self) -> list:
        """Watcher's tasks — UNALIASED, so it collides with Toucher's.

        Capability: read-only
        """
        return []
'''

_PROVIDER_BROKEN = '''
class Leaky:
    """Declares a session it can never release.

    Capability-Namespace: fakeleak
    """

    async def start_thing(self) -> bool:
        """No release named at all.

        Capability: session-start
        """
        return True

    async def start_other(self) -> bool:
        """Names a release that is not a session-end.

        Capability: session-start, release=not_an_end
        """
        return True

    async def not_an_end(self):
        """Exists, but was never tagged session-end.

        Capability: read-only
        """
'''


@pytest.fixture
def tree(tmp_path: Path, monkeypatch) -> Path:
    """A real package tree with real annotated modules."""
    pkg = tmp_path / "fakeprov"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "streamer.py").write_text(_PROVIDER_A)
    (pkg / "toucher.py").write_text(_PROVIDER_B)
    (pkg / "leaky.py").write_text(_PROVIDER_BROKEN)
    import sys
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))
    for mod in list(sys.modules):
        if mod.startswith("fakeprov"):
            del sys.modules[mod]


@pytest.fixture
def fed(tree: Path) -> CapabilityFederation:
    return CapabilityFederation(discover(roots=("fakeprov",), repo_root=tree))


class TestTheMandatedScenario:
    async def test_the_three_subsystems_reach_the_hud(self):
        """THE scenario, against the REAL annotated classes in this repo.

        Discovery, hydration, naming and the Iron Gate's name check — because a
        capability the gate rejects is unreachable however well it was found.
        """
        from backend.hud.tool_definitions import (
            ToolCall, derived_tool_names, derived_tool_schemas,
            validate_tool_call,
        )

        federation = cf.get_federation()
        assert {"space", "video", "touch"} <= set(federation.namespaces())

        await federation.warm()

        names = derived_tool_names()
        schemas = derived_tool_schemas()
        for want in ("space.analyze_desktop_spaces", "video.start_streaming",
                     "touch.watch_and_react"):
            assert want in names, f"{want} is not nameable by the model"
            assert want in schemas, f"{want} is not in the prompt"
            ok, why = validate_tool_call(ToolCall(name=want, args={}))
            assert ok, f"Iron Gate blocked a real capability: {why}"

        # The hand-written nine are not displaced by any of it.
        assert "take_screenshot" in schemas
        assert schemas["take_screenshot"]["description"].startswith("Capture")


class TestDiscovery:
    def test_it_finds_providers_without_importing_anything(self, tree):
        import sys
        specs = discover(roots=("fakeprov",), repo_root=tree)
        assert {s.class_name for s in specs} == {"Streamer", "Toucher",
                                                 "Watcher", "Leaky"}
        # The whole point of a static scan: nothing ran.
        assert not any(m.startswith("fakeprov.") for m in sys.modules)

    def test_an_unannotated_class_is_not_a_provider(self, tmp_path):
        (tmp_path / "plain").mkdir()
        (tmp_path / "plain" / "__init__.py").write_text("")
        (tmp_path / "plain" / "m.py").write_text(
            'class NotAProvider:\n    """No tag here."""\n')
        assert discover(roots=("plain",), repo_root=tmp_path) == []

    def test_an_unparseable_module_does_not_stop_the_scan(self, tree):
        (tree / "fakeprov" / "broken.py").write_text(
            'class X:\n """Capability-Namespace: nope"""\n  def (:\n')
        specs = discover(roots=("fakeprov",), repo_root=tree)
        assert {s.class_name for s in specs} >= {"Streamer", "Toucher"}

    def test_a_missing_root_is_skipped_not_fatal(self, tree):
        assert discover(roots=("nope_not_here",), repo_root=tree) == []

    def test_disabled_discovers_nothing(self, monkeypatch, tree):
        monkeypatch.setenv("JARVIS_CAPABILITY_FEDERATION_ENABLED", "0")
        assert cf.federation_enabled() is False
        assert discover(roots=("fakeprov",), repo_root=tree) == []

    def test_roots_come_from_env_not_from_this_file(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CAPABILITY_FEDERATION_ROOTS", "a/b, c/d ")
        assert cf.scan_roots() == ("a/b", "c/d")


class TestNamespacingAndCollisions:
    async def test_only_declared_methods_are_exported(self, fed):
        await fed.ensure("fakevideo")
        exported = {split(n)[1] for n in fed.names()}
        assert {"start_streaming", "stop_streaming", "get_metrics"} <= exported
        # `public and callable` over-collects on a subsystem: silence means
        # "not offered", which makes the ratchet honest.
        assert "cleanup" not in exported

    async def test_an_alias_renames_the_export_not_the_method(self, fed):
        await fed.ensure("faketouch")
        assert "faketouch.start_toucher" in fed.names()
        assert "faketouch.stop_toucher" in fed.names()
        # The METHOD keeps the name its existing callers use.
        assert fed.method_for("faketouch.start_toucher") == "start"
        assert fed.method_for("faketouch.stop_toucher") == "stop"

    async def test_a_name_two_providers_claim_is_refused(self, fed):
        """Arrival order must never decide which subsystem a caller reached."""
        await fed.ensure("faketouch")
        conflicts = fed.conflicts()
        assert "faketouch.list_tasks" in conflicts
        assert len(conflicts["faketouch.list_tasks"]) == 2
        # REFUSED, not resolved: neither provider gets the name.
        assert "faketouch.list_tasks" not in fed.names()
        assert fed.get("faketouch.list_tasks") is None

    async def test_an_alias_is_what_prevents_the_collision(self, fed):
        """The same two classes both define `start`; only one is aliased.

        `Toucher.start` exports as `start_toucher` and `Watcher.start` exports
        as `start`, so both survive. Their `list_tasks` — neither aliased — does
        not. The alias is doing the work, at the declaration.
        """
        await fed.ensure("faketouch")
        names = set(fed.names())
        assert {"faketouch.start_toucher", "faketouch.start"} <= names
        assert fed.method_for("faketouch.start_toucher") == "start"
        assert fed.method_for("faketouch.start") == "start"
        # ...and they resolve to DIFFERENT providers.
        assert (type(fed.resolve_target("faketouch.start_toucher")).__name__
                != type(fed.resolve_target("faketouch.start")).__name__)

    async def test_namespaces_do_not_collide_with_each_other(self, fed):
        await fed.warm()
        assert "fakevideo.start_streaming" in fed.names()
        assert "faketouch.start_toucher" in fed.names()

    def test_split_handles_a_bare_name(self):
        assert split("video.start") == ("video", "start")
        assert split("lock_screen") == ("", "lock_screen")


class TestSessionRules:
    async def test_a_start_is_never_safe_auto(self, fed):
        """Duration is itself the risk — one screenshot is not a recording."""
        await fed.ensure("fakevideo")
        start = fed.get("fakevideo.start_streaming")
        assert start.starts_session is True
        assert start.tier != Tier.SAFE_AUTO.value
        assert fed.iron_gate_required("fakevideo.start_streaming") is True

    async def test_an_end_is_always_safe_auto(self, fed):
        """A gate on the release path is a deadlock wearing the costume of caution."""
        await fed.ensure("fakevideo")
        end = fed.get("fakevideo.stop_streaming")
        assert end.ends_session is True
        assert end.tier == Tier.SAFE_AUTO.value
        assert fed.iron_gate_required("fakevideo.stop_streaming") is False

    async def test_a_start_with_no_release_is_surfaced(self, fed):
        await fed.ensure("fakeleak")
        assert "fakeleak.start_thing" in fed.unreleasable()

    async def test_a_release_the_reaper_cannot_call_is_surfaced(self, fed):
        """The subtler leak: a release that exists but is gated."""
        await fed.ensure("fakeleak")
        broken = fed.broken_releases()
        assert "fakeleak.start_other" in broken
        assert "not tagged session-end" in broken["fakeleak.start_other"]

    async def test_the_real_providers_have_neither_defect(self):
        """A defect a test fails on, rather than a stream nothing can stop."""
        federation = cf.get_federation()
        await federation.warm()
        assert federation.unreleasable() == []
        assert federation.broken_releases() == {}
        assert federation.conflicts() == {}
        assert len(federation.sessions()) >= 5


class TestConstruction:
    async def test_a_provider_with_a_required_dependency_is_buildable(self, fed):
        """The `TypeError` that made the whole video namespace dead on arrival."""
        await fed.ensure("fakevideo")

        assert fed.resolve_target("fakevideo.start_streaming") is None
        why = fed.unbuildable()
        assert any("vision_analyzer" in v for v in why.values())

        sentinel = object()
        cf.bind("vision_analyzer", sentinel)
        inst = fed.resolve_target("fakevideo.start_streaming")

        assert inst is not None
        assert inst.vision_analyzer is sentinel
        assert fed.unbuildable() == {}

    async def test_an_instance_is_cached_per_provider(self, fed):
        cf.bind("vision_analyzer", object())
        await fed.ensure("fakevideo")
        assert (fed.resolve_target("fakevideo.start_streaming")
                is fed.resolve_target("fakevideo.get_metrics"))

    async def test_a_defaulted_parameter_is_left_alone(self, fed):
        """`cache_size_mb=200` means the engine chose 200, not that it is asking."""
        cf.bind("config", "SPOOKY")
        await fed.ensure("fakevideo")
        cf.bind("vision_analyzer", object())
        inst = fed.resolve_target("fakevideo.start_streaming")
        assert inst is not None

    def test_a_binding_may_be_a_factory_or_the_value_itself(self):
        b = cf.ProviderBindings()
        b.bind("made", lambda: "built")
        b.bind("given", "as-is")

        async def analyzer(prompt, image):    # a dependency that IS callable
            return "x"

        b.bind("analyzer", analyzer)
        assert b.resolve("made") == (True, "built")
        assert b.resolve("given") == (True, "as-is")
        # An async callable is the dependency, not a factory — calling it here
        # would fire a vision request instead of binding one.
        assert b.resolve("analyzer") == (True, analyzer)

    def test_bound_to_none_is_not_the_same_as_unbound(self):
        b = cf.ProviderBindings()
        b.bind("thing", None)
        assert b.resolve("thing") == (True, None)
        assert b.resolve("other") == (False, None)


class TestReadiness:
    async def test_an_unhydrated_namespace_is_not_an_empty_one(self, fed):
        """UNKNOWN is not EMPTY."""
        st = fed.stats()["namespaces"]["fakevideo"]
        assert st["readiness"] == Readiness.UNHYDRATED.value
        assert fed.get("fakevideo.start_streaming") is None

        await fed.ensure("fakevideo")

        st = fed.stats()["namespaces"]["fakevideo"]
        assert st["readiness"] == Readiness.READY.value
        assert st["capabilities"] == 3

    async def test_a_namespace_that_cannot_import_is_degraded_with_a_reason(self, tree):
        fed = CapabilityFederation([ProviderSpec(
            namespace="ghost", module="fakeprov.does_not_exist",
            class_name="Nope")])
        st = await fed.ensure("ghost")
        assert st.readiness == Readiness.DEGRADED.value
        assert "ModuleNotFoundError" in st.detail or "Nope" in st.detail

    async def test_hydration_is_idempotent(self, fed):
        a = await fed.ensure("fakevideo")
        b = await fed.ensure("fakevideo")
        assert a is b

    async def test_concurrent_hydration_happens_once(self, fed):
        results = await asyncio.gather(*(fed.ensure("fakevideo")
                                         for _ in range(8)))
        assert all(r is results[0] for r in results)

    async def test_a_wedged_import_does_not_wedge_the_federation(
            self, monkeypatch, tree):
        monkeypatch.setenv("JARVIS_CAPABILITY_HYDRATE_TIMEOUT_S", "1")
        fed = CapabilityFederation([ProviderSpec(
            namespace="slow", module="fakeprov.streamer", class_name="Streamer")])

        def _hang(_ns):
            import time
            time.sleep(30)

        monkeypatch.setattr(fed, "_hydrate_ns_blocking", _hang)
        st = await asyncio.wait_for(fed.ensure("slow"), timeout=15)
        assert st.readiness == Readiness.DEGRADED.value
        assert "exceeded" in st.detail

    async def test_warm_hydrates_every_namespace(self, fed):
        result = await fed.warm()
        assert set(result) == {"fakevideo", "faketouch", "fakeleak"}
        assert all(v == Readiness.READY.value for v in result.values())


class TestTheSchemaSurface:
    async def test_schemas_carry_the_namespaced_name(self, fed):
        await fed.ensure("fakevideo")
        schemas = fed.tool_schemas()
        assert schemas["fakevideo.get_metrics"]["name"] == "fakevideo.get_metrics"
        assert "Streaming metrics" in schemas["fakevideo.get_metrics"]["description"]

    async def test_an_unhydrated_namespace_contributes_nothing(self, fed):
        assert fed.tool_schemas() == {}

    async def test_stats_never_raise_and_report_the_defects(self, fed):
        await fed.warm()
        s = fed.stats()
        for key in ("providers", "namespaces", "capabilities", "conflicts",
                    "sessions", "unreleasable", "broken_releases",
                    "unbuildable", "bindings"):
            assert key in s


class TestItNeverRaises:
    def test_a_hostile_target_degrades_quietly(self, fed):
        assert fed.get(None) is None            # type: ignore[arg-type]
        assert fed.get("") is None
        assert fed.iron_gate_required("nope.nope") is True
        assert fed.resolve_target("nope.nope") is None
        assert fed.method_for("nope.nope") == ""

    def test_an_unknown_name_is_gated_not_allowed(self, fed):
        """The inversion, at the federated layer too."""
        assert fed.iron_gate_required("never.heard.of.it") is True

    def test_the_singleton_is_process_wide_and_resettable(self):
        a = cf.get_federation()
        assert cf.get_federation() is a
        cf.reset_federation()
        assert cf.get_federation() is not a
