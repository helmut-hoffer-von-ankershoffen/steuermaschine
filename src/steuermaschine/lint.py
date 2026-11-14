"""Static rules for native code, run by the promote gate. They read source; they run nothing.

1. `write_reach(cmd)`: a command registered `writes=False` must not reach code that writes outside this machine.
   Followed: calls from the command's function into functions of its own module and its `<module>_lib` package,
   by name. Flagged: a GitHub client call with a method other than GET, a `gh` argument list that starts a write
   (`pr merge`, `variable set`, `workflow run`, `run cancel`, ...), a `git push`, and the write methods of the
   provider Protocols. It is a net, not a proof: a write reached through a name this cannot follow passes.
2. `main_blocks(root)`: moved libraries expose functions only. A module with `if __name__ == "__main__"` is a second
   way in that no review gate sees.
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


def write_reach(cmd: Any) -> list[str]:
    """Findings for one native command; empty when nothing that writes outside is reachable from its function."""
    files = cmd.code_files()
    if not files:
        return []
    defs: dict[str, list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]] = {}
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(node.name, []).append((path, node))
    start = [(p, n) for p, n in defs.get(cmd.fn.__name__, []) if p == files[0]]
    seen: set[int] = set()
    queue, findings = list(start), []
    while queue:
        path, func = queue.pop()
        if id(func) in seen:
            continue
        seen.add(id(func))
        findings += [f"{path.name}:{func.name}: {text}" for text in _writes_in(func)]
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                queue += defs.get(name, [])
    return sorted(set(findings))


def review_unit_gaps(cmd: Any) -> list[str]:
    """A write command's review unit (the files its verdict's digest covers) must contain every `*_lib` package of
    its plugin that its module imports: a change there can alter what the command writes."""
    files = cmd.code_files()
    if not files:
        return []
    module, covered = files[0], {p.resolve() for p in files}
    gaps = []
    for node in ast.walk(ast.parse(module.read_text())):
        names = []
        if isinstance(node, ast.ImportFrom) and node.level >= 1:
            names = [(node.module or "").split(".")[0]] if node.module else [a.name for a in node.names]
        for name in names:
            lib = module.parent / name
            if name.endswith("_lib") and lib.is_dir() and not any(p.resolve() in covered for p in lib.rglob("*.py")):
                gaps.append(f"{module.name} imports {name}, which is not in the review unit of `{cmd.path}`: add files=(\"{name}\", ...) to its @native")
    return sorted(set(gaps))


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
