#!/usr/bin/env python3
"""扫描源码文件，为初学者友好的工作日志输出函数位置。"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
}
DEFAULT_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}
KIND_LABELS = {
    "async function": "异步函数",
    "function": "函数",
    "class": "类",
}
JS_FUNCTION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\b"
    r"|^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="输出代码函数位置的 Markdown 表格行。")
    parser.add_argument("paths", nargs="+", help="要扫描的文件或目录。")
    parser.add_argument(
        "--extensions",
        default=",".join(sorted(DEFAULT_EXTENSIONS)),
        help="要扫描的扩展名，用逗号分隔。",
    )
    return parser.parse_args()


def iter_files(paths: list[str], extensions: set[str]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if path.is_file() and path.suffix in extensions:
            files.append(path)
            continue
        if path.is_dir():
            for child in path.rglob("*"):
                if any(part in SKIP_DIRS for part in child.parts):
                    continue
                if child.is_file() and child.suffix in extensions:
                    files.append(child)
    return sorted(set(files))


def python_symbols(path: Path) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    symbols: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append((node.lineno, node.name, "async function" if isinstance(node, ast.AsyncFunctionDef) else "function"))
        elif isinstance(node, ast.ClassDef):
            symbols.append((node.lineno, node.name, "class"))
    return sorted(symbols)


def js_symbols(path: Path) -> list[tuple[int, str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    symbols: list[tuple[int, str, str]] = []
    for number, line in enumerate(lines, start=1):
        match = JS_FUNCTION_RE.search(line)
        if not match:
            continue
        name = next((group for group in match.groups() if group), None)
        if name and name not in {"if", "for", "while", "switch", "catch", "with"}:
            symbols.append((number, name, "function"))
    return symbols


def escape_cell(value: str) -> str:
    return value.replace("|", "\\|")


def main() -> None:
    args = parse_args()
    extensions = {ext.strip() if ext.strip().startswith(".") else f".{ext.strip()}" for ext in args.extensions.split(",") if ext.strip()}
    rows: list[str] = []
    for path in iter_files(args.paths, extensions):
        symbols = python_symbols(path) if path.suffix == ".py" else js_symbols(path)
        for line, name, kind in symbols:
            kind_label = KIND_LABELS.get(kind, kind)
            rows.append(
                "| "
                + " | ".join(
                    [
                        escape_cell(f"{path}:{line}"),
                        escape_cell(name),
                        escape_cell(f"待补充：说明这个{kind_label}的输入、处理和输出。"),
                        escape_cell("打开对应行号，检查调用处、边界输入和测试覆盖。"),
                    ]
                )
                + " |"
            )
    print("| 位置 | 函数 | 作用 | 怎么核对 |")
    print("| --- | --- | --- | --- |")
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
