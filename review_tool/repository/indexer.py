#This file indexes all files and extracts function,class and method calls 

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from review_tool.repository.scanner import RepositoryFile


@dataclass
class IndexedSymbol:
    name: str  # e.g., "OrderService.checkout" or "parse_time"
    short_name: str  # e.g., "checkout"
    kind: str  # "function", "async_function", "class"
    file_path: str  # absolute path
    relative_path: str  # relative to repo root
    start_line: int
    end_line: int
    docstring: str = ""
    args: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)  # Function/method names called inside
    imported_names: list[str] = field(default_factory=list)
    constants: list[str] = field(default_factory=list)  # String literals, keys, and values


@dataclass
class IndexedRepository:
    repository_path: str
    files: list[RepositoryFile]
    symbols: list[IndexedSymbol]
    symbols_by_name: dict[str, list[IndexedSymbol]] = field(default_factory=dict)
    symbols_by_file: dict[str, list[IndexedSymbol]] = field(default_factory=dict)
    parse_errors: list[str] = field(default_factory=list)

    @property
    def total_symbols(self) -> int:
        return len(self.symbols)

    @property
    def total_files_indexed(self) -> int:
        return len(self.symbols_by_file)


def _extract_calls(node: ast.AST) -> list[str]:
    """Find all function or method names invoked inside an AST node."""
    calls: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            if isinstance(item.func, ast.Name):
                calls.add(item.func.id)
            elif isinstance(item.func, ast.Attribute):
                calls.add(item.func.attr)
    return sorted(calls)


def _extract_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Extract positional and keyword argument names from function signature."""
    args = [arg.arg for arg in node.args.args]
    if node.args.vararg:
        args.append(node.args.vararg.arg)
    if node.args.kwarg:
        args.append(node.args.kwarg.arg)
    return args


def _extract_literals(node: ast.AST) -> list[str]:
    """Extract string literals, dictionary keys, and attribute names from AST."""
    literals: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            val = item.value.strip()
            if 2 <= len(val) <= 100:
                literals.add(val)
        elif isinstance(item, ast.Attribute) and isinstance(item.attr, str):
            if len(item.attr) >= 2:
                literals.add(item.attr)
        elif isinstance(item, ast.Subscript):
            if isinstance(item.slice, ast.Constant) and isinstance(item.slice.value, str):
                if len(item.slice.value) >= 2:
                    literals.add(item.slice.value)
    return sorted(literals)


def index_file(repo_file: RepositoryFile) -> tuple[list[IndexedSymbol], list[str]]:
    """Parse a single Python file into indexed symbols without executing code."""
    path = Path(repo_file.path)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"{repo_file.relative_path}: Could not read file: {exc}"]

    try:
        tree = ast.parse(source, filename=repo_file.relative_path)
    except SyntaxError as exc:
        return [], [f"{repo_file.relative_path}:{exc.lineno}: SyntaxError: {exc.msg}"]

    symbols: list[IndexedSymbol] = []
    file_imports: list[str] = []

    # Extract top-level imports and module constants
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                file_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                file_imports.append(node.module)
            for alias in node.names:
                file_imports.append(alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    const_lits = _extract_literals(node)
                    symbols.append(
                        IndexedSymbol(
                            name=target.id,
                            short_name=target.id,
                            kind="constant",
                            file_path=repo_file.path,
                            relative_path=repo_file.relative_path,
                            start_line=node.lineno,
                            end_line=getattr(node, "end_lineno", node.lineno),
                            docstring="",
                            constants=const_lits,
                        )
                    )

    def visit(node: ast.AST, parents: tuple[str, ...] = ()) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                class_name = child.name
                qualified_name = ".".join(parents + (class_name,))
                doc = ast.get_docstring(child) or ""
                symbols.append(
                    IndexedSymbol(
                        name=qualified_name,
                        short_name=class_name,
                        kind="class",
                        file_path=repo_file.path,
                        relative_path=repo_file.relative_path,
                        start_line=child.lineno,
                        end_line=getattr(child, "end_lineno", child.lineno),
                        docstring=doc,
                        calls=_extract_calls(child),
                        imported_names=file_imports.copy(),
                        constants=_extract_literals(child),
                    )
                )
                visit(child, parents + (class_name,))

            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = child.name
                qualified_name = ".".join(parents + (func_name,))
                doc = ast.get_docstring(child) or ""
                start_line = (
                    child.decorator_list[0].lineno
                    if child.decorator_list
                    else child.lineno
                )
                symbols.append(
                    IndexedSymbol(
                        name=qualified_name,
                        short_name=func_name,
                        kind="async_function" if isinstance(child, ast.AsyncFunctionDef) else "function",
                        file_path=repo_file.path,
                        relative_path=repo_file.relative_path,
                        start_line=start_line,
                        end_line=getattr(child, "end_lineno", child.lineno),
                        docstring=doc,
                        args=_extract_args(child),
                        calls=_extract_calls(child),
                        imported_names=file_imports.copy(),
                        constants=_extract_literals(child),
                    )
                )
                visit(child, parents + (func_name,))

            else:
                visit(child, parents)

    visit(tree)
    return symbols, []


def index_repository(
    files: list[RepositoryFile],
    repository_path: str,
) -> IndexedRepository:
    """Index all Python files in a repository into symbols and cross-file references."""
    all_symbols: list[IndexedSymbol] = []
    symbols_by_name: dict[str, list[IndexedSymbol]] = {}
    symbols_by_file: dict[str, list[IndexedSymbol]] = {}
    all_errors: list[str] = []

    for file_info in files:
        file_symbols, errors = index_file(file_info)
        all_errors.extend(errors)

        if file_symbols:
            symbols_by_file[file_info.relative_path] = file_symbols
            for symbol in file_symbols:
                all_symbols.append(symbol)
                # Index by qualified name and short name
                symbols_by_name.setdefault(symbol.name.lower(), []).append(symbol)
                symbols_by_name.setdefault(symbol.short_name.lower(), []).append(symbol)

    return IndexedRepository(
        repository_path=repository_path,
        files=files,
        symbols=all_symbols,
        symbols_by_name=symbols_by_name,
        symbols_by_file=symbols_by_file,
        parse_errors=all_errors,
    )

