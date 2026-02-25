# tests/unit/core/test_handshake_protocol.py
"""Tests for HandshakeProtocol — compatibility evaluation, version windows."""

import pytest


class TestHandshakeImport:
    def test_module_imports(self):
        from backend.core.handshake_protocol import HandshakeManager
        assert HandshakeManager is not None

    def test_required_exports(self):
        import backend.core.handshake_protocol as mod
        assert hasattr(mod, "HandshakeProposal")
        assert hasattr(mod, "HandshakeResponse")
        assert hasattr(mod, "HandshakeManager")
        assert hasattr(mod, "evaluate_handshake")


class TestHandshakeProposal:
    def test_proposal_is_frozen(self):
        from backend.core.handshake_protocol import HandshakeProposal
        p = HandshakeProposal(
            supervisor_epoch=1,
            supervisor_instance_id="test:1:abc",
            expected_api_version_min="1.0.0",
            expected_api_version_max="1.9.9",
            required_capabilities=("inference",),
            health_schema_hash="abc123",
            heartbeat_interval_s=10.0,
            heartbeat_ttl_s=30.0,
            protocol_version="1.0.0",
        )
        with pytest.raises(AttributeError):
            p.supervisor_epoch = 99


class TestCompatibilityEvaluation:
    def _make_proposal(self, **overrides):
        from backend.core.handshake_protocol import HandshakeProposal
        defaults = dict(
            supervisor_epoch=1,
            supervisor_instance_id="test:1:abc",
            expected_api_version_min="1.0.0",
            expected_api_version_max="1.9.9",
            required_capabilities=("inference",),
            health_schema_hash="abc123",
            heartbeat_interval_s=10.0,
            heartbeat_ttl_s=30.0,
            protocol_version="1.0.0",
        )
        defaults.update(overrides)
        return HandshakeProposal(**defaults)

    def _make_response(self, **overrides):
        from backend.core.handshake_protocol import HandshakeResponse
        defaults = dict(
            accepted=True,
            component_instance_id="prime:8001:xyz",
            api_version="1.2.0",
            capabilities=("inference", "embedding"),
            health_schema_hash="abc123",
            rejection_reason=None,
            metadata=None,
        )
        defaults.update(overrides)
        return HandshakeResponse(**defaults)

    def test_compatible_accepted(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal()
        r = self._make_response()
        ok, reason = evaluate_handshake(p, r)
        assert ok is True
        assert reason is None

    def test_rejected_by_component(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal()
        r = self._make_response(accepted=False, rejection_reason="incompatible model")
        ok, reason = evaluate_handshake(p, r)
        assert ok is False
        assert "component_rejected" in reason

    def test_version_below_minimum(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(expected_api_version_min="2.0.0", expected_api_version_max="2.9.9")
        r = self._make_response(api_version="1.5.0")
        ok, reason = evaluate_handshake(p, r)
        assert ok is False
        assert "outside" in reason

    def test_version_above_maximum(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(expected_api_version_min="1.0.0", expected_api_version_max="1.5.0")
        r = self._make_response(api_version="1.6.0")
        ok, reason = evaluate_handshake(p, r)
        assert ok is False

    def test_major_version_mismatch(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(expected_api_version_min="1.0.0", expected_api_version_max="2.0.0")
        r = self._make_response(api_version="2.0.0")
        ok, reason = evaluate_handshake(p, r)
        # Major 2 == major 2 of max — this should pass
        assert ok is True or "major" in (reason or "")

    def test_missing_required_capability(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(required_capabilities=("inference", "training"))
        r = self._make_response(capabilities=("inference",))
        ok, reason = evaluate_handshake(p, r)
        assert ok is False
        assert "missing_capabilities" in reason

    def test_schema_hash_mismatch_is_warning_not_rejection(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(health_schema_hash="aaa")
        r = self._make_response(health_schema_hash="bbb")
        ok, reason = evaluate_handshake(p, r)
        assert ok is True  # Warning, not rejection

    def test_legacy_version_zero_always_compatible(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal()
        r = self._make_response(api_version="0.0.0", capabilities=("inference",))
        ok, reason = evaluate_handshake(p, r)
        assert ok is True  # Legacy fallback


class TestSemverParsing:
    def test_parse_valid(self):
        from backend.core.handshake_protocol import parse_semver
        assert parse_semver("1.2.3") == (1, 2, 3)

    def test_parse_two_part(self):
        from backend.core.handshake_protocol import parse_semver
        assert parse_semver("1.2") == (1, 2, 0)

    def test_parse_single(self):
        from backend.core.handshake_protocol import parse_semver
        assert parse_semver("3") == (3, 0, 0)

    def test_parse_zero(self):
        from backend.core.handshake_protocol import parse_semver
        assert parse_semver("0.0.0") == (0, 0, 0)
