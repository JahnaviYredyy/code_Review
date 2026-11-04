#Matches question with mos relevant functions

import difflib
import re
from dataclasses import dataclass
from typing import Any

from review_tool.repository.indexer import IndexedRepository, IndexedSymbol


STOP_WORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "by", "can", "could",
    "did", "didn", "didnt", "do", "doe", "does", "explain", "follow", "for", "from", "got", "how",
    "i", "if", "in", "into", "is", "isn", "isnt", "it", "its", "me", "my",
    "no", "not", "of", "on", "or", "our", "please", "should", "so", "such",
    "tell", "that", "the", "their", "then", "there", "these", "they", "this",
    "to", "used", "was", "wasn", "wasnt", "way", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "rs", "inr", "usd", "eur",
}

DOMAIN_SYNONYMS: dict[str, list[str]] = {
    "off": ["discount"],
    "save": ["discount", "coupon"],
    "savings": ["discount"],
    "saving": ["discount"],
    "capped": ["cap", "max", "limit", "max_discount"],
    "capping": ["cap", "max", "limit"],
    "cap": ["max", "limit", "max_discount", "maximum"],
    "limit": ["max", "cap"],
    "ceiling": ["max", "limit", "cap"],
    "coupon": ["discount", "code"],
    "coupons": ["coupon", "discount"],
    "promo": ["coupon", "discount"],
    "promocode": ["coupon", "discount"],
    "voucher": ["coupon", "discount"],
    "ship": ["shipping", "delivery"],
    "shipping": ["ship", "delivery"],
    "cost": ["price", "cents", "amount"],
    "tax": ["tax_cents", "taxable", "gst", "vat"],
    "gst": ["tax", "tax_cents", "taxable"],
    "vat": ["tax", "tax_cents", "taxable"],
    "buyer": ["customer"],
    "purchased": ["order", "checkout"],
}


@dataclass
class SearchResult:
    query_tokens: list[str]
    candidates: list[tuple[IndexedSymbol, float]]  # (symbol, score)
    total_symbols_considered: int


def _stem_word(word: str) -> str | None:
    """Derive simple base stem for regular English word endings."""
    if word in ("does", "this", "these", "status", "focus", "basis", "gross"):
        return None
    if word.endswith("ed") and len(word) > 4:
        if word.endswith("pped") or word.endswith("bbed") or word.endswith("gged") or word.endswith("tted"):
            return word[:-3]  # e.g., capped -> cap
        return word[:-2]  # e.g., created -> creat
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]  # e.g., shipping -> ship
    if word.endswith("s") and len(word) > 3 and not word.endswith("ss"):
        return word[:-1]  # e.g., coupons -> coupon, discounts -> discount
    return None


def tokenize_query(query: str) -> list[str]:
    """Extract meaningful lowercase identifier, stem, and synonym tokens from query."""
    raw_tokens = re.findall(r"[A-Za-z0-9_]+", query)
    tokens: list[str] = []

    for raw in raw_tokens:
        raw_lower = raw.lower()

        # Filter out pure digits (e.g. 10, 000, 1000)
        if raw_lower.isdigit():
            continue

        # Keep full alphanumeric token (e.g. SAVE10 -> save10)
        if len(raw_lower) >= 2 and raw_lower not in STOP_WORDS:
            tokens.append(raw_lower)

            # Extract prefix for alphanumeric codes like save10 -> save
            code_prefix = re.match(r"^([a-z]+)[0-9]+$", raw_lower)
            if code_prefix:
                prefix = code_prefix.group(1)
                if len(prefix) >= 2 and prefix not in STOP_WORDS:
                    tokens.append(prefix)

        # Split snake_case if present
        if "_" in raw_lower:
            for part in raw_lower.split("_"):
                if len(part) >= 2 and not part.isdigit() and part not in STOP_WORDS:
                    tokens.append(part)

        # Split camelCase if mixed uppercase and lowercase
        elif any(c.isupper() for c in raw) and any(c.islower() for c in raw):
            parts = re.findall(r"[A-Z][a-z0-9]*|[a-z0-9]+", raw)
            for part in parts:
                p_lower = part.lower()
                if len(p_lower) >= 2 and not p_lower.isdigit() and p_lower not in STOP_WORDS:
                    tokens.append(p_lower)

        # Derive stem
        stem = _stem_word(raw_lower)
        if stem and len(stem) >= 2 and stem not in STOP_WORDS:
            tokens.append(stem)

        # Expand domain synonyms
        for key in (raw_lower, stem):
            if key and key in DOMAIN_SYNONYMS:
                tokens.extend(DOMAIN_SYNONYMS[key])

    # Return deduplicated while preserving order
    seen = set()
    unique = []
    for t in tokens:
        if t not in seen and t not in STOP_WORDS and not t.isdigit():
            seen.add(t)
            unique.append(t)
    return unique


def score_symbol(symbol: IndexedSymbol, query_tokens: list[str]) -> float:
    """Calculate relevance score between a symbol and search query tokens."""
    if not query_tokens:
        return 0.0

    score = 0.0
    symbol_name_lower = symbol.name.lower()
    short_name_lower = symbol.short_name.lower()
    doc_lower = symbol.docstring.lower()
    rel_path_lower = symbol.relative_path.lower()
    args_lower = [a.lower() for a in symbol.args]
    calls_lower = [c.lower() for c in symbol.calls]
    constants_lower = [c.lower() for c in symbol.constants]

    for token in query_tokens:
        # Exact short name match is the strongest signal
        if token == short_name_lower:
            score += 20.0
        elif token in short_name_lower:
            score += 10.0
        elif token in symbol_name_lower:
            score += 6.0
        elif len(token) >= 5 and difflib.SequenceMatcher(None, token, short_name_lower).ratio() >= 0.82:
            # Typo tolerance (e.g. "disscount" matches "discount", "custmer" matches "customer")
            score += 8.0

        # Argument names matching query
        if any(token == arg or token in arg for arg in args_lower):
            score += 6.0

        # String literals, keys, and constants inside body
        if any(token == const or token in const for const in constants_lower):
            score += 8.0

        # File path match (e.g. "order" in "order_service.py")
        if token in rel_path_lower:
            score += 4.0

        # Outbound calls match (e.g. symbol invokes "coupon_discount")
        if any(token in call for call in calls_lower):
            score += 4.0

        # Docstring match
        if token in doc_lower:
            score += 2.0

    return score


def search_relevant_symbols(
    index: IndexedRepository,
    request: str,
    top_k: int = 6,
    extra_tokens: list[str] | None = None,
) -> SearchResult:
    """Find top candidate symbols relevant to the user request."""
    query_tokens = tokenize_query(request)
    if extra_tokens:
        seen = set(query_tokens)
        for t in extra_tokens:
            t_lower = t.lower()
            if t_lower not in seen and t_lower not in STOP_WORDS and not t_lower.isdigit():
                seen.add(t_lower)
                query_tokens.append(t_lower)

    if not query_tokens:
        return SearchResult(query_tokens=[], candidates=[], total_symbols_considered=len(index.symbols))

    scored_candidates: list[tuple[IndexedSymbol, float]] = []

    for symbol in index.symbols:
        s = score_symbol(symbol, query_tokens)
        if s > 0:
            scored_candidates.append((symbol, s))

    # Sort descending by score, then ascending by relative_path and start_line
    scored_candidates.sort(
        key=lambda item: (-item[1], item[0].relative_path, item[0].start_line)
    )

    return SearchResult(
        query_tokens=query_tokens,
        candidates=scored_candidates[:top_k],
        total_symbols_considered=len(index.symbols),
    )


def expand_query_with_ai(llm: Any, query: str) -> list[str]:
    """Translate abstract conversational phrasing, slang, or metaphors into Python code keywords."""
    system_prompt = (
        "You are a code search assistant. Convert the user's natural language question, "
        "slang, or error description into 4 to 6 lowercase Python identifier keywords (functions, "
        "fields, concepts) likely to exist in source code. Output ONLY a comma-separated list of words."
    )
    user_prompt = f"Inquiry: {query}\nKeywords:"
    try:
        response, _ = llm.complete(system_prompt, user_prompt, max_tokens=40, temperature=0.0)
        words = re.findall(r"[a-z0-9_]+", response.lower())
        return [w for w in words if len(w) >= 2 and not w.isdigit() and w not in STOP_WORDS]
    except Exception:
        return []

