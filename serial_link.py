"""The bridge's side of the USB cable to the agent deck: records out, the deck's answers in (the firmware's AGENT_DECK §16).

    link = SerialLink(on_line, on_open)      # a thread keeps the port open while a deck is attached
    link.start()
    link.send({"type": "agent", ...})        # one record, framed
    link.release(60)                          # let go of the port for flashing, the boot check or a screenshot

A line is `@deck <crc32 hex> <json>`, both ways (onx/deck_link.h). The board's console log shares the cable: a
line that is not a frame is the board's log, handed to `on_log`, and a frame may sit at the end of a log line a
write split, so the prefix is looked for anywhere in a line.

The port is the ESP32-S3's USB Serial/JTAG (`/dev/cu.usbmodem*` on macOS, `/dev/ttyACM*` on Linux). DTR low with RTS high resets the chip, so the
port is opened with both lines kept low (FIRMWARE_CONFIG §8). Standard library only: termios and fcntl.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import select
import struct
import termios
import threading
import time
import zlib

PREFIX = b"@deck "
PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/ttyACM*")  # macOS, Linux
REOPEN_S = 1.0


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


def open_without_reset(path: str) -> int:
    """The port opened raw, DTR and RTS low before anything else (FIRMWARE_CONFIG §8)."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    lines = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)

    def lower_lines():
        try:
            fcntl.ioctl(fd, termios.TIOCMBIC, lines)
        except OSError:
            pass  # No modem lines to lower: a pseudo-terminal, as the tests use

    try:
        lower_lines()
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0                                                  # iflag: no translation
        attrs[1] = 0                                                  # oflag: raw out
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL       # cflag: 8N1, no modem hang-up
        attrs[3] = 0                                                  # lflag: no echo, no canonical lines
        attrs[4] = attrs[5] = termios.B115200                         # speed: ignored on USB, set all the same
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        lower_lines()
    except OSError:
        os.close(fd)
        raise
    return fd


class SerialLink:
    def __init__(self, on_record, on_open, on_log=None, on_lost=None, port: str | None = None):
        self.on_record, self.on_open, self.on_log, self.on_lost = on_record, on_open, on_log, on_lost
        self.port = port
        self.fd: int | None = None
        self.path: str | None = None  # the port open now
        self.lock = threading.Lock()          # one writer at a time
        self.released_until = 0.0
        self.stopping = False

    # Port -------------------------------------------------------------------------------------------------------

    def find(self) -> str | None:
        if self.port:
            return self.port if os.path.exists(self.port) else None
        ports = sorted(p for pattern in PORT_GLOBS for p in glob.glob(pattern))
        return ports[0] if len(ports) == 1 else None

    def close(self) -> None:
        with self.lock:
            was_open = self.fd is not None
            if was_open:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
        if was_open and self.on_lost:
            self.on_lost()

    @property
    def connected(self) -> bool:
        return self.fd is not None

    def release(self, seconds: float) -> None:
        """Lets go of the port for `seconds`, or until release(0)."""
        self.released_until = time.time() + max(0.0, seconds)
        self.close()

    # Lines ------------------------------------------------------------------------------------------------------

    def send(self, record: dict) -> bool:
        data = encode(record)
        failed = False
        with self.lock:
            if self.fd is None:
                return False
            try:
                while data:
                    _, ready, _ = select.select([], [self.fd], [], 1.0)
                    if not ready:
                        return False  # The deck not reading yet — booting: this line is dropped, the port kept
                    data = data[os.write(self.fd, data):]
            except BlockingIOError:
                return False
            except OSError:
                failed = True  # The port gone: unplugged or reset
        if failed:
            self.close()
        return not failed

    def run(self) -> None:
        buf = b""
        while not self.stopping:
            if self.fd is None:
                path = None if time.time() < self.released_until else self.find()
                if path is None:
                    time.sleep(REOPEN_S)
                    continue
                try:
                    fd = open_without_reset(path)
                except OSError:
                    time.sleep(REOPEN_S)
                    continue
                with self.lock:
                    self.fd, self.path = fd, path
                buf = b""
                self.on_open()
            fd = self.fd
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
                chunk = os.read(fd, 4096) if ready else b""
                if ready and not chunk:
                    raise OSError("the port closed")
            except (OSError, ValueError, TypeError):
                self.close()
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                record = decode(raw)
                if record is not None:
                    self.on_record(record)
                elif self.on_log and raw.strip():
                    self.on_log(raw.decode(errors="replace").rstrip("\r"))

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()
