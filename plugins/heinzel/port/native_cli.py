#!/usr/bin/env python3
"""The native side of the parity harness: the `sm` command tree with the test bypass of the review gate.

A native write command is off until its review is approved, and no setting or environment variable changes that. A
parity run has to exercise the write commands before the review, so this driver sets the constructor-level test
switch (`Project.allow_unreviewed`), and it refuses to run anywhere but inside the harness: `gh` must resolve to the
scripted one of port/fakebin and HOME must be a throwaway directory that holds the harness's answers file.
"""

import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv: list[str]) -> None:
    fake = str((HERE / "fakebin" / "gh").resolve())
    found = shutil.which("gh") or ""
    home = Path(os.environ.get("HOME", ""))
    if not found or str(Path(found).resolve()) != fake or not (home / "answers.json").is_file() or "PARITY_TRANSCRIPT" not in os.environ:
        sys.exit("port/native_cli.py runs only inside port/parity.py (scripted gh first on PATH, throwaway HOME)")
    from steuermaschine import PROG, app

    rt = app.runtime(None, {})
    if rt.project is None or rt.plugin is None:
        sys.exit(f"the harness project did not load its plugin: {rt.problem}")
    rt.project.allow_unreviewed = True
    app.OUT["format"] = "text"
    app.command_tree(rt)(args=argv, prog_name=PROG)


if __name__ == "__main__":
    main(sys.argv[1:])
