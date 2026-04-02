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
from typing import Dict, List, Optional, Tuple

_JACCARD_THRESHOLD = float(os.environ.get("JARVIS_VALIDATE_DUPLICATION_JACCARD", "0.8"))


def check_duplication(
    candidate_content: str,
    source_content: str,
    file_path: str,
) -> Optional[str]:
    """Check if candidate introduces functions that duplicate existing source.

    Returns error message if duplication detected, None if clean.
    """
    if not file_path.endswith(".py"):
        return None

    try:
        source_tree = ast.parse(source_content)
    except SyntaxError:
        return None

    try:
        candidate_tree = ast.parse(candidate_content)
    except SyntaxError:
        return None

    source_units = _extract_units(source_tree)
    candidate_units = _extract_units(candidate_tree)

    source_names = {name for name, _ in source_units}
    new_units = [(name, node) for name, node in candidate_units if name not in source_names]

    if not new_units or not source_units:
        return None

    source_fingerprints: Dict[str, str] = {}
    source_features: Dict[str, Counter] = {}
    for name, node in source_units:
        source_fingerprints[name] = _canonical_fingerprint(node)
        source_features[name] = _extract_features(node)

    for new_name, new_node in new_units:
        new_fp = _canonical_fingerprint(new_node)
        new_features = _extract_features(new_node)

        for src_name, src_fp in source_fingerprints.items():
            if new_fp == src_fp:
                return (
                    f"Duplication detected: new function '{new_name}' is structurally "
                    f"identical to existing '{src_name}'"
                )

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
    """Normalize AST for structural comparison."""

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
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
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
        if isinstance(child, ast.stmt):
            features[type(child).__name__] += 1
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
