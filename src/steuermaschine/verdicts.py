"""Review digests and verdict receipts: the one store `sm review verdict` writes and the write commands read.

A digest becomes reviewable when a run stops at it (the engine) or when a command that produced it registers it here
(`register`), so a verdict can be recorded when no run is waiting (an interrupted run, a hand route). A write command
that applies reviewed bytes calls `require_independent`: it refuses unless an approving receipt exists for the digest
and its reviewer is not one of the agents that produced the bytes.

A digest can also bind a commit of code (`need_check`). Then an approval is recorded only when the cheap checks of
exactly that commit were green: a plugin's check command writes one check receipt per commit (`record_check`), and
`sm review verdict --approve` refuses without it (`missing_check`), naming the command that writes it."""

from __future__ import annotations

from typing import Any

from .config import environment
from .engine import Refuse

KIND = "review.digests"
CI_CHANGE = "ci_change"  # a verdict on a commit whose CI paths changed since the last release: the release gate needs it
KINDS = ("", CI_CHANGE)  # "": a verdict on prepared bytes (the 04ro gate)
CHECKS = "checks"  # receipts, one per commit: the cheap checks of that commit were green (written once, by a plugin's check command)


def current_agent() -> str:
    """Who drives the command: the same default as `--as` of a pipeline run."""
    return environment().steuermaschine_agent or "coordinator"


def register(store: Any, digest: str, source: str, author: str | None = None) -> None:
    """Record that `author` (default: the current agent) produced the bytes with this digest, through `source`."""
    who = author or current_agent()
    with store.lock(f"{KIND}.{digest}"):
        data = store.load(KIND, digest) or {"digest": digest, "authors": [], "sources": []}
        if who not in data["authors"]:
            data["authors"].append(who)
        if source not in data["sources"]:
            data["sources"].append(source)
        store.save(KIND, digest, data)


def authors_of(store: Any, digest: str, run_states: list[dict[str, Any]]) -> set[str]:
    """Every agent that produced the digest: the runs stopped at it, and the commands that registered it."""
    found = {s.get("author") for s in run_states if any(st.get("digest") == digest for st in s.get("steps", {}).values())}
    found |= set((store.load(KIND, digest) or {}).get("authors", []))
    return {a for a in found if a}


def require_independent(store: Any, digest: str, run_states: list[dict[str, Any]], recorded: str, kind: str = "") -> dict[str, Any]:
    """The approving verdict receipt for `digest`, or a Refuse naming the command that records it. With `kind`
    (`CI_CHANGE`), only a receipt of that kind counts: a verdict on prepared bytes never covers a CI change."""
    receipt = store.get_receipt("verdicts", digest)
    if not receipt:
        raise Refuse(f"no verdict is recorded for {digest}: the digest alone is not a review", recorded)
    if kind and receipt.get("kind", "") != kind:
        raise Refuse(f"the verdict for {digest[:12]} is not a {kind} verdict: a review of the CI change itself is needed", recorded)
    if receipt.get("verdict") != "approve":
        raise Refuse(f"the recorded verdict for {digest[:12]} is '{receipt.get('verdict')}' by {receipt.get('reviewer')}: "
                     f"{receipt.get('notes', '')}. Change the work; new bytes get a new review", recorded)
    if receipt.get("reviewer") in authors_of(store, digest, run_states):
        raise Refuse(f"the verdict for {digest[:12]} was recorded by {receipt.get('reviewer')}, who also produced these bytes: "
                     "a different agent must review", recorded)
    if unchecked := missing_check(store, digest):
        raise Refuse(unchecked, recorded)
    return dict(receipt)


def check_receipt(store: Any, head: str) -> tuple[dict[str, Any] | None, str]:
    """(the green check receipt of the commit, "") or (None, why there is none that counts): missing, unreadable, or
    not green. A receipt that cannot be read proves nothing."""
    try:
        receipt = store.get_receipt(CHECKS, head)
    except (OSError, ValueError, TypeError) as why:
        return None, f"its check receipt exists but cannot be read ({type(why).__name__}): a damaged receipt is no proof and is never rewritten, so the commit needs a new sha"
    if not isinstance(receipt, dict) or not receipt:
        return None, "no check receipt exists for exactly that commit"
    if receipt.get("green") is not True:
        return None, "its check receipt is not green"
    return dict(receipt), ""


def record_check(store: Any, head: str, data: dict[str, Any]) -> str:
    """Write the check receipt of the commit `head`: its cheap checks were green, measured from `data["base"]`.
    -> "written"; "kept" when the commit already has a green receipt that can be read (the first stays); else why the
    one that is there does not count (it cannot be replaced: a receipt is written once)."""
    try:
        store.put_receipt(CHECKS, head, {**data, "head": head, "green": True})
    except FileExistsError:
        return "kept" if check_receipt(store, head)[0] else check_receipt(store, head)[1]
    return "written"


def need_check(store: Any, digest: str, head: str, command: str, base: str = "") -> None:
    """Record that `digest` binds the commit `head`: an approval of it needs that commit's check receipt, which
    `command` writes. With `base` (the commit the bytes under review build the head on) the receipt must have been
    measured from exactly that base: checks of a part of the head are no checks of the head. The command that
    produces the digest calls this; a digest nobody bound needs no receipt."""
    want = {"head": head, "command": command, **({"base": base} if base else {})}
    with store.lock(f"{KIND}.{digest}"):
        data = store.load(KIND, digest) or {"digest": digest, "authors": [], "sources": []}
        if data.get("check") != want:
            store.save(KIND, digest, {**data, "check": want})


def missing_check(store: Any, digest: str) -> str:
    """Why `digest` cannot be approved, or an approval of it cannot be used: it binds a commit without a green check
    receipt measured from the bound base. "" when it binds no commit or the receipt is there. The text names the
    command that writes the receipt. Asked when the verdict is recorded and again wherever it is used."""
    need = (store.load(KIND, digest) or {}).get("check") or {}
    head, base = str(need.get("head") or ""), str(need.get("base") or "")
    if not head:
        return ""
    receipt, why = check_receipt(store, head)
    run = f"Run `{need.get('command')}` on that head (it writes the receipt), then record the verdict"
    if receipt is None:
        return f"{digest[:12]} binds the head {head[:12]}, and {why}: an approval needs the cheap checks green first. {run}"
    if base and receipt.get("base") != base:
        return (f"{digest[:12]} binds the head {head[:12]} on the base {base[:12]}, but its check receipt was measured from {str(receipt.get('base'))[:12]}: "
                f"the checks did not cover what the head changes over that base. Bring the head onto that base (a new commit), then: {run}")
    return ""
