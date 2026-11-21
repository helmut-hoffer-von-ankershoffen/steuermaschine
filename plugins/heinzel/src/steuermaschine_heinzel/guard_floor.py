"""The floor of the Bash hook: the refusals that hold even when sm or this plugin cannot load.

Standard library only, and nothing from this package: `sm-guard --floor steuermaschine_heinzel.guard_floor` loads this
file by its path (the package's `__init__` is not run), on every Bash call of an agent. Keep it small and fast.

The floor is:
- every refusal of the former git hook, unchanged (`git_refusal`): a git push or commit with --no-verify, --force,
  --force-with-lease, -f or -n (-f on a commit is allowed);
- never run a script of the retired tools directory by hand (`old_tool_refusal`);
- never `make pr` (`make_pr_refusal`), gated or not;
- never dispatch a workflow on a PR head SHA where the command line shows it (`dispatch_refusal`): `gh workflow run
  --ref/-r` or a `ref=` / `sha=` field of `gh workflow run` or of `gh api .../dispatches`, whose value is a hex SHA, a
  pull ref, or names a PR head (`headRefOid`, `$HEAD_SHA`, ...);
- no new admission or release write step (`sm head prepare|apply|admit|validate`, `sm release admit|resume`) while
  bin/promote is switching the live sm (`switch_refusal`): promote writes PENDING with its pid before it waits for the
  running steps to end, and removes it after the switch. A file whose pid is gone is stale and ignored.

`FULL` names the full rule set that `sm-guard` tries as well; when it cannot load, the floor alone decides.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

FULL = "steuermaschine_heinzel.native.guards_lib.hooks:git_refusal"

GIT_BLOCKED = "Blocked: no --no-verify or force push. If pre-push fails, wait for lower load or report it."
OLD_TOOL_BLOCKED = ("Blocked: the scripts of the retired tools directory are not run by hand; run the sm command that replaced it "
                    "(`sm tools map` lists them).")
MAKE_PR_BLOCKED = ("Blocked: never `make pr` on this machine; run focused tests with `sm gate run -- pytest PATH` and let hosted CI "
                   "run the full gate.")
DISPATCH_BLOCKED = "Blocked: never dispatch a workflow on a PR head SHA; dispatch on a branch, or use the sm command that validates a head."
SWITCH_BLOCKED = ("Blocked: bin/promote is switching the live sm (pid {pid}); no new admission or release step starts until it is done. "
                  "Wait a few minutes and run the same command again.")

PENDING = (".cache", "sm-promote-switch-pending")  # under the home directory; bin/promote writes its pid there
SM_PROGRAMS = {"sm", "steuermaschine", "sm-dev"}
ONE_WORD_STEPS = {"ship"}  # top-level verbs that are a write step by themselves: `sm ship WORKTREE` (not `group verb`)
WRITE_STEPS = {("head", "prepare"), ("head", "apply"), ("head", "admit"), ("head", "validate"), ("release", "admit"), ("release", "resume"),
               ("release", "cut"),
               ("ship",)}  # the same steps as busy_steps in bin/promote_lib.sh (a test holds them equal)

# The former git hook, verbatim.
GIT = re.compile(r"\bgit\b[^;&|\n]*?\b(push|commit)\b[^;&|\n]*")
SKIPPING = re.compile(r"(^|\s)(--no-verify|--force|--force-with-lease|-f|-n)(\s|$|=)")
COMMIT_F = re.compile(r"\s-f\b")

# A word that is a script of the retired tools directory: a file directly in it (the management checkout's tools/), a
# path to an old tool anywhere (`hz-` + name, as linked into ~/.local/bin, or hz_*.py), or the bare name of one.
# (The directory is spelled with `/+` so that no line of this file is mistaken for a call of it.)
OLD_DIR = re.compile(r"heinzel-management/+tools/+[^/\s]+$")
OLD_PATH = re.compile(r"(^|/)(hz-[a-z][a-z0-9-]*|hz_[a-z0-9_]+\.py)$")
OLD_NAME = re.compile(r"^hz-[a-z][a-z0-9-]*$")
READERS = {"cat", "less", "more", "head", "tail", "sed", "awk", "grep", "egrep", "rg", "wc", "diff", "cmp", "git", "ls", "file", "stat",
           "bat", "vim", "vi", "nano", "code", "open", "echo", "printf", "cd", "pushd", "readlink", "realpath", "which", "type", "find",
           "cp", "mv", "rm", "ln", "chmod", "touch", "shasum", "sha256sum", "md5", "jq", "sort", "uniq", "tee", "test", "["}
WRAPPERS = {"env", "exec", "nohup", "time", "nice", "command", "builtin", "sudo", "xargs", "caffeinate", "timeout", "gtimeout"}
SHELLS = {"sh", "bash", "zsh", "dash", "eval"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SEGMENT = re.compile(r"&&|\|\||[;|&\n()`]|\$\(")

SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
PR_HEAD = re.compile(r"refs/pull/|(^|/)pull/\d+/(head|merge)|headRefOid|head_?sha|head\.sha|\bheadsha\b|\$\{?[A-Za-z_]*sha[A-Za-z_]*\}?", re.I)
REF_FIELD = re.compile(r"^(ref|sha|head_sha|head)=(.*)$")


def base(word: str) -> str:
    return word.rstrip("/").rsplit("/", 1)[-1]


def words_of(segment: str) -> list[str]:
    try:
        words = shlex.split(segment, comments=False)
    except ValueError:
        words = segment.split()
    while words and ASSIGNMENT.match(words[0]):
        words.pop(0)
    return words


def segments(command: str) -> list[list[str]]:
    """The simple commands of a command line, each as words (leading VAR=value dropped); a shell's -c string and eval's
    arguments are split again."""
    out: list[list[str]] = []
    for part in SEGMENT.split(command):
        words = words_of(part)
        if not words:
            continue
        out.append(words)
        if base(words[0]) in SHELLS:
            inner = words[words.index("-c") + 1] if "-c" in words[:-1] else " ".join(words[1:]) if base(words[0]) == "eval" else ""
            if inner:
                out.extend(segments(inner))
    return out


def programs(words: list[str]) -> list[int]:
    """Indexes of the words that are run as programs: the first, the one after a wrapper (env, exec, nohup, ...) and its
    options, and the one after every `--` (sm gate run -- X, sm lim -- X)."""
    found, i = [], 0
    while i < len(words):
        found.append(i)
        if base(words[i]) not in WRAPPERS:
            break
        i += 1
        while i < len(words) and (words[i].startswith("-") or ASSIGNMENT.match(words[i]) or words[i].replace(".", "").isdigit()):
            i += 1
    found += [j + 1 for j, word in enumerate(words) if word == "--" and j + 1 < len(words)]
    return found


def git_refusal(command: str) -> str | None:
    for match in GIT.finditer(command):
        if SKIPPING.search(match.group(0)) and not (match.group(1) == "commit" and COMMIT_F.search(match.group(0))):
            return GIT_BLOCKED
    return None


def old_tool_refusal(command: str) -> str | None:
    for words in segments(command):
        if base(words[0]) in READERS:
            continue
        if any(OLD_DIR.search(w) or ("/" in w and OLD_PATH.search(w)) for w in words):
            return OLD_TOOL_BLOCKED
        if any(OLD_NAME.match(words[i]) for i in programs(words)):
            return OLD_TOOL_BLOCKED
    return None


def make_pr_refusal(command: str) -> str | None:
    for words in segments(command):
        for i in programs(words):
            if base(words[i]) in ("make", "gmake") and "pr" in words[i + 1:]:
                return MAKE_PR_BLOCKED
    return None


def head_like(value: str) -> bool:
    value = value.strip("'\"")
    return bool(SHA.match(value) or PR_HEAD.search(value))


def dispatch_refusal(command: str) -> str | None:
    # Read on the raw text, so that a ref given as $(gh pr view ... headRefOid) is still seen.
    for match in re.finditer(r"\bgh\s+(workflow\s+run|api)\b([^;\n]*?)(?=&&|\|\||;|\n|$)", command):
        kind, rest = match.group(1), match.group(2)
        if kind == "api" and "/dispatches" not in rest:
            continue
        try:
            words = shlex.split(rest)
        except ValueError:
            words = rest.split()
        for i, word in enumerate(words):
            value = None
            if word in ("--ref", "-r") and kind != "api" and i + 1 < len(words):
                value = " ".join(words[i + 1:]) if words[i + 1].startswith(("$(", "`")) else words[i + 1]  # a substitution: all of it
            elif word.startswith("--ref="):
                value = word.split("=", 1)[1]
            elif word in ("-f", "-F", "--field", "--raw-field") and i + 1 < len(words) and REF_FIELD.match(words[i + 1]):
                value = REF_FIELD.match(words[i + 1]).group(2)  # type: ignore[union-attr]
            if value is not None and head_like(value):
                return DISPATCH_BLOCKED
    return None


def switch_pending() -> int | None:
    """The pid of a running bin/promote that is switching the live sm, else None (no file, unreadable, or its pid gone)."""
    try:
        text = Path.home().joinpath(*PENDING).read_text().split()
        pid = int(text[0])
        os.kill(pid, 0)
    except (OSError, ValueError, IndexError):
        return None
    return pid


def sm_step(words: list[str], start: int) -> tuple[str, ...] | None:
    """The command path (two words; one for a top-level verb in ONE_WORD_STEPS) of an sm call whose program is words[start], skipping `--format X`."""
    rest = words[start + 1:]
    while rest and rest[0].startswith("--format"):
        rest = rest[1:] if "=" in rest[0] else rest[2:]
    if rest and rest[0] in ONE_WORD_STEPS:
        return (rest[0],)
    return (rest[0], rest[1]) if len(rest) >= 2 else None


def switch_refusal(command: str) -> str | None:
    steps = [step for words in segments(command) for i in programs(words) if base(words[i]) in SM_PROGRAMS
             for step in [sm_step(words, i)] if step in WRITE_STEPS]
    if not steps:
        return None
    pid = switch_pending()
    return SWITCH_BLOCKED.format(pid=pid) if pid is not None else None


RULES = (git_refusal, old_tool_refusal, make_pr_refusal, dispatch_refusal, switch_refusal)


def refusal(command: str) -> str | None:
    """The first floor rule that refuses `command`, as the text to show; None: the floor allows it."""
    for rule in RULES:
        why = rule(command)
        if why:
            return why
    return None
