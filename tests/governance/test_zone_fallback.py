"""Multi-Zonal Fallback -- eradicate GPU STOCKOUTs.

A hardcoded zone is brittle: GPU capacity stocks out per-zone. On a STOCKOUT the
system must autonomously retry the SAME request in the next zone. Pure helpers:
the ordered fallback chain + STOCKOUT detection.
"""
from __future__ import annotations

import re

import pytest

from backend.core.ouroboros.governance.zone_fallback import (
    zone_fallback_chain,
    is_stockout_error,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("JARVIS_GCP_ZONE_FALLBACK", raising=False)
    yield


def test_default_chain_is_nonempty_and_valid():
    chain = zone_fallback_chain()
    assert len(chain) >= 3
    assert all(re.fullmatch(r"[a-z]+-[a-z]+\d+-[a-z]", z) for z in chain), chain


def test_preferred_zone_goes_first():
    chain = zone_fallback_chain(preferred="us-east4-a")
    assert chain[0] == "us-east4-a"
    assert len(chain) == len(set(chain))  # no duplicate even if preferred already in defaults


def test_preferred_already_in_defaults_not_duplicated():
    chain = zone_fallback_chain(preferred="us-central1-b")
    assert chain[0] == "us-central1-b"
    assert chain.count("us-central1-b") == 1


def test_env_override():
    import os
    os.environ["JARVIS_GCP_ZONE_FALLBACK"] = "us-west1-b, us-west1-c"
    try:
        assert zone_fallback_chain() == ["us-west1-b", "us-west1-c"]
    finally:
        del os.environ["JARVIS_GCP_ZONE_FALLBACK"]


def test_spans_multiple_regions():
    regions = {z.rsplit("-", 1)[0] for z in zone_fallback_chain()}
    assert len(regions) >= 2  # cross-region so a regional outage can't strand us


@pytest.mark.parametrize("text,stock", [
    ("The zone 'us-central1-b' does not have enough resources available", True),
    ("... (state:STOCKOUT, sub-state:STOCKOUT, resource type:compute)", True),
    ("ZONE_RESOURCE_POOL_EXHAUSTED", True),
    ("RESOURCE_EXHAUSTED", True),
    ("Quota 'NVIDIA_L4_GPUS' exceeded", False),   # quota is NOT a stockout -> don't retry blindly
    ("Permission denied", False),
    ("", False),
])
def test_is_stockout_error(text, stock):
    assert is_stockout_error(text) is stock
