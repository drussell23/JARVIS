"""What the INSTALLED library says, read from disk — never imported, never listed.

The exemplar injector has nothing to show for FastAPI subjects: this repository
holds no passing FastAPI test. So the 30B wrote ``assert "x" in response``
against a ``JSONResponse`` nine attempts running. How a response is read is a
fact about a library, and the fact is in ``site-packages``.

A synthetic installed package keeps these tests independent of whichever
FastAPI version happens to be in the venv; one test then checks the real thing.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import library_contract as lc


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A fake ``site-packages`` holding ``webfw`` (re-exports from ``corelib``)."""
    root = tmp_path / "site-packages"
    (root / "corelib").mkdir(parents=True)
    (root / "webfw").mkdir()
    (root / "corelib" / "__init__.py").write_text("")
    (root / "corelib" / "responses.py").write_text('''
class Response:
    """Base response.

    Long second paragraph that must not be shown."""
    def __init__(self, content=None, status_code: int = 200):
        self.status_code = status_code
        self.body = self.render(content)
        self._private = 1
    def render(self, content) -> bytes: ...
    def _hidden(self): ...

class JSONResponse(Response):
    def render(self, content) -> bytes: ...
''')
    (root / "corelib" / "testclient.py").write_text('''
class TestClient:
    def __init__(self, app, base_url: str = "http://testserver"):
        self.app = app
    def get(self, url: str, **kwargs): ...
''')
    (root / "webfw" / "__init__.py").write_text("from .routing import Router as Router\n")
    (root / "webfw" / "routing.py").write_text('''
from typing import Annotated
class Router:
    def __init__(self, prefix: Annotated[str, Doc("""pages
of prose""")] = ""):
        self.prefix = prefix
''')
    (root / "webfw" / "responses.py").write_text("from corelib.responses import JSONResponse as JSONResponse\n")
    (root / "webfw" / "testclient.py").write_text("from corelib.testclient import TestClient as TestClient\n")
    (root / "webfw" / "_internal_testing.py").write_text("class Nope: ...\n")
    (root / "webfw" / "utils.py").write_text("def helper(): ...\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(lc, "_site_roots", lambda: (root.resolve(),))
    for name in ("webfw", "corelib"):
        sys.modules.pop(name, None)
    return root


SUBJECT = "from webfw import Router\nfrom webfw.responses import JSONResponse\n\nasync def h():\n    return JSONResponse({})\n"


def test_nothing_third_party_is_imported_to_read_it(site):
    before = set(sys.modules)
    assert lc.contract_for(SUBJECT, ["webfw"], 10_000, estimate=len)
    assert not {m for m in set(sys.modules) - before if m.split(".")[0] in ("webfw", "corelib")}


def test_reexports_are_followed_across_packages(site):
    origin, node, _tree = lc.resolve_name("webfw.responses", "JSONResponse")
    assert origin == "corelib.responses" and isinstance(node, ast.ClassDef)
    assert lc.resolve_name("webfw", "Router")[0] == "webfw.routing"


def test_a_reexport_cycle_terminates(site):
    (site / "webfw" / "a.py").write_text("from webfw.b import X\n")
    (site / "webfw" / "b.py").write_text("from webfw.a import X\n")
    assert lc.resolve_name("webfw.a", "X") is None


def test_testing_entry_points_are_found_by_structure_not_by_name_list(site):
    assert lc.testing_modules("webfw") == ["webfw.testclient"], (
        "private modules and non-testing modules must not qualify"
    )


def test_the_contract_shows_what_no_signature_shows(site):
    text = lc.contract_for(SUBJECT, ["webfw"], 10_000, estimate=len)
    assert "class JSONResponse(Response):" in text and "# corelib.responses" in text
    assert "instance attributes set by __init__: status_code, body" in text
    assert "_private" not in text and "_hidden" not in text
    assert "inherited from Response" in text
    assert "class TestClient" in text, "the testing entry point rides along unasked"


def test_annotated_metadata_is_not_the_type(site):
    text = lc.contract_for(SUBJECT, ["webfw"], 10_000, estimate=len)
    assert "prefix: str" in text and "pages" not in text and "Doc(" not in text


def test_only_the_first_docstring_paragraph(site):
    text = lc.contract_for("from corelib.responses import Response\n", ["corelib"], 10_000, estimate=len)
    assert "Base response." in text and "second paragraph" not in text


def test_blocks_are_atomic_under_a_budget(site):
    whole = lc.contract_for(SUBJECT, ["webfw"], 10_000, estimate=len)
    tight = lc.contract_for(SUBJECT, ["webfw"], 200, estimate=len)
    assert len(tight) <= 200
    for block in filter(None, tight.split("\n\n")):
        assert block in whole, "a block was truncated rather than dropped"


@pytest.mark.parametrize("tops", [["os"], ["not_installed_anywhere"], [""], ["bad name"], []])
def test_stdlib_and_absent_packages_have_no_contract(site, tops):
    assert lc.contract_for("import os\n", tops, 10_000, estimate=len) == ""


def test_an_unparseable_subject_contributes_no_names_and_does_not_raise(site):
    """Nothing can be derived FROM the subject — but the caller asked about
    ``webfw``, so its testing entry point is still a true and useful answer.
    (In the real flow an unparseable subject has no traits, so no package is
    ever requested on its behalf.)"""
    text = lc.contract_for("def broken(:\n", ["webfw"], 10_000, estimate=len)
    assert "class TestClient" in text
    assert "JSONResponse" not in text and "class Router" not in text
    assert lc.imported_names("def broken(:\n", "webfw") == []


def test_signature_rendering_covers_every_parameter_kind():
    node = ast.parse("async def f(a, /, b: int = 1, *args, c, d: str = 'x', **kw) -> bool: ...").body[0]
    assert lc.signature(node) == "async def f(a, /, b: int = 1, *args, c, d: str = 'x', **kw) -> bool"


def test_the_real_installed_fastapi_says_a_response_has_a_body():
    """The fact the model lacked through nine attempts, from the actual venv."""
    if lc.third_party_root("fastapi") is None:
        pytest.skip("fastapi not installed")
    text = lc.contract_for(
        "from fastapi.responses import JSONResponse\n", ["fastapi"], 100_000, estimate=len,
    )
    assert "class JSONResponse" in text
    line = next(l for l in text.splitlines() if "instance attributes" in l and "body" in l)
    assert "body" in line
    assert "class TestClient" in text, "fastapi.testclient -> starlette.testclient was not followed"
