"""Slice 76 Phase 5 — async PR janitor safety-gate + backoff tests.

The load-bearing invariant: ``should_close`` NEVER returns True for a human PR.
A closed PR is irreversible at 67k scale, so this predicate is the single
chokepoint every closure flows through — it gets the most adversarial tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# load the script module by path (scripts/ is not an importable package)
_SPEC = importlib.util.spec_from_file_location(
    "async_pr_janitor",
    Path(__file__).resolve().parents[2] / "scripts" / "utils" / "async_pr_janitor.py",
)
janitor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(janitor)  # type: ignore[union-attr]


def _bot_pr(number=99999, title="🚨 Fix CI/CD: PR Automation & Validation (Run #1)"):
    return {"number": number, "title": title,
            "user": {"login": "github-actions[bot]", "type": "Bot"}}


def _human_pr(number=36818, title="fix(governance): R1 timeout coherence"):
    return {"number": number, "title": title,
            "user": {"login": "drussell23", "type": "User"}}


# --- the safety invariant: never close a human PR ---

def test_closes_a_clean_bot_pr():
    assert janitor.should_close(_bot_pr()) is True


def test_accepts_search_api_bot_login_too():
    pr = _bot_pr()
    pr["user"]["login"] = "app/github-actions"
    assert janitor.should_close(pr) is True


def test_never_closes_a_human_pr():
    assert janitor.should_close(_human_pr()) is False


def test_never_closes_a_protected_id_even_if_bot_shaped():
    # belt-and-suspenders: a protected number with a bot-looking author/title
    pr = _bot_pr(number=106)
    assert janitor.should_close(pr) is False


def test_every_protected_id_is_refused():
    for n in janitor.PROTECTED_PR_IDS:
        assert janitor.should_close(_bot_pr(number=n)) is False, n


def test_human_login_with_bot_type_is_refused():
    # author.type gate alone isn't enough — login must also match a known bot
    pr = _bot_pr()
    pr["user"]["login"] = "drussell23"
    assert janitor.should_close(pr) is False


def test_bot_author_but_wrong_title_is_refused():
    # title gate guards against a non-runaway bot PR (e.g. dependabot-shaped)
    assert janitor.should_close(_bot_pr(title="chore(deps): bump foo")) is False


def test_user_type_is_refused_even_with_bot_login():
    pr = _bot_pr()
    pr["user"]["type"] = "User"
    assert janitor.should_close(pr) is False


def test_missing_or_malformed_fields_fail_closed():
    assert janitor.should_close({}) is False
    assert janitor.should_close({"number": "x", "user": {"type": "Bot"}}) is False
    assert janitor.should_close({"number": 5, "user": None}) is False


# --- adaptive backoff ---

def test_backoff_honors_retry_after():
    assert janitor.compute_backoff(0, 42.0) == 42.0


def test_backoff_is_exponential_without_retry_after():
    assert janitor.compute_backoff(0, None) == 1.0
    assert janitor.compute_backoff(1, None) == 2.0
    assert janitor.compute_backoff(3, None) == 8.0


def test_backoff_is_capped():
    assert janitor.compute_backoff(50, None) == 300.0
    assert janitor.compute_backoff(0, 99999.0) == 300.0


# --- checkpoint resume ---

def test_load_done_parses_checkpoint(tmp_path):
    cp = tmp_path / "cp.jsonl"
    cp.write_text('{"number": 1, "ts": 0}\n{"number": 2}\nbad line\n{"number": 3}\n',
                  encoding="utf-8")
    assert janitor._load_done(cp) == {1, 2, 3}


def test_load_done_absent_is_empty(tmp_path):
    assert janitor._load_done(tmp_path / "nope.jsonl") == set()
