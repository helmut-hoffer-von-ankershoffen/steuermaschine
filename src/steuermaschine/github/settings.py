"""Settings of the GitHub and Sonar providers: the tables [github] and [sonar] of a settings file. No import from the rest of core."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GitHubSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field("", description="OWNER/NAME. Empty: `gh` works it out from the current directory's git remote.")
    timeout_seconds: float = Field(30.0, description="Limit for one `gh` call.")
    max_pages: int = Field(3, description="Upper bound of pages (100 rows each) one listing reads.")
    merge_method: str = Field("squash", description="squash, merge or rebase: how `Forge.merge` merges.")
    variable_writes: bool = Field(False, description="Allow `Forge.set_variable`. Off: a write of a repository variable is refused.")


class SonarSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_key: str = Field("", description="The project key on the Sonar host. Empty: the project has no Sonar provider.")
    host: str = Field("https://sonarcloud.io", description="Base URL of the Sonar web API.")
    token_env: str = Field("SONAR_TOKEN", description="Name of an environment variable with a token, sent only when it is set. Public projects need none.")
    timeout_seconds: float = Field(20.0, description="Limit for one HTTP call.")
