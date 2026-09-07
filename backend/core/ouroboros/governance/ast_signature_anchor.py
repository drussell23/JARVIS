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
import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_ENV_ENABLED = "JARVIS_AST_SIGNATURE_ANCHOR_ENABLED"
_ENV_MAX_MODULES = "JARVIS_AST_SIGNATURE_ANCHOR_MAX_MODULES"
_ENV_MAX_CHARS = "JARVIS_AST_SIGNATURE_ANCHOR_MAX_CHARS"
_DEFAULT_MAX_MODULES = 4
_DEFAULT_MAX_CHARS = 4000

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


def extract_public_api(source: str, module_import_path: str = "") -> str:
    """Compact authoritative signature block for the PUBLIC top-level API of
    *source* — public functions and public classes (public methods + __init__).
    ``""`` when there is no extractable public API or the source cannot be
    parsed. NEVER raises."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""
    lines: List[str] = []
    try:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_public(node.name):
                    lines.append(_sig_line(node))
            elif isinstance(node, ast.ClassDef):
                if not _is_public(node.name):
                    continue
                try:
                    bases = ", ".join(ast.unparse(b) for b in node.bases)
                except Exception:  # noqa: BLE001
                    bases = ""
                header = f"class {node.name}" + (f"({bases})" if bases else "") + ":"
                methods = [
                    "    " + _sig_line(m)
                    for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and (_is_public(m.name) or m.name == "__init__")
                ]
                lines.append(header)
                lines.extend(methods if methods else ["    ..."])
    except Exception:  # noqa: BLE001 — partial extraction beats a crash
        pass
    if not lines:
        return ""
    return f"# {module_import_path or 'module'}\n" + "\n".join(lines)


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
            block = extract_public_api(src, label)
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
