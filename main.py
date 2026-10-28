
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

from review_tool.llm_client import GroqReviewClient
from review_tool.source_reader import read_source


MODEL_DEFAULT = "openai/gpt-oss-120b"
MAX_BATCH_CHARS = 8500
SAFE_TPM_TARGET = 6500


def wait_for_token_budget(tokens: int | None) -> None:
    """Best-effort pause to reduce token-rate-limit errors."""
    if not tokens:
        time.sleep(10)
        return

    seconds = (tokens / SAFE_TPM_TARGET) * 60 + 2
    time.sleep(min(max(seconds, 2), 60))


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

                entries.append({
                    "name": qualified_name,
                    "short_name": child.name,
                    "start": child.lineno,
                    "end": getattr(child, "end_lineno", child.lineno),
                    "calls": sorted(calls),
                })

                visit(child, parents + (child.name,))

            else:
                visit(child, parents)

    visit(tree)
    return sorted(entries, key=lambda item: item["start"])


def build_index_text(entries: list[dict[str, Any]]) -> str:
    lines = []

    for entry in entries:
        calls = ", ".join(entry["calls"]) or "none"
        lines.append(
            f"{entry['name']} | "
            f"lines {entry['start']}-{entry['end']} | "
            f"calls: {calls}"
        )

    return "\n".join(lines)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from a model response."""
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()

    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue

    return None


def plan_review(
    llm: GroqReviewClient,
    request: str,
    rules: str,
    entries: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Choose review scope and validate the selected functions."""
    if not entries:
        return "all", []

    index_text = build_index_text(entries)

    system_prompt = (
        "You are a careful Python code-review planner. "
        "Return a valid JSON review plan."
    )

    user_prompt = f"""
Plan a Python source-code review.

USER REQUEST:
{request}

BUSINESS RULES:
{rules or "No business rules supplied."}

AVAILABLE FUNCTIONS:
{index_text}

Understand the user's goal and choose the appropriate scope.
For a broad review, select all functions.
For a specific issue, select relevant functions and helpers.

Return one JSON object in one of these formats:
{{"scope":"all","functions":[]}}
or
{{"scope":"focused","functions":["ClassName.function_name"]}}

Rules:
- scope must be "all" or "focused".
- For "all", functions must be an empty list.
- For "focused", use exact qualified names from the index.
- Do not invent function names.
- Return JSON only.
"""

    evidence["calls"].append({
        "stage": "planning",
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "code_context": index_text,
        "raw_response": None,
    })

    response, _ = llm.complete(
        system_prompt,
        user_prompt,
        max_tokens=800,
    )
    evidence["calls"][-1]["raw_response"] = response

    plan = extract_json_object(response)

    if not plan:
        evidence["warnings"].append(
            "Invalid planning JSON; reviewed all functions."
        )
        return "all", entries

    scope = plan.get("scope")
    names = plan.get("functions")

    if (
        scope not in {"all", "focused"}
        or not isinstance(names, list)
        or not all(isinstance(name, str) for name in names)
    ):
        evidence["warnings"].append(
            "Invalid planning schema; reviewed all functions."
        )
        return "all", entries

    if scope == "all":
        return "all", entries

    by_name = {entry["name"]: entry for entry in entries}

    selected = [
        by_name[name]
        for name in dict.fromkeys(names)
        if name in by_name
    ]

    if not selected:
        evidence["warnings"].append(
            "No valid functions selected; reviewed all functions."
        )
        return "all", entries

    return "focused", selected


def expand_dependencies(
    selected: list[dict[str, Any]],
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Include discoverable functions called by selected functions."""
    by_short_name: dict[str, list[dict[str, Any]]] = {}

    for entry in entries:
        by_short_name.setdefault(entry["short_name"], []).append(entry)

    by_qualified_name = {entry["name"]: entry for entry in entries}

    included = {entry["name"]: entry for entry in selected}
    frontier = list(selected)

    while frontier:
        next_frontier = []

        for entry in frontier:
            for called_name in entry["calls"]:
                candidate = by_qualified_name.get(called_name)
                candidates = (
                    [candidate]
                    if candidate
                    else by_short_name.get(called_name, [])
                )

                for item in candidates:
                    if item["name"] not in included:
                        included[item["name"]] = item
                        next_frontier.append(item)

        frontier = next_frontier

    return sorted(included.values(), key=lambda item: item["start"])


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
    """Parse labeled findings from a model response."""
    if not response_text:
        return []

    if (
        "NO_BUGS_FOUND" in response_text.upper()
        and not re.search(r"\bBug\s*:", response_text, re.IGNORECASE)
    ):
        return []

    pattern = re.compile(
        r"^\s*(?:[-*]\s*)?\**\s*"
        r"(Bug|Function|Location|Evidence|Impact|"
        r"Uncertainty|Next checks)"
        r"\s*:\s*\**\s*(.*?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    findings: list[dict[str, str]] = []
    current: dict[str, str] = {}

    def save_current() -> None:
        if all(
            current.get(key)
            for key in ("bug", "function", "location")
        ):
            for key in (
                "evidence",
                "impact",
                "uncertainty",
                "next_checks",
            ):
                current.setdefault(key, "Not specified by the model.")

            findings.append(current.copy())

    for match in pattern.finditer(response_text):
        label = match.group(1).lower()
        key = FINDING_LABELS[label]
        value = match.group(2).strip().strip("*").strip()

        if key == "bug":
            save_current()
            current = {"bug": value}
        elif current:
            current[key] = value

    save_current()
    return findings


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

Report all distinct defects supported by the supplied source and rules.
Distinguish source evidence from assumptions. Do not invent line numbers.

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

        # Parse syntax only; do not import or execute source code.
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

        scope, selected = plan_review(
            llm,
            args.request,
            rules,
            entries,
            evidence,
        )
        evidence["scope"] = scope

        relevant = (
            entries
            if scope == "all"
            else expand_dependencies(selected, entries)
        )

        batches = make_function_batches(source, relevant)

        if not batches:
            raise ValueError("No source batches were created.")

        # Accumulate findings from every batch.
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

            # Keep evidence synchronized with the accumulated findings.
            evidence["findings"] = findings.copy()

            if index < len(batches) - 1:
                wait_for_token_budget(
                    llm.calls[-1].get("total_tokens")
                )

        # IMPORTANT:
        # Do not ask another LLM call to consolidate findings.
        # That extra step could omit valid findings.
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
        # Preserve any findings accumulated before the error.
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
            evidence["outcome"]="failed"
            evidence["error"]=(
                f"Could not save review evidence:"
                f"{type(save_exc).__name__}:{save_exc}"
            )
            print(f"Review failed: {evidence['error']}"
            )

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

    # Print the exact same findings list used by save_evidence().
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
