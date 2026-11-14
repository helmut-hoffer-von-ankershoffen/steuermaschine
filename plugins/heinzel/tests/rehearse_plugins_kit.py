"""A plugins repository for the rehearsal's Consumer lock checks, on disk, without a network or a process.

`cli` below is the part of the plugins repository's own tooling (`heinzel_plugin_tools/cli.py` on plugins main) that
the rehearsal's lock check calls: `projects`, `core_metadata` and `validate_lock` with their helpers, copied from
plugins main (3efe891) with only the URL branch of `core_metadata` left out. `lock_check` runs the rehearsal's own
script (`rehearsal.LOCK_CHECK`) in this process against it, as `uv run ... python -c` runs it in the plugins worktree."""

import contextlib
import io
import json
import re
import sys
import tomllib
import types
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from steuermaschine.protocols import Result

# Core 0.9.39's dependencies, and what 0.9.40 added: pyjwt[crypto] through a Core extra (the yluq case).
CORE_0939 = ["httpx>=0.28", "pydantic>=2.11"]
CORE_0940 = [*CORE_0939, 'pyjwt[crypto]>=2.10; extra == "github"']


def wheel(path: Path, version: str, requires: list[str]) -> Path:
    """A core wheel with just its METADATA."""
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f"Metadata-Version: 2.4\nName: heinzel\nVersion: {version}\n" + "".join(f"Requires-Dist: {r}\n" for r in requires)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"heinzel-{version}.dist-info/METADATA", metadata)
    return path


def plugin(tree: Path, name: str, locked_core: list[str], core_range: str = ">=0.9.1,<0.10", version: str = "0.1.0") -> None:
    """One plugin of plugins main with its lock, as `make lock` wrote it against a core with `locked_core`."""
    home = tree / "plugins" / name
    home.mkdir(parents=True)
    dependencies = [f"heinzel{core_range}"]
    (home / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = {json.dumps(dependencies)}\n'
        f'[project.entry-points."heinzel.modules"]\nx = "x"\n[build-system]\nrequires = ["hatchling"]\n')
    inputs = sorted([*dependencies, "hatchling"])
    record = {"format": 1, "sdk": 1, "inputs": inputs, "core": {"version": "0.9.39", "dependencies": sorted(str(Requirement(v)) for v in locked_core)}}
    (home / "plugin-lock.json").write_text(json.dumps(record))
    (home / "requirements.lock").write_text("")


# ----------------------------------------------------------------------------- copied from plugins main's cli.py
@dataclass(frozen=True)
class Project:
    directory: Path
    name: str
    version: str
    metadata: dict

    @property
    def options(self) -> dict:
        return self.metadata.get("tool", {}).get("heinzel", {}).get("plugin", {})


def make_cli(root: Path) -> types.ModuleType:
    cli = types.ModuleType("heinzel_plugin_tools.cli")

    def projects(root: Path = root) -> list[Project]:
        paths = sorted((root / "plugins").glob("*/pyproject.toml"))
        if (root / "pyproject.toml").is_file():
            paths.append(root / "pyproject.toml")
        found = []
        for path in paths:
            metadata = tomllib.loads(path.read_text())
            project = metadata.get("project", {})
            if not project.get("entry-points", {}).get("heinzel.modules"):
                continue
            name, version = project["name"], project["version"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 100:
                raise ValueError(f"{path}: use a canonical distribution name")
            found.append(Project(path.parent, name, version, metadata))
        return found

    def requirements(project: Project, group: str) -> list[str]:
        found = []
        for requirement in project.metadata.get("dependency-groups", {}).get(group, []):
            if isinstance(requirement, str):
                found.append(requirement)
            elif "include-group" in requirement:
                found += requirements(project, requirement["include-group"])
        return found

    def core_metadata(source: str) -> dict:
        content = Path(source).read_bytes()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            metadata = Parser().parsestr(archive.read(name).decode())
        if canonicalize_name(metadata["Name"]) != "heinzel":
            raise ValueError("Select a built Heinzel core wheel")
        return {"version": metadata["Version"], "dependencies": sorted(str(Requirement(value)) for value in metadata.get_all("Requires-Dist", []))}

    def dependency_inputs(project: Project) -> list[str]:
        return sorted([*project.metadata["project"].get("dependencies", []), *requirements(project, "test"), *requirements(project, "dev"),
                       *requirements(project, "tooling"), *project.metadata["build-system"]["requires"]])

    def supports_core(project: Project, version: str) -> bool:
        matches = [Requirement(value) for value in project.metadata["project"].get("dependencies", []) if canonicalize_name(Requirement(value).name) == "heinzel"]
        return bool(matches) and all(Version(version) in item.specifier for item in matches)

    def validate_lock(project: Project, core: str | None) -> dict:
        path = project.directory / "plugin-lock.json"
        hint = f"make lock NAME={project.name} CORE=<compatible-core-wheel>"
        if not path.is_file() or not (project.directory / "requirements.lock").is_file():
            raise ValueError(f"{project.name} needs its own dependency lock. Run: {hint}")
        record = json.loads(path.read_text())
        if record["inputs"] != dependency_inputs(project):
            raise ValueError(f"{project.name} dependencies changed. Run: {hint}")
        metadata = cli.core_metadata(core) if core else record["core"]  # a module global in the original
        if not supports_core(project, metadata["version"]):
            raise ValueError(f"{project.name} does not allow core {metadata['version']}")
        if metadata["dependencies"] != record["core"]["dependencies"]:
            raise ValueError(f"Core dependencies changed; relock only this consumer. Run: {hint}")
        return record

    cli.projects, cli.core_metadata, cli.validate_lock = projects, core_metadata, validate_lock
    return cli


def lock_check(script: str, argv: list[str], cwd: Path) -> Result:
    """Run the rehearsal's lock check script as `python -c` would, in this process, with the copied tooling."""
    package = types.ModuleType("heinzel_plugin_tools")
    package.cli = make_cli(Path(cwd))  # type: ignore[attr-defined]
    saved = {name: sys.modules.get(name) for name in ("heinzel_plugin_tools", "heinzel_plugin_tools.cli")}
    sys.modules.update({"heinzel_plugin_tools": package, "heinzel_plugin_tools.cli": package.cli})  # type: ignore[attr-defined]
    out, old_argv = io.StringIO(), sys.argv
    try:
        sys.argv = ["-c", *argv]
        with contextlib.redirect_stdout(out):
            exec(compile(script, "<lock check>", "exec"), {"__name__": "__main__"})
    except Exception as err:
        return Result(1, out.getvalue(), f"Traceback ...\n{type(err).__name__}: {err}")
    finally:
        sys.argv = old_argv
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return Result(0, out.getvalue(), "")
