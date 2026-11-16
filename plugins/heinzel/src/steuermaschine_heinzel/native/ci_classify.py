"""ci classify RUN: ask the outside classifier service for the cause class of one failed run. A WRITE command.

It sends the condensed public failed-step log of the run to a paid service outside this machine and counts the call
in the budget file. It is off twice: by its independent review (reviews/ci-classify.md) and by the setting
`ci_classifier` (default off; the owner turns it on, nobody else). The key is read only when the command runs with
the setting on. The label is advisory: it never decides, closes, merges or reruns anything.

The client library is ci_classify_lib/jev.py, moved verbatim; this module is the only caller.
"""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import BaseModel, Field, SecretStr

from steuermaschine.engine import Refuse
from steuermaschine.github import logs
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native

from .ci_classify_lib import jev
from .go import world

SERVICE = "api.typesafe.ai"
RUN_ID_FLOOR = 100000  # a number above this is a run id, below it a pull request number


class ClassifySettings(BaseModel):
    """Settings of `sm ci classify` (mixin of the plugin's settings table)."""

    ci_classifier: bool = Field(default=False, description="Allow `sm ci classify` to send a condensed PUBLIC CI log to the paid classifier service. Off: the command refuses and reads no key.")
    ci_classifier_dir: Path | None = Field(default=None, description="State of the classifier: daily budget, the kill switch file OFF, the pause after a rejected key. None: the command refuses.")
    ci_classifier_key: SecretStr | None = Field(default=None, description="API key of the classifier service; none: the keychain item is asked.")
    ci_classifier_keychain_item: str = Field(default="", description="Name of the macOS keychain item that holds the classifier key; empty: the keychain is never asked.")


class Classification(Model):
    """What the classifier service said about one failed run."""

    run: int
    label: str = ""  # one of the cause classes, or "" when the service gave no confident answer
    probability: float | None = None
    service: str = SERVICE
    sent_characters: int = 0
    calls_today: int = 0
    input_tokens_today: int = 0
    call_limit: int = jev.DAILY_CALLS
    input_token_limit: int = jev.DAILY_TOKENS

    def render(self) -> list[str]:
        answer = f"classifier: {self.label} p={self.probability:.2f}" if self.label and self.probability is not None else "classifier: no confident class (or no answer)"
        return [answer, f"sent {self.sent_characters} characters of the condensed public log of run {self.run} to {self.service}",
                f"budget today: {self.calls_today} of {self.call_limit} calls, {self.input_tokens_today} of {self.input_token_limit} input tokens",
                "advisory only: the label decides nothing; read the cause with sm ci why"]


@native("ci classify", writes=True, replaces=("hz-faillog",), help=H(
    "Ask the paid outside classifier service for the cause class of one failed run. Advisory label; off by default.",
    "Use after the owner switched the setting ci_classifier on, and only when `sm ci why` left a failure's class unclear.",
    f"sends the condensed PUBLIC failed-step log of the run (at most {jev.MAX_STATE_CHARS} characters) to {SERVICE}, which bills each call; "
    "adds the call to the daily budget file; reads the API key (settings, else the keychain item).",
    "sm ci classify 43301234567",
    "sm ci why RUN: the label is a hint for reading the log, it never decides, closes, merges or reruns.",
    "Refuses, sending nothing and reading no key, while the setting ci_classifier is off, for a pull request number, for a\n"
    "repository that is not on the allowlist of public repositories, when the kill switch file OFF exists in the state\n"
    f"directory, and when the run has no failed-step log. Daily caps: {jev.DAILY_CALLS} calls, {jev.DAILY_TOKENS} input tokens.\n"
    "Classes: " + ", ".join(jev.CLASSES) + "."))
def ci_classify(run: int = typer.Argument(..., metavar="RUN", help="The run id (a number over 100000) of a public repository on the allowlist.")) -> Classification:
    w = world()
    s = w.settings
    if not s.get("ci_classifier"):
        raise Refuse("the classifier is off (setting ci_classifier): nothing was sent and no key was read; only the owner turns it on", f"sm ci why {run}")
    if run <= RUN_ID_FLOOR:
        raise Refuse("give a run id (a number over 100000); the runs of a pull request are on the RUN lines of `sm ci why`", f"sm ci why {run}")
    if not jev.check_repo(w.repo):
        raise Refuse(f"{w.repo or 'this repository'} is not on the allowlist of public repositories: nothing was sent", f"sm ci why {run}")
    if not s.get("ci_classifier_dir"):
        raise Refuse("the setting ci_classifier_dir is not set (budget, kill switch): nothing was sent", "sm self config")
    key = s.get("ci_classifier_key")
    jev.configure(mode="on", dir=str(s["ci_classifier_dir"]), api_key=key.get_secret_value() if isinstance(key, SecretStr) else str(key or ""),
                  keychain_item=str(s.get("ci_classifier_keychain_item") or ""), run=w.commands.run)
    if jev.killed():
        raise Refuse(f"the kill switch file {Path(jev.state_dir()) / 'OFF'} exists: nothing was sent", f"sm ci why {run}")
    res = w.gh.run(["run", "view", str(run), "-R", w.repo, "--log-failed"], timeout=120)
    text, why = (res.out, "") if res.rc == 0 else ("", w.gh.why(res))
    if why or not text.strip():
        raise Refuse(f"run {run} has no failed-step log to classify" + (f": {why}" if why else ""), f"sm ci why {run}")
    condensed = "\n".join(logs.condense(logs.clean(text), 6, 12))
    answer = jev.classify_failure(condensed, w.repo, tool="classify")
    use = jev.budget_use()
    return Classification(run=run, label=answer[0] if answer else "", probability=answer[1] if answer else None, sent_characters=len(condensed),
                          calls_today=int(use.get("calls") or 0), input_tokens_today=int(use.get("input_tokens") or 0))
