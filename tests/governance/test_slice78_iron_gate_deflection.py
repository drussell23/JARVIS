"""Slice 78 Track 1 — Iron Gate test-only patch guard (source-domain purity).

A SWE-bench failure mode: the model "resolves" a bug by editing the TEST files
(the held-out fail_to_pass / pass_to_pass suite) instead of the source defect.
`SCORE_REJECT_TEST_MODS` already catches this — but only at SCORING time, after
the op burned its full budget. This guard catches it EARLY (post-GENERATE,
pre-APPLY, Iron-Gate style) and short-circuits to GENERATE_RETRY with corrective
feedback, so the model is told to fix the source instead of the tests.

Verify-first: swe_bench candidates carry `full_content` per file, not diffs — so
the guard inspects the candidate's MODIFIED FILE PATHS (the pre-apply signal),
with unified-diff header parsing as a fallback for diff-shaped inputs.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.patch_domain_guard import (
    is_test_path,
    extract_modified_paths,
    verify_patch_domain_purity,
    build_retry_feedback,
    PatchDomainGuard,
)


# --- test-path classification ---

def test_recognizes_common_test_paths():
    for p in ("tests/test_foo.py", "foo/tests/bar.py", "test_thing.py",
              "src/Markdown-test.ts", "pkg/foo_test.go", "a/b/conftest.py",
              "spec/models_spec.rb", "src/__tests__/x.js"):
        assert is_test_path(p) is True, p


def test_source_paths_are_not_test_paths():
    for p in ("qutebrowser/misc/guiprocess.py", "lib/ansible/cli/doc.py",
              "src/Markdown.ts", "backend/api/server.py", "contest.py"):
        assert is_test_path(p) is False, p


# --- the core purity verdict ---

def test_test_only_patch_is_impure():
    v = verify_patch_domain_purity(["tests/test_a.py", "tests/test_b.py"], [])
    assert v.is_pure is False
    assert v.test_only is True
    assert set(v.test_files) == {"tests/test_a.py", "tests/test_b.py"}


def test_patch_touching_source_is_pure():
    v = verify_patch_domain_purity(["src/core.py", "tests/test_core.py"], [])
    assert v.is_pure is True
    assert v.test_only is False
    assert list(v.source_files) == ["src/core.py"]


def test_target_paths_count_as_test_files():
    # the SWE-bench test_patch footprint (Slice 69 target_paths) is authoritative:
    # a non-conventionally-named file that IS the held-out test counts as a test.
    v = verify_patch_domain_purity(["weird/holdout_suite.py"],
                                   ["weird/holdout_suite.py"])
    assert v.is_pure is False and v.test_only is True


def test_empty_patch_is_not_flagged():
    # no modified paths → cannot judge → not test-only (let other gates handle)
    v = verify_patch_domain_purity([], [])
    assert v.test_only is False
    assert v.is_pure is True


def test_single_source_file_is_pure():
    v = verify_patch_domain_purity(["qutebrowser/misc/guiprocess.py"], [])
    assert v.is_pure is True and v.test_only is False


# --- diff header extraction (fallback path) ---

def test_extract_paths_from_unified_diff():
    diff = (
        "diff --git a/src/core.py b/src/core.py\n"
        "--- a/src/core.py\n+++ b/src/core.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/tests/test_core.py b/tests/test_core.py\n"
        "--- a/tests/test_core.py\n+++ b/tests/test_core.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    paths = extract_modified_paths(diff)
    assert "src/core.py" in paths and "tests/test_core.py" in paths


def test_extract_handles_dev_null_new_file():
    diff = "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+x\n"
    assert extract_modified_paths(diff) == ["src/new.py"]


def test_diff_only_touching_tests_is_impure():
    diff = "--- a/tests/t.py\n+++ b/tests/t.py\n@@ -1 +1 @@\n-a\n+b\n"
    v = verify_patch_domain_purity(extract_modified_paths(diff), [])
    assert v.test_only is True


# --- corrective feedback ---

def test_retry_feedback_names_the_test_files_and_demands_source():
    v = verify_patch_domain_purity(["tests/test_a.py"], [])
    fb = build_retry_feedback(v)
    assert "test" in fb.lower()
    assert "source" in fb.lower()
    assert "tests/test_a.py" in fb


# --- the gate wrapper (mirrors AsciiStrictGate) ---

def test_guard_disabled_is_inert(monkeypatch):
    monkeypatch.setenv("JARVIS_PATCH_DOMAIN_GUARD_ENABLED", "false")
    g = PatchDomainGuard()
    assert g.enabled is False
    ok, reason, verdict = g.check(["tests/test_a.py"], [])
    assert ok is True  # inert when disabled


def test_guard_blocks_test_only_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_PATCH_DOMAIN_GUARD_ENABLED", "true")
    g = PatchDomainGuard()
    assert g.enabled is True
    ok, reason, verdict = g.check(["tests/test_a.py"], [])
    assert ok is False
    assert "test" in reason.lower()


def test_guard_passes_source_patch_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_PATCH_DOMAIN_GUARD_ENABLED", "true")
    g = PatchDomainGuard()
    ok, reason, verdict = g.check(["src/core.py"], [])
    assert ok is True


def test_guard_never_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("JARVIS_PATCH_DOMAIN_GUARD_ENABLED", "true")
    g = PatchDomainGuard()
    ok, _r, _v = g.check(None, None)  # type: ignore[arg-type]
    assert ok is True  # fail-open: never block the pipeline on a guard error
