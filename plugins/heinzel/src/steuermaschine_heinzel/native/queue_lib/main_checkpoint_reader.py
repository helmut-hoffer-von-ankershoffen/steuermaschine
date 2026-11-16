"""Execute unchanged protected Core evidence readers from authenticated main.

The historical adapter replaces only verify_main's current-tip assertion with
explicit ancestry to the independently observed current main. It never changes
an API response or a protected reader function.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types

REPOSITORY = "helmut-hoffer-von-ankershoffen/heinzel"
MAX_INPUT = 8 * 1024 * 1024


def command(args, *, cwd=None, limit=16 * 1024 * 1024):
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            args,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
        )
        if result.returncode or output.tell() > limit:
            raise ValueError("protected evidence command failed or exceeded its bound")
        output.seek(0)
        return output.read(limit + 1)


def api(path):
    return json.loads(command(["gh", "api", f"repos/{REPOSITORY}/{path}"]))


def read(path):
    flags = ["--allow-escape-sequences"] if path.endswith("/logs") else []
    return command(["gh", "api", *flags, f"repos/{REPOSITORY}/{path}"], limit=5 * 1024 * 1024)


def verified_segment(request, fp, fg):
    """Use original source/config/workload/artifact checks on one real main interval."""
    head, base, reader = request["head"], request["base"], request["reader_head"]
    fp.require(
        all(fp.valid_sha(value) for value in (head, base, reader)),
        "invalid focused interval commits",
    )
    fp.require(
        api("git/ref/heads/main")["object"]["sha"] == reader, "protected main advanced"
    )
    fg.git("merge-base", "--is-ancestor", head, reader)
    fp.require(
        fg.git("merge-base", base, head).decode().strip() == base,
        "focused interval base is not an ancestor",
    )

    class EvidenceGate(fg.Gate):
        def github(self, path):
            return api(path)

    gate = EvidenceGate(REPOSITORY, "", head)
    run_id = request["run"]
    run = fg._authenticated_run(gate, run_id)
    fp.require(
        run["run_attempt"] == 1
        and run["event"] == "push"
        and run["head_branch"] == "main"
        and run["head_repository"]["full_name"] == REPOSITORY,
        "not original actual-main evidence",
    )
    registry = request["registry"]
    tree = fp.object_id(f"{head}^{{tree}}")
    key = f"push:{head}" if f"push:{head}" in registry else f"push-tree:{tree}"
    fp.require(key in registry, "missing exact main manifest")
    selected = json.dumps(
        {key: registry[key]}, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    source = fp.WorkloadSource(
        "push", REPOSITORY, "refs/heads/main", {}, base, head, fp.raw_diff(base, head)
    )
    scope = fp.resolve(source, head, selected)
    fp.require(
        isinstance(scope, fp.Scope), "candidate cannot substitute for focused interval"
    )
    fg._verify_config(scope)
    fg._verify_workload(gate, fg.GateSource(run, source, head), scope, read)
    fp.require(
        api("git/ref/heads/main")["object"]["sha"] == reader
        and fg._authenticated_run(gate, run_id) == run,
        "main or historical workflow changed",
    )
    return {
        "type": "focused-main/v1",
        "base": base,
        "head": head,
        "tree": scope.tree,
        "run": run_id,
        "attempt": 1,
        "manifest_sha256": scope.manifest_digest,
    }


def verified_full_pr(request, fp, fg):
    """Consume protected Core's complete ordinary-PR proof, never a synthetic focused manifest."""
    if not callable(getattr(fg, "full_pr_jobs", None)):
        raise ValueError("protected Core does not yet support full PR evidence")
    head, base, tree = request["head"], request["reader_head"], request["tree"]
    fp.require(all(fp.valid_sha(value) for value in (head, base, tree)), "invalid full PR identity")
    # The legacy coverage reader reads the protected workflow from its working directory.
    workflow = Path(fg.WORKFLOW)
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_bytes(fg.git("show", f"{base}:{fg.WORKFLOW}"))

    class EvidenceGate(fg.Gate):
        def github(self, path):
            return api(path)

    gate = EvidenceGate(REPOSITORY, str(request["pr"]), head)
    verdict = fg.verify(gate, request["run"], "{}", read)
    fp.require(verdict == "Whole-project coverage measured by full PR CI", "not ordinary full PR evidence")
    _pr, merge, current = fg.pull_identity(gate, head)
    fp.require(current == base and fp.object_id(f"{merge}^{{tree}}") == tree, "full PR merge tree or main moved")
    return {"type": "full-pr/v1", "head": head, "base": base, "tree": tree, "run": request["run"], "attempt": 1}


def main():
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("protected reader request exceeds bound")
    request = json.loads(raw)
    reader = request["reader_head"]
    if (
        not isinstance(reader, str)
        or len(reader) != 40
        or any(c not in "0123456789abcdef" for c in reader)
    ):
        raise ValueError("invalid authenticated source SHA")
    # The caller supplies only the shared local object source. Never run in it or
    # inherit its index/config; use a disposable clone and official remote.
    for name in command(["git", "rev-parse", "--local-env-vars"]).decode().splitlines():
        os.environ.pop(name, None)
    os.environ.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    with tempfile.TemporaryDirectory(prefix="sm-checkpoint-") as directory:
        root = Path(directory)
        repo = root / "repo"
        source = root / "source"
        command(
            [
                "git",
                "clone",
                "--shared",
                "--no-checkout",
                "--",
                request["core_repo"],
                str(repo),
            ]
        )
        command(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                f"https://github.com/{REPOSITORY}.git",
            ],
            cwd=repo,
        )
        command(["git", "fetch", "--no-tags", "origin", "refs/heads/main"], cwd=repo)
        if (
            command(["git", "rev-parse", "FETCH_HEAD"], cwd=repo).decode().strip()
            != reader
        ):
            raise ValueError("actual protected main differs from requested source")
        archive = command(["git", "archive", reader, "scripts"], cwd=repo)
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(source, filter="data")
        sys.path.insert(0, str(source))
        package = types.ModuleType("scripts")
        package.__path__ = [str(source / "scripts")]
        package.__package__ = "scripts"
        sys.modules["scripts"] = package
        from scripts import focused_gate as fg, focused_plan as fp

        os.chdir(repo)
        if request.get("mode") == "registry":
            legacy, slots = request["legacy"], tuple(request["slots"])
            if hasattr(fp, "plan_registry"):
                result = fp.plan_registry(legacy, slots)
            else:
                fp.require(
                    not any(slots),
                    "current protected reader does not support compressed slots",
                )
                fp.require(
                    len(legacy.encode()) <= fp.MAX_PLAN, "legacy registry exceeds bound"
                )
                result = json.loads(legacy or "{}", object_pairs_hook=fp.unique_object)
                fp.require(isinstance(result, dict), "invalid focused registry")
        elif request.get("mode") == "full-pr":
            result = verified_full_pr(request, fp, fg)
        else:
            result = verified_segment(request, fp, fg)
        print(
            json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )


if __name__ == "__main__":
    main()
