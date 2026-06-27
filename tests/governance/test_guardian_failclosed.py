"""Tests for SemanticGuardian per-pattern fail-closed + non-Python shell-exec detection.

Task 1 of anti-venom hardening:
  S4 closure — per-pattern crash yields a HARD *_eval_failed finding (never swallowed).
  S6 closure — shell-exec calls in non-Python files (.sh/.yaml/.pth) are detected.
"""
from __future__ import annotations

import backend.core.ouroboros.governance.semantic_guardian as sg


def test_pattern_eval_crash_yields_hard_finding(monkeypatch):
    """A crashing pattern detector must produce a HARD *_eval_failed finding.

    Injects a boom callable for the first registered pattern and asserts
    inspect() returns a Detection whose pattern ends with '_eval_failed'
    and whose severity is 'hard'.
    """
    def boom(**_kwargs):
        raise RuntimeError("detector boom")

    # Patch the first pattern in _PATTERNS to raise.
    first_pat = next(iter(sg._PATTERNS))
    monkeypatch.setitem(sg._PATTERNS, first_pat, boom)

    g = sg.SemanticGuardian()
    findings = g.inspect(file_path="x.py", old_content="a=1\n", new_content="a=2\n")

    eval_failed = [d for d in findings if d.pattern.endswith("_eval_failed")]
    assert eval_failed, (
        f"Expected at least one *_eval_failed finding but got: {findings}"
    )
    assert eval_failed[0].severity == "hard", (
        f"Expected severity='hard' on eval_failed finding, got: {eval_failed[0].severity}"
    )
    assert eval_failed[0].pattern == f"{first_pat}_eval_failed", (
        f"Expected pattern='{first_pat}_eval_failed', got: {eval_failed[0].pattern}"
    )


def test_shell_exec_in_sh_is_detected():
    """Shell-exec call introduced in a non-Python (.sh) file must be flagged hard.

    deploy.sh is not valid Python so _safe_parse returns None for it;
    the regex-based shell_exec_introduced pattern must still fire.
    """
    g = sg.SemanticGuardian()
    # NOTE: "os.system(...)" here is a plain string literal passed as new_content;
    # it is the *test-input text* the guardian must flag — it is never executed.
    findings = g.inspect(
        file_path="deploy.sh",
        old_content="",
        new_content="os.system('rm -rf /')\n",
    )
    shell_findings = [d for d in findings if "shell_exec" in d.pattern]
    assert shell_findings, (
        f"Expected a shell_exec* finding on deploy.sh but got: {findings}"
    )
    assert shell_findings[0].severity == "hard", (
        f"Expected severity='hard', got: {shell_findings[0].severity}"
    )


def test_shell_exec_delta_gated_no_false_positive():
    """No false-positive when old content already contained the shell-exec call."""
    g = sg.SemanticGuardian()
    # NOTE: string literal as test-input text only — never executed.
    existing = "os.system('echo hello')\n"
    findings = g.inspect(
        file_path="deploy.sh",
        old_content=existing,
        new_content=existing,
    )
    shell_findings = [d for d in findings if "shell_exec" in d.pattern]
    assert not shell_findings, (
        f"Expected NO shell_exec finding when delta=0, got: {shell_findings}"
    )
