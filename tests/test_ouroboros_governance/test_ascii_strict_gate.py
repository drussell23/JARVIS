"""Tests for ``ascii_strict_gate`` — Iron Gate (Manifesto §6) defence
against ``rapidفuzz``-class Unicode-in-identifier typos.

Covers:
* Pure unit tests for ``scan_content``: ASCII clean, various bad
  codepoints, line/column math, offset tracking, sample cap.
* Pure unit tests for ``scan_candidate``: single-file shape,
  multi-file shape, raw_content fallback, malformed candidates.
* Formatters ``format_rejection_reason`` + ``build_retry_feedback``.
* ``AsciiStrictGate`` class behaviour: enable/disable, policy overrides,
  telemetry counter.
* Env-var integration (``JARVIS_ASCII_GATE``).
* Regression: the exact ``rapidفuzz`` typo from bt-2026-04-10-045911.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.ascii_strict_gate import (
    AsciiStrictGate,
    BadCodepoint,
    build_retry_feedback,
    format_rejection_reason,
    get_rejection_count,
    is_enabled,
    record_rejection,
    reset_rejection_count,
    scan_candidate,
    scan_content,
    scan_content_token_aware,
)


# ─────────────────────────────────────────────────────────────────────
# scan_content
# ─────────────────────────────────────────────────────────────────────


class TestScanContentPure:
    def test_ascii_clean_returns_empty(self):
        content = "import os\n\ndef foo(x: int) -> int:\n    return x + 1\n"
        assert scan_content(content, "foo.py") == []

    def test_empty_string_returns_empty(self):
        assert scan_content("", "empty.py") == []

    def test_non_string_returns_empty(self):
        # scan_content tolerates bad inputs rather than crashing.
        assert scan_content(None, "x.py") == []  # type: ignore[arg-type]
        assert scan_content(123, "x.py") == []  # type: ignore[arg-type]
        assert scan_content({"k": "v"}, "x.py") == []  # type: ignore[arg-type]

    def test_single_arabic_fa_detected(self):
        # The exact rapidفuzz case: Arabic fa (U+0641) replaces 'f'.
        content = "rapidفuzz==3.5.0\n"
        offenders = scan_content(content, "requirements.txt")
        assert len(offenders) == 1
        bc = offenders[0]
        assert bc.codepoint == 0x0641
        assert bc.char == "ف"
        assert bc.file_path == "requirements.txt"
        assert bc.line == 1
        # Column is 1-based, the fa is the 6th char (index 5).
        assert bc.column == 6
        assert bc.offset == 5

    def test_cyrillic_a_in_identifier(self):
        # Cyrillic 'а' (U+0430) — visually identical to Latin 'a'.
        content = "def lаunch():\n    pass\n"
        offenders = scan_content(content, "launcher.py")
        assert len(offenders) == 1
        assert offenders[0].codepoint == 0x0430

    def test_smart_quotes_flagged(self):
        # Curly quotes in what should be straight-quoted strings.
        content = 'print(\u201chello\u201d)\n'
        offenders = scan_content(content, "bad.py")
        # Two codepoints: U+201C and U+201D.
        assert len(offenders) == 2
        assert {bc.codepoint for bc in offenders} == {0x201C, 0x201D}

    def test_line_column_math_multi_line(self):
        content = "line1\nline2\nx = 'fоo'\n"  # Cyrillic 'о' on line 3
        offenders = scan_content(content, "x.py")
        assert len(offenders) == 1
        bc = offenders[0]
        assert bc.line == 3
        # "x = 'f" is 6 chars before the 'о', so col 7.
        assert bc.column == 7
        assert bc.codepoint == 0x043E

    def test_newline_does_not_advance_column(self):
        content = "a\nb"  # newline is ASCII — no offenders
        assert scan_content(content) == []

    def test_offset_tracking_first_match(self):
        # 100 chars of ASCII + 1 bad char.
        content = ("x" * 100) + "ف"
        offenders = scan_content(content)
        assert len(offenders) == 1
        assert offenders[0].offset == 100

    def test_max_samples_caps_results(self):
        content = "فففففففففف"  # 10 bad chars
        offenders = scan_content(content, "x.py", max_samples=3)
        assert len(offenders) == 3

    def test_max_samples_zero_returns_empty(self):
        content = "rapidفuzz"
        assert scan_content(content, max_samples=0) == []

    def test_default_sample_cap_is_five(self):
        content = "ف" * 20
        offenders = scan_content(content)
        assert len(offenders) == 5

    def test_bom_detected(self):
        # UTF-8 BOM (U+FEFF) at start of file.
        content = "\ufeffimport os\n"
        offenders = scan_content(content)
        assert len(offenders) == 1
        assert offenders[0].codepoint == 0xFEFF
        assert offenders[0].offset == 0

    def test_emoji_detected(self):
        content = "comment = '🚀'\n"  # single emoji codepoint in SMP
        offenders = scan_content(content)
        assert len(offenders) == 1
        assert offenders[0].codepoint == 0x1F680


class TestBadCodepointFormatting:
    def test_format_sample_contains_all_fields(self):
        bc = BadCodepoint(
            file_path="src/foo.py",
            offset=42,
            char="ف",
            codepoint=0x0641,
            line=3,
            column=7,
        )
        s = bc.format_sample()
        assert "src/foo.py" in s
        assert "@42" in s
        assert "U+0641" in s
        assert "L3" in s
        assert "C7" in s


# ─────────────────────────────────────────────────────────────────────
# scan_candidate — single + multi file shapes
# ─────────────────────────────────────────────────────────────────────


class TestScanCandidateSingleFile:
    def test_single_file_ascii_clean(self):
        candidate = {
            "file_path": "src/x.py",
            "full_content": "x = 1\n",
        }
        assert scan_candidate(candidate) == []

    def test_single_file_with_rapidfuzz_typo(self):
        candidate = {
            "file_path": "requirements.txt",
            "full_content": "rapidفuzz==3.5.0\n",
        }
        offenders = scan_candidate(candidate)
        assert len(offenders) == 1
        assert offenders[0].file_path == "requirements.txt"
        assert offenders[0].codepoint == 0x0641

    def test_raw_content_fallback_used_when_full_content_missing(self):
        candidate = {
            "file_path": "src/x.py",
            "raw_content": "fooف = 1\n",
        }
        offenders = scan_candidate(candidate)
        assert len(offenders) == 1

    def test_raw_content_fallback_when_full_content_empty(self):
        candidate = {
            "file_path": "src/x.py",
            "full_content": "",
            "raw_content": "fooف = 1\n",
        }
        offenders = scan_candidate(candidate)
        assert len(offenders) == 1

    def test_missing_file_path_uses_question_mark(self):
        candidate = {"full_content": "rapidفuzz\n"}
        offenders = scan_candidate(candidate)
        assert len(offenders) == 1
        assert offenders[0].file_path == "?"

    def test_non_dict_candidate_returns_empty(self):
        assert scan_candidate(None) == []  # type: ignore[arg-type]
        assert scan_candidate("a string") == []  # type: ignore[arg-type]
        assert scan_candidate([]) == []  # type: ignore[arg-type]


class TestScanCandidateMultiFile:
    def test_clean_multi_file(self):
        candidate = {
            "files": [
                {"file_path": "a.py", "full_content": "a = 1\n"},
                {"file_path": "b.py", "full_content": "b = 2\n"},
                {"file_path": "c.py", "full_content": "c = 3\n"},
            ],
        }
        assert scan_candidate(candidate) == []

    def test_unicode_in_file_2_caught(self):
        # File 1 is clean; file 2 has a typo. Must still be caught.
        candidate = {
            "files": [
                {"file_path": "clean.py", "full_content": "x = 1\n"},
                {"file_path": "reqs.txt", "full_content": "rapidفuzz\n"},
                {"file_path": "also_clean.py", "full_content": "y = 2\n"},
            ],
        }
        offenders = scan_candidate(candidate)
        assert len(offenders) == 1
        assert offenders[0].file_path == "reqs.txt"

    def test_unicode_in_multiple_files(self):
        candidate = {
            "files": [
                {"file_path": "a.py", "full_content": "defف():\n    pass\n"},
                {"file_path": "b.py", "full_content": "classа:\n    pass\n"},
            ],
        }
        offenders = scan_candidate(candidate)
        # Both files contribute offenders.
        assert len(offenders) >= 2
        file_paths = {bc.file_path for bc in offenders}
        assert "a.py" in file_paths
        assert "b.py" in file_paths

    def test_malformed_entries_skipped(self):
        candidate = {
            "files": [
                "a string, not a dict",
                {"file_path": "ok.py", "full_content": "rapidفuzz\n"},
                None,
                {"file_path": "x.py", "full_content": 42},  # content is int
            ],
        }
        offenders = scan_candidate(candidate)  # type: ignore[arg-type]
        assert len(offenders) == 1
        assert offenders[0].file_path == "ok.py"

    def test_empty_files_list_falls_through_to_single_file(self):
        candidate = {
            "files": [],
            "file_path": "fallback.py",
            "full_content": "فoo\n",
        }
        offenders = scan_candidate(candidate)
        assert len(offenders) == 1
        assert offenders[0].file_path == "fallback.py"

    def test_raw_content_fallback_in_multi_file(self):
        candidate = {
            "files": [
                {"file_path": "x.py", "raw_content": "rapidفuzz\n"},
            ],
        }
        offenders = scan_candidate(candidate)
        assert len(offenders) == 1


# ─────────────────────────────────────────────────────────────────────
# Formatters
# ─────────────────────────────────────────────────────────────────────


class TestFormatRejectionReason:
    def test_empty_list_still_returns_prefixed_string(self):
        result = format_rejection_reason([])
        assert result.startswith("ascii_corruption:")

    def test_prefix_is_load_bearing(self):
        # Orchestrator retry loop matches on this exact prefix — do not
        # change without updating orchestrator.py retry classifier.
        bc = BadCodepoint(
            "x.py", 0, "ف", 0x0641, 1, 1,
        )
        result = format_rejection_reason([bc])
        assert result.startswith("ascii_corruption:")

    def test_contains_sample_details(self):
        bc = BadCodepoint("x.py", 5, "ف", 0x0641, 1, 6)
        result = format_rejection_reason([bc])
        assert "x.py" in result
        assert "U+0641" in result

    def test_multiple_samples_joined(self):
        samples = [
            BadCodepoint("a.py", 0, "ف", 0x0641, 1, 1),
            BadCodepoint("b.py", 1, "а", 0x0430, 1, 2),
        ]
        result = format_rejection_reason(samples)
        assert "U+0641" in result
        assert "U+0430" in result


class TestBuildRetryFeedback:
    def test_mentions_unicode_corruption(self):
        bc = BadCodepoint("x.py", 0, "ف", 0x0641, 1, 1)
        feedback = build_retry_feedback([bc])
        assert "UNICODE CORRUPTION" in feedback

    def test_mentions_specific_codepoint(self):
        bc = BadCodepoint("x.py", 0, "ف", 0x0641, 1, 1)
        feedback = build_retry_feedback([bc])
        assert "U+0641" in feedback

    def test_mentions_rapidfuzz_example(self):
        # Concrete example in the feedback helps the model self-correct.
        bc = BadCodepoint("x.py", 0, "ف", 0x0641, 1, 1)
        feedback = build_retry_feedback([bc])
        assert "rapidfuzz" in feedback

    def test_caps_samples_at_five(self):
        samples = [
            BadCodepoint(f"f{i}.py", 0, "ف", 0x0641, 1, 1)
            for i in range(10)
        ]
        feedback = build_retry_feedback(samples)
        # Only first 5 filenames should appear.
        for i in range(5):
            assert f"f{i}.py" in feedback
        for i in range(5, 10):
            assert f"f{i}.py" not in feedback


# ─────────────────────────────────────────────────────────────────────
# AsciiStrictGate class
# ─────────────────────────────────────────────────────────────────────


class TestAsciiStrictGateClass:
    def setup_method(self):
        reset_rejection_count()

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ASCII_GATE", raising=False)
        gate = AsciiStrictGate()
        assert gate.enabled is True

    def test_enable_override_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ASCII_GATE", "false")
        gate = AsciiStrictGate(enabled=True)
        assert gate.enabled is True

    def test_disable_override_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ASCII_GATE", "true")
        gate = AsciiStrictGate(enabled=False)
        assert gate.enabled is False

    def test_disabled_gate_scan_returns_empty_even_for_bad_content(self):
        gate = AsciiStrictGate(enabled=False)
        candidate = {"file_path": "x.py", "full_content": "rapidفuzz\n"}
        assert gate.scan(candidate) == []

    def test_check_returns_ok_for_clean_candidate(self):
        gate = AsciiStrictGate()
        candidate = {"file_path": "x.py", "full_content": "x = 1\n"}
        ok, reason, samples = gate.check(candidate)
        assert ok is True
        assert reason is None
        assert samples == []

    def test_check_returns_failure_for_bad_candidate(self):
        gate = AsciiStrictGate()
        candidate = {"file_path": "x.py", "full_content": "rapidفuzz\n"}
        ok, reason, samples = gate.check(candidate)
        assert ok is False
        assert reason is not None
        assert reason.startswith("ascii_corruption:")
        assert len(samples) == 1

    def test_check_records_telemetry(self):
        gate = AsciiStrictGate()
        bad = {"file_path": "x.py", "full_content": "rapidفuzz\n"}
        clean = {"file_path": "y.py", "full_content": "y = 2\n"}

        before = get_rejection_count()
        gate.check(bad)
        after_bad = get_rejection_count()
        gate.check(clean)
        after_clean = get_rejection_count()

        assert after_bad > before
        # Clean candidate must not bump the counter.
        assert after_clean == after_bad

    def test_policy_max_samples_respected(self):
        # 8 offenders but max_samples=2 → only 2 samples returned.
        gate = AsciiStrictGate(max_samples=2)
        candidate = {"file_path": "x.py", "full_content": "ف" * 8}
        ok, _, samples = gate.check(candidate)
        assert ok is False
        assert len(samples) == 2


# ─────────────────────────────────────────────────────────────────────
# Telemetry counter
# ─────────────────────────────────────────────────────────────────────


class TestTelemetryCounter:
    def setup_method(self):
        reset_rejection_count()

    def test_initial_count_is_zero(self):
        assert get_rejection_count() == 0

    def test_record_rejection_increments(self):
        record_rejection()
        assert get_rejection_count() == 1
        record_rejection()
        assert get_rejection_count() == 2

    def test_record_rejection_with_samples(self):
        record_rejection(5)
        assert get_rejection_count() == 5

    def test_reset(self):
        record_rejection(10)
        reset_rejection_count()
        assert get_rejection_count() == 0


# ─────────────────────────────────────────────────────────────────────
# Env-var integration
# ─────────────────────────────────────────────────────────────────────


class TestIsEnabled:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ASCII_GATE", raising=False)
        assert is_enabled() is True

    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("JARVIS_ASCII_GATE", value)
        assert is_enabled() is True

    @pytest.mark.parametrize("value", ["false", "FALSE", "False", "0", "no", "off"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("JARVIS_ASCII_GATE", value)
        assert is_enabled() is False


# ─────────────────────────────────────────────────────────────────────
# Regression: exact failure from bt-2026-04-10-045911
# ─────────────────────────────────────────────────────────────────────


class TestRegressionBattleTest:
    """Reproduces the typo that slipped through before the gate was beefed up.

    Battle test session bt-2026-04-10-045911 saw the model emit
    ``rapidفuzz`` in requirements.txt — a visually-near-identical
    Unicode substitution that only surfaced at ``pip install`` time.
    The gate must catch this before APPLY.
    """

    def test_rapidfuzz_typo_caught_in_single_file_candidate(self):
        candidate = {
            "file_path": "requirements.txt",
            "full_content": (
                "# Auto-generated\n"
                "requests==2.31.0\n"
                "rapidفuzz==3.5.0\n"  # the typo
                "pytest==7.4.0\n"
            ),
        }
        gate = AsciiStrictGate()
        ok, reason, samples = gate.check(candidate)
        assert ok is False
        assert "rapidفuzz" not in (reason or "")  # reason summarises, not echoes
        assert samples[0].file_path == "requirements.txt"
        assert samples[0].codepoint == 0x0641
        # Typo is on line 3 of the synthetic content.
        assert samples[0].line == 3

    def test_rapidfuzz_typo_caught_in_multi_file_candidate(self):
        # Same typo but hiding in file 2 of a multi-file candidate.
        candidate = {
            "files": [
                {
                    "file_path": "src/main.py",
                    "full_content": "import requests\n",
                },
                {
                    "file_path": "requirements.txt",
                    "full_content": "rapidفuzz==3.5.0\n",
                },
            ],
        }
        gate = AsciiStrictGate()
        ok, reason, samples = gate.check(candidate)
        assert ok is False
        assert reason is not None and reason.startswith("ascii_corruption:")
        assert samples[0].file_path == "requirements.txt"

    def test_clean_requirements_passes(self):
        candidate = {
            "file_path": "requirements.txt",
            "full_content": (
                "requests==2.31.0\n"
                "rapidfuzz==3.5.0\n"
                "pytest==7.4.0\n"
            ),
        }
        gate = AsciiStrictGate()
        ok, reason, samples = gate.check(candidate)
        assert ok is True
        assert reason is None
        assert samples == []


# ─────────────────────────────────────────────────────────────────────
# Token-aware scan: scan_content_token_aware
# ─────────────────────────────────────────────────────────────────────


class TestTokenAwareScan:
    """Verify the token-aware scan honours the gate's stated policy:
    Unicode in string literals / comments is ALLOWED; Unicode in identifier
    positions is REJECTED (``rapidفuzz`` protection preserved).
    """

    def test_emoji_only_in_docstring_is_allowed(self):
        """📁 emoji in a module / function docstring must NOT be flagged.

        This is the repl_input_polish.py regression case: the file has emoji
        characters inside docstrings (U+1F4C1, U+1F517 etc.), which are
        meaningful Unicode in a *string literal* and cannot be a homoglyph
        attack. The whole-content scan incorrectly rejected them.
        """
        content = (
            'def show_files():\n'
            '    """📁 Show a list of files. 🔗 Click to open."""\n'
            '    return []\n'
        )
        offenders = scan_content_token_aware(content, "repl_input_polish.py")
        assert offenders == [], (
            "emoji in a docstring string literal must be allowed "
            f"(got {offenders})"
        )

    def test_cyrillic_in_identifier_is_rejected(self):
        """Cyrillic 'а' (U+0430) in an identifier must still be caught.

        This is the core ``rapidفuzz``-class threat: a Unicode letter that
        looks like an ASCII letter slips into an identifier position. The
        token-aware scan must flag it regardless of what surrounds it.
        """
        # 'lаunch' — Latin 'l', Cyrillic 'а' (U+0430), rest ASCII.
        content = "def lаunch():\n    pass\n"
        offenders = scan_content_token_aware(content, "launcher.py")
        assert len(offenders) == 1
        assert offenders[0].codepoint == 0x0430
        assert offenders[0].file_path == "launcher.py"

    def test_arabic_in_identifier_is_rejected(self):
        """Arabic ف (U+0641) in a name token must be flagged — the canonical
        ``rapidفuzz`` case."""
        content = "import rapidفuzz\n"
        offenders = scan_content_token_aware(content, "requirements.py")
        assert len(offenders) == 1
        assert offenders[0].codepoint == 0x0641

    def test_unicode_in_comment_is_allowed(self):
        """Unicode in a ``#`` comment must not be flagged.

        Comments are ``COMMENT`` tokens and cannot occupy identifier positions.
        """
        content = "x = 1  # résumé 📎 done\n"
        offenders = scan_content_token_aware(content, "foo.py")
        assert offenders == [], (
            f"Unicode in comment must be allowed (got {offenders})"
        )

    def test_unicode_in_string_literal_is_allowed(self):
        """Emoji inside a regular string literal must be allowed."""
        content = "msg = '🚀 launch complete'\n"
        offenders = scan_content_token_aware(content, "status.py")
        assert offenders == [], (
            f"emoji in string literal must be allowed (got {offenders})"
        )

    def test_unparseable_python_falls_back_to_whole_content_scan(self):
        """Unclosed string → TokenError → falls back to conservative
        whole-content scan, which still rejects the non-ASCII character.

        This is the critical safety invariant: a syntactically broken file
        cannot smuggle homoglyphs past the gate by triggering a parse error.
        """
        # Unclosed string literal — tokenize will raise TokenError.
        content = "x = 'unclosed\nfubar = rapidفuzz\n"
        offenders = scan_content_token_aware(content, "broken.py")
        # Falls back to whole-content scan → Arabic ف must still be found.
        assert any(bc.codepoint == 0x0641 for bc in offenders), (
            "unparseable Python must fall back to whole-content scan (rejected)"
        )

    def test_ascii_only_content_returns_empty(self):
        """Pure ASCII content fast-paths and returns empty list."""
        content = "def add(a, b):\n    return a + b\n"
        assert scan_content_token_aware(content, "math.py") == []

    def test_non_ascii_only_in_docstring_multi_line(self):
        """Multi-line docstring with emoji on several lines — all allowed."""
        content = (
            'class Foo:\n'
            '    """Bar.\n\n'
            '    📌 Note: see §6 for details.\n'
            '    Arrows: → left, ← right.\n'
            '    """\n'
            '    pass\n'
        )
        offenders = scan_content_token_aware(content, "foo.py")
        assert offenders == [], (
            f"multi-line docstring Unicode must be allowed (got {offenders})"
        )

    def test_max_samples_respected(self):
        """max_samples cap applies to token-aware scan too."""
        # Three identifiers each with non-ASCII: only 2 should be returned.
        content = "а = 1\nб = 2\nв = 3\n"  # Cyrillic variable names
        offenders = scan_content_token_aware(content, "vars.py", max_samples=2)
        assert len(offenders) == 2

    def test_line_column_accuracy_in_identifier(self):
        """Offender line / column must be correct for identifier position."""
        content = "x = 1\ny = rаpid\n"  # 'а' is Cyrillic, on line 2
        offenders = scan_content_token_aware(content, "x.py")
        assert len(offenders) == 1
        bc = offenders[0]
        assert bc.line == 2
        # 'r' is col 5 (1-based), 'а' is at index 1 within token 'rаpid' → col 6
        assert bc.column == 6
        assert bc.codepoint == 0x0430


# ─────────────────────────────────────────────────────────────────────
# scan_candidate token-aware routing
# ─────────────────────────────────────────────────────────────────────


class TestScanCandidateTokenAware:
    """scan_candidate must route .py files through the token-aware scan and
    non-.py files through the conservative whole-content scan."""

    def test_py_file_with_docstring_emoji_passes(self):
        """A .py file whose only non-ASCII is emoji in a docstring → clean."""
        candidate = {
            "file_path": "src/ui.py",
            "full_content": (
                'def render():\n'
                '    """📁 Render the UI."""\n'
                '    return ""\n'
            ),
        }
        offenders = scan_candidate(candidate)
        assert offenders == [], (
            "scan_candidate must allow emoji in .py docstrings "
            f"(got {offenders})"
        )

    def test_py_file_with_identifier_unicode_rejected(self):
        """A .py file with Cyrillic in an identifier must still be rejected."""
        candidate = {
            "file_path": "src/core.py",
            "full_content": "def rаpid():\n    pass\n",
        }
        offenders = scan_candidate(candidate)
        assert len(offenders) == 1
        assert offenders[0].codepoint == 0x0430

    def test_non_py_file_whole_content_scan(self):
        """A non-.py file with any non-ASCII is rejected by whole-content scan."""
        candidate = {
            "file_path": "config.yaml",
            "full_content": "# résumé\nname: foo\n",
        }
        offenders = scan_candidate(candidate)
        # 'é' (U+00E9) must be flagged via whole-content scan.
        assert any(bc.codepoint == 0x00E9 for bc in offenders)

    def test_multi_file_py_docstring_allowed_txt_identifier_rejected(self):
        """Multi-file candidate: .py docstring passes, .txt Arabic fails."""
        candidate = {
            "files": [
                {
                    "file_path": "src/app.py",
                    "full_content": 'def go():\n    """🚀 Launch."""\n    pass\n',
                },
                {
                    "file_path": "requirements.txt",
                    "full_content": "rapidفuzz==3.5.0\n",
                },
            ],
        }
        offenders = scan_candidate(candidate)
        # Only the .txt file's Arabic fa must be flagged.
        assert len(offenders) == 1
        assert offenders[0].file_path == "requirements.txt"
        assert offenders[0].codepoint == 0x0641

    def test_gate_check_py_docstring_emoji_passes(self):
        """AsciiStrictGate.check passes a .py file with emoji in docstring."""
        gate = AsciiStrictGate(auto_repair=False)
        candidate = {
            "file_path": "src/repl.py",
            "full_content": (
                'def polish():\n'
                '    """📌 Polishes the REPL input. 🔗"""\n'
                '    return True\n'
            ),
        }
        ok, reason, samples = gate.check(candidate)
        assert ok is True, (
            f"gate.check must pass .py with docstring emoji (reason={reason!r})"
        )
