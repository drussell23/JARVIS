"""Structured feature extraction from raw email dicts.

Two-tier extraction:
1. Heuristic (always available) -- parse subject, sender, labels
2. J-Prime structured (when available) -- AI-powered keyword/urgency extraction

Falls back to heuristic-only if J-Prime is unavailable or returns bad JSON.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.events import EVENT_EXTRACTION_DEGRADED, emit_triage_event
from autonomy.email_triage.schemas import EmailFeatures

logger = logging.getLogger("jarvis.email_triage.extraction")

# Heuristic urgency keywords
_HEURISTIC_URGENCY = {
    "urgent", "critical", "immediate", "asap", "emergency",
    "action required", "time-sensitive", "deadline", "due today",
}

# Contract validation constants (WS3)
_EXTRACTION_CONTRACT_VERSION = "1.0"
_REQUIRED_FIELDS = {"keywords", "sender_frequency", "urgency_signals"}
_VALID_SENDER_FREQ = frozenset({"first_time", "occasional", "frequent"})
_VALID_URGENCY_SIGNALS = frozenset({
    "deadline", "action_required", "escalation", "time_sensitive", "follow_up",
})
_MAX_KEYWORDS = 10


def _validate_extraction_contract(
    data: Dict[str, Any],
) -> Tuple[bool, list]:
    """Validate a J-Prime extraction response against the v1.0 contract.

    Returns (valid, warnings) where warnings are non-fatal issues.
    """
    warnings = []

    # Required fields
    for field in _REQUIRED_FIELDS:
        if field not in data:
            return False, [f"missing required field: {field}"]

    # keywords: list of strings, max length
    kw = data["keywords"]
    if not isinstance(kw, list):
        return False, ["keywords is not a list"]
    if not all(isinstance(k, str) for k in kw):
        return False, ["keywords contains non-string entries"]
    if len(kw) > _MAX_KEYWORDS:
        warnings.append(f"keywords truncated from {len(kw)} to {_MAX_KEYWORDS}")

    # sender_frequency: must be in valid set
    sf = data["sender_frequency"]
    if sf not in _VALID_SENDER_FREQ:
        return False, [f"invalid sender_frequency: {sf!r}"]

    # urgency_signals: list of strings, warn on unknown
    us = data["urgency_signals"]
    if not isinstance(us, list):
        return False, ["urgency_signals is not a list"]
    unknown = [s for s in us if isinstance(s, str) and s not in _VALID_URGENCY_SIGNALS]
    if unknown:
        warnings.append(f"unknown urgency_signals: {unknown}")

    return True, warnings


_EXTRACTION_SYSTEM_PROMPT = (
    "You are an email classification assistant. Analyze the email and return "
    "ONLY a JSON object with these fields:\n"
    '  "keywords": list of relevant topic keywords (max 5)\n'
    '  "sender_frequency": "first_time" | "occasional" | "frequent"\n'
    '  "urgency_signals": list from ["deadline", "action_required", '
    '"escalation", "time_sensitive", "follow_up"]\n'
    "Return ONLY valid JSON, no markdown, no explanation."
)


def _extract_domain(sender: str) -> str:
    """Extract domain from sender string like 'Name <user@domain.com>'."""
    match = re.search(r"@([\w.-]+)", sender)
    return match.group(1) if match else ""


def _detect_reply(subject: str) -> bool:
    """Detect if subject indicates a reply thread."""
    return bool(re.match(r"^(Re|RE|Fwd|FWD):\s", subject))


def _heuristic_keywords(subject: str, snippet: str) -> Tuple[str, ...]:
    """Extract keywords from subject/snippet using heuristics."""
    text = f"{subject} {snippet}".lower()
    found = []
    for kw in _HEURISTIC_URGENCY:
        if kw in text:
            found.append(kw)
    return tuple(found)


def _heuristic_features(email_dict: Dict[str, Any]) -> EmailFeatures:
    """Build features using only heuristic parsing (no AI)."""
    sender = email_dict.get("from", "")
    subject = email_dict.get("subject", "")
    snippet = email_dict.get("snippet", "")
    labels = email_dict.get("labels", [])

    keywords = _heuristic_keywords(subject, snippet)
    urgency_signals = tuple(
        kw for kw in keywords
        if kw in {"deadline", "action required", "urgent", "critical", "emergency"}
    )

    return EmailFeatures(
        message_id=email_dict.get("id", ""),
        sender=sender,
        sender_domain=_extract_domain(sender),
        subject=subject,
        snippet=snippet,
        is_reply=_detect_reply(subject),
        has_attachment=False,  # Not available in list() metadata
        label_ids=tuple(labels),
        keywords=keywords,
        sender_frequency="first_time",  # Can't determine without history
        urgency_signals=urgency_signals,
        extraction_confidence=0.0,
    )


def _build_extraction_prompt(email_dict: Dict[str, Any]) -> str:
    """Build the prompt for J-Prime structured extraction."""
    return (
        f"Analyze this email:\n"
        f"From: {email_dict.get('from', '')}\n"
        f"Subject: {email_dict.get('subject', '')}\n"
        f"Snippet: {email_dict.get('snippet', '')}\n"
        f"Labels: {', '.join(email_dict.get('labels', []))}\n\n"
        f"Return a JSON object with keywords, sender_frequency, and urgency_signals."
    )


def _merge_features(
    heuristic: EmailFeatures,
    ai_data: Dict[str, Any],
    extraction_source: str = "jprime_v1",
) -> EmailFeatures:
    """Merge AI extraction results into heuristic features."""
    keywords = tuple(ai_data.get("keywords", []))[:_MAX_KEYWORDS] or heuristic.keywords
    sender_freq = ai_data.get("sender_frequency", heuristic.sender_frequency)
    urgency = tuple(ai_data.get("urgency_signals", [])) or heuristic.urgency_signals

    if sender_freq not in _VALID_SENDER_FREQ:
        sender_freq = heuristic.sender_frequency

    return EmailFeatures(
        message_id=heuristic.message_id,
        sender=heuristic.sender,
        sender_domain=heuristic.sender_domain,
        subject=heuristic.subject,
        snippet=heuristic.snippet,
        is_reply=heuristic.is_reply,
        has_attachment=heuristic.has_attachment,
        label_ids=heuristic.label_ids,
        keywords=keywords,
        sender_frequency=sender_freq,
        urgency_signals=urgency,
        extraction_confidence=0.8,
        extraction_source=extraction_source,
        extraction_contract_version=_EXTRACTION_CONTRACT_VERSION if extraction_source == "jprime_v1" else "",
    )


async def extract_features(
    email_dict: Dict[str, Any],
    router: Any,
    deadline: Optional[float] = None,
    config: Optional[TriageConfig] = None,
) -> EmailFeatures:
    """Extract structured features from a raw email dict.

    Args:
        email_dict: Raw email from Gmail API (id, from, subject, snippet, labels).
        router: PrimeRouter instance for AI extraction.
        deadline: Optional monotonic deadline.
        config: Triage config (defaults to singleton).

    Returns:
        EmailFeatures with heuristic + optional AI enrichment.
    """
    config = config or get_triage_config()
    heuristic = _heuristic_features(email_dict)

    if not config.extraction_enabled:
        return heuristic

    try:
        prompt = _build_extraction_prompt(email_dict)
        response = await router.generate(
            prompt=prompt,
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            max_tokens=512,
            temperature=0.0,
            deadline=deadline,
        )
        parsed = json.loads(response.content)

        # Contract validation (WS3)
        valid, warnings = _validate_extraction_contract(parsed)
        if warnings:
            logger.debug("Extraction contract warnings: %s", warnings)
        if not valid:
            emit_triage_event(EVENT_EXTRACTION_DEGRADED, {
                "message_id": email_dict.get("id", ""),
                "reason": "contract_validation_failed",
                "details": warnings,
            })
            return EmailFeatures(
                message_id=heuristic.message_id,
                sender=heuristic.sender,
                sender_domain=heuristic.sender_domain,
                subject=heuristic.subject,
                snippet=heuristic.snippet,
                is_reply=heuristic.is_reply,
                has_attachment=heuristic.has_attachment,
                label_ids=heuristic.label_ids,
                keywords=heuristic.keywords,
                sender_frequency=heuristic.sender_frequency,
                urgency_signals=heuristic.urgency_signals,
                extraction_confidence=0.0,
                extraction_source="jprime_degraded_fallback",
            )

        return _merge_features(heuristic, parsed, extraction_source="jprime_v1")
    except (json.JSONDecodeError, AttributeError) as e:
        logger.debug("Extraction JSON parse failed: %s", e)
        emit_triage_event(EVENT_EXTRACTION_DEGRADED, {
            "message_id": email_dict.get("id", ""),
            "reason": "json_parse_failed",
            "details": [str(e)],
        })
        return heuristic
    except Exception as e:
        logger.warning("Extraction failed, using heuristic: %s", e)
        emit_triage_event(EVENT_EXTRACTION_DEGRADED, {
            "message_id": email_dict.get("id", ""),
            "reason": "extraction_exception",
            "details": [str(e)],
        })
        return heuristic
