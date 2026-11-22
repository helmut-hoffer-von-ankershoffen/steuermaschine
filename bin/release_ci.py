#!/usr/bin/env python3
"""The automatic sm release: the CI gate, the release steps and the Mini follower, one module.

    release_ci.py record PR --reviewer NAME     the reviewer's verdict: status `Gate review` = success on the PR's head
    release_ci.py gate --sha SHA                CI (release.yml): is the merge commit SHA releasable, and as which version?
    release_ci.py draft --version X --sha SHA   CI: keep the built files in a draft release vX (the resume point), or resume it
    release_ci.py match --version X             CI acceptance: PyPI's files of both packages equal the release's, by sha256
    release_ci.py follow                        the Mini (launchd, every 5 min): install the Latest release's wheels, switch

The verdict. The independent reviewer reads the pull request; the Gate review workflow then sets its check run `Gate
review`, or a login of GATE_REVIEW_CREATORS runs `release_ci.py record PR --reviewer NAME` (commit status `Gate review`,
description `APPROVE reviewer=NAME`, on the PR's head). A merge to main is released when its CI is green (release.yml
starts on the finished CI run of main), the merged pull request's final head carries a verdict, the merged tree equals
the reviewed head's tree, and the reviewer is none of the commit's authors (bin/promote_verdict.py's identity rule).
github-actions[bot] never sets a valid status; all agents share one token, so the allowlist cannot tell agents apart.
Only the merge being released needs its verdict: a release takes every commit since the last tag (a merge whose own run
was superseded is released with its successor). A refusal is red (exit 1, an `::error::` line); the one exception is a
merge already in a release (exit 0). The version is the commitizen bump from the commits since the last tag (a patch
bump when they carry no release note); steuermaschine and steuermaschine-heinzel get it in lockstep, the plugin pins
steuermaschine==X. Order: draft release with the files, core to PyPI, plugin to PyPI (skip-existing: a rerun resumes),
publish the release (this makes the tag), clean-container acceptance, then Latest.

The follower. It reads the Latest GitHub release (Latest only after acceptance), checks the verdict of that tag's merge
the same way, takes the PyPI wheels of both packages whose sha256 equal the release's assets, installs them into
$SM_STABLE/releases/vX (its own uv venv), migrates the user config, smokes, then switches with the release's
bin/promote_lib.sh (waits for running admission and release steps, moves state, points `current`) and restarts the
launchd loops that run the stable `sm`. Its own code is the clone ($SM_FOLLOW_CLONE, default ~/Code/steuermaschine-follow)
checked out at the tag. It never goes back. bin/rollback switches to the previous release. Exit 0: live or nothing to
do; 1: refused (the line says why); 75: wait.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import promote_verdict as pv

CONTEXT = "Gate review"
BOT = "github-actions[bot]"
WORKFLOW_REVIEWER = "gate-review-workflow"
GATE_WORKFLOW = ".github/workflows/gate-review.yml"
DESCRIPTION = re.compile(r"APPROVE reviewer=(\S.*)")
REPO = os.environ.get("SM_RELEASE_REPO", "helmut-hoffer-von-ankershoffen/steuermaschine")
LOOPS = re.compile(r"steuermaschine-stable|\.local/bin/sm<|/sm</string>")
Run = Callable[..., str]


class Refused(Exception):
    """Not releasable or not promotable; the message says why."""


class Wait(Exception):
    """bin/promote exited 75: wait and run the same command again."""


def sh(*cmd: str, cwd: Path | None = None, check: bool = True) -> str:
    done = subprocess.run(cmd, capture_output=True, check=False, text=True, cwd=cwd, timeout=900)
    if check and done.returncode != 0:
        raise Refused(f"{' '.join(cmd[:3])} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


def api(path: str, run: Run = sh) -> object:
    return json.loads(run("gh", "api", "-H", "Accept: application/vnd.github+json", path))


def creators(get: Callable[[str], object]) -> set[str]:
    """Logins allowed to set the `Gate review` status by hand: $SM_GATE_CREATORS (CI passes the repo variable), else read the
    repository variable GATE_REVIEW_CREATORS. Empty means none: fail closed. github-actions[bot] is never one."""
    raw = os.environ.get("SM_GATE_CREATORS")
    if raw is None:
        variable = get(f"repos/{REPO}/actions/variables/GATE_REVIEW_CREATORS")
        raw = str(variable.get("value", "")) if isinstance(variable, dict) else ""
    return {c.strip() for c in raw.split(",") if c.strip()} - {BOT}


def verdict_for(sha: str, api_get: Callable[[str], object] | None = None) -> tuple[int, str, str]:
    """(PR number, PR head, reviewer) of the merged pull request of commit SHA; Refused when there is no valid verdict.
    Two sources, the first that holds wins: the check run `Gate review` of the Gate review workflow (an app check run: only
    the workflow can make one, the model reviewer is `gate-review-workflow`), or the commit status `Gate review` set by hand
    (`record`) by a login of the allowlist. Limit: every agent shares the owner's token, so the allowlist cannot tell
    agents apart; independence rests on the reviewer name against the commit's authors (promote_verdict.independent)."""
    get = api_get or api
    pulls = get(f"repos/{REPO}/commits/{sha}/pulls")
    merged = [p for p in pulls if isinstance(p, dict) and p.get("merge_commit_sha") == sha and p.get("merged_at")] if isinstance(pulls, list) else []
    if not merged:
        raise Refused(f"{sha[:12]} was not merged from a pull request: no review verdict, no release")
    pull = merged[0]
    head = pull["head"]["sha"]
    number = int(pull["number"])
    runs = get(f"repos/{REPO}/commits/{head}/check-runs?per_page=100")
    mine = [r for r in (runs.get("check_runs", []) if isinstance(runs, dict) else [])
            if r.get("name") == CONTEXT and (r.get("app") or {}).get("slug") == "github-actions"]
    forged = ""
    if mine:
        latest_run = max(mine, key=lambda r: int(r.get("id", 0)))
        if latest_run.get("conclusion") == "success":
            # Any workflow of the PR's own files can name a job `Gate review`: only the base branch's gate-review.yml counts.
            suite = (latest_run.get("check_suite") or {}).get("id")
            found = get(f"repos/{REPO}/actions/runs?check_suite_id={suite}") if suite else {}
            runs_of = found.get("workflow_runs", []) if isinstance(found, dict) else []
            if any(w.get("path") == GATE_WORKFLOW and w.get("event") == "pull_request_target" and w.get("head_sha") == head
                   and w.get("conclusion") == "success" for w in runs_of):
                return number, head, WORKFLOW_REVIEWER
            forged = f" (a check run `{CONTEXT}` exists but does not come from {GATE_WORKFLOW} on pull_request_target for this head)"
    statuses = get(f"repos/{REPO}/commits/{head}/statuses?per_page=100")
    latest = next((s for s in statuses if s.get("context") == CONTEXT), None) if isinstance(statuses, list) else None  # newest first
    if latest is None or latest.get("state") != "success":
        raise Refused(f"PR #{number} head {head[:12]} has no `{CONTEXT}` success: the independent review is missing{forged}")
    login = (latest.get("creator") or {}).get("login", "")
    if login == BOT or login not in creators(get):
        raise Refused(f"the `{CONTEXT}` status of PR #{number} was set by {login or 'nobody'}, who is not in GATE_REVIEW_CREATORS")
    match = DESCRIPTION.fullmatch((latest.get("description") or "").strip())
    if not match:
        raise Refused(f"the `{CONTEXT}` of PR #{number} is not `APPROVE reviewer=NAME`: {(latest.get('description') or '')[:60]!r}")
    return number, head, match.group(1).strip()


def verify(repo: Path, sha: str, api_get: Callable[[str], object] | None = None, run: Run = sh) -> tuple[int, str, str]:
    """verdict_for plus: the merged tree is the reviewed head's tree, and the reviewer is none of the authors. Live, every time."""
    get = api_get or api
    number, head, reviewer = verdict_for(sha, get)
    reviewed = get(f"repos/{REPO}/git/commits/{head}")
    tree = reviewed["tree"]["sha"] if isinstance(reviewed, dict) and isinstance(reviewed.get("tree"), dict) else ""
    merged = run("git", "rev-parse", f"{sha}^{{tree}}", cwd=repo).strip()
    if not tree or tree != merged:
        raise Refused(f"{sha[:12]} is not what was reviewed: its tree {merged[:12]} differs from the head {head[:12]} of PR #{number} ({tree[:12] or 'unknown'})")
    independent(repo, sha, reviewer)
    return number, head, reviewer


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


PACKAGES = ("steuermaschine", "steuermaschine-heinzel")  # one version, from one commit, in this order


def pypi_files(name: str, version: str) -> list[dict[str, str]] | None:
    """The files of NAME VERSION on PyPI as {filename, sha256, url, kind}; None when PyPI has no such release (404).
    Any other answer refuses."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as resp:
            data = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise Refused(f"PyPI answered {exc.code} for {name} {version}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise Refused(f"PyPI cannot be read: {exc}") from None
    return [{"filename": str(u.get("filename", "")), "sha256": str((u.get("digests") or {}).get("sha256", "")),
             "url": str(u.get("url", "")), "kind": str(u.get("packagetype", ""))} for u in data.get("urls", []) if isinstance(u, dict)]


def pypi_has(version: str, name: str = "steuermaschine") -> bool:
    """Is NAME VERSION on PyPI? Any answer but 200 or 404 refuses."""
    return pypi_files(name, version) is not None


class AlreadyReleased(Refused):
    """The merge is in a release already: the one refusal that is not a failure."""


def gate(repo: Path, sha: str, api_get: Callable[[str], object] | None = None, run: Run = sh) -> dict[str, str]:
    """The outputs of the release gate; Refused names why SHA is not released. Only the merge being released needs its
    verdict: a release takes everything since the last tag (the merges before it whose own run was superseded or that
    predate the Gate review check are not re-reviewed; their review was the gate of their own PR)."""
    if run("git", "tag", "--list", "v*", "--contains", sha, cwd=repo).strip():
        raise AlreadyReleased(f"{sha[:12]} is already in a release")
    number, head, reviewer = verify(repo, sha, api_get, run)
    version = next_version(repo, run)
    return {"version": version, "reviewer": reviewer, "pr": str(number), "head": head}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def release_of(tag: str, get: Callable[[str], object]) -> dict[str, Any] | None:
    """The GitHub release named TAG, drafts included (the token must have push access to see drafts)."""
    found = get(f"repos/{REPO}/releases?per_page=100")
    return next((r for r in found if isinstance(r, dict) and r.get("tag_name") == tag), None) if isinstance(found, list) else None


def draft(version: str, sha: str, dist: Path, api_get: Callable[[str], object] | None = None, run: Run = sh,
          pypi: Callable[[str, str], list[dict[str, str]] | None] = pypi_files) -> str:
    """Build once, keep the files in a draft release before PyPI sees them: the draft is the resume point. A draft of
    this commit (an earlier run of the same merge) replaces DIST with its files, so PyPI and the GitHub release always
    carry the same bytes. A draft or a PyPI release of VERSION from another commit refuses: nothing is overwritten."""
    get = api_get or (lambda p: api(p, run))
    tag = f"v{version}"
    found = release_of(tag, get)
    if found is not None:
        target = str(found.get("target_commitish", ""))
        if not found.get("draft"):
            raise Refused(f"{tag} is published already (target {target[:12]}), but {sha[:12]} is in no release tag: the tag and the release disagree")
        if target != sha:
            raise Refused(f"{tag} is a draft of {target[:12]}, not of {sha[:12]}: finish that run first (re-run its failed jobs)")
        for old in dist.glob("*"):
            old.unlink()
        run("gh", "release", "download", tag, "--repo", REPO, "--dir", str(dist), "--clobber")
        return f"resume {tag}: {len(list(dist.glob('*')))} file(s) of the draft of {sha[:12]}"
    for name in PACKAGES:
        if pypi(name, version) is not None:
            raise Refused(f"{name} {version} is on PyPI but there is no release {tag}: nothing is overwritten (yank it or bump the version)")
    files = sorted(dist.glob("*"))
    names = {f.name for f in files}
    for name in PACKAGES:
        if not any(n.startswith(f"{name.replace('-', '_')}-{version}-") and n.endswith(".whl") for n in names):
            raise Refused(f"dist has no wheel of {name} {version}: {sorted(names)}")
    notes = "\n".join(f"{sha256(f)}  {f.name}" for f in files)
    run("gh", "release", "create", tag, *map(str, files), "--repo", REPO, "--draft", "--target", sha, "--title", tag,
        "--notes", f"sha256 of the files (the same as on PyPI):\n\n```\n{notes}\n```")
    return f"draft {tag} of {sha[:12]} with {len(files)} file(s)"


def asset_digests(tag: str, get: Callable[[str], object], run: Run = sh) -> dict[str, str]:
    """{filename: sha256} of the assets of the GitHub release TAG (GitHub's own `digest`; downloaded and hashed when absent)."""
    found = release_of(tag, get) or get(f"repos/{REPO}/releases/tags/{tag}")
    assets = found.get("assets", []) if isinstance(found, dict) else []
    out: dict[str, str] = {}
    for a in assets:
        digest = str(a.get("digest") or "")
        if digest.startswith("sha256:"):
            out[str(a["name"])] = digest.split(":", 1)[1]
        else:
            with tempfile.TemporaryDirectory() as tmp:
                run("gh", "release", "download", tag, "--repo", REPO, "--dir", tmp, "--pattern", str(a["name"]))
                out[str(a["name"])] = sha256(Path(tmp) / str(a["name"]))
    return out


def matching_wheels(version: str, get: Callable[[str], object], run: Run = sh,
                    pypi: Callable[[str, str], list[dict[str, str]] | None] = pypi_files) -> list[dict[str, str]]:
    """The PyPI wheels of both packages at VERSION, each with the sha256 of the same file on the GitHub release."""
    assets = asset_digests(f"v{version}", get, run)
    wheels: list[dict[str, str]] = []
    for name in PACKAGES:
        files = pypi(name, version)
        if not files:
            raise Refused(f"{name} {version} is not on PyPI")
        for f in files:
            if f["filename"] not in assets:
                raise Refused(f"{f['filename']} is on PyPI but not on the release v{version}")
            if assets[f["filename"]] != f["sha256"]:
                raise Refused(f"{f['filename']}: PyPI sha256 {f['sha256'][:12]} differs from the release asset's {assets[f['filename']][:12]}")
        wheel = [f for f in files if f["kind"] == "bdist_wheel"]
        if len(wheel) != 1:
            raise Refused(f"{name} {version} has {len(wheel)} wheels on PyPI, not one")
        wheels.append(wheel[0])
    return wheels


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


SWITCH = r"""
set -u
. "$LIB"
mark_switch_pending || echo "follow: (the switch-pending file could not be written)" >&2
trap clear_switch_pending EXIT
wait_idle 1800 15 >/dev/null || { echo "follow: an admission or release step still runs after 30 min" >&2; exit 75; }
live=""; [ -x "$STABLE/current/venv/bin/steuermaschine" ] && live="$(cd "$STABLE/current" && pwd -P)"
migrate_state_to "$REL/venv/bin/steuermaschine" "$REL/user-config.toml" "${live:+$live/venv/bin/steuermaschine}" "${live:+$live/user-config.toml}" >&2 || exit 1
install_launcher "$STABLE" || exit 1
point_current "$STABLE" "$ID" || exit 1
mkdir -p "$HOME/.local/bin" && ln -sfn "$STABLE/bin/sm" "$HOME/.local/bin/sm"
"""


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def live_release(stable: Path) -> dict[str, str]:
    """What runs now: {kind: none|wheel|commit, name, version (wheel) or commit (commit)}."""
    current = stable / "current"
    if not current.is_symlink() and not current.exists():
        return {"kind": "none", "name": ""}
    name = Path(os.readlink(current)).name if current.is_symlink() else current.resolve().name
    meta = current / "release.json"
    if meta.is_file():
        data = json.loads(meta.read_text(encoding="utf-8"))
        return {"kind": "wheel", "name": name, "version": str(data["version"]), "commit": str(data.get("commit", ""))}
    return {"kind": "commit", "name": name}


def smoke(rel: Path, run: Run = sh) -> None:
    """The installed pair answers from its own directory, with the heinzel plugin loaded under the migrated config."""
    exe = str(rel / "venv" / "bin" / "steuermaschine")
    env = {**os.environ, "STEUERMASCHINE_USER_CONFIG": str(rel / "user-config.toml")}
    env.pop("STEUERMASCHINE_PINNED", None)
    with tempfile.TemporaryDirectory() as empty:
        for args, want in ((["--help"], "Commands, in the order of a working day"), (["self", "config"], "plugin: heinzel (loaded)"),
                           (["self", "plugins"], "1. heinzel (base")):
            done = subprocess.run([exe, *args], capture_output=True, text=True, cwd=empty, env=env, check=False, timeout=300)
            if done.returncode != 0 or want not in done.stdout + done.stderr:
                raise Refused(f"smoke: sm {' '.join(args)} exited {done.returncode} without {want!r}: {(done.stdout + done.stderr).strip()[-200:]}")
    code = f"import sys, steuermaschine, steuermaschine_heinzel; sys.exit(any(not m.__file__.startswith({str(rel) + '/'!r}) for m in (steuermaschine, steuermaschine_heinzel)))"
    if subprocess.run([str(rel / "venv" / "bin" / "python"), "-c", code], check=False, timeout=120).returncode:
        raise Refused(f"smoke: the release in {rel} imports code from outside its own directory")


def download(url: str, path: Path) -> None:
    if not url.startswith("https://files.pythonhosted.org/"):
        raise Refused(f"{url[:80]} is not a PyPI file URL")
    urllib.request.urlretrieve(url, path)


def follow(stable: Path, clone: Path, home: Path, run: Run = sh, api_get: Callable[[str], object] | None = None,
           pypi: Callable[[str, str], list[dict[str, str]] | None] = pypi_files,
           fetch: Callable[[str, Path], None] | None = None) -> str:
    """Install the Latest release (marked Latest only after its clean-container acceptance) as a user installs it: the
    PyPI wheels of steuermaschine and steuermaschine-heinzel at that version, each equal by sha256 to the GitHub
    release's asset, into STABLE/releases/vX; smoke; switch between two steps (bin/promote_lib.sh of the release); then
    restart the loops. Never back or sideways. Returns a one-line outcome; Refused when nothing is switched."""
    get = api_get or (lambda p: api(p, run))
    latest = get(f"repos/{REPO}/releases/latest")
    tag = str(latest["tag_name"])  # type: ignore[index,call-overload]
    version = tag.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise Refused(f"the latest release {tag} is not vX.Y.Z")
    live = live_release(stable)
    if live["name"] == tag:
        return f"{tag} is live"
    if not clone.exists():
        run("gh", "repo", "clone", REPO, str(clone))
    run("git", "fetch", "--quiet", "--tags", "--force", "origin", cwd=clone)
    commit = run("git", "rev-parse", f"{tag}^{{commit}}", cwd=clone).strip()
    if live["kind"] == "wheel" and version_key(version) <= version_key(live["version"]):
        raise Refused(f"{tag} is not newer than the live v{live['version']}: not going back")
    if live["kind"] == "commit":
        try:
            base = pv.promoted_base(stable, clone)
        except pv.Refused as exc:
            raise Refused(str(exc)) from None
        if subprocess.run(["git", "-C", str(clone), "merge-base", "--is-ancestor", base, commit], check=False).returncode != 0:
            raise Refused(f"{tag} ({commit[:12]}) does not descend from the live {base[:12]}: not going back or sideways")
    number, _, reviewer = verify(clone, commit, get, run)  # the Gate review of the merge this release was cut from
    wheels = matching_wheels(version, get, run, pypi)
    run("git", "checkout", "--quiet", "--force", "--detach", commit, cwd=clone)  # the follower's own code and helpers: the release's
    rel = stable / "releases" / tag
    if not (rel / "release.json").is_file():
        if rel.exists():
            run("rm", "-rf", str(rel))
        (rel / "wheels").mkdir(parents=True)
        for w in wheels:
            target = rel / "wheels" / w["filename"]
            (fetch or download)(w["url"], target)
            if sha256(target) != w["sha256"]:
                raise Refused(f"{w['filename']} downloaded from PyPI does not have the sha256 PyPI and the release name")
        python = (clone / ".python-version").read_text(encoding="utf-8").strip()
        run("uv", "venv", "--quiet", "--managed-python", "--python", python, str(rel / "venv"))
        run("uv", "pip", "install", "--quiet", "--python", str(rel / "venv" / "bin" / "python"), *(str(rel / "wheels" / w["filename"]) for w in wheels))
        source = stable / "current" / "user-config.toml"
        if not source.is_file():
            source = home / ".config" / "steuermaschine" / "config.toml"
        run(str(rel / "venv" / "bin" / "python"), str(clone / "bin" / "migrate_config.py"), str(source),
            str(home / ".config" / "steuermaschine" / "migrate-values.toml"), str(rel / "user-config.toml"))
        smoke(rel, run)
        meta = {"tag": tag, "version": version, "commit": commit, "pr": number, "reviewer": reviewer,
                "wheels": {w["filename"]: w["sha256"] for w in wheels}}
        (rel / "release.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    done = subprocess.run(["bash", "-c", SWITCH], check=False, env={**os.environ, "LIB": str(clone / "bin" / "promote_lib.sh"), "STABLE": str(stable),
                                                                   "REL": str(rel), "ID": tag, "HOME": str(home)})
    if done.returncode == 75:
        raise Wait(f"{tag}: a write step still runs; the next run switches")
    if done.returncode != 0:
        raise Refused(f"the switch to {tag} failed with exit {done.returncode}; the lines above say why")
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (rel / "live-at").write_text(now + "\n", encoding="utf-8")  # the stage `Mini live` of the release's timings
    uid = os.getuid()
    labels = loops(home)
    for label in labels:
        run("launchctl", "kickstart", "-k", f"gui/{uid}/{label}", check=False)
    return f"{tag} ({commit[:12]}) is live from the PyPI wheels; restarted {len(labels)} loop(s)"


def outputs(lines: list[str]) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="release_ci.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="verb", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("pr", type=int)
    rec.add_argument("--reviewer", required=True)
    gt = sub.add_parser("gate")
    gt.add_argument("--sha", required=True)
    dr = sub.add_parser("draft")
    dr.add_argument("--version", required=True)
    dr.add_argument("--sha", required=True)
    dr.add_argument("--dist", default="dist")
    mt = sub.add_parser("match")
    mt.add_argument("--version", required=True)
    sub.add_parser("follow")
    args = parser.parse_args(argv)
    try:
        if args.verb == "record":
            print(f"release_ci: `{CONTEXT}` success on {record(args.pr, args.reviewer)[:12]} (PR #{args.pr}, reviewer {args.reviewer})")
        elif args.verb == "gate":
            out = gate(Path.cwd(), args.sha)
            lines = ["release=true", *(f"{k}={v}" for k, v in out.items())]
            outputs(lines)
            print("release_ci: releasing " + " ".join(lines[1:]))
        elif args.verb == "draft":
            print("release_ci: " + draft(args.version, args.sha, Path(args.dist)))
        elif args.verb == "match":
            wheels = matching_wheels(args.version, lambda p: api(p))
            print(f"release_ci: PyPI and the release v{args.version} carry the same files: " + ", ".join(w["filename"] for w in wheels))
        else:
            stable = Path(os.environ.get("SM_STABLE", Path.home() / "Code" / "steuermaschine-stable"))
            print("release_ci: " + follow(stable, Path(os.environ.get("SM_FOLLOW_CLONE", Path.home() / "Code" / "steuermaschine-follow")), Path.home()))
    except Wait as exc:
        print(f"release_ci: WAIT: {exc}", file=sys.stderr)
        return 75
    except AlreadyReleased as exc:
        outputs(["release=false"])
        print(f"release_ci: nothing to release: {exc}", file=sys.stderr)
        return 0  # the one refusal that is no failure: a later run released this merge with its successor
    except Refused as exc:
        if args.verb == "gate":
            outputs(["release=false"])
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=Not released::{exc}")
        print(f"release_ci: NOT {'released' if args.verb in ('gate', 'draft', 'match') else 'promoted'}: {exc}", file=sys.stderr)
        return 1  # a refusal is red: a gate never fails quietly
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
