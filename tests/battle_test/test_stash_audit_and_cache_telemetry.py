"""Stash triage + DW cache telemetry.

Both against real substrate: a REAL git repo for the audit (a mocked subprocess
would "prove" a stack never exercised) and real usage-payload shapes for the
telemetry, including the shape DoubleWord actually returned on 2026-07-24.
"""

from __future__ import annotations

import subprocess

import pytest

from backend.core.ouroboros.governance.doubleword_provider import (
    emit_cache_telemetry,
    extract_cache_usage,
)
from scripts import jarvis_stash_audit as audit


def _git(root, *a):
    return subprocess.run(
        ["git", "-C", str(root), *a], capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.py").write_text("base\n")
    _git(tmp_path, "add", "f.py")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


# ---------------------------------------------------------------------------
# Stash triage
# ---------------------------------------------------------------------------


def test_dry_run_mutates_nothing(repo):
    (repo / "f.py").write_text("dirty\n")
    _git(repo, "stash", "push", "-q", "-m", "work")

    rep = audit.audit(str(repo), apply=False)

    assert rep["total"] == 1
    assert rep["applied"] is False
    assert rep["dropped"] == 0 and rep["archived"] == 0
    assert len(_git(repo, "stash", "list").stdout.strip().splitlines()) == 1


def test_divergent_stash_is_archived_before_being_dropped(repo):
    """Unique work must survive the clear — recoverable from the archive ref."""
    (repo / "f.py").write_text("UNIQUE WORK\n")
    _git(repo, "stash", "push", "-q", "-m", "unique")

    rep = audit.audit(str(repo), apply=True)

    assert rep["total"] == 1
    assert len(rep["divergent"]) == 1, rep
    assert rep["archived"] == 1 and rep["dropped"] == 1
    assert _git(repo, "stash", "list").stdout.strip() == "", "stack not cleared"

    refs = _git(repo, "for-each-ref", "--format=%(objectname)", audit.ARCHIVE_NS)
    shas = [s for s in refs.stdout.split() if s]
    assert shas, "divergent work was dropped without an archive ref"

    # The archived snapshot really restores the work.
    assert _git(repo, "stash", "apply", shas[0]).returncode == 0
    assert (repo / "f.py").read_text() == "UNIQUE WORK\n"


def test_integrated_stash_is_still_archived(repo):
    """Even a zero-diff entry gets a recovery ref BEFORE any drop — a classifier
    bug must never be able to destroy work."""
    (repo / "f.py").write_text("landed\n")
    _git(repo, "stash", "push", "-q", "-m", "will-land")
    # Land the same content on HEAD so the stash becomes redundant.
    (repo / "f.py").write_text("landed\n")
    _git(repo, "add", "f.py")
    _git(repo, "commit", "-qm", "land it")

    rep = audit.audit(str(repo), apply=True)

    assert len(rep["integrated"]) == 1, rep
    assert rep["archived"] == 1, "integrated entry was dropped unarchived"
    assert rep["dropped"] == 1
    assert _git(repo, "stash", "list").stdout.strip() == ""


def test_clears_a_multi_entry_stack_completely(repo):
    for i in range(5):
        (repo / "f.py").write_text(f"rev{i}\n")
        _git(repo, "stash", "push", "-q", "-m", f"s{i}")

    rep = audit.audit(str(repo), apply=True)

    assert rep["total"] == 5
    assert rep["archived"] == 5 and rep["dropped"] == 5
    assert _git(repo, "stash", "list").stdout.strip() == ""


def test_empty_stack_is_a_clean_noop(repo):
    rep = audit.audit(str(repo), apply=True)
    assert rep["total"] == 0 and rep["dropped"] == 0 and not rep["errors"]


def test_archive_failure_blocks_every_drop(repo, monkeypatch):
    """FAIL-CLOSED: if archiving any entry fails, nothing is dropped at all."""
    (repo / "f.py").write_text("precious\n")
    _git(repo, "stash", "push", "-q", "-m", "precious")

    monkeypatch.setattr(audit, "archive", lambda *a, **k: None)

    rep = audit.audit(str(repo), apply=True)

    assert rep["dropped"] == 0, "dropped despite a failed archive"
    assert rep["errors"]
    assert len(_git(repo, "stash", "list").stdout.strip().splitlines()) == 1


def test_unreadable_entry_is_never_classified_integrated(repo, monkeypatch):
    """A diff we cannot read fails CLOSED — never 'safe to drop'."""
    (repo / "f.py").write_text("x\n")
    _git(repo, "stash", "push", "-q", "-m", "s")

    real = audit._git

    def _fail_diff(root, *args):
        if args and args[0] == "diff":
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(root, *args)

    monkeypatch.setattr(audit, "_git", _fail_diff)
    rep = audit.audit(str(repo), apply=False)
    assert len(rep["unreadable"]) == 1 and not rep["integrated"]


# ---------------------------------------------------------------------------
# Cache telemetry
# ---------------------------------------------------------------------------


def test_omitted_cache_fields_degrade_gracefully():
    """No cache fields at all -> None, so absence never masquerades as a
    measured zero."""
    assert extract_cache_usage({"prompt_tokens": 500}) is None
    assert emit_cache_telemetry({"prompt_tokens": 500}) is None
    assert extract_cache_usage(None) is None
    assert extract_cache_usage("not-a-dict") is None


def test_measured_zero_is_distinct_from_absent():
    """Cache armed but nothing warm yet is REAL data and must be reported."""
    stats = extract_cache_usage({
        "prompt_tokens": 100,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    })
    assert stats is not None
    assert stats["hit_ratio"] == 0.0
    assert stats["saved_input_units"] == 0.0


def test_savings_math_on_the_real_observed_payload():
    """Shape DW actually returned on 2026-07-24 (2166 prompt tokens)."""
    stats = extract_cache_usage({
        "prompt_tokens": 2166,
        "completion_tokens": 6083,
        "cache_read_input_tokens": 1900,
        "cache_creation_input_tokens": 0,
    })
    assert stats["fresh_input_tokens"] == 266
    # 266 fresh + 1900 * 0.1 = 456 billed, vs 2166 uncached.
    assert stats["billed_input_units"] == pytest.approx(456.0)
    assert stats["uncached_input_units"] == pytest.approx(2166.0)
    assert stats["saved_input_units"] == pytest.approx(1710.0)
    assert stats["hit_ratio"] == pytest.approx(0.8772, abs=1e-3)


def test_cache_write_costs_more_than_plain_input():
    """A write is a PREMIUM — savings must not be claimed on the write itself,
    or the first call of every soak would report phantom gains."""
    stats = extract_cache_usage({
        "prompt_tokens": 1000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1000,
    })
    assert stats["billed_input_units"] > stats["uncached_input_units"]
    assert stats["saved_input_units"] < 0


def test_multipliers_are_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_CACHE_READ_MULT", "0.5")
    stats = extract_cache_usage({
        "prompt_tokens": 1000, "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 0,
    })
    assert stats["billed_input_units"] == pytest.approx(500.0)


def test_garbage_values_never_raise():
    for bad in (
        {"prompt_tokens": "x", "cache_read_input_tokens": None},
        {"cache_read_input_tokens": -5, "prompt_tokens": 10},
        {"cache_creation_input_tokens": [1, 2], "prompt_tokens": 10},
    ):
        out = extract_cache_usage(bad)
        assert out is None or isinstance(out, dict)


def test_prompt_tokens_exclusive_of_reads_does_not_go_negative():
    """Some providers report prompt_tokens EXCLUDING cached reads; fresh must
    clamp at 0 rather than emitting a negative."""
    stats = extract_cache_usage({
        "prompt_tokens": 100, "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 0,
    })
    assert stats["fresh_input_tokens"] == 0
