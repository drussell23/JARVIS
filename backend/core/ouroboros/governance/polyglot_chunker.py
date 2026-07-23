"""Polymorphic Source Router — the Swarm goes multi-language.

The chunker/resolver were Python-`ast`-only. Point the Swarm at a 50k-line
``config.json``, a ``manifest.yaml``, or a ``.tsx`` and ``ast.parse`` raises
``SyntaxError`` — taking the whole op down. The root cause is a PARSER MISMATCH,
not "dirty" input, so the fix is native structural parsing dispatched by file
type — never regex-forcing a non-Python file through the Python parser.

Strategy Pattern:

  * ``BaseChunker`` — ``extract`` (target → standardized ``Chunk``) + ``validate``
    (is the stitched whole well-formed?). ``stitch`` is INHERITED and reuses the
    existing line-based ``stitch_replacement`` (DRY — the stitcher is already
    language-agnostic; only validation is language-specific).
  * ``PythonASTChunker`` — wraps the existing ``extract_target_chunk`` +
    ``ast.parse``. The ``.py`` path stays byte-identical.
  * ``JSONTreeChunker`` — a native string-aware bracket scanner locates a nested
    key path's entry span (key line → value's closing bracket + trailing comma)
    WITHOUT breaking surrounding brackets; validates with ``json.loads``.
  * ``YAMLTreeChunker`` — an indentation-structured key-path line locator;
    validates with ``yaml.safe_load`` (or a brace/indent sanity check if PyYAML
    is absent).
  * ``RegexIndentationChunker`` — the universal fallback for ``.txt`` / ``.md`` /
    unknown: a strict line-based block; validates as always-well-formed (text has
    no grammar to corrupt).

The factory returns a standardized ``Chunk`` the existing ``ChunkTarget`` /
``ProductionAgentTurnFn`` / ``stitch_replacement`` already process. ``polyglot_repair``
is the language-agnostic sibling of ``swarm_repair`` — it REUSES ``swarm_concurrency``
(the AIMD-capped semaphore) and ``stitch_replacement`` (the stitcher); it does not
rewrite either. Never raises.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.PolyglotChunker")


@dataclass
class Chunk:
    """Standardized chunk — duck-types the #70020 CodeChunk so the existing
    ``stitch_replacement`` (reads ``start_line`` / ``end_line``) and the swarm
    consume it unchanged. 1-indexed inclusive line range.

    ``context_header`` — read-only context bundled by the Chunker (e.g. TSX
    imports + relevant interfaces) so the LLM has zero prop/hook context
    starvation. It is NOT part of the stitched region.
    ``indent`` — the locked absolute leading-indent depth (YAML), used by the
    strategy's ``stitch`` to normalize the LLM's whitespace before grafting."""
    start_line: int
    end_line: int
    source_code: str
    name: str
    qualified_name: str = ""
    file_path: str = ""
    language: str = ""
    context_header: str = ""
    indent: int = 0


# ---------------------------------------------------------------------------
# Strategy interface
# ---------------------------------------------------------------------------


class BaseChunker(ABC):
    extensions: Tuple[str, ...] = ()
    language: str = "unknown"

    @abstractmethod
    def extract(self, source: str, file_path: str, target: str) -> Optional[Chunk]:
        """Isolate *target* (a symbol / dotted key-path / line-anchor) → Chunk."""

    @abstractmethod
    def validate(self, text: str) -> bool:
        """Is the WHOLE stitched *text* structurally well-formed for this type?"""

    def validate_detail(self, text: str) -> Optional[str]:
        """The in-memory pre-compiler's error surface: ``None`` if valid, else a
        human-readable syntax detail. Default derives from ``validate``; typed
        strategies override to capture the exact parser error (line/message)."""
        return None if self.validate(text) else (
            f"{self.language} structural validation failed at the graft seam"
        )

    def stitch(self, full_source: str, chunk: Chunk, new_body: str) -> Optional[str]:
        """Line-based replacement — DRY on the existing stitcher (language-
        agnostic). Never raises."""
        try:
            from backend.core.ouroboros.governance.chunked_generation import (
                stitch_replacement,
            )
            return stitch_replacement(full_source, chunk, new_body)
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Python — wrap the existing AST path (byte-identical)
# ---------------------------------------------------------------------------


class PythonASTChunker(BaseChunker):
    extensions = (".py", ".pyi")
    language = "python"

    def extract(self, source: str, file_path: str, target: str) -> Optional[Chunk]:
        try:
            from backend.core.ouroboros.governance.chunked_generation import (
                extract_target_chunk,
            )
            c = extract_target_chunk(source, file_path, target)
        except Exception:  # noqa: BLE001
            return None
        if c is None:
            return None
        return Chunk(
            start_line=int(getattr(c, "start_line", 0)),
            end_line=int(getattr(c, "end_line", 0)),
            source_code=getattr(c, "source_code", "") or "",
            name=getattr(c, "name", target) or target,
            qualified_name=getattr(c, "qualified_name", "") or "",
            file_path=file_path, language="python",
        )

    def validate(self, text: str) -> bool:
        import ast
        try:
            ast.parse(text)
            return True
        except SyntaxError:
            return False

    def validate_detail(self, text: str) -> Optional[str]:
        import ast
        try:
            ast.parse(text)
            return None
        except SyntaxError as exc:
            return f"SyntaxError: {exc.msg} (line {exc.lineno}, offset {exc.offset})"


# ---------------------------------------------------------------------------
# JSON — native string-aware bracket scanner (DOM/Tree strategy)
# ---------------------------------------------------------------------------


def _json_key_line_span(text: str, target_path: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Return the 1-indexed inclusive line span of the ENTRY for the nested key
    ``target_path`` (e.g. ['services','web','port']) — the key line through the
    end of its value + any trailing comma. A recursive-descent, string-aware,
    bracket-matched scan (native structural parse; NO regex). None if absent."""
    target = list(target_path)
    n = len(text)

    def line_of(pos: int) -> int:
        return text.count("\n", 0, max(0, min(pos, n))) + 1

    def skip_ws(j: int) -> int:
        while j < n and text[j] in " \t\r\n":
            j += 1
        return j

    def scan_string(j: int) -> Tuple[str, int]:
        # j at opening quote; return (decoded, index-after-closing-quote)
        j += 1
        buf: List[str] = []
        while j < n:
            c = text[j]
            if c == "\\":
                nxt = text[j + 1] if j + 1 < n else ""
                buf.append({"n": "\n", "t": "\t", "r": "\r", '"': '"',
                            "\\": "\\", "/": "/", "b": "\b", "f": "\f"}.get(nxt, nxt))
                j += 2
                continue
            if c == '"':
                return "".join(buf), j + 1
            buf.append(c)
            j += 1
        return "".join(buf), j

    def scan_value_end(j: int) -> int:
        j = skip_ws(j)
        if j >= n:
            return j
        c = text[j]
        if c == '"':
            _, j2 = scan_string(j)
            return j2
        if c in "{[":
            open_c, close_c = ("{", "}") if c == "{" else ("[", "]")
            depth = 0
            while j < n:
                cc = text[j]
                if cc == '"':
                    _, j = scan_string(j)
                    continue
                if cc == open_c:
                    depth += 1
                elif cc == close_c:
                    depth -= 1
                    if depth == 0:
                        return j + 1
                j += 1
            return j
        while j < n and text[j] not in ",}] \t\r\n":
            j += 1
        return j

    result: List[Optional[Tuple[int, int]]] = [None]
    path: List[str] = []

    def parse_value(j: int) -> int:
        j = skip_ws(j)
        if j >= n:
            return j
        if text[j] == "{":
            return parse_object(j)
        if text[j] == "[":
            return parse_array(j)
        return scan_value_end(j)

    def parse_object(j: int) -> int:
        j += 1  # past '{'
        while True:
            j = skip_ws(j)
            if j >= n:
                return j
            if text[j] == "}":
                return j + 1
            if text[j] == ",":
                j += 1
                continue
            if text[j] != '"':
                j += 1
                continue
            key_start = j
            key, j = scan_string(j)
            j = skip_ws(j)
            if j < n and text[j] == ":":
                j += 1
            j = skip_ws(j)
            full = path + [key]
            is_target = full == target
            if text[j] in "{[":
                if is_target:
                    end = scan_value_end(j)
                    k = skip_ws(end)
                    if k < n and text[k] == ",":
                        end = k + 1
                    result[0] = (line_of(key_start), line_of(end - 1))
                    j = scan_value_end(j)
                else:
                    path.append(key)
                    j = parse_value(j)
                    path.pop()
            else:
                end = scan_value_end(j)
                if is_target:
                    k = skip_ws(end)
                    if k < n and text[k] == ",":
                        end = k + 1
                    result[0] = (line_of(key_start), line_of(end - 1))
                j = end
            j = skip_ws(j)
            if j < n and text[j] == ",":
                j += 1
        # unreachable

    def parse_array(j: int) -> int:
        j += 1  # past '['
        while True:
            j = skip_ws(j)
            if j >= n:
                return j
            if text[j] == "]":
                return j + 1
            if text[j] == ",":
                j += 1
                continue
            j = parse_value(j)
            j = skip_ws(j)
            if j < n and text[j] == ",":
                j += 1

    parse_value(skip_ws(0))
    return result[0]


class JSONTreeChunker(BaseChunker):
    extensions = (".json",)
    language = "json"

    def extract(self, source: str, file_path: str, target: str) -> Optional[Chunk]:
        span = _json_key_line_span(source, target.split("."))
        if span is None:
            return None
        s, e = span
        lines = source.splitlines(keepends=True)
        if s < 1 or e > len(lines) or e < s:
            return None
        return Chunk(
            start_line=s, end_line=e, source_code="".join(lines[s - 1:e]),
            name=target, qualified_name=target, file_path=file_path, language="json",
        )

    def validate(self, text: str) -> bool:
        import json
        try:
            json.loads(text)
            return True
        except ValueError:
            return False

    def validate_detail(self, text: str) -> Optional[str]:
        import json
        try:
            json.loads(text)
            return None
        except ValueError as exc:
            return f"JSONDecodeError: {exc}"


# ---------------------------------------------------------------------------
# YAML — indentation-structured key-path locator
# ---------------------------------------------------------------------------


def _yaml_key_line_span(text: str, target_path: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Locate a nested YAML key path by indentation. Returns the 1-indexed span
    from the key line through the last line MORE-indented than the key (its
    block). Handles the common block-mapping case. None if absent."""
    lines = text.splitlines()
    target = list(target_path)
    depth = 0
    start_idx: Optional[int] = None
    key_indent = -1
    i = 0
    while i < len(lines) and depth < len(target):
        raw = lines[i]
        stripped = raw.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(raw) - len(stripped)
        want = target[depth]
        if stripped.split(":", 1)[0].strip() == want and (key_indent < 0 or indent > key_indent):
            key_indent = indent
            depth += 1
            if depth == len(target):
                start_idx = i
                break
        i += 1
    if start_idx is None:
        return None
    # Extend through the block: subsequent lines more-indented than the key.
    end_idx = start_idx
    j = start_idx + 1
    while j < len(lines):
        raw = lines[j]
        stripped = raw.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            j += 1
            continue
        indent = len(raw) - len(stripped)
        if indent <= key_indent:
            break
        end_idx = j
        j += 1
    return (start_idx + 1, end_idx + 1)


def _normalize_to_indent(body: str, indent: int) -> str:
    """Indentation Lock: re-align *body* so its shallowest non-blank line sits at
    exactly ``indent`` leading spaces, preserving RELATIVE internal indentation.
    Mathematically guarantees the grafted block matches the locked YAML baseline
    regardless of what leading whitespace the LLM produced (over- or under-
    indented, tab-mixed). Never raises."""
    lines = body.replace("\t", "  ").split("\n")
    non_blank = [ln for ln in lines if ln.strip()]
    if not non_blank:
        return body
    min_lead = min(len(ln) - len(ln.lstrip(" ")) for ln in non_blank)
    pad = " " * max(0, int(indent))
    out: List[str] = []
    for ln in lines:
        if not ln.strip():
            out.append("")
        else:
            out.append(pad + ln[min_lead:])   # strip the block's own base, apply the lock
    return "\n".join(out)


class YAMLTreeChunker(BaseChunker):
    extensions = (".yaml", ".yml")
    language = "yaml"

    def extract(self, source: str, file_path: str, target: str) -> Optional[Chunk]:
        span = _yaml_key_line_span(source, target.split("."))
        if span is None:
            return None
        s, e = span
        lines = source.splitlines(keepends=True)
        first = lines[s - 1] if s - 1 < len(lines) else ""
        locked_indent = len(first) - len(first.lstrip(" "))   # absolute depth of the key
        return Chunk(
            start_line=s, end_line=e, source_code="".join(lines[s - 1:e]),
            name=target, qualified_name=target, file_path=file_path, language="yaml",
            indent=locked_indent,
        )

    def stitch(self, full_source: str, chunk: Chunk, new_body: str) -> Optional[str]:
        """Indentation Lock at Fan-In: normalize the LLM's whitespace to the
        stored ``chunk.indent`` BEFORE the line-based graft, so indentation drift
        can never corrupt the global YAML tree."""
        normalized = _normalize_to_indent(new_body, getattr(chunk, "indent", 0))
        return super().stitch(full_source, chunk, normalized)

    def validate(self, text: str) -> bool:
        try:
            import yaml  # type: ignore
        except Exception:  # noqa: BLE001 — PyYAML absent → indentation sanity only
            return "\t" not in text  # tabs are illegal indentation in YAML
        try:
            yaml.safe_load(text)
            return True
        except Exception:  # noqa: BLE001
            return False

    def validate_detail(self, text: str) -> Optional[str]:
        try:
            import yaml  # type: ignore
        except Exception:  # noqa: BLE001
            return None if "\t" not in text else "YAMLError: tab in indentation"
        try:
            yaml.safe_load(text)
            return None
        except Exception as exc:  # noqa: BLE001
            return f"YAMLError: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"


# ---------------------------------------------------------------------------
# TSX/JSX — structural scan + Semantic Context Bundling
# ---------------------------------------------------------------------------

import re as _re

_TS_IMPORT_RE = _re.compile(r"^\s*import\s.+?;?\s*$", _re.MULTILINE)
_TS_IDENT_RE = _re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _ts_definition_spans(source: str):
    """Yield (name, start_idx, end_idx, text) for every top-level ``interface`` /
    ``type`` / component-ish def, via brace/`;`-matched structural scan (no TS AST
    available in-process — native line/brace scan, not regex-forcing). Never raises."""
    out = []
    for m in _re.finditer(
        r"(?:export\s+)?(?:interface|type|function|const)\s+([A-Za-z_$][\w$]*)",
        source,
    ):
        name = m.group(1)
        kw = m.group(0)
        start = m.start()
        if "type " in kw and "=" in source[m.end(): m.end() + 200].split("\n", 1)[0]:
            # `type X = ...;` — ends at the terminating semicolon at depth 0.
            j = source.find("=", m.end())
            depth = 0
            k = j
            while k < len(source):
                c = source[k]
                if c in "{[(":
                    depth += 1
                elif c in "}])":
                    depth -= 1
                elif c == ";" and depth == 0:
                    break
                k += 1
            out.append((name, start, min(k + 1, len(source)), source[start:min(k + 1, len(source))]))
        else:
            # brace-bodied (interface/function/const-arrow-with-body).
            brace = source.find("{", m.end())
            if brace == -1:
                continue
            depth = 0
            k = brace
            while k < len(source):
                c = source[k]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            out.append((name, start, min(k + 1, len(source)), source[start:min(k + 1, len(source))]))
    return out


class TSXChunker(BaseChunker):
    extensions = (".tsx", ".jsx", ".ts")
    language = "tsx"

    def extract(self, source: str, file_path: str, target: str) -> Optional[Chunk]:
        defs = _ts_definition_spans(source)
        node = next((d for d in defs if d[0] == target), None)
        if node is None:
            return None
        _name, start, end, node_text = node
        lines = source.splitlines(keepends=True)
        start_line = source.count("\n", 0, start) + 1
        end_line = source.count("\n", 0, end - 1) + 1

        # Semantic Context Bundling: imports + LOCAL interface/type defs whose
        # names appear in the target node → zero prop/hook context starvation.
        imports = [m.group(0).strip() for m in _TS_IMPORT_RE.finditer(source)]
        referenced = {t for t in _TS_IDENT_RE.findall(node_text)}
        relevant_types = [
            text.strip() for (nm, s, e, text) in defs
            if nm != target and nm in referenced
            and _re.match(r"\s*(?:export\s+)?(?:interface|type)\b", text)
        ]
        header_parts: List[str] = []
        if imports:
            header_parts.append("// --- imports (read-only context) ---\n" + "\n".join(imports))
        if relevant_types:
            header_parts.append(
                "// --- relevant type/interface defs (read-only context) ---\n"
                + "\n\n".join(relevant_types)
            )
        context_header = "\n\n".join(header_parts)

        return Chunk(
            start_line=start_line, end_line=end_line,
            source_code="".join(lines[start_line - 1:end_line]),
            name=target, qualified_name=target, file_path=file_path,
            language="tsx", context_header=context_header,
        )

    def validate(self, text: str) -> bool:
        # No in-process TS parser — validate balanced braces/parens/brackets
        # (the structural corruption class the stitch could introduce). Strings/
        # comments are not fully tokenized; this is a structural sanity gate.
        depth = {"{": 0, "(": 0, "[": 0}
        pairs = {"}": "{", ")": "(", "]": "["}
        for c in text:
            if c in depth:
                depth[c] += 1
            elif c in pairs:
                depth[pairs[c]] -= 1
                if depth[pairs[c]] < 0:
                    return False
        return all(v == 0 for v in depth.values())


def build_context_bundled_target(chunk: Chunk, *, instruction: str = ""):
    """Compose the ``ChunkTarget`` the swarm consumes, folding a Chunk's
    ``context_header`` into the ChunkTarget's ``prompt`` (read-only context) so
    the downstream ProductionAgentTurnFn needs no change. DRY: the bundling lives
    in the Chunker layer; the orchestrator just reads ``ChunkTarget``."""
    from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
    header = getattr(chunk, "context_header", "") or ""
    prompt = ""
    if header:
        prompt = (
            "READ-ONLY CONTEXT (do not modify or repeat — for reference only):\n"
            f"{header}\n\n"
        )
    return ChunkTarget(
        symbol=chunk.name, chunk=chunk,
        instruction=instruction or f"repair {chunk.name}", prompt=prompt,
    )


# ---------------------------------------------------------------------------
# Universal fallback — line-based block, always-well-formed
# ---------------------------------------------------------------------------


class RegexIndentationChunker(BaseChunker):
    extensions = ()
    language = "text"

    def extract(self, source: str, file_path: str, target: str) -> Optional[Chunk]:
        """*target* is either ``"start-end"`` (1-indexed line range) or a literal
        anchor string; returns that line (block). Text has no brackets to break."""
        lines = source.splitlines(keepends=True)
        if not lines:
            return None
        if "-" in target and all(p.strip().isdigit() for p in target.split("-", 1)):
            a, b = (int(p) for p in target.split("-", 1))
            s, e = max(1, a), min(len(lines), b)
            if e < s:
                return None
        else:
            hit = next((k for k, ln in enumerate(lines) if target in ln), None)
            if hit is None:
                return None
            s = e = hit + 1
        return Chunk(
            start_line=s, end_line=e, source_code="".join(lines[s - 1:e]),
            name=target, qualified_name=target, file_path=file_path, language="text",
        )

    def validate(self, text: str) -> bool:
        return True  # plain text / markdown has no grammar to corrupt


# ---------------------------------------------------------------------------
# Factory — dynamic dispatch by extension
# ---------------------------------------------------------------------------


class ChunkerFactory:
    _STRATEGIES: Tuple[BaseChunker, ...] = (
        PythonASTChunker(), JSONTreeChunker(), YAMLTreeChunker(), TSXChunker(),
    )
    _FALLBACK = RegexIndentationChunker()

    @classmethod
    def for_file(cls, file_path: str) -> BaseChunker:
        ext = os.path.splitext(file_path or "")[1].lower()
        for strat in cls._STRATEGIES:
            if ext in strat.extensions:
                return strat
        return cls._FALLBACK


def polymorphic_extract_target(source: str, file_path: str, target: str) -> Optional[Chunk]:
    """The dynamic-dispatch seam ``intercept_full_content`` / the resolver call:
    inspect the extension, instantiate the right strategy, extract. None → the
    caller fails closed to the RAG pathway. Never raises."""
    try:
        return ChunkerFactory.for_file(file_path).extract(source, file_path, target)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# polyglot_repair — the language-agnostic sibling of swarm_repair
# ---------------------------------------------------------------------------


@dataclass
class PolyglotResult:
    stitched: str
    succeeded: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    language: str = ""
    agents_spawned: int = 0


# agent_fn(chunk) -> Awaitable[str] : the repaired node body for that chunk.
PolyAgentFn = Callable[[Chunk], Awaitable[str]]


async def polyglot_repair(
    source: str,
    file_path: str,
    targets: Sequence[str],
    agent_fn: PolyAgentFn,
    *,
    max_concurrency: Optional[int] = None,
) -> PolyglotResult:
    """Extract each target via the file's Chunker strategy, repair concurrently
    (semaphore-bounded), then stitch descending-line + atomically, validating the
    WHOLE with the strategy's polymorphic ``validate`` (json.loads / yaml / none —
    NOT ast.parse). REUSES ``swarm_concurrency`` (AIMD cap) + ``stitch_replacement``
    (the stitcher) — rewrites neither. A node whose graft would break the global
    structure is rejected; the file stays well-formed. Never raises."""
    import asyncio

    strat = ChunkerFactory.for_file(file_path)
    try:
        from backend.core.ouroboros.governance.chunk_swarm import swarm_concurrency
        limit = max_concurrency if max_concurrency is not None else swarm_concurrency()
    except Exception:  # noqa: BLE001
        limit = max_concurrency or 4
    sem = asyncio.Semaphore(max(1, int(limit)))

    resolved: List[Tuple[str, Chunk]] = []
    for t in targets:
        ch = strat.extract(source, file_path, t)
        if ch is not None:
            resolved.append((t, ch))

    async def _run(item: Tuple[str, Chunk]) -> Tuple[str, Chunk, str]:
        name, chunk = item
        async with sem:
            try:
                body = await agent_fn(chunk)
            except Exception:  # noqa: BLE001 — an agent fault isolates its node
                body = ""
        return (name, chunk, body or "")

    outcomes = await asyncio.gather(*[_run(it) for it in resolved]) if resolved else []

    # Descending-line atomic stitch (later lines first so earlier spans stay valid).
    ordered = sorted(outcomes, key=lambda o: o[1].start_line, reverse=True)
    stitched = source
    succeeded: List[str] = []
    failed: List[str] = []
    for name, chunk, body in ordered:
        if not body:
            failed.append(name)
            continue
        candidate = strat.stitch(stitched, chunk, body)
        if candidate is not None and strat.validate(candidate):
            stitched = candidate
            succeeded.append(name)
        else:
            failed.append(name)  # graft would corrupt the structure → rejected

    # Any target that never resolved to a chunk is a failure too.
    for t in targets:
        if t not in succeeded and t not in failed:
            failed.append(t)

    return PolyglotResult(
        stitched=stitched, succeeded=succeeded, failed=failed,
        language=strat.language, agents_spawned=len(resolved),
    )


__all__ = [
    "BaseChunker",
    "Chunk",
    "ChunkerFactory",
    "JSONTreeChunker",
    "PolyglotResult",
    "PythonASTChunker",
    "RegexIndentationChunker",
    "TSXChunker",
    "YAMLTreeChunker",
    "build_context_bundled_target",
    "polyglot_repair",
    "polymorphic_extract_target",
]
