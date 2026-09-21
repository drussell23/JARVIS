"""A subject that imports a first-party module which does not exist.

Soak bt-2026-09-20-183259: the Sentinel chose
``backend/jarvis_integrated_assistant.py``, which opens with
``from vision.proactive_vision_assistant import ...``. ``vision`` is a real
package; that module is not in it. The import verdict stopped at the TOP of the
dotted path -- "vision resolves, so it is first-party, so it is fine" -- and the
30B then wrote three different test files over three rounds, all nine attempts
dying on the same ``ModuleNotFoundError``. Nine of the 51 signed roadmap goals
had subjects like that.

The rule accuses only what it can prove from the filesystem. Everything it
cannot be sure of -- optional imports, dynamic packages, compiled extensions,
attribute access past a module -- passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import environment_integrity as ei


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = . backend\n")
    pkg = tmp_path / "backend" / "vision"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "real_engine.py").write_text("def run():\n    return 1\n")
    return tmp_path


def _subject(repo: Path, body: str, rel: str = "backend/assistant.py") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_the_soak_case(repo):
    path = _subject(repo, "from vision.proactive_vision_assistant import X\n\ndef f():\n    return X\n")
    assert ei.missing_first_party_imports(path, repo) == ("vision.proactive_vision_assistant",)


def test_a_real_submodule_is_not_accused(repo):
    path = _subject(repo, "from vision.real_engine import run\nimport vision.real_engine\n")
    assert ei.missing_first_party_imports(path, repo) == ()


@pytest.mark.parametrize("body", [
    # the optional-dependency idiom
    "try:\n    from vision.missing import X\nexcept ImportError:\n    X = None\n",
    # never executed at import
    "def f():\n    from vision.missing import X\n    return X\n",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from vision.missing import X\n",
    "if __name__ == '__main__':\n    from vision.missing import X\n",
    "import os\nif os.environ.get('X'):\n    from vision.missing import X\n",
    # attribute access past a real module: not the filesystem's to judge
    "import vision.real_engine.run\n",
    # third-party and stdlib belong to the other check
    "import numpy.linalg\nfrom os import path\n",
    # a bare top-level name belongs to the other check too
    "import vision\n",
    "def broken(:\n",
])
def test_nothing_it_cannot_prove_is_accused(repo, body):
    assert ei.missing_first_party_imports(_subject(repo, body), repo) == ()


def test_a_dynamic_package_shields_everything_beneath_it(repo):
    (repo / "backend" / "vision" / "__init__.py").write_text(
        "def __getattr__(name):\n    return object()\n"
    )
    path = _subject(repo, "from vision.conjured_at_runtime import X\n")
    assert ei.missing_first_party_imports(path, repo) == ()


def test_a_compiled_extension_counts_as_present(repo):
    (repo / "backend" / "vision" / "fast.cpython-311-x86_64-linux-gnu.so").write_bytes(b"")
    path = _subject(repo, "from vision.fast import kernel\n")
    assert ei.missing_first_party_imports(path, repo) == ()


def test_a_namespace_directory_counts_as_present(repo):
    (repo / "backend" / "vision" / "plugins").mkdir()
    (repo / "backend" / "vision" / "plugins" / "one.py").write_text("X = 1\n")
    path = _subject(repo, "from vision.plugins.one import X\n")
    assert ei.missing_first_party_imports(path, repo) == ()


def test_relative_imports_resolve_against_the_importer(repo):
    (repo / "backend" / "ctx" / "core").mkdir(parents=True)
    for init in ("backend/ctx/__init__.py", "backend/ctx/core/__init__.py"):
        (repo / init).write_text("")
    (repo / "backend" / "ctx" / "core" / "present.py").write_text("X = 1\n")
    path = _subject(
        repo,
        "from ..core.present import X\nfrom ..core.context_manager import Y\n",
        rel="backend/ctx/integrations/wrapper.py",
    )
    assert ei.missing_first_party_imports(path, repo) == ("..core.context_manager",)


def test_the_verdict_quarantines_and_says_which_import(repo):
    _subject(repo, "from vision.proactive_vision_assistant import X\n\ndef f():\n    return X\n")
    verdict = ei.target_import_verdict(
        ["tests/test_assistant.py"],
        "`backend/assistant.py` has no test module. CREATE `tests/test_assistant.py`",
        repo,
    )
    assert verdict.importable is False
    assert verdict.impossible is True, "demotion would re-rank it forever"
    assert verdict.reason.startswith(ei.FIRST_PARTY_IMPORT_MISSING)
    assert "vision.proactive_vision_assistant" in verdict.reason


def test_repairing_the_import_lifts_the_verdict(repo):
    """Quarantine is a fact about the tree, re-read every pass — not a label."""
    subject = _subject(repo, "from vision.soon import X\n\ndef f():\n    return X\n")
    args = (["backend/assistant.py"], "fix `backend/assistant.py`", repo)
    assert ei.target_import_verdict(*args).impossible is True
    before = subject.stat().st_mtime_ns
    (repo / "backend" / "vision" / "soon.py").write_text("X = 1\n")
    # The SUBJECT is deliberately untouched: that is how a broken import really
    # gets repaired, and it is the case a verdict cached on the importer would
    # never notice.
    assert subject.stat().st_mtime_ns == before
    assert ei.target_import_verdict(*args).importable is True


def test_an_unreadable_subject_accuses_nobody(repo):
    assert ei.missing_first_party_imports(repo / "backend" / "absent.py", repo) == ()
