# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Refuse history that does not descend from the public root commit (scripts/public-root).

The public repository started from one clean commit; the earlier private history must never reach it. A branch made
from that old history has no path to the root, so `git merge-base --is-ancestor ROOT SHA` fails for it.

    check_public_root.py --pre-push REMOTE URL     the pre-push hook: refs on stdin as git passes them
    check_public_root.py --head [SHA]             CI: the checked-out commit (default HEAD)

Pre-push refuses: a pushed commit that does not descend from the root; any ref other than a branch (tags and notes come
only from releases made on public history, by hand); every push to the old private repository's URL.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ZERO = "0" * 40
OLD_REPO = "steuermaschine-dev"  # the archived private repository: nothing is pushed there any more


def root() -> str:
    path = Path(__file__).with_name("public-root")
    sha = path.read_text().split()[0] if path.exists() else ""
    if len(sha) != 40:
        sys.exit(f"public-root guard: {path} names no commit; refusing")
    return sha


def descends(sha: str, base: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", base, sha], capture_output=True).returncode == 0


def pre_push(remote: str, url: str, lines: list[str]) -> int:
    if OLD_REPO in url:
        print(f"public-root guard: {remote} is the archived private repository ({url}); push to the public one", file=sys.stderr)
        return 1
    base, bad = root(), []
    for line in lines:
        words = line.split()
        if len(words) != 4:
            continue
        local_ref, local_sha, remote_ref, _ = words
        if local_sha == ZERO:
            continue  # a deletion carries no history
        if not remote_ref.startswith("refs/heads/"):
            bad.append(f"{remote_ref}: only branches are pushed (tags and notes come from releases)")
        elif not descends(local_sha, base):
            bad.append(f"{local_ref} {local_sha[:12]}: does not descend from the public root {base[:12]} (old private history?)")
    for why in bad:
        print(f"public-root guard: refused {why}", file=sys.stderr)
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--pre-push"] and len(argv) >= 3:
        return pre_push(argv[1], argv[2], sys.stdin.read().splitlines())
    if argv[:1] == ["--head"]:
        sha = argv[1] if len(argv) > 1 else "HEAD"
        if descends(sha, root()):
            return 0
        print(f"public-root guard: {sha} does not descend from the public root {root()[:12]}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
