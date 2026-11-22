"""Bind public release acceptance to immutable GitHub assets and its source commit."""

import base64
import hashlib
import json
import re
import tomllib
import urllib.request

from ..queue_lib import hzlib

SHA = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
WORKFLOW = ".github/workflows/acceptance.yml"
PINS = "release/pins.toml"
"""Core pins the reviewed workflow digest here; a Core contract test fails the PR that changes one without the other."""

VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


class InvalidSnapshot(ValueError):
    pass


def asset_digest(release: dict, filename: str) -> str:
    """Fail closed on a missing, duplicate, unpublished, or unhashed release asset."""
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise InvalidSnapshot("release is not published and final")
    return _provider_digest(release, filename)


def _provider_digest(release: dict, filename: str) -> str:
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == filename]
    if len(assets) != 1:
        raise InvalidSnapshot(f"release must contain exactly one {filename}")
    digest = assets[0].get("digest", "")
    if not isinstance(digest, str) or not digest.startswith("sha256:") or not SHA.fullmatch(digest[7:]):
        raise InvalidSnapshot(f"{filename} has no provider SHA256 digest")
    return digest[7:]


HELD_ASSETS = ("artifact-manifest.json",)
"""Besides the wheel, the Publish workflow that holds a release (core ADR 0114) always attaches these."""


def core_wheel_digest(release: dict, version: str) -> str:
    """The Core wheel digest of the final vX.Y.Z release, or of the same release HELD as a pre-release for acceptance.

    A held release is accepted only as the new Publish flow creates it: exact tag, published (never a draft),
    `prerelease` exactly True, and its producer assets present with provider digests. Every other release, and the
    app release, keeps the strict published-final rule of `asset_digest`.
    """
    if not VERSION.fullmatch(version) or release.get("tag_name") != f"v{version}":
        raise InvalidSnapshot("release tag differs from the requested version")
    wheel = f"heinzel-{version}-py3-none-any.whl"
    if release.get("draft") is not False or release.get("prerelease") is not True:
        return asset_digest(release, wheel)
    for name in HELD_ASSETS:
        _provider_digest(release, name)
    return _provider_digest(release, wheel)


def fetch_profile(url: str) -> bytes:
    """Download only the official profile selected by the validated release version."""
    with urllib.request.urlopen(url, timeout=30) as reply:
        if not reply.url.startswith("https://"):
            raise InvalidSnapshot("profile redirected away from HTTPS")
        content = reply.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise InvalidSnapshot("profile exceeds the installer limit")
    return content


def validate_profile(content: bytes, version: str, core_digest: str, app: dict) -> str:
    """Bind every artifact to its published profile and the selected Core wheel."""
    profile_digest = asset_digest(app, "standard.json")
    if hashlib.sha256(content).hexdigest() != profile_digest:
        raise InvalidSnapshot("profile bytes differ from the provider digest")
    try:
        profile = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise InvalidSnapshot("profile is not JSON") from exc
    artifacts = profile.get("artifacts") if isinstance(profile, dict) else None
    if not isinstance(profile, dict) or profile.get("format") != 2 or profile.get("core_version") != version or not isinstance(artifacts, list) or not artifacts:
        raise InvalidSnapshot("profile selects a different release or invalid format")
    names = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise InvalidSnapshot("profile artifact is invalid")
        name, filename, digest = (artifact.get(key) for key in ("name", "filename", "sha256"))
        if not isinstance(name, str) or name in names or not isinstance(filename, str):
            raise InvalidSnapshot("profile artifact identity is missing or repeated")
        names.add(name)
        if artifact.get("url") != f"https://github.com/{hzlib.PLUGINS}/releases/download/app-v{version}/{filename}":
            raise InvalidSnapshot("profile artifact is outside the pinned app release")
        if digest != asset_digest(app, filename):
            raise InvalidSnapshot("profile artifact differs from the published asset")
        if name == "heinzel" and (artifact.get("version") != version or digest != core_digest):
            raise InvalidSnapshot("profile selects a different Core wheel")
    if "heinzel" not in names:
        raise InvalidSnapshot("profile has no Core wheel")
    return profile_digest


def capture(version: str) -> dict:
    """Read the exact source, wheel and profile identities that public acceptance must prove."""
    if not VERSION.fullmatch(version):
        raise InvalidSnapshot("release version is not canonical")
    tag = f"v{version}"
    code, commit = hzlib.gh("api", f"repos/{hzlib.CORE}/commits/{tag}", "--jq", ".sha")
    commit = commit.strip()
    if code != 0 or not COMMIT.fullmatch(commit):
        raise InvalidSnapshot("release tag has no verified source commit")
    release = hzlib.ghj("api", f"repos/{hzlib.CORE}/releases/tags/{tag}", default={}) or {}
    wheel_digest = core_wheel_digest(release, version)
    app = hzlib.ghj("api", f"repos/{hzlib.PLUGINS}/releases/tags/app-v{version}", default={}) or {}
    content = fetch_profile(f"https://github.com/{hzlib.PLUGINS}/releases/download/app-v{version}/standard.json")
    profile_digest = validate_profile(content, version, wheel_digest, app)
    return {"version": version, "tag": tag, "commit": commit, "wheel_sha256": wheel_digest, "profile_sha256": profile_digest}


def _core_file(path: str, commit: str) -> bytes | None:
    """The exact bytes of one Core file at one commit, or None."""
    code, encoded = hzlib.gh("api", f"repos/{hzlib.CORE}/contents/{path}?ref={commit}", "--jq", ".content")
    if code != 0:
        return None
    try:
        return base64.b64decode("".join(encoded.split()), validate=True)
    except (ValueError, TypeError):
        return None


def workflow_supports_snapshot(commit: str) -> bool:
    """The acceptance workflow at this commit is the one Core pinned at the same commit.

    The receipt check alone can fail open: the workflow writes the receipt, so a workflow that ignores the dispatched
    digests can still echo them. Core reviews the workflow and its digest in `release/pins.toml` in one PR (its
    contract test fails a workflow change without the pin), and sm reads both at the commit, never a copy of its own.
    A commit without the pin, a malformed pin or a different workflow fails closed.
    """
    if not COMMIT.fullmatch(commit):
        return False
    pins, source = _core_file(PINS, commit), _core_file(WORKFLOW, commit)
    if pins is None or source is None:
        return False
    try:
        pinned = tomllib.loads(pins.decode("utf-8"))["sha256"][WORKFLOW]
    except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return False
    return isinstance(pinned, str) and SHA.fullmatch(pinned) is not None and hashlib.sha256(source).hexdigest() == pinned
