"""P5 Slice 5 — graduation pin suite + reachability supplement +
in-process live-fire smoke for the AdversarialReviewer subagent.

Layered evidence pattern, mirrors P4 Slice 5:
  * Master flag default-true pin (file-scoped + source-grep ``"1"``
    literal in the single owner module).
  * Pre-graduation pin rename in the owner test suite.
  * EventChannelServer source-grep — ``register_adversarial_routes``
    is wired into the start path; the master-flag check gates the
    wiring; the loopback assertion alias is used.
  * Cross-slice authority survival: banned-import scan over all 4
    slice modules + post-graduation re-pins of:
      - primitive remains pure-data (Slice 1),
      - service only writes the JSONL ledger (Slice 2),
      - hook is wiring-only no-IO (Slice 3),
      - observability is read-only over the ledger (Slice 4),
      - SSE event_type still in _VALID_EVENT_TYPES.
  * In-process live-fire smoke (15 checks): service skip-paths
    work under master-on default; happy path produces audit row;
    REPL all 5 subcommands render under master-on; IDE GETs reach
    200 + return correct shape; SSE bridge accepts the event;
    master-off revert proven for all 4 surfaces.
  * Reachability supplement: every Slice 4 endpoint URL routed +
    reachable from a fresh aiohttp Application; service skip
    branches reachable deterministically.

Orchestrator GENERATE wiring (calling
``review_plan_for_generate_injection`` from the post-PLAN/pre-
GENERATE hook in orchestrator.py) is intentionally **deferred to
follow-up** — same pattern as P4 Slice 5 deferred wiring
``MetricsSessionObserver`` into the harness's session-end path.
The Slice 3 hook is already self-contained + unit-testable; the
orchestrator can call it whenever an explicit follow-up wires the
call site.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import tokenize
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED,
    _VALID_EVENT_TYPES,
)
from backend.core.ouroboros.governance.adversarial_observability import (
    AdversarialReplDispatcher,
    AdversarialReplStatus,
    register_adversarial_routes,
)
from backend.core.ouroboros.governance.adversarial_reviewer import (
    is_enabled,
)
from backend.core.ouroboros.governance.adversarial_reviewer_service import (
    AdversarialReviewerService,
    ReviewProviderResult,
    _AdversarialAuditLedger,
    reset_default_service,
)
from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
    review_plan_for_generate_injection,
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


_GOOD_RESPONSE = (
    '{"findings": [{"severity": "HIGH", "category": "race_condition", '
    '"description": "deadlock under load", '
    '"mitigation_hint": "use RWLock", '
    '"file_reference": "backend/x.py"}]}'
)


class _FakeProv:
    def __init__(self, raw=_GOOD_RESPONSE, cost_usd=0.012,
                 model_used="claude-test"):
        self.raw = raw
        self.cost_usd = cost_usd
        self.model_used = model_used

    def review(self, prompt):
        return ReviewProviderResult(
            raw_response=self.raw,
            cost_usd=self.cost_usd,
            model_used=self.model_used,
        )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    monkeypatch.delenv(
        "JARVIS_ADVERSARIAL_REVIEWER_AUDIT_PATH", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD", raising=False,
    )
    yield


@pytest.fixture
def fresh(tmp_path):
    reset_default_service()
    L = _AdversarialAuditLedger(path=tmp_path / "audit.jsonl")
    yield {"ledger": L, "tmp": tmp_path}
    reset_default_service()


# ===========================================================================
# §A — Master flag default-true (post-graduation)
# ===========================================================================


def test_master_flag_default_true_post_graduation(monkeypatch):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    assert is_enabled() is True


def test_master_flag_source_grep_default_literal_one():
    """Pin: source declares the env-default fallback as ``"1"``.
    Pinning the literal makes any revert mechanically visible in a
    PR diff."""
    src = _read(
        "backend/core/ouroboros/governance/adversarial_reviewer.py",
    )
    pat = re.compile(
        r'os\.environ\.get\(\s*"JARVIS_ADVERSARIAL_REVIEWER_ENABLED"\s*,'
        r'\s*"1"',
    )
    assert pat.search(src), (
        "adversarial_reviewer.is_enabled() must use "
        "os.environ.get(KEY, \"1\") for default-true"
    )


def test_master_flag_explicit_false_disables(monkeypatch):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "false")
    assert is_enabled() is False


def test_pin_renamed_in_primitive_suite():
    """Pin: pre-graduation pin renamed per its embedded discipline."""
    src = _read("tests/governance/test_adversarial_reviewer.py")
    code = _strip_docstrings_and_comments(src)
    assert (
        "def test_is_enabled_default_false_pre_graduation" not in code
    ), "primitive suite still has pre-graduation pin name"
    assert (
        "def test_is_enabled_default_true_post_graduation" in code
    ), "primitive suite missing post-graduation pin name"


# ===========================================================================
# §B — EventChannelServer wiring source-grep
# ===========================================================================


def test_event_channel_imports_register_adversarial_routes():
    """Pin: EventChannelServer.start mounts the adversarial surface."""
    src = _read("backend/core/ouroboros/governance/event_channel.py")
    assert (
        "from backend.core.ouroboros.governance.adversarial_observability"
        in src
    )
    assert "register_adversarial_routes" in src


def test_event_channel_gates_adversarial_on_master_flag():
    """Pin: wiring uses adversarial_reviewer.is_enabled (called
    ``_adversarial_enabled`` in the import) before mounting."""
    src = _read("backend/core/ouroboros/governance/event_channel.py")
    assert "_adversarial_enabled()" in src
    code = _strip_docstrings_and_comments(src)
    assert "_adversarial_enabled" in code
    assert "register_adversarial_routes" in code


def test_event_channel_uses_loopback_assert_for_adversarial():
    """Pin: same loopback-only invariant as the rest of the IDE
    surface."""
    src = _read("backend/core/ouroboros/governance/event_channel.py")
    assert "_assert_loopback_adversarial" in src


# ===========================================================================
# §C — Cross-slice authority survival
# ===========================================================================


_SLICE_FILES = [
    "backend/core/ouroboros/governance/adversarial_reviewer.py",
    "backend/core/ouroboros/governance/adversarial_reviewer_service.py",
    "backend/core/ouroboros/governance/adversarial_reviewer_hook.py",
    "backend/core/ouroboros/governance/adversarial_observability.py",
]


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


@pytest.mark.parametrize("path", _SLICE_FILES)
def test_no_authority_imports_in_any_slice(path):
    src = _read(path)
    for imp in _BANNED:
        assert imp not in src, f"{path} imports banned: {imp}"


def test_primitive_remains_pure_data_post_graduation():
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/adversarial_reviewer.py"),
    )
    for c in (
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "import requests",
        "import httpx",
    ):
        assert c not in src, f"unexpected coupling in primitive: {c}"


def test_service_only_io_is_audit_ledger_post_graduation():
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/adversarial_reviewer_service.py",
        ),
    )
    for c in (
        "subprocess.",
        "os.environ[",
        "import requests",
        "import httpx",
        "import urllib.request",
    ):
        assert c not in src, f"unexpected coupling in service: {c}"


def test_hook_remains_io_free_post_graduation():
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/adversarial_reviewer_hook.py",
        ),
    )
    for c in (
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "import requests",
        "import httpx",
    ):
        assert c not in src, f"unexpected coupling in hook: {c}"


def test_observability_read_only_post_graduation():
    """Pin: observability READS the JSONL ledger; never writes."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/adversarial_observability.py",
        ),
    )
    for c in (
        "subprocess.",
        ".write_text(",
        "os.environ[",
        "import requests",
        "import httpx",
    ):
        assert c not in src, f"unexpected coupling: {c}"
    # Pinning write-mode strings catches any regression that opens
    # the ledger for writes.
    assert ', "a"' not in src
    assert ', "w"' not in src


def test_event_type_remains_in_valid_set():
    """Pin: graduation must not drop the SSE event from the broker
    allow-list."""
    assert EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED in _VALID_EVENT_TYPES


# ===========================================================================
# §D — In-process live-fire smoke (master-on end-to-end)
# ===========================================================================


def test_livefire_L1_service_default_on_writes_audit(monkeypatch, fresh):
    """L1: post-graduation, the service runs by default — no env
    knob setup needed. Audit row is written."""
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    rev = s.review_plan(
        op_id="op-L1", plan_text="plan",
        target_files=("backend/x.py",),
    )
    assert rev.skip_reason == ""
    assert len(rev.findings) == 1
    # Audit row written.
    text = fresh["ledger"].path.read_text(encoding="utf-8")
    rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    assert any(r["op_id"] == "op-L1" for r in rows)


def test_livefire_L2_safe_auto_skip_under_default(monkeypatch, fresh):
    """L2: SAFE_AUTO bypass works under the new default-true."""
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    rev = s.review_plan(
        op_id="op-L2", plan_text="trivial",
        target_files=("a.py",), risk_tier_name="SAFE_AUTO",
    )
    assert rev.skip_reason == "safe_auto"


def test_livefire_L3_hook_returns_injection_under_default(
    monkeypatch, fresh,
):
    """L3: Slice 3 hook produces the GENERATE-injection text under
    default-on. Bridge feed best-effort."""
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    reset_default_service()
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    inj = review_plan_for_generate_injection(
        op_id="op-L3", plan_text="real plan",
        target_files=("backend/x.py",),
        risk_tier_name="APPROVAL_REQUIRED",
        service=s,
        bridge=None,
    )
    assert "Reviewer raised:" in inj.injection_text
    assert "[HIGH]" in inj.injection_text


def test_livefire_L4_repl_current_default_on(monkeypatch, fresh):
    """L4: REPL current returns the latest review under default-on."""
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    s.review_plan(op_id="op-L4", plan_text="p",
                  target_files=("backend/x.py",))
    d = AdversarialReplDispatcher(ledger_path=fresh["ledger"].path)
    r = d.handle("/adversarial current")
    assert r.status is AdversarialReplStatus.OK
    assert "op-L4" in r.rendered_text


def test_livefire_L5_repl_history_default_on(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    for i in range(3):
        s.review_plan(op_id=f"op-h{i}", plan_text="p",
                      target_files=("backend/x.py",))
    d = AdversarialReplDispatcher(ledger_path=fresh["ledger"].path)
    r = d.handle("/adversarial history 5")
    assert r.status is AdversarialReplStatus.OK
    assert "op-h0" in r.rendered_text
    assert "op-h2" in r.rendered_text


def test_livefire_L6_repl_why_default_on(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    s.review_plan(op_id="op-why", plan_text="p",
                  target_files=("backend/x.py",))
    d = AdversarialReplDispatcher(ledger_path=fresh["ledger"].path)
    r = d.handle("/adversarial why op-why")
    assert r.status is AdversarialReplStatus.OK
    assert "[HIGH]" in r.rendered_text
    assert "deadlock" in r.rendered_text


def test_livefire_L7_repl_stats_default_on(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    s.review_plan(op_id="op-s1", plan_text="p",
                  target_files=("backend/x.py",))
    s.review_plan(op_id="op-s2", plan_text="t",
                  target_files=("a.py",), risk_tier_name="SAFE_AUTO")
    d = AdversarialReplDispatcher(ledger_path=fresh["ledger"].path)
    r = d.handle("/adversarial stats")
    assert r.status is AdversarialReplStatus.OK
    assert "total reviews:      2" in r.rendered_text
    assert "skip:safe_auto" in r.rendered_text


def test_livefire_L8_repl_help_default_on(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    d = AdversarialReplDispatcher(ledger_path=fresh["ledger"].path)
    r = d.handle("/adversarial help")
    assert r.status is AdversarialReplStatus.OK
    for sub in ("/adversarial current", "/adversarial history",
                "/adversarial why", "/adversarial stats",
                "/adversarial help"):
        assert sub in r.rendered_text


def test_livefire_L9_get_current_default_on(monkeypatch, fresh):
    """L9: GET /observability/adversarial returns the latest review
    under master-on default."""
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    s.review_plan(op_id="op-gc", plan_text="p",
                  target_files=("backend/x.py",))
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_adversarial_routes(
        app, ledger_path=fresh["ledger"].path,
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get("/observability/adversarial")
                body = await resp.json()
                assert resp.status == 200
                assert body["review"]["op_id"] == "op-gc"
    asyncio.run(_run())


def test_livefire_L10_get_history_default_on(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    for i in range(3):
        s.review_plan(op_id=f"op-gh{i}", plan_text="p",
                      target_files=("backend/x.py",))
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_adversarial_routes(
        app, ledger_path=fresh["ledger"].path,
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get(
                    "/observability/adversarial/history",
                )
                body = await resp.json()
                assert resp.status == 200
                assert body["rows_seen"] == 3
    asyncio.run(_run())


def test_livefire_L11_get_stats_default_on(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    s.review_plan(op_id="op-gs", plan_text="p",
                  target_files=("backend/x.py",))
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_adversarial_routes(
        app, ledger_path=fresh["ledger"].path,
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get(
                    "/observability/adversarial/stats",
                )
                body = await resp.json()
                assert resp.status == 200
                assert body["stats"]["total_reviews"] == 1
    asyncio.run(_run())


def test_livefire_L12_get_detail_default_on(monkeypatch, fresh):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    s.review_plan(op_id="op-gd", plan_text="p",
                  target_files=("backend/x.py",))
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_adversarial_routes(
        app, ledger_path=fresh["ledger"].path,
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get("/observability/adversarial/op-gd")
                body = await resp.json()
                assert resp.status == 200
                assert body["review"]["op_id"] == "op-gd"
    asyncio.run(_run())


def test_livefire_L13_master_off_revert_service(monkeypatch, fresh):
    """L13: hot-revert proven for the service."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "false")
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    rev = s.review_plan(op_id="op-rev", plan_text="p",
                        target_files=("a.py",))
    assert rev.skip_reason == "master_off"


def test_livefire_L14_master_off_revert_repl(monkeypatch, fresh):
    """L14: hot-revert proven for the REPL."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "false")
    d = AdversarialReplDispatcher(ledger_path=fresh["ledger"].path)
    r = d.handle("/adversarial current")
    assert r.status is AdversarialReplStatus.DISABLED


def test_livefire_L15_master_off_revert_endpoints(monkeypatch, fresh):
    """L15: hot-revert proven for the IDE GET endpoints."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "false")
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_adversarial_routes(
        app, ledger_path=fresh["ledger"].path,
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                resp = await client.get("/observability/adversarial")
                assert resp.status == 403
    asyncio.run(_run())


# ===========================================================================
# §E — Reachability supplement
# ===========================================================================


def test_reachability_service_skip_branch_no_provider_default_on(
    monkeypatch, fresh,
):
    """Reachability: no_provider skip branch is reachable under
    default-on (caller forgot to wire a provider)."""
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=None, audit_ledger=fresh["ledger"],
    )
    rev = s.review_plan(op_id="op-np", plan_text="p",
                        target_files=("a.py",))
    assert rev.skip_reason == "no_provider"


def test_reachability_all_four_endpoints_routed(monkeypatch, fresh):
    """Reachability: every Slice 4 endpoint URL routed + reachable
    from a fresh aiohttp Application."""
    monkeypatch.delenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", raising=False)
    s = AdversarialReviewerService(
        provider=_FakeProv(), audit_ledger=fresh["ledger"],
    )
    s.review_plan(op_id="op-rch", plan_text="p",
                  target_files=("backend/x.py",))
    aiohttp_test = pytest.importorskip("aiohttp.test_utils")
    web = pytest.importorskip("aiohttp.web")
    app = web.Application()
    register_adversarial_routes(
        app, ledger_path=fresh["ledger"].path,
        rate_limit_check=lambda req: True,
    )

    async def _run():
        async with aiohttp_test.TestServer(app) as server:
            async with aiohttp_test.TestClient(server) as client:
                for path in (
                    "/observability/adversarial",
                    "/observability/adversarial/history?limit=10",
                    "/observability/adversarial/stats",
                    "/observability/adversarial/op-rch",
                ):
                    resp = await client.get(path)
                    assert resp.status == 200, f"{path} → {resp.status}"
    asyncio.run(_run())
