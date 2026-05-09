"""Configuration persistence for mimi-chat.

State lives under ``~/.mimi-chat/`` as a small set of JSON files. Paths are
persisted in tilde form (``~/...``) for portability across machines/usernames;
expand on load when an absolute path is needed for filesystem ops.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .prompts import default_prompts

CONFIG_DIR = Path.home() / ".mimi-chat"
CONFIG_FILE = CONFIG_DIR / "config.json"
PROMPTS_FILE = CONFIG_DIR / "prompts.json"
SAMPLING_FILE = CONFIG_DIR / "sampling.json"
TOOLS_FILE = CONFIG_DIR / "tools.json"
IGNORE_FILE = CONFIG_DIR / "ignore.json"

RECENT_DIRS_MAX = 8

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "ollama",
    "endpoint": "",
    "model": "",
    "projectDir": "",
    "recentDirs": [],
    "thinking": True,
    "autoAcceptEdits": False,
    "promptId": 1,
    "readFileTokenLimit": 10000,
    "windowGeometry": {"x": None, "y": None, "width": 800, "height": 750},
    "terminal": {"open": False, "height": None},
}

DEFAULT_PROMPTS: dict[str, Any] = default_prompts()

DEFAULT_SAMPLING: dict[str, float] = {
    "temperature": 0.7,
    "topP": 0.9,
    "topK": 40,
    "minP": 0.05,
    "repeatPenalty": 1.0,
}

DEFAULT_IGNORE: dict[str, Any] = {
    "honorGitignore": True,
    "patterns": [],  # seeded from ignore.DEFAULT_PATTERNS on first load
}

DEFAULT_TOOLS: dict[str, str] = {
    "fuzzy_find_filename": "allow",
    "fuzzy_find_contents": "allow",
    "list_directory":      "allow",
    "read_file":           "allow",
    "edit_file":           "ask",
    "apply_patch":         "ask",
    "append_file":         "ask",
    "mkdir":               "ask",
    "rm":                  "ask",
    "move":                "ask",
}


def expand_user_path(p: str) -> str:
    """Expand a leading ``~`` / ``~user`` to the absolute home path. Empty stays empty."""
    if not p:
        return ""
    return os.path.expanduser(str(p))


def collapse_to_tilde(p: str) -> str:
    """If ``p`` is at or under ``$HOME``, collapse the prefix to ``~``."""
    if not p:
        return ""
    s = str(p)
    home = str(Path.home())
    if s == home:
        return "~"
    if s.startswith(home + os.sep):
        return "~" + s[len(home):]
    return s


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _atomic_write(path: Path, data: Any) -> None:
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalize_recent_dirs(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for d in items:
        if not d:
            continue
        v = collapse_to_tilde(str(d))
        if v and v not in seen:
            out.append(v)
            seen.add(v)
    return out[:RECENT_DIRS_MAX]


def load_config() -> dict[str, Any]:
    raw = _read_json(CONFIG_FILE, {}) or {}
    cfg = {**DEFAULT_CONFIG, **(raw if isinstance(raw, dict) else {})}
    cfg["projectDir"] = collapse_to_tilde(str(cfg.get("projectDir") or ""))
    cfg["recentDirs"] = _normalize_recent_dirs(cfg.get("recentDirs"))
    return cfg


def save_config(partial: dict[str, Any]) -> dict[str, Any]:
    cur = load_config()
    if isinstance(partial, dict):
        cur.update(partial)
    cur["projectDir"] = collapse_to_tilde(str(cur.get("projectDir") or ""))
    cur["recentDirs"] = _normalize_recent_dirs(cur.get("recentDirs"))
    _atomic_write(CONFIG_FILE, cur)
    return cur


def load_prompts() -> dict[str, Any]:
    raw = _read_json(PROMPTS_FILE, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return DEFAULT_PROMPTS
    return raw


def save_prompts(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return load_prompts()
    _atomic_write(PROMPTS_FILE, payload)
    return payload


def load_sampling() -> dict[str, float]:
    raw = _read_json(SAMPLING_FILE, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    return {**DEFAULT_SAMPLING, **raw}


def save_sampling(payload: dict[str, float]) -> dict[str, float]:
    cur = {**DEFAULT_SAMPLING, **(payload if isinstance(payload, dict) else {})}
    _atomic_write(SAMPLING_FILE, cur)
    return cur


def load_tools() -> dict[str, str]:
    raw = _read_json(TOOLS_FILE, None)
    if not isinstance(raw, dict):
        return dict(DEFAULT_TOOLS)
    return {**DEFAULT_TOOLS, **{k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}}


def save_tools(payload: dict[str, str]) -> dict[str, str]:
    if not isinstance(payload, dict):
        return load_tools()
    cleaned = {k: v for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)}
    _atomic_write(TOOLS_FILE, cleaned)
    return cleaned


def add_recent_dir(path: str) -> list[str]:
    cfg = load_config()
    tilde = collapse_to_tilde(expand_user_path(str(path or "")))
    if not tilde:
        return cfg.get("recentDirs", [])
    recents = [d for d in cfg.get("recentDirs", []) if d != tilde]
    recents.insert(0, tilde)
    cfg["recentDirs"] = recents[:RECENT_DIRS_MAX]
    save_config(cfg)
    return cfg["recentDirs"]


def remove_recent_dir(path: str) -> list[str]:
    cfg = load_config()
    tilde = collapse_to_tilde(expand_user_path(str(path or "")))
    if not tilde:
        return cfg.get("recentDirs", [])
    cfg["recentDirs"] = [d for d in cfg.get("recentDirs", []) if d != tilde]
    save_config(cfg)
    return cfg["recentDirs"]


def _coerce_pattern_list(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [s for s in items if isinstance(s, str)]


def load_ignore() -> dict[str, Any]:
    """Read ``ignore.json``, seeding/migrating to the canonical schema.

    Schema: ``{"honorGitignore": bool, "patterns": [str]}``. ``patterns`` is the
    full user-editable list; defaults are the seed used on first run. The
    legacy ``extraPatterns`` shape (defaults always-on + extras) is migrated by
    treating ``defaults + extras`` as the new ``patterns``.
    """
    from . import ignore as _ignore  # avoid import cycle at module load
    raw = _read_json(IGNORE_FILE, None)

    honor = True
    if isinstance(raw, dict) and "honorGitignore" in raw:
        honor = bool(raw["honorGitignore"])

    if isinstance(raw, dict) and isinstance(raw.get("patterns"), list):
        patterns = _coerce_pattern_list(raw["patterns"])
    elif isinstance(raw, dict) and isinstance(raw.get("extraPatterns"), list):
        # Legacy: defaults were always-on; combine with extras for the new schema.
        seen: set[str] = set()
        merged: list[str] = []
        for src in (_ignore.DEFAULT_PATTERNS, _coerce_pattern_list(raw["extraPatterns"])):
            for p in src:
                if p not in seen:
                    seen.add(p)
                    merged.append(p)
        patterns = merged
    else:
        # Fresh install — seed from defaults.
        patterns = list(_ignore.DEFAULT_PATTERNS)

    return {"honorGitignore": honor, "patterns": patterns}


def save_ignore(payload: dict[str, Any]) -> dict[str, Any]:
    cur = load_ignore()
    if isinstance(payload, dict):
        if "honorGitignore" in payload:
            cur["honorGitignore"] = bool(payload["honorGitignore"])
        if "patterns" in payload and isinstance(payload["patterns"], list):
            cur["patterns"] = _coerce_pattern_list(payload["patterns"])
    _atomic_write(IGNORE_FILE, cur)
    return cur


def load_all() -> dict[str, Any]:
    from . import ignore as _ignore  # avoid import cycle at module load

    return {
        "config": load_config(),
        "prompts": {**load_prompts(), "defaults": list(DEFAULT_PROMPTS["items"])},
        "sampling": load_sampling(),
        "tools": load_tools(),
        "ignore": {**load_ignore(), "defaults": list(_ignore.DEFAULT_PATTERNS)},
        "home": str(Path.home()),
    }
