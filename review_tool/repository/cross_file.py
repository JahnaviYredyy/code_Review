# Follows call links across files and bundles the exact code snippets needed.

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from review_tool.repository.indexer import IndexedRepository, IndexedSymbol

MAX_INVESTIGATION_CHARS = 16_000  # Safe context window budget (~3,500-4,000 tokens)


@dataclass
class CrossFileHop:
    caller_symbol: str
    caller_file: str
    callee_symbol: str
    callee_file: str


@dataclass
class InvestigationContext:
    formatted_code: str
    symbols_included: list[IndexedSymbol]
    files_included: list[str]
    cross_file_hops: list[CrossFileHop]
    total_characters: int
    has_symbols: bool


def _extract_symbol_source(symbol: IndexedSymbol) -> str:
    """Extract source lines for a symbol with original line numbers."""
    try:
        lines = Path(symbol.file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return f"# Could not read {symbol.relative_path}\n"

    start = symbol.start_line
    end = min(symbol.end_line, len(lines))
    selected = lines[start - 1 : end]

    numbered = "\n".join(
        f"{line_no}: {line}" for line_no, line in enumerate(selected, start=start)
    )

    return (
        f"FILE: {symbol.relative_path}\n"
        f"SYMBOL: {symbol.name} ({symbol.kind}, lines {start}-{end})\n"
        f"CODE:\n{numbered}\n"
    )


def assemble_investigation_context(
    index: IndexedRepository,
    seed_candidates: list[tuple[IndexedSymbol, float]],
    max_chars: int = MAX_INVESTIGATION_CHARS,
) -> InvestigationContext:
    """Traverse cross-file call-graph and assemble bounded source code context."""
    if not seed_candidates:
        return InvestigationContext(
            formatted_code="",
            symbols_included=[],
            files_included=[],
            cross_file_hops=[],
            total_characters=0,
            has_symbols=False,
        )

    # 1. Collect candidate symbols and resolve their cross-file call targets
    symbols_to_include: list[IndexedSymbol] = []
    seen_symbols = set()
    cross_file_hops: list[CrossFileHop] = []

    # First add the highest scoring seeds
    for sym, _ in seed_candidates:
        sig = (sym.relative_path, sym.name)
        if sig not in seen_symbols:
            seen_symbols.add(sig)
            symbols_to_include.append(sym)

    # Second, expand 1-hop cross-file calls
    for seed in list(symbols_to_include):
        for call_name in seed.calls:
            call_lower = call_name.lower()
            matching_targets = index.symbols_by_name.get(call_lower, [])

            for target in matching_targets:
                target_sig = (target.relative_path, target.name)
                # Check if this is a cross-file reference or important helper
                if target.relative_path != seed.relative_path:
                    cross_file_hops.append(
                        CrossFileHop(
                            caller_symbol=seed.name,
                            caller_file=seed.relative_path,
                            callee_symbol=target.name,
                            callee_file=target.relative_path,
                        )
                    )

                if target_sig not in seen_symbols:
                    seen_symbols.add(target_sig)
                    symbols_to_include.append(target)

    # 2. Extract numbered source code, staying strictly within the character budget
    context_blocks: list[str] = []
    included_symbols: list[IndexedSymbol] = []
    files_included_set: set[str] = set()
    current_chars = 0

    for symbol in symbols_to_include:
        block = _extract_symbol_source(symbol)
        block_len = len(block)

        if current_chars + block_len > max_chars and context_blocks:
            # Stop if adding more code exceeds the strict token budget
            break

        context_blocks.append(block)
        included_symbols.append(symbol)
        files_included_set.add(symbol.relative_path)
        current_chars += block_len

    formatted_code = "\n" + ("=" * 60) + "\n\n"
    formatted_code += "\n\n".join(context_blocks)
    formatted_code += "\n" + ("=" * 60) + "\n"

    return InvestigationContext(
        formatted_code=formatted_code,
        symbols_included=included_symbols,
        files_included=sorted(files_included_set),
        cross_file_hops=cross_file_hops,
        total_characters=current_chars,
        has_symbols=len(included_symbols) > 0,
    )

