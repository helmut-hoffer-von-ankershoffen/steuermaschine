#!/usr/bin/env python3
"""Verbatim-move check for the coordinator port: prints every line of a moved function that differs from its original.

usage: coord_verbatim.py [--tools DIR]      (default ~/Code/heinzel-management/tools; read only)

For each entry of MOVED the named top-level definitions are cut from the original and from the moved module, type
annotations are removed from both (the moved side is typed for mypy strict), both are printed back in one canonical
form (`ast.unparse`) and compared line by line. No output below a name means: the same code. Exit 1 when a line
differs or a name is missing.
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
    ("hz-asks-ingest", "title"): "keyword maxsplit: the positional form is deprecated in Python 3.13",
    ("hz_bead_sweep.py", "days_since"): "datetime.UTC is the same object as timezone.utc",
    ("hz_bead_sweep.py", "build"): "release_of is a parameter (git runs through the one process runner)",
    ("hz_bead_sweep.py", "propose_close"): "the close reason names the sm command, not the old tool",
    ("hz-affected", "Repo"): "an unusable checkout raises RepoError instead of exiting; related_tests is loaded from the checkout's file "
                               "and its graph is read through graph_files (the core renamed Graph.trees to Graph.known)",
    ("hz-agent", "add"): "the table file is a required keyword (no default path in code); review B4: lock from read to write, atomic replace, "
                         "refusal of an unusable table or agent id, whole-cell duplicate check, cleaned cells",
    ("hz-velocity", "Api"): "no default runner: the caller passes one that goes through the one process runner",
    ("hz-doc", "find_sections"): "a lambda became the same lambda with a lint marker; no code change",
}

# Moved test files: (original under tools/tests, moved under plugins/heinzel/tests, the line both share from which on they must be identical).
TAILS = {
    "test_velocity_bounded.py": ("test_native_coord_velocity_moved.py", "def check(name=\"CI OK\", **extra):"),
}

# original file -> (moved module, [top-level names])
MOVED: dict[str, tuple[str, list[str]]] = {
    "hz-stamp": ("coord_lib/journal.py", ["merged_line", "released_line", "earliest_tag", "merged_prs", "is_released"]),
    "hz-ask": ("asks_lib/asks.py", ["STAMP", "PR", "CONTEXT_BLOCK", "LINE_WIDTH", "owner_text", "owner_messages", "short_time"]),
    "hz-asks-ingest": ("asks_lib/asks.py", ["title"]),
    "hz_review_brief.py": ("briefs_lib/reviewer.py", ["members"]),
    "hz-merged-via": ("merges_lib/combine_close.py", ["members"]),
    "hz-affected": ("affected_lib/select.py", ["JOBS_FULL", "JOBS_ALWAYS", "FULL", "DOCS", "SITE_TEST_FILES", "DESKTOP", "PLUGINS", "WEB_SRC", "WEB_OPENAPI", "PY_SRC", "PY_TEST",
                                               "PY_SUPPORT", "NATIVE_PY", "CLI_LANE_PY", "SITE_TESTS_DEFAULT", "DOCS_READERS", "WEB_SHARED", "Repo", "classify", "render", "CASES"]),
    "hz-doc": ("../../../../../src/steuermaschine/docsection.py", ["headings", "find_sections"]),
    "hz-agent": ("agents_lib/table.py", ["add"]),
    "hz-velocity": ("velocity_lib/samples.py", ["MAX_CALLS", "MAX_SAMPLES", "ADVISORY", "LIMITS", "utc_now", "seconds", "selection", "Unavailable", "valid_repo", "directory", "read_json_at",
                                                "write_json_at", "valid_run", "valid_jobs", "Api", "pr_metadata", "job_metrics", "check_ready", "journal", "record_eligible",
                                                "eligible_observation", "count_ci", "repository_matches", "ci_binding", "collect"]),
    "hz_refill.py": ("refill_lib/slots.py", ["TARGET", "LOAD_MAX", "_age_min", "suggest_model", "short", "agent_ids", "dead_owners", "free_slots", "start_lines", "refill_line"]),
    "hz_digest.py": ("refill_lib/slots.py", ["BEAD", "bead_of"]),
    "hz-skills": ("../../../../../src/steuermaschine/skillgen.py", ["frontmatter"]),
    "hz_bead_sweep.py": ("sweep_lib/evidence.py", ["STOP", "vkey", "words", "noted_prs", "match_prs", "days_since", "classify_hint", "build", "table", "propose_close", "summary"]),
}


def block(text: str, name: str) -> list[str] | None:
    """One top-level def, class or assignment in canonical form, without annotations."""
    for node in ast.parse(text).body:
        names = [node.name] if isinstance(node, ast.FunctionDef | ast.ClassDef) else [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)]
        if name in names:
            for inner in ast.walk(node):
                if isinstance(inner, ast.FunctionDef):
                    inner.returns = None
                    for arg in [*inner.args.posonlyargs, *inner.args.args, *inner.args.kwonlyargs, inner.args.vararg, inner.args.kwarg]:
                        if arg is not None:
                            arg.annotation = None
            return ast.unparse(node).splitlines()
    return None


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
    for original, (moved, marker) in TAILS.items():
        old_text, new_text = (tools / "tests" / original).read_text(), (HERE / "tests" / moved).read_text()
        same = marker in old_text and old_text[old_text.index(marker):] == new_text[new_text.index(marker):]
        print(f"tests/{original} -> {moved}: {'VERBATIM from `' + marker[:24] + '` on' if same else 'DIFFERS'}")
        bad += not same
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
