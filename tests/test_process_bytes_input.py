"""Literal verifier requests use the centralized bounded process runner."""

import os
import sys

from steuermaschine.defaults import Subprocess


def test_bytes_request_is_exact_and_nonzero_exit_is_preserved(tmp_path):
    payload = b"\x00\xff\nrequest\r\n"
    rc, output = Subprocess().run_bytes(
        [sys.executable, "-I", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(7)"],
        cwd=str(tmp_path),
        timeout=10,
        env={"PATH": os.defpath},
        input=payload,
    )
    assert (rc, output) == (7, payload)


def test_bytes_request_timeout_fails_closed(tmp_path):
    rc, output = Subprocess().run_bytes(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        timeout=0.05,
        env={"PATH": os.defpath},
        input=b"request",
    )
    assert (rc, output) == (124, b"")
