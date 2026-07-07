"""Full-duplex Karen voice — arbiter core (Sprint 1).

Engine-free coordination layer: depends only on the PlaybackHandle protocol so
the concurrency/barge-in logic is unit-testable without a mic or speaker.
"""
from __future__ import annotations
