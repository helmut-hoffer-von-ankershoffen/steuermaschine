"""plugin gallery: recapture one plugin's screenshot gallery in a heinzel-plugins worktree (bead heinzel-management-jbgo).

What is structural (sm controls it): the capture runs as one frozen argv, the same command the repository's
`make screenshots NAME=...` runs, without make and without `--headed`, `--open` or other tooling. Its environment is an
allowlist (no MAKEFLAGS, OPEN, TOOLING, PLUGIN_*, tokens). Every git push from its process tree goes to an unreachable
URL (`pushInsteadOf`). The remote-tracking refs are compared before and after, and a change fails the run.

What is drift detection (sm reads the worktree and refuses on a difference, but cannot prove it): the Makefile still
runs that same command, and the capture script still has its throwaway profile, headless browser, fictional identity
and deterministic text guards. No module of the tooling package mentions a push or an agent CLI (codex, claude):
taking screenshots needs no AI (owner rule, bead heinzel-management-0tkk8).

Afterwards it lists the images that changed; the PR diff shows them for the normal review. It never commits or pushes
(`sm ship` takes a gallery `sm check` regenerated into the shipped commit: gallery_sync.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import REFUSED, WAITING
from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.runlock import Request

from .lock import lock_of

NAME = re.compile(r"[a-z][a-z0-9-]*")
TOOLING = Path("src/heinzel_plugin_tools")
CAPTURE = TOOLING / "capture.py"
GUARDS = TOOLING / "privacy.py"
DEFINITION = Path("docs/screenshot-definition.json")
OUTPUT = Path("docs/assets/screenshots")
DRAFT = Path(".preview/screenshots/draft")
LOG = Path(".preview/sm-plugin-gallery.log")
TIMEOUT_SECONDS = 1800
# The repository's frozen tooling command (Makefile `PLUGIN :=` without TOOLING); sm runs it itself, never through make.
FROZEN = ["uv", "run", "--frozen", "--no-build", "--group", "tooling", "hz-plugin"]  # old name kept: the plugins repository's own tool, not an sm tool
ENV_ALLOWED = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TERM")
# One pushInsteadOf with an EMPTY value matches every push URL (paths, relative paths, scp-like, any scheme) and rewrites
# it to a transport that does not exist. git does not apply it to an explicit remote.*.pushurl: `pushurls` refuses those.
PUSH_BLOCK = "sm-push-blocked:/"

# What the capture tooling must still contain (condition, module, needle): drift detection, read before every run.
REQUIRED = (
    ("isolated profile", CAPTURE, "env = demo_environment(work, interpreter)"),
    ("isolated profile", CAPTURE, "HOME=str(home)"),
    ("isolated profile", CAPTURE, 'env["HZ_" + key] = str(home / key.lower())'),
    ("headless", CAPTURE, "headless=not headed"),
    ("fixture data", CAPTURE, 'HZ_ORG__EMAIL="mira@example.com"'),
    ("fixture data", CAPTURE, 'HZ_SYSTEM_CREDENTIAL__BACKEND="environment"'),
    ("text guards", GUARDS, "def scan_text("),
    ("text guards", GUARDS, "def page_text("),
)
PUSH = re.compile(r"""\bgit\s+push\b|["']push["']|pushurl""")
AGENT = re.compile(r"\b(?:codex|claude)\b", re.IGNORECASE)  # the screenshot path never starts an agent


class GallerySettings(BaseModel):
    gallery_plugins_repo: Path | None = Field(
        None, description="The heinzel-plugins worktree `sm plugin gallery` captures in when --repo is not given. Unset: --repo is required.")


class Gallery(Model):
    """One gallery recapture: the conditions read, the run, the changed images."""

    plugin: str
    repo: str
    live: bool  # False: the conditions only (gallery-check)
    conditions: list[str] = []  # "CONDITION: why" per condition met
    problems: list[str] = []  # a condition not met (nothing ran), or remote refs that moved during the run
    ran: bool = False
    exit: int | None = None
    error: str = ""  # the capture's own error line, when it failed
    log: str = ""
    changed: list[str] = []  # images added, changed or removed (repository-relative)
    next: str = ""

    def render(self) -> list[str]:
        out = [f"CHECK  {line}" for line in self.conditions] + [f"REFUSE {line}" for line in self.problems]
        if self.ran:
            out.append(f"RAN    {' '.join(FROZEN)} screenshots {self.plugin} --json in {self.repo}: exit {self.exit}; log {self.log}")
        if self.error:
            out.append(f"ERROR  {self.error}")
        out += [f"IMAGE  {path}" for path in self.changed]
        if self.ran and not self.changed:
            out.append("IMAGE  none changed")
        if not self.live and not self.problems:
            out.append(f"PLAN   sm plugin gallery runs the frozen capture of {self.plugin} under the run lock with an allowlisted "
                       "environment and pushes blocked; nothing is committed or pushed")
        if self.next:
            out.append(f"NEXT   {self.next}")
        return out

    @property
    def exit_code(self) -> int:
        if self.problems or (self.ran and self.exit):
            return REFUSED
        return 0


def recipe(makefile: str, target: str) -> list[str] | None:
    """The recipe lines of `target:` in a Makefile (tab-indented lines after it), or None without that target."""
    lines = makefile.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"{re.escape(target)}\s*:(?!=)", line):
            body = []
            for following in lines[index + 1:]:
                if not following.startswith("\t"):
                    break
                body.append(following.strip())
            return body
    return None


def assignments(makefile: str, variable: str) -> list[str]:
    """Every value assigned to `variable` at any indentation (`:=`, `=`, `?=`, `+=`, `override`, `export`)."""
    pattern = re.compile(rf"^\s*(?:export\s+|override\s+)*{re.escape(variable)}\s*(?::{{1,3}}=|\?=|\+=|!=|=)\s*(.*)$")
    return [m.group(1).strip() for line in makefile.splitlines() if (m := pattern.match(line))]


def makefile_drift(makefile: str) -> list[str]:
    """Problems when the Makefile no longer runs the frozen command this command runs (drift detection)."""
    problems = []
    plugin = assignments(makefile, "PLUGIN")
    if not plugin or plugin[0] != " ".join(FROZEN) or any(value != " ".join(FROZEN) and "HZ_PLUGIN_TOOLING" not in value for value in plugin[1:]):
        problems.append(f"the Makefile's PLUGIN is not the frozen capture command ({' '.join(FROZEN)}): {plugin[:3]}")
    body = recipe(makefile, "screenshots")
    if body is None:
        problems.append("the Makefile has no screenshots target")
    else:
        text = " ".join(body)
        if len(body) != 1 or not re.fullmatch(r"@?\$\(PLUGIN\) screenshots( \$\(if \$\((OPEN|JSON)\),--(open|json)\))*", text):
            problems.append(f"the screenshots recipe is not the plain capture call: {text[:120]}")
    return problems


def tooling_sources(repo: Path) -> dict[Path, str]:
    """Every Python module of the tooling package, by repository-relative path."""
    return {p.relative_to(repo): p.read_text(errors="replace") for p in sorted((repo / TOOLING).rglob("*.py")) if p.is_file()}


def conditions(repo: Path, plugin: str) -> tuple[list[str], list[str]]:
    """(met, problems) of the Makefile, the tooling package and the plugin's plan, read from the worktree."""
    met: list[str] = []
    problems: list[str] = []
    try:
        makefile = (repo / "Makefile").read_text()
        modules = {path: (repo / path).read_text() for path in (CAPTURE, GUARDS)}
        sources = tooling_sources(repo)
    except OSError as exc:
        return met, [f"not a heinzel-plugins worktree: {exc.strerror}: {exc.filename}"]
    problems += makefile_drift(makefile)
    for condition, path, needle in REQUIRED:
        if needle not in modules[path]:
            problems.append(f"{condition}: {path} no longer contains {needle!r}")
    problems += [f"no push: {path} mentions a push" for path, source in sources.items() if PUSH.search(source)]
    problems += [f"no agent: {path} mentions an agent CLI" for path, source in sources.items() if AGENT.search(source)]
    if not problems:
        met += ["command: the frozen capture runs directly (no make, no --headed, no --open, no TOOLING); the Makefile still runs the same",
                "environment: an allowlist (no MAKEFLAGS, OPEN, TOOLING, PLUGIN_*, tokens); git pushes rewritten to an unreachable URL",
                "isolated profile (drift check): the capture keeps its throwaway HOME, XDG and HZ_* directories",
                "headless (drift check): the capture starts the browser headless unless --headed, which sm never passes",
                "fixture data (drift check): fictional identity, environment credentials only",
                "text guards (drift check): page text, title, URL, transcripts and captions are scanned for secrets and private data",
                f"no push (drift check): no module of {TOOLING} mentions a push; remote refs are compared before and after",
                f"no agent (drift check): no module of {TOOLING} mentions an agent CLI; capture is deterministic"]
    plan = repo / "plugins" / f"heinzel-{plugin}" / DEFINITION
    if not plan.is_file():
        problems.append(f"heinzel-{plugin} has no screenshot definition ({plan.relative_to(repo)})")
    return met, problems


def capture_environment(environ: dict[str, str], plugin: str) -> dict[str, str]:
    """The capture's whole environment: the allowlisted variables, the plugin's name and the push block."""
    env = {key: environ[key] for key in ENV_ALLOWED if key in environ}
    env["PLUGIN_NAME"] = plugin
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = f"url.{PUSH_BLOCK}.pushInsteadOf"
    env["GIT_CONFIG_VALUE_0"] = ""
    return env


def capture_argv(plugin: str) -> list[str]:
    return [*FROZEN, "screenshots", plugin, "--json"]


def images(folder: Path) -> dict[str, str]:
    """sha256 of every PNG below `folder`, by path."""
    if not folder.is_dir():
        return {}
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*.png")) if p.is_file()}


def changed_images(before: dict[str, str], after: dict[str, str], repo: Path) -> list[str]:
    """The images added, changed or removed, repository-relative, with what happened."""
    out = []
    for path in sorted(set(before) | set(after)):
        word = "added" if path not in before else "removed" if path not in after else "changed" if before[path] != after[path] else ""
        if word:
            out.append(f"{Path(path).relative_to(repo)} {word}")
    return out


def capture_error(log: Path) -> str:
    """The `error` of the last JSON line the capture printed, or ""."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            return str(data.get("error") or "") if isinstance(data, dict) and data.get("ok") is False else ""
    return ""


def pushurls(repo: Path) -> str | None:
    """Every remote.*.pushurl and url.*.pushInsteadOf git sees in the worktree (all config scopes), "" when none, None
    when unreadable. A pushurl skips pushInsteadOf, and a configured pushInsteadOf with a longer prefix beats the empty
    block (git takes the longest match); either refuses.
    git exits 1 when nothing matches."""
    res = runtime().providers.commands.run(["git", "-C", str(repo), "config", "--get-regexp", r"^(remote\..*\.pushurl|url\..*\.pushinsteadof)$"], timeout=20)
    return res.out.strip() if res.rc == 0 else "" if res.rc == 1 else None


def remote_refs(repo: Path) -> str | None:
    """The worktree's remote-tracking refs and their commits, or None when git cannot read them."""
    res = runtime().providers.commands.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"], timeout=20)
    return None if res.rc else res.out


class Logged:
    """The process runner of the run lock for the capture: its output goes to the log file, not to sm's stdout."""

    def __init__(self, inner: Any, log: Path):
        self.inner, self.log = inner, log

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.run(*args, **kwargs)

    def passthrough(self, argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None, timeout: float | None = None) -> int:
        return int(self.inner.passthrough(["sh", "-c", 'exec "$@" >"$0" 2>&1', str(self.log), *argv], cwd=cwd, env=env, timeout=timeout))


def run_capture(repo: Path, plugin: str, log: Path) -> tuple[int, str]:
    """The frozen capture under the run lock, allowlisted environment. -> (exit code, why the lock did not run it, or "")."""
    rt = runtime()
    gate = lock_of()
    gate.commands = Logged(rt.providers.commands, log)
    outcome = gate.run(Request(argv=capture_argv(plugin), environ=capture_environment(child_environment(), plugin),
                               cwd=str(repo), timeout_seconds=TIMEOUT_SECONDS, full=True))
    return outcome.exit_code, "" if outcome.status == "ran" else outcome.reason or outcome.status


def checked(name: str, repo: Path | None, live: bool) -> Gallery:
    """The conditions read for plugin `name` in the worktree (`repo`, else the setting); Refuse on bad usage."""
    name = name.removeprefix("heinzel-")
    if not NAME.fullmatch(name):
        raise Refuse("NAME is a plugin name in lowercase letters, digits and dashes", "sm plugin gallery-check google --repo PATH", code=2)
    configured = (runtime().settings or {}).get("gallery_plugins_repo")
    root = repo or (Path(str(configured)) if configured else None)
    if root is None:
        raise Refuse("no worktree: give --repo PATH or set gallery_plugins_repo", f"sm plugin gallery-check {name} --repo PATH", code=2)
    root = root.expanduser().resolve()
    met, problems = conditions(root, name)
    out = Gallery(plugin=name, repo=str(root), live=live, conditions=met, problems=problems)
    if problems:
        out.next = "fix the worktree (or this command, if heinzel-plugins changed its capture on purpose); nothing ran"
    elif not live:
        out.next = f"sm plugin gallery {name} --repo {root}"
    return out


NAME_ARG = typer.Argument(..., metavar="NAME", help="The plugin, without the heinzel- prefix (google).")
REPO_OPT = typer.Option(None, "--repo", metavar="PATH", help="The heinzel-plugins worktree (default: setting gallery_plugins_repo).")
CONDITIONS = ("Structural: the frozen capture command runs directly (never make, never --headed or --open) with an allowlisted\n"
              "environment in which git pushes go to an unreachable URL, and compares the remote refs before and after.\n"
              "Drift detection: the Makefile still runs that command, and the capture tooling keeps its throwaway profile, headless\n"
              "browser, fictional identity and text guards, and no tooling module mentions a push or an agent CLI; any difference refuses.")


@native("plugin gallery-check", writes=False, help=H(
    "Can this worktree recapture a plugin's gallery safely? The conditions of sm plugin gallery, read only.",
    "Use before sm plugin gallery, and after heinzel-plugins changes its capture tooling.",
    "nothing (read-only: the Makefile, the tooling package and the plugin's screenshot definition).",
    "sm plugin gallery-check google --repo ~/Code/heinzel-plugins-gws",
    "sm plugin gallery NAME --repo PATH",
    CONDITIONS))
def plugin_gallery_check(name: str = NAME_ARG, repo: Path | None = REPO_OPT) -> Gallery:
    return checked(name, repo, live=False)


@native("plugin gallery", writes=True, help=H(
    "Recapture one plugin's screenshot gallery in an isolated profile, headless, from fixture data; never pushes.",
    "Use when a plugin change makes its gallery stale (the plugin check: the gallery digest no longer matches).",
    "runs the screenshot capture in the given heinzel-plugins worktree, which rewrites that plugin's gallery, its README "
    "gallery block and catalog metadata; no agent runs. It commits and pushes nothing.",
    "sm plugin gallery google --repo ~/Code/heinzel-plugins-gws",
    "commit the listed images on your branch (sm ship takes a gallery sm check regenerated into the head commit)",
    CONDITIONS + "\nThe capture runs under the run lock; its output goes to .preview/sm-plugin-gallery.log.\n"
    "The last lines list every image added, changed or removed; the PR diff shows them for the normal review."))
def plugin_gallery(name: str = NAME_ARG, repo: Path | None = REPO_OPT) -> Gallery:
    out = checked(name, repo, live=True)
    if out.problems:
        return out
    name, root = out.plugin, Path(out.repo)
    urls = pushurls(root)
    if urls is None or urls:
        out.problems.append("a remote.*.pushurl or url.*.pushInsteadOf is configured (the push block cannot cover it); nothing ran" if urls
                            else "cannot read the git configuration of the worktree; nothing ran")
        return out
    refs = remote_refs(root)
    if refs is None:
        out.problems.append("cannot read the worktree's remote-tracking refs (git for-each-ref); nothing ran")
        return out
    plugin_dir = root / "plugins" / f"heinzel-{name}"
    folders = (plugin_dir / OUTPUT, plugin_dir / DRAFT)
    before = {k: v for folder in folders for k, v in images(folder).items()}
    log = plugin_dir / LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    code, why = run_capture(root, name, log)
    if why:
        if code == WAITING:
            raise Wait(f"the run lock did not admit the capture: {why}")
        raise Refuse(f"the run lock refused the capture: {why}", "sm gate status")
    after = {k: v for folder in folders for k, v in images(folder).items()}
    out.ran, out.exit, out.log = True, code, str(log)
    if remote_refs(root) != refs:
        out.problems.append("the remote-tracking refs changed during the capture: a push or fetch ran; do not trust this run, tell the coordinator")
    out.error = capture_error(log) if code else ""
    out.changed = changed_images(before, after, root)
    if out.problems:
        out.next = "tell the coordinator: the remote refs moved during the capture"
    elif code:
        out.next = f"read {log}; fix the definition or fixture it names, then sm plugin gallery {name} --repo {root}"
    elif out.changed:
        out.next = "commit the changed gallery on your branch (sm ship after sm check does it); the PR diff shows the images"
    else:
        out.next = "nothing to review: the gallery bytes did not change"
    return out
