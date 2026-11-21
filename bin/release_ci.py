#!/usr/bin/env python3
"""The automatic sm release: the CI gate and the Mini follower, one module.

    release_ci.py record PR --reviewer NAME     the reviewer's verdict: status `Gate review` = success on the PR's head
    release_ci.py gate --sha SHA                CI (release.yml): is the merge commit SHA releasable, and as which version?
    release_ci.py follow                        the Mini (launchd, every 5 min): promote the newest release, restart loops

The verdict. The independent reviewer reads the pull request and then runs `release_ci.py record PR --reviewer NAME`,
which sets the commit status `Gate review` (state success, description `APPROVE reviewer=NAME`) on the PR's head commit.
A merge to main is released when its CI is green (release.yml starts on the finished CI run of main), the merged pull
request's final head carries that status, and the reviewer is none of the commit's authors (bin/promote_verdict.py's
identity rule). No verdict, no release: nothing is dispatched by hand. The version is the commitizen bump from the
commits since the last tag (a patch bump when they carry no release note); the tag is made by the GitHub release, on
the merged commit, after PyPI took the files.

The follower. It reads the latest GitHub release, finds the verdict of that tag's commit the same way, records it as the
promote verdict (`promote_verdict.py record-ci`) and runs bin/promote for exactly that commit in its own clone
($SM_FOLLOW_CLONE, default ~/Code/steuermaschine-follow). promote waits for idle admission and release steps itself;
afterwards the follower restarts the launchd loops that run the stable `sm`. It never goes back: a release that does not
descend from the live commit is refused. Exit 0: live or nothing to do; 1: refused (the line says why); 75: wait.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promote_verdict as pv  # noqa: E402  (sibling script, found through the path line above)

CONTEXT = "Gate review"
DESCRIPTION = re.compile(r"APPROVE reviewer=(\S.*)")
REPO = os.environ.get("SM_RELEASE_REPO", "helmut-hoffer-von-ankershoffen/steuermaschine")
LOOPS = re.compile(r"steuermaschine-stable|\.local/bin/sm<|/sm</string>")
Run = Callable[..., str]


class Refused(Exception):
    """Not releasable or not promotable; the message says why."""


def sh(*cmd: str, cwd: Path | None = None, check: bool = True) -> str:
    done = subprocess.run(cmd, capture_output=True, check=False, text=True, cwd=cwd, timeout=900)
    if check and done.returncode != 0:
        raise Refused(f"{' '.join(cmd[:3])} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


def api(path: str, run: Run = sh) -> object:
    return json.loads(run("gh", "api", "-H", "Accept: application/vnd.github+json", path))


def verdict_for(sha: str, api_get: Callable[[str], object] | None = None) -> tuple[int, str, str]:
    """(PR number, PR head, reviewer) of the merged pull request of commit SHA; Refused when there is no valid verdict."""
    get = api_get or api
    pulls = get(f"repos/{REPO}/commits/{sha}/pulls")
    merged = [p for p in pulls if isinstance(p, dict) and p.get("merge_commit_sha") == sha and p.get("merged_at")] if isinstance(pulls, list) else []
    if not merged:
        raise Refused(f"{sha[:12]} was not merged from a pull request: no review verdict, no release")
    pull = merged[0]
    head = pull["head"]["sha"]
    statuses = get(f"repos/{REPO}/commits/{head}/statuses?per_page=100")
    latest = next((s for s in statuses if s.get("context") == CONTEXT), None) if isinstance(statuses, list) else None  # newest first
    if latest is None or latest.get("state") != "success":
        raise Refused(f"PR #{pull['number']} head {head[:12]} has no `{CONTEXT}` success: the independent review is missing")
    match = DESCRIPTION.fullmatch((latest.get("description") or "").strip())
    if not match:
        raise Refused(f"the `{CONTEXT}` of PR #{pull['number']} is not `APPROVE reviewer=NAME`: {(latest.get('description') or '')[:60]!r}")
    return int(pull["number"]), head, match.group(1).strip()


def independent(repo: Path, sha: str, reviewer: str) -> None:
    try:
        pv.independent(repo, f"{sha}~1", sha, reviewer)  # a squash merge is one commit; its parent is the base
    except pv.Refused as exc:
        raise Refused(str(exc)) from None


def next_version(repo: Path, run: Run = sh) -> str:
    """The commitizen bump from the commits since the last tag, else a patch bump of the last tag."""
    try:
        got = run("uv", "run", "cz", "bump", "--get-next", "--yes", cwd=repo).strip().splitlines()
        if got and re.fullmatch(r"\d+\.\d+\.\d+", got[-1]):
            return got[-1]
    except Refused:
        pass
    tags = [t for t in run("git", "tag", "--list", "v*", cwd=repo).split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    if not tags:
        raise Refused("no release tag vX.Y.Z to bump from")
    major, minor, patch = max(tuple(int(n) for n in t[1:].split(".")) for t in tags)
    return f"{major}.{minor}.{patch + 1}"


def gate(repo: Path, sha: str, api_get: Callable[[str], object] | None = None, run: Run = sh) -> dict[str, str]:
    """The outputs of the release gate; Refused names why SHA is not released."""
    if run("git", "tag", "--list", "v*", "--contains", sha, cwd=repo).strip():
        raise Refused(f"{sha[:12]} is already in a release")
    number, head, reviewer = verdict_for(sha, api_get)
    independent(repo, sha, reviewer)
    return {"version": next_version(repo, run), "reviewer": reviewer, "pr": str(number), "head": head}


def record(pr: int, reviewer: str, run: Run = sh) -> str:
    pull = api(f"repos/{REPO}/pulls/{pr}", run)
    head = str(pull["head"]["sha"])  # type: ignore[index,call-overload]
    run("gh", "api", f"repos/{REPO}/statuses/{head}", "-f", "state=success", "-f", f"context={CONTEXT}",
        "-f", f"description=APPROVE reviewer={reviewer}", "-f", "target_url=https://github.com/" + REPO + f"/pull/{pr}")
    return head


def loops(home: Path) -> list[str]:
    """Labels of the launchd agents that run the stable `sm` (a --loop daemon restarts onto the new release)."""
    found = []
    for plist in sorted((home / "Library" / "LaunchAgents").glob("*.plist")):
        try:
            if LOOPS.search(plist.read_text(encoding="utf-8", errors="replace")):
                found.append(plist.stem)
        except OSError:
            continue
    return found


def follow(stable: Path, clone: Path, home: Path, run: Run = sh, api_get: Callable[[str], object] | None = None) -> str:
    """Promote the latest release's commit. Returns a one-line outcome; Refused when it is not promoted."""
    get = api_get or (lambda p: api(p, run))
    latest = get(f"repos/{REPO}/releases/latest")
    tag = str(latest["tag_name"])  # type: ignore[index,call-overload]
    if not clone.exists():
        run("gh", "repo", "clone", REPO, str(clone))
    run("git", "fetch", "--quiet", "--tags", "--force", "origin", cwd=clone)
    commit = run("git", "rev-parse", f"{tag}^{{commit}}", cwd=clone).strip()
    try:
        live = pv.promoted_base(stable, clone)
    except pv.Refused as exc:
        raise Refused(str(exc)) from None
    if live == commit:
        return f"{tag} ({commit[:12]}) is live"
    if live != "none" and subprocess.run(["git", "-C", str(clone), "merge-base", "--is-ancestor", live, commit], check=False).returncode != 0:
        raise Refused(f"{tag} ({commit[:12]}) does not descend from the live {live[:12]}: not going back or sideways")
    number, _, reviewer = verdict_for(commit, get)
    run("git", "checkout", "--quiet", "--force", "--detach", commit, cwd=clone)
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        pv.record_ci(stable, clone, commit, reviewer, f"ci {tag} https://github.com/{REPO}/pull/{number}", now)
    except pv.Refused as exc:
        raise Refused(str(exc)) from None
    # --accept-diff: the CI gate and the review stand for the change of the CLI surface; promote still prints each difference.
    done = subprocess.run([str(clone / "bin" / "promote"), "--accept-diff"], cwd=clone, env={**os.environ, "SM_STABLE": str(stable)}, check=False)
    if done.returncode != 0:
        raise Refused(f"bin/promote refused {tag} ({commit[:12]}) with exit {done.returncode}; the stable install is unchanged (the lines above say why)")
    uid = os.getuid()
    for label in loops(home):
        run("launchctl", "kickstart", "-k", f"gui/{uid}/{label}", check=False)
    return f"{tag} ({commit[:12]}) is live; restarted {len(loops(home))} loop(s)"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="release_ci.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="verb", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("pr", type=int)
    rec.add_argument("--reviewer", required=True)
    gt = sub.add_parser("gate")
    gt.add_argument("--sha", required=True)
    sub.add_parser("follow")
    args = parser.parse_args(argv)
    try:
        if args.verb == "record":
            print(f"release_ci: `{CONTEXT}` success on {record(args.pr, args.reviewer)[:12]} (PR #{args.pr}, reviewer {args.reviewer})")
        elif args.verb == "gate":
            out = gate(Path.cwd(), args.sha)
            lines = ["release=true", *(f"{k}={v}" for k, v in out.items())]
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            print("release_ci: releasing " + " ".join(lines[1:]))
        else:
            stable = Path(os.environ.get("SM_STABLE", Path.home() / "Code" / "steuermaschine-stable"))
            print("release_ci: " + follow(stable, Path(os.environ.get("SM_FOLLOW_CLONE", Path.home() / "Code" / "steuermaschine-follow")), Path.home()))
    except Refused as exc:
        if args.verb == "gate" and os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write("release=false\n")
        print(f"release_ci: NOT {'released' if args.verb == 'gate' else 'promoted'}: {exc}", file=sys.stderr)
        return 0 if args.verb == "gate" else 1  # a merge without verdict is no failure of CI
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
