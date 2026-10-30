# Agent deck bridge

The [agent deck](https://github.com/alxv2016/nextion-agent-deck) is a small round knob display that sits on your desk and shows what Claude Code is doing: your
plan's usage, each session and sub-agent at work, and permission requests you can answer with the knob. This
bridge is the part that runs on your Mac. It takes what Claude Code reports and sends it to the deck over the
USB-C cable, and it sends your answers back.

It is a Claude Code plugin, `alxvlabs-agent-deck`, and a folder of Python scripts with nothing to install beyond
Python itself. The firmware for the deck is [alxv2016/nextion-timer](https://github.com/alxv2016/nextion-timer).

## What you need

- A Mac with Python 3.9 or later. The `python3` that comes with macOS works; so does Homebrew's. Linux should
  work too but is untested. Windows is not supported yet: there the plugin does nothing, so it does no harm.
- Claude Code, in the terminal or the VS Code extension.
- The agent deck, plugged into the Mac with a USB-C cable. It runs from the cable; there is no battery.

## Set it up

Inside Claude Code, in the terminal or the VS Code extension:

```text
/plugin marketplace add alxv2016/alxvlabs-agent-bridge
/plugin install alxvlabs-agent-deck@alxvlabs
```

On Claude Code 2.1.275 or later, one line does both:

```text
/plugin install alxvlabs-agent-deck --marketplace alxv2016/alxvlabs-agent-bridge
```

Then plug in the deck. The bridge starts by itself with the next thing Claude Code does, in the background with no
window, and stays running across sessions; the deck leaves `Connecting...` within a few seconds and shows your
sessions. Its log is `~/Library/Logs/agent-deck-bridge.log`.

- **Turn it off** with `/plugin disable alxvlabs-agent-deck@alxvlabs`, or remove it with `/plugin uninstall
  alxvlabs-agent-deck@alxvlabs`: the bridge notices within about ten seconds and stops.
- **Update** with `/plugin marketplace update alxvlabs`, then `/plugin update alxvlabs-agent-deck@alxvlabs`.
- The plugin sets no status line: Claude Code lets a plugin carry hooks but not a status line.

### Without the plugin

`install.py` sets the bridge up instead: the same hooks, written into Claude Code's settings, and a status line.
Use one or the other — while `install.py`'s hooks are in, the plugin's stand aside.

1. Put this folder where it can stay, for example `~/Applications/agent-deck-bridge`.
2. In a terminal, from inside the folder:

   ```sh
   python3 install.py
   ```

3. It adds the hooks, then asks whether to start the bridge at login:

   ```text
   Start the bridge at login?

     yes  The bridge starts now and every time you log in, in the background with no window, and starts again if it
          ever stops. macOS should list it in System Settings > General > Login Items, where you can switch it off.
          Its log is ~/Library/Logs/agent-deck-bridge.log. `python3 install.py --uninstall` removes it.
     no   Nothing starts by itself. Run `python3 bridge.py` in a terminal while you want the deck; Ctrl-C stops it.

   Start at login? [y/N]
   ```

   No is the default. `--login` or `--no-login` answers it without asking; run `install.py` again to change your
   mind — saying no then removes the login item.
4. Plug in the deck. With the bridge running it leaves `Connecting...` within a few seconds and shows your sessions.

What it changes, all undone by `python3 install.py --uninstall`:

- **Claude Code's settings** (`~/.claude/settings.json`, backed up first): hooks and a status line, so Claude Code
  tells the bridge what it is doing. Your other settings are left alone; if you already have a status line of your
  own, it stops and asks you to move it aside.
- **A login item**, only if you said yes: `~/Library/LaunchAgents/local.agent-deck.bridge.plist`.

If you move the folder, run `python3 install.py` again from its new place.

Either way, while it runs the bridge also asks Claude Code for your plan's usage once an hour (below);
`--refresh-usage 0` turns that off when you run it by hand.

## Check it

```sh
python3 install.py --status
```

It says whether the hooks or the plugin are in, whether the bridge starts at login, whether it is running, and
whether the deck is attached; it works the same with the plugin, from a copy of this folder or from the plugin's own
(`~/.claude/plugins/cache/alxvlabs/alxvlabs-agent-deck/<version>/`). With `install.py`'s hooks, the status line at the
bottom of a terminal Claude Code session reads `deck | …` while the bridge runs and `deck off` while it does not.

## Using it

- **Sessions and sub-agents** each get a page on the deck. A session is named as Claude Code titles it, as in VS
  Code's session list; a sub-agent after its task. Turn the knob to move between pages. A finished sub-agent's page goes
  when you send the session's next prompt (one still running stays); a session's goes when Claude Code ends it (`/exit` in a terminal).
- **The token count** on a page is what that agent's latest response has written, thinking included: the number
  Claude Code shows beside "Thinking...", from zero with each response. It moves a block at a time, not token by token.
- **Permission requests** open on the deck by themselves. Scroll to the end and press the knob on Allow or Deny.
  Claude Code's own prompt shows at the same time, and whichever you answer first settles it. Hold the knob to put
  a request aside and answer it later.
- **Questions Claude asks you**, with options to pick from, show on the deck as `Question: …` but are answered in
  Claude Code: the deck cannot pick an option for you.
- **Plan usage** comes from Claude Code. The VS Code extension does not save the figures its panel shows, so the
  bridge asks Claude Code for them when the deck connects and once an hour after (`claude -p /usage`, which asks
  the model nothing). A terminal session updates them after every reply.

## When the bridge is not running

Nothing breaks. Claude Code asks for permissions in the terminal as it always does, the hooks stay silent, and the
deck says `Connecting...`. Unplug the deck in the middle of a request and Claude Code's own prompt is still there
to answer.

## Running it by hand

`python3 install.py --no-login` adds only the hooks. Then start the bridge yourself, and stop it with Ctrl-C — with
the plugin, the bridge it started already holds the port, so a second one goes at once:

```sh
python3 bridge.py
```

By hand it prints each update it sends the deck, and the deck's own log lines as `board: …`.

| Option | Does |
| --- | --- |
| `--serial PATH` | The deck's port, when more than one `/dev/cu.usbmodem*` (Linux: `/dev/ttyACM*`) is plugged in |
| `--hold SECONDS` | How long a request waits for the deck before it is left to Claude Code alone (570) |
| `--refresh-usage SECONDS` | How often to ask Claude Code for the plan's usage while the deck is attached (3600; 0 never) |
| `--context-window TOKENS` | The context window to show a VS Code session's share of, for a model the bridge does not know |
| `--quiet` | Print no updates, only the bridge's and the deck's own lines |
| `--no-serial` | Run without the deck: print the updates only |
| `--port PORT` | The local port the hooks reach the bridge on (`DECK_BRIDGE_PORT`, else 8737); Claude Code's hooks must see the same `DECK_BRIDGE_PORT` |

## Flashing the deck while the bridge runs

The bridge keeps the deck's USB port open. A tool that needs the port — `idf.py flash`, `idf.py monitor`, a
serial terminal — must ask it to let go first:

```sh
curl -s -X POST -d '{"seconds": 600}' http://127.0.0.1:8737/release
```

`{"seconds": 0}` hands the port back early; otherwise the bridge takes it again when the time is up. The firmware
repo's own build and test scripts do this themselves.

## Troubleshooting

| What you see | Why, and what to do |
| --- | --- |
| The deck stays on `Connecting...` | The bridge is not running or cannot find the deck: run `install.py --status`. With two USB serial devices plugged in, start it by hand with `--serial`. |
| The status line reads `deck off` | The bridge is not running: `install.py --status`, then look at the log |
| Everything shows twice, or the plugin seems to do nothing | Both the plugin and `install.py`'s hooks are in: the plugin's stand aside, so the bridge follows `install.py`'s. Keep one — `python3 install.py --uninstall`, or `/plugin uninstall alxvlabs-agent-deck@alxvlabs` |
| Requests never reach the deck | Claude Code is approving them itself, in auto mode or by a rule, so no permission prompt exists to send |
| Usage on the deck lags VS Code's panel | It refreshes hourly; type `/usage` in any terminal `claude` session for fresh figures at once |
| No context share on a VS Code session's page | The bridge does not know that model's window: run one terminal session on it, or pass `--context-window` |
| `idf.py` cannot open the port | The bridge holds it: release it as above |

## What it reads and sends

- **Reads:** what Claude Code's hooks and status line tell it; Claude Code's transcripts under `~/.claude/projects`,
  for token counts, session titles and sub-agent names; and `~/.claude.json`, of which it keeps only the saved usage figures.
- **Sends:** to the deck over the USB cable, and nowhere else. It listens on `127.0.0.1` only. The hourly usage
  refresh is Claude Code's own `/usage`, under your own login.

## Files

| File | Is |
| --- | --- |
| `.claude-plugin/`, `hooks/` | The Claude Code plugin and its marketplace |
| `install.py` | Setting up without the plugin, checking and removing |
| `bridge.py` | The bridge itself |
| `hook.py` | What Claude Code runs for each hook and status line |
| `state.py`, `transcripts.py`, `usage_cache.py`, `usage_refresh.py`, `serial_link.py`, `plugin_state.py` | Its parts |
| `DESIGN.md` | How it works and why |
| `test_bridge.py`, `replay.py`, `scrub.py`, `fixtures/` | For working on the bridge |

## Working on the bridge

```sh
python3 -m unittest discover -s .
```

The tests replay recorded Claude Code sessions (`fixtures/`) into the updates the deck would get, with no deck and
no Claude Code. To record one, start the bridge with `--record FILE`; `scrub.py` cleans a recording of ids, paths
and text before it goes in `fixtures/`, and `replay.py` turns it into the expected updates. The design and the
reasons behind it are in [DESIGN.md](DESIGN.md); the deck's side, the records and the link on the cable, in the
firmware repo's [docs/ONX2424G013_AGENT_DECK.md](https://github.com/alxv2016/nextion-timer/blob/main/docs/ONX2424G013_AGENT_DECK.md)
§6 and §16.

To try the plugin from a working copy, point Claude Code at the folder: `claude --plugin-dir .` — a bridge started
that way is never seen installed, so stop it yourself afterwards. Raise `version` in `.claude-plugin/plugin.json`
for each release: Claude Code updates an installed plugin only when it changes.
