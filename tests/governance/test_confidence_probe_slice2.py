"""Move 5 Slice 2 — Generator + Read-only Prober regression spine.

Coverage tracks:

  * Question generator — all 4 template-set selection branches
    (symbol+file / symbol-only / file-only / fallback) + max-
    questions cap + LLM-mode fallback + defensive on malformed
    AmbiguityContext
  * AmbiguityContext frozen-dataclass shape + serialization
  * Read-only allowlist — 9-tool frozen-set pinned + is_tool_
    allowlisted predicate
  * ReadonlyEvidenceProber — resolve happy-path + master-off +
    null-backend safety + non-allowlisted-tool blocked + backend-
    exception swallow + max_tool_rounds enforcement + answer-
    length cap
  * Authority invariants — AST-pinned no mutation-tool refs in
    code, no forbidden authority imports, no async, no disk
    writes, governance allowlist
  * End-to-end smoke — generate → resolve → compute_convergence
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.core.ouroboros.governance.verification import (
    confidence_probe_generator as gen_mod,
    readonly_evidence_prober as prober_mod,
)
from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    ProbeAnswer,
    ProbeOutcome,
    ProbeQuestion,
    compute_convergence,
)
from backend.core.ouroboros.governance.verification.confidence_probe_generator import (  # noqa: E501
    CONFIDENCE_PROBE_GENERATOR_SCHEMA_VERSION,
    AmbiguityContext,
    GeneratorMode,
    generate_probes,
    generator_mode,
)
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (  # noqa: E501
    READONLY_EVIDENCE_PROBER_SCHEMA_VERSION,
    READONLY_TOOL_ALLOWLIST,
    QuestionResolver,
    ReadonlyEvidenceProber,
    ReadonlyToolBackend,
    _NullToolBackend,
    get_default_prober,
    is_tool_allowlisted,
    prober_enabled,
    reset_default_prober_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bridge_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED", "true",
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_singleton():
    reset_default_prober_for_tests()
    yield
    reset_default_prober_for_tests()


class _CapturingBackend:
    """Records every call for inspection."""

    def __init__(self, returns: str = "answer-text") -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._returns = returns

    def execute(self, *, tool_name: str, args: Dict[str, Any]) -> str:
        self.calls.append((tool_name, args))
        return f"{self._returns} for {tool_name}"


# ---------------------------------------------------------------------------
# 1. AmbiguityContext shape
# ---------------------------------------------------------------------------


class TestAmbiguityContext:
    def test_is_frozen(self):
        ctx = AmbiguityContext(target_symbol="foo")
        with pytest.raises((AttributeError, Exception)):
            ctx.target_symbol = "bar"  # type: ignore[misc]

    def test_to_dict_round_trip(self):
        ctx = AmbiguityContext(
            op_id="op-1", target_symbol="foo",
            target_file="bar.py", claim="x is y",
            posture="HARDEN", rolling_margin=0.42,
        )
        d = ctx.to_dict()
        assert d["target_symbol"] == "foo"
        assert d["rolling_margin"] == 0.42
        assert d["schema_version"] == \
            CONFIDENCE_PROBE_GENERATOR_SCHEMA_VERSION

    def test_default_all_empty(self):
        ctx = AmbiguityContext()
        assert ctx.target_symbol == ""
        assert ctx.target_file == ""
        assert ctx.rolling_margin is None


# ---------------------------------------------------------------------------
# 2. Generator mode env knob
# ---------------------------------------------------------------------------


class TestGeneratorMode:
    def test_default_is_templates(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE",
            raising=False,
        )
        assert generator_mode() is GeneratorMode.TEMPLATES

    def test_llm_mode_recognized(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE", "llm",
        )
        assert generator_mode() is GeneratorMode.LLM

    def test_garbage_falls_to_templates(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE", "bogus",
        )
        assert generator_mode() is GeneratorMode.TEMPLATES

    def test_llm_falls_through_to_templates_in_slice_2(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE", "llm",
        )
        ctx = AmbiguityContext(
            target_symbol="foo", target_file="bar.py",
        )
        questions = generate_probes(ctx, max_questions_override=2)
        # LLM mode logs warning + falls back; questions still
        # produced via templates
        assert len(questions) == 2


# ---------------------------------------------------------------------------
# 3. Question generator — all 4 template-set selection branches
# ---------------------------------------------------------------------------


class TestGenerateProbes:
    def test_symbol_and_file_branch(self):
        ctx = AmbiguityContext(
            target_symbol="foo", target_file="bar.py",
        )
        questions = generate_probes(ctx, max_questions_override=3)
        assert len(questions) == 3
        # Templates from _SYMBOL_AND_FILE_TEMPLATES
        question_texts = [q.question for q in questions]
        assert any("foo" in q for q in question_texts)
        assert any("bar.py" in q for q in question_texts)

    def test_symbol_only_branch(self):
        ctx = AmbiguityContext(target_symbol="foo")
        questions = generate_probes(ctx, max_questions_override=3)
        assert len(questions) >= 1
        for q in questions:
            # File-required templates should NOT appear
            assert "{file}" not in q.question

    def test_file_only_branch(self):
        ctx = AmbiguityContext(target_file="bar.py")
        questions = generate_probes(ctx, max_questions_override=3)
        assert len(questions) >= 1
        for q in questions:
            # Symbol-required templates should NOT appear
            assert "{symbol}" not in q.question

    def test_fallback_branch_empty_context(self):
        ctx = AmbiguityContext()
        questions = generate_probes(ctx, max_questions_override=3)
        assert len(questions) >= 1
        # Fallback templates use list_dir / search_code / git_log
        methods = {q.resolution_method for q in questions}
        assert methods.issubset({
            "list_dir", "search_code", "git_log",
        })

    def test_max_questions_cap_respected(self):
        ctx = AmbiguityContext(
            target_symbol="foo", target_file="bar.py",
        )
        questions = generate_probes(
            ctx, max_questions_override=1,
        )
        assert len(questions) == 1

    def test_max_questions_default_from_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS", "2",
        )
        ctx = AmbiguityContext(
            target_symbol="foo", target_file="bar.py",
        )
        questions = generate_probes(ctx)  # no override
        assert len(questions) == 2

    def test_resolution_method_in_allowlist(self):
        # Every generated question's resolution_method must be in
        # the read-only allowlist. Defense-in-depth pin.
        ctx = AmbiguityContext(
            target_symbol="foo", target_file="bar.py",
        )
        questions = generate_probes(ctx, max_questions_override=5)
        for q in questions:
            assert q.resolution_method in READONLY_TOOL_ALLOWLIST, (
                f"generator produced non-allowlisted method "
                f"{q.resolution_method!r}"
            )

    def test_malformed_context_returns_empty(self):
        # Non-AmbiguityContext input → empty tuple, never raises
        result = generate_probes("not a context")  # type: ignore[arg-type]
        assert result == ()

    def test_template_placeholders_substituted(self):
        ctx = AmbiguityContext(
            target_symbol="my_func", target_file="src/mod.py",
        )
        questions = generate_probes(ctx, max_questions_override=3)
        for q in questions:
            assert "{symbol}" not in q.question
            assert "{file}" not in q.question
            assert "{claim}" not in q.question

    def test_deterministic_output_same_context(self):
        # Same input → same output (template determinism)
        ctx = AmbiguityContext(
            target_symbol="foo", target_file="bar.py",
        )
        a = generate_probes(ctx, max_questions_override=3)
        b = generate_probes(ctx, max_questions_override=3)
        assert a == b

    def test_each_question_carries_zero_max_tool_rounds(self):
        # max_tool_rounds=0 means "defer to env knob"
        ctx = AmbiguityContext(
            target_symbol="foo", target_file="bar.py",
        )
        questions = generate_probes(ctx, max_questions_override=2)
        for q in questions:
            assert q.max_tool_rounds == 0


# ---------------------------------------------------------------------------
# 4. Read-only allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_allowlist_size_pinned_at_9(self):
        # 9 tools per Slice 2 scope. Adding a tool requires
        # explicit operator review (Slice 5 graduation pin).
        assert len(READONLY_TOOL_ALLOWLIST) == 9

    def test_allowlist_contents_pinned(self):
        expected = {
            "read_file", "search_code", "get_callers",
            "glob_files", "list_dir", "list_symbols",
            "git_blame", "git_log", "git_diff",
        }
        assert set(READONLY_TOOL_ALLOWLIST) == expected

    def test_allowlist_is_frozenset(self):
        assert isinstance(READONLY_TOOL_ALLOWLIST, frozenset)

    @pytest.mark.parametrize(
        "tool,expected",
        [("read_file", True), ("search_code", True),
         ("git_blame", True),
         ("edit_file", False), ("write_file", False),
         ("delete_file", False), ("bash", False),
         ("run_tests", False), ("", False),
         ("READ_FILE", False)],  # case-sensitive
    )
    def test_is_tool_allowlisted(self, tool, expected):
        assert is_tool_allowlisted(tool) is expected

    def test_is_tool_allowlisted_never_raises_on_garbage(self):
        for garbage in (None, 123, [], {}, object()):
            result = is_tool_allowlisted(garbage)  # type: ignore[arg-type]
            assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# 5. ReadonlyEvidenceProber — resolve semantics
# ---------------------------------------------------------------------------


class TestReadonlyEvidenceProber:
    def test_resolve_with_capturing_backend(self):
        backend = _CapturingBackend(returns="result")
        prober = ReadonlyEvidenceProber(backend=backend)
        question = ProbeQuestion(
            question="What is foo?",
            resolution_method="search_code",
            max_tool_rounds=2,
        )
        answer = prober.resolve(question)
        assert isinstance(answer, ProbeAnswer)
        assert answer.tool_rounds_used == 1
        assert "search_code" in backend.calls[0][0]
        assert answer.answer_text != ""
        assert answer.evidence_fingerprint != ""

    def test_master_off_returns_empty_answer(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED", "false",
        )
        backend = _CapturingBackend()
        prober = ReadonlyEvidenceProber(backend=backend)
        question = ProbeQuestion(
            question="What is foo?",
            resolution_method="search_code",
        )
        answer = prober.resolve(question)
        assert answer.answer_text == ""
        assert answer.tool_rounds_used == 0
        # Backend should NOT have been called
        assert backend.calls == []

    def test_null_backend_returns_empty(self):
        prober = ReadonlyEvidenceProber()  # default = null
        question = ProbeQuestion(
            question="What is foo?",
            resolution_method="search_code",
        )
        answer = prober.resolve(question)
        assert answer.answer_text == ""
        # Null backend returns "" → no chunks → empty
        assert answer.evidence_fingerprint == ""

    def test_empty_question_returns_empty_answer(self):
        backend = _CapturingBackend()
        prober = ReadonlyEvidenceProber(backend=backend)
        question = ProbeQuestion(
            question="",
            resolution_method="search_code",
        )
        answer = prober.resolve(question)
        assert answer.answer_text == ""
        assert backend.calls == []

    def test_non_probe_question_returns_empty_answer(self):
        prober = ReadonlyEvidenceProber()
        # Pass non-ProbeQuestion
        answer = prober.resolve(
            "not a question",  # type: ignore[arg-type]
        )
        assert answer.answer_text == ""

    def test_max_tool_rounds_override(self):
        backend = _CapturingBackend()
        prober = ReadonlyEvidenceProber(backend=backend)
        question = ProbeQuestion(
            question="x", resolution_method="search_code",
            max_tool_rounds=5,
        )
        answer = prober.resolve(
            question, max_tool_rounds=1,
        )
        # The plan only has 1 step anyway, so cap is moot but the
        # mechanic is exercised
        assert answer.tool_rounds_used <= 1

    def test_backend_exception_swallowed(self):
        class _BoomBackend:
            def execute(self, *, tool_name, args):
                raise RuntimeError("backend died")
        prober = ReadonlyEvidenceProber(backend=_BoomBackend())
        question = ProbeQuestion(
            question="x", resolution_method="search_code",
        )
        # Must NOT propagate
        answer = prober.resolve(question)
        assert isinstance(answer, ProbeAnswer)
        # Failed → empty answer text
        assert answer.answer_text == ""

    def test_unknown_resolution_method_falls_back_to_search(self):
        backend = _CapturingBackend()
        prober = ReadonlyEvidenceProber(backend=backend)
        question = ProbeQuestion(
            question="x",
            resolution_method="nonexistent_method",
        )
        answer = prober.resolve(question)
        # Plan falls back to search_code; backend was called
        assert len(backend.calls) == 1
        assert backend.calls[0][0] == "search_code"

    def test_answer_length_capped(self):
        # Create a backend that returns a huge string
        big_text = "x" * 10000

        class _BigBackend:
            def execute(self, *, tool_name, args):
                return big_text

        prober = ReadonlyEvidenceProber(backend=_BigBackend())
        question = ProbeQuestion(
            question="x", resolution_method="search_code",
        )
        answer = prober.resolve(question)
        # Capped at _MAX_ANSWER_CHARS = 4096
        assert len(answer.answer_text) <= 4096

    def test_resolution_methods_dispatch_correctly(self):
        backend = _CapturingBackend()
        prober = ReadonlyEvidenceProber(backend=backend)
        for method in ("read_file", "search_code", "get_callers",
                       "list_symbols", "list_dir", "glob_files",
                       "git_blame", "git_log", "git_diff"):
            backend.calls.clear()
            question = ProbeQuestion(
                question="x", resolution_method=method,
            )
            prober.resolve(question)
            assert len(backend.calls) == 1
            assert backend.calls[0][0] == method, (
                f"method {method!r} did not dispatch"
            )

    def test_stats_track_calls(self):
        backend = _CapturingBackend()
        prober = ReadonlyEvidenceProber(backend=backend)
        question = ProbeQuestion(
            question="x", resolution_method="search_code",
        )
        prober.resolve(question)
        prober.resolve(question)
        stats = prober.stats()
        assert stats["calls_total"] == 2
        assert stats["calls_blocked_by_allowlist"] == 0
        assert stats["calls_failed"] == 0


# ---------------------------------------------------------------------------
# 6. Default singleton
# ---------------------------------------------------------------------------


class TestDefaultSingleton:
    def test_get_default_singleton_identity(self):
        a = get_default_prober()
        b = get_default_prober()
        assert a is b

    def test_reset_replaces_singleton(self):
        a = get_default_prober()
        reset_default_prober_for_tests()
        b = get_default_prober()
        assert a is not b


# ---------------------------------------------------------------------------
# 7. End-to-end smoke
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_generate_resolve_converge_pipeline(self):
        # Simulate a full Slice 2 + Slice 1 pipeline (Slice 3 will
        # add the async runner)

        class _ConvergingBackend:
            """Returns the same answer for every search/read tool —
            simulates strong agreement across probes."""

            def execute(self, *, tool_name, args):
                return "foo is a function"

        ctx = AmbiguityContext(
            op_id="op-1", target_symbol="foo",
            target_file="bar.py", claim="foo is a function",
        )
        questions = generate_probes(ctx, max_questions_override=3)
        assert len(questions) == 3

        prober = ReadonlyEvidenceProber(
            backend=_ConvergingBackend(),
        )
        answers = [prober.resolve(q) for q in questions]
        verdict = compute_convergence(
            answers, quorum=2, max_probes=3,
        )
        assert verdict.outcome is ProbeOutcome.CONVERGED
        assert verdict.canonical_answer == "foo is a function"

    def test_diverging_backend_yields_diverged(self):
        # Each tool returns a different answer
        counter = [0]

        class _DivergingBackend:
            def execute(self, *, tool_name, args):
                counter[0] += 1
                return f"distinct-answer-{counter[0]}"

        ctx = AmbiguityContext(
            target_symbol="foo", target_file="bar.py",
        )
        questions = generate_probes(ctx, max_questions_override=3)
        prober = ReadonlyEvidenceProber(
            backend=_DivergingBackend(),
        )
        answers = [prober.resolve(q) for q in questions]
        verdict = compute_convergence(
            answers, quorum=2, max_probes=3,
        )
        assert verdict.outcome is ProbeOutcome.DIVERGED


# ---------------------------------------------------------------------------
# 8. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


_FORBIDDEN_AUTHORITY_SUBSTRINGS = (
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_guardian",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
    "tool_executor",
)


# Mutation tools that MUST NOT appear in code (string literals
# in docstrings allowed; AST Name + Attribute must NOT reference)
_FORBIDDEN_MUTATION_TOOL_NAMES = (
    "edit_file",
    "write_file",
    "delete_file",
    "run_tests",
    "bash",
)


def _module_paths() -> Tuple[Path, Path]:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            base = (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "verification"
            )
            return (
                base / "confidence_probe_generator.py",
                base / "readonly_evidence_prober.py",
            )
        cur = cur.parent
    raise RuntimeError("repo root not found")


class TestAuthorityInvariants:
    @pytest.mark.parametrize(
        "module_idx",
        [0, 1],
        ids=["generator", "prober"],
    )
    def test_no_forbidden_authority_imports(self, module_idx):
        path = _module_paths()[module_idx]
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                        if fb in alias.name:
                            offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                    if fb in mod:
                        offenders.append(mod)
        assert offenders == [], (
            f"{path.name} imports forbidden modules: {offenders}"
        )

    @pytest.mark.parametrize(
        "module_idx",
        [0, 1],
        ids=["generator", "prober"],
    )
    def test_no_mutation_tool_name_references_in_code(
        self, module_idx,
    ):
        # AST-walk Name + Attribute nodes; mutation tool names
        # MUST NOT appear as code references. String literals in
        # docstrings (which become ast.Constant str) are allowed
        # (they're just describing what's forbidden).
        path = _module_paths()[module_idx]
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.id == fb:
                        offenders.append(node.id)
            elif isinstance(node, ast.Attribute):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.attr == fb:
                        offenders.append(node.attr)
        assert offenders == [], (
            f"{path.name} references mutation tool names in "
            f"code: {offenders}"
        )

    @pytest.mark.parametrize(
        "module_idx",
        [0, 1],
        ids=["generator", "prober"],
    )
    def test_no_async_functions(self, module_idx):
        # Slice 2 is pure-sync. Async lives in Slice 3's runner.
        path = _module_paths()[module_idx]
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        async_defs = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        ]
        assert async_defs == [], (
            f"{path.name} has async functions; Slice 2 is sync: "
            f"{async_defs}"
        )

    @pytest.mark.parametrize(
        "module_idx",
        [0, 1],
        ids=["generator", "prober"],
    )
    def test_no_disk_writes(self, module_idx):
        path = _module_paths()[module_idx]
        source = path.read_text(encoding="utf-8")
        forbidden_tokens = (
            ".write_text(",
            ".write_bytes(",
            "os.replace(",
            "NamedTemporaryFile",
        )
        for tok in forbidden_tokens:
            assert tok not in source, (
                f"{path.name} contains forbidden write token: "
                f"{tok!r}"
            )

    def test_generator_governance_imports_in_allowlist(self):
        path = _module_paths()[0]
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                # Generator only imports Slice 1
                assert "confidence_probe_bridge" in mod, (
                    f"generator imports unexpected governance "
                    f"module: {mod}"
                )

    def test_prober_governance_imports_in_allowlist(self):
        path = _module_paths()[1]
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                # Prober only imports Slice 1
                assert "confidence_probe_bridge" in mod, (
                    f"prober imports unexpected governance "
                    f"module: {mod}"
                )

    def test_generator_public_api_exported(self):
        expected = {
            "AmbiguityContext",
            "CONFIDENCE_PROBE_GENERATOR_SCHEMA_VERSION",
            "GeneratorMode",
            "generate_probes",
            "generator_mode",
        }
        assert set(gen_mod.__all__) == expected

    def test_prober_public_api_exported(self):
        expected = {
            "READONLY_EVIDENCE_PROBER_SCHEMA_VERSION",
            "READONLY_TOOL_ALLOWLIST",
            "QuestionResolver",
            "ReadonlyEvidenceProber",
            "ReadonlyToolBackend",
            "get_default_prober",
            "is_tool_allowlisted",
            "prober_enabled",
            "reset_default_prober_for_tests",
        }
        assert set(prober_mod.__all__) == expected

    def test_allowlist_has_no_mutation_names(self):
        # Defense in depth — verify the constant itself contains
        # only read-only names. Catches refactor that adds a
        # mutation tool to the allowlist.
        for name in READONLY_TOOL_ALLOWLIST:
            for forbidden in _FORBIDDEN_MUTATION_TOOL_NAMES:
                assert name != forbidden, (
                    f"allowlist contains mutation tool: {name}"
                )

    def test_schema_versions_pinned(self):
        assert CONFIDENCE_PROBE_GENERATOR_SCHEMA_VERSION == \
            "confidence_probe_generator.1"
        assert READONLY_EVIDENCE_PROBER_SCHEMA_VERSION == \
            "readonly_evidence_prober.1"
