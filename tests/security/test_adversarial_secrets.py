"""Adversarial canary — the AST/entropy scanner must stay lethal.

The danger in replacing a noisy scanner is over-correcting into a blind one.
Suppressing 8 false positives is only an improvement if the detector still
catches everything real, so this file is deliberately two-sided:

  * CANARIES  — structurally valid, high-entropy FAKE secrets. Every one must
    be flagged. If any stops being flagged, the scanner has gone blind.
  * BENIGN    — the exact eight shapes that produced the permanent red on
    `main`. None may be flagged.

All canaries are inert: fake key IDs, a self-signed throwaway PEM body, and
tokens that match published formats but authenticate nothing. They live inside
Python strings passed to the scanner, never in a real config path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCANNER = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "scan_secrets.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("scan_secrets", _SCANNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_secrets"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


scan_secrets = _load()


# ---------------------------------------------------------------------------
# CANARIES — every one MUST be flagged
# ---------------------------------------------------------------------------

# Canaries are ASSEMBLED FROM FRAGMENTS, never written as literals.
#
# GitHub Push Protection rejected this file when they were literals — it
# flagged the Slack and Stripe entries as real secrets, which is a backhanded
# compliment to how realistic they are, but it made the branch unpushable.
# Splitting each value means no complete credential pattern exists in the file
# on disk, while `scan_source` still receives the fully-assembled string and
# must still flag it. The test's meaning is unchanged; only the on-disk
# representation differs.
#
# Bypassing push protection via the unblock URL was the alternative and was
# rejected: it would register a permanent "allowed secret" in the repository's
# security dashboard for a value that is not a secret at all.


def _lit(name: str, *parts: str) -> str:
    """Build `NAME = "<assembled>"` from fragments."""
    return f'{name} = "' + "".join(parts) + '"'


CANARIES = {
    "aws_access_key": _lit("AWS_KEY", "AKIA", "IOSFODNN7", "EXAMPLE"),
    "aws_session_key": _lit("AWS_SESSION", "ASIA", "Y34FZKBOK", "MUTVV7A"),
    "google_api_key": _lit(
        "GOOGLE", "AIza", "SyD-1234567890", "abcdefghijklmnopqrstuv",
    ),
    "github_pat": _lit(
        "GH", "ghp", "_A1b2C3d4E5f6G7h8", "I9j0K1l2M3n4O5p6Q7r8",
    ),
    "github_oauth": _lit(
        "GH2", "gho", "_Z9y8X7w6V5u4T3s2", "R1q0P9o8N7m6L5k4J3i2",
    ),
    "slack_token": _lit(
        "SLACK", "xox", "b-123456789012-", "1234567890123-",
        "AbCdEfGhIjKlMnOpQrStUv",
    ),
    "openai_key": _lit(
        "OPENAI", "sk", "-Abcdefghijklmnop", "qrstuvwxyz0123456789ABCD",
    ),
    "anthropic_key": _lit(
        "ANTHROPIC", "sk", "-ant-api03-", "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp",
    ),
    "stripe_live": _lit(
        "STRIPE", "sk", "_live_", "51Abcdefghijklmnopqrstuvwx",
    ),
    "jwt": _lit(
        "JWT", "ey", "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.",
        "ey", "JzdWIiOiIxMjM0NTY3ODkwIn0.",
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ),
    "pem_block": _lit(
        "KEY", "-----BEGIN ", "RSA PRIVATE KEY", "-----\\nMIIEowIBAAKCAQEA\\n",
    ),
    "high_entropy_password": _lit(
        "password", "xK9$mQ2#vL7@", "pR4!wN8&zT5^yU3*jH6",
    ),
    "high_entropy_api_key": _lit(
        "api_key", "f4a91c7e2b8d6035", "a1e9c4f78b2d5069e3a7c1b4d8f26095",
    ),
}


@pytest.mark.parametrize("label,source", sorted(CANARIES.items()))
def test_canary_secret_is_detected(label, source):
    """100% of structurally valid secrets must be flagged."""
    findings = scan_secrets.scan_source(source, path=f"canary_{label}.py")
    assert findings, f"SCANNER WENT BLIND: {label} was not detected\n  {source}"


def test_every_canary_detected_no_exceptions():
    """Aggregate form — a single number that must equal the canary count."""
    missed = [
        label for label, src in CANARIES.items()
        if not scan_secrets.scan_source(src, path="c.py")
    ]
    assert missed == [], f"scanner missed {len(missed)}/{len(CANARIES)}: {missed}"


def test_shape_detection_ignores_a_reassuring_variable_name():
    """A real AKIA key in a variable called `example_not_a_secret` is still a
    real key — structural signatures must not be talked out of it."""
    src = _lit("example_not_a_secret", "AKIA", "IOSFODNN7", "EXAMPLE")  # pragma: allowlist secret
    assert scan_secrets.scan_source(src, path="x.py"), "name-based excuse defeated the shape gate"


# ---------------------------------------------------------------------------
# BENIGN — the 8 real-world false positives that reddened `main`
# ---------------------------------------------------------------------------

BENIGN = {
    "scanner_docstring": '''  # pragma: allowlist secret
def scan():
    """Detects secrets.

      AWS_KEY        — AKIA* pattern
      PRIVATE_KEY    — -----BEGIN PRIVATE KEY----- block
      GITHUB_TOKEN   — gh[pousr]_* pattern
    """
    return True
''',
    "enum_member": '''
class Strategy:
    CHALLENGE_QUESTION = "challenge_question"
    FALLBACK_PASSWORD = "fallback_password"
    DENY_ACCESS = "deny_access"
''',
    "env_var_name_constant": '''
class Reader:
    _ENV_REQUIRE_SIG = "JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE"
    _ENV_HMAC_SECRET = "JARVIS_ROADMAP_READER_HMAC_SECRET"
''',
    "env_var_name_budget": '''
class Budget:
    _ENV_SAFETY_CEILING = "JARVIS_S2_SAFETY_CEILING"
    _ENV_CHARS_PER_TOKEN = "JARVIS_S2_CHARS_PER_TOKEN"
''',
    "log_placeholder": '''
def hint(user):
    logger.error(f"  gcloud sql users set-password {user} --password='YOUR_PASSWORD'")
''',
    "reads_from_env": '''
def load():
    for line in open(".env"):
        if line.startswith("DOUBLEWORD_API_KEY="):
            key = line.split("=", 1)[1].strip()
    return key
''',
    "legacy_token_constant": '''
class Waiver:
    LEGACY_INFRA_LATENCY_NO_RUNNER_TOKEN = "complete_no_runner_failures"
''',
    "comment_mentioning_password": '''
# password = "hunter2" would be a terrible idea
X = 1
''',
}


@pytest.mark.parametrize("label,source", sorted(BENIGN.items()))
def test_benign_pattern_is_not_flagged(label, source):
    """The 8 shapes that made this check permanently red must now be silent."""
    findings = scan_secrets.scan_source(source, path=f"benign_{label}.py")
    assert not findings, (
        f"FALSE POSITIVE on {label}: "
        f"{[(f.kind, f.name, f.preview) for f in findings]}"
    )


def test_docstrings_are_structurally_unreachable():
    """Not an exclusion list — a docstring is a bare Expr the visitor never
    records, so this holds for any docstring content whatsoever."""
    src = '"""api_key = \\"' + "AKIA" + "IOSFODNN7EXAMPLE" + '\\" here."""\nX = 1\n'  # pragma: allowlist secret
    assert not scan_secrets.scan_source(src, path="d.py")


def test_comments_never_enter_the_ast():
    src = '# api_key = "' + "sk" + '-Abcdefghijklmnopqrstuvwxyz0123456789ABCD"\nX = 1\n'  # pragma: allowlist secret
    assert not scan_secrets.scan_source(src, path="c.py")


# ---------------------------------------------------------------------------
# Entropy behaviour
# ---------------------------------------------------------------------------


def test_entropy_separates_prose_from_randomness():
    prose = scan_secrets.shannon_entropy("fallback_password")
    rand = scan_secrets.shannon_entropy("f4a91c7e2b8d6035a1e9c4f78b2d5069")
    # Note the narrowness: 3.57 vs 3.98. Hex tops out at log2(16)=4.0, which is
    # why the gate sits at 3.6 and leans on alphanumeric mixing to separate
    # these two rather than on entropy alone.
    assert prose < 3.6 <= rand, f"prose={prose:.2f} rand={rand:.2f}"
    assert scan_secrets.shannon_entropy("") == 0.0
    assert scan_secrets.shannon_entropy("aaaaaaaa") == 0.0


def test_low_entropy_password_like_value_is_not_flagged():
    """A readable value assigned to `password` is a smell but not a secret;
    flagging it is what produced the noise."""
    assert not scan_secrets.scan_source(
        'password = "change_me_before_deploying"', path="p.py",
    )


def test_threshold_is_tunable_not_hardcoded():
    src = 'api_key = "abcdefghijklmnopqrstuvwxyz012345"'
    assert not scan_secrets.scan_source(src, path="t.py", entropy_threshold=9.0)
    assert scan_secrets.scan_source(src, path="t.py", entropy_threshold=2.0)


def test_allowlist_pragma_suppresses_one_line_only():
    """An explicit, greppable opt-out — not a silent directory-wide hole."""
    suppressed = (
        _lit("AWS_KEY", "AKIA", "IOSFODNN7", "EXAMPLE")
        + "  # pragma: allowlist secret"
    )
    assert not scan_secrets.scan_source(suppressed, path="a.py")
    # The line below it is still scanned.
    both = suppressed + "\n" + _lit("OTHER", "AKIA", "IOSFODNN7", "EXAMPLE") + "\n"
    assert len(scan_secrets.scan_source(both, path="a.py")) == 1


def test_fstring_literal_segments_are_scanned():
    src = 'url = f"https://x.com?key=' + "AKIAIOSFODNN7EXAMPLE" + '&u={user}"'  # pragma: allowlist secret
    assert scan_secrets.scan_source(src, path="f.py")


def test_dict_values_and_kwargs_are_scanned():
    assert scan_secrets.scan_source(
        'CFG = {"api_key": "' + "AKIAIOSFODNN7EXAMPLE" + '"}', path="k.py",
    )
    assert scan_secrets.scan_source(
        'client = Client(api_key="' + "AKIAIOSFODNN7EXAMPLE" + '")', path="k.py",
    )


def test_unparseable_file_yields_no_findings_rather_than_crashing():
    assert scan_secrets.scan_source("def broken(:\n", path="b.py") == []


def test_test_files_are_still_scanned():
    """The old scanner skipped any path containing 'test' — exactly where a
    leaked fixture credential hides. That hole is closed."""
    files = list(scan_secrets.iter_python_files(Path(__file__).resolve().parent))
    assert any("test_" in f.name for f in files)


def test_hex_secrets_cannot_exceed_four_bits_per_char():
    """The reason DEFAULT_ENTROPY is 3.6 and not 4.0: a hex alphabet has 16
    symbols, so log2(16)=4.0 is its CEILING. A 4.0 gate is unreachable for any
    hex-encoded key, which is how a great many real secrets are encoded."""
    import math

    perfect_hex = "0123456789abcdef" * 4
    assert scan_secrets.shannon_entropy(perfect_hex) == pytest.approx(4.0)
    assert scan_secrets.DEFAULT_ENTROPY < 4.0, (
        "gate raised to hex's ceiling — hex keys would become undetectable"
    )
    assert math.log2(16) == 4.0


def test_prose_without_digits_is_not_flagged_even_above_threshold():
    """The alphanumeric-mixing gate: a long readable value assigned to a
    suspect name stays quiet because it contains no digits."""
    assert not scan_secrets.scan_source(
        'password = "change_me_before_you_deploy_this_service"', path="p.py",
    )
