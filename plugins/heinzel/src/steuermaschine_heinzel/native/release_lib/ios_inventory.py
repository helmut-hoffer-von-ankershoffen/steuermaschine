# /// script
# requires-python = ">=3.11"
# dependencies = ["pyjwt[crypto]"]
# ///
"""Read complete ASC inventory and scope the tagged TestFlight tool's number-based verbs to a verified build ID."""

import argparse
import json
import re
import sys
from collections.abc import Callable
from importlib.machinery import SourceFileLoader
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit


def inventory(call: Callable[[str], dict[str, Any]], app: str, api: str) -> dict[str, Any]:
    endpoint = "/v1/builds"
    filters = {"filter[app]": app, "include": "preReleaseVersion"}
    path = endpoint + "?" + urlencode({**filters, "limit": 200})
    seen = set()
    builds = {}
    total = None
    while path:
        if path in seen:
            raise ValueError("ASC build pagination repeated a page")
        seen.add(path)
        page = call(path)
        count = page["meta"]["paging"]["total"]
        if type(count) is not int or count < 0 or (total is not None and total != count):
            raise ValueError("ASC build inventory changed during pagination")
        total = count
        versions = {item["id"]: item["attributes"]["version"] for item in page.get("included", []) if item["type"] == "preReleaseVersions"}
        for item in page["data"]:
            identifier, attrs = item["id"], item["attributes"]
            if item["type"] != "builds" or not identifier or identifier in builds or not re.fullmatch("[0-9]+", attrs["version"]):
                raise ValueError("invalid or repeated ASC build")
            pre = item["relationships"]["preReleaseVersion"]["data"]
            short = versions[pre["id"]] if pre else None
            if type(attrs["expired"]) is not bool or not attrs["processingState"] or (pre and not short):
                raise ValueError("missing ASC build identity or state")
            builds[identifier] = {"build": attrs["version"], "short": short, "state": attrs["processingState"], "expired": attrs["expired"]}
        link = page["links"].get("next")
        if not link:
            break
        url, origin = urlsplit(link), urlsplit(api)
        params = parse_qs(url.query)
        if (url.scheme, url.netloc, url.path) != (origin.scheme, origin.netloc, endpoint) or any(params.get(k) != [v] for k, v in filters.items()):
            raise ValueError("unexpected ASC pagination URL")
        path = endpoint + "?" + url.query
    if len(builds) != total:
        raise ValueError("incomplete ASC build inventory")
    return {"app": app, "builds": builds}


def scoped_command(tool: ModuleType, env: dict[str, str], verb: str, args: list[str], version: str, identifier: str) -> None:
    """The original notes, beta and assign implementations all resolve numbers through this tool's builds()."""
    number = args[0]
    read_builds = tool.builds

    def exact_build(env: dict[str, str]) -> list[dict[str, Any]]:
        matches = [r for r in read_builds(env) if r["build"] == number]
        if len(matches) != 1 or matches[0]["id"] != identifier or matches[0]["short"] != version or matches[0]["expired"] or matches[0]["state"] != "VALID":
            raise ValueError("TestFlight lookup does not match the submitted build identity")
        return matches

    tool.__dict__["builds"] = exact_build
    try:
        operation = {"assign": tool.cmd_assign, "beta": tool.cmd_beta, "notes": tool.cmd_notes}[verb]
        operation(env, argparse.Namespace(build=number, file=args[1] if verb == "notes" else None))
    finally:
        tool.__dict__["builds"] = read_builds


if __name__ == "__main__":
    path = sys.argv[-1]
    loader = SourceFileLoader("release_testflight", path)
    tool = ModuleType(loader.name)
    tool.__file__ = path
    loader.exec_module(tool)
    env = tool.load_env()
    if len(sys.argv) == 2:
        print(json.dumps(inventory(lambda path: tool.call(env, "GET", path), tool.APP_ID, tool.API)))
    else:
        scoped_command(tool, env, sys.argv[1], sys.argv[2:-3], sys.argv[-3], sys.argv[-2])
