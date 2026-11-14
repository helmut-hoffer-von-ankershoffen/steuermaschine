"""Test-only entry for the cross-process run lock tests: the plugin's real policy and the real lock, with the load
fixed by the test (LOCK_TEST = {"load": 5, "settings": {...}}). The product has no way to fake the load."""

import json
import os
import sys

from steuermaschine.commands import gate
from steuermaschine.defaults import Subprocess, SystemClock
from steuermaschine.runlock import Request, RunLock
from steuermaschine_heinzel.native import lock


def main(argv: list[str]) -> int:
    conf = json.loads(os.environ["LOCK_TEST"])
    commands, clock = Subprocess(), SystemClock()
    say = lambda text: print(text, file=sys.stderr, flush=True)  # noqa: E731
    opts = {"priority": 0, "max_wait": 0, "timeout": None}
    flags = set()
    verb, words = argv[0], argv[1:]
    while words and words[0] != "--":
        if words[0] in ("--priority", "--max-wait", "--timeout"):
            opts[words[0][2:].replace("-", "_")] = int(words[1])
            words = words[2:]
        else:
            flags.add(words[0])
            words = words[1:]
    command = words[1:]
    rl = RunLock(lock.policy(conf["settings"], commands, clock, "--allow-full" in flags, os.getcwd()), commands, clock, load=lambda: float(conf["load"]), say=say)
    if verb == "status":
        print("\n".join(rl.status().render()))
        return 0
    if not command or opts["priority"] not in (0, 1, 2):
        return 2
    request = Request(argv=command, environ=dict(os.environ), timeout_seconds=opts["timeout"], priority=opts["priority"], max_wait_seconds=opts["max_wait"],
                      light="--light" in flags, hold_exempt="--hold-exempt" in flags, cwd=os.getcwd())
    return gate.execute(rl, request, say).exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
