"""Keeps Claude Code's saved plan usage fresh while the deck is attached, by asking Claude Code for it.

    refresh = UsageRefresh(every_s=3600, wanted=bridge.device_ready)
    refresh.start()        # a thread: `claude -p /usage` as soon as wanted() says so, then every every_s while it does

The VS Code extension fetches the plan's usage for its panel without saving it; `/usage` saves it to
`~/.claude.json`, which usage_cache.py reads (DESIGN.md §3). The run reads the account's usage and asks
the model nothing. It fires no hooks — so it never shows on the deck as a session of its own — and keeps no session.
`--bare` would skip the login it needs, so it is not used.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

COMMAND = ["-p", "/usage", "--settings", '{"disableAllHooks": true}', "--no-session-persistence"]
TIMEOUT_S = 60


class UsageRefresh:
    def __init__(self, every_s: float, wanted, claude: str | None = None):
        self.every_s, self.wanted = every_s, wanted
        self.claude = claude or shutil.which("claude")
        self.stopping = False

    def command(self) -> list[str] | None:
        return [self.claude] + COMMAND if self.claude else None

    def run_once(self) -> bool:
        """One `/usage`; whether it ran to the end. Its output is not needed: the cache it writes is."""
        cmd = self.command()
        if cmd is None:
            return False
        try:
            return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=TIMEOUT_S).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def run(self) -> None:
        last = 0.0
        while not self.stopping:
            time.sleep(1)
            if self.wanted() and time.time() - last >= self.every_s:
                last = time.time()
                self.run_once()

    def start(self) -> bool:
        if self.every_s <= 0 or self.claude is None:
            return False
        threading.Thread(target=self.run, daemon=True).start()
        return True
