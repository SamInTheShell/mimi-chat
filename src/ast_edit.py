"""AST-aware code editing powered by tree-sitter.

Identifies edit targets by symbol name (function, class, method) or line
range, then applies structural operations (replace, replace_body,
insert_before, insert_after, delete) with automatic indentation handling.

Falls back to line-range-only editing for files whose tree-sitter grammar
is not installed, so plaintext / Markdown / config files are still editable.
"""
from __future__ import annotations

import difflib
import importlib
import re
from pathlib import Path
from typing import Any

# ── Language registry ────────────────────────────

EXTENSION_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".go": "go",
    ".json": "json",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".rb": "ruby",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}

_lang_cache: dict[str, Any] = {}


def _load_language(name: str):
    """Load a tree-sitter Language by grammar name. Returns None on failure."""
    if name in _lang_cache:
        return _lang_cache[name]

    lang = None
    try:
        from tree_sitter import Language

        if name == "typescript":
            import tree_sitter_typescript as mod
            lang = Language(mod.language_typescript())
        elif name == "tsx":
            import tree_sitter_typescript as mod
            lang = Language(mod.language_tsx())
        else:
            mod = importlib.import_module(f"tree_sitter_{name}")
            lang = Language(mod.language())
    except (ImportError, AttributeError, OSError):
        pass

    _lang_cache[name] = lang
    return lang


def _language_for_file(filepath: str):
    """Return ``(lang_name, Language)`` for *filepath*, or ``(None, None)``."""
    ext = Path(filepath).suffix.lower()
    name = EXTENSION_LANG.get(ext)
    if not name:
        return None, None
    return name, _load_language(name)


# ── Target parsing ───────────────────────────────

_LINE_RE = re.compile(r"^lines?:\s*(\d+)(?:\s*-\s*(\d+))?$", re.IGNORECASE)

VALID_ACTIONS = frozenset({"replace", "replace_body", "insert_before", "insert_after", "delete"})


def _parse_target(target: str) -> tuple[str, Any]:
    """Parse a target string.

    Returns one of:
      ("symbol", "MyClass.method")
      ("line",   15)                    # 1-based
      ("lines",  (10, 20))              # 1-based inclusive
    """
    m = _LINE_RE.match(target.strip())
    if m:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if start == end:
            return ("line", start)
        return ("lines", (min(start, end), max(start, end)))
    return ("symbol", target.strip())


# ── AST helpers ──────────────────────────────────


def _node_name(node) -> str | None:
    """Extract the identifier name from a definition node."""
    # Most grammars use a "name" field on definition nodes.
    name_child = node.child_by_field_name("name")
    if name_child:
        return name_child.text.decode("utf-8")

    # HTML elements — extract the tag name.
    if node.type == "element":
        for child in node.children:
            if child.type == "start_tag":
                for gc in child.children:
                    if gc.type == "tag_name":
                        return gc.text.decode("utf-8")
    # CSS rule_set — use the selector text as the name.
    if node.type == "rule_set":
        for child in node.children:
            if "selector" in child.type:
                return child.text.decode("utf-8").strip()

    return None


# Node types we recognise as "definitions" (things a user would target by name).
_DEF_KEYWORDS = {
    "definition", "declaration", "declarator", "spec",
    "rule_set", "element", "import_statement",
}


def _is_definition(node) -> bool:
    t = node.type
    return any(kw in t for kw in _DEF_KEYWORDS)


def _effective_node(node):
    """If *node* is wrapped in a decorator/export, return the wrapper instead.

    This way ``target="foo"`` captures the decorators above ``def foo`` or the
    ``export`` keyword in front of a JS function, which is almost always what
    the user wants when doing ``replace`` or ``delete``.
    """
    parent = node.parent
    if parent and parent.type in (
        "decorated_definition",   # Python
        "export_statement",       # JS/TS
    ):
        return parent
    return node


def _find_symbol(root_node, symbol_path: str):
    """Resolve a dot-separated symbol path against the AST.

    Returns ``(node, None)`` on success, or ``(None, error_msg)`` on failure.
    The returned node is the *effective* node (including wrappers like
    decorators), but ``replace_body`` will drill into the inner definition.
    """
    # Try the full string as a single name first (handles CSS selectors like
    # ".container", "#app", "div.active", etc.).  Only split on dots if the
    # whole-string lookup fails.
    parts_candidates: list[list[str]] = [[symbol_path]]
    if "." in symbol_path:
        parts_candidates.append(symbol_path.split("."))

    def _try_resolve(parts: list[str]):
        """Attempt resolution with a specific parts list. Returns (node, error)."""
        return _resolve_parts(root_node, parts)

    for parts in parts_candidates:
        node, err = _try_resolve(parts)
        if node is not None:
            return node, None

    # Last resort: deep recursive search for a single-component name.
    if len(parts_candidates[0]) == 1:
        node = _deep_find(root_node, symbol_path)
        if node is not None:
            return _effective_node(node), None

    # Return the error from the split attempt (more informative).
    _, err = _try_resolve(parts_candidates[-1])
    return None, err


def _deep_find(node, name: str, depth: int = 0):
    """Recursively search the entire tree for a definition named *name*."""
    if depth > 30:
        return None
    n = _node_name(node)
    if n == name and _is_definition(node):
        return node
    # Also check wrapper types
    if node.type in ("decorated_definition", "export_statement", "expression_statement",
                      "lexical_declaration", "variable_declaration"):
        for child in node.children:
            cn = _node_name(child)
            if cn == name and _is_definition(child):
                return child
    for child in node.children:
        found = _deep_find(child, name, depth + 1)
        if found is not None:
            return found
    return None


def _resolve_parts(root_node, parts: list[str]):
    """Resolve a list of symbol-path components against the AST."""

    def _search(scope_node, depth: int = 0):
        """Yield ``(name, raw_node)`` for definitions directly inside *scope_node*."""
        if depth > 20:
            return
        for child in scope_node.children:
            name = _node_name(child)
            if name and _is_definition(child):
                yield (name, child)
            # Descend into wrappers that don't have their own name (decorated_definition, etc.)
            if child.type in (
                "decorated_definition",
                "export_statement",
                "expression_statement",
            ):
                for gc in child.children:
                    gc_name = _node_name(gc)
                    if gc_name and _is_definition(gc):
                        yield (gc_name, gc)
            # For variable declarations like `const foo = () => {}`, the real
            # definition hides inside a variable_declarator child.
            if child.type in ("lexical_declaration", "variable_declaration"):
                for gc in child.children:
                    gc_name = _node_name(gc)
                    if gc_name:
                        yield (gc_name, gc)

    current_scope = root_node
    resolved_so_far: list[str] = []

    for i, part in enumerate(parts):
        found = None
        for name, node in _search(current_scope):
            if name == part:
                found = node
                break
        if found is None:
            available = sorted({n for n, _ in _search(current_scope)})
            avail_str = ", ".join(available[:20]) if available else "(none)"
            ctx = ".".join(resolved_so_far) if resolved_so_far else "top level"
            return None, (
                f"Symbol `{part}` not found at {ctx}. "
                f"Available: {avail_str}"
            )
        resolved_so_far.append(part)
        # For intermediate path components, descend into the body.
        if i < len(parts) - 1:
            body = _node_body(found)
            if body:
                current_scope = body
            else:
                current_scope = found
        else:
            # Final component — return the effective (wrapper-inclusive) node.
            return _effective_node(found), None

    return None, f"Empty symbol path."


def _node_body(node):
    """Return the body sub-node of a definition, or ``None``."""
    body = node.child_by_field_name("body")
    if body:
        return body
    for child in node.children:
        if child.type in (
            "block",
            "statement_block",
            "class_body",
            "declaration_list",
            "field_declaration_list",
            "switch_body",
        ):
            return child
    return None


def _inner_definition(node):
    """Unwrap decorators / exports to get the actual definition node."""
    if node.type in ("decorated_definition", "export_statement"):
        for child in node.children:
            if _is_definition(child) and _node_name(child):
                return child
    return node


def _list_top_symbols(root_node, limit: int = 30) -> list[str]:
    """List top-level symbol names for error messages."""
    names: list[str] = []
    seen: set[str] = set()

    def _collect(scope, depth=0):
        if depth > 2:
            return
        for child in scope.children:
            name = _node_name(child)
            if name and _is_definition(child) and name not in seen:
                names.append(name)
                seen.add(name)
            if child.type in ("decorated_definition", "export_statement", "expression_statement"):
                for gc in child.children:
                    gc_name = _node_name(gc)
                    if gc_name and _is_definition(gc) and gc_name not in seen:
                        names.append(gc_name)
                        seen.add(gc_name)
            if child.type in ("lexical_declaration", "variable_declaration"):
                for gc in child.children:
                    gc_name = _node_name(gc)
                    if gc_name and gc_name not in seen:
                        names.append(gc_name)
                        seen.add(gc_name)
            if len(names) >= limit:
                return

    _collect(root_node)
    return names


# ── Indentation ──────────────────────────────────


def _detect_indent(lines: list[str], start_row: int) -> str:
    """Detect the indentation string of the first non-empty line at or after ``start_row``."""
    for i in range(start_row, min(start_row + 5, len(lines))):
        stripped = lines[i].lstrip()
        if stripped:
            return lines[i][: len(lines[i]) - len(stripped)]
    return ""


def _reindent(code: str, target_indent: str) -> str:
    """Shift *code* so its base indentation matches *target_indent*.

    Preserves relative indentation within the block.
    """
    raw_lines = code.split("\n")
    # Determine the minimum indentation of non-empty lines.
    min_indent: int | None = None
    for line in raw_lines:
        stripped = line.lstrip()
        if stripped:
            n = len(line) - len(stripped)
            if min_indent is None or n < min_indent:
                min_indent = n
    if min_indent is None:
        min_indent = 0

    out: list[str] = []
    for line in raw_lines:
        stripped = line.lstrip()
        if not stripped:
            out.append("")
        else:
            relative = len(line) - len(stripped) - min_indent
            out.append(target_indent + " " * max(0, relative) + stripped)
    return "\n".join(out)


# ── Line / byte helpers ─────────────────────────


def _lines_for_range(source_lines: list[str], start_1: int, end_1: int) -> tuple[int, int]:
    """Clamp a 1-based inclusive range to valid 0-based indices."""
    total = len(source_lines)
    s = max(0, start_1 - 1)
    e = min(total - 1, end_1 - 1)
    return s, e


# ── Diff helpers ─────────────────────────────────


def _unified_diff(a: str, b: str, label: str) -> str:
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = difflib.unified_diff(a_lines, b_lines, fromfile=label, tofile=label, lineterm="")
    return "\n".join(line.rstrip("\n") for line in diff)


def _diff_stats(diff_text: str) -> tuple[int, int]:
    add = rem = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            rem += 1
    return add, rem


# ── Core edit logic ──────────────────────────────


def _detect_eol(source: str) -> str:
    crlf = source.count("\r\n")
    lf = source.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _apply_line_edit(
    source: str,
    target_start_0: int,
    target_end_0: int,
    action: str,
    code: str,
    indent: str,
    eol: str,
) -> str:
    """Apply an edit action at the line level.

    ``target_start_0`` and ``target_end_0`` are 0-based inclusive line indices.
    """
    lines = source.splitlines(keepends=True)

    if action == "delete":
        new_lines = lines[:target_start_0] + lines[target_end_0 + 1 :]
        # Collapse double blank lines left by the deletion.
        result = "".join(new_lines)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result

    reindented = _reindent(code, indent)
    new_content_lines = reindented.splitlines(keepends=True)
    # Ensure every injected line has a line ending.
    if new_content_lines and not new_content_lines[-1].endswith(("\n", "\r\n")):
        new_content_lines[-1] += eol

    if action in ("replace", "replace_body"):
        new_lines = lines[:target_start_0] + new_content_lines + lines[target_end_0 + 1 :]
    elif action == "insert_before":
        new_lines = lines[:target_start_0] + new_content_lines + lines[target_start_0:]
    elif action == "insert_after":
        insert_at = target_end_0 + 1
        new_lines = lines[:insert_at] + new_content_lines + lines[insert_at:]
    else:
        raise EditError(f"Unknown action: {action}")

    return "".join(new_lines)


class EditError(Exception):
    """Raised for user-meaningful edit failures."""


def compute(
    source: str,
    filepath: str,
    target: str,
    action: str,
    code: str,
) -> dict[str, Any]:
    """Compute an AST-aware edit without writing to disk.

    Returns a dict with ``diff``, ``added``, ``removed``, ``updated`` keys.
    Raises ``EditError`` on failure.
    """
    # Guard: detect when model stuffs code into the target field.
    if "\n" in target or len(target) > 120:
        raise EditError(
            "`target` must be a short symbol name (e.g. \"myFunc\", \"MyClass.method\") "
            "or a line reference (e.g. \"line:15\", \"lines:10-20\"). "
            "Do NOT put code in the target field — put it in `code`."
        )

    if action not in VALID_ACTIONS:
        raise EditError(
            f"Unknown action `{action}`. "
            f"Valid actions: {', '.join(sorted(VALID_ACTIONS))}"
        )
    if action != "delete" and not code:
        raise EditError(f"Action `{action}` requires a non-empty `code` argument.")

    target_kind, target_data = _parse_target(target)
    eol = _detect_eol(source)
    source_lines = source.splitlines(keepends=True)
    total_lines = len(source_lines)

    lang_name, language = _language_for_file(filepath)
    tree = None
    if language:
        try:
            from tree_sitter import Parser
            parser = Parser(language)
            tree = parser.parse(source.encode("utf-8"))
        except Exception:
            tree = None

    # ── Resolve target to line range ──
    if target_kind == "symbol":
        if tree is None:
            # No grammar — try a naive text search as a courtesy.
            hit_line = _naive_symbol_search(source_lines, target_data)
            if hit_line is not None:
                return _compute_with_naive_hit(
                    source, source_lines, filepath, hit_line, target_data, action, code, eol
                )
            raise EditError(
                f"No tree-sitter grammar available for `{Path(filepath).suffix}` files, "
                f"and a text search for `{target_data}` found no match. "
                f"Use line-based targets (e.g. `line:15` or `lines:10-20`) for this file type."
            )

        node, err = _find_symbol(tree.root_node, target_data)
        if node is None:
            raise EditError(err)

        if action == "replace_body":
            inner = _inner_definition(node)
            body = _node_body(inner)
            if body is None:
                raise EditError(
                    f"Symbol `{target_data}` has no body to replace "
                    f"(node type: {inner.type})."
                )
            # For brace-delimited bodies (statement_block, class_body, etc.)
            # replace only the inner lines between { and }, keeping the braces.
            # For indentation-delimited bodies (Python block) replace the
            # entire body range.
            has_braces = any(
                c.type in ("{", "}") for c in body.children
            )
            if not has_braces:
                # Python-style: the block IS the body content (indent-delimited).
                t_start = body.start_point[0]
                t_end = body.end_point[0]
            else:
                # Brace-style (statement_block, class_body, field_declaration_list, etc.)
                # The opening brace is on start_point row; closing brace on end_point row.
                # Replace only the lines between them.
                brace_open_row = body.start_point[0]
                brace_close_row = body.end_point[0]
                if brace_close_row - brace_open_row <= 1:
                    # Single-line or empty body like `{}` — expand to insert between braces.
                    # Replace the whole body line(s) and wrap the new code in braces.
                    t_start = brace_open_row
                    t_end = brace_close_row
                    # Detect the definition's own indentation for the braces.
                    def_indent = _detect_indent(source_lines, node.start_point[0])
                    inner_indent = def_indent + "  "
                    reindented_inner = _reindent(code, inner_indent)
                    code = def_indent + "{" + eol + reindented_inner + eol + def_indent + "}"
                    # Skip the normal reindent — we already handled it.
                    indent = ""
                else:
                    t_start = brace_open_row + 1
                    t_end = brace_close_row - 1
        else:
            t_start = node.start_point[0]
            t_end = node.end_point[0]

    elif target_kind == "line":
        line_1 = target_data
        if line_1 < 1:
            raise EditError("Line numbers are 1-based.")
        if line_1 > total_lines:
            raise EditError(f"Line {line_1} is out of range (file has {total_lines} lines).")
        t_start = line_1 - 1
        t_end = line_1 - 1
        if action == "replace_body":
            raise EditError("`replace_body` requires a symbol target, not a line target.")

    elif target_kind == "lines":
        s1, e1 = target_data
        if s1 < 1:
            raise EditError("Line numbers are 1-based.")
        if s1 > total_lines:
            raise EditError(f"Start line {s1} is out of range (file has {total_lines} lines).")
        # Clamp end to file length — allows "lines:100-999" to mean "from 100 to EOF".
        t_start = s1 - 1
        t_end = min(e1 - 1, total_lines - 1)
        if action == "replace_body":
            raise EditError("`replace_body` requires a symbol target, not a line range.")

    else:
        raise EditError(f"Cannot parse target: {target}")

    # Determine indentation for the replacement code.
    indent = _detect_indent(source_lines, t_start)

    updated = _apply_line_edit(source, t_start, t_end, action, code, indent, eol)

    if updated == source:
        raise EditError("Edit produced no changes.")

    # ── Validate: re-parse to check for syntax errors ──
    # Only validate symbol-based edits. Line-range edits are an escape hatch —
    # the user/model may be fixing a broken file or writing partial code.
    if tree is not None and action != "delete" and target_kind == "symbol":
        _validate_syntax(updated, language, filepath)

    diff = _unified_diff(source, updated, Path(filepath).as_posix())
    add, rem = _diff_stats(diff)
    return {
        "diff": diff,
        "added": add,
        "removed": rem,
        "updated": updated,
    }


# ── Naive symbol search (no grammar) ─────────────


_NAIVE_PATTERNS = [
    # Python
    re.compile(r"^(\s*)(?:async\s+)?def\s+{name}\s*\("),
    re.compile(r"^(\s*)class\s+{name}\s*[\(:]"),
    # JS / TS
    re.compile(r"^(\s*)(?:export\s+)?(?:async\s+)?function\s+{name}\s*[\(<]"),
    re.compile(r"^(\s*)(?:export\s+)?class\s+{name}\s"),
    re.compile(r"^(\s*)(?:const|let|var)\s+{name}\s*="),
    # Go
    re.compile(r"^(\s*)func\s+(?:\(.*?\)\s*)?{name}\s*\("),
    re.compile(r"^(\s*)type\s+{name}\s"),
    # Rust
    re.compile(r"^(\s*)(?:pub\s+)?fn\s+{name}\s*[\(<]"),
    re.compile(r"^(\s*)(?:pub\s+)?struct\s+{name}\s"),
    re.compile(r"^(\s*)(?:pub\s+)?enum\s+{name}\s"),
    # Ruby
    re.compile(r"^(\s*)def\s+{name}\s*[\(;]?"),
    # CSS (selector as name)
    re.compile(r"^(\s*){name}\s*\{{"),
]


def _naive_symbol_search(source_lines: list[str], symbol_path: str) -> int | None:
    """Try to find a definition by text matching. Returns a 0-based line index or None.

    Only the *last* component of a dot path is searched — scoping is not
    possible without a parser.
    """
    name = symbol_path.split(".")[-1]
    escaped = re.escape(name)
    for i, line in enumerate(source_lines):
        for pat in _NAIVE_PATTERNS:
            concrete = re.compile(pat.pattern.replace("{name}", escaped))
            if concrete.match(line):
                return i
    return None


def _compute_with_naive_hit(
    source: str,
    source_lines: list[str],
    filepath: str,
    hit_line_0: int,
    symbol: str,
    action: str,
    code: str,
    eol: str,
) -> dict[str, Any]:
    """Handle a naive-search hit: find the extent of the definition and apply the edit."""
    total = len(source_lines)

    # Guess the end of the definition by indentation: the block extends while
    # subsequent lines are more-indented or blank.
    start_indent = len(source_lines[hit_line_0]) - len(source_lines[hit_line_0].lstrip())
    end_0 = hit_line_0
    for j in range(hit_line_0 + 1, total):
        stripped = source_lines[j].lstrip()
        if not stripped:
            # Blank lines continue the block.
            end_0 = j
            continue
        line_indent = len(source_lines[j]) - len(stripped)
        if line_indent > start_indent:
            end_0 = j
        else:
            break

    if action == "replace_body":
        # Body starts on the line after the definition header.
        body_start = hit_line_0 + 1
        if body_start > end_0:
            raise EditError(
                f"Symbol `{symbol}` appears to be a single-line definition; "
                f"no body to replace."
            )
        indent = _detect_indent(source_lines, body_start)
        updated = _apply_line_edit(source, body_start, end_0, "replace", code, indent, eol)
    else:
        indent = _detect_indent(source_lines, hit_line_0)
        updated = _apply_line_edit(source, hit_line_0, end_0, action, code, indent, eol)

    if updated == source:
        raise EditError("Edit produced no changes.")

    diff = _unified_diff(source, updated, Path(filepath).as_posix())
    add, rem = _diff_stats(diff)
    return {
        "diff": diff,
        "added": add,
        "removed": rem,
        "updated": updated,
    }


# ── Syntax validation ───────────────────────────


def _validate_syntax(updated: str, language, filepath: str) -> None:
    """Re-parse *updated* and raise if the tree has errors."""
    try:
        from tree_sitter import Parser
        parser = Parser(language)
        new_tree = parser.parse(updated.encode("utf-8"))
    except Exception:
        return  # can't validate — proceed optimistically

    errors = _collect_errors(new_tree.root_node, limit=5)
    if errors:
        locations = ", ".join(f"line {r + 1}" for r, _c in errors)
        raise EditError(
            f"Edit would introduce syntax errors at {locations}. "
            f"Check your code and try again."
        )


def _collect_errors(node, limit: int = 5) -> list[tuple[int, int]]:
    """Collect ``(row, col)`` positions of ERROR / MISSING nodes."""
    found: list[tuple[int, int]] = []

    def walk(n):
        if len(found) >= limit:
            return
        if n.type == "ERROR" or n.is_missing:
            found.append(n.start_point)
        for child in n.children:
            walk(child)

    walk(node)
    return found
