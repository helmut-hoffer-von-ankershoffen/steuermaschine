"""Run one gate with private full output and bounded live progress."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def run(name: str, output: Path, command: list[str], interval: float = 30) -> int:
    print(f"check: START {name}", flush=True)
    started = time.monotonic()
    with output.open("wb") as stream:
        try:
            child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        except OSError as exc:
            stream.write(f"cannot start gate: {exc}\n".encode())
            return 127
        try:
            while True:
                try:
                    return child.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    print(f"check: RUN {name} ({int(time.monotonic() - started)}s)", flush=True)
        except KeyboardInterrupt:
            if child.poll() is None:
                child.terminate()
                child.wait()
            raise


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: check_progress.py NAME OUTPUT COMMAND [ARGS...]")
    raise SystemExit(run(sys.argv[1], Path(sys.argv[2]), sys.argv[3:]))
