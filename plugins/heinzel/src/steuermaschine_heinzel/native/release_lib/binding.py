"""What the native release commands share: their settings, how a moved module is bound to them, captured output."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.native import runtime
from steuermaschine.plugins import old_variable

from ... import state as statedir  # the plugin's state paths
from . import edge


class ReleaseSettings(BaseModel):
    """Settings of the release area (mixin of the plugin's table). A path that is not set is the one the release
    tooling has always used, named in its description; no default here is a path of a machine. The release state
    itself (receipt, tail receipts, owned holds, the freeze file) is not a setting: the merge queue reads the same
    files, so they stay where both sides have them, in the project's state directory (`state.py`)."""

    release_dir: Path | None = Field(None, description="The checkout in which a release PR is prepared: clean, on main, with the push hooks. Was the environment variable "
                                                        "HZ_RELEASE_DIR of the step runner. Default: /private/tmp/heinzel-air-release.")
    release_core_checkout: Path | None = Field(None, description="A checkout of the core repository that is only read: `release pins` reads the pinned files at a ref, "
                                                                  "`release assets` reads its tool and requirement lists. No default: unset, both commands refuse and name this key.")
    release_train_state_dir: Path | None = Field(None, description="The release train's state directory: one file, lock and log per version, the cadence file. Default: train/ in "
                                                                    "the project's state directory.", json_schema_extra=old_variable("HZ_TRAIN_STATE_DIR"))
    release_assets_cache: Path | None = Field(None, description="The FILE with the last result of the asset probes (JSON: time and one row per asset), read by --digest. "
                                                                 "Default: assets-health.json in the project's state directory.")


def conf() -> dict:
    values = runtime().settings
    values = values.of("heinzel") if hasattr(values, "of") else values
    return values if isinstance(values, dict) else dict(values)


def path_setting(name: str) -> Path | None:
    value = conf().get(name)
    return Path(str(value)).expanduser() if value else None


def train_state_dir() -> Path:
    return path_setting("release_train_state_dir") or statedir.path("train")


def assets_cache() -> Path:
    return path_setting("release_assets_cache") or statedir.path("assets-health.json")


def core_checkout() -> Path | None:
    """The setting release_core_checkout; None when unset (a per-machine path has no default)."""
    return path_setting("release_core_checkout")


def required_core_checkout(command: str) -> Path:
    """The read-only core checkout a command cannot work without: refuses (usage error) and names the key when unset."""
    found = core_checkout()
    if found is None:
        raise Refuse(f"`sm {command}` reads a core checkout: set release_core_checkout in [heinzel] "
                     "(or SM_HEINZEL__RELEASE_CORE_CHECKOUT)", "sm self config", code=2)
    return found


def release_dir() -> Path:
    return path_setting("release_dir") or Path(edge.DEFAULT_RELEASE_DIR)


def moved(dotted: str):
    """A fresh copy of a moved module (`release_lib.runner`), with the edges bound to the settings."""
    edge.configure(conf().get("queue_records_dir"))
    edge.configure_release(release_dir(), core_checkout())
    return edge.fresh(f"{edge.PACKAGE}.{dotted}")


def captured(call, *args, **kwargs) -> tuple[int, list[str], list[str]]:
    """Run a moved function that prints, and return its exit code, stdout lines and stderr lines."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = call(*args, **kwargs)
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
            if not isinstance(stop.code, int) and stop.code is not None:
                err.write(str(stop.code) + "\n")
    return int(code or 0), out.getvalue().splitlines(), err.getvalue().splitlines()


def core_repo() -> str:
    from ..queue_lib import hzlib

    return str(conf().get("repo") or hzlib.CORE)
