#!/usr/bin/env python3
"""Collect a focused plan's test cases on an exact Git tree instead of carrying a digest from an older base.

    collect_cases.py collect --repo GIT --tree TREE --selection SEL.json --lane LANE -o JUNIT
                     [--python PY] [--node-modules DIR] [--work DIR]
    collect_cases.py compare --hosted JUNIT --pr JUNIT --result JUNIT [-o DIFF.txt]

`collect` extracts TREE (git archive) into a scratch directory, lists the lane's selected tests without running
them (pytest --collect-only for python/browser; the web lane runs its selected Vitest files because `vitest
list` leaves test.each names unexpanded) and writes a minimal JUnit file whose
testcase [classname, name] pairs are exactly what the hosted focused job reports (pytest's junitxml naming; vitest
junit's file-relative classname and "describe > test" name). hz-focused-manifest --junit LANE=FILE reads it.

`compare` is the admission rule for a resulting-main plan:
  1. the cases collected on the PR's tested tree must equal the hosted PR run's JUnit cases, otherwise collection
     does not reproduce hosted CI (platform-dependent ids, runtime skips, markers): REFUSED, exit 1;
  2. the cases collected on the resulting tree are what the main run will report. When they differ from the PR
     tree, a selected test scans the tree (e.g. tests/unit/standards_test.py parametrizes over every *.py): exit 3
     and print the differing ids; the main plan MUST then use the resulting tree's collected JUnit (never the
     carried PR digest). Exit 0: identical.
Root cause this guards against: main run 43130763651 (tree 5f378857) failed "python test cases differ from
reviewed selection" because the a-web2 main plan carried the PR's 2411-case digest while #769-#771 had added
6 *.py files, giving 12 more standards_test cases on main (2423).

Reuse: next-main.sh uses this for every resulting-main plan (a-ai, a-rel and later mains alike).
Never runs tests, never writes the focused registry, never touches the live checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

LANE_KIND = {"python": "pytest", "python312": "pytest", "macos": "pytest", "browser": "e2e", "web": "vitest"}


class CollectError(RuntimeError):
    pass


def require(ok: object, message: str) -> None:
    if not ok:
        raise CollectError(message)


def mangle(nodeid: str) -> tuple[str, str]:
    """pytest junitxml's mangle_test_address: (classname, name) of a node id."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    require(len(names) >= 2 and names[0].endswith(".py"), f"not a test node id: {nodeid}")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def pytest_cases(output: str) -> list[tuple[str, str]]:
    ids = [line.strip() for line in output.splitlines() if "::" in line and not line.startswith((" ", "<"))]
    require(ids, "pytest collected no test")
    return [mangle(i) for i in ids]


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(cases: list[tuple[str, str]]) -> str:
    """Core focused_coverage.cases_digest over the sorted pairs (as hz-focused-manifest.junit_cases does)."""
    return hashlib.sha256(canonical([list(p) for p in sorted(cases)])).hexdigest()


def write_junit(cases: list[tuple[str, str]], path: Path) -> None:
    require(len(set(cases)) == len(cases), "duplicate collected case")
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="collected", tests=str(len(cases)))
    for classname, name in cases:
        ET.SubElement(suite, "testcase", classname=classname, name=name)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def read_junit(path: Path) -> list[tuple[str, str]]:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    return sorted((c.get("classname", ""), c.get("name", "")) for c in cases)


def extract(repo: Path, tree: str, dest: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", tree], env=env, check=True,
                             capture_output=True, timeout=300).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True, timeout=300)
    # Tests that list the repository (standards_test.py: git ls-files) need a Git index equal to TREE, as in the
    # hosted checkout: a private repository borrowing the source objects, index read from TREE (no commit).
    objects = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", "objects"],
                             env=env, check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    subprocess.run(["git", "init", "-q", str(dest)], env=env, check=True, timeout=60)
    (dest / ".git/objects/info/alternates").write_text(objects + "\n")
    subprocess.run(["git", "-C", str(dest), "read-tree", tree], env=env, check=True, timeout=60)
    subprocess.run(["git", "-C", str(dest), "update-index", "-q", "--refresh"], env=env, timeout=120)


def collect(repo: Path, tree: str, selection: dict, lane: str, python: str, node_modules: Path | None,
            work: Path) -> list[tuple[str, str]]:
    kind = LANE_KIND.get(lane)
    require(kind, f"unknown lane {lane}")
    ids = selection.get(lane) or []
    require(ids, f"lane {lane} has no selection")
    dest = Path(tempfile.mkdtemp(prefix=f"collect-{lane}-{tree[:8]}-", dir=work))
    try:
        extract(repo, tree, dest)
        if kind == "vitest":
            require(node_modules and node_modules.is_dir(), "--node-modules is required for the web lane")
            (dest / "web" / "node_modules").symlink_to(node_modules)
            # `vitest list` leaves test.each names unformatted ("%s"); the hosted JUnit has them expanded. So the
            # web lane runs the selected files (focused, --maxWorkers=2) and reads its own JUnit's names.
            report = dest / "vitest-junit.xml"
            # ["web/ALL"] is Core's VITEST_ALL sentinel: the whole suite, no file filter (focused_job.plan).
            files = [] if ids == ["web/ALL"] else [i.removeprefix("web/") for i in ids]
            run([str(dest / "web/node_modules/.bin/vitest"), "run", "--maxWorkers=2", "--reporter=junit",
                 f"--outputFile.junit={report}", *files], dest / "web")
            return read_junit(report)
        inherited = os.environ.get("PYTHONPATH", "")  # hz-gate's own pytest plugin path stays importable
        env_py = {"PYTHONPATH": os.pathsep.join(filter(None, [str(dest / "src"), str(dest / "scripts"), inherited]))}
        marker = ["-m", "e2e"] if kind == "e2e" else []
        out = run([python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *marker, *ids],
                  dest, env_py)
        return pytest_cases(out)
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def run(cmd: list[str], cwd: Path, extra: dict | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | (extra or {})
    done = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    require(done.returncode == 0, f"collection failed ({done.returncode}): {' '.join(cmd[:4])}...\n"
            + (done.stdout + done.stderr)[-2000:])
    return done.stdout


def compare(hosted: list, pr: list, result: list) -> tuple[int, list[str]]:
    lines = []
    if sorted(hosted) != sorted(pr):
        lines += [f"- hosted-only {c}::{n}" for c, n in sorted(set(hosted) - set(pr))]
        lines += [f"+ collected-only {c}::{n}" for c, n in sorted(set(pr) - set(hosted))]
        return 1, lines
    if sorted(pr) != sorted(result):
        lines += [f"- pr-tree-only {c}::{n}" for c, n in sorted(set(pr) - set(result))]
        lines += [f"+ resulting-tree-only {c}::{n}" for c, n in sorted(set(result) - set(pr))]
        return 3, lines
    return 0, lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--repo", type=Path, required=True)
    c.add_argument("--tree", required=True)
    c.add_argument("--selection", type=Path, required=True)
    c.add_argument("--lane", required=True)
    c.add_argument("--python", default=sys.executable)
    c.add_argument("--node-modules", type=Path)
    c.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "collect-cases-work")
    c.add_argument("-o", "--output", type=Path, required=True)
    k = sub.add_parser("compare")
    k.add_argument("--hosted", type=Path, required=True)
    k.add_argument("--pr", type=Path, required=True)
    k.add_argument("--result", type=Path, required=True)
    k.add_argument("-o", "--output", type=Path)
    a = p.parse_args(argv)
    try:
        if a.cmd == "collect":
            a.work.mkdir(parents=True, exist_ok=True)
            cases = collect(a.repo, a.tree, json.loads(a.selection.read_text()), a.lane, a.python,
                            a.node_modules, a.work)
            write_junit(cases, a.output)
            print(f"{a.lane} {a.tree[:12]} {len(cases)} cases digest {digest(cases)}", file=sys.stderr)
            return 0
        code, lines = compare(read_junit(a.hosted), read_junit(a.pr), read_junit(a.result))
        text = "\n".join(lines) + ("\n" if lines else "")
        if a.output:
            a.output.write_text(text)
        sys.stderr.write(text[-4000:])
        return code
    except (CollectError, OSError, ValueError, ET.ParseError, subprocess.SubprocessError) as exc:
        print(f"collect_cases: REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
