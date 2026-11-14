"""`sm doc skills generate|check|link`: the role skills against the command tree. The logic is the core's
`steuermaschine.skillgen`; this module names the skills directory, the old tool names and the link targets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import PROG, USAGE, helpdoc, skillgen
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime

from .coord_lib.base import tracker_dir

UNIT = ("skills.py", "coord_lib")  # the review unit: the default skills directory is in the tracker's repository (coord_lib)


class SkillsSettings(BaseModel):
    """Settings of the role skills (mixin of the plugin's settings table)."""

    skills_dir: Path | None = Field(None, description="The directory with one directory per role skill; none: skills/ in the tracker's repository.")
    skills_reference: str = Field(skillgen.REFERENCE, description="The generated reference file inside each skill.")
    skills_link_dirs: list[str] = Field([], description="Directories that get one symbolic link per skill (the agents' skill directories).")
    # old name kept: agent types of the skills on disk
    skills_allow_words: list[str] = Field(["hz-builder", "hz-chore"], description="Words that look like old tool names and are not (agent types, flags).")
    skills_unlisted_ok: list[str] = Field(["self", "tools", "review", "focus", "work"], description="Command paths (and everything below them) that no skill has to name.")


SKILLS = Group("doc skills", H(
    "doc skills: keep the role skills and the command tree from drifting apart.",
    "Use after a command was added, renamed or removed, and after a skill was edited.",
    "generate writes the generated reference files; link writes symbolic links; check only reads.",
    "sm doc skills check",
    "sm doc skills generate"))


class SkillFiles(Model):
    """Generated reference files that changed."""

    skills_dir: str
    changed: list[str] = []

    def render(self) -> list[str]:
        return [*(f"wrote {name}" for name in self.changed), f"generated: {len(self.changed)} file(s) changed"]


class SkillCheck(Model):
    """Where the skills and the command tree disagree."""

    skills_dir: str
    problems: list[str] = []

    @property
    def exit_code(self) -> int:
        return 1 if self.problems else 0

    def render(self) -> list[str]:
        return [*self.problems, "OK" if not self.problems else f"{len(self.problems)} problem(s)"]


class SkillLinks(Model):
    actions: list[str] = []
    targets: list[str] = []

    def render(self) -> list[str]:
        return self.actions or [f"every skill is linked in {', '.join(self.targets)}"]


def _where(ctx: typer.Context) -> tuple[Any, Path, list[dict[str, Any]]]:
    rt: Any = runtime()
    s = rt.settings or {}
    skills = Path(os.path.expanduser(str(s["skills_dir"]))) if s.get("skills_dir") else tracker_dir(s) / "skills"
    if not skills.is_dir():
        raise Refuse(f"the skills directory {skills} does not exist", "sm self config   (settings skills_dir, coord_tracker_dir)", code=USAGE)
    return s, skills, helpdoc.tree(ctx.find_root().command, PROG)


def _old_names() -> dict[str, str]:
    from ..toolmap import ROWS

    return {row.tool: (f"{PROG} {row.path}" if row.path else "") for row in ROWS}


@native("doc skills generate", writes=True, replaces=("hz-skills",), files=UNIT, help=H(
    "Write each skill's command reference and the command index from the command tree.",
    "Use after a command or a skill changed, before `sm doc skills check`.",
    "rewrites <skill>/references/tools.md of every skill and INDEX.md in the skills directory; nothing else. Not committed.",
    "sm doc skills generate",
    "sm doc skills check",
    "A skill's reference holds the help of every command the skill names in code (`sm queue add 12` names `queue add`),\n"
    "first 40 lines each, and its options. The index lists every command, whether it writes, and the skills naming it.\n"
    "No command is run and no script is read: the source is the built command tree."))
def doc_skills_generate(ctx: typer.Context) -> SkillFiles:
    s, skills, tree = _where(ctx)
    changed = skillgen.generate(skills, tree, PROG, str(s.get("skills_reference") or skillgen.REFERENCE))
    return SkillFiles(skills_dir=str(skills), changed=[str(p.relative_to(skills)) for p in changed])


@native("doc skills check", writes=False, replaces=("hz-skills",), help=H(
    "Find drift between the role skills and the command tree; exit 1 when there is any.",
    "Use before committing a skill or a command change, and in the quality gate.",
    "nothing (read-only).",
    "sm doc skills check",
    "fix what it lists, then `sm doc skills generate`.",
    "Problems: bad front matter, name unlike the directory, a description that does not say when to use the skill,\n"
    "SKILL.md over 150 lines, a named command that does not exist, an old tool name (with the command it became),\n"
    "a command no skill names, a stale generated file."))
def doc_skills_check(ctx: typer.Context) -> SkillCheck:
    s, skills, tree = _where(ctx)
    problems = skillgen.check(skills, tree, PROG, str(s.get("skills_reference") or skillgen.REFERENCE), _old_names(),
                              list(s.get("skills_allow_words") or []), list(s.get("skills_unlisted_ok") or []))
    return SkillCheck(skills_dir=str(skills), problems=problems)


@native("doc skills link", writes=True, replaces=("hz-skills",), files=UNIT, help=H(
    "Link every role skill into the agents' skill directories.",
    "Use after a skill was added or the skills directory moved.",
    "creates or repairs one symbolic link per skill in each directory of the setting skills_link_dirs; nothing else.",
    "sm doc skills link",
    "sm doc skills check",
    "A correct link is left alone; a link that points elsewhere is replaced; a real file or directory of the same name\n"
    "is never touched (it is reported)."))
def doc_skills_link(ctx: typer.Context) -> SkillLinks:
    s, skills, _tree = _where(ctx)
    targets = [Path(os.path.expanduser(str(d))) for d in s.get("skills_link_dirs") or []]
    if not targets:
        raise Refuse("no link directory is configured: set skills_link_dirs in the plugin's settings table", "sm self config", code=USAGE)
    return SkillLinks(actions=skillgen.link(skills, targets), targets=[str(t) for t in targets])
