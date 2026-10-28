# AI-Powered Code Review Tool

A Python command-line tool that uses a Groq-hosted large language model (LLM) to review source code for potential bugs and logic errors. It can use business rules as additional context so that the review considers both the implementation and the expected behavior.

## Features

* AI-powered code review using Groq.
* Business-rule-aware analysis.
* Python AST indexing of functions, line ranges, and function calls.
* Focused reviews using a natural-language request.
* Dependency expansion to include called functions.
* Batching of functions to manage request size.
* Structured finding extraction: bug, function, location, evidence, impact, uncertainty, and next checks.
* Exact duplicate finding removal.
* JSON evidence and metadata output.
* Source-file hash comparison before and after review.
* Token usage tracking when provider metadata is available.

> \*\*Important:\*\* The tool uses an LLM to suggest possible defects. Findings are not guaranteed to be correct or complete; verify them before making changes.

## Project Structure


code-review-tool/
├── review\_tool/
│   ├── main.py
│   ├── source\_reader.py
│   └── llm\_client.py
├── review-input/
│   ├── order\_service.py
│   └── BUSINESS\_RULES.md
├── reviews/
├──.env
├── .gitignore
├── requirements.txt
└── README.md


### Main components

* `review\_tool/main.py` — Coordinates the review workflow, parses command-line arguments, builds the function index, plans and batches the review, parses findings, removes exact duplicates, and saves output.
* `review\_tool/source\_reader.py` — Reads source code for review.
* `review\_tool/llm\_client.py` — Handles communication with the Groq API.
* `review-input/order\_service.py` — Example source file to review.
* `review-input/BUSINESS\_RULES.md` — Example business rules supplied as review context.
* `reviews/` — Intended location for saved review output; the actual output directory depends on the command-line option.


The exact contents may vary by local project version.

## Requirements

* Python 3.10 or newer is recommended.
* A Groq API key.
* Internet access to call the Groq API.
* Python packages listed in `requirements.txt`.

## Setup

### 1\. Open the project directory

Open the `code-review-tool` folder in VS Code and use the terminal to enter the project root:

```powershell
cd path\\to\\code-review-tool
```

Replace the example path with the actual path on your computer.

### 2\. Create and activate a virtual environment

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
```

If PowerShell blocks activation in the current terminal session, run:

```powershell
Set-ExecutionPolicy -Scope Process RemoteSigned
.\\.venv\\Scripts\\Activate.ps1
```

### 3\. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If `requirements.txt` does not include every package imported by your code, add the required dependencies.

### 4\. Configure the Groq API key

Create a `.env` file in the project root, using `.env.example` as a template if available:

```dotenv
GROQ\_API\_KEY=your\_groq\_api\_key\_here
```

Replace the placeholder with your own key. Keep `.env` private and ensure it is excluded by `.gitignore`. **Do not commit or share your API key.**

The review code's default model is:

```text
openai/gpt-oss-120b
```

The configured model must be available to your Groq account. If your code or environment specifies a different model, follow that configuration.

## Run a Code Review

Run commands from the project root.

### Example: review discount calculation

```powershell
python -m review\_tool.main --file review-input/order\_service.py --request "Check discount calculation" --business-rules review-input/BUSINESS\_RULES.md
```

### Example: review without business rules

```powershell
python -m review\_tool.main --file review-input/order\_service.py --request "Find potential bugs in order cancellation"
```

### Command-line arguments

|Argument|Purpose|
|-|-|
|`--file`|Path to the source file to review.|
|`--request`|Describes the review goal or behavior to investigate.|
|`--business-rules`|Optional path to the business-rules document.|
|`--output-dir`|Directory in which review output is saved.|

Check the arguments supported by your current `main.py` if you have changed the implementation.

## How It Works

1. **Read inputs:** Reads the source file and, if supplied, the business-rules document.
2. **Validate and fingerprint source:** Checks that the source can be parsed and calculates a hash to help detect changes during review.
3. **Build a function index:** Python's AST identifies functions, their line ranges, and function calls. AST organizes code; it does not independently determine whether the code contains a bug.
4. **Plan the review:** The LLM receives the request, rules, function index, and supporting evidence to help select the review scope.
5. **Expand dependencies:** May include functions called by selected functions for extra context.
6. **Create batches:** Groups function source into manageable batches.
7. **Review batches:** Sends each batch and relevant rules to the model to identify potential defects.
8. **Parse findings:** Converts model output into structured findings. Responses missing required fields may be discarded.
9. **Deduplicate findings:** Removes exact duplicates based on the configured bug, function, and location fields. Similar findings with different wording may remain.
10. **Save results:** Saves evidence and metadata to the configured output directory and reports findings in the terminal.
11. **Check source preservation:** Compares the source hash after review with the hash captured before review to check whether the source changed.

## Understanding Findings

A finding may include:

* **Bug:** Description of the suspected defect.
* **Function:** Function associated with the finding.
* **Location:** Relevant line number or code location.
* **Evidence:** Code or rule supporting the concern.
* **Impact:** Possible consequence.
* **Uncertainty:** Any uncertainty in the model's conclusion.
* **Next checks:** Suggested verification steps.

Treat findings as hypotheses to investigate, not proof that a defect exists. Confirm the location, trace the execution path, and compare behavior with the business rules.

## Output

The tool writes evidence and review metadata to the output directory specified by `--output-dir`, using the filenames and formats implemented in the current code. If no output directory is specified, the code's default is used.

Example:

```powershell
python -m review\_tool.main --file review-input/order\_service.py --request "Review coupon validation" --business-rules review-input/BUSINESS\_RULES.md --output-dir reviews
```

Inspect the terminal output and generated JSON evidence files. Exact filenames depend on the implementation.

## Troubleshooting

### `GROQ\_API\_KEY` is missing or invalid

* Check that `.env` is in the project root.
* Confirm the variable is spelled `GROQ\_API\_KEY`.
* Ensure the key is valid and has not been revoked.
* Do not paste the secret key into source code or public logs.

### Model or provider error

* Confirm the configured model is available to your account.
* Check your network connection and provider limits.
* Read the client error and retry when appropriate.

### Source file cannot be parsed

* Check the reported syntax error.
* Try a valid Python source file.

### No findings are returned

* Make the `--request` more specific.
* Check that the business rules clearly describe expected behavior.
* Check provider responses and the parser's required fields.
* An empty result does not prove the code is bug-free.

### Similar findings appear more than once

The tool removes exact duplicates, not all semantically similar findings. Compare findings by root cause and determine whether they describe separate defects or multiple consequences of one defect.





