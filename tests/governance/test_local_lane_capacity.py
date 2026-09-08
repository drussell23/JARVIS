"""One local GPU cannot serve four concurrent generations.

Measured 2026-09-08: four ops entered GENERATE together, each negotiating a 32k
context against the 30B, and every stream returned `tokens=0
first_token_ms=-1 tps=0.0`. The orchestrator recorded `no_candidates_returned`
→ `generation_failed` for thirteen of thirty ops. That reads like a model
quality problem and is not one — the model never ran. The default
`primary_concurrency=4` is correct for a hosted fleet and structurally wrong
for one card.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.autonomy.local_lane_capacity import (
    LaneCapacity,
    local_lane_is_primary,
    resolve_primary_concurrency,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "JARVIS_LOCAL_PRIMARY_CONCURRENCY",
        "JARVIS_LOCAL_MAX_CONCURRENCY",
        "JARVIS_LOCAL_KV_FRACTION",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _hydrated():
    """Capacity may only be derived from a LOADED environment — see
    init_guard. Production declares this at the boot seam; a test that
    exercises capacity must declare it too, or it is testing the fail-safe."""
    from backend.core.ouroboros.governance.init_guard import (
        mark_hydrated, reset_for_tests,
    )
    reset_for_tests()
    mark_hydrated()
    yield
    reset_for_tests()


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")


# --------------------------------------------------------------------------
# The cloud lane is untouched
# --------------------------------------------------------------------------

def test_a_paid_lane_keeps_its_concurrency(cloud):
    """Four requests to a hosted fleet are somebody else's capacity problem."""
    cap = resolve_primary_concurrency(cloud_default=4)
    assert cap.concurrency == 4
    assert cap.basis == "cloud_lane"


def test_the_local_lane_is_detected(local):
    assert local_lane_is_primary() is True


# --------------------------------------------------------------------------
# The local lane is bounded, and fails SAFE
# --------------------------------------------------------------------------

def test_an_unmeasurable_card_serves_one(local):
    """The failure being prevented is over-subscription, so an unreadable card
    must not be assumed roomy."""
    cap = resolve_primary_concurrency(cloud_default=6)
    assert cap.concurrency == 1
    assert "fail_safe" in cap.basis


def test_the_local_lane_never_inherits_the_cloud_default(local):
    """This is the whole defect: six workers against one GPU."""
    assert resolve_primary_concurrency(cloud_default=6).concurrency < 6


def test_an_operator_override_wins(local, monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIMARY_CONCURRENCY", "3")
    cap = resolve_primary_concurrency(cloud_default=6)
    assert cap.concurrency == 3
    assert cap.basis == "operator_override"


def test_concurrency_is_derived_from_headroom(local, monkeypatch):
    """Weights load once; each extra in-flight request needs its own KV cache,
    and THAT is what exhausts the card."""
    import backend.core.ouroboros.governance.candidate_generator as cg

    monkeypatch.setattr(cg, "_awakened_vram_bytes", lambda: 32 * 1024 ** 3)
    monkeypatch.setitem(cg._JPRIME_SERVED_BYTES_CACHE, "http://x", 16 * 1024 ** 3)
    monkeypatch.setenv("JARVIS_LOCAL_KV_FRACTION", "0.5")
    cap = resolve_primary_concurrency(cloud_default=6, endpoint="http://x")
    assert cap.basis == "derived_from_vram"
    assert cap.concurrency == 2          # 16GiB headroom / 8GiB per stream


def test_a_model_larger_than_the_card_serves_one(local, monkeypatch):
    import backend.core.ouroboros.governance.candidate_generator as cg

    monkeypatch.setattr(cg, "_awakened_vram_bytes", lambda: 8 * 1024 ** 3)
    monkeypatch.setitem(cg._JPRIME_SERVED_BYTES_CACHE, "http://x", 24 * 1024 ** 3)
    cap = resolve_primary_concurrency(cloud_default=6, endpoint="http://x")
    assert cap.concurrency == 1


def test_the_ceiling_is_respected(local, monkeypatch):
    import backend.core.ouroboros.governance.candidate_generator as cg

    monkeypatch.setattr(cg, "_awakened_vram_bytes", lambda: 640 * 1024 ** 3)
    monkeypatch.setitem(cg._JPRIME_SERVED_BYTES_CACHE, "http://x", 1024 ** 3)
    monkeypatch.setenv("JARVIS_LOCAL_MAX_CONCURRENCY", "2")
    assert resolve_primary_concurrency(cloud_default=6,
                                       endpoint="http://x").concurrency == 2


def test_it_never_raises(local, monkeypatch):
    import backend.core.ouroboros.governance.candidate_generator as cg

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(cg, "_awakened_vram_bytes", boom)
    assert resolve_primary_concurrency(cloud_default=6).concurrency >= 1


def test_the_capacity_explains_itself():
    text = LaneCapacity(2, "derived_from_vram", 32 * 1024 ** 3, 16 * 1024 ** 3).render()
    assert "concurrency=2" in text and "vram=32.0GiB" in text


# --------------------------------------------------------------------------
# The envelope must size the pool from the LANE
# --------------------------------------------------------------------------

def test_the_envelope_pool_follows_the_lane(local):
    from backend.core.ouroboros.governance.production_envelope import build

    assert int(build("cockpit").as_env()["JARVIS_BG_POOL_SIZE"]) == 1


def test_the_envelope_pool_is_unchanged_on_a_paid_lane(cloud):
    from backend.core.ouroboros.governance.production_envelope import build

    assert int(build("soak").as_env()["JARVIS_BG_POOL_SIZE"]) == 6


def test_the_size_is_read_from_the_negotiators_cache_not_refetched():
    """A second fetch would be a second answer to a question the negotiator
    has already asked the endpoint."""
    import inspect

    from backend.core.ouroboros.governance.autonomy import local_lane_capacity

    src = inspect.getsource(local_lane_capacity.resolve_primary_concurrency)
    assert "_JPRIME_SERVED_BYTES_CACHE" in src
    assert "_fetch_served_model_bytes" not in src
