from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Tuple

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_TRACEBACK = re.compile(r"Traceback \(most recent call last\):.*", re.DOTALL)
_MAX_FIELD = 160


def strip_code(text: str) -> str:
    """Remove fenced code blocks + Python tracebacks so they are never spoken
    (mandate #4 — a stack trace would freeze the TTS buffer). NEVER raises."""
    try:
        t = _FENCE.sub(" ", text or "")
        t = _TRACEBACK.sub(" ", t)
        return " ".join(t.split())
    except Exception:  # noqa: BLE001
        return ""


def first_line(text: str) -> str:
    try:
        for ln in (text or "").splitlines():
            s = ln.strip()
            if s:
                return s
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _basename(p: str) -> str:
    return os.path.basename(str(p)) if p else ""


def _cap(s: str) -> str:
    s = strip_code(s)
    return s[:_MAX_FIELD].rstrip()


@dataclass(frozen=True)
class LedgerView:
    phase: str
    goal: str = ""
    files: Tuple[str, ...] = ()
    risk_tier: str = ""
    provider: str = ""
    outcome: str = ""
    root_cause: str = ""

    @classmethod
    def from_payload(cls, phase: str, payload: Mapping) -> "LedgerView":
        p = payload or {}
        files = tuple(_basename(f) for f in (p.get("target_files") or ()) if f)
        return cls(
            phase=str(phase or ""),
            goal=_cap(str(p.get("goal", ""))),
            files=files[:5],
            risk_tier=str(p.get("risk_tier", "") or ""),
            provider=str(p.get("provider", "") or ""),
            outcome=str(p.get("outcome", "") or ""),
            root_cause=_cap(first_line(str(p.get("root_cause", "")))),
        )

    def to_context_line(self) -> str:
        parts = [f"phase={self.phase}"]
        if self.goal:
            parts.append(f"goal={self.goal}")
        if self.files:
            parts.append("files=" + ",".join(self.files))
        if self.risk_tier:
            parts.append(f"risk={self.risk_tier}")
        if self.outcome:
            parts.append(f"outcome={self.outcome}")
        if self.root_cause:
            parts.append(f"cause={self.root_cause}")
        return " ".join(parts)
