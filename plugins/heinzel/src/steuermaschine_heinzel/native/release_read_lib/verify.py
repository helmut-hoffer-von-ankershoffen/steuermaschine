#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""hz-verify checks what is live and prints PASS or FAIL per line (exit 1 on any FAIL).

  hz-verify pypi heinzel 0.9.11 [--homepage heinzel.team]   version exists; its project links mention the host
  hz-verify site /imprint/ --contains "text" [--base https://heinzel.team]   page answers 200 and has each text
  hz-verify release app-v0.9.11 [--latest] [--repo plugins]   the GitHub release exists (app-* tags are in plugins)
Several checks can be chained in one call with `--and`."""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import edge  # moved: siblings are imported relatively, no sys.path entry
from ..queue_lib import hzlib  # noqa: E402


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sm release verify"}), timeout=30) as reply:
            return reply.status, reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, OSError):
        return 0, ""


def line(ok: bool, text: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {text}")
    return ok


def pypi(args) -> bool:
    status, body = fetch(f"https://pypi.org/pypi/{args.name}/{args.version}/json")
    ok = line(status == 200, f"pypi {args.name} {args.version}" + ("" if status == 200 else f" (HTTP {status})"))
    if args.homepage and status == 200:
        info = json.loads(body)["info"]
        links = " ".join([info.get("home_page") or "", *(info.get("project_urls") or {}).values()])
        ok &= line(args.homepage in links, f"pypi {args.name} links to {args.homepage}")
    return ok


def site(args) -> bool:
    url = args.path if args.path.startswith("http") else args.base.rstrip("/") + "/" + args.path.lstrip("/")
    status, body = fetch(url)
    ok = line(status == 200, f"site {url}" + ("" if status == 200 else f" (HTTP {status})"))
    for text in args.contains:
        ok &= line(text in body, f"site {url} contains {text!r}")
    return ok


def release(args) -> bool:
    repo = hzlib.SHORT[args.repo] if args.repo else (hzlib.PLUGINS if args.tag.startswith("app-") else hzlib.CORE)
    found = next((r for r in hzlib.ghj("release", "list", "-R", repo, "-L", "20", "--json", "tagName,isLatest", default=[]) or [] if r["tagName"] == args.tag), None)
    ok = line(found is not None, f"release {args.tag} in {repo.split('/')[-1]}")
    if args.latest and found:
        ok &= line(found["isLatest"], f"release {args.tag} is latest")
    return ok


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="sm release verify")
    sub = top.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pypi")
    p.add_argument("name")
    p.add_argument("version")
    p.add_argument("--homepage")
    p = sub.add_parser("site")
    p.add_argument("path")
    p.add_argument("--contains", action="append", default=[])
    p.add_argument("--base", default="https://heinzel.team")
    p = sub.add_parser("release")
    p.add_argument("tag")
    p.add_argument("--latest", action="store_true")
    p.add_argument("--repo", choices=[k for k in hzlib.SHORT if k])
    return top


def main(argv: list[str]) -> int:
    groups, current = [], []
    for word in argv:
        if word == "--and":
            groups.append(current)
            current = []
        else:
            current.append(word)
    groups.append(current)
    ok = True
    for group in groups:
        args = parser().parse_args(group)
        ok &= {"pypi": pypi, "site": site, "release": release}[args.cmd](args)
    return 0 if ok else 1

