#!/usr/bin/env python3
"""Sets the agent deck's bridge up on this Mac in one go, takes it all out again, or says how it stands.

    install.py [--login | --no-login] [--dry-run] [--settings ~/.claude/settings.json]
    install.py --uninstall [--dry-run]
    install.py --status

Two things, both undone by --uninstall:

- Claude Code's hooks and status lines, in its user settings, running hook.py with this Python.
- On macOS, only if you say yes: a login agent (~/Library/LaunchAgents/local.agent-deck.bridge.plist) — the bridge
  starts now and at every login, quiet, and starts again if it stops; its log is ~/Library/Logs/agent-deck-bridge.log.
  It asks, and no is the default: then nothing starts by itself, and you run bridge.py by hand. --login and
  --no-login answer for it; with no terminal to ask in, the answer is no. Saying no removes a login agent set up
  before. Move the folder, and run install.py again from its new place.

Every hook the deck reads, the statusLine and the subagentStatusLine run hook.py
(DESIGN.md §5). The settings file is backed up beside itself first (settings.json.deck-backup-<time>);
other hooks are left as they are. An existing statusLine or subagentStatusLine that is not the bridge's
stops the install — move it aside first. --dry-run prints the result and writes nothing.

The Claude Code plugin (alxvlabs-agent-deck) is the other way to set the bridge up; use one or the other. While
these hooks are in, the plugin's stand aside (hook.py), so nothing reaches the bridge twice.

Run it yourself: an agent's sandbox may not write ~/.claude. Claude Code reloads the file while it runs; a
session in a folder whose workspace trust was not accepted runs no hooks.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time

import plugin_state
from hook import MARK, PLUGIN

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_PY = os.path.join(HERE, "hook.py")
BRIDGE_PY = os.path.join(HERE, "bridge.py")
LABEL = "local.agent-deck.bridge"
AGENT_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOG = os.path.expanduser("~/Library/Logs/agent-deck-bridge.log")
BRIDGE_PORT = int(os.environ.get("DECK_BRIDGE_PORT", "8737"))
EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
          "PermissionRequest", "PermissionDenied", "Notification", "Stop", "SubagentStart", "SubagentStop")
HOOK_TIMEOUT_S = 5
REQUEST_TIMEOUT_S = 600  # A permission request or question the bridge holds for the device: over hook.py's REQUEST_TIMEOUT_S
HELD_EVENTS = ("PermissionRequest", "PreToolUse")  # hook.py's own limits keep every other PreToolUse to 2 s


def command(kind: str) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(HOOK_PY)} {kind}"


def ours(entry) -> bool:
    return bool(MARK.search(json.dumps(entry)))


def strip(settings: dict) -> dict:
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept = [m for m in hooks[event] if not ours(m)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    for key in ("statusLine", "subagentStatusLine"):
        if ours(settings.get(key)):
            del settings[key]
    return settings


def add(settings: dict) -> dict:
    for key in ("statusLine", "subagentStatusLine"):
        if key in settings and not ours(settings[key]):
            sys.exit(f"install: {key} is already set to something else — move it aside first:\n"
                     f"  {json.dumps(settings[key])}")
    settings = strip(settings)
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": command("hook"),
                        "timeout": REQUEST_TIMEOUT_S if event in HELD_EVENTS else HOOK_TIMEOUT_S}]})
    settings["statusLine"] = {"type": "command", "command": command("status")}
    settings["subagentStatusLine"] = {"type": "command", "command": command("subagent-status")}
    return settings


# The login agent (macOS) ----------------------------------------------------------------------------------------

def agent_plist() -> bytes:
    """The bridge as a login agent: this Python, quiet, started again if it stops. launchd's PATH is bare, so the
    folder `claude` is in joins it, for the bridge's `claude -p /usage`."""
    claude = shutil.which("claude")
    dirs = ([os.path.dirname(claude)] if claude else []) + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [sys.executable, BRIDGE_PY, "--quiet"],
        "WorkingDirectory": HERE,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        # No __pycache__ beside the scripts: the folder stays as it was copied.
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(dirs)), "PYTHONDONTWRITEBYTECODE": "1"},
        "StandardOutPath": LOG,
        "StandardErrorPath": LOG,
    })


def launchctl(*args: str) -> bool:
    try:
        return subprocess.run(["launchctl", *args], capture_output=True).returncode == 0
    except OSError:
        return False


def start_at_login() -> bool:
    os.makedirs(os.path.dirname(AGENT_PLIST), exist_ok=True)
    with open(AGENT_PLIST, "wb") as f:
        f.write(agent_plist())
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")  # an older copy, if there is one
    return launchctl("bootstrap", f"gui/{os.getuid()}", AGENT_PLIST)


def stop_at_login() -> None:
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if os.path.exists(AGENT_PLIST):
        os.remove(AGENT_PLIST)


LOGIN_QUESTION = """
Start the bridge at login?

  yes  The bridge starts now and every time you log in, in the background with no window, and starts again if it
       ever stops. macOS should list it in System Settings > General > Login Items, where you can switch it off.
       Its log is ~/Library/Logs/agent-deck-bridge.log. `python3 install.py --uninstall` removes it.
  no   Nothing starts by itself. Run `python3 bridge.py` in a terminal while you want the deck; Ctrl-C stops it.

Start at login? [y/N] """


def choose_login(login: bool, no_login: bool, ask=input, tty: bool | None = None) -> bool:
    """Whether to set up the login agent: the flag if one was given, else the answer to LOGIN_QUESTION — no unless
    the answer is yes, and no when there is no terminal to ask in."""
    if login or no_login:
        return login
    if not (sys.stdin.isatty() if tty is None else tty):
        return False
    try:
        return ask(LOGIN_QUESTION).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def health() -> dict | None:
    """The running bridge's own account (GET /health), or None when none answers."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", BRIDGE_PORT, timeout=1)
        conn.request("GET", "/health")
        answer = json.loads(conn.getresponse().read() or b"{}")
        conn.close()
        return answer
    except (OSError, ValueError, http.client.HTTPException):
        return None


def status(settings_path: str) -> int:
    try:
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    hooks = any(ours(m) for ms in settings.get("hooks", {}).values() for m in ms)
    print(f"hooks in Claude Code:   {'yes' if hooks else 'no'} ({settings_path})")
    if plugin_state.installs(PLUGIN):
        print(f"the {PLUGIN} plugin:  installed, {'on' if plugin_state.enabled(PLUGIN) else 'off'}"
              f"{' (standing aside for these hooks)' if hooks else ''}")
    if sys.platform == "darwin":
        loaded = launchctl("print", f"gui/{os.getuid()}/{LABEL}")
        print(f"starts at login:        {'yes' if os.path.exists(AGENT_PLIST) else 'no'}"
              f"{', loaded' if loaded else ''}")
    h = health()
    if h is None:
        print(f"bridge running:         no (nothing on 127.0.0.1:{BRIDGE_PORT})")
        return 1
    print("bridge running:         yes")
    print(f"deck attached:          {'yes, ' + h['port'] if h.get('deck') else 'no'}")
    return 0 if h.get("deck") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--settings", default=os.path.expanduser("~/.claude/settings.json"))
    ap.add_argument("--uninstall", action="store_true", help="take the hooks and the login agent out")
    ap.add_argument("--login", action="store_true", help="start the bridge at login, without asking")
    ap.add_argument("--no-login", action="store_true", help="the hooks only, without asking: run bridge.py by hand")
    ap.add_argument("--status", action="store_true", help="say what is set up and whether the deck is attached")
    ap.add_argument("--dry-run", action="store_true", help="print the settings and the login agent; write nothing")
    args = ap.parse_args()
    if args.status:
        return status(args.settings)
    if args.login and args.no_login:
        ap.error("--login or --no-login, not both")

    try:
        with open(args.settings, encoding="utf-8") as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    result = strip(settings) if args.uninstall else add(settings)
    text = json.dumps(result, indent=2) + "\n"
    if args.dry_run:
        print(text, end="")
        if args.login and sys.platform == "darwin":
            print(agent_plist().decode(), end="")
        return 0

    if os.path.exists(args.settings):
        backup = f"{args.settings}.deck-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        with open(args.settings, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        print(f"install: backed up to {backup}")
    tmp = args.settings + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, args.settings)
    print(f"install: {'removed the bridge from' if args.uninstall else 'added the bridge to'} {args.settings}")
    if plugin_state.installs(PLUGIN):
        print(f"install: the {PLUGIN} plugin is installed too — its hooks "
              + ("take over again" if args.uninstall else "stand aside while these are in; uninstall one of the two"))
    if args.uninstall:
        if sys.platform == "darwin" and os.path.exists(AGENT_PLIST):
            stop_at_login()
            print("install: the bridge no longer starts at login")
        return 0
    if sys.platform != "darwin":
        print("install: start the bridge with `python3 bridge.py` while you want the deck")
        return 0
    if choose_login(args.login, args.no_login):
        if health() is not None and not launchctl("print", f"gui/{os.getuid()}/{LABEL}"):
            print("install: a bridge is running by hand already — stop it (Ctrl-C) and the login agent takes over")
        if start_at_login():
            print(f"install: the bridge runs now and at every login, in the background; its log is {LOG}")
        else:
            print(f"install: the login agent did not load — run bridge.py by hand, or look at {AGENT_PLIST}")
        return 0
    if os.path.exists(AGENT_PLIST):
        stop_at_login()
        print("install: the bridge no longer starts at login")
    print(f"install: start the bridge with `python3 {os.path.relpath(BRIDGE_PY)}` while you want the deck")
    return 0


if __name__ == "__main__":
    sys.exit(main())
