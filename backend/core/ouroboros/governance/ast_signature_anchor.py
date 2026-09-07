"""AST-Signature Anchor — inject a target module's EXACT public API into the
generation prompt so the model cannot hallucinate interfaces.

The local coder model reliably produces structurally-valid code but invents
function signatures, argument counts, and return shapes when writing against a
module it has only skimmed (measured 2026-09-06: writing a test for
``model_physics.py`` it guessed ``parse_model_physics("model_a", 100, 200)`` and
``effective_ceiling(100, 50) == 150.0`` when the real API is
``parse_model_physics(payload) -> Optional[ModelPhysics]`` and a two-arg
``effective_ceiling(physics, configured_ceiling) -> int``). Reading the whole
286-line source did NOT fix it; a distilled, authoritative signature block does —
short, unmissable, and stating the contract the candidate MUST honour.

Pure, deterministic, stdlib-only (``ast``). NEVER raises — every edge (syntax
error, exotic decorators, missing type hints, unreadable file) degrades to a
weaker-but-valid signature or an empty string, never a crash. Master switch
``JARVIS_AST_SIGNATURE_ANCHOR_ENABLED`` (default TRUE); OFF ⇒ empty block ⇒
byte-identical legacy prompt.
"""
from __future__ import annotations

import ast
import copy
import os
import logging
import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.SigAnchor")

_ENV_ENABLED = "JARVIS_AST_SIGNATURE_ANCHOR_ENABLED"
_ENV_MAX_MODULES = "JARVIS_AST_SIGNATURE_ANCHOR_MAX_MODULES"
_ENV_MAX_CHARS = "JARVIS_AST_SIGNATURE_ANCHOR_MAX_CHARS"
#: Per-symbol docstring excerpt cap (chars). The docstring IS the semantic
#: contract a signature cannot carry — ``payload: Any`` says nothing, while
#: the docstring says "an Ollama /api/show payload, architecture-prefixed
#: keys under model_info, None when any load-bearing field is missing".
#: Measured 2026-09-07: with signatures alone the model built flat
#: ``{"context_length": 2048, "num_layers": 32}`` payloads and every test
#: failed on ``assert None is not None``.
_ENV_DOC_CHARS = "JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS"
#: Ceiling for the ADAPTIVE per-symbol excerpt: the module's remaining char
#: budget is spread evenly across its public symbols, floored at
#: ``_DEFAULT_DOC_CHARS`` and capped here — a module with few symbols keeps
#: its full contracts, a module with dozens degrades gracefully to the floor.
_ENV_DOC_CHARS_MAX = "JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS_MAX"
_DEFAULT_MAX_MODULES = 4
_DEFAULT_MAX_CHARS = 6000
_DEFAULT_DOC_CHARS = 420
_DEFAULT_DOC_CHARS_MAX = 1200
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")

_PY_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.py")


def anchor_enabled() -> bool:
    raw = os.environ.get(_ENV_ENABLED, "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _sig_line(node) -> str:
    """One-line signature for a def/async def — name, params (with whatever type
    hints exist), and return annotation if present. Falls back to bare-name
    reconstruction if ``ast.unparse`` is unavailable or raises."""
    is_async = isinstance(node, ast.AsyncFunctionDef)
    prefix = "async def " if is_async else "def "
    try:
        clone = copy.copy(node)
        clone.body = [ast.Pass()]
        clone.decorator_list = []
        ast.fix_missing_locations(clone)
        head = ast.unparse(clone).split("\n", 1)[0].rstrip()
        return head + " ..."
    except Exception:  # noqa: BLE001 — degrade to bare-name reconstruction
        try:
            a = node.args
            names: List[str] = [
                arg.arg for arg in
                list(getattr(a, "posonlyargs", []) or []) + list(a.args)
            ]
            if a.vararg:
                names.append("*" + a.vararg.arg)
            names += [arg.arg for arg in a.kwonlyargs]
            if a.kwarg:
                names.append("**" + a.kwarg.arg)
            return f"{prefix}{node.name}({', '.join(names)}): ..."
        except Exception:  # noqa: BLE001
            return f"{prefix}{node.name}(...): ..."


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _doc_excerpt(node, max_chars: int) -> str:
    """Single-line excerpt of *node*'s docstring, bounded to *max_chars* and
    cut at the last sentence boundary (else the last word) before the cap.
    ``""`` when there is no docstring. Triple double-quotes are neutralised so
    the excerpt can be re-embedded as a docstring in the anchor block. NEVER
    raises."""
    try:
        raw = ast.get_docstring(node, clean=True)
    except Exception:  # noqa: BLE001 — exotic node/body shapes
        return ""
    if not raw:
        return ""
    text = " ".join(raw.split()).replace('"""', "'''").replace("\\", "/")
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = -1
    for m in _SENTENCE_END_RE.finditer(head):
        cut = m.end()
    if cut < max_chars // 3:
        sp = head.rfind(" ")
        cut = sp if sp >= max_chars // 3 else max_chars
    return head[:cut].rstrip() + " …"


def _def_lines(node, indent: str, doc_chars: int) -> List[str]:
    """Signature line for a def, expanded with its docstring contract when
    one exists (``def f(...) -> T:`` / docstring / ``...``). Without a
    docstring the legacy one-liner ``def f(...) -> T: ...`` is emitted."""
    sig = _sig_line(node)
    doc = _doc_excerpt(node, doc_chars) if doc_chars > 0 else ""
    if not doc or not sig.endswith(" ..."):
        return [indent + sig]
    return [
        indent + sig[: -len(" ...")],
        indent + '    """' + doc + '"""',
        indent + "    ...",
    ]


def _field_lines(cls_node, indent: str) -> List[str]:
    """Public annotated class-body fields (``name: T``) — the data contract
    of dataclasses / attrs / plain annotated classes. NEVER raises."""
    out: List[str] = []
    try:
        for item in cls_node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue
            if not _is_public(item.target.id):
                continue
            try:
                ann = ast.unparse(item.annotation)
            except Exception:  # noqa: BLE001
                ann = "Any"
            out.append(f"{indent}{item.target.id}: {ann}")
    except Exception:  # noqa: BLE001 — partial extraction beats a crash
        pass
    return out


def _public_doc_nodes(tree) -> List[object]:
    """Module, public top-level defs, public classes and their public /
    __init__ methods — every node whose docstring the anchor may carry."""
    nodes: List[object] = [tree]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_public(node.name):
                nodes.append(node)
        elif isinstance(node, ast.ClassDef) and _is_public(node.name):
            nodes.append(node)
            nodes.extend(
                m for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (_is_public(m.name) or m.name == "__init__")
            )
    return nodes


def _waterfill_level(lengths: Sequence[int], budget: int, ceiling: int) -> int:
    """Highest per-item cap such that ``sum(min(len, cap)) <= budget``: short
    docstrings stay whole and their unused share flows to the long ones.
    Returns ``ceiling`` when everything fits."""
    remaining = max(0, budget)
    level = ceiling
    pending = sorted(int(x) for x in lengths if x > 0)
    for i, ln in enumerate(pending):
        share = remaining // (len(pending) - i)
        take = min(ln, share, ceiling)
        if ln > share or ln > ceiling:
            level = min(level, share, ceiling)
        remaining -= take
    return max(0, level)


def _adaptive_doc_chars(tree, budget: Optional[int], skeleton_len: int = 0) -> int:
    """Per-symbol excerpt cap: the env floor when no budget is known;
    otherwise the water-fill level of every public docstring inside
    ``budget - skeleton_len`` (the chars left after signatures), floored at
    the env floor and capped at the env ceiling."""
    floor = _int_env(_ENV_DOC_CHARS, _DEFAULT_DOC_CHARS)
    if budget is None or budget <= 0:
        return floor
    ceiling = max(floor, _int_env(_ENV_DOC_CHARS_MAX, _DEFAULT_DOC_CHARS_MAX))
    lengths: List[int] = []
    for node in _public_doc_nodes(tree):
        try:
            doc = ast.get_docstring(node, clean=True) or ""
        except Exception:  # noqa: BLE001
            doc = ""
        if doc:
            # +12: the quotes / indent / newline each embedded excerpt costs.
            lengths.append(len(" ".join(doc.split())) + 12)
    if not lengths:
        return floor
    level = _waterfill_level(lengths, budget - skeleton_len, ceiling)
    return max(floor, min(ceiling, level))


def extract_public_api(
    source: str,
    module_import_path: str = "",
    doc_chars: Optional[int] = None,
    budget: Optional[int] = None,
) -> str:
    """Compact authoritative block for the PUBLIC top-level API of *source* —
    public functions and public classes (annotated fields, public methods +
    __init__), each carrying a bounded docstring excerpt: the semantic
    contract (accepted input shape, return semantics) that a bare signature
    cannot express. ``doc_chars`` bounds each excerpt (``None`` → env
    ``JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS``, default 420, raised adaptively
    toward ``budget / public symbols`` when a char ``budget`` is given, capped
    by ``JARVIS_AST_SIGNATURE_ANCHOR_DOC_CHARS_MAX``). ``""`` when there
    is no extractable public API or the source cannot be parsed. NEVER
    raises."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""
    if doc_chars is None:
        skeleton = extract_public_api(source, module_import_path, doc_chars=0)
        doc_chars = _adaptive_doc_chars(tree, budget, len(skeleton))
    lines: List[str] = []
    try:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_public(node.name):
                    lines.extend(_def_lines(node, "", doc_chars))
            elif isinstance(node, ast.ClassDef):
                if not _is_public(node.name):
                    continue
                try:
                    bases = ", ".join(ast.unparse(b) for b in node.bases)
                except Exception:  # noqa: BLE001
                    bases = ""
                header = f"class {node.name}" + (f"({bases})" if bases else "") + ":"
                body: List[str] = []
                cls_doc = _doc_excerpt(node, doc_chars) if doc_chars > 0 else ""
                if cls_doc:
                    body.append('    """' + cls_doc + '"""')
                body.extend(_field_lines(node, "    "))
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                        _is_public(m.name) or m.name == "__init__"
                    ):
                        body.extend(_def_lines(m, "    ", doc_chars))
                lines.append(header)
                lines.extend(body if body else ["    ..."])
    except Exception:  # noqa: BLE001 — partial extraction beats a crash
        pass
    if not lines:
        return ""
    head = [f"# {module_import_path or 'module'}"]
    try:
        mod_doc = _doc_excerpt(tree, doc_chars) if doc_chars > 0 else ""
    except Exception:  # noqa: BLE001
        mod_doc = ""
    if mod_doc:
        head.append('"""' + mod_doc + '"""')
    return "\n".join(head + lines)


def _import_label(path: Path, repo_root: Path) -> str:
    try:
        rel = path.resolve().relative_to(Path(repo_root).resolve())
        return str(rel).replace(os.sep, "/")
    except Exception:  # noqa: BLE001
        return path.name


def _module_stem_from_test(rel_path: str) -> Optional[str]:
    name = Path(rel_path).name
    if not name.endswith(".py"):
        return None
    stem = name[:-3]
    if stem.startswith("test_"):
        return stem[len("test_"):]
    if stem.endswith("_test"):
        return stem[: -len("_test")]
    return None


def _norm(p: str) -> str:
    return str(p).replace("\\", "/")


def collect_anchor_sources(
    target_files: Sequence[str],
    description: str,
    repo_root: os.PathLike,
) -> List[Tuple[str, Path]]:
    """Resolve the source modules whose API the candidate must honour, in order:
      1. existing TARGET files that are Python source (modifications must preserve
         the current API);
      2. repo-relative ``*.py`` paths named in the op DESCRIPTION that exist
         (operator/roadmap goals name the module-under-test explicitly);
      3. for a ``test_*.py`` / ``*_test.py`` target, the source module it tests,
         found by a bounded, non-test search under the repo.
    De-duplicated, order-preserving. Bounded. NEVER raises."""
    root = Path(repo_root)
    out: List[Tuple[str, Path]] = []
    seen = set()

    def _add(p: Path) -> None:
        try:
            rp = p.resolve()
        except Exception:  # noqa: BLE001
            return
        key = str(rp)
        if key in seen or not rp.is_file() or rp.suffix != ".py":
            return
        seen.add(key)
        out.append((_import_label(rp, root), rp))

    try:
        # (1) existing target source files
        for tf in target_files or ():
            _add(root / str(tf))
        # (2) .py paths named in the description
        for m in _PY_PATH_RE.findall(description or ""):
            cand = root / m.lstrip("./")
            if cand.is_file():
                _add(cand)
        # (3) module-under-test for test_* targets (bounded search)
        for tf in target_files or ():
            stem = _module_stem_from_test(str(tf))
            if not stem:
                continue
            found: List[Path] = []
            try:
                for p in root.rglob(stem + ".py"):
                    sp = _norm(str(p))
                    nm = p.name
                    # Exclude the worktree tree, any tests/ or test/ DIRECTORY
                    # component, and test FILES themselves — but NOT a source
                    # module that merely lives under a dir whose name contains
                    # 'test_' (e.g. a pytest tmp dir or a 'test_project/' repo).
                    if "/.worktrees/" in sp or "/tests/" in sp or "/test/" in sp:
                        continue
                    if nm.startswith("test_") or nm.endswith("_test.py"):
                        continue
                    found.append(p)
                    if len(found) >= 32:  # hard cap: never walk unbounded
                        break
            except Exception:  # noqa: BLE001
                found = []
            found.sort(key=lambda p: len(str(p)))
            for p in found[:1]:
                _add(p)
    except Exception:  # noqa: BLE001
        pass
    return out


def build_signature_anchor(
    target_files: Sequence[str],
    description: str,
    repo_root: os.PathLike,
) -> str:
    """The prompt block: authoritative public API signatures for the modules the
    candidate must call. Empty string when disabled, nothing resolves, or every
    source is unreadable/API-less — in which case the prompt is byte-identical to
    the pre-anchor legacy. NEVER raises."""
    try:
        if not anchor_enabled():
            return ""
        sources = collect_anchor_sources(target_files, description, repo_root)
        if not sources:
            return ""
        max_modules = _int_env(_ENV_MAX_MODULES, _DEFAULT_MAX_MODULES)
        max_chars = _int_env(_ENV_MAX_CHARS, _DEFAULT_MAX_CHARS)
        blocks: List[str] = []
        used = 0
        for label, path in sources[:max_modules]:
            try:
                src = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            block = extract_public_api(src, label, budget=max_chars - used)
            if not block:
                continue
            if used + len(block) > max_chars:
                block = block[: max(0, max_chars - used)]
            blocks.append(block)
            used += len(block)
            if used >= max_chars:
                break
        if not blocks:
            return ""
        body = "\n\n".join(blocks)
        try:
            logger.info(
                "[SigAnchor] injected %d chars for %d module(s): %s",
                len(body), len(blocks),
                ", ".join(lbl for lbl, _ in sources[:max_modules]),
            )
        except Exception:  # noqa: BLE001
            pass
        return (
            "## AUTHORITATIVE API SIGNATURES (ground truth — use EXACTLY)\n\n"
            "The signatures below are parsed from the real, current source on "
            "disk. Any function/method you call from these modules MUST match "
            "these signatures EXACTLY — the same name, argument names/order/"
            "count, and return shape. Do NOT invent parameters, overloads, or "
            "return types. If a needed capability is absent here, it does not "
            "exist — do not assume it.\n\n"
            "```python\n" + body + "\n```"
        )
    except Exception:  # noqa: BLE001 — the anchor is additive, never fatal
        return ""
