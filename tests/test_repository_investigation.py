"""Automated tests for Multi-File Python Repository Investigation."""

import json
from pathlib import Path
import pytest

import review_tool.main as review_main
from review_tool.repository.scanner import scan_repository
from review_tool.repository.indexer import index_repository
from review_tool.repository.search import search_relevant_symbols, tokenize_query
from review_tool.repository.cross_file import assemble_investigation_context
from review_tool.repository.investigator import parse_investigation_response


CART_SOURCE = """\
class Cart:
    def __init__(self, items=None):
        self.items = items

    def is_empty(self) -> bool:
        if len(self.items) == 0:
            return True
        return False
"""

ORDER_SOURCE = """\
from models.cart import Cart

class OrderService:
    def __init__(self, db):
        self.db = db

    def checkout(self, cart: Cart):
        if cart.is_empty():
            raise ValueError("Cart is empty")
        return {"status": "created"}
"""


@pytest.fixture
def sample_repo(tmp_path):
    """Creates a temporary multi-file repository."""
    repo = tmp_path / "ecommerce_repo"
    repo.mkdir()

    models_dir = repo / "models"
    models_dir.mkdir()
    (models_dir / "cart.py").write_text(CART_SOURCE, encoding="utf-8")

    services_dir = repo / "services"
    services_dir.mkdir()
    (services_dir / "order.py").write_text(ORDER_SOURCE, encoding="utf-8")

    # Create ignored directories and files
    venv_dir = repo / ".venv"
    venv_dir.mkdir()
    (venv_dir / "ignored.py").write_text("# ignored", encoding="utf-8")

    env_file = repo / ".env"
    env_file.write_text("SECRET=123", encoding="utf-8")

    return repo


def test_scanner_ignores_venv_and_non_python(sample_repo):
    """Scanner must discover only valid Python files outside ignored directories."""
    result = scan_repository(str(sample_repo))

    rel_paths = [f.relative_path for f in result.files]
    assert "models/cart.py" in rel_paths
    assert "services/order.py" in rel_paths
    assert not any(".venv" in p for p in rel_paths)
    assert not any(".env" in p for p in rel_paths)
    assert result.total_files == 2


def test_indexer_extracts_symbols_and_calls(sample_repo):
    """Indexer parses multi-file AST to extract classes, methods, and calls."""
    scan_res = scan_repository(str(sample_repo))
    index = index_repository(scan_res.files, scan_res.repository_path)

    assert index.total_files_indexed == 2
    symbol_names = [s.name for s in index.symbols]

    assert "Cart" in symbol_names
    assert "Cart.is_empty" in symbol_names
    assert "OrderService" in symbol_names
    assert "OrderService.checkout" in symbol_names

    checkout_sym = next(s for s in index.symbols if s.name == "OrderService.checkout")
    assert "is_empty" in checkout_sym.calls


def test_relevance_search_ranks_correctly(sample_repo):
    """Search matches query keywords to symbols and ranks highest relevance."""
    scan_res = scan_repository(str(sample_repo))
    index = index_repository(scan_res.files, scan_res.repository_path)

    search_res = search_relevant_symbols(
        index,
        "Why does checkout fail when the cart is empty?"
    )

    top_names = [sym.short_name for sym, _ in search_res.candidates]
    assert "checkout" in top_names or "is_empty" in top_names
    assert len(search_res.candidates) > 0


def test_cross_file_hop_resolution(sample_repo):
    """Traversing call-graph from OrderService.checkout resolves Cart.is_empty in another file."""
    scan_res = scan_repository(str(sample_repo))
    index = index_repository(scan_res.files, scan_res.repository_path)
    search_res = search_relevant_symbols(index, "checkout")

    context = assemble_investigation_context(index, search_res.candidates)

    assert context.has_symbols is True
    assert "models/cart.py" in context.files_included
    assert "services/order.py" in context.files_included

    hops = [(h.caller_symbol, h.callee_symbol) for h in context.cross_file_hops]
    assert any("checkout" in caller and "is_empty" in callee for caller, callee in hops)
    assert "FILE: models/cart.py" in context.formatted_code
    assert "FILE: services/order.py" in context.formatted_code


def test_parse_investigation_response_sufficient():
    """Parses model response with sufficient evidence, lines, and fix."""
    sample_response = """\
OUTCOME: SUFFICIENT_EVIDENCE
SUMMARY: Checkout fails because Cart.is_empty() raises TypeError if items is None.
ROOT_CAUSE:
File: models/cart.py
Lines: 6-7
Evidence: `if len(self.items) == 0:` fails when self.items is None.
SUGGESTED_FIX:
File: models/cart.py
Lines: 6-7
Fix:
```python
if not self.items:
    return True
```
"""
    report = parse_investigation_response(sample_response)
    assert report.outcome == "SUFFICIENT_EVIDENCE"
    assert "models/cart.py" in report.root_cause_file
    assert report.root_cause_lines == "6-7"
    assert "if not self.items:" in report.suggested_fix


def test_parse_investigation_response_insufficient():
    """Parses model response indicating insufficient evidence."""
    sample_response = """\
OUTCOME: INSUFFICIENT_EVIDENCE
REASON: The error occurs inside a C-extension not provided in the codebase.
"""
    report = parse_investigation_response(sample_response)
    assert report.outcome == "INSUFFICIENT_EVIDENCE"
    assert "C-extension" in report.summary


def test_main_repo_investigation_end_to_end(sample_repo, tmp_path, monkeypatch, capsys):
    """End-to-end repository investigation using simulated Groq LLM client."""
    output_dir = tmp_path / "reviews"

    class FakeInvestigatorClient:
        def __init__(self):
            self.model = "fake-investigator-model"
            self.calls = []

        def complete(self, system_prompt, user_prompt, **kwargs):
            self.calls.append({
                "prompt_tokens": 200,
                "completion_tokens": 100,
                "total_tokens": 300,
            })
            fake_output = (
                "OUTCOME: SUFFICIENT_EVIDENCE\n"
                "SUMMARY: Calling len() on None raises TypeError.\n"
                "ROOT_CAUSE:\n"
                "File: models/cart.py\n"
                "Lines: 6-7\n"
                "Evidence: len(self.items) crashes if items is None\n"
                "SUGGESTED_FIX:\n"
                "File: models/cart.py\n"
                "Lines: 6-7\n"
                "Fix:\n"
                "```python\n"
                "if not self.items:\n"
                "    return True\n"
                "```\n"
            )
            return fake_output, {"total_tokens": 300}

    monkeypatch.setattr(review_main, "GroqReviewClient", FakeInvestigatorClient)

    exit_code = review_main.main([
        "--repo", str(sample_repo),
        "--request", "Why does checkout fail when the cart is empty?",
        "--output-dir", str(output_dir),
    ])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "INVESTIGATION REPORT" in captured.out
    assert "OUTCOME: SUFFICIENT_EVIDENCE" in captured.out
    assert "ROOT CAUSE:" in captured.out
    assert "models/cart.py" in captured.out

    # Verify JSON evidence file was created with full metadata
    evidence_files = list(output_dir.glob("review_*.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))

    assert evidence["mode"] == "repository_investigation"
    assert evidence["outcome"] == "success"
    assert evidence["scanned_files"] == 2
    assert "models/cart.py" in evidence["files_inspected"]
    assert "services/order.py" in evidence["files_inspected"]
    assert evidence["investigation_report"]["outcome"] == "SUFFICIENT_EVIDENCE"


def test_main_repo_insufficient_evidence_when_no_match(sample_repo, tmp_path, capsys):
    """When query terms have no matching symbols, reports INSUFFICIENT_EVIDENCE immediately."""
    output_dir = tmp_path / "reviews"

    exit_code = review_main.main([
        "--repo", str(sample_repo),
        "--request", "quantum_encryption_handshake_failure",
        "--output-dir", str(output_dir),
    ])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "OUTCOME: INSUFFICIENT_EVIDENCE" in captured.out
    assert "No code symbols or keywords matching" in captured.out


def test_tokenize_alphanumeric_and_synonyms():
    """Tokenizer preserves full alphanumeric codes, eliminates pure numbers, and expands domain synonyms."""
    query = "Customer used SAVE10 on Rs 10,000 cart, got Rs 1,000 off, why wasn't it capped?"
    tokens = tokenize_query(query)

    # 1. Full code preserved
    assert "save10" in tokens
    assert "save" in tokens

    # 2. Pure numbers and currency abbreviations filtered out
    assert "10" not in tokens
    assert "000" not in tokens
    assert "rs" not in tokens

    # 3. Synonym and stem expansion bridged natural language to code terms
    assert "discount" in tokens
    assert "cap" in tokens
    assert "max" in tokens


def test_fuzzy_typo_matching(sample_repo):
    """Typo in symbol name (e.g. chechout) still matches OrderService.checkout via fuzzy matching."""
    scan_res = scan_repository(str(sample_repo))
    index = index_repository(scan_res.files, scan_res.repository_path)

    # User misspelled "checkout" as "chechout"
    res = search_relevant_symbols(index, "Why did chechout fail?")
    candidate_names = [sym.short_name for sym, _ in res.candidates]
    assert "checkout" in candidate_names


def test_expand_query_with_ai_mocked():
    """AI keyword expansion extracts code search terms from metaphors."""
    from review_tool.repository.search import expand_query_with_ai

    class FakeLLM:
        def complete(self, sys_prompt, user_prompt, **kwargs):
            return "discount, price, calculate_discount, promo", {}

    expanded = expand_query_with_ai(FakeLLM(), "Why was the price slashed?")
    assert "discount" in expanded
    assert "price" in expanded
    assert "calculate_discount" in expanded



