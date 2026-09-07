"""Spine for the AST-Signature Anchor — the structural cure for the local
model's API hallucination (it wrote parse_model_physics("model_a", 100, 200)
against a real parse_model_physics(payload) -> Optional[ModelPhysics])."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import ast_signature_anchor as A


MOD = textwrap.dedent('''
    from typing import Any, Optional
    _PRIVATE = 1
    def public_fn(payload: Any, *, flag: bool = False) -> "Optional[int]":
        return None
    async def afetch(url: str) -> bytes:
        return b""
    def _private_fn(x):
        return x
    class Thing:
        def __init__(self, x: int) -> None:
            self.x = x
        def method(self, a, b=2) -> int:
            return a + b
        def _hidden(self):
            return None
    class _PrivateClass:
        pass
''')


def test_extract_public_api_real_signatures():
    out = A.extract_public_api(MOD, "pkg.mod")
    assert "def public_fn(payload: Any, *, flag: bool=False) -> 'Optional[int]': ..." in out
    assert "async def afetch(url: str) -> bytes: ..." in out
    assert "class Thing:" in out
    assert "def __init__(self, x: int) -> None: ..." in out
    assert "def method(self, a, b=2) -> int: ..." in out
    # privates excluded
    assert "_private_fn" not in out
    assert "_hidden" not in out
    assert "_PrivateClass" not in out


@pytest.mark.parametrize("bad", ["def broken(:\n", "", "   ", "not python at ("])
def test_extract_public_api_failsoft(bad):
    assert A.extract_public_api(bad) == ""


def test_extract_no_hints_degrades_to_bare_names():
    out = A.extract_public_api("def f(a, b, *args, **kw):\n    return a\n")
    assert "def f(a, b, *args, **kw): ..." in out


def test_collect_and_build_from_description(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    src = tmp_path / "pkg" / "widget.py"
    src.write_text("def build(spec: dict) -> str:\n    return ''\n")
    desc = "author a test at tests/test_widget.py for pkg/widget.py"
    block = A.build_signature_anchor(["tests/test_widget.py"], desc, tmp_path)
    assert "AUTHORITATIVE API SIGNATURES" in block
    assert "def build(spec: dict) -> str: ..." in block


def test_resolves_module_under_test_from_name(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text("def go() -> int:\n    return 1\n")
    # no description path — must resolve widget.py from test_widget.py alone
    block = A.build_signature_anchor(["tests/test_widget.py"], "author a test", tmp_path)
    assert "def go() -> int: ..." in block


def test_disabled_flag_yields_empty(tmp_path, monkeypatch):
    (tmp_path / "w.py").write_text("def f() -> int:\n    return 1\n")
    monkeypatch.setenv("JARVIS_AST_SIGNATURE_ANCHOR_ENABLED", "false")
    assert A.build_signature_anchor(["tests/test_w.py"], "w.py", tmp_path) == ""


def test_nothing_resolves_yields_empty(tmp_path):
    assert A.build_signature_anchor(["tests/test_nonexistent_xyz.py"], "no path here", tmp_path) == ""


def test_never_raises_on_garbage(tmp_path):
    assert A.build_signature_anchor(None, None, tmp_path) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Semantic contract (2026-09-07): signatures alone left the model building
# flat payloads against parse_model_physics(payload: Any) -> every test failed
# on ``assert None is not None``. The docstring IS the contract; the anchor
# now carries a bounded excerpt per public symbol, public annotated class
# fields, and the module docstring.
# ---------------------------------------------------------------------------
import ast as _ast

DOC_MOD = textwrap.dedent('''
    """Module contract: parses Ollama /api/show payloads.

    Second paragraph detail."""
    from dataclasses import dataclass
    from typing import Any, Optional

    @dataclass(frozen=True)
    class Physics:
        """Per-model physics."""
        native_context: int
        kv_heads: int
        _hidden: int = 0
        def to_dict(self) -> dict:
            return {}

    def parse(payload: Any) -> "Optional[Physics]":
        """Build Physics from an Ollama ``/api/show`` payload.

        Keys are architecture-prefixed under ``model_info``. Returns ``None``
        when any load-bearing field is missing. NEVER raises."""
        return None

    def bare(x):
        return x
''')


def test_docstring_contract_is_embedded_under_signature():
    out = A.extract_public_api(DOC_MOD, "pkg.phys")
    assert "def parse(payload: Any) -> 'Optional[Physics]':" in out
    assert "architecture-prefixed under ``model_info``" in out
    assert "Returns ``None`` when any load-bearing field is missing" in out
    assert "def bare(x): ..." in out  # no docstring keeps the one-liner


def test_class_fields_and_class_doc_are_embedded():
    out = A.extract_public_api(DOC_MOD, "pkg.phys")
    assert "    native_context: int" in out and "    kv_heads: int" in out
    assert "_hidden" not in out
    assert '"""Per-model physics."""' in out


def test_module_docstring_and_block_is_valid_python():
    out = A.extract_public_api(DOC_MOD, "pkg.phys")
    assert "Module contract: parses Ollama /api/show payloads." in out
    _ast.parse(out)  # the anchor block must remain syntactically valid Python


def test_doc_excerpt_cuts_at_sentence_boundary():
    first = "First sentence is reasonably long enough to pass the floor rule here."
    src = f'def f(a):\n    """{first} ' + "x" * 600 + ' tail."""\n    return a\n'
    out = A.extract_public_api(src, "m", doc_chars=120)
    assert first + " …" in out
    assert "x" * 100 not in out


def test_doc_chars_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS", "40")
    out = A.extract_public_api(DOC_MOD, "pkg.phys")
    assert "load-bearing" not in out  # truncated before the second sentence
    assert "…" in out


def test_triple_quotes_in_docstring_are_neutralised():
    src = "def f():\n    '''Has \"\"\" inside.'''\n    return 1\n"
    out = A.extract_public_api(src, "m")
    assert "inside." in out
    _ast.parse(out)


def test_real_model_physics_contract_reaches_the_anchor():
    """The live case: the excerpt must carry the payload SHAPE the model
    needs — model_info + architecture-prefixed keys + None-on-missing."""
    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/core/ouroboros/governance/model_physics.py").read_text()
    out = A.extract_public_api(src, "model_physics", budget=6000)
    assert "def parse_model_physics(payload: Any)" in out
    assert "/api/show" in out and "architecture-prefixed" in out
    assert "Returns ``None``" in out
    assert "    kv_bytes_per_token: int" in out
    # the access pattern: nesting + exact key vocabulary
    reads = [l for l in out.splitlines() if "# reads:" in l and "model_info" in l][0]
    for frag in (
        "payload.get('model_info')", "info.get('general.architecture')",
        "info.get(arch + '.' + name)", "field('context_length')",
        "field('attention.head_count_kv')", "field('attention.key_length')",
    ):
        assert frag in reads, frag


def test_adaptive_budget_raises_cap_for_small_modules_and_floors_for_large():
    small = _ast.parse(DOC_MOD)
    assert A._adaptive_doc_chars(small, budget=6000) > A._DEFAULT_DOC_CHARS
    assert A._adaptive_doc_chars(small, budget=6000) <= A._DEFAULT_DOC_CHARS_MAX
    many = _ast.parse("\n".join(f"def f{i}(x):\n    return x" for i in range(200)))
    assert A._adaptive_doc_chars(many, budget=6000) == A._DEFAULT_DOC_CHARS
    assert A._adaptive_doc_chars(small, budget=None) == A._DEFAULT_DOC_CHARS


def test_build_anchor_stays_within_max_chars(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_AST_SIGNATURE_ANCHOR_MAX_CHARS", "1500")
    mod = tmp_path / "big.py"
    mod.write_text("\n".join(
        f'def f{i}(x):\n    """{"sentence. " * 80}"""\n    return x' for i in range(30)
    ))
    out = A.build_signature_anchor(("tests/test_big.py",), "cover big.py", tmp_path)
    body = out.split("```python", 1)[1].rsplit("```", 1)[0]
    assert 0 < len(body) <= 1500 + 2


def test_waterfill_gives_unused_short_doc_share_to_long_docs():
    # three short docs + one long: budget 1000 must let the long one reach
    # 1000 - 3*50 = 850, not 1000 // 4 = 250.
    assert A._waterfill_level([50, 50, 50, 5000], 1000, 1200) == 850
    assert A._waterfill_level([50, 50], 1000, 1200) == 1200      # all fit
    assert A._waterfill_level([600, 600, 600], 900, 1200) == 300  # even split
    assert A._waterfill_level([], 0, 1200) == 1200


ACCESS_MOD = textwrap.dedent('''
    def parse(payload):
        """Doc."""
        info = payload.get("model_info")
        arch = info.get("general.architecture")
        def field(name):
            return info.get(arch + "." + name)
        n = field("block_count")
        if "extra" in payload:
            return payload["extra"]
        return n
    def plain(x):
        return x + 1
''')


def test_access_pattern_lines_expose_key_vocabulary_and_nesting():
    out = A.extract_public_api(ACCESS_MOD, "m")
    line = [l for l in out.splitlines() if "# reads:" in l][0]
    for frag in (
        "payload.get('model_info')", "info.get('general.architecture')",
        "info.get(arch + '.' + name)", "field('block_count')",
        "'extra' in payload", "payload['extra']",
    ):
        assert frag in line, frag
    assert line.index("payload.get('model_info')") < line.index("field('block_count')")
    assert "def plain(x): ..." in out
    _ast.parse(out)


def test_access_items_env_bound(monkeypatch):
    monkeypatch.setenv("JARVIS_AST_SIGNATURE_ANCHOR_ACCESS_ITEMS", "2")
    out = A.extract_public_api(ACCESS_MOD, "m")
    line = [l for l in out.splitlines() if "# reads:" in l][0]
    assert line.count("; ") == 1
