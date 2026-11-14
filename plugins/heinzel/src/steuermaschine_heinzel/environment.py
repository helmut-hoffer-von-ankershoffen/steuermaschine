"""The process environment the plugin reads that is not a setting, as typed models (pydantic-settings).

Settings come from the settings layer (files, SM_HEINZEL__<FIELD>, and the old variable a setting names; see
`steuermaschine.plugins.old_variable`). What is left is context the process is started in, never configuration:
the identity of a GitHub Actions run, the interpreter's isolation, the tokens whose values are redacted from a log,
and the old state overrides live admission refuses. This module is the only place of the plugin that reads the
environment (a test enforces it); a hand-on to a child process goes through `steuermaschine.config.child_environment`.
Read fresh on every call: a test changes the environment between two calls."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _var(name: str, description: str) -> Any:
    return Field(None, validation_alias=AliasChoices(name), description=description)


class Actions(BaseSettings):
    """The public identity of a GitHub Actions run (set by the runner); never a token or a workflow grant."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=False)
    event_name: str | None = _var("GITHUB_EVENT_NAME", "The event that started the workflow.")
    event_path: str | None = _var("GITHUB_EVENT_PATH", "The file with the event's JSON payload.")
    repository: str | None = _var("GITHUB_REPOSITORY", "OWNER/NAME of the repository.")
    ref: str | None = _var("GITHUB_REF", "The full ref of the run.")
    ref_name: str | None = _var("GITHUB_REF_NAME", "The short ref of the run.")
    sha: str | None = _var("GITHUB_SHA", "The commit of the run.")
    run_id: int | None = _var("GITHUB_RUN_ID", "The run's id.")
    run_attempt: int | None = _var("GITHUB_RUN_ATTEMPT", "The run's attempt.")
    output: str | None = _var("GITHUB_OUTPUT", "The file the step's outputs are appended to.")


class Interpreter(BaseSettings):
    """The variables that load code into Python from outside the installed package (an isolation check reads them)."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=False)
    pythonpath: str | None = _var("PYTHONPATH", "Extra import directories.")
    pythonstartup: str | None = _var("PYTHONSTARTUP", "A file run at interactive start.")
    pythonhome: str | None = _var("PYTHONHOME", "Another standard library.")
    pythoninspect: str | None = _var("PYTHONINSPECT", "Interactive mode after the script.")

    def loads_foreign_code(self) -> bool:
        return any((self.pythonpath, self.pythonstartup, self.pythonhome, self.pythoninspect))


class Tokens(BaseSettings):
    """GitHub tokens of this process, only to redact their values from a text that is logged. Never printed."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=False)
    gh_token: SecretStr | None = _var("GH_TOKEN", "The gh CLI token.")
    github_token: SecretStr | None = _var("GITHUB_TOKEN", "The Actions token.")
    gh_enterprise_token: SecretStr | None = _var("GH_ENTERPRISE_TOKEN", "The gh CLI token of an enterprise host.")
    github_enterprise_token: SecretStr | None = _var("GITHUB_ENTERPRISE_TOKEN", "The Actions token of an enterprise host.")

    def values(self) -> list[str]:
        return [secret.get_secret_value() for secret in (self.gh_token, self.github_token, self.gh_enterprise_token, self.github_enterprise_token)
                if secret is not None and secret.get_secret_value()]


class StateOverrides(BaseSettings):
    """The old variables that pointed a tool at other state or another checkout (tests did): live admission refuses
    while one of the state overrides is set, and a command whose setting wins over one says so."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=False)
    queue_state: str | None = _var("HZ_QUEUE_STATE", "Old name of the setting queue_state_file.")
    train_state_dir: str | None = _var("HZ_TRAIN_STATE_DIR", "Old name of the setting release_train_state_dir.")
    release_dir: str | None = _var("HZ_RELEASE_DIR", "The release checkout of the old step runner.")
    train_checkout: str | None = _var("HZ_TRAIN_CHECKOUT", "Old name of the setting release_train_checkout.")
    train_every: str | None = _var("HZ_TRAIN_EVERY", "Old name of the setting release_train_every_minutes.")

    def named(self, variable: str) -> str | None:
        """The value of one old variable by its name (None: unset or empty)."""
        for field, info in type(self).model_fields.items():
            if variable in getattr(info.validation_alias, "choices", ()):
                return getattr(self, field) or None
        raise KeyError(variable)

    def any(self) -> bool:
        return any((self.queue_state, self.train_state_dir, self.release_dir))


def actions() -> Actions:
    return Actions()


def interpreter() -> Interpreter:
    return Interpreter()


def tokens() -> Tokens:
    return Tokens()


def state_overrides() -> StateOverrides:
    return StateOverrides()
