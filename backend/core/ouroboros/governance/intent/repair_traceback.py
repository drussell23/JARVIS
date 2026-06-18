"""Repair Context Bridge — Slice 1: deep semantic failure ingestion.

Parses pytest tracebacks frame-by-frame and AST-maps each frame to a canonical Oracle node key, so a
test failure carries the *precise functional coordinates* of the fault (not just a one-line error).
Those coordinates seed the blast-radius cone in Slice 2 and the structural gate in Slice 3.

Pure + fail-soft + injectable: the line->node resolver is any object exposing the GraphBackend
``nodes_in_file(rel) -> [keys]`` + ``get_node(key) -> attrs`` primitives (the Oracle's lazy backend
in production, a fake in tests). Mapping never raises — any failure degrades to the unenriched signal,
so the immune system is never weakened by the thing meant to strengthen it.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

# pytest --tb=short frame line: ``path/to/file.py:123: in func_name``
_FRAME_RE = re.compile(r"^(?P<file>.+\.py):(?P<line>\d+): in (?P<func>\S+)\s*$")
# a per-failure block header in the FAILURES section: ``____ test_foo ____`` / ``__ TestX.test_foo __``
_BLOCK_RE = re.compile(r"^_{3,}\s+(?P<name>[^_].*?)\s+_{3,}\s*$")


def bridge_enabled() -> bool:
    """``JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED`` (default OFF) — master switch for traceback
    enrichment + (Slice 2) blast-radius cone. OFF → signals are byte-identical to today."""
    return os.environ.get("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


class NodeResolver(Protocol):
    def nodes_in_file(self, file_path: str) -> List[str]: ...
    def get_node(self, key: str) -> Optional[Dict[str, Any]]: ...


@dataclass
class Frame:
    """One traceback frame, optionally AST-mapped to its Oracle node."""
    file: str                       # path as printed by pytest
    line: int
    func: str
    in_repo: bool = False           # resolvable under a known repo root
    rel_path: Optional[str] = None  # path relative to the repo root (matches Oracle file_path)
    node_key: Optional[str] = None  # canonical Oracle node key spanning this line, if any

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file, "line": self.line, "func": self.func,
            "in_repo": self.in_repo, "rel_path": self.rel_path, "node_key": self.node_key,
        }


@dataclass
class TracebackMap:
    frames: List[Frame] = field(default_factory=list)
    # deepest-first repo-internal node keys — the prime fault coordinates for blast-radius
    fault_node_keys: List[str] = field(default_factory=list)

    def to_evidence(self) -> Dict[str, Any]:
        return {
            "traceback_frames": [f.to_dict() for f in self.frames],
            "fault_node_keys": list(self.fault_node_keys),
        }


# --------------------------------------------------------------------------- parse
def parse_pytest_tracebacks(stdout: str) -> Dict[str, List[Frame]]:
    """Parse the FAILURES section into ``{block_name: [Frame, ...]}`` (in pytest print order, i.e.
    outermost test frame first → deepest fault frame last). Best-effort; unparseable → {}."""
    out: Dict[str, List[Frame]] = {}
    current: Optional[str] = None
    for raw in stdout.splitlines():
        block = _BLOCK_RE.match(raw)
        if block:
            current = str(block.group("name")).strip()
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        m = _FRAME_RE.match(raw.strip())
        if m:
            out[current].append(Frame(file=m.group("file"), line=int(m.group("line")),
                                      func=m.group("func")))
    return {k: v for k, v in out.items() if v}


def _frames_for_test(blocks: Dict[str, List[Frame]], test_id: str) -> List[Frame]:
    """Correlate a ``file.py::Class::test_name`` id to its parsed block by the test function name."""
    name = test_id.split("::")[-1].split("[")[0]      # strip params
    # exact tail match first (handles ``TestX.test_name`` headers), then substring
    for key, frames in blocks.items():
        if key == name or key.endswith("." + name):
            return frames
    for key, frames in blocks.items():
        if name in key:
            return frames
    return []


# --------------------------------------------------------------------------- AST map
def _to_rel(abs_or_rel: str, repo_roots: List[str]) -> Optional[str]:
    """Path as printed by pytest → repo-relative (matches Oracle ``file_path``), or None if external
    (stdlib / site-packages / outside every repo root).

    pytest ``--tb=short`` runs with ``cwd=repo_path`` and prints **repo-relative** paths for in-repo
    frames and **absolute** paths for out-of-repo frames (stdlib / site-packages). So a relative path
    is already the Oracle ``file_path``; only absolute paths need root-relativization. This is robust
    to the mapping process's own CWD differing from the repo root (it usually does)."""
    p = abs_or_rel
    if "site-packages" in p or "/lib/python" in p or "/dist-packages/" in p:
        return None
    if not os.path.isabs(p):
        return os.path.normpath(p)            # already repo-relative (pytest cwd=repo)
    for root in repo_roots:                    # absolute → relativize to a known root
        try:
            return str(Path(p).resolve().relative_to(Path(root).resolve()))
        except ValueError:
            continue
    return None                                # absolute + outside every root → external


def _node_for_line(rel_path: str, line: int, resolver: NodeResolver) -> Optional[str]:
    """Innermost Oracle node whose span ``[line_number, line_number+line_count)`` contains *line*.
    Innermost (smallest span) wins, so a function is preferred over the enclosing file/class node."""
    best_key: Optional[str] = None
    best_span = 1 << 62
    for key in resolver.nodes_in_file(rel_path):
        attrs = resolver.get_node(key)
        if not attrs:
            continue
        nid = attrs.get("node_id") or {}
        start = int(nid.get("line_number", 0) or 0)
        span = int(attrs.get("line_count", 0) or 0)
        if start <= 0 or span <= 0:
            continue
        if start <= line < start + span and span < best_span:
            best_key, best_span = key, span
    return best_key


def map_frames_to_nodes(frames: List[Frame], repo_roots: List[str],
                        resolver: Optional[NodeResolver]) -> List[Frame]:
    """Fill ``in_repo``/``rel_path``/``node_key`` on each frame. Pure; never raises."""
    for fr in frames:
        rel = _to_rel(fr.file, repo_roots)
        if rel is None:
            continue
        fr.in_repo = True
        fr.rel_path = rel
        if resolver is not None:
            try:
                fr.node_key = _node_for_line(rel, fr.line, resolver)
            except Exception:  # noqa: BLE001 — mapping is best-effort
                fr.node_key = None
    return frames


def build_traceback_map(stdout: str, test_id: str, repo_roots: List[str],
                        resolver: Optional[NodeResolver]) -> TracebackMap:
    """Top-level enrichment: parse → correlate to the test → AST-map → fault coordinates.

    ``fault_node_keys`` are the repo-internal frames' nodes **deepest-first** (the innermost in-repo
    frame = the prime fault suspect for blast-radius). De-duplicated, order-preserving. Fail-soft:
    any error returns an empty map (caller keeps the unenriched signal)."""
    try:
        blocks = parse_pytest_tracebacks(stdout)
        frames = _frames_for_test(blocks, test_id)
        if not frames:
            return TracebackMap()
        map_frames_to_nodes(frames, repo_roots, resolver)
        seen: set = set()
        fault: List[str] = []
        for fr in reversed(frames):                      # deepest-first
            if fr.node_key and fr.node_key not in seen:
                seen.add(fr.node_key)
                fault.append(fr.node_key)
        return TracebackMap(frames=frames, fault_node_keys=fault)
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the sensor
        logger.debug("[RepairBridge] traceback enrichment failed (non-fatal): %s", exc)
        return TracebackMap()
