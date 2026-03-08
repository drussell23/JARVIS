"""tests/governance/comms/test_narrator_script.py"""
import pytest


class TestNarratorScript:
    def test_format_signal_detected(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("signal_detected", {
            "test_count": 2,
            "file": "tests/test_utils.py",
        })
        assert "test_utils.py" in text
        assert "2" in text

    def test_format_generating(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("generating", {
            "file": "tests/test_utils.py",
            "provider": "gcp-jprime",
        })
        assert "test_utils.py" in text
        assert "gcp-jprime" in text

    def test_format_approve(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("approve", {
            "file": "prime_client.py",
            "goal": "fix connection timeout",
            "op_id": "op-047",
        })
        assert "prime_client.py" in text
        assert "op-047" in text

    def test_format_applied(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("applied", {"file": "test_utils.py"})
        assert "test_utils.py" in text

    def test_format_postmortem(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("postmortem", {
            "file": "api_handler.py",
            "reason": "AST parse failed",
        })
        assert "api_handler.py" in text
        assert "AST parse failed" in text

    def test_format_observe_error(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("observe_error", {
            "file": "prime_client.py",
            "error_summary": "ConnectionTimeout at line 342",
        })
        assert "prime_client.py" in text

    def test_unknown_phase_returns_fallback(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("unknown_phase", {"op_id": "op-999"})
        assert text  # non-empty fallback
        assert "op-999" in text

    def test_missing_placeholder_does_not_crash(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        # "signal_detected" expects {test_count} and {file} but we omit them
        text = format_narration("signal_detected", {})
        assert isinstance(text, str)  # graceful degradation
