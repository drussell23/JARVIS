"""Tests for Ouroboros VALIDATE duplication guard."""
import textwrap

import pytest


def test_exact_fingerprint_duplicate():
    """Strict match: copy-pasted function with different name -> caught."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    source = textwrap.dedent('''\
        def existing_filter(steps):
            """Filter bad steps."""
            result = []
            for s in steps:
                if s.action != "click":
                    result.append(s)
            return result
    ''')
    candidate = textwrap.dedent('''\
        def existing_filter(steps):
            """Filter bad steps."""
            result = []
            for s in steps:
                if s.action != "click":
                    result.append(s)
            return result

        def new_filter(steps):
            """Filter bad steps."""
            result = []
            for s in steps:
                if s.action != "click":
                    result.append(s)
            return result
    ''')
    result = check_duplication(candidate, source, "module.py")
    assert result is not None
    assert "new_filter" in result
    assert "duplication" in result.lower() or "similar" in result.lower()


def test_fuzzy_jaccard_above_threshold():
    """Near-duplicate: same structure with minor tweaks -> caught."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    source = textwrap.dedent('''\
        def process_data(items):
            """Process items."""
            output = []
            for item in items:
                if item.valid:
                    transformed = item.value * 2
                    output.append(transformed)
            return output
    ''')
    candidate = textwrap.dedent('''\
        def process_data(items):
            """Process items."""
            output = []
            for item in items:
                if item.valid:
                    transformed = item.value * 2
                    output.append(transformed)
            return output

        def handle_records(records):
            """Handle records."""
            results = []
            for record in records:
                if record.valid:
                    converted = record.value * 2
                    results.append(converted)
            return results
    ''')
    result = check_duplication(candidate, source, "module.py")
    assert result is not None
    assert "handle_records" in result


def test_fuzzy_jaccard_below_threshold():
    """Legitimately different code -> passes."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    source = textwrap.dedent('''\
        def validate_input(data):
            """Check input data."""
            if not isinstance(data, dict):
                raise TypeError("Expected dict")
            return True
    ''')
    candidate = textwrap.dedent('''\
        def validate_input(data):
            """Check input data."""
            if not isinstance(data, dict):
                raise TypeError("Expected dict")
            return True

        def send_notification(user, message):
            """Send a notification to user."""
            import smtplib
            server = smtplib.SMTP("localhost")
            server.sendmail("bot@jarvis", user.email, message)
            server.quit()
            return True
    ''')
    result = check_duplication(candidate, source, "module.py")
    assert result is None


def test_modified_function_not_flagged():
    """Editing an existing function (same name) -> not flagged."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication

    source = textwrap.dedent('''\
        def process(data):
            return data
    ''')
    candidate = textwrap.dedent('''\
        def process(data):
            if not data:
                return []
            return [x * 2 for x in data]
    ''')
    result = check_duplication(candidate, source, "module.py")
    assert result is None


def test_syntax_error_skips_guard():
    """Unparseable source -> skip guard, return None."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication
    result = check_duplication("def good(): pass", "def broken(:", "module.py")
    assert result is None


def test_non_python_skips_guard():
    """Non-.py files -> skip guard, return None."""
    from backend.core.ouroboros.governance.duplication_checker import check_duplication
    result = check_duplication("const x = 1;", "const y = 2;", "file.js")
    assert result is None
