"""Adapter productivity: what the organism landed while each adapter served it.

Pinned against the three ways the first live run of this tracker was wrong
(2026-10-08, all fixed before it was ever trusted):

* landings carried no ``committed_at`` -> every one read as time 0 and none
  was attributed (now: git author time, shared with the training yield);
* a killed session's fallback summary put its "end" hours before its real
  end -> half the landings fell outside every window (now: the session id
  is the start, the running log's last write bounds the end);
* summaries rewritten weeks later stretched September sessions to 400 h each
  (now: the summary FILE's mtime is never evidence of activity).
"""
from __future__ import annotations

import calendar
import json
import math
import os
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.observability import adapter_productivity as ap

H = 3600.0


def _ts(s: str) -> float:
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%d-%H%M%S")))


def _versions(*published, active=None, rejected=()):
    vs = [{"version": "origin", "published_at": None}]
    for name, t in published:
        v = {"version": name, "published_at": t}
        if name in rejected:
            v.update(status="rejected", rejected_at=t + 60)
        vs.append(v)
    return {"active": active or (published[-1][0] if published else "origin"), "versions": vs}


# ---------------------------------------------------------------------------
# Who served when
# ---------------------------------------------------------------------------

def test_origin_serves_until_the_first_publish_then_each_until_the_next():
    iv = ap.service_intervals(_versions(("v1", 1000.0), ("v2", 5000.0)))
    assert ap.serving_at(iv, 10.0) == "origin"
    assert ap.serving_at(iv, 1000.0) == "v1"
    assert ap.serving_at(iv, 4999.0) == "v1"
    assert ap.serving_at(iv, 9e9) == "v2"


def test_a_rejected_version_serves_only_its_own_verification():
    iv = ap.service_intervals(_versions(("v1", 1000.0), ("bad", 2000.0), active="v1", rejected=("bad",)))
    assert ap.serving_at(iv, 2030.0) == "bad"          # while it was being verified
    assert ap.serving_at(iv, 3000.0) == "v1"           # the incumbent, again, after rejection


# ---------------------------------------------------------------------------
# Session windows from the evidence that survives
# ---------------------------------------------------------------------------

def _session(root: Path, sid: str, *, summary=None, log_end=None):
    d = root / sid
    d.mkdir(parents=True)
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary))
    if log_end is not None:
        (d / "debug.log").write_text("x")
        os.utime(d / "debug.log", (log_end, log_end))
    return d


def test_the_start_is_the_session_id_and_the_log_bounds_the_end(tmp_path):
    start = _ts("2026-10-05-012717")
    # the atexit fallback recorded a window that ended at boot
    _session(tmp_path, "bt-2026-10-05-012717", summary={"duration_s": 5.0, "last_activity_ts": start,
                                                        "stats": {"attempted": 9}},
             log_end=start + 6 * H)
    (s,), missing = ap.read_sessions(tmp_path)
    assert s.start == start and s.hours == pytest.approx(6.0) and s.attempted == 9 and missing == 0


def test_a_rewritten_summary_file_does_not_stretch_its_session(tmp_path):
    start = _ts("2026-09-07-010247")
    d = _session(tmp_path, "bt-2026-09-07-010247", summary={"duration_s": 786.0, "last_activity_ts": start + 786},
                 log_end=start + 786)
    later = start + 400 * H
    os.utime(d / "summary.json", (later, later))       # rewritten weeks later
    (s,), _ = ap.read_sessions(tmp_path)
    assert s.hours == pytest.approx(786 / H)


def test_a_session_without_a_summary_still_counts_its_hours(tmp_path):
    start = _ts("2026-10-01-100000")
    _session(tmp_path, "bt-2026-10-01-100000", log_end=start + 2 * H)
    (s,), missing = ap.read_sessions(tmp_path)
    assert missing == 1 and s.hours == pytest.approx(2.0) and s.attempted == 0


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

def _S(sid, start, hours, attempted=10):
    return ap.Session(session_id=sid, start=start, end=start + hours * H, attempted=attempted)


def test_landings_are_attributed_to_the_adapter_that_served_their_session():
    docs = _versions(("v1", 100 * H))
    sessions = [_S("a", 0, 10), _S("b", 200 * H, 10)]
    landings = {"x": 5 * H, "y": 6 * H, "z": 205 * H, "stray": 50 * H}     # stray: between sessions
    by, owner, stray = ap.tally(sessions, landings, ap.service_intervals(docs))
    assert by["origin"].landed == 2 and by["v1"].landed == 1 and stray == 1
    assert owner == {"a": "origin", "b": "v1"}


def test_the_incumbent_is_judged_over_matched_exposure():
    """Its most recent sessions, as many hours as the active adapter has
    served -- a long history must not swamp a short one."""
    docs = _versions(("v1", 1000 * H))
    sessions = [_S("old", 0, 50), _S("recent", 900 * H, 8), _S("new", 1100 * H, 8)]
    landings = {f"o{i}": 1 * H + i for i in range(40)}                    # all in the old session
    landings.update({f"r{i}": 901 * H + i for i in range(2)})
    landings.update({f"n{i}": 1101 * H + i for i in range(6)})
    out = ap.compare(docs, sessions, landings, min_hours=6.0, alpha=0.05)
    m = out["matched"]
    assert out["incumbent"] == "origin"
    assert m["incumbent"]["sessions"] == 1 and m["incumbent"]["landed"] == 2   # only "recent"
    assert m["active"]["landed"] == 6 and out["rate_ratio"] == pytest.approx(3.0)


def test_below_the_minimum_exposure_the_verdict_waits():
    docs = _versions(("v1", 1000 * H))
    out = ap.compare(docs, [_S("o", 0, 20), _S("n", 1100 * H, 2)],
                     {f"n{i}": 1100 * H + i for i in range(5)}, min_hours=6.0, alpha=0.05)
    assert out["verdict"] == "insufficient_evidence" and "2.0 of 6.0" in out["detail"]


@pytest.mark.parametrize("k_new, k_old, verdict", [(20, 2, "better"), (2, 20, "worse"), (5, 5, "no_detectable_difference")])
def test_the_verdict_follows_the_exact_rate_test(k_new, k_old, verdict):
    docs = _versions(("v1", 1000 * H))
    landings = {f"o{i}": 1 * H + i for i in range(k_old)}
    landings.update({f"n{i}": 1101 * H + i for i in range(k_new)})
    out = ap.compare(docs, [_S("o", 0, 10), _S("n", 1100 * H, 10)], landings, min_hours=6.0, alpha=0.05)
    assert out["verdict"] == verdict


def test_the_exact_test_matches_its_definition():
    # equal exposure: X ~ Binomial(n, 1/2); P(X >= 8 | n = 10) = 56/1024
    assert ap.p_better(8, 2, 5.0, 5.0) == pytest.approx(56 / 1024)
    assert ap.p_better(0, 0, 5.0, 5.0) is None and ap.p_better(3, 1, 0.0, 5.0) is None


def test_no_incumbent_is_reported_as_such():
    out = ap.compare(_versions(), [_S("o", 0, 10)], {"a": 1 * H}, min_hours=6.0, alpha=0.05)
    assert out["active"] == "origin" and out["verdict"] == "no_incumbent"
    assert not math.isnan(out["by_version"]["origin"]["hours"])
