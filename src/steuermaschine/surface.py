"""The visible surface: the few commands a caller types, mounted over the whole tree, which keeps working underneath.

A plugin declares it with four kinds of objects in its native list (next to `Group` and `Alias`):

    Surface(verbs=(("check [WORKTREE]", "run the cheap checks"), ...), visible=("check", "work", "work add", ...))
    Verb("work add", "beads tasks add")          # the same command, mounted at a new visible path
    Renamed("beads tasks add", "work add")       # the old name becomes a stub that names the new one
    Default("release", "resume", empty="train status")   # `sm release 1.2.3` runs `sm release resume 1.2.3`

`apply` changes the built Click tree in place, in this order: mount the verbs (copies, so a hidden old path and a
visible new one never share a `hidden` flag), install the stubs, route the defaults, hide everything that is not
visible, and give every group the unknown-command resolver. Nothing is deleted: a hidden command keeps its path,
because service jobs and hooks call it.

A stub for a read-only old name runs the new command with the same arguments and prints `Renamed: sm NEW (was: sm
OLD)` on stderr. A stub for a writing old name prints that line and exits 2 (usage); it runs nothing. A `Renamed`
with `stub=False` keeps the old command as it is (an internal caller still runs it) and only translates the old name
in texts such as NEXT (`translate`).

The resolver: a word that is not a command is looked up against every command name, hidden ones included, and
mapped through the renames. One match prints `Did you mean: sm X`; several list them. It always exits 2 and never
runs a guess.
"""

from __future__ import annotations

import copy
import difflib
import re
import shlex
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import typer

from .helpdoc import READ_ONLY, H

JARGON = ("admission", "receipt", "pin", "packet", "plan", "window", "train", "candidate")
HELP_MAX_LINES = 60
UNKNOWN = re.compile(r"No such command '([^']+)'")


@dataclass(frozen=True)
class Verb:
    """Mount the command at `target` (a leaf) also at `path`. The new path is visible; the old one keeps working."""

    path: str
    target: str


@dataclass(frozen=True)
class Renamed:
    """The old name `old` moved to `new` (the new command as typed, with any fixed leading arguments: `why main`)."""

    old: str
    new: str
    stub: bool = True  # False: the old command keeps running unchanged (internal callers); only texts are translated

    @property
    def path(self) -> str:
        return f"renamed {self.old}"


@dataclass(frozen=True)
class Default:
    """`sm GROUP WORD ...` with WORD not a subcommand runs `sm GROUP TARGET WORD ...`; bare `sm GROUP` runs `empty`."""

    group: str
    target: str
    empty: str = ""

    @property
    def path(self) -> str:
        return f"default {self.group}"


@dataclass(frozen=True)
class Surface:
    """The visible commands. `verbs`: (usage, plain description) for `sm -h`, in the order of a working day.
    `visible`: every visible path (groups and leaves, without the program name). `rules`: extra lines for `sm -h`."""

    verbs: tuple[tuple[str, str], ...]
    visible: tuple[str, ...]
    rules: tuple[str, ...] = ()
    path: str = "surface"


def of(items: Iterable[Any]) -> tuple[Surface | None, list[Verb], list[Renamed], list[Default]]:
    items = list(items)
    surface = next((i for i in items if isinstance(i, Surface)), None)
    return (surface, [i for i in items if isinstance(i, Verb)], [i for i in items if isinstance(i, Renamed)],
            [i for i in items if isinstance(i, Default)])


# ----------------------------------------------------------------------------- the tree
def walk(node: Any, words: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any, Any]]:
    """(words, command, parent) for every command below `node`, depth first, in insertion order."""
    for name, child in list(getattr(node, "commands", {}).items()):
        yield (*words, name), child, node
        yield from walk(child, (*words, name))


def find(root: Any, path: str) -> Any:
    node = root
    for word in path.split():
        node = getattr(node, "commands", {}).get(word)
        if node is None:
            return None
    return node


def _group(root: Any, words: list[str]) -> Any:
    """The group at `words`, made (as a plain group of the root's class) when it is missing."""
    node = root
    for word in words:
        child = node.commands.get(word)
        if child is None:
            child = type(root)(name=word, help=H(f"{word}: the commands below.", "Use when one of them is what you need.", "nothing by itself.",
                                                 f"sm {' '.join(words)} --help", "sm"), no_args_is_help=True,
                               context_settings=dict(getattr(root, "context_settings", {}) or {}))
            node.commands[word] = child
        node = child
    return node


def split_new(new: str, root: Any) -> tuple[str, list[str]]:
    """('work add', []) or ('why', ['main']): the command words of `new` and its fixed arguments."""
    words, node, path = new.split(), root, []
    for word in words:
        child = getattr(node, "commands", {}).get(word)
        if child is None:
            break
        path.append(word)
        node = child
    return " ".join(path), words[len(path):]


def usage_of(root: Any, new: str, prog: str) -> str:
    """`sm review sonar PR`: the new command with the metavars of its required arguments."""
    path, fixed = split_new(new, root)
    command = find(root, path)
    args = [str(p.metavar or p.name).upper() for p in getattr(command, "params", []) if not getattr(p, "opts", [""])[0].startswith("-")
            and getattr(p, "required", False)]
    return " ".join([prog, path, *fixed, *([] if fixed else args)]).strip()


# ----------------------------------------------------------------------------- stubs
def _line(prog: str, new: str, old_path: str) -> str:
    return f"Renamed: {prog} {new} (was: {old_path})"


def read_stub(target: Any, name: str, new: str, fixed: list[str], prog: str, old: str, echo: Callable[..., None]) -> Any:
    """A copy of the new command under the old name: it prints the renamed line on stderr, then runs the new one."""
    stub = copy.copy(target)
    stub.name, stub.hidden = name, True
    stub.help = H(f"Renamed: {prog} {new} (was: {prog} {old}).", f"Use for callers that still type the old name; type `{prog} {new}` instead.",
                  f"nothing by itself (read-only); it runs `{prog} {new}` and prints the new name on stderr.", f"{prog} {new} --help", f"{prog} {new}")
    stub.short_help = f"Renamed: {prog} {new}"
    parse, invoke = target.parse_args, target.invoke

    def parse_args(ctx: Any, args: list[str]) -> list[str]:
        return parse(ctx, [*fixed, *args])  # type: ignore[no-any-return]

    def run(ctx: Any) -> Any:
        echo(_line(prog, new, ctx.command_path), err=True)
        return invoke(ctx)

    stub.parse_args, stub.invoke = parse_args, run
    return stub


def write_stub(name: str, new: str, prog: str, old: str, fail: Callable[..., Any], context: dict[str, Any]) -> Any:
    """A command that only says where the old name went, and exits 2 (usage). It runs nothing."""
    app = typer.Typer(add_completion=False, rich_markup_mode=None)

    @app.command(name, hidden=True, short_help=f"Renamed: {prog} {new}",
                 context_settings={**context, "ignore_unknown_options": True, "allow_extra_args": True},
                 help=H(f"Renamed: {prog} {new} (was: {prog} {old}).", f"Use for callers that still type the old name: it only says where the command went; type `{prog} {new}`.",
                        "nothing; it prints the new name and exits 2 (usage).", f"{prog} {new} --help", f"{prog} {new}"))
    def stub(ctx: typer.Context, rest: list[str] | None = typer.Argument(None, metavar="[ARGS]", help="Ignored: the old arguments.")) -> None:
        fail(_line(prog, new, ctx.command_path), 2, hint=f"{prog} {new} --help")

    return typer.main.get_command(app)


# ----------------------------------------------------------------------------- the resolver
def suggest(root: Any, word: str, renames: list[Renamed], prog: str) -> list[str]:
    """What a caller who typed `word` most likely meant: every command named `word` (hidden ones too, or the joined
    path `ci-sonar-close`), mapped through the renames; else the close matches of the names. Visible ones first."""
    paths = [" ".join(words) for words, _, _ in walk(root)]
    hits = [p for p in paths if p.split()[-1] == word or "-".join(p.split()) == word]
    if not hits:
        names = sorted({p.split()[-1] for p in paths})
        close = set(difflib.get_close_matches(word, names, n=3, cutoff=0.75))
        hits = [p for p in paths if p.split()[-1] in close]
    out: list[str] = []
    for path in hits:
        rename = next((r for r in sorted(renames, key=lambda r: -len(r.old)) if path == r.old or path.startswith(r.old + " ")), None)
        line = usage_of(root, rename.new, prog) if rename else usage_of(root, path, prog)
        if line not in out:
            out.append(line)
    hidden = {usage_of(root, p, prog) for p in paths if is_hidden(root, p)}
    return sorted(out, key=lambda line: (line in hidden, line))


def is_hidden(root: Any, path: str) -> bool:
    node = root
    for word in path.split():
        node = getattr(node, "commands", {}).get(word)
        if node is None or getattr(node, "hidden", False):
            return True
    return False


def message(word: str, found: list[str]) -> str:
    if not found:
        return f"No such command '{word}'."
    if len(found) == 1:
        return f"No such command '{word}'. Did you mean: {found[0]}"
    return f"No such command '{word}'. Did you mean one of: " + "; ".join(found)


def install_resolver(root: Any, renames: list[Renamed], prog: str) -> None:
    """Every group answers an unknown word with `message(...)`, as a usage error (exit 2)."""
    for _, node, _ in [((), root, None), *walk(root)]:
        if not hasattr(node, "commands"):
            continue
        resolve = node.resolve_command

        def resolve_command(ctx: Any, args: list[str], resolve: Any = resolve) -> Any:
            try:
                return resolve(ctx, args)
            except Exception as exc:
                found = UNKNOWN.search(str(getattr(exc, "message", "")))
                if found is None or not hasattr(exc, "message"):
                    raise
                exc.message = message(found.group(1), suggest(root, found.group(1), renames, prog))
                raise

        node.resolve_command = resolve_command


# ----------------------------------------------------------------------------- applying the surface
def apply(root: Any, items: Iterable[Any], writes: dict[str, bool], fail: Callable[..., Any], echo: Callable[..., None], prog: str,
          project_line: str = "") -> None:
    surface, verbs, renames, defaults = of(items)
    if surface is None and not renames:
        install_resolver(root, renames, prog)
        return
    context = dict(getattr(root, "context_settings", {}) or {})
    originals = {v.path: find(root, v.target) for v in verbs}
    for verb in verbs:  # 1. mount copies at the new paths
        target = originals[verb.path]
        if target is None:
            continue
        *parents, name = verb.path.split()
        twin = copy.copy(target)
        twin.name, twin.hidden = name, False
        _group(root, parents).commands[name] = twin
    for rename in renames:  # 2. stubs at the old paths
        if not rename.stub or (old := find(root, rename.old)) is None:
            continue
        path, fixed = split_new(rename.new, root)
        target = find(root, path)
        if target is None:
            continue
        *parents, name = rename.old.split()
        read_only = (not writes[f"{prog} {rename.old}"]) if f"{prog} {rename.old}" in writes else bool(READ_ONLY.search(old.help or ""))
        stub = read_stub(target, name, rename.new, fixed, prog, rename.old, echo) if read_only else write_stub(name, rename.new, prog, rename.old, fail, context)
        _group(root, parents).commands[name] = stub
    for default in defaults:  # 3. a group that runs a default subcommand
        if (group := find(root, default.group)) is not None:
            route(group, default)
    if surface is not None:  # 4. hide what is not visible
        hide(root, surface.visible)
        root.help = root_help(surface, prog, project_line)
        root.format_help = lambda ctx, formatter: formatter.write(root.help.replace("\b\n", "") + "\n")
    install_resolver(root, renames, prog)


def route(group: Any, default: Default) -> None:
    parse = group.parse_args
    group.no_args_is_help = False

    def parse_args(ctx: Any, args: list[str]) -> list[str]:
        if not args and default.empty:
            args = default.empty.split()
        elif args and args[0] not in group.commands and not args[0].startswith("-"):
            args = [default.target, *args]
        return parse(ctx, args)  # type: ignore[no-any-return]

    group.parse_args = parse_args


def hide(root: Any, visible: Iterable[str]) -> None:
    shown = set(visible)
    keep = shown | {" ".join(p.split()[:n]) for p in shown for n in range(1, len(p.split()))}
    seen = {id(find(root, p)) for p in keep if find(root, p) is not None}
    for words, node, parent in list(walk(root)):
        path = " ".join(words)
        if path in keep:
            node.hidden = False
            continue
        if id(node) in seen:  # the same object is also visible elsewhere: hide a copy here
            node = copy.copy(node)
            parent.commands[words[-1]] = node
        node.hidden = True


def root_help(surface: Surface, prog: str, project_line: str) -> str:
    width = max(len(f"{prog} {u}".strip()) for u, _ in surface.verbs) + 2
    lines = [f"{prog} (steuermaschine) takes a code change from a commit to a live release, one step at a time.",
             f"Run `{prog}`. Do what NEXT says. Use `{prog} why X` when something is unclear.", "",
             "Commands, in the order of a working day:"]
    lines += [f"  {(prog + ' ' + usage).strip():<{width}}{text}" for usage, text in surface.verbs]
    lines += ["", "Rules:",
              "  --live          a command that writes outside this machine (push, pull request, merge, publish) runs only with",
              "                  --live; without it, it shows what it would do and changes nothing there.",
              "  --format json   works on every command: exactly one JSON document on stdout.",
              f"  -h              works on every command: what it does, what it changes, an example. `{prog} X -h` runs nothing.",
              *surface.rules, "",
              "Exit codes: 0 done; 1 refused (the line says why and which command fixes it); 2 usage (a wrong or renamed",
              "command: the line names the right one); 3 an alarm holds; 70 tool error; 75 try again later (the same command)."]
    if project_line:
        lines += ["", project_line]
    return "\b\n" + "\n".join(lines)


def help_findings(text: str) -> list[str]:
    """The `sm -h` contract: at most 60 lines, the loop, exit codes, --format json, and no word that needs a skill."""
    plain = text.replace("\b\n", "")
    out = []
    if (n := len(plain.splitlines())) > HELP_MAX_LINES:
        out.append(f"sm -h: {n} lines, more than {HELP_MAX_LINES}")
    for need in ("Do what NEXT says", "Exit codes:", "--format json", "--live"):
        if need not in plain:
            out.append(f"sm -h: does not say '{need}'")
    for word in JARGON:
        if re.search(rf"(?i)\b{word}s?\b", plain):
            out.append(f"sm -h: uses the word '{word}', which needs a skill to understand")
    return out


# ----------------------------------------------------------------------------- texts that name commands
def translate(text: str, renames: Iterable[Renamed], prog: str = "sm") -> str:
    """Replace every old command name in `text` by its new one (`sm ci why 12` -> `sm why 12`); the longest name first."""
    for rename in sorted(renames, key=lambda r: -len(r.old)):
        text = re.sub(rf"(?<![\w/-]){re.escape(prog)} {re.escape(rename.old)}(?![\w-])", f"{prog} {rename.new}".strip(), text)
    return text


def commands_in(text: str, prog: str = "sm") -> list[list[str]]:
    """The command lines of `prog` in a text such as a NEXT line: each `sm ...` up to `&&`, `;`, `|`, `(` or the end."""
    out = []
    for found in re.finditer(rf"(?<![\w/-]){re.escape(prog)}((?: [^&;|()]*)?)", text):
        try:
            words = shlex.split(found.group(1))
        except ValueError:
            words = found.group(1).split()
        out.append(words)
    return out


def dry_run(root: Any, words: list[str], prog: str = "sm") -> str:
    """Parse one command line against the tree without running it: "" when it is a visible command whose arguments
    parse, else what is wrong (a hidden or unknown command, a usage error). `--help` lines are not parsed."""
    if "-h" in words or "--help" in words:
        words = [w for w in words if w not in ("-h", "--help")]
    node, ctx, rest, path = root, None, list(words), []
    try:
        while True:
            ctx = node.make_context(path[-1] if path else prog, rest, parent=ctx, resilient_parsing=False)
            if not hasattr(node, "commands"):
                return ""
            args = [*ctx._protected_args, *ctx.args]
            if not args:
                return "" if getattr(node, "invoke_without_command", False) or path == [] else f"{prog} {' '.join(path)}: needs a command"
            name, child, rest = node.resolve_command(ctx, args)
            path.append(name)
            if getattr(child, "hidden", False):
                return f"{prog} {' '.join(path)} is hidden"
            node = child
    except Exception as exc:  # a usage error of the parser
        text = exc.format_message() if hasattr(exc, "format_message") else str(exc)
        return f"{prog} {' '.join(words)}: {text}"
