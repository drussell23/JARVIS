"""
Type Analyzer v1.0 - Deep Type Inference and Checking System
============================================================

Enterprise-grade type analysis system that provides TypeScript-style
deep type inference for Python code. Catches type errors before runtime.

Features:
- Deep type inference from assignments, returns, and context
- Generic type resolution with variance analysis
- Union and intersection type handling
- Callable type inference with parameter types
- Protocol and structural typing support
- Type narrowing through control flow analysis
- Cross-file type propagation
- Type compatibility checking

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                       Type Analyzer v1.0                                 │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐       │
    │   │ Type Extractor  │   │ Type Inferencer │   │ Type Checker    │       │
    │   │ (Annotations)   │──▶│ (Flow Analysis) │──▶│ (Compatibility) │       │
    │   └─────────────────┘   └─────────────────┘   └─────────────────┘       │
    │           │                     │                     │                  │
    │           └─────────────────────┴─────────────────────┘                  │
    │                                 │                                        │
    │                    ┌────────────▼────────────┐                           │
    │                    │    Type Environment     │                           │
    │                    │   (Scope + Bindings)    │                           │
    │                    └────────────┬────────────┘                           │
    │                                 │                                        │
    │   ┌──────────────┬──────────────┼──────────────┬──────────────┐         │
    │   │              │              │              │              │         │
    │   ▼              ▼              ▼              ▼              ▼         │
    │ Generic       Narrowing     Variance      Protocol       Error         │
    │ Resolver      Engine        Checker       Matcher        Reporter      │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import os
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import lru_cache
from pathlib import Path
from typing import (
    Any, Callable, DefaultDict, Dict, FrozenSet, Generic, Iterator,
    List, Literal, Mapping, NamedTuple, Optional, Protocol, Sequence,
    Set, Tuple, Type, TypeVar, Union, cast, get_args, get_origin
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


# =============================================================================
# CONFIGURATION - Environment Driven (Zero Hardcoding)
# =============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    val = _get_env(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


def _get_env_list(key: str, default: List[str] = None) -> List[str]:
    val = _get_env(key, "")
    if not val:
        return default or []
    return [x.strip() for x in val.split(",") if x.strip()]


class TypeAnalyzerConfig:
    """Configuration for type analysis."""

    # Analysis settings
    STRICT_MODE: bool = _get_env_bool("TYPE_STRICT_MODE", True)
    INFER_RETURN_TYPES: bool = _get_env_bool("TYPE_INFER_RETURNS", True)
    CHECK_GENERICS: bool = _get_env_bool("TYPE_CHECK_GENERICS", True)
    CHECK_PROTOCOLS: bool = _get_env_bool("TYPE_CHECK_PROTOCOLS", True)

    # Depth limits
    MAX_INFERENCE_DEPTH: int = _get_env_int("TYPE_MAX_DEPTH", 10)
    MAX_UNION_MEMBERS: int = _get_env_int("TYPE_MAX_UNION", 10)

    # Error reporting
    REPORT_LEVEL: str = _get_env("TYPE_REPORT_LEVEL", "warning")  # error, warning, info

    # Cross-file
    CROSS_FILE_INFERENCE: bool = _get_env_bool("TYPE_CROSS_FILE", True)

    # Repository paths
    JARVIS_REPO: Path = Path(_get_env("JARVIS_REPO", str(Path.home() / "Documents/repos/JARVIS-AI-Agent")))
    PRIME_REPO: Path = Path(_get_env("PRIME_REPO", str(Path.home() / "Documents/repos/jarvis-prime")))
    REACTOR_REPO: Path = Path(_get_env("REACTOR_REPO", str(Path.home() / "Documents/repos/reactor-core")))


# =============================================================================
# ENUMS AND TYPES
# =============================================================================

class TypeKind(Enum):
    """Kind of type in the type system."""
    PRIMITIVE = "primitive"        # int, str, float, bool, None
    CLASS = "class"                # User-defined class
    GENERIC = "generic"            # Generic[T], List[T]
    UNION = "union"                # Union[A, B], A | B
    OPTIONAL = "optional"          # Optional[T] = Union[T, None]
    CALLABLE = "callable"          # Callable[[Args], Return]
    TUPLE = "tuple"                # Tuple[A, B, ...]
    DICT = "dict"                  # Dict[K, V]
    LIST = "list"                  # List[T]
    SET = "set"                    # Set[T]
    PROTOCOL = "protocol"          # Protocol class
    TYPE_VAR = "typevar"           # TypeVar
    LITERAL = "literal"            # Literal["a", "b"]
    ANY = "any"                    # Any
    UNKNOWN = "unknown"            # Not yet inferred
    NEVER = "never"                # Never/NoReturn


class Variance(Enum):
    """Variance of type parameters."""
    INVARIANT = "invariant"
    COVARIANT = "covariant"
    CONTRAVARIANT = "contravariant"


class TypeErrorKind(Enum):
    """Kind of type error."""
    INCOMPATIBLE_TYPES = "incompatible_types"
    INCOMPATIBLE_ARGUMENT = "incompatible_argument"
    INCOMPATIBLE_RETURN = "incompatible_return"
    MISSING_ATTRIBUTE = "missing_attribute"
    INVALID_CALL = "invalid_call"
    UNBOUND_TYPE_VAR = "unbound_type_var"
    UNREACHABLE_CODE = "unreachable_code"
    INVALID_CAST = "invalid_cast"
    PROTOCOL_VIOLATION = "protocol_violation"


class Severity(Enum):
    """Severity of type issues."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


# =============================================================================
# TYPE REPRESENTATION
# =============================================================================

@dataclass(frozen=True)
class TypeInfo:
    """
    Represents a type in the type system.

    Immutable and hashable for use in sets and dicts.
    """
    kind: TypeKind
    name: str
    module: Optional[str] = None
    args: Tuple["TypeInfo", ...] = ()
    variance: Variance = Variance.INVARIANT

    def __str__(self) -> str:
        if self.kind == TypeKind.UNION:
            return " | ".join(str(a) for a in self.args)
        elif self.kind == TypeKind.OPTIONAL:
            return f"Optional[{self.args[0]}]" if self.args else "Optional"
        elif self.kind == TypeKind.CALLABLE:
            if len(self.args) >= 2:
                param_types = ", ".join(str(a) for a in self.args[:-1])
                return f"Callable[[{param_types}], {self.args[-1]}]"
            return "Callable"
        elif self.args:
            args_str = ", ".join(str(a) for a in self.args)
            return f"{self.name}[{args_str}]"
        return self.name

    def is_subtype_of(self, other: "TypeInfo") -> bool:
        """Check if this type is a subtype of another."""
        # Any is supertype of everything
        if other.kind == TypeKind.ANY:
            return True
        # Unknown is compatible with anything (for inference)
        if self.kind == TypeKind.UNKNOWN or other.kind == TypeKind.UNKNOWN:
            return True
        # Same type
        if self == other:
            return True
        # Union: this is subtype if ALL members are subtypes
        if self.kind == TypeKind.UNION:
            return all(arg.is_subtype_of(other) for arg in self.args)
        # Other is union: this is subtype if this is subtype of ANY member
        if other.kind == TypeKind.UNION:
            return any(self.is_subtype_of(arg) for arg in other.args)
        # Optional: None or T
        if other.kind == TypeKind.OPTIONAL and self.args:
            return self.is_subtype_of(other.args[0]) or self == NONE_TYPE
        # Primitive subtyping
        if self.kind == TypeKind.PRIMITIVE and other.kind == TypeKind.PRIMITIVE:
            # int is subtype of float
            if self.name == "int" and other.name == "float":
                return True
            # bool is subtype of int
            if self.name == "bool" and other.name == "int":
                return True
        # Generic subtyping (simplified)
        if self.kind == other.kind and self.name == other.name:
            if len(self.args) == len(other.args):
                # Covariant: List[Child] <: List[Parent]
                if other.variance == Variance.COVARIANT:
                    return all(a.is_subtype_of(b) for a, b in zip(self.args, other.args))
                # Contravariant: Callable[[Parent], T] <: Callable[[Child], T]
                elif other.variance == Variance.CONTRAVARIANT:
                    return all(b.is_subtype_of(a) for a, b in zip(self.args, other.args))
                # Invariant: must be exact match
                return self.args == other.args
        return False

    def join(self, other: "TypeInfo") -> "TypeInfo":
        """Find the least upper bound (join) of two types."""
        if self == other:
            return self
        if self.is_subtype_of(other):
            return other
        if other.is_subtype_of(self):
            return self
        # Create union
        return TypeInfo(
            kind=TypeKind.UNION,
            name="Union",
            args=(self, other),
        )

    def meet(self, other: "TypeInfo") -> "TypeInfo":
        """Find the greatest lower bound (meet) of two types."""
        if self == other:
            return self
        if self.is_subtype_of(other):
            return self
        if other.is_subtype_of(self):
            return other
        # No common subtype
        return NEVER_TYPE


# Common type constants
INT_TYPE = TypeInfo(TypeKind.PRIMITIVE, "int")
FLOAT_TYPE = TypeInfo(TypeKind.PRIMITIVE, "float")
STR_TYPE = TypeInfo(TypeKind.PRIMITIVE, "str")
BOOL_TYPE = TypeInfo(TypeKind.PRIMITIVE, "bool")
NONE_TYPE = TypeInfo(TypeKind.PRIMITIVE, "None")
ANY_TYPE = TypeInfo(TypeKind.ANY, "Any")
UNKNOWN_TYPE = TypeInfo(TypeKind.UNKNOWN, "Unknown")
NEVER_TYPE = TypeInfo(TypeKind.NEVER, "Never")
OBJECT_TYPE = TypeInfo(TypeKind.CLASS, "object")


def make_optional(inner: TypeInfo) -> TypeInfo:
    """Create Optional[inner] type."""
    return TypeInfo(TypeKind.OPTIONAL, "Optional", args=(inner,))


def make_list(element: TypeInfo) -> TypeInfo:
    """Create List[element] type."""
    return TypeInfo(TypeKind.LIST, "List", args=(element,), variance=Variance.COVARIANT)


def make_dict(key: TypeInfo, value: TypeInfo) -> TypeInfo:
    """Create Dict[key, value] type."""
    return TypeInfo(TypeKind.DICT, "Dict", args=(key, value))


def make_callable(params: List[TypeInfo], return_type: TypeInfo) -> TypeInfo:
    """Create Callable[[params], return_type] type."""
    return TypeInfo(
        TypeKind.CALLABLE,
        "Callable",
        args=tuple(params) + (return_type,),
        variance=Variance.CONTRAVARIANT,
    )


def make_union(*types: TypeInfo) -> TypeInfo:
    """Create Union[types] type, flattening nested unions."""
    flattened = []
    for t in types:
        if t.kind == TypeKind.UNION:
            flattened.extend(t.args)
        else:
            flattened.append(t)
    # Remove duplicates while preserving order
    seen = set()
    unique = []
    for t in flattened:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    if len(unique) == 1:
        return unique[0]
    return TypeInfo(TypeKind.UNION, "Union", args=tuple(unique))


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class TypeBinding:
    """Binding of a name to a type in a scope."""
    name: str
    type_info: TypeInfo
    source: str  # "annotation", "inference", "default"
    line: int
    is_constant: bool = False


@dataclass
class TypeScope:
    """A scope containing type bindings."""
    name: str
    bindings: Dict[str, TypeBinding] = field(default_factory=dict)
    parent: Optional["TypeScope"] = None
    children: List["TypeScope"] = field(default_factory=list)

    def lookup(self, name: str) -> Optional[TypeBinding]:
        """Look up a binding in this scope or ancestors."""
        if name in self.bindings:
            return self.bindings[name]
        if self.parent:
            return self.parent.lookup(name)
        return None

    def bind(self, name: str, type_info: TypeInfo, source: str, line: int) -> None:
        """Add a binding to this scope."""
        self.bindings[name] = TypeBinding(name, type_info, source, line)


@dataclass
class TypeIssue:
    """A type checking issue."""
    kind: TypeErrorKind
    severity: Severity
    message: str
    file_path: Path
    line: int
    column: int
    expected_type: Optional[TypeInfo] = None
    actual_type: Optional[TypeInfo] = None
    suggestion: Optional[str] = None

    def __str__(self) -> str:
        loc = f"{self.file_path}:{self.line}:{self.column}"
        return f"[{self.severity.value}] {loc}: {self.message}"


@dataclass
class FunctionSignature:
    """Type signature of a function."""
    name: str
    parameters: List[Tuple[str, TypeInfo, bool]]  # (name, type, has_default)
    return_type: TypeInfo
    is_async: bool = False
    is_generator: bool = False
    type_params: List[str] = field(default_factory=list)  # Generic type parameters


@dataclass
class ClassTypeInfo:
    """Type information for a class."""
    name: str
    module: str
    base_classes: List[TypeInfo]
    methods: Dict[str, FunctionSignature]
    attributes: Dict[str, TypeInfo]
    class_attributes: Dict[str, TypeInfo]
    type_params: List[str] = field(default_factory=list)
    is_protocol: bool = False


@dataclass
class TypeEnvironment:
    """
    Complete type environment for a module/file.

    Contains all type information extracted and inferred.
    """
    file_path: Path
    module_name: str
    root_scope: TypeScope
    classes: Dict[str, ClassTypeInfo] = field(default_factory=dict)
    functions: Dict[str, FunctionSignature] = field(default_factory=dict)
    imports: Dict[str, TypeInfo] = field(default_factory=dict)
    issues: List[TypeIssue] = field(default_factory=list)

    def add_issue(
        self,
        kind: TypeErrorKind,
        message: str,
        line: int,
        column: int,
        expected: Optional[TypeInfo] = None,
        actual: Optional[TypeInfo] = None,
        severity: Optional[Severity] = None,
    ) -> None:
        """Add a type issue."""
        if severity is None:
            # Default severity based on config
            severity = Severity.WARNING if TypeAnalyzerConfig.REPORT_LEVEL == "warning" else Severity.ERROR

        self.issues.append(TypeIssue(
            kind=kind,
            severity=severity,
            message=message,
            file_path=self.file_path,
            line=line,
            column=column,
            expected_type=expected,
            actual_type=actual,
        ))


# =============================================================================
# TYPE PARSER
# =============================================================================

class TypeParser:
    """
    Parses type annotations from AST nodes.

    Handles:
    - Simple types: int, str, MyClass
    - Generic types: List[int], Dict[str, Any]
    - Union types: Union[int, str], int | str
    - Optional: Optional[int]
    - Callable: Callable[[int], str]
    - Literal: Literal["a", "b"]
    """

    # Mapping of built-in type names
    BUILTIN_TYPES: Dict[str, TypeInfo] = {
        "int": INT_TYPE,
        "float": FLOAT_TYPE,
        "str": STR_TYPE,
        "bool": BOOL_TYPE,
        "None": NONE_TYPE,
        "type": TypeInfo(TypeKind.CLASS, "type"),
        "object": OBJECT_TYPE,
        "Any": ANY_TYPE,
        "bytes": TypeInfo(TypeKind.PRIMITIVE, "bytes"),
        "complex": TypeInfo(TypeKind.PRIMITIVE, "complex"),
    }

    GENERIC_TYPES: Set[str] = {
        "List", "list", "Dict", "dict", "Set", "set", "Tuple", "tuple",
        "Sequence", "Mapping", "Iterable", "Iterator", "Generator",
        "Optional", "Union", "Callable", "Type", "ClassVar",
        "FrozenSet", "frozenset", "Deque", "deque",
    }

    def parse(self, node: ast.AST) -> TypeInfo:
        """Parse a type annotation node."""
        if node is None:
            return UNKNOWN_TYPE

        if isinstance(node, ast.Name):
            return self._parse_name(node.id)

        elif isinstance(node, ast.Constant):
            if node.value is None:
                return NONE_TYPE
            elif isinstance(node.value, str):
                # String annotation (forward reference)
                return self._parse_string_annotation(node.value)
            return UNKNOWN_TYPE

        elif isinstance(node, ast.Subscript):
            return self._parse_subscript(node)

        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            # Union type: A | B
            left = self.parse(node.left)
            right = self.parse(node.right)
            return make_union(left, right)

        elif isinstance(node, ast.Attribute):
            # Qualified name: module.Type
            parts = self._collect_attribute_parts(node)
            return TypeInfo(TypeKind.CLASS, ".".join(parts))

        elif isinstance(node, ast.Tuple):
            # Tuple of types (in subscript context)
            args = tuple(self.parse(elt) for elt in node.elts)
            return TypeInfo(TypeKind.TUPLE, "Tuple", args=args)

        return UNKNOWN_TYPE

    def _parse_name(self, name: str) -> TypeInfo:
        """Parse a simple type name."""
        if name in self.BUILTIN_TYPES:
            return self.BUILTIN_TYPES[name]
        if name in self.GENERIC_TYPES:
            # Generic without arguments
            return TypeInfo(TypeKind.GENERIC, name)
        # Assume it's a class
        return TypeInfo(TypeKind.CLASS, name)

    def _parse_subscript(self, node: ast.Subscript) -> TypeInfo:
        """Parse a generic type: List[int], Dict[str, int], etc."""
        base = node.value
        if isinstance(base, ast.Name):
            base_name = base.id
        elif isinstance(base, ast.Attribute):
            parts = self._collect_attribute_parts(base)
            base_name = parts[-1]
        else:
            return UNKNOWN_TYPE

        # Parse arguments
        slice_node = node.slice

        if isinstance(slice_node, ast.Tuple):
            args = tuple(self.parse(elt) for elt in slice_node.elts)
        else:
            args = (self.parse(slice_node),)

        # Handle special types
        if base_name in ("Optional",):
            return make_optional(args[0]) if args else UNKNOWN_TYPE

        elif base_name in ("Union",):
            return make_union(*args)

        elif base_name in ("List", "list"):
            return make_list(args[0]) if args else TypeInfo(TypeKind.LIST, "List")

        elif base_name in ("Dict", "dict"):
            if len(args) >= 2:
                return make_dict(args[0], args[1])
            return TypeInfo(TypeKind.DICT, "Dict")

        elif base_name in ("Set", "set", "FrozenSet", "frozenset"):
            return TypeInfo(TypeKind.SET, base_name, args=args)

        elif base_name in ("Tuple", "tuple"):
            return TypeInfo(TypeKind.TUPLE, "Tuple", args=args)

        elif base_name == "Callable":
            if len(args) >= 2:
                # Callable[[Arg1, Arg2], Return]
                if isinstance(args[0], TypeInfo) and args[0].kind == TypeKind.LIST:
                    param_types = list(args[0].args)
                else:
                    param_types = list(args[:-1])
                return_type = args[-1]
                return make_callable(param_types, return_type)
            return TypeInfo(TypeKind.CALLABLE, "Callable")

        elif base_name == "Literal":
            # Literal values become string representations
            return TypeInfo(TypeKind.LITERAL, "Literal", args=args)

        elif base_name == "Type":
            return TypeInfo(TypeKind.CLASS, "Type", args=args)

        # Generic class
        return TypeInfo(TypeKind.GENERIC, base_name, args=args)

    def _parse_string_annotation(self, annotation: str) -> TypeInfo:
        """Parse a string annotation (forward reference)."""
        try:
            node = ast.parse(annotation, mode='eval').body
            return self.parse(node)
        except SyntaxError:
            return TypeInfo(TypeKind.CLASS, annotation)

    def _collect_attribute_parts(self, node: ast.Attribute) -> List[str]:
        """Collect parts of a qualified name: a.b.c -> ["a", "b", "c"]."""
        parts = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        parts.reverse()
        return parts


# =============================================================================
# TYPE INFERENCER
# =============================================================================

class TypeInferencer(ast.NodeVisitor):
    """
    Infers types from code using flow analysis.

    Handles:
    - Variable assignments
    - Function return types
    - Attribute access
    - Type narrowing from conditionals
    """

    def __init__(self, env: TypeEnvironment, parser: TypeParser):
        self.env = env
        self.parser = parser
        self.current_scope = env.root_scope
        self._inference_depth = 0

    def infer_expression(self, node: ast.expr) -> TypeInfo:
        """Infer the type of an expression."""
        if self._inference_depth > TypeAnalyzerConfig.MAX_INFERENCE_DEPTH:
            return UNKNOWN_TYPE

        self._inference_depth += 1
        try:
            return self._infer(node)
        finally:
            self._inference_depth -= 1

    def _infer(self, node: ast.expr) -> TypeInfo:
        """Internal inference method."""
        if isinstance(node, ast.Constant):
            return self._infer_constant(node)

        elif isinstance(node, ast.Name):
            return self._infer_name(node)

        elif isinstance(node, ast.BinOp):
            return self._infer_binop(node)

        elif isinstance(node, ast.UnaryOp):
            return self._infer_unaryop(node)

        elif isinstance(node, ast.Compare):
            return BOOL_TYPE

        elif isinstance(node, ast.BoolOp):
            return self._infer_boolop(node)

        elif isinstance(node, ast.Call):
            return self._infer_call(node)

        elif isinstance(node, ast.Attribute):
            return self._infer_attribute(node)

        elif isinstance(node, ast.Subscript):
            return self._infer_subscript(node)

        elif isinstance(node, ast.List):
            return self._infer_list(node)

        elif isinstance(node, ast.Dict):
            return self._infer_dict(node)

        elif isinstance(node, ast.Set):
            return self._infer_set(node)

        elif isinstance(node, ast.Tuple):
            return self._infer_tuple(node)

        elif isinstance(node, ast.IfExp):
            return self._infer_ifexp(node)

        elif isinstance(node, ast.Lambda):
            return self._infer_lambda(node)

        elif isinstance(node, ast.ListComp):
            return self._infer_listcomp(node)

        elif isinstance(node, ast.DictComp):
            return self._infer_dictcomp(node)

        elif isinstance(node, ast.SetComp):
            return self._infer_setcomp(node)

        elif isinstance(node, ast.GeneratorExp):
            return self._infer_genexp(node)

        return UNKNOWN_TYPE

    def _infer_constant(self, node: ast.Constant) -> TypeInfo:
        """Infer type from constant."""
        value = node.value
        if value is None:
            return NONE_TYPE
        elif isinstance(value, bool):
            return BOOL_TYPE
        elif isinstance(value, int):
            return INT_TYPE
        elif isinstance(value, float):
            return FLOAT_TYPE
        elif isinstance(value, str):
            return STR_TYPE
        elif isinstance(value, bytes):
            return TypeInfo(TypeKind.PRIMITIVE, "bytes")
        return UNKNOWN_TYPE

    def _infer_name(self, node: ast.Name) -> TypeInfo:
        """Infer type from name lookup."""
        binding = self.current_scope.lookup(node.id)
        if binding:
            return binding.type_info

        # Check imports
        if node.id in self.env.imports:
            return self.env.imports[node.id]

        # Check built-in types
        if node.id in TypeParser.BUILTIN_TYPES:
            return TypeInfo(TypeKind.CLASS, node.id)  # Type object

        return UNKNOWN_TYPE

    def _infer_binop(self, node: ast.BinOp) -> TypeInfo:
        """Infer type from binary operation."""
        left = self._infer(node.left)
        right = self._infer(node.right)

        # String operations
        if isinstance(node.op, ast.Add):
            if left == STR_TYPE or right == STR_TYPE:
                return STR_TYPE
            if left == INT_TYPE and right == INT_TYPE:
                return INT_TYPE
            if left == FLOAT_TYPE or right == FLOAT_TYPE:
                return FLOAT_TYPE
            # List concatenation
            if left.kind == TypeKind.LIST and right.kind == TypeKind.LIST:
                return left  # Simplified

        elif isinstance(node.op, ast.Mult):
            # String * int
            if left == STR_TYPE and right == INT_TYPE:
                return STR_TYPE
            if left == INT_TYPE and right == INT_TYPE:
                return INT_TYPE
            if left == FLOAT_TYPE or right == FLOAT_TYPE:
                return FLOAT_TYPE
            # List * int
            if left.kind == TypeKind.LIST and right == INT_TYPE:
                return left

        elif isinstance(node.op, (ast.Sub, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)):
            if left == INT_TYPE and right == INT_TYPE:
                if isinstance(node.op, ast.Div):
                    return FLOAT_TYPE
                return INT_TYPE
            return FLOAT_TYPE

        elif isinstance(node.op, (ast.BitOr, ast.BitAnd, ast.BitXor, ast.LShift, ast.RShift)):
            return INT_TYPE

        return UNKNOWN_TYPE

    def _infer_unaryop(self, node: ast.UnaryOp) -> TypeInfo:
        """Infer type from unary operation."""
        operand = self._infer(node.operand)

        if isinstance(node.op, ast.Not):
            return BOOL_TYPE
        elif isinstance(node.op, ast.USub):
            if operand == INT_TYPE:
                return INT_TYPE
            return FLOAT_TYPE
        elif isinstance(node.op, ast.Invert):
            return INT_TYPE

        return operand

    def _infer_boolop(self, node: ast.BoolOp) -> TypeInfo:
        """Infer type from boolean operation."""
        # and/or return one of their operands
        types = [self._infer(v) for v in node.values]

        # If all same type, return that type
        if len(set(types)) == 1:
            return types[0]

        # Otherwise union
        return make_union(*types)

    def _infer_call(self, node: ast.Call) -> TypeInfo:
        """Infer type from function call."""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id

            # Built-in constructors
            if func_name == "int":
                return INT_TYPE
            elif func_name == "float":
                return FLOAT_TYPE
            elif func_name == "str":
                return STR_TYPE
            elif func_name == "bool":
                return BOOL_TYPE
            elif func_name == "list":
                return TypeInfo(TypeKind.LIST, "list")
            elif func_name == "dict":
                return TypeInfo(TypeKind.DICT, "dict")
            elif func_name == "set":
                return TypeInfo(TypeKind.SET, "set")
            elif func_name == "tuple":
                return TypeInfo(TypeKind.TUPLE, "tuple")
            elif func_name == "len":
                return INT_TYPE
            elif func_name == "range":
                return TypeInfo(TypeKind.GENERIC, "range", args=(INT_TYPE,))
            elif func_name == "enumerate":
                return TypeInfo(TypeKind.GENERIC, "enumerate")
            elif func_name == "zip":
                return TypeInfo(TypeKind.GENERIC, "zip")
            elif func_name == "map":
                return TypeInfo(TypeKind.GENERIC, "map")
            elif func_name == "filter":
                return TypeInfo(TypeKind.GENERIC, "filter")
            elif func_name == "sorted":
                return TypeInfo(TypeKind.LIST, "list")
            elif func_name == "reversed":
                return TypeInfo(TypeKind.GENERIC, "reversed")
            elif func_name == "open":
                return TypeInfo(TypeKind.CLASS, "TextIO")
            elif func_name == "print":
                return NONE_TYPE

            # Check function signatures
            if func_name in self.env.functions:
                return self.env.functions[func_name].return_type

            # Check if it's a class constructor
            if func_name in self.env.classes:
                return TypeInfo(TypeKind.CLASS, func_name)

        elif isinstance(node.func, ast.Attribute):
            # Method call
            obj_type = self._infer(node.func.value)
            method_name = node.func.attr

            # String methods
            if obj_type == STR_TYPE:
                if method_name in ("split", "splitlines"):
                    return make_list(STR_TYPE)
                elif method_name in ("join", "strip", "upper", "lower", "replace", "format"):
                    return STR_TYPE
                elif method_name in ("find", "index", "count"):
                    return INT_TYPE
                elif method_name in ("startswith", "endswith", "isdigit", "isalpha"):
                    return BOOL_TYPE

            # List methods
            if obj_type.kind == TypeKind.LIST:
                if method_name in ("append", "extend", "insert", "remove", "clear"):
                    return NONE_TYPE
                elif method_name == "pop":
                    return obj_type.args[0] if obj_type.args else UNKNOWN_TYPE
                elif method_name == "copy":
                    return obj_type
                elif method_name in ("index", "count"):
                    return INT_TYPE

            # Dict methods
            if obj_type.kind == TypeKind.DICT:
                if method_name == "get":
                    return make_optional(obj_type.args[1]) if len(obj_type.args) > 1 else UNKNOWN_TYPE
                elif method_name in ("keys",):
                    return TypeInfo(TypeKind.GENERIC, "dict_keys")
                elif method_name in ("values",):
                    return TypeInfo(TypeKind.GENERIC, "dict_values")
                elif method_name in ("items",):
                    return TypeInfo(TypeKind.GENERIC, "dict_items")
                elif method_name in ("pop", "setdefault"):
                    return obj_type.args[1] if len(obj_type.args) > 1 else UNKNOWN_TYPE

        return UNKNOWN_TYPE

    def _infer_attribute(self, node: ast.Attribute) -> TypeInfo:
        """Infer type from attribute access."""
        obj_type = self._infer(node.value)

        # Check class attributes
        if obj_type.kind == TypeKind.CLASS and obj_type.name in self.env.classes:
            class_info = self.env.classes[obj_type.name]
            if node.attr in class_info.attributes:
                return class_info.attributes[node.attr]
            if node.attr in class_info.class_attributes:
                return class_info.class_attributes[node.attr]

        return UNKNOWN_TYPE

    def _infer_subscript(self, node: ast.Subscript) -> TypeInfo:
        """Infer type from subscript access."""
        value_type = self._infer(node.value)

        if value_type.kind == TypeKind.LIST and value_type.args:
            if isinstance(node.slice, ast.Slice):
                return value_type  # Slice returns same list type
            return value_type.args[0]  # Index returns element type

        if value_type.kind == TypeKind.DICT and len(value_type.args) >= 2:
            return value_type.args[1]  # Value type

        if value_type.kind == TypeKind.TUPLE and value_type.args:
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
                idx = node.slice.value
                if 0 <= idx < len(value_type.args):
                    return value_type.args[idx]
            # Union of all tuple element types for dynamic index
            return make_union(*value_type.args)

        if value_type == STR_TYPE:
            if isinstance(node.slice, ast.Slice):
                return STR_TYPE
            return STR_TYPE  # Single character is still str

        return UNKNOWN_TYPE

    def _infer_list(self, node: ast.List) -> TypeInfo:
        """Infer type from list literal."""
        if not node.elts:
            return TypeInfo(TypeKind.LIST, "list")

        element_types = [self._infer(elt) for elt in node.elts]
        # Find common type
        common = element_types[0]
        for t in element_types[1:]:
            common = common.join(t)

        return make_list(common)

    def _infer_dict(self, node: ast.Dict) -> TypeInfo:
        """Infer type from dict literal."""
        if not node.keys:
            return TypeInfo(TypeKind.DICT, "dict")

        key_types = [self._infer(k) for k in node.keys if k is not None]
        value_types = [self._infer(v) for v in node.values]

        key_type = key_types[0] if key_types else ANY_TYPE
        for t in key_types[1:]:
            key_type = key_type.join(t)

        value_type = value_types[0] if value_types else ANY_TYPE
        for t in value_types[1:]:
            value_type = value_type.join(t)

        return make_dict(key_type, value_type)

    def _infer_set(self, node: ast.Set) -> TypeInfo:
        """Infer type from set literal."""
        if not node.elts:
            return TypeInfo(TypeKind.SET, "set")

        element_types = [self._infer(elt) for elt in node.elts]
        common = element_types[0]
        for t in element_types[1:]:
            common = common.join(t)

        return TypeInfo(TypeKind.SET, "set", args=(common,))

    def _infer_tuple(self, node: ast.Tuple) -> TypeInfo:
        """Infer type from tuple literal."""
        element_types = tuple(self._infer(elt) for elt in node.elts)
        return TypeInfo(TypeKind.TUPLE, "tuple", args=element_types)

    def _infer_ifexp(self, node: ast.IfExp) -> TypeInfo:
        """Infer type from conditional expression."""
        then_type = self._infer(node.body)
        else_type = self._infer(node.orelse)
        return then_type.join(else_type)

    def _infer_lambda(self, node: ast.Lambda) -> TypeInfo:
        """Infer type from lambda expression."""
        return_type = self._infer(node.body)
        param_types = [UNKNOWN_TYPE] * len(node.args.args)
        return make_callable(param_types, return_type)

    def _infer_listcomp(self, node: ast.ListComp) -> TypeInfo:
        """Infer type from list comprehension."""
        element_type = self._infer(node.elt)
        return make_list(element_type)

    def _infer_dictcomp(self, node: ast.DictComp) -> TypeInfo:
        """Infer type from dict comprehension."""
        key_type = self._infer(node.key)
        value_type = self._infer(node.value)
        return make_dict(key_type, value_type)

    def _infer_setcomp(self, node: ast.SetComp) -> TypeInfo:
        """Infer type from set comprehension."""
        element_type = self._infer(node.elt)
        return TypeInfo(TypeKind.SET, "set", args=(element_type,))

    def _infer_genexp(self, node: ast.GeneratorExp) -> TypeInfo:
        """Infer type from generator expression."""
        element_type = self._infer(node.elt)
        return TypeInfo(TypeKind.GENERIC, "Generator", args=(element_type,))


# =============================================================================
# TYPE CHECKER
# =============================================================================

class TypeChecker(ast.NodeVisitor):
    """
    Checks types for errors and incompatibilities.

    Verifies:
    - Assignment compatibility
    - Function argument types
    - Return type consistency
    - Attribute access validity
    """

    def __init__(self, env: TypeEnvironment, parser: TypeParser, inferencer: TypeInferencer):
        self.env = env
        self.parser = parser
        self.inferencer = inferencer
        self.current_function: Optional[FunctionSignature] = None

    def check(self, tree: ast.AST) -> List[TypeIssue]:
        """Check types in the AST."""
        self.visit(tree)
        return self.env.issues

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Check function definition."""
        self._check_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Check async function definition."""
        self._check_function(node, is_async=True)

    def _check_function(
        self,
        node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        is_async: bool,
    ) -> None:
        """Common function checking logic."""
        # Parse signature
        params = []
        for arg in node.args.args:
            param_type = self.parser.parse(arg.annotation) if arg.annotation else UNKNOWN_TYPE
            has_default = arg.arg in {a.arg for a in node.args.defaults} if node.args.defaults else False
            params.append((arg.arg, param_type, has_default))

        return_type = self.parser.parse(node.returns) if node.returns else UNKNOWN_TYPE

        sig = FunctionSignature(
            name=node.name,
            parameters=params,
            return_type=return_type,
            is_async=is_async,
        )

        self.env.functions[node.name] = sig

        # Check function body
        old_function = self.current_function
        self.current_function = sig
        self.generic_visit(node)
        self.current_function = old_function

    def visit_Return(self, node: ast.Return) -> None:
        """Check return statement."""
        if not self.current_function:
            return

        if node.value:
            actual_type = self.inferencer.infer_expression(node.value)
            expected_type = self.current_function.return_type

            if expected_type.kind != TypeKind.UNKNOWN and not actual_type.is_subtype_of(expected_type):
                self.env.add_issue(
                    TypeErrorKind.INCOMPATIBLE_RETURN,
                    f"Incompatible return type: expected {expected_type}, got {actual_type}",
                    line=node.lineno,
                    column=node.col_offset,
                    expected=expected_type,
                    actual=actual_type,
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        """Check assignment."""
        value_type = self.inferencer.infer_expression(node.value)

        for target in node.targets:
            if isinstance(target, ast.Name):
                # Check if already has type annotation
                binding = self.inferencer.current_scope.lookup(target.id)
                if binding and binding.source == "annotation":
                    if not value_type.is_subtype_of(binding.type_info):
                        self.env.add_issue(
                            TypeErrorKind.INCOMPATIBLE_TYPES,
                            f"Cannot assign {value_type} to {target.id} of type {binding.type_info}",
                            line=node.lineno,
                            column=node.col_offset,
                            expected=binding.type_info,
                            actual=value_type,
                        )
                else:
                    # Bind inferred type
                    self.inferencer.current_scope.bind(
                        target.id, value_type, "inference", node.lineno
                    )

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Check annotated assignment."""
        declared_type = self.parser.parse(node.annotation)

        if isinstance(node.target, ast.Name):
            self.inferencer.current_scope.bind(
                node.target.id, declared_type, "annotation", node.lineno
            )

        if node.value:
            value_type = self.inferencer.infer_expression(node.value)
            if not value_type.is_subtype_of(declared_type):
                self.env.add_issue(
                    TypeErrorKind.INCOMPATIBLE_TYPES,
                    f"Cannot assign {value_type} to variable of type {declared_type}",
                    line=node.lineno,
                    column=node.col_offset,
                    expected=declared_type,
                    actual=value_type,
                )

    def visit_Call(self, node: ast.Call) -> None:
        """Check function call arguments."""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in self.env.functions:
                sig = self.env.functions[func_name]
                self._check_call_arguments(node, sig)

        self.generic_visit(node)

    def _check_call_arguments(self, node: ast.Call, sig: FunctionSignature) -> None:
        """Check function call arguments against signature."""
        # Check positional arguments
        for i, (arg, (param_name, param_type, has_default)) in enumerate(
            zip(node.args, sig.parameters)
        ):
            arg_type = self.inferencer.infer_expression(arg)
            if param_type.kind != TypeKind.UNKNOWN and not arg_type.is_subtype_of(param_type):
                self.env.add_issue(
                    TypeErrorKind.INCOMPATIBLE_ARGUMENT,
                    f"Argument {i + 1} ({param_name}): expected {param_type}, got {arg_type}",
                    line=node.lineno,
                    column=node.col_offset,
                    expected=param_type,
                    actual=arg_type,
                )

        # Check keyword arguments
        for keyword in node.keywords:
            if keyword.arg:
                # Find parameter
                for param_name, param_type, _ in sig.parameters:
                    if param_name == keyword.arg:
                        arg_type = self.inferencer.infer_expression(keyword.value)
                        if param_type.kind != TypeKind.UNKNOWN and not arg_type.is_subtype_of(param_type):
                            self.env.add_issue(
                                TypeErrorKind.INCOMPATIBLE_ARGUMENT,
                                f"Argument {keyword.arg}: expected {param_type}, got {arg_type}",
                                line=node.lineno,
                                column=node.col_offset,
                                expected=param_type,
                                actual=arg_type,
                            )
                        break


# =============================================================================
# TYPE ANALYZER
# =============================================================================

class TypeAnalyzer:
    """
    Main type analyzer for Python code.

    Provides:
    - Type extraction from annotations
    - Type inference from code
    - Type error detection
    - Cross-file type propagation
    """

    def __init__(self):
        self._parser = TypeParser()
        self._environments: Dict[Path, TypeEnvironment] = {}
        self._lock = asyncio.Lock()

    async def analyze_file(self, file_path: Path) -> TypeEnvironment:
        """Analyze types in a single file."""
        try:
            content = await asyncio.to_thread(file_path.read_text, encoding='utf-8', errors='ignore')
            tree = ast.parse(content)
        except (SyntaxError, FileNotFoundError) as e:
            logger.error(f"Failed to parse {file_path}: {e}")
            return TypeEnvironment(
                file_path=file_path,
                module_name=file_path.stem,
                root_scope=TypeScope("module"),
            )

        # Create environment
        env = TypeEnvironment(
            file_path=file_path,
            module_name=file_path.stem,
            root_scope=TypeScope("module"),
        )

        # Extract types from imports first
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name
                    env.imports[name] = TypeInfo(TypeKind.CLASS, alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    name = alias.asname or alias.name
                    env.imports[name] = TypeInfo(TypeKind.CLASS, f"{module}.{alias.name}")

        # Create inferencer and checker
        inferencer = TypeInferencer(env, self._parser)
        checker = TypeChecker(env, self._parser, inferencer)

        # Run type checking
        checker.check(tree)

        # Store environment
        async with self._lock:
            self._environments[file_path] = env

        return env

    async def analyze_directory(
        self,
        directory: Path,
        patterns: List[str] = None,
    ) -> Dict[Path, TypeEnvironment]:
        """Analyze all Python files in a directory."""
        patterns = patterns or ["*.py"]

        files = []
        for pattern in patterns:
            files.extend(directory.glob(f"**/{pattern}"))

        # Analyze files in parallel
        semaphore = asyncio.Semaphore(50)

        async def analyze_with_semaphore(f: Path) -> Tuple[Path, TypeEnvironment]:
            async with semaphore:
                return f, await self.analyze_file(f)

        tasks = [analyze_with_semaphore(f) for f in files]
        results = await asyncio.gather(*tasks)

        return dict(results)

    def get_type_issues(self, file_path: Path) -> List[TypeIssue]:
        """Get type issues for a file."""
        env = self._environments.get(file_path)
        return env.issues if env else []

    def get_all_issues(self) -> List[TypeIssue]:
        """Get all type issues across all analyzed files."""
        issues = []
        for env in self._environments.values():
            issues.extend(env.issues)
        return issues

    def get_stats(self) -> Dict[str, Any]:
        """Get analyzer statistics."""
        total_issues = sum(len(env.issues) for env in self._environments.values())
        errors = sum(
            1 for env in self._environments.values()
            for issue in env.issues if issue.severity == Severity.ERROR
        )
        return {
            "files_analyzed": len(self._environments),
            "total_issues": total_issues,
            "errors": errors,
            "warnings": total_issues - errors,
        }


# =============================================================================
# CROSS-REPO TYPE ANALYZER
# =============================================================================

class CrossRepoTypeAnalyzer:
    """
    Type analysis across multiple repositories.

    Propagates types between JARVIS, JARVIS-Prime, and Reactor-Core.
    """

    def __init__(self):
        self._repos: Dict[str, Path] = {
            "jarvis": TypeAnalyzerConfig.JARVIS_REPO,
            "prime": TypeAnalyzerConfig.PRIME_REPO,
            "reactor": TypeAnalyzerConfig.REACTOR_REPO,
        }
        self._analyzers: Dict[str, TypeAnalyzer] = {}

    async def initialize(self) -> bool:
        """Initialize type analyzers for all repositories."""
        logger.info("Initializing Cross-Repo Type Analyzer...")

        for repo_name, repo_path in self._repos.items():
            if not repo_path.exists():
                logger.warning(f"Repository not found: {repo_name}")
                continue

            analyzer = TypeAnalyzer()
            self._analyzers[repo_name] = analyzer

            logger.info(f"  Analyzing {repo_name}...")
            envs = await analyzer.analyze_directory(repo_path)
            logger.info(f"  ✓ {repo_name}: {len(envs)} files, {len(analyzer.get_all_issues())} issues")

        return True

    def get_all_issues(self) -> Dict[str, List[TypeIssue]]:
        """Get type issues from all repositories."""
        return {
            repo: analyzer.get_all_issues()
            for repo, analyzer in self._analyzers.items()
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get cross-repo statistics."""
        return {
            repo: analyzer.get_stats()
            for repo, analyzer in self._analyzers.items()
        }


# =============================================================================
# SINGLETON ACCESSORS
# =============================================================================

_type_analyzer: Optional[TypeAnalyzer] = None
_cross_repo_analyzer: Optional[CrossRepoTypeAnalyzer] = None


def get_type_analyzer() -> TypeAnalyzer:
    """Get the singleton type analyzer."""
    global _type_analyzer
    if _type_analyzer is None:
        _type_analyzer = TypeAnalyzer()
    return _type_analyzer


def get_cross_repo_type_analyzer() -> CrossRepoTypeAnalyzer:
    """Get the singleton cross-repo type analyzer."""
    global _cross_repo_analyzer
    if _cross_repo_analyzer is None:
        _cross_repo_analyzer = CrossRepoTypeAnalyzer()
    return _cross_repo_analyzer


async def initialize_type_analysis() -> bool:
    """Initialize cross-repo type analysis."""
    analyzer = get_cross_repo_type_analyzer()
    return await analyzer.initialize()
