"""Repository investigation module for multi-file Python codebases."""

from review_tool.repository.scanner import RepositoryFile, ScanResult, scan_repository
from review_tool.repository.indexer import IndexedRepository, IndexedSymbol, index_repository
from review_tool.repository.search import SearchResult, expand_query_with_ai, search_relevant_symbols
from review_tool.repository.cross_file import InvestigationContext, assemble_investigation_context
from review_tool.repository.investigator import InvestigationReport, investigate_code

__all__ = [
    "RepositoryFile",
    "ScanResult",
    "scan_repository",
    "IndexedRepository",
    "IndexedSymbol",
    "index_repository",
    "SearchResult",
    "expand_query_with_ai",
    "search_relevant_symbols",
    "InvestigationContext",
    "assemble_investigation_context",
    "InvestigationReport",
    "investigate_code",
]

