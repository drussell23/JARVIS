"""Regression spine — SWE-Bench-Pro Phase B.2.1 envelope builder.

Phase B.2.1 is the pure-data composition bridge that turns a
``ProblemSpec`` (Phase A) + ``PreparedProblem`` (Phase B.1) into an
``IntentEnvelope`` ready for ``IntakeLayerService.ingest_envelope``.
The builder composes ONLY canonical surfaces and ships no side effects.

Spine invariants
----------------

  1. Builder produces a valid IntentEnvelope (passes __post_init__).
  2. ``source = "swe_bench_pro"`` — registered in ``_VALID_SOURCES``.
  3. ``target_files = tuple(prepared.target_paths)``.
  4. ``evidence[EVIDENCE_REPO_ROOT_KEY]`` = str(prepared.worktree_path).
  5. Evidence carries instance_id / base_commit / branch_name /
     repo_url / signature.
  6. ``description = problem.problem_statement``.
  7. Urgency derivation: default ``"low"`` (BACKGROUND route); env
     override accepts the four valid urgencies; invalid value falls
     back to default with a WARN log.
  8. ``repo`` is derived from ProblemSpec (prefers .repo over .repo_url).
  9. ``confidence = 1.0`` (benchmark-confirmed bug).
 10. ``requires_human_ack = False`` (autonomous benchmark workload).
 11. ``signature = problem.instance_id`` → drives router-side dedup.
 12. ``causal_id`` allocated fresh per build (auto via make_envelope).
 13. AST pin: ENVELOPE_SOURCE constant is a member of _VALID_SOURCES.
 14. AST pin: builder imports EVIDENCE_REPO_ROOT_KEY from
     operation_advisor (no parallel "repo_root" string literal in
     the substrate file — drift would silently fork the canonical key).
 15. AST pin: builder uses canonical make_envelope (no direct
     IntentEnvelope() constructor call — guards against parallel
     envelope construction logic).
 16. AST pin: no master-flag (swe_bench_pro_enabled) call inside the
     builder — that responsibility lives in the B.2.2 evaluator façade.
 17. FlagRegistry seed: 1 spec for the urgency env knob.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    _VALID_SOURCES,
    _VALID_URGENCIES,
    IntentEnvelope,
)
from backend.core.ouroboros.governance.operation_advisor import (
    EVIDENCE_REPO_ROOT_KEY,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.envelope_builder import (
    ENVELOPE_SOURCE,
    ENVELOPE_URGENCY_ENV_VAR,
    build_evaluation_envelope,
    register_flags,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    PreparedProblem,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def problem() -> ProblemSpec:
    return ProblemSpec(
        instance_id="octocat__hello-001",
        repo="octocat/hello",
        base_commit="abc123def456",
        problem_statement=(
            "Fix the parser bug where multi-line headers are "
            "dropped under nested quoting."
        ),
        test_patch=(
            "--- a/src/parser.py\n"
            "+++ b/src/parser.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        gold_patch="",
        repo_url="https://github.com/octocat/hello",
    )


@pytest.fixture
def prepared(problem: ProblemSpec, tmp_path: Path) -> PreparedProblem:
    wt = tmp_path / "worktree-001"
    wt.mkdir()
    return PreparedProblem(
        problem_instance_id=problem.instance_id,
        worktree_path=wt,
        base_commit=problem.base_commit,
        repo_url=problem.repo_url,
        branch_name="swebp/octocat__hello-001",
        target_paths=("src/parser.py", "tests/test_parser.py"),
        elapsed_s=2.7,
    )


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(ENVELOPE_URGENCY_ENV_VAR, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. Basic compose
# ---------------------------------------------------------------------------


def test_builder_returns_intent_envelope(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    env = build_evaluation_envelope(problem, prepared)
    assert isinstance(env, IntentEnvelope)


def test_builder_envelope_schema_validates(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """Envelope construction passes IntentEnvelope.__post_init__ —
    a basic sanity check that all required fields are populated."""
    # If __post_init__ would raise, build_evaluation_envelope would too.
    env = build_evaluation_envelope(problem, prepared)
    # Round-trip via to_dict / from_dict proves the schema is
    # complete + self-consistent.
    payload = env.to_dict()
    restored = IntentEnvelope.from_dict(payload)
    assert restored.source == env.source
    assert restored.target_files == env.target_files
    assert restored.evidence == env.evidence


# ---------------------------------------------------------------------------
# 2. Source / target_files / evidence / description
# ---------------------------------------------------------------------------


def test_source_is_swe_bench_pro(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    env = build_evaluation_envelope(problem, prepared)
    assert env.source == ENVELOPE_SOURCE
    assert env.source == "swe_bench_pro"


def test_target_files_is_empty_not_test_patch_paths(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """Cognition-feed fix (soak bt-2026-05-17-194855).

    The OLD contract asserted ``target_files == prepared.target_paths``
    — but those are the *test_patch* paths; surfacing them inverted the
    agent's task (forbidden to edit tests; scorer rejects test edits as
    cheating) → CLASSIFY no-op. A SWE-bench envelope now carries NO
    target_files; the agent localizes from the issue via the
    exploration-first Iron Gate. This test now guards the FIX."""
    env = build_evaluation_envelope(problem, prepared)
    assert env.target_files == ()
    assert env.target_files != tuple(prepared.target_paths)


def test_evidence_carries_canonical_repo_root_key(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    env = build_evaluation_envelope(problem, prepared)
    assert EVIDENCE_REPO_ROOT_KEY in env.evidence
    assert env.evidence[EVIDENCE_REPO_ROOT_KEY] == str(prepared.worktree_path)


def test_evidence_carries_required_keys(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    env = build_evaluation_envelope(problem, prepared)
    expected_keys = {
        EVIDENCE_REPO_ROOT_KEY,
        "problem_instance_id",
        "base_commit",
        "branch_name",
        "repo_url",
        "signature",
    }
    assert expected_keys.issubset(env.evidence.keys())
    assert env.evidence["problem_instance_id"] == problem.instance_id
    assert env.evidence["base_commit"] == problem.base_commit
    assert env.evidence["branch_name"] == prepared.branch_name
    assert env.evidence["repo_url"] == problem.repo_url


def test_description_is_problem_statement(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    env = build_evaluation_envelope(problem, prepared)
    assert env.description == problem.problem_statement


# ---------------------------------------------------------------------------
# 3. Urgency derivation — default + env override + invalid fallback
# ---------------------------------------------------------------------------


def test_urgency_default_is_normal(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """Trace-2 fix (soak bt-2026-05-17-225244): the OLD default "low"
    gave the injected op the lowest priority-queue rank with
    deadline=inf — structurally starved by the background-sensor
    flood (django: 0 dispatches). Default is now "normal" (finite
    deadline + starvation-guard protection). Operators keep the env
    escape hatch for bulk DW-only economics. This test now guards the
    FIX, not the defect."""
    env = build_evaluation_envelope(problem, prepared)
    assert env.urgency == "normal"


@pytest.mark.parametrize("urgency", sorted(_VALID_URGENCIES))
def test_urgency_env_override_accepts_all_valid_values(
    urgency: str,
    problem: ProblemSpec, prepared: PreparedProblem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENVELOPE_URGENCY_ENV_VAR, urgency)
    env = build_evaluation_envelope(problem, prepared)
    assert env.urgency == urgency


def test_urgency_env_override_invalid_falls_back_to_default(
    problem: ProblemSpec, prepared: PreparedProblem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid urgency value MUST NOT crash the build — falls back to
    the default with a WARN log. Keeps benchmark robust to operator
    typos. Trace-2: the default is now "normal" (anti-starvation)."""
    monkeypatch.setenv(ENVELOPE_URGENCY_ENV_VAR, "URGENT_NOW")
    env = build_evaluation_envelope(problem, prepared)
    assert env.urgency == "normal"


def test_urgency_env_override_case_insensitive(
    problem: ProblemSpec, prepared: PreparedProblem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENVELOPE_URGENCY_ENV_VAR, "  NORMAL  ")
    env = build_evaluation_envelope(problem, prepared)
    assert env.urgency == "normal"


# ---------------------------------------------------------------------------
# 4. repo / confidence / requires_human_ack
# ---------------------------------------------------------------------------


def test_repo_prefers_problem_repo_over_repo_url(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    env = build_evaluation_envelope(problem, prepared)
    assert env.repo == problem.repo


def test_repo_falls_back_to_repo_url_when_repo_empty(
    prepared: PreparedProblem, tmp_path: Path, clean_env: None,
) -> None:
    p = ProblemSpec(
        instance_id="x",
        repo="",
        base_commit="abc",
        problem_statement="fix it",
        test_patch="",
        gold_patch="",
        repo_url="https://example.com/r",
    )
    env = build_evaluation_envelope(p, prepared)
    assert env.repo == "https://example.com/r"


def test_confidence_is_one(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """Benchmark-confirmed bug — confidence is 1.0."""
    env = build_evaluation_envelope(problem, prepared)
    assert env.confidence == 1.0


def test_requires_human_ack_is_false(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """Autonomous benchmark workload — no human-ack gating."""
    env = build_evaluation_envelope(problem, prepared)
    assert env.requires_human_ack is False


# ---------------------------------------------------------------------------
# 5. Signature / dedup / causal_id
# ---------------------------------------------------------------------------


def test_signature_equals_problem_instance_id(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    env = build_evaluation_envelope(problem, prepared)
    assert env.evidence["signature"] == problem.instance_id


def test_causal_id_is_fresh_per_build(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """Each (problem × build) gets a fresh causal_id. Retries at the
    builder level are distinct ops; downstream dedup happens via the
    intake router's idempotency_key store, not at the builder."""
    env_a = build_evaluation_envelope(problem, prepared)
    env_b = build_evaluation_envelope(problem, prepared)
    assert env_a.causal_id != env_b.causal_id
    assert env_a.idempotency_key != env_b.idempotency_key


def test_dedup_key_stable_across_builds_for_same_problem(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """The dedup_key is derived from (source, target_files, evidence
    signature). Same problem → same dedup_key, so back-to-back ingests
    of the same problem within the router's idempotency window
    collapse to one op."""
    env_a = build_evaluation_envelope(problem, prepared)
    env_b = build_evaluation_envelope(problem, prepared)
    assert env_a.dedup_key == env_b.dedup_key


# ---------------------------------------------------------------------------
# 6. Source registration in _VALID_SOURCES
# ---------------------------------------------------------------------------


def test_envelope_source_is_member_of_valid_sources() -> None:
    """The envelope source token MUST be registered in
    _VALID_SOURCES. Drift here causes a runtime
    EnvelopeValidationError that's harder to diagnose than a clear
    spine failure."""
    assert ENVELOPE_SOURCE in _VALID_SOURCES


# ---------------------------------------------------------------------------
# 7. AST pins
# ---------------------------------------------------------------------------


def _builder_source() -> str:
    from backend.core.ouroboros.governance.swe_bench_pro import (
        envelope_builder,
    )
    return Path(envelope_builder.__file__).read_text()


def test_ast_pin_builder_imports_evidence_repo_root_key() -> None:
    """Operator binding (B.2.0 hardening note 2): single source of
    truth for the canonical evidence key. The builder MUST import
    ``EVIDENCE_REPO_ROOT_KEY`` from operation_advisor — re-deriving
    or hardcoding the string "repo_root" would silently fork the
    canonical key and break the producer/consumer contract."""
    src = _builder_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "operation_advisor" in module:
                for alias in node.names:
                    if alias.name == "EVIDENCE_REPO_ROOT_KEY":
                        found = True
                        break
    assert found, (
        "envelope_builder.py does not import EVIDENCE_REPO_ROOT_KEY "
        "from operation_advisor — risk of parallel 'repo_root' literal"
    )


def test_ast_pin_no_repo_root_string_literal_in_builder_body() -> None:
    """Defensive twin of the import-pin: even with the import in
    place, a stray string literal "repo_root" elsewhere in the module
    would re-introduce drift. Walk every string Constant and assert
    no naked match."""
    src = _builder_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            # The docstring legitimately references "repo_root" in
            # prose. Filter on length to exclude prose comments.
            if v == "repo_root":
                raise AssertionError(
                    'envelope_builder.py contains a naked "repo_root" '
                    'string literal — use EVIDENCE_REPO_ROOT_KEY instead'
                )


def test_ast_pin_builder_composes_make_envelope_not_intent_envelope() -> None:
    """Single envelope-construction discipline: the builder calls
    ``make_envelope(...)`` (the canonical factory) and NEVER invokes
    ``IntentEnvelope(...)`` directly. Direct construction would
    bypass causal_id / idempotency_key allocation + dedup_key
    derivation."""
    src = _builder_source()
    tree = ast.parse(src)
    make_envelope_called = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = ""
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name == "make_envelope":
                make_envelope_called = True
            if name == "IntentEnvelope":
                # Type-annotation usage (IntentEnvelope in returns)
                # is NOT a constructor call — those are
                # ast.Subscript/ast.Name in annotations, not Call
                # nodes. Any Call with this name is a constructor
                # invocation and is forbidden.
                raise AssertionError(
                    "envelope_builder.py invokes IntentEnvelope(...) "
                    "directly — must compose make_envelope instead"
                )
    assert make_envelope_called, (
        "envelope_builder.py never calls make_envelope — wiring missing"
    )


def test_ast_pin_no_master_flag_gate_in_builder() -> None:
    """Operator binding: master-flag responsibility lives with the
    side-effect-producing surface (B.2.2 evaluator façade), NOT in
    the pure-data builder. A ``swe_bench_pro_enabled()`` call inside
    the builder would couple data composition to env state — making
    the builder hard to unit-test and creating "flag drift across
    layers" risk."""
    src = _builder_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = ""
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name == "swe_bench_pro_enabled":
                raise AssertionError(
                    "envelope_builder.py calls swe_bench_pro_enabled() "
                    "— master-flag gating belongs in B.2.2 evaluator "
                    "façade, not in the pure-data builder"
                )


def test_ast_pin_envelope_source_value_pinned() -> None:
    """The ENVELOPE_SOURCE constant value is load-bearing — every
    downstream observability filter, scorer, and SSE consumer
    filters on this exact string. Renames must propagate through
    intent_envelope.py's _VALID_SOURCES entry simultaneously, which
    is a separate AST pin above."""
    assert ENVELOPE_SOURCE == "swe_bench_pro"


# ---------------------------------------------------------------------------
# 8. FlagRegistry seed
# ---------------------------------------------------------------------------


def test_register_flags_returns_one_spec() -> None:
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    count = register_flags(_Capturer())
    assert count == 1
    assert captured[0].name == ENVELOPE_URGENCY_ENV_VAR
    # Trace-2 fix (bt-2026-05-17-225244): anti-starvation default.
    assert captured[0].default == "normal"


def test_register_flags_never_raises_when_registry_register_throws() -> None:
    class _ExplodingCapturer:
        def register(self, spec) -> None:
            raise RuntimeError("simulated registry failure")

    assert register_flags(_ExplodingCapturer()) == 0


# ---------------------------------------------------------------------------
# 9. End-to-end — envelope is ingest-ready
# ---------------------------------------------------------------------------


def test_envelope_roundtrips_through_unified_intake_router_create_context(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """Sanity check: the envelope's evidence dict is JSON-serializable,
    which is the load-bearing precondition for unified_intake_router's
    ``intake_evidence_json`` stamping at op_context.py creation
    (``json.dumps(envelope.evidence, sort_keys=True)``).
    """
    import json
    env = build_evaluation_envelope(problem, prepared)
    # If any evidence value were not JSON-serializable, this would
    # raise TypeError — and the downstream router would silently
    # stamp an empty string instead of the canonical repo_root.
    serialized = json.dumps(env.evidence, sort_keys=True)
    parsed = json.loads(serialized)
    assert parsed[EVIDENCE_REPO_ROOT_KEY] == str(prepared.worktree_path)
    assert parsed["problem_instance_id"] == problem.instance_id


def test_envelope_target_files_empty_localize_from_issue(
    problem: ProblemSpec, prepared: PreparedProblem, clean_env: None,
) -> None:
    """Cognition-feed fix (soak bt-2026-05-17-194855).

    The OLD docstring claimed "SWE-Bench-Pro envelopes always carry at
    least one target path (parsed from the test_patch)". That WAS the
    defect — the test_patch paths are not the agent's target. SWE-bench
    is now in the same epistemic class as vision_sensor: NO target
    files; the agent localizes from the issue. The envelope still
    constructs (intent_envelope's _EMPTY_TARGET_FILES_EXEMPT_SOURCES
    honours source='swe_bench_pro')."""
    env = build_evaluation_envelope(problem, prepared)
    assert env.target_files == ()
    assert env.source == "swe_bench_pro"
