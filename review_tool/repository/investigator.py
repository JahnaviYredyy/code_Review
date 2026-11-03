#Sends those snippets to the AI to verify evidence and produce the diagnosis

import re
from dataclasses import dataclass
from typing import Any

from langfuse import get_client, observe
from review_tool.llm_client import GroqReviewClient
from review_tool.repository.cross_file import InvestigationContext

langfuse = get_client()

INVESTIGATOR_SYSTEM_PROMPT = """\
You are an expert Python software investigator.
Your job is to investigate a user's question or bug report across a multi-file Python repository.

CRITICAL RULES:
1. Base your explanation strictly on the provided source code blocks. Do not invent files, functions, or lines.
2. Read the file paths and line numbers carefully from the supplied code headers.
3. If the provided code contains sufficient evidence to explain the issue:
   - State the concise summary of the issue.
   - Point to the exact root cause with File path and Line numbers.
   - Propose a concrete Suggested Fix with code.
4. If the provided code DOES NOT contain sufficient evidence to conclusively explain the issue:
   - You MUST output OUTCOME: INSUFFICIENT_EVIDENCE.
   - Explain what is missing and what additional files, dependencies, or runtime data would be needed.

OUTPUT FORMAT:

If evidence is sufficient:
OUTCOME: SUFFICIENT_EVIDENCE
SUMMARY: <clear explanation answering the user question>
ROOT_CAUSE:
File: <exact relative file path>
Lines: <line numbers, e.g. 48-52>
Evidence: <source expression or statement causing the behavior>
SUGGESTED_FIX:
File: <exact relative file path>
Lines: <lines to replace>
Fix:
```python
<fixed code snippet>
```

If evidence is NOT sufficient:
OUTCOME: INSUFFICIENT_EVIDENCE
REASON: <what cannot be verified from the retrieved code and what additional files are needed>
"""


@dataclass
class InvestigationReport:
    outcome: str  # "SUFFICIENT_EVIDENCE" or "INSUFFICIENT_EVIDENCE"
    summary: str
    root_cause_file: str | None = None
    root_cause_lines: str | None = None
    root_cause_evidence: str | None = None
    suggested_fix: str | None = None
    raw_response: str = ""


def parse_investigation_response(response_text: str) -> InvestigationReport:
    """Parse structured output from the LLM investigation response."""
    text = response_text.strip()
    if not text:
        return InvestigationReport(
            outcome="INSUFFICIENT_EVIDENCE",
            summary="The model returned an empty response.",
            raw_response="",
        )

    if "OUTCOME: INSUFFICIENT_EVIDENCE" in text or "INSUFFICIENT_EVIDENCE" in text.splitlines()[0]:
        reason_match = re.search(r"REASON:\s*(.*)", text, re.DOTALL | re.IGNORECASE)
        reason = reason_match.group(1).strip() if reason_match else text
        return InvestigationReport(
            outcome="INSUFFICIENT_EVIDENCE",
            summary=reason,
            raw_response=text,
        )

    # Extract fields for sufficient evidence
    summary_match = re.search(r"SUMMARY:\s*(.*?)(?=ROOT_CAUSE:|$)", text, re.DOTALL | re.IGNORECASE)
    summary = summary_match.group(1).strip() if summary_match else ""

    file_match = re.search(r"File:\s*([^\r\n]+)", text)
    root_cause_file = file_match.group(1).strip() if file_match else None

    lines_match = re.search(r"Lines:\s*([^\r\n]+)", text)
    root_cause_lines = lines_match.group(1).strip() if lines_match else None

    evidence_match = re.search(r"Evidence:\s*(.*?)(?=SUGGESTED_FIX:|$)", text, re.DOTALL | re.IGNORECASE)
    root_cause_evidence = evidence_match.group(1).strip() if evidence_match else None

    fix_match = re.search(r"SUGGESTED_FIX:\s*(.*)", text, re.DOTALL | re.IGNORECASE)
    suggested_fix = fix_match.group(1).strip() if fix_match else None

    if not summary and not root_cause_file:
        # Fallback if model didn't follow exact headers but provided an explanation
        return InvestigationReport(
            outcome="SUFFICIENT_EVIDENCE",
            summary=text,
            raw_response=text,
        )

    return InvestigationReport(
        outcome="SUFFICIENT_EVIDENCE",
        summary=summary,
        root_cause_file=root_cause_file,
        root_cause_lines=root_cause_lines,
        root_cause_evidence=root_cause_evidence,
        suggested_fix=suggested_fix,
        raw_response=text,
    )


@observe(name="investigate_repository")
def investigate_code(
    llm: GroqReviewClient,
    request: str,
    context: InvestigationContext,
) -> InvestigationReport:
    """Send targeted multi-file context to the LLM and return structured investigation report."""
    user_prompt = f"""\
USER QUESTION / REQUEST:
{request}

RELEVANT SOURCE CODE EXTRACTED FROM REPOSITORY:
{context.formatted_code}

Investigate the question using the extracted code above.
Cite exact file paths and line numbers.
If the cause is not supported by this code, state OUTCOME: INSUFFICIENT_EVIDENCE.
"""

    response, _ = llm.complete(INVESTIGATOR_SYSTEM_PROMPT, user_prompt)
    report = parse_investigation_response(response)
    return report

