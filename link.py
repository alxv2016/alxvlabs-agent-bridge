"""What every link to the deck shares (DESIGN.md §10): the lines, and the one interface the bridge
talks to, whichever link carries them.

    link = SerialLink(on_record, on_open, on_lost=on_lost)   # the USB cable (serial_link.py)
    link.start()
    link.send({"type": "agent", ...})        # one record, framed; False with no deck there
    link.release(60)                          # let go of the deck for so long

A line is `@deck <crc32 hex> <json>`, both ways, on every link, so the records and the state machine never learn which
link carried one. A link reports a deck there with `on_open`, each record with `on_record`, and the deck gone with
`on_lost`. Standard library only.
"""

from __future__ import annotations

import json
import zlib

PREFIX = b"@deck "

USB = "usb"    # The cable: the deck's port, its console and the way to flash it
WIFI = "wifi"  # The deck on the network (net_link.py)


def encode(record: dict) -> bytes:
    body = json.dumps(record, separators=(",", ":")).encode()
    return PREFIX + b"%08x " % zlib.crc32(body) + body + b"\n"


def decode(line: bytes) -> dict | None:
    """The record a frame carries, or None for a log line, a torn frame or a wrong CRC."""
    at = line.rfind(PREFIX)
    if at < 0:
        return None
    rest = line[at + len(PREFIX):].strip()
    if len(rest) < 10 or rest[8:9] != b" ":
        return None
    try:
        want = int(rest[:8], 16)
    except ValueError:
        return None
    body = rest[9:]
    if zlib.crc32(body) != want:
        return None
    try:
        record = json.loads(body)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


class Link:
    """One way to the deck. `kind` names it (USB or WIFI). `grace_s` is how long the bridge keeps what it holds for
    the deck after `on_lost`: 0 on the cable, where a pulled cable means the developer has gone to the terminal; about
    15 s on Wi-Fi, where a deck back within it gets them again (DESIGN.md §10.6). `path` says where the deck is, for
    `GET /health`. Only the cable's word may make the bridge read the Keychain (DESIGN.md §9)."""

    kind = ""
    grace_s = 0.0
    path: str | None = None

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def send(self, record: dict) -> bool:
        """Queues one record for the deck and returns at once: False with no deck there."""
        raise NotImplementedError

    def release(self, seconds: float) -> None:
        """Lets go of the deck for `seconds`, or until release(0)."""
        raise NotImplementedError
