"""Priority A Slice A1 — Default-claim evaluators regression spine.

Three deterministic evaluators back the mandatory `must_hold` claims
that every op captures at PLAN exit. Without these, every
verification_postmortem record has total_claims=0 and Phase 2 is
theatrical.

Pins:
  §1   All three new evaluators registered at module load
  §2   `file_parses_after_change` — happy path: every .py file parses
  §3   `file_parses_after_change` — fails on SyntaxError with
       deterministic reason (line + offset + truncated message)
  §4   `file_parses_after_change` — non-Python files (yaml/json/md)
       are silently skipped (no false-fail)
  §5   `file_parses_after_change` — INSUFFICIENT_EVIDENCE on missing
       target_files_post key
  §6   `file_parses_after_change` — bytes content decoded as utf-8
  §7   `file_parses_after_change` — malformed entries (missing path)
       skipped without raising
  §8   `file_parses_after_change` — empty file list passes
  §9   `test_set_hash_stable` — happy path: post is superset of pre
  §10  `test_set_hash_stable` — fails on file removal/rename with
       missing-list (truncated to 5)
  §11  `test_set_hash_stable` — additions OK (more files in post)
  §12  `test_set_hash_stable` — INSUFFICIENT_EVIDENCE on missing key
  §13  `test_set_hash_stable` — sha digests in reason for replay
  §14  `no_new_credential_shapes` — happy path: clean diff
  §15  `no_new_credential_shapes` — detects each of the 5 patterns
       (sk-*, AKIA*, ghp_*, xoxb-*, PEM)
  §16  `no_new_credential_shapes` — secret value NEVER echoed in
       reason (defensive — only pattern + offset)
  §17  `no_new_credential_shapes` — INSUFFICIENT_EVIDENCE on missing
       diff_text
  §18  `no_new_credential_shapes` — bytes diff decoded as utf-8
  §19  `no_new_credential_shapes` — empty diff_text passes
  §20  `no_new_credential_shapes` — reuses canonical
       `_CREDENTIAL_SHAPE_PATTERNS` from semantic_firewall (single
       source of truth — no pattern duplication in property_oracle)
  §21  All three evaluators NEVER raise (defensive — exception →
       caller sees EVALUATOR_ERROR via dispatch wrapper)
  §22  All three evaluators return PropertyVerdict with confidence=1.0
       on definitive verdicts (PASSED / FAILED)
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.verification.property_oracle import (
    Property,
    PropertyVerdict,
    VerdictKind,
    _eval_file_parses_after_change,
    _eval_no_new_credential_shapes,
    _eval_test_set_hash_stable,
    is_kind_registered,
    known_kinds,
)


def _prop(kind: str, name: str = "default") -> Property:
    return Property.make(kind=kind, name=name)


# ===========================================================================
# §1 — Registration
# ===========================================================================


@pytest.mark.parametrize(
    "kind",
    [
        "file_parses_after_change",
        "test_set_hash_stable",
        "no_new_credential_shapes",
    ],
)
def test_evaluator_registered_at_module_load(kind) -> None:
    assert is_kind_registered(kind), f"{kind!r} must be registered"
    assert kind in known_kinds()


# ===========================================================================
# §2-§8 — file_parses_after_change
# ===========================================================================


def test_file_parses_happy_path() -> None:
    evidence = {
        "target_files_post": [
            {"path": "a.py", "content": "x = 1\n"},
            {"path": "b.py", "content": "def f():\n    pass\n"},
        ]
    }
    v = _eval_file_parses_after_change(_prop("file_parses_after_change"), evidence)
    assert v.verdict is VerdictKind.PASSED
    assert "parsed_ok=2" in v.reason
    assert v.confidence == 1.0


def test_file_parses_fails_on_syntax_error() -> None:
    evidence = {
        "target_files_post": [
            {"path": "good.py", "content": "x = 1\n"},
            {"path": "bad.py", "content": "def f(:\n    pass\n"},  # SyntaxError
        ]
    }
    v = _eval_file_parses_after_change(_prop("file_parses_after_change"), evidence)
    assert v.verdict is VerdictKind.FAILED
    assert "SyntaxError" in v.reason
    assert "bad.py" in v.reason
    assert v.confidence == 1.0


def test_file_parses_skips_non_python() -> None:
    evidence = {
        "target_files_post": [
            {"path": "config.yaml", "content": "::: not yaml :::"},
            {"path": "data.json", "content": "{ broken json"},
            {"path": "README.md", "content": "# title\n"},
            {"path": "real.py", "content": "x = 1\n"},
        ]
    }
    v = _eval_file_parses_after_change(_prop("file_parses_after_change"), evidence)
    assert v.verdict is VerdictKind.PASSED
    # Only one .py file actually parsed
    assert "parsed_ok=1" in v.reason


def test_file_parses_insufficient_on_missing_key() -> None:
    v = _eval_file_parses_after_change(_prop("file_parses_after_change"), {})
    assert v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE


def test_file_parses_decodes_bytes_content() -> None:
    evidence = {
        "target_files_post": [
            {"path": "a.py", "content": b"x = 1\n"},
        ]
    }
    v = _eval_file_parses_after_change(_prop("file_parses_after_change"), evidence)
    assert v.verdict is VerdictKind.PASSED


def test_file_parses_skips_malformed_entries() -> None:
    evidence = {
        "target_files_post": [
            {"path": "good.py", "content": "x = 1\n"},
            "this is a bare string not a dict",  # malformed
            {"content": "missing path"},  # missing path
            {"path": "skip.py", "content": None},  # non-str content
        ]
    }
    v = _eval_file_parses_after_change(_prop("file_parses_after_change"), evidence)
    # Only the good entry parsed; malformed ones skipped silently
    assert v.verdict is VerdictKind.PASSED
    assert "parsed_ok=1" in v.reason


def test_file_parses_empty_list_passes() -> None:
    v = _eval_file_parses_after_change(
        _prop("file_parses_after_change"), {"target_files_post": []},
    )
    assert v.verdict is VerdictKind.PASSED
    assert "parsed_ok=0" in v.reason


# ===========================================================================
# §9-§13 — test_set_hash_stable
# ===========================================================================


def test_test_set_hash_stable_happy_path() -> None:
    evidence = {
        "test_files_pre": ("tests/test_a.py", "tests/test_b.py"),
        "test_files_post": ("tests/test_a.py", "tests/test_b.py"),
    }
    v = _eval_test_set_hash_stable(_prop("test_set_hash_stable"), evidence)
    assert v.verdict is VerdictKind.PASSED
    assert "pre_sha=" in v.reason
    assert "post_sha=" in v.reason
    assert "additions=0" in v.reason


def test_test_set_hash_stable_fails_on_removal() -> None:
    evidence = {
        "test_files_pre": ("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"),
        "test_files_post": ("tests/test_a.py",),
    }
    v = _eval_test_set_hash_stable(_prop("test_set_hash_stable"), evidence)
    assert v.verdict is VerdictKind.FAILED
    assert "removed 2" in v.reason
    assert "test_b.py" in v.reason
    assert "test_c.py" in v.reason


def test_test_set_hash_stable_additions_pass() -> None:
    evidence = {
        "test_files_pre": ("tests/test_a.py",),
        "test_files_post": ("tests/test_a.py", "tests/test_new.py"),
    }
    v = _eval_test_set_hash_stable(_prop("test_set_hash_stable"), evidence)
    assert v.verdict is VerdictKind.PASSED
    assert "additions=1" in v.reason


def test_test_set_hash_stable_insufficient_on_missing_key() -> None:
    v = _eval_test_set_hash_stable(
        _prop("test_set_hash_stable"), {"test_files_post": ()},
    )
    assert v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE


def test_test_set_hash_stable_sha_digests_for_replay() -> None:
    evidence = {
        "test_files_pre": ("tests/a.py", "tests/b.py"),
        "test_files_post": ("tests/a.py", "tests/b.py"),
    }
    v1 = _eval_test_set_hash_stable(_prop("test_set_hash_stable"), evidence)
    v2 = _eval_test_set_hash_stable(_prop("test_set_hash_stable"), evidence)
    # Same input → same digest (replay-stable)
    assert v1.reason == v2.reason


# ===========================================================================
# §14-§20 — no_new_credential_shapes
# ===========================================================================


def test_no_new_credentials_clean_diff() -> None:
    diff = (
        "+++ b/foo.py\n"
        "+def hello():\n"
        "+    return 'world'\n"
    )
    v = _eval_no_new_credential_shapes(
        _prop("no_new_credential_shapes"), {"diff_text": diff},
    )
    assert v.verdict is VerdictKind.PASSED
    assert "no credential shapes" in v.reason


@pytest.mark.parametrize(
    "secret_text, label",
    [
        # 5 canonical patterns; each must be detected
        ("API_KEY = 'sk-AbCdEfGhIjKlMnOpQrStUvWxYz123456'", "openai"),
        ("AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'", "aws"),
        ("GH_TOKEN = 'ghp_AbCdEfGhIjKlMnOpQrStUvWxYz12345'", "github"),
        ("SLACK = 'xoxb-1234567890-foo-bar-baz-1234567890'", "slack"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCA...", "pem"),
    ],
)
def test_no_new_credentials_detects_all_5_patterns(secret_text, label) -> None:
    v = _eval_no_new_credential_shapes(
        _prop("no_new_credential_shapes"), {"diff_text": secret_text},
    )
    assert v.verdict is VerdictKind.FAILED, f"{label} pattern must trip"
    assert "credential shape detected" in v.reason
    assert "offset" in v.reason


def test_no_new_credentials_secret_value_never_echoed() -> None:
    # Defensive: the actual secret must NEVER appear in the verdict
    # reason — only the pattern + offset. Otherwise the verdict log
    # leaks the secret operators just tried to keep out.
    secret = "sk-VerySecretValueDoNotLeakIt-1234567890"
    v = _eval_no_new_credential_shapes(
        _prop("no_new_credential_shapes"), {"diff_text": f"key = '{secret}'"},
    )
    assert v.verdict is VerdictKind.FAILED
    assert secret not in v.reason
    assert "VerySecretValue" not in v.reason


def test_no_new_credentials_insufficient_on_missing_key() -> None:
    v = _eval_no_new_credential_shapes(_prop("no_new_credential_shapes"), {})
    assert v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE
    assert "diff_text" in v.reason


def test_no_new_credentials_decodes_bytes() -> None:
    diff_bytes = b"+def f(): pass\n"
    v = _eval_no_new_credential_shapes(
        _prop("no_new_credential_shapes"), {"diff_text": diff_bytes},
    )
    assert v.verdict is VerdictKind.PASSED


def test_no_new_credentials_empty_diff_passes() -> None:
    v = _eval_no_new_credential_shapes(
        _prop("no_new_credential_shapes"), {"diff_text": ""},
    )
    assert v.verdict is VerdictKind.PASSED


def test_no_new_credentials_reuses_canonical_patterns() -> None:
    """Single source of truth invariant: the evaluator must import
    the credential patterns from semantic_firewall, NOT redefine them
    locally. Source-grep pin enforces this."""
    src = inspect.getsource(_eval_no_new_credential_shapes)
    # Must import the canonical tuple
    assert "_CREDENTIAL_SHAPE_PATTERNS" in src
    assert "from backend.core.ouroboros.governance.semantic_firewall" in src
    # Must NOT redefine the regexes locally — none of the canonical
    # token shapes appear as raw string literals in this evaluator.
    forbidden_local_definitions = (
        r"\bsk-",
        r"\bAKIA",
        r"\bghp_",
        r"\bxox[bp]-",
        "BEGIN RSA",
    )
    for token in forbidden_local_definitions:
        assert token not in src, (
            f"evaluator must reuse semantic_firewall patterns, not "
            f"redefine {token!r} locally"
        )


# ===========================================================================
# §21 — Defensive (never raises)
# ===========================================================================


@pytest.mark.parametrize(
    "evaluator, evidence",
    [
        (_eval_file_parses_after_change, {"target_files_post": [None, 42, []]}),
        (_eval_test_set_hash_stable, {"test_files_pre": None, "test_files_post": None}),
        (_eval_no_new_credential_shapes, {"diff_text": 12345}),
    ],
)
def test_evaluators_never_raise_on_garbage_input(evaluator, evidence) -> None:
    """Each evaluator must return a verdict, never raise. The dispatch
    wrapper assumes this contract."""
    v = evaluator(_prop("test"), evidence)
    assert isinstance(v, PropertyVerdict)
    # Non-PASSED outcome on garbage input is fine — what matters is no raise
    assert v.verdict in (
        VerdictKind.PASSED, VerdictKind.FAILED,
        VerdictKind.INSUFFICIENT_EVIDENCE, VerdictKind.EVALUATOR_ERROR,
    )


# ===========================================================================
# §22 — Definitive verdicts have confidence=1.0
# ===========================================================================


def test_definitive_verdicts_have_full_confidence() -> None:
    # PASSED case
    v_pass = _eval_file_parses_after_change(
        _prop("file_parses_after_change"),
        {"target_files_post": [{"path": "a.py", "content": "x=1\n"}]},
    )
    assert v_pass.verdict is VerdictKind.PASSED
    assert v_pass.confidence == 1.0

    # FAILED case
    v_fail = _eval_test_set_hash_stable(
        _prop("test_set_hash_stable"),
        {"test_files_pre": ("a.py",), "test_files_post": ()},
    )
    assert v_fail.verdict is VerdictKind.FAILED
    assert v_fail.confidence == 1.0
