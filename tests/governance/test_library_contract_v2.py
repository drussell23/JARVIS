"""Contract v2: what a test RECEIVES gets the full contract, what the subject
merely USES gets an index, the exemplar and the contract share one budget, and
a retry answers the error with the one type it names."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import library_contract as lc
from backend.core.ouroboros.governance.episodic_memory import EpisodicFailureMemory


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    (root / "webfw").mkdir(parents=True)
    (root / "webfw" / "__init__.py").write_text("from .routing import Router as Router\n")
    (root / "webfw" / "routing.py").write_text(
        "class Router:\n"
        "    def __init__(self, prefix: str = '', tags=None, deps=None, deprecated=False):\n"
        "        self.prefix = prefix\n"
        "    def include(self, other): ...\n"
        "    def add(self, path, fn): ...\n"
    )
    (root / "webfw" / "responses.py").write_text(
        "class Response:\n"
        "    def __init__(self, content=None, status_code: int = 200):\n"
        "        self.status_code = status_code\n"
        "        self.body = b''\n"
        "    def render(self, content) -> bytes: ...\n"
        "class JSONResponse(Response):\n"
        "    def render(self, content) -> bytes: ...\n"
    )
    (root / "webfw" / "testclient.py").write_text(
        "class TestClient:\n"
        "    def __init__(self, app, base_url: str = 'http://t'):\n"
        "        self.app = app\n"
        "    def get(self, url: str, params=None, headers=None): ...\n"
        "    def post(self, url: str, json=None): ...\n"
    )
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(lc, "_site_roots", lambda: (root.resolve(),))
    for name in list(sys.modules):
        if name.split(".")[0] == "webfw":
            del sys.modules[name]
    return root


SUBJECT = (
    "from webfw import Router\n"
    "from webfw.responses import JSONResponse\n\n"
    "router = Router(prefix='/x')\n\n"
    "async def handler():\n"
    "    return JSONResponse({'ok': True})\n"
)


def test_produced_types_are_what_the_subject_returns_or_raises():
    src = "from a import X, Y, Z\n\ndef f():\n    if 1:\n        raise Y('bad')\n    return X(1)\n\nasync def g():\n    return await Z()\n"
    assert set(lc.produced_types(src)) == {"X", "Y", "Z"}


def test_produced_types_get_the_full_contract_and_used_types_an_index(site):
    text = lc.contract_for(SUBJECT, ["webfw"], 10_000, estimate=len)
    blocks = text.split("\n\n")
    assert blocks[0].startswith("class JSONResponse"), "what the test receives comes first"
    assert "instance attributes set by __init__: status_code, body" in blocks[0]
    assert "def render(self, content) -> bytes: ..." in blocks[0]
    router = next(b for b in blocks if b.startswith("class Router"))
    assert "# methods: include(), add()" in router
    assert "def include(self, other)" not in router, "a merely-used class is an index"
    client = next(b for b in blocks if b.startswith("class TestClient"))
    assert "# methods: get(), post()" in client and "params=None" not in client


def test_the_index_is_much_smaller_than_the_full_render(site):
    tree = lc._parse(site / "webfw" / "testclient.py")
    node = next(n for n in tree.body if n.__class__.__name__ == "ClassDef")
    assert len(lc.render_index(node, "webfw.testclient")) < len(lc.render(node, tree, "webfw.testclient"))


# ---------------------------------------------------------------------------
# The error names the type; the retry gets that type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message, expected", [
    ("TypeError: argument of type 'JSONResponse' is not iterable", ["JSONResponse"]),
    ("AttributeError: 'JSONResponse' object has no attribute 'json'", ["JSONResponse"]),
    ("TypeError: Router() got an unexpected keyword argument 'x'", ["Router"]),
    ("AssertionError: assert 1 == 2", []),
    ("", []),
])
def test_type_names_are_read_from_any_error_shape(message, expected):
    assert lc.type_names_in_error(message) == expected


def test_a_type_is_located_through_what_the_sources_import(site):
    block = lc.contract_for_type("JSONResponse", [SUBJECT])
    assert block.startswith("class JSONResponse") and "body" in block
    assert lc.contract_for_type("JSONResponse", ["import os\n"]) == ""
    assert lc.contract_for_type("NotAType", [SUBJECT]) == ""


def test_the_retry_prompt_carries_the_named_types_contract(site, tmp_path):
    subject = tmp_path / "backend" / "svc.py"
    subject.parent.mkdir()
    subject.write_text(SUBJECT)
    ctx = SimpleNamespace(op_id="op-1", target_files=("tests/test_svc.py",),
                          description="`backend/svc.py` has no test. CREATE `tests/test_svc.py`")
    memory = EpisodicFailureMemory.for_op(ctx, tmp_path)
    memory.record(
        file_path="tests/test_svc.py", attempt=1, failure_class="test",
        error_summary="1 critique(s): 1 error(s)",
        specific_errors=["test_x · TypeError: argument of type 'JSONResponse' is not iterable"],
        candidate_source="from webfw.responses import JSONResponse\n",
    )
    memory.record(
        file_path="tests/test_svc.py", attempt=2, failure_class="test",
        error_summary="again", specific_errors=["'JSONResponse' object has no attribute 'json'"],
    )
    text = memory.format_for_prompt()
    assert "## API contract for the type(s) these errors name" in text
    assert text.count("class JSONResponse") == 1, "one contract per distinct type"
    assert "body" in text
    assert "Do not repeat these mistakes" in text


def test_a_memory_without_a_resolver_formats_exactly_as_before():
    memory = EpisodicFailureMemory("op-2")
    memory.record(file_path="t.py", attempt=1, failure_class="test",
                  error_summary="s", specific_errors=["'JSONResponse' is not iterable"])
    text = memory.format_for_prompt()
    assert "API contract" not in text and "Do not repeat these mistakes" in text


def test_an_error_naming_no_library_type_adds_nothing(site, tmp_path):
    ctx = SimpleNamespace(op_id="op-3", target_files=(), description="")
    memory = EpisodicFailureMemory.for_op(ctx, tmp_path)
    memory.record(file_path="t.py", attempt=1, failure_class="test",
                  error_summary="AssertionError: assert False", specific_errors=[])
    assert "API contract" not in memory.format_for_prompt()


def test_a_broken_resolver_never_breaks_the_retry(site, tmp_path):
    ctx = SimpleNamespace(op_id="op-4", target_files=(), description="")
    memory = EpisodicFailureMemory.for_op(ctx, tmp_path)

    def boom(*_a):
        raise RuntimeError("no")

    memory._contract_resolver = boom
    memory.record(file_path="t.py", attempt=1, failure_class="test",
                  error_summary="'JSONResponse' object has no attribute 'x'", specific_errors=[])
    assert "Do not repeat these mistakes" in memory.format_for_prompt()
