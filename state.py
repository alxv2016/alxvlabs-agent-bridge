"""The bridge's state machine: Claude Code's hook and status line inputs in, the deck's records out.

    deck = Deck()
    records = deck.feed("hook", hook_input, now)          # now: seconds, any epoch
    records = deck.feed("status", status_line_input, now)
    records = deck.feed("subagent-status", input, now)
    records = deck.feed("transcript", input, now)          # what transcripts.py read for a hook
    records = deck.feed("usage-cache", input, now)         # what usage_cache.py read from Claude Code's state
    records = deck.tick(now)                               # once a second: usage's reset minutes

Pure and deterministic: the clock is passed in, so a recording replays into the same records
(DESIGN.md §3; the records are the firmware's AGENT_DECK §6). A record is a dict, one of:

    {"type": "agent", "id", "parent", "name", "state", "since_s", "detail", "context_pct", "tokens"}
    {"type": "remove", "id"}
    {"type": "usage", "five_hour_pct", "five_hour_resets_min", "seven_day_pct", "seven_day_resets_min"}
    {"type": "request", "id", "agent", "tool", "body", "cut"}
    {"type": "question", "id", "agent", "part", "tool", "question", "options", "multi"}
    {"type": "close", "id", "reason"}

with the field names and limits of `onx::ui::deck` (onx/deck.h); `state` is "working", "needs_you" or "done",
`reason` "answered_elsewhere" or "timed_out". An agent record is sent only when something but `since_s`
changed: the device counts `since_s` up itself from the record's arrival. A question goes out only while
`asks_questions` says the deck takes them, one part of its AskUserQuestion at a time, under one id (DESIGN.md §4).
"""

from __future__ import annotations

import math
import os

# onx/deck.h's limits.
NAME_CHARS = 40
DETAIL_CHARS = 64
TOOL_CHARS = 48
BODY_BYTES = 2048
QUESTION_CHARS = 240
OPTION_CHARS = 96
MAX_OPTIONS = 4
MAX_ID = 0xFFFF  # Agent::id is a uint16_t, non-zero

WORKING, NEEDS_YOU, DONE = "working", "needs_you", "done"

# Notification types that mean a session waits on the developer (DESIGN.md §3).
NEEDS_YOU_NOTICES = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}

# Tools whose permission request the deck cannot answer: allowing AskUserQuestion only lets the question be asked,
# and its answer is the option the developer picks (DESIGN.md §3). The deck gets the question at its PreToolUse, where
# an answer can be given (§4); a PermissionRequest after that means the terminal is asking.
TERMINAL_ONLY = {"AskUserQuestion"}

# A session's page before Claude Code has titled it.
SESSION_NAME = "Claude Code"

# Context windows the bridge cannot learn in the VS Code extension, which runs no status line: claude-opus-5-5's
# 1M is the developer's session at 664k tokens with 31% left before auto-compact `[R]` 2026-10-29. A window a status
# line teaches, or --context-window, comes first (DESIGN.md §3).
KNOWN_WINDOWS = {"claude-opus-5-5": 1_000_000}

# The faces carry ASCII, ° and • (DESIGN_TOKENS §6.4); every other character becomes ?.
EXTRA_CHARS = {"°", "•"}


def to_glass(text: str, multiline: bool = False) -> str:
    """`text` in the characters the faces carry; on one line unless `multiline`."""
    out = []
    for ch in str(text):
        if ch == "\n":
            out.append("\n" if multiline else " ")
        elif ch == "\t":
            out.append("  " if multiline else " ")
        elif ch == "\r":
            continue
        elif " " <= ch <= "~" or ch in EXTRA_CHARS:
            out.append(ch)
        else:
            out.append("?")
    s = "".join(out)
    return s if multiline else " ".join(s.split())


def fit(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: chars - 3] + "..."


def percent(value) -> int:
    """A share as the device's whole per cent, halves up."""
    return max(0, min(100, math.floor(float(value) + 0.5)))


def cut_body(body: str) -> tuple[str, bool]:
    """The body within BODY_BYTES of UTF-8, cut on a character, and whether it was cut."""
    data = body.encode()
    if len(data) <= BODY_BYTES:
        return body, False
    return data[:BODY_BYTES].decode(errors="ignore"), True


def tool_summary(tool: str, tool_input: dict) -> str:
    """What a tool call is doing, for an agent's detail line: `Bash: npm test`, `Edit: deck.cpp`."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    what = ""
    questions = ti.get("questions")
    if isinstance(questions, list) and questions and isinstance(questions[0], dict):
        return fit(to_glass(f"Question: {questions[0].get('question', '')}"), DETAIL_CHARS)
    if ti.get("command"):
        what = str(ti["command"]).strip().splitlines()[0] if str(ti["command"]).strip() else ""
    elif ti.get("file_path") or ti.get("notebook_path"):
        what = os.path.basename(str(ti.get("file_path") or ti.get("notebook_path")))
    elif ti.get("pattern"):
        what = str(ti["pattern"])
    elif ti.get("url"):
        what = str(ti["url"]).split("//", 1)[-1].split("/", 1)[0]
    elif ti.get("query"):
        what = str(ti["query"])
    elif ti.get("description"):
        what = str(ti["description"])
    return fit(to_glass(f"{tool}: {what}" if what else tool), DETAIL_CHARS)


def request_body(tool: str, tool_input: dict) -> str:
    """The whole of what a request asks, as the glass shows it (the firmware's AGENT_DECK §4.3)."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if "command" in ti:
        parts = [str(ti["command"])]
        if ti.get("description"):
            parts.append(f"# {ti['description']}")
        text = "\n".join(parts)
    elif tool in ("Edit", "MultiEdit") or "old_string" in ti:
        edits = ti.get("edits") or [ti]
        lines = [str(ti.get("file_path", ""))]
        for e in edits:
            lines += [f"- {line}" for line in str(e.get("old_string", "")).splitlines()]
            lines += [f"+ {line}" for line in str(e.get("new_string", "")).splitlines()]
        text = "\n".join(lines)
    elif "content" in ti and "file_path" in ti:
        text = f"{ti['file_path']}\n{ti['content']}"
    elif "url" in ti:
        text = "\n".join(str(ti[k]) for k in ("url", "prompt") if ti.get(k))
    else:
        text = "\n".join(f"{k}: {v}" for k, v in ti.items())
    return to_glass(text, multiline=True)


def question_parts(tool_input: dict) -> list | None:
    """AskUserQuestion's questions when the deck can ask them all, else None: then the terminal asks."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list) or not questions:
        return None
    for q in questions:
        options = q.get("options") if isinstance(q, dict) else None
        if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
            return None
        if not all(isinstance(o, dict) and o.get("label") for o in options):
            return None
    return questions


def question_record(rid: int, agent: int, questions: list, part: int) -> dict:
    """Part `part` of a call, on the glass: the header as its tool line, each option a card."""
    q = questions[part]
    tool = to_glass(q.get("header") or "Question")
    if len(questions) > 1:
        tool = f"{tool} {part + 1}/{len(questions)}"

    def card(o: dict) -> str:
        text = f"{o['label']}: {o['description']}" if o.get("description") else str(o["label"])
        return fit(to_glass(text), OPTION_CHARS)

    return {"type": "question", "id": rid, "agent": agent, "part": part, "tool": fit(tool, TOOL_CHARS),
            "question": fit(to_glass(q.get("question", "")), QUESTION_CHARS),
            "options": [card(o) for o in q["options"]], "multi": bool(q.get("multiSelect"))}


class Ask:
    """A call to AskUserQuestion on the deck: its questions as Claude Code sent them, and the answers so far."""

    def __init__(self, questions: list):
        self.questions, self.part, self.answers = questions, 0, {}


class Agent:
    def __init__(self, aid: int, parent: int, name: str, now: float):
        self.id, self.parent, self.name = aid, parent, name
        self.kind = ""  # A sub-agent's agent_type, kept when a description names its page
        self.state, self.since = DONE, now
        self.detail, self.context_pct, self.tokens = "", -1, 0
        self.has_status = False  # A status line has given its context: the transcript's is not needed
        self.sent = None  # the last record sent, less since_s

    def set_state(self, state: str, now: float):
        if state != self.state:
            self.state, self.since = state, now

    def record(self, now: float) -> dict:
        return {"type": "agent", "id": self.id, "parent": self.parent, "name": self.name, "state": self.state,
                "since_s": max(0, int(now - self.since)), "detail": self.detail,
                "context_pct": self.context_pct, "tokens": self.tokens}


def base_model(model_id: str) -> str:
    """`claude-opus-4-8[1m]` as the transcript names it, `claude-opus-4-8`."""
    return str(model_id).split("[", 1)[0]


class Deck:
    def __init__(self, context_window: int | None = None):
        # A window's size by model, learned from status lines; else `context_window`, the developer's, for a
        # session in the VS Code extension, which runs none (DESIGN.md §3).
        self.windows: dict[str, int] = {}
        self.context_window = context_window
        self.agents: dict[str, Agent] = {}   # session_id, or session_id/agent_id for a sub-agent
        self.requests: dict[int, tuple[str, str]] = {}  # request id -> (agent key, tool)
        self.request_records: dict[int, dict] = {}       # request id -> its record, for a snapshot
        self.asks: dict[int, Ask] = {}                   # request id -> the question it is
        self.tool_uses: dict[tuple[str, str], str] = {}  # (agent key, tool) -> the id of its latest PreToolUse
        self.request_uses: dict[int, str] = {}           # request id -> the tool call it holds
        self.asks_questions = False  # The attached deck takes "question" records: its hello's version says (§4)
        self.next_agent, self.next_request = 1, 1
        self.rate_limits: dict = {}
        self.rate_limits_at = 0.0  # When they were measured: the newer of a status line and the usage cache wins
        self.usage_sent = None

    # Ids -----------------------------------------------------------------------------------------------------

    def _new_id(self) -> int:
        used = {a.id for a in self.agents.values()}
        while self.next_agent in used or self.next_agent == 0:
            self.next_agent = self.next_agent % MAX_ID + 1
        aid, self.next_agent = self.next_agent, self.next_agent % MAX_ID + 1
        return aid

    def _session(self, sid: str, cwd: str, now: float) -> Agent:
        if sid not in self.agents:
            # Named as Claude Code titles the session once a hook, the status line or the transcript says.
            self.agents[sid] = Agent(self._new_id(), 0, SESSION_NAME, now)
        return self.agents[sid]

    def _subagent(self, sid: str, agent_id: str, agent_type: str, cwd: str, now: float) -> Agent:
        session = self._session(sid, cwd, now)
        key = f"{sid}/{agent_id}"
        if key not in self.agents:
            a = Agent(self._new_id(), session.id, fit(to_glass(agent_type or "agent"), NAME_CHARS), now)
            a.kind = agent_type
            a.set_state(WORKING, now)
            self.agents[key] = a
        return self.agents[key]

    def _actor(self, h: dict, now: float) -> tuple[str, Agent | None]:
        """The agent a hook speaks for: the sub-agent it fired in, or its session. None for an agent Claude Code
        runs for itself — an `agent_id` never started, with no `agent_type` (DESIGN.md §3)."""
        sid, cwd = h.get("session_id", ""), h.get("cwd", "")
        if h.get("agent_id"):
            key = f"{sid}/{h['agent_id']}"
            if key not in self.agents and not h.get("agent_type"):
                return key, None
            return key, self._subagent(sid, h["agent_id"], h.get("agent_type", ""), cwd, now)
        return sid, self._session(sid, cwd, now)

    # Records -------------------------------------------------------------------------------------------------

    def _changed(self, now: float) -> list[dict]:
        out = []
        for a in sorted(self.agents.values(), key=lambda a: a.id):
            r = a.record(now)
            key = {k: v for k, v in r.items() if k != "since_s"}
            if key != a.sent:
                a.sent = key
                out.append(r)
        return out

    def _close_for(self, key: str, reason: str = "answered_elsewhere", tool: str | None = None) -> list[dict]:
        """Closes the open requests `key` made — all, or the oldest for `tool`."""
        ids = [rid for rid, (k, t) in self.requests.items() if k == key and (tool is None or t == tool)]
        if tool is not None:
            ids = ids[:1]
        for rid in ids:
            self._forget(rid)
        return [{"type": "close", "id": rid, "reason": reason} for rid in ids]

    def _forget(self, rid: int) -> tuple | None:
        """Request `rid` gone from every table; what the requests table held, or None."""
        self.request_records.pop(rid, None)
        self.asks.pop(rid, None)
        self.request_uses.pop(rid, None)
        return self.requests.pop(rid, None)

    def _hold_use(self, rid: int, key: str, tool: str) -> None:
        use = self.tool_uses.get((key, tool))
        if use:
            self.request_uses[rid] = use

    def _ended_elsewhere(self, rids: list[int], now: float) -> list[dict]:
        """Requests `rids` settled with no hook to say so: closed, and their agents work on."""
        out = []
        for rid in rids:
            key, _ = self._forget(rid) or (None, None)
            if key is None:
                continue
            out.append({"type": "close", "id": rid, "reason": "answered_elsewhere"})
            if key in self.agents and not any(k == key for k, _ in self.requests.values()):
                self.agents[key].set_state(WORKING, now)
        return out

    def hook_gone(self, rid: int, now: float) -> list[dict]:
        """Claude Code stopped waiting on request `rid`'s hook: something else answered it."""
        return self._ended_elsewhere([rid], now) + self._changed(now)

    def _usage(self, now: float) -> list[dict]:
        def window(name):
            w = self.rate_limits.get(name)
            if not isinstance(w, dict) or w.get("used_percentage") is None:
                return -1, -1
            at = w.get("resets_at")
            if at is not None and float(at) <= now:
                # Past its reset the share is the old window's: unknown until a status line or a new fetch says,
                # as Claude Code drops a window once its resets_at passes (DESIGN.md §3).
                return -1, -1
            mins = math.ceil((float(at) - now) / 60) if at is not None else -1
            return percent(w["used_percentage"]), mins

        fh, sd = window("five_hour"), window("seven_day")
        u = {"type": "usage", "five_hour_pct": fh[0], "five_hour_resets_min": fh[1],
             "seven_day_pct": sd[0], "seven_day_resets_min": sd[1]}
        if u == self.usage_sent or not self.rate_limits:
            return []
        self.usage_sent = u
        return [u]

    # Inputs --------------------------------------------------------------------------------------------------

    def feed(self, kind: str, data: dict, now: float) -> list[dict]:
        if not isinstance(data, dict):
            return []
        if kind == "hook":
            out = self._hook(data, now)
        elif kind == "status":
            out = self._status(data, now)
        elif kind == "subagent-status":
            out = self._subagent_status(data, now)
        elif kind == "transcript":
            out = self._transcript(data, now)
        elif kind == "usage-cache":
            out = self._usage_cache(data)
        else:
            return []
        return out + self._changed(now) + self._usage(now)

    def tick(self, now: float) -> list[dict]:
        return self._usage(now)

    def snapshot(self, now: float) -> list[dict]:
        """Everything the device should show, after a reset: for a deck that has just started or come back."""
        agents = sorted(self.agents.values(), key=lambda a: (a.parent != 0, a.id))
        out = [{"type": "reset"}] + [a.record(now) for a in agents]
        if self.usage_sent is not None:
            out.append(self.usage_sent)
        return out + [self.request_records[rid] for rid in sorted(self.request_records)]

    def answered(self, rid: int, now: float) -> list[dict]:
        """The device answered request `rid` and closed it itself: its agent works on."""
        key, _ = self._forget(rid) or (None, None)
        if key is None:
            return []
        if key in self.agents and not any(k == key for k, _ in self.requests.values()):
            self.agents[key].set_state(WORKING, now)
        return self._changed(now)

    def answered_question(self, rid: int, part: int, chosen: int, now: float) -> tuple[list[dict], bool, dict | None]:
        """The device's answer to part `part` of question `rid`: bit n for option n, 0 to leave it to the terminal.
        Gives the records it makes, whether the call is settled, and the answers Claude Code gets, keyed by question
        as AskUserQuestion takes them; settled with none, the terminal asks. A resend of an old part changes nothing."""
        ask = self.asks.get(rid)
        if ask is None or part != ask.part:
            return [], False, None
        q = ask.questions[part]
        labels = [str(o["label"]) for i, o in enumerate(q["options"]) if chosen >> i & 1]
        if not labels:
            self._forget(rid)
            return [], True, None
        ask.answers[q.get("question", "")] = ", ".join(labels)
        ask.part += 1
        if ask.part < len(ask.questions):
            key, _ = self.requests[rid]
            record = question_record(rid, self.agents[key].id, ask.questions, ask.part)
            self.request_records[rid] = record
            return [record], False, None
        answers = ask.answers
        return self.answered(rid, now), True, answers

    def expire(self, rid: int, now: float) -> list[dict]:
        """Request `rid` held past the bridge's hold: the device closes it, and Claude Code asks in the terminal."""
        if self._forget(rid) is None:
            return []
        return [{"type": "close", "id": rid, "reason": "timed_out"}]

    def _hook(self, h: dict, now: float) -> list[dict]:
        event = h.get("hook_event_name", "")
        sid = h.get("session_id", "")
        if not sid:
            return []
        out: list[dict] = []

        if event == "SessionEnd":
            gone = [k for k in self.agents if k == sid or k.startswith(sid + "/")]
            for k in gone:
                out += self._close_for(k)
            # Sub-agents first, so the device never holds an orphan it did not need to.
            for k in sorted(gone, key=lambda k: k == sid):
                out.append({"type": "remove", "id": self.agents.pop(k).id})
            return out

        if event == "SubagentStart":
            a = self._subagent(sid, h.get("agent_id", ""), h.get("agent_type", ""), h.get("cwd", ""), now)
            a.set_state(WORKING, now)
            return out

        key, a = self._actor(h, now)
        if a is None:
            return out
        if h.get("session_title") and key == sid:
            a.name = fit(to_glass(h["session_title"]), NAME_CHARS)  # As Claude Code titles the session

        if event == "SessionStart":
            pass  # A page, idle until the first prompt
        elif event == "UserPromptSubmit":
            # A deny in the terminal fires no hook; one with feedback arrives as this prompt. A sub-agent's
            # request still open was denied, and the sub-agent carries on until its own SubagentStop.
            for k in [k for k in self.agents if k.startswith(sid + "/")]:
                closed = self._close_for(k)
                if closed:
                    self.agents[k].set_state(WORKING, now)
                out += closed
            out += self._close_for(key)
            a.set_state(WORKING, now)
            a.detail = "Thinking"
            if key == sid:
                # The session's sub-agents that finished go with the turn they worked in — the developer's choice,
                # 2026-10-29; one still running, in the background or after a deny, stays.
                for k in [k for k, x in self.agents.items() if k.startswith(sid + "/") and x.state == DONE]:
                    out.append({"type": "remove", "id": self.agents.pop(k).id})
        elif event == "PreToolUse":
            out += self._close_for(key)
            a.set_state(WORKING, now)
            a.detail = tool_summary(h.get("tool_name", ""), h.get("tool_input", {}))
            if h.get("tool_use_id"):
                self.tool_uses[(key, h.get("tool_name", ""))] = str(h["tool_use_id"])
            questions = question_parts(h.get("tool_input", {})) if h.get("tool_name") in TERMINAL_ONLY else None
            if questions and self.asks_questions:
                rid = self.next_request
                self.next_request = self.next_request % 0xFFFFFFFF + 1
                self.requests[rid] = (key, h["tool_name"])
                self._hold_use(rid, key, h["tool_name"])
                self.asks[rid] = Ask(questions)
                a.set_state(NEEDS_YOU, now)
                out += self._changed(now)  # The page shows Needs you before its question opens
                record = question_record(rid, a.id, questions, 0)
                self.request_records[rid] = record
                out.append(record)
        elif event in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            # The tool ran or was refused: a request it made was answered somewhere else. A sub-agent's
            # request is seen to carry agent_id (§15) though not documented to; looking on its session hedges.
            self.tool_uses.pop((key, h.get("tool_name", "")), None)
            closed = self._close_for(key, tool=h.get("tool_name"))
            if not closed and key != sid:
                closed = self._close_for(sid, tool=h.get("tool_name"))
                if closed and not any(k == sid for k, _ in self.requests.values()):
                    self.agents[sid].set_state(WORKING, now)
            out += closed
            if not any(k == key for k, _ in self.requests.values()):
                a.set_state(WORKING, now)
        elif event == "PermissionRequest" and h.get("tool_name", "") in TERMINAL_ONLY:
            a.set_state(NEEDS_YOU, now)
            a.detail = tool_summary(h.get("tool_name", ""), h.get("tool_input", {}))
        elif event == "PermissionRequest":
            tool = h.get("tool_name", "")
            body, cut = cut_body(request_body(tool, h.get("tool_input", {})))
            rid = self.next_request
            self.next_request = self.next_request % 0xFFFFFFFF + 1
            self.requests[rid] = (key, tool)
            self._hold_use(rid, key, tool)
            a.set_state(NEEDS_YOU, now)
            a.detail = tool_summary(tool, h.get("tool_input", {}))
            out += self._changed(now)  # The page shows Needs you before its request opens
            record = {"type": "request", "id": rid, "agent": a.id, "tool": fit(to_glass(tool), TOOL_CHARS),
                      "body": body, "cut": cut}
            self.request_records[rid] = record
            out.append(record)
        elif event == "Notification":
            # A notice carries no agent_id: the terminal's prompt for a request already shown is the request's
            # own agent waiting, not the session.
            asking = any(k == sid or k.startswith(sid + "/") for k, _ in self.requests.values())
            if h.get("notification_type") in NEEDS_YOU_NOTICES and not asking:
                a.set_state(NEEDS_YOU, now)
        elif event in ("Stop", "SubagentStop"):
            out += self._close_for(key)
            a.set_state(DONE, now)
            if a.detail == "Thinking":
                a.detail = ""  # A turn with no tool call leaves nothing it did
        return out

    def _status(self, s: dict, now: float) -> list[dict]:
        sid = s.get("session_id", "")
        if not sid:
            return []
        a = self._session(sid, s.get("cwd", ""), now)
        if s.get("session_name"):
            a.name = fit(to_glass(s["session_name"]), NAME_CHARS)
        cw = s.get("context_window") if isinstance(s.get("context_window"), dict) else {}
        model = s.get("model") if isinstance(s.get("model"), dict) else {}
        if cw.get("context_window_size") and model.get("id"):
            self.windows[base_model(model["id"])] = int(cw["context_window_size"])
        if cw.get("used_percentage") is not None:
            a.context_pct = percent(cw["used_percentage"])
            a.has_status = True
        if isinstance(s.get("rate_limits"), dict) and s["rate_limits"]:
            self.rate_limits, self.rate_limits_at = s["rate_limits"], now
        return []

    def _subagent_status(self, s: dict, now: float) -> list[dict]:
        """Nothing: the transcript gives a sub-agent's tokens, in every session, the status line or not."""
        return []

    def _transcript(self, t: dict, now: float) -> list[dict]:
        sid, agent = t.get("session_id", ""), t.get("agent_id", "")
        a = self.agents.get(f"{sid}/{agent}" if agent else sid)
        if a is None:
            return []
        if t.get("tool_results"):
            ended = set(t["tool_results"])
            return self._ended_elsewhere([r for r, use in self.request_uses.items() if use in ended], now)
        if t.get("description") and agent:
            a.name = fit(to_glass(t["description"]), NAME_CHARS)  # As Claude Code's agent map titles it
        if t.get("title") and not agent:
            a.name = fit(to_glass(t["title"]), NAME_CHARS)  # As Claude Code's session list titles it
        context = t.get("context_tokens")
        if context is not None:
            # As Claude Code's agent map counts them: a session's context, a sub-agent's with its last response.
            a.tokens = max(0, int(context) + (int(t.get("response_tokens") or 0) if agent else 0))
        if context is None or agent or a.has_status:
            return []
        model = base_model(t.get("model", ""))
        window = self.windows.get(model) or self.context_window or KNOWN_WINDOWS.get(model)
        if window:
            a.context_pct = percent(int(context) * 100 / window)
        return []

    def _usage_cache(self, u: dict) -> list[dict]:
        at = float(u.get("fetched_at") or 0)
        limits = {k: u[k] for k in ("five_hour", "seven_day") if isinstance(u.get(k), dict)}
        if limits and at >= self.rate_limits_at:
            self.rate_limits, self.rate_limits_at = limits, at
        return []
