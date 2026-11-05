#!/usr/bin/env python3
"""The agent deck's bridge on the Mac: Claude Code's hooks and status line in, the deck's records out.

    bridge.py [--port 8737] [--record FILE] [--context-window 1000000] [--no-usage-cache] [--refresh-usage 3600]
              [--serial /dev/cu.usbmodem101 | --no-serial] [--hold 570]
              [--answer allow|deny SECONDS] [--plugin NAME]

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
prompt shows meanwhile, and whichever answers first settles it (DESIGN.md §4). A question (AskUserQuestion) is held
the same way, at its PermissionRequest, the terminal asking it meanwhile. With no
deck, or --no-serial, each goes back at once with no decision. POST /release {"seconds": N} lets go of the port
for flashing, the boot check or a screenshot; the bridge opens it again afterwards and sends the deck everything.

The deck's Connect, on its Wi-Fi page, sends its scan on the cable (`wifi_join`): the bridge crosses it with the Mac's
saved networks and sends the one left, with its password from the Keychain behind macOS's admin prompt (`wifi`), or
the several to pick from (`wifi_choose`), or why not (`wifi_error`) — on its own thread, so nothing waits on the prompt
(wifi.py, DESIGN.md §9). Only the cable is answered, and the password goes on it alone: never on stdout, in a log or
in --record's file.

It takes 127.0.0.1's port before the deck's, and goes quietly when another bridge has it: one bridge at a time.
--plugin NAME is how the Claude Code plugin's hook.py starts it: every PLUGIN_CHECK_S it reads Claude Code's own
files (plugin_state.py) and stops once the plugin, seen installed, is disabled or uninstalled, or its folder is gone
— Claude Code stops nothing it started (DESIGN.md §5). Standard library only. Ctrl-C stops it.
"""

from __future__ import annotations

import argparse
import errno
import functools
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hook
import pairing
import plugin_state
import wifi
from link import USB, WIFI, Link
from net_link import NetLink
from serial_link import SerialLink
from state import Deck
from transcripts import Transcripts
from usage_cache import UsageCache
from usage_refresh import UsageRefresh

KINDS = {"/hook": "hook", "/status": "status", "/subagent-status": "subagent-status"}
MAX_INPUT = 1 << 20
DENY_MESSAGE = "Denied on the agent deck."  # What Claude is told; the turn carries on
QUESTIONS_VERSION = 2  # The deck's hello from which it takes "question" records
IDLE_VERSION = 3       # And the state "idle" and `active`
NAMED_VERSION = 4      # And names the deck: `id`, `board` and `link` (the firmware's AGENT_DECK §19.3)
HELLO_FIELD = re.compile(r"[a-z0-9-]{1,16}")  # A hello's names, as the deck sends them (deck_link.h kMaxHelloField)
PING_S = 2.0          # onx/deck_link.h kPingMs: the deck takes the bridge for gone after three missed
DEFAULT_HOLD_S = 570  # Under hook.py's 590 s wait on a request
DEFAULT_REFRESH_S = 3600  # How often `/usage` freshens the plan's usage while the deck is attached — the developer's
                         # choice, 2026-10-29: the shares move slowly, and the resets count down from their moments
MAX_RELEASE_S = 600
MAX_SCAN = 10  # The networks a wifi_join carries at most; past them the scan is not read (DESIGN.md §9)
GONE_POLL_S = 0.25  # How often a held hook is looked at, to see whether Claude Code still waits on it
ORPHAN_MARK = ".orphaned_at"  # Claude Code's mark on a version's folder an update has left behind
UPDATED = "a newer version of the plugin is installed"
FOLDER_GONE = "the plugin's folder is gone (updated or uninstalled)"
PLUGIN_CHECK_S = 10  # How often a bridge the plugin started looks whether the plugin is still installed and on
HERE = os.path.dirname(os.path.abspath(__file__))


def peer_gone(sock: socket.socket) -> bool:
    """Whether hook.py has gone from this connection: having sent its input it sends nothing more, so anything to read
    is the end of the stream. A hook Claude Code stopped takes the connection with it."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class PluginWatch:
    """Whether the plugin that started this bridge still wants it. An uninstall only counts once the plugin has been
    seen listed, so a bridge run from a plugin folder Claude Code was pointed at (`claude --plugin-dir`) stays."""

    def __init__(self, name: str, folder: str = HERE, claude_dir: str = plugin_state.CLAUDE_DIR):
        self.name, self.folder, self.claude_dir = name, folder, claude_dir
        self.seen = False
        self.current = False  # This folder has been listed as an install

    def replaced(self) -> bool:
        """Whether an update has put another version in this one's place. Claude Code keeps the old folder, marked
        `.orphaned_at`, so its being there says nothing (DESIGN.md §5). A folder never listed and never marked is
        one Claude Code was pointed at, `claude --plugin-dir`, and is left running."""
        paths = plugin_state.install_paths(self.name, self.claude_dir)
        if not paths:
            return False
        if os.path.realpath(self.folder) in paths:
            self.current = True
            return False
        return self.current or os.path.exists(os.path.join(self.folder, ORPHAN_MARK))

    def gone(self) -> str | None:
        """Why the bridge should stop, or None."""
        if not os.path.isdir(self.folder):
            return FOLDER_GONE
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
        if self.replaced():
            return UPDATED
        return None


def successor(why: str, name: str, own: str = HERE, claude_dir: str = plugin_state.CLAUDE_DIR) -> str | None:
    """The folder whose bridge takes over from one stopping for `why`, or None: an update hands the deck to the new
    version at once, not at the next hook, which may be long in coming (DESIGN.md §5). A plugin disabled or
    uninstalled hands over to nothing."""
    if why not in (UPDATED, FOLDER_GONE):
        return None
    if not plugin_state.installed(name, claude_dir) or not plugin_state.enabled(name, claude_dir):
        return None
    folder = plugin_state.current_folder(name, own, claude_dir)
    if os.path.realpath(folder) == os.path.realpath(own) or not os.path.isfile(os.path.join(folder, "bridge.py")):
        return None
    return folder


class Bridge:
    def __init__(self, record_path: str | None, out=sys.stdout, context_window: int | None = None,
                 usage_cache: UsageCache | None = None, hold_s: float = 0, quiet: bool = False):
        self.deck = Deck(context_window)
        self.hold_s = hold_s
        self.stand_in: tuple[str, float] | None = None  # --answer: "allow" or "deny" after so many seconds
        self.waiting: dict[int, list] = {}  # request id -> [threading.Event, "allow" | "deny" | answers | None]
        self.transcripts = Transcripts()
        self.watched: dict[int, dict] = {}  # request id -> the hook that opened it, for its transcript
        self.turns: dict[tuple, dict] = {}  # (session, agent) -> what its last hook says of its transcript
        self.usage_cache = usage_cache
        # Reentrant: a send that fails under it closes the link, whose on_lost takes it on the same thread.
        self.lock = threading.RLock()
        self.out = out
        self.quiet = quiet  # no record on stdout: a login agent's log stays small
        self.record = open(record_path, "a", encoding="utf-8") if record_path else None
        self.link: Link | None = None   # The link the deck talks on now: the last to open, the cable first
        self.links: list[Link] = []
        self.named: dict | None = None  # The attached deck's `id`, `board` and `link`, from a version 4 hello
        self.said = ""  # What the log last said of the deck's names
        self.lost_count = 0  # Each loss and each hello counts one: a grace let go only if nothing came since
        self.mac = wifi.Mac()  # What the deck's Connect asks macOS; a fake in the tests
        self.wifi_lock = threading.Lock()
        self.wifi_thread: threading.Thread | None = None  # The lookup running, or None
        self.wifi_offered: list[str] = []  # The last join's networks the Mac knows: the only ones a pick may name
        self.wifi_secure: dict[str, bool] = {}  # Each scanned network's `secure`, from the last join
        self.pair_key: str | None = None  # The shared key sent with the last network, until the deck says where it is
        self.pairing_run = subprocess.run  # What writes the Keychain's pairing item; a fake in the tests

    # The deck on the cable --------------------------------------------------------------------------------------

    def attach(self, link: Link) -> None:
        """A link the deck may talk on, the first the bridge's link until another opens. Its records, its opening and
        its loss come back with the link: only the cable's word reads the Keychain, and only the link the deck was on
        says how long to wait for it."""
        self.links.append(link)
        if self.link is None:
            self.link = link
        if hasattr(link, "standby"):
            link.standby = self.cable_open  # A link that stands down for the cable waits while it is open
        link.on_record = functools.partial(self.on_record, source=link)
        link.on_open = functools.partial(self.on_open, source=link)
        link.on_lost = functools.partial(self.on_lost, source=link)

    def cable_open(self) -> bool:
        """Whether a cable link has the deck."""
        return any(getattr(link, "kind", "") == USB and link.connected for link in self.links)

    def device_ready(self) -> bool:
        return self.link is not None and self.link.connected

    def on_open(self, source: Link | None = None) -> None:
        """A link opened to the deck: it is the bridge's link now, and asks the deck to say hello. The deck may still
        be booting and miss this — then its own hello at start does the asking — so the snapshot waits for the deck's
        hello, sent once it listens. The cable wins: a deck on it stands the Wi-Fi link down (AGENT_DECK §19.3)."""
        source = source if source is not None else self.link
        if getattr(source, "kind", "") != USB and self.cable_open():
            if hasattr(source, "pause"):
                source.pause(True)  # Opened as the cable did: the cable wins, and this one stands down
            return
        with self.lock:
            self.link = source
        if getattr(source, "kind", "") == USB:
            for other in self.links:
                if other is not source and hasattr(other, "pause"):
                    other.pause(True)
        source.send({"type": "hello"})

    def send_snapshot(self) -> None:
        with self.lock:
            for r in self.deck.snapshot(time.time()):
                self.link.send(r)

    def on_record(self, r: dict, source: Link | None = None) -> None:
        if r.get("type") == "hello":
            if source is not None and not source.connected:
                return  # A line read before its link closed: the deck is gone, whatever it said then
            if source is not None and source is not self.link and (getattr(source, "kind", "") == USB
                                                                  or not getattr(self.link, "connected", False)):
                with self.lock:
                    self.link = source  # The deck speaks on this link: the cable always, another while none is open
            with self.lock:
                self.lost_count += 1  # Back within a grace: what is held stays held, and goes out in the snapshot
                version = r["version"] if isinstance(r.get("version"), int) else 0
                self.deck.asks_questions = version >= QUESTIONS_VERSION
                self.deck.shows_idle = version >= IDLE_VERSION
                self.name_deck(r, version)
            self.send_snapshot()  # The deck started, came back, or was asked: everything, after a reset
        elif r.get("type") == "decision" and isinstance(r.get("id"), int):
            self.decide(r["id"], bool(r.get("allow")))
            if self.link:
                self.link.send({"type": "ack", "id": r["id"]})
        elif r.get("type") == "answer" and isinstance(r.get("id"), int):
            part, chosen = r.get("part"), r.get("chosen")
            if isinstance(part, int) and isinstance(chosen, int):
                self.answer(r["id"], part, chosen)
                if self.link:
                    self.link.send({"type": "ack", "id": r["id"], "part": part})
        # Only the cable's word: nothing on the network may make the bridge read the Keychain (DESIGN.md §9).
        elif r.get("type") in ("wifi_join", "wifi_pick") and getattr(source, "kind", "") == USB:
            (self.wifi_join if r["type"] == "wifi_join" else self.wifi_pick)(r)
        # Where a paired deck is on the network, from the cable alone: it says which certificate to pin.
        elif r.get("type") == "wifi_status" and getattr(source, "kind", "") == USB:
            self.wifi_status(r)

    def name_deck(self, r: dict, version: int) -> None:
        """The deck's names from its hello, version 4 on; an older deck has none. Logged as they change. A deck the
        Keychain holds a pairing for is remembered as paired, for the link over Wi-Fi to look for it."""
        fields = ("id", "board", "link")
        named = None
        if version >= NAMED_VERSION and all(isinstance(r.get(f), str) and HELLO_FIELD.fullmatch(r[f]) for f in fields):
            named = {f: r[f] for f in fields}
        self.named = named
        if named:
            said = f"{named['board']} {named['id']} on {named['link']}"
        else:
            said = "its names unreadable" if version >= NAMED_VERSION else "with no name, an older deck"
        if said != self.said:
            self.said = said  # Once a change: a reconnect, a reset or a /release says the same again
            print(f"bridge: the deck says hello {version}, {said}", file=sys.stderr)
            if named and named["id"] not in pairing.paired() and pairing.load(named["id"], run=self.pairing_run):
                pairing.remember(named["id"])

    def on_lost(self, source: Link | None = None) -> None:
        """The deck gone — unplugged, reset, or the port lent out. On the cable nothing will answer what is held, so
        it goes back at once; on a link with a grace it is kept that long for the deck to come back (AGENT_DECK
        §19.3). A link the deck was not on going changes nothing."""
        if source is not None and self.link is not None and source is not self.link:
            return
        fallback = next((other for other in self.links
                         if other is not source and other is not self.link and other.connected), None)
        if fallback is not None:
            # Another link still has the deck — the cable, as the Wi-Fi session stands down for it: the bridge's link
            # now, nothing let go, and the deck asked to say hello on it.
            with self.lock:
                self.link = fallback
            fallback.send({"type": "hello"})
            return
        if getattr(source, "kind", "") == USB:
            for other in self.links:
                if other is not source and hasattr(other, "pause"):
                    other.pause(False)  # Off the cable: the Wi-Fi link looks for the deck
        with self.wifi_lock:
            self.wifi_offered, self.wifi_secure = [], {}  # A deck back asks again with a scan of its own
            self.pair_key = None  # And is paired again with the next network it is sent
        grace = getattr(source, "grace_s", 0) or 0
        if getattr(source, "kind", "") == USB:
            # The cable pulled to leave the desk: a deck paired for Wi-Fi gets that link's grace to come back on it,
            # what is held kept for it (the firmware's AGENT_DECK §19.3).
            for other in self.links:
                if other is not source and hasattr(other, "reachable") and other.reachable():
                    grace = max(grace, getattr(other, "grace_s", 0) or 0)
        with self.lock:
            self.lost_count += 1
            count, held = self.lost_count, list(self.waiting)
            if grace > 0:
                timer = threading.Timer(grace, self.let_go, (count, held))
                timer.daemon = True
                timer.start()
                return
            self.let_go(count, held)

    def let_go(self, count: int, held: list[int]) -> None:
        """What was held at loss `count` goes back to the terminal, unless the deck said hello again since. A hold
        begun after the loss keeps its own end."""
        with self.lock:
            if count != self.lost_count:
                return
            self.deck.asks_questions = False
            self.named = None
            for rid in held:
                waiter = self.waiting.get(rid)
                if waiter is not None:
                    waiter[0].set()

    # Joining a network from the deck (DESIGN.md §9) ------------------------------------------------------------

    def wifi_join(self, r: dict) -> None:
        """The deck's scan: the networks the Mac knows too are looked up on a thread of their own. Ignored while a
        lookup runs — the admin prompt may be up."""
        names, rssi, secure = r.get("names"), r.get("rssi"), r.get("secure")
        if not isinstance(names, list):
            return
        rssi = rssi if isinstance(rssi, list) else []
        secure = secure if isinstance(secure, list) else []
        scan: list[tuple[str, int, bool]] = []
        for i, name in enumerate(names[:MAX_SCAN]):
            if not isinstance(name, str) or not name or any(name == s[0] for s in scan):
                continue
            level = rssi[i] if i < len(rssi) and isinstance(rssi[i], int) and not isinstance(rssi[i], bool) else 0
            locked = secure[i] if i < len(secure) and isinstance(secure[i], bool) else True  # Unknown: ask the Keychain
            scan.append((name, level, locked))
        self.wifi_start(self.wifi_joined, scan)

    def wifi_pick(self, r: dict) -> None:
        """The network picked on the deck from a wifi_choose: only one that was offered, else not_in_reach."""
        name = r.get("name")
        with self.wifi_lock:
            offered = isinstance(name, str) and name in self.wifi_offered
        if not offered:
            self.wifi_send({"type": "wifi_error", "reason": wifi.NOT_IN_REACH})
            return
        self.wifi_start(self.wifi_send_network, name)

    def wifi_start(self, work, arg) -> bool:
        """`work(arg)` on a daemon thread, unless a lookup already runs; whether it started."""
        with self.wifi_lock:
            if self.wifi_thread is not None and self.wifi_thread.is_alive():
                return False
            self.wifi_thread = threading.Thread(target=self.wifi_run, args=(work, arg), daemon=True)
            self.wifi_thread.start()
        return True

    def wifi_run(self, work, arg) -> None:
        try:
            work(arg)
        except Exception as e:  # Its kind alone: a traceback or a message here could carry a name or the password
            print(f"bridge: the Wi-Fi lookup failed: {type(e).__name__}", file=sys.stderr)
            self.wifi_send({"type": "wifi_error", "reason": wifi.NO_PASSWORD})

    def wifi_joined(self, scan: list[tuple[str, int, bool]]) -> None:
        """On the lookup's thread: the scan crossed with the Mac's saved networks — none, one, or several to pick."""
        both = wifi.candidates([s[0] for s in scan], self.mac.saved())
        with self.wifi_lock:
            self.wifi_offered = both
            self.wifi_secure = {s[0]: s[2] for s in scan}
        if not both:
            self.wifi_send({"type": "wifi_error", "reason": wifi.NOT_IN_REACH})
        elif len(both) == 1:
            self.wifi_send_network(both[0])
        else:
            level = {s[0]: s[1] for s in scan}
            self.wifi_send({"type": "wifi_choose", "names": both, "rssi": [level[n] for n in both]})

    def wifi_send_network(self, name: str) -> None:
        """On the lookup's thread: the network and its password, or why not. An open network needs no Keychain."""
        with self.wifi_lock:
            locked = self.wifi_secure.get(name, True)
        password, why = self.mac.password(name) if locked else ("", None)
        if password is None:
            self.wifi_send({"type": "wifi_error", "reason": why or wifi.NO_PASSWORD})
            return
        # A new shared key with every network sent, kept here until the deck says where it is (DESIGN.md §9).
        key = pairing.new_key()
        with self.wifi_lock:
            self.pair_key = key
        # Straight to the cable: not through emit(), whose records reach stdout and the bridge's log.
        self.wifi_send({"type": "wifi", "name": name, "password": password, "key": key})

    def wifi_status(self, r: dict) -> None:
        """The deck joined and serving TLS: its pairing kept in the Keychain — the key sent with its network, its host,
        port and certificate fingerprint. Only after a key was sent, and only for the deck that said hello."""
        deck_id, host, port, fingerprint = r.get("id"), r.get("host"), r.get("port"), r.get("fingerprint")
        with self.wifi_lock:
            key = self.pair_key
        named = self.named or {}
        if key is None or not isinstance(deck_id, str) or deck_id != named.get("id"):
            return  # A deck that rejoined by itself, or not the one on the cable: nothing to pair
        record = {"key": key, "fingerprint": fingerprint, "host": host, "port": port}
        if not pairing.valid(record):
            print(f"bridge: the deck {deck_id}'s wifi_status was not a pairing", file=sys.stderr)
            return
        if pairing.store(deck_id, record, run=self.pairing_run):
            with self.wifi_lock:
                if self.pair_key == key:
                    self.pair_key = None
            self.wifi_send({"type": "wifi_paired"})  # The deck's page leaves its bridge step
            print(f"bridge: paired with the deck {deck_id} at {host}:{port}, certificate {fingerprint[:16]}…",
                  file=sys.stderr)
        else:
            print(f"bridge: the pairing with the deck {deck_id} was not kept in the Keychain", file=sys.stderr)

    def wifi_send(self, record: dict) -> None:
        """A Wi-Fi record to the deck alone, while it is attached: never printed, never recorded."""
        link = self.link
        if link is not None and link.connected:
            link.send(record)

    def ping(self) -> None:
        if self.link is not None:
            self.link.send({"type": "ping"})

    def keep(self, now: float, kind: str, data: dict) -> None:
        """An input into the recording, so it replays with no transcript on disk."""
        if self.record:
            self.record.write(json.dumps({"t": round(now, 3), "kind": kind, "input": data}) + "\n")
            self.record.flush()

    def take(self, kind: str, data: dict) -> list[dict]:
        """Feeds an input and its transcripts' through the state machine; the records it made."""
        made: list[dict] = []
        with self.lock:
            now = time.time()
            inputs = [(kind, data)]
            if kind == "hook" and isinstance(data, dict):
                inputs += [("transcript", t) for t in self.transcripts.observe(data)]
                self.turns[(data.get("session_id"), data.get("agent_id"))] = {
                    k: data[k] for k in ("session_id", "agent_id", "agent_type", "transcript_path",
                                         "agent_transcript_path") if k in data}
            for k, d in inputs:
                self.keep(now, k, d)
                records = self.deck.feed(k, d, now)
                made += records
                for r in records:
                    if r["type"] == "question":
                        # Before it goes out: the deck's answer can beat this hook's call to hold(), and a question
                        # it has no room for comes straight back.
                        self.waiting.setdefault(r["id"], [threading.Event(), None])
                    if r["type"] in ("request", "question"):
                        self.watched[r["id"]] = d
                self.emit(records)
        return made

    def watch_transcripts(self) -> None:
        """No hook reports an answer in the terminal, but a refused call's result is in the transcript at once: read
        on, while a request is open, for the ones that ended (DESIGN.md §4). Nor does one report a turn interrupted, so
        read on while an agent works too (§3)."""
        with self.lock:
            self.watched = {r: h for r, h in self.watched.items() if r in self.deck.requests}
            self.turns = {k: h for k, h in self.turns.items() if self.deck.working(k[0] or "", k[1] or "") is not None}
            hooks = {k: h for k, h in self.turns.items() if self.deck.working(k[0] or "", k[1] or "")}
            hooks.update({(h.get("session_id"), h.get("agent_id")): h for h in self.watched.values()})
            now = time.time()
            for h in hooks.values():
                for t in self.transcripts.observe(h):
                    self.keep(now, "transcript", t)
                    self.emit(self.deck.feed("transcript", t, now))

    def hold(self, rid: int, seconds: float | None = None, gone=None) -> str | dict | None:
        """Waits up to the hold for the device's answer to request `rid`: "allow", "deny", a question's answers, or
        None. `gone` says whether Claude Code has stopped waiting on the hook: then something else answered it."""
        with self.lock:
            waiter = self.waiting.setdefault(rid, [threading.Event(), None])
            if rid not in self.deck.requests and not waiter[0].is_set():
                self.waiting.pop(rid, None)
                return None  # Closed while its hook was on its way
            asking = rid in self.deck.asks
        if self.stand_in and not asking:  # It stands in for allow and deny; a question's answer is the developer's
            behavior, after_s = self.stand_in
            threading.Timer(after_s, self.decide, (rid, behavior == "allow")).start()
        deadline = time.monotonic() + (self.hold_s if seconds is None else seconds)
        while not waiter[0].wait(min(GONE_POLL_S, max(0.0, deadline - time.monotonic()))):
            if time.monotonic() >= deadline:
                break
            if gone is not None and gone():
                with self.lock:
                    self.emit(self.deck.hook_gone(rid, time.time()))
                break
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

    def holding(self) -> bool:
        """Whether a hook is waiting on the deck's answer."""
        with self.lock:
            return bool(self.waiting)

    def drop(self, rid: int) -> None:
        """A question with no deck left to hold it for: closed, and left out of every snapshot."""
        with self.lock:
            self.waiting.pop(rid, None)
            self.emit(self.deck.expire(rid, time.time()))

    def answer(self, rid: int, part: int, chosen: int) -> None:
        """The device's answer to a part of question `rid`: the next part goes out, or the hook goes back."""
        with self.lock:
            waiter = self.waiting.get(rid)
            if waiter is None or waiter[0].is_set():
                return  # Held past the hold, or answered in the terminal: Claude Code no longer waits
            records, settled, answers = self.deck.answered_question(rid, part, chosen, time.time())
            self.emit(records)
            if settled:
                waiter[1] = answers  # None: left to the terminal
                waiter[0].set()

    def tick(self) -> None:
        cached = self.usage_cache.poll() if self.usage_cache else None
        if cached:
            self.take("usage-cache", cached)
        self.watch_transcripts()
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
            """GET /health: whether the deck is attached, on which port, and the names its hello gave — what
            `install.py --status` reads."""
            if self.path != "/health":
                self.send_error(404)
                return
            link = bridge.link
            ready = bridge.device_ready()
            named = (bridge.named or {}) if ready else {}
            body = json.dumps({"deck": ready, "port": link.path if ready else None, "agents": len(bridge.deck.agents),
                               "id": named.get("id"), "board": named.get("board"),
                               "link": link.kind if ready else None}).encode()
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
            asked = next((r["id"] for r in records if r["type"] == "question"), None)
            if asked is not None and not bridge.device_ready():
                bridge.drop(asked)
            elif asked is not None:
                answers = bridge.hold(asked, gone=lambda: peer_gone(self.connection))
                if answers:
                    # The whole input again, its answers added: AskUserQuestion then runs without asking, and the
                    # terminal's prompt goes (DESIGN.md §4).
                    answer = json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
                        "behavior": "allow",
                        "updatedInput": {**data.get("tool_input", {}), "answers": answers}}}}).encode()
            elif rid is not None and bridge.hold_s > 0 and (bridge.stand_in or bridge.device_ready()):
                behavior = bridge.hold(rid, gone=lambda: peer_gone(self.connection))
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
            cables = [link for link in bridge.links if getattr(link, "kind", "") == USB]
            for link in cables:
                link.release(seconds)
            body = json.dumps({"released_s": seconds if cables else 0}).encode()
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
                    help=f"hold each permission request and question this long for the deck's answer (under 580; {DEFAULT_HOLD_S})")
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
        link = SerialLink(None, bridge.on_open, on_log=lambda line: print(f"board: {line}", file=sys.stderr),
                          port=args.serial)
        bridge.attach(link)  # Its records, opening and loss, told with the link
        link.start()
        # The link over Wi-Fi, to a deck paired on the cable: standing down while a deck is on the cable.
        net = NetLink(pairing.paired, pairing.load, on_log=lambda line: print(f"bridge: {line}", file=sys.stderr))
        bridge.attach(net)
        net.start()
        if not args.no_usage_cache and UsageRefresh(args.refresh_usage, bridge.device_ready).start():
            print(f"bridge: refreshing the plan's usage every {args.refresh_usage:g} s while the deck is attached",
                  file=sys.stderr)
    server.RequestHandlerClass = handler(bridge)
    watch = PluginWatch(args.plugin) if args.plugin else None
    stopped: dict[str, str] = {}

    def ticker():
        beat = 0
        while True:
            time.sleep(1)
            try:
                bridge.tick()
            except Exception as e:  # This thread pings the deck: anything the tick reads must not stop it
                print(f"bridge: tick failed: {e!r}", file=sys.stderr)
            beat += 1
            if beat % int(PING_S) == 0:
                bridge.ping()
            why = watch.gone() if watch and beat % PLUGIN_CHECK_S == 0 else None
            if why == UPDATED and bridge.holding():
                why = None  # Not out from under an answer: the next look finds the request settled
            if why:
                print(f"bridge: {why}; stopping", file=sys.stderr)
                stopped["why"] = why
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
    folder = successor(stopped.get("why", ""), args.plugin) if args.plugin else None
    if folder is not None:
        # The port and the cable let go first, so the new bridge takes both at once.
        for link in bridge.links:
            if isinstance(link, SerialLink):
                link.stopping = True
                link.close()
        try:
            hook.start_bridge(folder)
            print(f"bridge: started the new version from {folder}; it takes the deck from here", file=sys.stderr)
        except OSError as e:
            print(f"bridge: could not start the new version ({e}); the next hook will", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
