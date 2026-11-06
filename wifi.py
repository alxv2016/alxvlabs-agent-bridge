"""What the bridge asks macOS when the deck joins a network: the Mac's saved Wi-Fi networks, and one's password.

    mac = Mac()                                   # run: subprocess.run, or a fake in the tests
    saved = mac.saved()                           # the Mac's saved networks' names; empty when it has none or no Wi-Fi
    both = candidates(scan_names, saved)          # the deck's scan crossed with them, in the deck's order
    password, reason = mac.password("Studio")     # (password, None), or (None, "cancelled" | "no_password")

The deck's Connect sends its scan on the cable; the bridge crosses it with the networks the Mac has saved, as macOS
hides the current network's name from a process with no Location permission, and reads the one left from the
Keychain, behind macOS's own admin prompt (DESIGN.md §9; the knob's AGENT_DECK §19.8). Every command is run with
its arguments as a list, never through a shell, and its absolute path.

The password is returned and nothing else: it is never logged, printed or put in an exception here, and the caller
sends it on the cable and keeps nothing.
"""

from __future__ import annotations

import subprocess

NETWORKSETUP = "/usr/sbin/networksetup"
SECURITY = "/usr/bin/security"
WIFI_PORT = "Wi-Fi"                     # `networksetup -listallhardwareports`' name for the radio
PREFERRED_HEADER = "Preferred networks on "
AIRPORT_KIND = "AirPort network password"  # The Keychain's description (kind) of a saved Wi-Fi network's item
LIST_TIMEOUT_S = 10
# The person types their admin password into macOS's prompt while `security` waits: long enough to find the Mac.
KEYCHAIN_TIMEOUT_S = 120

NOT_IN_REACH, CANCELLED, NO_PASSWORD = "not_in_reach", "cancelled", "no_password"

# `security` exits with the Security framework's OSStatus cut to its low byte (`security error` names each; DESIGN.md §9):
ITEM_NOT_FOUND = 44                    # errSecItemNotFound, -25300: no saved password for that network
REFUSED = {
    128,  # errSecUserCanceled, -128: Cancel on the admin prompt
    51,   # errSecAuthFailed, -25293: the wrong admin password
    36,   # errSecInteractionNotAllowed, -25308: no prompt could be shown
    155,  # errAuthorizationDenied, -60005
    154,  # errAuthorizationCanceled, -60006
}


def run_text(run, argv: list[str], timeout: float) -> str | None:
    """A command's standard output, or None when it failed, timed out or could not start."""
    try:
        done = run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    out = done.stdout
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else out


def parse_hardware_ports(text: str) -> str | None:
    """The Wi-Fi port's device, `en0`, from `networksetup -listallhardwareports`; None with no Wi-Fi port."""
    port = None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "Hardware Port":
            port = value.strip()
        elif key.strip() == "Device" and port == WIFI_PORT:
            return value.strip() or None
    return None


def parse_preferred(text: str) -> list[str]:
    """The saved networks' names from `networksetup -listpreferredwirelessnetworks`: a header, then each name on a line
    of its own after a tab. Nothing without the header — `en3 is not a Wi-Fi interface.` and the like."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith(PREFERRED_HEADER):
        return []
    return [line[1:] for line in lines[1:] if line.startswith("\t") and line[1:]]


def wifi_device(run=subprocess.run) -> str | None:
    """The Mac's Wi-Fi device, or None when it has none or networksetup fails."""
    text = run_text(run, [NETWORKSETUP, "-listallhardwareports"], LIST_TIMEOUT_S)
    return parse_hardware_ports(text) if text is not None else None


def saved_networks(device: str, run=subprocess.run) -> list[str]:
    """The networks the Mac has saved on `device`, most preferred first; empty on any failure."""
    text = run_text(run, [NETWORKSETUP, "-listpreferredwirelessnetworks", device], LIST_TIMEOUT_S)
    return parse_preferred(text) if text is not None else []


def keychain_password(name: str, run=subprocess.run) -> tuple[str | None, str | None]:
    """The saved password of network `name` from the Keychain, with only the newline `security` ends it with taken
    off: (password, None), or (None, why) — "cancelled" when the admin prompt was cancelled, refused or never
    answered, "no_password" for no item or any other failure."""
    argv = [SECURITY, "find-generic-password", "-D", AIRPORT_KIND, "-a", name, "-w"]
    try:
        done = run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   timeout=KEYCHAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, CANCELLED  # The prompt left unanswered; run() has killed `security`
    except (OSError, subprocess.SubprocessError):
        return None, NO_PASSWORD
    if done.returncode in REFUSED:
        return None, CANCELLED
    if done.returncode != 0:
        return None, NO_PASSWORD  # ITEM_NOT_FOUND, and anything else
    out = done.stdout
    try:
        password = out.decode("utf-8") if isinstance(out, bytes) else out
    except UnicodeDecodeError:
        return None, NO_PASSWORD  # Not a password the deck could take; not shown either
    if password.endswith("\n"):
        password = password[:-1]
    return (password, None) if password else (None, NO_PASSWORD)


def candidates(scan_names: list[str], saved) -> list[str]:
    """The scanned networks the Mac has saved, in the deck's order — strongest first — each once."""
    known, out = set(saved), []
    for name in scan_names:
        if name in known and name not in out:
            out.append(name)
    return out


class Mac:
    """The three questions put to macOS, with `run` in place of subprocess.run for the tests."""

    def __init__(self, run=subprocess.run):
        self.run = run

    def saved(self) -> list[str]:
        device = wifi_device(self.run)
        return saved_networks(device, self.run) if device else []

    def password(self, name: str) -> tuple[str | None, str | None]:
        return keychain_password(name, self.run)
