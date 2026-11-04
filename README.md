# AI-Powered Code Review & Repository Investigation Tool

A production-grade Python CLI tool that uses Groq-hosted Large Language Models (LLMs) and Python AST analysis to investigate bugs across multi-file codebases and perform single-file code reviews. It cross-examines implementation code against business specifications with end-to-end Langfuse observability.

---

## Key Features

### 1. Multi-File Repository Investigation (`--repo`)
* **AST-Based Symbol Indexing:** Parses all project `.py` files into an in-memory symbol graph (classes, functions, line bounds, and call hierarchies) without executing any code.
* **Smart Hybrid Search Engine:**
  * **Alphanumeric Code Preservation:** Keeps codes like `SAVE10` intact while extracting prefixes like `save`.

### 2. Business Rules Auto-Detection
* Automatically detects and loads rules files (`BUSINESS_RULES.md`, `rules.md`, `SPECIFICATION.md`, `REQUIREMENTS.md`) from the repository root **or subdirectories** (such as `review-input/` or `docs/`).
* You can also specify any custom-named rules file using `--business-rules <path>`.
* The AI compares source code logic directly against these rules to catch domain bugs (e.g., taxes applied before vs. after discounts, or un-capped promotions).

### 3. Step-by-Step Chain-of-Thought Reasoning
* The investigation prompt requires the model to produce an `ANALYSIS:` execution trace before writing `ROOT_CAUSE` and `SUGGESTED_FIX`, eliminating hasty guesses on subtle bugs.

### 4. Single-File Deep Code Review (`--file`)
* Batches functions with character limits (`MAX_BATCH_CHARS = 8500`) to prevent context window overflow.
* Extracts structured defect records: bug description, function, line location, evidence, impact, uncertainty, and next verification checks.
* Deduplicates findings and verifies source file immutability with pre- and post-review SHA-256 checksums.

### 5. Observability & Telemetry (Langfuse & Local JSON)
* Traces AST indexing, token counts, model latency, search tokens, and evidence outcomes.
* Scores investigation outcomes in Langfuse (`sufficient_evidence` = 1.0 vs. 0.0).
* Fallback proxy ensures the CLI runs smoothly even if Langfuse credentials or network access are unavailable.
* Saves full timestamped run metadata to `reviews/review_<uuid>.json`.

---

## Project Structure

```text
code-review-tool/
├── review_tool/
│   ├── main.py                     # CLI entry point, routing, and batch review controller
│   ├── llm_client.py               # Groq LLM client with OpenTelemetry / Langfuse instrumentation
│   ├── source_reader.py            # Safe source code and rules reader
│   └── repository/                 # Multi-file repository investigation subsystem
│       ├── __init__.py             # Package exports
│       ├── scanner.py              # Recursive file traversal, filtering .git, .venv, caches
│       ├── indexer.py              # AST parser extracting classes, functions, calls, and literals
│       ├── search.py               # Tokenizer, stemmer, domain synonyms, and fuzzy relevance scoring
│       ├── cross_file.py           # Cross-file call-graph tracer and context dossier assembler
│       └── investigator.py         # AI prompt builder, chain-of-thought analyzer, and output parser
├── tests/
│   ├── test_main.py                # Single-file review unit tests
│   └── test_repository_investigation.py # 19 unit & integration tests covering the repository subsystem
├── review-input/                   # Sample e-commerce codebase with known edge-case bugs
│   ├── order_service.py            # Multi-service ordering, inventory, and pricing system
│   └── BUSINESS_RULES.md           # Business specification rules for validation
├── reviews/                        # Output folder where JSON run evidence is saved
├── requirements.txt                # Python package dependencies
└── README.md                       # Documentation
```

---

## Prerequisites

* **Python:** 3.10 or newer (tested on Python 3.11 and 3.13).
* **Groq API Key:** Free API key from [console.groq.com](https://console.groq.com/).
* **Langfuse Credentials (Optional):** Free account from [cloud.langfuse.com](https://cloud.langfuse.com/) for tracing.

---

## Setup & Installation

### 1. Clone or Open the Project
Open terminal and navigate to the project directory:
```bash
cd path/to/code-review-tool
```

### 2. Create and Activate a Virtual Environment
**On Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
*(If PowerShell blocks script execution, run: `Set-ExecutionPolicy -Scope Process RemoteSigned` first).*

**On Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Create a `.env` file in the project root:
```dotenv
# Required: Groq LLM configuration
GROQ_API_KEY=gsk_your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b

# Optional: Langfuse Cloud Observability
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

---

## Usage Guide

### Mode 1: Multi-File Repository Investigation (`--repo`)

Investigate bugs or questions across an entire project folder:

#### Example A: Check discount capping across files
```bash
python -m review_tool.main --repo review-input --request "Customer used SAVE10 on Rs 10,000 cart, got Rs 1,000 off, why wasn't it capped?"
```

#### Example B: Check tax / GST compliance against business rules
```bash
python -m review_tool.main --repo review-input --request "Does the way GST is calculated on an order follow our business rules?"
```

#### Example C: Investigate a custom external repository
```bash
python -m review_tool.main --repo "C:\Projects\my-app" --request "Why does order placement crash when the inventory is empty?"
```

#### Example D: Explicitly provide a custom rules file
```bash
python -m review_tool.main --repo "C:\Projects\my-app" --business-rules "docs/spec.txt" --request "Check refund calculation limits"
```

---

### Mode 2: Single-File Code Review (`--file`)

Perform an exhaustive, function-by-function code review on a single Python file:

```bash
python -m review_tool.main --file review-input/order_service.py --business-rules review-input/BUSINESS_RULES.md --request "Review checkout and discount calculation"
```

---

## Command-Line Arguments Reference

| Argument | Type | Description |
| :--- | :--- | :--- |
| `--repo` | Optional | Path to a repository folder to investigate (mutually exclusive with `--file`). |
| `--file` | Optional | Path to a single Python file to review (mutually exclusive with `--repo`). |
| `--request` | **Required** | The plain-English question, defect inquiry, or review goal. |
| `--business-rules` | Optional | Path to a custom rules or specification file (`.md`, `.txt`). If omitted in `--repo` mode, standard rules files are auto-detected. |
| `--output-dir` | Optional | Folder where JSON run evidence is saved (default: `reviews`). |

---

## Understanding the Investigation Report

When running in `--repo` mode, the tool outputs a structured diagnosis:

### 1. When evidence is confirmed:
```text
======================= INVESTIGATION REPORT =======================
OUTCOME: SUFFICIENT_EVIDENCE

ANALYSIS:
Rule 10 states standard shipping is free when merchandise AFTER discount
is at least 10,000 cents. In order_service.py:525, shipping_cost is called
with `subtotal` instead of `subtotal - discount`. When an order has a subtotal
of 12,000 cents and a 3,000 cent discount, the net merchandise is 9,000 cents,
so shipping should have been charged 500 cents, but was made free.

SUMMARY:
Standard shipping threshold is calculated against gross subtotal instead of
merchandise after discount, violating Business Rule 10.

ROOT CAUSE:
File: order_service.py
Lines: 524-526
Evidence: `shipping = self.shipping_cost(request.shipping, subtotal, discount)`

SUGGESTED FIX:
File: order_service.py
Lines: 525-526
Fix:
```python
net_subtotal = max(subtotal - discount, 0)
shipping = self.shipping_cost(request.shipping, net_subtotal, discount)
```
====================================================================
```

### 2. When evidence is missing or unsupported:
```text
======================= INVESTIGATION REPORT =======================
OUTCOME: INSUFFICIENT_EVIDENCE

REASON:
The extracted snippets from order_service.py call external payment gateway
methods, but payment_gateway.py was not found or provided. Without inspecting
the gateway's decline-handling logic, the root cause cannot be verified.
====================================================================
```

---

## Running the Automated Test Suite

The project includes an automated test suite verifying file scanning, AST indexing, cross-file call resolution, fuzzy search, prompt generation, and business-rules auto-detection:

```bash
# Set PYTHONPATH and execute pytest
$env:PYTHONPATH="code-review-tool"; python -m pytest code-review-tool/tests
```

**Expected output:**
```text
============================= 19 passed in 6.20s ==============================
```

---

## Safety & Security Principles

* **Strictly Read-Only:** The tool analyzes source files as text; it **never executes, imports, or modifies** code in the target repository.
* **Bounded Context:** Rather than sending whole repositories to the LLM (which introduces token limits and prompt distraction), it extracts only relevant functions and their direct call partners.
* **Secret Protection:** Automatic scanner filters ignore `.env`, `.git`, `.venv`, and binary files.
* **Deterministic Analysis:** LLM temperature is pinned to `0.0` for repeatable, analytical evaluations.
