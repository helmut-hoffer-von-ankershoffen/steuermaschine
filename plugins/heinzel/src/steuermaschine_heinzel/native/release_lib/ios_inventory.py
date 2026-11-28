"""Read complete App Store Connect inventory and operate on a verified build UUID."""

import ast
import json
import os
import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import jwt

API = "https://api.appstoreconnect.apple.com"
CONSTANTS = frozenset({"APP_ID", "GROUP_NAME", "NOTES_LOCALE", "API"})
RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
BUILD_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
GROUP_POST = re.compile(r"/v1/betaGroups/[A-Za-z0-9_-]{1,100}/relationships/builds\Z")
GROUP_REFUSAL = {
    "code": "ENTITY_UNPROCESSABLE",
    "title": "Builds cannot be assigned to this internal group.",
    "detail": "Cannot add internal group to a build.",
}


@dataclass(frozen=True)
class TagIdentity:
    app: str
    group: str
    locale: str


def tag_identity(path: Path) -> TagIdentity:
    """Read four top-level literal values without importing or running the tag's script."""
    module = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, str] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if name not in CONSTANTS:
            continue
        if name in values or not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            raise ValueError("expected unique literal App Store Connect identity")
        values[name] = node.value.value
    stores = [node for node in ast.walk(module) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in CONSTANTS]
    if values.keys() != CONSTANTS or len(stores) != len(CONSTANTS) or values["API"] != API:
        raise ValueError("unexpected tagged App Store Connect identity or API origin")
    app, group, locale = values["APP_ID"], values["GROUP_NAME"], values["NOTES_LOCALE"]
    if not re.fullmatch(r"[0-9]+", app) or not group.isprintable() or len(group) > 200 or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", locale):
        raise ValueError("invalid tagged App Store Connect app, group or locale")
    return TagIdentity(app, group, locale)


def auth_token() -> str:
    """Read the tagged tool's ASC_ENV_FILE contract only when opening the client."""
    config = Path(os.environ.get("ASC_ENV_FILE", "~/.appstoreconnect/heinzel-mini.env")).expanduser()
    fields = {}
    try:
        for raw in config.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                fields[key.removeprefix("export ").strip()] = value.strip().strip("'\"")
        key_path = Path(os.path.expandvars(fields["ASC_KEY_PATH"])).expanduser()
        private_key = key_path.read_text(encoding="utf-8")
        now = int(time.time())
        return jwt.encode(
            {"iss": fields["ASC_ISSUER_ID"], "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
            private_key,
            algorithm="ES256",
            headers={"kid": fields["ASC_KEY_ID"]},
        )
    except OSError, ValueError, KeyError, TypeError, jwt.PyJWTError:
        raise ValueError("could not read or sign with the App Store Connect credentials") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class AscRequestError(RuntimeError):
    """A credential-free transport failure suitable for the release caller's diagnostic."""


def assignment_refusal(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except ValueError, RecursionError:
        return False
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return isinstance(errors, list) and len(errors) == 1 and isinstance(errors[0], dict) and all(errors[0].get(key) == expected for key, expected in GROUP_REFUSAL.items())


def request_failure(error: HTTPError, method: str, path: str) -> AscRequestError:
    with error:
        if method == "POST" and error.code == 422 and GROUP_POST.fullmatch(path):
            try:
                matches = assignment_refusal(error.read(65536))
            except OSError, HTTPException:
                matches = False
            if matches:
                payload = json.dumps({"errors": [GROUP_REFUSAL]}, separators=(",", ":"))
                return AscRequestError(f"POST {path}: HTTP 422 {payload}")
    return AscRequestError(f"ASC {method} request failed: HTTP {error.code}")


class Call(Protocol):
    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None, /) -> dict[str, Any]: ...


def client(timeout: float) -> Call:
    deadline = time.monotonic() + timeout
    if timeout <= 0:
        raise AscRequestError("App Store Connect request deadline exhausted")
    bearer = auth_token()
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urlsplit(path)
        if not url.path.startswith("/v1/") or url.scheme or url.netloc or url.fragment or re.search(r"[^A-Za-z0-9_/-]", url.path) or re.search(r"[\x00-\x20\x7f]", path):
            raise ValueError("invalid local App Store Connect endpoint")
        request = Request(
            API + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
            method=method,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AscRequestError("App Store Connect request deadline exhausted")
        try:
            with opener.open(request, timeout=min(60, remaining)) as response:
                response_body = response.read()
        except HTTPError as error:
            raise request_failure(error, method, path) from None
        except URLError, OSError, HTTPException:
            raise AscRequestError(f"ASC {method} request failed") from None
        result = json.loads(response_body) if response_body else {}
        if not isinstance(result, dict):
            raise ValueError("invalid App Store Connect response")
        return result

    return call


def pages(call: Callable[[str], dict[str, Any]], api: str, endpoint: str, filters: dict[str, str], limit: int) -> Iterator[dict[str, Any]]:
    query = urlencode({**filters, "limit": limit})
    seen: set[str] = set()
    origin = urlsplit(api)
    while True:
        if query in seen:
            raise ValueError("ASC pagination repeated a page")
        seen.add(query)
        page = call(endpoint + "?" + query)
        yield page
        link = page.get("links", {}).get("next")
        if not link:
            return
        url = urlsplit(link)
        params = parse_qs(url.query, keep_blank_values=True)
        if (url.scheme, url.netloc, url.path, url.fragment) != (origin.scheme, origin.netloc, endpoint, "") or any(params.get(k) != [v] for k, v in filters.items()):
            raise ValueError("unexpected ASC pagination URL")
        query = url.query


def inventory(call: Callable[[str], dict[str, Any]], app: str, api: str = API) -> dict[str, Any]:
    builds: dict[str, Any] = {}
    total = None
    for page in pages(call, api, "/v1/builds", {"filter[app]": app, "include": "preReleaseVersion"}, 200):
        count = page["meta"]["paging"]["total"]
        if type(count) is not int or count < 0 or (total is not None and total != count):
            raise ValueError("ASC build inventory changed during pagination")
        total = count
        versions = {item["id"]: item["attributes"]["version"] for item in page.get("included", []) if item["type"] == "preReleaseVersions"}
        for item in page["data"]:
            identifier, attrs = item["id"], item["attributes"]
            if item["type"] != "builds" or not identifier or identifier in builds or not re.fullmatch(r"[0-9]+", attrs["version"]):
                raise ValueError("invalid or repeated ASC build")
            pre = item["relationships"]["preReleaseVersion"]["data"]
            short = versions[pre["id"]] if pre else None
            if type(attrs["expired"]) is not bool or not attrs["processingState"] or (pre and not short):
                raise ValueError("missing ASC build identity or state")
            builds[identifier] = {"build": attrs["version"], "short": short, "state": attrs["processingState"], "expired": attrs["expired"]}
    if len(builds) != total:
        raise ValueError("incomplete ASC build inventory")
    return {"app": app, "builds": builds}


def related(call: Call, endpoint: str, limit: int, kind: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for page in pages(lambda path: call("GET", path), API, endpoint, {}, limit):
        for row in page["data"]:
            identifier = row["id"]
            if row["type"] != kind or not RESOURCE_ID.fullmatch(identifier) or identifier in rows:
                raise ValueError("invalid or repeated ASC resource identity")
            rows[identifier] = row
    return rows


def verify_build(call: Call, app: str, number: str, version: str, identifier: str) -> None:
    builds = inventory(lambda path: call("GET", path), app)["builds"]
    matches = [key for key, row in builds.items() if row["build"] == number]
    if matches != [identifier] or builds[identifier] != {"build": number, "short": version, "state": "VALID", "expired": False}:
        raise ValueError("TestFlight lookup does not match the submitted build identity")


def assign(call: Call, tag: TagIdentity, number: str, version: str, identifier: str) -> str | None:
    groups = related(call, f"/v1/apps/{tag.app}/betaGroups", 200, "betaGroups")
    matches = [key for key, row in groups.items() if row["attributes"]["name"] == tag.group and row["attributes"].get("isInternalGroup") is True]
    if len(matches) != 1:
        raise ValueError("expected exactly one tagged internal TestFlight group")
    path = f"/v1/betaGroups/{matches[0]}/relationships/builds"
    members = related(call, path, 200, "builds")
    verify_build(call, tag.app, number, version, identifier)
    if identifier in members:
        return None
    call("POST", path, {"data": [{"type": "builds", "id": identifier}]})
    return f"assigned build {number} ({identifier})"


def plain_notes(markdown: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE).replace("**", "").replace("`", "")
    text = "".join(c for c in text if unicodedata.category(c) not in ("So", "Sk", "Cs", "Co", "Cn", "Mn", "Cf") or c in "\n\t")
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:4000]


def notes(call: Call, tag: TagIdentity, number: str, version: str, identifier: str, text: str) -> str:
    localizations = related(call, f"/v1/builds/{identifier}/betaBuildLocalizations", 50, "betaBuildLocalizations")
    matches = [key for key, row in localizations.items() if row["attributes"]["locale"] == tag.locale]
    if len(matches) > 1:
        raise ValueError("ambiguous TestFlight notes localization")
    verify_build(call, tag.app, number, version, identifier)
    data: dict[str, Any] = {"type": "betaBuildLocalizations", "attributes": {"whatsNew": text}}
    if matches:
        data["id"] = matches[0]
        call("PATCH", f"/v1/betaBuildLocalizations/{matches[0]}", {"data": data})
    else:
        data["attributes"]["locale"] = tag.locale
        data["relationships"] = {"build": {"data": {"type": "builds", "id": identifier}}}
        call("POST", "/v1/betaBuildLocalizations", {"data": data})
    return f"notes set for build {number} ({len(text)} characters)"


def run(call: Call, tag: TagIdentity, command: str, number: str, version: str, identifier: str, text: str = "") -> str | None:
    if not BUILD_UUID.fullmatch(identifier) or not re.fullmatch(r"[0-9]+", number) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("invalid submitted TestFlight build identity")
    if command == "assign":
        return assign(call, tag, number, version, identifier)
    if command == "beta":
        verify_build(call, tag.app, number, version, identifier)
        state = call("GET", f"/v1/builds/{identifier}/buildBetaDetail")["data"]["attributes"].get("internalBuildState")
        if state is None:
            state = "None"
        elif not isinstance(state, str) or not re.fullmatch(r"[A-Z_]+", state):
            raise ValueError("invalid internal TestFlight build state")
        return f"build {number} internalBuildState={state}"
    if command == "notes":
        return notes(call, tag, number, version, identifier, plain_notes(text))
    raise ValueError("invalid App Store Connect command")


def invoke(tool: Path, action: str, *args: str, timeout: float = 120) -> str:
    """Call ASC from the registered release writer's verified operation context."""
    try:
        tag = tag_identity(tool)
        call = client(timeout)
        if action == "inventory" and not args:
            return json.dumps(inventory(lambda path: call("GET", path), tag.app))
        if action == "notes" and len(args) == 4:
            number, version, identifier, filename = args
            text = Path(filename).read_text(encoding="utf-8")
            return run(call, tag, action, number, version, identifier, text) or ""
        if action in ("assign", "beta") and len(args) == 3:
            number, version, identifier = args
            return run(call, tag, action, number, version, identifier) or ""
        raise ValueError("invalid App Store Connect arguments")
    except ValueError, OSError, KeyError, TypeError, SyntaxError, RecursionError:
        raise AscRequestError("Could not verify App Store Connect identity or credentials.") from None
