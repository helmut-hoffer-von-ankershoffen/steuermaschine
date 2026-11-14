"""bin/review_gate.py: the review gate of a promote (Fable B1). Every gated write command of the candidate must be
approved; an allow-list entry needs unit, reason, owner and expiry, and never covers a daemon, queue, head or release
command. bin/promote no longer runs it (one commit verdict instead: bin/promote_verdict.py); it goes with the
review files."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "bin" / "review_gate.py"
TODAY = "2026-11-13"


def run(tmp_path: Path, rows: list[dict[str, str]], allow: str = "") -> subprocess.CompletedProcess[str]:
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    (tmp_path / "allow.toml").write_text(allow)
    return subprocess.run([sys.executable, "-I", str(GATE), str(tmp_path / "rows.json"), str(tmp_path / "allow.toml"), TODAY],
                          capture_output=True, check=False, text=True, timeout=30)


def row(path: str, file: str, state: str = "approved") -> dict[str, str]:
    return {"path": path, "file": file, "state": state, "digest": "0" * 64}


def entry(unit: str, expires: str = "2026-11-20") -> str:
    return f'[[allow]]\nunit = "{unit}"\nreason = "help text only"\nowner = "coordinator"\nexpires = "{expires}"\n'


def test_all_approved_passes_and_counts(tmp_path):
    done = run(tmp_path, [row("note", "reviews/note.md"), row("daemon queue", "reviews/daemon-queue.md")])
    assert done.returncode == 0 and "2/2 gated commands approved, 0 unit(s) refused" in done.stderr


def test_a_stale_or_open_unit_refuses(tmp_path):
    done = run(tmp_path, [row("note", "reviews/note.md", "stale"), row("doc ask", "reviews/doc-ask.md", "open")])
    assert done.returncode == 1 and "REFUSED reviews/note.md is stale" in done.stderr and "REFUSED reviews/doc-ask.md is open" in done.stderr


def test_an_allowed_unit_passes_and_is_printed(tmp_path):
    done = run(tmp_path, [row("note", "reviews/note.md", "stale")], entry("reviews/note.md"))
    assert done.returncode == 0 and "ALLOWED reviews/note.md (stale): help text only (owner coordinator, until 2026-11-20)" in done.stderr


def test_live_write_paths_are_never_allowed(tmp_path):
    for path in ("daemon train", "queue add", "head apply", "release resume"):
        done = run(tmp_path, [row(path, "reviews/x.md", "stale")], entry("reviews/x.md"))
        assert done.returncode == 1 and "never allowed" in done.stderr, path


def test_an_expired_or_incomplete_entry_refuses(tmp_path):
    assert run(tmp_path, [row("note", "reviews/note.md", "stale")], entry("reviews/note.md", "2026-11-12")).returncode == 1
    incomplete = '[[allow]]\nunit = "reviews/note.md"\nreason = "x"\n'
    done = run(tmp_path, [row("note", "reviews/note.md")], incomplete)
    assert done.returncode == 1 and "lacks owner, expires" in done.stderr
