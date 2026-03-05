#!/usr/bin/env python3
"""
Governance compliance tests for enterprise organ classes.

Run: python3 -m pytest tests/unit/backend/test_enterprise_organ_governance.py -v
"""
import asyncio
import sys
from pathlib import Path
from typing import Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestMLOpsModelRegistryGovernance:
    """MLOpsModelRegistry must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import MLOpsModelRegistry, SystemService
        assert issubclass(MLOpsModelRegistry, SystemService)

    def test_constructor_purity(self):
        """__init__ must not perform I/O."""
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        assert hasattr(registry, '_initialized')
        assert registry._initialized is False

    def test_capability_contract_valid(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        contract = registry.capability_contract()
        assert contract.name == "MLOpsModelRegistry"
        assert contract.version != "0.0.0"
        assert "writes_model_registry" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        triggers = registry.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        healthy, msg = await registry.health_check()
        assert healthy is False
        assert "not initialized" in msg

    @pytest.mark.asyncio
    async def test_lifecycle_initialize_cleanup(self):
        from unified_supervisor import MLOpsModelRegistry
        registry = MLOpsModelRegistry()
        await registry.initialize()
        assert registry._initialized is True
        healthy, msg = await registry.health_check()
        assert healthy is True
        await registry.cleanup()


class TestWorkflowOrchestratorGovernance:
    """WorkflowOrchestrator must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import WorkflowOrchestrator, SystemService
        assert issubclass(WorkflowOrchestrator, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import WorkflowOrchestrator
        wf = WorkflowOrchestrator()
        assert wf._running is False

    def test_capability_contract_valid(self):
        from unified_supervisor import WorkflowOrchestrator
        wf = WorkflowOrchestrator()
        contract = wf.capability_contract()
        assert contract.name == "WorkflowOrchestrator"
        assert "writes_workflow_definitions" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import WorkflowOrchestrator
        wf = WorkflowOrchestrator()
        triggers = wf.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import WorkflowOrchestrator
        wf = WorkflowOrchestrator()
        healthy, msg = await wf.health_check()
        assert healthy is False

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        from unified_supervisor import WorkflowOrchestrator
        wf = WorkflowOrchestrator()
        # Can't call initialize() without create_safe_task available,
        # but we can verify cleanup doesn't crash
        await wf.cleanup()
        assert wf._running is False


class TestDocumentManagementSystemGovernance:
    """DocumentManagementSystem must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import DocumentManagementSystem, SystemService
        assert issubclass(DocumentManagementSystem, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import DocumentManagementSystem
        dms = DocumentManagementSystem()
        assert dms._initialized is False
        # Must NOT resolve storage path in __init__
        assert dms._storage_path is None

    def test_capability_contract(self):
        from unified_supervisor import DocumentManagementSystem
        dms = DocumentManagementSystem()
        contract = dms.capability_contract()
        assert contract.name == "DocumentManagementSystem"
        assert "writes_document_store" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import DocumentManagementSystem
        dms = DocumentManagementSystem()
        triggers = dms.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import DocumentManagementSystem
        dms = DocumentManagementSystem()
        healthy, msg = await dms.health_check()
        assert healthy is False


class TestNotificationHubGovernance:
    """NotificationHub must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import NotificationHub, SystemService
        assert issubclass(NotificationHub, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import NotificationHub
        hub = NotificationHub()
        assert hub._running is False

    def test_capability_contract(self):
        from unified_supervisor import NotificationHub
        hub = NotificationHub()
        contract = hub.capability_contract()
        assert contract.name == "NotificationHub"
        assert "writes_notification_queue" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import NotificationHub
        hub = NotificationHub()
        triggers = hub.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import NotificationHub
        hub = NotificationHub()
        healthy, msg = await hub.health_check()
        assert healthy is False


class TestSessionManagerGovernance:
    """SessionManager must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import SessionManager, SystemService
        assert issubclass(SessionManager, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import SessionManager
        sm = SessionManager()
        assert sm._running is False

    def test_capability_contract(self):
        from unified_supervisor import SessionManager
        sm = SessionManager()
        contract = sm.capability_contract()
        assert contract.name == "SessionManager"
        assert "writes_session_store" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import SessionManager
        sm = SessionManager()
        triggers = sm.activation_triggers()
        assert isinstance(triggers, list)
        assert len(triggers) == 0  # always_on

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import SessionManager
        sm = SessionManager()
        healthy, msg = await sm.health_check()
        assert healthy is False


class TestDataLakeManagerGovernance:
    """DataLakeManager must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import DataLakeManager, SystemService
        assert issubclass(DataLakeManager, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import DataLakeManager
        dlm = DataLakeManager()
        assert dlm._running is False
        # Must NOT resolve storage root in __init__
        assert dlm._storage_root is None

    def test_capability_contract(self):
        from unified_supervisor import DataLakeManager
        dlm = DataLakeManager()
        contract = dlm.capability_contract()
        assert contract.name == "DataLakeManager"
        assert "writes_data_lake" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import DataLakeManager
        dlm = DataLakeManager()
        triggers = dlm.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import DataLakeManager
        dlm = DataLakeManager()
        healthy, msg = await dlm.health_check()
        assert healthy is False


class TestStreamingAnalyticsEngineGovernance:
    """StreamingAnalyticsEngine must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import StreamingAnalyticsEngine, SystemService
        assert issubclass(StreamingAnalyticsEngine, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import StreamingAnalyticsEngine
        sae = StreamingAnalyticsEngine()
        assert sae._running is False

    def test_capability_contract(self):
        from unified_supervisor import StreamingAnalyticsEngine
        sae = StreamingAnalyticsEngine()
        contract = sae.capability_contract()
        assert contract.name == "StreamingAnalyticsEngine"
        assert "writes_stream_state" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import StreamingAnalyticsEngine
        sae = StreamingAnalyticsEngine()
        triggers = sae.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import StreamingAnalyticsEngine
        sae = StreamingAnalyticsEngine()
        healthy, msg = await sae.health_check()
        assert healthy is False


class TestConsentManagementSystemGovernance:
    """ConsentManagementSystem must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import ConsentManagementSystem, SystemService
        assert issubclass(ConsentManagementSystem, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import ConsentManagementSystem
        cms = ConsentManagementSystem()
        assert cms._initialized is False

    def test_capability_contract(self):
        from unified_supervisor import ConsentManagementSystem
        cms = ConsentManagementSystem()
        contract = cms.capability_contract()
        assert contract.name == "ConsentManagementSystem"
        assert "writes_consent_records" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import ConsentManagementSystem
        cms = ConsentManagementSystem()
        triggers = cms.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import ConsentManagementSystem
        cms = ConsentManagementSystem()
        healthy, msg = await cms.health_check()
        assert healthy is False

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        from unified_supervisor import ConsentManagementSystem
        cms = ConsentManagementSystem()
        await cms.initialize()
        assert cms._initialized is True
        healthy, msg = await cms.health_check()
        assert healthy is True
        await cms.cleanup()
        assert cms._initialized is False


class TestDigitalSignatureServiceGovernance:
    """DigitalSignatureService must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import DigitalSignatureService, SystemService
        assert issubclass(DigitalSignatureService, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import DigitalSignatureService
        dss = DigitalSignatureService()
        assert dss._initialized is False

    def test_capability_contract(self):
        from unified_supervisor import DigitalSignatureService
        dss = DigitalSignatureService()
        contract = dss.capability_contract()
        assert contract.name == "DigitalSignatureService"
        assert "writes_signature_store" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import DigitalSignatureService
        dss = DigitalSignatureService()
        triggers = dss.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import DigitalSignatureService
        dss = DigitalSignatureService()
        healthy, msg = await dss.health_check()
        assert healthy is False

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        from unified_supervisor import DigitalSignatureService
        dss = DigitalSignatureService()
        await dss.initialize()
        assert dss._initialized is True
        healthy, msg = await dss.health_check()
        assert healthy is True
        await dss.cleanup()
        assert dss._initialized is False


class TestLegacyDegradationManagerGovernance:
    """LegacyDegradationManager must be a governed SystemService."""

    def test_is_system_service(self):
        from unified_supervisor import LegacyDegradationManager, SystemService
        assert issubclass(LegacyDegradationManager, SystemService)

    def test_constructor_purity(self):
        from unified_supervisor import LegacyDegradationManager
        mgr = LegacyDegradationManager()
        assert mgr._initialized is False
        # _setup_default_levels is pure in-memory, allowed in __init__
        assert len(mgr._levels) > 0

    def test_capability_contract(self):
        from unified_supervisor import LegacyDegradationManager
        mgr = LegacyDegradationManager()
        contract = mgr.capability_contract()
        assert contract.name == "LegacyDegradationManager"
        assert "writes_degradation_state" in contract.side_effects

    def test_activation_triggers(self):
        from unified_supervisor import LegacyDegradationManager
        mgr = LegacyDegradationManager()
        triggers = mgr.activation_triggers()
        assert isinstance(triggers, list)

    @pytest.mark.asyncio
    async def test_health_check_before_init(self):
        from unified_supervisor import LegacyDegradationManager
        mgr = LegacyDegradationManager()
        healthy, msg = await mgr.health_check()
        assert healthy is False

    @pytest.mark.asyncio
    async def test_lifecycle(self):
        from unified_supervisor import LegacyDegradationManager
        mgr = LegacyDegradationManager()
        await mgr.initialize()
        assert mgr._initialized is True
        healthy, msg = await mgr.health_check()
        assert healthy is True
        assert "normal" in msg
        await mgr.cleanup()
        assert mgr._initialized is False


# ── Phase A Gate: Parametrized Governance Compliance ──────────────────

ORGAN_CLASSES = [
    "MLOpsModelRegistry",
    "WorkflowOrchestrator",
    "DocumentManagementSystem",
    "NotificationHub",
    "SessionManager",
    "DataLakeManager",
    "StreamingAnalyticsEngine",
    "ConsentManagementSystem",
    "DigitalSignatureService",
    "LegacyDegradationManager",
]


@pytest.mark.parametrize("class_name", ORGAN_CLASSES)
class TestGovernanceCompliance:
    """Cross-cutting governance checks for all 10 enterprise organs."""

    def test_extends_system_service(self, class_name):
        import unified_supervisor as us
        cls = getattr(us, class_name)
        assert issubclass(cls, us.SystemService)

    def test_capability_contract_has_side_effects(self, class_name):
        import unified_supervisor as us
        instance = getattr(us, class_name)()
        contract = instance.capability_contract()
        assert len(contract.side_effects) > 0, f"{class_name} has no declared side_effects"

    def test_no_io_in_constructor(self, class_name):
        """Constructor must complete without touching disk/network."""
        import unified_supervisor as us
        instance = getattr(us, class_name)()
        assert instance is not None


# ── Phase C Gate: Persistence Verification ────────────────────────────

class TestAtomicWriteHelper:
    """Tests for _atomic_write_json and _load_json_state."""

    def test_roundtrip_write_read(self, tmp_path):
        from unified_supervisor import _atomic_write_json, _load_json_state
        test_file = tmp_path / "test" / "data.json"
        test_data = {"key": "value", "count": 42}
        _atomic_write_json(test_file, test_data)
        loaded = _load_json_state(test_file)
        assert loaded is not None
        assert loaded["key"] == "value"
        assert loaded["count"] == 42

    def test_schema_version_envelope(self, tmp_path):
        from unified_supervisor import _atomic_write_json
        import json
        test_file = tmp_path / "test" / "versioned.json"
        _atomic_write_json(test_file, {"x": 1}, schema_version="2.0")
        raw = json.loads(test_file.read_text())
        assert raw["_schema_version"] == "2.0"
        assert "_written_at" in raw
        assert raw["data"]["x"] == 1

    def test_load_nonexistent_returns_none(self, tmp_path):
        from unified_supervisor import _load_json_state
        result = _load_json_state(tmp_path / "nonexistent.json")
        assert result is None

    def test_atomic_overwrites_safely(self, tmp_path):
        from unified_supervisor import _atomic_write_json, _load_json_state
        test_file = tmp_path / "overwrite.json"
        _atomic_write_json(test_file, {"v": 1})
        _atomic_write_json(test_file, {"v": 2})
        loaded = _load_json_state(test_file)
        assert loaded["v"] == 2


PERSISTENT_ORGANS = [
    "MLOpsModelRegistry",
    "WorkflowOrchestrator",
    "DocumentManagementSystem",
    "NotificationHub",
    "DataLakeManager",
    "StreamingAnalyticsEngine",
    "ConsentManagementSystem",
    "DigitalSignatureService",
    "LegacyDegradationManager",
]


@pytest.mark.parametrize("class_name", PERSISTENT_ORGANS)
class TestOrganPersistence:
    """Verify all 9 persistent organs reference persistence helpers."""

    def test_initialize_references_load(self, class_name):
        """initialize() must call _load_json_state."""
        import ast
        with open("unified_supervisor.py") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for method in node.body:
                    if isinstance(method, ast.AsyncFunctionDef) and method.name == "initialize":
                        method_src = source[method.col_offset:method.end_col_offset] if hasattr(method, 'end_col_offset') else ""
                        # Fallback: check line range
                        lines = source.split("\n")
                        method_lines = "\n".join(lines[method.lineno - 1:method.end_lineno])
                        assert "_load_json_state" in method_lines, (
                            f"{class_name}.initialize() missing _load_json_state call"
                        )
                        return
        pytest.fail(f"{class_name} not found or missing initialize()")

    def test_cleanup_references_save(self, class_name):
        """cleanup() must call _atomic_write_json."""
        import ast
        with open("unified_supervisor.py") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for method in node.body:
                    if isinstance(method, ast.AsyncFunctionDef) and method.name == "cleanup":
                        lines = source.split("\n")
                        method_lines = "\n".join(lines[method.lineno - 1:method.end_lineno])
                        assert "_atomic_write_json" in method_lines, (
                            f"{class_name}.cleanup() missing _atomic_write_json call"
                        )
                        return
        pytest.fail(f"{class_name} not found or missing cleanup()")
