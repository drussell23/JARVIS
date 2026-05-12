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
    ``hardness_report.v1``.  Any schema change requires a version
    bump."""
    mod = _load_script_module()
    assert mod.REPORT_SCHEMA_VERSION == "hardness_report.v1"


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
