"""Farming tasks are staged most-design-freedom first.

Soak `bt-2026-09-02-003459` measured it directly: the canonical tasks
(re-raise an exception, swap in `datetime.now(timezone.utc)`) collapsed to
one AST across three draws at 0.2 / 0.70 / 0.95, while the free-form tasks
(a per-type strategy table, a type guard with recursion and list handling,
an ok/error flag) drew 2-3 structurally distinct candidates. Sampling
cannot manufacture variance a task does not admit, and dispatch order is
batch order (the sensor reads the tail, the pool is FIFO), so the batch
must LEAD with the work that can pair.

The provisioner is a script, loaded here by path the way the arc's other
scripts are tested. Its two structural refusals -- cage paths and cosmetic
tasks -- must survive the ranking untouched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provision_farming_work.py"


@pytest.fixture(scope="module")
def prov():
    spec = importlib.util.spec_from_file_location("_prov_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_prov_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


_CANONICAL = (
    "Replace every timezone-naive datetime.now() with datetime.now(timezone.utc) "
    "and import timezone, so emitted timestamps are unambiguous."
)
_FREE = (
    "Make the fallback strategy depend on error_type: network retries with "
    "backoff, no-speech retries once, aborted does not retry; keep a lookup "
    "table so each policy has one definition."
)


def test_free_form_work_outranks_canonical_work(prov) -> None:
    free = prov.design_freedom_score("backend/api/audio_error_fallback.py", _FREE)
    canon = prov.design_freedom_score("backend/api/model_status_api.py", _CANONICAL)
    assert free > canon
    assert canon < 0 < free


def test_score_is_deterministic_and_explainable(prov) -> None:
    a = prov.design_freedom_score("backend/api/audio_error_fallback.py", _FREE)
    b = prov.design_freedom_score("backend/api/audio_error_fallback.py", _FREE)
    assert a == b
    # A file that does not exist contributes no branch density and no error.
    assert prov.design_freedom_score("does/not/exist.py", _FREE) == pytest.approx(
        sum(1.0 for s in prov._FREEDOM_SIGNALS if s in _FREE.lower())
        - sum(1.0 for s in prov._CANONICAL_SIGNALS if s in _FREE.lower()),
    )


def test_the_shipped_batch_is_ranked_free_form_first(prov) -> None:
    """The two canonical tasks must not be the first two orders."""
    orders = prov.build_orders(len(prov.TASKS), prov.live_sentinels())
    assert orders, "no orders staged in this checkout"
    head = " ".join(orders[:2]).lower()
    assert "datetime.now(timezone" not in head
    assert "re-raise httpexception" not in head
    tail = " ".join(orders[-2:]).lower()
    assert "datetime.now(timezone" in tail or "re-raise httpexception" in tail


def test_ranking_does_not_bypass_the_cosmetic_refusal(prov) -> None:
    with pytest.raises(ValueError):
        prov.assert_produces_executable_change(
            "backend/api/sse_contract.py", "Add docstrings. DOCS ONLY.",
        )


def test_ranking_does_not_bypass_the_cage_refusal(prov) -> None:
    trip = prov.is_caged("backend/core/ouroboros/governance/providers.py", prov.live_sentinels())
    assert trip, "the cage sentinel list no longer covers governance/"
