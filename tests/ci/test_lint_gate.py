"""The gate that would have caught both cockpit incidents on the commit that made them.

`ci-cd-pipeline.yml` runs `flake8 backend/ ... || true`; the result is
discarded, so an undefined name has never failed a build. Two shipped in the
operator's front door and stayed invisible for ten days.

`|| true` cannot simply be removed — ~812 undefined-name findings exist, most
in vendored `venv/` and `core/quarantine` — so the gate is differential: it
reports only findings on lines the branch actually touched. Legacy debt never
fails a build; a NEW undefined name cannot land.

Every test here builds a REAL git repository in a tmp dir. A differential gate
tested against a mocked diff would be testing the mock.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyflakes", reason="pyflakes gates this suite")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci import lint_gate  # noqa: E402


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    """A real repo with a `main` carrying one pre-existing undefined name."""
    _git(["init", "-q", "-b", "main"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    legacy = tmp_path / "legacy.py"
    legacy.write_text(
        "def old():\n"
        "    return already_undefined_on_main\n",
        encoding="utf-8",
    )
    _git(["add", "legacy.py"], tmp_path)
    _git(["commit", "-qm", "base"], tmp_path)
    _git(["checkout", "-q", "-b", "feature"], tmp_path)
    return tmp_path


class TestDifferential:
    def test_a_new_undefined_name_on_a_changed_line_fails(self, repo):
        (repo / "new.py").write_text(
            "def f():\n    return brand_new_typo\n", encoding="utf-8")
        _git(["add", "new.py"], repo)
        _git(["commit", "-qm", "add"], repo)
        res = lint_gate.differential_findings(repo, ref="main")
        assert [f.name for f in res.findings] == ["brand_new_typo"]
        assert not res.ok

    def test_legacy_findings_on_untouched_lines_are_ignored(self, repo):
        """The whole point: existing debt must never fail someone else's build."""
        legacy = repo / "legacy.py"
        legacy.write_text(
            legacy.read_text(encoding="utf-8") + "\n\ndef added():\n    return 1\n",
            encoding="utf-8")
        _git(["add", "legacy.py"], repo)
        _git(["commit", "-qm", "touch"], repo)
        res = lint_gate.differential_findings(repo, ref="main")
        assert res.ok, f"legacy debt leaked into the gate: {res.findings}"

    def test_a_new_defect_in_a_legacy_file_still_fails(self, repo):
        """Touching a dirty file does not grant amnesty for new lines in it."""
        legacy = repo / "legacy.py"
        legacy.write_text(
            legacy.read_text(encoding="utf-8")
            + "\n\ndef added():\n    return another_new_typo\n",
            encoding="utf-8")
        _git(["add", "legacy.py"], repo)
        _git(["commit", "-qm", "touch"], repo)
        res = lint_gate.differential_findings(repo, ref="main")
        assert [f.name for f in res.findings] == ["another_new_typo"]

    def test_untracked_files_are_gated_in_full(self, repo):
        """A brand-new file is entirely new surface, committed or not."""
        (repo / "fresh.py").write_text(
            "def f():\n    return untracked_typo\n", encoding="utf-8")
        res = lint_gate.differential_findings(repo, ref="main")
        assert any(f.name == "untracked_typo" for f in res.findings)

    def test_deleted_files_are_not_linted(self, repo):
        (repo / "legacy.py").unlink()
        _git(["add", "-A"], repo)
        _git(["commit", "-qm", "rm"], repo)
        res = lint_gate.differential_findings(repo, ref="main")
        assert res.ok


class TestInertNamesAreNotReported:
    """16 of this repo's 24 findings are annotations that never evaluate.

    Reporting them would push people to add runtime imports for names that
    currently cost nothing — trading zero cost for import time and
    circular-import risk. That is worse than the thing being fixed."""

    def test_annotation_under_future_import_is_inert(self, repo):
        (repo / "ann.py").write_text(
            "from __future__ import annotations\n\n"
            "def f() -> NeverImported:\n    return None\n",
            encoding="utf-8")
        res = lint_gate.differential_findings(repo, ref="main")
        assert res.ok, f"inert annotation reported: {res.findings}"

    def test_quoted_annotation_is_inert_even_without_the_future_import(self, repo):
        """A string annotation is a string. Missing this mis-classified
        `verify_gate.py` in the first pass of this analysis."""
        (repo / "q.py").write_text(
            'def f(x: "NeverImported") -> None:\n    return None\n',
            encoding="utf-8")
        res = lint_gate.differential_findings(repo, ref="main")
        assert res.ok, f"quoted annotation reported: {res.findings}"

    def test_an_annotation_name_used_in_EXECUTABLE_position_is_still_reported(
        self, repo,
    ):
        """Inertness is a property of the POSITION, never of the name."""
        (repo / "x.py").write_text(
            "from __future__ import annotations\n\n"
            "def f() -> Thing:\n"
            "    return Thing()\n",           # executable — real NameError
            encoding="utf-8")
        res = lint_gate.differential_findings(repo, ref="main")
        assert [f.name for f in res.findings] == ["Thing"]


class TestBlindnessIsNotAPass:
    def test_an_unresolvable_base_ref_is_reported_not_swallowed(self, repo):
        res = lint_gate.differential_findings(repo, ref="no/such/ref")
        assert res.blind is not None
        assert not res.ok, "a gate that cannot see must not report success"

    def test_require_base_turns_blindness_into_failure(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        rc = lint_gate.main(
            ["--root", str(repo), "--base", "no/such/ref",
             "--differential-only", "--require-base"])
        assert rc == 1

    def test_without_require_base_blindness_is_a_loud_skip(self, repo, capsys):
        rc = lint_gate.main(
            ["--root", str(repo), "--base", "no/such/ref", "--differential-only"])
        assert rc == 0
        assert "BLIND" in capsys.readouterr().out


class TestResilience:
    def test_a_syntax_error_is_a_finding_not_silence(self, repo):
        (repo / "broken.py").write_text("def f(:\n", encoding="utf-8")
        res = lint_gate.differential_findings(repo, ref="main")
        assert any("syntax" in f.name for f in res.findings)

    def test_excluded_trees_are_never_gated(self, repo, monkeypatch):
        vendored = repo / "backend" / "venv" / "lib"
        vendored.mkdir(parents=True)
        (vendored / "v.py").write_text(
            "def f():\n    return vendored_typo\n", encoding="utf-8")
        res = lint_gate.differential_findings(repo, ref="main")
        assert res.ok, f"vendored code leaked in: {res.findings}"

    def test_exclusions_extend_and_cannot_be_emptied(self, monkeypatch):
        monkeypatch.setenv(lint_gate.EXCLUDE_ENV, "")
        assert lint_gate.DEFAULT_EXCLUDES[0] in lint_gate.excludes()
        monkeypatch.setenv(lint_gate.EXCLUDE_ENV, "/extra/")
        got = lint_gate.excludes()
        assert "/extra/" in got
        assert lint_gate.DEFAULT_EXCLUDES[0] in got

    def test_undefined_names_never_raises_on_a_missing_file(self, tmp_path):
        assert lint_gate.undefined_names(tmp_path / "nope.py")

    def test_non_python_changes_are_ignored(self, repo):
        (repo / "README.md").write_text("# hi\n", encoding="utf-8")
        _git(["add", "README.md"], repo)
        _git(["commit", "-qm", "docs"], repo)
        res = lint_gate.differential_findings(repo, ref="main")
        assert res.ok


class TestPackageGate:
    def test_a_clean_package_stays_clean(self, repo):
        pkg = repo / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
        res = lint_gate.package_findings(repo, packages=["pkg"])
        assert res.ok and res.scanned_files == 1

    def test_a_regression_in_a_clean_package_fails(self, repo):
        pkg = repo / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("def f():\n    return oops\n", encoding="utf-8")
        res = lint_gate.package_findings(repo, packages=["pkg"])
        assert [f.name for f in res.findings] == ["oops"]

    def test_a_missing_package_is_not_a_silent_pass_for_the_others(self, repo):
        pkg = repo / "pkg"
        pkg.mkdir()
        (pkg / "a.py").write_text("def f():\n    return oops\n", encoding="utf-8")
        res = lint_gate.package_findings(repo, packages=["nope", "pkg"])
        assert [f.name for f in res.findings] == ["oops"]
