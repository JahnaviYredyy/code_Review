
"""Automated tests for the AI Code Review Tool.

Run from the project root:
    python -m pytest -v tests
"""

import json
from pathlib import Path
import pytest
import review_tool.main as review_main

SOURCE_TEXT = """\
def calculate_total(price, quantity):
    return price * quantity
"""

VALID_FINDING = """\
Bug: Negative quantities are not validated.
Function: calculate_total
Location: 1-2
Evidence: The function multiplies quantity without checking its value.
Impact: A negative quantity can produce a negative total.
Uncertainty: The caller may validate quantity elsewhere.
Next checks: Check input validation performed by callers.
"""


class FakeGroqReviewClient:
    """Simulates successful Groq responses without making API calls."""

    def __init__(self):
        self.model = "fake-test-model"
        self.calls = []
        self.responses = [

            VALID_FINDING,
        ]

    def complete(self, system_prompt, user_prompt, **kwargs):
        self.calls.append({
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        })

        if not self.responses:
            raise AssertionError("Unexpected extra provider call")

        return self.responses.pop(0), {"total_tokens": 15}


class FailingGroqReviewClient:
    """Simulates a provider outage."""

    def __init__(self):
        self.model = "fake-test-model"
        self.calls = []

    def complete(self, system_prompt, user_prompt, **kwargs):
        self.calls.append({
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        })
        raise RuntimeError("Simulated provider outage")


def install_fake_source_reader(monkeypatch):
    """Read temporary test files without depending on project-specific logic."""

    def fake_read_source(path):
        return Path(path).read_text(encoding="utf-8")

    monkeypatch.setattr(review_main, "read_source", fake_read_source)


def get_evidence_files(output_dir):
    return list(Path(output_dir).glob("review_*.json"))


def test_successful_output_and_source_preservation(
    tmp_path, monkeypatch, capsys
):
    """Successful review produces findings and leaves source unchanged."""

    source_file = tmp_path / "sample.py"
    source_file.write_text(SOURCE_TEXT, encoding="utf-8")
    original_bytes = source_file.read_bytes()

    output_dir = tmp_path / "reviews"

    install_fake_source_reader(monkeypatch)
    monkeypatch.setattr(
        review_main,
        "GroqReviewClient",
        FakeGroqReviewClient,
    )

    # Avoid waiting between batches during automated testing.
    monkeypatch.setattr(
        review_main,
        "wait_for_token_budget",
        lambda _tokens: None,
    )

    exit_code = review_main.main([
        "--file", str(source_file),
        "--request", "Review the total calculation",
        "--output-dir", str(output_dir),
    ])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Negative quantities are not validated" in captured.out

    # The original source must remain byte-for-byte identical.
    assert source_file.read_bytes() == original_bytes

    files = get_evidence_files(output_dir)
    assert len(files) == 1

    evidence = json.loads(files[0].read_text(encoding="utf-8"))

    assert evidence["outcome"] == "success"
    assert evidence["source_unchanged"] is True
    assert len(evidence["findings"]) == 1
    assert evidence["findings"][0]["function"] == "calculate_total"


def test_missing_source_file_is_handled(
    tmp_path, monkeypatch, capsys
):
    """A missing source file fails cleanly before contacting Groq."""

    output_dir = tmp_path / "reviews"

    install_fake_source_reader(monkeypatch)

    def provider_must_not_be_created():
        pytest.fail("Groq should not be created for a missing file")

    monkeypatch.setattr(
        review_main,
        "GroqReviewClient",
        provider_must_not_be_created,
    )

    exit_code = review_main.main([
        "--file", str(tmp_path / "does_not_exist.py"),
        "--request", "Review this file",
        "--output-dir", str(output_dir),
    ])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Review failed:" in captured.out

    files = get_evidence_files(output_dir)
    assert len(files) == 1

    evidence = json.loads(files[0].read_text(encoding="utf-8"))

    assert evidence["outcome"] == "failed"
    assert "FileNotFoundError" in evidence["error"]


def test_invalid_python_syntax_is_handled(
    tmp_path, monkeypatch, capsys
):
    """Invalid Python syntax is rejected before contacting Groq."""

    source_file = tmp_path / "invalid.py"
    invalid_source = "def broken(:\n    pass\n"
    source_file.write_text(invalid_source, encoding="utf-8")
    original_bytes = source_file.read_bytes()

    output_dir = tmp_path / "reviews"

    install_fake_source_reader(monkeypatch)

    def provider_must_not_be_created():
        pytest.fail("Groq should not be created for invalid Python")

    monkeypatch.setattr(
        review_main,
        "GroqReviewClient",
        provider_must_not_be_created,
    )

    exit_code = review_main.main([
        "--file", str(source_file),
        "--request", "Review this file",
        "--output-dir", str(output_dir),
    ])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot parse Python source" in captured.out
    assert source_file.read_bytes() == original_bytes


def test_provider_failure_is_recorded(
    tmp_path, monkeypatch, capsys
):
    """A provider error must not be reported as a successful review."""

    source_file = tmp_path / "sample.py"
    source_file.write_text(SOURCE_TEXT, encoding="utf-8")
    original_bytes = source_file.read_bytes()

    output_dir = tmp_path / "reviews"

    install_fake_source_reader(monkeypatch)

    monkeypatch.setattr(
        review_main,
        "GroqReviewClient",
        FailingGroqReviewClient,
    )

    exit_code = review_main.main([
        "--file", str(source_file),
        "--request", "Review this file",
        "--output-dir", str(output_dir),
    ])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Review failed:" in captured.out

    # Provider failure must not alter the original source.
    assert source_file.read_bytes() == original_bytes

    files = get_evidence_files(output_dir)
    assert len(files) == 1

    evidence = json.loads(files[0].read_text(encoding="utf-8"))

    assert evidence["outcome"] == "failed"
    assert "Simulated provider outage" in evidence["error"]
    assert evidence["findings"] == []


def test_parse_findings_with_markdown_bold_and_multiline():
    """Verify bold markdown (**Bug**:) and multi-line explanations are preserved."""
    sample = """
**Bug**: Negative quantities are not validated
**Function**: calculate_total
**Location**: 1-2
**Evidence**: Line 2 multiplies price by quantity without validation.
Additional line of evidence details.
**Impact**: Customers can be credited money.
**Uncertainty**: None
**Next checks**:
1. Check negative price
2. Check zero quantity
"""
    findings = review_main.parse_findings(sample)
    assert len(findings) == 1
    assert findings[0]["bug"] == "Negative quantities are not validated"
    assert "Additional line of evidence details." in findings[0]["evidence"]
    assert "1. Check negative price" in findings[0]["next_checks"]


def test_build_function_index_captures_decorator_lines():
    """Verify function start line accounts for decorators."""
    code = """\
@some_decorator
def decorated_function():
    pass
"""
    entries = review_main.build_function_index(code)
    assert len(entries) == 1
    assert entries[0]["start"] == 1
