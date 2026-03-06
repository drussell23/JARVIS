"""Tests for J-Prime structured feature extraction."""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.extraction import (
    extract_features,
    _heuristic_features,
    _build_extraction_prompt,
)
from autonomy.email_triage.schemas import EmailFeatures
from autonomy.email_triage.config import TriageConfig


def _sample_email():
    return {
        "id": "msg_001",
        "thread_id": "t_001",
        "from": "Alice Smith <alice@company.com>",
        "to": "derek@example.com",
        "subject": "Re: Q4 Budget Review - Action Required",
        "date": "Mon, 1 Mar 2026 10:00:00 -0800",
        "snippet": "Hi Derek, please review the attached budget report by Friday.",
        "labels": ["INBOX", "UNREAD", "IMPORTANT"],
    }


class TestHeuristicFeatures:
    """Heuristic extraction works without any AI."""

    def test_basic_extraction(self):
        email = _sample_email()
        f = _heuristic_features(email)
        assert isinstance(f, EmailFeatures)
        assert f.message_id == "msg_001"
        assert f.sender == "Alice Smith <alice@company.com>"
        assert f.sender_domain == "company.com"
        assert f.is_reply is True
        assert "INBOX" in f.label_ids

    def test_sender_domain_extraction(self):
        email = _sample_email()
        email["from"] = "bob@sub.domain.org"
        f = _heuristic_features(email)
        assert f.sender_domain == "sub.domain.org"

    def test_empty_email_fields(self):
        email = {"id": "m1", "from": "", "subject": "", "snippet": "", "labels": []}
        f = _heuristic_features(email)
        assert f.message_id == "m1"
        assert f.sender_domain == ""
        assert f.extraction_confidence == 0.0

    def test_subject_urgency_keywords_detected(self):
        email = _sample_email()
        email["subject"] = "URGENT: Server is down"
        f = _heuristic_features(email)
        assert any("urgent" in k.lower() for k in f.keywords)


class TestBuildExtractionPrompt:
    """Prompt construction for J-Prime."""

    def test_prompt_contains_email_fields(self):
        email = _sample_email()
        prompt = _build_extraction_prompt(email)
        assert "alice@company.com" in prompt
        assert "Q4 Budget Review" in prompt

    def test_prompt_requests_json(self):
        email = _sample_email()
        prompt = _build_extraction_prompt(email)
        assert "JSON" in prompt or "json" in prompt


class TestExtractFeatures:
    """Full extraction with J-Prime + fallback."""

    @pytest.mark.asyncio
    async def test_successful_extraction(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=True)

        mock_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "keywords": ["budget", "review", "action_required"],
            "sender_frequency": "frequent",
            "urgency_signals": ["deadline", "action_required"],
        })
        mock_router.generate.return_value = mock_response

        f = await extract_features(email, mock_router, config=config)
        assert f.extraction_confidence > 0.0
        assert "budget" in f.keywords
        assert "deadline" in f.urgency_signals

    @pytest.mark.asyncio
    async def test_fallback_on_router_failure(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=True)

        mock_router = AsyncMock()
        mock_router.generate.side_effect = RuntimeError("Router down")

        f = await extract_features(email, mock_router, config=config)
        assert f.extraction_confidence == 0.0
        assert f.message_id == "msg_001"

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=True)

        mock_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "This is not JSON at all"
        mock_router.generate.return_value = mock_response

        f = await extract_features(email, mock_router, config=config)
        assert f.extraction_confidence == 0.0

    @pytest.mark.asyncio
    async def test_degraded_response_uses_normalized_metadata_details(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=True)

        mock_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.source = "degraded"
        mock_response.metadata = {
            "error_code": "timeout",
            "error_message": "Timeout after 1.0s",
            "origin_layer": "trinity_ultra_coordinator",
        }
        mock_router.generate.return_value = mock_response

        with patch("autonomy.email_triage.extraction.emit_triage_event") as mock_emit:
            f = await extract_features(email, mock_router, config=config)

        assert f.extraction_confidence == 0.0
        assert mock_emit.call_args[0][1]["details"] == ["timeout", "Timeout after 1.0s"]

    @pytest.mark.asyncio
    async def test_degraded_response_uses_legacy_reason_fallback(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=True)

        mock_router = AsyncMock()
        mock_response = MagicMock()
        mock_response.source = "degraded"
        mock_response.metadata = {"reason": "no_backend_available"}
        mock_router.generate.return_value = mock_response

        with patch("autonomy.email_triage.extraction.emit_triage_event") as mock_emit:
            f = await extract_features(email, mock_router, config=config)

        assert f.extraction_confidence == 0.0
        assert mock_emit.call_args[0][1]["details"] == ["no_backend_available"]

    @pytest.mark.asyncio
    async def test_extraction_disabled_uses_heuristic(self):
        email = _sample_email()
        config = TriageConfig(extraction_enabled=False)

        mock_router = AsyncMock()
        f = await extract_features(email, mock_router, config=config)
        assert f.extraction_confidence == 0.0
        mock_router.generate.assert_not_called()
