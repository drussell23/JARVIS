# tests/governance/test_fleet_quality_battery.py
from __future__ import annotations
from backend.core.ouroboros.governance import fleet_quality_battery as b


def test_prompts_exist_and_are_strings():
    assert isinstance(b.CODEGEN_PROMPT, str) and "ONLY" in b.CODEGEN_PROMPT.upper()
    assert isinstance(b.CLASSIFY_PROMPT, str)
    assert b.EXPECTED_LABEL == "ENRICH"


def test_extract_code_block_fenced_and_bare():
    assert "def f" in b.extract_code_block("```python\ndef f():\n    return 1\n```")
    assert "def g" in b.extract_code_block("def g():\n    return 2")


def test_is_ast_valid_true_for_real_code():
    assert b.is_ast_valid("```python\ndef merge(x):\n    return sorted(x)\n```") is True


def test_is_ast_valid_false_for_syntax_error():
    assert b.is_ast_valid("```python\ndef merge(x):\n    return sorted(\n```") is False


def test_placeholder_ellipsis_body_detected():
    assert b.has_semantic_placeholder("```python\ndef f():\n    ...\n```") is True


def test_placeholder_notimplemented_detected():
    assert b.has_semantic_placeholder(
        "```python\ndef f():\n    raise NotImplementedError\n```") is True


def test_real_code_has_no_placeholder():
    assert b.has_semantic_placeholder(
        "```python\ndef f(x):\n    '''doc'''\n    return x + 1\n```") is False


def test_code_quality_pass_combines_both():
    assert b.code_quality_pass("```python\ndef f(x):\n    return x*2\n```") is True
    assert b.code_quality_pass("```python\ndef f():\n    ...\n```") is False
    assert b.code_quality_pass("not code at all !!!(") is False


def test_label_adherence_exact_prose_empty():
    assert b.label_adherence("ENRICH", "ENRICH") == 1.0
    assert b.label_adherence("  enrich \n", "ENRICH") == 1.0
    assert 0.0 < b.label_adherence("The label is ENRICH because...", "ENRICH") < 1.0
    assert b.label_adherence("", "ENRICH") == 0.0
    assert b.label_adherence("NO_OP", "ENRICH") == 0.0


def test_adversarial_string_is_parsed_not_executed(tmp_path):
    # A malicious payload as a STRING parses to a tree but must never run.
    canary = tmp_path / "canary.txt"
    payload = f"```python\nimport os\nos.system('touch {canary}')\n```"
    assert b.is_ast_valid(payload) is True       # syntactically valid
    assert canary.exists() is False              # but NEVER executed
