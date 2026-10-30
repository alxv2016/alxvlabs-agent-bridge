"""Whether the bridge's Claude Code plugin is installed and switched on, as Claude Code's own files say.

    installed(name)   # some install of the plugin is listed (~/.claude/plugins/installed_plugins.json)
    enabled(name)     # and the settings of that install's scope have it switched on (enabledPlugins)
    install_paths(name)            # the folders listed, newest first: an update replaces them (DESIGN.md §5)
    current_folder(name, own)      # the folder a bridge should start from

Claude Code leaves a process the plugin started running when the plugin is disabled or uninstalled (DESIGN.md
§5), so a bridge the plugin started asks this on its tick and stops when the answer is no. An install's scope
picks its settings file: `user` the user settings, `project` the project's `.claude/settings.json`, `local` its
`.claude/settings.local.json`; the plugin is off only when every install's file has
`enabledPlugins["<name>@<marketplace>"]` false, which disabling writes, or no install is listed, which uninstalling
leaves. Anything missing or unreadable counts as on, so a file caught mid-write never stops the bridge. Standard
library only.
"""

from __future__ import annotations

import json
import os

CLAUDE_DIR = os.path.expanduser("~/.claude")


def _read(path: str):
    """The file's JSON; None when it is missing; ... when it cannot be read or parsed."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return ...


def installs(name: str, claude_dir: str = CLAUDE_DIR) -> dict | None:
    """{"<name>@<marketplace>": [install, …]} for every listed install of the plugin; None when unreadable."""
    data = _read(os.path.join(claude_dir, "plugins", "installed_plugins.json"))
    if data is ...:
        return None
    plugins = (data or {}).get("plugins", {}) if isinstance(data, dict) else {}
    return {k: v for k, v in plugins.items() if k.split("@", 1)[0] == name and isinstance(v, list)}


def installed(name: str, claude_dir: str = CLAUDE_DIR) -> bool:
    found = installs(name, claude_dir)
    return found is None or bool(found)


def install_paths(name: str, claude_dir: str = CLAUDE_DIR) -> list[str] | None:
    """The folders Claude Code lists for the plugin, newest first; None when the file is unreadable."""
    found = installs(name, claude_dir)
    if found is None:
        return None
    entries = [i for v in found.values() for i in v if isinstance(i, dict) and i.get("installPath")]
    entries.sort(key=lambda i: str(i.get("lastUpdated", "")), reverse=True)
    return [os.path.realpath(str(i["installPath"])) for i in entries]


def current_folder(name: str, own: str, claude_dir: str = CLAUDE_DIR) -> str:
    """The folder a bridge should start from: `own` while it is listed or nothing is, else the newest listed one on
    disk, so a session still running an old version's hooks starts the current code."""
    own = os.path.realpath(own)
    paths = install_paths(name, claude_dir) or []
    if own in paths:
        return own
    return next((p for p in paths if os.path.isfile(os.path.join(p, "bridge.py"))), own)


def settings_file(install: dict, claude_dir: str = CLAUDE_DIR) -> str | None:
    scope = install.get("scope", "user")
    if scope == "user":
        return os.path.join(claude_dir, "settings.json")
    project = install.get("projectPath")
    if not project:
        return None
    return os.path.join(project, ".claude", "settings.local.json" if scope == "local" else "settings.json")


def enabled(name: str, claude_dir: str = CLAUDE_DIR) -> bool:
    found = installs(name, claude_dir)
    if found is None:
        return True
    for key, entries in found.items():
        for install in entries:
            path = settings_file(install, claude_dir) if isinstance(install, dict) else None
            if path is None:
                return True  # A scope this cannot place: left running
            settings = _read(path)
            if settings is ...:
                return True
            switches = settings.get("enabledPlugins") if isinstance(settings, dict) else None
            if not isinstance(switches, dict) or switches.get(key) is not False:
                return True
    return False
