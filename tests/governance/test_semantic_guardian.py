"""Adversarial regression suite for SemanticGuardian.

The audit found that O+V's SAFE_AUTO classification relies entirely on
SIZE heuristics (file count, blast radius, test confidence). A
syntactically-valid but semantically-inverted candidate would land
Green and auto-apply while the operator is asleep. This suite is the
regression spine that proves each of the guardian's 10 patterns
actually catches the specific semantic-wrongness class it's named for.

Each test constructs:
  • An ``old`` version of a file (the pre-apply state)
  • A ``new`` version that contains the semantic problem
  • Asserts the guardian fires on the expected pattern + severity

Plus negative-case tests that confirm the guardian is quiet on
identical content, pure refactors, and edge-cases that shouldn't trip
the detector (otherwise operators would disable it in frustration).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.semantic_guardian import (
    Detection,
    SemanticGuardian,
    all_pattern_names,
    guardian_enabled,
    pattern_enabled,
    recommend_tier_floor,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_SEMANTIC_GUARD_") or key.startswith(
            "JARVIS_SEMGUARD_"
        ):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def guard() -> SemanticGuardian:
    return SemanticGuardian()


# ---------------------------------------------------------------------------
# (0) Env gates
# ---------------------------------------------------------------------------


def test_guardian_enabled_default_on():
    assert guardian_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no"])
def test_guardian_disabled_values(monkeypatch, val):
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", val)
    assert guardian_enabled() is False


def test_disabled_guardian_returns_empty_findings(monkeypatch, guard):
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "0")
    # Feed a candidate that would otherwise fire multiple patterns.
    old = "import os\ndef foo():\n    return os.path.join('a', 'b')\n"
    new = "def foo():\n    return os.path.join('a', 'b')\n"
    assert guard.inspect(
        file_path="x.py", old_content=old, new_content=new,
    ) == []


def test_per_pattern_kill_switch(monkeypatch, guard):
    """Disabling a single pattern leaves the rest working."""
    monkeypatch.setenv(
        "JARVIS_SEMGUARD_REMOVED_IMPORT_STILL_REFERENCED_ENABLED", "0",
    )
    old = "import os\ndef foo():\n    return os.getcwd()\n"
    new = "def foo():\n    return os.getcwd()\n"
    findings = guard.inspect(
        file_path="x.py", old_content=old, new_content=new,
    )
    patterns = [f.pattern for f in findings]
    assert "removed_import_still_referenced" not in patterns


def test_all_pattern_names_is_exhaustive():
    """Every registered detector should have a canonical name entry —
    this test fails if _ALL_PATTERNS and _PATTERNS desync."""
    from backend.core.ouroboros.governance.semantic_guardian import _PATTERNS
    assert set(all_pattern_names()) == set(_PATTERNS.keys())
    assert len(all_pattern_names()) == 10


# ---------------------------------------------------------------------------
# Pattern 1 — removed_import_still_referenced
# ---------------------------------------------------------------------------


def test_removed_import_still_referenced_fires(guard):
    old = "import os\n\ndef cwd():\n    return os.getcwd()\n"
    new = "\ndef cwd():\n    return os.getcwd()\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    hit = next((f for f in findings
                if f.pattern == "removed_import_still_referenced"), None)
    assert hit is not None
    assert hit.severity == "hard"
    assert "os" in hit.message


def test_removed_import_not_referenced_silent(guard):
    """Removing an unused import is legitimate cleanup — don't flag."""
    old = "import json\nimport os\n\ndef cwd():\n    return os.getcwd()\n"
    new = "import os\n\ndef cwd():\n    return os.getcwd()\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(
        f.pattern == "removed_import_still_referenced" for f in findings
    )


def test_removed_import_via_attribute_chain(guard):
    """``os.path.join`` → root Name is ``os``; removing the ``os``
    import must fire."""
    old = "import os\n\ndef j():\n    return os.path.join('a', 'b')\n"
    new = "\ndef j():\n    return os.path.join('a', 'b')\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert any(
        f.pattern == "removed_import_still_referenced" for f in findings
    )


# ---------------------------------------------------------------------------
# Pattern 2 — function_body_collapsed
# ---------------------------------------------------------------------------


def test_function_body_collapsed_to_pass(guard):
    old = (
        "def compute(x):\n"
        "    y = x * 2\n"
        "    y += 5\n"
        "    y -= 1\n"
        "    return y\n"
    )
    new = "def compute(x):\n    pass\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    hit = next(
        (f for f in findings if f.pattern == "function_body_collapsed"), None,
    )
    assert hit is not None
    assert hit.severity == "hard"
    assert "compute" in hit.message


def test_function_body_collapsed_to_raise_not_implemented(guard):
    old = (
        "def process():\n"
        "    x = 1\n"
        "    y = 2\n"
        "    z = 3\n"
        "    return x + y + z\n"
    )
    new = "def process():\n    raise NotImplementedError\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert any(f.pattern == "function_body_collapsed" for f in findings)


def test_function_body_collapsed_silent_on_small_old_body(guard):
    """2-statement functions don't meet the ≥3 substantive threshold."""
    old = "def f():\n    x = 1\n    return x\n"
    new = "def f():\n    pass\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(f.pattern == "function_body_collapsed" for f in findings)


def test_function_body_substantive_refactor_silent(guard):
    """A legit refactor with different statements but still substantive
    must not fire."""
    old = "def f():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n"
    new = "def f():\n    return sum(range(1, 4))\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(f.pattern == "function_body_collapsed" for f in findings)


# ---------------------------------------------------------------------------
# Pattern 3 — guard_boolean_inverted
# ---------------------------------------------------------------------------


def test_guard_boolean_inverted_fires(guard):
    old = "def can_write(user):\n    if user.is_admin:\n        return True\n    return False\n"
    new = "def can_write(user):\n    if not user.is_admin:\n        return True\n    return False\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    hit = next(
        (f for f in findings if f.pattern == "guard_boolean_inverted"), None,
    )
    assert hit is not None
    assert hit.severity == "soft"
    assert "can_write" in hit.message


def test_guard_boolean_inverted_reverse_direction(guard):
    """Reverse case: ``if not X`` → ``if X``."""
    old = "def f(x):\n    if not x:\n        return 1\n    return 2\n"
    new = "def f(x):\n    if x:\n        return 1\n    return 2\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert any(f.pattern == "guard_boolean_inverted" for f in findings)


def test_guard_complex_condition_silent(guard):
    """Only bare Name conditions are flagged — complex expressions
    aren't (too many false positives)."""
    old = "def f(a, b):\n    if a and b:\n        return 1\n    return 2\n"
    new = "def f(a, b):\n    if not (a and b):\n        return 1\n    return 2\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(f.pattern == "guard_boolean_inverted" for f in findings)


# ---------------------------------------------------------------------------
# Pattern 4 — credential_shape_introduced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cred", [
    "sk-abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_abcdefghijklmnop0123456789ABCDEFGHIJ",
    "xoxb-1234567890-abcdefghij",
])
def test_credential_shape_fires(guard, cred):
    old = "TOKEN = ''\n"
    new = f"TOKEN = '{cred}'\n"
    findings = guard.inspect(
        file_path="config.py", old_content=old, new_content=new,
    )
    hit = next(
        (f for f in findings if f.pattern == "credential_shape_introduced"), None,
    )
    assert hit is not None
    assert hit.severity == "hard"


def test_credential_shape_api_key_assignment(guard):
    old = "API_KEY = ''\n"
    new = "API_KEY = 'super-secret-123456'\n"
    findings = guard.inspect(
        file_path="config.py", old_content=old, new_content=new,
    )
    assert any(
        f.pattern == "credential_shape_introduced" for f in findings
    )


def test_credential_shape_silent_when_preexisting(guard):
    """Don't flag credentials that already existed in the old content."""
    cred = "ghp_abcdefghijklmnop0123456789ABCDEFGHIJ"
    old = f"TOKEN = '{cred}'\n"
    new = f"TOKEN = '{cred}'\nX = 1\n"
    findings = guard.inspect(
        file_path="config.py", old_content=old, new_content=new,
    )
    assert not any(
        f.pattern == "credential_shape_introduced" for f in findings
    )


def test_credential_shape_private_key_header(guard):
    new = (
        "KEY = '''-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA...\n"
        "-----END RSA PRIVATE KEY-----'''\n"
    )
    findings = guard.inspect(
        file_path="secrets.py", old_content="", new_content=new,
    )
    assert any(
        f.pattern == "credential_shape_introduced" for f in findings
    )


# ---------------------------------------------------------------------------
# Pattern 5 — test_assertion_inverted
# ---------------------------------------------------------------------------


def test_assertion_inverted_fires_in_test_file(guard):
    old = (
        "def test_fetch_succeeds():\n"
        "    result = fetch()\n"
        "    assert result.ok\n"
    )
    new = (
        "def test_fetch_succeeds():\n"
        "    result = fetch()\n"
        "    assert not result.ok\n"
    )
    findings = guard.inspect(
        file_path="tests/test_fetch.py", old_content=old, new_content=new,
    )
    hit = next(
        (f for f in findings if f.pattern == "test_assertion_inverted"), None,
    )
    assert hit is not None
    assert hit.severity == "hard"


def test_assertion_inverted_silent_in_non_test_file(guard):
    """The same flip in a non-test file isn't flagged by THIS pattern
    (other patterns may fire). Keeps the test-file heuristic scoped."""
    old = "def check(x):\n    assert x.ok\n"
    new = "def check(x):\n    assert not x.ok\n"
    findings = guard.inspect(
        file_path="module.py", old_content=old, new_content=new,
    )
    assert not any(
        f.pattern == "test_assertion_inverted" for f in findings
    )


def test_assertion_new_test_case_silent(guard):
    """Adding a new assertion (not flipping existing) must not fire."""
    old = "def test_one():\n    assert True\n"
    new = (
        "def test_one():\n    assert True\n"
        "\ndef test_two():\n    assert not False\n"
    )
    findings = guard.inspect(
        file_path="test_x.py", old_content=old, new_content=new,
    )
    assert not any(
        f.pattern == "test_assertion_inverted" for f in findings
    )


# ---------------------------------------------------------------------------
# Pattern 6 — return_value_flipped
# ---------------------------------------------------------------------------


def test_return_value_flipped_fires(guard):
    old = "def is_ready():\n    return True\n"
    new = "def is_ready():\n    return False\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    hit = next(
        (f for f in findings if f.pattern == "return_value_flipped"), None,
    )
    assert hit is not None
    assert hit.severity == "soft"


def test_return_value_flipped_silent_on_non_bool(guard):
    """Only bool-literal returns are flagged — integer/str changes are
    normal refactors."""
    old = "def count():\n    return 1\n"
    new = "def count():\n    return 2\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(f.pattern == "return_value_flipped" for f in findings)


# ---------------------------------------------------------------------------
# Pattern 7 — permission_loosened
# ---------------------------------------------------------------------------


def test_permission_loosened_chmod_fires(guard):
    old = "def setup():\n    pass\n"
    new = "import os\ndef setup():\n    os.chmod('/etc/passwd', 0o777)\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    hit = next(
        (f for f in findings if f.pattern == "permission_loosened"), None,
    )
    assert hit is not None
    assert hit.severity == "hard"


def test_permission_preexisting_silent(guard):
    """Pre-existing chmod that's untouched must not fire."""
    old = "import os\ndef setup():\n    os.chmod('/tmp/x', 0o644)\n"
    new = (
        "import os\ndef setup():\n"
        "    os.chmod('/tmp/x', 0o644)\n    return True\n"
    )
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(f.pattern == "permission_loosened" for f in findings)


# ---------------------------------------------------------------------------
# Pattern 8 — silent_exception_swallow
# ---------------------------------------------------------------------------


def test_silent_exception_swallow_fires(guard):
    old = "def run():\n    try:\n        work()\n    except ValueError:\n        log('boom')\n"
    new = "def run():\n    try:\n        work()\n    except Exception:\n        pass\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    hit = next(
        (f for f in findings if f.pattern == "silent_exception_swallow"), None,
    )
    assert hit is not None
    assert hit.severity == "soft"


def test_specific_except_not_flagged(guard):
    """Catching a specific exception type (not broad Exception/bare) is
    a legitimate pattern — don't flag."""
    old = "def run():\n    x = 1\n"
    new = "def run():\n    try:\n        work()\n    except ValueError:\n        pass\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(f.pattern == "silent_exception_swallow" for f in findings)


# ---------------------------------------------------------------------------
# Pattern 9 — hardcoded_url_swap
# ---------------------------------------------------------------------------


def test_hardcoded_url_swap_fires(guard):
    old = "BASE = 'https://api.prod.example.com/v1'\n"
    new = "BASE = 'https://api.staging.example.com/v1'\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    hit = next(
        (f for f in findings if f.pattern == "hardcoded_url_swap"), None,
    )
    assert hit is not None
    assert hit.severity == "soft"


def test_pure_url_addition_silent(guard):
    """Adding a new URL with no removal is normal code growth."""
    old = "X = 1\n"
    new = "BASE = 'https://api.example.com/v1'\nX = 1\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(f.pattern == "hardcoded_url_swap" for f in findings)


# ---------------------------------------------------------------------------
# Pattern 10 — docstring_only_delete
# ---------------------------------------------------------------------------


def test_docstring_only_delete_fires(guard):
    old = (
        "def compute(x):\n"
        "    \"\"\"Return x doubled.\"\"\"\n"
        "    return x * 2\n"
    )
    new = "def compute(x):\n    return x * 2\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    hit = next(
        (f for f in findings if f.pattern == "docstring_only_delete"), None,
    )
    assert hit is not None
    assert hit.severity == "soft"


def test_docstring_plus_body_rewrite_silent(guard):
    """When the body also changes, the strip isn't docstring-ONLY."""
    old = (
        "def f(x):\n"
        "    \"\"\"Old doc.\"\"\"\n"
        "    return x\n"
    )
    new = "def f(x):\n    y = x * 2\n    return y\n"
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert not any(f.pattern == "docstring_only_delete" for f in findings)


# ---------------------------------------------------------------------------
# Malformed input — guardian never propagates exceptions
# ---------------------------------------------------------------------------


def test_syntax_error_in_old_returns_empty(guard):
    """If old content doesn't parse, AST-based patterns skip silently."""
    old = "def broken(:\n  this_is_not_python\n"
    new = "def fine():\n    return 1\n"
    # Must not raise. Some regex patterns may still fire, but the AST
    # ones return None for invalid input — the guardian as a whole
    # never raises.
    findings = guard.inspect(file_path="x.py", old_content=old, new_content=new)
    assert isinstance(findings, list)


def test_empty_strings_return_empty(guard):
    assert guard.inspect(
        file_path="x.py", old_content="", new_content="",
    ) == []


# ---------------------------------------------------------------------------
# Tier-floor recommendation
# ---------------------------------------------------------------------------


def test_recommend_floor_from_hard_is_approval_required():
    det = Detection(
        pattern="function_body_collapsed", severity="hard", message="x",
    )
    assert recommend_tier_floor([det]) == "approval_required"


def test_recommend_floor_from_soft_is_notify_apply():
    det = Detection(
        pattern="guard_boolean_inverted", severity="soft", message="x",
    )
    assert recommend_tier_floor([det]) == "notify_apply"


def test_recommend_floor_mixed_uses_strictest():
    """One hard + one soft → approval_required (strictest wins)."""
    a = Detection(pattern="a", severity="hard", message="h")
    b = Detection(pattern="b", severity="soft", message="s")
    assert recommend_tier_floor([a, b]) == "approval_required"


def test_recommend_floor_empty_is_none():
    assert recommend_tier_floor([]) is None


# ---------------------------------------------------------------------------
# Inspect-batch convenience
# ---------------------------------------------------------------------------


def test_inspect_batch_aggregates_findings(guard):
    """Multi-file candidate: each file contributes its own findings."""
    files = [
        # Credential shape in file 1.
        ("config.py", "", "KEY = 'sk-abcdefghijklmnopqrstuvwxyz1234567890'\n"),
        # Function body collapsed in file 2.
        (
            "mod.py",
            "def run():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n",
            "def run():\n    pass\n",
        ),
    ]
    findings = guard.inspect_batch(files)
    patterns = {f.pattern for f in findings}
    assert "credential_shape_introduced" in patterns
    assert "function_body_collapsed" in patterns


# ---------------------------------------------------------------------------
# AST canary — orchestrator calls the guardian
# ---------------------------------------------------------------------------


def test_orchestrator_invokes_semantic_guardian():
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/orchestrator.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "SemanticGuardian" in src
    assert "recommend_tier_floor" in src
    # Specifically confirm it's called (not just imported).
    assert "SemanticGuardian()" in src or "SemanticGuardian(" in src
