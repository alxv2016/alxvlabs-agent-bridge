"""The bridge's pairings with decks over Wi-Fi (the knob's AGENT_DECK §19.6, DESIGN.md §9): a login-Keychain item
per deck, holding the shared key the bridge made for it and where the deck is — its host, port and the SHA-256 of the
certificate the bridge pins.

    key = new_key()                                  # 32 random bytes, hex: sent to the deck with the network
    store("3f2a", {"key": key, "fingerprint": ..., "host": "agentdeck-3f2a.local", "port": 8443})
    load("3f2a")                                     # the record, or None
    paired()                                         # the ids of the decks paired, from the file beside the items

The item is the bridge's own: `security` writes it and reads it back with no admin prompt. The key never appears in a
process's arguments — the item is written through `security -i` on stdin, its data as hex — and is never logged,
printed or put in an exception here. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys

SECURITY = "/usr/bin/security"
SERVICE = "alxvlabs-agent-deck"     # The items' service; each item's account is the deck's id
TIMEOUT_S = 10
ITEM_NOT_FOUND = 44                 # errSecItemNotFound's low byte, as `security` exits
DECK_ID = re.compile(r"[a-z0-9-]{1,16}")  # A hello's id (the knob's deck_link.h kMaxHelloField; DESIGN.md §10.3)
HEX64 = re.compile(r"[0-9a-f]{64}")
# Which decks are paired, so the bridge knows whom to look for with no deck on the cable: their ids only, nothing secret;
# the Keychain holds the rest, by id.
IDS_PATH = os.path.expanduser("~/Library/Application Support/alxvlabs-agent-deck/paired.json" if sys.platform == "darwin"
                              else "~/.local/state/alxvlabs-agent-deck/paired.json")


def new_key() -> str:
    return secrets.token_hex(32)


def valid(record: dict) -> bool:
    """A pairing record as kept: the key and the fingerprint 64 hex digits, a host and a port."""
    return (isinstance(record.get("key"), str) and HEX64.fullmatch(record["key"]) is not None
            and isinstance(record.get("fingerprint"), str) and HEX64.fullmatch(record["fingerprint"]) is not None
            and isinstance(record.get("host"), str) and 0 < len(record["host"]) <= 64
            and isinstance(record.get("port"), int) and not isinstance(record["port"], bool)
            and 0 < record["port"] < 65536)


def store(deck_id: str, record: dict, run=subprocess.run) -> bool:
    """The deck's pairing kept, replacing one before; False when refused or the record is not one."""
    if DECK_ID.fullmatch(deck_id) is None or not valid(record):
        return False
    data = json.dumps(record, separators=(",", ":"), sort_keys=True).encode().hex()
    command = f"add-generic-password -U -s {SERVICE} -a {deck_id} -l agent-deck-{deck_id} -X {data}\n"
    try:
        result = run([SECURITY, "-i"], input=command, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    # `security -i` exits 0 whatever its commands did; a refusal is reported on stderr.
    if result.returncode != 0 or (result.stderr or "").strip():
        return False
    remember(deck_id)
    return True


def paired() -> list[str]:
    """The ids of the decks paired, most recent last; empty when none or the file is not one."""
    try:
        with open(IDS_PATH, encoding="utf-8") as f:
            ids = json.load(f)
    except (OSError, ValueError):
        return []
    return [i for i in ids if isinstance(i, str) and DECK_ID.fullmatch(i)] if isinstance(ids, list) else []


def remember(deck_id: str) -> None:
    ids = [i for i in paired() if i != deck_id] + [deck_id]
    try:
        os.makedirs(os.path.dirname(IDS_PATH), exist_ok=True)
        with open(IDS_PATH, "w", encoding="utf-8") as f:
            json.dump(ids, f)
    except OSError:
        pass  # The pairing is kept all the same: the cable brings the id again


def load(deck_id: str, run=subprocess.run) -> dict | None:
    """The deck's pairing, or None: never paired, refused, or not a record."""
    if DECK_ID.fullmatch(deck_id) is None:
        return None
    try:
        result = run([SECURITY, "find-generic-password", "-s", SERVICE, "-a", deck_id, "-w"],
                     capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        record = json.loads(result.stdout.strip())
    except ValueError:
        return None
    return record if isinstance(record, dict) and valid(record) else None
