"""Hand chores scripted: overdue-triage, pin-diagnose, post-publish (see `hz-chores --help`)."""
import argparse, hashlib, json, os, re, subprocess, sys
from . import edge  # moved: records, processes and what used to be separate scripts
CORE = "helmut-hoffer-von-ankershoffen/heinzel"
CORE_DIR = edge.core_checkout()  # moved: the read-only core checkout, from the plugin's settings
PR_RE = re.compile(r"(?:#|PR\s*|pull/)(\d{3,4})\b")


def pr_refs(text):
    return sorted({int(m) for m in PR_RE.findall(text or "")})


def triage(beads, shows, merged):
    """beads: overdue list; shows: id -> text; merged: set of merged PR numbers -> [(id, title, [prs])] closable."""
    out = []
    for b in beads:
        hit = [n for n in pr_refs(shows.get(b["id"], "")) if n in merged]
        if hit:
            out.append((b["id"], b["title"][:70], hit))
    return out


def approved_sources(ref):
    """The pins of Core's reviewed release policy at REF (scripts/release_policy.json there; never a local copy)."""
    from ..queue_lib import core_policy
    commit = sh("git", "-C", CORE_DIR, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    raw = edge.run(["git", "-C", CORE_DIR, "show", f"{commit}:{core_policy.PATH}"], capture_output=True).stdout if commit else b""
    return core_policy.parse(raw, commit).recorded_sources


def sha(data):
    return hashlib.sha256(data).hexdigest()


def find_approved_blob(history, want):
    """history: [(commit, bytes)] newest first; returns the commit whose content hashes to want, else None."""
    return next((c for c, data in history if sha(data) == want), None)


def skeleton(name, old, new, want, got):
    return (f"Review request: pin update for {name}\n  approved {want[:12]} (commit {old})\n  executed {got[:12]} (ref {new})\n"
            f"  Q1: does the diff above weaken a check or widen what the candidate trusts? yes/no with reasons\n"
            f"  Q2: is every changed line needed for the release? Then regenerate scripts/release_policy.json in Core")


def sh(*a):
    return edge.run(a, capture_output=True, text=True).stdout


def cmd_triage(a):
    beads = json.loads(sh("bd", "list", "--overdue", "--json"))
    shows = {b["id"]: sh("bd", "show", b["id"]) for b in beads}
    nums = sorted({n for t in shows.values() for n in pr_refs(t)})
    merged = {n for n in nums if sh("gh", "pr", "view", str(n), "-R", CORE, "--json", "state", "-q", ".state").strip() == "MERGED"}
    rows = triage(beads, shows, merged)
    print(f"{len(beads)} overdue, {len(rows)} reference a merged PR (verify before closing):")
    for i, t, prs in rows:
        print(f"  {i}  {t}  merged: {','.join('#%d' % n for n in prs)}  -> bd close {i} --reason 'merged #{prs[-1]}'")


def cmd_pin(a):
    from ..queue_lib.release_candidate import CandidateError
    try:
        pins = approved_sources(a.ref)
    except CandidateError as exc:
        print(f"no reviewed release policy at {a.ref}: {exc}", file=sys.stderr)
        return 1
    for name, want in pins.items():
        print(f"pin  {name} {want}")
        got_b = edge.run(["git", "-C", CORE_DIR, "show", f"{a.ref}:{name}"], capture_output=True).stdout
        if sha(got_b) == want:
            print(f"ok   {name}"); continue
        revs = sh("git", "-C", CORE_DIR, "log", "--format=%H", "-n", "60", a.ref, "--", name).split()
        hist = [(c, edge.run(["git", "-C", CORE_DIR, "show", f"{c}:{name}"], capture_output=True).stdout) for c in revs]
        old = find_approved_blob(hist, want)
        print(f"DIFF {name}: executed {sha(got_b)[:12]} != approved {want[:12]}")
        if old:
            print(sh("git", "-C", CORE_DIR, "diff", old, a.ref, "--", name)[:6000])
        else:
            print("  approved blob not found in the last 60 commits of this file")
        print(skeleton(name, old or "unknown", a.ref, want, sha(got_b)))


def cmd_post(a):
    v = a.version
    print(f"Post-publish checklist for v{v} (run in order):\n  1. sm release train resume-ci --dry-run ; then without --dry-run\n"
          f"  2. when main CI runs: sm ci rerun\n  3. sm release verify --mini   (expect latest={v} cli=daemon=web={v})\n"
          f"  4. close beads: sm beads stamp-released --version {v} ; then the evidence table: sm beads sweep\n"
          f"  5. PushNotification: 'Heinzelmaennchen {v} is live on the Mini (cli/web/daemon {v})' after step 3 shows mismatch=false")


def main(argv=None):
    p = argparse.ArgumentParser(prog="sm release after", description=__doc__)
    s = p.add_subparsers(dest="c", required=True)
    s.add_parser("overdue-triage", help="list overdue beads whose referenced PRs are merged").set_defaults(f=cmd_triage)
    q = s.add_parser("pin-diagnose", help="compare the pinned files at REF with the digests of Core's release policy at REF, print diff and review skeleton")
    q.add_argument("ref", nargs="?", default="origin/main"); q.set_defaults(f=cmd_pin)
    r = s.add_parser("post-publish", help="print the release-post-publish checklist"); r.add_argument("version"); r.set_defaults(f=cmd_post)
    a = p.parse_args(argv); return a.f(a) or 0

