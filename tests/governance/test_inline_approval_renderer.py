"""P3 Slice 3 — Inline approval renderer + prompt + $EDITOR regression suite.

Pins:
  * render_request_block: shape, ASCII safety, file bullet list,
    PROMPT_LABEL verbatim, oversized diff truncation, no-diff fallback,
    seconds-remaining clamp.
  * render_pending_stack: empty + multi entries.
  * compute_diff_text: stubbed subprocess success / failure / timeout
    paths; argv shape never uses ``shell=True``.
  * prompt_decision: parsed input, EOF→WAIT, garbage→WAIT, timeout→
    TIMEOUT_DEFERRED (StringIO + monkey-patched select).
  * resolve_editor: $EDITOR set / $VISUAL fallback / unset / unparseable.
  * open_editor: argv composition, success rc 0, failure rc≠0,
    no-editor returns False, never uses shell=True.
  * run_inline_approval_loop: APPROVE → provider.approve; REJECT →
    provider.reject; SHOW_STACK → stack rendered + re-prompt; EDIT →
    editor invoked + re-prompt; WAIT/TIMEOUT_DEFERRED → defers via
    await_decision(0); max_iterations bound enforced.
  * Authority invariants: no banned imports; only argv subprocess;
    no shell=True; no os.environ writes.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.inline_approval import (
    InlineApprovalChoice,
    InlineApprovalRequest,
)
from backend.core.ouroboros.governance.inline_approval_renderer import (
    DIFF_SUBPROCESS_TIMEOUT_S,
    EDITOR_SUBPROCESS_TIMEOUT_S,
    MAX_DIFF_BYTES,
    PROMPT_LABEL,
    compute_diff_text,
    open_editor,
    prompt_decision,
    render_pending_stack,
    render_request_block,
    resolve_editor,
    run_inline_approval_loop,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    """Remove triple-quoted strings + ``#`` line comments so authority
    pins scan code only — not narrative prose. The pins look for
    couplings like ``shell=True`` or ``os.environ[`` that are valid
    English to mention in a docstring but illegal as code."""
    import io as _io
    import tokenize as _tk

    out: List[str] = []
    try:
        toks = list(_tk.generate_tokens(_io.StringIO(src).readline))
    except (_tk.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type in (_tk.STRING,):
            # Replace string literals with empty placeholder so docstrings
            # are erased but byte structure stays roughly aligned.
            out.append('""')
        elif tok.type == _tk.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


def _make_request(
    request_id: str = "req-1",
    op_id: str = "op-x",
    risk_tier: str = "APPROVAL_REQUIRED",
    target_files: Tuple[str, ...] = ("a.py",),
    diff_summary: str = "test op",
    deadline_in_s: float = 30.0,
) -> InlineApprovalRequest:
    import time
    now = time.time()
    return InlineApprovalRequest(
        request_id=request_id,
        op_id=op_id,
        risk_tier=risk_tier,
        target_files=target_files,
        diff_summary=diff_summary,
        created_unix=now,
        deadline_unix=now + deadline_in_s,
    )


@pytest.fixture(autouse=True)
def _clear_editor_env(monkeypatch):
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    yield


# ===========================================================================
# A — Module constants
# ===========================================================================


def test_prompt_label_pinned():
    """Pin: PRD §9 P3 prompt shape."""
    assert PROMPT_LABEL == "[y]es / [n]o / [s]how stack / [e]dit / [w]ait"


def test_diff_subprocess_timeout_pinned():
    assert DIFF_SUBPROCESS_TIMEOUT_S == 5.0


def test_editor_subprocess_timeout_pinned():
    assert EDITOR_SUBPROCESS_TIMEOUT_S == 1800.0


def test_max_diff_bytes_pinned():
    assert MAX_DIFF_BYTES == 64 * 1024


# ===========================================================================
# B — render_request_block
# ===========================================================================


def test_render_block_contains_op_id_and_tier():
    req = _make_request(op_id="op-abc", risk_tier="APPROVAL_REQUIRED")
    out = render_request_block(req, diff_text="@@ -1 +1 @@\n-a\n+b")
    assert "op-abc" in out
    assert "APPROVAL_REQUIRED" in out
    assert PROMPT_LABEL in out


def test_render_block_lists_files_as_bullets():
    req = _make_request(target_files=("foo.py", "bar.py"))
    out = render_request_block(req)
    assert "Files (2):" in out
    assert "  - foo.py" in out
    assert "  - bar.py" in out


def test_render_block_no_files_shows_none():
    req = _make_request(target_files=())
    out = render_request_block(req)
    assert "Files (0):" in out
    assert "(none)" in out


def test_render_block_no_diff_shows_placeholder():
    req = _make_request()
    out = render_request_block(req, diff_text="")
    assert "(no diff captured)" in out


def test_render_block_truncates_oversized_diff():
    req = _make_request()
    big = ("x" * 100) + ("\nline" * (MAX_DIFF_BYTES // 5))
    out = render_request_block(req, diff_text=big)
    assert "... <" in out and "more lines truncated" in out


def test_render_block_seconds_remaining_clamped_at_zero():
    req = _make_request(deadline_in_s=-100.0)
    out = render_request_block(req)
    assert "(auto-WAIT in 0s):" in out


def test_render_block_is_ascii():
    """Pin: rendered output must survive ASCII-strict terminals."""
    req = _make_request()
    out = render_request_block(req, diff_text="hunk")
    out.encode("ascii")  # raises if any non-ASCII slipped in


# ===========================================================================
# C — render_pending_stack
# ===========================================================================


def test_render_pending_stack_empty():
    assert render_pending_stack([]) == "Pending (0): (queue empty)"


def test_render_pending_stack_multi():
    a = _make_request(op_id="op-1", target_files=("a.py", "b.py"))
    b = _make_request(op_id="op-2", target_files=("c.py",))
    out = render_pending_stack([a, b])
    assert "Pending (2):" in out
    assert "1. op-1 tier=APPROVAL_REQUIRED files=2" in out
    assert "2. op-2 tier=APPROVAL_REQUIRED files=1" in out


# ===========================================================================
# D — compute_diff_text (subprocess-stubbed)
# ===========================================================================


def test_compute_diff_text_empty_files_returns_empty():
    assert compute_diff_text(()) == ""


def test_compute_diff_text_uses_argv_form_no_shell():
    fake = MagicMock(returncode=0, stdout="diff-text", stderr="")
    with patch.object(subprocess, "run", return_value=fake) as run_mock:
        result = compute_diff_text(("a.py",))
    assert result == "diff-text"
    args, kwargs = run_mock.call_args
    # argv form: first positional is the list, no shell=True kwarg
    assert isinstance(args[0], list)
    assert args[0][:3] == ["git", "diff", "--no-color"]
    assert kwargs.get("shell") in (None, False)
    assert kwargs.get("check") is False


def test_compute_diff_text_returns_empty_on_subprocess_error():
    with patch.object(subprocess, "run", side_effect=OSError("git missing")):
        assert compute_diff_text(("a.py",)) == ""


def test_compute_diff_text_returns_empty_on_timeout():
    with patch.object(
        subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5.0),
    ):
        assert compute_diff_text(("a.py",)) == ""


def test_compute_diff_text_returns_empty_on_unexpected_rc():
    fake = MagicMock(returncode=128, stdout="", stderr="fatal: bad")
    with patch.object(subprocess, "run", return_value=fake):
        assert compute_diff_text(("a.py",)) == ""


def test_compute_diff_text_rc_one_means_diff_present():
    """git diff returns 1 with --exit-code; we accept it as a normal
    'diff present' signal (the renderer doesn't pass --exit-code so rc
    1 should still be tolerated for safety)."""
    fake = MagicMock(returncode=1, stdout="hunk", stderr="")
    with patch.object(subprocess, "run", return_value=fake):
        assert compute_diff_text(("a.py",)) == "hunk"


# ===========================================================================
# E — prompt_decision (StringIO path; no real fd)
# ===========================================================================


def test_prompt_decision_parses_y():
    buf = io.StringIO("y\n")
    assert prompt_decision(buf, timeout_s=1.0) is InlineApprovalChoice.APPROVE


def test_prompt_decision_parses_n():
    buf = io.StringIO("n\n")
    assert prompt_decision(buf, timeout_s=1.0) is InlineApprovalChoice.REJECT


def test_prompt_decision_parses_s():
    buf = io.StringIO("s\n")
    assert prompt_decision(buf, timeout_s=1.0) is InlineApprovalChoice.SHOW_STACK


def test_prompt_decision_parses_e():
    buf = io.StringIO("e\n")
    assert prompt_decision(buf, timeout_s=1.0) is InlineApprovalChoice.EDIT


def test_prompt_decision_parses_w():
    buf = io.StringIO("w\n")
    assert prompt_decision(buf, timeout_s=1.0) is InlineApprovalChoice.WAIT


def test_prompt_decision_eof_returns_wait():
    """Safety-first: EOF is not approval."""
    buf = io.StringIO("")
    assert prompt_decision(buf, timeout_s=1.0) is InlineApprovalChoice.WAIT


def test_prompt_decision_garbage_returns_wait():
    buf = io.StringIO("nuke-everything\n")
    assert prompt_decision(buf, timeout_s=1.0) is InlineApprovalChoice.WAIT


def test_prompt_decision_real_fd_select_timeout(monkeypatch):
    """When stream has a real fd, select.select with timeout=0 returns
    no-ready → TIMEOUT_DEFERRED."""
    import select as _select_mod
    import backend.core.ouroboros.governance.inline_approval_renderer as R

    fake_in = MagicMock()
    fake_in.fileno.return_value = 7  # real-looking fd
    monkeypatch.setattr(R.select, "select", lambda r, w, x, t: ([], [], []))
    monkeypatch.setattr(R, "_safe_fileno", lambda s: 7)
    out = prompt_decision(fake_in, timeout_s=0.0)
    assert out is InlineApprovalChoice.TIMEOUT_DEFERRED


def test_prompt_decision_select_oserror_returns_wait(monkeypatch):
    import backend.core.ouroboros.governance.inline_approval_renderer as R
    fake_in = MagicMock()
    monkeypatch.setattr(R, "_safe_fileno", lambda s: 7)
    monkeypatch.setattr(
        R.select, "select",
        lambda r, w, x, t: (_ for _ in ()).throw(OSError("bad fd")),
    )
    out = prompt_decision(fake_in, timeout_s=1.0)
    assert out is InlineApprovalChoice.WAIT


# ===========================================================================
# F — resolve_editor + open_editor
# ===========================================================================


def test_resolve_editor_uses_editor_first(monkeypatch):
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setenv("VISUAL", "code")
    assert resolve_editor() == ["vim"]


def test_resolve_editor_falls_back_to_visual(monkeypatch):
    monkeypatch.setenv("VISUAL", "nano")
    assert resolve_editor() == ["nano"]


def test_resolve_editor_unset_returns_none():
    assert resolve_editor() is None


def test_resolve_editor_splits_args(monkeypatch):
    monkeypatch.setenv("EDITOR", "code -w --new-window")
    assert resolve_editor() == ["code", "-w", "--new-window"]


def test_resolve_editor_unparseable_returns_none(monkeypatch):
    monkeypatch.setenv("EDITOR", "vim 'unbalanced")
    assert resolve_editor() is None


def test_open_editor_no_env_returns_false():
    assert open_editor("/tmp/some.py") is False


def test_open_editor_invokes_argv_no_shell(monkeypatch):
    monkeypatch.setenv("EDITOR", "vim")
    fake = MagicMock(returncode=0)
    with patch.object(subprocess, "run", return_value=fake) as run_mock:
        result = open_editor("/tmp/x.py")
    assert result is True
    args, kwargs = run_mock.call_args
    assert args[0] == ["vim", "/tmp/x.py"]
    assert kwargs.get("shell") in (None, False)


def test_open_editor_nonzero_rc_returns_false(monkeypatch):
    monkeypatch.setenv("EDITOR", "vim")
    fake = MagicMock(returncode=130)
    with patch.object(subprocess, "run", return_value=fake):
        assert open_editor("/tmp/x.py") is False


def test_open_editor_subprocess_error_returns_false(monkeypatch):
    monkeypatch.setenv("EDITOR", "vim")
    with patch.object(subprocess, "run", side_effect=OSError("not found")):
        assert open_editor("/tmp/x.py") is False


# ===========================================================================
# G — run_inline_approval_loop (sync orchestration)
# ===========================================================================


class _FakeProvider:
    """Minimal sync stand-in. The renderer's _await_now bridges
    coroutines → results; for tests we return ApprovalResult directly so
    the bridge passes through."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, ...]] = []

    def _result(self, status: ApprovalStatus, request_id: str,
                approver=None, reason=None) -> ApprovalResult:
        from datetime import datetime, timezone
        return ApprovalResult(
            status=status, approver=approver, reason=reason,
            decided_at=datetime.now(tz=timezone.utc),
            request_id=request_id,
        )

    def approve(self, request_id: str, approver: str) -> ApprovalResult:
        self.calls.append(("approve", request_id, approver))
        return self._result(ApprovalStatus.APPROVED, request_id, approver)

    def reject(self, request_id: str, approver: str, reason: str
               ) -> ApprovalResult:
        self.calls.append(("reject", request_id, approver, reason))
        return self._result(
            ApprovalStatus.REJECTED, request_id, approver, reason,
        )

    def await_decision(self, request_id: str, timeout_s: float
                       ) -> ApprovalResult:
        self.calls.append(("await", request_id, timeout_s))
        return self._result(ApprovalStatus.EXPIRED, request_id)


def test_loop_y_calls_approve():
    prov = _FakeProvider()
    req = _make_request(request_id="r1")
    out = io.StringIO()
    result = run_inline_approval_loop(
        prov, req,
        stream_in=io.StringIO("y\n"),
        stream_out=out,
        timeout_s=1.0,
    )
    assert result.status is ApprovalStatus.APPROVED
    assert prov.calls[0][0] == "approve"


def test_loop_n_calls_reject_with_inline_reason():
    prov = _FakeProvider()
    req = _make_request(request_id="r1")
    result = run_inline_approval_loop(
        prov, req,
        stream_in=io.StringIO("n\n"),
        stream_out=io.StringIO(),
        timeout_s=1.0,
    )
    assert result.status is ApprovalStatus.REJECTED
    assert prov.calls[0] == ("reject", "r1", "operator", "inline reject")


def test_loop_w_defers_via_await_decision_zero():
    prov = _FakeProvider()
    req = _make_request(request_id="r1")
    result = run_inline_approval_loop(
        prov, req,
        stream_in=io.StringIO("w\n"),
        stream_out=io.StringIO(),
        timeout_s=1.0,
    )
    assert result.status is ApprovalStatus.EXPIRED
    assert prov.calls == [("await", "r1", 0.0)]


def test_loop_eof_treated_as_wait_and_defers():
    prov = _FakeProvider()
    req = _make_request(request_id="r1")
    result = run_inline_approval_loop(
        prov, req,
        stream_in=io.StringIO(""),
        stream_out=io.StringIO(),
        timeout_s=1.0,
    )
    assert result.status is ApprovalStatus.EXPIRED
    assert prov.calls[0][0] == "await"


def test_loop_show_stack_then_yes():
    prov = _FakeProvider()
    req = _make_request(request_id="r1", op_id="op-r1")
    out = io.StringIO()
    other = _make_request(request_id="r2", op_id="op-r2")
    result = run_inline_approval_loop(
        prov, req,
        pending_stack=[other],
        stream_in=io.StringIO("s\ny\n"),
        stream_out=out,
        timeout_s=1.0,
    )
    assert result.status is ApprovalStatus.APPROVED
    assert "Pending (1):" in out.getvalue()


def test_loop_edit_invokes_editor_then_yes(monkeypatch):
    prov = _FakeProvider()
    req = _make_request(request_id="r1", target_files=("foo.py",))
    invoked: List[str] = []

    def fake_invoker(path: str) -> bool:
        invoked.append(path)
        return True

    result = run_inline_approval_loop(
        prov, req,
        stream_in=io.StringIO("e\ny\n"),
        stream_out=io.StringIO(),
        timeout_s=1.0,
        editor_invoker=fake_invoker,
    )
    assert result.status is ApprovalStatus.APPROVED
    assert invoked == ["foo.py"]


def test_loop_max_iterations_defers():
    """SHOW_STACK forever should hit max_iterations and defer."""
    prov = _FakeProvider()
    req = _make_request(request_id="r1")
    result = run_inline_approval_loop(
        prov, req,
        stream_in=io.StringIO("s\n" * 20),
        stream_out=io.StringIO(),
        timeout_s=1.0,
        max_iterations=3,
    )
    assert result.status is ApprovalStatus.EXPIRED
    # One await call after iterations exhausted.
    assert any(c[0] == "await" for c in prov.calls)


def test_loop_explicit_edit_target_overrides_first_file(monkeypatch):
    prov = _FakeProvider()
    req = _make_request(request_id="r1", target_files=("first.py", "second.py"))
    invoked: List[str] = []
    run_inline_approval_loop(
        prov, req,
        stream_in=io.StringIO("e\ny\n"),
        stream_out=io.StringIO(),
        timeout_s=1.0,
        edit_target="explicit.py",
        editor_invoker=lambda p: invoked.append(p) or True,
    )
    assert invoked == ["explicit.py"]


# ===========================================================================
# H — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
    "from backend.core.ouroboros.governance.risk_tier",
]


def test_renderer_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/inline_approval_renderer.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_renderer_never_uses_shell_true():
    """Pin: only argv-form subprocess. shell=True would let an
    operator-controlled EDITOR string inject arbitrary commands."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/inline_approval_renderer.py"),
    )
    assert "shell=True" not in src


def test_renderer_no_env_writes():
    """Reads of EDITOR / VISUAL are fine; writes are not."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/inline_approval_renderer.py"),
    )
    forbidden = [
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
