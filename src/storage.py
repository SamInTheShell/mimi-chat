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
MODES_FILE = CONFIG_DIR / "modes.json"
IGNORE_FILE = CONFIG_DIR / "ignore.json"
MCP_FILE = CONFIG_DIR / "mcp.json"

RECENT_DIRS_MAX = 8

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "ollama",
    "endpoint": "",
    "model": "",
    "projectDir": "",
    "recentDirs": [],
    "thinking": True,
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

TOOL_IDS: tuple[str, ...] = (
    "fuzzy_find_filename",
    "fuzzy_find_contents",
    "list_directory",
    "read_file",
    "edit_file",
    "apply_patch",
    "append_file",
    "mkdir",
    "rm",
    "move",
)

VALID_PERMS: frozenset[str] = frozenset({"ask", "allow", "disabled"})

# Built-in mode templates. Both ship pre-populated; users can edit them and
# "Restore defaults" to roll back, or clone them as the seed for custom modes.
BUILTIN_MODES: tuple[dict[str, Any], ...] = (
    {
        "id": "default",
        "name": "Default",
        "tools": {
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
        },
    },
    {
        "id": "accept_edits",
        "name": "Accept Edits",
        "tools": {tid: "allow" for tid in TOOL_IDS},
    },
)

BUILTIN_MODE_IDS: frozenset[str] = frozenset(m["id"] for m in BUILTIN_MODES)


def _default_modes_payload() -> dict[str, Any]:
    return {
        "activeId": "default",
        "items": [
            {"id": m["id"], "name": m["name"], "builtin": True, "tools": dict(m["tools"])}
            for m in BUILTIN_MODES
        ],
    }


def _builtin_default_tools(mode_id: str) -> dict[str, str]:
    for m in BUILTIN_MODES:
        if m["id"] == mode_id:
            return dict(m["tools"])
    return {tid: "ask" for tid in TOOL_IDS}


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


def _normalize_mode_item(raw: Any) -> dict[str, Any] | None:
    """Coerce one persisted mode entry to the canonical shape, or drop it.

    Built-in tool ids are always present (filled from the mode's defaults
    when missing). Extra keys — currently MCP tools, namespaced as
    ``mcp__<server>__<tool>`` — are kept verbatim so user-set permissions
    survive across reloads even before the MCP server is started.
    """
    if not isinstance(raw, dict):
        return None
    mid = raw.get("id")
    name = raw.get("name")
    if not isinstance(mid, str) or not mid.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    is_builtin = mid in BUILTIN_MODE_IDS
    tools_raw = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
    tools: dict[str, str] = {}
    seed = _builtin_default_tools(mid) if is_builtin else {tid: "ask" for tid in TOOL_IDS}
    for tid in TOOL_IDS:
        v = tools_raw.get(tid)
        if isinstance(v, str) and v in VALID_PERMS:
            tools[tid] = v
        else:
            tools[tid] = seed[tid]
    # Preserve any extra (non-builtin) keys with valid perms — e.g. mcp__server__tool.
    for k, v in tools_raw.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if k in tools:
            continue
        if isinstance(v, str) and v in VALID_PERMS:
            tools[k] = v
    return {"id": mid, "name": name, "builtin": is_builtin, "tools": tools}


def load_modes() -> dict[str, Any]:
    """Load the modes catalog, ensuring both built-in modes always exist.

    Schema: ``{"activeId": str, "items": [{id, name, builtin, tools}]}``.
    Tool perm maps are filled in from the built-in defaults so a saved file
    that pre-dates a newly added tool still loads with sane permissions.
    """
    raw = _read_json(MODES_FILE, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return _default_modes_payload()

    cleaned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in raw["items"]:
        norm = _normalize_mode_item(entry)
        if not norm or norm["id"] in seen_ids:
            continue
        cleaned.append(norm)
        seen_ids.add(norm["id"])

    # Re-insert any built-in mode that was deleted from the saved file. We keep
    # them at the front so the cycle order matches the shipped order.
    for builtin in BUILTIN_MODES:
        if builtin["id"] not in seen_ids:
            cleaned.insert(
                len(seen_ids),
                {
                    "id": builtin["id"],
                    "name": builtin["name"],
                    "builtin": True,
                    "tools": dict(builtin["tools"]),
                },
            )
            seen_ids.add(builtin["id"])

    active = raw.get("activeId")
    if not isinstance(active, str) or active not in seen_ids:
        active = "default"

    return {"activeId": active, "items": cleaned}


def save_modes(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the modes catalog, validating + reseating built-ins if missing."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return load_modes()

    cleaned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in payload["items"]:
        norm = _normalize_mode_item(entry)
        if not norm or norm["id"] in seen_ids:
            continue
        cleaned.append(norm)
        seen_ids.add(norm["id"])

    for builtin in BUILTIN_MODES:
        if builtin["id"] not in seen_ids:
            cleaned.insert(
                len(seen_ids),
                {
                    "id": builtin["id"],
                    "name": builtin["name"],
                    "builtin": True,
                    "tools": dict(builtin["tools"]),
                },
            )
            seen_ids.add(builtin["id"])

    active = payload.get("activeId")
    if not isinstance(active, str) or active not in seen_ids:
        active = "default"

    out = {"activeId": active, "items": cleaned}
    _atomic_write(MODES_FILE, out)
    return out


def builtin_mode_defaults() -> dict[str, dict[str, str]]:
    """Per-builtin-mode default tool perms, used by the frontend's Restore button."""
    return {m["id"]: dict(m["tools"]) for m in BUILTIN_MODES}


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


_MCP_SERVER_ID_RE = "abcdefghijklmnopqrstuvwxyz0123456789_-"


def _slug_server_id(s: Any) -> str:
    raw = str(s or "").strip().lower()
    return "".join(c if c in _MCP_SERVER_ID_RE else "_" for c in raw)


def _normalize_mcp_server(raw: Any) -> dict[str, Any] | None:
    """Coerce a persisted MCP server entry to the canonical shape, or drop it."""
    if not isinstance(raw, dict):
        return None
    sid = _slug_server_id(raw.get("id"))
    if not sid:
        return None
    name = str(raw.get("name") or sid).strip() or sid
    command = str(raw.get("command") or "").strip()
    args_raw = raw.get("args")
    args = [str(a) for a in args_raw if isinstance(a, str)] if isinstance(args_raw, list) else []
    env_raw = raw.get("env")
    env: dict[str, str] = {}
    if isinstance(env_raw, dict):
        for k, v in env_raw.items():
            if isinstance(k, str) and k.strip():
                env[k] = "" if v is None else str(v)
    cwd = str(raw.get("cwd") or "").strip()
    enabled = bool(raw.get("enabled", True))
    autostart = bool(raw.get("autostart", False))
    return {
        "id": sid,
        "name": name,
        "command": command,
        "args": args,
        "env": env,
        "cwd": cwd,
        "enabled": enabled,
        "autostart": autostart,
    }


def load_mcp() -> dict[str, Any]:
    """Read ``mcp.json`` — the configured MCP server list."""
    raw = _read_json(MCP_FILE, None)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw, dict) and isinstance(raw.get("servers"), list):
        for entry in raw["servers"]:
            norm = _normalize_mcp_server(entry)
            if norm and norm["id"] not in seen:
                items.append(norm)
                seen.add(norm["id"])
    return {"servers": items}


def save_mcp(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("servers"), list):
        return load_mcp()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in payload["servers"]:
        norm = _normalize_mcp_server(entry)
        if norm and norm["id"] not in seen:
            items.append(norm)
            seen.add(norm["id"])
    out = {"servers": items}
    _atomic_write(MCP_FILE, out)
    return out


def load_all() -> dict[str, Any]:
    from . import ignore as _ignore  # avoid import cycle at module load

    return {
        "config": load_config(),
        "prompts": {**load_prompts(), "defaults": list(DEFAULT_PROMPTS["items"])},
        "sampling": load_sampling(),
        "modes": {**load_modes(), "builtinDefaults": builtin_mode_defaults(), "toolIds": list(TOOL_IDS)},
        "ignore": {**load_ignore(), "defaults": list(_ignore.DEFAULT_PATTERNS)},
        "mcp": load_mcp(),
        "home": str(Path.home()),
    }
