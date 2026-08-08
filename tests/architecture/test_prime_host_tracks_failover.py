"""Failover writes J-Prime's address; a module-level constant cannot hear it.

`failover_lifecycle` wires a new node and writes the address into the
environment:

    os.environ["JARVIS_PRIME_URL"]  = url
    os.environ["JARVIS_PRIME_HOST"] = ip

then logs "endpoint WIRED ... PrimeProvider now targets the live node".

`distributed_resilience` bound that variable to a module constant at import:

    _GCP_HOST = os.environ.get("JARVIS_PRIME_HOST", "136.113.252.164")

A constant bound at import cannot see a write that happens later, so every
rsync and heartbeat in that module kept addressing whatever the value was when
the module first loaded — with the variable unset, a hardcoded public IP that
by then may belong to an instance which no longer exists. The failover log said
the endpoint was wired. For this module it was not.

A claim outrunning the measurement, which is the same defect as every other one
found in this sweep — here in its module-level-constant form, and the only one
where the stale value is a network address.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.governance import distributed_resilience as dr  # noqa: E402

_ENV = "JARVIS_PRIME_HOST"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)


def test_a_failover_write_is_seen_without_reimport(monkeypatch) -> None:
    """THE regression. The module is already imported — as it is in a live
    process — and the address must still change."""
    before = dr._gcp_host()
    monkeypatch.setenv(_ENV, "10.1.2.3")
    assert dr._gcp_host() == "10.1.2.3"
    assert dr._gcp_host() != before


def test_the_fallback_is_unchanged_when_no_failover_has_run(monkeypatch) -> None:
    """Behaviour with nothing configured is identical to before, so this
    changes when the value is read and never what it is."""
    assert dr._gcp_host() == dr._GCP_HOST_DEFAULT


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_address_falls_back_rather_than_addressing_nothing(
        monkeypatch, blank) -> None:
    """An empty JARVIS_PRIME_HOST would otherwise build `user@` and rsync to a
    hostname that is not there."""
    monkeypatch.setenv(_ENV, blank)
    assert dr._gcp_host() == dr._GCP_HOST_DEFAULT


def test_every_consumer_asks_rather_than_reading_a_frozen_copy() -> None:
    """Structural: one surviving `_GCP_HOST` reference would be a site that
    silently keeps the import-time address while its neighbours track failover
    — harder to diagnose than uniform staleness."""
    import inspect

    src = inspect.getsource(dr)
    # The default constant is expected; a bare frozen host is not.
    assert "_GCP_HOST =" not in src, "the import-time capture is back"
    for line in src.splitlines():
        stripped = line.strip()
        if "_GCP_HOST" in stripped and "_GCP_HOST_DEFAULT" not in stripped:
            raise AssertionError(f"frozen host still read at: {stripped}")


def test_the_writer_and_the_reader_agree_on_the_variable() -> None:
    """The two halves are in different modules and only this name joins them.
    If either renames it, the failover goes quiet rather than failing."""
    import inspect
    from backend.core.ouroboros.governance import failover_lifecycle

    writer = inspect.getsource(failover_lifecycle)
    assert f'os.environ["{_ENV}"]' in writer, (
        "failover no longer publishes the host under this name"
    )
    assert _ENV in inspect.getsource(dr)


def test_reading_is_cheap_enough_to_do_every_time() -> None:
    """The reason a constant was tempting. An env read is nanoseconds; an rsync
    is milliseconds at best, so there is no cost argument for caching an
    address that must be allowed to change."""
    import time

    start = time.perf_counter()
    for _ in range(10_000):
        dr._gcp_host()
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"10k resolutions took {elapsed:.3f}s"
