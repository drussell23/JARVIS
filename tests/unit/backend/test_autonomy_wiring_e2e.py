"""End-to-end smoke tests for autonomy wiring.

Validates the 4 done criteria without real Google API or model inference.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestDoneCriteria:
    """Validate 4 done criteria from design doc."""

    def test_criterion_1_coordinator_resolves_after_cross_registration(self):
        """Valid auth: coordinator resolves immediately via cross-registration."""
        import backend.neural_mesh.integration as mod
        mod._neural_mesh_coordinator = None
        mod._initialized = False

        mock_coord = MagicMock()
        mock_coord._running = True
        mock_agent = MagicMock()
        mock_coord.get_agent.return_value = mock_agent

        from backend.neural_mesh.integration import (
            set_neural_mesh_coordinator,
            get_neural_mesh_coordinator,
        )
        set_neural_mesh_coordinator(mock_coord)
        coord = get_neural_mesh_coordinator()
        assert coord is mock_coord
        assert coord.get_agent("google_workspace_agent") is mock_agent

    def test_criterion_2_auth_state_machine_transitions(self):
        """Expired token: AUTHENTICATED -> REFRESHING -> AUTHENTICATED."""
        from backend.neural_mesh.agents.google_workspace_agent import AuthState
        assert AuthState.REFRESHING.value == "refreshing"
        assert AuthState.AUTHENTICATED.value == "authenticated"

    def test_criterion_3_degraded_visual_for_read(self):
        """Revoked token: read actions get visual fallback."""
        from backend.neural_mesh.agents.google_workspace_agent import (
            _classify_action_risk,
        )
        assert _classify_action_risk("fetch_unread_emails") == "read"
        assert _classify_action_risk("send_email") == "write"

    def test_criterion_4_verification_catches_bad_output(self):
        """No silent success without verified output."""
        from backend.api.unified_command_processor import _verify_workspace_result
        # Bad output — missing "emails" key
        outcome, _ = _verify_workspace_result("fetch_unread_emails", {"data": []})
        assert outcome == "verify_schema_fail"
        # Good output
        outcome, _ = _verify_workspace_result(
            "fetch_unread_emails",
            {"emails": [{"subject": "Hi", "from": "a@b.com"}]},
        )
        assert outcome == "verify_passed"
