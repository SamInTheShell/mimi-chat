"""Path ignore filtering for the sandboxed filesystem tools.

Combines two pattern sources, both in gitignore syntax:

- The user's editable list from Settings → Ignore (``patterns``). On first run
  this is seeded from ``DEFAULT_PATTERNS``; afterward the user owns it
  completely (add, remove, "Restore defaults").
- The project's root ``.gitignore`` (when ``honorGitignore`` is true).

Walking tools (``fuzzy_find_*``, ``list_directory``) skip matched paths during
traversal; path-targeting tools (``read_file``, ``edit_file`` …) hard-refuse a
matched path with a ``ToolError`` so the model can't sneak around the filter.

Nested ``.gitignore`` files inside subdirectories are not currently merged in —
the goal is to keep small models from wasting context on obviously-skippable
trees (deps, caches, build outputs), which the root ``.gitignore`` covers in
practice for the vast majority of projects.
"""
from __future__ import annotations

from pathlib import Path

import pathspec

DEFAULT_PATTERNS: list[str] = [
    # Defaults aim at directories and specific filenames that obviously waste
    # context (caches, deps, build outputs, VCS, secrets). They deliberately
    # avoid file-type globs (`*.png`, `*.zip`, `*.pdf`, …) — the model should
    # be free to discover any file it can read; users can add their own
    # exclusions or rely on the project's `.gitignore`.
    # ── Version control ─────────────────────────
    ".git/", ".hg/", ".svn/",
    # ── Python ──────────────────────────────────
    "__pycache__/",
    ".venv/",
    ".tox/", ".nox/",
    ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    "*.egg-info/", ".eggs/",
    # ── JS / TS ────────────────────────────────
    "node_modules/", ".pnpm-store/", ".yarn/cache/", ".next/", ".nuxt/", ".svelte-kit/",
    # ── Build / dist ───────────────────────────
    "dist/", "build/", "out/", "target/",
    # ── Lock files (specific filenames, huge & auto-generated) ─
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "uv.lock", "poetry.lock", "Pipfile.lock", "Cargo.lock", "composer.lock",
    # ── Secrets / env ──────────────────────────
    ".env", ".env.*",
    # ── Editor / OS noise ──────────────────────
    ".idea/", ".vscode/",
    ".DS_Store", "Thumbs.db",
    # ── Logs / coverage ────────────────────────
    "logs/", ".coverage", "coverage/", "htmlcov/",
]


class IgnoreFilter:
    """Combined ignore filter rooted at a project directory."""

    def __init__(self, spec: pathspec.GitIgnoreSpec) -> None:
        self._spec = spec

    def is_ignored(self, rel: str, is_dir: bool = False) -> bool:
        """Match a path *relative to project root* (POSIX separators) against the spec."""
        s = (rel or "").replace("\\", "/").lstrip("/")
        if not s or s == ".":
            return False
        # gitwildmatch needs the trailing slash for directory-only patterns to fire.
        candidate = s + "/" if (is_dir and not s.endswith("/")) else s
        return bool(self._spec.match_file(candidate))


def _clean_user_patterns(items: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in (items or []):
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def build_filter(
    root: Path,
    patterns: list[str] | None,
    honor_gitignore: bool,
) -> IgnoreFilter:
    """Build an ``IgnoreFilter`` for ``root`` from the user-curated pattern list.

    ``patterns`` is the *full* user-managed list (the defaults are just the
    initial seed — the user can edit, remove, or add freely in Settings).
    Comments (``#``) and blank lines are stripped.
    """
    lines: list[str] = _clean_user_patterns(patterns)
    if honor_gitignore:
        try:
            text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
            lines.extend(text.splitlines())
        except OSError:
            pass
    return IgnoreFilter(pathspec.GitIgnoreSpec.from_lines(lines))
