"""Tests for §24.8.1 — dynamic_import_chain pattern (20 Quine variants).

Per OUROBOROS_VENOM_PRD.md §24.8.1:

  > Plus a dedicated test suite with 20 hand-crafted Quine variants.

Each test provides an ``old_content`` (benign) and a ``new_content``
(containing the attack variant). The pattern must:

  * DETECT the variant (severity ``hard``)
  * NOT fire on pre-existing usage (old == new)
  * NOT fire on normal ``import os`` (false positive guard)
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.semantic_guardian import (
    SemanticGuardian,
    Detection,
)


@pytest.fixture
def guardian():
    return SemanticGuardian()


# Benign old content shared across most variants.
_BENIGN_OLD = '''\
import os
import json

def process(data):
    return json.loads(data)
'''


def _detect(guardian, new_content, old_content=_BENIGN_OLD):
    findings = guardian.inspect(
        file_path="target.py",
        old_content=old_content,
        new_content=new_content,
    )
    chain_hits = [f for f in findings if f.pattern == "dynamic_import_chain"]
    return chain_hits


# ---------------------------------------------------------------------------
# 20 Quine variants — each must be CAUGHT
# ---------------------------------------------------------------------------


class TestQuineVariants:
    """20 hand-crafted attack patterns covering every known dynamic
    execution obfuscation class."""

    # 1. Direct __import__
    def test_variant_01_direct_dunder_import(self, guardian):
        new = _BENIGN_OLD + '\nm = __import__("subprocess")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1
        assert hits[0].severity == "hard"

    # 2. Chained __import__ with method call
    def test_variant_02_chained_dunder_import(self, guardian):
        new = _BENIGN_OLD + '\n__import__("subprocess").run(["ls"])\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 3. eval wrapping import
    def test_variant_03_eval_wrapping_import(self, guardian):
        new = _BENIGN_OLD + '\neval("__import__(\'os\')")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 4. exec with embedded import
    def test_variant_04_exec_with_import(self, guardian):
        new = _BENIGN_OLD + '\nexec("import os\\nos.system(\'id\')")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 5. compile + exec chain
    def test_variant_05_compile_exec_chain(self, guardian):
        new = _BENIGN_OLD + '''
code = compile("import os\\nos.system('id')", "<string>", "exec")
exec(code)
'''
        hits = _detect(guardian, new)
        # compile + exec = 2 nodes
        assert len(hits) >= 1

    # 6. getattr on __import__ result
    def test_variant_06_getattr_dynamic_dispatch(self, guardian):
        new = _BENIGN_OLD + '\ngetattr(__import__("os"), "system")("id")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 7. base64-encoded payload + exec
    def test_variant_07_base64_decode_exec(self, guardian):
        new = _BENIGN_OLD + '''
import base64
exec(base64.b64decode("cHJpbnQoJ293bmVkJyk=").decode())
'''
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 8. codecs-encoded payload + exec
    def test_variant_08_codecs_decode_exec(self, guardian):
        new = _BENIGN_OLD + '''
import codecs
exec(codecs.decode("cHJpbnQoJ293bmVkJyk=", "base64").decode())
'''
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 9. importlib.import_module
    def test_variant_09_importlib_import_module(self, guardian):
        new = _BENIGN_OLD + '''
import importlib
m = importlib.import_module("subprocess")
m.run(["id"])
'''
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 10. builtins.__import__
    def test_variant_10_builtins_dunder_import(self, guardian):
        new = _BENIGN_OLD + '''
import builtins
m = builtins.__import__("os")
m.system("id")
'''
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 11. globals chain (eval wrapping)
    def test_variant_11_globals_chain(self, guardian):
        new = _BENIGN_OLD + '\neval("globals()")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 12. chr-code obfuscation (eval wrapping)
    def test_variant_12_chr_code_obfuscation(self, guardian):
        new = _BENIGN_OLD + '\neval(chr(105)+chr(109)+"port os")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 13. bytes decode + exec
    def test_variant_13_bytes_decode_exec(self, guardian):
        new = _BENIGN_OLD + '\nexec(bytes([105,109,112,111,114,116]).decode())\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 14. nested getattr chain
    def test_variant_14_nested_getattr(self, guardian):
        new = _BENIGN_OLD + '\ngetattr(getattr(__import__("os"), "path"), "exists")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 15. metaclass injection via exec
    def test_variant_15_metaclass_exec(self, guardian):
        new = _BENIGN_OLD + '\nexec("class Evil: pass")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 16. lambda wrapping __import__
    def test_variant_16_lambda_import(self, guardian):
        new = _BENIGN_OLD + '\nf = lambda: __import__("os")\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 17. list comprehension wrapping
    def test_variant_17_list_comprehension(self, guardian):
        new = _BENIGN_OLD + '\nx = [__import__("os")]\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 18. dict comprehension wrapping
    def test_variant_18_dict_comprehension(self, guardian):
        new = _BENIGN_OLD + '\nx = {k: __import__(k) for k in ["os"]}\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 19. ternary wrapping
    def test_variant_19_ternary_import(self, guardian):
        new = _BENIGN_OLD + '\nx = __import__("os") if True else None\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1

    # 20. exec + open().read() chain
    def test_variant_20_exec_open_read(self, guardian):
        new = _BENIGN_OLD + '\nexec(open("/tmp/payload.py").read())\n'
        hits = _detect(guardian, new)
        assert len(hits) >= 1


# ---------------------------------------------------------------------------
# False positive guards — MUST NOT fire
# ---------------------------------------------------------------------------


class TestFalsePositiveGuards:
    """Normal Python patterns that MUST NOT trigger the detector."""

    def test_normal_import_statement(self, guardian):
        new = _BENIGN_OLD + '\nimport subprocess\n'
        hits = _detect(guardian, new)
        assert len(hits) == 0

    def test_from_import(self, guardian):
        new = _BENIGN_OLD + '\nfrom os.path import join\n'
        hits = _detect(guardian, new)
        assert len(hits) == 0

    def test_normal_function_call(self, guardian):
        new = _BENIGN_OLD + '\nresult = json.dumps({"key": "value"})\n'
        hits = _detect(guardian, new)
        assert len(hits) == 0

    def test_getattr_with_safe_target(self, guardian):
        new = _BENIGN_OLD + '\nx = getattr(obj, "name")\n'
        hits = _detect(guardian, new)
        assert len(hits) == 0

    def test_open_for_reading_no_exec(self, guardian):
        new = _BENIGN_OLD + '\nwith open("data.txt") as f:\n    data = f.read()\n'
        hits = _detect(guardian, new)
        assert len(hits) == 0

    def test_compile_regex(self, guardian):
        """re.compile() must NOT trigger — only builtin compile()."""
        new = _BENIGN_OLD + '\nimport re\npat = re.compile(r"\\d+")\n'
        hits = _detect(guardian, new)
        assert len(hits) == 0

    def test_empty_old_and_new(self, guardian):
        hits = _detect(guardian, "", "")
        assert len(hits) == 0


# ---------------------------------------------------------------------------
# Delta logic — pre-existing chains must NOT trigger
# ---------------------------------------------------------------------------


class TestDeltaLogic:
    """The pattern fires on introductions, not pre-existing usage."""

    def test_preexisting_eval_not_flagged(self, guardian):
        code = _BENIGN_OLD + '\nresult = eval("1 + 1")\n'
        # old and new are identical — no delta
        hits = _detect(guardian, code, old_content=code)
        assert len(hits) == 0

    def test_preexisting_import_not_flagged(self, guardian):
        code = _BENIGN_OLD + '\nm = __import__("json")\n'
        hits = _detect(guardian, code, old_content=code)
        assert len(hits) == 0

    def test_removing_eval_not_flagged(self, guardian):
        old = _BENIGN_OLD + '\nresult = eval("1 + 1")\n'
        # new removes the eval — count goes DOWN, no flag
        hits = _detect(guardian, _BENIGN_OLD, old_content=old)
        assert len(hits) == 0

    def test_additional_chain_flagged(self, guardian):
        old = _BENIGN_OLD + '\nx = eval("1")\n'
        new = old + '\ny = exec("print(1)")\n'
        hits = _detect(guardian, new, old_content=old)
        assert len(hits) >= 1


# ---------------------------------------------------------------------------
# Pattern registration check
# ---------------------------------------------------------------------------


class TestPatternRegistration:
    def test_pattern_in_all_patterns(self):
        from backend.core.ouroboros.governance.semantic_guardian import all_pattern_names
        assert "dynamic_import_chain" in all_pattern_names()

    def test_env_gate_respected(self, guardian, monkeypatch):
        monkeypatch.setenv("JARVIS_SEMGUARD_DYNAMIC_IMPORT_CHAIN_ENABLED", "0")
        new = _BENIGN_OLD + '\neval("bad")\n'
        # The guardian should skip the disabled pattern.
        findings = guardian.inspect(
            file_path="test.py", old_content=_BENIGN_OLD, new_content=new,
        )
        chain_hits = [f for f in findings if f.pattern == "dynamic_import_chain"]
        assert len(chain_hits) == 0

    def test_severity_is_hard(self, guardian):
        new = _BENIGN_OLD + '\nexec("bad")\n'
        hits = _detect(guardian, new)
        assert all(h.severity == "hard" for h in hits)


# ---------------------------------------------------------------------------
# Multi-chain accumulation
# ---------------------------------------------------------------------------


class TestMultiChain:
    def test_multiple_chains_counted(self, guardian):
        new = _BENIGN_OLD + '''
x = __import__("os")
y = eval("1+1")
z = exec("pass")
w = compile("1", "", "eval")
'''
        hits = _detect(guardian, new)
        assert len(hits) >= 1
        # The delta should report 4 new nodes.
        assert "4" in hits[0].snippet or "delta=4" in hits[0].snippet
