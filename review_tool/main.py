import argparse
import ast
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langfuse import get_client, observe
from review_tool.llm_client import GroqReviewClient
from review_tool.source_reader import read_source

langfuse = get_client()

MODEL_DEFAULT = "openai/gpt-oss-120b"
MAX_BATCH_CHARS = 8500
SAFE_TPM_TARGET = 6500


def wait_for_token_budget(tokens: int | None) -> None:
    """Best-effort pause to reduce token-rate-limit errors."""
    if tokens is None:
        time.sleep(10)
        return

    seconds = (tokens / SAFE_TPM_TARGET) * 60 + 2
    time.sleep(min(max(seconds, 2), 60))


@observe(name="ast_indexing")
def build_function_index(source: str) -> list[dict[str, Any]]:
    """Index functions and line numbers without executing source."""
    tree = ast.parse(source)
    entries: list[dict[str, Any]] = []

    def visit(node: ast.AST, parents: tuple[str, ...] = ()) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, parents + (child.name,))

            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified_name = ".".join(parents + (child.name,))
                calls: set[str] = set()

                for item in ast.walk(child):
                    if isinstance(item, ast.Call):
                        if isinstance(item.func, ast.Name):
                            calls.add(item.func.id)
                        elif isinstance(item.func, ast.Attribute):
                            calls.add(item.func.attr)

                start_line = (
                    child.decorator_list[0].lineno
                    if child.decorator_list
                    else child.lineno
                )

                entries.append({
                    "name": qualified_name,
                    "short_name": child.name,
                    "start": start_line,
                    "end": getattr(child, "end_lineno", child.lineno),
                    "calls": sorted(calls),
                })

                visit(child, parents + (child.name,))

            else:
                visit(child, parents)

    visit(tree)
    return sorted(entries, key=lambda item: item["start"])


def extract_complete_function(
    source_lines: list[str],
    entry: dict[str, Any],
) -> str:
    """Extract a function with its original line numbers."""
    start = entry["start"]
    end = entry["end"]
    lines = source_lines[start - 1:end]

    numbered_source = "\n".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(lines, start=start)
    )
    return (
        f"Function: {entry['name']}\n"
        f"Original lines: {start}-{end}\n"
        f"SOURCE CODE:\n{numbered_source}"
    )


@observe(name="batch_creation")
def make_function_batches(
    source: str,
    entries: list[dict[str, Any]],
) -> list[list[str]]:
    """Group complete functions into batches."""
    source_lines = source.splitlines()
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_size = 0

    for entry in entries:
        function_text = extract_complete_function(source_lines, entry)
        if (
            current_batch
            and current_size + len(function_text) > MAX_BATCH_CHARS
        ):
            batches.append(current_batch)
            current_batch = []
            current_size = 0

        current_batch.append(function_text)
        current_size += len(function_text)

        if current_size >= MAX_BATCH_CHARS:
            batches.append(current_batch)
            current_batch = []
            current_size = 0

    if current_batch:
        batches.append(current_batch)

    return batches


FINDING_LABELS = {
    "bug": "bug",
    "function": "function",
    "location": "location",
    "evidence": "evidence",
    "impact": "impact",
    "uncertainty": "uncertainty",
    "next checks": "next_checks",
}


def parse_findings(response_text: str) -> list[dict[str, str]]:
    """Parse labeled findings from a model response with multi-line support."""
    if not response_text or not response_text.strip():
        return []

    if "NO_BUGS_FOUND" in response_text.upper() and not re.search(
        r"(?:[-*]\s*)?(?:\*\*|__)?\s*Bug\s*(?:\*\*|__)?\s*:",
        response_text,
        re.IGNORECASE,
    ):
        return []

    header_pattern = re.compile(
        r"^\s*(?:[-*]\s*)?(?:#{1,6}\s*)?(?:\*\*|__)?\s*"
        r"(Bug|Function|Location|Evidence|Impact|Uncertainty|Next checks)"
        r"\s*(?:\*\*|__)?\s*:\s*(.*)$",
        re.IGNORECASE,
    )

    findings: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_key: str | None = None

    def save_current() -> None:
        if all(current.get(k) for k in ("bug", "function", "location")):
            if current["bug"].strip().lower() in (
                "none",
                "no bugs found",
                "no bug found",
                "n/a",
                "no relevant bugs identified",
                "no_bugs_found",
            ):
                current.clear()
                return

            for key in ("evidence", "impact", "uncertainty", "next_checks"):
                current.setdefault(key, "Not specified by the model.")

            findings.append({k: v.strip() for k, v in current.items()})
            current.clear()

    for line in response_text.splitlines():
        match = header_pattern.match(line)
        if match:
            raw_label = match.group(1).lower()
            key = FINDING_LABELS[raw_label]
            value = match.group(2).strip().strip("*_").strip()

            if key in current:
                save_current()

            current[key] = value
            current_key = key
        elif current_key and line.strip() and current:
            current[current_key] += "\n" + line.strip()
        elif not line.strip() and all(
            current.get(k) for k in ("bug", "function", "location")
        ):
            save_current()
            current_key = None

    save_current()
    return findings


@observe(name="review_batch")
def review_batch(
    llm: GroqReviewClient,
    request: str,
    rules: str,
    batch: list[str],
    evidence: dict[str, Any],
) -> list[dict[str, str]]:
    """Review one batch and retain the prompt and response."""
    source_text = "\n\n".join(batch)

    system_prompt = (
        "You are a careful Python code reviewer. "
        "Analyze source code as text only. Never claim execution. "
        "Ground findings in source and business rules."
    )

    user_prompt = f"""
USER REQUEST:
{request}

BUSINESS RULES:
{rules or "No business rules supplied."}

SOURCE FUNCTIONS WITH ORIGINAL LINE NUMBERS:
{source_text}

Review the supplied functions against the user's request and business rules.

1. Identify only bugs relevant to the user's request.
2. Report a bug only when there is sufficient evidence in the source code and business rules.
3. Do not report unrelated defects.
4. Do not report multiple findings for the same underlying defect.
5. Do not report consequences of an existing finding as separate bugs unless they are independently actionable defects.
6. Do not treat assumptions or possible risks as confirmed bugs.
7. If the request concerns a specific behavior, examine that behavior and its necessary dependencies, but review every supplied function.
8. Report multiple findings only when they represent distinct, evidence-supported defects.
9. Do not invent line numbers or claim code was executed.

For every finding, return exactly these fields:
Bug: concise defect
Function: exact function or class name
Location: original source line number(s)
Evidence: source expression or behavior supporting the finding
Impact: how the issue could cause the reported behavior
Uncertainty: what cannot be confirmed from the supplied source
Next checks: a practical check or test to verify the concern

Separate findings with a blank line.
If no supported relevant bug is found, return exactly:
NO_BUGS_FOUND

Do not claim the program was executed.
"""

    evidence["calls"].append({
        "stage": "review",
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "code_context": source_text,
        "raw_response": None,
    })

    response, _ = llm.complete(system_prompt, user_prompt)
    evidence["calls"][-1]["raw_response"] = response

    return parse_findings(response)


def deduplicate_findings(
    findings: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Remove exact duplicate bug/function/location combinations."""
    unique: list[dict[str, str]] = []
    seen = set()

    for item in findings:
        key = (
            item.get("bug", "").lower().strip(),
            item.get("function", "").lower().strip(),
            item.get("location", "").lower().strip(),
        )

        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


def calculate_usage(
    call_metadata: list[dict[str, Any]],
) -> dict[str, Any]:
    """Sum available token usage and count calls with unknown usage."""
    totals: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "known_usage_calls": 0,
        "unknown_usage_calls": 0,
    }

    for call in call_metadata:
        if call.get("total_tokens") is None:
            totals["unknown_usage_calls"] += 1
            continue

        totals["known_usage_calls"] += 1

        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = call.get(key)
            if value is not None:
                totals[key] += value

    return totals


def save_evidence(
    evidence: dict[str, Any],
    output_dir: str | Path,
) -> Path:
    """Save complete review evidence without overwriting earlier reviews."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"review_{uuid4().hex}.json"

    with path.open("x", encoding="utf-8") as handle:
        json.dump(
            evidence,
            handle,
            indent=2,
            ensure_ascii=False,
        )

    return path


@observe(name="code_review_run")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review Python source code for bugs."
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Path to the Python source file.",
    )
    parser.add_argument(
        "--request",
        required=True,
        help="Describe what you want reviewed.",
    )
    parser.add_argument(
        "--business-rules",
        help="Optional path to business rules.",
    )
    parser.add_argument(
        "--output-dir",
        default="reviews",
        help="Evidence folder (default: reviews).",
    )

    args = parser.parse_args(argv)

    if not args.request.strip():
        parser.error("--request cannot be blank.")

    if os.getenv("LANGFUSE_PUBLIC_KEY"):
        try:
            langfuse.update_current_span(
                metadata={
                    "source_file": str(args.file),
                    "business_rules": args.business_rules,
                    "request": args.request,
                }
            )
        except Exception:
            pass

    evidence: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "request": args.request,
        "source_file": str(args.file),
        "business_rules_file": args.business_rules,
        "provider": "Groq",
        "model": os.getenv("GROQ_MODEL", MODEL_DEFAULT),
        "outcome": "failed",
        "warnings": [],
        "calls": [],
        "findings": [],
    }

    llm = None
    findings: list[dict[str, str]] = []
    source_path = Path(args.file)

    try:
        source = read_source(args.file)

        rules = (
            read_source(args.business_rules)
            if args.business_rules
            else ""
        )

        if not source.strip():
            raise ValueError("The source file is empty.")

        entries = build_function_index(source)

        if not entries:
            raise ValueError(
                "No Python functions were found in the source file."
            )

        source_hash_before = hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest()

        llm = GroqReviewClient()
        evidence["model"] = llm.model

        evidence["scope"] = "all_functions"
        batches = make_function_batches(source, entries)

        if not batches:
            raise ValueError("No source batches were created.")

        for index, batch in enumerate(batches):
            batch_findings = review_batch(
                llm,
                args.request,
                rules,
                batch,
                evidence,
            )

            findings.extend(batch_findings)
            findings = deduplicate_findings(findings)

            evidence["findings"] = findings.copy()

            if index < len(batches) - 1:
                wait_for_token_budget(
                    llm.calls[-1].get("total_tokens")
                )

        findings = deduplicate_findings(findings)
        evidence["findings"] = findings.copy()

        source_hash_after = hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest()

        evidence["source_sha256_before"] = source_hash_before
        evidence["source_sha256_after"] = source_hash_after
        evidence["source_unchanged"] = (
            source_hash_before == source_hash_after
        )

        if not evidence["source_unchanged"]:
            raise RuntimeError("The source file changed during review.")

        evidence["outcome"] = "success"

    except SyntaxError as exc:
        evidence["error"] = (
            f"Cannot parse Python source at line "
            f"{exc.lineno}: {exc.msg}"
        )

    except Exception as exc:
        evidence["findings"] = findings.copy()
        evidence["error"] = f"{type(exc).__name__}: {exc}"

    finally:
        if llm is not None:
            evidence["call_metadata"] = llm.calls
            evidence["usage_totals"] = calculate_usage(llm.calls)
        else:
            evidence["call_metadata"] = []
            evidence["usage_totals"] = {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "known_usage_calls": 0,
                "unknown_usage_calls": 0,
            }

        evidence["completed_at_utc"] = datetime.now(
            timezone.utc
        ).isoformat()

        try:
            output_path = save_evidence(evidence, args.output_dir)
            print(f"JSON saved to: {output_path.resolve()}")
            print(
                f"Findings saved: {len(evidence.get('findings', []))}"
            )
        except Exception as save_exc:
            evidence["outcome"] = "failed"
            evidence["error"] = (
                f"Could not save review evidence: "
                f"{type(save_exc).__name__}: {save_exc}"
            )

        if os.getenv("LANGFUSE_PUBLIC_KEY"):
            try:
                if evidence.get("outcome") == "success":
                    findings_count = len(evidence.get("findings", []))
                    langfuse.score_current_trace(
                        name="findings_count",
                        value=findings_count,
                        comment=f"{findings_count} findings identified",
                    )
                trace_url = langfuse.get_trace_url()
                if trace_url:
                    print(f"Langfuse Trace: {trace_url}")
                langfuse.flush()
            except Exception:
                pass

    if evidence["outcome"] != "success":
        print(
            "Review failed: "
            f"{evidence.get('error', 'Unknown error')}"
        )
        return 1

    final_findings = evidence["findings"]

    if not final_findings:
        print("No relevant bugs identified by the model.")
        return 0

    for index, finding in enumerate(final_findings):
        if index:
            print()

        for label, key in (
            ("Bug", "bug"),
            ("Function", "function"),
            ("Location", "location"),
            ("Evidence", "evidence"),
            ("Impact", "impact"),
            ("Uncertainty", "uncertainty"),
            ("Next checks", "next_checks"),
        ):
            print(f"{label}: {finding.get(key, 'Not specified.')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
