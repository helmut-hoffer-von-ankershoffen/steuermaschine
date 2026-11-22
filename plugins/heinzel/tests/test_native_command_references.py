"""Every `sm ...` command that a help text or a hint names exists in the built command tree (g5 finding 8).

Collected: the help text of every command and group of the tree (references in backticks, and the start of the
Example:/Next: lines), and every string literal of the plugin's modules where `sm ...` starts the text or a line,
follows a separator (`;`, `:`, `(`), a step number, "then" or "run", or stands in backticks. Docstrings are not
hints. A reference resolves when its words walk the tree to a command (what follows is its arguments) or end at a
group. Pipeline paths (`head admit`, `release cut`) are in the tree like every command."""

import ast
import json
import re
from pathlib import Path

from coord_kit import app_runtime

from steuermaschine.testing import invoke

SRC = Path(__file__).resolve().parents[1] / "src" / "steuermaschine_heinzel"
WORDS = r"sm((?: [a-z][a-z0-9-]*)+)"
TICKED = re.compile(r"`" + WORDS)
LEADING = re.compile(r"^(?:Example: |Next: |\s*)?" + WORDS, re.M)
AFTER = re.compile(r"(?:^|[;:(]\s*|\d\. |\bthen |\brun )" + WORDS, re.M)  # a command after a separator, a step number, "then" or "run"
# Deliberate: text that names no live command, each with its reason.
DELIBERATE = {
    ("native/combine.py", "sm head combine build"): "not ported yet; the hint says so and names `sm tools gaps` (MISSING there)",
    ("native/packet_lib/admit_wave.py", "sm head packet predicted"): "the message of a predicted squash commit (part of its SHA), no command",
    ("native/release_drive.py", "sm gap"): "the label of a gap note line (`sm gap: ...`), no command",
}


def tree(tmp_path) -> dict[str, bool]:
    rows = json.loads(invoke(app_runtime(tmp_path), ["--format", "json", "self", "tree", "--all"]).stdout)
    return {row["path"].removeprefix("sm").strip(): row["group"] and not row.get("default") for row in rows}  # `sm release 0.9.55` runs its default


def unresolved(paths: dict[str, bool], words: list[str]) -> str:
    """The reference up to its first word that is no command, or "" when it resolves."""
    walked: list[str] = []
    for word in words:
        if " ".join([*walked, word]) not in paths:
            break
        walked.append(word)
    if walked and not (paths[" ".join(walked)] and len(walked) < len(words)):
        return ""
    return "sm " + " ".join(words[: len(walked) + 1])


def references(text: str, lead: bool) -> list[list[str]]:
    found = [m.group(1).split() for m in TICKED.finditer(text)]
    if lead:
        found += [m.group(1).split() for m in LEADING.finditer(text)]
    else:
        found += [m.group(1).split() for m in AFTER.finditer(text)]
    return found


def test_every_command_a_help_or_hint_names_exists(tmp_path):
    paths = tree(tmp_path)
    assert {"head admit", "release cut", "queue main-proof", "ci rerun"} <= set(paths)
    missing = []
    rows = json.loads(invoke(app_runtime(tmp_path), ["--format", "json", "self", "tree", "--all"]).stdout)
    for row in rows:
        missing += [(f"help of {row['path']}", bad) for words in references(row["help"], True) if (bad := unresolved(paths, words))]
    for file in sorted(SRC.rglob("*.py")):
        where = str(file.relative_to(SRC))
        module = ast.parse(file.read_text())
        docs = {id(node.body[0].value) for node in ast.walk(module)  # docstrings explain; they are no hints
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(module):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                for words in references(node.value, False):
                    if (bad := unresolved(paths, words)) and (where, bad) not in DELIBERATE:
                        missing.append((f"{where}:{node.lineno}", bad))
    assert missing == [], missing


def test_the_walk_resolves_commands_groups_and_arguments():
    paths = {"queue": True, "queue why": False, "release": True, "release train": True, "release train resume-ci": False}
    assert unresolved(paths, ["queue", "why", "pr"]) == ""  # a command takes the rest as its arguments
    assert unresolved(paths, ["release", "train"]) == ""  # a group alone
    assert unresolved(paths, ["release", "train", "resume"]) == "sm release train resume"
    assert unresolved(paths, ["beads", "triage"]) == "sm beads"
    assert references("run `sm ci rerun --all` now", False) == [["ci", "rerun"]]
    assert references("Example: sm go\nNext: sm queue why PR", True) == [["go"], ["queue", "why"]]
    assert references("  4. close beads: bd list --label release ; sm beads triage\n", False) == [["beads", "triage"]]  # the g5 find
    assert references("2. when main CI runs: sm ci rerun", False) == [["ci", "rerun"]]
