"""P3 Slice 4 — graduation pin suite + reachability supplement +
in-process live-fire smoke for the inline approval UX.

Pins the irreversible cross-slice surface that Slice 4's master-flag
flip and factory-wiring depend on. Mirrors the P0 / P0.5 / P1 / P1.5
graduation test patterns:

  Layered evidence:
    * Master flag default-true pin (file-scoped + source-grep literal).
    * Pre-graduation pin rename pin (the test in
      ``test_inline_approval_primitive.py`` was renamed to
      ``..._default_true_post_graduation`` per its embedded contract).
    * Factory-selection invariants: master-on → InlineApprovalProvider;
      master-off → CLIApprovalProvider. Hot-revert matrix proven.
    * GovernedLoopService construction site uses the factory, not a
      direct CLIApprovalProvider(...) call (source-grep).
    * Cross-slice authority survival: banned-import scan over all 4
      slice modules; renderer's I/O surface remains argv-only +
      docstring-stripped pin.
    * Reachability supplement: factory-built provider end-to-end
      flow (request → enqueue → renderer → fake operator types `y` →
      provider.approve → audit ledger row → terminal APPROVED).
      No mocks of the queue / provider / audit ledger — only the
      stdin stream is faked.
    * In-process live-fire smoke: 15 deterministic checks proving
      the four slices compose correctly when masters are on.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import re
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.approval_provider import (
    CLIApprovalProvider,
)
from backend.core.ouroboros.governance.inline_approval import (
    InlineApprovalQueue,
    is_enabled,
    reset_default_queue,
)
from backend.core.ouroboros.governance.inline_approval_provider import (
    AUDIT_LEDGER_SCHEMA_VERSION,
    InlineApprovalProvider,
    _AuditLedger,
    build_approval_provider,
)
from backend.core.ouroboros.governance.inline_approval_renderer import (
    PROMPT_LABEL,
    render_request_block,
    run_inline_approval_loop,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.risk_engine import RiskTier


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    """Token-based docstring/comment strip — pins scan code only."""
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


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("JARVIS_INLINE_APPROVAL_AUDIT_PATH", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    yield


@pytest.fixture
def temp_audit(tmp_path: Path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("JARVIS_INLINE_APPROVAL_AUDIT_PATH", str(p))
    yield p


# ===========================================================================
# §A — Master flag default-true (post-graduation)
# ===========================================================================


def test_master_flag_default_true_post_graduation(monkeypatch):
    """Pin: Slice 4 graduation flipped default OFF→ON.

    Operator hot-revert: ``JARVIS_APPROVAL_UX_INLINE_ENABLED=false``."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    assert is_enabled() is True


def test_master_flag_source_grep_default_literal_one():
    """Pin: source declares the env-default fallback as ``"1"``.

    Reverting the flag means changing this literal back to ``""``.
    Pinning the literal makes the revert mechanically visible in any
    PR diff that touches it."""
    src = _read("backend/core/ouroboros/governance/inline_approval.py")
    # Match the default-arg in os.environ.get(...).
    pat = re.compile(
        r'os\.environ\.get\(\s*"JARVIS_APPROVAL_UX_INLINE_ENABLED"\s*,\s*"1"',
    )
    assert pat.search(src), (
        "is_enabled() must use os.environ.get(KEY, \"1\") for default-true"
    )


def test_master_flag_explicit_false_disables(monkeypatch):
    """Hot-revert path: any non-truthy explicit value disables."""
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "false")
    assert is_enabled() is False


def test_master_flag_pin_was_renamed_in_primitive_suite():
    """Pin: the pre-graduation pin
    ``test_is_enabled_default_false_pre_graduation`` MUST have been
    renamed to ``..._default_true_post_graduation`` per its own
    embedded discipline. Catches anyone who tries to revert by adding
    a new test instead of editing the renamed one."""
    src = _read("tests/governance/test_inline_approval_primitive.py")
    code = _strip_docstrings_and_comments(src)
    # The defun line is what counts (docstrings may still mention the
    # old name as historical context; the function symbol itself must
    # be renamed).
    assert "def test_is_enabled_default_false_pre_graduation" not in code
    assert "def test_is_enabled_default_true_post_graduation" in code


# ===========================================================================
# §B — Factory selection (build_approval_provider)
# ===========================================================================


def test_factory_returns_inline_when_master_on(monkeypatch):
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    p = build_approval_provider()
    assert isinstance(p, InlineApprovalProvider)


def test_factory_returns_cli_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "false")
    p = build_approval_provider()
    assert isinstance(p, CLIApprovalProvider)
    assert not isinstance(p, InlineApprovalProvider)


def test_factory_threads_project_root_to_cli_provider(monkeypatch, tmp_path):
    """Hot-revert: project_root MUST flow to CLIApprovalProvider so the
    correction_writer integration (Gap 8) keeps working."""
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "false")
    p = build_approval_provider(project_root=tmp_path)
    assert isinstance(p, CLIApprovalProvider)
    assert p._project_root == tmp_path


def test_factory_truthy_variants_select_inline(monkeypatch):
    for val in ("1", "true", "yes", "on"):
        monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", val)
        assert isinstance(build_approval_provider(), InlineApprovalProvider)


def test_factory_falsy_variants_select_cli(monkeypatch):
    for val in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", val)
        assert isinstance(build_approval_provider(), CLIApprovalProvider)


def test_factory_is_called_on_each_construction(monkeypatch):
    """Pin: the factory checks the env on every call (no caching).
    This is what makes a master-off→on toggle take effect on the next
    builder invocation without process restart."""
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "false")
    a = build_approval_provider()
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "true")
    b = build_approval_provider()
    assert isinstance(a, CLIApprovalProvider)
    assert isinstance(b, InlineApprovalProvider)


# ===========================================================================
# §C — GovernedLoopService construction site
# ===========================================================================


def test_governed_loop_service_uses_factory_not_direct_cli():
    """Pin: the construction site must call build_approval_provider(),
    not CLIApprovalProvider(...) directly. Source-grep ensures any
    revert is mechanically visible."""
    src = _read("backend/core/ouroboros/governance/governed_loop_service.py")
    # Code-only scan (strip docstrings).
    code = _strip_docstrings_and_comments(src)
    assert "build_approval_provider" in code, (
        "factory call missing from governed_loop_service.py"
    )
    # Direct construction calls must be gone (the import line keeps
    # `CLIApprovalProvider` for back-compat reference but no `(...)` call).
    direct_calls = re.findall(r"CLIApprovalProvider\s*\(", code)
    assert not direct_calls, (
        f"direct CLIApprovalProvider(...) calls found: {direct_calls}"
    )


def test_factory_import_present_in_governed_loop_service():
    src = _read("backend/core/ouroboros/governance/governed_loop_service.py")
    assert "from backend.core.ouroboros.governance.inline_approval_provider" in src
    assert "build_approval_provider" in src


# ===========================================================================
# §D — Cross-slice authority survival (banned imports across all 4)
# ===========================================================================


_SLICE_FILES = [
    "backend/core/ouroboros/governance/inline_approval.py",
    "backend/core/ouroboros/governance/inline_approval_provider.py",
    "backend/core/ouroboros/governance/inline_approval_renderer.py",
]


_BANNED_AUTH_IMPORTS = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


@pytest.mark.parametrize("path", _SLICE_FILES)
def test_no_authority_imports_in_any_slice(path):
    src = _read(path)
    for imp in _BANNED_AUTH_IMPORTS:
        assert imp not in src, f"{path} imports banned: {imp}"


def test_renderer_still_no_shell_true_post_graduation():
    """Pin: graduation does not relax the shell=True ban."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/inline_approval_renderer.py"),
    )
    assert "shell=True" not in src


def test_provider_still_only_audit_ledger_io_post_graduation():
    """Pin: graduation does not widen the provider's I/O surface."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/inline_approval_provider.py"),
    )
    for c in (
        "subprocess.",
        "os.environ[",
        "import requests",
        "import httpx",
        "import urllib.request",
    ):
        assert c not in src, f"unexpected coupling in provider: {c}"


def test_primitive_remains_pure_data_post_graduation():
    """Pin: Slice 1 surface stays pure-data even with master-on."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/inline_approval.py"),
    )
    for c in (
        "subprocess.",
        "open(",
        ".write_text(",
        "import requests",
        "import httpx",
    ):
        assert c not in src, f"unexpected coupling in primitive: {c}"


# ===========================================================================
# §E — In-process live-fire smoke (factory-built provider end-to-end)
# ===========================================================================


def _make_ctx(op_id: str = "op-livefire-1") -> OperationContext:
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="live-fire approval test",
        op_id=op_id,
    )
    return dataclasses.replace(ctx, risk_tier=RiskTier.APPROVAL_REQUIRED)


def test_livefire_factory_built_provider_is_inline(monkeypatch, temp_audit):
    """L1: post-graduation, factory selects InlineApprovalProvider."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    reset_default_queue()
    p = build_approval_provider()
    assert isinstance(p, InlineApprovalProvider)


def test_livefire_request_enqueues_into_singleton_queue(
    monkeypatch, temp_audit,
):
    """L2: provider's `request` populates the process-wide queue so a
    Slice 3 renderer (or external observer) can see it."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    reset_default_queue()
    from backend.core.ouroboros.governance.inline_approval import (
        get_default_queue,
    )
    p = build_approval_provider()

    async def _run():
        await p.request(_make_ctx(op_id="op-l2"))

    asyncio.run(_run())
    pend = get_default_queue().next_pending()
    assert pend is not None
    assert pend.op_id == "op-l2"


def test_livefire_full_loop_y_writes_audit_ledger(monkeypatch, temp_audit):
    """L3: provider build → request → renderer prompt with synthetic
    `y` → audit row written. End-to-end through 3 slice surfaces."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    reset_default_queue()
    p = build_approval_provider()

    async def _request():
        return await p.request(_make_ctx(op_id="op-l3"))

    asyncio.run(_request())

    from backend.core.ouroboros.governance.inline_approval import (
        get_default_queue,
    )
    pend = get_default_queue().next_pending()
    assert pend is not None

    out = io.StringIO()
    result = run_inline_approval_loop(
        p, pend,
        stream_in=io.StringIO("y\n"),
        stream_out=out,
        timeout_s=1.0,
    )
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )
    assert result.status is ApprovalStatus.APPROVED

    # Audit ledger row written.
    lines = [
        json.loads(line) for line in temp_audit.read_text().splitlines()
        if line.strip()
    ]
    assert any(
        r["status"] == "APPROVED" and r["op_id"] == "op-l3"
        and r["schema_version"] == AUDIT_LEDGER_SCHEMA_VERSION
        for r in lines
    )


def test_livefire_full_loop_n_writes_rejected_audit(monkeypatch, temp_audit):
    """L4: same end-to-end but with `n` → REJECTED row in ledger."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    reset_default_queue()
    p = build_approval_provider()

    async def _request():
        return await p.request(_make_ctx(op_id="op-l4"))

    asyncio.run(_request())

    from backend.core.ouroboros.governance.inline_approval import (
        get_default_queue,
    )
    pend = get_default_queue().next_pending()
    out = io.StringIO()
    result = run_inline_approval_loop(
        p, pend,
        stream_in=io.StringIO("n\n"),
        stream_out=out,
        timeout_s=1.0,
    )
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )
    assert result.status is ApprovalStatus.REJECTED
    lines = [
        json.loads(line) for line in temp_audit.read_text().splitlines()
        if line.strip()
    ]
    assert any(
        r["status"] == "REJECTED" and r["reason"] == "inline reject"
        for r in lines
    )


def test_livefire_full_loop_w_defers_writes_expired_audit(
    monkeypatch, temp_audit,
):
    """L5: `w` (wait/defer) → await_decision(0.0) → EXPIRED in ledger."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    reset_default_queue()
    p = build_approval_provider()

    async def _request():
        return await p.request(_make_ctx(op_id="op-l5"))

    asyncio.run(_request())

    from backend.core.ouroboros.governance.inline_approval import (
        get_default_queue,
    )
    pend = get_default_queue().next_pending()
    out = io.StringIO()
    result = run_inline_approval_loop(
        p, pend,
        stream_in=io.StringIO("w\n"),
        stream_out=out,
        timeout_s=1.0,
    )
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )
    assert result.status is ApprovalStatus.EXPIRED
    lines = [
        json.loads(line) for line in temp_audit.read_text().splitlines()
        if line.strip()
    ]
    assert any(r["status"] == "EXPIRED" for r in lines)


def test_livefire_render_block_displayed_in_output(monkeypatch, temp_audit):
    """L6: rendered prompt block is actually printed (operator sees diff
    + PROMPT_LABEL before being asked to decide)."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    reset_default_queue()
    p = build_approval_provider()

    async def _request():
        return await p.request(_make_ctx(op_id="op-l6"))

    asyncio.run(_request())

    from backend.core.ouroboros.governance.inline_approval import (
        get_default_queue,
    )
    pend = get_default_queue().next_pending()
    out = io.StringIO()
    run_inline_approval_loop(
        p, pend,
        diff_text="@@ -1 +1 @@\n-old\n+new",
        stream_in=io.StringIO("y\n"),
        stream_out=out,
        timeout_s=1.0,
    )
    rendered = out.getvalue()
    assert "[INLINE APPROVAL]" in rendered
    assert PROMPT_LABEL in rendered
    assert "@@ -1 +1 @@" in rendered


def test_livefire_render_block_function_alone_is_ascii_safe():
    """L7: render_request_block always emits ASCII (no surprise
    Unicode in the live-fire prompt that breaks strict-ASCII terms)."""
    from backend.core.ouroboros.governance.inline_approval import (
        InlineApprovalRequest,
    )
    import time
    req = InlineApprovalRequest(
        request_id="r", op_id="op-ascii", risk_tier="APPROVAL_REQUIRED",
        target_files=("a.py",), diff_summary="x",
        created_unix=time.time(), deadline_unix=time.time() + 30,
    )
    block = render_request_block(req, diff_text="hunk")
    block.encode("ascii")  # raises if any non-ASCII slipped in


def test_livefire_master_off_revert_uses_cli_provider(
    monkeypatch, temp_audit,
):
    """L8: hot-revert proven — master-off → CLIApprovalProvider, no
    audit ledger row written (CLI provider doesn't touch it)."""
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "false")
    reset_default_queue()
    p = build_approval_provider()
    assert isinstance(p, CLIApprovalProvider)

    async def _run():
        rid = await p.request(_make_ctx(op_id="op-l8"))
        return await p.approve(rid, "operator")

    result = asyncio.run(_run())
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )
    assert result.status is ApprovalStatus.APPROVED
    # No inline audit ledger row (CLI provider has its own paths).
    assert (
        not temp_audit.exists()
        or all(
            "op-l8" not in line
            for line in temp_audit.read_text().splitlines()
        )
    )


def test_livefire_audit_schema_version_pinned():
    """L9: audit schema version is the contracted v1."""
    assert AUDIT_LEDGER_SCHEMA_VERSION == 1


def test_livefire_audit_default_path_under_dot_jarvis(monkeypatch):
    """L10: default audit path lands under ``.jarvis/`` so operators
    can find it without env knob discovery."""
    monkeypatch.delenv("JARVIS_INLINE_APPROVAL_AUDIT_PATH", raising=False)
    from backend.core.ouroboros.governance.inline_approval_provider import (
        audit_ledger_path,
    )
    p = audit_ledger_path()
    assert p.parent.name == ".jarvis"
    assert p.name == "inline_approval_audit.jsonl"


def test_livefire_audit_io_failure_does_not_propagate(tmp_path):
    """L11: best-effort: read-only audit dir doesn't break the FSM."""
    bad = tmp_path / "ro" / "audit.jsonl"
    bad.parent.mkdir()
    bad.parent.chmod(0o400)
    try:
        ledger = _AuditLedger(bad)
        assert ledger.append({"x": 1}) is False  # never raises
    finally:
        bad.parent.chmod(0o700)


def test_livefire_provider_request_idempotent_on_op_id(monkeypatch, temp_audit):
    """L12: provider idempotency on op_id is preserved post-graduation."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    reset_default_queue()
    p = build_approval_provider()
    ctx = _make_ctx(op_id="op-l12")

    async def _run():
        a = await p.request(ctx)
        b = await p.request(ctx)
        return a, b

    a, b = asyncio.run(_run())
    assert a == b == "op-l12"


def test_livefire_inline_provider_implements_protocol(monkeypatch):
    """L13: Protocol conformance survives graduation — the orchestrator
    can swap factories without code-path changes."""
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalProvider,
    )
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    p = build_approval_provider()
    assert isinstance(p, ApprovalProvider)


def test_livefire_cli_provider_implements_protocol(monkeypatch):
    """L14: hot-revert path still satisfies Protocol (else swap breaks)."""
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalProvider,
    )
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "false")
    p = build_approval_provider()
    assert isinstance(p, ApprovalProvider)


def test_livefire_queue_singleton_inspectable_when_master_off(
    monkeypatch, temp_audit,
):
    """L15: even with master-off, the InlineApprovalQueue singleton is
    still constructible + inspectable. Operators can read prior
    decisions after a revert. Pinned because graduation explicitly
    promised this in inline_approval.get_default_queue() docstring."""
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "false")
    reset_default_queue()
    from backend.core.ouroboros.governance.inline_approval import (
        get_default_queue,
    )
    q = get_default_queue()
    assert isinstance(q, InlineApprovalQueue)
    assert len(q) == 0  # fresh


# ===========================================================================
# §F — Reachability supplement (factory hits both branches deterministically)
# ===========================================================================


def test_reachability_factory_branch_inline(monkeypatch):
    """W3(6)-style reachability supplement: both branches of the factory
    are deterministically reached without external timing."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    p = build_approval_provider()
    assert type(p).__name__ == "InlineApprovalProvider"


def test_reachability_factory_branch_cli(monkeypatch):
    monkeypatch.setenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", "false")
    p = build_approval_provider()
    assert type(p).__name__ == "CLIApprovalProvider"


def test_reachability_round_trip_request_to_audit(monkeypatch, temp_audit):
    """Reachability: a single request reaches the audit ledger when
    the operator decides — no orchestrator, no pipeline, no cron."""
    monkeypatch.delenv("JARVIS_APPROVAL_UX_INLINE_ENABLED", raising=False)
    reset_default_queue()
    p = build_approval_provider()

    async def _flow():
        rid = await p.request(_make_ctx(op_id="op-reach"))
        return await p.approve(rid, "operator")

    result = asyncio.run(_flow())
    from backend.core.ouroboros.governance.approval_provider import (
        ApprovalStatus,
    )
    assert result.status is ApprovalStatus.APPROVED
    text = temp_audit.read_text() if temp_audit.exists() else ""
    assert "op-reach" in text
    assert "APPROVED" in text
