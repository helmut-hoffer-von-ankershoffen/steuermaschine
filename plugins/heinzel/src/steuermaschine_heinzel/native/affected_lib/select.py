"""Changed files -> the minimal pytest paths, Vitest files, E2E lanes and CI jobs for a core pull request.

Conservative on purpose (owner 2026-11-07: selection never weakens a test, unsure means run everything): workflows,
Makefile, pyproject, the lockfile, scripts, the kernel, installers, the updater, the daemon, credentials, release code,
shared test fixtures and any path the map does not know select the FULL set. Everything else narrows:
  docs and Markdown only      -> site tests (make test-site), no app jobs
  web/src, web/e2e            -> the Vitest files that import the change (the core's import graph), the browser E2E
                                 lanes, lint (biome, tsc, vitest); no Python matrix, no coverage, no CLI lane
  modules/*, heads/* (python) -> unit and integration test files that import the change (scripts/related_tests.py's
                                 graph) plus the tests named after it, the Linux test cell, coverage and the E2E lanes
                                 the head needs; macOS smoke only for native paths
  tests/*                     -> the changed test files themselves (and their lane)
  desktop/                    -> Electron; plugins/ -> the source plugin row
The map reads the checkout's own Makefile lanes and tests, so it follows the repository rather than a copy of its rules.

Moved from the former selector: everything from JOBS_FULL to `classify`, `render` and CASES is the original code
(check: plugins/heinzel/tests/coord_verbatim.py). `changed_from_git` runs git through the one process runner and
`self_test` returns its lines instead of printing them. As before, `Repo.related` imports the checkout's own
scripts/related_tests.py: selecting tests runs that one module of the checkout.
"""
# mypy: disable-error-code="no-untyped-def,no-untyped-call,type-arg,attr-defined,import-not-found"

from __future__ import annotations

import functools
import os
import re
import sys
from pathlib import Path
from typing import Any


class RepoError(Exception):
    """The checkout is unusable as the core repository (no Makefile, no tests), or git failed."""


# Every CI job the ordinary pull request of a core change can start (the CI workflow). "full" is all of them.
JOBS_FULL = [
    "lint",
    "setup",
    "audit",
    "test-linux",
    "coverage",
    "perf",
    "e2e-cli",
    "e2e-supervised",
    "e2e-web-1",
    "e2e-web-2",
    "macos-smoke",
    "macos-e2e",
    "electron",
    "template",
    "core-wheel",
    "consumers",
    "build",
]
JOBS_ALWAYS = ["commits", "dependency-review", "secrets"]  # cheap, every pull request

# Paths whose change selects the full set (owner rule: workflows, scripts, tools, kernel, security, installers, updater,
# release, lockfiles; plus the shared test harness, build metadata and the template the plugins are generated from).
FULL = re.compile(
    r"^(?:\.github/|Makefile$|pyproject\.toml$|uv\.lock$|hatch_build\.py$|copier\.yml$|template/|starter/"
    r"|scripts/(?!site_[a-z_]+\.py$|check_docs\.py$)"
    r"|src/heinzel/__init__\.py$|src/heinzel/__main__\.py$|src/heinzel/_lifecycle_control\.py$|src/_heinzel_launcher\.py$"
    r"|src/heinzel/kernel/|src/heinzel/testing\.py$"
    r"|src/heinzel/heads/(?:install|bootstrap|daemon_recovery|_desktop_[a-z]+|_tool_install|_prerequisites|_presence|_exposure)\.py$"
    r"|src/heinzel/heads/daemon/|src/heinzel/heads/web/_(?:auto_update|supervision|lifecycle|service)\.py$"
    r"|src/heinzel/modules/system/(?:daemon|lifecycle|credential|plugin|prerequisite|runner|layer|doctor)/"
    r"|src/heinzel/modules/system/notify/macos/"
    r"|tests/conftest\.py$|tests/_[a-z_]+\.py$|tests/fixtures/|tests/unit/conftest\.py$|tests/integration/conftest\.py$"
    r"|tests/e2e/conftest\.py$|tests/e2e/_[a-z_]+\.py$"
    r"|web/package\.json$|web/pnpm-lock\.yaml$|web/vite\.config\.ts$|web/tsconfig\.json$|web/biome\.json$"
    r"|web/src/test/|web/src/main\.tsx$|web/src/App\.tsx$|web/index\.html$"
    r"|fuzz/|agent-skills/|design/tokens|\.pre-commit|\.gitleaks|SECURITY\.md$)"
)
DOCS = re.compile(r"^(?:docs/(?!.*\.sh$).*|[^/]*\.md|mkdocs\.yml|scripts/(?:site_[a-z_]+|check_docs)\.py|design/README\.md)$")
SITE_TEST_FILES = re.compile(r"^tests/unit/(?:site_[a-z_]+|docs_style|documentation|standards|repository|pypi_description|readme_diagnostics)_test\.py$")
DESKTOP = re.compile(r"^desktop/")
PLUGINS = re.compile(r"^plugins/")
WEB_SRC = re.compile(r"^web/src/.*\.(?:ts|tsx|css)$")
WEB_E2E = re.compile(r"^web/(?:e2e/.*\.(?:mjs|html|tsx)|public/.*)$")
WEB_OPENAPI = re.compile(r"^web/openapi\.json$")
PY_SRC = re.compile(r"^src/heinzel/(?:modules|heads)/.*\.py$")
PY_TEST = re.compile(r"^tests/(unit|integration|e2e)/.*_test\.py$")
PY_SUPPORT = re.compile(r"^tests/(unit|integration|e2e)/.*\.py$")  # a helper next to the tests: run its directory
NATIVE_PY = re.compile(r"src/heinzel/(?:heads/(?:cli|web)/|modules/(?:ui|system/(?:notify|sound|theme)/))")
CLI_LANE_PY = re.compile(r"src/heinzel/heads/(?:cli/|mcp/|api/)")

# Sites tests, as `make test-site` lists them; read from the Makefile when present.
SITE_TESTS_DEFAULT = [
    "tests/unit/documentation_test.py",
    "tests/unit/standards_test.py",
    "tests/unit/repository_test.py",
    "tests/unit/pypi_description_test.py",
    "tests/unit/readme_diagnostics_test.py",
    "tests/unit/docs_style_test.py",
    "tests/unit/site_catalog_test.py",
    "tests/unit/site_domain_test.py",
    "tests/integration/guides_test.py",
]
# Tests that read the repository's Markdown: a docs change runs them, a code change does not (they are in test-site).
DOCS_READERS = SITE_TESTS_DEFAULT

# Shared web files: when one of these changes, more than half of the Vitest files import it; run them all (it is
# one job of a minute or two) rather than list 80 files.
WEB_SHARED = re.compile(r"^web/src/(?:i18n\.ts|index\.css|api/|lib/|components/ui/|splash/|lifecycle/)")


class Repo:
    """The core checkout the map is read from: its Makefile lanes, tests and import graphs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        if not (root / "Makefile").is_file() or not (root / "src/heinzel").is_dir():
            msg = f"{root} is not a core checkout (no Makefile or src/heinzel); pass --repo"
            raise RepoError(msg)
        self._related = None

    # -- Python ---------------------------------------------------------------------------------------------------
    @functools.cached_property
    def related(self):
        """The core's own import graph (scripts/related_tests.py), loaded from the checkout."""
        import importlib.util

        script = self.root / "scripts/related_tests.py"
        # Loaded from this checkout's file under its own name: a plain `import` returns whichever checkout came first.
        spec = importlib.util.spec_from_file_location(f"related_tests_{abs(hash(str(self.root)))}", script)
        if spec is None or spec.loader is None:
            msg = f"{script} cannot be loaded"
            raise RepoError(msg)
        related_tests = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(self.root / "scripts"))
        cwd = os.getcwd()
        os.chdir(self.root)  # the graph resolves files against ROOT = scripts/..; it uses absolute paths, chdir is belt and braces
        try:
            spec.loader.exec_module(related_tests)
            related_tests.ROOT = self.root
            return related_tests
        finally:
            os.chdir(cwd)

    @staticmethod
    def graph_files(graph) -> set[str]:
        """The files the graph knows: `known` in the current core, `trees` in older ones, else `files`."""
        for name in ("known", "trees", "files"):
            found = getattr(graph, name, None)
            if found is not None:
                return set(found)
        return set()

    def python_tests_for(self, changed: list[str]) -> list[str]:
        """Unit and integration test files that import a changed source file, or are named after it."""
        graph = self.related.graph()
        known = self.graph_files(graph)
        existing = {path for path in changed if path in known}
        reached = graph.dependents(existing)
        tests = {p for p in reached if re.fullmatch(r"tests/(?:unit|integration)/.*_test\.py", p)}
        for path in changed:
            stem = Path(path).stem
            tests |= {t for t in graph.files if re.fullmatch(r"tests/(?:unit|integration)/.*_test\.py", t) and t.endswith(f"/{stem}_test.py")}
        return sorted(t for t in tests if (self.root / t).is_file())

    # -- E2E lanes ------------------------------------------------------------------------------------------------
    @functools.cached_property
    def lanes(self) -> dict[str, list[str]]:
        """E2E lane -> its pytest files, from the Makefile's E2E_* variables (web-1 = web_ui_test, web-2 = the rest)."""
        text = (self.root / "Makefile").read_text(encoding="utf-8")
        supervised = re.search(r"^E2E_SUPERVISED\s*:=\s*(.*)$", text, re.M)
        supervised_files = supervised[1].split() if supervised else []
        all_e2e = sorted(str(p.relative_to(self.root)) for p in (self.root / "tests/e2e").glob("*_test.py"))
        candidates = re.search(r"^E2E_WEB_CANDIDATES\s*:=\s*(.*)$", text, re.M)
        if candidates and "$(" not in candidates[1]:
            web_candidates = candidates[1].split()
        else:
            # Browser tests are the ones whose module drives a web/e2e script.
            web_candidates = [f for f in all_e2e if re.search(r"[a-z0-9-]+\.mjs", (self.root / f).read_text(encoding="utf-8", errors="replace"))]
        web = [f for f in web_candidates if f not in supervised_files]
        web_1 = [f for f in web if f == "tests/e2e/web_ui_test.py"]
        web_2 = [f for f in web if f not in web_1]
        cli = [f for f in all_e2e if f not in web and f not in supervised_files]
        return {"cli": cli, "supervised": supervised_files, "web-1": web_1, "web-2": web_2}

    @functools.cached_property
    def script_owners(self) -> dict[str, list[str]]:
        """web/e2e/<name>.mjs -> the tests/e2e files that run it."""
        owners: dict[str, list[str]] = {}
        for f in (self.root / "tests/e2e").glob("*.py"):
            text = f.read_text(encoding="utf-8", errors="replace")
            for name in set(re.findall(r"([a-z0-9-]+\.mjs)", text)):
                owners.setdefault(f"web/e2e/{name}", []).append(str(f.relative_to(self.root)))
        return owners

    def lane_of(self, test_file: str) -> str | None:
        return next((lane for lane, files in self.lanes.items() if test_file in files), None)

    # -- Vitest ---------------------------------------------------------------------------------------------------
    @functools.cached_property
    def web_graph(self) -> tuple[list[str], dict[str, set[str]]]:
        """web/src files and who imports whom (`@/` alias and relative imports; `.d.ts` resolved like modules)."""
        root = self.root / "web/src"
        files: list[str] = []
        for p in root.rglob("*"):
            if p.suffix in {".ts", ".tsx"} and "node_modules" not in p.parts:
                files.append(str(p.relative_to(self.root)))
        pattern = re.compile(r"(?:import|export)\s[^;]*?from\s+[\"']([^\"']+)[\"']|import\s*\(\s*[\"']([^\"']+)[\"']\s*\)|import\s+[\"']([^\"']+)[\"']")

        def resolve(src: str, spec: str) -> str | None:
            if spec.startswith("@/"):
                base = self.root / "web/src" / spec[2:]
            elif spec.startswith("."):
                base = (self.root / src).parent / spec
            else:
                return None
            base = Path(os.path.normpath(base))
            for c in (base, base.with_suffix(".ts"), base.with_suffix(".tsx"), Path(str(base) + ".d.ts"), base / "index.ts", base / "index.tsx"):
                if c.is_file():
                    return str(c.relative_to(self.root))
            return None

        reverse: dict[str, set[str]] = {}
        for f in files:
            text = (self.root / f).read_text(encoding="utf-8", errors="replace")
            for m in pattern.finditer(text):
                target = resolve(f, m.group(1) or m.group(2) or m.group(3) or "")
                if target:
                    reverse.setdefault(target, set()).add(f)
        return files, reverse

    def vitest_for(self, changed: list[str]) -> list[str] | None:
        """Vitest files importing a changed web file; None means all of them (a shared file changed)."""
        if any(WEB_SHARED.match(p) for p in changed):
            return None
        files, reverse = self.web_graph
        reached, queue = set(changed), list(changed)
        while queue:
            for importer in reverse.get(queue.pop(), ()):
                if importer not in reached:
                    reached.add(importer)
                    queue.append(importer)
        tests = {f for f in reached if ".test." in f}
        for p in changed:  # a sibling test named after the file, even without an import (e.g. CSS modules)
            stem = Path(p).name.split(".")[0]
            tests |= {f for f in files if ".test." in f and Path(f).name.split(".")[0] == stem}
        return sorted(t for t in tests if (self.root / t).is_file())


def classify(repo: Repo, changed: list[str]) -> dict:
    """The selection for a list of changed paths."""
    changed = sorted({p.strip() for p in changed if p.strip()})
    reasons: list[str] = []
    result = {
        "files": changed,
        "full": False,
        "pytest": [],  # paths for `uv run pytest` (unit and integration), [] when none, ["ALL"] for the whole suite
        "vitest": [],  # files for `vitest run`, ["ALL"] for the whole suite, [] when none
        "e2e_lanes": [],  # cli, supervised, web-1, web-2
        "jobs": [],  # CI jobs of the CI workflow beyond the always-on ones
        "reasons": reasons,
    }
    if not changed:
        reasons.append("no changed files: nothing to run")
        result["jobs"] = list(JOBS_ALWAYS)
        return result

    full_hits = [p for p in changed if FULL.match(p)]
    unknown = [
        p
        for p in changed
        if not (FULL.match(p) or DOCS.match(p) or DESKTOP.match(p) or PLUGINS.match(p) or WEB_SRC.match(p) or WEB_E2E.match(p) or WEB_OPENAPI.match(p) or PY_SRC.match(p) or PY_SUPPORT.match(p) or SITE_TEST_FILES.match(p))
    ]
    if full_hits or unknown:
        for p in full_hits:
            reasons.append(f"{p}: shared/sensitive path, selects the full set")
        for p in unknown:
            reasons.append(f"{p}: not in the map, selects the full set")
        result.update(full=True, pytest=["ALL"], vitest=["ALL"], e2e_lanes=["cli", "supervised", "web-1", "web-2"], jobs=[*JOBS_ALWAYS, *JOBS_FULL])
        return result

    jobs: set[str] = set(JOBS_ALWAYS)
    pytest: set[str] = set()
    lanes: set[str] = set()
    vitest: set[str] | None = set()

    docs = [p for p in changed if DOCS.match(p) or SITE_TEST_FILES.match(p)]
    if docs:
        pytest.update(DOCS_READERS)
        pytest.update(p for p in docs if SITE_TEST_FILES.match(p))
        jobs.add("site-tests")
        reasons.append(f"{len(docs)} document/site file(s): site tests")

    py_src = [p for p in changed if PY_SRC.match(p)]
    py_tests = [p for p in changed if PY_TEST.match(p) and not SITE_TEST_FILES.match(p)]
    py_support = [p for p in changed if PY_SUPPORT.match(p) and not PY_TEST.match(p)]
    if py_src or py_tests or py_support:
        jobs.update({"lint", "test-linux", "coverage", "perf", "setup", "audit", "core-wheel", "build"})
        if py_src:
            related = repo.python_tests_for(py_src)
            pytest.update(related)
            reasons.append(f"{len(py_src)} python source file(s): {len(related)} unit/integration test file(s) import or are named after them")
            # The API serves the browser: any module or head change can change what the web E2E lanes see.
            lanes.update({"web-1", "web-2"})
            if any(CLI_LANE_PY.search(p) for p in py_src):
                lanes.add("cli")
            if any(re.search(r"heads/(?:web|cli)/|modules/ui/", p) for p in py_src):
                lanes.add("supervised")
            if any(NATIVE_PY.search(p) for p in py_src):
                jobs.add("macos-smoke")
            if any(re.search(r"src/heinzel/modules/system/ui/|src/heinzel/heads/", p) for p in py_src):
                jobs.add("consumers")  # the contracts rule of the CI workflow
        for t in py_tests:
            if t.startswith("tests/e2e/"):
                lane = repo.lane_of(t)
                if lane:
                    lanes.add(lane)
                else:
                    lanes.update({"cli", "supervised", "web-1", "web-2"})
                    reasons.append(f"{t}: not in a known E2E lane, all lanes")
            else:
                pytest.add(t)
        for s in py_support:
            directory = str(Path(s).parent)
            if directory.startswith("tests/e2e"):
                lanes.update({"cli", "supervised", "web-1", "web-2"})
            else:
                pytest.add(directory + "/")
            reasons.append(f"{s}: test helper, its directory runs")

    web_src = [p for p in changed if WEB_SRC.match(p)]
    web_e2e = [p for p in changed if WEB_E2E.match(p)]
    openapi = [p for p in changed if WEB_OPENAPI.match(p)]
    if web_src or web_e2e or openapi:
        jobs.update({"lint", "build"})  # biome, tsc and vitest run inside the lint job
        if web_src:
            selected = repo.vitest_for(web_src)
            if selected is None:
                vitest = None
                reasons.append("a shared web file changed (i18n, api, lib, ui, splash, lifecycle): every Vitest file")
            else:
                vitest = (vitest or set()) | set(selected)
                reasons.append(f"{len(web_src)} web source file(s): {len(selected)} Vitest file(s) import them")
            lanes.update({"web-1", "web-2"})
            if any(re.search(r"web/src/(?:updates|lifecycle|pwa)/", p) for p in web_src):
                jobs.add("macos-smoke")  # the auto-update and lifecycle paths are native-sensitive
            if any(re.search(r"web/src/plugins/", p) for p in web_src):
                jobs.add("consumers")
        for p in web_e2e:
            owners = repo.script_owners.get(p, [])
            if owners:
                for o in owners:
                    lanes.add(repo.lane_of(o) or "web-2")
            elif p.endswith(".test.mjs"):
                vitest = None if vitest is None else vitest | {p}
            else:
                lanes.update({"supervised", "web-1", "web-2"})
                reasons.append(f"{p}: no tests/e2e owner found, all browser lanes")
        if openapi:
            jobs.update({"test-linux", "coverage"})
            pytest.update(repo.python_tests_for(["src/heinzel/heads/api/__init__.py"]) or ["tests/integration/api_input_schema_test.py"])
            lanes.update({"web-1", "web-2"})
            reasons.append("web/openapi.json: the API's tests and both browser lanes")

    if any(DESKTOP.match(p) for p in changed):
        jobs.add("electron")
        reasons.append("desktop/: Electron")
    if any(PLUGINS.match(p) for p in changed):
        jobs.update({"core-wheel", "local-plugins"})
        reasons.append("plugins/: the source plugin row")

    if lanes:
        jobs.update(f"e2e-{lane}" for lane in sorted(lanes))
    result.update(
        pytest=sorted(pytest),
        vitest=["ALL"] if vitest is None else sorted(vitest),
        e2e_lanes=sorted(lanes),
        jobs=sorted(jobs),
    )
    return result


def render(result: dict) -> str:
    lines = [f"full: {'yes' if result['full'] else 'no'}"]
    lines.append("jobs: " + (" ".join(result["jobs"]) or "-"))
    lines.append("e2e lanes: " + (" ".join(result["e2e_lanes"]) or "-"))
    py = result["pytest"]
    lines.append(f"pytest ({len(py)}): " + (" ".join(py) if len(py) <= 12 else " ".join(py[:12]) + f" ... +{len(py) - 12}"))
    vt = result["vitest"]
    lines.append(f"vitest ({len(vt)}): " + (" ".join(vt) if len(vt) <= 12 else " ".join(vt[:12]) + f" ... +{len(vt) - 12}"))
    lines.extend("  - " + r for r in result["reasons"])
    return "\n".join(lines)


# Self-test: each case is (changed files, expectations). Expectations check containment, so the repo may grow.
CASES = [
    ("web-only page", ["web/src/pages/Home.tsx", "web/src/pages/Home.test.tsx"], {"full": False, "jobs_has": ["lint", "e2e-web-1"], "jobs_not": ["test-linux", "coverage", "e2e-cli"], "vitest_has": ["web/src/pages/Home.test.tsx"], "pytest": []}),
    ("web shared i18n", ["web/src/i18n.ts", "web/src/layout/SearchPalette.tsx"], {"full": False, "vitest": ["ALL"], "jobs_not": ["test-linux"]}),
    ("docs only", ["docs/guides/install.md", "README.md"], {"full": False, "jobs_has": ["site-tests"], "jobs_not": ["lint", "test-linux", "e2e-web-1"], "pytest_has": ["tests/unit/documentation_test.py"]}),
    ("kernel = full", ["src/heinzel/kernel/_session_store.py"], {"full": True, "pytest": ["ALL"]}),
    ("workflow = full", [".github/workflows/any.yml"], {"full": True}),
    ("lockfile = full", ["uv.lock"], {"full": True}),
    ("daemon module = full", ["src/heinzel/modules/system/daemon/_service.py"], {"full": True}),
    ("notify module", ["src/heinzel/modules/system/notify/_store.py", "tests/unit/modules/notify_recovery_test.py"], {"full": False, "jobs_has": ["test-linux", "coverage", "e2e-web-1", "macos-smoke"], "jobs_not": ["e2e-cli"], "pytest_has": ["tests/unit/modules/notify_recovery_test.py"]}),
    ("cli head", ["src/heinzel/heads/cli/_overview.py", "tests/unit/heads/cli_overview_test.py"], {"full": False, "jobs_has": ["e2e-cli", "e2e-supervised", "consumers"], "pytest_has": ["tests/unit/heads/cli_overview_test.py"]}),
    ("one unit test", ["tests/unit/standards_test.py"], {"full": False, "pytest_has": ["tests/unit/standards_test.py"], "jobs_has": ["site-tests"]}),
    ("integration test only", ["tests/integration/installation_test.py"], {"full": False, "pytest": ["tests/integration/installation_test.py"], "jobs_has": ["test-linux"], "jobs_not": ["e2e-web-1"]}),
    ("e2e script", ["web/e2e/mobile-responsive.mjs"], {"full": False, "e2e_lanes": ["web-1"], "jobs_not": ["test-linux"]}),
    ("e2e python test", ["tests/e2e/audio_visualizer_test.py"], {"full": False, "e2e_lanes": ["web-2"]}),
    ("desktop", ["desktop/src/main.js", "desktop/test/window.test.js"], {"full": False, "jobs_has": ["electron"], "jobs_not": ["test-linux", "lint"]}),
    ("conftest = full", ["tests/conftest.py"], {"full": True}),
    ("unknown path = full", ["something/new.txt"], {"full": True}),
    ("openapi", ["web/openapi.json", "web/src/api/schema.d.ts"], {"full": False, "jobs_has": ["test-linux", "e2e-web-2"], "vitest": ["ALL"]}),
    ("heinzel module", ["src/heinzel/modules/heinzel/_autonomous_schedule.py"], {"full": False, "pytest_has": ["tests/unit/modules/autonomous_schedule_test.py"], "jobs_has": ["e2e-web-1"], "jobs_not": ["e2e-cli", "macos-smoke"]}),
    ("empty", [], {"full": False, "jobs": list(JOBS_ALWAYS)}),
]


def changed_from_git(commands: Any, repo: Path, base: str, env: dict[str, str] | None = None) -> list[str]:
    """The files the checkout differs from `base` in: the three-dot diff plus the working tree."""
    out = commands.run(["git", "-C", str(repo), "diff", "--name-only", f"{base}...HEAD"], env=env, timeout=60)
    if out.rc != 0:
        raise RepoError(f"git diff failed: {out.err.strip()}")
    work = commands.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"], env=env, timeout=60)
    tree = [line[3:].split(" -> ")[-1] for line in work.out.splitlines() if line.strip()]
    return sorted(set(out.out.split()) | set(tree))


def self_test(repo: Repo) -> tuple[int, list[str]]:
    """(failures, lines): the built-in cases against the repository."""
    failures, lines = 0, []
    for name, files, want in CASES:
        got = classify(repo, files)
        problems = []
        for key, value in want.items():
            if key.endswith("_has"):
                missing = [v for v in value if v not in got[key[:-4]]]
                if missing:
                    problems.append(f"{key[:-4]} lacks {missing}")
            elif key.endswith("_not"):
                present = [v for v in value if v in got[key[:-4]]]
                if present:
                    problems.append(f"{key[:-4]} should not have {present}")
            elif got[key] != value:
                problems.append(f"{key} = {got[key]!r}, want {value!r}")
        if got["full"] is False and got["pytest"] == ["ALL"]:
            problems.append("pytest ALL without full")
        status = "ok " if not problems else "FAIL"
        lines.append(f"{status} {name}: {len(got['pytest'])} pytest, {len(got['vitest'])} vitest, lanes {got['e2e_lanes']}, {len(got['jobs'])} jobs")
        lines += [f"     {p}" for p in problems]
        failures += bool(problems)
    # The map must know every test file the repo has a lane for.
    unlaned = [f for f in sorted(str(p.relative_to(repo.root)) for p in (repo.root / "tests/e2e").glob("*_test.py")) if repo.lane_of(f) is None]
    if unlaned:
        lines.append(f"FAIL e2e files without a lane: {unlaned}")
        failures += 1
    lines.append(f"{len(CASES) - failures}/{len(CASES)} cases pass" if not failures else f"{failures} failing")
    return failures, lines
