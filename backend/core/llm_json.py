"""
llm_json - read the JSON object a language model meant to return.

Models wrap JSON in markdown fences, prefix it with prose, drop a closing
quote, stop mid-object at the token limit, or leave an enum value unquoted.
A bare ``json.loads(response.content)`` turns each of those into a failure
(the intent classifier, the app/URL resolvers). This is the repair ladder the
vision loop already proved (LeanVisionLoop._parse_response), shared.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _loads_object(text: str) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def parse_json_object(raw: Any) -> Optional[Dict[str, Any]]:
    """The JSON object in a model response, repairing common errors; else None."""
    if raw is None:
        return None
    raw = str(raw)

    result = _loads_object(raw)
    if result is not None:
        return result

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        result = _loads_object(cleaned)
        if result is not None:
            return result

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    json_text = match.group()
    result = _loads_object(json_text)
    if result is not None:
        return result

    # Missing closing quote: "click,  ->  "click",
    result = _loads_object(re.sub(r'"(\w+),\s*\n', r'"\1",\n', json_text))
    if result is not None:
        logger.debug("[llm_json] repaired a missing quote")
        return result

    # Truncated: cut back to the last complete value and close what is open.
    open_braces = json_text.count("{") - json_text.count("}")
    open_brackets = json_text.count("[") - json_text.count("]")
    if open_braces > 0 or open_brackets > 0:
        last_comma, last_colon = json_text.rfind(","), json_text.rfind(":")
        truncated = json_text[:last_comma] if last_comma > last_colon else json_text
        truncated += "]" * max(0, open_brackets) + "}" * max(0, open_braces)
        result = _loads_object(truncated)
        if result is not None:
            logger.debug("[llm_json] repaired a truncated object")
            return result

    # Unquoted values: "action": click  ->  "action": "click"
    result = _loads_object(re.sub(
        r':\s*([a-zA-Z_]\w*)\s*([,}\]])',
        lambda m: m.group(0) if m.group(1) in ("true", "false", "null")
        else f': "{m.group(1)}"{m.group(2)}',
        json_text,
    ))
    if result is not None:
        logger.debug("[llm_json] repaired unquoted values")
    return result


__all__ = ["parse_json_object"]
