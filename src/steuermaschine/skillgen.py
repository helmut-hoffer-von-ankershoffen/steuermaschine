"""Role skills from the command tree: the reference of each skill is generated from the help of the commands the skill
names, and a check finds what drifted. Nothing here runs a command or reads a script; the one source is the built
command tree (`helpdoc.tree`).

A skills directory holds one directory per skill with a `SKILL.md` (front matter `name`, `description`). A skill
"names" a command when its Markdown shows it in code: `sm queue add 12` names `queue add`.

    files = skillgen.render(skills_dir, tree, prog)       # {path: text}: <skill>/references/<file> and INDEX.md
    problems = skillgen.check(skills_dir, tree, prog, old_names={"old-tool": "sm new command"})
    skillgen.link(skills_dir, [Path("~/.agent/skills")])   # one symbolic link per skill, idempotent
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

Row = Mapping[str, Any]
HELP_LINES = 40
MAX_SKILL_LINES = 150
REFERENCE = "references/tools.md"
INDEX = "INDEX.md"
CODE = re.compile(r"```.*?```|`[^`\n]+`", re.S)


def skill_dirs(skills: Path) -> list[Path]:
    return sorted(p for p in skills.glob("*") if (p / "SKILL.md").is_file())


def frontmatter(text: str) -> dict[str, str] | None:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    return {k.strip(): v.strip() for k, _, v in (ln.partition(":") for ln in m.group(1).splitlines() if ":" in ln)}


def commands_of(tree: Sequence[Row], prog: str) -> tuple[dict[str, Row], set[str]]:
    """({path: row} of every visible command and group, {paths of groups}); paths without the program name."""
    rows = {row["path"][len(prog):].strip(): row for row in tree if not row.get("hidden")}
    return rows, {path for path, row in rows.items() if row.get("group")}


def mentions(text: str, prog: str) -> list[list[str]]:
    """The word lists that follow the program name in the code of a Markdown text: [["queue", "add"], [], ...]."""
    found = []
    for code in CODE.findall(text):
        for m in re.finditer(rf"(?:^|[\s;&|(`]){re.escape(prog)}((?: [a-z][a-z0-9-]*)*)(?![\w-])", code):
            found.append(m.group(1).split())
    return found


def resolve(words: Sequence[str], rows: Mapping[str, Row], groups: set[str]) -> tuple[str, str]:
    """(the longest command path the words start with, the word that follows a group and is no command or '')."""
    best = ""
    for n in range(len(words), 0, -1):
        if " ".join(words[:n]) in rows:
            best = " ".join(words[:n])
            break
    rest = list(words[len(best.split()):])
    return best, (rest[0] if rest and best in groups | {""} else "")


def named(skill: Path, rows: Mapping[str, Row], groups: set[str], prog: str, skip: Sequence[str] = ()) -> tuple[set[str], set[str]]:
    """(command paths the skill names, mentions that are no command) over the skill's Markdown, generated files left out."""
    paths, unknown = set(), set()
    for f in sorted(skill.rglob("*.md")):
        if any(str(f.relative_to(skill)) == s for s in skip):
            continue
        for words in mentions(f.read_text(), prog):
            path, bad = resolve(words, rows, groups)
            if bad:
                unknown.add(f"{prog} {' '.join([*path.split(), bad])}")
            elif path and path not in groups:
                paths.add(path)
    return paths, unknown


def render(skills: Path, tree: Sequence[Row], prog: str, reference: str = REFERENCE) -> dict[Path, str]:
    rows, groups = commands_of(tree, prog)
    files: dict[Path, str] = {}
    by_command: dict[str, list[str]] = {}
    for sd in skill_dirs(skills):
        mine, _ = named(sd, rows, groups, prog, skip=(reference,))
        body = [f"# Commands for {sd.name} (generated from the {prog} command tree; do not edit)\n"]
        for path in sorted(mine):
            by_command.setdefault(path, []).append(sd.name)
            text = "\n".join(ln.rstrip() for ln in str(rows[path].get("help", "")).splitlines()[:HELP_LINES]) or "(no help)"
            options = " ".join(rows[path].get("options") or [])
            body.append(f"## {prog} {path}\n\n```\n{text}\n```\n" + (f"\nOptions: {options}\n" if options else ""))
        files[sd / reference] = "\n".join(body)
    index = [f"# Command index (generated from the {prog} command tree; do not edit)\n", "| Command | Changes | Skills |", "|---|---|---|"]
    for path in sorted(p for p in rows if p and p not in groups):
        index.append(f"| {prog} {path} | {'read-only' if rows[path].get('read_only') else 'writes'} | {', '.join(by_command.get(path, []))} |")
    files[skills / INDEX] = "\n".join(index) + "\n"
    return files


def generate(skills: Path, tree: Sequence[Row], prog: str, reference: str = REFERENCE) -> list[Path]:
    """Write the generated files; returns the ones whose content changed."""
    changed = []
    for path, text in render(skills, tree, prog, reference).items():
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            changed.append(path)
    return changed


def check(skills: Path, tree: Sequence[Row], prog: str, reference: str = REFERENCE, old_names: Mapping[str, str] | None = None,
          allow_words: Sequence[str] = (), unlisted_ok: Sequence[str] = ()) -> list[str]:
    """Problems, empty when the skills and the command tree agree."""
    errs: list[str] = []
    rows, groups = commands_of(tree, prog)
    seen: set[str] = set()
    dirs = skill_dirs(skills)
    allowed = {sd.name for sd in dirs} | set(allow_words)
    for sd in dirs:
        text = (sd / "SKILL.md").read_text()
        fm = frontmatter(text)
        if fm is None:
            errs.append(f"{sd.name}: no frontmatter")
            continue
        if fm.get("name") != sd.name:
            errs.append(f"{sd.name}: frontmatter name {fm.get('name')!r} != directory")
        d = fm.get("description", "")
        if not d or len(d) > 1024:
            errs.append(f"{sd.name}: description missing or over 1024 chars")
        if "Use" not in d or not re.search(r"\b(when|before)\b", d):
            errs.append(f"{sd.name}: description must say when to use it ('Use ... when/before ...')")
        if len(text.splitlines()) > MAX_SKILL_LINES:
            errs.append(f"{sd.name}: SKILL.md over {MAX_SKILL_LINES} lines")
        mine, unknown = named(sd, rows, groups, prog, skip=(reference,))
        seen |= mine
        errs += [f"{sd.name}: names `{u}`, which is no command" for u in sorted(unknown)]
        for f in sorted(p for p in sd.rglob("*.md") if str(p.relative_to(sd)) != reference):
            for old, now in sorted((old_names or {}).items()):
                if old not in allowed and re.search(rf"(?<![\w./-]){re.escape(old)}(?![\w-])", f.read_text()):
                    errs.append(f"{f.relative_to(skills)}: names the old tool {old}" + (f" (now: {now})" if now else ""))
    for path in sorted(p for p in rows if p and p not in groups and p not in seen and not any(p == u or p.startswith(u + " ") for u in unlisted_ok)):
        errs.append(f"no skill names `{prog} {path}`")
    for p, text in render(skills, tree, prog, reference).items():
        if not p.exists() or p.read_text() != text:
            errs.append(f"stale generated file: {p.relative_to(skills)} (run the generate command)")
    return errs


def link(skills: Path, targets: Sequence[Path]) -> list[str]:
    """One symbolic link per skill in each target directory; returns what was done or skipped."""
    done = []
    for base in targets:
        base.mkdir(parents=True, exist_ok=True)
        for sd in skill_dirs(skills):
            dest = base / sd.name
            if dest.is_symlink() and dest.resolve() == sd.resolve():
                continue
            if dest.exists() or dest.is_symlink():
                if not dest.is_symlink():
                    done.append(f"skip {dest}: exists and is not a symlink")
                    continue
                dest.unlink()
            dest.symlink_to(sd.resolve())
            done.append(f"linked {dest}")
    return done
