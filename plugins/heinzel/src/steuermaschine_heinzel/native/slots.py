"""`sm head slots collect` and `sm head slots apply`: collect the focused plan registry.

The registry (five repository variables, see `slots_lib/collect.py`) only grows: nothing removes the entries of heads
that merged or closed, and a full slot stops admissions and releases. `collect` is the dry run (read-only): it lists the
entries that may go and prints the digest of the exact change. `apply --approved DIGEST` rebuilds the same change from
fresh reads, refuses unless the digest is the approved one, and writes it: the same pattern as `sm head prepare` and
`sm head apply`. It is a separate command because a command with one writing option is split (INTEGRATION 8.1).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, NoReturn

import typer

from steuermaschine.engine import Refuse, Wait
from steuermaschine.github import Gh, GhError, GitHub
from steuermaschine.github.base import ProviderError
from steuermaschine.github.cli import table
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime
from steuermaschine.protocols import Forge

from .. import state as statedir
from .slots_lib import collect, legacy, registry_lock

PR_LIMIT = 1000

SLOTS = Group("head slots", H(
    "head slots: the focused plan registry (five repository variables) and what may be collected from it.",
    "Use when a slot passes 85 % (`sm go`, `sm alarm check`) and before an admission that would not fit.",
    "collect reads only; apply writes the slots that lose entries.",
    "sm head slots collect",
    "sm head slots apply --approved DIGEST (after an independent review of the collect output)"))


class SlotFill(Model):
    name: str
    bytes: int
    limit_bytes: int
    percent: int
    after_bytes: int | None = None  # bytes after the collection; none when the slot loses nothing


class SlotEntry(Model):
    slot: str
    key: str
    kind: str  # pull_request | push-tree | other
    tree: str
    prs: list[int]
    state: str  # open | merged | closed | unmatched | main | unknown
    action: str  # collect | keep
    why: str


class SlotsCollect(Model):
    """What a collection would do now. `digest` is the sha256 of the canonical `transaction`."""

    repository: str
    fills: list[SlotFill]
    entries: list[SlotEntry]  # the entries to collect; with --all also those that stay
    collect_count: int
    keep_count: int
    reservations: int  # release reservations that protect a tree
    transaction: dict[str, Any]
    digest: str

    def render(self) -> list[str]:
        lines = [f"{f.name.replace('FOCUSED_CI_PLANS', 'PLANS'):<12} {f.bytes:>6}/{f.limit_bytes} B  {f.percent:>3}%"
                 + (f"  -> {f.after_bytes} B ({collect.fill_percent(f.after_bytes, f.name)}%)" if f.after_bytes is not None else "") for f in self.fills]
        lines += [f"{e.action:<8} {e.slot.replace('FOCUSED_CI_PLANS', 'PLANS'):<10} {e.key[:30]}  {e.state:<9} {e.why}" for e in self.entries]
        lines.append(f"{self.collect_count} entries to collect, {self.keep_count} stay ({self.reservations} release reservations protect a tree).")
        lines.append(f"digest {self.digest}" if self.collect_count else "nothing to collect.")
        if self.collect_count:
            lines.append(f"after an independent review of this output (`sm head slots collect --format json`): sm head slots apply --approved {self.digest}")
        return lines


class SlotsApplied(Model):
    digest: str
    written: list[str]
    removed: int
    receipt: str

    def render(self) -> list[str]:
        return [f"wrote {', '.join(self.written) or 'nothing'}: {self.removed} entries removed (digest {self.digest}); receipt {self.receipt}"]


def _gh() -> tuple[Gh, str]:
    rt = runtime()
    repo = str(rt.settings.get("repo") or "")
    if not repo:
        raise Refuse("the repository is not set: set `repo` (OWNER/NAME) in the plugin's settings.", "sm self config", code=2)
    return Gh(rt.providers.commands, repo=repo, clock=rt.providers.clock), repo


def _epoch(stamp: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp() if stamp else None
    except ValueError:
        return None


def _prs(gh: Gh) -> dict[str, list[tuple[int, str, float | None]]]:
    rows = gh.json(["pr", "list", "--state", "all", "--limit", str(PR_LIMIT), "--json", "number,state,headRefOid,closedAt"])
    out: dict[str, list[tuple[int, str, float | None]]] = {}
    for row in rows or []:
        out.setdefault(str(row.get("headRefOid", "")), []).append((int(row["number"]), str(row.get("state", "")).lower(), _epoch(row.get("closedAt"))))
    return out


def _main(gh: Gh) -> dict[str, str]:
    """Main's head and its tree, read now: the plan of that tree is what a rerun of main's CI looks up."""
    data = gh.json(["api", f"repos/{gh.repo()[0]}/commits/main"])
    sha, tree = str((data or {}).get("sha", "")), str((((data or {}).get("commit") or {}).get("tree") or {}).get("sha", ""))
    if not sha or not tree:
        raise GhError("main's head and tree could not be read")
    return {"sha": sha, "tree": tree}


def _unreleased_trees(gh: Gh) -> set[str]:
    """Trees of the release reservations whose version is not a published release (all of them when the releases cannot be read)."""
    found = legacy.reservations(statedir.path("focused-admission-receipts"))
    if not found:
        return set()
    try:
        rows = gh.json(["release", "list", "--limit", "50", "--json", "tagName"])
        published = {str(r.get("tagName", "")).removeprefix("v") for r in rows or []}
    except GhError:
        published = set()
    return {tree for version, tree in found if not version or version not in published}


def _forge(repo: str) -> Forge | GitHub:
    rt = runtime()
    if rt.providers.forge is not None:
        return rt.providers.forge
    # Use the same repository for the PR facts, reviewed transaction and variable writes.
    settings = table(rt, "github") | {"repo": repo}
    return GitHub.from_settings(rt.providers.commands, settings, rt.providers.clock)


def _read(gh: Gh, forge: Forge | GitHub) -> tuple[dict[str, str], list[dict[str, Any]], int, dict[str, str]]:
    rt = runtime()
    try:
        values = {name: forge.get_variable(name) for name in collect.NAMES}
        if all(value is None for value in values.values()):
            raise Refuse("no focused registry variable could be read; nothing collected.", "sm status", code=1)
        texts = {name: value or "" for name, value in values.items()}
        slots = {name: collect.decode(name, text) for name, text in texts.items()}
        prs = _prs(gh)
        reserved = _unreleased_trees(gh)
        main = _main(gh)
    except (collect.Unreadable, GhError, ProviderError) as error:
        raise Refuse(f"cannot collect: {error}", "sm status", code=1) from error
    return texts, collect.classify(slots, prs, reserved, main["tree"], rt.providers.clock.now()), len(reserved), main


def _model(repo: str, texts: dict[str, str], rows: list[dict[str, Any]], reserved: int, main: dict[str, str], show_all: bool) -> SlotsCollect:
    try:
        tx = collect.transaction(repo, texts, rows, main)
        after = collect.new_values(texts, tx)
    except collect.Unreadable as error:
        raise Refuse(f"cannot collect: {error}", "sm status", code=1) from error
    fills = [SlotFill(name=n, bytes=len(texts[n].encode()), limit_bytes=collect.LIMITS[n], percent=collect.fill_percent(len(texts[n].encode()), n),
                      after_bytes=len(after[n].encode()) if n in after else None) for n in collect.NAMES]
    shown = [SlotEntry.model_validate({k: r[k] for k in SlotEntry.model_fields}) for r in rows if show_all or r["action"] == "collect"]
    going = sum(1 for r in rows if r["action"] == "collect")
    return SlotsCollect(repository=repo, fills=fills, entries=shown, collect_count=going, keep_count=len(rows) - going, reservations=reserved,
                        transaction=tx, digest=collect.digest(tx))


@native("head slots collect", writes=False, help=H(
    "Dry run: the focused plan slot entries whose PR is merged or closed and whose tree nothing wants any more, and the digest of that change.",
    "Use when a slot passes 85 % or an admission or release failed for want of room; then have the output reviewed.",
    "nothing (read-only): it reads the five registry variables, the pull requests and the release reservation receipts.",
    "sm head slots collect",
    "sm head slots apply --approved DIGEST (after an independent review)",
    "An entry goes only when every pull request with that head is merged or closed and no open (or unmatched) pull request plan and no\n"
    "unreleased release reservation names its tree; a push-tree entry goes with its pull request entries. Everything else stays."))
def head_slots_collect(all_: bool = typer.Option(False, "--all", help="Also list the entries that stay, with the reason.")) -> SlotsCollect:
    gh, repo = _gh()
    texts, rows, reserved, main = _read(gh, _forge(repo))
    return _model(repo, texts, rows, reserved, main, all_)


@native("head slots apply", writes=True, help=H(
    "Apply one approved collection: rewrite the slots that lose entries, with a receipt.",
    "Use after an independent reviewer approved the digest of `sm head slots collect`.",
    "up to five repository variables (the focused plan slots that lose entries); writes a receipt.",
    "SM_GITHUB__VARIABLE_WRITES=true sm head slots apply --approved DIGEST",
    "sm go",
    "GitHub variable writes must be explicitly enabled. Rebuilds the change from fresh reads and refuses unless its digest is the approved one, the registry did not change while it\n"
    "wrote, and no Actions run is active (exit 75). The readback must equal what was written; nothing is restored automatically."))
def head_slots_apply(approved: str = typer.Option(..., "--approved", help="The digest of the collect output the reviewer approved.")) -> SlotsApplied:
    rt = runtime()
    gh, repo = _gh()
    forge = _forge(repo)
    if isinstance(forge, GitHub) and not forge.variable_writes:
        raise Refuse("GitHub variable writes are off; nothing was written.",
                     "enable SM_GITHUB__VARIABLE_WRITES=true for the reviewed command", code=1)
    texts, rows, reserved, main = _read(gh, forge)
    result = _model(repo, texts, rows, reserved, main, False)
    if approved != result.digest:
        raise Refuse(f"the approved digest is not the digest of the change that reads now ({result.digest}): the registry or a pull request "
                     "changed; nothing was written.", "sm head slots collect", code=1)
    if not result.collect_count:
        raise Refuse("nothing to collect; nothing was written.", "sm head slots collect", code=1)
    try:
        active = forge.active_runs()
    except ProviderError as error:
        raise Refuse("could not verify that Actions is idle; nothing was written.", "sm ci active", code=1) from error
    if active:
        raise Wait(f"{len(active)} Actions run(s) are active and may read the registry; nothing was written.", "no active Actions run")
    tx = result.transaction
    writes = collect.new_values(texts, tx)
    expected, written = dict(texts), list[str]()

    def record(partial: bool) -> str:
        key = result.digest[:16]
        receipt = {"schema": 1, "digest": result.digest, "transaction": tx, "written": list(written), "partial": partial,
                   "removed": len([key for name in written for key in tx["remove"][name]])}
        rt.providers.store.put_receipt("heinzel.slots.collect", key, json.loads(json.dumps(receipt)))
        return f"heinzel.slots.collect/{key}"

    def stop(why: str, *, uncertain: bool = False) -> NoReturn:
        if uncertain:
            done = f" Inspect the registry before retrying; partial receipt {record(True)}."
        else:
            done = f" Written before this: {', '.join(written)}; receipt {record(True)}." if written else " Nothing was written."
        raise Refuse(why + done, "sm head slots collect", code=1)

    try:
        with registry_lock.writer_lock(statedir.path("focused-pr-registry.lock")):  # the lock `head apply` takes
            for name, value in writes.items():
                # Read everything again right before each write, as `head apply` does: a writer that got in since is not overwritten.
                if {n: forge.get_variable(n) or "" for n in collect.NAMES} != expected:
                    stop("the registry changed since the read.")
                if not written and _main(gh) != tx["main"]:
                    stop("main moved since the read.")
                forge.set_variable(name, value)
                expected[name] = value
                written.append(name)
            if {n: forge.get_variable(n) or "" for n in collect.NAMES} != expected:
                stop("the readback differs from what was written: inspect the registry; nothing is restored automatically.")
    except registry_lock.Busy as error:
        raise Refuse(f"the registry writer lock is busy ({error}); nothing was written.", "sm head slots apply --approved DIGEST", code=1) from error
    except GhError as error:
        stop(f"cannot read main: {error}.")
    except ProviderError:
        stop("GitHub did not confirm the registry operation.", uncertain=True)
    removed = sum(len(keys) for keys in tx["remove"].values())
    receipt_ref = record(False)
    return SlotsApplied(digest=result.digest, written=sorted(writes), removed=removed, receipt=receipt_ref)
