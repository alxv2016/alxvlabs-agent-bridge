#!/usr/bin/env python3
"""Plays a recording through the bridge's state machine and prints the records, or checks them.

    replay.py INPUTS.jsonl                    # the records, one JSON line each
    replay.py INPUTS.jsonl --check RECORDS.jsonl [--context-window N]

INPUTS is what `bridge.py --record` writes: {"t": seconds, "kind": "hook"|"status"|"subagent-status",
"input": {...}} a line. The clock is each input's `t`, and the bridge's once-a-second tick runs on every whole
second between inputs, so a recording replays into the same records every time, with no board
(DESIGN.md §2). --check prints the first difference and exits 1 when they differ.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

from state import Deck


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def replay(inputs: list[dict], context_window: int | None = None) -> list[dict]:
    deck, out, last = Deck(context_window), [], None
    for item in inputs:
        t = float(item["t"])
        if last is not None:
            for s in range(math.floor(last) + 1, math.floor(t) + 1):
                out += deck.tick(float(s))
        out += deck.feed(item["kind"], item["input"], t)
        last = t
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("inputs")
    ap.add_argument("--check", metavar="RECORDS")
    ap.add_argument("--context-window", type=int, help="as bridge.py was given it")
    args = ap.parse_args()

    records = replay(load(args.inputs), args.context_window)
    if not args.check:
        for r in records:
            print(json.dumps(r))
        return 0
    expected = load(args.check)
    for i, (got, want) in enumerate(zip(records, expected)):
        if got != want:
            print(f"record {i + 1} differs:\n  got  {json.dumps(got)}\n  want {json.dumps(want)}")
            return 1
    if len(records) != len(expected):
        print(f"{len(records)} records, {len(expected)} expected")
        return 1
    print(f"replay: {len(records)} records as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
