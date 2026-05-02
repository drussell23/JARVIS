"""SkillRegistry-AutonomousReach Slice 5 -- graduation regression spine.

Pins the graduation invariants:

  * Master flag defaults flipped false -> true across all 3
    surfaces (trigger primitive / observer / venom bridge)
  * Each module owns register_flags + register_shipped_invariants
  * FlagRegistry discovery seeds all 8 arc flags
  * shipped_code_invariants discovery seeds all 4 AST pins; each
    pin passes against the live source
  * SSE event constant + publish helper exist
  * SkillObserver fires the publish helper on every fire-or-skip
    decision so observability sees full lifecycle
  * End-to-end: at graduated defaults (no env vars set) an
    autonomous-reach skill registered in the catalog fires when
    the bus delivers a matching signal AND a model-reach skill
    is ALLOWed by the policy gate when called from Venom
"""
from __future__ import annotations

import asyncio
import importlib
import pathlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List

import pytest

from backend.core.ouroboros.governance.skill_catalog import (
    SkillCatalog,
    SkillInvocationOutcome,
    SkillSource,
    reset_default_catalog,
    reset_default_invoker,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
    SkillReach,
)
from backend.core.ouroboros.governance.skill_observer import (
    SkillObserver,
    skill_observer_enabled,
)
from backend.core.ouroboros.governance.skill_trigger import (
    skill_trigger_enabled,
)
from backend.core.ouroboros.governance.skill_venom_bridge import (
    bridge_enabled,
)


# ---------------------------------------------------------------------------
# Graduated defaults
# ---------------------------------------------------------------------------


class TestGraduatedDefaults:
    def test_trigger_master_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SKILL_TRIGGER_ENABLED", raising=False,
        )
        assert skill_trigger_enabled() is True

    def test_observer_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SKILL_OBSERVER_ENABLED", raising=False,
        )
        assert skill_observer_enabled() is True

    def test_bridge_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", raising=False,
        )
        assert bridge_enabled() is True


# ---------------------------------------------------------------------------
# Module-owned registration callables
# ---------------------------------------------------------------------------


_MODULES_WITH_FLAGS = (
    "backend.core.ouroboros.governance.skill_trigger",
    "backend.core.ouroboros.governance.skill_observer",
    "backend.core.ouroboros.governance.skill_venom_bridge",
)

_MODULES_WITH_INVARIANTS = (
    "backend.core.ouroboros.governance.skill_trigger",
    "backend.core.ouroboros.governance.skill_observer",
    "backend.core.ouroboros.governance.skill_venom_bridge",
)


class TestModuleOwnedRegistration:
    @pytest.mark.parametrize("modname", _MODULES_WITH_FLAGS)
    def test_register_flags_callable(self, modname):
        mod = importlib.import_module(modname)
        fn = getattr(mod, "register_flags", None)
        assert callable(fn), (
            f"{modname} missing module-owned register_flags"
        )

    @pytest.mark.parametrize("modname", _MODULES_WITH_INVARIANTS)
    def test_register_shipped_invariants_callable(self, modname):
        mod = importlib.import_module(modname)
        fn = getattr(mod, "register_shipped_invariants", None)
        assert callable(fn), (
            f"{modname} missing register_shipped_invariants"
        )


# ---------------------------------------------------------------------------
# FlagRegistry seeding -- all 8 arc flags actually register
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeding:
    def _empty_registry(self):
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )
        return FlagRegistry()

    def _all_flag_names(self) -> set:
        registry = self._empty_registry()
        for modname in _MODULES_WITH_FLAGS:
            mod = importlib.import_module(modname)
            mod.register_flags(registry)
        return {spec.name for spec in registry.list_all()}

    def test_all_8_flags_seeded(self):
        names = self._all_flag_names()
        expected = {
            # skill_trigger (3)
            "JARVIS_SKILL_TRIGGER_ENABLED",
            "JARVIS_SKILL_PER_WINDOW_MAX",
            "JARVIS_SKILL_WINDOW_S",
            # skill_observer (3)
            "JARVIS_SKILL_OBSERVER_ENABLED",
            "JARVIS_SKILL_OBSERVER_CONCURRENCY",
            "JARVIS_SKILL_DEDUP_TTL_S",
            # skill_venom_bridge (2)
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED",
            "JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS",
        }
        missing = expected - names
        assert not missing, f"missing seeds: {sorted(missing)}"

    @pytest.mark.parametrize("master_flag", [
        "JARVIS_SKILL_TRIGGER_ENABLED",
        "JARVIS_SKILL_OBSERVER_ENABLED",
        "JARVIS_SKILL_VENOM_BRIDGE_ENABLED",
    ])
    def test_master_flags_seeded_default_true(self, master_flag):
        registry = self._empty_registry()
        for modname in _MODULES_WITH_FLAGS:
            mod = importlib.import_module(modname)
            mod.register_flags(registry)
        spec = next(
            (s for s in registry.list_all() if s.name == master_flag),
            None,
        )
        assert spec is not None
        assert spec.default is True


# ---------------------------------------------------------------------------
# Shipped invariants -- pins discoverable + pass on live source
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    @pytest.mark.parametrize("modname", _MODULES_WITH_INVARIANTS)
    def test_invariants_returned_as_list(self, modname):
        mod = importlib.import_module(modname)
        invariants = mod.register_shipped_invariants()
        assert isinstance(invariants, list)
        assert len(invariants) >= 1

    def test_total_pin_count_meets_target(self):
        total = 0
        for modname in _MODULES_WITH_INVARIANTS:
            mod = importlib.import_module(modname)
            total += len(mod.register_shipped_invariants())
        # skill_trigger: 2 (pure-stdlib + 5-value taxonomies)
        # skill_observer: 1 (authority)
        # skill_venom_bridge: 1 (authority)
        # = 4 pins minimum
        assert total >= 4

    def test_each_pin_passes_against_live_source(self):
        """Every pin's validate() returns no violations against
        its target source. Load-bearing -- ensures the AST
        contracts the pins check actually match what's shipped."""
        import ast as _ast

        repo_root = pathlib.Path(__file__).resolve().parents[2]
        for modname in _MODULES_WITH_INVARIANTS:
            mod = importlib.import_module(modname)
            for inv in mod.register_shipped_invariants():
                target_path = repo_root / inv.target_file
                source = target_path.read_text()
                tree = _ast.parse(source)
                violations = inv.validate(tree, source)
                assert violations == (), (
                    f"{inv.invariant_name!r} flagged violations: "
                    f"{violations}"
                )


# ---------------------------------------------------------------------------
# SSE event surface
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_constant_defined(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_SKILL_INVOKED,
        )
        assert EVENT_TYPE_SKILL_INVOKED == "skill_invoked"

    def test_publish_helper_exists(self):
        from backend.core.ouroboros.governance import (
            ide_observability_stream as mod,
        )
        assert hasattr(mod, "publish_skill_invocation")
        assert callable(mod.publish_skill_invocation)

    def test_publish_helper_returns_none_when_stream_disabled(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_skill_invocation,
        )
        out = publish_skill_invocation(
            qualified_name="x",
            triggered_by_kind="sensor_fired",
            triggered_by_signal="sensor.fired.test",
            outcome="invoked",
            fired=True,
        )
        assert out is None

    def test_publish_helper_never_raises_on_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_skill_invocation,
        )
        publish_skill_invocation(
            qualified_name="",
            triggered_by_kind="",
            triggered_by_signal="",
            outcome="",
        )
        publish_skill_invocation(
            qualified_name="x",
            triggered_by_kind="sensor_fired",
            triggered_by_signal="sensor.fired.test",
            outcome="failed",
            spec_index=None,
            fired=False,
            invocation_ok=None,
            invocation_duration_ms=None,
        )


# ---------------------------------------------------------------------------
# Observer fires SSE on every decision
# ---------------------------------------------------------------------------


@dataclass
class _StubEvent:
    topic: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


class _StubBus:
    def __init__(self) -> None:
        self._subs: Dict[str, "tuple[str, Callable]"] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    async def subscribe(
        self, pattern: str,
        handler: Callable[[Any], Awaitable[None]],
    ) -> str:
        async with self._lock:
            self._counter += 1
            sub_id = f"sub-{self._counter}"
            self._subs[sub_id] = (pattern, handler)
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        async with self._lock:
            return self._subs.pop(subscription_id, None) is not None

    async def deliver(self, topic: str, payload: Dict[str, Any]) -> int:
        async with self._lock:
            entries = list(self._subs.items())
        count = 0
        for _, (pattern, handler) in entries:
            if pattern == topic or pattern == "*" or (
                pattern.endswith(".*")
                and topic.startswith(pattern[:-2] + ".")
            ):
                event = _StubEvent(topic=topic, payload=dict(payload))
                await handler(event)
                count += 1
        return count


class _StubInvoker:
    def __init__(self):
        self.calls = []

    async def invoke(self, qualified_name, *, args=None,
                     output_preview_chars=400):
        self.calls.append((qualified_name, dict(args or {})))
        return SkillInvocationOutcome(
            qualified_name=qualified_name,
            ok=True, duration_ms=1.0, result_preview="ok",
        )


def _build_manifest(*, name, reach="any", trigger_specs=None):
    safe_name = name.replace("-", "_").replace(":", "_")
    return SkillManifest.from_mapping({
        "name": name,
        "description": "d", "trigger": "t",
        "entrypoint": f"mod.{safe_name}:run",
        "reach": reach,
        "trigger_specs": list(trigger_specs or ()),
    })


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in (
        "JARVIS_SKILL_TRIGGER_ENABLED",
        "JARVIS_SKILL_OBSERVER_ENABLED",
        "JARVIS_SKILL_VENOM_BRIDGE_ENABLED",
        "JARVIS_SKILL_PER_WINDOW_MAX",
        "JARVIS_SKILL_WINDOW_S",
        "JARVIS_SKILL_OBSERVER_CONCURRENCY",
        "JARVIS_SKILL_DEDUP_TTL_S",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    reset_default_catalog()
    reset_default_invoker()


class TestObserverSSEPublish:
    @pytest.mark.asyncio
    async def test_observer_fires_publish_on_decision(
        self, monkeypatch, tmp_path,
    ):
        # Capture publish calls by monkey-patching the helper.
        calls: List[Dict[str, Any]] = []

        def _spy(**kwargs):
            calls.append(kwargs)
            return "evt-42"

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.publish_skill_invocation",
            _spy,
        )

        catalog = SkillCatalog()
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        bus = _StubBus()
        invoker = _StubInvoker()
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        await bus.deliver(
            "sensor.fired.test", {"sensor_name": "t"},
        )
        # The fire decision (and the SSE publish) happened.
        assert len(invoker.calls) == 1
        assert len(calls) == 1
        assert calls[0]["qualified_name"] == "a"
        assert calls[0]["fired"] is True
        assert calls[0]["outcome"] == "invoked"

    @pytest.mark.asyncio
    async def test_observer_fires_publish_on_skip_too(
        self, monkeypatch, tmp_path,
    ):
        """Even skipped invocations publish (full lifecycle)."""
        calls: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.publish_skill_invocation",
            lambda **kw: calls.append(kw),
        )

        # OPERATOR_PLUS_MODEL reach excludes AUTONOMOUS; will be
        # SKIPPED_DISABLED at the decision layer.
        catalog = SkillCatalog()
        catalog.register(
            _build_manifest(
                name="a", reach="operator_plus_model",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        bus = _StubBus()
        invoker = _StubInvoker()
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        await bus.deliver(
            "sensor.fired.test", {"sensor_name": "t"},
        )
        assert invoker.calls == []  # no fire
        assert len(calls) == 1  # but still published
        assert calls[0]["fired"] is False
        assert calls[0]["outcome"] == "skipped_disabled"

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_stall_observer(
        self, monkeypatch, tmp_path,
    ):
        def _explode(**_):
            raise RuntimeError("publish boom")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.publish_skill_invocation",
            _explode,
        )

        catalog = SkillCatalog()
        catalog.register(
            _build_manifest(
                name="a", reach="autonomous",
                trigger_specs=[{
                    "kind": "sensor_fired",
                    "signal_pattern": "sensor.fired.test",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        bus = _StubBus()
        invoker = _StubInvoker()
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        await obs.start()
        # Publish raises but observer continues -- invoker still
        # called.
        await bus.deliver(
            "sensor.fired.test", {"sensor_name": "t"},
        )
        assert len(invoker.calls) == 1


# ---------------------------------------------------------------------------
# End-to-end at graduated defaults
# ---------------------------------------------------------------------------


class TestGraduatedEndToEnd:
    @pytest.mark.asyncio
    async def test_autonomous_skill_fires_at_default_env(
        self, monkeypatch,
    ):
        """At graduated defaults (no env vars set), an autonomous
        skill registered in the catalog fires when the bus
        delivers a matching signal."""
        for var in (
            "JARVIS_SKILL_TRIGGER_ENABLED",
            "JARVIS_SKILL_OBSERVER_ENABLED",
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED",
        ):
            monkeypatch.delenv(var, raising=False)

        # Stub publish to avoid SSE side effects.
        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.publish_skill_invocation",
            lambda **kw: None,
        )

        catalog = SkillCatalog()
        catalog.register(
            _build_manifest(
                name="autonomous-skill", reach="autonomous",
                trigger_specs=[{
                    "kind": "drift_detected",
                    "signal_pattern": "coherence.drift_detected",
                    "required_drift_kind": "RECURRENCE_DRIFT",
                }],
            ),
            source=SkillSource.OPERATOR,
        )
        bus = _StubBus()
        invoker = _StubInvoker()
        obs = SkillObserver(
            event_bus=bus, catalog=catalog, invoker=invoker,
        )
        n = await obs.start()
        assert n == 1  # observer subscribed
        await bus.deliver(
            "coherence.drift_detected",
            {"drift_kind": "RECURRENCE_DRIFT"},
        )
        assert len(invoker.calls) == 1
        assert invoker.calls[0][0] == "autonomous-skill"

    @pytest.mark.asyncio
    async def test_model_reach_skill_allowed_by_policy(
        self, monkeypatch,
    ):
        """At graduated defaults, the Venom policy gate ALLOWs
        skill__* tool calls for model-reach skills in the
        catalog."""
        for var in (
            "JARVIS_SKILL_TRIGGER_ENABLED",
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED",
        ):
            monkeypatch.delenv(var, raising=False)

        from backend.core.ouroboros.governance.skill_catalog import (
            get_default_catalog,
        )
        from backend.core.ouroboros.governance.tool_executor import (
            GoverningToolPolicy, PolicyContext, PolicyDecision,
            ToolCall,
        )
        cat = get_default_catalog()
        cat.register(
            _build_manifest(name="model-skill", reach="model"),
            source=SkillSource.OPERATOR,
        )
        from pathlib import Path
        policy = GoverningToolPolicy(
            repo_roots={"test": Path("/tmp")},
        )
        ctx = PolicyContext(
            repo="test", repo_root=Path("/tmp"),
            op_id="op-grad", call_id="op-grad:r0:skill",
            round_index=0, is_read_only=False,
        )
        result = policy.evaluate(
            ToolCall(name="skill__model-skill", arguments={}),
            ctx,
        )
        assert result.decision is PolicyDecision.ALLOW
        assert result.reason_code == "tool.allowed.skill_registry"


# ---------------------------------------------------------------------------
# Operator escape hatches still work
# ---------------------------------------------------------------------------


class TestOperatorEscapeHatches:
    def test_trigger_master_off_overrides_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_TRIGGER_ENABLED", "false",
        )
        assert skill_trigger_enabled() is False

    def test_observer_off_overrides_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_OBSERVER_ENABLED", "false",
        )
        assert skill_observer_enabled() is False

    def test_bridge_off_overrides_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "false",
        )
        assert bridge_enabled() is False
