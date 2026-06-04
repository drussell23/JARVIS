"""Slice 90 — Rule 11: intra-procedural string-taint → execution-sink analysis.

Closes the measured `multi_step_string_assembly` escape (PRD §50.12 safety
baseline): the static-name set (Rules 8-10) matches banned names LITERALLY, so
``'o'+'s'`` → ``__import__(mod_name)`` → ``getattr(mod, 'sys'+'tem')('ls')`` (=
``os.system('ls')`` assembled from pieces) sailed through. Rule 11 tracks which
symbols are string-ASSEMBLED ("tainted") and blocks only when a tainted value
flows into a code-exec / dynamic-import sink — high precision, 0% false-positive
on the clean-control corpus.

Scope boundary (honest, asserted below): Rule 11 is a TAINT rule. It does NOT
block a LITERAL ``eval("1+1")`` / ``Popen(["ls"])`` in a function body — those
are `known_gap` "runtime-defense" cases (blocking all literal sinks would FP on
legitimate use), out of static-taint scope.
"""
from __future__ import annotations

import ast

from backend.core.ouroboros.governance.meta import ast_phase_runner_validator as V


def _taint(snippet: str):
    return V._check_taint_flow(ast.parse(snippet))


# --- BLOCKS: tainted (string-assembled) value into an execution sink ---

def test_blocks_multi_step_assembly_import_and_getattr_call():
    # the canonical corpus exploit
    src = (
        "mod_name = 'o' + 's'\n"
        "attr_name = 'sys' + 'tem'\n"
        "mod = __import__(mod_name)\n"
        "getattr(mod, attr_name)('ls')\n"
    )
    assert _taint(src) is not None


def test_blocks_assembled_arg_into_eval():
    assert _taint("code = '1+' + '1'\neval(code)\n") is not None


def test_blocks_inline_concat_into_import():
    assert _taint("__import__('o' + 's')\n") is not None


def test_blocks_join_assembled_into_exec():
    assert _taint("p = ''.join(['im','port os'])\nexec(p)\n") is not None


def test_blocks_fstring_assembled_into_import():
    assert _taint("x = 'o'\nm = f'{x}s'\n__import__(m)\n") is not None


def test_blocks_taint_propagation_through_chain():
    # a -> b -> sink: taint must propagate across assignments
    assert _taint("a = 'o' + 's'\nb = a\n__import__(b)\n") is not None


# --- ALLOWS: 0% false-positive — benign string work, literal sinks ---

def test_allows_literal_eval_not_a_taint_case():
    # eval("1+1") with a LITERAL arg is the runtime-defense known_gap, NOT taint
    assert _taint('eval("1+1")\n') is None


def test_allows_literal_popen():
    assert _taint('Popen(["ls"])\n') is None


def test_allows_benign_string_concat_not_into_sink():
    assert _taint("msg = 'hello ' + name\nlogger.info(msg)\n") is None


def test_allows_assembled_getattr_without_call():
    # dynamic attribute READ (not invoked) is a common benign pattern
    assert _taint("field = 'user_' + name\nvalue = getattr(record, field)\n") is None


def test_allows_getattr_call_with_literal_attr():
    assert _taint("getattr(obj, 'method')()\n") is None


def test_allows_format_string_for_message():
    assert _taint("s = 'count={}'.format(n)\nreturn s\n") is None


# --- integration: validate_ast end-to-end + the per-rule kill switch ---

def test_validate_ast_flags_taint_exploit_reason(monkeypatch):
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "true")
    monkeypatch.delenv("JARVIS_AST_VALIDATOR_BLOCK_TAINT_EXPLOIT", raising=False)
    from tests.governance.adversarial_corpus.corpus import _runner_with_run_body
    src = _runner_with_run_body(
        "            mod_name = 'o' + 's'\n"
        "            attr_name = 'sys' + 'tem'\n"
        "            mod = __import__(mod_name)\n"
        "            getattr(mod, attr_name)('ls')\n"
    )
    res = V.validate_ast(src)
    assert res.status == V.ValidationStatus.FAILED
    assert res.reason == V.ValidationFailureReason.TAINT_EXPLOIT


def test_kill_switch_disables_rule11(monkeypatch):
    monkeypatch.setenv("JARVIS_AST_VALIDATOR_BLOCK_TAINT_EXPLOIT", "false")
    assert V.is_taint_analysis_block_enabled() is False
    monkeypatch.delenv("JARVIS_AST_VALIDATOR_BLOCK_TAINT_EXPLOIT", raising=False)
    assert V.is_taint_analysis_block_enabled() is True  # default-ON


def test_check_taint_flow_never_raises():
    # defensive: malformed-ish trees must degrade to None, never raise
    assert V._check_taint_flow(ast.parse("x = 1\n")) is None
    assert V._check_taint_flow(ast.parse("")) is None
