"""Regression spine for Phase 1.5.D — empirical hardness validator.

Phase 1.5.D ships ``scripts/validate_l2_exercise_corpus_hardness.py``
— an operator-paced bounded-cost CLI that measures the production
DW Tier 0 provider's first-try fail rate against the L2 exercise
corpus.

This spine pins the load-bearing structural invariants of that
script:

Composition pins (single-source-of-truth)
-----------------------------------------

* Imports ``list_corpus_problems`` from the canonical
  Phase 1.5.A substrate (no parallel directory walker)
* Imports ``load_exercise_problem`` from the canonical
  Phase 1.5.A substrate (no parallel manifest parser)
* Composes the same DW env vars ``DoublewordProvider.__init__``
  reads (no parallel config schema)
* Uses the same OpenAI-compat ``/chat/completions`` endpoint suffix
  the DW provider's realtime path uses

Refusal pins (operator-paced contract)
--------------------------------------

* Refuses to run without ``--confirm-paid`` (exit code 2)
* Refuses to run without ``DOUBLEWORD_API_KEY`` (exit code 2)
* ``--help`` works WITHOUT either of the above (lazy-import
  discipline; ``httpx`` is only imported inside
  ``call_doubleword_one_shot``)

Schema pins (report stability)
------------------------------

* ``REPORT_SCHEMA_VERSION == "hardness_report.v1"``
* ``REPORT_FILENAME == "_hardness_report.json"`` (underscore prefix
  so canonical walker skips the report)
* ``PROVIDER_DW == "doubleword"``
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPO_ROOT / "scripts" / "validate_l2_exercise_corpus_hardness.py"
)


_LOADED_MODULE = None


def _load_script_module():
    """Load the script as an importable module (it's a CLI under
    ``scripts/`` and not in any package).  Spec-based import so
    importing it does NOT invoke ``main()``.

    The module MUST be registered in ``sys.modules`` before
    ``exec_module`` so the ``@dataclass`` decorator's
    ``sys.modules[cls.__module__]`` lookup succeeds (otherwise the
    dataclass machinery crashes during class construction)."""
    global _LOADED_MODULE
    if _LOADED_MODULE is not None:
        return _LOADED_MODULE
    module_name = "validate_l2_exercise_corpus_hardness"
    spec = importlib.util.spec_from_file_location(
        module_name, str(SCRIPT_PATH),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    _LOADED_MODULE = mod
    return mod


_SCRIPT_SRC = SCRIPT_PATH.read_text(encoding="utf-8")
_SCRIPT_AST = ast.parse(_SCRIPT_SRC)


# ===========================================================================
# Composition pins — canonical Phase 1.5.A substrate
# ===========================================================================


def _all_imports():
    """Every ImportFrom node anywhere in the script (top-level OR
    lazy imports inside function bodies)."""
    out = []
    for node in ast.walk(_SCRIPT_AST):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def _top_level_imports():
    """Only ImportFrom + Import nodes at module top level."""
    out = []
    for node in _SCRIPT_AST.body:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def test_script_imports_canonical_list_corpus_problems():
    """Composition pin: the script's corpus walker MUST be
    ``list_corpus_problems`` from the Phase 1.5.A substrate, not a
    parallel ``os.listdir`` / ``glob`` walker."""
    matches = [
        (m, n) for (m, n) in _all_imports()
        if m.endswith(".l2_exercise_seed")
        and "list_corpus_problems" in n
    ]
    assert matches, (
        "Phase 1.5.D script MUST import list_corpus_problems from "
        "l2_exercise_seed — composition pin"
    )


def test_script_imports_canonical_load_exercise_problem():
    """Composition pin: the script's manifest parser MUST be
    ``load_exercise_problem`` from the Phase 1.5.A substrate, not a
    parallel ``json.loads`` ladder."""
    matches = [
        (m, n) for (m, n) in _all_imports()
        if m.endswith(".l2_exercise_seed")
        and "load_exercise_problem" in n
    ]
    assert matches, (
        "Phase 1.5.D script MUST import load_exercise_problem from "
        "l2_exercise_seed — composition pin"
    )


def test_l2_exercise_seed_imports_are_lazy_not_top_level():
    """Lazy-import discipline: substrate imports must NOT be at
    module top level so ``--help`` works without paying their cost
    (and so test-environment imports don't pull provider deps)."""
    top_l2 = [
        (m, n) for (m, n) in _top_level_imports()
        if m.endswith(".l2_exercise_seed")
    ]
    assert top_l2 == [], (
        f"l2_exercise_seed MUST be imported lazily (inside a function), "
        f"not at module top level; found {top_l2}"
    )


def test_httpx_import_is_lazy_not_top_level():
    """``httpx`` is the network dependency.  Importing it at module
    top level would break ``--help`` in environments without httpx
    AND make the import-only spine slow.  Pin: lazy import only."""
    top_httpx = [
        node for node in _SCRIPT_AST.body
        if isinstance(node, ast.Import)
        and any(a.name == "httpx" for a in node.names)
    ]
    assert top_httpx == [], (
        "httpx MUST be imported lazily inside call_doubleword_one_shot, "
        "not at module top level"
    )


# ===========================================================================
# Endpoint composition — same surface DoublewordProvider uses
# ===========================================================================


def test_dw_chat_completions_endpoint_suffix_pinned():
    """Endpoint pin: the constant MUST be ``/chat/completions``.
    Drift here = the validator hits a different DW surface than the
    production provider's realtime path, breaking the "same provider
    chain" composition claim."""
    mod = _load_script_module()
    assert mod.DW_CHAT_COMPLETIONS_SUFFIX == "/chat/completions"


def test_dw_endpoint_constant_actually_used_in_request_url():
    """Defense-in-depth: the endpoint constant must actually be
    composed into the request URL (not just defined and ignored).

    AST-based: collects every string-literal Constant node from
    ``_SCRIPT_AST``.  Exactly one literal may equal
    ``"/chat/completions"`` — the canonical constant assignment.
    Documentation strings that MENTION the endpoint may contain the
    suffix as a substring (e.g., ``"... /chat/completions endpoint
    ..."``); those are fine because they're not literal value-of
    the suffix.  We assert: at most one Constant node whose value
    equals the suffix exactly + the constant identifier
    ``DW_CHAT_COMPLETIONS_SUFFIX`` appears in source (proves
    composition)."""
    src = _SCRIPT_SRC
    assert "DW_CHAT_COMPLETIONS_SUFFIX" in src, (
        "Endpoint constant identifier must appear in source"
    )
    exact_matches = [
        node for node in ast.walk(_SCRIPT_AST)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value == "/chat/completions"
    ]
    assert len(exact_matches) == 1, (
        f"Exactly one string literal exact-matching "
        f"'/chat/completions' (the canonical constant assignment) "
        f"expected; found {len(exact_matches)} — parallel value snuck in"
    )


# ===========================================================================
# Refusal pins — operator-paced contract
# ===========================================================================


def _run_script(argv: list, env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Ensure DOUBLEWORD_API_KEY starts unset in the child unless the
    # test explicitly provides one
    env.pop("DOUBLEWORD_API_KEY", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)] + argv,
        capture_output=True, text=True, timeout=30,
        env=env, cwd=str(REPO_ROOT),
    )


def test_refuses_without_confirm_paid_flag():
    """Refusal pin: script MUST exit non-zero without
    ``--confirm-paid``, even with everything else valid."""
    result = _run_script([], {"DOUBLEWORD_API_KEY": "test-key"})
    assert result.returncode == 2, (
        f"Expected exit 2 without --confirm-paid; got {result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "confirm-paid" in result.stderr.lower() or \
        "real money" in result.stderr.lower(), (
            f"Refusal message must mention --confirm-paid / real money: "
            f"{result.stderr!r}"
        )


def test_refuses_without_doubleword_api_key():
    """Refusal pin: even with ``--confirm-paid``, missing
    ``DOUBLEWORD_API_KEY`` MUST refuse."""
    result = _run_script(["--confirm-paid"], {})
    assert result.returncode == 2, (
        f"Expected exit 2 without DOUBLEWORD_API_KEY; "
        f"got {result.returncode}\nstderr: {result.stderr!r}"
    )
    assert "DOUBLEWORD_API_KEY" in result.stderr, (
        f"Refusal message must mention DOUBLEWORD_API_KEY: "
        f"{result.stderr!r}"
    )


def test_help_works_without_api_key_or_confirm_paid():
    """Lazy-import discipline pin: ``--help`` MUST succeed without
    httpx, without DOUBLEWORD_API_KEY, and without --confirm-paid.
    This is what proves the operator can discover the CLI shape
    BEFORE committing to a paid run."""
    result = _run_script(["--help"], {})
    assert result.returncode == 0, (
        f"--help MUST exit 0; got {result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "--confirm-paid" in result.stdout
    assert "--max-cost-usd" in result.stdout
    assert "--attempts" in result.stdout


# ===========================================================================
# Report schema pins — stability across runs
# ===========================================================================


def test_report_schema_version_constant():
    """Schema pin: ``REPORT_SCHEMA_VERSION`` MUST be
    ``hardness_report.v2``.  Bumped from v1 in Phase 1.5.D.2
    Stage 1 to mark the addition of AttemptStatus / retry budget /
    HARDNESS_SET / gate fields.  v1-aware consumers MUST branch
    on this field; legacy v1 per-problem fields (passes/fails/
    attempts_completed/attempts_errored/
    measured_first_try_fail_rate) are preserved alongside the
    new structured fields so they keep parsing."""
    mod = _load_script_module()
    assert mod.REPORT_SCHEMA_VERSION == "hardness_report.v2"


def test_report_filename_is_underscore_prefixed():
    """Schema pin: the report filename MUST start with ``_`` so the
    canonical ``list_corpus_problems`` walker skips it.  Without
    this, the report would re-enter the corpus as a sibling "problem"
    on the NEXT validator run and crash the manifest loader."""
    mod = _load_script_module()
    assert mod.REPORT_FILENAME.startswith("_"), (
        f"REPORT_FILENAME {mod.REPORT_FILENAME!r} MUST start with '_' "
        f"so list_corpus_problems skips it"
    )
    assert mod.REPORT_FILENAME == "_hardness_report.json"


def test_provider_chain_label_constant():
    """Schema pin: report's ``provider_chain_used`` field MUST be
    ``"doubleword"``.  Operator-grep-stable label across all
    historical reports."""
    mod = _load_script_module()
    assert mod.PROVIDER_DW == "doubleword"


# ===========================================================================
# Prompt builder pins — clean-room format stable
# ===========================================================================


def test_clean_room_prompt_includes_target_and_test_filenames():
    """Prompt-format pin: the LLM MUST see BOTH the target file name
    AND the test file name in the prompt (so it knows which file
    to fix, not just which content)."""
    mod = _load_script_module()
    prompt = mod.build_clean_room_fix_prompt(
        target_file_name="before.py",
        before_content="def f(): return 0",
        test_file_name="test_before.py",
        test_content="def test_f(): assert f() == 1",
    )
    assert "before.py" in prompt
    assert "test_before.py" in prompt
    assert "def f(): return 0" in prompt
    assert "def test_f(): assert f() == 1" in prompt


def test_dw_response_parser_handles_content_field():
    """Composition pin: the parser MUST extract ``content`` from the
    standard OpenAI-compat shape ``{choices:[{message:{content:"..."}}]}``."""
    mod = _load_script_module()
    data = {
        "choices": [{"message": {"content": "def f(): return 1"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    assert mod._extract_dw_message_content(data) == "def f(): return 1"


def test_dw_response_parser_falls_back_to_reasoning_content():
    """Composition pin: mirrors the canonical
    ``DoublewordProvider._call_realtime_chat_completions`` fallback
    (Qwen3.5 reasoning models emit the final answer in
    ``reasoning_content`` instead of ``content`` when reasoning is
    long).  Without this fallback every validator attempt crashes
    with ``KeyError: 'content'`` against the production Qwen3.5
    model — empirically observed on first 1.5.D run, fixed in same
    arc."""
    mod = _load_script_module()
    data = {
        "choices": [{"message": {
            "content": "",
            "reasoning_content": "def f(): return 1",
        }}],
    }
    assert mod._extract_dw_message_content(data) == "def f(): return 1"
    # Also: missing 'content' key (not just empty) falls back too
    data2 = {
        "choices": [{"message": {
            "reasoning_content": "def f(): return 2",
        }}],
    }
    assert mod._extract_dw_message_content(data2) == "def f(): return 2"


def test_dw_response_parser_raises_clear_error_when_both_missing():
    """Diagnostic pin: when neither field is present, the parser
    MUST raise KeyError with the available keys listed (so the
    operator can diagnose response-shape drift in one step, not via
    re-running)."""
    import pytest as _pytest
    mod = _load_script_module()
    data = {
        "choices": [{"message": {"weird_field": "x"}}],
    }
    with _pytest.raises(KeyError) as excinfo:
        mod._extract_dw_message_content(data)
    assert "weird_field" in str(excinfo.value)


def test_strip_markdown_fences_handles_standard_fenced_block():
    """Defensive-parsing pin: if the LLM ignores the 'no fences'
    instruction, we strip one leading + trailing fence so pytest sees
    valid Python."""
    mod = _load_script_module()
    fenced = "```python\ndef f(): return 1\n```"
    assert mod.strip_markdown_fences(fenced) == "def f(): return 1"
    # Already-bare returns unchanged (after whitespace strip)
    assert mod.strip_markdown_fences("def f(): return 1") == "def f(): return 1"


# ===========================================================================
# Cost tracker pins
# ===========================================================================


def test_cost_tracker_uses_canonical_dw_pricing_constants():
    """Composition pin: the validator's pricing MUST come from the
    SAME env vars ``DoublewordProvider`` reads.  We assert the
    env-var names appear in the script source so accidental drift
    to parallel hardcoded values is caught."""
    src = _SCRIPT_SRC
    assert "DOUBLEWORD_INPUT_COST_PER_M" in src
    assert "DOUBLEWORD_OUTPUT_COST_PER_M" in src


def test_cost_tracker_exceeds_at_cap():
    """Behavioral pin: tracker reports exceeded() True once total
    crosses max."""
    mod = _load_script_module()
    tracker = mod.CostTracker(
        max_usd=0.10, input_per_m=0.10, output_per_m=0.40,
    )
    assert not tracker.exceeded()
    # 1M input + 1M output tokens = $0.10 + $0.40 = $0.50 → exceeded
    tracker.record({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
    assert tracker.exceeded()


def test_cost_tracker_zero_usage_is_safe():
    """Defensive: usage dict with missing fields / None values must
    not raise (real DW responses can omit usage on some error paths)."""
    mod = _load_script_module()
    tracker = mod.CostTracker(
        max_usd=1.0, input_per_m=0.10, output_per_m=0.40,
    )
    assert tracker.record({}) == 0.0
    assert tracker.record({"prompt_tokens": None, "completion_tokens": None}) == 0.0
    assert tracker.total_usd == 0.0


# ===========================================================================
# No-parallel-walker pin
# ===========================================================================


def test_no_parallel_corpus_directory_walker():
    """Composition discipline: the script MUST NOT contain a parallel
    ``os.listdir`` / ``Path.iterdir`` loop that ENUMERATES problem
    directories — that would duplicate ``list_corpus_problems`` and
    drift in their skip-rules.

    We DO allow ``Path.iterdir``-equivalent inside ``tempfile.
    TemporaryDirectory`` contexts (those are NOT corpus walkers).
    The pin is structural: only one call site to
    ``list_corpus_problems`` AND no ``.iterdir()`` calls on a
    ``corpus`` / ``problem_dir`` variable."""
    src = _SCRIPT_SRC
    # No ".iterdir()" anywhere in source (we deliberately keep
    # everything funneled through the canonical walker)
    assert ".iterdir()" not in src, (
        "Phase 1.5.D script MUST NOT call .iterdir() — corpus "
        "enumeration is owned by canonical list_corpus_problems"
    )


# ===========================================================================
# Honest documentation pin
# ===========================================================================


def test_module_docstring_documents_upper_bound_caveat():
    """Operator-honesty pin: the module docstring MUST document that
    the clean-room prompt produces an UPPER bound on production
    difficulty (production prompt has more context).  Without this
    note, an operator could misread the report as "production
    measurement" and make wrong calls."""
    mod = _load_script_module()
    docstring = (mod.__doc__ or "").lower()
    assert "upper bound" in docstring, (
        "Module docstring MUST document the upper-bound caveat"
    )
    assert "clean-room" in docstring or "clean room" in docstring, (
        "Module docstring MUST mention the clean-room prompt"
    )


# ===========================================================================
# Phase 1.5.D.2 Stage 1 — AttemptStatus taxonomy + retry + gate pins
# ===========================================================================


def test_attempt_status_taxonomy_is_closed_four_values():
    """Closed-taxonomy pin: ``AttemptStatus`` MUST have EXACTLY 4
    values, with the canonical string keys.  Adding a 5th value
    silently breaks the gate computation + report consumers; AST
    pin forces explicit version bump on any extension."""
    mod = _load_script_module()
    values = {m.value for m in mod.AttemptStatus}
    assert values == {
        "passed", "failed", "provider_parse_error", "errored",
    }, (
        f"AttemptStatus taxonomy drift; got {sorted(values)}"
    )


def test_attempt_status_taxonomy_ast_bytes_pinned():
    """AST-walk pin: the enum class body MUST contain exactly 4
    assignments with the canonical names.  Catches reorders / silent
    renames at the source level (not just the runtime value set)."""
    cls = None
    for node in ast.walk(_SCRIPT_AST):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "AttemptStatus"
        ):
            cls = node
            break
    assert cls is not None, (
        "AttemptStatus class MUST exist in source"
    )
    names = [
        a.targets[0].id for a in cls.body
        if isinstance(a, ast.Assign)
        and len(a.targets) == 1
        and isinstance(a.targets[0], ast.Name)
    ]
    assert names == ["PASSED", "FAILED", "PROVIDER_PARSE_ERROR", "ERRORED"], (
        f"AttemptStatus class-body assignments drifted; got {names}"
    )


# ----------------------------------------------------------------------------
# Env-knob defaults + clamping
# ----------------------------------------------------------------------------


def test_parse_retry_budget_default(monkeypatch):
    monkeypatch.delenv("JARVIS_VALIDATOR_PARSE_RETRY", raising=False)
    mod = _load_script_module()
    assert mod.parse_retry_budget() == mod.DEFAULT_PARSE_RETRY_BUDGET == 3


def test_parse_retry_budget_overridable_and_clamped(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("JARVIS_VALIDATOR_PARSE_RETRY", "5")
    assert mod.parse_retry_budget() == 5
    monkeypatch.setenv("JARVIS_VALIDATOR_PARSE_RETRY", "999")
    assert mod.parse_retry_budget() == 10  # clamped to maximum
    monkeypatch.setenv("JARVIS_VALIDATOR_PARSE_RETRY", "garbage")
    assert mod.parse_retry_budget() == 3  # default on parse error
    monkeypatch.setenv("JARVIS_VALIDATOR_PARSE_RETRY", "-2")
    assert mod.parse_retry_budget() == 0  # clamped to minimum


def test_min_completed_per_problem_default(monkeypatch):
    monkeypatch.delenv("JARVIS_VALIDATOR_MIN_COMPLETED_PER_PROBLEM", raising=False)
    mod = _load_script_module()
    assert mod.min_completed_per_problem() == 3


def test_acceptance_threshold_default_and_clamping(monkeypatch):
    mod = _load_script_module()
    monkeypatch.delenv("JARVIS_VALIDATOR_ACCEPTANCE_THRESHOLD", raising=False)
    # Stage 3.5 bumped the default from 0.40 → 0.45 (margin above
    # the Stage 3 boundary-pass at exactly 0.40).
    assert mod.acceptance_threshold() == 0.45
    monkeypatch.setenv("JARVIS_VALIDATOR_ACCEPTANCE_THRESHOLD", "0.6")
    assert abs(mod.acceptance_threshold() - 0.6) < 1e-9
    monkeypatch.setenv("JARVIS_VALIDATOR_ACCEPTANCE_THRESHOLD", "2.5")
    assert mod.acceptance_threshold() == 1.0  # clamped
    monkeypatch.setenv("JARVIS_VALIDATOR_ACCEPTANCE_THRESHOLD", "-1")
    assert mod.acceptance_threshold() == 0.0


# ----------------------------------------------------------------------------
# parse_hardness_set — comma-separated parser, whitespace tolerant
# ----------------------------------------------------------------------------


def test_parse_hardness_set_empty_inputs():
    """Empty / None / whitespace-only → empty frozenset."""
    mod = _load_script_module()
    assert mod.parse_hardness_set(None) == frozenset()
    assert mod.parse_hardness_set("") == frozenset()
    assert mod.parse_hardness_set("   ") == frozenset()
    assert mod.parse_hardness_set(", ,  ") == frozenset()


def test_parse_hardness_set_simple_and_whitespace():
    mod = _load_script_module()
    assert mod.parse_hardness_set("problem_002,problem_003") == frozenset(
        {"problem_002", "problem_003"}
    )
    # Whitespace around tokens tolerated
    assert mod.parse_hardness_set(" problem_002 , problem_003 ") == frozenset(
        {"problem_002", "problem_003"}
    )


def test_parse_hardness_set_duplicates_collapse():
    mod = _load_script_module()
    assert mod.parse_hardness_set("p1,p1,p2") == frozenset({"p1", "p2"})


def test_hardness_set_from_env_round_trip(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("JARVIS_VALIDATOR_HARDNESS_SET", "problem_002,problem_003")
    assert mod.hardness_set_from_env() == frozenset(
        {"problem_002", "problem_003"}
    )
    monkeypatch.delenv("JARVIS_VALIDATOR_HARDNESS_SET", raising=False)
    assert mod.hardness_set_from_env() == frozenset()


# ----------------------------------------------------------------------------
# compute_acceptance_gate — pure-function evaluator
# ----------------------------------------------------------------------------


def _result(pid, completed=3, fails=2, fail_rate=None):
    """Helper: synthesize a per-problem result dict shaped like
    validate_one_problem's return."""
    if fail_rate is None and completed > 0:
        fail_rate = fails / completed
    return {
        "problem_id": pid,
        "kind": "logic_inversion",
        "attempts_completed": completed,
        "fails": fails,
        "passes": max(0, completed - fails),
        "measured_first_try_fail_rate": fail_rate,
    }


def test_gate_empty_set_returns_unevaluable():
    mod = _load_script_module()
    mean, meets, diag = mod.compute_acceptance_gate(
        frozenset(), [], threshold=0.40, min_completed=3,
    )
    assert mean is None
    assert meets is False
    assert diag["reason"] == "empty_set"


def test_gate_missing_members_returns_unevaluable():
    mod = _load_script_module()
    mean, meets, diag = mod.compute_acceptance_gate(
        frozenset({"problem_002", "problem_003"}),
        [_result("problem_002", completed=3, fails=2)],
        threshold=0.40, min_completed=3,
    )
    assert mean is None
    assert meets is False
    assert diag["reason"] == "missing_members"
    assert diag["missing"] == ["problem_003"]


def test_gate_insufficient_samples_returns_unevaluable():
    mod = _load_script_module()
    mean, meets, diag = mod.compute_acceptance_gate(
        frozenset({"problem_002"}),
        [_result("problem_002", completed=2, fails=2)],
        threshold=0.40, min_completed=3,
    )
    assert mean is None
    assert meets is False
    assert diag["reason"] == "insufficient_samples"
    assert diag["under_sampled"] == ["problem_002"]


def test_gate_passes_when_mean_meets_threshold():
    mod = _load_script_module()
    mean, meets, diag = mod.compute_acceptance_gate(
        frozenset({"problem_002", "problem_003"}),
        [
            _result("problem_002", completed=4, fails=3),  # 0.75
            _result("problem_003", completed=4, fails=1),  # 0.25
        ],
        threshold=0.40, min_completed=3,
    )
    # mean = (0.75 + 0.25) / 2 = 0.50, meets 0.40
    assert mean is not None
    assert abs(mean - 0.50) < 1e-9
    assert meets is True
    assert diag["reason"] == "ok"
    assert diag["n_problems"] == 2


def test_gate_fails_when_mean_below_threshold():
    mod = _load_script_module()
    mean, meets, diag = mod.compute_acceptance_gate(
        frozenset({"problem_002"}),
        [_result("problem_002", completed=5, fails=1)],  # 0.20
        threshold=0.40, min_completed=3,
    )
    assert mean is not None
    assert abs(mean - 0.20) < 1e-9
    assert meets is False
    assert diag["reason"] == "ok"


def test_gate_default_acceptance_threshold_pinned():
    """Operator-honesty pin: bumped to 0.45 in Stage 3.5 after the
    first paid Stage 3 run landed exactly on the prior 0.40
    boundary with high per-problem variance.  0.45 gives margin
    without being so aggressive that legitimate corpora fail."""
    mod = _load_script_module()
    assert mod.DEFAULT_ACCEPTANCE_THRESHOLD == 0.45


def test_gate_default_min_completed_pinned():
    mod = _load_script_module()
    assert mod.DEFAULT_MIN_COMPLETED_PER_PROBLEM == 3


def test_gate_default_parse_retry_pinned():
    mod = _load_script_module()
    assert mod.DEFAULT_PARSE_RETRY_BUDGET == 3


def test_gate_default_per_problem_floor_pinned():
    """Stage 3.5 contract pin: per-problem floor default is 0.20
    (the 'no freeriders' rule).  Each HARDNESS_SET member must
    individually clear this floor — one fixture at 0% can no
    longer be carried by another fixture at 80% past the mean."""
    mod = _load_script_module()
    assert mod.DEFAULT_PER_PROBLEM_FLOOR == 0.20


def test_per_problem_floor_env_override_and_clamping(monkeypatch):
    mod = _load_script_module()
    monkeypatch.setenv("JARVIS_VALIDATOR_PER_PROBLEM_FLOOR", "0.30")
    assert abs(mod.per_problem_floor() - 0.30) < 1e-9
    monkeypatch.setenv("JARVIS_VALIDATOR_PER_PROBLEM_FLOOR", "2.5")
    assert mod.per_problem_floor() == 1.0  # clamped to maximum
    monkeypatch.setenv("JARVIS_VALIDATOR_PER_PROBLEM_FLOOR", "-1")
    assert mod.per_problem_floor() == 0.0  # clamped to minimum
    monkeypatch.setenv("JARVIS_VALIDATOR_PER_PROBLEM_FLOOR", "garbage")
    assert mod.per_problem_floor() == 0.20  # default on parse error


def test_per_problem_floor_default(monkeypatch):
    monkeypatch.delenv("JARVIS_VALIDATOR_PER_PROBLEM_FLOOR", raising=False)
    mod = _load_script_module()
    assert mod.per_problem_floor() == 0.20


def test_gate_below_floor_returns_unevaluable():
    """Stage 3.5 'no freeriders' canary: a HARDNESS_SET where ALL
    members are sufficiently sampled AND the mean clears the
    threshold MUST STILL fail the gate if ANY single member is
    below the per-problem floor.

    This is the empirical failure mode from Stage 3: problem_002
    at 80% + problem_003 at 0% → mean=0.40 (passes old gate) but
    003 is a freerider (carries no signal).  Floor=0.20 catches it."""
    mod = _load_script_module()
    mean, meets, diag = mod.compute_acceptance_gate(
        frozenset({"problem_002", "problem_003"}),
        [
            _result("problem_002", completed=5, fails=4),  # 0.80
            _result("problem_003", completed=5, fails=0),  # 0.00
        ],
        threshold=0.40, min_completed=3,
        per_problem_floor_value=0.20,
    )
    assert mean is None
    assert meets is False
    assert diag["reason"] == "below_floor"
    assert diag["under_floor"] == ["problem_003"]
    assert diag["floor"] == 0.20
    # Per-problem rates surface in diagnostic for operator review
    assert diag["per_problem_rates"]["problem_003"] == 0.0
    assert diag["per_problem_rates"]["problem_002"] == 0.80


def test_gate_all_above_floor_evaluates_normally():
    """When every member clears the floor, gate proceeds to mean
    check as before — floor is additive, not overriding."""
    mod = _load_script_module()
    mean, meets, diag = mod.compute_acceptance_gate(
        frozenset({"problem_002", "problem_003"}),
        [
            _result("problem_002", completed=5, fails=3),  # 0.60
            _result("problem_003", completed=5, fails=2),  # 0.40
        ],
        threshold=0.45, min_completed=3,
        per_problem_floor_value=0.20,
    )
    # mean = 0.50, threshold 0.45 → PASS; floor 0.20, all clear
    assert mean is not None
    assert abs(mean - 0.50) < 1e-9
    assert meets is True
    assert diag["reason"] == "ok"


def test_gate_floor_check_runs_after_insufficient_samples():
    """Diagnostic-order pin: floor check fires AFTER insufficient
    samples (so an under-sampled fixture doesn't get reported as
    'below_floor' when the real issue is sample count)."""
    mod = _load_script_module()
    mean, meets, diag = mod.compute_acceptance_gate(
        frozenset({"problem_002"}),
        [_result("problem_002", completed=2, fails=0)],  # 0.00 + low samples
        threshold=0.45, min_completed=3,
        per_problem_floor_value=0.20,
    )
    assert mean is None
    assert meets is False
    # Diagnostic should be insufficient_samples, NOT below_floor —
    # under-sampling is the more fundamental problem
    assert diag["reason"] == "insufficient_samples"


def test_report_v2_schema_includes_per_problem_floor_field():
    """Stage 3.5: ``per_problem_floor`` MUST be a top-level report
    field so a consumer can reconstruct exactly which floor was
    applied (especially when env-configured to a non-default value)."""
    src = _SCRIPT_SRC
    assert '"per_problem_floor"' in src


# ----------------------------------------------------------------------------
# validate_one_problem — exercise the retry + classification spine
# without paying for real DW calls
# ----------------------------------------------------------------------------


class _FakeProblem:
    """Minimal duck-typed stand-in for ExerciseProblem."""

    def __init__(self, problem_id="problem_test", kind_value="off_by_one"):
        self.problem_id = problem_id

        class _K:
            value = kind_value

        self.kind = _K()
        self.target_file_name = "before.py"
        self.test_file_name = "test_before.py"
        self.before_content = "def f(): return 0\n"
        self.test_content = (
            "from before import f\n"
            "def test_f(): assert f() == 1\n"
        )


async def _run_validate(
    monkeypatch, *,
    call_responses,
    pytest_results,
    attempts=3,
    parse_retry=2,
    min_completed=2,
):
    """Helper: monkeypatch ``call_doubleword_one_shot`` to return
    a sequence of (text, usage) tuples or to raise; monkeypatch
    ``run_pytest_against_candidate`` to return a deterministic
    sequence of bool verdicts.  Then run validate_one_problem and
    return its result dict."""
    mod = _load_script_module()
    call_iter = iter(call_responses)
    pytest_iter = iter(pytest_results)

    async def fake_call(*a, **kw):
        nxt = next(call_iter)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def fake_pytest(*a, **kw):
        return next(pytest_iter)

    monkeypatch.setattr(mod, "call_doubleword_one_shot", fake_call)
    monkeypatch.setattr(mod, "run_pytest_against_candidate", fake_pytest)

    tracker = mod.CostTracker(max_usd=1.0, input_per_m=0.10, output_per_m=0.40)
    return await mod.validate_one_problem(
        _FakeProblem(), attempts, {
            "api_key": "k", "base_url": "https://x", "model": "m",
            "timeout_s": 10.0,
        }, tracker,
        parse_retry_budget_value=parse_retry,
        min_completed_value=min_completed,
    )


def test_validate_classifies_pass_and_fail(monkeypatch):
    """Happy path: pytest verdicts route to PASSED / FAILED status."""
    import asyncio as _aio
    result = _aio.run(_run_validate(
        monkeypatch,
        call_responses=[
            ("def f(): return 1\n", {"prompt_tokens": 10, "completion_tokens": 5}),
            ("def f(): return 1\n", {"prompt_tokens": 10, "completion_tokens": 5}),
            ("def f(): return 0\n", {"prompt_tokens": 10, "completion_tokens": 5}),
        ],
        pytest_results=[True, True, False],
        attempts=3,
    ))
    counts = result["attempt_status_counts"]
    assert counts["passed"] == 2
    assert counts["failed"] == 1
    assert counts["provider_parse_error"] == 0
    assert counts["errored"] == 0
    assert result["measured_first_try_fail_rate"] == 1 / 3
    assert result["insufficient_samples"] is False


def test_validate_retries_on_provider_parse_error(monkeypatch):
    """PROVIDER_PARSE_ERROR (KeyError from response parser) MUST be
    retried up to the budget within one logical attempt.  After
    successful retry, the attempt records the final status — NOT
    PROVIDER_PARSE_ERROR."""
    import asyncio as _aio
    # First call raises KeyError (parse error), second call succeeds
    result = _aio.run(_run_validate(
        monkeypatch,
        call_responses=[
            KeyError("first parse fail"),
            ("def f(): return 1\n", {"prompt_tokens": 10, "completion_tokens": 5}),
        ],
        pytest_results=[True],
        attempts=1,
        parse_retry=2,
    ))
    counts = result["attempt_status_counts"]
    assert counts["passed"] == 1
    assert counts["provider_parse_error"] == 0
    assert result["per_attempt"][0]["retries_used"] == 1


def test_validate_retry_budget_exhausted_records_parse_error(monkeypatch):
    """When retries exhaust without a parseable response, the attempt
    is classified PROVIDER_PARSE_ERROR + excluded from fail-rate."""
    import asyncio as _aio
    result = _aio.run(_run_validate(
        monkeypatch,
        call_responses=[
            KeyError("a"), KeyError("b"), KeyError("c"),
        ],
        pytest_results=[],
        attempts=1,
        parse_retry=2,  # 2 retries → 3 total attempts (initial + 2)
    ))
    counts = result["attempt_status_counts"]
    assert counts["provider_parse_error"] == 1
    assert counts["passed"] == 0 and counts["failed"] == 0
    # Excluded from fail-rate denominator AND numerator
    assert result["measured_first_try_fail_rate"] is None
    assert result["per_attempt"][0]["reason"] == "retry_budget_exhausted"


def test_validate_other_exception_routes_to_errored(monkeypatch):
    """Non-KeyError exceptions route to ERRORED — NOT retried."""
    import asyncio as _aio
    result = _aio.run(_run_validate(
        monkeypatch,
        call_responses=[
            RuntimeError("network down"),
        ],
        pytest_results=[],
        attempts=1,
        parse_retry=5,  # retries would exist but errored doesn't use them
    ))
    counts = result["attempt_status_counts"]
    assert counts["errored"] == 1
    assert counts["provider_parse_error"] == 0
    assert result["per_attempt"][0]["retries_used"] == 0


def test_validate_parse_errors_excluded_from_fail_rate(monkeypatch):
    """Defense-in-depth: a mix of PROVIDER_PARSE_ERROR + completed
    attempts MUST produce a fail-rate computed over completed only.
    This is the bias-fix the user's contract demands."""
    import asyncio as _aio
    result = _aio.run(_run_validate(
        monkeypatch,
        call_responses=[
            # Attempt 0: parse error exhaust retries (counts as 1 PROVIDER_PARSE_ERROR)
            KeyError("x"), KeyError("y"),
            # Attempt 1: passes pytest (1 PASSED)
            ("ok", {"prompt_tokens": 10, "completion_tokens": 5}),
            # Attempt 2: fails pytest (1 FAILED)
            ("ok", {"prompt_tokens": 10, "completion_tokens": 5}),
        ],
        pytest_results=[True, False],
        attempts=3,
        parse_retry=1,  # 1 retry → 2 total tries per attempt
    ))
    counts = result["attempt_status_counts"]
    assert counts["passed"] == 1
    assert counts["failed"] == 1
    assert counts["provider_parse_error"] == 1
    # fail_rate = 1 failed / 2 completed = 0.50 (PROVIDER_PARSE_ERROR excluded)
    assert result["measured_first_try_fail_rate"] == 0.50


def test_validate_insufficient_samples_flag_set(monkeypatch):
    """insufficient_samples MUST be True when completed < min."""
    import asyncio as _aio
    result = _aio.run(_run_validate(
        monkeypatch,
        call_responses=[
            ("ok", {"prompt_tokens": 10, "completion_tokens": 5}),
        ],
        pytest_results=[True],
        attempts=1,
        min_completed=3,
    ))
    assert result["insufficient_samples"] is True


# ----------------------------------------------------------------------------
# Report-level structured fields — present in serialized v2 schema
# ----------------------------------------------------------------------------


def test_report_v2_schema_constants_present_in_source():
    """Source-level pin: the new schema-v2 field names MUST appear
    in the script's report-construction dict so a reader can find
    where they come from (operator-debug + grep-discoverability)."""
    src = _SCRIPT_SRC
    for field in [
        "parse_retry_budget",
        "min_completed_per_problem",
        "acceptance_threshold",
        "hardness_set",
        "hardness_set_mean_fail_rate",
        "meets_acceptance_gate",
        "gate_diagnostic",
    ]:
        # Match key form: "field":  OR field=  (constants and dict keys)
        assert (
            f'"{field}"' in src
        ), f"Schema-v2 field {field!r} missing from script source"


def test_compute_acceptance_gate_is_pure_no_io():
    """Discipline pin: ``compute_acceptance_gate`` MUST NOT import or
    call I/O — it's a pure-function evaluator over its inputs.  We
    inspect the function body's AST for any forbidden patterns."""
    cls = None
    for node in ast.walk(_SCRIPT_AST):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "compute_acceptance_gate"
        ):
            cls = node
            break
    assert cls is not None, "compute_acceptance_gate MUST exist"
    # No Calls to: open/print/Path/json/os.environ/subprocess
    forbidden_calls = {
        "open", "print", "Path", "input",
    }
    for sub in ast.walk(cls):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name) and sub.func.id in forbidden_calls:
                raise AssertionError(
                    f"compute_acceptance_gate calls forbidden "
                    f"function {sub.func.id!r}"
                )
            if isinstance(sub.func, ast.Attribute):
                full = []
                cur = sub.func
                while isinstance(cur, ast.Attribute):
                    full.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    full.append(cur.id)
                full = ".".join(reversed(full))
                for forbidden in (
                    "os.environ", "subprocess", "json.loads", "json.dumps",
                    "Path.read_text", "Path.write_text",
                ):
                    if forbidden in full:
                        raise AssertionError(
                            f"compute_acceptance_gate calls forbidden "
                            f"{full!r}"
                        )
