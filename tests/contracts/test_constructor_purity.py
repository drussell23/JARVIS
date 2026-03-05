"""AST-based constructor purity verification for promoted services.

Scans __init__ methods of Wave 1 immune-tier services for forbidden patterns:
- Network calls (socket, requests, aiohttp, urllib)
- File I/O (open, pathlib write)
- Thread/process spawning (threading.Thread, subprocess, asyncio.create_task)

Side-effect-free constructors are required so that immune-tier services can be
instantiated safely during cold boot without blocking the event loop or
depending on external resources.
"""
import ast
import re
from pathlib import Path

# Wave 1 services (will be extracted to backend/services/immune/)
IMMUNE_SERVICES = [
    "SecurityPolicyEngine",
    "AnomalyDetector",
    "AuditTrailRecorder",
    "ThreatIntelligenceManager",
    "IncidentResponseCoordinator",
    "ComplianceAuditor",
    "DataClassificationManager",
    "AccessControlManager",
]

FORBIDDEN_INIT_PATTERNS = [
    r"socket\.",
    r"requests\.",
    r"aiohttp\.",
    r"urllib\.",
    r"open\(",
    r"Path\(.*\)\.write",
    r"threading\.Thread",
    r"subprocess\.",
    r"asyncio\.create_task",
    r"asyncio\.ensure_future",
    r"os\.system\(",
    r"os\.popen\(",
]

# Resolve absolute path to unified_supervisor.py from repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SUPERVISOR_PATH = _REPO_ROOT / "unified_supervisor.py"


class TestConstructorPurity:
    def test_immune_service_inits_are_pure(self):
        """All immune-tier service __init__ methods must be side-effect free."""
        src = _SUPERVISOR_PATH.read_text()
        tree = ast.parse(src)

        found_classes = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in IMMUNE_SERVICES:
                found_classes.add(node.name)
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        init_src = ast.get_source_segment(src, item)
                        if init_src is None:
                            continue
                        for pattern in FORBIDDEN_INIT_PATTERNS:
                            assert not re.search(pattern, init_src), (
                                f"{node.name}.__init__ contains forbidden pattern: {pattern}\n"
                                f"Constructors for promoted services must be side-effect free.\n"
                                f"Move this to initialize() or start()."
                            )

        # Ensure we actually found all the classes we expected
        missing = set(IMMUNE_SERVICES) - found_classes
        assert not missing, (
            f"Could not find these immune-tier classes in {_SUPERVISOR_PATH.name}: "
            f"{sorted(missing)}"
        )

    def test_immune_service_inits_have_no_await(self):
        """Immune-tier __init__ must be sync (def, not async def) with no awaits."""
        src = _SUPERVISOR_PATH.read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in IMMUNE_SERVICES:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        # __init__ should be a regular FunctionDef, not AsyncFunctionDef
                        assert not isinstance(item, ast.AsyncFunctionDef), (
                            f"{node.name}.__init__ is async — constructors must be synchronous."
                        )
                        # Walk the __init__ body for any Await nodes
                        for child in ast.walk(item):
                            assert not isinstance(child, ast.Await), (
                                f"{node.name}.__init__ contains an await expression.\n"
                                f"Constructors must be synchronous and side-effect free."
                            )

    def test_immune_service_inits_no_ast_calls_to_forbidden_functions(self):
        """AST-level check: no Call nodes to forbidden functions inside __init__."""
        src = _SUPERVISOR_PATH.read_text()
        tree = ast.parse(src)

        # Forbidden fully-qualified function calls (as they appear in AST attr chains)
        forbidden_calls = {
            # (module_or_obj, function_name)
            ("socket", "socket"),
            ("socket", "create_connection"),
            ("requests", "get"),
            ("requests", "post"),
            ("requests", "put"),
            ("requests", "delete"),
            ("requests", "request"),
            ("threading", "Thread"),
            ("subprocess", "run"),
            ("subprocess", "Popen"),
            ("subprocess", "call"),
            ("subprocess", "check_call"),
            ("subprocess", "check_output"),
            ("asyncio", "create_task"),
            ("asyncio", "ensure_future"),
            ("asyncio", "create_subprocess_exec"),
            ("asyncio", "create_subprocess_shell"),
            ("os", "system"),
            ("os", "popen"),
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in IMMUNE_SERVICES:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        for child in ast.walk(item):
                            if isinstance(child, ast.Call):
                                func = child.func
                                # Check attr-style calls: module.function(...)
                                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                                    pair = (func.value.id, func.attr)
                                    assert pair not in forbidden_calls, (
                                        f"{node.name}.__init__ calls {pair[0]}.{pair[1]}() — "
                                        f"forbidden in immune-tier constructors.\n"
                                        f"Move this to initialize() or start()."
                                    )
                                # Check bare open() call
                                if isinstance(func, ast.Name) and func.id == "open":
                                    raise AssertionError(
                                        f"{node.name}.__init__ calls open() — "
                                        f"forbidden in immune-tier constructors.\n"
                                        f"Move this to initialize() or start()."
                                    )
