"""Priority employers and stars.

A priority company (FAANG+ by default, editable in Settings) is one whose
applications the candidate wants to refine personally. Its postings are
starred automatically, prepared like any other, and never sent without the
candidate's own approval: unattended runs skip them, and "Send ready" leaves
starred applications for one-by-one review.

A job's ``starred`` column is three-valued. ``None`` follows the priority list,
``1`` is a star the candidate added, and ``0`` is a star the candidate removed.
Pure functions only; safe to use from views, the scheduler, and tests.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "plc", "gmbh", "lp", "llp"}


def normalize_company(name: str) -> str:
    """Lower-case words without punctuation or legal suffixes; initials join ("D. E. Shaw" -> "de shaw")."""
    words = re.findall(r"[a-z0-9]+", str(name or "").casefold().replace("&", " "))
    joined: list[str] = []
    for word in words:
        if len(word) == 1 and word.isalpha() and joined and len(joined[-1]) <= 3 and joined[-1].isalpha():
            joined[-1] += word
        else:
            joined.append(word)
    while joined and joined[-1] in _SUFFIXES:
        joined.pop()
    return " ".join(joined)


def is_priority(company: str, priority: Iterable[str]) -> bool:
    """Whole-word prefix match: "Meta" matches "Meta Platforms" but never "Metabase"."""
    name = normalize_company(company)
    if not name:
        return False
    squashed = name.replace(" ", "")
    for entry in priority:
        term = normalize_company(entry)
        if not term:
            continue
        if name == term or name.startswith(term + " ") or squashed == term.replace(" ", ""):
            return True
    return False


def _starred_value(record: Any) -> Any:
    value = record.get("starred") if isinstance(record, dict) else getattr(record, "starred", None)
    return None if value is None else bool(value)


def _company(record: Any) -> str:
    return str((record.get("company") if isinstance(record, dict) else getattr(record, "company", "")) or "")


def is_starred(record: Any, priority: Iterable[str]) -> bool:
    """The star the candidate sees: their own choice, else the priority list."""
    value = _starred_value(record)
    return value if value is not None else is_priority(_company(record), priority)


def held_from_bulk_send(record: Any, priority: Iterable[str]) -> bool:
    """Starred applications wait for their own Approve & send, never a batch send."""
    return is_starred(record, priority)


def held_from_automation(record: Any, priority: Iterable[str]) -> bool:
    """Unattended runs never apply to a priority company or a starred job."""
    return is_priority(_company(record), priority) or is_starred(record, priority)
