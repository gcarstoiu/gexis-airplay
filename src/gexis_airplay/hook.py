# SPDX-License-Identifier: GPL-3.0-or-later
"""**Shairport Sync's active-state hooks** (ADR-0167 T5 in gexis-player).

Run by Shairport Sync, waited for (`wait_for_completion`):

    python3 -m gexis_airplay.hook begin   # before it goes active
    python3 -m gexis_airplay.hook end     # after it has gone inactive

`begin` asks the plugin to take the device and returns once the core says
it is free - before Shairport Sync opens it, so it never finds the device
busy. It always returns, and always with 0: a hook that failed or hung
would hold an AirPlay session's setup, which the Apple device would see as
the player not answering. The plugin bounds its own wait (`main.BEGIN_S`);
this bound is only for a plugin that is not there.
"""
from __future__ import annotations

import socket
import sys

SOCKET = "/run/gexis-airplay/hook.sock"
#: Longer than the plugin's own bound, so the plugin's answer is the one
#: that ends the wait.
WAIT_S = 12.0


def main(argv: list[str]) -> int:
    what = argv[1] if len(argv) > 1 else ""
    if what not in ("begin", "end"):
        print("usage: python3 -m gexis_airplay.hook begin|end", file=sys.stderr)
        return 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(WAIT_S)
            s.connect(SOCKET)
            s.sendall(what.encode() + b"\n")
            if what == "begin":
                answer = s.makefile("rb").readline().strip().decode(errors="replace")
                print(f"airplay hook: {answer or 'no answer'}", file=sys.stderr)
    except OSError as exc:
        print(f"airplay hook: the plugin did not answer ({exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
