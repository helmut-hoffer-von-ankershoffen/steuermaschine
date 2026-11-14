"""GitHub (through the `gh` CLI) and Sonar for the core: the one `Gh` client, the provider `GitHub` that satisfies
the core Protocols `Forge` and `CIProvider` and the richer `ForgeReader` and `CIRuns`, the `Sonar` quality provider,
and the generic commands `sm ci ...` and `sm pr ...` (cli.py).

    from steuermaschine.github import Gh, GhError, GitHub

Rules of this package:
- the process runner (`Commands`) and the HTTP fetch function are injected, so tests use fakes;
- every return value of a provider is a pydantic model from `github.models`;
- a read never raises for what the world answers: the model says `unknown: <why>`;
- a write is dry unless `live=True`, and says what it would run.
"""

from .base import CIRuns, ForgeReader, ProviderError, QualityProvider
from .client import Gh, GhError
from .forge import GitHub

__all__ = ["CIRuns", "ForgeReader", "Gh", "GhError", "GitHub", "ProviderError", "QualityProvider"]
