"""Build or verify one focused admission packet as a pure function of Git objects.

    hz-focused-manifest PR_OR_BRANCH_OR_HEAD [--base REV] [--selection FILE] [--junit LANE=FILE ...]
                        [--production PATH ...] [--review REL --review-root DIR] [-o PACKET]
    hz-focused-manifest --verify PACKET [--junit LANE=FILE ...] [--review-root DIR]

The packet is the pair `hz-focused-admit prepare` takes: the pre-open PR plan
(`pull_request:<head>`, ref `refs/pull/*/merge`) and the headless resulting-main
plan (`push-tree:<tree>`), plus the review binding. Every field except the test
case inventory comes from Git objects in the Core repository:

- tree: `git merge-tree --write-tree BASE HEAD` (a conflict refuses);
- changed, diff_sha256: `git diff-tree -r --raw --no-abbrev --no-renames -z BASE TREE`;
- production: blobs of changed `src/heinzel/**.py` (plus reviewed --production extras);
- execution: blobs of base `scripts/focused_plan.py` PROTECTED_FILES in the tree;
- config_blob: the tree's `pyproject.toml` blob;
- selection: the base's own `scripts/affected.py` run on a checkout of the tree
  (web-1/web-2 browser lanes map to the `browser` lane), or a reviewed --selection.
  A full, "ALL", directory, empty or over-limit selection refuses like the resolver,
  except Vitest: when the base's focused_job.py has VITEST_ALL ("web/ALL"), a Vitest
  "ALL" or more than MAX_IDS Vitest files becomes the web lane ["web/ALL"] (the whole
  suite, no file filter). The sentinel is only accepted alone in the web lane.

case_sha256 is the canonical digest of each lane's JUnit `[classname, name]` pairs
(Core `focused_coverage.cases_digest`), read from --junit files of the focused run.
Every pytest case must belong to a selected test file and every selected file must
have a case. For ["web/ALL"] every JUnit classname must be a Vitest file of the tree
(web/**.test|spec.*, relative to web/) and every such file must have a case. Base
limits (LANES, MAX_IDS, PYTHON_CI_MAX_IDS, VITEST_ALL, TOKEN, PATH) are read from
base sources with `ast`; no Core or candidate module is imported here.
The base's affected.py and related_tests.py run with `python3 -I` from a directory
outside the candidate tree, which is read as data only and never on sys.path; no
candidate code executes. --junit files are not bound to the head by Git: confirm
they come from a focused run of exactly this head.

--verify regenerates from the packet's own head and base and compares bytes. The
selection, production extras and case digests are carried from the packet as
declared inputs (each revalidated; --junit re-derives the cases). The summary on
stderr shows what a reviewer checks. Reads PR metadata with `gh` only for a PR
number. Never fetches Git objects and never writes the focused registry.
Exit 0 ok/byte-equal, 1 refused or different, 2 usage.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from steuermaschine.config import child_environment

SHA = re.compile(r"[0-9a-f]{40}")
HEX = re.compile(r"[0-9a-f]{64}")
PREOPEN_PR_REF = "refs/pull/*/merge"
MAIN_REF = "refs/heads/main"
MAX_DIFF = 1024 * 1024
MAX_COVERED_FILES = 100
MAX_JUNIT = 32 * 1024 * 1024
KIND_PREFIX = {"pytest": "tests/", "vitest": "web/", "e2e": "tests/e2e/", "managed": "desktop/managed/test/",
               "node": "desktop/test/", "consumer": "plugins/"}
BROWSER_LANES = {"web-1", "web-2"}
TEST_FILE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
VITEST_ALL = "web/ALL"  # Core focused_job.VITEST_ALL; the base's value is read with ast and must match
MANIFEST_KEYS = {"schema", "event", "ref", "base", "head", "tree", "changed", "diff_sha256", "mode",
                 "production", "config_blob", "execution", "selection", "case_sha256"}
DRIVER = """
import json, pathlib, sys
selector, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path.insert(0, str(selector))  # only the two base blobs; the tree's scripts/ never enters sys.path
import affected, related_tests
related_tests.ROOT = root
repo = affected.Repo(root)
repo.__dict__["related"] = related_tests  # bypass Repo.related, which imports from the tree's scripts/
result = affected.classify(repo, json.load(sys.stdin))
result["lane_files"] = {} if result["full"] else {lane: repo.lanes[lane] for lane in result["e2e_lanes"]}
if hasattr(affected, "focused_lanes"):  # merge tier (ADR 0149): the base's own lane mapping, never a local copy
    lanes, reason = affected.focused_lanes(result, {} if result["full"] else repo.lanes)
    result["focused"] = {"lanes": lanes, "reason": reason}
print(json.dumps(result, sort_keys=True))
"""
# Round 3 (S10b): the judge pins exactly the base's focused_plan.protected_blobs(tree); run it, never re-derive it.
PROTECTED_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[1])  # only the base's own scripts, written outside the candidate tree
from scripts import focused_plan
print(json.dumps(focused_plan.protected_blobs(sys.argv[2]), sort_keys=True))
"""
BASE_SCRIPTS = ("focused_plan.py", "ci_workload.py", "focused_job.py", "release_candidate.py")


class ManifestError(Exception):
    """The packet cannot be built from these exact inputs."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


FORMATS = ("indent", "canonical", "canonical-nl")


def packet_bytes(packet: dict[str, Any], layout: str = "indent") -> bytes:
    """The reviewed packets' two file layouts: sorted two-space indent plus newline, or canonical JSON."""
    if layout == "indent":
        return (json.dumps(packet, sort_keys=True, indent=2) + "\n").encode()
    return canonical(packet) + (b"\n" if layout == "canonical-nl" else b"")


def layout_of(data: bytes) -> str:
    if data.startswith(b"{\n"):
        return "indent"
    return "canonical-nl" if data.endswith(b"\n") else "canonical"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean_env() -> dict[str, str]:
    # A hook or caller can export GIT_DIR/GIT_INDEX_FILE; never let it redirect these reads.
    return child_environment(drop_prefixes=("GIT_",))


class Repo:
    """Read-only access to Git objects of one Core repository."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True,
                              env=env or _clean_env(), timeout=120)
        if check and done.returncode:
            raise ManifestError(f"git {args[0]} failed: {done.stderr.decode(errors='replace').strip()[:300]}")
        return done

    def out(self, *args: str) -> bytes:
        return self.git(*args).stdout

    def commit(self, revision: str) -> str:
        value = self.out("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()
        require(SHA.fullmatch(value), f"not a commit: {revision}")
        return value

    def show(self, revision: str, path: str) -> bytes:
        return self.out("cat-file", "blob", f"{revision}:{path}")

    def blob(self, treeish: str, path: str) -> str:
        """Bind a regular source blob, rejecting symlinks and submodules (focused_plan.git_blob)."""
        entry = self.out("ls-tree", "-z", treeish, "--", path).split(b"\0")
        require(len(entry) == 2 and entry[-1] == b"", f"missing or ambiguous tracked file: {path}")
        metadata, name = entry[0].split(b"\t")
        mode, kind, blob = metadata.decode().split()
        require(name.decode() == path and mode in {"100644", "100755"} and kind == "blob" and SHA.fullmatch(blob),
                f"unsupported tracked source file: {path}")
        return blob

    def merge_tree(self, base: str, head: str) -> str:
        done = self.git("merge-tree", "--write-tree", "--no-messages", base, head, check=False)
        lines = done.stdout.decode().splitlines()
        require(done.returncode == 0 and lines and SHA.fullmatch(lines[0].strip()),
                "base and head do not merge cleanly; resolve the conflict first")
        return lines[0].strip()

    def raw_diff(self, base: str, tree: str) -> bytes:
        return self.out("diff-tree", "--no-commit-id", "-r", "--raw", "--no-abbrev", "--no-renames", "-z", base, tree)

    def files(self, tree: str, prefix: str) -> list[str]:
        """Tracked regular files under PREFIX in TREE (ls-tree; symlinks and submodules excluded)."""
        out = self.out("ls-tree", "-r", "-z", "--full-tree", tree, "--", prefix)
        files = []
        for entry in filter(None, out.split(b"\0")):
            metadata, name = entry.split(b"\t", 1)
            mode, kind, _blob = metadata.decode().split()
            if kind == "blob" and mode in {"100644", "100755"}:
                files.append(name.decode())
        return sorted(files)

    def checkout(self, tree: str, root: Path) -> None:
        """Materialize TREE exactly (no export-ignore, symlinks as plain files) under ROOT."""
        env = {**_clean_env(), "GIT_INDEX_FILE": str(root.parent / "index")}
        self.git("read-tree", tree, env=env)
        self.git("-c", "core.symlinks=false", f"--work-tree={root}", "checkout-index", "-a", "-f", env=env)


# -- base sources, read with ast, never imported ------------------------------------------------------------------

def _assignments(source: bytes) -> dict[str, ast.expr]:
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            found[node.target.id] = node.value
    return found


def _regex(node: ast.expr, name: str) -> re.Pattern[str]:
    require(isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant),
            f"unsupported base definition of {name}")
    flags = 0
    for extra in node.args[1:]:
        require(isinstance(extra, ast.Attribute) and extra.attr in {"ASCII", "A"}, f"unsupported flag in {name}")
        flags |= re.ASCII
    return re.compile(node.args[0].value, flags)


def _constant(node: ast.expr, *sources: dict[str, ast.expr]) -> str:
    """A string literal, or a module-level name bound to one in the given base sources (no code runs)."""
    if isinstance(node, ast.Name):
        found = next((source[node.id] for source in sources if node.id in source), None)
        require(found is not None, f"base defines no {node.id}")
        node = found
    value = ast.literal_eval(node)
    require(isinstance(value, str), "a whole-suite token is not a string")
    return value


def _whole_suites(plan: dict[str, ast.expr], job: dict[str, ast.expr]) -> dict[str, str]:
    """The base's WHOLE_SUITES {lane: token}; a value may name a constant of focused_plan.py or focused_job.py."""
    node = plan["WHOLE_SUITES"]
    require(isinstance(node, ast.Dict) and all(isinstance(k, ast.Constant) for k in node.keys),
            "unsupported base definition of WHOLE_SUITES")
    return {ast.literal_eval(k): _constant(v, plan, job) for k, v in zip(node.keys, node.values, strict=True)}


def _literal_set(node: ast.expr | None, name: str) -> frozenset[str]:
    """frozenset({...}) or a literal set/tuple from the base; absent: empty."""
    if node is None:
        return frozenset()
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset" and len(node.args) == 1:
        node = node.args[0]
    try:
        return frozenset(ast.literal_eval(node))
    except ValueError as exc:
        raise ManifestError(f"unsupported base definition of {name}") from exc


class Policy:
    """The resolver limits of the exact base (trusted main when admitted)."""

    def __init__(self, repo: Repo, base: str) -> None:
        plan = _assignments(repo.show(base, "scripts/focused_plan.py"))
        job = _assignments(repo.show(base, "scripts/focused_job.py"))
        try:
            self.protected = tuple(ast.literal_eval(plan["PROTECTED_FILES"]))
            # Round 2 (S10): tracked files matching a pattern are pinned too (focused_plan.protected_blobs).
            self.patterns = tuple(ast.literal_eval(plan["PROTECTED_PATTERNS"])) if "PROTECTED_PATTERNS" in plan else ()
            self.lanes = {k: tuple(v) for k, v in ast.literal_eval(plan["LANES"]).items()}
            self.max_ids = int(ast.literal_eval(job["MAX_IDS"]))
            # Older bases had one limit for every lane.
            self.python_max_ids = int(ast.literal_eval(job.get("PYTHON_CI_MAX_IDS", job["MAX_IDS"])))
            # Older bases have no whole-suite web selection: None keeps refusing Vitest ALL as before.
            self.vitest_all = ast.literal_eval(job["VITEST_ALL"]) if "VITEST_ALL" in job else None
        except (KeyError, ValueError, TypeError) as exc:
            raise ManifestError("base focused_plan.py/focused_job.py lack the expected literals") from exc
        require(self.vitest_all is None or self.vitest_all == VITEST_ALL,
                f"base VITEST_ALL is {self.vitest_all!r}, this tool knows {VITEST_ALL!r}")
        # Merge tier (ADR 0149): WHOLE_SUITES names one token per lane, the manifest only widens and a missing lane
        # fails open to the full set. Older bases: the web lane's VITEST_ALL alone, and a missing lane refuses.
        self.merge_tier = "WHOLE_SUITES" in plan
        self.whole = _whole_suites(plan, job) if self.merge_tier else (
            {"web": self.vitest_all} if self.vitest_all is not None else {})
        self.advisory = _literal_set(plan.get("ADVISORY_LANES"), "ADVISORY_LANES")
        self.path = _regex(plan.get("PATH"), "PATH")
        self.token = _regex(job.get("TOKEN"), "TOKEN")

    def protected_paths(self, repo: Repo, tree: str) -> list[str]:
        """Core focused_plan.protected_blobs' paths: PROTECTED_FILES plus the tracked files under tests/ that a
        PROTECTED_PATTERNS entry matches in its own directory (one level, fnmatch on the name)."""
        matched = []
        for path in repo.files(tree, "tests") if self.patterns else []:
            folder, _, name = path.rpartition("/")
            if any(folder == pattern.rpartition("/")[0] and fnmatch.fnmatchcase(name, pattern.rpartition("/")[2])
                   for pattern in self.patterns):
                matched.append(path)
        return [*self.protected, *(p for p in matched if p not in self.protected)]

    def path_ok(self, path: object) -> bool:
        return isinstance(path, str) and self.path.fullmatch(path) is not None and not {".", ".."} & set(path.split("/"))


# -- the same checks the resolver applies ----------------------------------------------------------------------

def changed_paths(raw: bytes, policy: Policy) -> dict[str, str]:
    require(len(raw) <= MAX_DIFF and (not raw or raw.endswith(b"\0")), "unsupported focused diff")
    parts = raw.split(b"\0")[:-1]
    require(not len(parts) % 2, "malformed focused diff")
    changed: dict[str, str] = {}
    for record, name in zip(parts[::2], parts[1::2], strict=True):
        fields, path = record.decode().split(), name.decode()
        require(len(fields) == 5 and fields[-1] in {"A", "M", "D", "T"} and policy.path_ok(path) and path not in changed,
                f"unrecognized diff record requires explicit reviewed handling: {path}")
        changed[path] = fields[-1]
    require(changed, "empty change: nothing to admit")
    return changed


def production_file(path: str) -> bool:
    return path.startswith("src/heinzel/") and path.endswith(".py")


def web_production(path: str) -> bool:
    return path.startswith("web/src/") and not TEST_FILE.search(path)


def check_selection(selection: Any, repo: Repo, tree: str, policy: Policy) -> None:
    """focused_plan._selections plus the lane requirements of _scope."""
    require(isinstance(selection, dict) and selection and set(selection) <= set(policy.lanes),
            "missing or unknown focused lane")
    for lane, tests in selection.items():
        kind = policy.lanes[lane][2]
        limit = policy.python_max_ids if lane == "python" else policy.max_ids
        require(isinstance(tests, list) and 0 < len(tests) <= limit,
                f"empty or broad focused lane {lane} ({len(tests) if isinstance(tests, list) else '?'} > {limit})")
        require(len(set(tests)) == len(tests), f"duplicate selection in {lane}")
        whole = [(owner, token) for owner, token in policy.whole.items() if token in tests]
        if whole:  # a whole suite names no file: only alone, only in its own lane, no blob to pin
            owner, token = whole[0]
            require(whole == [(lane, token)] and tests == [token], f"{token} is only a {owner} lane on its own: {lane} {tests}")
            continue
        for test in tests:
            path = test.split("::", 1)[0] if isinstance(test, str) else None
            require(isinstance(test, str) and policy.token.fullmatch(test) and policy.path_ok(path)
                    and path.startswith(KIND_PREFIX[kind]), f"invalid focused test in {lane}: {test}")
            require((kind == "vitest" and path == test and TEST_FILE.search(path))
                    or (kind == "managed" and path == test and path.endswith(".test.ts"))
                    or (kind == "node" and path == test and path.endswith(".test.js"))
                    or (kind in {"pytest", "e2e"} and path.endswith("_test.py")),
                    f"focused selections require owning test files or exact node IDs: {test}")
            require(kind != "pytest" or not path.startswith("tests/e2e/"), "browser tests require their owning lane")
            repo.blob(tree, path)


def desktop_unit(path: str) -> bool:
    """Core focused_plan.desktop_unit: the desktop app's own files, which its Node unit suite tests."""
    return path.startswith("desktop/") and not path.startswith("desktop/managed/") and not path.endswith(".md")


def unmet(changed: dict[str, str], production: dict[str, str], selection: dict[str, list[str]]) -> str | None:
    """Core focused_plan._unmet: the first lane requirement a selection misses. The merge tier then runs the full set
    (fail-open), so for a merge-tier base this is a note, not a refusal."""
    if len(production) > MAX_COVERED_FILES:
        return f"{len(production)} production files exceed the scoped coverage bound of {MAX_COVERED_FILES}"
    if production and "python" not in selection:
        return "Python production without a Python selection"
    browser = {"browser", "macos-browser", "e2e-web-1", "e2e-web-2"}
    if any(web_production(p) for p in changed) and not ("web" in selection and browser & selection.keys()):
        return "web production without Vitest and browser selections"
    if any(desktop_unit(p) for p in changed) and "desktop" not in selection:
        return "a desktop change without the desktop unit suite"
    return None


def check_scope(changed: dict[str, str], production: dict[str, str], selection: dict[str, list[str]],
                policy: Policy | None = None) -> str | None:
    """Older bases refuse a selection that misses a lane; a merge-tier base returns why CI runs the full set."""
    if policy is not None and policy.merge_tier:
        return unmet(changed, production, selection)
    require(len(production) <= MAX_COVERED_FILES, "too many production files")
    require(not production or "python" in selection, "Python production requires a Linux coverage selection")
    if any(web_production(p) for p in changed):
        require("web" in selection and {"browser", "macos-browser"} & selection.keys(),
                "web production requires reviewed Vitest and browser selections")
    if any(p.startswith("desktop/managed/src/") for p in changed):
        require("managed" in selection, "managed production requires reviewed owning Node tests")
    return None


# -- selection from the repository's own logic -------------------------------------------------------------------

def repo_selection(repo: Repo, base: str, tree: str, changed: dict[str, str]) -> dict[str, Any]:
    """Run the base's scripts/affected.py (isolated) against a checkout of the tree."""
    with tempfile.TemporaryDirectory(prefix="sm-focused-manifest-") as scratch:
        root, selector = Path(scratch) / "tree", Path(scratch) / "selector"
        root.mkdir()
        selector.mkdir()
        repo.checkout(tree, root)
        for name in ("affected.py", "related_tests.py"):  # trusted base selector, outside the candidate tree
            (selector / name).write_bytes(repo.show(base, f"scripts/{name}"))
        done = subprocess.run([sys.executable, "-I", "-c", DRIVER, str(selector), str(root)],
                              input=json.dumps(list(changed)).encode(),
                              capture_output=True, cwd=scratch, env=_clean_env(), timeout=300)
    require(done.returncode == 0, f"base affected.py failed: {done.stderr.decode(errors='replace')[-300:]}")
    return json.loads(done.stdout)


def base_protected_blobs(repo: Repo, base: str, treeish: str) -> dict[str, str] | None:
    """The base's own focused_plan.protected_blobs(TREEISH), or None for a base without it (older bases).

    The base's scripts run with `python3 -I` from a scratch directory, reading Git objects of this repository; no
    candidate code runs."""
    if b"def protected_blobs(" not in repo.show(base, "scripts/focused_plan.py"):
        return None
    with tempfile.TemporaryDirectory(prefix="sm-focused-protected-") as scratch:
        (Path(scratch) / "scripts").mkdir()
        (Path(scratch) / "scripts" / "__init__.py").write_bytes(b"")  # a regular package: no site-packages `scripts` shadows it
        for name in BASE_SCRIPTS:
            if repo.git("cat-file", "-e", f"{base}:scripts/{name}", check=False).returncode == 0:
                (Path(scratch) / "scripts" / name).write_bytes(repo.show(base, f"scripts/{name}"))
        done = subprocess.run([sys.executable, "-I", "-c", PROTECTED_DRIVER, scratch, treeish], capture_output=True,
                              cwd=repo.path, env=_clean_env(), timeout=300)
    require(done.returncode == 0, f"base focused_plan.protected_blobs failed: {done.stderr.decode(errors='replace')[-300:]}")
    blobs = json.loads(done.stdout)
    require(isinstance(blobs, dict) and all(isinstance(k, str) and isinstance(v, str) and SHA.fullmatch(v) for k, v in blobs.items()),
            "base focused_plan.protected_blobs returned no path -> blob map")
    return blobs


def derive_selection(result: dict[str, Any], *, vitest_all: str | None = None,
                     max_ids: int | None = None) -> tuple[dict[str, list[str]] | None, list[str]]:
    """Map the advisory selection to focused lanes; return (selection or None, refusal reasons).

    With the base's VITEST_ALL, a Vitest "ALL" or more than max_ids Vitest files is the whole suite. A merge-tier
    base's selector maps the lanes itself (affected.focused_lanes, carried as result["focused"]): taken as it is.
    """
    if isinstance(result.get("focused"), dict):
        lanes, reason = result["focused"].get("lanes"), str(result["focused"].get("reason") or "")
        if lanes is None:
            return None, [f"the base selector runs the full set: {reason}", *result["reasons"]]
        return {lane: list(tests) for lane, tests in lanes.items()}, []
    whole_web = vitest_all is not None and (
        "ALL" in result["vitest"] or (max_ids is not None and len(result["vitest"]) > max_ids))
    if result["full"] or "ALL" in result["pytest"] or ("ALL" in result["vitest"] and not whole_web):
        return None, ["repository selector selects the full set", *result["reasons"]]
    refusals = [f"directory selection is broad: {p}" for p in result["pytest"] if p.endswith("/")]
    refusals += [f"e2e lane {lane} has no focused lane mapping" for lane in result["e2e_lanes"]
                 if lane not in BROWSER_LANES]
    selection: dict[str, list[str]] = {}
    if result["pytest"]:
        selection["python"] = sorted(result["pytest"])
    if whole_web:
        selection["web"] = [vitest_all]
    elif result["vitest"]:
        selection["web"] = sorted(result["vitest"])
    browser = sorted({f for lane in result["e2e_lanes"] if lane in BROWSER_LANES for f in result["lane_files"][lane]})
    if browser:
        selection["browser"] = browser
    if not selection:
        refusals.append("repository selector selects no tests")
    return (None if refusals else selection), refusals


def selector_asks_more(result: dict[str, Any], selection: dict[str, list[str]], policy: Policy) -> list[tuple[str, str]]:
    """The concrete test files the repository selector names that a declared selection leaves out (0rxo).

    Only exact files count: a directory or ALL is the broad ask a declared selection exists to narrow. A named file is
    covered by itself or by its lane's whole-suite token, never by a node id in it: `FILE::one_test` would leave out the
    rest of the tests that read the changed code."""
    asks = [(lane, t) for lane, key in (("python", "pytest"), ("web", "vitest"))
            for t in result.get(key, []) if t != "ALL" and not t.endswith("/")]  # the lanes derive_selection maps to
    for lane in result.get("e2e_lanes", []):
        if lane in BROWSER_LANES:
            asks += [("browser", t) for t in result.get("lane_files", {}).get(lane, [])]

    def covered(lane: str, test: str) -> bool:
        chosen = selection.get(lane, [])
        whole = policy.whole.get(lane)
        return (whole is not None and chosen == [whole]) or test in chosen

    return sorted({(lane, t) for lane, t in asks if not covered(lane, t)})


# -- case inventory ----------------------------------------------------------------------------------------------

def junit_cases(data: bytes, *, advisory: bool = False) -> list[tuple[str, str]]:
    """Core focused_coverage.cases_digest's checks, returning the sorted pairs. An advisory lane (Core ADVISORY_LANES,
    focused_coverage.advisory_cases) reports failed cases instead of refusing them; skipped cases stay refused."""
    require(0 < len(data) <= MAX_JUNIT, "missing or oversized focused test report")
    require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(), "unexpected XML declaration")
    root = ET.fromstring(data)  # noqa: S314 - bounded local report with no DTD or entity declaration
    require(root.tag in {"testsuite", "testsuites"}, "invalid test report")
    cases = list(root.iter("testcase"))
    names = [(case.get("classname", ""), case.get("name", "")) for case in cases]
    require(cases and len(set(names)) == len(cases) and all(name and group for group, name in names)
            and not any(child.tag in ({"skipped"} if advisory else {"failure", "error", "skipped"})
                        for case in cases for child in case),
            "focused selection skipped, failed, duplicated, or collected no tests")
    return sorted(names)


def junit_files(data: bytes) -> list[str]:
    """The file a Node test case ran from (node --test names it in `file`; the classname when there is none)."""
    root = ET.fromstring(data)  # noqa: S314 - bounded by junit_cases before this is called
    return sorted({case.get("file") or case.get("classname", "") for case in root.iter("testcase")})


def cases_digest(cases: list[tuple[str, str]]) -> str:
    return sha256(canonical([list(pair) for pair in cases]))


def check_case_owners(kind: str, tests: list[str], cases: list[tuple[str, str]], lane: str) -> None:
    """Every case belongs to a selected file, every selected file has a case.

    pytest classnames are dotted modules (optionally plus a class); Vitest and
    managed Node classnames are the file path relative to the project root; a
    desktop Node case names its file (absolute, or relative to the root).
    """
    if kind in {"pytest", "e2e"}:
        modules = {t.split("::", 1)[0][:-3].replace("/", "."): t for t in tests}
        owns = lambda m, c: c == m or c.startswith(m + ".")  # noqa: E731
    else:
        modules = {t: t for t in tests}
        owns = lambda m, c: c == m or m.endswith("/" + c) or (kind == "node" and c.endswith("/" + m))  # noqa: E731
    seen = set()
    for classname, _ in cases:
        owner = next((m for m in modules if owns(m, classname)), None)
        require(owner, f"{lane} JUnit case {classname} is outside the selection")
        seen.add(owner)
    missing = sorted(modules[m] for m in modules if m not in seen)
    require(not missing, f"{lane} selection has no JUnit case for {', '.join(missing)}")


def vitest_files(repo: Repo, tree: str) -> list[str]:
    """The tree's Vitest files as JUnit names them: web/ test files, relative to web/."""
    return [p.removeprefix("web/") for p in repo.files(tree, "web/") if TEST_FILE.search(p)]


def check_whole_vitest(files: list[str], cases: list[tuple[str, str]], lane: str) -> None:
    """For ["web/ALL"]: every case is in a Vitest file of the tree, every such file has a case (exact names)."""
    known, seen = set(files), {classname for classname, _ in cases}
    outside = sorted(seen - known)
    require(not outside, f"{lane} JUnit cases outside the tree's Vitest files: {', '.join(outside[:5])}")
    missing = sorted(known - seen)
    require(not missing, f"{lane} whole suite has no JUnit case for {len(missing)} file(s): {', '.join(missing[:5])}")


# -- build -------------------------------------------------------------------------------------------------------

def build(repo: Repo, head: str, base: str, *, selection: dict[str, list[str]] | None, cases: dict[str, str],
          junit: dict[str, bytes], extra_production: list[str], review: tuple[str, bytes] | None,
          summary: list[str]) -> dict[str, Any]:
    policy = Policy(repo, base)
    tree = repo.merge_tree(base, head)
    raw = repo.raw_diff(base, tree)
    changed = changed_paths(raw, policy)
    changed_python = sorted(p for p, s in changed.items() if production_file(p) and s != "D")
    for path in extra_production:
        require(policy.path_ok(path) and production_file(path), f"not a covered production file: {path}")
    production = {p: repo.blob(tree, p) for p in sorted({*changed_python, *extra_production})}
    pinned = base_protected_blobs(repo, base, tree)
    if pinned is not None:  # the base's own protected_blobs, on the tested tree and on the base itself
        execution, earlier = pinned, base_protected_blobs(repo, base, base) or {}
    else:
        execution = {p: repo.blob(tree, p) for p in policy.protected_paths(repo, tree)}
        earlier = {p: repo.blob(base, p) for p in policy.protected_paths(repo, base)}
    protected_changed = [p for p in execution if execution[p] != earlier.get(p)]
    protected_changed += sorted(set(earlier) - set(execution))  # a pinned file the change deletes
    config_blob = repo.blob(tree, "pyproject.toml")

    advisory = repo_selection(repo, base, tree, changed)
    derived, refusals = derive_selection(advisory, vitest_all=policy.vitest_all, max_ids=policy.max_ids)
    if selection is None:
        require(derived is not None, "refused: " + "; ".join(refusals[:8]) + " (supply a reviewed --selection)")
        selection = derived
    check_selection(selection, repo, tree, policy)
    fail_open = check_scope(changed, production, selection, policy)
    if selection is not derived and not policy.merge_tier:
        missing = selector_asks_more(advisory, selection, policy)
        require(not missing, f"refused: the declared selection leaves out {len(missing)} test(s) the repository selector asks for "
                f"(tests that read the changed files): " + ", ".join(f"{lane} {t}" for lane, t in missing[:12])
                + " (add them to the selection)")

    case_sha = dict(cases)
    case_source = {lane: "carried from packet" for lane in case_sha}
    counts = {}
    for lane, data in junit.items():
        require(lane in selection, f"--junit lane {lane} is not selected")
        pairs = junit_cases(data, advisory=lane in policy.advisory)
        kind = policy.lanes[lane][2]
        if policy.vitest_all is not None and selection[lane] == [policy.vitest_all]:
            check_whole_vitest(vitest_files(repo, tree), pairs, lane)
        elif selection[lane] == [policy.whole.get(lane)]:
            pass  # another whole suite (desktop, consumers, a browser e2e suite) names no file: Core checks no owner
        elif kind == "node":
            check_case_owners(kind, selection[lane], [(f, "") for f in junit_files(data)], lane)
        else:
            check_case_owners(kind, selection[lane], pairs, lane)
        case_sha[lane] = cases_digest(pairs)
        case_source[lane] = f"junit sha256 {sha256(data)}, {len(pairs)} cases, NOT bound to head by Git: confirm its run"
        counts[lane] = len(pairs)
    require(set(case_sha) == set(selection), "case_sha256 needs exactly one JUnit inventory per selected lane: "
            f"missing {sorted(set(selection) - set(case_sha))}, extra {sorted(set(case_sha) - set(selection))}")
    require(all(isinstance(v, str) and HEX.fullmatch(v) for v in case_sha.values()), "invalid case digest")

    pr = {
        "schema": 1, "event": "pull_request", "ref": PREOPEN_PR_REF, "base": base, "head": head, "tree": tree,
        "changed": changed, "diff_sha256": sha256(raw),
        "mode": "pr-scoped" if production else "python-coverage-not-applicable", "production": production,
        "config_blob": config_blob, "execution": execution, "selection": selection, "case_sha256": case_sha,
    }
    main = {k: v for k, v in pr.items() if k != "head"} | {"event": "push", "ref": MAIN_REF}
    binding = {"base": base, "head": head, "tree": tree, "manifest_sha256": sha256(canonical(pr)),
               "main_manifest_sha256": sha256(canonical(main))}
    if review:
        binding |= {"path": review[0], "sha256": sha256(review[1])}

    summary += [
        f"head {head}", f"base {base}", f"tree {tree} (merge-tree)",
        f"changed ({len(changed)}):", *(f"  {s} {p}" for p, s in changed.items()),
        "protected execution changed: " + (", ".join(protected_changed) + " -> needs --reviewed-protected-producer"
                                           if protected_changed else "none"),
        "pyproject.toml: " + ("changed" if config_blob != repo.blob(base, "pyproject.toml") else "unchanged"),
        f"production ({len(production)}): " + ", ".join(
            p + ("" if p in changed_python else " [declared extra]") for p in production),
        "repo selector (base scripts/affected.py): " + ("; ".join(refusals) if refusals else "bounded")
        + f"; advisory jobs {', '.join(advisory['jobs'])}",
    ]
    if policy.merge_tier:
        summary.append("merge tier: CI selects the lanes itself; this manifest only widens them"
                       + (f"; CI runs the FULL set: {fail_open}" if fail_open else "")
                       + ("; it is REQUIRED: a protected file changed" if protected_changed else "; it is optional"))
    if policy.vitest_all is not None and selection.get("web") == [policy.vitest_all]:
        summary.append(f"web: {policy.vitest_all} = the whole Vitest suite, no file filter "
                       f"({len(advisory['vitest'])} advisory id(s): {', '.join(advisory['vitest'][:3])})")
    if derived == selection:
        summary.append("selection: derived from the repository selector")
    else:
        summary.append("selection: DECLARED, delta vs repository selector:")
        old = {(lane, t) for lane, ts in (derived or {}).items() for t in ts}
        new = {(lane, t) for lane, ts in selection.items() for t in ts}
        summary += [f"  + {lane} {t}" for lane, t in sorted(new - old)]
        summary += [f"  - {lane} {t}" for lane, t in sorted(old - new)] if derived else ["  (no bounded derivation)"]
    summary += [f"case_sha256 {lane} {case_sha[lane]} ({case_source[lane]})" for lane in sorted(case_sha)]
    summary += [f"manifest_sha256 {binding['manifest_sha256']}", f"main_manifest_sha256 {binding['main_manifest_sha256']}",
                f"review {binding['path']} {binding['sha256']}" if review else
                "review: none yet (not admissible; review these digests, then rerun with --review)"]
    return {"schema": 1, "manifest": pr, "main_manifest": main, "review": binding}


# -- inputs ------------------------------------------------------------------------------------------------------

def resolve_target(repo: Repo, target: str) -> str:
    if SHA.fullmatch(target):
        return repo.commit(target)
    if target.isdigit():
        url = repo.out("remote", "get-url", "origin").decode().strip()
        slug = re.sub(r"\.git$", "", re.split(r"github\.com[:/]", url)[-1])
        done = subprocess.run(["gh", "pr", "view", target, "-R", slug, "--json", "headRefOid"],
                              capture_output=True, timeout=60)
        require(done.returncode == 0, f"cannot read PR {target}: {done.stderr.decode(errors='replace').strip()[:200]}")
        head = json.loads(done.stdout)["headRefOid"]
        require(SHA.fullmatch(head), "PR metadata lacks an exact head")
        require(repo.git("cat-file", "-e", f"{head}^{{commit}}", check=False).returncode == 0,
                f"PR head {head} is not in {repo.path}; fetch it first (this command never fetches)")
        return head
    return repo.commit(target)


def read_junit(values: list[str]) -> dict[str, bytes]:
    junit = {}
    for value in values:
        lane, sep, path = value.partition("=")
        require(sep and lane and path and lane not in junit, f"--junit expects one LANE=FILE per lane: {value}")
        junit[lane] = Path(path).expanduser().read_bytes()
    return junit


MANAGEMENT = Path.home() / "Code/heinzel-management"


def read_review(rel: str | None, roots: list[Path]) -> tuple[str, bytes] | None:
    """Hash the review artifact under the first root that has it (hz-focused-admit --review-root)."""
    if rel is None:
        return None
    require(not Path(rel).is_absolute() and ".." not in Path(rel).parts, "review path must be relative")
    for root in roots:
        path = (root / rel).resolve()
        if path.is_file():
            path.relative_to(root.resolve(strict=True))
            return rel, path.read_bytes()
    raise ManifestError(f"review artifact {rel} not found under {', '.join(map(str, roots))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sm head manifest", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", nargs="?", help="PR number, branch or exact head commit")
    parser.add_argument("--verify", metavar="PACKET", help="regenerate PACKET and compare bytes")
    parser.add_argument("--core-repo", type=Path, default=Path.home() / "Code/heinzel")
    parser.add_argument("--base", default="origin/main", help="base revision (default origin/main, read locally)")
    parser.add_argument("--selection", type=Path, help="reviewed JSON {lane: [test ids]} instead of the derived one")
    parser.add_argument("--junit", action="append", default=[], metavar="LANE=FILE")
    parser.add_argument("--production", action="append", default=[], metavar="PATH",
                        help="explicitly affected covered file beyond the changed ones")
    parser.add_argument("--review", metavar="REL", help="review artifact path relative to --review-root")
    parser.add_argument("--review-root", type=Path,
                        help="default: the packet's directory (--verify), then ~/Code/heinzel-management")
    parser.add_argument("--format", choices=FORMATS, default="indent",
                        help="packet layout; --verify uses the original's layout")
    parser.add_argument("-o", "--output", type=Path, help="write the packet here instead of stdout")
    args = parser.parse_args(argv)
    if bool(args.target) == bool(args.verify):
        parser.error("give exactly one of TARGET or --verify PACKET")
    repo = Repo(args.core_repo.expanduser())
    summary: list[str] = []
    try:
        junit = read_junit(args.junit)
        if args.verify:
            original = Path(args.verify).read_bytes()
            packet = json.loads(original)
            plan = packet["manifest"]
            require(set(plan) <= MANIFEST_KEYS, f"packet manifest has unsupported fields: {sorted(set(plan) - MANIFEST_KEYS)}")
            changed_python = {p for p, s in plan.get("changed", {}).items() if production_file(p) and s != "D"}
            roots = [args.review_root.expanduser()] if args.review_root else [Path(args.verify).resolve().parent, MANAGEMENT]
            result = build(repo, repo.commit(plan["head"]), repo.commit(plan["base"]), selection=plan["selection"],
                           cases=plan["case_sha256"], junit=junit,
                           extra_production=sorted(set(plan["production"]) - changed_python),
                           review=read_review(packet["review"].get("path"), roots), summary=summary)
        else:
            selection = json.loads(args.selection.read_bytes()) if args.selection else None
            roots = [args.review_root.expanduser()] if args.review_root else [MANAGEMENT]
            result = build(repo, resolve_target(repo, args.target), repo.commit(args.base), selection=selection,
                           cases={}, junit=junit, extra_production=args.production,
                           review=read_review(args.review, roots), summary=summary)
    except (ManifestError, OSError, ValueError, KeyError, ET.ParseError) as exc:
        print("\n".join(summary), file=sys.stderr)
        print(f"sm head manifest: REFUSED: {exc}", file=sys.stderr)
        return 1
    data = packet_bytes(result, layout_of(original) if args.verify else args.format)
    print("\n".join(summary), file=sys.stderr)
    if args.verify:
        if data == original:
            print(f"BYTE-EQUAL sha256 {sha256(data)}", file=sys.stderr)
            return 0
        theirs = json.loads(original)
        for part in ("manifest", "main_manifest", "review"):
            for key in sorted(set(result[part]) | set(theirs.get(part, {}))):
                if result[part].get(key) != theirs.get(part, {}).get(key):
                    print(f"DIFFERENT {part}.{key}", file=sys.stderr)
        if json.loads(data) == theirs:
            print("DIFFERENT formatting only (same JSON value)", file=sys.stderr)
        return 1
    if args.output:
        args.output.write_bytes(data)
        print(f"wrote {args.output} sha256 {sha256(data)}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(data)
    return 0

