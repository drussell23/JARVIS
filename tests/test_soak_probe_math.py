"""Coverage for backend.soak_probes.soak_probe_math."""
from __future__ import annotations

from backend.soak_probes.soak_probe_math import probe_add


def test_probe_add_sums():
    assert probe_add(2, 3) == 5

# nudge: 1784668619
