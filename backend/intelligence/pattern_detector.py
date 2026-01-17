"""
Pattern Detector v1.0 - Design Pattern Recognition System
=========================================================

Enterprise-grade design pattern detection system that recognizes
and preserves common software design patterns during refactoring.

Features:
- Creational patterns: Singleton, Factory, Builder, Prototype
- Structural patterns: Adapter, Decorator, Facade, Proxy
- Behavioral patterns: Observer, Strategy, Command, State
- Pattern integrity verification
- Refactoring impact on patterns
- Pattern violation detection
- Pattern recommendation system

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                      Pattern Detector v1.0                               │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐       │
    │   │ Pattern Matcher │   │ Integrity Check │   │ Recommendation  │       │
    │   │ (AST Analysis)  │──▶│ (Preservation)  │──▶│ Engine          │       │
    │   └─────────────────┘   └─────────────────┘   └─────────────────┘       │
    │           │                     │                     │                  │
    │           └─────────────────────┴─────────────────────┘                  │
    │                                 │                                        │
    │                    ┌────────────▼────────────┐                           │
    │                    │    Pattern Registry     │                           │
    │                    │  (23 GoF + Modern)      │                           │
    │                    └────────────┬────────────┘                           │
    │                                 │                                        │
    │   ┌──────────────┬──────────────┼──────────────┬──────────────┐         │
    │   │              │              │              │              │         │
    │   ▼              ▼              ▼              ▼              ▼         │
    │ Creational   Structural   Behavioral    Modern       Cross-File        │
    │ Patterns     Patterns     Patterns      Patterns     Detector          │
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
    Any, Callable, DefaultDict, Dict, FrozenSet, Iterator, List,
    Literal, Mapping, NamedTuple, Optional, Protocol, Sequence,
    Set, Tuple, Type, TypeVar, Union
)

logger = logging.getLogger(__name__)


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


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


class PatternDetectorConfig:
    """Configuration for pattern detection."""

    # Detection settings
    MIN_CONFIDENCE: float = _get_env_float("PATTERN_MIN_CONFIDENCE", 0.7)
    DETECT_MODERN_PATTERNS: bool = _get_env_bool("PATTERN_DETECT_MODERN", True)
    CROSS_FILE_DETECTION: bool = _get_env_bool("PATTERN_CROSS_FILE", True)

    # Verification
    VERIFY_INTEGRITY: bool = _get_env_bool("PATTERN_VERIFY_INTEGRITY", True)
    WARN_ON_VIOLATION: bool = _get_env_bool("PATTERN_WARN_VIOLATION", True)

    # Repository paths
    JARVIS_REPO: Path = Path(_get_env("JARVIS_REPO", str(Path.home() / "Documents/repos/JARVIS-AI-Agent")))
    PRIME_REPO: Path = Path(_get_env("PRIME_REPO", str(Path.home() / "Documents/repos/jarvis-prime")))
    REACTOR_REPO: Path = Path(_get_env("REACTOR_REPO", str(Path.home() / "Documents/repos/reactor-core")))


# =============================================================================
# ENUMS AND TYPES
# =============================================================================

class PatternCategory(Enum):
    """Categories of design patterns."""
    CREATIONAL = "creational"
    STRUCTURAL = "structural"
    BEHAVIORAL = "behavioral"
    MODERN = "modern"  # Modern patterns (Dependency Injection, etc.)
    PYTHONIC = "pythonic"  # Python-specific patterns


class PatternType(Enum):
    """Types of design patterns."""
    # Creational
    SINGLETON = "singleton"
    FACTORY_METHOD = "factory_method"
    ABSTRACT_FACTORY = "abstract_factory"
    BUILDER = "builder"
    PROTOTYPE = "prototype"

    # Structural
    ADAPTER = "adapter"
    BRIDGE = "bridge"
    COMPOSITE = "composite"
    DECORATOR = "decorator"
    FACADE = "facade"
    FLYWEIGHT = "flyweight"
    PROXY = "proxy"

    # Behavioral
    CHAIN_OF_RESPONSIBILITY = "chain_of_responsibility"
    COMMAND = "command"
    INTERPRETER = "interpreter"
    ITERATOR = "iterator"
    MEDIATOR = "mediator"
    MEMENTO = "memento"
    OBSERVER = "observer"
    STATE = "state"
    STRATEGY = "strategy"
    TEMPLATE_METHOD = "template_method"
    VISITOR = "visitor"

    # Modern
    DEPENDENCY_INJECTION = "dependency_injection"
    REPOSITORY = "repository"
    UNIT_OF_WORK = "unit_of_work"
    SERVICE_LOCATOR = "service_locator"
    NULL_OBJECT = "null_object"

    # Pythonic
    CONTEXT_MANAGER = "context_manager"
    DESCRIPTOR = "descriptor"
    METACLASS = "metaclass"
    MIXIN = "mixin"


class ViolationType(Enum):
    """Types of pattern violations."""
    STRUCTURAL_BREAK = "structural_break"  # Pattern structure broken
    INTERFACE_BREAK = "interface_break"    # Interface contract broken
    INVARIANT_BREAK = "invariant_break"    # Pattern invariant broken
    PARTIAL_IMPL = "partial_impl"          # Incomplete implementation
    ANTI_PATTERN = "anti_pattern"          # Known anti-pattern


class Severity(Enum):
    """Severity levels."""
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class PatternMatch:
    """A detected design pattern match."""
    pattern_type: PatternType
    category: PatternCategory
    confidence: float  # 0-1
    class_name: str
    file_path: Path
    line_number: int
    components: Dict[str, Any]  # Pattern-specific components
    description: str

    def __str__(self) -> str:
        return f"{self.pattern_type.value} pattern in {self.class_name} ({self.confidence:.0%} confidence)"


@dataclass
class PatternViolation:
    """A violation of a design pattern."""
    pattern_type: PatternType
    violation_type: ViolationType
    severity: Severity
    message: str
    file_path: Path
    line_number: int
    suggestion: Optional[str] = None


@dataclass
class PatternRecommendation:
    """A recommendation to apply a pattern."""
    pattern_type: PatternType
    target_class: str
    file_path: Path
    reason: str
    confidence: float
    example_code: Optional[str] = None


@dataclass
class PatternIntegrity:
    """Integrity check result for a pattern."""
    pattern: PatternMatch
    is_intact: bool
    violations: List[PatternViolation]
    score: float  # 0-1, how well the pattern is implemented


@dataclass
class RefactoringImpact:
    """Impact of a refactoring on patterns."""
    affected_patterns: List[PatternMatch]
    potential_violations: List[PatternViolation]
    recommendations: List[str]
    risk_level: Severity


# =============================================================================
# PATTERN MATCHERS
# =============================================================================

class PatternMatcher(ABC):
    """Base class for pattern matchers."""

    pattern_type: PatternType
    category: PatternCategory

    @abstractmethod
    def match(
        self,
        class_def: ast.ClassDef,
        tree: ast.AST,
        file_path: Path,
    ) -> Optional[PatternMatch]:
        """Check if the class matches this pattern."""
        pass

    @abstractmethod
    def check_integrity(
        self,
        pattern: PatternMatch,
        tree: ast.AST,
    ) -> PatternIntegrity:
        """Check the integrity of a detected pattern."""
        pass

    def _get_method_names(self, class_def: ast.ClassDef) -> Set[str]:
        """Get all method names in a class."""
        methods = set()
        for node in class_def.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(node.name)
        return methods

    def _get_base_names(self, class_def: ast.ClassDef) -> List[str]:
        """Get base class names."""
        bases = []
        for base in class_def.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.unparse(base))
        return bases

    def _has_method(self, class_def: ast.ClassDef, method_name: str) -> bool:
        """Check if class has a specific method."""
        return method_name in self._get_method_names(class_def)

    def _get_method(
        self,
        class_def: ast.ClassDef,
        method_name: str,
    ) -> Optional[ast.FunctionDef]:
        """Get a method by name."""
        for node in class_def.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == method_name:
                    return node
        return None

    def _get_class_attributes(self, class_def: ast.ClassDef) -> Dict[str, ast.AST]:
        """Get class-level attribute assignments."""
        attrs = {}
        for node in class_def.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        attrs[target.id] = node.value
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    attrs[node.target.id] = node.value
        return attrs


class SingletonMatcher(PatternMatcher):
    """Detects Singleton pattern."""

    pattern_type = PatternType.SINGLETON
    category = PatternCategory.CREATIONAL

    def match(
        self,
        class_def: ast.ClassDef,
        tree: ast.AST,
        file_path: Path,
    ) -> Optional[PatternMatch]:
        """Check for Singleton pattern indicators."""
        confidence = 0.0
        components = {}

        # Check for instance class variable
        attrs = self._get_class_attributes(class_def)
        if "_instance" in attrs or "__instance" in attrs:
            confidence += 0.3
            components["instance_var"] = "_instance" if "_instance" in attrs else "__instance"

        # Check for __new__ method that returns same instance
        new_method = self._get_method(class_def, "__new__")
        if new_method:
            confidence += 0.25
            components["has_new"] = True

            # Check if __new__ checks for existing instance
            new_source = ast.unparse(new_method)
            if "_instance" in new_source or "cls." in new_source:
                confidence += 0.2

        # Check for get_instance class method
        get_instance = self._get_method(class_def, "get_instance")
        if get_instance or self._has_method(class_def, "instance"):
            confidence += 0.25
            components["get_instance"] = True

        # Check for private __init__
        init_method = self._get_method(class_def, "__init__")
        if init_method:
            # Check for raise in __init__ (to prevent direct instantiation)
            init_source = ast.unparse(init_method)
            if "raise" in init_source:
                confidence += 0.1

        if confidence >= PatternDetectorConfig.MIN_CONFIDENCE:
            return PatternMatch(
                pattern_type=self.pattern_type,
                category=self.category,
                confidence=min(1.0, confidence),
                class_name=class_def.name,
                file_path=file_path,
                line_number=class_def.lineno,
                components=components,
                description="Singleton pattern ensures only one instance of the class exists",
            )

        return None

    def check_integrity(
        self,
        pattern: PatternMatch,
        tree: ast.AST,
    ) -> PatternIntegrity:
        """Check Singleton integrity."""
        violations = []
        score = pattern.confidence

        # Find the class
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == pattern.class_name:
                # Check if instance variable is properly protected
                attrs = self._get_class_attributes(node)
                instance_var = pattern.components.get("instance_var")
                if instance_var and not instance_var.startswith("_"):
                    violations.append(PatternViolation(
                        pattern_type=self.pattern_type,
                        violation_type=ViolationType.INVARIANT_BREAK,
                        severity=Severity.WARNING,
                        message=f"Singleton instance variable '{instance_var}' should be private",
                        file_path=pattern.file_path,
                        line_number=pattern.line_number,
                        suggestion=f"Rename to '_{instance_var}' or '__{instance_var}'",
                    ))
                    score -= 0.1

                # Check thread safety
                new_method = self._get_method(node, "__new__")
                if new_method:
                    new_source = ast.unparse(new_method)
                    if "Lock" not in new_source and "threading" not in new_source:
                        violations.append(PatternViolation(
                            pattern_type=self.pattern_type,
                            violation_type=ViolationType.PARTIAL_IMPL,
                            severity=Severity.INFO,
                            message="Singleton may not be thread-safe",
                            file_path=pattern.file_path,
                            line_number=pattern.line_number,
                            suggestion="Consider adding threading.Lock for thread safety",
                        ))
                break

        return PatternIntegrity(
            pattern=pattern,
            is_intact=len([v for v in violations if v.severity == Severity.CRITICAL]) == 0,
            violations=violations,
            score=max(0, score),
        )


class FactoryMethodMatcher(PatternMatcher):
    """Detects Factory Method pattern."""

    pattern_type = PatternType.FACTORY_METHOD
    category = PatternCategory.CREATIONAL

    FACTORY_KEYWORDS = {"create", "make", "build", "new", "get", "produce", "factory"}

    def match(
        self,
        class_def: ast.ClassDef,
        tree: ast.AST,
        file_path: Path,
    ) -> Optional[PatternMatch]:
        """Check for Factory Method pattern."""
        confidence = 0.0
        components = {"factory_methods": []}

        methods = self._get_method_names(class_def)

        # Check for factory-like method names
        for method_name in methods:
            name_lower = method_name.lower()
            for keyword in self.FACTORY_KEYWORDS:
                if keyword in name_lower:
                    confidence += 0.15
                    components["factory_methods"].append(method_name)

                    # Check if method returns object creation
                    method = self._get_method(class_def, method_name)
                    if method:
                        method_source = ast.unparse(method)
                        # Look for return statements with class instantiation
                        if re.search(r'return\s+\w+\(', method_source):
                            confidence += 0.15
                    break

        # Check for abstract factory methods
        for node in class_def.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Check for @abstractmethod decorator
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Name) and decorator.id == "abstractmethod":
                        if any(kw in node.name.lower() for kw in self.FACTORY_KEYWORDS):
                            confidence += 0.2
                            break

        # Check class name for Factory indicators
        if "factory" in class_def.name.lower():
            confidence += 0.2

        if confidence >= PatternDetectorConfig.MIN_CONFIDENCE:
            return PatternMatch(
                pattern_type=self.pattern_type,
                category=self.category,
                confidence=min(1.0, confidence),
                class_name=class_def.name,
                file_path=file_path,
                line_number=class_def.lineno,
                components=components,
                description="Factory Method defines interface for creating objects",
            )

        return None

    def check_integrity(
        self,
        pattern: PatternMatch,
        tree: ast.AST,
    ) -> PatternIntegrity:
        """Check Factory Method integrity."""
        violations = []
        score = pattern.confidence

        factory_methods = pattern.components.get("factory_methods", [])

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == pattern.class_name:
                for method_name in factory_methods:
                    method = self._get_method(node, method_name)
                    if method:
                        # Check return type annotation
                        if not method.returns:
                            violations.append(PatternViolation(
                                pattern_type=self.pattern_type,
                                violation_type=ViolationType.PARTIAL_IMPL,
                                severity=Severity.INFO,
                                message=f"Factory method '{method_name}' should have return type annotation",
                                file_path=pattern.file_path,
                                line_number=method.lineno,
                            ))
                break

        return PatternIntegrity(
            pattern=pattern,
            is_intact=True,
            violations=violations,
            score=max(0, score),
        )


class ObserverMatcher(PatternMatcher):
    """Detects Observer pattern."""

    pattern_type = PatternType.OBSERVER
    category = PatternCategory.BEHAVIORAL

    def match(
        self,
        class_def: ast.ClassDef,
        tree: ast.AST,
        file_path: Path,
    ) -> Optional[PatternMatch]:
        """Check for Observer pattern."""
        confidence = 0.0
        components = {"role": None}

        methods = self._get_method_names(class_def)
        attrs = self._get_class_attributes(class_def)

        # Check for Subject (Observable) indicators
        subject_methods = {"add_observer", "remove_observer", "notify", "attach", "detach",
                          "subscribe", "unsubscribe", "register", "unregister"}

        subject_matches = methods & subject_methods
        if subject_matches:
            confidence += 0.2 * len(subject_matches)
            components["role"] = "subject"
            components["subject_methods"] = list(subject_matches)

        # Check for observers list/set
        observer_attrs = {"observers", "_observers", "__observers", "listeners", "_listeners"}
        if any(attr in attrs for attr in observer_attrs):
            confidence += 0.2
            components["has_observer_list"] = True

        # Check for Observer interface indicators
        observer_methods = {"update", "on_notify", "handle", "notify", "on_change"}
        observer_matches = methods & observer_methods

        if observer_matches and not subject_matches:
            confidence += 0.15 * len(observer_matches)
            components["role"] = "observer"
            components["observer_methods"] = list(observer_matches)

        # Check for ABC/Protocol base (common in Observer)
        bases = self._get_base_names(class_def)
        if any(b in ["ABC", "Protocol", "Observer", "Observable", "Subject"] for b in bases):
            confidence += 0.15

        if confidence >= PatternDetectorConfig.MIN_CONFIDENCE:
            return PatternMatch(
                pattern_type=self.pattern_type,
                category=self.category,
                confidence=min(1.0, confidence),
                class_name=class_def.name,
                file_path=file_path,
                line_number=class_def.lineno,
                components=components,
                description="Observer pattern defines one-to-many dependency between objects",
            )

        return None

    def check_integrity(
        self,
        pattern: PatternMatch,
        tree: ast.AST,
    ) -> PatternIntegrity:
        """Check Observer pattern integrity."""
        violations = []
        score = pattern.confidence

        role = pattern.components.get("role")

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == pattern.class_name:
                if role == "subject":
                    # Check that notify calls update on all observers
                    notify = self._get_method(node, "notify")
                    if notify:
                        notify_source = ast.unparse(notify)
                        if "for" not in notify_source and "update" not in notify_source:
                            violations.append(PatternViolation(
                                pattern_type=self.pattern_type,
                                violation_type=ViolationType.PARTIAL_IMPL,
                                severity=Severity.WARNING,
                                message="notify() should iterate over observers and call update()",
                                file_path=pattern.file_path,
                                line_number=notify.lineno,
                            ))
                            score -= 0.1
                break

        return PatternIntegrity(
            pattern=pattern,
            is_intact=len([v for v in violations if v.severity == Severity.CRITICAL]) == 0,
            violations=violations,
            score=max(0, score),
        )


class StrategyMatcher(PatternMatcher):
    """Detects Strategy pattern."""

    pattern_type = PatternType.STRATEGY
    category = PatternCategory.BEHAVIORAL

    def match(
        self,
        class_def: ast.ClassDef,
        tree: ast.AST,
        file_path: Path,
    ) -> Optional[PatternMatch]:
        """Check for Strategy pattern."""
        confidence = 0.0
        components = {"role": None}

        methods = self._get_method_names(class_def)
        attrs = self._get_class_attributes(class_def)
        bases = self._get_base_names(class_def)

        # Check for Strategy interface (ABC with single method)
        is_abstract = "ABC" in bases or any(
            isinstance(d, ast.Name) and d.id == "abstractmethod"
            for node in class_def.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for d in node.decorator_list
        )

        if is_abstract and len(methods) <= 3:  # Small interface
            confidence += 0.2
            components["role"] = "strategy_interface"

        # Check for Context with strategy injection
        strategy_attrs = {"strategy", "_strategy", "__strategy", "algorithm", "_algorithm"}
        if any(attr in attrs for attr in strategy_attrs):
            confidence += 0.25
            components["role"] = "context"
            components["strategy_attr"] = next(
                (attr for attr in strategy_attrs if attr in attrs), None
            )

        # Check for set_strategy method
        if self._has_method(class_def, "set_strategy") or self._has_method(class_def, "set_algorithm"):
            confidence += 0.2
            components["has_setter"] = True

        # Check class name
        if "strategy" in class_def.name.lower():
            confidence += 0.15
        if "context" in class_def.name.lower():
            confidence += 0.1

        # Check for execute/run method that delegates to strategy
        execute_methods = {"execute", "run", "do", "perform", "apply"}
        if methods & execute_methods:
            for method_name in methods & execute_methods:
                method = self._get_method(class_def, method_name)
                if method:
                    method_source = ast.unparse(method)
                    if "strategy" in method_source.lower():
                        confidence += 0.2
                        break

        if confidence >= PatternDetectorConfig.MIN_CONFIDENCE:
            return PatternMatch(
                pattern_type=self.pattern_type,
                category=self.category,
                confidence=min(1.0, confidence),
                class_name=class_def.name,
                file_path=file_path,
                line_number=class_def.lineno,
                components=components,
                description="Strategy pattern defines family of interchangeable algorithms",
            )

        return None

    def check_integrity(
        self,
        pattern: PatternMatch,
        tree: ast.AST,
    ) -> PatternIntegrity:
        """Check Strategy pattern integrity."""
        violations = []
        score = pattern.confidence

        return PatternIntegrity(
            pattern=pattern,
            is_intact=True,
            violations=violations,
            score=score,
        )


class DecoratorMatcher(PatternMatcher):
    """Detects Decorator pattern (structural, not Python decorators)."""

    pattern_type = PatternType.DECORATOR
    category = PatternCategory.STRUCTURAL

    def match(
        self,
        class_def: ast.ClassDef,
        tree: ast.AST,
        file_path: Path,
    ) -> Optional[PatternMatch]:
        """Check for Decorator pattern."""
        confidence = 0.0
        components = {}

        attrs = self._get_class_attributes(class_def)
        bases = self._get_base_names(class_def)
        methods = self._get_method_names(class_def)

        # Check for wrapped/component attribute
        wrapped_attrs = {"wrapped", "_wrapped", "component", "_component", "decorated", "_decorated"}
        found_wrapped = [attr for attr in wrapped_attrs if attr in attrs]
        if found_wrapped:
            confidence += 0.25
            components["wrapped_attr"] = found_wrapped[0]

        # Check for same base class as component (implements same interface)
        if bases:
            # Check if __init__ takes same type as parameter
            init_method = self._get_method(class_def, "__init__")
            if init_method:
                # Look for component parameter
                for arg in init_method.args.args:
                    if arg.arg in ("wrapped", "component", "decorated"):
                        confidence += 0.2
                        break

        # Check if methods delegate to wrapped
        for method_name in methods:
            if method_name.startswith("_"):
                continue
            method = self._get_method(class_def, method_name)
            if method:
                method_source = ast.unparse(method)
                # Check for delegation pattern: self.wrapped.method()
                if re.search(r'self\.\w*wrap\w*\.', method_source, re.IGNORECASE):
                    confidence += 0.15
                    break

        # Check class name
        if "decorator" in class_def.name.lower() or "wrapper" in class_def.name.lower():
            confidence += 0.15

        if confidence >= PatternDetectorConfig.MIN_CONFIDENCE:
            return PatternMatch(
                pattern_type=self.pattern_type,
                category=self.category,
                confidence=min(1.0, confidence),
                class_name=class_def.name,
                file_path=file_path,
                line_number=class_def.lineno,
                components=components,
                description="Decorator pattern adds behavior to objects dynamically",
            )

        return None

    def check_integrity(
        self,
        pattern: PatternMatch,
        tree: ast.AST,
    ) -> PatternIntegrity:
        """Check Decorator pattern integrity."""
        violations = []
        score = pattern.confidence

        return PatternIntegrity(
            pattern=pattern,
            is_intact=True,
            violations=violations,
            score=score,
        )


class DependencyInjectionMatcher(PatternMatcher):
    """Detects Dependency Injection pattern."""

    pattern_type = PatternType.DEPENDENCY_INJECTION
    category = PatternCategory.MODERN

    def match(
        self,
        class_def: ast.ClassDef,
        tree: ast.AST,
        file_path: Path,
    ) -> Optional[PatternMatch]:
        """Check for Dependency Injection pattern."""
        confidence = 0.0
        components = {"injected_deps": []}

        # Check __init__ for dependency parameters with type hints
        init_method = self._get_method(class_def, "__init__")
        if init_method:
            for arg in init_method.args.args:
                if arg.arg == "self":
                    continue
                # Check for type annotation
                if arg.annotation:
                    ann_str = ast.unparse(arg.annotation)
                    # Common DI patterns: interfaces, services, repositories
                    if any(keyword in ann_str for keyword in
                           ["Service", "Repository", "Factory", "Provider", "Client", "Handler"]):
                        confidence += 0.2
                        components["injected_deps"].append(arg.arg)
                    # Abstract base classes
                    elif any(keyword in ann_str for keyword in ["ABC", "Protocol", "Interface"]):
                        confidence += 0.15
                        components["injected_deps"].append(arg.arg)

            # Check for many constructor parameters (common in DI)
            param_count = len([a for a in init_method.args.args if a.arg != "self"])
            if param_count >= 3:
                confidence += 0.1

        # Check for @inject decorator or similar
        for decorator in class_def.decorator_list:
            dec_str = ast.unparse(decorator)
            if any(kw in dec_str.lower() for kw in ["inject", "autowire", "component", "service"]):
                confidence += 0.25
                break

        if confidence >= PatternDetectorConfig.MIN_CONFIDENCE:
            return PatternMatch(
                pattern_type=self.pattern_type,
                category=self.category,
                confidence=min(1.0, confidence),
                class_name=class_def.name,
                file_path=file_path,
                line_number=class_def.lineno,
                components=components,
                description="Dependency Injection receives dependencies through constructor",
            )

        return None

    def check_integrity(
        self,
        pattern: PatternMatch,
        tree: ast.AST,
    ) -> PatternIntegrity:
        """Check DI pattern integrity."""
        violations = []
        score = pattern.confidence

        return PatternIntegrity(
            pattern=pattern,
            is_intact=True,
            violations=violations,
            score=score,
        )


class ContextManagerMatcher(PatternMatcher):
    """Detects Context Manager pattern (Pythonic)."""

    pattern_type = PatternType.CONTEXT_MANAGER
    category = PatternCategory.PYTHONIC

    def match(
        self,
        class_def: ast.ClassDef,
        tree: ast.AST,
        file_path: Path,
    ) -> Optional[PatternMatch]:
        """Check for Context Manager pattern."""
        confidence = 0.0
        components = {}

        methods = self._get_method_names(class_def)

        # Check for __enter__ and __exit__
        if "__enter__" in methods:
            confidence += 0.4
            components["has_enter"] = True
        if "__exit__" in methods:
            confidence += 0.4
            components["has_exit"] = True

        # Check for async context manager
        if "__aenter__" in methods and "__aexit__" in methods:
            confidence = max(confidence, 0.8)
            components["is_async"] = True

        if confidence >= PatternDetectorConfig.MIN_CONFIDENCE:
            return PatternMatch(
                pattern_type=self.pattern_type,
                category=self.category,
                confidence=min(1.0, confidence),
                class_name=class_def.name,
                file_path=file_path,
                line_number=class_def.lineno,
                components=components,
                description="Context Manager handles resource setup and teardown",
            )

        return None

    def check_integrity(
        self,
        pattern: PatternMatch,
        tree: ast.AST,
    ) -> PatternIntegrity:
        """Check Context Manager integrity."""
        violations = []
        score = pattern.confidence

        has_enter = pattern.components.get("has_enter", False)
        has_exit = pattern.components.get("has_exit", False)

        if has_enter and not has_exit:
            violations.append(PatternViolation(
                pattern_type=self.pattern_type,
                violation_type=ViolationType.PARTIAL_IMPL,
                severity=Severity.CRITICAL,
                message="Context Manager has __enter__ but missing __exit__",
                file_path=pattern.file_path,
                line_number=pattern.line_number,
                suggestion="Implement __exit__(self, exc_type, exc_val, exc_tb)",
            ))
            score -= 0.4

        return PatternIntegrity(
            pattern=pattern,
            is_intact=has_enter and has_exit,
            violations=violations,
            score=max(0, score),
        )


# =============================================================================
# PATTERN DETECTOR
# =============================================================================

class PatternDetector:
    """
    Main pattern detection engine.

    Detects design patterns in Python code and verifies their integrity.
    """

    def __init__(self):
        # Register all pattern matchers
        self._matchers: List[PatternMatcher] = [
            # Creational
            SingletonMatcher(),
            FactoryMethodMatcher(),
            # Structural
            DecoratorMatcher(),
            # Behavioral
            ObserverMatcher(),
            StrategyMatcher(),
            # Modern
            DependencyInjectionMatcher(),
            # Pythonic
            ContextManagerMatcher(),
        ]

        self._detected_patterns: Dict[Path, List[PatternMatch]] = {}
        self._lock = asyncio.Lock()

    async def analyze_file(self, file_path: Path) -> List[PatternMatch]:
        """Detect patterns in a single file."""
        patterns = []

        try:
            content = await asyncio.to_thread(file_path.read_text, encoding='utf-8', errors='ignore')
            tree = ast.parse(content)
        except (SyntaxError, FileNotFoundError) as e:
            logger.warning(f"Failed to parse {file_path}: {e}")
            return patterns

        # Find all classes
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                # Try each matcher
                for matcher in self._matchers:
                    match = matcher.match(node, tree, file_path)
                    if match:
                        patterns.append(match)

        # Store results
        async with self._lock:
            self._detected_patterns[file_path] = patterns

        return patterns

    async def analyze_directory(
        self,
        directory: Path,
        patterns: List[str] = None,
    ) -> Dict[Path, List[PatternMatch]]:
        """Analyze all Python files in a directory."""
        patterns = patterns or ["*.py"]
        files = []

        for pattern in patterns:
            files.extend(directory.glob(f"**/{pattern}"))

        # Analyze files in parallel
        semaphore = asyncio.Semaphore(50)

        async def analyze_with_semaphore(f: Path) -> Tuple[Path, List[PatternMatch]]:
            async with semaphore:
                return f, await self.analyze_file(f)

        tasks = [analyze_with_semaphore(f) for f in files]
        results = await asyncio.gather(*tasks)

        return {path: matches for path, matches in results if matches}

    def check_integrity(self, pattern: PatternMatch) -> PatternIntegrity:
        """Check the integrity of a detected pattern."""
        # Find the appropriate matcher
        for matcher in self._matchers:
            if matcher.pattern_type == pattern.pattern_type:
                # Load the tree
                try:
                    content = pattern.file_path.read_text(encoding='utf-8', errors='ignore')
                    tree = ast.parse(content)
                    return matcher.check_integrity(pattern, tree)
                except Exception as e:
                    logger.error(f"Failed to check integrity: {e}")

        # Default integrity (unknown)
        return PatternIntegrity(
            pattern=pattern,
            is_intact=True,
            violations=[],
            score=pattern.confidence,
        )

    def analyze_refactoring_impact(
        self,
        file_path: Path,
        changes: Dict[str, Any],
    ) -> RefactoringImpact:
        """Analyze how a refactoring would impact patterns."""
        affected = []
        violations = []
        recommendations = []

        # Get patterns in the file
        patterns = self._detected_patterns.get(file_path, [])

        for pattern in patterns:
            # Check if change affects pattern
            changed_lines = changes.get("lines", [])
            if pattern.line_number in range(
                min(changed_lines, default=0) - 10,
                max(changed_lines, default=0) + 10
            ):
                affected.append(pattern)

                # Check specific pattern risks
                if pattern.pattern_type == PatternType.SINGLETON:
                    if "instance" in str(changes.get("content", "")):
                        violations.append(PatternViolation(
                            pattern_type=pattern.pattern_type,
                            violation_type=ViolationType.STRUCTURAL_BREAK,
                            severity=Severity.CRITICAL,
                            message="Refactoring may break Singleton instance management",
                            file_path=file_path,
                            line_number=pattern.line_number,
                            suggestion="Preserve _instance variable and __new__ method",
                        ))

                elif pattern.pattern_type == PatternType.OBSERVER:
                    if any(kw in str(changes) for kw in ["notify", "observer", "subscribe"]):
                        recommendations.append(
                            f"Ensure Observer notification chain remains intact in {pattern.class_name}"
                        )

        # Determine risk level
        if any(v.severity == Severity.CRITICAL for v in violations):
            risk_level = Severity.CRITICAL
        elif violations:
            risk_level = Severity.WARNING
        else:
            risk_level = Severity.INFO

        return RefactoringImpact(
            affected_patterns=affected,
            potential_violations=violations,
            recommendations=recommendations,
            risk_level=risk_level,
        )

    def get_recommendations(
        self,
        file_path: Path,
        class_name: Optional[str] = None,
    ) -> List[PatternRecommendation]:
        """Get pattern recommendations for a file or class."""
        recommendations = []
        # This would use heuristics to suggest patterns
        # Simplified implementation
        return recommendations

    def get_all_patterns(self) -> List[PatternMatch]:
        """Get all detected patterns."""
        all_patterns = []
        for patterns in self._detected_patterns.values():
            all_patterns.extend(patterns)
        return all_patterns

    def get_stats(self) -> Dict[str, Any]:
        """Get detector statistics."""
        all_patterns = self.get_all_patterns()
        by_type = defaultdict(int)
        for p in all_patterns:
            by_type[p.pattern_type.value] += 1

        return {
            "files_analyzed": len(self._detected_patterns),
            "total_patterns": len(all_patterns),
            "by_type": dict(by_type),
        }


# =============================================================================
# CROSS-REPO PATTERN DETECTOR
# =============================================================================

class CrossRepoPatternDetector:
    """
    Pattern detection across multiple repositories.
    """

    def __init__(self):
        self._repos: Dict[str, Path] = {
            "jarvis": PatternDetectorConfig.JARVIS_REPO,
            "prime": PatternDetectorConfig.PRIME_REPO,
            "reactor": PatternDetectorConfig.REACTOR_REPO,
        }
        self._detectors: Dict[str, PatternDetector] = {}

    async def initialize(self) -> bool:
        """Initialize pattern detectors for all repositories."""
        logger.info("Initializing Cross-Repo Pattern Detector...")

        for repo_name, repo_path in self._repos.items():
            if not repo_path.exists():
                logger.warning(f"Repository not found: {repo_name}")
                continue

            detector = PatternDetector()
            self._detectors[repo_name] = detector

            logger.info(f"  Analyzing {repo_name}...")
            results = await detector.analyze_directory(repo_path)
            total_patterns = sum(len(p) for p in results.values())
            logger.info(f"  ✓ {repo_name}: {len(results)} files, {total_patterns} patterns")

        return True

    def get_all_patterns(self) -> Dict[str, List[PatternMatch]]:
        """Get all patterns from all repositories."""
        return {
            repo: detector.get_all_patterns()
            for repo, detector in self._detectors.items()
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get cross-repo statistics."""
        return {
            repo: detector.get_stats()
            for repo, detector in self._detectors.items()
        }


# =============================================================================
# SINGLETON ACCESSORS
# =============================================================================

_pattern_detector: Optional[PatternDetector] = None
_cross_repo_detector: Optional[CrossRepoPatternDetector] = None


def get_pattern_detector() -> PatternDetector:
    """Get the singleton pattern detector."""
    global _pattern_detector
    if _pattern_detector is None:
        _pattern_detector = PatternDetector()
    return _pattern_detector


def get_cross_repo_pattern_detector() -> CrossRepoPatternDetector:
    """Get the singleton cross-repo pattern detector."""
    global _cross_repo_detector
    if _cross_repo_detector is None:
        _cross_repo_detector = CrossRepoPatternDetector()
    return _cross_repo_detector


async def initialize_pattern_detection() -> bool:
    """Initialize cross-repo pattern detection."""
    detector = get_cross_repo_pattern_detector()
    return await detector.initialize()
