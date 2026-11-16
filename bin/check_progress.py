"""Run one gate with private full output and bounded live progress."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class Terminated(SystemExit):
    """A termination signal requires the same child cleanup as an interrupt."""


def run(name: str, output: Path, command: list[str], interval: float = 30) -> int:
    print(f"check: START {name}", flush=True)
    started = time.monotonic()
    starting = True
    pending = None

    def terminate(signum, _frame):
        nonlocal pending
        if starting:
            pending = signum
        else:
            raise Terminated(128 + signum)

    watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old_handlers = {sig: signal.getsignal(sig) for sig in watched}
    child = None
    try:
        for sig in watched:
            signal.signal(sig, terminate)
        with output.open("wb") as stream:
            try:
                child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                stream.write(f"cannot start gate: {exc}\n".encode())
                return 127
            starting = False
            if pending is not None:
                raise Terminated(128 + pending)
            while True:
                try:
                    return child.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    print(f"check: RUN {name} ({int(time.monotonic() - started)}s)", flush=True)
    except (KeyboardInterrupt, Terminated):
        # Further termination signals cannot interrupt the bounded cleanup itself.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched)
        try:
            for sig in watched:
                signal.signal(sig, signal.SIG_IGN)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        raise
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)



if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: check_progress.py NAME OUTPUT COMMAND [ARGS...]")
    raise SystemExit(run(sys.argv[1], Path(sys.argv[2]), sys.argv[3:]))
