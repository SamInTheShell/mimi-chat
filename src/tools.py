"""Sandboxed filesystem tools for mimi-chat.

Every tool resolves its input path against a fixed project root and refuses
anything that escapes via ``..``, absolute paths outside the root, or symlinks
pointing outside. The root is supplied by trusted JS-side state; tool
arguments from the LLM never carry their own root.

Each tool exposes both a *preview* (dry-run shape used to render the
confirmation modal and the chat-history card) and an *apply* path that performs
the side effect. Read-only tools share the same shape for both.
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import ignore as _ignore_mod
from . import storage as _storage

DEFAULT_LIMIT = 100
DEFAULT_READ_BYTES = 256_000          # 256 KB cap on read content
DIFF_INPUT_MAX_BYTES = 10_000_000     # 10 MB cap for files participating in a diff
RM_PREVIEW_CAP = 200

DEFAULT_READ_FILE_TOKEN_LIMIT = 10_000  # mirrors storage.DEFAULT_CONFIG["readFileTokenLimit"]
READ_FILE_BYTE_CAP = 10_000_000          # hard ceiling for read_file (10 MB)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token. Conservative for English/code."""
    if not text:
        return 0
    return (len(text) + 3) // 4


class ToolError(Exception):
    """Raised by tool implementations for user-meaningful failures."""


# ── Path safety ────────────────────────────────


def _resolve_root(root_str: str) -> Path:
    if not root_str:
        raise ToolError("No project directory selected.")
    p = Path(os.path.expanduser(str(root_str))).resolve()
    if not p.exists() or not p.is_dir():
        raise ToolError(f"Project directory does not exist: {root_str}")
    return p


def _safe_join(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root``, refusing escapes via ``..``, absolute paths, or symlinks."""
    rel_str = "" if rel is None else str(rel)
    # Reject NUL bytes and the like up front.
    if "\x00" in rel_str:
        raise ToolError("Path contains a NUL byte.")
    candidate = (root / rel_str).resolve(strict=False) if rel_str else root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ToolError(f"Path escapes project directory: {rel_str}")
    return candidate


def _load_filter(root: Path) -> _ignore_mod.IgnoreFilter:
    cfg = _storage.load_ignore()
    return _ignore_mod.build_filter(
        root,
        cfg.get("patterns") or [],
        bool(cfg.get("honorGitignore", True)),
    )


def _rel_posix(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _refuse_if_ignored(flt: _ignore_mod.IgnoreFilter, root: Path, target: Path, *, is_dir_hint: bool | None = None) -> None:
    """Raise ToolError if ``target`` matches the ignore filter.

    ``is_dir_hint`` lets callers say up-front whether to treat the path as a
    directory; when ``None`` the existing filesystem state decides.
    """
    rel = _rel_posix(root, target)
    if not rel or rel == ".":
        return
    if is_dir_hint is None:
        try:
            is_dir = target.is_dir() and not target.is_symlink()
        except OSError:
            is_dir = False
    else:
        is_dir = bool(is_dir_hint)
    if flt.is_ignored(rel, is_dir):
        raise ToolError(
            f"Path is excluded by the ignore list (Settings → Ignore): {rel}"
        )


# ── Helpers ────────────────────────────────────


def _read_text(p: Path, max_bytes: int) -> tuple[str, bool, int]:
    """Returns ``(text, truncated, size_in_bytes)``. Text decoded as UTF-8 with replacement."""
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as e:
        raise ToolError(f"Cannot read file: {e.strerror or e}")
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    return text, truncated, size


def _unified_diff(a: str, b: str, label: str) -> str:
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = difflib.unified_diff(a_lines, b_lines, fromfile=label, tofile=label, lineterm="")
    return "\n".join(line.rstrip("\n") for line in diff)


def _diff_stats(diff_text: str) -> tuple[int, int]:
    """Count added/removed lines in a unified diff (excluding the file headers)."""
    add = rem = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            rem += 1
    return add, rem


def _basename_score(query: str, name: str) -> int | None:
    q = query.lower()
    n = name.lower()
    if not q:
        return None
    if q in n:
        return n.index(q)
    i = 0
    for c in n:
        if i < len(q) and c == q[i]:
            i += 1
    if i == len(q):
        return 1000 + (len(n) - len(q))
    return None


# ── Read-only tools ────────────────────────────


def fuzzy_find_filename(root: Path, flt: _ignore_mod.IgnoreFilter, query: str, limit: int) -> dict:
    if not query.strip():
        raise ToolError("`query` is required.")
    matches: list[tuple[int, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Filter dirs in-place so os.walk doesn't descend into ignored trees.
        kept: list[str] = []
        for d in dirnames:
            rel_dir = _rel_posix(root, Path(dirpath) / d)
            if flt.is_ignored(rel_dir, is_dir=True):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            if flt.is_ignored(rel, is_dir=False):
                continue
            score = _basename_score(query, fn)
            if score is not None:
                matches.append((score, rel))
        if len(matches) > limit * 4:
            break
    matches.sort()
    return {"matches": [m[1] for m in matches[:limit]], "total": len(matches)}


def fuzzy_find_contents(root: Path, flt: _ignore_mod.IgnoreFilter, query: str, limit: int, max_per_file: int = 5) -> dict:
    if not query.strip():
        raise ToolError("`query` is required.")
    matches: list[dict] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept: list[str] = []
        for d in dirnames:
            rel_dir = _rel_posix(root, Path(dirpath) / d)
            if flt.is_ignored(rel_dir, is_dir=True):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            if flt.is_ignored(rel, is_dir=False):
                continue
            try:
                with full.open("r", encoding="utf-8", errors="replace") as fh:
                    file_hits = 0
                    for i, line in enumerate(fh, start=1):
                        if query in line:
                            matches.append({
                                "path": rel,
                                "line": i,
                                "text": line.rstrip("\n"),
                            })
                            file_hits += 1
                            if file_hits >= max_per_file or len(matches) >= limit:
                                break
            except OSError:
                continue
            if len(matches) >= limit:
                truncated = True
                break
        if len(matches) >= limit:
            truncated = True
            break
    return {"matches": matches[:limit], "truncated": truncated}


def list_directory(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, depth: int) -> dict:
    """List entries under ``path`` (relative to ``root``).

    ``depth=0`` returns only direct children of ``path``. ``depth=N`` descends N
    levels into subdirectories. Names in the result are relative to ``path``
    itself, so the model can read them without re-deriving the prefix.
    """
    target = _safe_join(root, path)
    if not target.exists():
        raise ToolError(f"Path does not exist: {path or '.'}")
    if not target.is_dir():
        raise ToolError(f"Path is not a directory: {path or '.'}")
    _refuse_if_ignored(flt, root, target, is_dir_hint=True)
    if depth < 0:
        depth = 0
    cap = DEFAULT_LIMIT * 5
    entries: list[dict] = []

    def walk(dir_path: Path, level: int) -> bool:
        """Walk ``dir_path``; return True if the cap was hit."""
        try:
            children = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return False
        for child in children:
            try:
                is_dir = child.is_dir() and not child.is_symlink()
            except OSError:
                is_dir = False
            rel_child = _rel_posix(root, child)
            if flt.is_ignored(rel_child, is_dir=is_dir):
                continue
            kind = "dir" if is_dir else "file"
            row: dict = {"name": child.relative_to(target).as_posix(), "kind": kind}
            if kind == "file":
                try:
                    row["size"] = child.stat().st_size
                except OSError:
                    pass
            entries.append(row)
            if len(entries) >= cap:
                return True
            if is_dir and level < depth:
                if walk(child, level + 1):
                    return True
        return False

    truncated = walk(target, 0)
    return {"entries": entries, "truncated": truncated}


def read_file(
    root: Path,
    flt: _ignore_mod.IgnoreFilter,
    path: str,
    offset: int | None = None,
    limit: int | None = None,
    token_limit: int = DEFAULT_READ_FILE_TOKEN_LIMIT,
) -> dict:
    """Read a project file, optionally a line slice, and refuse oversize reads.

    ``offset`` is 1-based; ``limit`` is a line count. With both omitted the
    whole file is returned, but only when the rough token estimate stays under
    ``token_limit``; otherwise we raise so the caller re-issues with a slice.
    """
    target = _safe_join(root, path)
    if not target.exists():
        raise ToolError(f"File does not exist: {path}")
    if not target.is_file():
        raise ToolError(f"Path is not a file: {path}")
    _refuse_if_ignored(flt, root, target, is_dir_hint=False)

    if token_limit <= 0:
        token_limit = DEFAULT_READ_FILE_TOKEN_LIMIT
    size = target.stat().st_size
    if size > READ_FILE_BYTE_CAP:
        raise ToolError(
            f"File is too large to load ({size} bytes; cap is {READ_FILE_BYTE_CAP}). "
            "Use other tools to inspect specific regions."
        )

    text, _trunc, _size = _read_text(target, READ_FILE_BYTE_CAP)
    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    posix_path = Path(path).as_posix()
    partial = offset is not None or limit is not None

    if partial:
        start_line = offset if (isinstance(offset, int) and offset > 0) else 1
        start_idx = min(start_line - 1, total_lines)
        if limit is None:
            end_idx = total_lines
        else:
            end_idx = min(total_lines, start_idx + max(0, int(limit)))
        slice_lines = lines[start_idx:end_idx]
        slice_text = "".join(slice_lines)
        tokens = _estimate_tokens(slice_text)
        if tokens > token_limit:
            raise ToolError(
                f"Requested slice is ~{tokens} tokens (limit {token_limit}). "
                f"Reduce `limit`. Slice covers {len(slice_lines)} of {total_lines} lines."
            )
        return {
            "path": posix_path,
            "size": size,
            "content": slice_text,
            "offset": start_idx + 1,
            "lines_returned": len(slice_lines),
            "total_lines": total_lines,
            "tokens": tokens,
            "token_limit": token_limit,
            "partial": True,
            "eof": end_idx >= total_lines,
        }

    tokens = _estimate_tokens(text)
    if tokens > token_limit:
        raise ToolError(
            f"File is ~{tokens} tokens (limit {token_limit}). "
            f"Read in parts: pass `offset` (1-based line) and `limit` (line count). "
            f"Total lines: {total_lines}."
        )
    return {
        "path": posix_path,
        "size": size,
        "content": text,
        "total_lines": total_lines,
        "tokens": tokens,
        "token_limit": token_limit,
        "partial": False,
        "eof": True,
    }


# ── Mutating tools — preview + apply pairs ──────


def edit_file_compute(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, search: str, replace: str) -> dict:
    target = _safe_join(root, path)
    if not target.exists():
        raise ToolError(f"File does not exist: {path}")
    if not target.is_file():
        raise ToolError(f"Path is not a file: {path}")
    _refuse_if_ignored(flt, root, target, is_dir_hint=False)
    if not isinstance(search, str) or not search:
        raise ToolError("`search` must be a non-empty string.")
    if not isinstance(replace, str):
        raise ToolError("`replace` must be a string.")
    if target.stat().st_size > DIFF_INPUT_MAX_BYTES:
        raise ToolError("File is too large to edit safely with this tool.")
    original, _trunc, _size = _read_text(target, DIFF_INPUT_MAX_BYTES)
    count = original.count(search)
    if count == 0:
        raise ToolError("`search` text not found in file.")
    if count > 1:
        raise ToolError(f"`search` matches {count} occurrences; refine to a unique snippet.")
    updated = original.replace(search, replace, 1)
    diff = _unified_diff(original, updated, Path(path).as_posix())
    add, rem = _diff_stats(diff)
    return {
        "path": Path(path).as_posix(),
        "diff": diff,
        "added": add,
        "removed": rem,
        "_target": target,
        "_updated": updated,
    }


def edit_file_apply(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, search: str, replace: str) -> dict:
    info = edit_file_compute(root, flt, path, search, replace)
    target: Path = info.pop("_target")
    updated: str = info.pop("_updated")
    target.write_text(updated, encoding="utf-8")
    return {
        "path": info["path"],
        "diff": info["diff"],
        "added": info["added"],
        "removed": info["removed"],
        "bytes_written": len(updated.encode("utf-8")),
    }


_HUNK_HDR_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _parse_unified_patch(patch_text: str) -> list[dict]:
    """Parse a unified diff into a list of hunks. Tolerant of file headers/diff metadata.

    Each hunk: ``{old_start, lines: [(' '|'-'|'+', text), ...]}``. ``old_start``
    is taken from the ``@@`` header but treated as a *hint* — the applier will
    re-locate the hunk by context match if the line numbers drift.
    """
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise ToolError("`patch` must be a non-empty unified diff string.")
    hunks: list[dict] = []
    cur: dict | None = None

    for raw in patch_text.splitlines():
        m = _HUNK_HDR_RE.match(raw)
        if m:
            if cur is not None:
                hunks.append(cur)
            cur = {"old_start": int(m.group(1)), "lines": []}
            continue
        if cur is None:
            # Outside a hunk: skip metadata (--- a/, +++ b/, diff --git, Index:, etc.)
            continue
        if raw == "":
            cur["lines"].append((" ", ""))
            continue
        op = raw[0]
        if op in (" ", "+", "-"):
            cur["lines"].append((op, raw[1:]))
        elif op == "\\":
            # "\ No newline at end of file" — informational; we don't try to roundtrip exactly.
            continue
        else:
            raise ToolError(f"Unexpected line in hunk: {raw[:80]!r}")
    if cur is not None:
        hunks.append(cur)
    if not hunks:
        raise ToolError("No hunks found in patch (need at least one `@@` header).")
    for h in hunks:
        if not any(op in ("+", "-") for op, _ in h["lines"]):
            raise ToolError("Hunk has no additions or removals.")
    return hunks


def _strip_eol(s: str) -> str:
    if s.endswith("\r\n"):
        return s[:-2]
    if s.endswith("\n"):
        return s[:-1]
    return s


def _detect_default_eol(src_lines: list[str]) -> str:
    crlf = sum(1 for s in src_lines if s.endswith("\r\n"))
    lf = sum(1 for s in src_lines if s.endswith("\n") and not s.endswith("\r\n"))
    return "\r\n" if crlf > lf else "\n"


def _find_hunk_match(src_lines: list[str], src_idx_min: int, hint: int, expected: list[str]) -> int | None:
    """Locate ``expected`` (lines without EOL) in ``src_lines`` near ``hint``.

    Returns the 0-based start index, or None. If ``expected`` is empty (pure
    additions hunk), the hint position is returned as-is.
    """
    if not expected:
        return max(src_idx_min, min(hint, len(src_lines)))

    def matches_at(start: int) -> bool:
        if start < src_idx_min or start + len(expected) > len(src_lines):
            return False
        for i, e in enumerate(expected):
            if _strip_eol(src_lines[start + i]) != e:
                return False
        return True

    if matches_at(hint):
        return hint
    horizon = max(len(src_lines), 200)
    for delta in range(1, horizon):
        for cand in (hint - delta, hint + delta):
            if matches_at(cand):
                return cand
    return None


def _apply_hunks(original: str, hunks: list[dict]) -> str:
    src_lines = original.splitlines(keepends=True)
    default_eol = _detect_default_eol(src_lines)
    out: list[str] = []
    src_idx = 0

    for hunk in hunks:
        expected_old = [text for op, text in hunk["lines"] if op in (" ", "-")]
        hint = max(0, hunk["old_start"] - 1)
        match_idx = _find_hunk_match(src_lines, src_idx, hint, expected_old)
        if match_idx is None:
            head = next((t for op, t in hunk["lines"] if op in (" ", "-")), "")
            head_excerpt = head[:80] + ("…" if len(head) > 80 else "")
            raise ToolError(
                f"Patch hunk at hint line {hunk['old_start']} does not match file content "
                f"(near {head_excerpt!r}). The file may have changed; re-read it and resubmit."
            )
        out.extend(src_lines[src_idx:match_idx])
        cursor = match_idx
        for op, text in hunk["lines"]:
            if op == " ":
                out.append(src_lines[cursor])
                cursor += 1
            elif op == "-":
                cursor += 1
            else:  # '+'
                out.append(text + default_eol)
        src_idx = cursor

    out.extend(src_lines[src_idx:])
    return "".join(out)


def apply_patch_compute(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, patch: str) -> dict:
    target = _safe_join(root, path)
    if not target.exists():
        raise ToolError(f"File does not exist: {path}")
    if not target.is_file():
        raise ToolError(f"Path is not a file: {path}")
    _refuse_if_ignored(flt, root, target, is_dir_hint=False)
    if target.stat().st_size > DIFF_INPUT_MAX_BYTES:
        raise ToolError("File is too large to edit safely with this tool.")
    original, _trunc, _size = _read_text(target, DIFF_INPUT_MAX_BYTES)
    hunks = _parse_unified_patch(patch)
    updated = _apply_hunks(original, hunks)
    if updated == original:
        raise ToolError("Patch produced no changes (additions and removals canceled out).")
    diff = _unified_diff(original, updated, Path(path).as_posix())
    add, rem = _diff_stats(diff)
    return {
        "path": Path(path).as_posix(),
        "diff": diff,
        "added": add,
        "removed": rem,
        "_target": target,
        "_updated": updated,
    }


def apply_patch_apply(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, patch: str) -> dict:
    info = apply_patch_compute(root, flt, path, patch)
    target: Path = info.pop("_target")
    updated: str = info.pop("_updated")
    target.write_text(updated, encoding="utf-8")
    return {
        "path": info["path"],
        "diff": info["diff"],
        "added": info["added"],
        "removed": info["removed"],
        "bytes_written": len(updated.encode("utf-8")),
    }


def append_file_preview(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, content: str) -> dict:
    target = _safe_join(root, path)
    if target.exists() and not target.is_file():
        raise ToolError(f"Path is not a file: {path}")
    _refuse_if_ignored(flt, root, target, is_dir_hint=False)
    if not isinstance(content, str):
        raise ToolError("`content` must be a string.")
    current_size = target.stat().st_size if target.exists() else 0
    return {
        "path": Path(path).as_posix(),
        "exists": target.exists(),
        "content": content,
        "appended_bytes": len(content.encode("utf-8")),
        "size_after": current_size + len(content.encode("utf-8")),
    }


def append_file_apply(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, content: str) -> dict:
    target = _safe_join(root, path)
    if target.exists() and not target.is_file():
        raise ToolError(f"Path is not a file: {path}")
    _refuse_if_ignored(flt, root, target, is_dir_hint=False)
    if not isinstance(content, str):
        raise ToolError("`content` must be a string.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(content)
    return {
        "path": Path(path).as_posix(),
        "appended_bytes": len(content.encode("utf-8")),
        "size_after": target.stat().st_size,
    }


def mkdir_preview(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, recursive: bool) -> dict:
    target = _safe_join(root, path)
    _refuse_if_ignored(flt, root, target, is_dir_hint=True)
    return {"path": Path(path).as_posix(), "exists": target.exists(), "recursive": recursive}


def mkdir_apply(root: Path, flt: _ignore_mod.IgnoreFilter, path: str, recursive: bool) -> dict:
    target = _safe_join(root, path)
    _refuse_if_ignored(flt, root, target, is_dir_hint=True)
    if recursive:
        target.mkdir(parents=True, exist_ok=True)
    else:
        try:
            target.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            raise ToolError(f"Already exists: {path}")
        except FileNotFoundError:
            raise ToolError(f"Parent directory does not exist (set recursive=true): {path}")
    return {"path": Path(path).as_posix(), "created": True}


def _resolve_move_targets(
    root: Path,
    flt: _ignore_mod.IgnoreFilter,
    src: str,
    dst: str,
    overwrite: bool,
) -> tuple[Path, Path, bool, bool]:
    """Validate a move and return ``(src_path, dst_path, src_is_dir, dst_will_overwrite)``."""
    if not isinstance(src, str) or not src.strip():
        raise ToolError("`from` is required.")
    if not isinstance(dst, str) or not dst.strip():
        raise ToolError("`to` is required.")
    src_path = _safe_join(root, src)
    dst_path = _safe_join(root, dst)
    if src_path == root:
        raise ToolError("Refusing to move the project directory itself.")
    if dst_path == root:
        raise ToolError("Refusing to move onto the project directory itself.")
    if not src_path.exists() and not src_path.is_symlink():
        raise ToolError(f"Source does not exist: {src}")
    if src_path == dst_path:
        raise ToolError("`from` and `to` are the same path.")
    try:
        src_is_dir = src_path.is_dir() and not src_path.is_symlink()
    except OSError:
        src_is_dir = False
    _refuse_if_ignored(flt, root, src_path, is_dir_hint=src_is_dir)
    _refuse_if_ignored(flt, root, dst_path, is_dir_hint=src_is_dir)
    if src_is_dir:
        try:
            dst_path.relative_to(src_path)
            raise ToolError("Destination is inside the source directory.")
        except ValueError:
            pass
    parent = dst_path.parent
    if not parent.exists() or not parent.is_dir():
        raise ToolError(f"Destination parent directory does not exist: {Path(dst).parent.as_posix()}")
    will_overwrite = False
    if dst_path.exists() or dst_path.is_symlink():
        if not overwrite:
            raise ToolError(f"Destination already exists (set overwrite=true to replace): {dst}")
        try:
            dst_is_dir = dst_path.is_dir() and not dst_path.is_symlink()
        except OSError:
            dst_is_dir = False
        if dst_is_dir:
            raise ToolError(f"Destination is a directory; refusing to overwrite: {dst}")
        if src_is_dir:
            raise ToolError(f"Cannot overwrite file with directory: {dst}")
        will_overwrite = True
    return src_path, dst_path, src_is_dir, will_overwrite


def move_preview(root: Path, flt: _ignore_mod.IgnoreFilter, src: str, dst: str, overwrite: bool) -> dict:
    src_path, dst_path, src_is_dir, will_overwrite = _resolve_move_targets(root, flt, src, dst, overwrite)
    return {
        "from": Path(src).as_posix(),
        "to": Path(dst).as_posix(),
        "kind_of_source": "dir" if src_is_dir else "file",
        "overwrite": bool(overwrite),
        "will_overwrite": will_overwrite,
    }


def move_apply(root: Path, flt: _ignore_mod.IgnoreFilter, src: str, dst: str, overwrite: bool) -> dict:
    src_path, dst_path, src_is_dir, will_overwrite = _resolve_move_targets(root, flt, src, dst, overwrite)
    try:
        if will_overwrite:
            dst_path.unlink()
        shutil.move(str(src_path), str(dst_path))
    except OSError as e:
        raise ToolError(f"Move failed: {e.strerror or e}")
    return {
        "from": Path(src).as_posix(),
        "to": Path(dst).as_posix(),
        "kind_of_source": "dir" if src_is_dir else "file",
        "overwrote": will_overwrite,
        "moved": True,
    }


def _enumerate_for_rm(target: Path, root: Path, cap: int) -> tuple[list[dict], bool]:
    items: list[dict] = []
    if cap <= 0:
        return items, True
    if target.is_file() or target.is_symlink():
        items.append({"name": target.relative_to(root).as_posix(), "kind": "file"})
        return items, False
    items.append({"name": target.relative_to(root).as_posix(), "kind": "dir"})
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        for d in sorted(dirnames):
            full = Path(dirpath) / d
            items.append({"name": full.relative_to(root).as_posix(), "kind": "dir"})
            if len(items) >= cap:
                return items, True
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            items.append({"name": full.relative_to(root).as_posix(), "kind": "file"})
            if len(items) >= cap:
                return items, True
    return items, False


def _resolve_rm_targets(root: Path, flt: _ignore_mod.IgnoreFilter, paths: list[str]) -> list[tuple[str, Path]]:
    if not paths:
        raise ToolError("Provide at least one path to remove.")
    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for p in paths:
        target = _safe_join(root, p)
        if target == root:
            raise ToolError("Refusing to delete the project directory itself.")
        if not target.exists() and not target.is_symlink():
            raise ToolError(f"Path does not exist: {p}")
        _refuse_if_ignored(flt, root, target)
        if target in seen:
            continue
        seen.add(target)
        out.append((p, target))
    return out


def rm_preview(root: Path, flt: _ignore_mod.IgnoreFilter, paths: list[str], recursive: bool) -> dict:
    targets = _resolve_rm_targets(root, flt, paths)
    entries: list[dict] = []
    truncated_global = False
    remaining = RM_PREVIEW_CAP
    for p, target in targets:
        items, truncated = _enumerate_for_rm(target, root, remaining)
        if truncated:
            truncated_global = True
        remaining -= len(items)
        entries.append({
            "path": Path(p).as_posix(),
            "is_dir": target.is_dir() and not target.is_symlink(),
            "items": items,
            "truncated": truncated,
        })
    return {"paths": entries, "recursive": recursive, "truncated": truncated_global}


def rm_apply(root: Path, flt: _ignore_mod.IgnoreFilter, paths: list[str], recursive: bool) -> dict:
    targets = _resolve_rm_targets(root, flt, paths)
    # Deepest first so an ancestor rm doesn't yank a still-pending sibling.
    targets.sort(key=lambda t: len(t[1].parts), reverse=True)
    done: list[str] = []
    for p, target in targets:
        if not target.exists() and not target.is_symlink():
            done.append(Path(p).as_posix())
            continue
        try:
            if target.is_dir() and not target.is_symlink():
                if not recursive:
                    try:
                        target.rmdir()
                    except OSError:
                        raise ToolError(f"Directory is not empty (set recursive=true to remove): {p}")
                else:
                    shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as e:
            already = ", ".join(done) or "nothing"
            raise ToolError(f"Removed [{already}] but failed at {p}: {e.strerror or e}")
        done.append(Path(p).as_posix())
    return {"removed": True, "count": len(done)}


# ── Argument coercion ──────────────────────────


def _arg_str(args: dict, key: str, *, required: bool = False, default: str = "") -> str:
    v = args.get(key, default) if isinstance(args, dict) else default
    if v is None:
        if required:
            raise ToolError(f"Missing argument: {key}")
        return default
    return str(v)


def _arg_int(args: dict, key: str, default: int) -> int:
    v = args.get(key) if isinstance(args, dict) else None
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _arg_optional_int(args: dict, key: str) -> int | None:
    v = args.get(key) if isinstance(args, dict) else None
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _arg_str_list(args: dict, key: str, *, required: bool = False) -> list[str]:
    if not isinstance(args, dict) or key not in args:
        if required:
            raise ToolError(f"Missing argument: {key}")
        return []
    v = args[key]
    if not isinstance(v, list):
        raise ToolError(f"Argument `{key}` must be a list of strings.")
    out: list[str] = []
    for item in v:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            out.append(s)
    if required and not out:
        raise ToolError(f"Argument `{key}` must contain at least one path.")
    return out


def _arg_bool(args: dict, key: str, default: bool) -> bool:
    v = args.get(key) if isinstance(args, dict) else None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return default


# ── Dispatch ───────────────────────────────────


def preview(name: str, args: Any, root_str: str) -> dict:
    root = _resolve_root(root_str)
    flt = _load_filter(root)
    a = args if isinstance(args, dict) else {}
    if name == "fuzzy_find_filename":
        query = _arg_str(a, "query", required=True)
        limit = _arg_int(a, "limit", DEFAULT_LIMIT)
        return {"kind": name, "query": query, "limit": limit, **fuzzy_find_filename(root, flt, query, limit)}
    if name == "fuzzy_find_contents":
        query = _arg_str(a, "query", required=True)
        limit = _arg_int(a, "limit", DEFAULT_LIMIT)
        return {"kind": name, "query": query, "limit": limit, **fuzzy_find_contents(root, flt, query, limit)}
    if name == "list_directory":
        path = _arg_str(a, "path", default="")
        depth = max(0, _arg_int(a, "depth", 0))
        return {"kind": name, "path": path, "depth": depth, **list_directory(root, flt, path, depth)}
    if name == "read_file":
        cfg = _storage.load_config()
        token_limit = int(cfg.get("readFileTokenLimit") or DEFAULT_READ_FILE_TOKEN_LIMIT)
        path = _arg_str(a, "path", required=True)
        offset = _arg_optional_int(a, "offset")
        limit = _arg_optional_int(a, "limit")
        return {"kind": name, **read_file(root, flt, path, offset, limit, token_limit)}
    if name == "edit_file":
        info = edit_file_compute(root, flt, _arg_str(a, "path", required=True), _arg_str(a, "search", required=True), _arg_str(a, "replace"))
        info.pop("_target", None)
        info.pop("_updated", None)
        info["kind"] = "edit_file"
        return info
    if name == "apply_patch":
        info = apply_patch_compute(root, flt, _arg_str(a, "path", required=True), _arg_str(a, "patch", required=True))
        info.pop("_target", None)
        info.pop("_updated", None)
        info["kind"] = "edit_file"  # render identically to edit_file in the UI
        info["variant"] = "patch"
        return info
    if name == "append_file":
        return {"kind": name, **append_file_preview(root, flt, _arg_str(a, "path", required=True), _arg_str(a, "content"))}
    if name == "mkdir":
        return {"kind": name, **mkdir_preview(root, flt, _arg_str(a, "path", required=True), _arg_bool(a, "recursive", True))}
    if name == "rm":
        return {"kind": name, **rm_preview(root, flt, _arg_str_list(a, "paths", required=True), _arg_bool(a, "recursive", False))}
    if name == "move":
        return {"kind": name, **move_preview(root, flt, _arg_str(a, "from", required=True), _arg_str(a, "to", required=True), _arg_bool(a, "overwrite", False))}
    raise ToolError(f"Unknown tool: {name}")


def execute(name: str, args: Any, root_str: str) -> dict:
    root = _resolve_root(root_str)
    flt = _load_filter(root)
    a = args if isinstance(args, dict) else {}
    if name == "fuzzy_find_filename":
        return fuzzy_find_filename(root, flt, _arg_str(a, "query", required=True), _arg_int(a, "limit", DEFAULT_LIMIT))
    if name == "fuzzy_find_contents":
        return fuzzy_find_contents(root, flt, _arg_str(a, "query", required=True), _arg_int(a, "limit", DEFAULT_LIMIT))
    if name == "list_directory":
        return list_directory(root, flt, _arg_str(a, "path", default=""), max(0, _arg_int(a, "depth", 0)))
    if name == "read_file":
        cfg = _storage.load_config()
        token_limit = int(cfg.get("readFileTokenLimit") or DEFAULT_READ_FILE_TOKEN_LIMIT)
        return read_file(
            root,
            flt,
            _arg_str(a, "path", required=True),
            _arg_optional_int(a, "offset"),
            _arg_optional_int(a, "limit"),
            token_limit,
        )
    if name == "edit_file":
        return edit_file_apply(root, flt, _arg_str(a, "path", required=True), _arg_str(a, "search", required=True), _arg_str(a, "replace"))
    if name == "apply_patch":
        return apply_patch_apply(root, flt, _arg_str(a, "path", required=True), _arg_str(a, "patch", required=True))
    if name == "append_file":
        return append_file_apply(root, flt, _arg_str(a, "path", required=True), _arg_str(a, "content"))
    if name == "mkdir":
        return mkdir_apply(root, flt, _arg_str(a, "path", required=True), _arg_bool(a, "recursive", True))
    if name == "rm":
        return rm_apply(root, flt, _arg_str_list(a, "paths", required=True), _arg_bool(a, "recursive", False))
    if name == "move":
        return move_apply(root, flt, _arg_str(a, "from", required=True), _arg_str(a, "to", required=True), _arg_bool(a, "overwrite", False))
    raise ToolError(f"Unknown tool: {name}")


def _wrap(fn, *args):
    try:
        return {"ok": True, "result": fn(*args)} if fn is execute else {"ok": True, "preview": fn(*args)}
    except ToolError as e:
        return {"ok": False, "error": str(e), "error_kind": "tool"}
    except OSError as e:
        return {"ok": False, "error": (e.strerror or str(e)), "error_kind": "os"}
    except Exception as e:  # last-resort safety net
        return {"ok": False, "error": str(e), "error_kind": "unknown"}


def call(name: str, args: Any, root_str: str) -> dict:
    return _wrap(execute, name, args, root_str)


def preview_call(name: str, args: Any, root_str: str) -> dict:
    return _wrap(preview, name, args, root_str)
