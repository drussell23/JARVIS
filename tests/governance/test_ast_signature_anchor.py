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
    out = A.extract_public_api(src, "model_physics", budget=9000)
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
    # derived semantics: helper inlining, return formula, None guards
    assert "# where: field(name) = _as_int(info.get(arch + '.' + name))" in out
    ret = [l for l in out.splitlines() if "# returns: ModelPhysics(" in l][0]
    assert "native_context=field('context_length')" in ret
    assert "kv_bytes_per_token=field('block_count') * field('attention.head_count_kv') * (field('attention.key_length') + field('attention.value_length')) * kv_cache_dtype_bytes()" in ret
    guards = [l for l in out.splitlines() if "# returns None if:" in l][0]
    assert "not isinstance(payload, dict)" in guards
    # guards are substituted like return values: the model sees the KEYS
    assert "min(field('context_length'), field('block_count'), field('attention.head_count_kv')" in guards
    assert "source=source" in ret            # branch-dependent rebind stays symbolic
    where = [l for l in out.splitlines() if "# where:" in l and "field(name)" in l][0]
    assert "source in {'metadata', 'metadata+derived_head_dim'}" in where
    assert "DTYPE_BYTES_ENV = 'JARVIS_KV_CACHE_DTYPE_BYTES'" in out
    assert "_TRUTHY = " not in out   # private constants are not listed (may still appear inside formulas)
    assert "    source: str = 'metadata'" in out
    ceil = [l for l in out.splitlines() if "# returns:" in l and "physics.native_context" in l][0]
    assert "if physics is None or physics.native_context <= 0: max(0, int(configured_ceiling))" in ceil
    assert "if max(0, int(configured_ceiling)) <= 0: physics.native_context" in ceil
    assert "min(max(0, int(configured_ceiling)), physics.native_context)" in ceil
    assert "; on Exception: configured_ceiling" in ceil
    assert "if on Exception" not in ceil
    shape = [l for l in out.splitlines() if "# input shape: payload = " in l][0]
    assert shape.startswith("    # input shape: payload = {'model_info': {'general.architecture': ..., "
                            "'<general.architecture>.context_length': ..., ")
    assert "'<general.architecture>.attention.head_count_kv': ..." in shape
    assert shape.rstrip().endswith("}}")      # every key lives INSIDE model_info
    assert "# returns: _as_int(" not in out  # nested helper's return is not a shape
    assert "LITERAL flat keys" in A.build_signature_anchor(
        ("tests/governance/test_model_physics.py",), "cover model_physics.py", root)


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


CONTRACT_MOD = textwrap.dedent('''
    class Out:
        def __init__(self, a, b): ...
    def build(payload):
        if not isinstance(payload, dict):
            return None
        info = payload.get("info")
        def field(name):
            return int(info.get("p." + name))
        a = field("alpha")
        b = a * 2
        b = b or a
        if b <= 0:
            return None
        return Out(a=a, b=b)
''')


def test_contract_lines_inline_helpers_formulas_and_none_guards():
    out = A.extract_public_api(CONTRACT_MOD, "m")
    assert "# where: field(name) = int(info.get('p.' + name))" in out
    ret = [l for l in out.splitlines() if "# returns: Out(" in l][0]
    assert "a=field('alpha')" in ret
    assert "b=field('alpha') * 2" in ret          # self-referential rebind skipped
    guards = [l for l in out.splitlines() if "# returns None if:" in l][0]
    assert "not isinstance(payload, dict)" in guards
    assert "field('alpha') * 2 <= 0" in guards   # guards are substituted too
    assert "# input shape: payload = {'info': {'p.alpha': ...}}" in out
    _ast.parse(out)


def test_contract_lines_absent_for_plain_defs():
    out = A.extract_public_api("def f(x):\n    return x\n", "m")
    assert out.strip().endswith("def f(x): ...")


def test_input_shape_absent_without_param_reads():
    out = A.extract_public_api("def f(x):\n    return x + 1\n", "m")
    assert "# input shape" not in out
    out = A.extract_public_api("import os\ndef g():\n    return os.environ.get('K')\n", "m")
    assert "# input shape" not in out


def test_return_paths_carry_guard_chains_and_else_negation():
    src = textwrap.dedent('''
        LIMIT = 10
        _PRIV = 1
        def pick(x, y):
            if x is None:
                return y
            if x > LIMIT:
                return LIMIT
            else:
                return x + y
    ''')
    out = A.extract_public_api(src, "m")
    assert "LIMIT = 10" in out and "_PRIV" not in out
    ret = [l for l in out.splitlines() if "# returns:" in l][0]
    assert "if x is None: y" in ret
    assert "if x > LIMIT: LIMIT" in ret
    assert "if not (x > LIMIT): x + y" in ret
    _ast.parse(out)


TEST_MOD = textwrap.dedent('''
    import pytest
    class _FakeDW:
        def __init__(self, content="Fix applied. Tests green.", boom=False):
            self.content = content
        async def complete_sync(self, prompt, *, system_prompt, caller_id, max_tokens=512, **kw):
            return self.content
    def _make():
        return _FakeDW()
    async def test_yields(): ...
''')


def test_test_files_expose_private_fixtures(tmp_path):
    hidden = A.extract_public_api(TEST_MOD, "m")
    assert "_FakeDW" not in hidden and "async def test_yields" in hidden
    shown = A.extract_public_api(TEST_MOD, "m", include_private=True)
    assert "class _FakeDW:" in shown
    assert "def __init__(self, content='Fix applied. Tests green.', boom=False)" in shown
    assert "async def complete_sync(self, prompt, *, system_prompt, caller_id, max_tokens=512, **kw)" in shown
    assert "def _make()" in shown
    # path policy: tests dir or test_*.py / *_test.py, env-driven dir names
    assert A.is_test_path("tests/governance/comms/x/test_speech_provider.py")
    assert A.is_test_path("pkg/foo_test.py") and A.is_test_path("tests/helpers/fixtures.py")
    assert not A.is_test_path("backend/core/ouroboros/governance/model_physics.py")
    # end-to-end: a test target on disk is rendered with its fixtures + guidance
    tdir = tmp_path / "tests"; tdir.mkdir()
    (tdir / "test_speech.py").write_text(TEST_MOD)
    out = A.build_signature_anchor(("tests/test_speech.py",), "add a test", tmp_path)
    assert "class _FakeDW:" in out and "REUSE them" in out
