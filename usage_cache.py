"""Reads the plan usage Claude Code keeps in its state file, for when no terminal session's status line reports it.

    cache = UsageCache()              # ~/.claude.json
    data = cache.poll()               # once a second: a new fetch, or None
    if data:
        deck.feed("usage-cache", data, now)

Claude Code — the VS Code extension too — saves each fetch of the plan's limits under `cachedUsageUtilization`,
the figures its usage panel shows. The file also holds the account's details; it is parsed whole, only that entry
is kept, and only its two windows go on, in the status line's shape (DESIGN.md §3):

    {"fetched_at": seconds, "five_hour": {"used_percentage", "resets_at"}, "seven_day": {...}}

The file is read again only when it changes. Its format is not documented; it is read as Claude Code 2.1.195
writes it, and an entry it cannot read is skipped.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

STATE_FILE = os.path.expanduser("~/.claude.json")


def epoch(stamp) -> float | None:
    """`2026-10-29T23:20:00.168549+00:00` as seconds."""
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def usage_from(state: dict) -> dict | None:
    cached = state.get("cachedUsageUtilization") if isinstance(state, dict) else None
    if not isinstance(cached, dict) or not isinstance(cached.get("utilization"), dict):
        return None
    out = {"fetched_at": round(float(cached.get("fetchedAtMs") or 0) / 1000, 3)}
    for name in ("five_hour", "seven_day"):
        w = cached["utilization"].get(name)
        if isinstance(w, dict) and w.get("utilization") is not None:
            at = epoch(w.get("resets_at"))
            out[name] = {"used_percentage": w["utilization"], "resets_at": round(at) if at is not None else None}
    return out if len(out) > 1 else None


class UsageCache:
    def __init__(self, path: str = STATE_FILE):
        self.path, self.mtime, self.fetched = path, None, None

    def poll(self) -> dict | None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return None
        if mtime == self.mtime:
            return None
        self.mtime = mtime
        try:
            with open(self.path, encoding="utf-8") as f:
                data = usage_from(json.load(f))
        except (OSError, ValueError):
            self.mtime = None  # Claude Code may have been writing it: read it again next time
            return None
        if data is None or data["fetched_at"] == self.fetched:
            return None
        self.fetched = data["fetched_at"]
        return data
