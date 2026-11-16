"""The shared confidentiality rule used by the CLI and the repository checks."""

import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TERMS_FILES = (
    Path("~/.config/heinzel-dev/confidential-terms"),
    Path("~/.config/steuermaschine/private-terms"),
)
ANYWHERE = 6  # words at least this long match inside other words too
NOT_A_LETTER_BEFORE, NOT_A_LETTER_AFTER = r"(?<![^\W\d_])", r"(?![^\W\d_])"
PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")
# Domains that name a machine or a home or company network, not a mailbox; git builds such an email from the hostname
# when user.email isn't set.
HOST_DOMAINS = (".local", ".localdomain", ".lan", ".home", ".internal")


@dataclass(frozen=True)
class Finding:
    """Where a term was found; `term` is its 1-based number in the list, so the term itself stays private."""

    where: str
    line: int
    term: int

    def __str__(self) -> str:
        """`path:line: confidential term #N`."""
        return f"{self.where}:{self.line}: confidential term #{self.term}"


def load_terms(env: Mapping[str, str]) -> list[str]:
    """The configured terms, or none: `$CONFIDENTIAL_TERMS` (CI), else `$CONFIDENTIAL_TERMS_FILE`, else both default files."""
    if text := env.get("CONFIDENTIAL_TERMS"):
        return parse(text)
    override = env.get("CONFIDENTIAL_TERMS_FILE")
    paths = [Path(override)] if override else list(DEFAULT_TERMS_FILES)
    terms: list[str] = []
    for path in paths:
        if (path := path.expanduser()).is_file():
            terms += parse(path.read_text(encoding="utf-8"))
    return terms


def parse(text: str) -> list[str]:
    """One term per line; blank lines and `#` comments are skipped."""
    return [term for line in text.splitlines() if (term := line.split("#", 1)[0].strip())]


def patterns(terms: Iterable[str]) -> list[re.Pattern[str]]:
    """Case-insensitive patterns, as described in the module docstring."""
    compiled: list[re.Pattern[str]] = []
    for term in terms:
        words = normalize(term).split()
        body = r"[\W_]*".join(map(re.escape, words))
        if len(words) > 1 or len(words[0]) < ANYWHERE:
            body = NOT_A_LETTER_BEFORE + body + NOT_A_LETTER_AFTER
        compiled.append(re.compile(body, re.IGNORECASE))
    return compiled


def normalize(text: str) -> str:
    """Fold compatibility characters and drop invisible ones (zero-width spaces, joiners, soft hyphens)."""
    return "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")


def scan(where: str, text: str, compiled: list[re.Pattern[str]]) -> Iterator[Finding]:
    """Find the terms in `text`.

    Yields:
        A finding per line and term.
    """
    text = normalize(text)
    seen: set[tuple[int, int]] = set()
    for index, pattern in enumerate(compiled, start=1):
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            if (line, index) not in seen:
                seen.add((line, index))
                yield Finding(where, line, index)


def hostname_email(ident: str) -> str | None:
    """The email of a `git var GIT_*_IDENT` line if its domain is a machine's hostname, else `None`."""
    match = re.search(r"<([^>]*)>", ident)
    email = match[1] if match else ""
    domain = email.rpartition("@")[2].lower().rstrip(".")
    return email if "." not in domain or domain.endswith(HOST_DOMAINS) else None


def text_bytes(data: bytes) -> str:
    """Read UTF-8 text or take the policy's printable strings from binary data."""
    if b"\0" not in data:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return "\n".join(run.decode("ascii") for run in PRINTABLE.findall(data))
