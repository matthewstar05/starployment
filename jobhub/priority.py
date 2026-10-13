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
from functools import lru_cache
from typing import Any

_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "plc", "gmbh", "lp", "llp"}


def normalize_company(name: str) -> str:
    """Lower-case words without punctuation or legal suffixes; initials join ("D. E. Shaw" -> "de shaw")."""
    return _normalize(str(name or ""))


@lru_cache(maxsize=16384)
def _normalize(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.casefold().replace("&", " "))
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


from .fortune import is_fortune_500, is_fortune_1000


def _starred_value(record: Any) -> Any:
    value = record.get("starred") if isinstance(record, dict) else getattr(record, "starred", None)
    return None if value is None else bool(value)


def _company(record: Any) -> str:
    return str((record.get("company") if isinstance(record, dict) else getattr(record, "company", "")) or "")


def is_starred(record: Any, priority: Iterable[str]) -> str | bool:
    """The star the candidate sees: red for F500, amber for user star / priority / F1000, False otherwise."""
    val = _starred_value(record)
    if val is False:
        return False
    company = _company(record)
    if is_fortune_500(company):
        return "red"
    if val is True:
        return "amber"
    if is_priority(company, priority):
        return "amber"
    if is_fortune_1000(company):
        return "amber"
    return False


def held_from_bulk_send(record: Any, priority: Iterable[str]) -> bool:
    """Starred applications wait for their own Approve & send, never a batch send."""
    val = _starred_value(record)
    if val is False:
        return False
    if val is True:
        return True
    company = _company(record)
    if is_priority(company, priority):
        return True
    if is_fortune_500(company):
        return True
    return False


def held_from_automation(record: Any, priority: Iterable[str]) -> bool:
    """Unattended runs never apply to a priority company or a starred job."""
    val = _starred_value(record)
    if val is False:
        return is_priority(_company(record), priority)
    if val is True:
        return True
    company = _company(record)
    return is_priority(company, priority) or is_fortune_500(company)
