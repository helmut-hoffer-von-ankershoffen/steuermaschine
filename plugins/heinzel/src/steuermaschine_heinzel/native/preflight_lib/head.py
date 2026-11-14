"""The checks of `sm head preflight`: what hosted CI and the release candidate check, run locally on one Core head.

Moved from the former head preflight with its checks unchanged. What changed is how it reaches the world: every
process, the run lock, the commit-meta check, scoped coverage and the one GitHub read are callables on `Ctx`, bound
by `native/preflight.py` to the providers of the call; the constants are settings; the step record, the verdict and
the report are the core's (`steuermaschine.preflight`). The hosted lanes are no step any more: `sm head validate`
runs them, and `hosted_lanes` names the lanes for that command.

Checks, in the order they run (a check is left out only by `--skip NAME`, and the verdict names every skipped one):
  tree        clean tree, head ahead of the base, merge-base == base tip, local base == remote main
  lint        `make lint` (format, ruff, contracts, typos, ...) through the run lock
  typos       `uv run typos` over the changed files and over the commit messages
  types       pyright on the changed Python files (py)
  standards   the repository-wide standards tests, always, whatever the selection (py)
  related     tests that import a changed module, integration tests of a changed CLI or access path (py)
  meta        the commit-meta check: conventional commit subjects <= 72 chars, branch name, confidential terms
  coverage    when src/ changed: scoped coverage of the selection (>= 90 %; prints the margin, warns under 2 points)
  dup         Sonar duplication estimate: new lines inside a 10-line / 100-token block that exists twice; fails at
              >= 2.5 % of new lines (the Sonar gate is 3 %); honours sonar.cpd.exclusions
  smells      WARN-ONLY HEURISTIC for the Sonar new-code maintainability rating (gate: debt over 5 % of new lines):
              on changed JS/TS/Python lines it finds functions nested more than 4 deep (S2004), control flow nested
              more than 4 deep (S134) and `await` inside a loop (S9382), prints the rule ids and a debt estimate.
              It is no Sonar run: every other Sonar rule is NOT covered; Python complexity is covered by ruff in
              `make lint` (C901, PLR09xx)
  deps        Core dependency gate: [project] dependencies / optional-dependencies changed vs the base -> FAIL
              "plugin consumers need a relock first". Unchanged and a consumer named: build the wheel from the head
              and run the Consumer check of that one plugin at the plugins ref pinned in ci.yml
  e2e         changed drivers under web/e2e: every owning journey (tests/e2e) and every journey whose driver imports
              a changed helper must be a selected browser id, or be listed under `local_only` with a reason
  validation  refuses a head SHA that already has check suites on a validation/* branch (checks are posted by SHA,
              the PR then sits BLOCKED with every check green). One GitHub read
"""
# ruff: noqa: E731  (moved code: three small lambdas keep their lines)
from __future__ import annotations

import ast
import fnmatch
import json
import re
import shlex
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steuermaschine.preflight import FAIL, Preflight, Step

BASE = "origin/main"  # the ref a head is compared with (setting `preflight_base`, see `bind`)
STEPS = ("tree", "lint", "typos", "types", "standards", "related", "meta", "coverage", "dup", "smells", "deps", "e2e", "validation")
COV_MIN, COV_WARN_MARGIN = 90.0, 2.0
DUP_FAIL, SONAR_GATE = 2.5, 3.0
DUP_LINES, DUP_TOKENS, DUP_MIN_NEW = 10, 100, 20
DUP_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css")
SMELL_JS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
SMELL_DEPTH, DEBT_GATE, DEBT_LINE_MIN = 4, 5.0, 30  # Sonar: ratio = remediation minutes / (30 min per new line)
SMELL_COST = {"S2004": 20, "S134": 10, "S9382": 5}  # rough remediation minutes per finding
DRIVER_SUFFIXES = (".mjs", ".js", ".ts", ".tsx", ".html")


def bind(base: str) -> None:
    """Set the base ref for this call."""
    global BASE
    BASE = base


@dataclass
class Ctx:
    wt: Path
    sh: Any = None  # (argv, cwd=None, input=None) -> (exit code, output): one process
    gate: Any = None  # (argv, cwd=None) -> (exit code, output): one heavy command through the run lock; 75 = no slot
    meta: Any = None  # (ctx) -> (exit code, output): the commit-meta check of the head
    scoped: Any = None  # (worktree, selection file) -> (exit code, output): scoped coverage, ending in a `SCOPED n%` line
    gh: Any = None  # (args) -> (exit code, output): one read through the GitHub client
    repo: str = ""  # OWNER/NAME of the core repository (setting `repo`)
    standards: list[str] = field(default_factory=list)  # globs of the repo-wide standards tests (setting `preflight_standards`)
    sel: dict | None = None
    sel_path: Path | None = None
    sel_bytes: bytes = b""  # the bytes `select` read: every check uses these, and the summary names their sha256
    local_only: dict = field(default_factory=dict)
    outdir: Path | None = None
    sha: str = ""
    branch: str = ""
    base_sha: str = ""
    gh_calls: int = 0

    def git(self, *args: str, input=None):
        return self.sh(["git", *args], cwd=self.wt, input=input)

    def changed(self, *paths: str, filter_: str = "") -> list[str]:
        args = ["diff", "--name-only", *([f"--diff-filter={filter_}"] if filter_ else []), BASE, "HEAD", "--", *paths]
        return [p for p in self.git(*args)[1].splitlines() if p.strip()]

    def log(self, name: str, text: str) -> str:
        if not self.outdir:
            return ""
        self.outdir.mkdir(parents=True, exist_ok=True)
        path = self.outdir / f"preflight-head-{self.sha[:8]}-{name}.log"
        path.write_text(text)
        return str(path)


def tail(text: str, n: int = 12) -> str:
    return "\n".join("       | " + line for line in text.strip().splitlines()[-n:])


# ---- pure helpers (unit tested) --------------------------------------------------------------------------------

def added_lines(diff: str) -> dict[str, set[int]]:
    """`git diff -U0` -> {path: line numbers added on the new side}."""
    out: dict[str, set[int]] = {}
    cur = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
            if cur:
                out.setdefault(cur, set())
            continue
        m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
        if m and cur:
            start, n = int(m[1]), int(m[2] if m[2] is not None else 1)
            out[cur].update(range(start, start + n))
    return out


def _code_lines(path: str, text: str) -> list[tuple[int, str, int]]:
    """(line number, normalised text, token count) of every line that is neither blank nor a pure comment."""
    marks = ("#",) if path.endswith(".py") else ("//", "/*", "*")
    rows = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = " ".join(raw.split())
        if line and not line.startswith(marks):
            rows.append((number, line, len(re.findall(r"\w+|[^\w\s]", line))))
    return rows


def dup_estimate(files: dict[str, str], added: dict[str, set[int]], min_lines: int = DUP_LINES, min_tokens: int = DUP_TOKENS) -> dict:
    """Sonar CPD stand-in: a block of `min_lines` code lines with >= `min_tokens` tokens found at two places is
    duplicated; the result is the share of NEW code lines that lie inside such a block."""
    seen: dict[int, tuple[str, int]] = {}
    marked: dict[str, set[int]] = {}
    twin: dict[str, str] = {}
    code = {path: _code_lines(path, text) for path, text in files.items()}
    for path in sorted(code):
        rows = code[path]
        for i in range(len(rows) - min_lines + 1):
            window = rows[i:i + min_lines]
            if sum(r[2] for r in window) < min_tokens:
                continue
            key = hash("\n".join(r[1] for r in window))
            first = seen.setdefault(key, (path, i))
            if first == (path, i) or (first[0] == path and abs(first[1] - i) < min_lines):
                continue
            other = code[first[0]][first[1]:first[1] + min_lines]
            marked.setdefault(path, set()).update(r[0] for r in window)
            marked.setdefault(first[0], set()).update(r[0] for r in other)
            twin.setdefault(path, f"{first[0]}:{other[0][0]}")
            twin.setdefault(first[0], f"{path}:{window[0][0]}")
    new = dup = 0
    per_file = {}
    for path, lines in sorted(added.items()):
        if path not in code:
            continue
        mine = lines & {r[0] for r in code[path]}
        hit = sorted(mine & marked.get(path, set()))
        new += len(mine)
        dup += len(hit)
        if hit:
            per_file[path] = {"lines": len(hit), "first": hit[0], "last": hit[-1], "twin": twin.get(path, "")}
    return {"new_lines": new, "duplicated": dup, "percent": round(100 * dup / new, 2) if new else 0.0, "files": per_file}


def _js_plain(text: str) -> str:
    """JS/TS with comments, strings and template text blanked, line count kept (regex literals are not handled)."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m[0])
    return re.sub(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`", blank, text, flags=re.S)


def js_smells(text: str, added: set[int]) -> list[tuple[int, str, str]]:
    """Heuristic (line, rule, message) on added lines: a brace opened after `function`/`=>`/a method head is a function,
    after for/while/do a loop, after if/else/switch/try/catch a block."""
    out, stack = [], []  # stack of "fn" | "loop" | "block" | "other"
    for number, line in enumerate(_js_plain(text).splitlines(), 1):
        pos = 0
        for m in re.finditer(r"[{}]|\bawait\b", line):
            head = line[pos:m.start()]
            if m[0] == "{":
                pos = m.end()
                if re.search(r"\bfor\s+await\b", head):
                    kind = "block"
                elif re.search(r"\b(for|while|do)\b", head):
                    kind = "loop"
                elif re.search(r"\b(if|else|switch|try|catch|finally)\b", head):
                    kind = "block"
                elif re.search(r"=>\s*$|\bfunction\b|\)\s*(:\s*[^={}]+)?\s*$", head):
                    kind = "fn"
                else:
                    kind = "other"
                stack.append(kind)
                if number in added and kind == "fn" and stack.count("fn") > SMELL_DEPTH:
                    out.append((number, "S2004", f"function nested {stack.count('fn')} deep (max {SMELL_DEPTH})"))
                since_fn = stack[len(stack) - 1 - stack[::-1].index("fn"):] if "fn" in stack else stack
                depth = sum(k in ("loop", "block") for k in since_fn)
                if number in added and kind in ("loop", "block") and depth > SMELL_DEPTH:
                    out.append((number, "S134", f"control flow nested {depth} deep (max {SMELL_DEPTH})"))
            elif m[0] == "}":
                pos = m.end()
                if stack:
                    stack.pop()
            elif number in added:
                since_fn = stack[len(stack) - 1 - stack[::-1].index("fn"):] if "fn" in stack else stack
                if "loop" in since_fn:
                    out.append((number, "S9382", "await inside a loop (run the promises together, or say why they must be serial)"))
    return out


def py_smells(text: str, added: set[int]) -> list[tuple[int, str, str]]:
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    nest = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
    scope = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

    def walk(node, depth, loop, chained=False):
        for name, value in ast.iter_fields(node):
            for child in value if isinstance(value, list) else [value]:
                if not isinstance(child, ast.AST):
                    continue
                if isinstance(child, scope):
                    walk(child, 0, False)
                    continue
                elif_ = isinstance(node, ast.If) and name == "orelse" and isinstance(child, ast.If) and len(value) == 1
                deeper = depth + (isinstance(child, nest) and not elif_)
                if isinstance(child, nest) and not elif_ and deeper > SMELL_DEPTH and child.lineno in added:
                    out.append((child.lineno, "S134", f"control flow nested {deeper} deep (max {SMELL_DEPTH})"))
                if isinstance(child, ast.Await) and loop and child.lineno in added:
                    out.append((child.lineno, "S9382", "await inside a loop (gather the awaitables, or say why they must be serial)"))
                in_loop = loop or (isinstance(child, (ast.For, ast.While)) and name != "iter")
                walk(child, deeper, in_loop or (isinstance(node, (ast.For, ast.While)) and name == "body"))

    walk(tree, 0, False)
    return sorted(set(out))


def smell_estimate(files: dict[str, str], added: dict[str, set[int]]) -> dict:
    """Findings on the added lines of the given files and the debt ratio they would give on Sonar's formula."""
    hits = []
    for path in sorted(files):
        found = py_smells(files[path], added.get(path, set())) if path.endswith(".py") else js_smells(files[path], added.get(path, set()))
        hits += [{"file": path, "line": line, "rule": rule, "message": message} for line, rule, message in found]
    new = sum(len(added.get(path, ())) for path in files)
    debt = sum(SMELL_COST[h["rule"]] for h in hits)
    return {"new_lines": new, "findings": hits, "rules": sorted({h["rule"] for h in hits}), "debt_minutes": debt,
            "percent": round(100 * debt / (DEBT_LINE_MIN * new), 1) if new else 0.0}


def cpd_exclusions(properties: str) -> list[str]:
    m = re.search(r"^\s*sonar\.cpd\.exclusions\s*=\s*(.+)$", properties, re.M)
    return [p.strip() for p in m[1].split(",") if p.strip()] if m else []


def in_dup_scope(path: str, exclusions: list[str]) -> bool:
    return path.endswith(DUP_SUFFIXES) and not path.endswith(".min.js") and not any(
        path == e or fnmatch.fnmatch(path, e.replace("**/", "*")) for e in exclusions)


def dependencies(pyproject: str) -> dict:
    project = tomllib.loads(pyproject).get("project", {})
    return {"dependencies": sorted(project.get("dependencies", [])),
            "optional-dependencies": {k: sorted(v) for k, v in sorted(project.get("optional-dependencies", {}).items())}}


def dependency_changes(base: str, head: str) -> list[str]:
    a, b = dependencies(base), dependencies(head)
    out = [f"dependencies: -{x}" for x in a["dependencies"] if x not in b["dependencies"]]
    out += [f"dependencies: +{x}" for x in b["dependencies"] if x not in a["dependencies"]]
    for extra in sorted(set(a["optional-dependencies"]) | set(b["optional-dependencies"])):
        old, new = a["optional-dependencies"].get(extra, []), b["optional-dependencies"].get(extra, [])
        out += [f"optional-dependencies.{extra}: -{x}" for x in old if x not in new]
        out += [f"optional-dependencies.{extra}: +{x}" for x in new if x not in old]
    return out


LOCK_BY_CONTRACT = "5eb12e2fa00c38548dc1e4ccd93a57546651453a"  # plugins: ADR 0003, a plugin lock is valid by contract
CONTRACT_FILE = "src/heinzel_plugin_tools/cli.py"  # where the plugins toolkit declares PLUGIN_API = {api: {library: major}}


def plugin_api(text: str) -> dict[int, dict[str, int]] | None:
    """The toolkit's PLUGIN_API as a plain literal, or None when it is absent or not a literal of that shape."""
    import ast

    try:
        body = ast.parse(text).body
    except (SyntaxError, ValueError):
        return None
    values = [node.value for node in body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLUGIN_API" for t in node.targets)]
    if len(values) != 1:
        return None
    try:
        api = ast.literal_eval(values[0])
    except (ValueError, TypeError, SyntaxError):
        return None
    ok = isinstance(api, dict) and api and all(type(n) is int and isinstance(libs, dict) and libs
                                                 and all(isinstance(k, str) and type(v) is int for k, v in libs.items()) for n, libs in api.items())
    return api if ok else None


def outside_contract(requirements: list[str], libraries: dict[str, int]) -> list[str]:
    """Why Core's base requirements leave one API's majors: a library missing, or a specifier that admits another major."""
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    found: dict[str, Requirement] = {}
    for line in requirements:
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        found[canonicalize_name(req.name)] = req
    problems = []
    for library, major in sorted(libraries.items()):
        named = found.get(canonicalize_name(library))
        if named is None:
            problems.append(f"{library} is no base requirement (the contract provides {library} {major})")
            continue
        others = [Version(v) for v in (f"{major + 1}.0", f"{major + 1}.99", f"{major + 50}.0", f"{max(major - 1, 0)}.999", "0.0.1") if Version(v).major != major]
        inside = [Version(f"{major}.{minor}") for minor in range(100)]
        if any(named.specifier.contains(v, prereleases=True) for v in others) or not any(named.specifier.contains(v) for v in inside):
            problems.append(f"{named} admits versions outside {library} {major}")
    return problems


def requirement_names(pyproject: str) -> set[str]:
    """The names of the base requirements of one pyproject.toml, lower case."""
    return {re.split(r"[\s<>=!~;\[(]", x, maxsplit=1)[0].lower() for x in dependencies(pyproject)["dependencies"]}


def lock_by_contract(c: Ctx, plugins: Path | None) -> tuple[dict[int, dict[str, int]] | None, str]:
    """PLUGIN_API at the plugins commit Core's consumer job tests, when that commit is at or after plugins ADR 0003
    (locks valid by contract); else (None, why it cannot be judged)."""
    ci = c.wt / ".github" / "workflows" / "ci.yml"
    ref = consumer_ref(ci.read_text()) if ci.is_file() else None
    if not ref:
        return None, "no consumer-check job in .github/workflows/ci.yml"
    if plugins is None:
        return None, "no plugins clone to read the contract from (--plugins-repo DIR or the setting preflight_plugins_repo)"
    if c.sh(["git", "-C", plugins, "cat-file", "-e", f"{ref}^{{commit}}"])[0] != 0:
        return None, f"plugins ref {ref[:12]} is not in {plugins} (git -C {plugins} fetch origin {ref})"
    if c.sh(["git", "-C", plugins, "merge-base", "--is-ancestor", LOCK_BY_CONTRACT, ref])[0] != 0:
        return None, f"the consumer ref {ref[:12]} is not at or after plugins {LOCK_BY_CONTRACT[:7]} (ADR 0003)"
    rc, text = c.sh(["git", "-C", plugins, "show", f"{ref}:{CONTRACT_FILE}"])
    api = plugin_api(text) if rc == 0 else None
    return (api, "") if api else (None, f"no PLUGIN_API literal in {CONTRACT_FILE} at plugins {ref[:12]}")


def consumer_ref(ci_yml: str) -> str | None:
    """The plugins commit the Core `consumer-check` job tests against."""
    m = re.search(r"^  consumer-check:\n(.*?)(?=(?:^  [\w-]+:\n)|\Z)", ci_yml, re.M | re.S)
    if not m:
        return None
    ref = re.search(r"repository:\s*\S+/heinzel-plugins\s*\n\s*ref:\s*([0-9a-f]{40}|[\w./-]+)", m[1])
    return ref[1] if ref else "main"


def _mentions(text: str, name: str) -> bool:
    return re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", text) is not None


def driver_importers(drivers: dict[str, str]) -> dict[str, set[str]]:
    """{web/e2e file: files that import it directly}; relative imports and html `src=` only."""
    back: dict[str, set[str]] = {}
    for path, text in drivers.items():
        folder = path.rsplit("/", 1)[0]
        for ref in re.findall(r"""(?:from|import|src=)\s*\(?\s*["'](\.{1,2}/[^"']+)["']""", text):
            parts: list[str] = []
            for part in f"{folder}/{ref}".split("/"):
                if part == "..":
                    parts and parts.pop()
                elif part not in (".", ""):
                    parts.append(part)
            target = "/".join(parts)
            for candidate in (target, *(target + s for s in DRIVER_SUFFIXES)):
                if candidate in drivers:
                    back.setdefault(candidate, set()).add(path)
                    break
    return back


def e2e_owners(changed: list[str], drivers: dict[str, str], journeys: dict[str, str]) -> tuple[dict[str, list[str]], list[str]]:
    """-> ({journey: reasons}, changed drivers no journey owns). `drivers`: web/e2e path -> text at the head;
    `journeys`: tests/e2e/*.py path -> text. A journey owns a driver when it (or a helper module it imports) names it."""
    back = driver_importers(drivers)
    why = {start: f"owns changed {start}" for start in changed}  # affected driver -> reason
    for start in changed:
        for importer in sorted(_closure(start, back)):
            why.setdefault(importer, f"its driver {importer} imports changed helper {start}")
    is_journey = lambda p: p.endswith("_test.py")
    module = lambda p: p.rsplit("/", 1)[1][:-3]
    reasons: dict[str, set[str]] = {}
    for path, text in journeys.items():
        for driver, reason in why.items():
            if _mentions(text, driver.rsplit("/", 1)[1]):
                reasons.setdefault(path, set()).add(reason)
    grew = True
    while grew:  # helper modules (and conftest) pass their drivers on to the journeys that import them
        grew = False
        for helper in [p for p in list(reasons) if not is_journey(p) and not p.endswith("conftest.py")]:
            name = module(helper)
            for path, text in journeys.items():
                if path != helper and re.search(rf"(tests\.e2e\.{name}\b|from \.{name} import|from \. import [^\n]*\b{name}\b)", text):
                    extra = {f"{r} via {helper}" if " via " not in r else r for r in reasons[helper]} - reasons.get(path, set())
                    if extra:
                        reasons.setdefault(path, set()).update(extra)
                        grew = True
    owners = {p: sorted(r) for p, r in sorted(reasons.items()) if is_journey(p) or p.endswith("conftest.py")}
    named = {d for d in why if any(_mentions(t, d.rsplit("/", 1)[1]) for t in journeys.values())}
    unowned = sorted(c for c in changed if c not in named and c in drivers and ".test." not in c
                     and not any(i in named for i in _closure(c, back)))
    return owners, unowned


def _closure(start: str, back: dict[str, set[str]]) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        for nxt in back.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def local_only_of(raw) -> dict[str, str]:
    """{"id": "reason"} or [{"id": ..., "reason": ...}] -> {file: reason}; entries without a reason are dropped."""
    items = raw.items() if isinstance(raw, dict) else [(e.get("id"), e.get("reason")) for e in raw or [] if isinstance(e, dict)]
    return {str(k).split("::")[0]: str(v).strip() for k, v in items if k and isinstance(v, str) and v.strip()}


WHOLE_E2E = {"e2e-web-1": "tests/e2e/web-1", "e2e-web-2": "tests/e2e/web-2"}  # Core WHOLE_SUITES: a whole browser suite


def missing_journeys(owners: dict[str, list[str]], sel: dict | None, local_only: dict[str, str]) -> list[str]:
    if any((sel or {}).get(lane) == [token] for lane, token in WHOLE_E2E.items()):
        return []  # merge tier (ADR 0149): a whole browser e2e suite lane runs every web journey
    selected = {i.split("::")[0] for lane in ("browser", "macos-browser") for i in (sel or {}).get(lane, [])}
    return [j for j in owners if j not in selected and j not in local_only]


def validation_suites(payload: dict) -> list[dict]:
    """Check suites of the head SHA that were started from a validation/* branch."""
    return [{"branch": s.get("head_branch"), "app": (s.get("app") or {}).get("slug"), "status": s.get("status"),
             "conclusion": s.get("conclusion")}
            for s in payload.get("check_suites", []) if str(s.get("head_branch") or "").startswith("validation/")]


def full_set(c: Ctx) -> str | None:
    """Why the merge tier runs this head's full set (the checkout's `scripts/focused_plan.py --suggest BASE`), or None:
    focused lanes, or an older Core without --suggest. A full-set PR has no focused lane to validate."""
    rc, out = c.sh(["python3", "scripts/focused_plan.py", "--suggest", c.base_sha], cwd=c.wt)
    try:
        found = json.loads(out) if rc == 0 else {}
    except ValueError:
        return None
    return str(found.get("reason") or "the selector selects the full set") if isinstance(found, dict) and found.get("full") is True else None


def hosted_lanes(changed: list[str]) -> list[str]:
    lanes = ["web-1", "web-2"]
    if any(p.startswith("src/heinzel/heads/cli/") or "modules/system/" in p for p in changed):
        lanes += ["cli", "supervised"]
    return lanes


def parse_scoped(out: str) -> float | None:
    m = re.search(r"^SCOPED (\d+(?:\.\d+)?)%", out, re.M)
    return float(m[1]) if m else None


# ---- steps -----------------------------------------------------------------------------------------------------

def step_tree(c: Ctx) -> Step:
    s = Step("tree")
    dirty = c.git("status", "--porcelain")[1].strip()
    if dirty:
        s.fail(f"working tree is not clean ({len(dirty.splitlines())} paths, first: {dirty.splitlines()[0].strip()})",
               "commit the reviewed change or remove the stray files; the packet binds the exact head SHA")
    ahead = c.git("rev-list", "--count", f"{BASE}..HEAD")[1].strip()
    if ahead in ("", "0") or not ahead.isdigit():
        s.fail(f"head is not ahead of {BASE}", "commit the change on the branch; there is nothing to hand over")
    merge_base = c.git("merge-base", BASE, "HEAD")[1].strip()
    if merge_base != c.base_sha:
        s.fail(f"merge-base {merge_base[:9]} is not the {BASE} tip {c.base_sha[:9]}",
               f"git merge {BASE} (never rebase a pushed branch), then rerun the focused checks")
    rc, out = c.git("ls-remote", "origin", "refs/heads/main")
    remote = out.split()[0] if rc == 0 and out.split() else ""
    if not remote:
        s.warn(f"could not read the remote main tip; local {BASE} {c.base_sha[:9]} may be stale")
    elif remote != c.base_sha:
        s.fail(f"local {BASE} {c.base_sha[:9]} is stale, remote main is {remote[:9]}",
               f"git fetch origin main && git merge {BASE}, then rerun the focused checks")
    s.detail = f"{ahead or '?'} commit(s) ahead of {BASE} {c.base_sha[:9]}"
    s.data.update(ahead=ahead, merge_base=merge_base, remote_main=remote)
    return s


def step_lint(c: Ctx) -> Step:
    s = Step("lint")
    rc, out = c.gate(["make", "lint"], cwd=c.wt)
    log = c.log("lint", out)
    if rc == 75:
        return s.fail("the run lock gave no slot for `make lint` (exit 75 = WAIT)", "wait for load to drop and rerun; do not bypass the run lock")
    if rc != 0:
        s.fail(f"`make lint` failed (exit {rc}){'; log ' + log if log else ''}\n{tail(out)}",
               "fix the named check; `make format` fixes format/ruff, a typos hit is fixed in the source or allow-listed "
               "under [tool.typos.default.extend-words] in pyproject.toml with a reason")
    s.detail = "make lint green (includes typos)" if rc == 0 else ""
    return s


def step_typos(c: Ctx) -> Step:
    s = Step("typos")
    files = [f for f in c.changed(filter_="ACMR") if (c.wt / f).is_file()]
    if files:
        rc, out = c.sh(["uv", "run", "typos", "--force-exclude", "--", *files], cwd=c.wt)
        if rc != 0:
            s.fail(f"typos in the changed files\n{tail(out)}", "correct the spelling, or allow-list a real word under [tool.typos.default.extend-words]")
    messages = c.git("log", "--format=%B", f"{BASE}..HEAD")[1]
    if messages.strip():
        rc, out = c.sh(["uv", "run", "typos", "-"], cwd=c.wt, input=messages)
        if rc != 0:
            s.fail(f"typos in the commit messages\n{tail(out)}",
                   f"messages cannot change after a push (no force-push): rebuild the branch from {BASE} with corrected messages BEFORE the first push")
    s.detail = f"{len(files)} changed file(s) and the commit messages"
    return s


def step_meta(c: Ctx) -> Step:
    s = Step("meta")
    rc, out = c.meta(c)
    if rc != 0:
        s.fail(f"the commit-meta check failed\n{tail(out)}", f"reword BEFORE the first push (subjects <= 72 chars, conventional); after a push rebuild the branch from {BASE}")
    s.detail = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
    return s


def step_coverage(c: Ctx) -> Step:
    s = Step("coverage")
    prod = [p for p in c.changed("src", filter_="d") if p.endswith(".py")]  # a deleted file has nothing to cover
    if not prod:
        s.detail = "no Python under src/ changed: scoped coverage does not apply"
        return s
    if not (c.sel or {}).get("python") or not c.sel_path:
        return s.fail(f"{len(prod)} file(s) under src/ changed but there is no SEL with python tests",
                      'pass --sel SEL.json with {"python": [test files]} that cover the changed files')
    with tempfile.TemporaryDirectory(prefix="sm-preflight-sel-") as frozen:  # scoped coverage reads the bytes `select` read
        copy = Path(frozen) / "sel.json"
        copy.write_bytes(c.sel_bytes)
        rc, out = c.scoped(c.wt, copy)
    log = c.log("coverage", out)
    percent = parse_scoped(out)
    s.data.update(percent=percent, threshold=COV_MIN, files=prod)
    if percent is None:
        return s.fail(f"scoped coverage gave no result (exit {rc}){'; log ' + log if log else ''}\n{tail(out)}",
                      "a selected test failed or the worktree has no .venv (`uv sync --locked` there); fix and rerun")
    margin = round(percent - COV_MIN, 3)
    s.data["margin"] = margin
    s.detail = f"scoped coverage {percent:.3f} %, margin {margin:+.3f} points over {COV_MIN:.0f} %"
    if percent < COV_MIN or rc != 0:
        missing = "\n".join("       | " + line for line in out.splitlines() if "missing lines" in line and "missing lines [] missing branches []" not in line)
        s.fail(f"scoped coverage {percent:.3f} % is below {COV_MIN:.0f} % ({margin:+.3f} points)\n{missing}" if percent < COV_MIN
               else f"the scoped coverage run exited {rc}\n{tail(out)}",
               "cover the missing lines and branches of the WHOLE changed files with focused tests and add them to SEL python")
    elif margin < COV_WARN_MARGIN:
        s.warn(f"scoped coverage margin is only {margin:.3f} points; hosted runs serially and can differ slightly")
    return s


def step_dup(c: Ctx) -> Step:
    s = Step("dup")
    properties = c.wt / ".sonarcloud.properties"
    exclusions = cpd_exclusions(properties.read_text()) if properties.is_file() else []
    added = {p: lines for p, lines in added_lines(c.git("diff", "-U0", BASE, "HEAD")[1]).items() if in_dup_scope(p, exclusions) and lines}
    if not added:
        s.detail = "no new source lines in the duplication scope"
        return s
    files = {}
    for path in c.git("ls-files")[1].splitlines():
        if in_dup_scope(path, exclusions) and (c.wt / path).is_file():
            try:
                files[path] = (c.wt / path).read_text(errors="replace")
            except OSError:
                continue
    result = dup_estimate(files, added)
    s.data.update(result, fail_at=DUP_FAIL, sonar_gate=SONAR_GATE, scanned_files=len(files))
    s.detail = f"{result['duplicated']} of {result['new_lines']} new code lines duplicated = {result['percent']:.2f} % (fail >= {DUP_FAIL} %, Sonar gate {SONAR_GATE:.0f} %)"
    where = "\n".join(f"       | {p}: {v['lines']} lines in {v['first']}-{v['last']}, twin near {v['twin']}" for p, v in result["files"].items())
    if result["percent"] >= DUP_FAIL and result["new_lines"] >= DUP_MIN_NEW:
        s.fail(f"duplication estimate {result['percent']:.2f} % of new lines (>= {DUP_FAIL} %; Sonar fails at {SONAR_GATE:.0f} %)\n{where}",
               "move the repeated block into one shared helper and call it from both places; do not add files to sonar.cpd.exclusions")
    elif result["duplicated"]:
        s.warn(f"{result['duplicated']} new duplicated line(s) below the bar:\n{where}")
    return s


def step_smells(c: Ctx) -> Step:
    s = Step("smells")
    properties = c.wt / ".sonarcloud.properties"
    exclusions = cpd_exclusions(properties.read_text()) if properties.is_file() else []
    added = {p: lines for p, lines in added_lines(c.git("diff", "-U0", BASE, "HEAD")[1]).items()
             if lines and p.endswith((".py", *SMELL_JS)) and not p.endswith(".min.js") and (c.wt / p).is_file()}
    if not added:
        s.detail = "no new JS/TS/Python lines (heuristic, warn-only)"
        return s
    result = smell_estimate({p: (c.wt / p).read_text(errors="replace") for p in added}, added)
    s.data.update(result, gate=DEBT_GATE, heuristic=True, cpd_exclusions_ignored=bool(exclusions))
    s.detail = (f"HEURISTIC, warn-only: {len(result['findings'])} finding(s) on {result['new_lines']} new lines"
                + (f" ({', '.join(result['rules'])}), est. debt {result['percent']:.1f} % (Sonar gate {DEBT_GATE:.0f} %)" if result["findings"] else "")
                + "; other Sonar rules are not checked")
    if result["findings"]:
        where = "\n".join(f"       | {h['file']}:{h['line']} {h['rule']} {h['message']}" for h in result["findings"][:20])
        over = "would FAIL the Sonar maintainability gate" if result["percent"] > DEBT_GATE else "below the gate by this estimate"
        s.warn(f"estimated new-code debt {result['percent']:.1f} % of {result['new_lines']} new lines, {over} (heuristic):\n{where}\n"
               "         fix: flatten the nesting (early return, extract a named function); replace await-in-loop with Promise.all / asyncio.gather")
    return s


def step_deps(c: Ctx, consumers: str | None, target: str, plugins: Path | None) -> Step:
    s = Step("deps")
    rc, base = c.git("show", f"{BASE}:pyproject.toml")
    head_file = c.wt / "pyproject.toml"
    if rc != 0 or not head_file.is_file():
        return s.fail("could not read pyproject.toml at the base or the head", "run on a Core worktree")
    head_text = head_file.read_text()
    changes = dependency_changes(base, head_text)
    s.data["changes"] = changes
    s.detail = "dependencies and optional-dependencies unchanged"
    if changes:
        listed = "\n".join("       | " + x for x in changes)
        api, why = lock_by_contract(c, plugins)
        if api is None:
            return s.fail(f"Core dependencies changed vs origin/main: plugin consumers need a relock first\n{listed}\n"
                          f"       | lock-by-contract (plugins ADR 0003) not judged: {why}",
                          "relock every consumer in heinzel-plugins against a wheel of this head (`make lock NAME=<plugin> CORE=<wheel>`), land it, "
                          "move the Consumer pin in ci.yml, then hand this head over; tell the coordinator now (the 0.9.40 candidate failed here)")
        requirements = dependencies(head_text)["dependencies"]
        verdicts = {number: outside_contract(requirements, libraries) for number, libraries in sorted(api.items())}
        inside = [number for number, problems in verdicts.items() if not problems]
        s.data["contract"] = {"api": inside[-1] if inside else None, "libraries": api[inside[-1]] if inside else {},
                              "problems": [] if inside else verdicts[max(verdicts)]}
        if not inside:
            return s.fail("Core dependencies leave the plugin-api contract (plugins ADR 0003)\n" + listed + "\n"
                          + "\n".join(f"       | core:plugin-api@{n}: {'; '.join(p)}" for n, p in verdicts.items()),
                          "keep every contract library inside its major (for example rich>=15,<16), or give plugins a new plugin API number first")
        libraries = ", ".join(f"{k} {v}" for k, v in sorted(api[inside[-1]].items()))
        s.detail = f"dependencies changed inside core:plugin-api@{inside[-1]} ({libraries}): plugin locks stay valid by contract (plugins ADR 0003)"
        if added := sorted(requirement_names(head_text) - requirement_names(base)):
            s.warn(f"new base dependency {', '.join(added)}: a plugin lock that does not pin it fails its consumer check (ADR 0003 rule 4); "
                   "relock those plugins as tooling")
    if not consumers:
        s.data["consumer"] = "not run (--consumers NAME)"
        return s
    ci = c.wt / ".github" / "workflows" / "ci.yml"
    ref = consumer_ref(ci.read_text()) if ci.is_file() else None
    if not ref:
        return s.fail("no consumer-check job in .github/workflows/ci.yml", "check how the head's CI tests consumers; nothing was run")
    if plugins is None:  # no default: the clone is a path of this machine
        return s.fail("--consumers needs a local clone of the plugins repository and none is configured", "give --plugins-repo DIR, or set preflight_plugins_repo in [heinzel]")
    if c.sh(["git", "-C", plugins, "cat-file", "-e", f"{ref}^{{commit}}"])[0] != 0:
        return s.fail(f"plugins ref {ref[:12]} is not in {plugins}", f"git -C {plugins} fetch origin {ref}")
    rc, out = c.gate(["make", "build"], cwd=c.wt)
    wheels = sorted((c.wt / "dist").glob("heinzel-*.whl"), key=lambda p: p.stat().st_mtime) if (c.wt / "dist").is_dir() else []
    if rc != 0 or not wheels:
        return s.fail(f"`make build` gave no wheel (exit {rc})\n{tail(out)}", "fix the build; the Consumer jobs test the built wheel")
    work = (c.outdir or c.wt.parent) / f"preflight-consumer-{c.sha[:8]}"
    rc, out = c.sh(["git", "-C", plugins, "worktree", "add", "--detach", work, ref])
    if rc != 0:
        return s.fail(f"could not create the plugins worktree {work}\n{tail(out)}", "remove a leftover worktree of an earlier run and rerun")
    try:
        rc, out = c.gate(["make", target, f"NAME={consumers}", f"CORE={wheels[-1]}"], cwd=work)
    finally:
        c.sh(["git", "-C", plugins, "worktree", "remove", "--force", work])
    log = c.log("consumer", out)
    s.data["consumer"] = {"plugin": consumers, "ref": ref, "target": target, "wheel": wheels[-1].name, "exit": rc}
    if rc != 0:
        refused = "refusing" in out
        s.fail(f"Consumer ({consumers}) `make {target}` at plugins {ref[:9]} failed (exit {rc}){'; log ' + log if log else ''}\n{tail(out)}",
               "the run lock refuses the full plugins `make check`: ask the coordinator to run this step with --allow-full, or use --consumers-target check-fast"
               if refused else "the head breaks the public plugin contract or a pinned lock: fix Core, or relock the consumer first")
    else:
        s.detail += f"; Consumer ({consumers}) `make {target}` green at plugins {ref[:9]}"
    return s


def step_e2e(c: Ctx) -> Step:
    s = Step("e2e")
    changed = c.changed("web/e2e")
    if not changed:
        s.detail = "no driver under web/e2e changed"
        return s
    read = lambda folder, suffixes: {str(p.relative_to(c.wt)): p.read_text(errors="replace")
                                    for p in sorted((c.wt / folder).rglob("*")) if p.is_file() and p.name.endswith(suffixes)}
    owners, unowned = e2e_owners(changed, read("web/e2e", DRIVER_SUFFIXES), read("tests/e2e", (".py",)))
    missing = missing_journeys(owners, c.sel, c.local_only)
    s.data.update(changed=changed, owners=owners, unowned=unowned, missing=missing,
                  local_only={j: r for j, r in c.local_only.items() if j in owners})
    s.detail = f"{len(changed)} changed driver file(s), {len(owners)} owning journey(s): " + (", ".join(owners) or "none")
    if missing:
        lines = "\n".join(f"       | {j}: {'; '.join(owners[j])}" for j in missing)
        s.fail(f"{len(missing)} owning journey(s) are not SEL browser ids\n{lines}",
               'add each to SEL "browser" (hosted evidence), or list it under "local_only": {"<journey>": "<reason>"} and run it on the Mini; '
               "over 10 browser ids: run the full lanes with `sm head validate`")
    if unowned:
        s.warn("no journey under tests/e2e names: " + ", ".join(unowned))
    return s


def step_validation(c: Ctx) -> Step:
    s = Step("validation")
    c.gh_calls += 1
    rc, out = c.gh(["api", f"repos/{c.repo}/commits/{c.sha}/check-suites?per_page=100"])
    try:
        payload = json.loads(out) if rc == 0 else None
    except ValueError:
        payload = None
    if payload is None:
        if "No commit found" in out or "HTTP 422" in out or "HTTP 404" in out:
            s.detail = "head SHA is not on GitHub yet: no check suite can be attached to it"
            return s
        return s.fail(f"could not read the check suites of {c.sha[:9]}\n{tail(out, 3)}", "check `gh auth status` and rerun; the #790 trap is not excluded")
    hits = validation_suites(payload)
    s.data.update(suites=len(payload.get("check_suites", [])), validation=hits)
    if hits:
        names = ", ".join(sorted({h["branch"] for h in hits}))
        return s.fail(f"head {c.sha[:9]} already has check suites from {names}: its PR checks would attach there and the PR sits BLOCKED (#790)",
                      "make a new head (an empty commit `git commit --allow-empty` changes the SHA), rerun this preflight, and validate only "
                      "throwaway commits (`sm head validate`), never the head itself")
    s.detail = f"no validation/* check suite on {c.sha[:9]} ({s.data['suites']} suite(s) in total)"
    return s


# ---- parity with the pre-push hook and the first CI lane (types, standards, related)
# What the Core pre-push hook (`make check-fast`, driven by scripts/scoped_checks.py) and the first CI lane check that the
# other preflight steps do not. Three checks, each a step of `sm head preflight`:
#   types      the type checks of the hook, over the whole project, when a Python file under src/, tests/ or scripts/
#              changed: the commands are read from the hook script (`TYPES` in scripts/scoped_checks.py: mypy and
#              pyright, strict), else `uv run mypy` and `uv run pyright`. Each one failing fails the step.
#   standards  the repository-wide standards tests, always. The list is the Core file `scripts/preflight_standards.txt`
#              when it exists (one glob per line, `#` comments; every glob must match a file and the list may not be
#              empty, else FAIL), else the setting `preflight_standards` (a glob that matches nothing is a WARN; no
#              file at all is a FAIL). The hook does not read either list: it is this tool's own always-run set.
#   related    the unit tests the hook selects: Core's own `scripts/related_tests.py` run on the changed files, and
#              exactly its selection, uncapped. "ALL" (it cannot tell) runs the unit suite as the hook does, or fails.
#              A Core without that script falls back to an `ast` import scan. Added to both: the integration tests that
#              name a changed CLI or access module, the e2e output rules for a changed web/e2e driver and the
#              documentation test for changed CLI help (at most RELATED_CAP of these extras, the skipped ones named).
#              Tests the SEL already lists are not run twice.

STANDARDS_FILE = "scripts/preflight_standards.txt"
DEFAULT_STANDARDS = (
    "web/src/test/*standards*.test.ts",
    "tests/unit/**/standards_test.py",
    "tests/unit/**/*logging_standards_test.py",
    "tests/unit/**/terminal*standards*_test.py",
    "tests/unit/documentation_test.py",
    "tests/unit/public_api_test.py",
)
WEB_INPUTS = ("web/", "desktop/")  # the hook's own list (scoped_checks.py): a change there runs its whole fast gate
HOOK_SCRIPT, RELATED_SCRIPT = "scripts/scoped_checks.py", "scripts/related_tests.py"
# The hook runs the type checks for a change that is not web, desktop, plugins or documents only (scoped_checks.py,
# python_overrides). This step also runs them for the root config files (pyproject.toml, uv.lock, Makefile): the hook
# treats those as web inputs, but a pyright or mypy setting there can break every file, and the run is cheap.
TYPES_EXEMPT = re.compile(r"(?:web/|desktop/|plugins/|docs/|[^/]*\.md$|mkdocs\.yml$)")
DEFAULT_TYPES = (("mypy", ["uv", "run", "mypy"]), ("pyright", ["uv", "run", "pyright"]))
UNIT_SUITE = ["uv", "run", "pytest", "-m", "unit and not perf", "-n", "2", "-q", "-p", "no:cacheprovider"]  # the hook's unit run (make check-fast)
RELATED_CAP = 30  # extras the related step runs at most; the hook's own selection is never capped
E2E_RULES = "tests/unit/e2e_browser_output_test.py"
DOCS_TEST = "tests/unit/documentation_test.py"
SRC = re.compile(r"^src/heinzel/.*\.py$")
ACCESS = re.compile(r"^src/heinzel/(?:heads/(?:cli|mcp|api|web)/|modules/[a-z_]+/(?:[a-z_]*access[a-z_]*|runner)[/.])")
DOC_SURFACE = re.compile(r"^src/heinzel/(?:heads/cli/|modules/[a-z_]+/(?:operations?|ops)[/.]|modules/[a-z_]+/operation)")
WEB_TESTS_CMD = ["uv", "run", "--quiet", "--script", "scripts/web_build.py", "exec", "vitest", "run"]


def standards_globs(wt: Path, configured: list[str]) -> tuple[list[str], bool]:
    """The globs and whether they come from the Core file."""
    file = wt / STANDARDS_FILE
    if file.is_file():
        lines = (line.split("#", 1)[0].strip() for line in file.read_text(encoding="utf-8").splitlines())
        return [line for line in lines if line], True
    return list(configured or DEFAULT_STANDARDS), False


def glob_files(wt: Path, pattern: str) -> list[str]:
    return sorted(str(p.relative_to(wt)) for p in wt.glob(pattern) if p.is_file() and "node_modules" not in p.parts)


def standards_files(wt: Path, globs: list[str]) -> list[str]:
    return sorted({f for pattern in globs for f in glob_files(wt, pattern)})


def module_of(path: str) -> str:
    parts = Path(path).with_suffix("").parts[1:]  # drop `src/`
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def import_names(text: str, module: str = "", is_package: bool = False) -> set[str]:
    """Every dotted name a file imports, read with `ast`: relative imports are resolved against `module`; `from a import b`
    gives a and a.b (b may be a module)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - (node.level - 1)] if package else []
                base = ".".join([*parts, *([node.module] if node.module else [])])
            if base:
                names.add(base)
                names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def importers(wt: Path, changed: list[str]) -> list[str]:
    """The fallback for a Core without related_tests.py: test files that import a changed module, followed through the
    source modules that import it (transitive, like Core's graph), `__init__` re-exports and relative imports included."""
    sources = {module_of(str(p.relative_to(wt))): p for p in (wt / "src/heinzel").rglob("*.py")}
    tests = [p for p in (wt / "tests/unit").rglob("*_test.py")]
    imports = {p: import_names(p.read_text(encoding="utf-8", errors="replace"), module_of(str(p.relative_to(wt))) if p in sources.values() else "", p.name == "__init__.py")
               for p in [*sources.values(), *tests]}
    reached = {module_of(c) for c in changed}
    queue = list(reached)
    while queue:
        mod = queue.pop()
        for name, p in sources.items():
            if name not in reached and any(i == mod or i.startswith(mod + ".") for i in imports[p]):
                reached.add(name)
                queue.append(name)
    hit = [str(p.relative_to(wt)) for p in tests if any(i in reached for i in imports[p])]
    hit += [str(p.relative_to(wt)) for p in tests if p.name.removesuffix("_test.py") in {r.rpartition(".")[2] for r in reached}]
    return sorted(set(hit))


def named_by(wt: Path, changed: list[str]) -> list[str]:
    """Integration tests that name a changed CLI or access module (its file stem as a word)."""
    stems = {Path(p).stem for p in changed if ACCESS.match(p) and Path(p).stem not in ("__init__", "main") and len(Path(p).stem) >= 4}
    if not stems:
        return []
    word = re.compile(r"\b(?:" + "|".join(re.escape(s) for s in sorted(stems)) + r")\b")
    return sorted(str(p.relative_to(wt)) for p in (wt / "tests/integration").rglob("*_test.py") if word.search(p.read_text(encoding="utf-8", errors="replace")))


def extra_tests(wt: Path, changed: list[str]) -> list[str]:
    """Beyond the hook's selection: the integration tests of a changed CLI or access path, the e2e output rules, the documentation test."""
    src = [p for p in changed if SRC.match(p) and (wt / p).is_file()]
    rules = set(named_by(wt, src))
    if any(p.startswith("web/e2e/") and p.endswith(".mjs") for p in changed):
        rules.add(E2E_RULES)
    if any(DOC_SURFACE.match(p) for p in changed) or any(p.startswith("docs/") and "CLI_REFERENCE" in p for p in changed):
        rules.add(DOCS_TEST)
    return sorted(t for t in rules if (wt / t).is_file())


def hook_types(wt: Path) -> tuple[list[tuple[str, list[str]]], bool]:
    """The hook's type-check commands, read from its script (`TYPES = '"&mypy=uv run mypy" ...'`), else the defaults; and
    whether they were read (False: the script exists but its TYPES line could not be parsed)."""
    script = wt / HOOK_SCRIPT
    found = re.search(r"^TYPES\s*=\s*'(.*)'", script.read_text(encoding="utf-8"), re.M) if script.is_file() else None
    commands = [(m[1], shlex.split(m[2])) for m in re.finditer(r'"&?([\w-]+)=([^"]+)"', found[1])] if found else []
    return (commands, True) if commands else ([(name, list(cmd)) for name, cmd in DEFAULT_TYPES], not script.is_file())


def step_types(c: Ctx) -> Step:
    s = Step("types")
    files = [f for f in c.changed(filter_="ACMRD") if not TYPES_EXEMPT.match(f)]
    if not files:
        s.detail = "only documents, web/, desktop/ or plugins/ changed: the hook runs no type check"
        return s
    commands, read = hook_types(c.wt)
    if not read:
        s.warn(f"could not read TYPES from {HOOK_SCRIPT}: the default mypy and pyright commands ran")
    for name, cmd in commands:
        rc, out = c.gate(cmd, cwd=c.wt)
        log = c.log(name, out)
        if rc == 75:
            return s.fail(f"the run lock gave no slot for {name} (exit 75 = WAIT)", "wait for load to drop and rerun; do not bypass the run lock")
        if rc != 0:
            s.fail(f"{name} failed over the project (exit {rc}); the pre-push hook runs it too{'; log ' + log if log else ''}\n{tail(out)}",
                   f"fix the reported errors (`{' '.join(cmd)}` reproduces; pyright reportPrivateUsage: no access to a `_name` of another module)")
    s.detail = f"{', '.join(name for name, _ in commands)} clean over the project ({len(files)} changed Python file(s))" if s.status != FAIL else ""
    return s


def run_tests(c: Ctx, s: Step, label: str, files: list[str]) -> None:
    py = [f for f in files if f.endswith(".py")]
    web = [f for f in files if f.endswith(".ts")]
    for cmd, group in ((["uv", "run", "pytest", "-q", "-n", "2", "-p", "no:cacheprovider", *py], py),
                       ([*WEB_TESTS_CMD, "--maxWorkers=2", *[f.removeprefix("web/") for f in web]], web)):
        if group:
            run_group(c, s, label, cmd, group)


def run_group(c: Ctx, s: Step, label: str, cmd: list[str], group: list[str]) -> None:
    rc, out = c.gate(cmd, cwd=c.wt)  # from the repository root: the Vitest script path is relative to it
    if rc == 75:
        s.fail(f"the run lock gave no slot for the {label} tests (exit 75 = WAIT)", "wait for load to drop and rerun; do not bypass the run lock")
    elif rc != 0:
        s.fail(f"{label} tests failed (exit {rc}): {', '.join(group) or 'the unit suite'}; log {c.log(label, out) or '-'}\n{tail(out)}",
               "fix the code, never the test; the pre-push hook and the first CI lane run these on every head")


def step_standards(c: Ctx) -> Step:
    s = Step("standards")
    globs, from_core = standards_globs(c.wt, c.standards)
    empty = [g for g in globs if not glob_files(c.wt, g)]
    files = standards_files(c.wt, globs)
    if not globs or not files:
        return s.fail(f"no standards test to run: {'the Core file ' + STANDARDS_FILE + ' lists no pattern' if from_core and not globs else 'no pattern matches a file'}"
                      f"{' (' + ', '.join(globs) + ')' if globs else ''}", f"list the repository-wide standards tests in {STANDARDS_FILE}, or fix the setting `preflight_standards`")
    if empty and from_core:
        s.fail(f"{STANDARDS_FILE} lists a pattern that matches no file: {', '.join(empty)}", "correct or remove the stale pattern; a standards list that runs less than it names must not pass")
    elif empty:
        s.warn(f"standards pattern(s) with no file in this checkout: {', '.join(empty)}")
    if s.status != FAIL:
        run_tests(c, s, "standards", files)
    s.detail = f"{len(files)} repository-wide standards test file(s)" if s.status != FAIL else ""
    return s


def touches_web(changed: list[str]) -> bool:
    return any(p.startswith(WEB_INPUTS) for p in changed)


TEST_LINE = re.compile(r"tests/unit/.*_test\.py")
PIN_TEST = "tests/unit/scoped_checks_test.py"  # what the hook runs when nothing is related (scoped_checks.python_overrides)


def hook_selection(c: Ctx, changed: list[str]) -> tuple[list[str] | None, str]:
    """Core's own related_tests.py on the changed files, read from its standard output ONLY (a tool notice on standard error,
    such as uv's VIRTUAL_ENV warning, must never hide the answer): (tests, "") ; (None, "") for ALL; (None, why) for anything else.
    The script prints ALL, or one test path per line, or nothing; every other line is an error, never "no tests"."""
    rc, out = c.sh(["sh", "-c", 'exec "$@" 2>/dev/null', "sh", "uv", "run", "--quiet", "python", RELATED_SCRIPT, *changed], cwd=c.wt)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if rc != 0:
        return None, f"{RELATED_SCRIPT} failed (exit {rc}); run `uv run python {RELATED_SCRIPT} FILES` in the worktree to see why"
    if "ALL" in lines:
        return (None, "") if lines == ["ALL"] else (None, f"{RELATED_SCRIPT} printed ALL among other lines: {tail(out)}")
    odd = [line for line in lines if not TEST_LINE.fullmatch(line)]
    if odd:
        return None, f"{RELATED_SCRIPT} printed something that is no test path: {', '.join(odd[:3])}"
    return lines, ""


def step_related(c: Ctx) -> Step:
    s = Step("related")
    changed = c.changed()
    listed = set((c.sel or {}).get("python") or [])
    chosen: list[str] = []
    if not (c.wt / RELATED_SCRIPT).is_file():
        sources = [p for p in changed if SRC.match(p) and (c.wt / p).is_file()]
        chosen = importers(c.wt, sources) if sources else []
        s.warn(f"Core has no {RELATED_SCRIPT}: the selection is this tool's own import scan, which may differ from the hook's")
    else:
        if touches_web(changed):  # the hook then runs the whole fast gate (every unit test and Vitest): too heavy here, so say so
            s.warn("the push hook runs the whole web gate; run `sm head validate --lanes web-1,web-2`")
        picked, why = hook_selection(c, [p for p in changed if not p.startswith(WEB_INPUTS)]) if any(not p.startswith(WEB_INPUTS) for p in changed) else ([], "")
        if why:
            return s.fail(why, "run it by hand in the worktree and fix the cause; the selection must come from the hook's own script")
        if picked is not None:
            chosen = picked
        else:
            rc, out = c.gate(UNIT_SUITE, cwd=c.wt)
            s.data["selection"] = "ALL"
            if rc != 0:
                outcome = "got no slot (WAIT)" if rc == 75 else "was refused or failed"
                s.fail(f"the hook cannot map this change to tests (related_tests: ALL) and runs the whole unit suite; it {outcome} here (exit {rc})\n{tail(out)}",
                       "rerun when the run lock has a slot, or let the first CI lane run the unit suite; never skip it")
    if not chosen and "selection" not in s.data and (c.wt / RELATED_SCRIPT).is_file() and (c.wt / PIN_TEST).is_file():
        chosen = [PIN_TEST]  # nothing related: the hook still runs its own test
    extras = [t for t in extra_tests(c.wt, changed) if t not in chosen]
    run_extras, skipped = extras[:RELATED_CAP], extras[RELATED_CAP:]
    todo = [t for t in [*chosen, *run_extras] if t not in listed]
    if todo:
        run_tests(c, s, "related", todo)
    s.data.update(tests=todo, hook=chosen, skipped=skipped)
    if skipped:
        s.warn(f"the cap of {RELATED_CAP} extra test files left {len(skipped)} unrun; list them in SEL to run them: {', '.join(skipped)}")
    s.detail = f"{len(todo)} test file(s) beyond the SEL (the hook's selection {len(chosen)}, extras {len(run_extras)})" if s.status != FAIL else ""
    return s


# ---- the run ------------------------------------------------------------------------------------------------------

def checks(consumers: str | None = None, target: str = "check", plugins: Path | None = None) -> Preflight:
    """The checks of a Core head, registered in the order they run."""
    table = {"tree": step_tree, "lint": step_lint, "typos": step_typos,
             "types": step_types, "standards": step_standards, "related": step_related, "meta": step_meta, "coverage": step_coverage, "dup": step_dup, "smells": step_smells,
             "deps": lambda ctx: step_deps(ctx, consumers, target, plugins), "e2e": step_e2e, "validation": step_validation}
    registry = Preflight()
    for name in STEPS:
        registry.add(name, table[name])
    return registry


def run(c: Ctx, skip: set[str], consumers: str | None = None, target: str = "check",
        plugins: Path | None = None, say=lambda text: None) -> tuple[list[Step], list[str], list[str]]:
    c.sha = c.git("rev-parse", "HEAD")[1].strip()
    c.base_sha = c.git("rev-parse", BASE)[1].strip()
    branch = c.git("rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    c.branch = "" if branch == "HEAD" else branch
    not_run = [] if consumers or "deps" in skip else ["consumers"]
    not_run.append("hosted")  # the hosted lanes are never run here: `sm head validate` runs them
    return checks(consumers, target, plugins).run(c, skip, not_run=not_run, say=say)


def select(c: Ctx, sel: str | None, local_only: str | None) -> None:
    """Read the selection and the local-only list into the context. Unreadable: OSError, ValueError or AttributeError."""
    if sel:
        c.sel_path = Path(sel).expanduser().resolve()
        c.sel_bytes = c.sel_path.read_bytes()  # read once: an edit during the run changes nothing this preflight checks
        c.sel = json.loads(c.sel_bytes.decode())
        c.local_only = local_only_of(c.sel.get("local_only"))
    if local_only:
        c.local_only.update(local_only_of(json.loads(Path(local_only).expanduser().read_text())))


def title(c: Ctx) -> str:
    return f"sm head preflight {c.sha[:9]} ({c.branch or 'detached'}) vs {BASE} {c.base_sha[:9]}"


def hosted_wanted(changed: list[str]) -> bool:
    """Whether the head touches what only the hosted e2e lanes prove: the web client, its drivers or the journeys."""
    return any(p.startswith(("web/", "tests/e2e/")) for p in changed)

