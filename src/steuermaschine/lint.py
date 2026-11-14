"""Static rules for native code, run by the promote gate. They read source; they run nothing.

1. `write_reach(cmd)`: a command registered `writes=False` must not reach code that writes outside this machine.
   Followed: calls from the command's function into the functions they name; where the source does not say which
   function a call means, every function of that name in the command's module and its `<module>_lib` package.
   Flagged: a GitHub client call with a method other than GET, a `gh` argument list that starts a write (`pr merge`,
   `variable set`, `workflow run`, `run cancel`, ...), a `git push`, and the write methods of the provider Protocols.
   It is a net, not a proof: a write reached through a name this cannot follow passes.
2. `main_blocks(root)`: moved libraries expose functions only. A module with `if __name__ == "__main__"` is a second
   way in that no command registers.
3. `script_calls(root, old_tools, old_dir)`: nothing starts an old tool script.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

PROTOCOL_WRITES = {"open_pr", "set_variable", "delete_variable", "spawn", "comment_issue", "close_issue"}  # names specific enough to match by name alone
GH_WRITES = {("pr", "merge"), ("pr", "create"), ("pr", "close"), ("pr", "comment"), ("pr", "edit"), ("pr", "review"), ("pr", "ready"), ("issue", "create"), ("issue", "close"),
             ("issue", "comment"), ("issue", "edit"), ("variable", "set"), ("variable", "delete"), ("secret", "set"), ("workflow", "run"), ("run", "cancel"),
             ("run", "rerun"), ("run", "delete"), ("release", "create"), ("release", "delete"), ("release", "edit"), ("label", "create"), ("repo", "edit")}
OWN_PROCESS = re.compile(r"tools_dir|^\s*(import subprocess|from subprocess )")


def _strings(node: ast.AST) -> list[str]:
    return [e.value for e in getattr(node, "elts", []) if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def _writes_in(func: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        if name == "api":
            method = keywords.get("method") or (node.args[2] if len(node.args) > 2 else None)
            if method is not None and not (isinstance(method, ast.Constant) and str(method.value).upper() == "GET"):
                found.append(f"line {node.lineno}: GitHub API call with a method other than GET")
        live = keywords.get("live")
        if name in ("cancel_run",) and isinstance(live, ast.Constant) and live.value is True:
            found.append(f"line {node.lineno}: cancel_run(live=True)")
        if name in PROTOCOL_WRITES and isinstance(node.func, ast.Attribute):
            found.append(f"line {node.lineno}: .{name}() writes outside this machine")
        for arg in node.args[:1]:
            words = _strings(arg)
            if tuple(words[:2]) in GH_WRITES or (words[:1] == ["gh"] and tuple(words[1:3]) in GH_WRITES):
                found.append(f"line {node.lineno}: gh {' '.join(words[-2:] if words[:1] != ['gh'] else words[1:3])}")
            if "push" in words and ("git" in words or name in ("git", "_git")):
                found.append(f"line {node.lineno}: git push")
            if words[:1] == ["api"] and any(w in ("-X", "--method") for w in words) and "GET" not in words:
                found.append(f"line {node.lineno}: gh api with a write method")
    return found


_Def = ast.FunctionDef | ast.AsyncFunctionDef


class _Source:
    """One parsed file: its functions by name, the names its imports bind to files of the command's package, and the
    names its other imports bind (the standard library, the core, third parties): a call on one of those leaves the
    package, so there is nothing to follow."""

    def __init__(self, path: Path, roots: dict[str, Path]):
        from . import native

        self.path = path
        self.defs: dict[str, list[_Def]] = {}
        self.binds: dict[str, tuple[Path, str]] = {}  # local name -> (file, "" for the module itself, else the name taken from it)
        self.outside: set[str] = set()
        name, package = native._module_of(path, roots)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs.setdefault(node.name, []).append(node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    hit = native._resolve(alias.name, roots)
                    if hit and (alias.asname or "." not in alias.name):
                        self.binds[alias.asname or alias.name] = (hit, "")
                    elif not hit:
                        self.outside.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    here = name.split(".") if package else name.split(".")[:-1]
                    if node.level - 1 > len(here) - 1 or not here[0]:
                        continue
                    base = ".".join(here[:len(here) - (node.level - 1)] + ([node.module] if node.module else []))
                for alias in node.names:
                    module = native._resolve(f"{base}.{alias.name}", roots)
                    inside = None if module else native._resolve(base, roots)
                    if module:
                        self.binds[alias.asname or alias.name] = (module, "")
                    elif inside:
                        self.binds[alias.asname or alias.name] = (inside, alias.name)
                    else:
                        self.outside.add(alias.asname or alias.name)
        self.outside -= set(self.binds) | set(self.defs)  # a name bound both ways stays in the net


def write_reach(cmd: Any) -> list[str]:
    """Findings for one native command; empty when nothing that writes outside is reachable from its function.

    A call is followed to its function: `module.name(...)` and an imported `name(...)` into the file the import names
    (a file of the command's own package, also outside its module and `<module>_lib`), a plain `name(...)` to the
    function of that name in the same file. A call on a name imported from outside the package (`subprocess.run`) is
    not followed. Any other call (`obj.name(...)`, a name nothing here defines) is followed by name alone into every
    function of that name in the command's module and its `<module>_lib`: the net. In a file outside those, reached
    through an import, the net is that file alone."""
    from . import native

    files = [Path(p).resolve() for p in cmd.code_files()]
    if not files:
        return []
    roots = {top: root for top, root in native._roots(cmd.fn.__module__).items() if top != native.CORE}
    sources: dict[Path, _Source] = {}

    def source(path: Path) -> _Source:
        if path not in sources:
            sources[path] = _Source(path, roots)
        return sources[path]

    def named(path: Path, name: str, depth: int = 0) -> list[tuple[Path, _Def]]:
        """The functions `name` means in the file `path`: defined there, or imported there from another file."""
        here = source(path)
        if name in here.defs:
            return [(path, node) for node in here.defs[name]]
        target, member = here.binds.get(name, (None, ""))
        return named(target, member, depth + 1) if target and member and depth < 5 else []

    def by_name(path: Path, name: str) -> list[tuple[Path, _Def]]:
        """The net. From a file outside the unit it is that file alone: such a file does not know the unit."""
        return [(p, node) for p in (files if path in files else [path]) for node in source(p).defs.get(name, [])]

    def targets(path: Path, call: ast.Call) -> list[tuple[Path, _Def]]:
        here, func = source(path), call.func
        if isinstance(func, ast.Attribute):
            base = func.value.id if isinstance(func.value, ast.Name) else ""
            if base in here.outside:
                return []
            target, member = here.binds.get(base, (None, ""))
            return named(target, func.attr) if target and not member else by_name(path, func.attr)
        name = getattr(func, "id", "")
        if not name or name in here.outside:
            return []
        return named(path, name) if name in here.defs or name in here.binds else by_name(path, name)

    seen: set[int] = set()
    queue, findings = [(files[0], node) for node in source(files[0]).defs.get(cmd.fn.__name__, [])], []
    while queue:
        path, func = queue.pop()
        if id(func) in seen:
            continue
        seen.add(id(func))
        findings += [f"{path.name}:{func.name}: {text}" for text in _writes_in(func)]
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                queue += targets(path, node)
    return sorted(set(findings))


def main_blocks(root: Path) -> list[str]:
    """Modules under `root` with an `if __name__ == "__main__"` block."""
    found = []
    for path in sorted(Path(root).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__":
                found.append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def script_calls(root: Path, old_tools: set[str], old_dir: str = "") -> list[str]:
    """Places under `root` that can start an old tool script: the old tools directory or a tools_dir setting, a
    `subprocess` import (processes go through the one runner), an argument list whose first element is an old tool,
    and a path to an old tool next to the code (`Path(__file__).parent / "old-tool"`). Naming an old tool as data
    (`replaces=(...)`, a state file name) is not a call."""
    found = []
    for path in sorted(Path(root).rglob("*.py")):
        text = path.read_text()
        for number, line in enumerate(text.splitlines(), 1):
            if OWN_PROCESS.search(line) or (old_dir and old_dir in line):
                found.append(f"{path.relative_to(root)}:{number}: {line.strip()[:120]}")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts and isinstance(node.elts[0], ast.Constant):
                first = _strings(node)[:1]
                if first and first[0].rsplit("/", 1)[-1] in old_tools and isinstance(node.ctx, ast.Load) and len(node.elts) > 1:
                    found.append(f"{path.relative_to(root)}:{node.lineno}: an argument list starts the old tool {first[0]}")
            if not isinstance(node, ast.BinOp):
                continue
            beside_code = "__file__" in ast.unparse(node.left)  # a script next to the code, not a state directory of that name
            if beside_code and isinstance(node.op, ast.Div) and isinstance(node.right, ast.Constant) and str(node.right.value) in old_tools:
                found.append(f"{path.relative_to(root)}:{node.lineno}: a path to the old tool {node.right.value!s}")
    return found
