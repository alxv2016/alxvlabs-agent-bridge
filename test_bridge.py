"""The bridge's checks, with no board and no Claude Code (DESIGN.md §2).

    python3 -m unittest discover -s .

Every fixtures/<name>.inputs.jsonl replays into its <name>.records.jsonl; then the rules one by one, the
installer on a scratch settings file, and hook.py through a running bridge.
"""

from __future__ import annotations

import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bridge  # noqa: E402
import hook  # noqa: E402
import install  # noqa: E402
import plugin_state  # noqa: E402
from replay import load, replay  # noqa: E402
from state import BODY_BYTES, Deck, cut_body, to_glass  # noqa: E402
from transcripts import Transcripts  # noqa: E402
from usage_cache import UsageCache  # noqa: E402
from usage_refresh import UsageRefresh  # noqa: E402
import serial_link  # noqa: E402


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

    def test_the_deck_shows_the_responses_tokens_and_the_context_share(self):
        d = Deck()
        d.feed("hook", h("SessionStart", sid="vs"), 0)
        out = d.feed("transcript", {"session_id": "vs", "model": "claude-opus-5-5", "context_tokens": 664000,
                                    "response_tokens": 1200}, 1)
        self.assertEqual((out[0]["tokens"], out[0]["context_pct"]), (1200, 66))  # opus-5-5's 1M window, known
        out = d.feed("hook", h("UserPromptSubmit", sid="vs"), 2)
        self.assertEqual(out[0]["tokens"], 0)  # a new turn
        d.feed("status", {"session_id": "cli", "model": {"id": "claude-x[1m]"},
                          "context_window": {"context_window_size": 1000000, "used_percentage": 5}}, 3)
        d.feed("hook", h("SessionStart", sid="vs2"), 4)
        out = d.feed("transcript", {"session_id": "vs2", "model": "claude-x", "context_tokens": 250000}, 5)
        self.assertEqual(out[0]["context_pct"], 25)  # the window a status line taught for that model
        d2 = Deck(context_window=200000)
        d2.feed("hook", h("SubagentStart", agent_id="a1", agent_type="Explore"), 0)
        d2.feed("transcript", {"session_id": "s1", "model": "m", "context_tokens": 50000}, 1)
        out = d2.feed("transcript", {"session_id": "s1", "agent_id": "a1", "description": "Find the bug",
                                     "response_tokens": 300}, 2)
        self.assertEqual(d2.agents["s1"].context_pct, 25)
        self.assertEqual((out[0]["name"], out[0]["tokens"]), ("Find the bug", 300))


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
            want = install.REQUEST_TIMEOUT_S if event == "PermissionRequest" else install.HOOK_TIMEOUT_S
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


if __name__ == "__main__":
    unittest.main()
