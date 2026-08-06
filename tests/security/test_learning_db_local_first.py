"""
The voiceprint is on local disk. Startup must not block on a remote database.

Measured 2026-08-06, on a real unlock attempt::

    13:11:08  [LearningDB v265.1] Initialization timed out (15s)
                  — retrying with fast_mode (SQLite-first)
    13:12:23  No speaker match found. Primary user: None, Best confidence: 0.00%
    13:12:23  'unlock_screen' NOT authorised (That didn't sound like you...)

The profile was never missing. ``~/.jarvis/learning/jarvis_learning.db`` holds
"Derek J. Russell" — a 768-byte, 192-dimension embedding built from 272 samples,
``is_primary_user=1`` — at exactly the path the service opens. It did not load
because init spent its entire budget reaching for Cloud SQL first.

Three fast_mode promotions already existed, and every one of them is an
INFERENCE about whether Cloud SQL is reachable. All three said "go standard" on
that run, because a readiness gate reporting READY is a claim about a proxy, not
a promise that a query returns inside fifteen seconds. These tests pin the
operator declaration that outranks them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.intelligence.learning_database import local_first_enabled

REPO_ROOT = Path(__file__).resolve().parents[2]
LEARNING_DB_SRC = REPO_ROOT / "backend/intelligence/learning_database.py"


def test_local_first_is_the_default():
    """
    Default ON.

    fast_mode is "SQLite-first, Cloud SQL deferred to background" — an ordering,
    not an exclusion. Cloud SQL still initialises and still syncs. Blocking a
    boot on a remote database for data already on local disk is not defensible
    even when the account is in good standing.
    """
    assert local_first_enabled({}) is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", " off "])
def test_operator_can_restore_cloud_first(value):
    """The declaration is reversible without a code change."""
    assert local_first_enabled({"JARVIS_LEARNING_DB_LOCAL_FIRST": value}) is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "", "anything-else"])
def test_anything_not_explicitly_disabling_keeps_local_first(value):
    """
    A malformed value must not silently restore cloud-first.

    The failure mode of guessing wrong here is a fifteen-second boot stall and
    an unlock that reports the wrong reason, so ambiguity resolves toward local.
    """
    assert local_first_enabled({"JARVIS_LEARNING_DB_LOCAL_FIRST": value}) is True


def test_declaration_is_checked_before_every_inference():
    """
    Ordering is the whole point.

    Three inferential promotions already existed and all three were wrong on the
    measured run. If the operator declaration were evaluated after them it would
    be unreachable whenever an inference had already decided — which is exactly
    the state that produced the 15s timeout.
    """
    source = LEARNING_DB_SRC.read_text()
    tree = ast.parse(source)

    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_learning_database"
    )

    declaration_line = None
    inference_lines = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "local_first_enabled":
            declaration_line = node.lineno
        # The inferences are identified by what they read, not by line text.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "JARVIS_STARTUP_MEMORY_MODE":
                inference_lines.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "state":
            inference_lines.append(node.lineno)

    assert declaration_line is not None, \
        "get_learning_database no longer consults local_first_enabled"
    assert inference_lines, "inferential promotions vanished — re-read this test"
    assert declaration_line < min(inference_lines), (
        f"operator declaration at line {declaration_line} is evaluated AFTER an "
        f"inference at line {min(inference_lines)}; an inference can now overrule it"
    )
