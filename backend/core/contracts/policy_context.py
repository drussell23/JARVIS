"""PolicyContext — typed context for PolicyGate evaluation.

Replaces untyped Dict[str, Any] context with a frozen dataclass
that enforces field presence and types at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class PolicyContext:
    """Typed context passed to PolicyGate.evaluate()."""
    tier: int
    score: int
    message_id: str
    sender_domain: str
    is_reply: bool
    has_attachment: bool
    label_ids: Tuple[str, ...]
    cycle_id: str
    fencing_token: int
    config_version: str
