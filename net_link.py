"""The bridge's side of the link over Wi-Fi (DESIGN.md §9, §10.6; the knob's AGENT_DECK §19.9): the paired deck
found by its name, TLS with its certificate pinned, the deck's challenge answered, then the same `@deck` lines as on
the cable.

    link = NetLink(pairings=pairing.paired, load=pairing.load)
    bridge.attach(link)                      # its records and its loss come back with the link
    link.start()
    link.pause(True)                         # a deck on the cable: the cable wins, and this link stands down

A session: TLS to `<host>:<port>` from the deck's pairing, the certificate's SHA-256 compared with the one pinned (no
CA is trusted); the deck's `agentdeck-challenge <nonce>` answered with `agentdeck-answer <HMAC-SHA256 of the nonce
under the shared key>`; its `agentdeck-ok`; then lines both ways, written and read on one thread, as an SSL socket is
no place for two. A deck not there, or a session lost, is tried again every RETRY_S. Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import queue
import select
import socket
import ssl
import threading
import time

from link import WIFI, Link, decode, encode

GRACE_S = 15.0       # what is held outlives a short drop this long (DESIGN.md §10.6)
RETRY_S = 5.0        # a deck not found, or a session lost, tried again this often
SETTLE_S = 2.0       # let loose by the cable going, this link waits this long first: a cable plugged in can open, close
                     # and open again within about 100 ms (the knob's AGENT_DECK §14), which would otherwise make a
                     # session for the moment between, ended at once as the cable opens again
CONNECT_S = 10.0     # the TCP connect and the TLS handshake; a dimmed deck runs slower (AGENT_DECK §14)
AUTH_S = 10.0        # the challenge, from the handshake done
POLL_S = 0.05        # how long the thread waits on the socket before it sends what is queued
OUT_LINES = 256      # lines waiting for the deck; past it a line is dropped, as on the cable
MAX_LINE = 9 * 1024  # a line past this is dropped: the deck's longest is 8 KB
CHALLENGE, ANSWER, AUTH_OK = b"agentdeck-challenge ", b"agentdeck-answer ", b"agentdeck-ok"


def answer(challenge_line: bytes, key_hex: str) -> bytes | None:
    """The answer to the deck's challenge line, or None when it is not one."""
    if not challenge_line.startswith(CHALLENGE):
        return None
    try:
        nonce = bytes.fromhex(challenge_line[len(CHALLENGE):].decode("ascii"))
        key = bytes.fromhex(key_hex)
    except ValueError:
        return None
    if len(nonce) != 32 or len(key) != 32:
        return None
    return ANSWER + hmac.new(key, nonce, hashlib.sha256).hexdigest().encode() + b"\n"


def pinned(der: bytes | None, fingerprint: str) -> bool:
    """Whether the certificate served is the one paired."""
    return der is not None and hmac.compare_digest(hashlib.sha256(der).hexdigest(), fingerprint)


class NetLink(Link):
    kind = WIFI
    grace_s = GRACE_S

    def __init__(self, pairings, load, on_record=None, on_open=None, on_lost=None, on_log=None):
        self.pairings, self.load = pairings, load  # the ids paired; one's record from the Keychain
        self.on_record, self.on_open, self.on_lost, self.on_log = on_record, on_open, on_lost, on_log
        self.path: str | None = None
        self.sock: ssl.SSLSocket | None = None
        self.out: queue.Queue[bytes] = queue.Queue(OUT_LINES)
        self.paused = False
        self.stopping = False
        self.standby = lambda: False  # Whether another link has the deck now: the bridge sets it as it attaches this
        self.wake = threading.Event()
        self.settle_until = 0.0  # time.monotonic() before which this link, let loose, does not connect

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def reachable(self) -> bool:
        """Whether a deck is paired, so this link may reach it once the cable goes."""
        return bool(self.pairings())

    def pause(self, paused: bool) -> None:
        """A deck on the cable: this link stands down, its session closed; False: it looks for the deck again, once the
        cable has stayed gone SETTLE_S."""
        if self.paused and not paused:
            self.settle_until = time.monotonic() + SETTLE_S
        self.paused = paused
        self.wake.set()

    def release(self, seconds: float) -> None:
        """The cable's port is what /release lends out; this link has none."""

    def send(self, record: dict) -> bool:
        if self.sock is None:
            return False
        try:
            self.out.put_nowait(encode(record))
        except queue.Full:
            return False
        return True

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    # The session ------------------------------------------------------------------------------------------------

    @staticmethod
    def read_line(tls: ssl.SSLSocket, buf: bytearray, limit: int) -> bytes | None:
        """One line from `tls`, its line end cut; what came after it stays in `buf`. None when it ended first."""
        while b"\n" not in buf:
            if len(buf) > limit:
                return None
            chunk = tls.recv(4096)
            if not chunk:
                return None
            buf += chunk
        line, _, rest = bytes(buf).partition(b"\n")
        buf[:] = rest
        return line.rstrip(b"\r")

    def connect(self, deck_id: str, record: dict) -> tuple[ssl.SSLSocket, bytes] | None:
        """TLS to the deck, pinned, and its challenge answered: the session and what came after the deck's ok; None
        when it is not there or not the deck paired."""
        try:
            raw = socket.create_connection((record["host"], record["port"]), timeout=CONNECT_S)
        except OSError:
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the pin below is the check: the deck's certificate is self-signed
        try:
            tls = context.wrap_socket(raw)
            if not pinned(tls.getpeercert(binary_form=True), record["fingerprint"]):
                self.log(f"the deck {deck_id} served another certificate: not connected")
                tls.close()
                return None
            tls.settimeout(AUTH_S)
            buf = bytearray()
            challenge = self.read_line(tls, buf, 256)
            reply = answer(challenge, record["key"]) if challenge is not None else None
            if reply is None:
                tls.close()
                return None
            tls.sendall(reply)
            if self.read_line(tls, buf, 256) != AUTH_OK:
                self.log(f"the deck {deck_id} refused the answer: not connected")
                tls.close()
                return None
            tls.settimeout(0.0)
            return tls, bytes(buf)
        except (OSError, ssl.SSLError, ValueError):
            raw.close()
            return None

    def carry(self, tls: ssl.SSLSocket, buf: bytes = b"") -> str:
        """Lines both ways on this thread until the session ends or the link stands down; `buf` what came already.
        Why it ended, for the log."""
        first = True
        while not self.stopping and not self.paused:
            try:
                ready = first or tls.pending() > 0 or bool(select.select([tls], [], [], POLL_S)[0])
                if ready:
                    if not first:
                        chunk = tls.recv(16384)
                        if not chunk:
                            return "the deck closed it"
                        buf += chunk
                    first = False
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        record = decode(raw)
                        if record is not None and self.on_record:
                            self.on_record(record)
                    if len(buf) > MAX_LINE:
                        buf = b""
                while True:
                    try:
                        data = self.out.get_nowait()
                    except queue.Empty:
                        break
                    tls.settimeout(CONNECT_S)
                    tls.sendall(data)
                    tls.settimeout(0.0)
            except ssl.SSLWantReadError:
                continue
            except (OSError, ssl.SSLError) as e:
                return f"{type(e).__name__}: {e}"
        return "stood down for the cable" if self.paused else "the bridge stopping"

    def run(self) -> None:
        while not self.stopping:
            if self.paused or self.standby():
                self.wake.wait(RETRY_S)
                self.wake.clear()
                continue
            settle = self.settle_until - time.monotonic()
            if settle > 0:
                self.wake.wait(settle)  # a pause() in the wait wakes it: the cable back, this link stands down again
                self.wake.clear()
                continue
            ids = self.pairings()
            deck_id = ids[-1] if ids else None
            record = self.load(deck_id) if deck_id else None
            session = self.connect(deck_id, record) if record else None
            if session is None:
                self.wake.wait(RETRY_S)
                self.wake.clear()
                continue
            tls, early = session
            if self.paused or self.standby():  # the cable opened while this one connected: the cable wins
                tls.close()
                continue
            self.path = f"{record['host']}:{record['port']}"
            self.drain()
            self.sock = tls
            self.log(f"the deck {deck_id} reached over Wi-Fi at {self.path}")
            if self.on_open:
                self.on_open()
            why = self.carry(tls, early)
            self.sock = None
            try:
                tls.close()
            except OSError:
                pass
            self.drain()
            self.log(f"the deck {deck_id}'s Wi-Fi session ended: {why}")
            if self.on_lost:
                self.on_lost()

    def drain(self) -> None:
        try:
            while True:
                self.out.get_nowait()
        except queue.Empty:
            pass
