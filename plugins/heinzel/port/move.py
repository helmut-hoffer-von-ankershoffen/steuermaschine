#!/usr/bin/env python3
"""Move tool sources into the plugin verbatim, changing only the edges, and verify that nothing else changed.

    port/move.py apply SOURCE_TOOLS_DIR     write every moved module and moved test from its source
    port/move.py verify SOURCE_TOOLS_DIR    exit 1 unless every moved file equals what the rules produce from its
                                            source; prints every changed line so a reviewer reads the whole delta

What moves is listed per directory in `src/steuermaschine_heinzel/native/<area>_lib/moved_map.py`: whole files (MOVED), parts of files
(EXTRACTS: named definitions plus everything they refer to, in source order, under the source's imports) and exact
replacements (PATCHES). Line rules, applied to every moved line; nothing else is touched:

  E1  `import X` / `import X as Y` / `from X import ...` where X moved          -> relative import of its module
  E2  `sys.path.insert(0, str(Path(__file__).resolve().parent))`                -> removed (replaced by the edge import)
  E3  `Path(__file__).resolve().parent.parent` (the old checkout of the tools)  -> edge.records_dir()
  E4  `subprocess.run(`                                                          -> edge.run(   (same arguments; the
      core's one process runner inside a native command, the plain call otherwise)
  E0  a module that uses `edge.` gets `from . import edge` after its imports
  A file listed as STANDALONE (it is also started as a program of its own, outside the package) gets no rule at all.

An `import X` of an old sibling that did not move is an error: nothing is loaded from a directory.
"""

from __future__ import annotations

import ast
import difflib
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent / "src" / "steuermaschine_heinzel" / "native"
TEST_DIR = HERE.parent / "tests" / "moved"
ROOT = "steuermaschine_heinzel.native"


def _table(path: Path):
    spec = importlib.util.spec_from_file_location(f"moved_map_{path.parent.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# lib directory -> its table of what moved there (MOVED, EXTRACTS, PATCHES, EDGE_LINES); the package reads the same files
MAPS = {path.parent.name: _table(path) for path in sorted(PKG.glob("*_lib/moved_map.py"))}

# moved tests: source test file -> new basename (unique repo-wide: test_native_<area>_...)
TESTS: dict[str, str] = {
    "test_fix_forward.py": "test_native_queue_fix_forward.py",
    "test_queue_wake.py": "test_native_queue_wake.py",
    "test_main_cancelled_timeout.py": "test_native_queue_main_cancelled_timeout.py",
    "test_main_rerun.py": "test_native_queue_main_rerun.py",
    "test_main_batch.py": "test_native_queue_main_batch.py",
    "test_merge_train.py": "test_native_queue_merge_train.py",
}
# exact replacements in moved tests beyond the import rule: (test file, old, new, why)
TEST_PATCHES: list[tuple[str, str, str, str]] = [
    (
        "test_queue_wake.py",
        """        "import importlib.machinery, sys; "
        f"sys.path.insert(0, {str(tools)!r}); "
        f"q=importlib.machinery.SourceFileLoader('queue_test', {str(tools / 'hz-queue')!r}).load_module(); "
""",
        """        "import sys; "
        f"sys.path[:0] = {[p for p in sys.path if p]!r}; "
        "from steuermaschine_heinzel.native.queue_lib import queue_daemon as q; "
""",
        "the child process imports the moved module instead of loading the script by its path",
    ),
]
for _table_ in MAPS.values():  # an area lists its moved tests, their patches and its copied fixtures in its own table
    TESTS.update(getattr(_table_, "TESTS", {}))
    TEST_PATCHES += getattr(_table_, "TEST_PATCHES", [])
FIXTURES: tuple[str, ...] = tuple(name for table in MAPS.values() for name in getattr(table, "FIXTURES", ()))  # byte for byte into tests/moved/fixtures/
# old sibling modules that are known and did not move: importing one from moved code is an error
OLD_SIBLINGS = re.compile(r"^(hz_\w+|hzlib|main_\w+|release_\w+|known_main_infra)$")


def old_module(src: str) -> str:
    return src.removesuffix(".py").replace("-", "_")


TARGETS: dict[str, tuple[str, str]] = {src: (lib, module) for lib, table in MAPS.items() for src, module in table.MOVED.items()}
TARGETS.update({src: (lib, spec[0]) for lib, table in MAPS.items() for src, spec in table.EXTRACTS.items()})
EXTRACTS = {src: spec for table in MAPS.values() for src, spec in table.EXTRACTS.items()}
PATCHES = [patch for table in MAPS.values() for patch in table.PATCHES]
STANDALONE = {src for table in MAPS.values() for src in getattr(table, "STANDALONE", {})}
BY_OLD = {old_module(src): src for src in sorted(TARGETS, key=lambda name: name.endswith(".py"))}  # `import x` means x.py when a script x exists too


def target(src: str) -> Path:
    area, mod = TARGETS[src]
    return PKG / area / f"{mod}.py"


def rewrite(line: str, area: str, src: str) -> str:
    if src in STANDALONE:  # started as a program of its own, outside the package: not one line changes
        return line
    body = line.rstrip("\n")
    m = re.match(r"^(\s*)import (\w+)( as \w+)?(\s*#.*)?$", body)
    if m and m[2] in BY_OLD:
        to_area, mod = TARGETS[BY_OLD[m[2]]]
        alias = m[3] or (f" as {m[2]}" if mod != m[2] else "")
        return f"{m[1]}from {'.' if to_area == area else '..' + to_area} import {mod}{alias}{m[4] or ''}\n"
    if m and OLD_SIBLINGS.match(m[2]):
        raise SystemExit(f"{src}: `{body.strip()}` imports an old sibling that did not move")
    m = re.match(r"^(\s*)from (\w+) import (.*)$", body)
    if m and m[2] in BY_OLD:
        to_area, mod = TARGETS[BY_OLD[m[2]]]
        return f"{m[1]}from {'.' if to_area == area else '..' + to_area + '.'}{mod} import {m[3]}\n"
    if m and OLD_SIBLINGS.match(m[2]):
        raise SystemExit(f"{src}: `{body.strip()}` imports an old sibling that did not move")
    if re.match(r"^\s*sys\.path\.insert\(0, str\(Path\(__file__\)\.resolve\(\)\.parent\)\)\s*$", body):
        return re.match(r"^\s*", body)[0] + "from . import edge  # moved: siblings are imported relatively, no sys.path entry\n"
    if "Path(__file__).resolve().parent.parent" in body:
        body = body.replace("Path(__file__).resolve().parent.parent", "edge.records_dir()")
    if "subprocess.run(" in body and not body.lstrip().startswith("#"):
        body = body.replace("subprocess.run(", "edge.run(")
    return body + "\n" if body != line.rstrip("\n") else line


def statements(text: str):
    """Top-level statements with the exact text each owns: from the end of the previous statement to its own end, so
    comments, blank lines and decorators above a definition stay with it."""
    tree, lines, prev = ast.parse(text), text.splitlines(keepends=True), 0
    for node in tree.body:
        names = set()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names = {node.name}
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        yield node, names, "".join(lines[prev:node.end_lineno])
        prev = node.end_lineno


def extract_source(text: str, purpose: str, roots: tuple[str, ...]) -> tuple[str, list[str]]:
    """The module text of an extract before the line rules: purpose, the source's imports, the chosen definitions."""
    parts = list(statements(text))
    defined = {name: i for i, (_, names, _) in enumerate(parts) for name in names}
    missing = [r for r in roots if r not in defined]
    if missing:
        raise SystemExit(f"extract roots not defined at top level: {missing}")
    chosen, todo = set(), [defined[r] for r in roots]
    while todo:
        i = todo.pop()
        if i in chosen:
            continue
        chosen.add(i)
        todo += [defined[n.id] for n in ast.walk(parts[i][0]) if isinstance(n, ast.Name) and n.id in defined]
    first_def = min((i for i, (node, _, _) in enumerate(parts) if isinstance(node, ast.FunctionDef | ast.ClassDef)), default=len(parts))
    imports = [seg.lstrip("\n") for i, (node, _, seg) in enumerate(parts) if i < first_def and isinstance(node, ast.Import | ast.ImportFrom)]
    imports = [seg for seg in imports if "sys.path.insert" not in seg]
    body = [parts[i][2] for i in sorted(chosen)]
    names = sorted(name for name, i in defined.items() if i in chosen)
    return f'"""{purpose}"""\n\n' + "".join(imports) + "".join(body), names


PRE_PATCHES = [patch for table in MAPS.values() for patch in getattr(table, "PRE_PATCHES", [])]


def finish(text: str, area: str, src: str) -> str:
    for patch_src, old, new, _why in PRE_PATCHES:  # a line the rules below would refuse (an old sibling that did not move)
        if patch_src == src:
            if text.count(old) != 1:
                raise SystemExit(f"{src}: pre-patch anchor not found exactly once: {old[:70]!r}")
            text = text.replace(old, new)
    text = "".join(rewrite(line, area, src) for line in text.splitlines(keepends=True))
    for patch_src, old, new, _why in PATCHES:
        if patch_src == src:
            if text.count(old) != 1:
                raise SystemExit(f"{src}: patch anchor not found exactly once: {old[:70]!r}")
            text = text.replace(old, new)
    if "edge." in text and not re.search(r"^from \. import edge\b", text, re.MULTILINE):  # E0
        tree = ast.parse(text)
        first_def = min((n.lineno for n in tree.body if isinstance(n, ast.FunctionDef | ast.ClassDef)), default=10**9)
        last = max((n.end_lineno for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom) and n.lineno < first_def), default=0)
        lines = text.splitlines(keepends=True)
        lines.insert(last, "from . import edge  # moved: records, processes and what used to be separate scripts\n")
        text = "".join(lines)
    return text


# reviewed patches that are not applied to the old tools yet: (patch file relative to the tools directory, sha256 of
# the patch, the files of the old tools it changes or creates, where its verdict is). The source of such a file is
# "the old file plus the reviewed patch"; once the patch is applied there, the old file alone is that source.
REVIEWED_PATCHES = [row for table in MAPS.values() for row in getattr(table, "REVIEWED_PATCHES", [])]
_patched: dict[tuple[str, str], str] = {}


def source(tools: Path, rel: str) -> str:
    """The text of one old file (`hz-release`, `tests/test_x.py`), with the reviewed patches that name it applied."""
    import hashlib
    import shutil
    import subprocess
    import tempfile

    for patch, digest, files, _verdict in REVIEWED_PATCHES:
        if rel not in files:
            continue
        if (patch, rel) not in _patched:
            body = (tools / patch).resolve().read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise SystemExit(f"{patch}: not the reviewed patch (sha256 differs from the one in the moved map)")
            with tempfile.TemporaryDirectory() as scratch:
                for name in files:
                    if (tools / name).exists():
                        (Path(scratch) / "tools" / name).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy(tools / name, Path(scratch) / "tools" / name)
                include = [f"--include=tools/{name}" for name in files]
                applied = subprocess.run(["git", "apply", *include, str((tools / patch).resolve())], cwd=scratch, capture_output=True, text=True, check=False)
                if applied.returncode:  # already applied to the old tools: then the old file is the source
                    reverse = subprocess.run(["git", "apply", "--reverse", "--check", *include, str((tools / patch).resolve())], cwd=scratch, capture_output=True, check=False)
                    if reverse.returncode:
                        raise SystemExit(f"{patch}: applies neither forward nor backward to {files}: {applied.stderr.strip()[-200:]}")
                for name in files:
                    _patched[(patch, name)] = (Path(scratch) / "tools" / name).read_text()
        return _patched[(patch, rel)]
    return (tools / rel).read_text()


def produce(tools: Path, src: str) -> tuple[str, str, list[str]]:
    """(the source text the result is compared with, the module text, the extracted names or [])"""
    raw = source(tools, src)
    if src in EXTRACTS:
        _, purpose, roots = EXTRACTS[src]
        base, names = extract_source(raw, purpose, roots)
        return base, finish(base, TARGETS[src][0], src), names
    return raw, finish(raw, TARGETS[src][0], src), []


def rewrite_test(line: str) -> str:
    """Tests change only their imports: a moved sibling comes from the package, `conftest` helpers from moved_support."""
    body = line.rstrip("\n")
    m = re.match(r"^(\s*)import (\w+)( as \w+)?(\s*#.*)?$", body)
    if m and m[2] in BY_OLD:
        area, mod = TARGETS[BY_OLD[m[2]]]
        alias = m[3] or (f" as {m[2]}" if mod != m[2] else "")
        return f"{m[1]}from {ROOT}.{area} import {mod}{alias}{m[4] or ''}\n"
    m = re.match(r"^(\s*)from (\w+) import (.*)$", body)
    if m and m[2] in BY_OLD:
        area, mod = TARGETS[BY_OLD[m[2]]]
        return f"{m[1]}from {ROOT}.{area}.{mod} import {m[3]}\n"
    if m and m[2] == "conftest":
        return f"{m[1]}from moved_support import {m[3]}\n"
    if m and f"{m[2]}.py" in TESTS:  # a moved test that imports names of another moved test
        return f"{m[1]}from {TESTS[m[2] + '.py'].removesuffix('.py')} import {m[3]}\n"
    return line


def moved_test_text(path: Path) -> str:
    text = "".join(rewrite_test(line) for line in source(path.parent.parent, f"tests/{path.name}").splitlines(keepends=True))
    for name, old, new, _why in TEST_PATCHES:
        if name == path.name:
            if text.count(old) != 1:
                raise SystemExit(f"{name}: test patch anchor not found exactly once")
            text = text.replace(old, new)
    return text


def apply(tools: Path) -> int:
    for src in TARGETS:
        out = target(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(produce(tools, src)[1])
        print(f"moved {src} -> {out.relative_to(HERE.parent)}")
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    for src, name in TESTS.items():
        (TEST_DIR / name).write_text(moved_test_text(tools / "tests" / src))
        print(f"moved tests/{src} -> tests/moved/{name}")
    for name in FIXTURES:
        (TEST_DIR / "fixtures").mkdir(exist_ok=True)
        (TEST_DIR / "fixtures" / name).write_bytes((tools / "tests" / "fixtures" / name).read_bytes())
        print(f"copied tests/fixtures/{name} -> tests/moved/fixtures/{name}")
    return 0


def delta(a: str, b: str) -> list[str]:
    return [line for line in difflib.unified_diff(a.splitlines(), b.splitlines(), n=0, lineterm="") if not line.startswith(("---", "+++"))]


def verify_pins(tools: Path) -> int:
    """The pins the release policy computes from the reviewed files equal the literal pins of the old policy source."""
    if "release_lib" not in MAPS:
        return 0
    found = re.search(r"^APPROVED_SOURCES = (\{.*\})$", (tools / "release_candidate_policy.py").read_text(), re.MULTILINE)
    if not found:
        print("release_candidate_policy.py: no literal APPROVED_SOURCES to compare the reviewed files with")
        return 1
    spec = importlib.util.spec_from_file_location("approved", PKG / "release_lib" / "approved.py")
    approved = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(approved)
    literal, computed = ast.literal_eval(found[1]), approved.pins()
    for path in sorted(literal.keys() | computed.keys()):
        same = literal.get(path) == computed.get(path)
        print(f"pin {path}: old literal {str(literal.get(path))[:12]}, reviewed file {str(computed.get(path))[:12]}: {'EQUAL' if same else 'DRIFT'}")
    return sum(literal.get(path) != computed.get(path) for path in literal.keys() | computed.keys())


def verify(tools: Path) -> int:
    bad = changed_tests = 0
    changed, removed = dict.fromkeys(MAPS, 0), dict.fromkeys(MAPS, 0)
    for src in TARGETS:
        base, expect, names = produce(tools, src)
        found = target(src).read_text()
        edges, drift = delta(base, expect), delta(expect, found)
        kind = f"extract of {len(names)} definitions ({', '.join(names)})" if names else f"{len(base.splitlines())} lines"
        print(f"{src} -> {target(src).relative_to(HERE.parent)}: {kind}; {sum(1 for e in edges if e.startswith('+'))} edge line(s); "
              f"{'IDENTICAL otherwise' if not drift else 'DRIFT'}")
        changed[TARGETS[src][0]] += sum(1 for e in edges if e.startswith("+"))
        removed[TARGETS[src][0]] += sum(1 for e in edges if e.startswith("-"))
        for line in edges:
            print(f"    {line}")
        for line in drift:
            bad += 1
            print(f"  DRIFT {line}")
    for src, name in TESTS.items():
        base = source(tools, f"tests/{src}")
        expect = moved_test_text(tools / "tests" / src)
        found = (TEST_DIR / name).read_text()
        edges, drift = delta(base, expect), delta(expect, found)
        print(f"tests/{src} -> tests/moved/{name}: {len(base.splitlines())} lines; {sum(1 for e in edges if e.startswith('+'))} import line(s); "
              f"{'IDENTICAL otherwise' if not drift else 'DRIFT'}")
        changed_tests += sum(1 for e in edges if e.startswith("+"))
        for line in edges:
            print(f"    {line}")
        for line in drift:
            bad += 1
            print(f"  DRIFT {line}")
    for name in FIXTURES:
        same = (tools / "tests" / "fixtures" / name).read_bytes() == (TEST_DIR / "fixtures" / name).read_bytes()
        bad += 0 if same else 1
        print(f"tests/fixtures/{name} -> tests/moved/fixtures/{name}: {'IDENTICAL bytes' if same else 'DRIFT'}")
    for patch, digest, files, verdict in REVIEWED_PATCHES:
        print(f"reviewed patch {patch} (sha256 {digest[:12]}, verdict {verdict}) is part of the source of: {', '.join(files)}")
    bad += verify_pins(tools)
    expected_tests = sum(table.TEST_EDGE_LINES for table in MAPS.values())
    for lib, table in MAPS.items():
        print(f"{lib}: {changed[lib]} edge line(s) in modules (expected {table.EDGE_LINES})")
        if changed[lib] != table.EDGE_LINES:
            bad += 1
            print(f"the number of edge lines of {lib} moved: read every line above, then set EDGE_LINES in its moved_map.py")
        if hasattr(table, "EDGE_REMOVED_LINES"):  # a patch that only deletes lines moves this count, not the one above
            print(f"{lib}: {removed[lib]} line(s) removed or replaced in modules (expected {table.EDGE_REMOVED_LINES})")
            if removed[lib] != table.EDGE_REMOVED_LINES:
                bad += 1
                print(f"the number of removed lines of {lib} moved: read every line above, then set EDGE_REMOVED_LINES in its moved_map.py")
    print(f"{changed_tests} import or patch line(s) in moved tests (expected {expected_tests})")
    if changed_tests != expected_tests:
        bad += 1
        print("the number of changed test lines moved: read every line above, then set TEST_EDGE_LINES in the moved_map.py of its area")
    print("VERBATIM except the edge lines above" if not bad else f"{bad} line(s) differ beyond the edge rules")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("apply", "verify"):
        sys.exit(__doc__)
    sys.exit({"apply": apply, "verify": verify}[sys.argv[1]](Path(sys.argv[2]).expanduser()))
