"""Test exemplars — show the model a test that PASSES for a module like this one.

## The failure this addresses

Soak bt-2026-09-20-183259, once the harness was out of the way: asked to write a
test file from scratch for an async FastAPI module, the local 30B went 0 for 5
at nine attempts each, and not at random:

    TypeError: argument of type 'coroutine' is not iterable     (never awaited)
    TypeError: argument of type 'JSONResponse' is not iterable  (`"x" in resp`)
    psutil.NoSuchProcess: process PID not found (pid=5678)      (unmocked I/O)

Each is a CONVENTION it was never shown — how this repository awaits a handler,
reads a response, fakes a process. It landed the two test files whose subjects
were plain synchronous utilities. A model with ~3B active parameters does not
derive ``@pytest.mark.asyncio`` from first principles; it copies structure it
can see. So it is shown some.

## Retrieval

Every ``test_<stem>.py`` whose ``<stem>.py`` resolves (``repo_state.ModuleIndex``)
is a candidate, described by the traits of the module it TESTS — its imported
top-level packages plus structural flags (``async_def``, ``class_def``,
``decorated_def``). A candidate is relevant when its subject resembles ours:
"here is how a module like yours is tested here".

Similarity is Jaccard weighted by inverse document frequency over the catalog,
so sharing ``fastapi`` (rare) counts for far more than sharing ``os`` (in
everything). The weights are computed from the repository, not written down.
There is no list of frameworks in this file, and ``fastapi`` is not special.

One structural rule is not a weight: a subject with ``async def`` requires an
exemplar that itself contains async tests. A synchronous exemplar is precisely
the pattern that produced "coroutine is not iterable".

## Nothing unverified is ever shown

Measured on this repository: 1,250 paired tests, but the async-FastAPI corner
holds FOUR — two under ``archive/deprecated``, one whose subject cannot be
imported. Similarity says nothing about whether a test works, and teaching a
broken pattern with the authority of "this is how we do it here" is worse than
teaching none. So a candidate is RUN first, through the one canonical pytest
spawn site (timeout, own session, descendants reaped), and the verdict is kept
in a registry keyed by the SHA-256 of the test and its subject: each pair is
verified once, and again only when either file changes. Candidates whose
subject runs a program on import, or imports a module that does not exist, are
never run at all.

If nothing relevant passes, nothing is injected. NEVER raises, and never holds
an op longer than its verification budget.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_TEST_EXEMPLAR_INJECTION_ENABLED"
_ENV_VERIFY_MAX = "JARVIS_TEST_EXEMPLAR_VERIFY_MAX"
_ENV_REGISTRY = "JARVIS_TEST_EXEMPLAR_REGISTRY"
_DEFAULT_VERIFY_MAX = 4

FLAG_ASYNC = "flag:async_def"
FLAG_CLASS = "flag:class_def"
FLAG_DECORATED = "flag:decorated_def"


def injection_enabled() -> bool:
    try:
        raw = os.environ.get(_ENV_ENABLED, "true")
        return raw.strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return True


def _verify_max() -> int:
    try:
        value = int(os.environ.get(_ENV_VERIFY_MAX, "") or _DEFAULT_VERIFY_MAX)
        return value if value > 0 else _DEFAULT_VERIFY_MAX
    except (TypeError, ValueError):
        return _DEFAULT_VERIFY_MAX


# ---------------------------------------------------------------------------
# Traits
# ---------------------------------------------------------------------------

_trait_cache: Dict[Tuple[str, int, int], FrozenSet[str]] = {}
_trait_lock = threading.Lock()


def traits_of_source(source: str) -> FrozenSet[str]:
    """Imported top-level packages plus structural flags. Raises on bad syntax."""
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.AsyncFunctionDef):
            found.add(FLAG_ASYNC)
            if node.decorator_list:
                found.add(FLAG_DECORATED)
        elif isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                found.add(FLAG_DECORATED)
        elif isinstance(node, ast.ClassDef):
            found.add(FLAG_CLASS)
    return frozenset(found)


def traits_of(path: Path) -> FrozenSet[str]:
    """:func:`traits_of_source`, content-cached. A parse is a pure function of
    the file, so the cache outlives tree states: after a landing only the files
    that changed are re-read. Unreadable / unparseable -> no traits."""
    try:
        st = path.stat()
        key = (str(path), int(st.st_mtime_ns), int(st.st_size))
        with _trait_lock:
            hit = _trait_cache.get(key)
        if hit is not None:
            return hit
        out = traits_of_source(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return frozenset()
    with _trait_lock:
        if len(_trait_cache) > 16384:
            _trait_cache.clear()
        _trait_cache[key] = out
    return out


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    test: Path
    subject: Path
    subject_traits: FrozenSet[str]
    async_tests: bool
    size: int


@dataclass(frozen=True)
class Catalog:
    candidates: Tuple[Candidate, ...]
    idf: Dict[str, float]

    def weight(self, trait: str) -> float:
        # A trait the catalog has never seen is maximally distinctive.
        return self.idf.get(trait, self.idf.get("", 1.0))


_catalog_lock = threading.Lock()
_catalog_held: Optional[Tuple[str, str, Catalog]] = None


def _is_test_name(name: str) -> bool:
    return name.startswith("test_") and name.endswith(".py")


def _subject_for(test: Path, index) -> Optional[Path]:
    for cand in index.find(test.name[len("test_"):]):
        parts = cand.parts
        if "tests" in parts or "test" in parts or _is_test_name(cand.name):
            continue
        return cand
    return None


def build_catalog(repo_root: Path) -> Catalog:
    """Every resolvable (test, subject) pair under *repo_root*. Synchronous and
    CPU-bound (~5 s cold on this repository): call it off the loop."""
    from backend.core.ouroboros.governance import repo_state  # noqa: PLC0415

    index = repo_state.module_index(Path(repo_root))
    found: List[Candidate] = []
    for name, paths in index._table.items():  # noqa: SLF001 -- same package
        if not _is_test_name(name):
            continue
        for test in paths:
            subject = _subject_for(test, index)
            if subject is None:
                continue
            s_traits = traits_of(subject)
            if not s_traits:
                continue
            try:
                size = test.stat().st_size
            except OSError:
                continue
            found.append(Candidate(
                test=test, subject=subject, subject_traits=s_traits,
                async_tests=FLAG_ASYNC in traits_of(test), size=size,
            ))
    total = max(1, len(found))
    df: Dict[str, int] = {}
    for cand in found:
        for trait in cand.subject_traits:
            df[trait] = df.get(trait, 0) + 1
    idf = {trait: math.log((1 + total) / (1 + n)) + 1.0 for trait, n in df.items()}
    idf[""] = math.log(1 + total) + 1.0
    return Catalog(tuple(found), idf)


def catalog_for(repo_root: Path) -> Catalog:
    """The catalog for the CURRENT tree state; rebuilt only when it moves (and
    then cheaply, because the per-file parses are content-cached)."""
    global _catalog_held
    from backend.core.ouroboros.governance import repo_state  # noqa: PLC0415

    root = str(Path(repo_root).resolve())
    state = repo_state.current_fingerprint(Path(repo_root))
    if state:
        with _catalog_lock:
            held = _catalog_held
        if held is not None and held[0] == root and held[1] == state:
            return held[2]
    built = build_catalog(Path(repo_root))
    if state:
        with _catalog_lock:
            _catalog_held = (root, state, built)
    return built


def similarity(a: FrozenSet[str], b: FrozenSet[str], catalog: Catalog) -> float:
    """IDF-weighted Jaccard in [0, 1]."""
    union = a | b
    if not union:
        return 0.0
    shared = sum(catalog.weight(t) for t in a & b)
    return shared / sum(catalog.weight(t) for t in union)


def rank(subject: Path, catalog: Catalog) -> List[Tuple[float, Candidate]]:
    """Candidates most like *subject* first; smaller files break ties, because
    a short exemplar leaves room for the task and shows one idea at a time."""
    wanted = traits_of(subject)
    if not wanted:
        return []
    here = str(Path(subject).resolve())
    scored = []
    for cand in catalog.candidates:
        if str(cand.subject.resolve()) == here:
            continue  # its own (possibly half-written) test teaches nothing
        if FLAG_ASYNC in wanted and not cand.async_tests:
            continue
        score = similarity(wanted, cand.subject_traits, catalog)
        if score > 0.0:
            scored.append((score, cand))
    scored.sort(key=lambda row: (-row[0], row[1].size, str(row[1].test)))
    return scored


# ---------------------------------------------------------------------------
# Verification registry
# ---------------------------------------------------------------------------


def _registry_path(repo_root: Path) -> Path:
    raw = (os.environ.get(_ENV_REGISTRY, "") or "").strip()
    return Path(raw).expanduser() if raw else Path(repo_root) / ".jarvis" / "test_exemplar_registry.json"


def _pair_digest(cand: Candidate) -> str:
    digest = hashlib.sha256()
    for path in (cand.test, cand.subject):
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


_registry_lock = threading.Lock()


def _registry_load(repo_root: Path) -> Dict[str, bool]:
    try:
        data = json.loads(_registry_path(repo_root).read_text(encoding="utf-8"))
        return {str(k): bool(v) for k, v in dict(data.get("verdicts") or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


def _registry_store(repo_root: Path, verdicts: Dict[str, bool]) -> None:
    try:
        path = _registry_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "verdicts": verdicts}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.debug("[TestExemplar] registry write degraded", exc_info=True)


def _safe_to_run(cand: Candidate, repo_root: Path) -> bool:
    """Never execute a candidate whose subject cannot be imported safely."""
    try:
        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            environment_integrity as ei,
        )
        root = Path(repo_root)
        if ei.import_execution_hazard(cand.subject, root):
            return False
        if ei.missing_first_party_imports(cand.subject, root):
            return False
        return not ei.unresolvable_imports(cand.subject, root)
    except Exception:  # noqa: BLE001
        return False


async def _run_passes(cand: Candidate, repo_root: Path, timeout_s: float) -> bool:
    from backend.core.ouroboros.governance.test_subprocess_helper import (  # noqa: PLC0415
        PYTEST_ISOLATION_ARGS, resolve_python_bin, run_pytest_subprocess,
    )
    # Isolation args FIRST: the nearest pytest.ini may demand plugins this venv
    # does not carry, and pytest exits 4 before collecting anything.
    argv = [
        resolve_python_bin(), "-m", "pytest", *PYTEST_ISOLATION_ARGS,
        "-x", "-q", "-p", "no:cacheprovider", str(cand.test),
    ]
    result = await run_pytest_subprocess(
        argv, cwd=str(repo_root), timeout_s=timeout_s, caller="test_exemplars.verify",
    )
    return bool(result.returncode == 0 and not result.timed_out)


async def verified(
    ranked: Sequence[Tuple[float, Candidate]], repo_root: Path,
) -> Optional[Tuple[float, Candidate]]:
    """The best-ranked candidate that PASSES, running at most
    ``JARVIS_TEST_EXEMPLAR_VERIFY_MAX`` unverified ones to find it."""
    from backend.core.ouroboros.governance.test_timeout_derivation import (  # noqa: PLC0415
        legacy_floor_s,
    )
    with _registry_lock:
        verdicts = _registry_load(repo_root)
    budget = _verify_max()
    dirty = False
    try:
        for score, cand in ranked:
            key = _pair_digest(cand)
            known = verdicts.get(key)
            if known is None:
                if budget <= 0:
                    continue  # keep scanning: a later one may already be verified
                budget -= 1
                known = _safe_to_run(cand, repo_root) and await _run_passes(
                    cand, repo_root, legacy_floor_s(),
                )
                verdicts[key] = bool(known)
                dirty = True
            if known:
                return score, cand
        return None
    finally:
        if dirty:
            with _registry_lock:
                merged = _registry_load(repo_root)
                merged.update(verdicts)
                _registry_store(repo_root, merged)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _span(node: ast.AST) -> Tuple[int, int]:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, int(getattr(node, "end_lineno", node.lineno))


def _shape(node: ast.AST) -> Tuple[bool, bool, bool, bool, bool]:
    """What a test DEMONSTRATES, structurally: is it async, does it take
    fixtures, does it await, does it patch/enter a context, does it assert an
    exception. Two tests of one shape teach the same lesson twice."""
    inner = list(ast.walk(node))
    funcs = [n for n in inner if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    takes_fixtures = any(
        [a.arg for a in f.args.args if a.arg not in ("self", "cls")] for f in funcs
    )
    raises = any(
        isinstance(n, ast.Attribute) and n.attr == "raises" for n in inner
    )
    return (
        any(isinstance(n, ast.AsyncFunctionDef) for n in inner),
        takes_fixtures,
        any(isinstance(n, ast.Await) for n in inner),
        any(isinstance(n, (ast.With, ast.AsyncWith)) for n in inner),
        raises,
    )


def excerpt(test_source: str, budget_tokens: int, *, prefer_async: bool) -> str:
    """The setup (imports, fixtures, helpers) plus ONE test per distinct shape.

    Whole definitions only — half a test is a syntax error the model will
    faithfully reproduce. Async shapes first when that is what is being taught.

    Not "as many tests as fit": the first version packed the budget and handed
    a 3B-active model 18 KB of someone else's assertions to show it how to
    write ``await``. A reference is read for its STRUCTURE, and thirty tests of
    one shape carry the structure of one. The stop condition is therefore
    "every shape has been shown", which is a property of the exemplar rather
    than a number chosen here; the budget is only the ceiling.
    """
    from backend.core.ouroboros.governance.ast_signature_pruner import (  # noqa: PLC0415
        _estimate,
    )
    try:
        tree = ast.parse(test_source)
    except (SyntaxError, ValueError):
        return ""
    lines = test_source.splitlines()

    def text(node: ast.AST) -> str:
        first, last = _span(node)
        return "\n".join(lines[first - 1:last])

    setup: List[str] = []
    tests: List[Tuple[int, str, Tuple[bool, bool, bool, bool, bool]]] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            continue  # module docstring: prose about another module
        is_test = (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")
        ) or (isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
        if not is_test:
            setup.append(text(node))
            continue
        is_async = isinstance(node, ast.AsyncFunctionDef) or (
            isinstance(node, ast.ClassDef)
            and any(isinstance(n, ast.AsyncFunctionDef) for n in node.body)
        )
        tests.append((0 if (is_async and prefer_async) else 1, text(node), _shape(node)))
    head = "\n".join(setup).strip()
    if not tests or _estimate(head) > budget_tokens:
        return ""
    chosen: List[str] = []
    shown = set()
    used = _estimate(head)
    # Stable within a priority: the file's own order is its author's idea of
    # simplest-first, and a shape is represented by its first instance.
    for _prio, body, shape in sorted(tests, key=lambda row: row[0]):
        if shape in shown:
            continue
        cost = _estimate(body)
        if used + cost > budget_tokens:
            continue
        chosen.append(body)
        shown.add(shape)
        used += cost
    if not chosen:
        return ""
    return (head + "\n\n\n" + "\n\n\n".join(chosen)).strip() + "\n"


def _subject_of_op(target_files: Sequence[str], description: str, repo_root: Path) -> Optional[Path]:
    """The module this op is writing tests FOR, or ``None`` if it is not
    writing a new test. Resolved the way the prompt's signature anchor does."""
    root = Path(repo_root)
    writes_new_test = any(
        _is_test_name(Path(str(t)).name) and not (root / str(t)).is_file()
        for t in (target_files or ())
    )
    if not writes_new_test:
        return None
    try:
        from backend.core.ouroboros.governance.ast_signature_anchor import (  # noqa: PLC0415
            collect_anchor_sources,
        )
        for _label, src in collect_anchor_sources(list(target_files or ()), description or "", root):
            path = Path(src)
            if path.suffix == ".py" and not _is_test_name(path.name):
                return path
    except Exception:  # noqa: BLE001
        logger.debug("[TestExemplar] subject resolution degraded", exc_info=True)
    return None


async def exemplar_instruction(
    target_files: Sequence[str], description: str, repo_root: Path, *, op_id: str = "",
) -> str:
    """Everything this module adds before GENERATE, or ``""``. NEVER raises.

    Two parts, each independently optional: a verified-passing reference test
    (:func:`_exemplar_block`), and — for every third-party package the subject
    uses that the reference does NOT cover — that package's contract as read
    from the installed source (:func:`_contract_block`). The second part
    retires itself per package: the day a passing FastAPI test lands here and
    becomes the exemplar, ``fastapi`` is covered and its contract is no longer
    injected.
    """
    try:
        if not injection_enabled():
            return ""
        covered: set = set()
        exemplar = await _exemplar_block(
            target_files, description, repo_root, op_id=op_id, covered=covered,
        )
        contract = await _contract_block(
            target_files, description, repo_root, frozenset(covered), op_id=op_id,
        )
        return exemplar + contract
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("[TestExemplar] instruction degraded", exc_info=True)
        return ""


async def _contract_block(
    target_files: Sequence[str], description: str, repo_root: Path,
    covered: FrozenSet[str], *, op_id: str = "",
) -> str:
    """Installed-library contracts for the subject's UNCOVERED third-party
    imports, rarest first. ``""`` when there are none. NEVER raises."""
    try:
        root = Path(repo_root)
        subject = _subject_of_op(target_files, description, root)
        if subject is None:
            return ""
        from backend.core.ouroboros.governance import library_contract as lc  # noqa: PLC0415
        from backend.core.ouroboros.governance.ast_signature_pruner import (  # noqa: PLC0415
            dependency_budget_tokens,
        )
        wanted = [t for t in traits_of(subject) if not t.startswith("flag:") and t not in covered]
        if not wanted:
            return ""
        catalog = await asyncio.to_thread(catalog_for, root)
        tops = await asyncio.to_thread(
            lambda: sorted(
                (t for t in wanted if lc.third_party_root(t) is not None),
                key=lambda t: (-catalog.weight(t), t),
            )
        )
        if not tops:
            return ""
        source = subject.read_text(encoding="utf-8", errors="replace")
        # Its own budget, like each dependency section the prompt assembler
        # builds: a different KIND of context, not a share of the exemplar's.
        body = await asyncio.to_thread(lc.contract_for, source, tops, dependency_budget_tokens())
        if not body:
            return ""
        logger.info(
            "[TestExemplar] op=%s injected LIBRARY CONTRACT for %s (no verified exemplar covers %s) — %d chars",
            op_id, subject.name, ", ".join(tops), len(body),
        )
        return (
            "\n\n## Library contract — read from the INSTALLED packages, not from memory\n"
            f"Extracted from site-packages for the versions installed here ({', '.join(tops)}). "
            "No passing test in this repository exercises these yet, so this is the "
            "authoritative API surface: construct and inspect these objects ONLY through "
            "what is listed. Note the `instance attributes` lines — they name what an "
            "object exposes that no signature shows (a response's payload is its `body`, "
            "as bytes; it is not iterable and is not a dict).\n"
            f"```python\n{body}\n```\n"
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("[TestExemplar] contract degraded", exc_info=True)
        return ""


async def _exemplar_block(
    target_files: Sequence[str], description: str, repo_root: Path, *,
    op_id: str = "", covered: Optional[set] = None,
) -> str:
    """The verified reference-test block, or ``""``. NEVER raises.

    ``""`` is the answer whenever this cannot help: the op is not writing a new
    test, nothing relevant exists, nothing relevant PASSES, or the excerpt does
    not fit. A missing exemplar costs nothing; a wrong one costs an op.
    """
    try:
        if not injection_enabled():
            return ""
        root = Path(repo_root)
        subject = _subject_of_op(target_files, description, root)
        if subject is None:
            return ""
        from backend.core.ouroboros.governance.ast_signature_pruner import (  # noqa: PLC0415
            dependency_budget_tokens,
        )
        from backend.core.ouroboros.governance.test_timeout_derivation import (  # noqa: PLC0415
            legacy_floor_s,
        )
        catalog = await asyncio.to_thread(catalog_for, root)
        ranked = await asyncio.to_thread(rank, subject, catalog)
        if not ranked:
            return ""
        # One budget for the whole search, so a cold registry cannot hold the
        # op for K x timeout. Whatever was verified before the bell is kept.
        try:
            found = await asyncio.wait_for(verified(ranked, root), timeout=legacy_floor_s())
        except asyncio.TimeoutError:
            logger.info("[TestExemplar] op=%s verification budget exhausted — injecting nothing", op_id)
            return ""
        if found is None:
            logger.info(
                "[TestExemplar] op=%s no VERIFIED exemplar for %s (%d candidate(s) ranked) — injecting nothing",
                op_id, subject.name, len(ranked),
            )
            return ""
        score, cand = found
        wanted = traits_of(subject)
        body = excerpt(
            cand.test.read_text(encoding="utf-8", errors="replace"),
            dependency_budget_tokens(), prefer_async=FLAG_ASYNC in wanted,
        )
        if not body:
            return ""
        # Only NOW: an exemplar that was found but did not fit covers nothing,
        # and must not suppress the library contract that would stand in for it.
        if covered is not None:
            covered.update(cand.subject_traits)
        shared = sorted(
            (t for t in wanted & cand.subject_traits),
            key=lambda t: -catalog.weight(t),
        )[:6]
        shown = ", ".join(t.replace("flag:", "") for t in shared) or "general structure"

        def rel(path: Path) -> str:
            try:
                return path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                return str(path)

        logger.info(
            "[TestExemplar] op=%s injected %s (similarity=%.2f, VERIFIED passing; shares: %s) for subject %s",
            op_id, rel(cand.test), score, shown, rel(subject),
        )
        return (
            "\n\n## Reference test — VERIFIED PASSING in this repository\n"
            f"`{rel(cand.test)}` tests `{rel(cand.subject)}`, which resembles your "
            f"subject ({shown}). It was run and passed.\n"
            "Mirror its STRUCTURE: how it imports the module under test, how it awaits "
            "coroutines, how it builds fixtures and replaces real I/O with mocks. "
            "Do NOT copy its names or assertions — your subject is a different module, "
            "and every name you call must exist in YOUR subject's source shown above.\n"
            f"```python\n{body}```\n"
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("[TestExemplar] injection degraded", exc_info=True)
        return ""


async def with_exemplar(ctx, repo_root: Path):
    """*ctx* with a verified reference test appended to its instructions, or
    *ctx* unchanged. The ONE place the context is rebuilt, so the phase runner
    and its legacy orchestrator twin cannot drift — the twin-file trap that hid
    the inert VALIDATE_RETRY ladder for 524 iterations. NEVER raises.
    """
    try:
        import dataclasses  # noqa: PLC0415
        block = await exemplar_instruction(
            tuple(getattr(ctx, "target_files", ()) or ()),
            str(getattr(ctx, "description", "") or ""),
            Path(repo_root), op_id=str(getattr(ctx, "op_id", "") or ""),
        )
        if not block:
            return ctx
        existing = getattr(ctx, "human_instructions", "") or ""
        return dataclasses.replace(
            ctx, human_instructions=existing + block,
            previous_hash=ctx.context_hash,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("[TestExemplar] context injection degraded", exc_info=True)
        return ctx


def reset_for_tests() -> None:
    global _catalog_held
    with _catalog_lock:
        _catalog_held = None
    with _trait_lock:
        _trait_cache.clear()


__all__ = [
    "Candidate",
    "Catalog",
    "build_catalog",
    "catalog_for",
    "excerpt",
    "exemplar_instruction",
    "injection_enabled",
    "rank",
    "similarity",
    "traits_of",
    "traits_of_source",
    "verified",
    "with_exemplar",
]
