#!/usr/bin/env python3
"""Parity of packet building against packets the former tools built and admitted (fixtures/packet/recorded).

Recorded 2026-11-12 by copying the packet directories of the former builder unchanged (inputs sel.json and the
collected JUnit files; outputs packet.json, REVIEW.md, wave.json, the manifest and verify reports).

usage: packet_parity.py --core GIT        (or PACKET_PARITY_CORE=GIT; there is no default path)
  --core  a Git repository that still holds the recorded commits (on the build machine of 2026-11-12: the scratch
          clone named in the recorded wave.json of the predict directory). It is only read: every step that writes
          Git objects runs in a `git clone --shared` of it under a temporary directory.
Prints one line per check and exits 1 when one differs. The pytest module test_native_packet_parity.py runs the
same checks when PACKET_PARITY_CORE names such a repository and skips otherwise (it is not part of the promote gate).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from steuermaschine_heinzel.native.packet_lib import admit_wave, ops, wave_ops

RECORDED = Path(__file__).resolve().parent / "fixtures/packet/recorded"
DEFAULT_CORE = Path(os.environ["PACKET_PARITY_CORE"]) if os.environ.get("PACKET_PARITY_CORE") else None
PREOPEN = ("e2e-details/m-437bb247", "e2e-details-2/m-437bb247")
PREDICT = "e2e-daemon-race/predict"
BOUND = "e2e-daemon-race/predict/bound-f19a3782232c"
ALL = (*PREOPEN, PREDICT, BOUND)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(name: str) -> dict:
    return json.loads((RECORDED / name / "wave.json").read_text())


def available(core: Path | None = DEFAULT_CORE) -> bool:
    """Whether `core` holds every recorded commit."""
    if core is None or not core.exists():
        return False
    commits = {record(n)[k] for n in ALL for k in ("head", "main")}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return all(subprocess.run(["git", "-C", str(core), "cat-file", "-e", f"{c}^{{commit}}"], capture_output=True, env=env).returncode == 0 for c in commits)


def shared_clone(core: Path, dest: Path) -> Path:
    """A clone that borrows the objects of `core` and writes only into itself."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "clone", "-q", "--shared", "--bare", str(core), str(dest)], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(dest), "fetch", "-q", str(core), "+refs/*:refs/*"], check=True, capture_output=True, env=env)
    return dest


def junit_args(directory: Path, lanes: list[str]) -> list[str]:
    return [f"{lane}={directory / f'{lane}.main-collected.xml'}" for lane in lanes]


def verify_recorded(core: Path, name: str) -> list[str]:
    """`head manifest --verify` on the recorded packet, with the recorded cases and review root: byte-equal, the recorded sha256."""
    rec = record(name)
    result = ops.verify_packet(core, RECORDED / name / "packet.json", junit=junit_args(RECORDED / name, rec["lanes"]), review_root=RECORDED)
    problems = []
    if result.status != "byte_equal":
        problems.append(f"verify says {result.status}: {result.differences}")
    if result.packet_sha256 != rec["packet_sha256"]:
        problems.append(f"sha256 {result.packet_sha256} != recorded {rec['packet_sha256']}")
    if result.report() != (RECORDED / name / "verify.err").read_text():
        problems.append("the verify report differs from the recorded one")
    return problems


def rebuild_recorded(core: Path, name: str, scratch: Path) -> list[str]:
    """`head manifest` from the recorded inputs (selection, collected cases, review file): the recorded bytes."""
    rec, source = record(name), RECORDED / name
    old = json.loads((source / "packet.json").read_text())["manifest"]
    extra = sorted(set(old["production"]) - set(old["changed"]))
    out = scratch / name.replace("/", "__") / "packet.json"
    out.parent.mkdir(parents=True)
    result = ops.build_packet(core, rec["head"], rec["main"], out, selection=source / "sel.json", junit=junit_args(source, rec["lanes"]),
                              production=extra, review=rec["review"], review_root=RECORDED)
    problems = []
    if out.read_bytes() != (source / "packet.json").read_bytes():
        problems.append("rebuilt packet.json differs from the recorded bytes")
    if result.packet_sha256 != rec["packet_sha256"] or result.manifest_sha256 != rec["manifest_sha256"]:
        problems.append(f"sha256 {result.packet_sha256} != recorded {rec['packet_sha256']}")
    recorded_report = (source / "manifest.final.err").read_text().replace(f"{rec['review_root']}/{name}/packet.json", str(out))
    if result.report() != recorded_report:
        problems.append("the build report differs from the recorded one (after replacing the output path)")
    return problems


# ----------------------------------------------------------------------------- the packet directory builders
NOTES = "## Notes for the independent reviewer\n"
VOLATILE = {"collector", "collector_sha256", "core_repo", "python", "python_version", "review_root", "worktree", "packet_sha256", "predicted_dir",
            "predicted_packet_sha256"}


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def replay(source: Path):
    """Stands in for one collection run: the recorded collected cases of the asked lane and tree."""
    def run(cmd: list[str]) -> SimpleNamespace:
        out = Path(cmd[cmd.index("-o") + 1])
        shutil.copyfile(source / out.name, out)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


class recorded_builder:
    """The builder label of the recorded REVIEW.md title, for the duration of one rebuild (the native label differs on purpose)."""

    def __init__(self, review: Path):
        title = review.read_text().splitlines()[0]
        self.label = title.split("(builder-prepared by ", 1)[1].split(";", 1)[0]

    def __enter__(self):
        self.before, admit_wave.BUILDER = admit_wave.BUILDER, self.label

    def __exit__(self, *exc):
        admit_wave.BUILDER = self.before


def stage(core: Path, scratch: Path, name: str) -> dict:
    """A scratch clone with the recorded branch, a clean checkout at the recorded head and an empty review root."""
    rec, source = record(name), RECORDED / name
    clone = scratch / "core.git"
    if not clone.exists():
        shared_clone(core, clone)
    git(clone, "update-ref", f"refs/heads/{rec['branch']}", rec["head"])
    worktree = scratch / f"wt-{rec['head'][:8]}"
    if not worktree.exists():
        git(clone, "worktree", "add", "-q", "--detach", str(worktree), rec["head"])
    root = (scratch / "root").resolve()
    root.mkdir(exist_ok=True)
    review = (source / "REVIEW.md").read_text()
    members = next((line.removeprefix("Members: ") for line in review.splitlines() if line.startswith("Members: ")), None)
    notes = None
    if NOTES in review:
        notes = scratch / f"notes-{name.replace('/', '__')}.md"
        notes.write_text(review.split(NOTES, 1)[1].split("\n## Bind to real main", 1)[0])
    old = json.loads((source / "packet.json").read_text())["manifest"]
    return dict(rec=rec, source=source, clone=clone, worktree=worktree, root=root, members=members, notes=notes,
                production=sorted(set(old["production"]) - set(old["changed"])))


def compare_directory(out: Path, source: Path, root: Path, rec: dict, names: list[str]) -> list[str]:
    """The built directory against the recorded one. The absolute review root is the one thing that may differ: it is
    quoted in the reports and in REVIEW.md, so the packet's review digest follows it."""
    built, recorded = json.loads((out / "packet.json").read_text()), json.loads((source / "packet.json").read_text())

    def text(path: Path) -> str:
        return path.read_text().replace(str(root), rec["review_root"])

    def report(path: Path) -> str:  # the final reports also quote the two digests that follow from the root
        return text(path).replace(built["review"]["sha256"], recorded["review"]["sha256"]).replace(sha(out / "packet.json"), rec["packet_sha256"])

    problems = [f"{n} differs from the recorded file" for n in names
                if (report if n in ("manifest.final.err", "verify.err") else text)(out / n) != (source / n).read_text()]
    if built["review"]["sha256"] != sha(out / "REVIEW.md"):
        problems.append("the packet does not bind the REVIEW.md beside it")
    if hashlib.sha256(text(out / "REVIEW.md").encode()).hexdigest() != recorded["review"]["sha256"]:
        problems.append("REVIEW.md (review root replaced) is not the recorded review")
    rebound = (out / "packet.json").read_bytes().replace(built["review"]["sha256"].encode(), recorded["review"]["sha256"].encode())
    if rebound != (source / "packet.json").read_bytes() or hashlib.sha256(rebound).hexdigest() != rec["packet_sha256"]:
        problems.append("packet.json differs from the recorded bytes in more than the review digest")
    new = json.loads((out / "wave.json").read_text())
    drift = sorted(k for k in set(new) | set(rec) if k not in VOLATILE and k != "after" and new.get(k) != rec.get(k))
    if drift:
        problems.append(f"wave.json differs in {drift}")
    return problems


def preopen_recorded(core: Path, name: str, scratch: Path, collect=None) -> list[str]:
    """`head packet preopen` from the recorded inputs, the collection replayed from the recorded collected cases."""
    s = stage(core, scratch, name)
    rec, source, out = s["rec"], s["source"], s["root"] / name
    with recorded_builder(source / "REVIEW.md"):
        built = wave_ops.preopen(rec["branch"], rec["main"], core_repo=s["clone"], worktree=s["worktree"], out=out, review_root=s["root"],
                                 selection=source / "sel.json", reviewed_packet=None, junit=[f"{lane}={source / f'{lane}.pr-collected.xml'}" for lane in rec["lanes"]],
                                 production=s["production"], members=s["members"], notes=s["notes"], python=None, node_modules=None, nonconsumers=None,
                                 collector=None, run_collect=collect or replay(source))
    names = ["sel.json", "collected.txt", "manifest.err", "manifest.final.err", "verify.err", "REVIEW.md",
             *(f"{lane}.{part}" for lane in rec["lanes"] for part in ("case-diff.txt", "pr-collected.xml", "main-collected.xml"))]
    problems = compare_directory(out, source, s["root"], rec, names)
    expected = f"sm head prepare {out / 'packet.json'} --core-repo {s['worktree'].resolve()} --review-root {s['root']}"
    if built.prepare_command != expected:
        problems.append(f"prepare command {built.prepare_command!r} != {expected!r}")
    return problems


def predict_and_bind_recorded(core: Path, scratch: Path) -> list[str]:
    """`head packet predict` on the recorded later head after the recorded earlier packet, then `head packet bind` to
    the recorded real main: the predicted commit, both directories and both packets against the recorded ones."""
    s, bound_rec = stage(core, scratch, PREDICT), record(BOUND)
    rec, source, out = s["rec"], s["source"], s["root"] / PREDICT
    earlier = rec["after"][0]
    name = str(Path(earlier["dir"]).relative_to(rec["review_root"]))
    problems = [] if (s["root"] / name).exists() else [f"earlier packet: {p}" for p in preopen_recorded(core, name, scratch)]
    with recorded_builder(source / "REVIEW.md"):
        built = wave_ops.predict(rec["branch"], [s["root"] / name], live_core=None, core_repo=s["clone"], worktree=s["worktree"], out=out,
                                 review_root=s["root"], selection=source / "sel.json", reviewed_packet=None,
                                 junit=[f"{lane}={source / f'{lane}.pr-collected.xml'}" for lane in rec["lanes"]], production=s["production"],
                                 members=s["members"], notes=s["notes"], python=None, node_modules=None, nonconsumers=None, collector=None,
                                 run_collect=replay(source))
    if (built.predicted_main, built.main, built.parent_main) != (rec["predicted_main"], rec["main"], rec["parent_main"]) or built.admissible:
        problems.append(f"predicted main {built.predicted_main} != recorded {rec['predicted_main']}")
    after = json.loads((out / "wave.json").read_text())["after"][0]
    if {k: v for k, v in after.items() if k != "dir"} != {k: v for k, v in earlier.items() if k != "dir"}:
        problems.append("the predicted chain differs from the recorded one")
    names = ["sel.json", "collected.txt", "manifest.err", "manifest.final.err", "verify.err", "REVIEW.md",
             *(f"{lane}.{part}" for lane in rec["lanes"] for part in ("case-diff.txt", "pr-collected.xml", "main-collected.xml"))]
    problems += [f"predict: {p}" for p in compare_directory(out, source, s["root"], rec, names)]
    # bind: the recorded real main; the collector recorded by predict must be unchanged, which it is (this run's own)
    bound = wave_ops.bind(out, bound_rec["main"])
    target = out / f"bound-{bound_rec['main'][:12]}"
    names = ["sel.json", "manifest.err", "manifest.final.err", "verify.err", *(f"{lane}.main-collected.xml" for lane in rec["lanes"])]
    problems += [f"bound: {p}" for p in compare_bound(target, RECORDED / BOUND, s["root"], bound_rec, names, out, source)]
    expected = f"sm head prepare {target / 'packet.json'} --core-repo {s['worktree'].resolve()} --review-root {s['root']}"
    if bound.prepare_command != expected or bound.kind != "bound" or not bound.admissible:
        problems.append(f"bound prepare command {bound.prepare_command!r} != {expected!r}")
    return problems


def compare_bound(out: Path, source: Path, root: Path, rec: dict, names: list[str], predict_out: Path, predict_source: Path) -> list[str]:
    """As compare_directory; a bound REVIEW.md also quotes the predicted packet's digest, which follows the root too."""
    built_predict, recorded_predict = sha(predict_out / "packet.json"), sha(predict_source / "packet.json")
    review = out / "REVIEW.md"
    original = review.read_text()
    normal = original.replace(str(root), rec["review_root"]).replace(built_predict, recorded_predict)
    predicted_review = json.loads((predict_out / "packet.json").read_text())["review"]["sha256"]
    normal = normal.replace(predicted_review, json.loads((predict_source / "packet.json").read_text())["review"]["sha256"])
    problems = []
    if normal != (source / "REVIEW.md").read_text():
        problems.append("REVIEW.md (review root and the digests that follow it replaced) is not the recorded review")
    built, recorded = json.loads((out / "packet.json").read_text()), json.loads((source / "packet.json").read_text())
    rebound = (out / "packet.json").read_bytes().replace(built["review"]["sha256"].encode(), recorded["review"]["sha256"].encode())
    if built["review"]["sha256"] != sha(review) or rebound != (source / "packet.json").read_bytes() or hashlib.sha256(rebound).hexdigest() != rec["packet_sha256"]:
        problems.append("packet.json differs from the recorded bytes in more than the review digest")

    def report(path: Path) -> str:
        return (path.read_text().replace(str(root), rec["review_root"]).replace(built["review"]["sha256"], recorded["review"]["sha256"])
                .replace(sha(out / "packet.json"), rec["packet_sha256"]))

    problems += [f"{n} differs from the recorded file" for n in names if report(out / n) != (source / n).read_text()]
    new = json.loads((out / "wave.json").read_text())
    drift = sorted(k for k in set(new) | set(rec) if k not in VOLATILE and new.get(k) != rec.get(k))
    return problems + ([f"wave.json differs in {drift}"] if drift else [])


CHECKS = [(f"verify {n}", lambda core, scratch, n=n: verify_recorded(core, n)) for n in ALL] + \
         [(f"rebuild {n}", lambda core, scratch, n=n: rebuild_recorded(core, n, scratch)) for n in ALL] + \
         [(f"preopen {n}", lambda core, scratch, n=n: preopen_recorded(core, n, scratch)) for n in PREOPEN] + \
         [("predict and bind e2e-daemon-race", lambda core, scratch: predict_and_bind_recorded(core, scratch))]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--core", type=Path, default=DEFAULT_CORE)
    args = parser.parse_args(argv)
    if not available(args.core):
        print(f"UNAVAILABLE: give --core GIT, a repository that holds the recorded commits (got {args.core})")
        return 2
    failed = 0
    scratch = Path(tempfile.mkdtemp(prefix="packet-parity-"))
    try:
        for label, check in CHECKS:
            problems = check(args.core, scratch)
            failed += bool(problems)
            print(f"{'DIFFERENT' if problems else 'IDENTICAL'} {label}" + "".join(f"\n    {p}" for p in problems))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
