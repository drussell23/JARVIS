"""Regression spine for Phase 1.5.A — l2_exercise_seed substrate.

Pins the load-bearing structural invariants for the L2 exercise
corpus injection mechanism:

* Two closed taxonomies (ExerciseProblemKind 5-value /
  ExerciseInjectionVerdict 5-value) bytes-pinned via AST walk
* Frozen ExerciseProblem dataclass with symmetric to_dict /
  from_dict round-trip per §33.5
* load_exercise_problem NEVER raises — returns None on every
  malformed-input class (missing dir / missing manifest /
  malformed JSON / unknown kind / missing files / I/O error)
* setup_exercise_worktree composes canonical WorktreeManager
  (no parallel isolation primitive); NEVER raises into caller
  except CancelledError
* build_exercise_intent composes canonical make_envelope (NO
  parallel IntentEnvelope construction); evidence carries
  category=l2_exercise_corpus + problem_id + kind
* CADENCE_SYNTHETIC_SOURCE constant equals canonical
  ``cadence_synthetic`` token (matches the _VALID_SOURCES
  whitelist entry added 2026-05-05 in intake/intent_envelope.py)
* maybe_inject_exercise_at_boot orchestrates the 4-stage pipeline
  + returns one of 5 verdict-taxonomy values; NEVER raises
* Master flag default-FALSE per §33.1; clamping + garbage-
  tolerant env loaders
* register_flags installs 3 specs; idempotent; auto-discovered
  by the canonical §33.3 walker
* AST pins: composition + authority asymmetry
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from backend.core.ouroboros.governance import l2_exercise_seed
from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagType,
)
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
)
from backend.core.ouroboros.governance.l2_exercise_seed import (
    CADENCE_SYNTHETIC_SOURCE,
    CORPUS_COUNT_ENV_VAR,
    CORPUS_PATH_ENV_VAR,
    ExerciseInjectionVerdict,
    ExerciseProblem,
    ExerciseProblemKind,
    L2_EXERCISE_SEED_SCHEMA_VERSION,
    MASTER_FLAG_ENV_VAR,
    build_exercise_intent,
    corpus_count,
    corpus_enabled,
    corpus_path,
    list_corpus_problems,
    load_exercise_problem,
    maybe_inject_exercise_at_boot,
    register_flags,
    setup_exercise_worktree,
)


_MODULE_SRC = Path(
    inspect.getfile(l2_exercise_seed),
).read_text(encoding="utf-8")
_MODULE_AST = ast.parse(_MODULE_SRC)


# ===========================================================================
# Fixtures + helpers
# ===========================================================================


def _write_problem_dir(
    base: Path,
    *,
    problem_id: str = "problem_test_001",
    kind: str = "off_by_one",
    before_content: str = "def nth(lst, n):\n    return lst[n]\n",
    test_content: str = (
        "from before import nth\n\n"
        "def test_nth_one_indexed():\n"
        "    assert nth([10, 20, 30], 1) == 10\n"
    ),
    target_file_name: str = "before.py",
    test_file_name: str = "test_before.py",
    extra_metadata: Optional[dict] = None,
) -> Path:
    """Write a complete fixture directory to ``base/problem_id/``.
    Returns the problem directory path."""
    problem_dir = base / problem_id
    problem_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": problem_id,
        "kind": kind,
        "target_file_name": target_file_name,
        "test_file_name": test_file_name,
    }
    if extra_metadata:
        manifest.update(extra_metadata)
    (problem_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    (problem_dir / target_file_name).write_text(
        before_content, encoding="utf-8",
    )
    (problem_dir / test_file_name).write_text(
        test_content, encoding="utf-8",
    )
    return problem_dir


class _StubWorktreeManager:
    """Composes WorktreeManager Protocol via duck typing for testing.

    Records create + cleanup calls so the spine can verify
    composition without spawning real git subprocesses."""

    def __init__(self, base: Path, fail_create: bool = False):
        self._base = base
        self.fail_create = fail_create
        self.created: list = []
        self.cleaned: list = []

    async def create(self, branch_name: str) -> Path:
        if self.fail_create:
            raise RuntimeError("stub-wm: create disabled for this test")
        safe = branch_name.replace("/", "_")
        path = self._base / safe
        path.mkdir(parents=True, exist_ok=True)
        self.created.append(branch_name)
        return path

    async def cleanup(self, worktree_path: Path) -> None:
        self.cleaned.append(worktree_path)


class _StubIntakeRouter:
    """Records every envelope ingested + returns canned result."""

    def __init__(self, *, raises: Optional[BaseException] = None):
        self.ingested: list = []
        self.raises = raises

    async def ingest(self, envelope: IntentEnvelope) -> str:
        if self.raises is not None:
            raise self.raises
        self.ingested.append(envelope)
        return "enqueued"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch) -> Iterator[None]:
    """Reset env state before/after each test."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(CORPUS_PATH_ENV_VAR, raising=False)
    monkeypatch.delenv(CORPUS_COUNT_ENV_VAR, raising=False)
    yield


# ===========================================================================
# Closed taxonomy bytes-pinning (drift detector)
# ===========================================================================


def _enum_values_from_ast(class_name: str) -> tuple:
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            values: list = []
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    values.append(stmt.value.value)
            return tuple(values)
    raise AssertionError(f"class {class_name!r} not found in module AST")


def test_exercise_problem_kind_five_values_pinned():
    expected = (
        "off_by_one",
        "logic_inversion",
        "missing_null_check",
        "type_mismatch",
        "dict_keyerror",
    )
    actual = _enum_values_from_ast("ExerciseProblemKind")
    assert actual == expected, (
        f"ExerciseProblemKind drift: expected {expected}, got {actual}"
    )
    assert {m.value for m in ExerciseProblemKind} == set(expected)


def test_exercise_injection_verdict_five_values_pinned():
    expected = (
        "injected",
        "skipped_disabled",
        "skipped_no_corpus",
        "failed_load",
        "failed_inject",
    )
    actual = _enum_values_from_ast("ExerciseInjectionVerdict")
    assert actual == expected
    assert {m.value for m in ExerciseInjectionVerdict} == set(expected)


# ===========================================================================
# Schema version + canonical source token
# ===========================================================================


def test_schema_version_constant():
    assert L2_EXERCISE_SEED_SCHEMA_VERSION == "l2_exercise_seed.v1"


def test_cadence_synthetic_source_matches_canonical_whitelist():
    """The CADENCE_SYNTHETIC_SOURCE constant in this module MUST
    equal the canonical entry in
    intake.intent_envelope._VALID_SOURCES.  Drift here means our
    envelopes would fail validation on the canonical builder."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        _VALID_SOURCES,
    )
    assert CADENCE_SYNTHETIC_SOURCE == "cadence_synthetic"
    assert CADENCE_SYNTHETIC_SOURCE in _VALID_SOURCES, (
        f"CADENCE_SYNTHETIC_SOURCE={CADENCE_SYNTHETIC_SOURCE!r} is "
        f"NOT in the canonical _VALID_SOURCES whitelist; drift would "
        f"cause make_envelope to raise EnvelopeValidationError."
    )


# ===========================================================================
# ExerciseProblem dataclass (frozen + symmetric round-trip per §33.5)
# ===========================================================================


def _sample_problem(**overrides: Any) -> ExerciseProblem:
    base = dict(
        problem_id="sample-001",
        kind=ExerciseProblemKind.OFF_BY_ONE,
        target_file_name="before.py",
        test_file_name="test_before.py",
        before_content="def x():\n    return 0\n",
        test_content="def test_x():\n    assert x() == 1\n",
        manifest_metadata={"id": "sample-001", "kind": "off_by_one"},
    )
    base.update(overrides)
    return ExerciseProblem(**base)


def test_exercise_problem_frozen():
    problem = _sample_problem()
    with pytest.raises((AttributeError, Exception)):
        problem.problem_id = "mutated"  # type: ignore[misc]


def test_exercise_problem_round_trip():
    original = _sample_problem(
        manifest_metadata={
            "id": "rt-001",
            "kind": "off_by_one",
            "difficulty": "medium",
            "expected_first_try_fail_rate": 0.6,
        },
    )
    payload = original.to_dict()
    restored = ExerciseProblem.from_dict(payload)
    assert restored == original
    assert payload["schema_version"] == L2_EXERCISE_SEED_SCHEMA_VERSION
    assert payload["kind"] == "off_by_one"


# ===========================================================================
# load_exercise_problem — every malformed-input class returns None
# ===========================================================================


def test_load_returns_problem_for_well_formed_fixture(tmp_path):
    problem_dir = _write_problem_dir(tmp_path)
    problem = load_exercise_problem(problem_dir)
    assert problem is not None
    assert problem.problem_id == "problem_test_001"
    assert problem.kind == ExerciseProblemKind.OFF_BY_ONE
    assert "def nth(lst, n):" in problem.before_content
    assert "def test_nth_one_indexed" in problem.test_content


def test_load_returns_none_for_missing_directory(tmp_path):
    assert load_exercise_problem(tmp_path / "does-not-exist") is None


def test_load_returns_none_for_missing_manifest(tmp_path):
    pd = tmp_path / "no_manifest"
    pd.mkdir()
    (pd / "before.py").write_text("x = 1\n")
    (pd / "test_before.py").write_text("def test(): assert x == 1\n")
    assert load_exercise_problem(pd) is None


def test_load_returns_none_for_malformed_json(tmp_path):
    pd = tmp_path / "bad_json"
    pd.mkdir()
    (pd / "manifest.json").write_text("{not valid json")
    (pd / "before.py").write_text("x\n")
    (pd / "test_before.py").write_text("x\n")
    assert load_exercise_problem(pd) is None


def test_load_returns_none_for_unknown_kind(tmp_path):
    _write_problem_dir(tmp_path, kind="not_a_real_kind")
    assert load_exercise_problem(tmp_path / "problem_test_001") is None


def test_load_returns_none_for_missing_target_file(tmp_path):
    pd = tmp_path / "missing_target"
    pd.mkdir()
    (pd / "manifest.json").write_text(json.dumps({
        "id": "missing_target", "kind": "off_by_one",
    }))
    # no before.py
    (pd / "test_before.py").write_text("x\n")
    assert load_exercise_problem(pd) is None


def test_load_returns_none_for_missing_test_file(tmp_path):
    pd = tmp_path / "missing_test"
    pd.mkdir()
    (pd / "manifest.json").write_text(json.dumps({
        "id": "missing_test", "kind": "off_by_one",
    }))
    (pd / "before.py").write_text("x\n")
    # no test_before.py
    assert load_exercise_problem(pd) is None


def test_load_returns_none_for_non_dict_manifest(tmp_path):
    pd = tmp_path / "list_manifest"
    pd.mkdir()
    (pd / "manifest.json").write_text("[1, 2, 3]")
    (pd / "before.py").write_text("x\n")
    (pd / "test_before.py").write_text("x\n")
    assert load_exercise_problem(pd) is None


def test_load_preserves_arbitrary_manifest_metadata(tmp_path):
    _write_problem_dir(
        tmp_path,
        problem_id="rich_meta",
        extra_metadata={
            "difficulty": "hard",
            "expected_first_try_fail_rate": 0.75,
            "tags": ["sequence", "indexing"],
        },
    )
    problem = load_exercise_problem(tmp_path / "rich_meta")
    assert problem is not None
    assert problem.manifest_metadata["difficulty"] == "hard"
    assert problem.manifest_metadata["expected_first_try_fail_rate"] == 0.75
    assert problem.manifest_metadata["tags"] == ["sequence", "indexing"]


# ===========================================================================
# list_corpus_problems — sorted + skips underscore-prefixed
# ===========================================================================


def test_list_returns_empty_for_missing_dir(tmp_path):
    assert list_corpus_problems(tmp_path / "no-such-dir") == []


def test_list_returns_empty_for_empty_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    assert list_corpus_problems(tmp_path / "empty") == []


def test_list_skips_underscore_prefixed(tmp_path):
    _write_problem_dir(tmp_path, problem_id="problem_001")
    _write_problem_dir(tmp_path, problem_id="problem_002")
    # Convention: underscore-prefixed are private
    (tmp_path / "_archive").mkdir()
    (tmp_path / "_private").mkdir()
    listed = list_corpus_problems(tmp_path)
    names = [p.name for p in listed]
    assert names == ["problem_001", "problem_002"]


def test_list_sorted_deterministic(tmp_path):
    _write_problem_dir(tmp_path, problem_id="zebra")
    _write_problem_dir(tmp_path, problem_id="apple")
    _write_problem_dir(tmp_path, problem_id="mango")
    listed = list_corpus_problems(tmp_path)
    assert [p.name for p in listed] == ["apple", "mango", "zebra"]


# ===========================================================================
# setup_exercise_worktree — composes canonical WorktreeManager
# ===========================================================================


def test_setup_writes_files_into_worktree(tmp_path):
    wm = _StubWorktreeManager(tmp_path)
    problem = _sample_problem(
        problem_id="setup-001",
        before_content="X = 1\n",
        test_content="def test_x(): assert X == 1\n",
    )
    result = asyncio.run(setup_exercise_worktree(problem, wm))
    assert result is not None
    assert (result / "before.py").read_text() == "X = 1\n"
    assert (result / "test_before.py").read_text() == (
        "def test_x(): assert X == 1\n"
    )
    # Verifies canonical WorktreeManager.create composed correctly
    assert wm.created == ["ouroboros/l2-exercise/setup-001"]


def test_setup_returns_none_on_worktree_create_failure(tmp_path):
    wm = _StubWorktreeManager(tmp_path, fail_create=True)
    problem = _sample_problem()
    result = asyncio.run(setup_exercise_worktree(problem, wm))
    assert result is None


# ===========================================================================
# build_exercise_intent — composes canonical make_envelope
# ===========================================================================


def test_build_intent_uses_canonical_make_envelope(tmp_path):
    """The envelope's schema_version matches the canonical
    IntentEnvelope SCHEMA_VERSION — proves we composed make_envelope
    (which stamps it) rather than constructing IntentEnvelope directly
    (which would require us to duplicate that constant)."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        SCHEMA_VERSION,
    )
    problem = _sample_problem(problem_id="env-001")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    envelope = build_exercise_intent(problem, worktree, repo_root="repo")
    assert envelope.schema_version == SCHEMA_VERSION


def test_build_intent_source_is_canonical_synthetic_token(tmp_path):
    problem = _sample_problem(problem_id="env-002")
    envelope = build_exercise_intent(problem, tmp_path, repo_root="r")
    assert envelope.source == CADENCE_SYNTHETIC_SOURCE
    assert envelope.source == "cadence_synthetic"


def test_build_intent_target_files_contains_target_file_name(tmp_path):
    problem = _sample_problem(
        problem_id="env-003",
        target_file_name="src/buggy.py",
    )
    envelope = build_exercise_intent(problem, tmp_path, repo_root="r")
    assert envelope.target_files == ("src/buggy.py",)


def test_build_intent_urgency_is_low(tmp_path):
    """``urgency=low`` routes via BACKGROUND ProviderRoute —
    never burns Claude budget on cadence injection."""
    problem = _sample_problem()
    envelope = build_exercise_intent(problem, tmp_path, repo_root="r")
    assert envelope.urgency == "low"


def test_build_intent_evidence_carries_l2_exercise_marker(tmp_path):
    problem = _sample_problem(
        problem_id="env-004",
        kind=ExerciseProblemKind.LOGIC_INVERSION,
    )
    envelope = build_exercise_intent(problem, tmp_path, repo_root="r")
    ev = envelope.evidence
    assert ev["category"] == "l2_exercise_corpus"
    assert ev["problem_id"] == "env-004"
    assert ev["kind"] == "logic_inversion"
    assert "worktree_path" in ev
    assert ev["schema_version"] == L2_EXERCISE_SEED_SCHEMA_VERSION


def test_build_intent_description_mentions_problem_id_and_files(tmp_path):
    problem = _sample_problem(
        problem_id="env-005",
        target_file_name="x.py",
        test_file_name="test_x.py",
    )
    envelope = build_exercise_intent(problem, tmp_path, repo_root="r")
    assert "env-005" in envelope.description
    assert "x.py" in envelope.description
    assert "test_x.py" in envelope.description


# ===========================================================================
# Env loaders (defaults / clamping / garbage-tolerant)
# ===========================================================================


def test_corpus_enabled_default_false():
    assert corpus_enabled() is False


def test_corpus_enabled_respects_env(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    assert corpus_enabled() is True
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    assert corpus_enabled() is False
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "0")
    assert corpus_enabled() is False


def test_corpus_count_default_one():
    assert corpus_count() == 1


def test_corpus_count_clamps(monkeypatch):
    monkeypatch.setenv(CORPUS_COUNT_ENV_VAR, "999")
    assert corpus_count() == 5  # ceiling
    monkeypatch.setenv(CORPUS_COUNT_ENV_VAR, "0")
    assert corpus_count() == 1  # floor


def test_corpus_count_garbage_falls_back(monkeypatch):
    monkeypatch.setenv(CORPUS_COUNT_ENV_VAR, "elephant")
    assert corpus_count() == 1


def test_corpus_path_default(monkeypatch):
    monkeypatch.delenv(CORPUS_PATH_ENV_VAR, raising=False)
    assert (
        str(corpus_path())
        == "tests/governance/fixtures/l2_exercise_corpus"
    )


def test_corpus_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(CORPUS_PATH_ENV_VAR, str(tmp_path / "custom"))
    assert corpus_path() == tmp_path / "custom"


# ===========================================================================
# maybe_inject_exercise_at_boot — 4-stage orchestration
# ===========================================================================


def test_inject_master_off_returns_skipped_disabled(tmp_path):
    """Master flag default-FALSE → boot hook short-circuits before
    any fixture I/O."""
    wm = _StubWorktreeManager(tmp_path)
    router = _StubIntakeRouter()
    verdict = asyncio.run(maybe_inject_exercise_at_boot(
        router, worktree_manager=wm,
    ))
    assert verdict == ExerciseInjectionVerdict.SKIPPED_DISABLED
    assert router.ingested == []
    assert wm.created == []


def test_inject_empty_corpus_returns_skipped_no_corpus(
    tmp_path, monkeypatch,
):
    """Master flag ON but corpus dir is empty → SKIPPED_NO_CORPUS."""
    empty = tmp_path / "empty_corpus"
    empty.mkdir()
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(CORPUS_PATH_ENV_VAR, str(empty))
    wm = _StubWorktreeManager(tmp_path)
    router = _StubIntakeRouter()
    verdict = asyncio.run(maybe_inject_exercise_at_boot(
        router, worktree_manager=wm,
    ))
    assert verdict == ExerciseInjectionVerdict.SKIPPED_NO_CORPUS


def test_inject_missing_corpus_dir_returns_skipped_no_corpus(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(
        CORPUS_PATH_ENV_VAR, str(tmp_path / "definitely-not-here"),
    )
    wm = _StubWorktreeManager(tmp_path)
    router = _StubIntakeRouter()
    verdict = asyncio.run(maybe_inject_exercise_at_boot(
        router, worktree_manager=wm,
    ))
    assert verdict == ExerciseInjectionVerdict.SKIPPED_NO_CORPUS


def test_inject_success_returns_injected(tmp_path, monkeypatch):
    """Happy path — corpus has one problem, master flag ON →
    INJECTED, envelope ingested by router."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_problem_dir(corpus, problem_id="happy_001")
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(CORPUS_PATH_ENV_VAR, str(corpus))
    wm_base = tmp_path / "worktrees"
    wm_base.mkdir()
    wm = _StubWorktreeManager(wm_base)
    router = _StubIntakeRouter()
    verdict = asyncio.run(maybe_inject_exercise_at_boot(
        router, worktree_manager=wm, repo_root="my-repo",
    ))
    assert verdict == ExerciseInjectionVerdict.INJECTED
    assert len(router.ingested) == 1
    envelope = router.ingested[0]
    assert envelope.source == "cadence_synthetic"
    assert envelope.evidence["problem_id"] == "happy_001"
    # Worktree was created
    assert wm.created == ["ouroboros/l2-exercise/happy_001"]


def test_inject_worktree_failure_returns_failed_inject(
    tmp_path, monkeypatch,
):
    """Corpus has a problem, master flag ON, but worktree manager
    fails → FAILED_INJECT (problem loaded successfully but couldn't
    be injected because worktree creation failed)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_problem_dir(corpus, problem_id="wt_fail_001")
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(CORPUS_PATH_ENV_VAR, str(corpus))
    wm = _StubWorktreeManager(tmp_path, fail_create=True)
    router = _StubIntakeRouter()
    verdict = asyncio.run(maybe_inject_exercise_at_boot(
        router, worktree_manager=wm,
    ))
    assert verdict == ExerciseInjectionVerdict.FAILED_INJECT
    assert router.ingested == []


def test_inject_only_malformed_problems_returns_failed_load(
    tmp_path, monkeypatch,
):
    """Corpus exists but every problem subdir is malformed (no
    manifest) → FAILED_LOAD."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    # Two malformed problems (no manifest.json)
    (corpus / "malformed_001").mkdir()
    (corpus / "malformed_002").mkdir()
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(CORPUS_PATH_ENV_VAR, str(corpus))
    wm = _StubWorktreeManager(tmp_path)
    router = _StubIntakeRouter()
    verdict = asyncio.run(maybe_inject_exercise_at_boot(
        router, worktree_manager=wm,
    ))
    assert verdict == ExerciseInjectionVerdict.FAILED_LOAD


def test_inject_respects_corpus_count(tmp_path, monkeypatch):
    """count env var clamps the number of problems injected per boot."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_problem_dir(corpus, problem_id="p_001")
    _write_problem_dir(corpus, problem_id="p_002")
    _write_problem_dir(corpus, problem_id="p_003")
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(CORPUS_PATH_ENV_VAR, str(corpus))
    monkeypatch.setenv(CORPUS_COUNT_ENV_VAR, "2")
    wm = _StubWorktreeManager(tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    router = _StubIntakeRouter()
    verdict = asyncio.run(maybe_inject_exercise_at_boot(
        router, worktree_manager=wm,
    ))
    assert verdict == ExerciseInjectionVerdict.INJECTED
    assert len(router.ingested) == 2  # clamped to count=2


def test_inject_cancellation_propagates(tmp_path, monkeypatch):
    """CancelledError MUST propagate — orchestrator POSTMORTEM contract."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_problem_dir(corpus, problem_id="cancel_001")
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(CORPUS_PATH_ENV_VAR, str(corpus))
    wm = _StubWorktreeManager(tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    router = _StubIntakeRouter(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(maybe_inject_exercise_at_boot(
            router, worktree_manager=wm,
        ))


def test_inject_router_exception_swallowed_returns_failed_inject(
    tmp_path, monkeypatch,
):
    """Non-Cancel exception in router.ingest is swallowed; if ALL
    ingests fail, returns FAILED_INJECT (not raised)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_problem_dir(corpus, problem_id="router_fail_001")
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(CORPUS_PATH_ENV_VAR, str(corpus))
    wm = _StubWorktreeManager(tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    router = _StubIntakeRouter(raises=RuntimeError("router broke"))
    verdict = asyncio.run(maybe_inject_exercise_at_boot(
        router, worktree_manager=wm,
    ))
    assert verdict == ExerciseInjectionVerdict.FAILED_INJECT


# ===========================================================================
# AST composition pins (single-source-of-truth invariants)
# ===========================================================================


def _module_imports() -> list:
    out: list = []
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def test_composition_pin_make_envelope_imported():
    """The module composes the canonical make_envelope builder — NOT
    a parallel IntentEnvelope(...) constructor.  Drift toward direct
    construction would duplicate schema_version stamping + dedup_key
    logic.  AST-pinned."""
    matches = [
        (m, n) for (m, n) in _module_imports()
        if m.endswith(".intent_envelope")
        and "make_envelope" in n
    ]
    assert matches, (
        "l2_exercise_seed.py MUST import make_envelope from "
        "intake.intent_envelope — composition pin"
    )


def test_authority_asymmetry_no_forbidden_imports():
    """l2_exercise_seed is descriptive substrate; it MUST NOT import
    orchestrator / iron_gate / change_engine / etc.  §1 Boundary."""
    forbidden = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "policy_engine",
        "risk_tier",
        "repair_engine",
    )
    imports = _module_imports()
    for f in forbidden:
        for (mod, _names) in imports:
            assert f not in mod, (
                f"l2_exercise_seed.py MUST NOT import {f!r} — found "
                f"in {mod!r}. §1 Boundary violation."
            )


def test_cadence_synthetic_source_used_in_build_intent():
    """The build_exercise_intent function MUST reference the
    canonical CADENCE_SYNTHETIC_SOURCE constant (not a hardcoded
    literal).  Single-source-of-truth for the source token."""
    src = inspect.getsource(build_exercise_intent)
    assert "CADENCE_SYNTHETIC_SOURCE" in src, (
        "build_exercise_intent MUST use CADENCE_SYNTHETIC_SOURCE "
        "constant — not a hardcoded 'cadence_synthetic' literal"
    )


def test_no_parallel_intent_envelope_construction():
    """No production code path may construct IntentEnvelope(...)
    directly (must compose make_envelope).  Drift would skip the
    canonical builder's auto-generated IDs + dedup-key logic."""
    forbidden_pattern = "IntentEnvelope("
    # Allow the import statement; forbid construction calls
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "IntentEnvelope", (
                "Direct IntentEnvelope(...) construction is forbidden; "
                "use make_envelope()."
            )


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by §33.3 walker)
# ===========================================================================


@pytest.fixture
def _fresh_registry() -> FlagRegistry:
    return FlagRegistry()


def test_register_flags_installs_three_specs(_fresh_registry):
    count = register_flags(_fresh_registry)
    assert count == 3


def test_master_flag_spec_shape(_fresh_registry):
    register_flags(_fresh_registry)
    spec = _fresh_registry.get_spec(MASTER_FLAG_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.BOOL
    assert spec.default is False  # §33.1 default-FALSE
    assert spec.category == Category.SAFETY
    assert "l2_exercise_seed" in spec.source_file


def test_path_flag_spec_shape(_fresh_registry):
    register_flags(_fresh_registry)
    spec = _fresh_registry.get_spec(CORPUS_PATH_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.STR
    assert spec.default == "tests/governance/fixtures/l2_exercise_corpus"
    assert spec.category == Category.INTEGRATION


def test_count_flag_spec_shape(_fresh_registry):
    register_flags(_fresh_registry)
    spec = _fresh_registry.get_spec(CORPUS_COUNT_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.INT
    assert spec.default == 1
    assert spec.category == Category.CAPACITY


def test_register_flags_idempotent(_fresh_registry):
    register_flags(_fresh_registry)
    register_flags(_fresh_registry)
    register_flags(_fresh_registry)
    specs = [
        _fresh_registry.get_spec(MASTER_FLAG_ENV_VAR),
        _fresh_registry.get_spec(CORPUS_PATH_ENV_VAR),
        _fresh_registry.get_spec(CORPUS_COUNT_ENV_VAR),
    ]
    assert all(s is not None for s in specs)


def test_register_flags_never_raises_on_malformed_registry():
    class _Broken:
        def register(self, _spec):
            raise RuntimeError("broken")
    count = register_flags(_Broken())
    assert count == 0


def test_auto_discovery_picks_up_specs():
    """The canonical §33.3 walker MUST discover l2_exercise_seed's
    register_flags() — same auto-discovery used by every other
    governance substrate."""
    from backend.core.ouroboros.governance.flag_registry import (
        ensure_seeded, reset_default_registry,
    )
    reset_default_registry()
    try:
        registry = ensure_seeded()
        for env_var in (
            MASTER_FLAG_ENV_VAR,
            CORPUS_PATH_ENV_VAR,
            CORPUS_COUNT_ENV_VAR,
        ):
            spec = registry.get_spec(env_var)
            assert spec is not None, (
                f"{env_var} MUST be auto-discovered via canonical walker"
            )
    finally:
        reset_default_registry()
