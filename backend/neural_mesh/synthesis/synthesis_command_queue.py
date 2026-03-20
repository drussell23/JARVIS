"""
SynthesisCommandQueue — Mode B TTL queue with semantic supersession.

Semantic supersession: newer entry with same dedupe_key replaces older.
Expired entries (past TTL) are silently discarded on dequeue.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent


@dataclass
class SynthesisCommand:
    event: CapabilityGapEvent
    enqueued_at: float = field(default_factory=time.monotonic)


class SynthesisCommandQueue:
    def __init__(self, ttl_seconds: Optional[float] = None) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else float(
            os.environ.get("DAS_MODE_B_TTL_S", "1800")
        )
        self._lock = threading.Lock()
        self._order: List[str] = []
        self._store: Dict[str, SynthesisCommand] = {}

    def enqueue(self, event: CapabilityGapEvent) -> None:
        key = event.dedupe_key
        cmd = SynthesisCommand(event=event)
        with self._lock:
            if key in self._store:
                self._store[key] = cmd  # supersede in-place
            else:
                self._order.append(key)
                self._store[key] = cmd

    def dequeue(self) -> Optional[SynthesisCommand]:
        now = time.monotonic()
        with self._lock:
            while self._order:
                key = self._order[0]
                cmd = self._store.get(key)
                if cmd is None:
                    self._order.pop(0)
                    continue
                if now - cmd.enqueued_at > self._ttl:
                    self._order.pop(0)
                    del self._store[key]
                    continue
                self._order.pop(0)
                del self._store[key]
                return cmd
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
