#!/usr/bin/env python3
"""Verbatim-move check for the views slice (ci main, head combine, go views): prints every line of a moved definition
that differs from its original.

usage: views_verbatim.py [--tools DIR]      (default ~/Code/heinzel-management/tools; read only)

For each entry of MOVED the named definitions are cut from the original and from the moved module (top level, or
`Class.method`), type annotations are removed from both, both are printed back in one canonical form (`ast.unparse`)
and compared line by line. Exit 1 when a line differs without a reason in MEANT, or a name is missing.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LIB = HERE / "src/steuermaschine_heinzel/native"

# Differences that are meant: (original, name) -> why. They are printed with their lines and do not fail the check.
MEANT: dict[tuple[str, str], str] = {
    ("hz-main-evidence", "explain"): "the queue module is a parameter (the tool loaded it as a module global)",
    ("hz-next", "brief_cmd"): "names sm beads brief instead of the old brief tool",
    ("hz-next", "open_ask_rows"): "the tracking document's lines are a parameter (the tool read a file under the home directory)",
    ("hz-next", "src_paused"): "the lines and the clock are parameters; the slot check names sm gate status",
    ("hz-next", "update_due"): "the clock and the last update are parameters (read by go_lib/legacy.next_last_update)",
    ("hz-next", "render"): "the clock is a parameter; the hints name sm go --view next; no claim hint (claims are not written by sm)",
    ("hz-start", "latest_checkpoint"): "the document's text is a parameter (the tool read it next to itself)",
    ("hz-start", "timer_rows"): "the manifest and the Codex home are parameters (settings go_timers_file, go_codex_home); no home: one row says so",
    ("hz_combine.py", "approved_sources"): "the pins come from release_lib's reviewed approved sources, not from parsing the policy file next to the tool",
}

# original file -> (moved module, [names])
MOVED: dict[str, tuple[str, list[str]]] = {
    "hz-main-evidence": ("ci_evidence_lib/main_evidence.py", ["explain"]),
    "hz-next": ("go_lib/views_moved.py", ["ASK_MINUTES", "MAX_LINES", "PER_PRIO", "STAMP", "PR", "PAUSED", "LOG_LINE", "slug", "RANK_BY_PRIO", "RANK_WORDS",
                                         "classify", "brief_cmd", "item", "open_ask_rows", "TEST_OR_CI", "ship_hint", "src_paused", "update_due", "render"]),
    "hz-start": ("go_lib/views_moved.py", ["latest_checkpoint", "timer_rows"]),
    "hz_combine.py": ("combine_lib/combine.py", ['AREAS', 'K_MIN', 'SOFT_CAP', 'WINDOW_MIN', 'MAX_SUBJECT', 'LIMITS', 'SCOPES', 'PLUGIN_SCOPES', 'TOOLS_PREFIXES', 'TOOLS_FILES', 'NEUTRAL_PREFIXES', 'NEUTRAL_SUFFIXES', 'GENERATED', 'SOLO_TOKENS', 'PINNED', 'approved_sources', 'FAILING', 'TYPE', 'META', 'is_neutral', 'file_area', 'solo_file', 'member_area', 'area_of_label', 'counted', 'Member', 'refusal', 'topo_order', 'clusters', 'Plan', 'plan', 'strip_suffix', 'shorten', 'member_subject', 'parse_meta', 'cap_line', 'age_min', 'PR_FIELDS', 'Combiner.__init__', 'Combiner.open_prs', 'Combiner.split', 'Combiner.area_for', 'Combiner.plan', 'Combiner.print_plan', 'Combiner.status']),
}


def find(body: list, name: str):
    cls, _, member = name.partition(".")
    for node in body:
        if member and isinstance(node, ast.ClassDef) and node.name == cls:
            return find(node.body, member)
        if member:
            continue
        targets = [e for t in getattr(node, "targets", []) for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
        names = [node.name] if isinstance(node, ast.FunctionDef | ast.ClassDef) else [t.id for t in targets if isinstance(t, ast.Name)]
        if name in names:
            return node
    return None


def block(text: str, name: str) -> list[str] | None:
    """One definition in canonical form, without annotations."""
    node = find(ast.parse(text).body, name)
    if node is None:
        return None
    for inner in ast.walk(node):
        if isinstance(inner, ast.FunctionDef):
            inner.returns = None
            for arg in [*inner.args.posonlyargs, *inner.args.args, *inner.args.kwonlyargs, inner.args.vararg, inner.args.kwarg]:
                if arg is not None:
                    arg.annotation = None
    return ast.unparse(node).splitlines()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tools", default=str(Path.home() / "Code/heinzel-management/tools"))
    tools, bad = Path(parser.parse_args().tools), 0
    for original, (moved, names) in MOVED.items():
        old_text, new_text = (tools / original).read_text(), (LIB / moved).read_text()
        for name in names:
            old, new = block(old_text, name), block(new_text, name)
            if old is None or new is None:
                print(f"{original} -> {moved}: {name}: MISSING in {'the original' if old is None else 'the moved module'}")
                bad += 1
                continue
            diff = [ln for ln in difflib.unified_diff(old, new, original, moved, lineterm="", n=0) if not ln.startswith(("---", "+++", "@@"))]
            meant = MEANT.get((original, name), "") if diff else ""
            print(f"{original} -> {moved}: {name}: {'VERBATIM' if not diff else f'MEANT ({meant})' if meant else f'{len(diff)} differing line(s)'}")
            for ln in diff:
                print("    " + ln)
            bad += bool(diff) and not meant
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
