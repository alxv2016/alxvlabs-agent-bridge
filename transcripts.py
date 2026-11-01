"""Reads Claude Code's transcripts: each agent's tokens, model and effort, a session's title and context size,
and a sub-agent's description.

    reader = Transcripts()
    for extra in reader.observe(hook_input):     # after each hook
        deck.feed("transcript", extra, now)

The hooks point at the files: `transcript_path` is the session's; a sub-agent's is
`<transcript dir>/<session_id>/subagents/agent-<agent_id>.jsonl`, with its `.meta.json` beside it. Each is read
from where the last read stopped. What it yields is an input like any other, so `bridge.py --record` keeps it and
a recording replays with no transcript on disk (DESIGN.md §3):

    {"session_id", "agent_id"?, "model", "context_tokens", "response_tokens", "effort"?}
        # the latest request's input and cache tokens; its output tokens, thinking included; its effort level
    {"session_id", "title"}                                   # the session's title, as Claude Code names it
    {"session_id", "agent_id", "description"}                 # a sub-agent's, once
    {"session_id", "agent_id"?, "tool_results"}               # the tool calls that ended: their tool_use ids, a refused
                                                              # one's too, which no hook reports (DESIGN.md §4)
    {"session_id", "agent_id"?, "interrupted"}                # when a refusal or Esc ended the turn, in seconds since
                                                              # the epoch, which no hook reports either (DESIGN.md §3)

The transcript's format is not documented; it is read as Claude Code 2.1.195 writes it, and a line it cannot read
is skipped.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

INTERRUPTED = "[Request interrupted by user"  # Claude Code's own line, `[R]` 2026-10-31 on Claude Code 2.1.314


def context_tokens(usage: dict) -> int:
    """A request's context: its input, the cache it wrote and the cache it read — what Claude Code's subagent
    status line calls `tokenCount`, within a few tokens (DESIGN.md §3)."""
    return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens"))


def interrupted_at(d: dict) -> float | None:
    """When a transcript line says the developer interrupted the turn, in seconds since the epoch, else None."""
    if d.get("type") != "user" or not isinstance(d.get("message"), dict):
        return None
    content = d["message"].get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    if not any(isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).startswith(INTERRUPTED)
               for b in blocks):
        return None
    try:
        return datetime.fromisoformat(str(d.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Tail:
    """A JSON lines file read from where the last read stopped, a partly written last line kept for the next."""

    def __init__(self, path: str):
        self.path, self.offset, self.rest = path, 0, b""

    def lines(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        if len(data) == 0:
            return []
        self.offset += len(data)
        data = self.rest + data
        whole, _, self.rest = data.rpartition(b"\n")
        out = []
        for raw in whole.split(b"\n"):
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
        return out


class Transcripts:
    def __init__(self):
        self.tails: dict[str, Tail] = {}
        self.sent: dict[tuple, tuple] = {}            # (session, agent) -> the last (model, context, response) yielded
        self.titles: dict[str, str] = {}
        self.described: set[tuple] = set()

    def _read(self, path: str, own: bool) -> tuple[tuple | None, str | None, list[str], float | None]:
        """What a transcript adds since the last read: (model, context, output tokens, effort) of its newest response, a
        new title, the tool calls that ended, and when an interrupt no response came after ended the turn. A response's
        lines carry its final count `[R]` 2026-10-30. A `<synthetic>` line is Claude Code's own, no response. A sub-agent's
        own file marks every line `isSidechain`; in a session's file such a line is a sub-agent's (`own` False)."""
        tail = self.tails.setdefault(path, Tail(path))
        latest, title = None, None
        newest_id, written = None, 0
        ended: list[str] = []
        interrupted = None
        for d in tail.lines():
            if not isinstance(d, dict) or (d.get("isSidechain") and not own):
                continue
            if d.get("type") == "ai-title" and d.get("aiTitle"):
                title = str(d["aiTitle"])
            if d.get("type") == "assistant":
                interrupted = None
            at = interrupted_at(d)
            interrupted = at if at is not None else interrupted
            m = d.get("message")
            if d.get("type") == "user" and isinstance(m, dict) and isinstance(m.get("content"), list):
                ended += [str(b["tool_use_id"]) for b in m["content"] if isinstance(b, dict)
                          and b.get("type") == "tool_result" and b.get("tool_use_id")]
            if d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict):
                if str(m.get("model") or "").startswith("<"):
                    continue
                try:
                    out = int(m["usage"].get("output_tokens") or 0)
                    context = context_tokens(m["usage"])
                except (TypeError, ValueError):
                    continue  # The format is undocumented: a line it cannot read is skipped
                if m.get("id") != newest_id or newest_id is None:
                    newest_id, written = m.get("id"), out
                else:
                    written = max(written, out)
                latest = (str(m.get("model") or ""), context, written, str(d.get("effort") or ""))
        return latest, title, ended, interrupted

    def _context(self, sid: str, agent: str, path: str) -> list[dict]:
        latest, title, ended, interrupted = self._read(path, own=bool(agent))
        out = []
        whose = {"agent_id": agent} if agent else {}
        if title and not agent and self.titles.get(sid) != title:
            self.titles[sid] = title
            out.append({"session_id": sid, "title": title})
        if ended:
            out.append({"session_id": sid, "tool_results": ended, **whose})
        if interrupted is not None:
            out.append({"session_id": sid, "interrupted": interrupted, **whose})
        if latest is None or self.sent.get((sid, agent)) == latest:
            return out
        self.sent[(sid, agent)] = latest
        rec = {"session_id": sid, "model": latest[0], "context_tokens": latest[1], "response_tokens": latest[2]}
        if latest[3]:
            rec["effort"] = latest[3]
        if agent:
            rec["agent_id"] = agent
        return out + [rec]

    def observe(self, h: dict) -> list[dict]:
        sid, path = h.get("session_id") or "", h.get("transcript_path") or ""
        if not sid or not path or h.get("hook_event_name") == "SessionEnd":
            return []
        out = self._context(sid, "", path)
        agent = h.get("agent_id") or ""
        if agent and h.get("agent_type"):
            base = os.path.join(os.path.dirname(path), sid, "subagents", f"agent-{agent}")
            out += self._context(sid, agent, h.get("agent_transcript_path") or base + ".jsonl")
            if (sid, agent) not in self.described:
                try:
                    with open(base + ".meta.json", encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    meta = {}
                if isinstance(meta, dict) and meta.get("description"):
                    self.described.add((sid, agent))
                    out.append({"session_id": sid, "agent_id": agent, "description": str(meta["description"])})
        return out
