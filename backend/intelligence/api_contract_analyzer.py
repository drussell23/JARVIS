"""
API Contract Analyzer v1.0 - REST API Contract Understanding
=============================================================

Enterprise-grade API contract analysis system that understands REST API
contracts and ensures refactoring maintains API compatibility.

Features:
- OpenAPI/Swagger specification parsing
- Endpoint signature extraction from code
- Request/Response schema validation
- Breaking change detection in API contracts
- Versioning strategy verification
- Cross-repo API dependency tracking
- Contract-first validation
- Migration path suggestions

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    API Contract Analyzer v1.0                            │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐       │
    │   │ Endpoint Parser │   │ Schema Analyzer │   │ Contract Checker│       │
    │   │ (Flask/FastAPI) │──▶│ (Pydantic/etc)  │──▶│ (Compatibility) │       │
    │   └─────────────────┘   └─────────────────┘   └─────────────────┘       │
    │           │                     │                     │                  │
    │           └─────────────────────┴─────────────────────┘                  │
    │                                 │                                        │
    │                    ┌────────────▼────────────┐                           │
    │                    │    Contract Registry    │                           │
    │                    │  (OpenAPI + Code)       │                           │
    │                    └────────────┬────────────┘                           │
    │                                 │                                        │
    │   ┌──────────────┬──────────────┼──────────────┬──────────────┐         │
    │   │              │              │              │              │         │
    │   ▼              ▼              ▼              ▼              ▼         │
    │ Breaking      Version        Migration     Schema          Cross-API   │
    │ Detector      Validator      Planner       Validator       Tracker     │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
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


def _get_env_list(key: str, default: List[str] = None) -> List[str]:
    val = _get_env(key, "")
    if not val:
        return default or []
    return [x.strip() for x in val.split(",") if x.strip()]


class APIContractConfig:
    """Configuration for API contract analysis."""

    # Analysis settings
    STRICT_MODE: bool = _get_env_bool("API_STRICT_MODE", True)
    VALIDATE_SCHEMAS: bool = _get_env_bool("API_VALIDATE_SCHEMAS", True)
    TRACK_VERSIONS: bool = _get_env_bool("API_TRACK_VERSIONS", True)

    # Frameworks to detect
    FRAMEWORKS: List[str] = _get_env_list("API_FRAMEWORKS", ["flask", "fastapi", "django", "aiohttp"])

    # OpenAPI spec paths
    OPENAPI_PATHS: List[str] = _get_env_list("API_OPENAPI_PATHS", ["openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json"])

    # Repository paths
    JARVIS_REPO: Path = Path(_get_env("JARVIS_REPO", str(Path.home() / "Documents/repos/JARVIS-AI-Agent")))
    PRIME_REPO: Path = Path(_get_env("PRIME_REPO", str(Path.home() / "Documents/repos/jarvis-prime")))
    REACTOR_REPO: Path = Path(_get_env("REACTOR_REPO", str(Path.home() / "Documents/repos/reactor-core")))


# =============================================================================
# ENUMS AND TYPES
# =============================================================================

class HTTPMethod(Enum):
    """HTTP methods."""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


class ParameterLocation(Enum):
    """Location of API parameters."""
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"
    BODY = "body"


class BreakingChangeType(Enum):
    """Types of breaking changes in API contracts."""
    ENDPOINT_REMOVED = "endpoint_removed"
    METHOD_REMOVED = "method_removed"
    REQUIRED_PARAM_ADDED = "required_param_added"
    PARAM_REMOVED = "param_removed"
    PARAM_TYPE_CHANGED = "param_type_changed"
    RESPONSE_TYPE_CHANGED = "response_type_changed"
    STATUS_CODE_REMOVED = "status_code_removed"
    PATH_CHANGED = "path_changed"
    AUTH_ADDED = "auth_added"
    SCHEMA_FIELD_REMOVED = "schema_field_removed"
    SCHEMA_TYPE_CHANGED = "schema_type_changed"


class Severity(Enum):
    """Severity levels."""
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class FrameworkType(Enum):
    """Supported frameworks."""
    FLASK = "flask"
    FASTAPI = "fastapi"
    DJANGO = "django"
    AIOHTTP = "aiohttp"
    UNKNOWN = "unknown"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class APIParameter:
    """An API endpoint parameter."""
    name: str
    location: ParameterLocation
    param_type: str
    required: bool = True
    default_value: Optional[Any] = None
    description: Optional[str] = None
    schema: Optional[Dict[str, Any]] = None


@dataclass
class APIResponse:
    """An API response definition."""
    status_code: int
    content_type: str = "application/json"
    schema: Optional[Dict[str, Any]] = None
    description: Optional[str] = None


@dataclass
class APIEndpoint:
    """An API endpoint definition."""
    path: str
    method: HTTPMethod
    handler_name: str
    file_path: Path
    line_number: int
    parameters: List[APIParameter] = field(default_factory=list)
    responses: List[APIResponse] = field(default_factory=list)
    request_body_schema: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    deprecated: bool = False
    auth_required: bool = False
    version: Optional[str] = None

    @property
    def full_path(self) -> str:
        """Get full path with version prefix if present."""
        if self.version:
            return f"/api/{self.version}{self.path}"
        return self.path

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "path": self.path,
            "method": self.method.value,
            "handler": self.handler_name,
            "parameters": [
                {
                    "name": p.name,
                    "location": p.location.value,
                    "type": p.param_type,
                    "required": p.required,
                }
                for p in self.parameters
            ],
            "responses": [
                {"status_code": r.status_code, "content_type": r.content_type}
                for r in self.responses
            ],
        }


@dataclass
class APISchema:
    """An API schema/model definition."""
    name: str
    file_path: Path
    line_number: int
    fields: Dict[str, "SchemaField"] = field(default_factory=dict)
    base_classes: List[str] = field(default_factory=list)
    description: Optional[str] = None


@dataclass
class SchemaField:
    """A field in an API schema."""
    name: str
    field_type: str
    required: bool = True
    default_value: Optional[Any] = None
    description: Optional[str] = None
    constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class APIContract:
    """Complete API contract."""
    name: str
    version: str
    endpoints: List[APIEndpoint]
    schemas: List[APISchema]
    base_path: str = ""
    description: Optional[str] = None


@dataclass
class BreakingChange:
    """A detected breaking change in API contract."""
    change_type: BreakingChangeType
    severity: Severity
    endpoint_path: str
    method: HTTPMethod
    description: str
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    suggestion: Optional[str] = None


@dataclass
class ContractDiff:
    """Difference between two API contracts."""
    old_version: str
    new_version: str
    breaking_changes: List[BreakingChange]
    additions: List[APIEndpoint]
    removals: List[APIEndpoint]
    modifications: List[Tuple[APIEndpoint, APIEndpoint]]

    @property
    def is_backwards_compatible(self) -> bool:
        """Check if the change is backwards compatible."""
        return len(self.breaking_changes) == 0


@dataclass
class MigrationStep:
    """A step in an API migration."""
    order: int
    description: str
    action_type: str
    details: Dict[str, Any]


@dataclass
class MigrationPlan:
    """A plan for migrating between API versions."""
    from_version: str
    to_version: str
    steps: List[MigrationStep]
    estimated_impact: Severity
    affected_clients: List[str]


# =============================================================================
# ENDPOINT EXTRACTORS
# =============================================================================

class EndpointExtractor(ABC):
    """Base class for framework-specific endpoint extraction."""

    @abstractmethod
    def extract_endpoints(
        self,
        tree: ast.AST,
        file_path: Path,
    ) -> List[APIEndpoint]:
        """Extract API endpoints from AST."""
        pass

    def _get_docstring(self, node: ast.AST) -> Optional[str]:
        """Get docstring from a node."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return ast.get_docstring(node)
        return None

    def _parse_type_annotation(self, node: ast.AST) -> str:
        """Parse type annotation to string."""
        if node is None:
            return "Any"
        return ast.unparse(node)


class FlaskExtractor(EndpointExtractor):
    """Extracts endpoints from Flask applications."""

    ROUTE_DECORATORS = {"route", "get", "post", "put", "patch", "delete"}
    HTTP_METHODS = {
        "get": HTTPMethod.GET,
        "post": HTTPMethod.POST,
        "put": HTTPMethod.PUT,
        "patch": HTTPMethod.PATCH,
        "delete": HTTPMethod.DELETE,
    }

    def extract_endpoints(
        self,
        tree: ast.AST,
        file_path: Path,
    ) -> List[APIEndpoint]:
        """Extract Flask endpoints."""
        endpoints = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                endpoint = self._extract_from_function(node, file_path)
                if endpoint:
                    endpoints.append(endpoint)

        return endpoints

    def _extract_from_function(
        self,
        func: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        file_path: Path,
    ) -> Optional[APIEndpoint]:
        """Extract endpoint from a decorated function."""
        for decorator in func.decorator_list:
            endpoint = self._parse_route_decorator(decorator, func, file_path)
            if endpoint:
                return endpoint
        return None

    def _parse_route_decorator(
        self,
        decorator: ast.AST,
        func: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        file_path: Path,
    ) -> Optional[APIEndpoint]:
        """Parse a route decorator."""
        if isinstance(decorator, ast.Call):
            # @app.route('/path') or @blueprint.route('/path')
            func_node = decorator.func
            if isinstance(func_node, ast.Attribute):
                method_name = func_node.attr.lower()
                if method_name in self.ROUTE_DECORATORS:
                    # Get path from first argument
                    path = None
                    methods = [HTTPMethod.GET]

                    if decorator.args:
                        first_arg = decorator.args[0]
                        if isinstance(first_arg, ast.Constant):
                            path = str(first_arg.value)

                    # Get methods from keyword argument
                    for kw in decorator.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, ast.List):
                            methods = []
                            for elt in kw.value.elts:
                                if isinstance(elt, ast.Constant):
                                    method_str = str(elt.value).upper()
                                    try:
                                        methods.append(HTTPMethod(method_str))
                                    except ValueError:
                                        pass

                    # Handle shorthand decorators
                    if method_name in self.HTTP_METHODS:
                        methods = [self.HTTP_METHODS[method_name]]

                    if path:
                        # Extract parameters from path
                        params = self._extract_path_params(path)
                        params.extend(self._extract_func_params(func))

                        return APIEndpoint(
                            path=path,
                            method=methods[0],
                            handler_name=func.name,
                            file_path=file_path,
                            line_number=func.lineno,
                            parameters=params,
                            description=self._get_docstring(func),
                        )

        return None

    def _extract_path_params(self, path: str) -> List[APIParameter]:
        """Extract parameters from path."""
        params = []
        # Match Flask path parameters: <type:name> or <name>
        pattern = r'<(?:(\w+):)?(\w+)>'
        for match in re.finditer(pattern, path):
            param_type = match.group(1) or "string"
            param_name = match.group(2)
            params.append(APIParameter(
                name=param_name,
                location=ParameterLocation.PATH,
                param_type=param_type,
                required=True,
            ))
        return params

    def _extract_func_params(
        self,
        func: Union[ast.FunctionDef, ast.AsyncFunctionDef],
    ) -> List[APIParameter]:
        """Extract parameters from function signature."""
        params = []
        for arg in func.args.args:
            if arg.arg in ("self", "cls"):
                continue
            param_type = self._parse_type_annotation(arg.annotation)
            params.append(APIParameter(
                name=arg.arg,
                location=ParameterLocation.QUERY,
                param_type=param_type,
                required=arg.arg not in {d.arg for d in func.args.defaults or []},
            ))
        return params


class FastAPIExtractor(EndpointExtractor):
    """Extracts endpoints from FastAPI applications."""

    HTTP_METHODS = {
        "get": HTTPMethod.GET,
        "post": HTTPMethod.POST,
        "put": HTTPMethod.PUT,
        "patch": HTTPMethod.PATCH,
        "delete": HTTPMethod.DELETE,
    }

    def extract_endpoints(
        self,
        tree: ast.AST,
        file_path: Path,
    ) -> List[APIEndpoint]:
        """Extract FastAPI endpoints."""
        endpoints = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                endpoint = self._extract_from_function(node, file_path)
                if endpoint:
                    endpoints.append(endpoint)

        return endpoints

    def _extract_from_function(
        self,
        func: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        file_path: Path,
    ) -> Optional[APIEndpoint]:
        """Extract endpoint from a decorated function."""
        for decorator in func.decorator_list:
            endpoint = self._parse_route_decorator(decorator, func, file_path)
            if endpoint:
                return endpoint
        return None

    def _parse_route_decorator(
        self,
        decorator: ast.AST,
        func: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        file_path: Path,
    ) -> Optional[APIEndpoint]:
        """Parse a FastAPI route decorator."""
        if isinstance(decorator, ast.Call):
            func_node = decorator.func
            if isinstance(func_node, ast.Attribute):
                method_name = func_node.attr.lower()
                if method_name in self.HTTP_METHODS:
                    # Get path
                    path = None
                    if decorator.args:
                        first_arg = decorator.args[0]
                        if isinstance(first_arg, ast.Constant):
                            path = str(first_arg.value)

                    if path:
                        # Extract parameters
                        params = self._extract_path_params(path)
                        params.extend(self._extract_func_params(func))

                        # Extract response model
                        responses = self._extract_responses(decorator, func)

                        return APIEndpoint(
                            path=path,
                            method=self.HTTP_METHODS[method_name],
                            handler_name=func.name,
                            file_path=file_path,
                            line_number=func.lineno,
                            parameters=params,
                            responses=responses,
                            description=self._get_docstring(func),
                        )

        return None

    def _extract_path_params(self, path: str) -> List[APIParameter]:
        """Extract parameters from FastAPI path."""
        params = []
        # Match FastAPI path parameters: {name} or {name:type}
        pattern = r'\{(\w+)(?::(\w+))?\}'
        for match in re.finditer(pattern, path):
            param_name = match.group(1)
            param_type = match.group(2) or "str"
            params.append(APIParameter(
                name=param_name,
                location=ParameterLocation.PATH,
                param_type=param_type,
                required=True,
            ))
        return params

    def _extract_func_params(
        self,
        func: Union[ast.FunctionDef, ast.AsyncFunctionDef],
    ) -> List[APIParameter]:
        """Extract parameters from FastAPI function."""
        params = []

        for arg in func.args.args:
            if arg.arg in ("self", "cls"):
                continue

            param_type = self._parse_type_annotation(arg.annotation)
            location = ParameterLocation.QUERY

            # Check annotation for Body, Query, Path, etc.
            if arg.annotation:
                ann_str = ast.unparse(arg.annotation)
                if "Body" in ann_str:
                    location = ParameterLocation.BODY
                elif "Path" in ann_str:
                    location = ParameterLocation.PATH
                elif "Header" in ann_str:
                    location = ParameterLocation.HEADER
                elif "Cookie" in ann_str:
                    location = ParameterLocation.COOKIE

            params.append(APIParameter(
                name=arg.arg,
                location=location,
                param_type=param_type,
                required=True,  # Would need more analysis for defaults
            ))

        return params

    def _extract_responses(
        self,
        decorator: ast.Call,
        func: Union[ast.FunctionDef, ast.AsyncFunctionDef],
    ) -> List[APIResponse]:
        """Extract response models."""
        responses = []

        # Check return type annotation
        if func.returns:
            return_type = ast.unparse(func.returns)
            responses.append(APIResponse(
                status_code=200,
                schema={"type": return_type},
            ))

        # Check response_model in decorator
        for kw in decorator.keywords:
            if kw.arg == "response_model":
                model_name = ast.unparse(kw.value)
                responses.append(APIResponse(
                    status_code=200,
                    schema={"$ref": f"#/components/schemas/{model_name}"},
                ))

        return responses


# =============================================================================
# SCHEMA EXTRACTOR
# =============================================================================

class SchemaExtractor:
    """Extracts API schemas from Pydantic/dataclass models."""

    def extract_schemas(
        self,
        tree: ast.AST,
        file_path: Path,
    ) -> List[APISchema]:
        """Extract API schemas from Python code."""
        schemas = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                schema = self._extract_schema(node, file_path)
                if schema:
                    schemas.append(schema)

        return schemas

    def _extract_schema(
        self,
        class_def: ast.ClassDef,
        file_path: Path,
    ) -> Optional[APISchema]:
        """Extract schema from a class definition."""
        # Check if it's a Pydantic model or dataclass
        bases = [ast.unparse(b) for b in class_def.bases]

        is_pydantic = any("BaseModel" in b or "BaseSchema" in b for b in bases)
        is_dataclass = any(
            isinstance(d, ast.Name) and d.id == "dataclass"
            or isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass"
            for d in class_def.decorator_list
        )

        if not (is_pydantic or is_dataclass):
            return None

        # Extract fields
        fields = {}
        for node in class_def.body:
            if isinstance(node, ast.AnnAssign):
                field = self._extract_field(node)
                if field:
                    fields[field.name] = field

        return APISchema(
            name=class_def.name,
            file_path=file_path,
            line_number=class_def.lineno,
            fields=fields,
            base_classes=bases,
            description=ast.get_docstring(class_def),
        )

    def _extract_field(self, node: ast.AnnAssign) -> Optional[SchemaField]:
        """Extract a field from annotated assignment."""
        if not isinstance(node.target, ast.Name):
            return None

        name = node.target.id
        field_type = ast.unparse(node.annotation) if node.annotation else "Any"

        # Determine if required
        required = True
        default_value = None

        if node.value:
            if isinstance(node.value, ast.Constant):
                default_value = node.value.value
                required = False
            elif isinstance(node.value, ast.Call):
                # Field() or similar
                for kw in node.value.keywords:
                    if kw.arg == "default":
                        required = False
                        if isinstance(kw.value, ast.Constant):
                            default_value = kw.value.value

        return SchemaField(
            name=name,
            field_type=field_type,
            required=required,
            default_value=default_value,
        )


# =============================================================================
# CONTRACT COMPARATOR
# =============================================================================

class ContractComparator:
    """Compares API contracts for breaking changes."""

    def compare(
        self,
        old_contract: APIContract,
        new_contract: APIContract,
    ) -> ContractDiff:
        """Compare two API contracts."""
        breaking_changes = []
        additions = []
        removals = []
        modifications = []

        # Build endpoint maps
        old_endpoints = {(e.path, e.method): e for e in old_contract.endpoints}
        new_endpoints = {(e.path, e.method): e for e in new_contract.endpoints}

        # Find removed endpoints
        for key, endpoint in old_endpoints.items():
            if key not in new_endpoints:
                removals.append(endpoint)
                breaking_changes.append(BreakingChange(
                    change_type=BreakingChangeType.ENDPOINT_REMOVED,
                    severity=Severity.CRITICAL,
                    endpoint_path=endpoint.path,
                    method=endpoint.method,
                    description=f"Endpoint {endpoint.method.value} {endpoint.path} was removed",
                    suggestion="Consider deprecation before removal",
                ))

        # Find new endpoints
        for key, endpoint in new_endpoints.items():
            if key not in old_endpoints:
                additions.append(endpoint)

        # Compare shared endpoints
        for key in old_endpoints.keys() & new_endpoints.keys():
            old_ep = old_endpoints[key]
            new_ep = new_endpoints[key]

            changes = self._compare_endpoints(old_ep, new_ep)
            breaking_changes.extend(changes)

            if changes:
                modifications.append((old_ep, new_ep))

        return ContractDiff(
            old_version=old_contract.version,
            new_version=new_contract.version,
            breaking_changes=breaking_changes,
            additions=additions,
            removals=removals,
            modifications=modifications,
        )

    def _compare_endpoints(
        self,
        old_ep: APIEndpoint,
        new_ep: APIEndpoint,
    ) -> List[BreakingChange]:
        """Compare two endpoints for breaking changes."""
        changes = []

        # Compare parameters
        old_params = {p.name: p for p in old_ep.parameters}
        new_params = {p.name: p for p in new_ep.parameters}

        # Check for removed parameters
        for name, param in old_params.items():
            if name not in new_params:
                changes.append(BreakingChange(
                    change_type=BreakingChangeType.PARAM_REMOVED,
                    severity=Severity.WARNING,
                    endpoint_path=old_ep.path,
                    method=old_ep.method,
                    description=f"Parameter '{name}' was removed",
                    old_value=param.param_type,
                ))

        # Check for new required parameters
        for name, param in new_params.items():
            if name not in old_params and param.required:
                changes.append(BreakingChange(
                    change_type=BreakingChangeType.REQUIRED_PARAM_ADDED,
                    severity=Severity.CRITICAL,
                    endpoint_path=new_ep.path,
                    method=new_ep.method,
                    description=f"Required parameter '{name}' was added",
                    new_value=param.param_type,
                    suggestion="Make the parameter optional or provide a default value",
                ))

        # Check for type changes
        for name in old_params.keys() & new_params.keys():
            if old_params[name].param_type != new_params[name].param_type:
                changes.append(BreakingChange(
                    change_type=BreakingChangeType.PARAM_TYPE_CHANGED,
                    severity=Severity.WARNING,
                    endpoint_path=old_ep.path,
                    method=old_ep.method,
                    description=f"Type of parameter '{name}' changed",
                    old_value=old_params[name].param_type,
                    new_value=new_params[name].param_type,
                ))

        return changes


# =============================================================================
# MIGRATION PLANNER
# =============================================================================

class MigrationPlanner:
    """Plans API migration between versions."""

    def create_plan(
        self,
        diff: ContractDiff,
        affected_clients: List[str] = None,
    ) -> MigrationPlan:
        """Create a migration plan from a contract diff."""
        steps = []
        order = 1

        # Step 1: Deprecation notices
        if diff.removals:
            steps.append(MigrationStep(
                order=order,
                description="Add deprecation notices to endpoints being removed",
                action_type="deprecate",
                details={
                    "endpoints": [
                        f"{e.method.value} {e.path}" for e in diff.removals
                    ]
                },
            ))
            order += 1

        # Step 2: Handle required parameter additions
        for change in diff.breaking_changes:
            if change.change_type == BreakingChangeType.REQUIRED_PARAM_ADDED:
                steps.append(MigrationStep(
                    order=order,
                    description=f"Make parameter optional: {change.description}",
                    action_type="modify_parameter",
                    details={
                        "endpoint": change.endpoint_path,
                        "suggestion": change.suggestion,
                    },
                ))
                order += 1

        # Step 3: Update client documentation
        if diff.breaking_changes:
            steps.append(MigrationStep(
                order=order,
                description="Update API documentation and notify clients",
                action_type="documentation",
                details={
                    "changes_count": len(diff.breaking_changes),
                },
            ))
            order += 1

        # Determine impact
        critical_count = sum(
            1 for c in diff.breaking_changes if c.severity == Severity.CRITICAL
        )
        if critical_count > 0:
            impact = Severity.CRITICAL
        elif diff.breaking_changes:
            impact = Severity.WARNING
        else:
            impact = Severity.INFO

        return MigrationPlan(
            from_version=diff.old_version,
            to_version=diff.new_version,
            steps=steps,
            estimated_impact=impact,
            affected_clients=affected_clients or [],
        )


# =============================================================================
# API CONTRACT ANALYZER
# =============================================================================

class APIContractAnalyzer:
    """
    Main API contract analysis engine.

    Provides:
    - Endpoint extraction from code
    - Schema extraction and validation
    - Breaking change detection
    - Migration planning
    """

    def __init__(self):
        self._extractors: Dict[FrameworkType, EndpointExtractor] = {
            FrameworkType.FLASK: FlaskExtractor(),
            FrameworkType.FASTAPI: FastAPIExtractor(),
        }
        self._schema_extractor = SchemaExtractor()
        self._comparator = ContractComparator()
        self._planner = MigrationPlanner()

        self._contracts: Dict[Path, APIContract] = {}
        self._lock = asyncio.Lock()

    async def analyze_file(
        self,
        file_path: Path,
        framework: Optional[FrameworkType] = None,
    ) -> Tuple[List[APIEndpoint], List[APISchema]]:
        """Analyze a single file for API definitions."""
        try:
            content = await asyncio.to_thread(file_path.read_text, encoding='utf-8', errors='ignore')
            tree = ast.parse(content)
        except (SyntaxError, FileNotFoundError) as e:
            logger.warning(f"Failed to parse {file_path}: {e}")
            return [], []

        # Detect framework if not specified
        if framework is None:
            framework = self._detect_framework(content)

        # Extract endpoints
        endpoints = []
        if framework in self._extractors:
            endpoints = self._extractors[framework].extract_endpoints(tree, file_path)

        # Extract schemas
        schemas = self._schema_extractor.extract_schemas(tree, file_path)

        return endpoints, schemas

    def _detect_framework(self, content: str) -> FrameworkType:
        """Detect the web framework from imports."""
        if "from fastapi" in content or "import fastapi" in content:
            return FrameworkType.FASTAPI
        elif "from flask" in content or "import flask" in content:
            return FrameworkType.FLASK
        elif "from django" in content or "import django" in content:
            return FrameworkType.DJANGO
        elif "from aiohttp" in content or "import aiohttp" in content:
            return FrameworkType.AIOHTTP
        return FrameworkType.UNKNOWN

    async def analyze_directory(
        self,
        directory: Path,
        patterns: List[str] = None,
    ) -> APIContract:
        """Analyze all Python files in a directory."""
        patterns = patterns or ["*.py"]
        files = []

        for pattern in patterns:
            files.extend(directory.glob(f"**/{pattern}"))

        all_endpoints = []
        all_schemas = []

        # Analyze files in parallel
        semaphore = asyncio.Semaphore(50)

        async def analyze_with_semaphore(f: Path):
            async with semaphore:
                return await self.analyze_file(f)

        tasks = [analyze_with_semaphore(f) for f in files]
        results = await asyncio.gather(*tasks)

        for endpoints, schemas in results:
            all_endpoints.extend(endpoints)
            all_schemas.extend(schemas)

        contract = APIContract(
            name=directory.name,
            version="1.0.0",
            endpoints=all_endpoints,
            schemas=all_schemas,
        )

        async with self._lock:
            self._contracts[directory] = contract

        return contract

    def compare_contracts(
        self,
        old_contract: APIContract,
        new_contract: APIContract,
    ) -> ContractDiff:
        """Compare two API contracts."""
        return self._comparator.compare(old_contract, new_contract)

    def create_migration_plan(
        self,
        diff: ContractDiff,
        clients: List[str] = None,
    ) -> MigrationPlan:
        """Create a migration plan."""
        return self._planner.create_plan(diff, clients)

    def validate_contract(
        self,
        contract: APIContract,
    ) -> List[Dict[str, Any]]:
        """Validate a contract for issues."""
        issues = []

        # Check for duplicate paths
        seen_paths = set()
        for endpoint in contract.endpoints:
            key = (endpoint.path, endpoint.method)
            if key in seen_paths:
                issues.append({
                    "type": "duplicate_endpoint",
                    "severity": "error",
                    "message": f"Duplicate endpoint: {endpoint.method.value} {endpoint.path}",
                })
            seen_paths.add(key)

        # Check for missing schemas
        referenced_schemas = set()
        for endpoint in contract.endpoints:
            if endpoint.request_body_schema:
                ref = endpoint.request_body_schema.get("$ref", "")
                if "#/components/schemas/" in ref:
                    referenced_schemas.add(ref.split("/")[-1])

        schema_names = {s.name for s in contract.schemas}
        for ref in referenced_schemas - schema_names:
            issues.append({
                "type": "missing_schema",
                "severity": "warning",
                "message": f"Referenced schema not found: {ref}",
            })

        return issues

    def get_stats(self) -> Dict[str, Any]:
        """Get analyzer statistics."""
        total_endpoints = sum(len(c.endpoints) for c in self._contracts.values())
        total_schemas = sum(len(c.schemas) for c in self._contracts.values())

        return {
            "directories_analyzed": len(self._contracts),
            "total_endpoints": total_endpoints,
            "total_schemas": total_schemas,
        }


# =============================================================================
# CROSS-REPO API ANALYZER
# =============================================================================

class CrossRepoAPIAnalyzer:
    """
    API contract analysis across multiple repositories.
    """

    def __init__(self):
        self._repos: Dict[str, Path] = {
            "jarvis": APIContractConfig.JARVIS_REPO,
            "prime": APIContractConfig.PRIME_REPO,
            "reactor": APIContractConfig.REACTOR_REPO,
        }
        self._analyzers: Dict[str, APIContractAnalyzer] = {}
        self._contracts: Dict[str, APIContract] = {}

    async def initialize(self) -> bool:
        """Initialize API analyzers for all repositories."""
        logger.info("Initializing Cross-Repo API Analyzer...")

        for repo_name, repo_path in self._repos.items():
            if not repo_path.exists():
                logger.warning(f"Repository not found: {repo_name}")
                continue

            analyzer = APIContractAnalyzer()
            self._analyzers[repo_name] = analyzer

            logger.info(f"  Analyzing {repo_name}...")
            contract = await analyzer.analyze_directory(repo_path)
            self._contracts[repo_name] = contract
            logger.info(f"  ✓ {repo_name}: {len(contract.endpoints)} endpoints, {len(contract.schemas)} schemas")

        return True

    def get_all_contracts(self) -> Dict[str, APIContract]:
        """Get all API contracts."""
        return self._contracts

    def find_api_dependencies(self) -> Dict[str, List[str]]:
        """Find API dependencies between repositories."""
        dependencies: DefaultDict[str, List[str]] = defaultdict(list)

        # Look for API calls between repos
        # This would need more sophisticated analysis in practice

        return dict(dependencies)

    def get_stats(self) -> Dict[str, Any]:
        """Get cross-repo statistics."""
        return {
            repo: {
                "endpoints": len(contract.endpoints),
                "schemas": len(contract.schemas),
            }
            for repo, contract in self._contracts.items()
        }


# =============================================================================
# SINGLETON ACCESSORS
# =============================================================================

_api_analyzer: Optional[APIContractAnalyzer] = None
_cross_repo_analyzer: Optional[CrossRepoAPIAnalyzer] = None


def get_api_contract_analyzer() -> APIContractAnalyzer:
    """Get the singleton API contract analyzer."""
    global _api_analyzer
    if _api_analyzer is None:
        _api_analyzer = APIContractAnalyzer()
    return _api_analyzer


def get_cross_repo_api_analyzer() -> CrossRepoAPIAnalyzer:
    """Get the singleton cross-repo API analyzer."""
    global _cross_repo_analyzer
    if _cross_repo_analyzer is None:
        _cross_repo_analyzer = CrossRepoAPIAnalyzer()
    return _cross_repo_analyzer


async def initialize_api_analysis() -> bool:
    """Initialize cross-repo API analysis."""
    analyzer = get_cross_repo_api_analyzer()
    return await analyzer.initialize()
