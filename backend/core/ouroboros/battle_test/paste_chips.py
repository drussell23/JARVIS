"""Large-paste collapse — the prompt stays a prompt, not a wall.

Pasting a 300-line traceback used to render all 300 lines into the input
box; the operator's caret disappeared below the fold of their own paste.
CC collapses the paste to a chip; so does this:

    [Pasted text #1 +300 lines]

The FULL content is preserved and spliced back in at submit — the daemon
receives what was pasted, the operator's screen shows what matters. The
chip is plain text in the buffer, so it can be deleted (dropping the
paste), moved, or surrounded with prose like any other token.

Thresholds are DELIBERATELY looser than CC's (>2 lines/800 chars): short
multi-line pastes stay inline and editable, because seeing-and-editing a
pasted block was itself a fought-for feature (#70172's data-loss fix).
Only a paste too big to edit comfortably collapses. Env-tunable.

Bracketed paste is the seam: prompt_toolkit delivers a paste as ONE
``Keys.BracketedPaste`` event, so a binding here sees the whole payload
before the buffer does. Application-level bindings take precedence over
the library's default insert.

Env: ``JARVIS_PASTE_COLLAPSE_ENABLED`` (default true),
``JARVIS_PASTE_COLLAPSE_LINES`` (default 12),
``JARVIS_PASTE_COLLAPSE_CHARS`` (default 1200).

NEVER raises into key dispatch or the submit path.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict

logger = logging.getLogger(__name__)

PASTE_CHIPS_SCHEMA_VERSION: str = "paste_chips.1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_PASTE_COLLAPSE_ENABLED"
LINES_ENV_VAR: str = "JARVIS_PASTE_COLLAPSE_LINES"
CHARS_ENV_VAR: str = "JARVIS_PASTE_COLLAPSE_CHARS"

_CHIP_RE = re.compile(r"\[Pasted text #(\d+) \+\d+ lines\]")

#: chip number → full pasted content. Bounded; module-level because the
#: chips live in ONE process's prompt buffer.
_STORE: Dict[int, str] = {}
_STORE_LOCK = threading.Lock()
_NEXT = {"n": 1}
_MAX_STORED = 20


def is_paste_collapse_enabled() -> bool:
    """Master flag — default true. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _threshold_lines() -> int:
    try:
        return max(3, int(os.environ.get(LINES_ENV_VAR, "12")))
    except (TypeError, ValueError):
        return 12


def _threshold_chars() -> int:
    try:
        return max(200, int(os.environ.get(CHARS_ENV_VAR, "1200")))
    except (TypeError, ValueError):
        return 1200


def should_collapse(data: str) -> bool:
    """Is this paste too big to live inline? NEVER raises."""
    try:
        return (
            data.count("\n") + 1 > _threshold_lines()
            or len(data) > _threshold_chars()
        )
    except Exception:  # noqa: BLE001
        return False


def store_paste(data: str) -> str:
    """Stash the content, return its chip. NEVER raises (falls back to
    the raw data as its own 'chip' so nothing is ever lost)."""
    try:
        with _STORE_LOCK:
            n = _NEXT["n"]
            _NEXT["n"] += 1
            _STORE[n] = data
            while len(_STORE) > _MAX_STORED:
                _STORE.pop(min(_STORE), None)
        lines = data.count("\n") + 1
        return f"[Pasted text #{n} +{lines} lines]"
    except Exception:  # noqa: BLE001
        return data


def expand_paste_chips(text: str) -> str:
    """Splice full contents back in at submit. A chip whose paste has
    rotated out of the store stays as literal text — visible, never a
    silent hole. NEVER raises."""
    try:
        if "[Pasted text #" not in (text or ""):
            return text

        def _sub(match: Any) -> str:
            with _STORE_LOCK:
                return _STORE.get(int(match.group(1)), match.group(0))

        return _CHIP_RE.sub(_sub, text)
    except Exception:  # noqa: BLE001
        return text


def reset_for_tests() -> None:
    with _STORE_LOCK:
        _STORE.clear()
        _NEXT["n"] = 1


def install_paste_collapse(kb: Any) -> bool:
    """Bind the bracketed-paste interceptor into *kb*. Small pastes flow
    through byte-identical to the library default. NEVER raises."""
    try:
        from prompt_toolkit.keys import Keys

        @kb.add(Keys.BracketedPaste)
        def _paste(event: Any) -> None:
            try:
                data = str(event.data or "")
                if is_paste_collapse_enabled() and should_collapse(data):
                    event.current_buffer.insert_text(store_paste(data))
                else:
                    event.current_buffer.insert_text(data)
            except Exception:  # noqa: BLE001 — a paste must never vanish
                try:
                    event.current_buffer.insert_text(str(event.data or ""))
                except Exception:  # noqa: BLE001
                    pass

        return True
    except Exception:  # noqa: BLE001
        logger.debug("[PasteChips] install degraded", exc_info=True)
        return False


__all__ = [
    "CHARS_ENV_VAR",
    "LINES_ENV_VAR",
    "MASTER_FLAG_ENV_VAR",
    "PASTE_CHIPS_SCHEMA_VERSION",
    "expand_paste_chips",
    "install_paste_collapse",
    "is_paste_collapse_enabled",
    "reset_for_tests",
    "should_collapse",
    "store_paste",
]
