"""A test exercises its subject THROUGH the subject's collaborators.

`collect_anchor_sources` anchored the module under test and nothing else, so
every boundary the test had to cross was unanchored and the model was guessing
at it. Measured in `bt-2026-09-18-034951`: `tests/test_trace_live_error.py`
anchored `backend/trace_live_error.py`, whose sole first-party import
(`vision.multi_space_intelligence`) never appeared in the prompt.

One hop, deliberately: the transitive closure of a repo this size is most of
the repo, and the signature budget would spend itself on modules the test never
touches.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import ast_signature_anchor as A


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "dep.py").write_text(
        "class RealBuilder:\n"
        "    def build(self, x):\n"
        "        return x\n"
    )
    # Imports its sibling the way this repo does — by package-relative name,
    # resolved against the importer's own directory, not the repo root.
    (tmp_path / "pkg" / "subject.py").write_text(
        "from dep import RealBuilder\n"
        "import os\n"
        "def run():\n"
        "    return RealBuilder().build(1)\n"
    )
    return tmp_path


def _sources(repo: Path, target: str, desc: str = ""):
    return [lbl for lbl, _ in A.collect_anchor_sources([target], desc, repo)]


def test_the_subjects_first_party_import_is_anchored(repo: Path):
    """THE regression: the collaborator the test must cross was invisible."""
    got = _sources(repo, "tests/test_subject.py")
    assert any("subject.py" in s for s in got), got
    assert any("dep.py" in s for s in got), got


def test_the_import_resolves_against_the_IMPORTERS_package_root(repo: Path):
    """`from dep import ...` inside `pkg/subject.py` means `pkg/dep.py`, not
    `<repo>/dep.py`. Walking the importer's ancestors is how Python finds it,
    and it is why no package prefix is named in the code."""
    dep = A._resolve_first_party("dep", repo / "pkg" / "subject.py", repo)
    assert dep is not None and dep.name == "dep.py"
    assert A._resolve_first_party("dep", repo / "tests" / "x.py", repo) is None


def test_stdlib_and_third_party_are_not_anchored(repo: Path):
    """"First-party" means "resolves to a file in this repository". Anything
    else is better covered by the model's training than by a signature block,
    and would spend the budget to say so."""
    assert A._resolve_first_party("os", repo / "pkg" / "subject.py", repo) is None
    assert A._resolve_first_party("fastapi", repo / "pkg" / "subject.py", repo) is None
    got = _sources(repo, "tests/test_subject.py")
    assert not any("os.py" in s for s in got)


def test_it_is_ONE_hop_not_a_closure(repo: Path):
    """A second hop is most of the repo. `grand.py` is imported by `dep.py`,
    which is itself only a dependency — it must not be pulled in."""
    (repo / "pkg" / "grand.py").write_text("def deep():\n    return 1\n")
    (repo / "pkg" / "dep.py").write_text(
        "from grand import deep\n"
        "class RealBuilder:\n"
        "    def build(self, x):\n"
        "        return deep()\n"
    )
    got = _sources(repo, "tests/test_subject.py")
    assert any("dep.py" in s for s in got), got
    assert not any("grand.py" in s for s in got), got


def test_dependencies_come_AFTER_the_module_under_test(repo: Path):
    """`build_signature_anchor` truncates in order, so when the budget runs out
    it must drop a collaborator rather than the subject itself."""
    got = _sources(repo, "tests/test_subject.py")
    assert got.index(next(s for s in got if "subject.py" in s)) < \
        got.index(next(s for s in got if "dep.py" in s))


def test_the_hop_respects_the_same_module_budget(repo: Path, monkeypatch):
    """Bounded by the cap the caller slices with, so a change there cannot
    leave this walking modules that will never be rendered."""
    monkeypatch.setenv(A._ENV_MAX_MODULES, "1")
    got = _sources(repo, "tests/test_subject.py")
    assert len(got) == 1
    assert "subject.py" in got[0]


def test_a_test_file_is_never_pulled_in_as_a_dependency(repo: Path):
    (repo / "pkg" / "subject.py").write_text(
        "from test_helper import thing\ndef run():\n    return thing\n"
    )
    (repo / "pkg" / "test_helper.py").write_text("thing = 1\n")
    got = _sources(repo, "tests/test_subject.py")
    assert not any("test_helper" in s for s in got), got


def test_an_unparseable_or_missing_source_never_raises(repo: Path):
    (repo / "pkg" / "broken.py").write_text("def (((:\n")
    assert A._imported_module_names("def (((:") == []
    assert A._resolve_first_party("", repo / "pkg" / "subject.py", repo) is None
    assert A._resolve_first_party("x", Path("/nope/a.py"), repo) is None
    assert _sources(repo, "tests/test_broken.py") is not None
