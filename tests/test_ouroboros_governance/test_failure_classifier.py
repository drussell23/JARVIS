from __future__ import annotations

from backend.core.ouroboros.governance.failure_classifier import (
    FailureClass,
    FailureClassifier,
    failure_signature_hash,
    patch_signature_hash,
)


class _SVR:
    """Minimal stand-in for SandboxValidationResult."""
    def __init__(self, stdout="", stderr="", returncode=1):
        self.passed = False
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.duration_s = 0.1


class TestFailureSignatureHash:
    def test_stable_for_same_input(self):
        h1 = failure_signature_hash(("a::b", "a::c"), "test")
        h2 = failure_signature_hash(("a::b", "a::c"), "test")
        assert h1 == h2

    def test_order_independent(self):
        h1 = failure_signature_hash(("a::b", "a::c"), "test")
        h2 = failure_signature_hash(("a::c", "a::b"), "test")
        assert h1 == h2

    def test_different_for_different_class(self):
        assert failure_signature_hash(("a::b",), "test") != failure_signature_hash(("a::b",), "syntax")

    def test_empty_ids(self):
        h = failure_signature_hash((), "test")
        assert isinstance(h, str) and len(h) == 64  # sha256 hex


class TestPatchSignatureHash:
    def test_stable(self):
        diff = "@@ -1,2 +1,2 @@\n-old\n+new\n context"
        assert patch_signature_hash(diff) == patch_signature_hash(diff)

    def test_different_for_different_diff(self):
        assert patch_signature_hash("diff A") != patch_signature_hash("diff B")


class TestFailureClassifier:
    def _make(self):
        return FailureClassifier()

    def test_classify_syntax(self):
        stdout = "SyntaxError: invalid syntax (foo.py, line 5)\nE   SyntaxError"
        r = self._make().classify(_SVR(stdout=stdout, stderr="SyntaxError at line 5"))
        assert r.failure_class == FailureClass.SYNTAX
        assert r.is_non_retryable is False

    def test_classify_test(self):
        stdout = (
            "FAILED tests/test_foo.py::test_bar - AssertionError\n"
            "FAILED tests/test_foo.py::test_baz - ValueError\n"
            "2 failed, 3 passed"
        )
        r = self._make().classify(_SVR(stdout=stdout))
        assert r.failure_class == FailureClass.TEST
        assert "tests/test_foo.py::test_bar" in r.failing_test_ids
        assert "tests/test_foo.py::test_baz" in r.failing_test_ids

    def test_classify_env_missing_module(self):
        stderr = "ModuleNotFoundError: No module named 'numpy'"
        r = self._make().classify(_SVR(stderr=stderr, returncode=2))
        assert r.failure_class == FailureClass.ENV
        assert r.is_non_retryable is True
        assert r.env_subtype == "missing_dependency"

    def test_classify_env_permission_denied(self):
        stderr = "PermissionError: [Errno 13] Permission denied: '/tmp/foo'"
        r = self._make().classify(_SVR(stderr=stderr))
        assert r.failure_class == FailureClass.ENV
        assert r.is_non_retryable is True
        assert r.env_subtype == "permission_denied"

    def test_classify_env_port_conflict(self):
        stderr = "OSError: [Errno 98] address already in use"
        r = self._make().classify(_SVR(stderr=stderr))
        assert r.failure_class == FailureClass.ENV
        assert r.is_non_retryable is True
        assert r.env_subtype == "port_conflict"

    def test_classify_env_interpreter_mismatch(self):
        stderr = "interpreter mismatch: expected python3.11, got python3.9"
        r = self._make().classify(_SVR(stderr=stderr))
        assert r.failure_class == FailureClass.ENV
        assert r.is_non_retryable is True
        assert r.env_subtype == "interpreter_mismatch"

    def test_classify_parametrized_test_id_preserved(self):
        stdout = "FAILED tests/test_foo.py::test_bar[param1-param2] - AssertionError\n1 failed"
        r = self._make().classify(_SVR(stdout=stdout))
        assert r.failure_class == FailureClass.TEST
        assert r.failing_test_ids == ("tests/test_foo.py::test_bar[param1-param2]",)

    def test_classify_fallback_to_test(self):
        r = self._make().classify(_SVR(stdout="some generic failure\n1 failed"))
        assert r.failure_class == FailureClass.TEST

    def test_failure_signature_hash_populated(self):
        stdout = "FAILED tests/a.py::test_x\n1 failed"
        r = self._make().classify(_SVR(stdout=stdout))
        assert len(r.failure_signature_hash) == 64  # sha256 hex

    def test_top5_failing_tests_capped(self):
        ids = [f"tests/t.py::test_{i}" for i in range(10)]
        stdout = "\n".join(f"FAILED {tid}" for tid in ids) + "\n10 failed"
        r = self._make().classify(_SVR(stdout=stdout))
        assert len(r.failing_test_ids) <= 5
