"""Default system prompts seeded on first launch.

Each prompt lives in its own ``.md`` file in this package. The index below
is the source of truth for id, display name, and ordering — adding a
prompt means dropping in a new ``.md`` file and appending an entry here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).parent

_PROMPT_INDEX: tuple[tuple[int, str, str], ...] = (
    (1, "Coding Agent",       "coding_agent.md"),
    (2, "Writing Editor",     "writing_editor.md"),
    (3, "Tutor",              "tutor.md"),
    (4, "Brainstorm Partner", "brainstorm_partner.md"),
    (5, "Legal Assistant",    "legal_assistant.md"),
    (6, "Medical Assistant",  "medical_assistant.md"),
    (7, "Financial Advisor",  "financial_advisor.md"),
    (8, "Therapist",          "therapist.md"),
)


def default_prompts() -> dict[str, Any]:
    return {
        "activeId": 1,
        "items": [
            {
                "id": pid,
                "name": name,
                "text": (_PROMPTS_DIR / fname).read_text(encoding="utf-8"),
            }
            for pid, name, fname in _PROMPT_INDEX
        ],
    }
