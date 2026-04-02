# Sub-project C: The Immune System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three deterministic gates to the Ouroboros governance pipeline: duplication detection in VALIDATE, diff-aware similarity check in GATE, and regression threshold enforcement in VERIFY with rollback on failure.

**Architecture:** Three new focused modules (`duplication_checker.py`, `similarity_gate.py`, `verify_gate.py`) wired into the existing orchestrator at precise insertion points. All gates are pure computation — no LLM calls, no subprocess except git/pytest for baselines. Each gate produces explicit phase outcomes, not just logs.

**Tech Stack:** Python 3.12, ast module, hashlib, difflib, tokenize, pytest

**Spec:** `docs/superpowers/specs/2026-04-02-sub-c-immune-system-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/core/ouroboros/governance/duplication_checker.py` | Create | AST fingerprint + Jaccard similarity for VALIDATE |
| `backend/core/ouroboros/governance/similarity_gate.py` | Create | N-gram overlap on added hunks for GATE |
| `backend/core/ouroboros/governance/verify_gate.py` | Create | Threshold enforcement on BenchmarkResult + rollback |
| `backend/core/ouroboros/governance/orchestrator.py` | Modify | Wire all 3 gates at insertion points |
| `tests/governance/test_duplication_checker.py` | Create | Unit tests for duplication guard |
| `tests/governance/test_similarity_gate.py` | Create | Unit tests for similarity gate |
| `tests/governance/test_verify_gate.py` | Create | Unit tests for verify gate + rollback |

---

### Task 1: Duplication checker — unit tests + implementation

**Files:**
- Create: `backend/core/ouroboros/governance/duplication_checker.py`
- Create: `tests/governance/test_duplication_checker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_duplication_checker.py`:

```python
"""Tests for Ouroboros VALIDATE duplication guard."""
import textwrap

import pytest


def test_exact_fingerprint_duplicate():
    """Strict match: copy-pasted function with different name → caught."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    source = textwrap.dedent('''\
        def existing_filter(steps):
            """Filter bad steps."""
            result = []
            for s in steps:
                if s.action != "click":
                    result.append(s)
            return result
    ''')
    # Same body, different name
    candidate = textwrap.dedent('''\
        def existing_filter(steps):
            """Filter bad steps."""
            result = []
            for s in steps:
                if s.action != "click":
                    result.append(s)
            return result

        def new_filter(steps):
            """Filter bad steps."""
            result = []
            for s in steps:
                if s.action != "click":
                    result.append(s)
            return result
    ''')
    result = check_duplication(candidate, source, "module.py")
    assert result is not None
    assert "new_filter" in result
    assert "duplication" in result.lower() or "similar" in result.lower()


def test_fuzzy_jaccard_above_threshold():
    """Near-duplicate: same structure with minor tweaks → caught."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    source = textwrap.dedent('''\
        def process_data(items):
            """Process items."""
            output = []
            for item in items:
                if item.valid:
                    transformed = item.value * 2
                    output.append(transformed)
            return output
    ''')
    # Same structure, slightly different names and one extra line
    candidate = textwrap.dedent('''\
        def process_data(items):
            """Process items."""
            output = []
            for item in items:
                if item.valid:
                    transformed = item.value * 2
                    output.append(transformed)
            return output

        def handle_records(records):
            """Handle records."""
            results = []
            for record in records:
                if record.valid:
                    converted = record.value * 2
                    results.append(converted)
            return results
    ''')
    result = check_duplication(candidate, source, "module.py")
    assert result is not None
    assert "handle_records" in result


def test_fuzzy_jaccard_below_threshold():
    """Legitimately different code → passes."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    source = textwrap.dedent('''\
        def validate_input(data):
            """Check input data."""
            if not isinstance(data, dict):
                raise TypeError("Expected dict")
            return True
    ''')
    candidate = textwrap.dedent('''\
        def validate_input(data):
            """Check input data."""
            if not isinstance(data, dict):
                raise TypeError("Expected dict")
            return True

        def send_notification(user, message):
            """Send a notification to user."""
            import smtplib
            server = smtplib.SMTP("localhost")
            server.sendmail("bot@jarvis", user.email, message)
            server.quit()
            return True
    ''')
    result = check_duplication(candidate, source, "module.py")
    assert result is None


def test_modified_function_not_flagged():
    """Editing an existing function (same name) → not flagged as duplication."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    source = textwrap.dedent('''\
        def process(data):
            return data
    ''')
    candidate = textwrap.dedent('''\
        def process(data):
            if not data:
                return []
            return [x * 2 for x in data]
    ''')
    result = check_duplication(candidate, source, "module.py")
    assert result is None


def test_syntax_error_skips_guard():
    """Unparseable source → skip guard, return None."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    result = check_duplication("def good(): pass", "def broken(:", "module.py")
    assert result is None


def test_non_python_skips_guard():
    """Non-.py files → skip guard, return None."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    result = check_duplication("const x = 1;", "const y = 2;", "file.js")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_duplication_checker.py -v`
Expected: FAIL — `check_duplication` does not exist

- [ ] **Step 3: Implement `duplication_checker.py`**

Create `backend/core/ouroboros/governance/duplication_checker.py`:

```python
"""Ouroboros VALIDATE duplication guard.

Detects when generated code duplicates existing functions/classes in the
target file. Uses AST-based canonical fingerprinting (strict match) and
multiset Jaccard similarity (fuzzy match).

This is a deterministic gate — no LLM calls, pure AST computation.
"""
import ast
import hashlib
import os
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

_JACCARD_THRESHOLD = float(os.environ.get("JARVIS_VALIDATE_DUPLICATION_JACCARD", "0.8"))


def check_duplication(
    candidate_content: str,
    source_content: str,
    file_path: str,
) -> Optional[str]:
    """Check if candidate introduces functions that duplicate existing source.

    Parameters
    ----------
    candidate_content:
        The proposed new file content.
    source_content:
        The original file content before modification.
    file_path:
        Path to the file (used for .py extension check).

    Returns
    -------
    Optional[str]
        Error message if duplication detected, None if clean.
    """
    if not file_path.endswith(".py"):
        return None

    try:
        source_tree = ast.parse(source_content)
    except SyntaxError:
        return None  # Can't protect a broken file

    try:
        candidate_tree = ast.parse(candidate_content)
    except SyntaxError:
        return None  # AST preflight should have caught this

    source_units = _extract_units(source_tree)
    candidate_units = _extract_units(candidate_tree)

    # Only check NEW units (names not in source)
    source_names = {name for name, _ in source_units}
    new_units = [(name, node) for name, node in candidate_units if name not in source_names]

    if not new_units or not source_units:
        return None

    # Build source fingerprints and feature sets
    source_fingerprints: Dict[str, str] = {}
    source_features: Dict[str, Counter] = {}
    for name, node in source_units:
        source_fingerprints[name] = _canonical_fingerprint(node)
        source_features[name] = _extract_features(node)

    # Check each new unit against all source units
    for new_name, new_node in new_units:
        new_fp = _canonical_fingerprint(new_node)
        new_features = _extract_features(new_node)

        # Strict match: exact structural fingerprint
        for src_name, src_fp in source_fingerprints.items():
            if new_fp == src_fp:
                return (
                    f"Duplication detected: new function '{new_name}' is structurally "
                    f"identical to existing '{src_name}'"
                )

        # Fuzzy match: multiset Jaccard similarity
        for src_name, src_feat in source_features.items():
            jaccard = _multiset_jaccard(new_features, src_feat)
            if jaccard > _JACCARD_THRESHOLD:
                return (
                    f"Duplication detected: new function '{new_name}' is structurally "
                    f"similar to existing '{src_name}' (Jaccard: {jaccard:.2f})"
                )

    return None


def _extract_units(tree: ast.AST) -> List[Tuple[str, ast.AST]]:
    """Extract top-level and class-level function/class definitions."""
    units: List[Tuple[str, ast.AST]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            units.append((node.name, node))
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    units.append((f"{node.name}.{item.name}", item))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append((node.name, node))
    return units


class _Normalizer(ast.NodeTransformer):
    """Normalize AST for structural comparison.

    - Replace local variable names with positional placeholders
    - Replace literal values with type-canonical placeholders
    - Keep call structure (normalized)
    """

    def __init__(self) -> None:
        self._var_counter = 0
        self._var_map: Dict[str, str] = {}

    def _get_var(self, name: str) -> str:
        if name not in self._var_map:
            self._var_map[name] = f"_v{self._var_counter}"
            self._var_counter += 1
        return self._var_map[name]

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = self._get_var(node.id)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, str):
            node.value = "_S"
        elif isinstance(node.value, int):
            node.value = 0
        elif isinstance(node.value, float):
            node.value = 0.0
        elif isinstance(node.value, bytes):
            node.value = b""
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        # Keep parameter names as-is (API contract)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        # Keep function name but normalize body
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        self.generic_visit(node)
        return node


def _canonical_fingerprint(node: ast.AST) -> str:
    """Compute a canonical structural fingerprint for an AST node."""
    import copy
    normalized = _Normalizer().visit(copy.deepcopy(node))
    dumped = ast.dump(normalized, include_attributes=False)
    return hashlib.sha256(dumped.encode()).hexdigest()


def _extract_features(node: ast.AST) -> Counter:
    """Extract a multiset of normalized statement types for Jaccard comparison."""
    features: Counter = Counter()
    for child in ast.walk(node):
        # Statement-level features
        if isinstance(child, ast.stmt):
            features[type(child).__name__] += 1
        # Expression-level features for finer granularity
        if isinstance(child, ast.Call):
            features["Call"] += 1
        if isinstance(child, ast.BoolOp):
            features[f"BoolOp_{type(child.op).__name__}"] += 1
        if isinstance(child, ast.Compare):
            ops = "_".join(type(o).__name__ for o in child.ops)
            features[f"Compare_{ops}"] += 1
    return features


def _multiset_jaccard(a: Counter, b: Counter) -> float:
    """Compute Jaccard similarity over multisets (min-count / max-count)."""
    all_keys = set(a) | set(b)
    if not all_keys:
        return 0.0
    intersection = sum(min(a[k], b[k]) for k in all_keys)
    union = sum(max(a[k], b[k]) for k in all_keys)
    return intersection / union if union > 0 else 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_duplication_checker.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/duplication_checker.py tests/governance/test_duplication_checker.py
git commit -m "feat(governance): add duplication checker for VALIDATE phase

AST-based canonical fingerprinting (strict match) and multiset Jaccard
similarity (fuzzy match) to detect when generated code duplicates
existing functions. Deterministic gate, no LLM calls.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Similarity gate — unit tests + implementation

**Files:**
- Create: `backend/core/ouroboros/governance/similarity_gate.py`
- Create: `tests/governance/test_similarity_gate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_similarity_gate.py`:

```python
"""Tests for Ouroboros GATE diff-aware similarity check."""
import textwrap

import pytest


def test_similarity_gate_high_overlap():
    """Added code that mostly copies existing source → escalate."""
    from backend.core.ouroboros.governance.similarity_gate import check_similarity

    source = textwrap.dedent('''\
        def process(items):
            result = []
            for item in items:
                if item.is_valid():
                    transformed = item.transform()
                    result.append(transformed)
            return result
    ''')
    # Candidate adds a near-copy with trivial rename
    candidate = textwrap.dedent('''\
        def process(items):
            result = []
            for item in items:
                if item.is_valid():
                    transformed = item.transform()
                    result.append(transformed)
            return result

        def process_v2(items):
            result = []
            for item in items:
                if item.is_valid():
                    transformed = item.transform()
                    result.append(transformed)
            return result
    ''')
    result = check_similarity(candidate, source)
    assert result is not None
    assert "similarity" in result.lower() or "overlap" in result.lower()


def test_similarity_gate_small_edit():
    """Small legitimate edit in existing function → no escalation."""
    from backend.core.ouroboros.governance.similarity_gate import check_similarity

    source = textwrap.dedent('''\
        def process(items):
            result = []
            for item in items:
                if item.is_valid():
                    result.append(item)
            return result
    ''')
    candidate = textwrap.dedent('''\
        def process(items):
            result = []
            for item in items:
                if item.is_valid():
                    transformed = item.transform()
                    result.append(transformed)
            return result
    ''')
    result = check_similarity(candidate, source)
    assert result is None


def test_similarity_gate_deletion_only():
    """Pure deletion: no added lines → overlap 0 → no escalation."""
    from backend.core.ouroboros.governance.similarity_gate import check_similarity

    source = textwrap.dedent('''\
        def func_a():
            pass

        def func_b():
            pass

        def func_c():
            pass
    ''')
    candidate = textwrap.dedent('''\
        def func_a():
            pass

        def func_c():
            pass
    ''')
    result = check_similarity(candidate, source)
    assert result is None


def test_similarity_gate_entirely_new_code():
    """Entirely new code unrelated to source → no escalation."""
    from backend.core.ouroboros.governance.similarity_gate import check_similarity

    source = textwrap.dedent('''\
        def validate_input(data):
            if not data:
                raise ValueError("empty")
            return True
    ''')
    candidate = textwrap.dedent('''\
        def validate_input(data):
            if not data:
                raise ValueError("empty")
            return True

        def send_email(to, subject, body):
            import smtplib
            msg = f"Subject: {subject}\\n\\n{body}"
            server = smtplib.SMTP("localhost", 587)
            server.starttls()
            server.login("bot", "pass")
            server.sendmail("bot@jarvis", to, msg)
            server.quit()
    ''')
    result = check_similarity(candidate, source)
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_similarity_gate.py -v`
Expected: FAIL — `check_similarity` does not exist

- [ ] **Step 3: Implement `similarity_gate.py`**

Create `backend/core/ouroboros/governance/similarity_gate.py`:

```python
"""Ouroboros GATE diff-aware similarity check.

Detects when added code in a candidate patch has high n-gram overlap
with existing source, suggesting copy-paste with minimal modification.
Escalates to APPROVAL_REQUIRED, does not hard-block.

This is a deterministic gate — no LLM calls, pure text computation.
"""
import io
import os
import tokenize as _tokenize
from difflib import SequenceMatcher
from typing import List, Optional, Set

_SIMILARITY_THRESHOLD = float(os.environ.get("JARVIS_GATE_SIMILARITY_THRESHOLD", "0.7"))
_NGRAM_SIZE = 3
_MIN_ADDED_LINES = 3  # Don't check trivial patches


def check_similarity(
    candidate_content: str,
    source_content: str,
    threshold: Optional[float] = None,
) -> Optional[str]:
    """Check if added code in candidate has high overlap with source.

    Parameters
    ----------
    candidate_content:
        The proposed new file content.
    source_content:
        The original file content.
    threshold:
        Override similarity threshold (default from env).

    Returns
    -------
    Optional[str]
        Reason string if similarity too high, None if acceptable.
    """
    thresh = threshold if threshold is not None else _SIMILARITY_THRESHOLD

    source_lines = _normalize_lines(source_content)
    candidate_lines = _normalize_lines(candidate_content)

    # Find added lines using SequenceMatcher
    added_lines = _extract_added_lines(source_lines, candidate_lines)

    if len(added_lines) < _MIN_ADDED_LINES:
        return None  # Too few added lines to meaningfully check

    # Build n-gram sets
    added_ngrams = _build_ngrams(added_lines, _NGRAM_SIZE)
    source_ngrams = _build_ngrams(source_lines, _NGRAM_SIZE)

    if not added_ngrams:
        return None

    # Overlap: what fraction of added n-grams already exist in source
    overlap = len(added_ngrams & source_ngrams) / len(added_ngrams)

    if overlap > thresh:
        return (
            f"High similarity between added code and existing source "
            f"(overlap: {overlap:.2f}, threshold: {thresh:.2f})"
        )
    return None


def _normalize_lines(content: str) -> List[str]:
    """Normalize content: strip comments, whitespace, blank lines."""
    lines = []
    for line in content.splitlines():
        normalized = _strip_comment(line).strip()
        if normalized:  # Skip blank lines
            lines.append(normalized)
    return lines


def _strip_comment(line: str) -> str:
    """Strip inline Python comments. Uses tokenize for reliability, falls back to simple split."""
    try:
        tokens = list(_tokenize.generate_tokens(io.StringIO(line + "\n").readline))
        result_parts = []
        for tok in tokens:
            if tok.type == _tokenize.COMMENT:
                break
            if tok.type not in (_tokenize.NEWLINE, _tokenize.NL, _tokenize.ENDMARKER):
                result_parts.append(tok.string)
        return " ".join(result_parts)
    except _tokenize.TokenError:
        # Fallback: simple split on #
        return line.split("#")[0]


def _extract_added_lines(source_lines: List[str], candidate_lines: List[str]) -> List[str]:
    """Extract lines present in candidate but not in source."""
    sm = SequenceMatcher(None, source_lines, candidate_lines)
    added: List[str] = []
    for op, _, _, j1, j2 in sm.get_opcodes():
        if op in ("insert", "replace"):
            added.extend(candidate_lines[j1:j2])
    return added


def _build_ngrams(lines: List[str], n: int) -> Set[tuple]:
    """Build a set of n-grams from a list of normalized lines."""
    if len(lines) < n:
        return set()
    return {tuple(lines[i:i + n]) for i in range(len(lines) - n + 1)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_similarity_gate.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/similarity_gate.py tests/governance/test_similarity_gate.py
git commit -m "feat(governance): add diff-aware similarity gate for GATE phase

N-gram overlap on added hunks detects copy-paste with minimal changes.
Escalates to APPROVAL_REQUIRED, does not hard-block. Threshold
configurable via JARVIS_GATE_SIMILARITY_THRESHOLD (default 0.7).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Verify gate — unit tests + implementation

**Files:**
- Create: `backend/core/ouroboros/governance/verify_gate.py`
- Create: `tests/governance/test_verify_gate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_verify_gate.py`:

```python
"""Tests for Ouroboros VERIFY regression gate."""
import pytest
from unittest.mock import MagicMock


def _make_benchmark_result(**overrides):
    """Build a BenchmarkResult with sensible defaults."""
    from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
    defaults = dict(
        pass_rate=1.0,
        lint_violations=0,
        coverage_pct=85.0,
        complexity_delta=0.0,
        patch_hash="abc123",
        quality_score=0.9,
        task_type="code_improvement",
        timed_out=False,
        error=None,
    )
    defaults.update(overrides)
    return BenchmarkResult(**defaults)


def test_verify_all_pass():
    """All metrics within thresholds → None (continue to COMPLETE)."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result()
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is None


def test_verify_pass_rate_failure():
    """pass_rate < 1.0 → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(pass_rate=0.85)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "pass_rate" in error


def test_verify_coverage_regression():
    """coverage drops > 5% from baseline → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(coverage_pct=75.0)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "coverage" in error.lower()


def test_verify_coverage_no_baseline():
    """No baseline coverage → skip coverage check, pass."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(coverage_pct=10.0)
    error = enforce_verify_thresholds(result, baseline_coverage=None)
    assert error is None


def test_verify_complexity_spike():
    """complexity_delta > 2.0 → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(complexity_delta=3.5)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "complexity" in error.lower()


def test_verify_lint_cap():
    """lint_violations > 5 → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(lint_violations=8)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "lint" in error.lower()


def test_verify_timed_out():
    """timed_out=True → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(timed_out=True)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "timed" in error.lower()


def test_verify_error_set():
    """error field set → error string."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(error="pytest crashed")
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is not None
    assert "error" in error.lower() or "pytest" in error.lower()


def test_verify_zero_tests_passes():
    """0 tests collected (pass_rate=1.0, no regressions) → pass."""
    from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
    result = _make_benchmark_result(pass_rate=1.0)
    error = enforce_verify_thresholds(result, baseline_coverage=85.0)
    assert error is None


def test_rollback_restores_files(tmp_path):
    """rollback_files restores from snapshots and deletes new files."""
    from backend.core.ouroboros.governance.verify_gate import rollback_files

    # Existing file modified by patch
    existing = tmp_path / "existing.py"
    existing.write_text("modified content")
    snapshots = {"existing.py": "original content"}

    # New file created by patch
    new_file = tmp_path / "new_module.py"
    new_file.write_text("new code")

    target_files = ["existing.py", "new_module.py"]

    rollback_files(
        pre_apply_snapshots=snapshots,
        target_files=target_files,
        repo_root=tmp_path,
    )

    assert existing.read_text() == "original content"
    assert not new_file.exists(), "New file should be deleted on rollback"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_verify_gate.py -v`
Expected: FAIL — `enforce_verify_thresholds` does not exist

- [ ] **Step 3: Implement `verify_gate.py`**

Create `backend/core/ouroboros/governance/verify_gate.py`:

```python
"""Ouroboros VERIFY regression gate.

Enforces thresholds on PatchBenchmarker metrics and provides file rollback
when thresholds are violated. All thresholds are env-driven.

This is a deterministic gate — no LLM calls, pure threshold comparison.
"""
import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Env-driven thresholds
_MIN_PASS_RATE = float(os.environ.get("JARVIS_VERIFY_MIN_PASS_RATE", "1.0"))
_COVERAGE_DROP_MAX = float(os.environ.get("JARVIS_VERIFY_COVERAGE_DROP_MAX", "5.0"))
_MAX_COMPLEXITY_DELTA = float(os.environ.get("JARVIS_VERIFY_MAX_COMPLEXITY_DELTA", "2.0"))
_MAX_LINT_VIOLATIONS = int(os.environ.get("JARVIS_VERIFY_MAX_LINT_VIOLATIONS", "5"))


def enforce_verify_thresholds(
    result: "BenchmarkResult",
    baseline_coverage: Optional[float] = None,
) -> Optional[str]:
    """Check BenchmarkResult against regression thresholds.

    Parameters
    ----------
    result:
        BenchmarkResult from PatchBenchmarker.
    baseline_coverage:
        Pre-APPLY coverage percentage (None = skip coverage check).

    Returns
    -------
    Optional[str]
        Error reason string if any threshold violated, None if all pass.
    """
    # Hard failures first
    if result.error is not None:
        return f"Benchmark error: {result.error}"

    if result.timed_out:
        return "Benchmark timed out — metrics unreliable"

    # Metric thresholds
    if result.pass_rate < _MIN_PASS_RATE:
        return (
            f"Test regression: pass_rate={result.pass_rate:.2f} "
            f"< threshold={_MIN_PASS_RATE:.2f}"
        )

    if baseline_coverage is not None:
        min_coverage = baseline_coverage - _COVERAGE_DROP_MAX
        if result.coverage_pct < min_coverage:
            return (
                f"Coverage regression: coverage={result.coverage_pct:.1f}% "
                f"< baseline={baseline_coverage:.1f}% - {_COVERAGE_DROP_MAX:.1f}% "
                f"(min={min_coverage:.1f}%)"
            )

    if result.complexity_delta > _MAX_COMPLEXITY_DELTA:
        return (
            f"Complexity spike: delta={result.complexity_delta:.1f} "
            f"> threshold={_MAX_COMPLEXITY_DELTA:.1f}"
        )

    if result.lint_violations > _MAX_LINT_VIOLATIONS:
        return (
            f"Lint violations: {result.lint_violations} "
            f"> threshold={_MAX_LINT_VIOLATIONS}"
        )

    return None


def rollback_files(
    pre_apply_snapshots: Dict[str, str],
    target_files: List[str],
    repo_root: Path,
) -> None:
    """Restore files from pre-apply snapshots and delete new files.

    Parameters
    ----------
    pre_apply_snapshots:
        Dict of {relative_path: original_content} captured before APPLY.
    target_files:
        List of relative paths the patch targeted.
    repo_root:
        Repository root for path resolution.
    """
    # Restore existing files from snapshots
    for rel_path, original_content in pre_apply_snapshots.items():
        if rel_path.startswith("_"):
            continue  # Skip metadata keys like "_coverage_baseline"
        abs_path = repo_root / rel_path
        try:
            abs_path.write_text(original_content, encoding="utf-8")
            # Verify restoration
            restored = abs_path.read_text(encoding="utf-8")
            restored_hash = hashlib.sha256(restored.encode()).hexdigest()
            expected_hash = hashlib.sha256(original_content.encode()).hexdigest()
            if restored_hash != expected_hash:
                logger.error(
                    "[VerifyGate] Rollback verification failed for %s: "
                    "expected %s, got %s",
                    rel_path, expected_hash[:12], restored_hash[:12],
                )
        except OSError as exc:
            logger.error("[VerifyGate] Failed to restore %s: %s", rel_path, exc)

    # Delete new files (not in snapshots)
    for rel_path in target_files:
        if rel_path not in pre_apply_snapshots and not rel_path.startswith("_"):
            abs_path = repo_root / rel_path
            if abs_path.exists():
                try:
                    abs_path.unlink()
                    logger.info("[VerifyGate] Deleted new file on rollback: %s", rel_path)
                except OSError as exc:
                    logger.error("[VerifyGate] Failed to delete %s: %s", rel_path, exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_verify_gate.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/verify_gate.py tests/governance/test_verify_gate.py
git commit -m "feat(governance): add verify gate for regression threshold enforcement

Enforces pass_rate, coverage, complexity, and lint thresholds on
PatchBenchmarker results. Provides file rollback (restore snapshots +
delete new files) when thresholds are violated. All env-driven.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire duplication checker into VALIDATE

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (~line 2354)

- [ ] **Step 1: Wire the duplication check**

In `backend/core/ouroboros/governance/orchestrator.py`, find the `_run_validation` method. After the AST preflight block (which ends around line 2353 with the `return ValidationResult(... failure_class="build" ...)`) and BEFORE the extension check (line 2355: `_RUNNABLE_EXTENSIONS = ...`), insert:

```python
        # Step 1b: Duplication guard — check for structural duplication (Python only)
        if target_file_str.endswith(".py"):
            try:
                from backend.core.ouroboros.governance.duplication_checker import check_duplication
                _source_content = ""
                _src_path = Path(target_file_str)
                if not _src_path.is_absolute():
                    _src_path = self._config.project_root / _src_path
                if _src_path.exists():
                    _source_content = _src_path.read_text(encoding="utf-8", errors="replace")
                if _source_content:
                    _dup_error = check_duplication(content, _source_content, target_file_str)
                    if _dup_error is not None:
                        return ValidationResult(
                            passed=False,
                            best_candidate=None,
                            validation_duration_s=0.0,
                            error=_dup_error,
                            failure_class="duplication",
                            short_summary=_dup_error[:300],
                            adapter_names_run=(),
                        )
            except Exception as exc:
                logger.debug("[Orchestrator] Duplication check skipped: %s", exc)
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/governance/test_duplication_checker.py tests/governance/intake/ -v --timeout=30 2>&1 | tail -15`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py
git commit -m "feat(governance): wire duplication checker into VALIDATE phase

Calls check_duplication() after AST preflight, before sandbox write.
Catches structural duplication with failure_class='duplication'.
Fault-isolated: skips gracefully on any error.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Wire similarity gate into GATE

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (~line 1585)

- [ ] **Step 1: Wire the similarity check**

In `backend/core/ouroboros/governance/orchestrator.py`, find the GATE phase. After the SecurityReviewer block (which ends around line 1584 with `except Exception: logger.debug(...)`) and BEFORE the autonomy tier gate (line 1586: `_frozen_tier = getattr(...)`), insert:

```python
        # ---- Diff-Aware Similarity Gate (Sub-project C) ----
        if best_candidate is not None:
            try:
                from backend.core.ouroboros.governance.similarity_gate import check_similarity
                _src_content = ""
                if ctx.target_files:
                    _src_path = self._config.project_root / ctx.target_files[0]
                    if _src_path.exists():
                        _src_content = _src_path.read_text(encoding="utf-8", errors="replace")
                if _src_content:
                    _sim_reason = check_similarity(best_candidate, _src_content)
                    if _sim_reason is not None:
                        logger.info(
                            "[Orchestrator] GATE similarity escalation: %s [%s]",
                            _sim_reason, ctx.op_id,
                        )
                        if risk_tier is not RiskTier.APPROVAL_REQUIRED:
                            risk_tier = RiskTier.APPROVAL_REQUIRED
            except Exception:
                logger.debug("[Orchestrator] Similarity gate skipped", exc_info=True)
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/governance/test_similarity_gate.py tests/governance/intake/ -v --timeout=30 2>&1 | tail -15`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py
git commit -m "feat(governance): wire similarity gate into GATE phase

Checks n-gram overlap on added hunks. High similarity escalates to
APPROVAL_REQUIRED. Fault-isolated: skips on error.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Wire verify gate into VERIFY phase

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (~lines 1816-1818)

- [ ] **Step 1: Wire the verify gate with rollback**

In `backend/core/ouroboros/governance/orchestrator.py`, find the VERIFY phase. The current code (around lines 1816-1821) is:

```python
        ctx = await self._run_benchmark(ctx, [])
        if _serpent: _serpent.update_phase("COMPLETE")
        ctx = ctx.advance(OperationPhase.COMPLETE, terminal_reason_code="complete")
        self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_apply)
        await self._publish_outcome(ctx, OperationState.APPLIED)
        await self._persist_performance_record(ctx)
```

Replace with:

```python
        ctx = await self._run_benchmark(ctx, [])

        # ---- Verify Gate: enforce regression thresholds (Sub-project C) ----
        _verify_error = None
        try:
            from backend.core.ouroboros.governance.verify_gate import (
                enforce_verify_thresholds,
                rollback_files,
            )
            _br = getattr(ctx, "benchmark_result", None)
            if _br is not None:
                _baseline_cov = None
                _snapshots = getattr(ctx, "pre_apply_snapshots", {})
                if isinstance(_snapshots, dict):
                    _baseline_cov = _snapshots.get("_coverage_baseline")
                _verify_error = enforce_verify_thresholds(_br, baseline_coverage=_baseline_cov)
        except Exception as exc:
            logger.debug("[Orchestrator] Verify gate skipped: %s", exc)

        if _verify_error is not None:
            logger.warning(
                "[Orchestrator] VERIFY regression gate fired: %s [%s]",
                _verify_error, ctx.op_id,
            )
            # Rollback files
            try:
                _snapshots = getattr(ctx, "pre_apply_snapshots", {})
                if _snapshots:
                    rollback_files(
                        pre_apply_snapshots=_snapshots,
                        target_files=list(ctx.target_files),
                        repo_root=self._config.project_root,
                    )
            except Exception as exc:
                logger.error("[Orchestrator] Verify rollback failed: %s", exc)

            if _serpent: _serpent.update_phase("POSTMORTEM")
            ctx = ctx.advance(
                OperationPhase.POSTMORTEM,
                terminal_reason_code="verify_regression",
                rollback_occurred=True,
            )
            await self._record_ledger(
                ctx,
                OperationState.FAILED,
                {"reason": "verify_regression", "detail": _verify_error, "rollback_occurred": True},
            )
            self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_apply, rolled_back=True)
            await self._publish_outcome(ctx, OperationState.FAILED, "verify_regression")
            return ctx

        if _serpent: _serpent.update_phase("COMPLETE")
        ctx = ctx.advance(OperationPhase.COMPLETE, terminal_reason_code="complete")
        self._record_canary_for_ctx(ctx, True, time.monotonic() - _t_apply)
        await self._publish_outcome(ctx, OperationState.APPLIED)
        await self._persist_performance_record(ctx)
```

- [ ] **Step 2: Run all tests**

Run: `python3 -m pytest tests/governance/test_verify_gate.py tests/governance/test_duplication_checker.py tests/governance/test_similarity_gate.py tests/governance/intake/ -v --timeout=30 2>&1 | tail -20`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py
git commit -m "feat(governance): wire verify gate into VERIFY phase with rollback

Enforces regression thresholds on benchmark metrics. On failure:
rolls back files from pre_apply_snapshots, deletes new files, advances
to POSTMORTEM with verify_regression reason code.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Full regression and verify

**Files:** None (verification only)

- [ ] **Step 1: Run all new tests**

Run: `python3 -m pytest tests/governance/test_duplication_checker.py tests/governance/test_similarity_gate.py tests/governance/test_verify_gate.py -v`
Expected: All 20 tests PASS

- [ ] **Step 2: Run governance test suite**

Run: `python3 -m pytest tests/governance/ -v --timeout=30 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 3: Run intake tests (Sub-projects A regression)**

Run: `python3 -m pytest tests/governance/intake/ -v --timeout=30 2>&1 | tail -10`
Expected: 114/114 pass

- [ ] **Step 4: Final commit if fixups needed**

```bash
git add -u
git commit -m "fix(governance): address regression findings from immune system gates

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
