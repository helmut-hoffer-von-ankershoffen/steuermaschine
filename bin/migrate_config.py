"""Migrate the user config to what THIS code understands. bin/promote runs it with the candidate's interpreter.

    migrate_config.py SOURCE.toml VALUES.toml OUT.toml

- Keys of a plugin table that the plugin's settings model no longer knows are dropped (and listed).
- Keys from VALUES.toml (same layout: [project], [<plugin>]) are added where the model knows them; they win.
- The result is validated with the settings models. A required key that is missing, or an invalid value, exits 1.
- Keys that are unset (no default, no value) are listed: the commands that need them will refuse and name the key.
- A path value that names a state path an earlier release used (a plugin's `sm_state_moves`) is rewritten to its new
  name in this release, so the config and `sm self migrate-state` agree (listed as "moved to the new state path").
Values are never printed, only key names: a value may be private.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


def toml_value(value):
    """Serialize nested settings as TOML, including inline tables."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(key), ensure_ascii=False)} = {toml_value(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(value, allow_nan=False, ensure_ascii=False)


def emit(data: dict) -> str:
    out = ["# steuermaschine user config for this release. Written by bin/promote (bin/migrate_config.py).",
           "# To change a value: edit ~/.config/steuermaschine/migrate-values.toml and promote, or edit this file (it is carried forward).", ""]
    for table, values in data.items():
        out.append(f"[{table}]")
        for key, value in values.items():
            if value is not None:
                out.append(f"{key} = {toml_value(value)}")
        out.append("")
    return "\n".join(out)


def new_state_path(value: str, moves: list) -> str | None:
    """The new name of a path value that is, or is inside, the old path of a state move; None for any other value."""
    expanded = Path(value).expanduser()
    for move in moves:
        old = Path(move.old)
        if expanded == old:
            return str(move.new)
        if old in expanded.parents:
            return str(Path(move.new) / expanded.relative_to(old))
    return None


def main() -> int:
    from steuermaschine import plugins

    source, values, target = (Path(a).expanduser() for a in sys.argv[1:4])
    old = tomllib.loads(source.read_text()) if source.is_file() else {}
    new = tomllib.loads(values.read_text()) if values.is_file() else {}
    project = {**(old.get("project") or {}), **(new.get("project") or {})}
    names = project.get("plugins") or ([project["plugin"]] if project.get("plugin") else [])
    if not project.get("name") or not names:
        print(f"migrate: {source} names no project and plugin", file=sys.stderr)
        return 1
    try:
        stack = plugins.load(names)
    except plugins.PluginError as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return 1
    models = stack.settings_models()
    result: dict[str, dict] = {"project": project}
    for core in ("lanes", "alarms", "github", "sonar"):  # core tables are carried; the settings loader validates them
        if old.get(core) or new.get(core):
            result[core] = {**(old.get(core) or {}), **(new.get(core) or {})}
    report = {"dropped": [], "added": [], "moved to the new state path": [], "unset": [], "ignored values": []}
    from steuermaschine import config

    state_moves = stack.state_moves({}, config.state_home() / project["name"])
    for name in names:
        fields = models[name].model_fields if name in models else {}
        table = {}
        for key, value in (old.get(name) or {}).items():
            if key in fields:
                table[key] = value
            else:
                report["dropped"].append(f"{name}.{key}")
        for key, value in (new.get(name) or {}).items():
            if key in fields:
                if table.get(key) != value:
                    report["added"].append(f"{name}.{key}")
                table[key] = value
            else:
                report["ignored values"].append(f"{name}.{key}")
        for key, value in table.items():
            if isinstance(value, str) and (moved := new_state_path(value, state_moves)) is not None:
                table[key] = moved
                report["moved to the new state path"].append(f"{name}.{key}")
        result[name] = table
    try:
        settings, _ = stack.validate({n: result.get(n, {}) for n in names}, target.parent)
    except plugins.PluginError as exc:
        print(f"migrate: the migrated config is not valid: {exc}", file=sys.stderr)
        return 1
    for name in names:
        report["unset"] += [f"{name}.{key}" for key, value in settings.of(name).items() if value is None]
    target.write_text(emit(result))
    for title, keys in report.items():
        print(f"migrate: {title}: {', '.join(sorted(keys)) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
