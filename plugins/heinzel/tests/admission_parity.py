#!/usr/bin/env python3
"""Parity driver: run `prepare` of the former tool and of the native port on one recorded packet, offline.

usage: admission_parity.py --impl old|native|native-argv|native-apply --case DIR --objects CORE_GIT_REPO --work TMP [--old-tools DIR]

The case directory holds the real packet.json, REVIEW.md and prepare.out of one admission (tests/fixtures/admission/
recorded/<case>). The world the tool sees is rebuilt from the recorded transaction itself:

- Git: a `--shared --no-checkout` clone of CORE_GIT_REPO (it must still hold the recorded main and head commits),
  on a branch at the recorded head. The one network Git call (fetch of main into the disposable clone) is answered
  locally with the recorded main.
- GitHub: no call leaves this process. Variables = the recorded `expected` and `presence`; main = the recorded main;
  no open PR; the active runs are exactly the runs of the recorded nonconsumer receipts, queued, without jobs;
  the cancel probe answers the recorded 409 refusal. Any other command raises.
- HOME is TMP, so caches, locks and receipts never touch the real home.

Prints the prepared transaction (canonical JSON + newline) on stdout, like the former tool.
`native-apply` goes on: it applies that transaction with `ops.apply` against an in-memory registry (the queue pause is
replaced, the slot write lands in a dict) and prints the receipt it wrote under TMP. No variable is written anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from steuermaschine_heinzel import state as statedir

sys.dont_write_bytecode = True  # importing the former tool must not leave files in its directory


def build_world(case: Path, objects: Path, work: Path):
    recorded = json.loads((case / "prepare.out").read_text())
    packet = json.loads((case / "packet.json").read_text())
    repo, head = recorded["repository"], recorded["head"]
    core = work / "core"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    def git(*args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)
    git("clone", "--shared", "--no-checkout", str(objects), str(core))
    git("remote", "set-url", "origin", f"https://github.com/{repo}.git", cwd=core)
    git("update-ref", "refs/heads/parity", head, cwd=core)
    git("symbolic-ref", "HEAD", "refs/heads/parity", cwd=core)
    root = work / "review-root"
    review = root / packet["review"]["path"]
    review.parent.mkdir(parents=True)
    review.write_bytes((case / "REVIEW.md").read_bytes())
    packet_path = review.parent / "packet.json"
    packet_path.write_bytes((case / "packet.json").read_bytes())
    nonconsumers = work / "nonconsumers.json"
    nonconsumers.write_text(json.dumps({"receipts": recorded["nonconsumers"]}))
    return recorded, core, root, packet_path, nonconsumers


def install_fakes(s, recorded):
    """Replace the IO boundary of module `s` (former or moved focused_slots): GitHub, launchd, clock, network Git."""
    a = s.a
    repo, main = recorded["repository"], recorded["main"]
    owner, name = repo.split("/")
    runs = {}
    for receipt in recorded["nonconsumers"]:
        # the real zombies: never touched since creation (updated_at == created_at == run_started_at)
        created = receipt["identity"]["created_at"]
        runs[receipt["run"]] = dict(id=receipt["run"], status="queued", conclusion=None, repository={"full_name": repo},
                                    run_started_at=created, updated_at=created, referenced_workflows=[], **receipt["identity"])
    real_run = a._run

    def offline_run(argv, *, cwd=None, input=None):
        if argv[:2] == ["gh", "api"]:
            assert argv[2:] == [f"repos/{repo}/git/ref/heads/main", "--jq", ".object.sha"], argv
            return (main + "\n").encode()
        assert argv[0] == "git", argv
        if argv[1] == "fetch":
            assert argv[2:] == ["--no-tags", "origin", "refs/heads/main:refs/hz-admission/live-main"], argv
            return real_run(["git", "update-ref", "refs/hz-admission/live-main", main], cwd=cwd)
        assert argv[1] not in ("push", "pull"), argv
        return real_run(argv, cwd=cwd, input=input)

    a._run = offline_run

    class Offline(s.Runtime):
        posts: list = []
        writes: list = []
        values = {n: recorded["expected"][n] for n in recorded["presence"]}
        paused = False

        @contextmanager
        def exclusions(self):  # launchd and the queue locks are not part of the parity world
            Offline.paused, self.held_version_locks = True, s.version_locks(self.cache)
            try:
                yield
            finally:
                Offline.paused, self.held_version_locks = False, None

        def now(self):
            return 1_800_000_000.0  # long after every recorded run started

        def run(self, *args, data=None, check=True, timeout=30):
            def done(rc=0, out=b"", err=b""):
                return subprocess.CompletedProcess(args, rc, out, err)
            if args[0] == "launchctl":
                assert args[1] == "print", args
                if Offline.paused:
                    return done(113, b"", b"Could not find service\n")
                return done(out=f"state = running\npath = {self.plist}\n".encode())
            assert args[0] == "gh" and args[1] == "api", args
            if args[2] == "-X":
                assert args[3] == "POST" and args[4].endswith("/cancel") and int(args[4].split("/")[-2]) in runs, args
                Offline.posts.append(args[4])
                return done(1, b"", b"gh: Cannot cancel a workflow run that has not been queued yet (HTTP 409)\n")
            assert data is None, args
            path = args[2]
            prefix = f"repos/{repo}"
            if path == prefix:
                return done(out=json.dumps(dict(full_name=repo, name=name, owner=dict(login=owner, type="User"))).encode())
            if path == f"{prefix}/git/ref/heads/main":
                return done(out=json.dumps({"object": {"sha": main}}).encode())
            if path.startswith(f"{prefix}/actions/variables?"):
                rows = [dict(name=n, value=v) for n, v in Offline.values.items()]
                return done(out=json.dumps(dict(total_count=len(rows), variables=rows)).encode())
            if path.startswith(f"{prefix}/pulls?state=open"):
                return done(out=b"[]")
            if path.startswith(f"{prefix}/actions/runs?status="):
                state = path.split("status=")[1].split("&")[0]
                rows = list(runs.values()) if state == "queued" else []
                assert len(rows) < 100
                return done(out=json.dumps(dict(total_count=len(rows), workflow_runs=rows)).encode())
            if path.startswith(f"{prefix}/actions/runs/") and path.endswith("/jobs?per_page=100"):
                return done(out=json.dumps(dict(total_count=0, jobs=[])).encode())
            if path.startswith(f"{prefix}/actions/runs/"):
                return done(out=json.dumps(runs[int(path.rsplit("/", 1)[1])]).encode())
            raise AssertionError(f"unexpected GitHub read: {path}")

        def set_variable(self, name, value):  # the in-memory registry; nothing leaves the process
            s.require(name in s.NAMES[1:], "only one compressed slot may be appended")
            Offline.writes.append((name, value))
            Offline.values[name] = value

    s.Runtime = Offline
    return Offline


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--impl", required=True, choices=("old", "native", "native-argv", "native-apply"))
    p.add_argument("--case", type=Path, required=True)
    p.add_argument("--objects", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--old-tools", type=Path, default=Path.home() / "Code/heinzel-management/tools")
    args = p.parse_args(argv)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(work)
    for name in ("HZ_QUEUE_STATE", "HZ_TRAIN_STATE_DIR", "HZ_RELEASE_DIR"):
        os.environ.pop(name, None)
    recorded, core, root, packet, nonconsumers = build_world(args.case.resolve(), args.objects.resolve(), work)
    # the recorded transaction says how it was prepared: pre-open (no PR), with or without the protected-producer approval
    assert recorded["pr"] is None and "pr_update" not in recorded and "resulting_main" not in recorded, "only pre-open single packets are replayed"
    protected = recorded["protected_producer"]
    argv_form = ["prepare", str(packet), "--core-repo", str(core), "--review-root", str(root), "--nonconsumers", str(nonconsumers)]
    argv_form += ["--reviewed-protected-producer"] if protected else []
    if args.impl == "old":
        sys.path.insert(0, str(args.old_tools))
        import hz_focused_slots as s
        fake = install_fakes(s, recorded)
        code = s.main(argv_form)
    else:
        from steuermaschine_heinzel.native.admission_lib import focused_slots as s
        from steuermaschine_heinzel.native.admission_lib import ops
        fake = install_fakes(s, recorded)
        if args.impl == "native-argv":
            code = s.main(argv_form)
        else:
            result = ops.prepare(packet, core, root, nonconsumers=nonconsumers, protected=protected)
            if args.impl == "native":
                sys.stdout.write(Path(result.prepared_path).read_text())
            stored = json.loads(Path(result.args_path).read_text())
            assert stored["sha256"] == result.sha256 and stored["packet"] == str(packet.resolve()), stored
            code = 0
            if args.impl == "native-apply":
                applied = ops.apply(packet, result.sha256, runtime=fake(core))
                assert fake.writes == [(recorded["slot"], recorded["desired"][recorded["slot"]])], "the one write is the reviewed slot value"
                assert Path(applied.receipt_path).parent == statedir.root() / "focused-admission-receipts"
                sys.stdout.write(Path(applied.receipt_path).read_text())
    stuck = len(recorded["nonconsumers"])
    # former tool: one probe per receipt in prepare. Native prepare (both forms) proves receipts from the list rows: none.
    # Native apply: only the full proof directly before the write probes; its re-prepare and the other guards do not.
    expected_posts = {"old": stuck, "native-argv": 0, "native": 0, "native-apply": stuck}[args.impl]
    assert code != 0 or len(fake.posts) == expected_posts, (len(fake.posts), expected_posts)
    print(json.dumps({"parity": args.impl, "cancel_probes_answered_offline": len(fake.posts)}), file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
