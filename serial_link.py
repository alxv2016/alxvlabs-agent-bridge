"""The bridge's side of the USB cable to the agent deck: records out, the deck's answers in (the firmware's AGENT_DECK §16).

    link = SerialLink(on_line, on_open)      # a thread keeps the port open while a deck is attached
    link.start()
    link.send({"type": "agent", ...})        # one record, framed
    link.release(60)                          # let go of the port for flashing, the boot check or a screenshot

A line is `@deck <crc32 hex> <json>`, both ways (onx/deck_link.h). The board's console log shares the cable: a
line that is not a frame is the board's log, handed to `on_log`, and a frame may sit at the end of a log line a
write split, so the prefix is looked for anywhere in a line. A damaged frame of a Wi-Fi record goes to `on_log`
without its body: a scan names the neighbours' networks (DESIGN.md §9). Nothing sent is logged.

The port is the ESP32-S3's USB Serial/JTAG (`/dev/cu.usbmodem*` on macOS, `/dev/ttyACM*` on Linux). DTR low with RTS high resets the chip, so the
port is opened with both lines kept low (FIRMWARE_CONFIG §8). Standard library only: termios and fcntl.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import queue
import select
import struct
import termios
import threading
import time
import zlib

PREFIX = b"@deck "
WIFI_MARK = b'"wifi'  # Every Wi-Fi record's type starts so, wherever a torn frame's JSON keeps it
PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/ttyACM*")  # macOS, Linux
REOPEN_S = 1.0
# send() only queues a line; the writer thread waits on the port. The bridge sends under its own lock, so a deck
# that stops reading — booting, hung, or a firmware that never reads the line — must not hold up Claude Code's
# hooks behind it: each waited WRITE_WAIT_S a line with the lock held, past hook.py's 2 s (DESIGN.md §2).
WRITE_WAIT_S = 1.0  # a line the deck does not take in this long is dropped, the port kept
OUT_LINES = 256  # lines waiting for the deck; past it a line is dropped, as one the deck does not read is


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


def log_text(line: bytes) -> str:
    """A line that is not a frame, as the board's log shows it: a damaged frame of a Wi-Fi record is cut at its
    prefix, as a scan names the neighbours' networks (DESIGN.md §9)."""
    at = line.find(PREFIX)
    if at >= 0 and WIFI_MARK in line[at:]:
        line = line[:at] + PREFIX + b"(a damaged Wi-Fi record, withheld)"
    return line.decode(errors="replace").rstrip("\r")


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
        self.lock = threading.Lock()          # the port's fd: opened, written and closed one at a time
        self.out: queue.Queue[bytes] = queue.Queue(OUT_LINES)
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
        self.clear()
        if was_open and self.on_lost:
            self.on_lost()

    def clear(self) -> None:
        """Drops the lines still waiting: they were for a deck that is gone, or for one that has just booted and will
        say hello for everything again."""
        try:
            while True:
                self.out.get_nowait()
        except queue.Empty:
            pass

    @property
    def connected(self) -> bool:
        return self.fd is not None

    def release(self, seconds: float) -> None:
        """Lets go of the port for `seconds`, or until release(0)."""
        self.released_until = time.time() + max(0.0, seconds)
        self.close()

    # Lines ------------------------------------------------------------------------------------------------------

    def send(self, record: dict) -> bool:
        """Queues one record for the deck and returns at once: False with no deck attached, or OUT_LINES behind."""
        if self.fd is None:
            return False
        try:
            self.out.put_nowait(encode(record))
        except queue.Full:
            return False  # The deck not reading: this line is dropped, as one it does not take is
        return True

    def write(self, data: bytes) -> None:
        """One line to the port, on the writer thread."""
        failed = False
        with self.lock:
            if self.fd is None:
                return
            try:
                while data:
                    _, ready, _ = select.select([], [self.fd], [], WRITE_WAIT_S)
                    if not ready:
                        return  # The deck not reading yet — booting: this line is dropped, the port kept
                    data = data[os.write(self.fd, data):]
            except BlockingIOError:
                return
            except OSError:
                failed = True  # The port gone: unplugged or reset
        if failed:
            self.close()

    def writer(self) -> None:
        while not self.stopping:
            try:
                data = self.out.get(timeout=0.5)
            except queue.Empty:
                continue
            self.write(data)

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
                self.clear()
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
                    self.on_log(log_text(raw))

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()
        threading.Thread(target=self.writer, daemon=True).start()
