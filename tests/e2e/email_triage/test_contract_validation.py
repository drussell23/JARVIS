"""Tests for J-Prime extraction contract validation (WS3).

Validates that extraction correctly handles:
- Well-formed contracts (accepted)
- Missing required fields (degraded fallback)
- Invalid field values (degraded fallback)
- Unknown signals (accepted with warnings)
- Extra fields (ignored)
- Extraction source tracking
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

from autonomy.email_triage.extraction import (
    _validate_extraction_contract,
    extract_features,
)

# Import conftest helpers via the test directory (pytest adds it to sys.path)
_conftest_dir = os.path.dirname(__file__)
if _conftest_dir not in sys.path:
    sys.path.insert(0, _conftest_dir)
from conftest import make_raw_email, make_triage_config


class TestContractValidation:
    """Unit tests for _validate_extraction_contract."""

    def test_valid_jprime_contract_accepted(self):
        """Well-formed JSON passes validation with no warnings."""
        data = {
            "keywords": ["deployment", "production"],
            "sender_frequency": "frequent",
            "urgency_signals": ["action_required", "escalation"],
        }
        valid, warnings = _validate_extraction_contract(data)
        assert valid is True
        assert warnings == []

    def test_missing_required_field_rejects(self):
        """Missing 'keywords' field fails validation."""
        data = {
            "sender_frequency": "frequent",
            "urgency_signals": ["deadline"],
        }
        valid, warnings = _validate_extraction_contract(data)
        assert valid is False
        assert any("keywords" in w for w in warnings)

    def test_invalid_sender_frequency_rejects(self):
        """Invalid sender_frequency value fails validation."""
        data = {
            "keywords": ["test"],
            "sender_frequency": "rarely",
            "urgency_signals": [],
        }
        valid, warnings = _validate_extraction_contract(data)
        assert valid is False
        assert any("sender_frequency" in w for w in warnings)

    def test_unknown_urgency_signals_warned_but_accepted(self):
        """Unknown urgency signals are accepted with a warning."""
        data = {
            "keywords": ["test"],
            "sender_frequency": "occasional",
            "urgency_signals": ["deadline", "custom_unknown_signal"],
        }
        valid, warnings = _validate_extraction_contract(data)
        assert valid is True
        assert len(warnings) == 1
        assert "unknown" in warnings[0].lower()

    def test_extra_fields_ignored(self):
        """Extra JSON fields don't affect validation."""
        data = {
            "keywords": ["test"],
            "sender_frequency": "first_time",
            "urgency_signals": [],
            "extra_field": "should be ignored",
            "another_extra": 42,
        }
        valid, warnings = _validate_extraction_contract(data)
        assert valid is True

    def test_keywords_not_list_rejects(self):
        """keywords as a string (not list) fails validation."""
        data = {
            "keywords": "single_keyword",
            "sender_frequency": "frequent",
            "urgency_signals": [],
        }
        valid, warnings = _validate_extraction_contract(data)
        assert valid is False


class TestExtractionSourceTracking:
    """Verify extraction_source and extraction_contract_version are set."""

    @pytest.mark.asyncio
    async def test_heuristic_extraction_source(self):
        """Heuristic-only extraction sets source='heuristic'."""
        config = make_triage_config(extraction_enabled=False)
        email = make_raw_email()
        features = await extract_features(email, router=None, config=config)
        assert features.extraction_source == "heuristic"
        assert features.extraction_contract_version == ""

    @pytest.mark.asyncio
    async def test_jprime_valid_extraction_source(self):
        """Valid J-Prime extraction sets source='jprime_v1' and contract version."""
        config = make_triage_config(extraction_enabled=True)
        email = make_raw_email()

        response_json = {
            "keywords": ["deployment", "production"],
            "sender_frequency": "frequent",
            "urgency_signals": ["action_required"],
        }
        mock_response = MagicMock()
        mock_response.content = json.dumps(response_json)
        router = MagicMock()
        router.generate = AsyncMock(return_value=mock_response)

        features = await extract_features(email, router=router, config=config)
        assert features.extraction_source == "jprime_v1"
        assert features.extraction_contract_version == "1.0"
        assert features.extraction_confidence == 0.8

    @pytest.mark.asyncio
    async def test_jprime_invalid_contract_degrades(self):
        """Invalid J-Prime response degrades to heuristic with event emitted."""
        config = make_triage_config(extraction_enabled=True)
        email = make_raw_email(id="msg_degrade_test")

        # Missing required field
        response_json = {
            "sender_frequency": "frequent",
            "urgency_signals": ["deadline"],
        }
        mock_response = MagicMock()
        mock_response.content = json.dumps(response_json)
        router = MagicMock()
        router.generate = AsyncMock(return_value=mock_response)

        with patch(
            "autonomy.email_triage.extraction.emit_triage_event"
        ) as mock_emit:
            features = await extract_features(email, router=router, config=config)
            # Should have emitted degradation event
            mock_emit.assert_called_once()
            event_type = mock_emit.call_args[0][0]
            assert event_type == "extraction_degraded"
            payload = mock_emit.call_args[0][1]
            assert payload["reason"] == "contract_validation_failed"

        assert features.extraction_source == "jprime_degraded_fallback"
        assert features.extraction_confidence == 0.0

    @pytest.mark.asyncio
    async def test_jprime_json_parse_failure_degrades(self):
        """Invalid JSON from J-Prime degrades gracefully."""
        config = make_triage_config(extraction_enabled=True)
        email = make_raw_email()

        mock_response = MagicMock()
        mock_response.content = "not valid json {{"
        router = MagicMock()
        router.generate = AsyncMock(return_value=mock_response)

        with patch(
            "autonomy.email_triage.extraction.emit_triage_event"
        ) as mock_emit:
            features = await extract_features(email, router=router, config=config)
            mock_emit.assert_called_once()
            event_type = mock_emit.call_args[0][0]
            assert event_type == "extraction_degraded"

        assert features.extraction_source == "heuristic"
        assert features.extraction_confidence == 0.0

    @pytest.mark.asyncio
    async def test_jprime_exception_degrades(self):
        """Router exception degrades gracefully."""
        config = make_triage_config(extraction_enabled=True)
        email = make_raw_email()

        router = MagicMock()
        router.generate = AsyncMock(side_effect=RuntimeError("Router down"))

        with patch(
            "autonomy.email_triage.extraction.emit_triage_event"
        ) as mock_emit:
            features = await extract_features(email, router=router, config=config)
            mock_emit.assert_called_once()

        assert features.extraction_source == "heuristic"
