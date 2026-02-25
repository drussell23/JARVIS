# tests/unit/core/test_control_plane_client.py
"""Tests for ControlPlaneClient — subscriber and handshake responder."""

import pytest


class TestClientImport:
    def test_module_imports(self):
        from backend.core.control_plane_client import ControlPlaneSubscriber
        assert ControlPlaneSubscriber is not None

    def test_required_exports(self):
        import backend.core.control_plane_client as mod
        assert hasattr(mod, "ControlPlaneSubscriber")
        assert hasattr(mod, "HandshakeResponder")


class TestHandshakeResponder:
    def test_creates_response(self):
        from backend.core.control_plane_client import HandshakeResponder
        responder = HandshakeResponder(
            api_version="1.2.0",
            capabilities=["inference", "embedding"],
            instance_id="prime:8001:abc",
        )
        proposal = {
            "supervisor_epoch": 1,
            "required_capabilities": ["inference"],
            "heartbeat_interval_s": 10.0,
            "heartbeat_ttl_s": 30.0,
        }
        response = responder.handle_handshake(proposal)
        assert response["accepted"] is True
        assert response["api_version"] == "1.2.0"
        assert "inference" in response["capabilities"]

    def test_rejects_missing_capability(self):
        from backend.core.control_plane_client import HandshakeResponder
        responder = HandshakeResponder(
            api_version="1.0.0",
            capabilities=["inference"],
            instance_id="prime:8001:abc",
        )
        proposal = {
            "supervisor_epoch": 1,
            "required_capabilities": ["inference", "training"],
            "heartbeat_interval_s": 10.0,
            "heartbeat_ttl_s": 30.0,
        }
        response = responder.handle_handshake(proposal)
        # Responder should still accept — it's the supervisor that rejects
        # Component reports what it has; supervisor evaluates compatibility
        assert response["accepted"] is True
        assert "training" not in response["capabilities"]

    def test_includes_instance_id(self):
        from backend.core.control_plane_client import HandshakeResponder
        responder = HandshakeResponder(
            api_version="1.0.0",
            capabilities=["inference"],
            instance_id="prime:8001:abc",
        )
        proposal = {
            "supervisor_epoch": 1,
            "required_capabilities": [],
            "heartbeat_interval_s": 10.0,
            "heartbeat_ttl_s": 30.0,
        }
        response = responder.handle_handshake(proposal)
        assert response["component_instance_id"] == "prime:8001:abc"

    def test_includes_health_schema_hash(self):
        from backend.core.control_plane_client import HandshakeResponder
        responder = HandshakeResponder(
            api_version="1.0.0",
            capabilities=["inference"],
            instance_id="prime:8001:abc",
            health_schema_hash="sha256:abc123",
        )
        proposal = {
            "supervisor_epoch": 1,
            "required_capabilities": [],
            "heartbeat_interval_s": 10.0,
            "heartbeat_ttl_s": 30.0,
        }
        response = responder.handle_handshake(proposal)
        assert response["health_schema_hash"] == "sha256:abc123"


class TestControlPlaneSubscriber:
    def test_subscriber_init(self):
        from backend.core.control_plane_client import ControlPlaneSubscriber
        sub = ControlPlaneSubscriber(
            subscriber_id="prime_sub_1",
            sock_path="/tmp/test.sock",
        )
        assert sub.subscriber_id == "prime_sub_1"
        assert sub.last_seen_seq == 0

    def test_subscriber_event_callback_registration(self):
        from backend.core.control_plane_client import ControlPlaneSubscriber
        sub = ControlPlaneSubscriber(
            subscriber_id="test_sub",
            sock_path="/tmp/test.sock",
        )
        callback_called = []
        sub.on_event(lambda event: callback_called.append(event))
        # Simulate receiving an event
        sub._dispatch_event({"type": "event", "seq": 1, "action": "start", "target": "prime"})
        assert len(callback_called) == 1
        assert callback_called[0]["seq"] == 1
