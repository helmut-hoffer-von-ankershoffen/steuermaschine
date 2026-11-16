import argparse, json, os, re, subprocess
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
    r = s.add_parser("post-publish", help="print the release-post-publish checklist"); r.add_argument("version"); r.set_defaults(f=cmd_post)
    a = p.parse_args(argv); return a.f(a) or 0

