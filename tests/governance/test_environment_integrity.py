"""The 46% repetition was a missing package, not a bad prompt.

Measured before any of this was written: 29 of the live roadmap's 51 goals
could not import their own subject, 17 of them because ``fastapi`` -- DECLARED
at ``requirements.txt:162`` -- was absent from the venv. The model wrote correct
tests for modules that cannot load, VALIDATE failed identically every time, and
from the transcript that is indistinguishable from doing the same thing over
and over, which is exactly how the operator described it.

Three gates, three scopes, one question -- can this actually run here?
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import environment_integrity as EI


# ---------------------------------------------------------------------------
# The boot gate
# ---------------------------------------------------------------------------

def _manifest(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_manifests_are_found_by_name_not_by_path(tmp_path):
    """The governance profile has already moved once. A hardcoded path would
    have turned this gate into a silent no-op the day it moved."""
    _manifest(tmp_path, "some/deep/nest/requirements-governance.txt", "pytest\n")
    found = EI.runtime_manifests(tmp_path)
    assert [p.name for p in found] == ["requirements-governance.txt"]


def test_sandbox_worktree_clones_are_not_asserted_four_times(tmp_path):
    """The live tree carries `.worktrees/ouroboros__auto__bt-*` clones of the
    WHOLE repo. Enumerating a denylist returned four copies of every manifest."""
    _manifest(tmp_path, "ci/requirements-ov-surface.txt", "pytest\n")
    _manifest(
        tmp_path, ".worktrees/auto-bt-1/ci/requirements-ov-surface.txt", "pytest\n",
    )
    found = EI.runtime_manifests(tmp_path)
    assert len(found) == 1
    assert ".worktrees" not in str(found[0])


def test_a_gate_with_no_contract_does_not_refuse(tmp_path):
    """A gate that cannot find its own contract has no standing to brake."""
    verdict = EI.environment_verdict(tmp_path)
    assert verdict.satisfied is True
    assert "no runtime manifest" in verdict.reason


def test_a_declared_dependency_that_is_absent_is_a_desync(tmp_path):
    _manifest(
        tmp_path, "ci/requirements-ov-surface.txt",
        "definitely-not-a-real-distribution-xyz>=1\n",
    )
    verdict = EI.environment_verdict(tmp_path)
    assert verdict.satisfied is False
    assert verdict.missing and "xyz" in verdict.missing[0]


def test_a_satisfied_closure_passes(tmp_path):
    _manifest(tmp_path, "ci/requirements-ov-surface.txt", "pytest\n")
    assert EI.environment_verdict(tmp_path).satisfied is True


def test_a_version_difference_is_advisory_never_fatal(tmp_path):
    """A patch-level difference is drift, not starvation. Refusing to boot over
    it would make the gate the thing that stops the organism."""
    _manifest(tmp_path, "ci/requirements-ov-surface.txt", "pytest==0.0.0-nope\n")
    verdict = EI.environment_verdict(tmp_path)
    assert verdict.satisfied is True
    assert verdict.mismatched and verdict.mismatched[0][0] == "pytest"


def test_names_compare_under_pep503_normalisation(tmp_path):
    """`PyYAML`, `typing_extensions` and `ruamel.yaml` must match the metadata."""
    _manifest(tmp_path, "ci/requirements-ov-surface.txt", "PyTest\ntyping-extensions\n")
    assert EI.environment_verdict(tmp_path).satisfied is True


def test_the_gate_raises_only_when_armed(tmp_path, monkeypatch):
    _manifest(
        tmp_path, "ci/requirements-ov-surface.txt", "definitely-not-real-xyz\n",
    )
    monkeypatch.setenv("JARVIS_ENV_PREFLIGHT_ENABLED", "0")
    assert EI.assert_environment(tmp_path).satisfied is False   # no raise
    monkeypatch.setenv("JARVIS_ENV_PREFLIGHT_ENABLED", "1")
    with pytest.raises(EI.EnvironmentDesyncFault):
        EI.assert_environment(tmp_path)


def test_the_live_repo_closure_is_satisfied():
    """The real assertion, against the real tree: this is the gate `ov` runs.

    It is also the regression guard for the fix itself -- `pyflakes>=3` was
    genuinely missing from the venv and this gate is what found it.
    """
    root = Path(__file__).resolve().parents[2]
    verdict = EI.environment_verdict(root)
    assert verdict.satisfied, verdict.summary()
    assert verdict.manifests


# ---------------------------------------------------------------------------
# The per-target validator
# ---------------------------------------------------------------------------

def test_a_target_importing_an_absent_package_is_not_importable(tmp_path):
    src = tmp_path / "subject.py"
    src.write_text("import totally_absent_package_xyz\n", encoding="utf-8")
    assert EI.unresolvable_imports(src, tmp_path) == ("totally_absent_package_xyz",)


def test_stdlib_and_installed_imports_are_resolvable(tmp_path):
    src = tmp_path / "subject.py"
    src.write_text("import os\nimport sys\nimport pytest\n", encoding="utf-8")
    assert EI.unresolvable_imports(src, tmp_path) == ()


def test_first_party_imports_are_never_accused(tmp_path):
    """"First-party" must mean the same thing here as it does to the prompt's
    signature anchor, because both ask the same helper."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sibling.py").write_text("", encoding="utf-8")
    src = tmp_path / "pkg" / "subject.py"
    src.write_text("from pkg import sibling\n", encoding="utf-8")
    assert EI.unresolvable_imports(src, tmp_path) == ()


def test_an_unparseable_file_accuses_nobody(tmp_path):
    src = tmp_path / "broken.py"
    src.write_text("def (((\n", encoding="utf-8")
    assert EI.unresolvable_imports(src, tmp_path) == ()


def test_the_ast_answer_is_cached_by_content_not_forever(tmp_path):
    """Content-keyed, so installing a package mid-session is still seen."""
    src = tmp_path / "s.py"
    src.write_text("import totally_absent_package_xyz\n", encoding="utf-8")
    assert EI.unresolvable_imports(src, tmp_path)
    src.write_text("import os\n", encoding="utf-8")
    import os as _os
    _os.utime(src, (0, 0))          # force a distinct mtime
    assert EI.unresolvable_imports(src, tmp_path) == ()


def test_target_verdict_reports_the_blocking_module(tmp_path):
    subject = tmp_path / "subject.py"
    subject.write_text("import totally_absent_package_xyz\n", encoding="utf-8")
    verdict = EI.target_import_verdict(["subject.py"], "", tmp_path)
    assert verdict.importable is False
    assert verdict.unresolvable == ("totally_absent_package_xyz",)
    assert EI.UNRESOLVABLE_TARGET_DEPENDENCY in verdict.reason


def test_an_unanswerable_goal_is_reported_importable(tmp_path):
    """A wrong accusation deletes real work; a wrong pass costs one op that
    cooldown already bounds."""
    assert EI.target_import_verdict([], "", tmp_path).importable is True
    assert EI.target_import_verdict(["nope.py"], "", tmp_path).importable is True


# ---------------------------------------------------------------------------
# Whose fault is this ImportError?
# ---------------------------------------------------------------------------

_ERR = "ModuleNotFoundError: No module named 'requests'"


def test_an_import_the_candidate_added_is_a_dependency_violation(tmp_path):
    verdict = EI.classify_import_failure(
        _ERR, repo_root=tmp_path,
        candidate_source="import requests\n\ndef test_x():\n    assert 1\n",
        baseline_source="def test_x():\n    assert 1\n",
    )
    assert verdict.kind == EI.DEPENDENCY_VIOLATION
    assert verdict.module == "requests"
    assert verdict.net_new is True
    assert verdict.is_violation


def test_an_import_that_was_already_there_is_environment_starvation(tmp_path):
    """The case that dominates this repo's telemetry. The candidate is
    blameless, and telling it "never invent packages" is simply false."""
    verdict = EI.classify_import_failure(
        _ERR, repo_root=tmp_path,
        candidate_source="import requests\ndef test_x():\n    assert 1\n",
        baseline_source="import requests\n",
    )
    assert verdict.kind == EI.ENVIRONMENT_STARVATION
    assert verdict.is_violation is False
    assert "not at fault" in verdict.lesson


def test_a_starved_subject_the_candidate_never_touched(tmp_path):
    verdict = EI.classify_import_failure(
        _ERR, repo_root=tmp_path,
        candidate_source="def test_x():\n    assert 1\n",
        baseline_source="",
    )
    assert verdict.kind == EI.ENVIRONMENT_STARVATION


def test_a_net_new_import_is_detected_through_a_unified_diff(tmp_path):
    """A diff does not parse, so an AST attempt returns nothing and EVERY
    violation in diff mode would silently read as "added no imports"."""
    diff = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n"
        " def test_x():\n+import requests\n     assert 1\n"
    )
    verdict = EI.classify_import_failure(
        _ERR, repo_root=tmp_path, candidate_source=diff, baseline_source="",
    )
    assert verdict.kind == EI.DEPENDENCY_VIOLATION
    assert verdict.module == "requests"


def test_a_removed_import_never_blames_the_candidate(tmp_path):
    """Counting the `-` side would blame a candidate for an import it is
    deleting."""
    diff = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,1 @@\n"
        "-import requests\n def test_x():\n"
    )
    verdict = EI.classify_import_failure(
        _ERR, repo_root=tmp_path, candidate_source=diff, baseline_source="",
    )
    assert verdict.kind == EI.ENVIRONMENT_STARVATION


def test_declaredness_changes_the_advice_never_the_verdict(tmp_path):
    """`cv2` ships as `opencv-python`; any import-name-to-distribution map
    would be a guess, and a guess must never be load-bearing for a rollback."""
    (tmp_path / "requirements.txt").write_text("requests==2.0\n", encoding="utf-8")
    verdict = EI.classify_import_failure(
        _ERR, repo_root=tmp_path,
        candidate_source="import requests\n", baseline_source="",
    )
    assert verdict.kind == EI.DEPENDENCY_VIOLATION      # unchanged
    assert verdict.declared is True
    assert "DECLARED" in verdict.lesson


def test_a_non_import_failure_yields_an_empty_verdict(tmp_path):
    """"Not mine" must not read as "nothing failed" -- the caller keeps its
    existing classification."""
    verdict = EI.classify_import_failure(
        "AssertionError: assert 1 == 2", repo_root=tmp_path,
    )
    assert verdict.kind == ""
    assert verdict.lesson == ""


@pytest.mark.parametrize("junk", ["", None, "\x00\x00", "No module named"])
def test_classification_never_raises(junk, tmp_path):
    assert isinstance(
        EI.classify_import_failure(junk, repo_root=tmp_path),
        EI.ImportFailureVerdict,
    )


# ---------------------------------------------------------------------------
# The queue actually asks
# ---------------------------------------------------------------------------

def test_the_ranker_demotes_an_unimportable_target(tmp_path):
    """Demotion, not deletion. A goal whose dependency gets installed rises
    again on its own, next pass."""
    from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

    (tmp_path / "tests").mkdir()
    good = tmp_path / "good.py"
    good.write_text("import os\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("import totally_absent_package_xyz\n", encoding="utf-8")

    works = [
        GD.DiscoveredWork(target_file="bad.py", kind="roadmap_goal",
                          evidence="b", weight=1.0, subject_file="bad.py"),
        GD.DiscoveredWork(target_file="good.py", kind="roadmap_goal",
                          evidence="g", weight=1.0, subject_file="good.py"),
    ]
    verdicts = GD._import_verdicts(works, tmp_path)
    ranked = sorted(
        works,
        key=lambda w: (not GD._is_importable(verdicts.get(w.goal_id)), -w.weight),
    )
    assert ranked[0].target_file == "good.py"
    assert ranked[1].target_file == "bad.py"


def test_demoted_work_is_still_dispatchable_by_default(tmp_path):
    """Quarantining by default would silently delete 41% of the live queue over
    a judgement the operator has not made."""
    from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

    bad = tmp_path / "bad.py"
    bad.write_text("import totally_absent_package_xyz\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("", encoding="utf-8")
    work = GD.DiscoveredWork(
        target_file="bad.py", kind="roadmap_goal", evidence="b",
        weight=1.0, subject_file="bad.py",
    )
    assert GD.is_dispatchable(work, tmp_path) is True


def test_quarantine_refuses_only_when_the_operator_arms_it(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

    bad = tmp_path / "bad.py"
    bad.write_text("import totally_absent_package_xyz\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("", encoding="utf-8")
    work = GD.DiscoveredWork(
        target_file="bad.py", kind="roadmap_goal", evidence="b",
        weight=1.0, subject_file="bad.py",
    )
    monkeypatch.setenv("JARVIS_QUARANTINE_UNIMPORTABLE_TARGETS", "1")
    assert GD.is_dispatchable(work, tmp_path) is False


def test_the_two_new_classes_carry_their_own_advice():
    """A lesson class with no mitigation renders as generic advice -- which is
    the exact defect these classes exist to end."""
    from backend.core.ouroboros.governance import lesson_memory as LM

    assert LM._MITIGATIONS[EI.DEPENDENCY_VIOLATION]
    assert LM._MITIGATIONS[EI.ENVIRONMENT_STARVATION]
    assert "not at fault" in LM._MITIGATIONS[EI.ENVIRONMENT_STARVATION]


def test_the_reason_constant_is_owned_by_one_module():
    """Two copies of the same rule is how two filters come to disagree."""
    from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

    assert GD._UNRESOLVABLE_REASON == EI.UNRESOLVABLE_TARGET_DEPENDENCY


# ---------------------------------------------------------------------------
# The false positive the LIVE census caught
# ---------------------------------------------------------------------------

def test_a_package_first_party_only_via_pythonpath_is_not_accused(tmp_path):
    """Found in production, not in review: the first census demoted nine goals
    for `vision` and three for `core`. Both are subpackages of `backend/` and
    resolve perfectly under the pytest that actually runs VALIDATE, because
    `pytest.ini` declares `pythonpath = . backend`.

    Accusing a first-party package of being a missing dependency is the worst
    thing this module can do -- it demotes exactly the work that CAN land.
    """
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\npythonpath = . backend\n", encoding="utf-8",
    )
    (tmp_path / "backend" / "vision").mkdir(parents=True)
    (tmp_path / "backend" / "vision" / "__init__.py").write_text("", encoding="utf-8")
    src = tmp_path / "backend" / "caller.py"
    src.write_text("from vision import thing\n", encoding="utf-8")
    assert EI.unresolvable_imports(src, tmp_path) == ()


def test_the_toml_form_of_pythonpath_is_read_too(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["backend", "."]\n',
        encoding="utf-8",
    )
    (tmp_path / "backend" / "core").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "__init__.py").write_text("", encoding="utf-8")
    src = tmp_path / "x.py"
    src.write_text("import core\n", encoding="utf-8")
    assert EI.unresolvable_imports(src, tmp_path) == ()


def test_a_declared_root_that_does_not_exist_is_ignored(tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\npythonpath = . nope_not_here\n", encoding="utf-8",
    )
    assert all(p.is_dir() for p in EI._pythonpath_roots(tmp_path))


def test_a_repo_with_no_pythonpath_declares_no_roots(tmp_path):
    assert EI._pythonpath_roots(tmp_path) == ()


def test_the_live_repo_declares_backend_as_an_import_root():
    """The regression guard for the real tree: if `pytest.ini` stops declaring
    it, this gate starts accusing 200+ first-party modules."""
    root = Path(__file__).resolve().parents[2]
    roots = EI._pythonpath_roots(root)
    assert any(p.name == "backend" for p in roots), roots


# ---------------------------------------------------------------------------
# Impossible here, or merely not installed?
# ---------------------------------------------------------------------------

def test_an_objective_c_framework_is_unavailable_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    v = EI.platform_capability("Quartz")
    assert v.available is False
    assert v.reason == "platform_bound"
    assert "darwin" in v.detail


def test_the_same_framework_is_available_on_macos(monkeypatch):
    """Quarantine must be a fact about the MACHINE, not a deletion. Driven
    from a Mac, the identical roadmap goal becomes selectable again."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert EI.platform_capability("Quartz").available is True


def test_an_ordinary_missing_package_is_never_platform_bound():
    """`torch` is absent, not impossible. Quarantining it would delete real
    work over a judgement the operator has not made."""
    assert EI.platform_capability("torch").available is True
    assert EI.platform_capability("chromadb").available is True


def test_a_display_bound_module_follows_the_actual_display(monkeypatch):
    """WSLg publishes a real DISPLAY, so this host is NOT headless and
    `pyautogui` is a missing package here rather than an impossible one.
    Asking the machine beats assuming "Linux means headless"."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert EI.platform_capability("pyautogui").available is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert EI.platform_capability("pyautogui").available is True


def test_a_manifest_marker_is_authoritative(tmp_path, monkeypatch):
    """The self-documenting lever: declaring a marker teaches the gate about a
    new platform-bound dependency with no code change."""
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "requirements.txt").write_text(
        'somepkg>=1.0; platform_system == "Darwin"\n', encoding="utf-8",
    )
    v = EI.platform_capability("somepkg", repo_root=tmp_path)
    assert v.available is False
    assert v.reason == "marker_excluded"
    # ...and without the marker the same name is just a missing package.
    (tmp_path / "requirements.txt").write_text("somepkg>=1.0\n", encoding="utf-8")
    assert EI.platform_capability("somepkg", repo_root=tmp_path).available is True


def test_the_live_manifest_marker_for_coremltools_is_honoured():
    """Regression guard on the real tree, and proof the marker path is wired."""
    root = Path(__file__).resolve().parents[2]
    if sys.platform == "darwin":
        pytest.skip("the marker excludes every platform BUT this one")
    assert EI.platform_capability("coremltools", repo_root=root).available is False


def test_an_unknown_module_is_never_accused():
    """Silence is not evidence of impossibility."""
    assert EI.platform_capability("some_package_nobody_has_heard_of").available is True
    assert EI.platform_capability("").available is True


def test_the_operator_can_extend_the_registry(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert EI.platform_capability("weirdlib").available is True
    monkeypatch.setenv("JARVIS_PLATFORM_BOUND_MODULES", "weirdlib:darwin|win32")
    assert EI.platform_capability("weirdlib").available is False


@pytest.mark.parametrize("junk", ["", None, 42, "a.b.c"])
def test_platform_capability_never_raises(junk):
    assert isinstance(EI.platform_capability(junk), EI.PlatformVerdict)


def test_the_verdict_separates_impossible_from_unprovisioned(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    src = tmp_path / "subject.py"
    src.write_text("import Quartz\nimport totally_absent_xyz\n", encoding="utf-8")
    v = EI.target_import_verdict(["subject.py"], "", tmp_path)
    assert v.importable is False
    assert v.impossible is True
    assert v.structural == ("Quartz",)
    assert set(v.unresolvable) == {"Quartz", "totally_absent_xyz"}
    assert EI.PLATFORM_UNAVAILABLE in v.reason


def test_an_unprovisioned_only_target_is_not_impossible(tmp_path):
    src = tmp_path / "subject.py"
    src.write_text("import totally_absent_xyz\n", encoding="utf-8")
    v = EI.target_import_verdict(["subject.py"], "", tmp_path)
    assert v.impossible is False
    assert EI.UNRESOLVABLE_TARGET_DEPENDENCY in v.reason


def test_structural_quarantine_needs_no_operator_switch(tmp_path, monkeypatch):
    """A macOS framework on Linux is reversed by nothing, so leaving it
    dispatchable burns an op per pass forever."""
    from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("JARVIS_QUARANTINE_UNIMPORTABLE_TARGETS", raising=False)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mac.py").write_text("", encoding="utf-8")
    (tmp_path / "mac.py").write_text("import Quartz\n", encoding="utf-8")
    work = GD.DiscoveredWork(
        target_file="mac.py", kind="roadmap_goal", evidence="m",
        weight=1.0, subject_file="mac.py",
    )
    assert GD.is_dispatchable(work, tmp_path) is False


def test_an_unprovisioned_target_stays_dispatchable_without_the_switch(tmp_path, monkeypatch):
    """The other half of the split: absent is reversed by an install, so the
    goal is demoted and rises again — never refused."""
    from backend.core.ouroboros.governance.autonomy import goal_discovery as GD

    monkeypatch.delenv("JARVIS_QUARANTINE_UNIMPORTABLE_TARGETS", raising=False)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("", encoding="utf-8")
    (tmp_path / "bad.py").write_text("import totally_absent_xyz\n", encoding="utf-8")
    work = GD.DiscoveredWork(
        target_file="bad.py", kind="roadmap_goal", evidence="b",
        weight=1.0, subject_file="bad.py",
    )
    assert GD.is_dispatchable(work, tmp_path) is True


def test_colorama_is_now_declared():
    """It is imported by repo code and was declared in NO manifest, which left
    two roadmap goals permanently unlandable."""
    root = Path(__file__).resolve().parents[2]
    assert "colorama" in EI.declared_distribution_names(root)


def test_the_pyobjc_declaration_carries_its_platform_marker():
    """Without the marker the line reads as a dependency Linux is merely
    missing, and the boot gate would demand an unbuildable wheel."""
    root = Path(__file__).resolve().parents[2]
    if sys.platform == "darwin":
        pytest.skip("the marker excludes every platform BUT this one")
    v = EI.platform_capability("pyobjc-framework-libdispatch", repo_root=root)
    assert v.available is False and v.reason == "marker_excluded"
