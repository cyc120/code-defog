"""Read-only, bounded code relationship graphs for monitored projects.

The graph is intentionally built from a selected project's workspace, never
from the Code Defog control-plane repository.  It is a structural aid rather
than a claim that every dynamic runtime call was resolved.  Each edge carries
an evidence level so the UI can keep parser facts separate from heuristics.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable


CODE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
SKIP_DIRECTORIES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "dist", "build", "coverage", "__pycache__", ".next", ".turbo",
}
MAX_FILES = 160
MAX_SYMBOLS = 480
MAX_FILE_BYTES = 800_000
MAX_IMPORTS_PER_FILE = 80
MAX_PREVIEW_LINES = 160
MAX_PREVIEW_CHARS = 16_000
MAX_SELECTION_LINES = 80
MAX_SELECTION_CHARS = 12_000
GRAPH_SCHEMA_VERSION = 1

_JS_IMPORT_RE = re.compile(
    r"(?:^|[;\n])\s*(?:import\s+(?:[^'\";]+?\s+from\s+)?|export\s+[^'\";]+?\s+from\s+|"
    r"(?:const|let|var)\s+[^=;]+?=\s*require\s*\(|import\s*\()\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
_JS_DECLARATION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
    r"|^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)\b"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
    re.MULTILINE,
)


class CodeGraphError(ValueError):
    """Raised when an untrusted code-map request falls outside its workspace."""


def _node_id(kind: str, value: str) -> str:
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:20]
    return f"{kind}-{digest}"


def _edge_id(source_id: str, target_id: str, relation: str, index: int) -> str:
    digest = hashlib.sha256(
        f"{source_id}|{target_id}|{relation}|{index}".encode("utf-8")
    ).hexdigest()[:20]
    return f"edge-{digest}"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise CodeGraphError("code path is outside monitored workspace") from error


def _safe_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _iter_source_files(root: Path, max_files: int) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in SKIP_DIRECTORIES)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix.lower() not in CODE_EXTENSIONS:
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
                if not resolved.is_file():
                    continue
            except (OSError, ValueError):
                continue
            files.append(resolved)
            if len(files) >= max_files:
                return files
    return files


def _language(path: Path) -> str:
    return {
        ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
        ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript",
        ".tsx": "TypeScript",
    }.get(path.suffix.lower(), path.suffix.lstrip(".").upper())


def _python_symbols(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        return [], [], f"Python 语法无法解析：第 {error.lineno or 0} 行"
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []

    def add_symbol(node: ast.AST, kind: str, name: str, parent: str | None = None) -> None:
        line = int(getattr(node, "lineno", 1) or 1)
        end_line = int(getattr(node, "end_lineno", line) or line)
        label = f"{parent}.{name}" if parent else name
        symbols.append({"name": label, "kind": kind, "line_start": line, "line_end": end_line})

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            add_symbol(node, "class", node.name)
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add_symbol(member, "method", member.name, node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_symbol(node, "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function", node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({"specifier": alias.name, "line": int(node.lineno), "level": 0})
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                suffix = "" if alias.name == "*" else (f".{alias.name}" if module else alias.name)
                imports.append({
                    "specifier": f"{'.' * int(node.level or 0)}{module}{suffix}",
                    "line": int(node.lineno), "level": int(node.level or 0),
                    "module": module,
                    "name": alias.name,
                })
    return symbols, imports, None


def _js_symbols_and_imports(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    symbols: list[dict[str, Any]] = []
    for match in _JS_DECLARATION_RE.finditer(text):
        name = next((value for value in match.groups() if value), "")
        if not name:
            continue
        line = text.count("\n", 0, match.start()) + 1
        kind = "class" if match.group(2) else "function"
        symbols.append({"name": name, "kind": kind, "line_start": line, "line_end": line})
    imports = [
        {"specifier": match.group(1), "line": text.count("\n", 0, match.start(1)) + 1}
        for match in _JS_IMPORT_RE.finditer(text)
    ]
    return symbols, imports[:MAX_IMPORTS_PER_FILE], None


def _resolve_python_import(root: Path, source: Path, item: dict[str, Any]) -> Path | None:
    specifier = str(item.get("specifier") or "")
    if not specifier:
        return None
    level = int(item.get("level") or 0)
    if level:
        base = source.parent
        for _ in range(max(level - 1, 0)):
            base = base.parent
        remainder = specifier[level:].lstrip(".")
        candidate_base = base / remainder.replace(".", "/") if remainder else base
    else:
        candidate_base = root / specifier.replace(".", "/")
    candidates = [candidate_base.with_suffix(".py"), candidate_base / "__init__.py"]
    # ``from package import symbol`` can refer to package.py even when symbol
    # itself is not a module; the parent package remains meaningful evidence.
    if item.get("name") and item.get("module"):
        module_base = (base / str(item["module"]).replace(".", "/")) if level else (root / str(item["module"]).replace(".", "/"))
        candidates.extend([module_base.with_suffix(".py"), module_base / "__init__.py"])
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
            if resolved.is_file():
                return resolved
        except (OSError, ValueError):
            continue
    return None


def _resolve_js_import(root: Path, source: Path, specifier: str) -> Path | None:
    if not specifier.startswith("."):
        return None
    base = (source.parent / specifier).resolve()
    candidates: list[Path] = [base]
    if not base.suffix:
        candidates.extend(base.with_suffix(ext) for ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))
    candidates.extend((base / f"index{ext}") for ext in (".js", ".jsx", ".ts", ".tsx"))
    for candidate in candidates:
        try:
            candidate.relative_to(root)
            if candidate.is_file():
                return candidate
        except (OSError, ValueError):
            continue
    return None


def _directory_chain(relative_path: str) -> Iterable[str]:
    parts = Path(relative_path).parts[:-1]
    for length in range(1, len(parts) + 1):
        yield Path(*parts[:length]).as_posix()


def build_code_graph(
    workspace: str | Path,
    *,
    include_symbols: bool = True,
    max_files: int = MAX_FILES,
    max_symbols: int = MAX_SYMBOLS,
) -> dict[str, Any]:
    """Build a deterministic, bounded graph without returning source text."""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise CodeGraphError("monitored workspace is not a directory")
    files = _iter_source_files(root, max(1, min(max_files, MAX_FILES)))
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    file_nodes: dict[Path, dict[str, Any]] = {}
    directory_nodes: dict[str, dict[str, Any]] = {}
    parsed: dict[Path, tuple[str, list[dict[str, Any]], list[dict[str, Any]], str | None]] = {}

    def add_edge(source_id: str, target_id: str, relation: str, evidence: str, **extra: Any) -> None:
        edge = {
            "id": _edge_id(source_id, target_id, relation, len(edges)),
            "source": source_id,
            "target": target_id,
            "relation": relation,
            "evidence": evidence,
        }
        edge.update({key: value for key, value in extra.items() if value not in (None, "")})
        edges.append(edge)

    # First pass gives imports a complete source-file lookup table.
    # Only symbols/imports/parse errors and a content hash are retained —
    # the full source text is never kept in memory (up to MAX_FILES ×
    # MAX_FILE_BYTES previously).
    for path in files:
        relative = _relative(root, path)
        text = _safe_text(path)
        language = _language(path)
        if path.suffix.lower() == ".py":
            symbols, imports, parse_error = _python_symbols(text)
        else:
            symbols, imports, parse_error = _js_symbols_and_imports(text)
        parsed[path] = (symbols, imports, parse_error)
        node = {
            "id": _node_id("file", relative),
            "type": "file",
            "label": Path(relative).name,
            "path": relative,
            "language": language,
            "line_start": 1,
            "line_end": max(1, text.count("\n") + 1) if text else 1,
            "content_hash": _content_hash(text),
            "symbol_count": len(symbols),
            "parse_status": "error" if parse_error else "ok",
        }
        nodes.append(node)
        file_nodes[path] = node
        for directory in _directory_chain(relative):
            if directory not in directory_nodes:
                directory_node = {
                    "id": _node_id("directory", directory), "type": "directory",
                    "label": Path(directory).name, "path": directory,
                    "language": "", "line_start": None, "line_end": None,
                }
                directory_nodes[directory] = directory_node
                nodes.append(directory_node)

    for directory, directory_node in directory_nodes.items():
        parent = str(Path(directory).parent)
        if parent not in ("", ".") and parent in directory_nodes:
            add_edge(directory_nodes[parent]["id"], directory_node["id"], "contains", "static")

    symbol_count = 0
    external_nodes: dict[str, dict[str, Any]] = {}
    for path in files:
        file_node = file_nodes[path]
        relative = str(file_node["path"])
        parent = str(Path(relative).parent)
        if parent not in ("", ".") and parent in directory_nodes:
            add_edge(directory_nodes[parent]["id"], file_node["id"], "contains", "static")
        symbols, imports, parse_error = parsed[path]
        if parse_error:
            error_node = {
                "id": _node_id("parse-error", relative), "type": "parse_error",
                "label": "解析待确认", "path": relative, "language": file_node["language"],
                "line_start": 1, "line_end": 1, "note": parse_error,
            }
            nodes.append(error_node)
            add_edge(file_node["id"], error_node["id"], "parse_error", "unresolved")
        if include_symbols:
            for symbol in symbols:
                if symbol_count >= max(1, min(max_symbols, MAX_SYMBOLS)):
                    break
                line_start = int(symbol["line_start"])
                label = str(symbol["name"])
                symbol_node = {
                    "id": _node_id("symbol", f"{relative}:{line_start}:{label}"),
                    "type": "symbol", "label": label, "symbol_kind": symbol["kind"],
                    "path": relative, "language": file_node["language"],
                    "line_start": line_start, "line_end": int(symbol["line_end"]),
                    "content_hash": file_node["content_hash"],
                }
                nodes.append(symbol_node)
                add_edge(file_node["id"], symbol_node["id"], "declares", "static")
                symbol_count += 1
        for item in imports[:MAX_IMPORTS_PER_FILE]:
            specifier = str(item.get("specifier") or "").strip()
            if not specifier:
                continue
            target_path = (
                _resolve_python_import(root, path, item)
                if path.suffix.lower() == ".py"
                else _resolve_js_import(root, path, specifier)
            )
            if target_path is not None and target_path in file_nodes:
                add_edge(
                    file_node["id"], file_nodes[target_path]["id"], "imports", "static",
                    line=int(item.get("line") or 1), specifier=specifier[:160],
                )
                continue
            external_key = f"{file_node['id']}:{specifier}"
            external = external_nodes.get(external_key)
            if external is None:
                external = {
                    "id": _node_id("unresolved", external_key), "type": "unresolved",
                    "label": specifier[:96], "path": relative, "language": file_node["language"],
                    "line_start": int(item.get("line") or 1), "line_end": int(item.get("line") or 1),
                }
                external_nodes[external_key] = external
                nodes.append(external)
            add_edge(
                file_node["id"], external["id"], "imports", "unresolved",
                line=int(item.get("line") or 1), specifier=specifier[:160],
            )

    nodes.sort(key=lambda node: (str(node.get("type")), str(node.get("path")), int(node.get("line_start") or 0), str(node.get("label"))))
    edge_fingerprint = hashlib.sha256(
        json_canonical(edges).encode("utf-8")
    ).hexdigest()[:24]
    graph_fingerprint = hashlib.sha256(
        json_canonical({
            "nodes": [
                {key: node.get(key) for key in ("id", "content_hash", "line_start", "line_end")}
                for node in nodes
            ],
            "edges": edges,
        }).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "truncated": len(files) >= max(1, min(max_files, MAX_FILES)),
        "limits": {"max_files": max(1, min(max_files, MAX_FILES)), "max_symbols": max(1, min(max_symbols, MAX_SYMBOLS))},
        "counts": {"files": len(file_nodes), "directories": len(directory_nodes), "symbols": symbol_count, "nodes": len(nodes), "edges": len(edges)},
        "nodes": nodes,
        "edges": edges,
        "edge_fingerprint": edge_fingerprint,
        "graph_fingerprint": graph_fingerprint,
    }


def json_canonical(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def find_node(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in graph.get("nodes", []):
        if isinstance(node, dict) and node.get("id") == node_id:
            return node
    raise CodeGraphError("code graph node not found")


def _safe_lines(path: Path, start_line: int, end_line: int, max_lines: int, max_chars: int) -> dict[str, Any]:
    text = _safe_text(path)
    lines = text.splitlines()
    if not lines:
        return {"path": "", "start_line": 1, "end_line": 1, "text": "", "truncated": False}
    start = max(1, min(int(start_line), len(lines)))
    end = max(start, min(int(end_line), len(lines), start + max_lines - 1))
    selected = "\n".join(lines[start - 1:end])
    truncated = end < int(end_line) or len(selected) > max_chars
    return {
        "start_line": start,
        "end_line": end,
        "text": selected[:max_chars],
        "truncated": truncated or len(selected) > max_chars,
    }


def build_node_dossier(
    workspace: str | Path,
    graph: dict[str, Any],
    node_id: str,
    *,
    selection: dict[str, Any] | None = None,
    include_preview: bool = False,
    include_source: bool = False,
) -> dict[str, Any]:
    """Return bounded facts and optional user-authorized code context for one node."""
    root = Path(workspace).expanduser().resolve()
    node = dict(find_node(graph, node_id))
    relative = node.get("path")
    if not isinstance(relative, str) or not relative or node.get("type") == "directory":
        raise CodeGraphError("select a file or code symbol to inspect")
    source_path = (root / relative).resolve()
    _relative(root, source_path)
    if not source_path.is_file() or source_path.suffix.lower() not in CODE_EXTENSIONS:
        raise CodeGraphError("selected code node is unavailable")
    start_line = int(node.get("line_start") or 1)
    end_line = int(node.get("line_end") or start_line)
    if selection is not None:
        if not isinstance(selection, dict):
            raise CodeGraphError("selection must be an object")
        selection_path = selection.get("path")
        if selection_path != relative:
            raise CodeGraphError("selection must stay within the selected code node")
        try:
            start_line = int(selection.get("start_line"))
            end_line = int(selection.get("end_line"))
        except (TypeError, ValueError) as error:
            raise CodeGraphError("selection line range is invalid") from error
        if start_line < 1 or end_line < start_line:
            raise CodeGraphError("selection line range is invalid")
        if end_line - start_line + 1 > MAX_SELECTION_LINES:
            raise CodeGraphError(f"selection is limited to {MAX_SELECTION_LINES} lines")
        node_start = int(node.get("line_start") or 1)
        node_end = int(node.get("line_end") or node_start)
        if start_line < node_start or end_line > node_end:
            scope = "symbol" if node.get("type") == "symbol" else "file"
            raise CodeGraphError(f"selection must stay within the selected {scope}")
    all_edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]
    related_edges = [edge for edge in all_edges if edge.get("source") == node_id or edge.get("target") == node_id]
    node_index = {item.get("id"): item for item in graph.get("nodes", []) if isinstance(item, dict)}
    neighbors: list[dict[str, Any]] = []
    for edge in related_edges[:24]:
        neighbor_id = edge.get("target") if edge.get("source") == node_id else edge.get("source")
        neighbor = node_index.get(neighbor_id)
        if not isinstance(neighbor, dict):
            continue
        neighbors.append({
            "node_id": neighbor.get("id"), "label": neighbor.get("label"), "type": neighbor.get("type"),
            "path": neighbor.get("path"), "relation": edge.get("relation"), "evidence": edge.get("evidence"),
            "direction": "outgoing" if edge.get("source") == node_id else "incoming", "edge_id": edge.get("id"),
        })
    evidence_refs: list[dict[str, Any]] = [{
        "ref_id": "E1", "kind": "node", "node_id": node_id, "path": relative,
        "line_start": start_line, "line_end": end_line,
    }]
    for index, edge in enumerate(related_edges[:12], start=2):
        evidence_refs.append({
            "ref_id": f"E{index}", "kind": "edge", "edge_id": edge.get("id"),
            "relation": edge.get("relation"), "evidence": edge.get("evidence"),
            "line_start": edge.get("line"), "path": relative,
        })
    preview = None
    if include_preview or include_source:
        line_limit = MAX_SELECTION_LINES if selection is not None else MAX_PREVIEW_LINES
        char_limit = MAX_SELECTION_CHARS if selection is not None else MAX_PREVIEW_CHARS
        preview = _safe_lines(source_path, start_line, end_line, line_limit, char_limit)
        preview["path"] = relative
    fingerprint_material = {
        "node": node.get("content_hash"), "node_id": node_id, "range": [start_line, end_line],
        "edge_fingerprint": graph.get("edge_fingerprint"),
        "neighbor_ids": [item.get("node_id") for item in neighbors],
        "source": _content_hash(str((preview or {}).get("text") or "")) if include_source else "metadata",
    }
    fingerprint = hashlib.sha256(json_canonical(fingerprint_material).encode("utf-8")).hexdigest()[:32]
    dossier: dict[str, Any] = {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "node": node,
        "selection": {"path": relative, "start_line": start_line, "end_line": end_line},
        "facts": {
            "language": node.get("language"), "symbol_kind": node.get("symbol_kind"),
            "parse_status": node.get("parse_status", "ok"), "symbol_count": node.get("symbol_count"),
            "neighbor_count": len(neighbors),
        },
        "neighbors": neighbors,
        "evidence_refs": evidence_refs,
        "fingerprint": fingerprint,
    }
    if include_preview and preview is not None:
        dossier["preview"] = preview
    if include_source and preview is not None:
        dossier["source_context"] = preview
    return dossier
