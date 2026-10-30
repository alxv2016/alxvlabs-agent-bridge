#!/usr/bin/env python3
"""Claude Code's side of the bridge: every hook, the status line and the subagent status line run this.

    hook.py hook              # a hook: forwards its input, prints the bridge's answer to a permission request
    hook.py hook --plugin     # the same, from the Claude Code plugin: starts the bridge when none answers
    hook.py status            # statusLine: forwards its input, prints the status line
    hook.py subagent-status   # subagentStatusLine: forwards its input, keeps Claude Code's own rows

Reads the input JSON on stdin and POSTs it to the bridge on 127.0.0.1 (DECK_BRIDGE_PORT, 8737). With the
bridge stopped it prints nothing and exits 0, so Claude Code carries on as without it — its own permission
prompt included (DESIGN.md §4). Standard library only; never fails a hook.

From the plugin (--plugin), a hook that finds no bridge starts one, detached, so it outlives the session; that
bridge stops by itself once the plugin is disabled or uninstalled (bridge.py --plugin). The plugin's hooks stand
aside while install.py's are in the user settings, so no event reaches the bridge twice.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import time

PORT = int(os.environ.get("DECK_BRIDGE_PORT", "8737"))
TIMEOUT_S = {"hook": 2.0, "status": 0.3, "subagent-status": 0.3}
# A permission request waits while the bridge holds it for the device: under its hook's timeout in install.py and
# hooks/hooks.json.
REQUEST_TIMEOUT_S = 590.0
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = "alxvlabs-agent-deck"  # .claude-plugin/plugin.json's name
SETTINGS = os.path.expanduser("~/.claude/settings.json")
# install.py's entries in the settings, wherever its folder lives: hook.py run as a hook or a status line.
MARK = re.compile(r"hook\.py['\"]? (hook|status|subagent-status)\b")
START_WAIT_S = 2.0  # How long a hook that started the bridge waits for it to listen, inside the hook's 5 s


def post(kind: str, body: bytes, timeout: float) -> bytes | None:
    """The bridge's answer, or None when it is not running."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
        conn.request("POST", f"/{kind}", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return data if resp.status == 200 else b""
    except (OSError, http.client.HTTPException):
        return None


def installed_by_hand(settings_path: str = SETTINGS) -> bool:
    """Whether install.py's hooks are in the user settings: then they reach the bridge, and the plugin's stand aside."""
    try:
        with open(settings_path, encoding="utf-8") as f:
            hooks = json.load(f).get("hooks", {})
    except (OSError, ValueError, AttributeError):
        return False
    return bool(MARK.search(json.dumps(hooks)))


def log_path() -> str:
    """Where a bridge the plugin starts writes its lines: outside the plugin's folders, which an update or an
    uninstall deletes."""
    base = "~/Library/Logs" if sys.platform == "darwin" else "~/.local/state"
    return os.path.join(os.path.expanduser(base), "agent-deck-bridge.log")


def start_bridge() -> None:
    """The bridge, detached: its own session, no terminal, so it runs on after this hook and its session end. Two
    hooks starting one each at once is harmless: the second cannot take the port and goes."""
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    with open(path, "ab") as log:
        subprocess.Popen([sys.executable, os.path.join(HERE, "bridge.py"), "--quiet", "--plugin", PLUGIN],
                         cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)


def post_starting(kind: str, body: bytes, timeout: float) -> bytes | None:
    """post(), starting the bridge first when none answers, then giving this input to the new bridge."""
    answer = post(kind, body, timeout)
    if answer is not None:
        return answer
    try:
        start_bridge()
    except OSError:
        return None
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(0.1)
        answer = post(kind, body, timeout)
        if answer is not None:
            return answer
    return None


def status_line(data: dict, connected: bool) -> str:
    parts = ["deck" if connected else "deck off"]
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {round(ctx)}%")
    limits = data.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{label} {round(pct)}%")
    return " | ".join(parts)


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    plugin = "--plugin" in sys.argv[2:]
    if kind not in TIMEOUT_S:
        print(__doc__, file=sys.stderr)
        return 0
    if plugin and installed_by_hand():
        return 0
    body = sys.stdin.buffer.read()
    timeout = REQUEST_TIMEOUT_S if kind == "hook" and b'"PermissionRequest"' in body else TIMEOUT_S[kind]
    answer = (post_starting if plugin and kind == "hook" else post)(kind, body, timeout)
    if kind == "hook" and answer:
        sys.stdout.buffer.write(answer)
    elif kind == "status":
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        print(status_line(data if isinstance(data, dict) else {}, answer is not None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
