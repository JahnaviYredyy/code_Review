import argparse
import ast
import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langfuse import get_client, observe
from review_tool.llm_client import GroqReviewClient
from review_tool.repository import (
    assemble_investigation_context,
    expand_query_with_ai,
    index_repository,
    investigate_code,
    scan_repository,
    search_relevant_symbols,
)
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


def run_repo_investigation(args: argparse.Namespace) -> int:
    """Investigate a plain-English question across a multi-file Python repository."""
    evidence: dict[str, Any] = {
        "mode": "repository_investigation",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "request": args.request,
        "repository_path": str(args.repo),
        "provider": "Groq",
        "model": os.getenv("GROQ_MODEL", MODEL_DEFAULT),
        "outcome": "failed",
        "scanned_files": 0,
        "indexed_symbols": 0,
        "search_tokens": [],
        "candidates_found": 0,
        "files_inspected": [],
        "cross_file_hops": [],
        "call_metadata": [],
        "usage_totals": {},
        "investigation_report": None,
    }

    if os.getenv("LANGFUSE_PUBLIC_KEY"):
        try:
            langfuse.update_current_span(
                metadata={
                    "mode": "repository_investigation",
                    "repository_path": str(args.repo),
                    "request": args.request,
                }
            )
        except Exception:
            pass

    llm = None
    report = None

    try:
        # Step 0: Check for business rules (explicit argument or auto-detected in repo)
        rules_text = ""
        rules_source = None
        if args.business_rules:
            rules_source = args.business_rules
            rules_text = read_source(args.business_rules)
        else:
            repo_path = Path(args.repo)
            candidate_names = (
                "BUSINESS_RULES.md", "business_rules.md", "RULES.md", "rules.md",
                "SPECIFICATION.md", "specification.md", "REQUIREMENTS.md", "requirements.md",
            )
            # Check repository root first
            for candidate_name in candidate_names:
                candidate_path = repo_path / candidate_name
                if candidate_path.is_file():
                    rules_source = candidate_name
                    rules_text = read_source(str(candidate_path))
                    break

            # If not at root, search subdirectories (e.g. review-input/, docs/, etc.)
            if not rules_text:
                for candidate_name in candidate_names:
                    matches = [
                        p for p in repo_path.glob(f"**/{candidate_name}")
                        if ".git" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts
                    ]
                    if matches:
                        rules_source = str(matches[0].relative_to(repo_path))
                        rules_text = read_source(str(matches[0]))
                        break

        if rules_source and rules_text.strip():
            evidence["business_rules_file"] = str(rules_source)
            print(f"\n[AI Tracing] Loaded business rules: {rules_source} ({len(rules_text):,} chars).")

        # Step 1: Discover all Python files in the repository (Read-only)
        scan_result = scan_repository(args.repo)
        evidence["scanned_files"] = scan_result.total_files
        print(f"\n[AI Tracing] Discovered {scan_result.total_files} Python file(s) in repository.")

        if scan_result.total_files == 0:
            raise ValueError(f"No Python files found in repository: {args.repo}")

        # Step 2: Index AST across all discovered files
        index = index_repository(scan_result.files, scan_result.repository_path)
        evidence["indexed_symbols"] = index.total_symbols
        print(f"[AI Tracing] Indexed {index.total_symbols} symbols across {index.total_files_indexed} file(s).")

        # Step 3: Search relevant candidate symbols matching user request
        search_result = search_relevant_symbols(index, args.request)
        evidence["search_tokens"] = search_result.query_tokens
        evidence["candidates_found"] = len(search_result.candidates)
        print(f"[AI Tracing] Search tokens: {search_result.query_tokens} -> Found {len(search_result.candidates)} candidate symbol(s).")

        # If conversational phrasing, slang, or metaphors returned no candidates, try AI keyword expansion fallback
        if not search_result.candidates:
            try:
                expansion_llm = GroqReviewClient()
                ai_keywords = expand_query_with_ai(expansion_llm, args.request)
                if ai_keywords:
                    print(f"[AI Tracing] Expanding search with AI code concepts: {ai_keywords}")
                    search_result = search_relevant_symbols(index, args.request, extra_tokens=ai_keywords)
                    evidence["search_tokens"] = search_result.query_tokens
                    evidence["candidates_found"] = len(search_result.candidates)
                    print(f"[AI Tracing] Recovered {len(search_result.candidates)} candidate symbol(s) after expansion.")
            except Exception:
                pass

        if not search_result.candidates:
            # Insufficient evidence before contacting LLM
            report_dict = {
                "outcome": "INSUFFICIENT_EVIDENCE",
                "summary": f"No code symbols or keywords matching '{args.request}' were found in the repository.",
                "root_cause_file": None,
                "root_cause_lines": None,
                "root_cause_evidence": None,
                "suggested_fix": None,
                "raw_response": "No matching symbols found in repository index.",
            }
            evidence["investigation_report"] = report_dict
            evidence["outcome"] = "success"

            print("\n======================= INVESTIGATION REPORT =======================")
            print("OUTCOME: INSUFFICIENT_EVIDENCE\n")
            print(f"REASON: {report_dict['summary']}")
            print("====================================================================")
            return 0

        # Step 4: Traverse cross-file dependencies and build bounded context
        context = assemble_investigation_context(index, search_result.candidates)
        evidence["files_inspected"] = context.files_included
        evidence["cross_file_hops"] = [
            {
                "caller": h.caller_symbol,
                "caller_file": h.caller_file,
                "callee": h.callee_symbol,
                "callee_file": h.callee_file,
            }
            for h in context.cross_file_hops
        ]
        hops_cnt = len(context.cross_file_hops)
        print(f"[AI Tracing] Cross-file hops traversed: {hops_cnt}. Assembled {len(context.symbols_included)} symbol(s) across {len(context.files_included)} file(s) ({context.total_characters:,} chars).")

        # Step 5: Run LLM Investigation
        llm = GroqReviewClient()
        evidence["model"] = llm.model
        print(f"[AI Tracing] Sending bounded multi-file context to {llm.model}...")

        report = investigate_code(llm, args.request, context, business_rules=rules_text)
        evidence["investigation_report"] = asdict(report)
        evidence["outcome"] = "success"

        # Step 6: Print clean terminal report
        print("\n======================= INVESTIGATION REPORT =======================")
        print(f"OUTCOME: {report.outcome}\n")

        if report.outcome == "SUFFICIENT_EVIDENCE":
            if report.analysis:
                print(f"ANALYSIS:\n{report.analysis}\n")
            if report.summary:
                print(f"SUMMARY:\n{report.summary}\n")
            if report.root_cause_file or report.root_cause_lines:
                print("ROOT CAUSE:")
                if report.root_cause_file:
                    print(f"File: {report.root_cause_file}")
                if report.root_cause_lines:
                    print(f"Lines: {report.root_cause_lines}")
                if report.root_cause_evidence:
                    print(f"Evidence: {report.root_cause_evidence}")
                print()
            if report.suggested_fix:
                print(f"SUGGESTED FIX:\n{report.suggested_fix}\n")
        else:
            print(f"REASON:\n{report.summary}\n")

        print("====================================================================")

    except Exception as exc:
        evidence["outcome"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        print(f"Investigation failed: {type(exc).__name__}: {exc}")
        return 1

    finally:
        evidence["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        if llm is not None:
            evidence["call_metadata"] = llm.calls
            evidence["usage_totals"] = calculate_usage(llm.calls)

        try:
            output_path = save_evidence(evidence, args.output_dir)
            print(f"JSON saved to: {output_path.resolve()}")
        except Exception:
            pass

        if os.getenv("LANGFUSE_PUBLIC_KEY"):
            try:
                if report:
                    langfuse.score_current_trace(
                        name="sufficient_evidence",
                        value=1.0 if report.outcome == "SUFFICIENT_EVIDENCE" else 0.0,
                        comment=f"Outcome: {report.outcome}",
                    )
                trace_url = langfuse.get_trace_url()
                if trace_url:
                    print(f"Langfuse Trace: {trace_url}")
                langfuse.flush()
            except Exception:
                pass

    return 0


# This main function is controller that connects all the individual functions
@observe(name="code_review_run")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review Python source code or investigate a multi-file repository."
    )
    parser.add_argument(
        "--file",
        help="Path to a single Python source file to review.",
    )
    parser.add_argument(
        "--repo",
        help="Path to a Python repository directory to investigate.",
    )
    parser.add_argument(
        "--request",
        required=True,
        help="Describe what you want reviewed or investigated.",
    )
    parser.add_argument(
        "--business-rules",
        help="Optional path to business rules (for single-file review).",
    )
    parser.add_argument(
        "--output-dir",
        default="reviews",
        help="Evidence folder (default: reviews).",
    )

    args = parser.parse_args(argv)

    if not args.request.strip():
        parser.error("--request cannot be blank.")

    if not args.file and not args.repo:
        parser.error("Either --file or --repo must be provided.")

    if args.file and args.repo:
        parser.error("Specify either --file or --repo, not both.")

    # Route to multi-file repository investigation mode
    if args.repo:
        return run_repo_investigation(args)

    # Otherwise execute single-file code review mode
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
