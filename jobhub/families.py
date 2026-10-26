"""Role families by title, so automatic runs spread sends across kinds of work.

A candidate who targets software, data, quant, and finance roles should not
see every overnight send go to the largest pile (usually software). Pure
functions; the first matching family wins, so the order below matters
("Quantitative Analyst" is quant, not finance; "Financial Data Analyst" is
finance, not data).
"""
from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

FAMILIES: tuple[tuple[str, re.Pattern], ...] = tuple((name, re.compile(pattern, re.I)) for name, pattern in (
    ("Quant & trading", r"\bquant|\btrad(?:er|ing)\b|\bstrats?\b|algorithmic|market mak"),
    ("Accounting", r"accountant|accounting|\baudit|\btax\b|controller|bookkeep|payroll|revenue recognition|"
                   r"financial reporting|\bcpa\b|general ledger"),
    ("Finance & FP&A", r"fp&a|fp & a|financial (?:analyst|planning|analysis)|\bfinanc|treasury|investment|"
                       r"banking|equity research|\bcredit\b|portfolio|capital markets|valuation|private equity|"
                       r"corporate development|wealth|asset management|\bm&a\b|budget"),
    ("Risk & actuarial", r"actuar|\brisk\b|underwrit|insurance|reinsurance|catastrophe|model validation|compliance|"
                         r"\bfraud\b|security analyst"),
    ("Economics & research", r"econom|policy analyst|research (?:analyst|associate)|statistic"),
    ("Business & strategy", r"business (?:analyst|operations|systems|intelligence analyst)|operations analyst|"
                            r"strateg|consult|bizops|biz ops|product analyst|program analyst|implementation|"
                            r"product manager|pricing analyst|revenue operations|supply chain|procurement|"
                            r"\bdeal\b|business development|competitive intelligence|operations systems|"
                            r"business growth"),
    ("Data & ML", r"\bdata\b|analytics|machine learning|\bml\b|\bai\b|scientist|decision|business intelligence"),
    ("Software", r"software|engineer|developer|\bsre\b|devops|programmer|\bswe\b|technolog"),
))
OTHER = "Other"


@lru_cache(maxsize=65536)
def family(title: str) -> str:
    """The role family a job title belongs to, or ``Other``."""
    for name, pattern in FAMILIES:
        if pattern.search(title or ""):
            return name
    return OTHER


def interleave(items: Iterable[T], key: Callable[[T], str]) -> list[T]:
    """Round-robin across groups, keeping each group's own order.

    The group of the first item leads, then the next group to appear, and so on,
    so the best item overall still comes first.
    """
    groups: dict[str, list[T]] = defaultdict(list)
    order: list[str] = []
    for item in items:
        name = key(item)
        if name not in groups:
            order.append(name)
        groups[name].append(item)
    result: list[T] = []
    depth = 0
    while len(result) < sum(len(group) for group in groups.values()):
        for name in order:
            if depth < len(groups[name]):
                result.append(groups[name][depth])
        depth += 1
    return result
