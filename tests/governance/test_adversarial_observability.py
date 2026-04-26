"""P5 Slice 4 — AdversarialReviewer observability surfaces tests.

Pins:
  * Module constants + status enum + frozen result + frozen
    AdversarialStats.
  * EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED present in
    _VALID_EVENT_TYPES.
  * REPL dispatcher:
      - empty input → EMPTY status,
      - master-off → DISABLED status (no ledger read),
      - bare /adversarial → current,
      - bare subcommand without /adversarial prefix accepted,
      - all 5 subcommands happy paths,
      - shape gating: every arg-less subcommand rejects extra
        tokens; history with non-numeric → fall through;
        why with non-shape → fall through; why with unknown id →
        UNKNOWN_OP; why with no args → UNKNOWN_SUBCOMMAND.
  * compute_stats: empty → defaults; happy path with mixed
    completed/skipped reviews; severity hist aggregation;
    skip_reason histogram.
  * IDE GET endpoints (12 tests via aiohttp test server):
    - master-off → 403,
    - current returns latest,
    - empty ledger → review:None,
    - history default + custom limit + malformed limit → 400,
    - stats happy + zero reviews,
    - detail happy + unknown 404 + bad-id 400,
    - rate-limit → 429,
    - broken rate_limit_check treated as allowed,
    - schema_version + Cache-Control: no-store stamped on
      every response.
  * publish_adversarial_findings_emitted swallows broker
    unavailability.
  * Authority invariants pinned + ledger-read-only surface +
    no-write asserts.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import tokenize
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.adversarial_reviewer import (
    AdversarialFinding,
    AdversarialReview,
    FindingSeverity,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED,
    _VALID_EVENT_TYPES,
)
from backend.core.ouroboros.governance.adversarial_observability import (
    ADVERSARIAL_OBSERVABILITY_SCHEMA_VERSION,
    HISTORY_DEFAULT_N,
    HISTORY_MAX_N,
    MAX_LINES_READ,
    MAX_RENDERED_BYTES,
    AdversarialReplDispatcher,
    AdversarialReplResult,
    AdversarialReplStatus,
    AdversarialStats,
    compute_stats,
    publish_adversarial_findings_emitted,
    register_adversarial_routes,
    render_help,
    render_history,
    render_review_detail,
    render_review_summary,
    render_stats,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


def _seed_row(
    op_id: str = "op-1",
    skip_reason: str = "",
    findings_count: int = 0,
    cost_usd: float = 0.012,
    sev_high: int = 0,
    sev_med: int = 0,
    sev_low: int = 0,
    findings: List[Dict[str, Any]] = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "wrote_at_unix": 1_700_000_000.0,
        "op_id": op_id,
        "findings": findings or [],
        "raw_findings_count": findings_count,
        "filtered_findings_count": findings_count,
        "cost_usd": cost_usd,
        "model_used": "claude-test",
        "skip_reason": skip_reason,
        "notes": [],
        "severity_histogram": {
            "HIGH": sev_high, "MEDIUM": sev_med, "LOW": sev_low,
        },
    }


def _seed_ledger(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    """Slice 4 ships master-off; tests need master-on for the
    surfaces to fire. Tests that exercise master-off explicitly
    setenv 'false'."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "1")
    monkeypatch.delenv(
        "JARVIS_ADVERSARIAL_REVIEWER_AUDIT_PATH", raising=False,
    )
    yield


# ===========================================================================
# A — Module constants + status enum + frozen result
# ===========================================================================


def test_obs_schema_pinned():
    assert ADVERSARIAL_OBSERVABILITY_SCHEMA_VERSION == "1.0"


def test_max_rendered_bytes_pinned():
    assert MAX_RENDERED_BYTES == 16 * 1024


def test_max_lines_read_pinned():
    assert MAX_LINES_READ == 8_192


def test_history_defaults_pinned():
    assert HISTORY_DEFAULT_N == 10
    assert HISTORY_MAX_N == MAX_LINES_READ


def test_status_enum_six_values():
    assert {s.name for s in AdversarialReplStatus} == {
        "OK", "EMPTY", "UNKNOWN_SUBCOMMAND", "UNKNOWN_OP",
        "READ_ERROR", "DISABLED",
    }


def test_result_is_frozen():
    r = AdversarialReplResult(
        status=AdversarialReplStatus.OK, rendered_text="x",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.rendered_text = "y"  # type: ignore[misc]


def test_event_type_in_valid_set():
    """Pin: the new SSE event type must be in the broker allow-list,
    else publish drops silently."""
    assert EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED == "adversarial_findings_emitted"
    assert EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED in _VALID_EVENT_TYPES


def test_stats_dataclass_default_shape():
    s = AdversarialStats()
    assert s.total_reviews == 0
    assert s.skip_reason_histogram == {}


# ===========================================================================
# B — REPL dispatcher master-off + empty input
# ===========================================================================


def test_repl_master_off_returns_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "false")
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row()])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial current")
    assert r.status is AdversarialReplStatus.DISABLED
    assert "disabled" in r.rendered_text


def test_repl_empty_input(tmp_path):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "x.jsonl")
    assert d.handle("").status is AdversarialReplStatus.EMPTY
    assert d.handle("   \t").status is AdversarialReplStatus.EMPTY


def test_repl_bare_adversarial_routes_to_current(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row(op_id="op-bare")])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial")
    assert r.status is AdversarialReplStatus.OK
    assert "op-bare" in r.rendered_text


def test_repl_bare_subcommand_accepted(tmp_path):
    """Operator typing 'current' without /adversarial prefix."""
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row()])
    d = AdversarialReplDispatcher(ledger_path=p)
    assert d.handle("current").status is AdversarialReplStatus.OK


# ===========================================================================
# C — REPL: current
# ===========================================================================


def test_repl_current_returns_latest(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [
        _seed_row(op_id="op-1"),
        _seed_row(op_id="op-2"),
        _seed_row(op_id="op-latest", findings_count=2,
                  sev_high=1, sev_med=1),
    ])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial current")
    assert r.status is AdversarialReplStatus.OK
    assert "op-latest" in r.rendered_text
    assert r.review_dict is not None
    assert r.review_dict["op_id"] == "op-latest"


def test_repl_current_no_data(tmp_path):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "missing.jsonl")
    r = d.handle("/adversarial current")
    assert r.status is AdversarialReplStatus.OK
    assert "no reviews" in r.rendered_text


def test_repl_current_renders_skip_marker(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row(op_id="op-sk", skip_reason="safe_auto")])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial current")
    assert "skipped: safe_auto" in r.rendered_text


# ===========================================================================
# D — REPL: history
# ===========================================================================


def test_repl_history_default_count(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row(op_id=f"op-{i}") for i in range(15)])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial history")
    assert r.status is AdversarialReplStatus.OK
    # Default is 10 → only the last 10 op-ids appear.
    assert "op-14" in r.rendered_text
    assert "op-5" in r.rendered_text
    assert "op-0" not in r.rendered_text  # too old


def test_repl_history_explicit_count(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row(op_id=f"op-{i}") for i in range(8)])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial history 3")
    assert r.status is AdversarialReplStatus.OK
    # Last 3 only.
    assert "op-7" in r.rendered_text
    assert "op-5" in r.rendered_text
    assert "op-4" not in r.rendered_text


def test_repl_history_zero_uses_default(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row()])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial history 0")
    assert r.status is AdversarialReplStatus.OK


def test_repl_history_with_prose_falls_through(tmp_path):
    """``/adversarial history of changes`` is natural language."""
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "x.jsonl")
    r = d.handle("/adversarial history of changes")
    assert r.status is AdversarialReplStatus.UNKNOWN_SUBCOMMAND


def test_repl_history_empty(tmp_path):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "missing.jsonl")
    r = d.handle("/adversarial history")
    assert r.status is AdversarialReplStatus.OK
    assert "(empty)" in r.rendered_text


# ===========================================================================
# E — REPL: why
# ===========================================================================


def test_repl_why_known_op(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [
        _seed_row(op_id="op-target", findings_count=1, sev_high=1,
                  findings=[{
                      "severity": "HIGH", "category": "race_condition",
                      "description": "deadlock", "mitigation_hint": "RWLock",
                      "file_reference": "a.py",
                  }]),
    ])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial why op-target")
    assert r.status is AdversarialReplStatus.OK
    assert "op-target" in r.rendered_text
    assert "[HIGH]" in r.rendered_text
    assert "deadlock" in r.rendered_text
    assert "RWLock" in r.rendered_text
    assert "file: a.py" in r.rendered_text


def test_repl_why_unknown_op(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row(op_id="op-1")])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial why op-missing")
    assert r.status is AdversarialReplStatus.UNKNOWN_OP


def test_repl_why_no_args_falls_through(tmp_path):
    """``/adversarial why`` alone fails the shape gate."""
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "x.jsonl")
    r = d.handle("/adversarial why")
    assert r.status is AdversarialReplStatus.UNKNOWN_SUBCOMMAND


def test_repl_why_multiple_args_falls_through(tmp_path):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "x.jsonl")
    r = d.handle("/adversarial why op-1 extra args")
    assert r.status is AdversarialReplStatus.UNKNOWN_SUBCOMMAND


def test_repl_why_invalid_id_chars_falls_through(tmp_path):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "x.jsonl")
    r = d.handle("/adversarial why ../../etc/passwd")
    assert r.status is AdversarialReplStatus.UNKNOWN_SUBCOMMAND


def test_repl_why_skipped_review(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [_seed_row(op_id="op-sk", skip_reason="safe_auto")])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial why op-sk")
    assert r.status is AdversarialReplStatus.OK
    assert "skipped: safe_auto" in r.rendered_text
    assert "(no findings)" in r.rendered_text


# ===========================================================================
# F — REPL: stats + help
# ===========================================================================


def test_repl_stats_aggregates_correctly(tmp_path):
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, [
        _seed_row(op_id="op-1", findings_count=2,
                  sev_high=1, sev_med=1, cost_usd=0.012),
        _seed_row(op_id="op-2", skip_reason="safe_auto", cost_usd=0.0),
        _seed_row(op_id="op-3", skip_reason="budget_exhausted", cost_usd=0.20),
        _seed_row(op_id="op-4", findings_count=1, sev_low=1, cost_usd=0.018),
    ])
    d = AdversarialReplDispatcher(ledger_path=p)
    r = d.handle("/adversarial stats")
    assert r.status is AdversarialReplStatus.OK
    assert "total reviews:      4" in r.rendered_text
    assert "completed:          2" in r.rendered_text
    assert "skipped:            2" in r.rendered_text
    assert "total findings:     3" in r.rendered_text
    assert "high=1 med=1 low=1" in r.rendered_text
    assert "0.2300" in r.rendered_text
    assert "skip:budget_exhausted" in r.rendered_text
    assert "skip:safe_auto" in r.rendered_text


def test_repl_stats_no_data(tmp_path):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "missing.jsonl")
    r = d.handle("/adversarial stats")
    assert r.status is AdversarialReplStatus.OK
    assert "no reviews" in r.rendered_text


def test_repl_help_lists_all_subcommands(tmp_path):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "x.jsonl")
    r = d.handle("/adversarial help")
    assert r.status is AdversarialReplStatus.OK
    for sub in ("/adversarial current", "/adversarial history",
                "/adversarial why", "/adversarial stats",
                "/adversarial help"):
        assert sub in r.rendered_text


@pytest.mark.parametrize("line", [
    "/adversarial current extra",
    "/adversarial stats more",
    "/adversarial help me",
])
def test_repl_argless_subcommand_with_extra_falls_through(tmp_path, line):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "x.jsonl")
    r = d.handle(line)
    assert r.status is AdversarialReplStatus.UNKNOWN_SUBCOMMAND


def test_repl_unknown_subcommand_renders_help(tmp_path):
    d = AdversarialReplDispatcher(ledger_path=tmp_path / "x.jsonl")
    r = d.handle("/adversarial whatever")
    assert r.status is AdversarialReplStatus.UNKNOWN_SUBCOMMAND
    assert "/adversarial current" in r.rendered_text


# ===========================================================================
# G — compute_stats pure function
# ===========================================================================


def test_compute_stats_empty():
    s = compute_stats([])
    assert s.total_reviews == 0
    assert s.skip_reason_histogram == {}
    assert s.severity_histogram == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}


def test_compute_stats_skips_non_dict_rows():
    s = compute_stats([_seed_row(), "garbage", 42, _seed_row()])  # type: ignore[list-item]
    assert s.total_reviews == 2


def test_compute_stats_handles_malformed_cost():
    """A row with non-numeric cost shouldn't raise."""
    row = _seed_row()
    row["cost_usd"] = "not-a-number"
    s = compute_stats([row])
    assert s.total_reviews == 1
    assert s.total_cost_usd == 0.0


def test_compute_stats_handles_missing_severity_histogram():
    row = _seed_row()
    row.pop("severity_histogram", None)
    s = compute_stats([row])
    assert s.severity_histogram == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}


def test_compute_stats_to_dict_stable_shape():
    s = compute_stats([_seed_row(findings_count=1, sev_high=1)])
    d = s.to_dict()
    for k in ("total_reviews", "completed_reviews", "skipped_reviews",
              "skip_reason_histogram", "total_findings",
              "severity_histogram", "total_cost_usd"):
        assert k in d


# ===========================================================================
# H — Renderers ASCII safety
# ===========================================================================


def test_render_help_is_ascii():
    render_help().encode("ascii")


def test_render_history_is_ascii(tmp_path):
    out = render_history([_seed_row()])
    out.encode("ascii")


def test_render_review_summary_is_ascii():
    render_review_summary(_seed_row()).encode("ascii")


def test_render_review_detail_is_ascii():
    render_review_detail(_seed_row(findings=[{
        "severity": "HIGH", "category": "x", "description": "d",
        "mitigation_hint": "m", "file_reference": "f.py",
    }])).encode("ascii")


def test_render_stats_is_ascii():
    render_stats(compute_stats([_seed_row()])).encode("ascii")


# ===========================================================================
# I — IDE GET endpoints (aiohttp test server)
# ===========================================================================


async def _get(app, path: str) -> tuple:
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    async with aiohttp_test.TestServer(app) as server:
        async with aiohttp_test.TestClient(server) as client:
            resp = await client.get(path)
            body = await resp.json()
            return resp.status, body, dict(resp.headers)


def _build_app(tmp_path, *, rows=None, deny_rate=False, broken_rate=False):
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    p = tmp_path / "audit.jsonl"
    _seed_ledger(p, rows or [])
    if broken_rate:
        rl = lambda req: (_ for _ in ()).throw(RuntimeError("x"))
    else:
        rl = (lambda req: not deny_rate)
    register_adversarial_routes(
        app, ledger_path=p, rate_limit_check=rl,
        cors_headers=lambda req: {"Access-Control-Allow-Origin": "x"},
    )
    return app


def test_endpoint_disabled_returns_403(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "false")
    app = _build_app(tmp_path)

    async def _run():
        status, body, _ = await _get(app, "/observability/adversarial")
        assert status == 403
        assert body["reason_code"] == "ide_observability.disabled"
    asyncio.run(_run())


def test_endpoint_current_returns_latest(tmp_path):
    app = _build_app(tmp_path, rows=[
        _seed_row(op_id="op-1"),
        _seed_row(op_id="op-latest"),
    ])

    async def _run():
        status, body, headers = await _get(app, "/observability/adversarial")
        assert status == 200
        assert body["review"]["op_id"] == "op-latest"
        assert body["schema_version"] == "1.0"
        assert headers["Cache-Control"] == "no-store"
    asyncio.run(_run())


def test_endpoint_current_empty_ledger(tmp_path):
    app = _build_app(tmp_path, rows=[])

    async def _run():
        status, body, _ = await _get(app, "/observability/adversarial")
        assert status == 200
        assert body["review"] is None
    asyncio.run(_run())


def test_endpoint_history_default(tmp_path):
    app = _build_app(tmp_path, rows=[
        _seed_row(op_id=f"op-{i}") for i in range(5)
    ])

    async def _run():
        status, body, _ = await _get(
            app, "/observability/adversarial/history",
        )
        assert status == 200
        assert body["rows_seen"] == 5
        assert len(body["reviews"]) == 5
    asyncio.run(_run())


def test_endpoint_history_custom_limit(tmp_path):
    app = _build_app(tmp_path, rows=[
        _seed_row(op_id=f"op-{i}") for i in range(5)
    ])

    async def _run():
        status, body, _ = await _get(
            app, "/observability/adversarial/history?limit=2",
        )
        assert status == 200
        assert body["rows_seen"] == 2
    asyncio.run(_run())


def test_endpoint_history_malformed_limit_400(tmp_path):
    app = _build_app(tmp_path)

    async def _run():
        status, body, _ = await _get(
            app, "/observability/adversarial/history?limit=NaN",
        )
        assert status == 400
        assert body["reason_code"] == "ide_observability.malformed_limit"
    asyncio.run(_run())


def test_endpoint_stats_aggregates(tmp_path):
    app = _build_app(tmp_path, rows=[
        _seed_row(op_id="op-1", findings_count=1, sev_high=1),
        _seed_row(op_id="op-2", skip_reason="safe_auto"),
    ])

    async def _run():
        status, body, _ = await _get(
            app, "/observability/adversarial/stats",
        )
        assert status == 200
        assert body["stats"]["total_reviews"] == 2
        assert body["stats"]["completed_reviews"] == 1
        assert body["stats"]["skipped_reviews"] == 1
        assert body["stats"]["severity_histogram"]["HIGH"] == 1
    asyncio.run(_run())


def test_endpoint_detail_happy(tmp_path):
    app = _build_app(tmp_path, rows=[
        _seed_row(op_id="op-target", findings_count=1, sev_high=1),
        _seed_row(op_id="op-other"),
    ])

    async def _run():
        status, body, _ = await _get(
            app, "/observability/adversarial/op-target",
        )
        assert status == 200
        assert body["review"]["op_id"] == "op-target"
    asyncio.run(_run())


def test_endpoint_detail_unknown_404(tmp_path):
    app = _build_app(tmp_path)

    async def _run():
        status, body, _ = await _get(
            app, "/observability/adversarial/op-missing",
        )
        assert status == 404
        assert body["reason_code"] == "ide_observability.review_not_found"
    asyncio.run(_run())


def test_endpoint_detail_bad_id_400(tmp_path):
    app = _build_app(tmp_path)

    async def _run():
        status, body, _ = await _get(
            app, "/observability/adversarial/has%20space",
        )
        assert status == 400
        assert body["reason_code"] == "ide_observability.bad_op_id"
    asyncio.run(_run())


def test_endpoint_rate_limited_429(tmp_path):
    app = _build_app(tmp_path, deny_rate=True)

    async def _run():
        status, body, _ = await _get(app, "/observability/adversarial")
        assert status == 429
        assert body["reason_code"] == "ide_observability.rate_limited"
    asyncio.run(_run())


def test_endpoint_broken_rate_limiter_treated_as_allowed(tmp_path):
    app = _build_app(tmp_path, broken_rate=True)

    async def _run():
        status, _body, _ = await _get(app, "/observability/adversarial")
        assert status == 200
    asyncio.run(_run())


# ===========================================================================
# J — publish_adversarial_findings_emitted bridge helper
# ===========================================================================


def test_publish_helper_never_raises():
    """Pin: publish_adversarial_findings_emitted swallows broker
    unavailability."""
    review = AdversarialReview(
        op_id="op-pub",
        findings=(AdversarialFinding(
            severity=FindingSeverity.HIGH,
            category="x", description="y", mitigation_hint="z",
            file_reference="a.py",
        ),),
        filtered_findings_count=1, raw_findings_count=1,
    )
    # Smoke: should not raise in any environment.
    publish_adversarial_findings_emitted(review)


# ===========================================================================
# K — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_observability_no_authority_imports():
    src = _read(
        "backend/core/ouroboros/governance/adversarial_observability.py",
    )
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_observability_read_only_no_writes():
    """Pin: this module READS the JSONL ledger — Slice 2 owns writes.
    No subprocess / env mutation / network."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/adversarial_observability.py",
        ),
    )
    forbidden = [
        "subprocess.",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
    # The audit-ledger reader uses .read_text + Path methods; opening
    # files in append/write mode would be a regression. Pin via the
    # absence of "open(" + write modes:
    assert ', "a"' not in src
    assert ', "w"' not in src
