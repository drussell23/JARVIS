"""Budget starts when WORK starts.

The governed loop stamps ``pipeline_deadline`` once at ``submit()``. A
background op then waits in the pool's FIFO behind whatever came first --
16-20 minutes in soak bt-2026-09-02-013719 -- and reaches a worker with
most of its budget already spent by the queue. VALIDATE then ran pytest
with the 30-46 s that were left and every roadmap op failed for a reason
that had nothing to do with its code.

The pool re-stamps the deadline at pickup. It only ever EXTENDS, so a
deadline a caller deliberately set further out is never pulled in.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from backend.core.ouroboros.governance.background_agent_pool import (
    restamp_pipeline_deadline_at_start,
)

_ENV_FLAG = "JARVIS_PIPELINE_DEADLINE_AT_START"
_ENV_BUDGET = "JARVIS_PIPELINE_TIMEOUT_S"


@dataclass(frozen=True)
class _Ctx:
    pipeline_deadline: Optional[datetime] = None

    def with_pipeline_deadline(self, deadline: datetime) -> "_Ctx":
        return replace(self, pipeline_deadline=deadline)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def test_a_stale_submit_time_deadline_is_rebased_to_now_plus_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ENV_FLAG, raising=False)
    monkeypatch.setenv(_ENV_BUDGET, "2400")
    stale = _Ctx(pipeline_deadline=_now() + timedelta(seconds=45))   # what the queue left
    out = restamp_pipeline_deadline_at_start(stale)
    remaining = (out.pipeline_deadline - _now()).total_seconds()
    assert 2390 < remaining <= 2400


def test_a_later_deadline_is_never_pulled_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_BUDGET, "600")
    far = _Ctx(pipeline_deadline=_now() + timedelta(seconds=5000))
    assert restamp_pipeline_deadline_at_start(far) is far


def test_flag_off_restores_the_submit_time_stamp_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV_FLAG, "false")
    stale = _Ctx(pipeline_deadline=_now() + timedelta(seconds=45))
    assert restamp_pipeline_deadline_at_start(stale) is stale


def test_a_context_without_the_seam_is_returned_unchanged() -> None:
    plain = object()
    assert restamp_pipeline_deadline_at_start(plain) is plain


def test_no_existing_deadline_gets_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_BUDGET, "900")
    out = restamp_pipeline_deadline_at_start(_Ctx())
    assert out.pipeline_deadline is not None
    assert 890 < (out.pipeline_deadline - _now()).total_seconds() <= 900


def test_a_garbage_budget_falls_back_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_BUDGET, "banana")
    stale = _Ctx(pipeline_deadline=_now() + timedelta(seconds=45))
    out = restamp_pipeline_deadline_at_start(stale)
    # float("banana") raises inside the try; the op keeps its old deadline.
    assert out is stale
