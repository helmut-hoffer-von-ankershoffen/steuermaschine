"""The merge stamp on a bead: a note line and a label, written with the bead tracker's own command."""

import argparse, json, os, re, subprocess, sys, time
from . import edge  # moved: records, processes and what used to be separate scripts
from steuermaschine.config import child_environment, search_path

CORE = "helmut-hoffer-von-ankershoffen/heinzel"
ROOT = str(edge.records_dir())  # moved: the checkout with the bead database, from the plugin's settings
PREFIX = "heinzel-management-"
ENV = child_environment(PATH=os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:" + search_path())


def run(*a, cwd=ROOT):
    r = edge.run(list(a), capture_output=True, text=True, cwd=cwd, env=ENV, check=False)
    return r.returncode, r.stdout.strip()


def bid(x): return x if x.startswith(PREFIX) else PREFIX + x


def log(event, **f):
    print(json.dumps({"event": event, **f}, sort_keys=True), file=sys.stderr)


def merged_line(pr, sha, at): return f"merged_in: PR #{pr} sha={sha[:8]} at={at}"
def released_line(version, at): return f"released_in: {version} released_at={at}"


def pr_facts(pr, repo):
    code, out = run("gh", "pr", "view", str(pr), "-R", repo, "--json", "state,mergeCommit,mergedAt")
    try:
        d = json.loads(out) if code == 0 else {}
    except ValueError:
        d = {}
    if d.get("state") != "MERGED":
        return None
    return d["mergeCommit"]["oid"], d.get("mergedAt", "")


def release_facts(tag, repo=CORE):
    """publishedAt of a non-draft release, else None."""
    code, out = run("gh", "release", "view", tag, "-R", repo, "--json", "isDraft,publishedAt")
    try:
        d = json.loads(out) if code == 0 else {}
    except ValueError:
        return None
    return None if d.get("isDraft") or not d.get("publishedAt") else d["publishedAt"]


def stamp(bead, line, label, dry=False):
    log("stamp.start", bead=bead, label=label)
    if dry:
        print(f"would stamp {bead}: {line}")
        return 0
    c1, _ = run("bd", "update", bid(bead), "--append-notes", f"{time.strftime('%H:%M')} {line}")
    c2, _ = run("bd", "label", "add", bid(bead), label)
    log("stamp.outcome", bead=bead, label=label, outcome="ok" if c1 == c2 == 0 else "failed")
    print(f"{bead}: {line}")
    return 0 if c1 == c2 == 0 else 1


def has_label(bead, label):
    code, out = run("bd", "show", bid(bead), "--json")
    try:
        d = json.loads(out)
        d = d[0] if isinstance(d, list) else d
    except (ValueError, IndexError):
        return False
    return label in (d.get("labels") or [])


def do_stamp(bead, pr=None, released=None, repo=CORE, dry=False):
    rc = 0
    if pr:
        facts = pr_facts(pr, repo)
        if not facts:
            print(f"refused: PR #{pr} in {repo} is not merged", file=sys.stderr)
            return 1
        if not has_label(bead, f"merged-{pr}"):
            rc |= stamp(bead, merged_line(pr, *facts), f"merged-{pr}", dry)
    if released:
        tag = released if released.startswith("v") else "v" + released
        at = release_facts(tag)
        if not at:
            print(f"refused: release {tag} is not published", file=sys.stderr)
            return 1
        if not has_label(bead, f"released-{tag}"):
            rc |= stamp(bead, released_line(tag, at), f"released-{tag}", dry)
    return rc
