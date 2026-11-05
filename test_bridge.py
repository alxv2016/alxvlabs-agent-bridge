"""The bridge's checks, with no board and no Claude Code (DESIGN.md §2).

    python3 -m unittest discover -s .

Every fixtures/<name>.inputs.jsonl replays into its <name>.records.jsonl; then the rules one by one, the
installer on a scratch settings file, joining a network from the deck on a made-up macOS, and hook.py through a
running bridge.
"""

from __future__ import annotations

import contextlib
import glob
import io
import ssl
import shutil
import hmac
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bridge  # noqa: E402
import hook  # noqa: E402
import install  # noqa: E402
import pairing  # noqa: E402
import plugin_state  # noqa: E402
from replay import load, replay  # noqa: E402
from state import BODY_BYTES, STALE_S, Deck, cut_body, model_name, to_glass  # noqa: E402
from transcripts import Transcripts  # noqa: E402
from usage_cache import UsageCache  # noqa: E402
from usage_refresh import UsageRefresh  # noqa: E402
import serial_link  # noqa: E402
import wifi  # noqa: E402
import net_link  # noqa: E402

# The paired decks' ids in a scratch file: a test never writes the developer's own.
pairing.IDS_PATH = os.path.join(tempfile.mkdtemp(prefix="bridge-test-"), "paired.json")


def h(event, sid="s1", cwd="/Users/dev/web-shop", **kw):
    return {"session_id": sid, "cwd": cwd, "hook_event_name": event, **kw}


class Fixtures(unittest.TestCase):
    def test_every_recording_replays_into_its_records(self):
        inputs = sorted(glob.glob(os.path.join(HERE, "fixtures", "*.inputs.jsonl")))
        self.assertTrue(inputs)
        for path in inputs:
            with self.subTest(os.path.basename(path)):
                self.assertEqual(replay(load(path)), load(path.replace(".inputs.", ".records.")))


class Rules(unittest.TestCase):
    def test_characters_the_faces_lack_become_question_marks(self):
        self.assertEqual(to_glass("café • 25°\tx\ny"), "caf? • 25° x y")
        self.assertEqual(to_glass("a\r\n\tb", multiline=True), "a\n  b")

    def test_a_body_is_cut_on_a_character_at_the_cap(self):
        body, cut = cut_body("a" * (BODY_BYTES - 1) + "•")
        self.assertTrue(cut)
        self.assertEqual(body, "a" * (BODY_BYTES - 1))
        self.assertEqual(cut_body("a" * BODY_BYTES), ("a" * BODY_BYTES, False))

    def test_a_request_answered_in_the_terminal_closes(self):
        d = Deck()
        d.feed("hook", h("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}), 0)
        out = d.feed("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}), 1)
        self.assertEqual([r["type"] for r in out], ["agent", "request"])
        self.assertEqual(out[0]["state"], "needs_you")
        out = d.feed("hook", h("PostToolUse", tool_name="Bash"), 2)
        self.assertEqual(out[0], {"type": "close", "id": 1, "reason": "answered_elsewhere"})
        self.assertEqual(out[1]["state"], "working")

    def test_a_sub_agents_tool_closes_a_request_on_its_session(self):
        d = Deck()
        d.feed("hook", h("SubagentStart", agent_id="a1", agent_type="Explore"), 0)
        d.feed("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}), 1)
        out = d.feed("hook", h("PostToolUse", agent_id="a1", agent_type="Explore", tool_name="Bash"), 2)
        self.assertIn({"type": "close", "id": 1, "reason": "answered_elsewhere"}, out)

    def test_the_terminals_prompt_for_a_sub_agents_request_leaves_the_session_be(self):
        d = Deck()
        d.feed("hook", h("SubagentStart", agent_id="a1", agent_type="Explore"), 0)
        d.feed("hook", h("Stop"), 1)
        d.feed("hook", h("PermissionRequest", agent_id="a1", tool_name="Bash", tool_input={"command": "ls"}), 2)
        d.feed("hook", h("Notification", notification_type="permission_prompt"), 3)
        self.assertEqual((d.agents["s1"].state, d.agents["s1/a1"].state), ("done", "needs_you"))

    def test_a_request_denied_in_the_terminal_closes_at_the_next_prompt(self):
        d = Deck()
        d.feed("hook", h("SubagentStart", agent_id="a1", agent_type="Explore"), 0)
        d.feed("hook", h("PermissionRequest", agent_id="a1", tool_name="Bash", tool_input={"command": "ls"}), 1)
        out = d.feed("hook", h("UserPromptSubmit"), 2)
        self.assertEqual(out[0], {"type": "close", "id": 1, "reason": "answered_elsewhere"})
        self.assertEqual(d.agents["s1/a1"].state, "working")

    def test_an_agent_claude_code_runs_for_itself_makes_no_page(self):
        d = Deck()
        d.feed("hook", h("Stop"), 0)
        out = d.feed("hook", h("SubagentStop", agent_id="helper", agent_type=""), 1)
        self.assertEqual(out, [])
        self.assertEqual(list(d.agents), ["s1"])

    def test_a_question_waits_on_the_developer_with_no_request(self):
        d = Deck()
        out = d.feed("hook", h("PermissionRequest", tool_name="AskUserQuestion",
                               tool_input={"questions": [{"question": "Approve the test?"}]}), 0)
        self.assertEqual([r["type"] for r in out], ["agent"])
        self.assertEqual((out[0]["state"], out[0]["detail"]), ("needs_you", "Question: Approve the test?"))

    def test_a_session_is_named_by_claude_codes_title_else_claude_code(self):
        d = Deck()
        out = d.feed("hook", h("SessionStart"), 0)
        self.assertEqual(out[0]["name"], "Claude Code")
        self.assertEqual(d.feed("hook", h("UserPromptSubmit", session_title="Fix the build"), 1)[0]["name"],
                         "Fix the build")
        self.assertEqual(d.feed("status", {"session_id": "s1", "session_name": "renamed"}, 2)[0]["name"], "renamed")
        self.assertEqual(d.feed("transcript", {"session_id": "s1", "title": "Next animation work"}, 3)[0]["name"],
                         "Next animation work")

    def test_the_status_lines_tokens_are_context_not_what_was_written(self):
        d = Deck()
        out = d.feed("status", {"session_id": "s1", "context_window": {"used_percentage": 8,
                                                                       "total_input_tokens": 15500}}, 0)
        self.assertEqual((out[0]["context_pct"], out[0]["tokens"]), (8, 0))
        d.feed("hook", h("SubagentStart", agent_id="a1", agent_type="Explore"), 1)
        self.assertEqual(d.feed("subagent-status", {"session_id": "s1", "tasks": [{"id": "a1", "tokenCount": 900}]},
                                2), [])

    def test_the_next_prompt_clears_the_sub_agents_that_finished(self):
        d = Deck()
        d.feed("hook", h("SubagentStart", agent_id="a1", agent_type="Explore"), 0)
        d.feed("hook", h("SubagentStart", agent_id="a2", agent_type="Explore"), 0)
        d.feed("hook", h("SubagentStop", agent_id="a1", agent_type="Explore"), 1)
        out = d.feed("hook", h("UserPromptSubmit"), 2)
        self.assertIn({"type": "remove", "id": 2}, out)
        self.assertEqual(sorted(d.agents), ["s1", "s1/a2"])  # the one still running stays

    def test_a_deck_too_old_for_idle_gets_done_and_no_active(self):
        d = Deck()
        d.shows_idle = False
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("Stop", sid="a"), 1)
        out = d.feed("hook", h("UserPromptSubmit", sid="b"), 2)
        previous = [r for r in out if r["type"] == "agent" and r["id"] == d.agents["a"].id]
        self.assertEqual(previous, [])  # nothing it can show has changed
        records = [r for r in d.snapshot(3) if r["type"] == "agent"]
        self.assertEqual({r["state"] for r in records}, {"done", "working"})
        self.assertFalse(any("active" in r for r in records))

    def test_a_previous_chat_interrupted_goes_idle_when_it_stays_quiet(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("SubagentStart", sid="a", agent_id="s", agent_type="Plan"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        self.assertEqual(d.tick(STALE_S - 1), [])
        sub = d.agents["a/s"].id
        out = d.tick(STALE_S)
        self.assertIn({"type": "remove", "id": sub}, out)
        self.assertEqual(d.agents["a"].state, "idle")
        self.assertEqual(d.agents["b"].state, "working")  # the active chat is never swept

    def test_a_previous_chats_open_request_keeps_it_from_the_sweep(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        d.feed("hook", h("PermissionRequest", sid="a", tool_name="Bash", tool_input={"command": "ls"}), 2)
        d.agents["a"].state = "working"
        d.tick(STALE_S + 3)
        self.assertEqual(d.agents["a"].state, "working")

    def test_a_sub_agents_hooks_keep_its_session_from_the_sweep(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("SubagentStart", sid="a", agent_id="s", agent_type="Plan"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        d.feed("hook", h("PreToolUse", sid="a", agent_id="s", agent_type="Plan", tool_name="Read", tool_input={}), STALE_S)
        d.tick(STALE_S + 1)
        self.assertEqual(d.agents["a"].state, "working")

    def test_an_allow_late_in_its_hold_keeps_the_agent_from_the_sweep(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        d.feed("hook", h("PermissionRequest", sid="a", tool_name="Bash", tool_input={"command": "ls"}), 2)
        d.answered(next(iter(d.requests)), STALE_S + 2)
        d.tick(STALE_S + 3)
        self.assertEqual(d.agents["a"].state, "working")

    def test_a_sweep_clears_thinking_and_a_grey_chats_new_sub_agent_is_grey(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        d.feed("hook", h("SessionEnd", sid="b", reason="other"), 2)
        d.feed("hook", h("SubagentStart", sid="a", agent_id="s", agent_type="Plan"), 2)
        self.assertFalse(d.agents["a/s"].active)
        d.feed("hook", h("SubagentStop", sid="a", agent_id="s", agent_type="Plan"), 3)
        d.tick(STALE_S + 4)
        self.assertEqual((d.agents["a"].state, d.agents["a"].detail), ("idle", ""))

    def test_the_active_chat_ending_leaves_the_next_one_orange(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        d.feed("hook", h("SessionEnd", sid="b", reason="other"), 2)
        d.feed("hook", h("SessionStart", sid="c"), 3)
        self.assertTrue(d.agents["c"].active)
        self.assertFalse(d.agents["a"].active)  # a previous chat stays one until it is prompted

    def test_a_prompt_makes_its_chat_the_active_one_and_a_done_one_idle(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("Stop", sid="a"), 1)
        self.assertNotIn("active", d.agents["a"].record(1))  # the chat in use says nothing, as an older record
        out = d.feed("hook", h("UserPromptSubmit", sid="b"), 2)
        previous = [r for r in out if r["type"] == "agent" and r["id"] == d.agents["a"].id]
        self.assertEqual([(r["state"], r["active"]) for r in previous], [("idle", False)])
        self.assertEqual(d.agents["b"].state, "working")
        self.assertNotIn("active", d.agents["b"].record(2))

    def test_a_previous_chat_prompted_takes_over_again(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("Stop", sid="a"), 1)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 2)
        d.feed("hook", h("Stop", sid="b"), 3)
        d.feed("hook", h("UserPromptSubmit", sid="a"), 4)
        self.assertEqual((d.agents["a"].state, d.agents["a"].active), ("working", True))
        self.assertEqual((d.agents["b"].state, d.agents["b"].active), ("idle", False))

    def test_a_previous_chat_still_at_work_keeps_its_state_and_ends_idle(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        self.assertEqual((d.agents["a"].state, d.agents["a"].active), ("working", False))
        out = d.feed("hook", h("Stop", sid="a"), 2)
        self.assertEqual([r["state"] for r in out if r["type"] == "agent"], ["idle"])

    def test_a_chat_taken_over_loses_its_finished_sub_agents_and_greys_the_rest(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("SubagentStart", sid="a", agent_id="done", agent_type="Explore"), 0)
        d.feed("hook", h("SubagentStart", sid="a", agent_id="busy", agent_type="Plan"), 0)
        d.feed("hook", h("SubagentStop", sid="a", agent_id="done", agent_type="Explore"), 1)
        gone = d.agents["a/done"].id
        out = d.feed("hook", h("UserPromptSubmit", sid="b"), 2)
        self.assertIn({"type": "remove", "id": gone}, out)
        self.assertEqual((d.agents["a/busy"].state, d.agents["a/busy"].active), ("working", False))

    def test_a_previous_chats_sub_agent_leaves_as_it_finishes(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("SubagentStart", sid="a", agent_id="busy", agent_type="Plan"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        gone = d.agents["a/busy"].id
        out = d.feed("hook", h("SubagentStop", sid="a", agent_id="busy", agent_type="Plan"), 2)
        self.assertIn({"type": "remove", "id": gone}, out)
        self.assertNotIn("a/busy", d.agents)

    def test_a_previous_chats_request_still_opens_and_a_new_chat_appears_grey(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a"), 0)
        d.feed("hook", h("UserPromptSubmit", sid="b"), 1)
        out = d.feed("hook", h("PermissionRequest", sid="a", tool_name="Bash", tool_input={"command": "ls"}), 2)
        self.assertEqual([r["agent"] for r in out if r["type"] == "request"], [d.agents["a"].id])
        self.assertEqual((d.agents["a"].state, d.agents["a"].active), ("needs_you", False))
        d.feed("hook", h("SessionStart", sid="c"), 3)
        self.assertEqual((d.agents["c"].state, d.agents["c"].active), ("idle", False))

    def test_a_session_ending_removes_its_sub_agents_first(self):
        d = Deck()
        d.feed("hook", h("SubagentStart", agent_id="a1", agent_type="Explore"), 0)
        out = d.feed("hook", h("SessionEnd", reason="other"), 1)
        self.assertEqual(out, [{"type": "remove", "id": 2}, {"type": "remove", "id": 1}])

    def test_ids_skip_those_in_use_and_never_zero(self):
        d = Deck()
        d.next_agent = 0xFFFF
        d.feed("hook", h("SessionStart", sid="a"), 0)
        d.feed("hook", h("SessionStart", sid="b"), 0)
        self.assertEqual((d.agents["a"].id, d.agents["b"].id), (0xFFFF, 1))

    def test_a_window_past_its_reset_is_unknown_not_zero_minutes(self):
        d = Deck()
        d.feed("status", {"session_id": "s1", "rate_limits": {
            "five_hour": {"used_percentage": 20, "resets_at": 120},
            "seven_day": {"used_percentage": 61, "resets_at": 99999}}}, 0)
        out = d.tick(121)
        self.assertEqual((out[0]["five_hour_pct"], out[0]["five_hour_resets_min"]), (-1, -1))
        self.assertEqual(out[0]["seven_day_pct"], 61)

    def test_usage_is_sent_again_only_as_its_minutes_change(self):
        d = Deck()
        d.feed("status", {"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 50,
                                                                           "resets_at": 600}}}, 0)
        self.assertEqual(d.tick(30), [])
        self.assertEqual(d.tick(61)[0]["five_hour_resets_min"], 9)


class OwnPrompts(unittest.TestCase):
    """Prompts Claude Code submits itself, in the hook orders of real sessions `[R]` 2026-10-31 (DESIGN.md §3)."""

    NOTICE = "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n</task-notification>"
    PEER = 'Another Claude session sent a message:\n<agent-message from="a1">[Subagent report]</agent-message>'

    def test_a_sub_agents_request_allowed_late_ends_with_nothing_at_work(self):
        d = Deck()
        sub = {"agent_id": "a1", "agent_type": "general-purpose"}
        d.feed("hook", h("UserPromptSubmit", prompt="run a test"), 0)
        d.feed("hook", h("SubagentStart", **sub), 2)
        d.feed("hook", h("Stop"), 3)  # Run in the background: the session's turn ends at once
        d.feed("hook", h("PreToolUse", tool_name="Bash", tool_use_id="t1", tool_input={"command": "ssh -V"}, **sub), 4)
        d.feed("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ssh -V"}, **sub), 5)
        d.feed("hook", h("Notification", notification_type="permission_prompt"), 11)  # Dismissed: time for the notice
        d.answered(1, 53)
        d.feed("hook", h("PostToolUse", tool_name="Bash", **sub), 54)
        d.feed("hook", h("UserPromptSubmit", prompt=self.PEER), 57)  # The handback starts a turn…
        self.assertEqual(d.agents["s1"].state, "working")
        d.feed("hook", h("SubagentStop", **sub), 57)
        d.feed("hook", h("Stop"), 60)
        out = d.feed("hook", h("UserPromptSubmit", prompt=self.NOTICE), 60)  # …and this one none: no Stop will come
        self.assertEqual(out, [])
        self.assertEqual(d.agents["s1"].state, "done")
        self.assertEqual(d.agents["s1/a1"].state, "done")

    def test_a_notice_that_starts_a_turn_shows_from_its_first_tool(self):
        d = Deck()
        d.feed("hook", h("Stop"), 0)
        d.feed("hook", h("UserPromptSubmit", prompt=self.NOTICE), 1)
        d.feed("hook", h("PreToolUse", tool_name="Bash", tool_input={"command": "git status"}), 2)
        self.assertEqual(d.agents["s1"].state, "working")
        d.feed("hook", h("Stop"), 3)
        self.assertEqual(d.agents["s1"].state, "done")

    def test_a_sub_agents_handback_leaves_anothers_request_open(self):
        for prompt in (self.NOTICE, self.PEER):
            with self.subTest(prompt[:8]):
                d = Deck()
                d.feed("hook", h("SubagentStart", agent_id="a", agent_type="Plan"), 0)
                d.feed("hook", h("SubagentStart", agent_id="b", agent_type="Plan"), 0)
                d.feed("hook", h("PermissionRequest", agent_id="b", agent_type="Plan", tool_name="Bash",
                                 tool_input={"command": "git rm -n x"}), 1)
                out = d.feed("hook", h("UserPromptSubmit", prompt=prompt), 2)
                self.assertEqual([r for r in out if r["type"] == "close"], [])
                self.assertIn(1, d.requests)
                self.assertEqual(d.agents["s1/b"].state, "needs_you")

    def test_a_handback_keeps_the_session_needing_you_while_it_waits(self):
        d = Deck()
        d.feed("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}), 0)
        d.feed("hook", h("UserPromptSubmit", prompt=self.PEER), 1)
        self.assertEqual(d.agents["s1"].state, "needs_you")
        self.assertIn(1, d.requests)

    def test_they_take_no_chat_over_and_keep_finished_sub_agents(self):
        for prompt in (self.NOTICE, self.PEER):
            with self.subTest(prompt[:8]):
                d = Deck()
                d.feed("hook", h("UserPromptSubmit", sid="a", prompt="one"), 0)
                d.feed("hook", h("UserPromptSubmit", sid="b", prompt="two"), 1)
                d.feed("hook", h("UserPromptSubmit", sid="a", prompt=prompt), 2)
                self.assertEqual(d.active_sid, "b")
                d.feed("hook", h("SubagentStop", sid="b", agent_id="y", agent_type="Plan"), 3)
                d.feed("hook", h("UserPromptSubmit", sid="b", prompt=prompt), 4)
                self.assertIn("b/y", d.agents)

    def test_the_developers_own_prompt_still_clears_the_finished(self):
        d = Deck()
        d.feed("hook", h("SubagentStop", agent_id="y", agent_type="Plan"), 1)
        d.feed("hook", h("UserPromptSubmit", prompt="what does a <task-notification> mean?"), 2)
        self.assertNotIn("s1/y", d.agents)


class Interrupted(unittest.TestCase):
    """A refusal in the terminal or Esc ends a turn and no hook says so `[R]` 2026-10-31 (DESIGN.md §3)."""

    def test_a_request_refused_in_the_terminal_leaves_nothing_at_work(self):
        d = Deck()
        sub = {"agent_id": "a1", "agent_type": "Plan"}
        d.feed("hook", h("PreToolUse", tool_name="Bash", tool_use_id="t1", tool_input={"command": "git rm -n x"},
                         **sub), 1)
        d.feed("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "git rm -n x"}, **sub), 2)
        d.feed("transcript", {"session_id": "s1", "agent_id": "a1", "tool_results": ["t1"]}, 9)
        self.assertEqual(d.agents["s1/a1"].state, "working")  # As far as the tool's result says
        out = d.feed("transcript", {"session_id": "s1", "agent_id": "a1", "interrupted": 9.0}, 9)
        self.assertEqual(out[-1]["state"], "done")

    def test_esc_in_a_session_closes_what_it_asked(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", prompt="go"), 1)
        d.feed("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}), 2)
        out = d.feed("transcript", {"session_id": "s1", "interrupted": 3.0}, 3)
        self.assertIn({"type": "close", "id": 1, "reason": "answered_elsewhere"}, out)
        self.assertEqual(d.agents["s1"].state, "done")

    def test_esc_before_any_tool_clears_thinking(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", prompt="go"), 1)
        out = d.feed("transcript", {"session_id": "s1", "interrupted": 2.0}, 2)
        self.assertEqual((out[-1]["state"], out[-1]["detail"]), ("done", ""))

    def test_an_interrupt_older_than_the_turn_is_history(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", prompt="go"), 5)
        self.assertEqual(d.feed("transcript", {"session_id": "s1", "interrupted": 4.0}, 5), [])
        self.assertEqual(d.agents["s1"].state, "working")

    def test_a_previous_chats_sub_agent_goes(self):
        d = Deck()
        d.feed("hook", h("UserPromptSubmit", sid="a", prompt="one"), 0)
        d.feed("hook", h("SubagentStart", sid="a", agent_id="x", agent_type="Plan"), 1)
        d.feed("hook", h("UserPromptSubmit", sid="b", prompt="two"), 2)
        out = d.feed("transcript", {"session_id": "a", "agent_id": "x", "interrupted": 3.0}, 3)
        self.assertEqual(out[0]["type"], "remove")
        self.assertNotIn("a/x", d.agents)

    @staticmethod
    def line(text, at="2026-10-31T14:54:41.250Z", **kw):
        return {"type": "user", "timestamp": at, "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                **kw}

    def read(self, *lines):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s1.jsonl")
            with open(path, "w") as f:
                f.writelines(json.dumps(x) + "\n" for x in lines)
            return Transcripts().observe(h("Stop", transcript_path=path))

    def test_the_transcript_says_when(self):
        got = self.read(self.line("[Request interrupted by user for tool use]"))
        self.assertEqual(got, [{"session_id": "s1", "interrupted": 1793458481.25}])

    def test_one_a_response_came_after_is_over(self):
        response = {"type": "assistant", "message": {"id": "m", "model": "claude-opus-5-5", "content": []}}
        self.assertEqual(self.read(self.line("[Request interrupted by user]"), response), [])

    def test_a_sub_agents_line_in_the_sessions_file_and_a_quoted_one_say_nothing(self):
        self.assertEqual(self.read(self.line("[Request interrupted by user]", isSidechain=True)), [])
        self.assertEqual(self.read(self.line("why did [Request interrupted by user] show?")), [])

    def test_the_bridge_reads_on_while_an_agent_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s1.jsonl")
            open(path, "w").close()
            b = bridge.Bridge(None, io.StringIO(), hold_s=5)
            b.take("hook", h("UserPromptSubmit", prompt="go", transcript_path=path))
            with open(path, "a") as f:
                f.write(json.dumps(self.line("[Request interrupted by user]", at="2099-01-01T00:00:00.000Z")) + "\n")
            b.tick()
            self.assertEqual(b.deck.agents["s1"].state, "done")
            b.take("hook", h("SessionEnd"))
            b.tick()
            self.assertEqual(b.turns, {})


class Transcript(unittest.TestCase):
    @staticmethod
    def line(model="claude-opus-5-5", tokens=(4, 500, 9000), sidechain=False):
        usage = {"input_tokens": tokens[0], "cache_creation_input_tokens": tokens[1],
                 "cache_read_input_tokens": tokens[2], "output_tokens": 77}
        return json.dumps({"type": "assistant", "isSidechain": sidechain,
                           "message": {"model": model, "usage": usage}}) + "\n"

class Usage(unittest.TestCase):
    STATE = {"oauthAccount": {"emailAddress": "dev@example.com"}, "cachedUsageUtilization": {
        "fetchedAtMs": 1793314477234, "accountUuid": "x", "utilization": {
            "five_hour": {"utilization": 20, "resets_at": "2026-10-29T23:20:00.168549+00:00"},
            "seven_day": {"utilization": 61, "resets_at": "2026-11-03T01:00:00.168567+00:00"},
            "seven_day_opus": None}}}

    def test_the_cache_gives_its_two_windows_once_per_fetch_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as f:
                json.dump(self.STATE, f)
            cache = UsageCache(path)
            self.assertEqual(cache.poll(), {"fetched_at": 1793314477.234,
                                            "five_hour": {"used_percentage": 20, "resets_at": 1793316000},
                                            "seven_day": {"used_percentage": 61, "resets_at": 1793667600}})
            self.assertIsNone(cache.poll())  # unchanged
            with open(path, "w") as f:
                f.write('{"cachedUsage')  # Claude Code part way through writing it
            os.utime(path, (1, 1))
            self.assertIsNone(cache.poll())
            with open(path, "w") as f:
                json.dump(self.STATE, f)
            self.assertIsNone(cache.poll())  # the same fetch again
            self.assertIsNone(UsageCache(os.path.join(tmp, "missing.json")).poll())

    def test_the_newer_of_the_status_line_and_the_cache_wins(self):
        d = Deck()
        d.feed("hook", h("SessionStart"), 0)
        out = d.feed("usage-cache", {"fetched_at": 100, "five_hour": {"used_percentage": 20, "resets_at": 3700}}, 101)
        self.assertEqual(out[-1]["five_hour_pct"], 20)
        out = d.feed("status", {"session_id": "s1", "rate_limits": {
            "five_hour": {"used_percentage": 22, "resets_at": 3700}}}, 200)
        self.assertEqual(out[-1]["five_hour_pct"], 22)
        self.assertEqual(d.feed("usage-cache", {"fetched_at": 150, "five_hour": {"used_percentage": 21}}, 201), [])
        out = d.feed("usage-cache", {"fetched_at": 300, "five_hour": {"used_percentage": 25, "resets_at": 3700}}, 301)
        self.assertEqual(out[-1]["five_hour_pct"], 25)


class Written(unittest.TestCase):
    @staticmethod
    def write(path, *lines):
        with open(path, "a") as f:
            for d in lines:
                f.write(json.dumps(d) + "\n")

    @staticmethod
    def said(mid, out, context=(4, 500, 9000), sidechain=False):
        return {"type": "assistant", "isSidechain": sidechain, "message": {
            "id": mid, "model": "claude-opus-5-5", "usage": {"input_tokens": context[0],
                                                         "cache_creation_input_tokens": context[1],
                                                         "cache_read_input_tokens": context[2], "output_tokens": out}}}

    PROMPT = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}}
    RESULT = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}

    def test_the_count_is_what_the_latest_response_has_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s1.jsonl")
            self.write(path, self.said("m0", 999), self.PROMPT, self.said("m1", 40), self.said("m1", 120),
                       self.RESULT, self.said("m2", 300), self.said("x", 7777, sidechain=True))
            r = Transcripts()
            hook = h("PostToolUse", transcript_path=path)
            self.assertEqual(r.observe(hook), [{"session_id": "s1", "model": "claude-opus-5-5",
                                                "context_tokens": 9504, "response_tokens": 300}])
            self.assertEqual(r.observe(hook), [])  # nothing new
            with open(path, "a") as f:
                f.write(json.dumps(self.PROMPT)[:20])  # a line still being written
            self.assertEqual(r.observe(hook), [])
            with open(path, "a") as f:
                f.write(json.dumps(self.PROMPT)[20:] + "\n")
            self.write(path, self.said("m3", 55), {"type": "ai-title", "aiTitle": "Fix the build"})
            out = r.observe(hook)
            self.assertIn({"session_id": "s1", "title": "Fix the build"}, out)
            self.assertEqual(out[-1]["response_tokens"], 55)  # a new response: from zero, as VS Code's count

    def test_a_sub_agents_response_and_description_are_read_beside_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s1.jsonl")
            open(path, "w").close()
            sub = os.path.join(tmp, "s1", "subagents")
            os.makedirs(sub)
            self.write(os.path.join(sub, "agent-a1.jsonl"), dict(self.PROMPT, isSidechain=True),
                       self.said("m1", 210, sidechain=True))
            with open(os.path.join(sub, "agent-a1.meta.json"), "w") as f:
                json.dump({"agentType": "Explore", "description": "Test bash approval prompt"}, f)
            r = Transcripts()
            out = r.observe(h("PreToolUse", transcript_path=path, agent_id="a1", agent_type="Explore"))
            self.assertIn({"session_id": "s1", "model": "claude-opus-5-5", "context_tokens": 9504,
                           "response_tokens": 210, "agent_id": "a1"}, out)
            self.assertIn({"session_id": "s1", "agent_id": "a1", "description": "Test bash approval prompt"}, out)

    def test_a_model_reads_as_claude_code_names_it(self):
        self.assertEqual(model_name("claude-opus-5-5"), "Opus 5.5")
        self.assertEqual(model_name("claude-haiku-5-5"), "Haiku 5.5")
        self.assertEqual(model_name("claude-opus-4-8[1m]"), "Opus 4.8")
        self.assertEqual(model_name("claude-fable-5"), "Fable 5")
        self.assertEqual(model_name("claude-opus-4-0"), "Opus 4")
        self.assertEqual(model_name("claude-sonnet-4-5-20250929"), "Sonnet 4.5")
        self.assertEqual(model_name("claude-3-5-sonnet-20241022"), "Sonnet 3.5")  # The older spelling
        self.assertEqual(model_name("claude-3-opus-20240229"), "Opus 3")
        self.assertEqual(model_name("us.anthropic.claude-sonnet-4-5-20250929-v1:0"), "Sonnet 4.5")  # Cloud providers'
        self.assertEqual(model_name("anthropic.claude-3-5-sonnet-20241022-v2:0"), "Sonnet 3.5")
        self.assertEqual(model_name("claude-sonnet-4-5@20250929"), "Sonnet 4.5")
        self.assertEqual(model_name("gpt-x"), "gpt-x")  # Not one it knows: as it came

    def test_an_agents_model_and_effort_reach_the_deck(self):
        d = Deck()
        d.feed("hook", h("SessionStart"), 0)
        out = d.feed("transcript", {"session_id": "s1", "model": "claude-haiku-5-5", "context_tokens": 100,
                                    "response_tokens": 1}, 1)
        self.assertEqual(out[0]["model"], "Haiku 5.5")
        out = d.feed("transcript", {"session_id": "s1", "model": "claude-opus-5-5", "context_tokens": 100,
                                    "response_tokens": 1, "effort": "high"}, 2)
        self.assertEqual(out[0]["model"], "Opus 5.5 • high")
        out = d.feed("hook", h("PreToolUse", tool_name="Bash", tool_input={"command": "ls"},
                               effort={"level": "xhigh"}), 3)
        self.assertEqual(out[0]["model"], "Opus 5.5 • xhigh")

    def test_a_synthetic_line_is_no_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {"model": "<synthetic>", "usage": {}}}) + "\n")
            self.assertEqual(Transcripts().observe(h("Stop", transcript_path=path)), [])

    def test_the_deck_shows_the_responses_tokens_and_the_context_share(self):
        d = Deck()
        d.feed("hook", h("SessionStart", sid="vs"), 0)
        out = d.feed("transcript", {"session_id": "vs", "model": "claude-opus-5-5", "context_tokens": 664000,
                                    "response_tokens": 1200}, 1)
        self.assertEqual((out[0]["tokens"], out[0]["context_pct"]), (664000, 66))  # opus-5-5's 1M window, known
        out = d.feed("hook", h("UserPromptSubmit", sid="vs"), 2)
        self.assertEqual(out[0]["tokens"], 664000)  # a new turn leaves the context where it was
        d.feed("status", {"session_id": "cli", "model": {"id": "claude-x[1m]"},
                          "context_window": {"context_window_size": 1000000, "used_percentage": 5}}, 3)
        d.feed("hook", h("SessionStart", sid="vs2"), 4)
        out = d.feed("transcript", {"session_id": "vs2", "model": "claude-x", "context_tokens": 250000}, 5)
        self.assertEqual(out[0]["context_pct"], 25)  # the window a status line taught for that model
        d2 = Deck(context_window=200000)
        d2.feed("hook", h("SubagentStart", agent_id="a1", agent_type="Explore"), 0)
        d2.feed("transcript", {"session_id": "s1", "model": "m", "context_tokens": 50000}, 1)
        out = d2.feed("transcript", {"session_id": "s1", "agent_id": "a1", "description": "Find the bug",
                                     "context_tokens": 85457, "response_tokens": 3287}, 2)
        self.assertEqual(d2.agents["s1"].context_pct, 25)
        # As the agent map counts it: a real sub-agent's 88.7k.
        self.assertEqual((out[0]["name"], out[0]["tokens"]), ("Find the bug", 88744))


class Refresh(unittest.TestCase):
    def test_the_refresh_asks_claude_code_for_usage_with_no_hooks_and_no_session(self):
        r = UsageRefresh(300, lambda: True, claude="/usr/local/bin/claude")
        cmd = r.command()
        self.assertEqual(cmd[:3], ["/usr/local/bin/claude", "-p", "/usage"])
        self.assertIn('{"disableAllHooks": true}', cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertNotIn("--bare", cmd)  # it would skip the login /usage needs

    def test_the_refresh_runs_claude_and_stays_off_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake, mark = os.path.join(tmp, "claude"), os.path.join(tmp, "ran")
            with open(fake, "w") as f:
                f.write(f"#!/bin/sh\necho \"$@\" > {mark}\n")
            os.chmod(fake, 0o755)
            self.assertTrue(UsageRefresh(300, lambda: True, claude=fake).run_once())
            with open(mark) as f:
                self.assertIn("-p /usage", f.read())
        self.assertFalse(UsageRefresh(0, lambda: True, claude="/bin/true").start())


class StatusLine(unittest.TestCase):
    def test_the_status_line_says_whether_the_bridge_listens(self):
        data = {"model": {"display_name": "Opus"}, "context_window": {"used_percentage": 8},
                "rate_limits": {"five_hour": {"used_percentage": 23.4}}}
        self.assertEqual(hook.status_line(data, True), "deck | Opus | ctx 8% | 5h 23%")
        self.assertEqual(hook.status_line({}, False), "deck off")


class Install(unittest.TestCase):
    def run_install(self, path, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "install.py"), "--settings", path, "--no-login", *args],
                              capture_output=True, text=True)

    def test_install_keeps_other_settings_and_uninstall_restores_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            mine = {"effortLevel": "high", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say"}]}]}}
            with open(path, "w") as f:
                json.dump(mine, f)
            self.assertEqual(self.run_install(path).returncode, 0)
            with open(path) as f:
                got = json.load(f)
            self.assertEqual(set(got["hooks"]), set(install.EVENTS))
            self.assertEqual(len(got["hooks"]["Stop"]), 2)
            self.assertIn("hook.py status", got["statusLine"]["command"])
            self.assertEqual(self.run_install(path).returncode, 0)  # twice adds nothing twice
            with open(path) as f:
                self.assertEqual(len(json.load(f)["hooks"]["Stop"]), 2)
            self.assertEqual(self.run_install(path, "--uninstall").returncode, 0)
            with open(path) as f:
                self.assertEqual(json.load(f), mine)
            self.assertTrue(glob.glob(path + ".deck-backup-*"))

    def test_install_knows_its_own_entries_wherever_its_folder_lives(self):
        moved = {"hooks": [{"type": "command", "command": "'/Users/x/Agent Deck/hook.py' hook", "timeout": 5}]}
        self.assertTrue(install.ours(moved))
        self.assertFalse(install.ours({"hooks": [{"type": "command", "command": "~/bin/hook.py --other"}]}))

    def test_the_login_agent_runs_the_bridge_quietly_and_finds_claude(self):
        import plistlib
        agent = plistlib.loads(install.agent_plist())
        self.assertEqual(agent["ProgramArguments"][1:], [install.BRIDGE_PY, "--quiet"])
        self.assertTrue(agent["KeepAlive"] and agent["RunAtLoad"])
        self.assertIn("/usr/bin", agent["EnvironmentVariables"]["PATH"])

    def test_login_is_asked_and_only_yes_means_yes(self):
        asked = []

        def answer(text):
            def ask(question):
                asked.append(question)
                return text
            return ask

        self.assertTrue(install.choose_login(False, False, answer("y"), tty=True))
        self.assertIn("yes  The bridge starts now and every time you log in", asked[0])
        self.assertIn("no   Nothing starts by itself", asked[0])
        for text in ("", "n", "maybe"):
            self.assertFalse(install.choose_login(False, False, answer(text), tty=True))
        self.assertFalse(install.choose_login(False, False, answer("y"), tty=False))  # no terminal: no, unasked
        self.assertTrue(install.choose_login(True, False, answer("n"), tty=True))     # the flag answers
        self.assertFalse(install.choose_login(False, True, answer("y"), tty=True))
        self.assertEqual(len(asked), 4)

    def test_install_stops_at_someone_elses_status_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w") as f:
                json.dump({"statusLine": {"type": "command", "command": "mine.sh"}}, f)
            r = self.run_install(path)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("statusLine is already set", r.stderr)


class Plugin(unittest.TestCase):
    KEY = f"{hook.PLUGIN}@alxvlabs"

    def claude_dir(self, tmp, installs=None, user=None, project=None):
        """A scratch ~/.claude: installed_plugins.json listing `installs`, the user settings `user`, and a project
        folder whose .claude/settings.json is `project`."""
        os.makedirs(os.path.join(tmp, "plugins"), exist_ok=True)
        if installs is not None:
            with open(os.path.join(tmp, "plugins", "installed_plugins.json"), "w") as f:
                json.dump({"version": 2, "plugins": installs}, f)
        if user is not None:
            with open(os.path.join(tmp, "settings.json"), "w") as f:
                json.dump(user, f)
        if project is not None:
            os.makedirs(os.path.join(tmp, "proj", ".claude"), exist_ok=True)
            with open(os.path.join(tmp, "proj", ".claude", "settings.json"), "w") as f:
                json.dump(project, f)
        return tmp

    def test_the_plugin_is_off_only_when_its_settings_say_false(self):
        user_install = {self.KEY: [{"scope": "user"}]}
        with tempfile.TemporaryDirectory() as tmp:
            d = self.claude_dir(tmp, user_install, {"enabledPlugins": {self.KEY: True}})
            self.assertTrue(plugin_state.installed(hook.PLUGIN, d) and plugin_state.enabled(hook.PLUGIN, d))
            self.claude_dir(tmp, user=({"enabledPlugins": {self.KEY: False}}))
            self.assertFalse(plugin_state.enabled(hook.PLUGIN, d))
            self.claude_dir(tmp, user={})
            self.assertTrue(plugin_state.enabled(hook.PLUGIN, d))  # no switch written: on
            with open(os.path.join(tmp, "settings.json"), "w") as f:
                f.write("{half a write")
            self.assertTrue(plugin_state.enabled(hook.PLUGIN, d))  # unreadable: on
            self.claude_dir(tmp, {"other@m": [{"scope": "user"}]})
            self.assertFalse(plugin_state.installed(hook.PLUGIN, d))

    def test_a_project_install_reads_the_projects_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            installs = {self.KEY: [{"scope": "project", "projectPath": os.path.join(tmp, "proj")}]}
            d = self.claude_dir(tmp, installs, {"enabledPlugins": {self.KEY: False}},
                                {"enabledPlugins": {self.KEY: True}})
            self.assertTrue(plugin_state.enabled(hook.PLUGIN, d))
            self.claude_dir(tmp, project={"enabledPlugins": {self.KEY: False}})
            self.assertFalse(plugin_state.enabled(hook.PLUGIN, d))

    def test_the_bridge_stops_once_the_plugin_it_saw_goes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as folder:
            d = self.claude_dir(tmp, {}, {})
            watch = bridge.PluginWatch(hook.PLUGIN, folder, d)
            self.assertIsNone(watch.gone())  # never listed: `claude --plugin-dir`, left running
            self.claude_dir(tmp, {self.KEY: [{"scope": "user"}]}, {"enabledPlugins": {self.KEY: True}})
            self.assertIsNone(watch.gone())
            self.claude_dir(tmp, user={"enabledPlugins": {self.KEY: False}})
            self.assertIn("disabled", watch.gone())
            self.claude_dir(tmp, {}, {})
            self.assertIn("uninstalled", watch.gone())
        self.assertIn("folder is gone", watch.gone())

    def install(self, tmp, *folders):
        """The plugin listed at `folders`, the first the newest, and switched on."""
        entries = [{"scope": "user", "installPath": f, "lastUpdated": f"2026-10-{30 - i}T00:00:00Z"}
                   for i, f in enumerate(folders)]
        return self.claude_dir(tmp, {self.KEY: entries}, {"enabledPlugins": {self.KEY: True}})

    def test_an_update_replaces_the_bridge_though_the_old_folder_stays(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as old, \
                tempfile.TemporaryDirectory() as new:
            d = self.install(tmp, old)
            watch = bridge.PluginWatch(hook.PLUGIN, old, d)
            self.assertIsNone(watch.gone())
            self.install(tmp, new)
            self.assertTrue(os.path.isdir(old))
            self.assertEqual(watch.gone(), bridge.UPDATED)

    def test_a_bridge_started_from_a_left_behind_folder_goes_too(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as old, \
                tempfile.TemporaryDirectory() as new:
            d = self.install(tmp, new)
            with open(os.path.join(old, bridge.ORPHAN_MARK), "w") as f:
                f.write("1")
            self.assertEqual(bridge.PluginWatch(hook.PLUGIN, old, d).gone(), bridge.UPDATED)

    def test_a_plugin_dir_bridge_is_left_running_beside_an_install(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as mine, \
                tempfile.TemporaryDirectory() as installed:
            d = self.install(tmp, installed)
            self.assertIsNone(bridge.PluginWatch(hook.PLUGIN, mine, d).gone())

    def test_a_list_it_cannot_read_stops_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as old:
            d = self.install(tmp, old)
            watch = bridge.PluginWatch(hook.PLUGIN, old, d)
            self.assertIsNone(watch.gone())
            with open(os.path.join(d, "plugins", "installed_plugins.json"), "w") as f:
                f.write("{ half written")
            self.assertIsNone(watch.gone())

    def test_a_held_request_keeps_an_updated_bridge_until_it_settles(self):
        b = bridge.Bridge(None, io.StringIO(), hold_s=5)
        self.assertFalse(b.holding())
        b.take("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}))
        b.waiting[1] = [threading.Event(), None]
        self.assertTrue(b.holding())

    def test_a_hook_starts_the_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as old, \
                tempfile.TemporaryDirectory() as new, tempfile.TemporaryDirectory() as mine:
            open(os.path.join(new, "bridge.py"), "w").close()
            d = self.install(tmp, new, old)
            self.assertEqual(plugin_state.current_folder(hook.PLUGIN, mine, d), os.path.realpath(new))
            self.assertEqual(plugin_state.current_folder(hook.PLUGIN, old, d), os.path.realpath(old))  # still listed
            self.install(tmp, os.path.join(tmp, "not-there"))
            self.assertEqual(plugin_state.current_folder(hook.PLUGIN, old, d), os.path.realpath(old))

    def test_the_plugin_never_starts_a_bridge_on_windows(self):
        self.assertTrue(hook.can_start("darwin") and hook.can_start("linux"))
        self.assertFalse(hook.can_start("win32"))

    def test_the_plugins_hooks_stand_aside_for_install_pys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            self.assertFalse(hook.installed_by_hand(path))
            with open(path, "w") as f:
                json.dump(install.add({}), f)
            self.assertTrue(hook.installed_by_hand(path))
            with open(path, "w") as f:
                json.dump(install.strip(install.add({"hooks": {"Stop": [{"hooks": [{"command": "say"}]}]}})), f)
            self.assertFalse(hook.installed_by_hand(path))

    def test_the_plugin_carries_every_hook_install_py_writes(self):
        with open(os.path.join(HERE, "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        self.assertEqual(set(hooks), set(install.EVENTS))
        for event, matchers in hooks.items():
            (entry,) = matchers[0]["hooks"]
            self.assertEqual(entry["command"], 'python3 "${CLAUDE_PLUGIN_ROOT}/hook.py" hook --plugin')
            want = install.REQUEST_TIMEOUT_S if event in install.HELD_EVENTS else install.HOOK_TIMEOUT_S
            self.assertEqual(entry["timeout"], want, event)
        with open(os.path.join(HERE, ".claude-plugin", "plugin.json")) as f:
            self.assertEqual(json.load(f)["name"], hook.PLUGIN)
        with open(os.path.join(HERE, ".claude-plugin", "marketplace.json")) as f:
            market = json.load(f)
        self.assertEqual([p["name"] for p in market["plugins"]], [hook.PLUGIN])
        self.assertNotIn("claude", market["name"])


class Hold(unittest.TestCase):
    def held(self, b, hook=None):
        """A request taken and held on its own thread, as a hook's HTTP request is; its answer when it returns."""
        b.take("hook", hook or h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}))
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("answer", b.hold(1)))
        t.start()
        for _ in range(100):
            if 1 in b.waiting:
                break
            threading.Event().wait(0.01)
        return t, result

    def test_the_devices_answer_goes_back_to_the_hook(self):
        out = io.StringIO()
        b = bridge.Bridge(None, out, hold_s=5)
        t, result = self.held(b)
        b.decide(1, True)
        t.join(2)
        self.assertEqual(result["answer"], "allow")
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1])["state"], "working")

    def test_an_answer_in_the_terminal_lets_the_hook_go_with_none(self):
        b = bridge.Bridge(None, io.StringIO(), hold_s=5)
        t, result = self.held(b)
        b.take("hook", h("PostToolUse", tool_name="Bash"))
        t.join(2)
        self.assertIsNone(result["answer"])
        b.decide(1, False)  # too late: nothing waits for it
        self.assertNotIn(1, b.waiting)

    def test_a_request_held_past_the_hold_closes_as_timed_out(self):
        out = io.StringIO()
        b = bridge.Bridge(None, out, hold_s=0.05)
        t, result = self.held(b)
        t.join(2)
        self.assertIsNone(result["answer"])
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1]),
                         {"type": "close", "id": 1, "reason": "timed_out"})


TWO_QUESTIONS = {"questions": [
    {"question": "Which base branch?", "header": "Branch", "multiSelect": False,
     "options": [{"label": "main", "description": "The release branch"}, {"label": "develop", "description": ""}]},
    {"question": "What else?", "header": "Plan", "multiSelect": True,
     "options": [{"label": "Tests"}, {"label": "Docs"}, {"label": "Pull request"}]}]}


class Questions(unittest.TestCase):
    def ask(self, tool_input=None):
        d = Deck()
        d.asks_questions = True
        return d, d.feed("hook", h("PreToolUse", tool_name="AskUserQuestion", tool_input=tool_input or TWO_QUESTIONS), 0)

    def test_a_question_reaches_a_deck_that_takes_them(self):
        _, out = self.ask()
        self.assertEqual(out[-2]["state"], "needs_you")
        self.assertEqual(out[-1], {"type": "question", "id": 1, "agent": 1, "part": 0, "tool": "Branch 1/2",
                                   "question": "Which base branch?", "multi": False,
                                   "options": ["main: The release branch", "develop"]})

    def test_a_deck_on_older_firmware_keeps_the_terminal(self):
        out = Deck().feed("hook", h("PreToolUse", tool_name="AskUserQuestion", tool_input=TWO_QUESTIONS), 0)
        self.assertNotIn("question", [r["type"] for r in out])

    def test_a_question_the_deck_cannot_hold_stays_in_the_terminal(self):
        many = {"questions": [{"question": "Which?", "options": [{"label": str(i)} for i in range(5)]}]}
        d = Deck()
        d.asks_questions = True
        out = d.feed("hook", h("PreToolUse", tool_name="AskUserQuestion", tool_input=many), 0)
        self.assertNotIn("question", [r["type"] for r in out])

    def test_each_part_follows_the_last_and_the_answers_are_keyed_by_question(self):
        d, _ = self.ask()
        out, settled, answers = d.answered_question(1, 0, 0b01, 1)
        self.assertEqual((out[0]["part"], out[0]["tool"], out[0]["multi"], settled), (1, "Plan 2/2", True, False))
        self.assertEqual(d.snapshot(1)[-1]["part"], 1)  # A deck that comes back gets the part in hand
        out, settled, answers = d.answered_question(1, 1, 0b101, 2)
        self.assertTrue(settled)
        self.assertEqual(answers, {"Which base branch?": "main", "What else?": "Tests, Pull request"})
        self.assertEqual(d.agents["s1"].state, "working")
        self.assertEqual(d.requests, {})

    def test_a_resent_answer_changes_nothing(self):
        d, _ = self.ask()
        d.answered_question(1, 0, 0b10, 1)
        self.assertEqual(d.answered_question(1, 0, 0b01, 2), ([], False, None))
        self.assertEqual(d.asks[1].answers, {"Which base branch?": "develop"})

    def test_nothing_chosen_leaves_the_call_to_the_terminal(self):
        d, _ = self.ask()
        self.assertEqual(d.answered_question(1, 0, 0, 1), ([], True, None))
        self.assertEqual(d.requests, {})
        self.assertEqual(d.agents["s1"].state, "needs_you")

    def test_an_answer_in_the_terminal_closes_it(self):
        d, _ = self.ask()
        out = d.feed("hook", h("PostToolUse", tool_name="AskUserQuestion"), 1)
        self.assertEqual(out[0], {"type": "close", "id": 1, "reason": "answered_elsewhere"})
        self.assertEqual(d.asks, {})

    def bridge_holding(self, seconds=5):
        out = io.StringIO()
        b = bridge.Bridge(None, out, hold_s=5, question_hold_s=seconds)
        b.deck.asks_questions = True
        b.take("hook", h("PreToolUse", tool_name="AskUserQuestion", tool_input=TWO_QUESTIONS))
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("answer", b.hold(1, seconds)))
        t.start()
        for _ in range(100):
            if 1 in b.waiting:
                break
            threading.Event().wait(0.01)
        return b, t, result, out

    def test_the_hook_goes_back_with_every_answer(self):
        b, t, result, out = self.bridge_holding()
        b.answer(1, 0, 0b01)
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1])["part"], 1)
        self.assertFalse(result)
        b.answer(1, 1, 0b10)
        t.join(2)
        self.assertEqual(result["answer"], {"Which base branch?": "main", "What else?": "Docs"})

    def test_a_question_left_to_the_terminal_lets_the_hook_go_with_none(self):
        b, t, result, _ = self.bridge_holding()
        b.answer(1, 0, 0)
        t.join(2)
        self.assertIsNone(result["answer"])

    def test_a_question_held_past_its_hold_closes_as_timed_out(self):
        b, t, result, out = self.bridge_holding(0.05)
        t.join(2)
        self.assertIsNone(result["answer"])
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1]),
                         {"type": "close", "id": 1, "reason": "timed_out"})

    def test_a_refusal_in_the_terminal_closes_the_request_from_the_transcript(self):
        d = Deck()
        d.feed("hook", h("PreToolUse", tool_name="Bash", tool_use_id="toolu_1", tool_input={"command": "ls"}), 0)
        d.feed("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}), 1)
        self.assertEqual(d.feed("transcript", {"session_id": "s1", "tool_results": ["toolu_0"]}, 2), [])
        out = d.feed("transcript", {"session_id": "s1", "tool_results": ["toolu_1"]}, 3)
        self.assertEqual(out[0], {"type": "close", "id": 1, "reason": "answered_elsewhere"})
        self.assertEqual(d.agents["s1"].state, "working")
        self.assertEqual(d.request_uses, {})

    def test_the_transcript_says_which_tool_calls_ended(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.jsonl")
            line = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]}}
            with open(path, "w") as f:
                f.write(json.dumps(line) + "\n")
            out = Transcripts().observe(h("PermissionRequest", transcript_path=path))
            self.assertEqual(out, [{"session_id": "s1", "tool_results": ["toolu_1"]}])

    def test_a_tool_that_fails_closes_its_request(self):
        d = Deck()
        d.feed("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "false"}), 0)
        out = d.feed("hook", h("PostToolUseFailure", tool_name="Bash"), 1)
        self.assertEqual(out[0], {"type": "close", "id": 1, "reason": "answered_elsewhere"})

    def test_a_hook_claude_code_stopped_closes_its_request(self):
        out = io.StringIO()
        b = bridge.Bridge(None, out, hold_s=5)
        b.take("hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}))
        self.assertIsNone(b.hold(1, gone=lambda: True))
        self.assertEqual(json.loads(out.getvalue().splitlines()[-2]),
                         {"type": "close", "id": 1, "reason": "answered_elsewhere"})

    def test_a_hook_that_left_is_seen_on_its_connection(self):
        here, there = socket.socketpair()
        self.assertFalse(bridge.peer_gone(here))
        there.close()
        self.assertTrue(bridge.peer_gone(here))
        here.close()

    def test_an_answer_that_beats_the_hooks_hold_is_kept(self):
        b = bridge.Bridge(None, io.StringIO(), hold_s=5, question_hold_s=5)
        b.deck.asks_questions = True
        b.take("hook", h("PreToolUse", tool_name="AskUserQuestion", tool_input=TWO_QUESTIONS))
        b.answer(1, 0, 0)  # The deck's queue was full: back before the hook reached hold()
        self.assertIsNone(b.hold(1, 5))
        self.assertEqual(b.waiting, {})

    def test_a_write_failing_under_the_lock_does_not_wedge_the_bridge(self):
        class Failing:
            def __init__(self, bridge_):
                self.bridge = bridge_

            def send(self, record):
                self.bridge.on_lost()  # What SerialLink.close() does on a failed write
                return False

        b = bridge.Bridge(None, io.StringIO(), hold_s=5)
        b.link = Failing(b)
        done = threading.Event()
        threading.Thread(target=lambda: (b.take("hook", h("SessionStart")), done.set()), daemon=True).start()
        self.assertTrue(done.wait(2))

    def test_the_decks_hello_says_whether_it_takes_questions(self):
        b = bridge.Bridge(None, io.StringIO())
        b.link = type("L", (), {"send": lambda self, r: None})()
        b.on_record({"type": "hello", "version": 1})
        self.assertFalse(b.deck.asks_questions)
        b.on_record({"type": "hello", "version": 2})
        self.assertTrue(b.deck.asks_questions)
        b.on_lost()
        self.assertFalse(b.deck.asks_questions)


class Link(unittest.TestCase):
    def test_a_frame_survives_the_trip_and_a_log_line_before_it(self):
        line = serial_link.encode({"type": "agent", "name": "25\u00b0 \u2022 web-shop"})
        self.assertTrue(line.startswith(b"@deck ") and line.endswith(b"\n"))
        self.assertEqual(serial_link.decode(line)["name"], "25\u00b0 \u2022 web-shop")
        self.assertEqual(serial_link.decode(b"I (812) usb_line: half a log li" + line)["type"], "agent")
        self.assertIsNone(serial_link.decode(b"I (812) app_shell: running"))
        self.assertIsNone(serial_link.decode(line.replace(b"web", b"wab")))  # its CRC no longer holds

    def test_the_crc_is_the_one_the_deck_computes(self):
        # onx::deck_link::crc32 is IEEE 802.3, as zlib's — its loop compiled on the host gives 497c309c for this
        # JSON too; the line carries it as eight hex digits.
        self.assertEqual(serial_link.encode({"type": "ping"}), b'@deck 497c309c {"type":"ping"}\n')

    def test_the_bridge_sends_everything_on_open_and_answers_a_decision(self):
        sent = []

        class FakeLink:
            connected = True

            def send(self, r):
                sent.append(r)
                return True

        b = bridge.Bridge(None, io.StringIO(), hold_s=5)
        b.attach(FakeLink())
        b.take("hook", h("SessionStart"))
        t, result = Hold().held(b)
        sent.clear()
        b.on_record({"type": "hello", "version": 1})
        self.assertEqual([r["type"] for r in sent], ["reset", "agent", "request"])
        sent.clear()
        b.on_record({"type": "decision", "id": 1, "allow": False})
        t.join(2)
        self.assertEqual(result["answer"], "deny")
        self.assertEqual(sent[-1], {"type": "ack", "id": 1})

    def test_a_request_let_go_without_the_decks_answer_is_never_offered_again(self):
        sent = []

        class FakeLink:
            connected = True

            def send(self, r):
                sent.append(r)
                return True

        b = bridge.Bridge(None, io.StringIO(), hold_s=5)
        b.attach(FakeLink())
        t, result = Hold().held(b)
        b.on_lost()  # the cable pulled: the hook goes back, the terminal asks
        t.join(2)
        self.assertIsNone(result["answer"])
        self.assertIn({"type": "close", "id": 1, "reason": "timed_out"}, sent)
        sent.clear()
        b.on_record({"type": "hello", "version": 1})  # the deck back
        self.assertNotIn("request", [r["type"] for r in sent])

    def test_opening_the_port_asks_for_a_hello_and_the_hello_gets_everything_once(self):
        sent = []

        class FakeLink:
            connected = True

            def send(self, r):
                sent.append(r)
                return True

        b = bridge.Bridge(None, io.StringIO())
        b.attach(FakeLink())
        b.take("hook", h("SessionStart"))
        sent.clear()
        b.on_open()
        self.assertEqual(sent, [{"type": "hello"}])  # the deck may be booting: nothing it could miss
        b.on_record({"type": "hello", "version": 1})
        self.assertEqual([r["type"] for r in sent], ["hello", "reset", "agent"])

    def test_losing_the_deck_lets_a_held_request_go(self):
        b = bridge.Bridge(None, io.StringIO(), hold_s=5)
        t, result = Hold().held(b)
        b.on_lost()
        t.join(2)
        self.assertIsNone(result["answer"])

    @staticmethod
    def linked(kind="usb", grace_s=0.0, **kw):
        sent = []

        class FakeLink:
            connected = True

            def send(self, r):
                sent.append(r)
                return True

        link = FakeLink()
        link.kind, link.grace_s = kind, grace_s
        b = bridge.Bridge(None, io.StringIO(), **kw)
        b.attach(link)
        return b, sent

    def test_a_version_4_hello_names_the_deck_and_keeps_what_it_takes(self):
        b, _ = self.linked()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            b.on_record({"type": "hello", "version": 4, "id": "3f2a", "board": "jc3636k718", "link": "usb"})
        self.assertEqual(b.named, {"id": "3f2a", "board": "jc3636k718", "link": "usb"})
        self.assertIn("jc3636k718 3f2a on usb", err.getvalue())
        self.assertTrue(b.deck.asks_questions and b.deck.shows_idle)
        with contextlib.redirect_stderr(io.StringIO()):
            b.on_record({"type": "hello", "version": 3})  # the Nextion deck on the cable: as today, with no name
        self.assertIsNone(b.named)
        self.assertTrue(b.deck.asks_questions and b.deck.shows_idle)

    def test_a_hello_whose_names_break_the_rule_names_nothing(self):
        b, _ = self.linked()
        for bad in ({"id": "3F2A"}, {"id": ""}, {"board": "a" * 17}, {"link": 'usb"'}, {"id": 7}, {"link": None}):
            with self.subTest(bad), contextlib.redirect_stderr(io.StringIO()):
                b.on_record({"type": "hello", "version": 4, "id": "3f2a", "board": "jc3636k718", "link": "usb", **bad})
                self.assertIsNone(b.named)
                self.assertTrue(b.deck.asks_questions)  # its version still says what it takes

    def test_only_the_cable_passes_a_wifi_record_on(self):
        b, _ = self.linked(kind="wifi")
        b.wifi_join = b.wifi_pick = lambda r: self.fail("a Wi-Fi record taken off the network")
        b.link.on_record({"type": "wifi_join", "names": ["Studio"], "rssi": [-50], "secure": [True]})
        b.link.on_record({"type": "wifi_pick", "name": "Studio"})

    def test_a_deck_back_within_the_grace_keeps_a_held_request(self):
        b, sent = self.linked(kind="wifi", grace_s=0.3, hold_s=5)
        t, result = Hold().held(b)
        b.link.on_lost()
        t.join(0.1)
        self.assertTrue(t.is_alive())  # within the grace: still held
        sent.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            b.on_record({"type": "hello", "version": 4, "id": "3f2a", "board": "jc3636k718", "link": "wifi"})
        self.assertIn("request", [r["type"] for r in sent])  # in the snapshot again
        t.join(0.4)
        self.assertTrue(t.is_alive())  # past the grace, still held: the deck came back in it
        b.decide(1, True)
        t.join(2)
        self.assertEqual(result["answer"], "allow")

    def test_a_deck_not_back_within_the_grace_lets_a_held_request_go(self):
        b, _ = self.linked(kind="wifi", grace_s=0.1, hold_s=5)
        t, result = Hold().held(b)
        b.link.on_lost()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertIsNone(result["answer"])

    def test_a_hello_read_after_its_link_closed_brings_nothing_back(self):
        b, sent = self.linked(kind="wifi", grace_s=0.1, hold_s=5)
        t, result = Hold().held(b)
        b.link.on_lost()
        b.link.connected = False  # the reader still had a hello buffered as the writer closed the port
        sent.clear()
        b.link.on_record({"type": "hello", "version": 4, "id": "3f2a", "board": "jc3636k718", "link": "wifi"})
        self.assertEqual(sent, [])
        t.join(2)
        self.assertFalse(t.is_alive())  # the grace still ran out
        self.assertIsNone(result["answer"])

    def test_the_grace_lets_go_only_what_was_held_as_the_deck_went(self):
        b, _ = self.linked(kind="wifi", grace_s=0.2, hold_s=5)
        b.link.on_lost()
        t, result = Hold().held(b)  # begun in the grace, as --answer holds with no deck
        threading.Event().wait(0.4)
        self.assertTrue(t.is_alive())
        b.decide(1, False)
        t.join(2)
        self.assertEqual(result["answer"], "deny")

    def test_the_log_names_the_deck_once_a_change(self):
        b, _ = self.linked()
        hello = {"type": "hello", "version": 4, "id": "3f2a", "board": "jc3636k718", "link": "usb"}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            b.on_record(hello)
            b.link.on_lost()  # a /release for flashing, then the deck back
            b.on_record(hello)
            b.on_record({**hello, "id": "3F2A"})
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("its names unreadable", lines[1])

    def test_the_link_over_a_pseudo_terminal(self):
        import pty
        try:
            master, slave = pty.openpty()
        except OSError as e:
            self.skipTest(f"no pseudo-terminal here: {e}")
        got, opened = [], threading.Event()
        link = serial_link.SerialLink(got.append, opened.set, port=os.ttyname(slave))
        link.start()
        try:
            self.assertTrue(opened.wait(5))
            os.write(master, b"I (1) boot: a log line\n" + serial_link.encode({"type": "hello", "version": 1}))
            for _ in range(100):
                if got:
                    break
                threading.Event().wait(0.02)
            self.assertEqual(got, [{"type": "hello", "version": 1}])
            self.assertTrue(link.send({"type": "ping"}))
            self.assertIn(b'{"type":"ping"}', os.read(master, 4096))
        finally:
            link.stopping = True
            link.close()
            os.close(master)
            os.close(slave)

    def test_a_deck_that_stops_reading_does_not_hold_up_the_hooks(self):
        """A firmware that never reads the line fills the port's buffer; a hook must still go through at once, its
        records dropped as a deck not reading drops them, not waited on under the bridge's lock."""
        import pty
        try:
            master, slave = pty.openpty()
        except OSError as e:
            self.skipTest(f"no pseudo-terminal here: {e}")
        b = bridge.Bridge(None, io.StringIO())
        opened = threading.Event()
        link = serial_link.SerialLink(b.on_record, None, port=os.ttyname(slave))
        b.attach(link)
        bridge_open = link.on_open  # attach() binds the link's opening to the bridge: told here as well
        link.on_open = lambda: (bridge_open(), opened.set())
        link.start()
        filler = os.open(os.ttyname(slave), os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self.assertTrue(opened.wait(5))
            try:
                while True:
                    os.write(filler, b"x" * 1024)  # Nobody reads the master side: the buffer fills
            except BlockingIOError:
                pass
            started = time.monotonic()
            for n in range(5):
                b.take("hook", h("SessionStart", sid=f"s{n}"))
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            link.stopping = True
            link.close()
            for fd in (filler, master, slave):
                os.close(fd)


# Made-up networks and passwords only: a real one never goes in this file (DESIGN.md §9).
HARDWARE_PORTS = """
Hardware Port: Ethernet Adapter (en3)
Device: en3
Ethernet Address: 02:00:00:00:00:03

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 02:00:00:00:00:00

VLAN Configurations
===================
"""


class FakeMacOS:
    """networksetup and security as the bridge runs them, the saved networks and passwords made up. `gate`, while
    clear, keeps `security` waiting, as the admin prompt does."""

    def __init__(self, saved=("Studio",), passwords=None, keychain_exit=0, gate=None, ports=HARDWARE_PORTS):
        self.saved, self.ports = saved, ports
        self.passwords = {"Studio": "test-pass", "Loft": "loft-pass"} if passwords is None else passwords
        self.keychain_exit, self.gate = keychain_exit, gate
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        assert isinstance(argv, list) and not kw.get("shell")
        code, out = 0, ""
        if argv[1:] == ["-listallhardwareports"]:
            out = self.ports
        elif argv[1:] == ["-listpreferredwirelessnetworks", "en0"]:
            out = "Preferred networks on en0:\n" + "".join(f"\t{n}\n" for n in self.saved)
        elif argv[1] == "find-generic-password":
            if self.gate is not None:
                self.gate.wait(5)
            name = argv[argv.index("-a") + 1]
            if self.keychain_exit:
                code = self.keychain_exit
            elif name in self.passwords:
                out = self.passwords[name] + "\n"
            else:
                code = wifi.ITEM_NOT_FOUND
        else:
            code = 1
        return subprocess.CompletedProcess(argv, code, out.encode(), b"")

    def keychain_asked(self) -> list[str]:
        return [c[c.index("-a") + 1] for c in self.calls if c[1] == "find-generic-password"]


def scan(*networks):
    """A wifi_join of (name, rssi, secure) networks, strongest first."""
    return {"type": "wifi_join", "names": [n[0] for n in networks], "rssi": [n[1] for n in networks],
            "secure": [n[2] for n in networks]}


class Wifi(unittest.TestCase):
    def test_the_wifi_port_is_read_from_networksetup(self):
        self.assertEqual(wifi.parse_hardware_ports(HARDWARE_PORTS), "en0")
        self.assertIsNone(wifi.parse_hardware_ports(HARDWARE_PORTS.replace("Wi-Fi", "Bluetooth PAN")))
        self.assertIsNone(wifi.parse_hardware_ports(""))
        self.assertEqual(wifi.wifi_device(FakeMacOS()), "en0")
        self.assertIsNone(wifi.wifi_device(lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"")))

    def test_the_saved_networks_are_read_from_networksetup(self):
        text = "Preferred networks on en0:\n\tStudio\n\tHome Office \n\tLoft\n"
        self.assertEqual(wifi.parse_preferred(text), ["Studio", "Home Office ", "Loft"])  # a name's spaces kept
        self.assertEqual(wifi.parse_preferred("Preferred networks on en0:\n"), [])
        self.assertEqual(wifi.parse_preferred("en3 is not a Wi-Fi interface.\n** Error: Error obtaining wireless "
                                              "information.\n"), [])
        self.assertEqual(wifi.saved_networks("en0", FakeMacOS(saved=("Studio", "Loft"))), ["Studio", "Loft"])
        self.assertEqual(wifi.saved_networks("en3", FakeMacOS()), [])
        self.assertEqual(wifi.Mac(FakeMacOS(saved=("Loft",))).saved(), ["Loft"])

    def test_no_wifi_port_means_no_saved_networks(self):
        fake = FakeMacOS(ports=HARDWARE_PORTS.replace("Wi-Fi", "Bluetooth PAN"))
        self.assertEqual(wifi.Mac(fake).saved(), [])
        self.assertNotIn("-listpreferredwirelessnetworks", [c[1] for c in fake.calls])

    def test_the_candidates_are_the_scanned_networks_the_mac_knows_in_the_decks_order(self):
        self.assertEqual(wifi.candidates(["Cafe", "Loft", "Studio", "Loft"], {"Studio", "Loft"}), ["Loft", "Studio"])
        self.assertEqual(wifi.candidates(["Cafe"], ["Studio"]), [])

    def test_the_keychain_is_asked_with_the_name_as_an_argument(self):
        fake = FakeMacOS(passwords={"Studio; rm -rf ~": "test pass \n"})
        self.assertEqual(wifi.keychain_password("Studio; rm -rf ~", fake), ("test pass \n", None))  # one newline off
        self.assertEqual(fake.calls[0], ["/usr/bin/security", "find-generic-password", "-D", "AirPort network password",
                                         "-a", "Studio; rm -rf ~", "-w"])

    def test_the_keychains_refusals_have_their_reasons(self):
        for code, why in ((44, "no_password"), (128, "cancelled"), (51, "cancelled"), (36, "cancelled"),
                          (1, "no_password")):
            self.assertEqual(wifi.keychain_password("Studio", FakeMacOS(keychain_exit=code)), (None, why), code)

        def late(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])

        self.assertEqual(wifi.keychain_password("Studio", late), (None, "cancelled"))  # the prompt left unanswered
        self.assertEqual(wifi.keychain_password("Studio", missing), (None, "no_password"))

    def test_the_prompt_is_given_minutes_not_seconds(self):
        seen = {}

        def run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, b"test-pass\n", b"")

        wifi.keychain_password("Studio", run)
        self.assertGreaterEqual(seen["timeout"], 120)
        self.assertNotIn("shell", seen)


class JoinFromTheDeck(unittest.TestCase):
    def bridge(self, fake, record_path=None, out=None):
        sent = []

        keys = self.keys = []

        class FakeLink:
            connected = True
            kind = "usb"

            def send(self, r):
                if r.get("type") == "wifi":  # Each network goes with a new key, 64 hex digits: set aside here
                    r = dict(r)
                    keys.append(r.pop("key"))
                    assert re.fullmatch(r"[0-9a-f]{64}", keys[-1])
                sent.append(r)
                return True

        b = bridge.Bridge(record_path, out or io.StringIO())
        b.attach(FakeLink())
        b.mac = wifi.Mac(fake)
        return b, sent

    @staticmethod
    def settle(b):
        t = b.wifi_thread
        if t is not None:
            t.join(5)

    def test_one_network_both_know_is_sent_with_its_password(self):
        b, sent = self.bridge(FakeMacOS(saved=("Studio", "Elsewhere")))
        b.link.on_record(scan(("Cafe", -40, True), ("Studio", -48, True)))
        self.settle(b)
        self.assertEqual(sent, [{"type": "wifi", "name": "Studio", "password": "test-pass"}])

    def test_several_are_offered_in_the_decks_order_with_its_signals(self):
        b, sent = self.bridge(FakeMacOS(saved=("Studio", "Loft")))
        b.link.on_record(scan(("Loft", -48, True), ("Cafe", -55, False), ("Studio", -63, True)))
        self.settle(b)
        self.assertEqual(sent, [{"type": "wifi_choose", "names": ["Loft", "Studio"], "rssi": [-48, -63]}])

    def test_a_pick_of_one_offered_gets_its_password(self):
        fake = FakeMacOS(saved=("Studio", "Loft"))
        b, sent = self.bridge(fake)
        b.link.on_record(scan(("Loft", -48, True), ("Studio", -63, True)))
        self.settle(b)
        self.assertEqual(fake.keychain_asked(), [])  # nothing read before the person picks
        sent.clear()
        b.link.on_record({"type": "wifi_pick", "name": "Studio"})
        self.settle(b)
        self.assertEqual(sent, [{"type": "wifi", "name": "Studio", "password": "test-pass"}])
        self.assertEqual(fake.keychain_asked(), ["Studio"])

    def test_a_pick_never_offered_is_out_of_reach_and_reads_nothing(self):
        fake = FakeMacOS(saved=("Studio", "Loft"))
        b, sent = self.bridge(fake)
        b.link.on_record({"type": "wifi_pick", "name": "Studio"})  # no join before it
        b.link.on_record(scan(("Loft", -48, True), ("Studio", -63, True)))
        self.settle(b)
        b.link.on_record({"type": "wifi_pick", "name": "Cafe"})
        b.link.on_record({"type": "wifi_pick", "name": 3})
        self.settle(b)
        self.assertEqual([r for r in sent if r["type"] == "wifi_error"],
                         [{"type": "wifi_error", "reason": "not_in_reach"}] * 3)
        self.assertEqual(fake.keychain_asked(), [])

    def test_no_network_both_know_is_out_of_reach(self):
        fake = FakeMacOS(saved=("Studio",))
        b, sent = self.bridge(fake)
        b.link.on_record(scan(("Cafe", -40, True), ("Neighbour", -70, True)))
        self.settle(b)
        self.assertEqual(sent, [{"type": "wifi_error", "reason": "not_in_reach"}])
        self.assertEqual(fake.keychain_asked(), [])

    def test_no_keychain_item_is_no_password(self):
        b, sent = self.bridge(FakeMacOS(saved=("Studio",), passwords={}))
        b.link.on_record(scan(("Studio", -48, True)))
        self.settle(b)
        self.assertEqual(sent, [{"type": "wifi_error", "reason": "no_password"}])

    def test_a_cancelled_prompt_is_cancelled(self):
        b, sent = self.bridge(FakeMacOS(saved=("Studio",), keychain_exit=128))
        b.link.on_record(scan(("Studio", -48, True)))
        self.settle(b)
        self.assertEqual(sent, [{"type": "wifi_error", "reason": "cancelled"}])

    def test_an_open_network_gets_no_password_and_no_keychain(self):
        fake = FakeMacOS(saved=("Cafe",))
        b, sent = self.bridge(fake)
        b.link.on_record(scan(("Cafe", -40, False)))
        self.settle(b)
        self.assertEqual(sent, [{"type": "wifi", "name": "Cafe", "password": ""}])
        self.assertEqual(fake.keychain_asked(), [])

    def test_a_second_join_while_the_prompt_is_up_is_ignored(self):
        gate = threading.Event()
        fake = FakeMacOS(saved=("Studio", "Loft"), gate=gate)
        b, sent = self.bridge(fake)
        b.link.on_record(scan(("Studio", -48, True)))
        for _ in range(100):
            if fake.keychain_asked():
                break
            time.sleep(0.01)
        started = time.monotonic()
        b.link.on_record(scan(("Loft", -40, True)))  # the serial reader is not held up by the prompt either
        b.on_record({"type": "hello", "version": 3})
        self.assertLess(time.monotonic() - started, 0.5)
        gate.set()
        self.settle(b)
        self.assertEqual([r for r in sent if r["type"].startswith("wifi")],
                         [{"type": "wifi", "name": "Studio", "password": "test-pass"}])
        self.assertEqual(fake.keychain_asked(), ["Studio"])

    def test_nothing_is_sent_with_no_deck_attached(self):
        b, sent = self.bridge(FakeMacOS())
        b.link.connected = False
        b.link.on_record(scan(("Studio", -48, True)))
        self.settle(b)
        self.assertEqual(sent, [])

    def test_the_password_and_the_scan_reach_the_cable_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, out, err = os.path.join(tmp, "inputs.jsonl"), io.StringIO(), io.StringIO()
            b, sent = self.bridge(FakeMacOS(saved=("Studio",)), record_path=path, out=out)
            with contextlib.redirect_stderr(err):
                b.take("hook", h("SessionStart"))  # the recording and stdout at work
                b.link.on_record(scan(("Neighbour", -40, True), ("Studio", -48, True)))
                self.settle(b)
                b.link.on_record({"type": "wifi_pick", "name": "Studio"})
                self.settle(b)
                b.take("hook", h("Stop"))
            b.record.close()
            with open(path, encoding="utf-8") as f:
                recorded = f.read()
            self.assertIn("SessionStart", recorded)
            self.assertIn({"type": "wifi", "name": "Studio", "password": "test-pass"}, sent)
            for where in (recorded, out.getvalue(), err.getvalue()):
                self.assertNotIn("test-pass", where)
                self.assertNotIn("Neighbour", where)
                self.assertNotIn("wifi", where)

    def test_a_damaged_wifi_frame_keeps_its_body_out_of_the_boards_log(self):
        frame = serial_link.encode(scan(("Neighbour", -40, True)))
        damaged = b"I (812) usb_line: half a log li" + frame.replace(b"Neighbour", b"Neighbur").rstrip(b"\n")
        self.assertIsNone(serial_link.decode(damaged))
        text = serial_link.log_text(damaged)
        self.assertNotIn("Neighb", text)
        self.assertTrue(text.startswith("I (812) usb_line: half a log li@deck "))
        self.assertEqual(serial_link.log_text(b"I (1) boot: a log line\r"), "I (1) boot: a log line")


    def paired(self, keychain_ok=True):
        """A deck that said hello, sent one network and its key; and what the Keychain's `security` was given."""
        b, sent = self.bridge(FakeMacOS())
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw.get("input")))
            return subprocess.CompletedProcess(argv, 0, "", "" if keychain_ok else "security: refused\n")

        b.pairing_run = run
        with contextlib.redirect_stderr(io.StringIO()):
            b.link.on_record({"type": "hello", "version": 4, "id": "3f2a", "board": "jc3636k718", "link": "usb"})
        b.link.on_record(scan(("Studio", -48, True)))
        self.settle(b)
        return b, sent, calls

    STATUS = {"type": "wifi_status", "id": "3f2a", "host": "agentdeck-3f2a.local", "address": "192.168.1.42",
              "port": 8443, "fingerprint": "ab" * 32}

    def test_the_deck_says_where_it_is_and_the_pairing_is_kept(self):
        b, sent, calls = self.paired()
        key = self.keys[-1]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            b.link.on_record(dict(self.STATUS))
        self.assertIn("paired with the deck 3f2a at agentdeck-3f2a.local:8443, certificate abababababababab…",
                      err.getvalue())
        argv, stdin = calls[-1]
        self.assertEqual(argv, [pairing.SECURITY, "-i"])
        self.assertNotIn(key, " ".join(argv))  # never in a process's arguments
        data = bytes.fromhex(stdin.split(" -X ")[1].split()[0])
        self.assertEqual(json.loads(data), {"key": key, "fingerprint": "ab" * 32, "host": "agentdeck-3f2a.local",
                                            "port": 8443})
        self.assertIn("-s alxvlabs-agent-deck -a 3f2a", stdin)
        self.assertEqual(sent[-1], {"type": "wifi_paired"})  # the deck's page leaves its bridge step
        self.assertNotIn(key, err.getvalue())
        self.assertIsNone(b.pair_key)

    def test_no_pairing_without_a_key_sent_or_from_another_deck_or_off_the_cable(self):
        b, _, calls = self.paired()
        n = len(calls)
        with contextlib.redirect_stderr(io.StringIO()):
            b.link.on_record({**self.STATUS, "id": "beef"})                 # not the deck that said hello
            b.on_record(dict(self.STATUS), source=type("W", (), {"kind": "wifi", "connected": True})())
            b.link.on_record({**self.STATUS, "fingerprint": "AB" * 32})     # not a fingerprint
        self.assertEqual(len(calls), n)
        b.pair_key = None
        b.link.on_record(dict(self.STATUS))                                 # a rejoin with no key sent
        self.assertEqual(len(calls), n)

    def test_a_pairing_the_keychain_refuses_is_said_and_the_key_kept_for_another_try(self):
        b, sent, _ = self.paired(keychain_ok=False)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            b.link.on_record(dict(self.STATUS))
        self.assertIn("was not kept in the Keychain", err.getvalue())
        self.assertIsNotNone(b.pair_key)
        self.assertNotIn({"type": "wifi_paired"}, sent)

    def test_losing_the_deck_drops_the_key_sent(self):
        b, _, _ = self.paired()
        b.link.on_lost()
        self.assertIsNone(b.pair_key)

    def test_a_pairing_reads_back_from_the_keychain(self):
        record = {"key": "cd" * 32, "fingerprint": "ab" * 32, "host": "agentdeck-3f2a.local", "port": 8443}

        def run(argv, **kw):
            self.assertEqual(argv, [pairing.SECURITY, "find-generic-password", "-s", pairing.SERVICE, "-a", "3f2a", "-w"])
            return subprocess.CompletedProcess(argv, 0, json.dumps(record) + "\n", "")

        self.assertEqual(pairing.load("3f2a", run=run), record)
        self.assertIsNone(pairing.load("3F2A", run=run))
        self.assertIsNone(pairing.load("3f2a", run=lambda a, **k: subprocess.CompletedProcess(a, 44, "", "")))

class EndToEnd(unittest.TestCase):
    def forward(self, port, kind, data):
        env = {**os.environ, "DECK_BRIDGE_PORT": str(port)}
        return subprocess.run([sys.executable, os.path.join(HERE, "hook.py"), kind], input=json.dumps(data),
                              capture_output=True, text=True, env=env, timeout=10)

    def test_hook_py_through_a_running_bridge_and_a_stopped_one(self):
        out = io.StringIO()
        b = bridge.Bridge(None, out)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.handler(b))
        except OSError as e:
            self.skipTest(f"cannot listen on 127.0.0.1 here: {e}")
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            r = self.forward(port, "hook", h("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}))
            self.assertEqual((r.returncode, r.stdout), (0, ""))  # No decision: the terminal asks
            r = self.forward(port, "status", {"session_id": "s1", "model": {"display_name": "Opus"}})
            self.assertEqual(r.stdout.strip(), "deck | Opus")
        finally:
            server.shutdown()
            server.server_close()
        types = [json.loads(line)["type"] for line in out.getvalue().splitlines()]
        self.assertEqual(types, ["agent", "request"])
        r = self.forward(port, "hook", h("Stop"))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        r = self.forward(port, "status", {})
        self.assertEqual(r.stdout.strip(), "deck off")



class OverWifi(unittest.TestCase):
    """The link over Wi-Fi (net_link.py): the challenge's answer, the pin, the bridge's two links, and a session against
    a stand-in deck — a local TLS server with a throwaway certificate."""

    KEY = "cd" * 32

    def test_the_answer_is_the_hmac_of_the_nonce_under_the_key(self):
        nonce = bytes(range(32))
        reply = net_link.answer(b"agentdeck-challenge " + nonce.hex().encode(), self.KEY)
        want = hmac.new(bytes.fromhex(self.KEY), nonce, hashlib.sha256).hexdigest()
        self.assertEqual(reply, b"agentdeck-answer " + want.encode() + b"\n")
        for bad in (b"agentdeck-challenge 00", b"agentdeck-challenge " + b"zz" * 32, b"hello"):
            self.assertIsNone(net_link.answer(bad, self.KEY))
        self.assertIsNone(net_link.answer(b"agentdeck-challenge " + nonce.hex().encode(), "00"))

    def test_only_the_certificate_paired_is_taken(self):
        der = b"certificate"
        self.assertTrue(net_link.pinned(der, hashlib.sha256(der).hexdigest()))
        self.assertFalse(net_link.pinned(der, "00" * 32))
        self.assertFalse(net_link.pinned(None, hashlib.sha256(der).hexdigest()))

    @staticmethod
    def links():
        class Cable:  # a SerialLink: it stands down for nothing
            kind = "usb"

            def __init__(self):
                self.sent, self.connected = [], False

            def send(self, r):
                self.sent.append(r)
                return True

            def open(self):  # as a real link does: connected, then told
                self.connected = True
                self.on_open()

            def lose(self):
                self.connected = False
                self.on_lost()

        class Net(Cable):
            kind, grace_s = "wifi", 0.3

            def __init__(self):
                super().__init__()
                self.paused, self.paired = [], True

            def pause(self, p):
                self.paused.append(p)

            def reachable(self):
                return self.paired

            standby = None  # set by the bridge as it attaches

        b = bridge.Bridge(None, io.StringIO(), hold_s=5)
        cable, net = Cable(), Net()
        b.attach(cable)
        b.attach(net)
        return b, cable, net

    def test_the_cable_wins_and_the_wifi_link_stands_down_while_it_is_there(self):
        b, cable, net = self.links()
        self.assertIs(b.link, cable)
        net.open()
        self.assertIs(b.link, net)
        self.assertEqual(net.sent, [{"type": "hello"}])
        cable.open()
        self.assertIs(b.link, cable)
        self.assertEqual(net.paused, [True])
        net.lose()  # the Wi-Fi session ending as it stands down: nothing held is let go
        self.assertIs(b.link, cable)
        cable.lose()
        self.assertEqual(net.paused, [True, False])

    def test_a_wifi_link_opening_while_the_cable_is_open_stands_down(self):
        """The race the board showed: the Wi-Fi link's connect finishing after the cable opened."""
        b, cable, net = self.links()
        self.assertTrue(net.standby is not None)
        cable.open()
        net.open()
        self.assertIs(b.link, cable)
        self.assertEqual(net.paused[-1], True)
        self.assertEqual(net.sent, [])  # never asked for a hello: it is not the bridge's link

    def test_the_link_in_use_lost_with_the_cable_still_open_falls_back_to_it(self):
        b, cable, net = self.links()
        net.open()
        cable.connected = True  # the cable open behind it, its own opening missed
        t, result = Hold().held(b)
        net.lose()
        self.assertIs(b.link, cable)
        self.assertEqual(cable.sent[-1], {"type": "hello"})
        t.join(0.2)
        self.assertTrue(t.is_alive())  # nothing let go
        b.decide(1, True)
        t.join(2)
        self.assertEqual(result["answer"], "allow")

    def test_a_hello_on_the_cable_makes_it_the_link(self):
        b, cable, net = self.links()
        net.open()
        cable.connected = True
        with contextlib.redirect_stderr(io.StringIO()):
            cable.on_record({"type": "hello", "version": 4, "id": "3f2a", "board": "jc3636k718", "link": "usb"})
        self.assertIs(b.link, cable)
        self.assertIn("reset", [r["type"] for r in cable.sent])  # the snapshot on the cable

    def test_a_loss_off_the_link_in_use_lets_nothing_go(self):
        b, cable, net = self.links()
        cable.open()
        t, result = Hold().held(b)
        net.lose()
        t.join(0.3)
        self.assertTrue(t.is_alive())
        b.decide(1, True)
        t.join(2)
        self.assertEqual(result["answer"], "allow")

    def test_the_cable_pulled_keeps_what_is_held_for_the_deck_back_on_wifi(self):
        b, cable, net = self.links()
        cable.open()
        t, result = Hold().held(b)
        cable.lose()  # unplugged to leave the desk: the Wi-Fi link's grace, not the cable's none
        t.join(0.1)
        self.assertTrue(t.is_alive())
        net.open()
        with contextlib.redirect_stderr(io.StringIO()):
            net.on_record({"type": "hello", "version": 4, "id": "3f2a", "board": "jc3636k718", "link": "wifi"})
        self.assertIn("request", [r["type"] for r in net.sent])  # back on the glass over Wi-Fi
        t.join(0.4)
        self.assertTrue(t.is_alive())  # past the grace: the deck came back in it
        net.on_record({"type": "decision", "id": 1, "allow": True})
        t.join(2)
        self.assertEqual(result["answer"], "allow")

    def test_the_cable_pulled_with_no_deck_paired_lets_what_is_held_go(self):
        b, cable, net = self.links()
        net.paired = False
        cable.open()
        t, result = Hold().held(b)
        cable.lose()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertIsNone(result["answer"])

    def test_a_session_against_a_stand_in_deck(self):
        if shutil.which("openssl") is None:
            self.skipTest("no openssl to make a throwaway certificate")
        folder = tempfile.mkdtemp(prefix="bridge-tls-")
        cert, key = os.path.join(folder, "c.pem"), os.path.join(folder, "k.pem")
        made = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                               "-nodes", "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=agentdeck"],
                              capture_output=True)
        if made.returncode != 0:
            self.skipTest("openssl could not make a certificate here")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        der = ssl.PEM_cert_to_DER_cert(open(cert).read())
        heard = []

        def deck(answer_key):
            conn, _ = listener.accept()
            with context.wrap_socket(conn, server_side=True) as tls:
                nonce = os.urandom(32)
                tls.sendall(b"agentdeck-challenge " + nonce.hex().encode() + b"\n")
                line = tls.recv(256).strip()
                want = hmac.new(bytes.fromhex(answer_key), nonce, hashlib.sha256).hexdigest().encode()
                if line != b"agentdeck-answer " + want:
                    return
                tls.sendall(b"agentdeck-ok\n" + serial_link.encode({"type": "hello", "version": 4}))
                heard.append(tls.recv(4096))

        record = {"key": self.KEY, "fingerprint": hashlib.sha256(der).hexdigest(), "host": "127.0.0.1", "port": port}
        got, opened = [], threading.Event()
        link = net_link.NetLink(lambda: ["3f2a"], lambda i: record, on_record=got.append, on_open=opened.set)
        server = threading.Thread(target=deck, args=(self.KEY,), daemon=True)
        server.start()
        link.start()
        try:
            self.assertTrue(opened.wait(10))
            for _ in range(100):
                if got:
                    break
                time.sleep(0.02)
            self.assertEqual(got, [{"type": "hello", "version": 4}])  # the line right after the ok, not lost
            link.send({"type": "ping"})
            server.join(5)
            self.assertEqual(heard, [serial_link.encode({"type": "ping"})])
        finally:
            link.stopping = True
            link.pause(True)
            listener.close()

    def test_a_deck_serving_another_certificate_is_not_connected(self):
        link = net_link.NetLink(lambda: [], lambda i: None)
        record = {"key": self.KEY, "fingerprint": "00" * 32, "host": "127.0.0.1", "port": 9}
        self.assertIsNone(link.connect("3f2a", record))  # nothing there: not connected, and nothing raised


if __name__ == "__main__":
    unittest.main()