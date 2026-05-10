"""Default system prompts seeded on first launch.

Each prompt lives in its own ``.md`` file in this package. The index below
is the source of truth for id, display name, and ordering — adding a
prompt means dropping in a new ``.md`` file and appending an entry here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).parent

# Each row: (id, display name, file, cyclable). ``cyclable`` is the default
# value of the per-prompt "include in Shift+Tab cycle" flag. Capability prompts
# (e.g. Inline Designer) ship with cyclable=False so they don't surprise the
# user when cycling — they're still pickable from the /system menu and Ctrl+P.
_PROMPT_INDEX: tuple[tuple[int, str, str, bool], ...] = (
    (1, "Coding Agent",       "coding_agent.md",       True),
    (2, "Writing Editor",     "writing_editor.md",     True),
    (3, "Tutor",              "tutor.md",              True),
    (4, "Brainstorm Partner", "brainstorm_partner.md", True),
    (5, "Legal Assistant",    "legal_assistant.md",    True),
    (6, "Medical Assistant",  "medical_assistant.md",  True),
    (7, "Financial Advisor",  "financial_advisor.md",  True),
    (8, "Therapist",          "therapist.md",          True),
    (9, "Inline Designer",    "designer.md",           False),
)


def default_prompts() -> dict[str, Any]:
    return {
        "activeId": 1,
        "items": [
            {
                "id": pid,
                "name": name,
                "text": (_PROMPTS_DIR / fname).read_text(encoding="utf-8"),
                "cyclable": cyclable,
            }
            for pid, name, fname, cyclable in _PROMPT_INDEX
        ],
    }


DESIGNER_PROMPT_ID = 9
