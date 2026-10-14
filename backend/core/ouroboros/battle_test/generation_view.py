"""What a transcript should show of a model's streamed output.

The local model answers in two registers: PROSE (a session answer, reasoning
before a tool call) and structured JSON (a tool-call round, a ``2b.1``
candidate envelope). Mirrored verbatim, the JSON reached the cockpit as raw
brackets and escaped diff strings -- ``"unified_diff": "@@ -66,7 +66,7 @@\\n
..."`` and a model-claimed ``"model_id": "gpt-4o"`` -- scrolling between the
blocks that ALREADY render the same content properly (measured live,
2026-10-13):

  * tool calls  -> the tool blocks (``● Read(path) ⎿ Read 144 lines``)
  * diffs       -> the Update / DIFF blocks (``⎿ 4 additions and 2 removals``)
  * preamble    -> the transport's narration line
  * metadata    -> nothing: the model's claims about itself are not evidence

So prose passes through, and JSON is buffered and reduced to the one thing no
other block says: each candidate's ``rationale``. Raw JSON never reaches the
transcript, whole or partial.

The stream is line-based (the mirrors deliver completed lines). A JSON value
begins at a line whose first character opens an object or array, or at a
```` ```json ```` fence, and ends where its brackets balance -- counted with
string and escape awareness, so a brace inside a diff string does not end it --
or at the closing fence. A value still open when the stream ends, or one the
model malformed (trailing commas, truncation, single quotes), is healed by the
providers' own repair path (``_extract_json_block`` + ``_repair_json``), the
same one the pipeline parses generations with. One that cannot be healed is
dropped and counted, never shown.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, List, Tuple

logger = logging.getLogger("Ouroboros.GenerationView")

#: The generation schema's fields that carry human prose no other block
#: renders. Data, not policy: a field added to the schema is added here.
NARRATIVE_FIELDS: Tuple[str, ...] = ("rationale",)
#: Subtrees that are the model's claims about itself -- never shown.
SUPPRESSED_FIELDS: Tuple[str, ...] = ("provider_metadata",)

_OPENERS = {"{": "}", "[": "]"}
_FENCE = "```"


class GenerationView:
    """Turns one generation's completed lines into transcript lines.
    One instance per stream; :meth:`finish` at end of stream. NEVER raises."""

    def __init__(self) -> None:
        self._json: List[str] = []
        self._fenced = False
        self._depth = 0
        self._in_string = False
        self._escaped = False
        self._was_fenced = False
        self.dropped = 0            # JSON values that could not be healed

    @property
    def buffering(self) -> bool:
        return bool(self._json) or self._fenced

    # ---- the stream ---------------------------------------------------------
    def feed_line(self, line: str) -> List[str]:
        try:
            text = str(line)
            stripped = text.strip()
            if not self.buffering:
                if stripped.startswith(_FENCE) and stripped[len(_FENCE):].strip().lower() in ("json", ""):
                    # A fence that opens JSON. A bare ``` may open any code;
                    # it is treated as JSON only if what follows is JSON.
                    self._fenced = stripped[len(_FENCE):].strip().lower() == "json"
                    return []
                if stripped[:1] in _OPENERS:
                    self._start_value()
                else:
                    return [text]
            if self._fenced and stripped == _FENCE:
                return self._close_value()
            self._json.append(text)
            if not self._fenced and self._scan(text):
                return self._close_value()
            return []
        except Exception:  # noqa: BLE001
            logger.debug("[GenerationView] line degraded", exc_info=True)
            return []

    def finish(self) -> List[str]:
        """End of stream: a value still open is healed, never dumped."""
        return self._close_value() if self.buffering else []

    # ---- internals ----------------------------------------------------------
    def _start_value(self) -> None:
        self._json = []
        self._depth = 0
        self._in_string = False
        self._escaped = False

    def _scan(self, text: str) -> bool:
        """Track bracket depth through *text*; True when the value closed."""
        for ch in text:
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif ch == "\\":
                    self._escaped = True
                elif ch == '"':
                    self._in_string = False
                continue
            if ch == '"':
                self._in_string = True
            elif ch in _OPENERS:
                self._depth += 1
            elif ch in ("}", "]"):
                self._depth -= 1
                if self._depth <= 0:
                    return True
        return False

    def _close_value(self) -> List[str]:
        lines = list(self._json)
        raw = "\n".join(lines)
        self._was_fenced = self._fenced
        self._json = []
        self._fenced = False
        self._depth = 0
        self._in_string = False
        self._escaped = False
        if not raw.strip():
            return []
        value = parse_lenient(raw)
        if value is None and len(lines) == 1 and not self._was_fenced:
            # One self-contained line that does not parse was never JSON --
            # prose that happens to open with a bracket (a markdown link,
            # "[1] ..."). The model's JSON spans lines.
            return [lines[0]]
        if value is None:
            self.dropped += 1
            logger.debug("[GenerationView] unhealable JSON value dropped (%d chars)", len(raw))
            return []
        return list(narrative_lines(value))


def parse_lenient(raw: str) -> Any:
    """The providers' own extraction + repair, then ``json.loads``. ``None``
    when nothing parseable remains. NEVER raises."""
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
            _extract_json_block, _repair_json,
        )
        block = _extract_json_block(raw) or raw
        for attempt in (block, _repair_json(block)):
            try:
                return json.loads(attempt)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        logger.debug("[GenerationView] repair path unavailable", exc_info=True)
    return None


def narrative_lines(value: Any) -> Iterable[str]:
    """The NARRATIVE_FIELDS anywhere in *value*, in document order, skipping
    SUPPRESSED_FIELDS subtrees and repeats. NEVER raises."""
    seen = set()
    stack = [value]
    out: List[str] = []
    try:
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in SUPPRESSED_FIELDS:
                        continue
                    if key in NARRATIVE_FIELDS and isinstance(child, str):
                        prose = " ".join(child.split())
                        if prose and prose not in seen:
                            seen.add(prose)
                            out.append(prose)
                    elif isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(node, list):
                stack.extend(c for c in node if isinstance(c, (dict, list)))
    except Exception:  # noqa: BLE001
        logger.debug("[GenerationView] narrative walk degraded", exc_info=True)
    return out


__all__ = ["GenerationView", "NARRATIVE_FIELDS", "SUPPRESSED_FIELDS", "narrative_lines",
           "parse_lenient"]
