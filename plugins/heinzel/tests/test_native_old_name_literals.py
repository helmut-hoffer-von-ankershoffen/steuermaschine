"""No old tool name reaches a command's output from a native module: every string literal with an old name is
`replaces=` metadata, the state moves (state.py), a pattern that detects old names, or an agent type. KNOWN lists
each with its reason; a new literal fails. The moved libraries name `sm` themselves (bin/debt.py counts every string
of the plugin and holds it at zero)."""

import ast
import re
from pathlib import Path

import steuermaschine_heinzel

ROOT = Path(steuermaschine_heinzel.__file__).parent
OLD = re.compile(r"hz-[\w.-]*")
KNOWN = {
    ("guard_floor.py", "hz-"): "the guard's patterns that recognise an old tool word",
    ("plugin_gallery.py", "hz-plugin"): "the plugins repository's own tooling command, not an old sm tool",
    ("skills.py", "hz-builder"): "an agent type that looks like an old tool name",
    ("skills.py", "hz-chore"): "an agent type that looks like an old tool name",
    ("state.py", "hz-"): "the state moves: the old prefix of every moved file",
    ("state.py", "hz-gate-history.jsonl"): "the state moves: an old path outside ~/.cache",
    ("state.py", "hz-leakwatch"): "the state moves: an old path outside ~/.cache",
    ("state.py", "hz-loadguard"): "the state moves: an old path outside ~/.cache",
    ("state.py", "hz-train"): "the state moves: the example in a docstring-like comment of old_of",
}


def literals() -> set[tuple[str, str]]:
    found = set()
    for path in sorted(ROOT.rglob("*.py")):
        if "_lib" in path.parent.name or path.name == "toolmap.py":
            continue
        tree = ast.parse(path.read_text())
        metadata = {id(node) for kw in ast.walk(tree) if isinstance(kw, ast.keyword) and kw.arg == "replaces" for node in ast.walk(kw.value)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in metadata:
                found |= {(path.name, word) for word in OLD.findall(node.value)}
    return found


def test_every_old_name_literal_in_a_native_module_is_known():
    assert literals() <= set(KNOWN), sorted(literals() - set(KNOWN))


def test_the_known_list_has_no_stale_entry():
    assert set(KNOWN) <= literals(), sorted(set(KNOWN) - literals())
