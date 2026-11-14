"""Resolve the conflicts every slice causes in the two registration files, by union. Exit 1 if another file conflicts.

native/__init__.py: the union of the imported area modules, sorted, in the import line and in MODULES.
plugin.py: the union of the settings mixin imports and of the bases of HeinzelSettings.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

INIT = "plugins/heinzel/src/steuermaschine_heinzel/native/__init__.py"
PLUGIN = "plugins/heinzel/src/steuermaschine_heinzel/plugin.py"
CONFLICT = re.compile(r"<<<<<<< [^\n]*\n(.*?)(?:\|\|\|\|\|\|\| [^\n]*\n.*?)?=======\n(.*?)>>>>>>> [^\n]*\n", re.S)


IMPORT = re.compile(r"from \. import (\([^)]*\)|[^\n(]+)\n")
MODULES = re.compile(r"MODULES: list = \[[^\]]*\]\n")


def resolve_init(text: str) -> str:
    # Both sides' import statements (one line or parenthesized over several lines), conflict markers or not.
    names = sorted(set(re.findall(r"\b([a-z_]+)\b", " ".join(m.strip("()") for m in IMPORT.findall(text)))) - {"import", "from"})
    body = CONFLICT.sub("", text)
    body = IMPORT.sub("", body)
    body = MODULES.sub("", body)
    head, tail = body.split("from steuermaschine.native import collect\n", 1)
    imports = "from . import (\n" + "".join(f"    {n},\n" for n in names) + ")\n"
    rows, row = [], "   "
    for n in names:
        if len(row) + len(n) + 2 > 118:
            rows.append(row.rstrip())
            row = "   "
        row += f" {n},"
    rows.append(row.rstrip())
    modules = "MODULES: list = [\n" + "\n".join(rows) + "\n]\n"
    return f"{head.rstrip()}\n\nfrom steuermaschine.native import collect\n\n{imports}\n{modules}\n\n{tail.lstrip()}"


def resolve_plugin(text: str) -> str:
    imports = sorted(set(re.findall(r"^from \.native\.[a-z_.]+ import [A-Za-z_, ]+?(?:  # noqa: E402)?$", text, re.M)))
    imports = sorted({line.replace("  # noqa: E402", "") for line in imports})
    bases: list[str] = []
    for found in re.findall(r"^class HeinzelSettings\(([^)]*)\):", text, re.M):
        for base in [b.strip() for b in found.split(",")]:
            if base and base != "BaseModel" and base not in bases:
                bases.append(base)
    body = CONFLICT.sub(lambda m: m[1] if "class HeinzelSettings" not in m[0] and "from .native" not in m[0] else "@@CLASS@@\n" if "class HeinzelSettings" in m[0] else "", text)
    body = re.sub(r"^from \.native\.[^\n]+\n", "", body, flags=re.M)
    line = f"class HeinzelSettings({', '.join([*sorted(bases), 'BaseModel'])}):\n"
    body = body.replace("@@CLASS@@\n", line) if "@@CLASS@@" in body else re.sub(r"^class HeinzelSettings\([^)]*\):\n", line, body, flags=re.M)
    anchor = "from steuermaschine.hookspecs import hookimpl\n"
    body = body.replace(anchor, anchor + "\n" + "\n".join(imports) + "\n", 1)
    return re.sub(r"\n{4,}", "\n\n\n", body)


def main() -> int:
    unmerged = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True, check=False).stdout.split()
    other = [f for f in unmerged if f not in (INIT, PLUGIN)]
    if other:
        return 1
    for name, resolve in ((INIT, resolve_init), (PLUGIN, resolve_plugin)):
        if name in unmerged:
            Path(name).write_text(resolve(Path(name).read_text()))
            if "<<<<<<<" in Path(name).read_text():
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
