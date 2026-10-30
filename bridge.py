#!/usr/bin/env python3
"""The agent deck's bridge on the Mac: Claude Code's hooks and status line in, the deck's records out.

    bridge.py [--port 8737] [--record FILE] [--context-window 1000000] [--no-usage-cache] [--refresh-usage 3600]
              [--serial /dev/cu.usbmodem101 | --no-serial] [--hold 570] [--answer allow|deny SECONDS] [--plugin NAME]

Listens on 127.0.0.1 only for what hook.py forwards — POST /hook, /status, /subagent-status — runs it through
the state machine (state.py), prints each record as a JSON line on stdout and sends it to the agent deck on the
USB cable (serial_link.py), the board's own log lines on stderr. --record appends every input,
with its arrival time, to FILE as JSON lines: what replay.py plays back and scrub.py cleans for a fixture. A
recording holds prompts, commands and paths as Claude Code sent them; keep it out of the repo.

After each hook it reads the session's transcript, and a sub-agent's, from where it last stopped (transcripts.py):
their context size and a sub-agent's description, for a session in the VS Code extension, which runs no status
line. --context-window gives the window those sessions' context share is taken of, until a terminal session's
status line has told it for their model. Once a second it looks at Claude Code's state file for a new fetch of the
plan's usage (usage_cache.py), so the usage rings follow with no terminal session; while the deck is attached it has
Claude Code fetch that usage every --refresh-usage seconds (usage_refresh.py, `claude -p /usage`, hourly by default and
once as the deck connects, 0 never), as the VS Code extension does not save its own; --no-usage-cache leaves the file unread and refreshes nothing.

With the deck attached, each permission request is held for its answer, up to --hold seconds; Claude Code's own
prompt shows meanwhile, and whichever answers first settles it (DESIGN.md §4). With no deck,
or --no-serial, a request goes back at once with no decision. POST /release {"seconds": N} lets go of the port
for flashing, the boot check or a screenshot; the bridge opens it again afterwards and sends the deck everything.

It takes 127.0.0.1's port before the deck's, and goes quietly when another bridge has it: one bridge at a time.
--plugin NAME is how the Claude Code plugin's hook.py starts it: every PLUGIN_CHECK_S it reads Claude Code's own
files (plugin_state.py) and stops once the plugin, seen installed, is disabled or uninstalled, or its folder is gone
— Claude Code stops nothing it started (DESIGN.md §5). Standard library only. Ctrl-C stops it.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import plugin_state
from serial_link import SerialLink
from state import Deck
from transcripts import Transcripts
from usage_cache import UsageCache
from usage_refresh import UsageRefresh

KINDS = {"/hook": "hook", "/status": "status", "/subagent-status": "subagent-status"}
MAX_INPUT = 1 << 20
DENY_MESSAGE = "Denied on the agent deck."  # What Claude is told; the turn carries on
PING_S = 2.0          # onx/deck_link.h kPingMs: the deck takes the bridge for gone after three missed
DEFAULT_HOLD_S = 570  # Under hook.py's 590 s wait on a request
DEFAULT_REFRESH_S = 3600  # How often `/usage` freshens the plan's usage while the deck is attached — the developer's
                         # choice, 2026-10-29: the shares move slowly, and the resets count down from their moments
MAX_RELEASE_S = 600
PLUGIN_CHECK_S = 10  # How often a bridge the plugin started looks whether the plugin is still installed and on
HERE = os.path.dirname(os.path.abspath(__file__))


class PluginWatch:
    """Whether the plugin that started this bridge still wants it. An uninstall only counts once the plugin has been
    seen listed, so a bridge run from a plugin folder Claude Code was pointed at (`claude --plugin-dir`) stays."""

    def __init__(self, name: str, folder: str = HERE, claude_dir: str = plugin_state.CLAUDE_DIR):
        self.name, self.folder, self.claude_dir = name, folder, claude_dir
        self.seen = False

    def gone(self) -> str | None:
        """Why the bridge should stop, or None."""
        if not os.path.isdir(self.folder):
            return "the plugin's folder is gone (updated or uninstalled)"
        listed = plugin_state.installs(self.name, self.claude_dir)
        if listed is None:
            return None
        if listed:
            self.seen = True
        if not self.seen:
            return None
        if not listed:
            return "the plugin was uninstalled"
        if not plugin_state.enabled(self.name, self.claude_dir):
            return "the plugin was disabled"
        return None


class Bridge:
    def __init__(self, record_path: str | None, out=sys.stdout, context_window: int | None = None,
                 usage_cache: UsageCache | None = None, hold_s: float = 0, quiet: bool = False):
        self.deck = Deck(context_window)
        self.hold_s = hold_s
        self.stand_in: tuple[str, float] | None = None  # --answer: "allow" or "deny" after so many seconds
        self.waiting: dict[int, list] = {}  # request id -> [threading.Event, "allow" | "deny" | None]
        self.transcripts = Transcripts()
        self.usage_cache = usage_cache
        self.lock = threading.Lock()
        self.out = out
        self.quiet = quiet  # no record on stdout: a login agent's log stays small
        self.record = open(record_path, "a", encoding="utf-8") if record_path else None
        self.link: SerialLink | None = None

    # The deck on the cable --------------------------------------------------------------------------------------

    def attach(self, link: SerialLink) -> None:
        self.link = link

    def device_ready(self) -> bool:
        return self.link is not None and self.link.connected

    def on_open(self) -> None:
        """The port opened: asks the deck to say hello. It may still be booting and miss this — then its own hello at
        start does the asking — so the snapshot waits for the deck's hello, sent once it listens."""
        self.link.send({"type": "hello"})

    def send_snapshot(self) -> None:
        with self.lock:
            for r in self.deck.snapshot(time.time()):
                self.link.send(r)

    def on_record(self, r: dict) -> None:
        if r.get("type") == "hello":
            self.send_snapshot()  # The deck started, came back, or was asked: everything, after a reset
        elif r.get("type") == "decision" and isinstance(r.get("id"), int):
            self.decide(r["id"], bool(r.get("allow")))
            if self.link:
                self.link.send({"type": "ack", "id": r["id"]})

    def on_lost(self) -> None:
        """The deck gone — unplugged, reset, or the port lent out: nothing will answer what is held."""
        with self.lock:
            for waiter in self.waiting.values():
                waiter[0].set()

    def ping(self) -> None:
        if self.link is not None:
            self.link.send({"type": "ping"})

    def take(self, kind: str, data: dict) -> list[dict]:
        """Feeds an input and its transcripts' through the state machine; the records it made."""
        made: list[dict] = []
        with self.lock:
            now = time.time()
            inputs = [(kind, data)]
            if kind == "hook" and isinstance(data, dict):
                inputs += [("transcript", t) for t in self.transcripts.observe(data)]
            for k, d in inputs:
                if self.record:
                    self.record.write(json.dumps({"t": round(now, 3), "kind": k, "input": d}) + "\n")
                    self.record.flush()
                records = self.deck.feed(k, d, now)
                made += records
                self.emit(records)
        return made

    def hold(self, rid: int) -> str | None:
        """Waits up to the hold for the device's answer to request `rid`: "allow", "deny", or None."""
        waiter = [threading.Event(), None]
        with self.lock:
            self.waiting[rid] = waiter
            if rid not in self.deck.requests:
                self.waiting.pop(rid, None)
                return None  # Closed while its hook was on its way
        if self.stand_in:
            behavior, after_s = self.stand_in
            threading.Timer(after_s, self.decide, (rid, behavior == "allow")).start()
        waiter[0].wait(self.hold_s)
        with self.lock:
            self.waiting.pop(rid, None)
            if waiter[1] is None and rid in self.deck.requests:
                # Past the hold, or the deck gone: the hook goes back with no decision, so only the terminal can
                # answer now. Closed on the deck, and left out of every snapshot from here on.
                self.emit(self.deck.expire(rid, time.time()))
        return waiter[1]

    def decide(self, rid: int, allow: bool) -> None:
        """The device's answer to request `rid` (phase 5's link calls it)."""
        with self.lock:
            waiter = self.waiting.get(rid)
            if waiter is None or waiter[0].is_set() or rid not in self.deck.requests:
                return  # Held past the hold, or answered in the terminal: Claude Code no longer waits
            waiter[1] = "allow" if allow else "deny"
            self.emit(self.deck.answered(rid, time.time()))
            waiter[0].set()

    def tick(self) -> None:
        cached = self.usage_cache.poll() if self.usage_cache else None
        if cached:
            self.take("usage-cache", cached)
        with self.lock:
            self.emit(self.deck.tick(time.time()))

    def emit(self, records: list[dict]) -> None:
        for r in records:
            if r["type"] == "close" and r["id"] in self.waiting:
                self.waiting[r["id"]][0].set()  # Answered in the terminal: the hook goes back with no decision
            if not self.quiet:
                self.out.write(json.dumps(r) + "\n")
            if self.link is not None:
                self.link.send(r)
        self.out.flush()


def handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            """GET /health: whether the deck is attached, and on which port — what `install.py --status` reads."""
            if self.path != "/health":
                self.send_error(404)
                return
            link = bridge.link
            body = json.dumps({"deck": bridge.device_ready(), "port": link.path if link and link.connected else None,
                               "agents": len(bridge.deck.agents)}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/release":
                self.release()
                return
            kind = KINDS.get(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if kind is None or length > MAX_INPUT:
                self.send_error(404 if kind is None else 413)
                return
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self.send_error(400)
                return
            records = bridge.take(kind, data)
            answer = b""
            rid = next((r["id"] for r in records if r["type"] == "request"), None)
            if rid is not None and bridge.hold_s > 0 and (bridge.stand_in or bridge.device_ready()):
                behavior = bridge.hold(rid)
                if behavior:
                    decision = {"behavior": behavior}
                    if behavior == "deny":
                        decision["message"] = DENY_MESSAGE
                    answer = json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                                "decision": decision}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))  # Empty: no decision, the terminal asks
            self.end_headers()
            self.wfile.write(answer)

        def release(self):
            """Lets go of the deck's port for a while, so flashing or a bench script can open it."""
            length = int(self.headers.get("Content-Length") or 0)
            try:
                seconds = float(json.loads(self.rfile.read(length) or b"{}").get("seconds", 60))
            except (ValueError, AttributeError):
                self.send_error(400)
                return
            seconds = max(0.0, min(seconds, MAX_RELEASE_S))
            if bridge.link is not None:
                bridge.link.release(seconds)
            body = json.dumps({"released_s": seconds if bridge.link is not None else 0}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("DECK_BRIDGE_PORT", "8737")))
    ap.add_argument("--record", help="append every input to this JSON lines file")
    ap.add_argument("--context-window", type=int, help="tokens in a VS Code session's context window")
    ap.add_argument("--no-usage-cache", action="store_true", help="leave Claude Code's state file unread")
    ap.add_argument("--refresh-usage", type=float, default=DEFAULT_REFRESH_S, metavar="SECONDS",
                    help=f"run `claude -p /usage` this often while the deck is attached; 0 never ({DEFAULT_REFRESH_S})")
    ap.add_argument("--hold", type=float, default=DEFAULT_HOLD_S, metavar="SECONDS",
                    help=f"hold each permission request this long for the deck's answer (under 580; {DEFAULT_HOLD_S})")
    ap.add_argument("--serial", metavar="PORT", help="the deck's port (else the one /dev/cu.usbmodem* or /dev/ttyACM*)")
    ap.add_argument("--no-serial", action="store_true", help="no deck: print the records only")
    ap.add_argument("--quiet", action="store_true", help="print no records, only the bridge's and the board's lines")
    ap.add_argument("--answer", nargs=2, metavar=("allow|deny", "SECONDS"),
                    help="stand in for the device: answer each held request so many seconds after it opens")
    ap.add_argument("--plugin", metavar="NAME", help="started by this Claude Code plugin: stop when it is switched off")
    args = ap.parse_args()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), None)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        print(f"bridge: another bridge has 127.0.0.1:{args.port} already; this one goes", file=sys.stderr)
        return 0
    server.daemon_threads = True

    bridge = Bridge(args.record, context_window=args.context_window,
                    usage_cache=None if args.no_usage_cache else UsageCache(), hold_s=min(args.hold, 580),
                    quiet=args.quiet)
    if args.answer:
        if args.answer[0] not in ("allow", "deny") or args.hold <= 0:
            ap.error("--answer allow|deny SECONDS needs --hold")
        bridge.stand_in = (args.answer[0], float(args.answer[1]))
    if not args.no_serial:
        link = SerialLink(bridge.on_record, bridge.on_open, on_log=lambda line: print(f"board: {line}", file=sys.stderr),
                          on_lost=bridge.on_lost, port=args.serial)
        bridge.attach(link)
        link.start()
        if not args.no_usage_cache and UsageRefresh(args.refresh_usage, bridge.device_ready).start():
            print(f"bridge: refreshing the plan's usage every {args.refresh_usage:g} s while the deck is attached",
                  file=sys.stderr)
    server.RequestHandlerClass = handler(bridge)
    watch = PluginWatch(args.plugin) if args.plugin else None

    def ticker():
        beat = 0
        while True:
            time.sleep(1)
            bridge.tick()
            beat += 1
            if beat % int(PING_S) == 0:
                bridge.ping()
            why = watch.gone() if watch and beat % PLUGIN_CHECK_S == 0 else None
            if why:
                print(f"bridge: {why}; stopping", file=sys.stderr)
                server.shutdown()
                return

    threading.Thread(target=ticker, daemon=True).start()
    print(f"bridge: listening on 127.0.0.1:{args.port}" + (f", recording to {args.record}" if args.record else "")
          + (f", for the {args.plugin} plugin" if args.plugin else ""), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
