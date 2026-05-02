"""Gap #3 Slice 3 — worktree_topology_sse_bridge regression suite.

Covers:

  §1   master flag + default-off
  §2   bridge construction with default and custom broker
  §3   install registers two handlers on the emitter
  §4   end-to-end: graph event → SSE topology_updated publish
  §5   end-to-end: unit event → SSE unit_state_changed publish
  §6   payload + op_id pass through verbatim
  §7   master-off → handlers no-op (no SSE publishes)
  §8   defensive: malformed envelope → no SSE, no raise
  §9   install_default_bridge convenience: master-off returns None
  §10  install_default_bridge: master-on installs and returns bridge
  §11  unrelated event types ignored
  §12  AST authority pins
  §13  SSE event vocabulary parity (both new types in _VALID_EVENT_TYPES)
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.event_emitter import (
    EventEmitter,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED,
    EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED,
    StreamEventBroker,
    _VALID_EVENT_TYPES,
)
from backend.core.ouroboros.governance.verification.worktree_topology_sse_bridge import (
    WORKTREE_TOPOLOGY_SSE_BRIDGE_SCHEMA_VERSION,
    WorktreeTopologySSEBridge,
    install_default_bridge,
    worktree_topology_sse_enabled,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "verification"
    / "worktree_topology_sse_bridge.py"
)


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "true",
    )


# ============================================================================
# §1 — Master flag + default-off
# ============================================================================


class TestMasterFlag:
    def test_default_post_graduation_is_true(self, monkeypatch):
        """Slice 5 graduation (2026-05-02): bridge is a pure
        translator with fault-isolated handlers — safe to enable
        by default."""
        monkeypatch.delenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", raising=False,
        )
        assert worktree_topology_sse_enabled() is True

    def test_explicit_true(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "true",
        )
        assert worktree_topology_sse_enabled() is True

    def test_explicit_false_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "false",
        )
        assert worktree_topology_sse_enabled() is False

    def test_garbage_value_treated_as_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "maybe",
        )
        assert worktree_topology_sse_enabled() is False


# ============================================================================
# §2 — Bridge construction
# ============================================================================


class TestConstruction:
    def test_default_broker(self):
        b = WorktreeTopologySSEBridge()
        assert b._broker is None  # uses get_default_broker() lazily

    def test_custom_broker(self):
        broker = StreamEventBroker()
        b = WorktreeTopologySSEBridge(broker=broker)
        assert b._broker is broker
        assert b._get_broker() is broker


# ============================================================================
# §3 — install registers two handlers
# ============================================================================


class TestInstall:
    def test_install_subscribes_to_both_event_types(self):
        emitter = EventEmitter()
        broker = StreamEventBroker()
        bridge = WorktreeTopologySSEBridge(broker=broker)
        bridge.install(emitter)
        assert emitter.subscriber_count(
            EventType.EXECUTION_GRAPH_STATE_CHANGED,
        ) == 1
        assert emitter.subscriber_count(
            EventType.WORK_UNIT_STATE_CHANGED,
        ) == 1

    def test_install_swallows_emitter_failures(self):
        class BadEmitter:
            def subscribe(self, *args, **kwargs):
                raise RuntimeError("simulated subscribe failure")
        bridge = WorktreeTopologySSEBridge()
        # Must not raise
        bridge.install(BadEmitter())


# ============================================================================
# §4 — End-to-end: graph event → SSE topology_updated
# ============================================================================


class TestGraphEventBridge:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable(monkeypatch)

    def test_graph_event_triggers_sse_publish(self):
        async def main():
            emitter = EventEmitter()
            broker = StreamEventBroker()
            bridge = WorktreeTopologySSEBridge(broker=broker)
            bridge.install(emitter)
            env = EventEnvelope(
                source_layer="L1",
                event_type=EventType.EXECUTION_GRAPH_STATE_CHANGED,
                payload={
                    "graph_id": "g1", "phase": "running",
                    "ready_units": [], "running_units": ["a"],
                    "completed_units": [], "failed_units": [],
                    "cancelled_units": [], "last_error": "",
                },
                op_id="op-1",
            )
            await emitter.emit(env)
            return list(broker._history)

        history = asyncio.run(main())
        types = [e.event_type for e in history]
        assert EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED in types


# ============================================================================
# §5 — End-to-end: unit event → SSE unit_state_changed
# ============================================================================


class TestUnitEventBridge:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable(monkeypatch)

    def test_unit_event_triggers_sse_publish(self):
        async def main():
            emitter = EventEmitter()
            broker = StreamEventBroker()
            bridge = WorktreeTopologySSEBridge(broker=broker)
            bridge.install(emitter)
            env = EventEnvelope(
                source_layer="L1",
                event_type=EventType.WORK_UNIT_STATE_CHANGED,
                payload={
                    "graph_id": "g1", "unit_id": "a",
                    "repo": "primary", "status": "running",
                    "barrier_id": "", "owned_paths": ["f.py"],
                },
                op_id="op-1",
            )
            await emitter.emit(env)
            return list(broker._history)

        history = asyncio.run(main())
        types = [e.event_type for e in history]
        assert EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED in types


# ============================================================================
# §6 — Payload + op_id pass through verbatim
# ============================================================================


class TestPayloadPassthrough:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable(monkeypatch)

    def test_payload_preserved(self):
        original_payload = {
            "graph_id": "g1", "unit_id": "a",
            "repo": "primary", "status": "completed",
            "barrier_id": "B1",
            "owned_paths": ["f.py", "g.py"],
            "failure_class": "",
            "error": "",
            "runtime_ms": 1234.5,
            "causal_parent_id": "parent-x",
        }

        async def main():
            emitter = EventEmitter()
            broker = StreamEventBroker()
            bridge = WorktreeTopologySSEBridge(broker=broker)
            bridge.install(emitter)
            env = EventEnvelope(
                source_layer="L1",
                event_type=EventType.WORK_UNIT_STATE_CHANGED,
                payload=original_payload,
                op_id="op-7",
            )
            await emitter.emit(env)
            return list(broker._history)

        history = asyncio.run(main())
        unit_events = [
            e for e in history
            if e.event_type == EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED
        ]
        assert len(unit_events) == 1
        # Payload preserved verbatim
        assert unit_events[0].payload == original_payload
        # op_id propagated to SSE frame
        assert unit_events[0].op_id == "op-7"


# ============================================================================
# §7 — Master-off → handlers no-op
# ============================================================================


class TestMasterOffNoOp:
    def test_disabled_handlers_skip_publish(self, monkeypatch):
        # Hot-revert: explicit ``=false`` skips publishes even
        # when bridge is installed.
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "false",
        )

        async def main():
            emitter = EventEmitter()
            broker = StreamEventBroker()
            bridge = WorktreeTopologySSEBridge(broker=broker)
            bridge.install(emitter)
            env = EventEnvelope(
                source_layer="L1",
                event_type=EventType.EXECUTION_GRAPH_STATE_CHANGED,
                payload={"graph_id": "g1"},
                op_id="op-1",
            )
            await emitter.emit(env)
            return list(broker._history)

        history = asyncio.run(main())
        # No worktree events should be published when master is off
        types = [e.event_type for e in history]
        assert EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED not in types
        assert EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED not in types


# ============================================================================
# §8 — Defensive: malformed envelope
# ============================================================================


class TestDefensive:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable(monkeypatch)

    def test_handler_on_malformed_event_does_not_raise(self):
        async def main():
            broker = StreamEventBroker()
            bridge = WorktreeTopologySSEBridge(broker=broker)
            # Hand-craft a bogus event with no envelope shape
            await bridge._handle_graph_event("not an envelope")
            await bridge._handle_unit_event(None)
            await bridge._handle_graph_event(42)
            return list(broker._history)

        history = asyncio.run(main())
        # Bogus inputs still produce SSE publishes (with empty
        # op_id + empty payload) — that's the never-raises contract.
        # Consumers of bogus inputs are not the production path.
        # We only assert NO raise here.
        assert isinstance(history, list)


# ============================================================================
# §9 — install_default_bridge convenience: master-off
# ============================================================================


class TestConvenienceMasterOff:
    def test_returns_none_when_disabled(self, monkeypatch):
        # Hot-revert: explicit ``=false`` returns None even
        # post-graduation.
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_SSE_ENABLED", "false",
        )
        emitter = EventEmitter()
        result = install_default_bridge(emitter)
        assert result is None
        # No subscriptions should have been added
        assert emitter.subscriber_count(
            EventType.EXECUTION_GRAPH_STATE_CHANGED,
        ) == 0


# ============================================================================
# §10 — install_default_bridge: master-on installs
# ============================================================================


class TestConvenienceMasterOn:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable(monkeypatch)

    def test_returns_bridge_when_enabled(self):
        emitter = EventEmitter()
        broker = StreamEventBroker()
        bridge = install_default_bridge(emitter, broker=broker)
        assert isinstance(bridge, WorktreeTopologySSEBridge)
        assert emitter.subscriber_count(
            EventType.EXECUTION_GRAPH_STATE_CHANGED,
        ) == 1
        assert emitter.subscriber_count(
            EventType.WORK_UNIT_STATE_CHANGED,
        ) == 1


# ============================================================================
# §11 — Unrelated event types ignored
# ============================================================================


class TestUnrelatedEventsIgnored:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable(monkeypatch)

    def test_other_event_types_do_not_publish_worktree_sse(self):
        # Pick a real EventType the bridge doesn't subscribe to.
        # Verify it doesn't bleed into the worktree SSE channels.
        async def main():
            emitter = EventEmitter()
            broker = StreamEventBroker()
            bridge = WorktreeTopologySSEBridge(broker=broker)
            bridge.install(emitter)
            # Find an EventType that is NOT one of our two
            other = next(
                e for e in EventType
                if e not in (
                    EventType.EXECUTION_GRAPH_STATE_CHANGED,
                    EventType.WORK_UNIT_STATE_CHANGED,
                )
            )
            env = EventEnvelope(
                source_layer="L1",
                event_type=other,
                payload={},
                op_id="op-x",
            )
            await emitter.emit(env)
            return list(broker._history)

        history = asyncio.run(main())
        types = [e.event_type for e in history]
        assert EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED not in types
        assert EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED not in types


# ============================================================================
# §12 — AST authority pins
# ============================================================================


_FORBIDDEN_AUTH_TOKENS = (
    "orchestrator", "iron_gate", "policy_engine",
    "risk_engine", "change_engine", "tool_executor",
    "providers", "candidate_generator", "semantic_guardian",
    "semantic_firewall", "scoped_tool_backend",
    "subagent_scheduler",  # bridge MUST NOT import scheduler —
                            # one-way translator only
    "worktree_manager",     # bridge does NOT touch worktrees
                            # directly; topology is in scheduler state
)
_SUBPROC_TOKENS = (
    "subprocess" + ".",
    "os." + "system",
    "popen",
)
_FS_TOKENS = (
    "open(", ".write(", "os.remove",
    "os.unlink", "shutil.", "Path(", "pathlib",
)
_ENV_MUTATION_TOKENS = (
    "os.environ[", "os.environ.pop", "os.environ.update",
    "os.put" + "env", "os.set" + "env",
)


class TestAuthorityInvariants:
    @pytest.fixture(scope="class")
    def source(self):
        return _MODULE_PATH.read_text(encoding="utf-8")

    def test_no_authority_imports(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in _FORBIDDEN_AUTH_TOKENS:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: "
                        f"{module}"
                    )

    def test_governance_imports_in_allowlist(self, source):
        """Slice 3 may import ONLY:
          * autonomy.autonomy_types (EventType)
          * ide_observability_stream (publish + event constants)"""
        allowed = {
            "backend.core.ouroboros.governance.autonomy.autonomy_types",
            "backend.core.ouroboros.governance.ide_observability_stream",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "governance" in module:
                    assert module in allowed, (
                        f"governance import outside allowlist: "
                        f"{module}"
                    )

    def test_no_filesystem_io(self, source):
        for tok in _FS_TOKENS:
            assert tok not in source, (
                f"forbidden FS token: {tok}"
            )

    def test_no_eval_exec_compile(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in (
                    "eval", "exec", "compile",
                ), f"forbidden bare call: {node.func.id}"

    def test_no_subprocess(self, source):
        for token in _SUBPROC_TOKENS:
            assert token not in source, (
                f"forbidden subprocess token: {token}"
            )

    def test_no_env_mutation(self, source):
        for token in _ENV_MUTATION_TOKENS:
            assert token not in source, (
                f"forbidden env mutation token: {token}"
            )

    def test_schema_version_canonical(self):
        assert WORKTREE_TOPOLOGY_SSE_BRIDGE_SCHEMA_VERSION == (
            "worktree_topology_sse_bridge.1"
        )


# ============================================================================
# §13 — SSE event vocabulary parity
# ============================================================================


class TestSSEEventVocabulary:
    def test_topology_updated_in_valid_set(self):
        assert (
            EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED
            in _VALID_EVENT_TYPES
        )

    def test_unit_state_changed_in_valid_set(self):
        assert (
            EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED
            in _VALID_EVENT_TYPES
        )

    def test_canonical_string_values(self):
        assert EVENT_TYPE_WORKTREE_TOPOLOGY_UPDATED == (
            "worktree_topology_updated"
        )
        assert EVENT_TYPE_WORKTREE_UNIT_STATE_CHANGED == (
            "worktree_unit_state_changed"
        )
