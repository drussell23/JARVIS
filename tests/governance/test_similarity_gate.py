"""Tests for Ouroboros GATE diff-aware similarity check."""
import textwrap

import pytest


def test_similarity_gate_high_overlap():
    """Added code that mostly copies existing source -> escalate."""
    from backend.core.ouroboros.governance.similarity_gate import check_similarity

    source = textwrap.dedent('''\
        def process(items):
            result = []
            for item in items:
                if item.is_valid():
                    transformed = item.transform()
                    result.append(transformed)
            return result
    ''')
    candidate = textwrap.dedent('''\
        def process(items):
            result = []
            for item in items:
                if item.is_valid():
                    transformed = item.transform()
                    result.append(transformed)
            return result

        def process_v2(items):
            result = []
            for item in items:
                if item.is_valid():
                    transformed = item.transform()
                    result.append(transformed)
            return result
    ''')
    result = check_similarity(candidate, source)
    assert result is not None
    assert "similarity" in result.lower() or "overlap" in result.lower()


def test_similarity_gate_small_edit():
    """Small legitimate edit in existing function -> no escalation."""
    from backend.core.ouroboros.governance.similarity_gate import check_similarity

    source = textwrap.dedent('''\
        def process(items):
            result = []
            for item in items:
                if item.is_valid():
                    result.append(item)
            return result
    ''')
    candidate = textwrap.dedent('''\
        def process(items):
            result = []
            for item in items:
                if item.is_valid():
                    transformed = item.transform()
                    result.append(transformed)
            return result
    ''')
    result = check_similarity(candidate, source)
    assert result is None


def test_similarity_gate_deletion_only():
    """Pure deletion: no added lines -> overlap 0 -> no escalation."""
    from backend.core.ouroboros.governance.similarity_gate import check_similarity

    source = textwrap.dedent('''\
        def func_a():
            pass

        def func_b():
            pass

        def func_c():
            pass
    ''')
    candidate = textwrap.dedent('''\
        def func_a():
            pass

        def func_c():
            pass
    ''')
    result = check_similarity(candidate, source)
    assert result is None


def test_similarity_gate_entirely_new_code():
    """Entirely new code unrelated to source -> no escalation."""
    from backend.core.ouroboros.governance.similarity_gate import check_similarity

    source = textwrap.dedent('''\
        def validate_input(data):
            if not data:
                raise ValueError("empty")
            return True
    ''')
    candidate = textwrap.dedent('''\
        def validate_input(data):
            if not data:
                raise ValueError("empty")
            return True

        def send_email(to, subject, body):
            import smtplib
            msg = f"Subject: {subject}\\n\\n{body}"
            server = smtplib.SMTP("localhost", 587)
            server.starttls()
            server.login("bot", "pass")
            server.sendmail("bot@jarvis", to, msg)
            server.quit()
    ''')
    result = check_similarity(candidate, source)
    assert result is None
