"""Slice 13 — semantic value gate (RED first).

Run-22 operator verdict: the only landed commit was a duplicate
requirements.txt comment. A candidate is a benign NO_OP iff EVERY proposed
file change is MATHEMATICALLY cosmetic: Python proven by AST equality after
docstring stripping (comments never reach the AST — no regex), declared
line-grammar formats by whole-line comment/blank normalization per their
grammar spec. Fail-safe FORWARD (mandate 4): one byte of executable-logic
change, a syntax error on either side, an unknown format, or a new file all
PASS the gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.candidate_value_gate import (
    COSMETIC,
    INDETERMINATE,
    SUBSTANTIVE,
    classify_file_change,
    evaluate_candidate_value,
)


class TestPythonASTProof:
    def test_comment_only_change_is_cosmetic(self, tmp_path):
        old = "def f(x):\n    return x + 1\n"
        new = "# annotation noise\ndef f(x):\n    # more noise\n    return x + 1\n"
        (tmp_path / "m.py").write_text(old)
        assert classify_file_change(tmp_path, "m.py", new) == COSMETIC

    def test_docstring_only_change_is_cosmetic(self, tmp_path):
        old = 'def f(x):\n    """Old doc."""\n    return x + 1\n'
        new = 'def f(x):\n    """New, longer documentation."""\n    return x + 1\n'
        (tmp_path / "m.py").write_text(old)
        assert classify_file_change(tmp_path, "m.py", new) == COSMETIC

    def test_whitespace_reformat_is_cosmetic(self, tmp_path):
        old = "def f(x):\n    return x + 1\n"
        new = "def f(x):\n\n\n    return (x + 1)\n"
        (tmp_path / "m.py").write_text(old)
        assert classify_file_change(tmp_path, "m.py", new) == COSMETIC

    def test_single_constant_change_is_substantive(self, tmp_path):
        old = "TIMEOUT = 30\n"
        new = "# tuned\nTIMEOUT = 31\n"
        (tmp_path / "m.py").write_text(old)
        assert classify_file_change(tmp_path, "m.py", new) == SUBSTANTIVE

    def test_logic_change_beside_comments_is_substantive(self, tmp_path):
        old = "def f(x):\n    return x + 1\n"
        new = "# noise\ndef f(x):\n    return x + 2\n"
        (tmp_path / "m.py").write_text(old)
        assert classify_file_change(tmp_path, "m.py", new) == SUBSTANTIVE

    def test_syntax_error_in_new_is_indeterminate(self, tmp_path):
        (tmp_path / "m.py").write_text("def f(x):\n    return x\n")
        assert classify_file_change(
            tmp_path, "m.py", "def f(x:\n    broken",
        ) == INDETERMINATE

    def test_syntax_error_in_old_is_indeterminate(self, tmp_path):
        (tmp_path / "m.py").write_text("def broken(:\n")
        assert classify_file_change(
            tmp_path, "m.py", "def f(x):\n    return x\n",
        ) == INDETERMINATE

    def test_new_file_is_substantive(self, tmp_path):
        assert classify_file_change(
            tmp_path, "created.py", "x = 1\n",
        ) == SUBSTANTIVE

    def test_byte_identical_is_cosmetic(self, tmp_path):
        content = "def f():\n    return 1\n"
        (tmp_path / "m.py").write_text(content)
        assert classify_file_change(tmp_path, "m.py", content) == COSMETIC


class TestLineGrammarFormats:
    def test_requirements_comment_append_is_cosmetic(self, tmp_path):
        """THE Run-22 noise class: '# torch is N versions behind' appended."""
        old = "torch==2.5.1\nnumpy==1.26.0\n"
        new = (
            "# [Ouroboros] torch is 8 minor versions behind\n"
            "torch==2.5.1\n\nnumpy==1.26.0\n"
        )
        (tmp_path / "requirements.txt").write_text(old)
        assert classify_file_change(
            tmp_path, "requirements.txt", new,
        ) == COSMETIC

    def test_requirements_version_bump_is_substantive(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("torch==2.5.1\n")
        assert classify_file_change(
            tmp_path, "requirements.txt", "torch==2.13.0\n",
        ) == SUBSTANTIVE

    def test_cfg_comment_only_is_cosmetic(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[flake8]\nmax-line-length = 100\n")
        assert classify_file_change(
            tmp_path, "setup.cfg",
            "; tuning notes\n[flake8]\nmax-line-length = 100\n",
        ) == COSMETIC

    def test_requirements_trailing_comment_edit_is_cosmetic(self, tmp_path):
        """Run-23 grammar hole: the model's ASCII rewrites (em-dash→hyphen)
        live INSIDE trailing comments on requirement lines. Pip's grammar
        defines a comment as '#' at line start or preceded by whitespace —
        stripping the trailing comment is the format's own normalization,
        not a regex band-aid (mandate 1)."""
        old = (
            "z3-solver>=4.16.0  # Slice 129: optional — import-guarded\n"
            "cffi==2.0.0\n"
        )
        new = (
            "z3-solver>=4.16.0  # Slice 129: optional - import-guarded\n"
            "cffi==2.0.0\n"
        )
        (tmp_path / "requirements.txt").write_text(old)
        assert classify_file_change(
            tmp_path, "requirements.txt", new,
        ) == COSMETIC

    def test_requirements_spec_change_with_same_comment_is_substantive(
        self, tmp_path,
    ):
        (tmp_path / "requirements.txt").write_text(
            "z3-solver>=4.16.0  # pinned\n",
        )
        assert classify_file_change(
            tmp_path, "requirements.txt", "z3-solver>=4.17.0  # pinned\n",
        ) == SUBSTANTIVE

    def test_requirements_hash_inside_url_not_treated_as_comment(
        self, tmp_path,
    ):
        """Pip grammar: '#' NOT preceded by whitespace is content (egg
        fragments in VCS URLs). A change there must stay substantive."""
        (tmp_path / "requirements.txt").write_text(
            "git+https://x/y.git#egg=pkg1\n",
        )
        assert classify_file_change(
            tmp_path, "requirements.txt",
            "git+https://x/y.git#egg=pkg2\n",
        ) == SUBSTANTIVE

    def test_cfg_trailing_hash_is_NOT_stripped(self, tmp_path):
        """Trailing-strip is requirements-grammar ONLY — ini/cfg values may
        legitimately contain '#'; whole-line stripping stays their rule."""
        (tmp_path / "setup.cfg").write_text("[x]\nkey = a#b\n")
        assert classify_file_change(
            tmp_path, "setup.cfg", "[x]\nkey = a#c\n",
        ) == SUBSTANTIVE

    def test_unknown_format_is_indeterminate(self, tmp_path):
        (tmp_path / "notes.md").write_text("# heading\n")
        assert classify_file_change(
            tmp_path, "notes.md", "# heading\nnew line\n",
        ) == INDETERMINATE


class TestCandidateAggregation:
    def test_all_cosmetic_is_no_op(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "requirements.txt").write_text("torch==2.5.1\n")
        verdict, detail = evaluate_candidate_value(
            tmp_path,
            [("a.py", "# c\nx = 1\n"),
             ("requirements.txt", "# note\ntorch==2.5.1\n")],
        )
        assert verdict == COSMETIC
        assert len(detail) == 2

    def test_one_substantive_passes(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 1\n")
        verdict, _ = evaluate_candidate_value(
            tmp_path,
            [("a.py", "# c\nx = 1\n"), ("b.py", "y = 2\n")],
        )
        assert verdict == SUBSTANTIVE

    def test_indeterminate_passes_forward(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "notes.md").write_text("original\n")
        verdict, _ = evaluate_candidate_value(
            tmp_path,
            [("a.py", "# c\nx = 1\n"), ("notes.md", "changed\n")],
        )
        assert verdict == INDETERMINATE

    def test_empty_candidate_is_indeterminate(self, tmp_path):
        verdict, _ = evaluate_candidate_value(tmp_path, [])
        assert verdict == INDETERMINATE


class TestOrchestratorSeamWiring:
    ORCH = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/orchestrator.py"
    )

    def test_gate_wired_pre_validate(self):
        src = self.ORCH.read_text()
        i = src.index('if generation.is_noop:')
        j = src.index("VALIDATERunner delegation gate", i)
        seam = src[i:j]
        assert "evaluate_candidate_value" in seam, (
            "the value gate must sit at the single post-GENERATE / "
            "pre-VALIDATE seam both GENERATE paths rejoin — terminating "
            "cosmetic NO_OPs before the ~14s candidate tree, APPLY, and "
            "VERIFY are ever paid"
        )
        assert "no_op_cosmetic" in seam

    def test_no_audit_reject_markers_in_gate_emits(self):
        src = self.ORCH.read_text()
        i = src.index("evaluate_candidate_value")
        window = src[i:i + 3000]
        for marker in ("BLOCKED", "APPROVAL_REQUIRED",
                       "exploration_insufficient", "POLICY_DENIED"):
            assert marker not in window, (
                f"value-gate emits must never carry the audit reject "
                f"marker {marker!r} — the NO_OP terminal is benign"
            )

    def test_master_kill_switch_present(self):
        src = self.ORCH.read_text()
        assert "JARVIS_CANDIDATE_VALUE_GATE_ENABLED" in src