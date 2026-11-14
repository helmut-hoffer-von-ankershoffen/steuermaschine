"""Running the command tree: `--format text|json` at every level, and one JSON document in json format.

- `add_format_option(root, choose)`: every command and group below the root accepts `--format text|json`, so
  `sm pr status 796 --format json` means the same as `sm --format json pr status 796`. It is a real option of each
  command and therefore in every `--help`.
- `scan_format(args)`: what the command line asks for, read before anything is parsed, so that an error of the
  parser itself (an unknown command, a missing argument) is already printed in the right form.
- `run(root, args, prog, as_json)`: runs the tree and returns the exit code. In json format a usage error or an
  unexpected exception is one `Error` document on stdout (`{error, code, hint}`) and nothing else is on stdout;
  in text format a usage error is what the parser prints and an unexpected exception is raised unchanged.

`--help` prints help text in either format: it is an answer for a person, not a result.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator
from typing import Any

import typer

from . import INTERNAL, USAGE
from .models import Error

FORMATS = ("text", "json")
OPTION = "--format"
HELP = "Output of this command: text, or json = exactly one JSON document on stdout. The same as the root option."


def scan_format(args: list[str]) -> str | None:
    """The value of the last `--format VALUE` or `--format=VALUE` on the command line, None when there is none.
    Words after a bare `--` are arguments and are not read."""
    found: str | None = None
    for i, arg in enumerate(args):
        if arg == "--":
            break
        if arg == OPTION:
            found = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith(OPTION + "="):
            found = arg.split("=", 1)[1]
    return found


def commands_below(root: Any) -> Iterator[Any]:
    """Every command and group below `root`, depth first; `root` itself is not included."""
    for child in getattr(root, "commands", {}).values():
        yield child
        yield from commands_below(child)


def format_option(choose: Callable[[str], None]) -> Any:
    """One `--format` option object, built by Typer so that it is whatever option class this Typer version uses."""

    def check(value: str | None) -> None:
        if value is None:
            return
        if value not in FORMATS:
            raise typer.BadParameter(f"{OPTION} is text or json, not '{value}'")
        choose(value)

    probe = typer.Typer(add_completion=False)

    @probe.command()
    def carrier(format_: str | None = typer.Option(None, OPTION, metavar="text|json", help=HELP, callback=check, is_eager=True, expose_value=False)) -> None:
        """Never run: it only carries the option."""

    return typer.main.get_command(probe).params[0]


def add_format_option(root: Any, choose: Callable[[str], None]) -> None:
    """Give every command and group below `root` the option `--format text|json`. A command that defines its own
    `--format` keeps it. `choose(value)` is called while the command line is parsed, before the command runs."""
    option = format_option(choose)
    for command in commands_below(root):
        if not any(OPTION in getattr(param, "opts", []) for param in command.params):
            command.params.append(option)


def _usage(exc: BaseException) -> tuple[str, int, str] | None:
    """(message, exit code, command path) of a parser error, None for any other exception. Read by shape, not by
    class: Typer keeps its parser's exception classes private."""
    message, code = getattr(exc, "format_message", None), getattr(exc, "exit_code", None)
    if not callable(message) or not isinstance(code, int) or not callable(getattr(exc, "show", None)):
        return None
    ctx = getattr(exc, "ctx", None)
    return str(message()), code, str(getattr(ctx, "command_path", "") or "")


def run(root: Any, args: list[str], prog: str, as_json: Callable[[], bool], echo: Callable[..., None] = typer.echo) -> int:
    """Run the command tree once and return the exit code. It never lets a parser error print text on stdout."""
    try:
        result = root.main(args=list(args), prog_name=prog, standalone_mode=False)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
    except typer.Abort:
        if as_json():
            echo(Error(error="aborted", code=1).model_dump_json())
        else:
            echo("Aborted!", err=True)
        return 1
    except Exception as exc:
        usage = _usage(exc)
        if usage is None:
            if not as_json():
                raise
            echo(traceback.format_exc().rstrip(), err=True)
            echo(Error(error=f"{type(exc).__name__}: {exc}", code=INTERNAL, hint="a bug in the tool or a plugin; the traceback is on stderr").model_dump_json())
            return INTERNAL
        message, code, path = usage
        if as_json():
            echo(Error(error=message, code=code or USAGE, hint=f"{path or prog} --help").model_dump_json())
        else:
            exc.show()  # type: ignore[attr-defined]  # the parser's own text: usage line, then the error, on stderr
        return code or USAGE
    return result if isinstance(result, int) and not isinstance(result, bool) else 0
