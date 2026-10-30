#!/usr/bin/env python3
"""Cleans a recording for the repo: ids, paths, prompts, commands and file contents replaced.

    scrub.py RECORDING.jsonl OUT.inputs.jsonl [--names web-shop,docs-site,...]

What `bridge.py --record` writes holds prompts, commands, file contents and paths as Claude Code sent them.
This keeps the shape — events, order, timing, tools, states, numbers, line and character counts — and
replaces the rest:

- ids (session, agent, prompt, tool use, task) by `session-1`, `agent-1`, … the same value the same each time;
- each project folder by `/Users/dev/<name>` from --names, the home folder by `/Users/dev`, a file's own name kept;
- every id, project folder, home folder and the user's name again wherever else a string holds one;
- free text (prompts, commands, contents, messages, queries, URLs) letter by letter with `x`, digits with `0`;
- a tool's output (`tool_response`) and the transcript's last message dropped;
- times moved to start at 1000 s; usage's reset times moved with them.

It prints every string key it kept untouched. Read OUT before committing it (DESIGN.md §2).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

ID_KEYS = {"session_id": "session", "agent_id": "agent", "prompt_id": "prompt", "tool_use_id": "tool"}
PATH_KEYS = {"cwd", "transcript_path", "agent_transcript_path", "project_dir", "current_dir", "file_path",
             "notebook_path", "scratchpad_dir", "git_worktree", "path"}
TEXT_KEYS = {"prompt", "command", "content", "old_string", "new_string", "description", "query", "url", "pattern",
             "message", "title", "session_title", "session_name", "label", "reason_text", "name", "prompt_text",
             "last_assistant_message"}
DROP_KEYS = {"tool_response", "last_assistant_message", "background_tasks", "session_crons", "repo", "pr",
             "permission_suggestions", "added_dirs"}
# Values Claude Code defines, kept as they are.
KEEP_KEYS = {"hook_event_name", "tool_name", "agent_type", "agentType", "notification_type", "source", "reason",
             "permission_mode", "status", "type", "id_kind", "display_name", "level", "mcp_server", "version",
             "model", "style", "mode", "subagent_type"}
HOME = os.path.expanduser("~")
USER = os.path.basename(HOME)


class Scrubber:
    def __init__(self, names: list[str]):
        self.names, self.projects, self.ids, self.kept = names, {}, {}, set()

    def fake_id(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self.ids:
            self.ids[key] = f"{kind}-{sum(1 for k in self.ids if k[0] == kind) + 1}"
        return self.ids[key]

    def project(self, root: str) -> None:
        if root and root not in self.projects and root.startswith(HOME):
            i = len(self.projects)
            self.projects[root] = f"/Users/dev/{self.names[i] if i < len(self.names) else f'project-{i + 1}'}"

    def path(self, p: str) -> str:
        for root, fake in sorted(self.projects.items(), key=lambda kv: -len(kv[0])):
            if p == root or p.startswith(root + "/"):
                return fake + p[len(root):]
        if p.startswith(HOME):
            head, base = os.path.split(p)
            return "/Users/dev/" + re.sub(r"[^/]+", "x", head[len(HOME):].strip("/")).strip("/") + "/" + base
        return p

    @staticmethod
    def mask(text: str) -> str:
        return re.sub(r"[0-9]", "0", re.sub(r"[^\W\d_]", "x", text))

    def collect(self, value, key: str = "", parent: str = "") -> None:
        """Gives every id its fake before any string is rewritten, so an id inside a path is replaced too."""
        if isinstance(value, dict):
            for k, v in value.items():
                self.collect(v, k, key)
        elif isinstance(value, list):
            for v in value:
                self.collect(v, key, parent)
        elif isinstance(value, str) and (key in ID_KEYS or (key == "id" and parent == "tasks")):
            self.fake_id(ID_KEYS.get(key, "agent"), value)

    def everywhere(self, text: str) -> str:
        """Ids, project folders — also as Claude Code's dashed folder names — the home folder and the user's
        name replaced wherever they sit in a string."""
        for (_, real), fake in self.ids.items():
            if len(real) >= 8:
                text = text.replace(real, fake)
        for root, fake in sorted(self.projects.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(root, fake).replace(root.replace("/", "-"), fake.replace("/", "-"))
        return text.replace(HOME, "/Users/dev").replace(USER, "dev")

    def walk(self, value, key: str = "", parent: str = ""):
        if isinstance(value, dict):
            return {k: self.walk(v, k, key) for k, v in value.items() if k not in DROP_KEYS}
        if isinstance(value, list):
            return [self.walk(v, key, parent) for v in value]
        if not isinstance(value, str):
            return value
        if key in ID_KEYS:
            return self.fake_id(ID_KEYS[key], value)
        if key == "id":
            # A subagent status line task's id; the model's id is Claude Code's own
            return self.fake_id("agent", value) if parent == "tasks" else value
        if key in PATH_KEYS:
            return self.everywhere(self.path(value))
        if key in TEXT_KEYS:
            return self.mask(self.everywhere(value))  # Paths at their fake lengths before masking
        if key not in KEEP_KEYS:
            self.kept.add(key)
        return self.everywhere(value)


def shift(item: dict, offset: float) -> dict:
    item["t"] = round(item["t"] + offset, 3)
    data = item["input"]
    limits = (data.get("rate_limits") or {}) if item["kind"] == "status" else {}
    if item["kind"] == "usage-cache":
        limits = {k: v for k, v in data.items() if k in ("five_hour", "seven_day")}
        data["fetched_at"] = round(data.get("fetched_at", 0) + offset, 3)
    for w in limits.values():
        if isinstance(w, dict) and w.get("resets_at") is not None:
            w["resets_at"] = round(w["resets_at"] + offset)
    return item


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recording")
    ap.add_argument("out")
    ap.add_argument("--names", default="web-shop,docs-site,api-server,notes-app")
    args = ap.parse_args()

    with open(args.recording, encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]
    if not items:
        sys.exit("scrub: the recording is empty")
    s = Scrubber(args.names.split(","))
    for item in items:
        data = item["input"]
        s.project((data.get("workspace") or {}).get("project_dir", ""))
        s.project(data.get("cwd", ""))
        s.collect(data)
    offset = 1000.0 - float(items[0]["t"])
    with open(args.out, "w", encoding="utf-8") as f:
        for item in items:
            item = shift(item, offset)
            f.write(json.dumps({"t": item["t"], "kind": item["kind"], "input": s.walk(item["input"])}) + "\n")
    print(f"scrub: {len(items)} inputs to {args.out}; {len(s.projects)} projects, {len(s.ids)} ids replaced")
    if s.kept:
        print("scrub: kept as they were — check each:", ", ".join(sorted(s.kept)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
