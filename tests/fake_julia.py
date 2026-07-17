#!/usr/bin/env python3
"""
A tiny, controllable stand-in for the `julia` REPL, used by the fast test suite so
the server's timeout / interrupt / kill / crash logic can be exercised
deterministically without a real Julia install.

It reads Julia-ish lines from stdin (exactly what the server writes) and:

  * `println("X")`            -> prints X (this is how the server's markers and
                                 simple test output come back)
  * `__HANG_UNTIL_SIGINT__`   -> blocks until it receives SIGINT (Ctrl-C), then
                                 resumes reading — simulating a REPL that recovers
                                 from an interrupt with its state intact
  * `__HANG_IGNORE_SIGINT__`  -> ignores SIGINT and blocks forever, forcing the
                                 server to fall back to kill + restart
  * `__CRASH__`               -> exits immediately (simulates a dead process / EOF)
  * anything else             -> ignored (like evaluating an expression that prints
                                 nothing)

The server drives everything through the same channel it uses for real Julia, so
this stub gives high-fidelity coverage of the recovery machinery.
"""

import re
import signal
import sys
import time

PRINTLN = re.compile(r'^\s*println\("(?P<text>.*)"\)\s*$')


def hang_until_sigint():
    try:
        while True:
            time.sleep(0.02)
    except KeyboardInterrupt:
        # Recovered — return to the main read loop and carry on.
        pass


def hang_ignore_sigint():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        time.sleep(0.02)


def main():
    while True:
        line = sys.stdin.readline()
        if not line:  # EOF: parent closed our stdin
            break
        line = line.rstrip("\n")

        if line.strip() == "__CRASH__":
            sys.exit(1)
        elif line.strip() == "__HANG_UNTIL_SIGINT__":
            hang_until_sigint()
            continue
        elif line.strip() == "__HANG_IGNORE_SIGINT__":
            hang_ignore_sigint()  # never returns
        elif line.strip() == "exit()":
            break

        m = PRINTLN.match(line)
        if m:
            sys.stdout.write(m.group("text") + "\n")
            sys.stdout.flush()
        # else: silently ignore, like a no-output expression


if __name__ == "__main__":
    main()
