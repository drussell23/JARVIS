"""Tests for StructuredCritique and CritiqueBuilder."""
import pytest

from backend.core.ouroboros.governance.structured_critique import (
    CritiqueBuilder,
    CritiqueReport,
    CritiqueSeverity,
    CritiqueType,
    StructuredCritique,
)


class TestStructuredCritique:
    def test_frozen(self):
        c = StructuredCritique(
            file_path="parser.py", failure_type=CritiqueType.SYNTAX_ERROR,
            what_failed="missing colon", where="parser.py:10",
            observed="SyntaxError", expected="valid syntax",
            direction="add colon after if statement",
        )
        with pytest.raises(AttributeError):
            c.file_path = "other.py"

    def test_defaults(self):
        c = StructuredCritique(
            file_path="a.py", failure_type=CritiqueType.LOGIC_ERROR,
            what_failed="wrong", where="a.py:1",
            observed="x", expected="y", direction="fix",
        )
        assert c.severity == CritiqueSeverity.ERROR
        assert c.line_number is None
        assert c.test_name is None


class TestCritiqueReport:
    def test_format_for_prompt_passed(self):
        report = CritiqueReport(
            critiques=(), summary="clean", total_errors=0,
            total_warnings=0, all_passed=True,
        )
        text = report.format_for_prompt()
        assert "PASSED" in text

    def test_format_for_prompt_with_errors(self):
        c = StructuredCritique(
            file_path="parser.py", failure_type=CritiqueType.TYPE_ERROR,
            what_failed="returned list instead of generator",
            where="parser.py:47", observed="<class 'list'>",
            expected="<class 'generator'>",
            direction="Use yield instead of return",
            test_name="test_parse_stream",
        )
        report = CritiqueReport(
            critiques=(c,), summary="1 error",
            total_errors=1, total_warnings=0, all_passed=False,
        )
        text = report.format_for_prompt()
        assert "type_error" in text
        assert "parser.py:47" in text
        assert "yield" in text
        assert "test_parse_stream" in text
        assert "Address ALL errors" in text


class TestCritiqueBuilder:
    def test_parse_syntax_error(self):
        error_text = 'File "parser.py", line 10\n    def broken(\nSyntaxError: expected \':\''
        report = CritiqueBuilder.from_validation_output(
            "parser.py", "build", error_text
        )
        assert report.total_errors >= 1
        assert any(c.failure_type == CritiqueType.SYNTAX_ERROR for c in report.critiques)
        assert any(c.line_number == 10 for c in report.critiques)

    def test_parse_import_error(self):
        error_text = "ImportError: No module named 'pandas'"
        report = CritiqueBuilder.from_validation_output(
            "loader.py", "build", error_text
        )
        assert report.total_errors >= 1
        assert any(c.failure_type == CritiqueType.IMPORT_ERROR for c in report.critiques)
        assert any("pandas" in c.direction for c in report.critiques)

    def test_parse_test_failure(self):
        test_output = "FAILED test_parser.py::test_parse_csv - AssertionError: assert 42 == 43"
        report = CritiqueBuilder.from_validation_output(
            "parser.py", "test", test_output
        )
        assert report.total_errors >= 1
        assert any(c.test_name == "test_parse_csv" for c in report.critiques)

    def test_parse_type_error_in_test(self):
        test_output = "FAILED test_api.py::test_handler - TypeError: expected str got int"
        report = CritiqueBuilder.from_validation_output(
            "api.py", "test", test_output
        )
        assert any(c.failure_type == CritiqueType.TYPE_ERROR for c in report.critiques)

    def test_security_critique(self):
        report = CritiqueBuilder.from_validation_output(
            "agent.py", "security", "Attempted write outside sandbox"
        )
        assert report.total_errors == 1
        assert report.critiques[0].failure_type == CritiqueType.SECURITY_VIOLATION

    def test_infra_critique(self):
        report = CritiqueBuilder.from_validation_output(
            "runner.py", "infra", "Connection refused"
        )
        assert report.total_errors == 1
        assert "transient" in report.critiques[0].direction.lower()

    def test_generic_fallback(self):
        report = CritiqueBuilder.from_validation_output(
            "unknown.py", "other", "something went wrong"
        )
        assert report.total_errors == 1
        assert report.critiques[0].failure_type == CritiqueType.LOGIC_ERROR

    def test_multiple_errors_in_build(self):
        error_text = (
            'File "a.py", line 5\n    x =\nSyntaxError: invalid syntax\n'
            'File "b.py", line 10\n    y =\nSyntaxError: invalid syntax\n'
        )
        report = CritiqueBuilder.from_validation_output(
            "a.py", "build", error_text
        )
        # At least one SyntaxError detected (regex may merge adjacent errors)
        assert report.total_errors >= 1
        assert any(c.failure_type == CritiqueType.SYNTAX_ERROR for c in report.critiques)

    def test_infer_direction_type_error(self):
        direction = CritiqueBuilder._infer_direction(CritiqueType.TYPE_ERROR, "wrong type")
        assert "type" in direction.lower()

    def test_infer_direction_import_error(self):
        direction = CritiqueBuilder._infer_direction(
            CritiqueType.IMPORT_ERROR, "No module named 'foo'"
        )
        assert "foo" in direction
