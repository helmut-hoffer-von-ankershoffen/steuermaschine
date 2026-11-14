"""`sm-guard`: a Claude Code PreToolUse hook on Bash that stays safe when sm, or a plugin, cannot load.

    sm-guard --floor MODULE [--input FILE]

It reads the hook JSON (`{"tool_input": {"command": ...}}`) on stdin and exits 0 (allow) or 2 (block; the reason on
stderr). It imports nothing but the standard library and the floor module, so it starts in a few milliseconds and does
not depend on the packages a full `sm` needs.

The floor module (dotted name; standard library only) has `refusal(command) -> str | None`, the hard-coded rules that
must always hold, and may name its full rule set as `FULL = "dotted.module:function"` (same signature). Both modules are
loaded by their file, without running the `__init__` of their packages (which may import much more).

Decision:
- input that is not the hook JSON: block (fail closed);
- the floor cannot load or raises: block (fail closed: it cannot be told what the floor would refuse);
- the floor refuses: block;
- the full rule set refuses: block; it cannot load or raises: the floor's answer stands (allow), with a note on stderr;
- any other error inside sm-guard: block.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

ALLOW, BLOCK = 0, 2
USAGE_TEXT = "usage: sm-guard --floor MODULE [--input FILE]   (the hook JSON on stdin; exit 0 allows, 2 blocks)"


class Unreadable(Exception):
    """The hook input is not the JSON the guard expects."""


def read_command(text: str) -> str:
    """`tool_input.command` of the hook JSON; anything else raises Unreadable. No command at all is an empty one."""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise Unreadable(str(err)) from None
    if not isinstance(data, dict):
        raise Unreadable("the hook input is not a JSON object")
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        raise Unreadable("tool_input is not an object")
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        raise Unreadable("command is not a string")
    return command


def load_file_module(dotted: str) -> ModuleType:
    """The module `dotted`, executed from its file under the top-level package's directory, without importing the
    packages in between (their `__init__` is not run)."""
    top, _, rest = dotted.partition(".")
    spec = importlib.util.find_spec(top)
    if spec is None:
        raise ImportError(f"no module {top}")
    if rest:
        locations = list(spec.submodule_search_locations or [])
        if not locations:
            raise ImportError(f"{top} is not a package")
        path = Path(locations[0]).joinpath(*rest.split(".")).with_suffix(".py")
    else:
        path = Path(spec.origin or "")
    file_spec = importlib.util.spec_from_file_location(f"_sm_guard.{dotted}", path)
    if file_spec is None or file_spec.loader is None or not path.is_file():
        raise ImportError(f"no file for {dotted}: {path}")
    module = importlib.util.module_from_spec(file_spec)
    file_spec.loader.exec_module(module)
    return module


def load_rule(target: str) -> Callable[[str], str | None]:
    module_name, _, function = target.partition(":")
    rule = getattr(load_file_module(module_name), function or "refusal")
    if not callable(rule):
        raise TypeError(f"{target} is not callable")
    return rule  # type: ignore[no-any-return]


def parse(argv: list[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        name = argv[i]
        if name in ("--floor", "--input") and i + 1 < len(argv):
            opts[name[2:]] = argv[i + 1]
            i += 2
        elif name.startswith(("--floor=", "--input=")):
            key, _, value = name[2:].partition("=")
            opts[key] = value
            i += 1
        else:
            raise ValueError(f"unknown argument {name!r}")
    if not opts.get("floor"):
        raise ValueError("--floor MODULE is required")
    return opts


def decide(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
    """(exit code, text for stderr)."""
    try:
        opts = parse(argv)
    except ValueError as err:
        return BLOCK, f"sm-guard: {err}\n{USAGE_TEXT}"
    try:
        source = opts.get("input", "-")
        text = (sys.stdin.read() if stdin is None else stdin) if source == "-" else Path(source).read_text()
        command = read_command(text)
    except (Unreadable, OSError) as err:
        return BLOCK, f"Blocked: the hook input could not be read ({err})."
    try:
        floor = load_file_module(opts["floor"])
        why = floor.refusal(command)
    except Exception as err:
        return BLOCK, f"Blocked: the floor rules ({opts['floor']}) could not be applied: {err!r}. Fix the install or roll the hook back."
    if why:
        return BLOCK, str(why)
    full = getattr(floor, "FULL", "")
    if not full:
        return ALLOW, ""
    try:
        why = load_rule(str(full))(command)
    except Exception as err:
        return ALLOW, f"sm-guard: the full rules ({full}) are not available ({err!r}); the floor allowed this command."
    return (BLOCK, str(why)) if why else (ALLOW, "")


def main(argv: list[str] | None = None) -> None:
    try:
        code, text = decide(sys.argv[1:] if argv is None else argv)
    except BaseException as err:  # an exit code other than 0 or 2 would let the call pass: block instead
        code, text = BLOCK, f"Blocked: sm-guard failed ({err!r})."
    if text:
        print(text, file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
