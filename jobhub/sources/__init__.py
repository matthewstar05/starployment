"""Job sources. Each exposes `fetch(settings) -> list[Job]` and is registered in SOURCES."""
from __future__ import annotations

from . import ashby, greenhouse, jobspy_source, lever, remoteok, simplify, workday

SOURCES = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workday": workday.fetch,
    "simplify": simplify.fetch,
    "remoteok": remoteok.fetch,
    "jobspy": jobspy_source.fetch,
}
