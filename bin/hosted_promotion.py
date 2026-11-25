"""Verify exact hosted promotion test and coverage evidence; never write a gate receipt."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import debt
from coverage.results import display_covered

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH = re.compile(r"(\d+)% \((\d+)/(\d+)\)")
CORE = "src/steuermaschine/"
PLUGIN = "plugins/heinzel/src/steuermaschine_heinzel/"
TRUSTED = (".github/workflows/ci.yml", "Makefile", "pyproject.toml", "uv.lock", "docs/debt.json")
MAX_ARCHIVE = 10 * 1024 * 1024
MAX_XML = 50 * 1024 * 1024
CHECK_STEPS = (
    "Public history guard (every pushed or proposed commit descends from the public root)",
    "Private-term deny-list (tree and commit messages)",
    "Conventional Commits (the commits this push or PR adds)",
    "Run make install", "Run make check", "Run make audit", "Coverage summary",
    "Run actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
)
SUMMARY_STEP = "Require integration and test results"


class Refused(ValueError):
    """Hosted evidence is missing, ambiguous or below the local gate."""


def require(condition: object, why: str) -> None:
    if not condition:
        raise Refused(why)


def command(*args: str) -> bytes:
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, check=False, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("evidence command unavailable or timed out") from exc
    require(result.returncode == 0, "evidence command failed")
    return result.stdout


def api(path: str) -> dict:
    try:
        value = json.loads(command("gh", "api", path))
    except (ValueError, UnicodeError) as exc:
        raise Refused("GitHub returned malformed evidence") from exc
    require(isinstance(value, dict), "GitHub evidence is not an object")
    return value


def trusted_sources(installed: Path) -> None:
    for name in TRUSTED:
        try:
            require((ROOT / name).read_bytes() == (installed / name).read_bytes(), f"trusted gate source differs: {name}")
        except OSError as exc:
            raise Refused(f"trusted gate source unavailable: {name}") from exc


def inventory(document: dict, key: str) -> list:
    rows = document.get(key)
    require(isinstance(rows, list) and type(document.get("total_count")) is int
            and document["total_count"] == len(rows) and len(rows) < 100, f"{key} inventory incomplete")
    return rows


def good_job(job: dict, required_steps: tuple[str, ...]) -> None:
    require(job.get("status") == "completed" and job.get("conclusion") == "success"
            and type(job.get("runner_id")) is int and job["runner_id"] > 0
            and job.get("runner_group_id") == 0 and job.get("runner_group_name") == "GitHub Actions"
            and str(job.get("runner_name", "")).startswith("GitHub Actions ")
            and "self-hosted" not in (job.get("labels") or []), "required job did not run successfully on a hosted runner")
    steps = job.get("steps")
    require(isinstance(steps, list), "job steps unavailable")
    for name in required_steps:
        found = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
        require(len(found) == 1 and found[0].get("status") == "completed" and found[0].get("conclusion") == "success",
                f"required step missing or unsuccessful: {name}")


def verify_jobs(jobs: list, run_id: int, sha: str, branch: str) -> None:
    """Require the complete legacy or split CI layout for this exact push."""
    by_name = {}
    for job in jobs:
        require(isinstance(job, dict) and isinstance(job.get("name"), str) and job["name"] not in by_name,
                "malformed or duplicate hosted job")
        by_name[job["name"]] = job

    required = {"check": CHECK_STEPS, "Test (ubuntu-24.04, Python 3.15)": ("Tests",)}
    if branch == "main":
        required["Test (macos-latest, Python 3.15)"] = ("Tests",)
        required["badge"] = ("Write badges/coverage.svg",)
    legacy = set(required)
    split = legacy | {"Integration checks"}
    names = set(by_name)
    # The badge is a main-only job. Older non-main push evidence can include
    # the skipped badge row, which never contributes a successful gate.
    if branch != "main":
        names.discard("badge")
    require(names in (legacy, split), "hosted jobs do not match a complete CI layout")
    if names == split:
        required["check"] = (SUMMARY_STEP,)
        required["Integration checks"] = CHECK_STEPS
    for name, steps in required.items():
        job = by_name[name]
        require(type(job.get("run_id")) is int and job["run_id"] == run_id
                and type(job.get("run_attempt")) is int and job["run_attempt"] == 1
                and job.get("head_sha") == sha, "job identity differs")
        good_job(job, steps)
    if names == split:
        allowed = {"Set up job", SUMMARY_STEP, "Complete job"}
        require(all(isinstance(step, dict) and step.get("name") in allowed for step in by_name["check"]["steps"]),
                "hosted summary job contains unexpected steps")
    integration = by_name["Integration checks" if names == split else "check"]
    require(not any(isinstance(step, dict) and step.get("name") == SUMMARY_STEP for step in integration["steps"]),
            "hosted integration job contains a summary step")


def coverage(xml: bytes, expected_files: set[str], baseline: dict) -> dict[str, float]:
    require(len(xml) <= MAX_XML and b"<!DOCTYPE" not in xml.upper() and b"<!ENTITY" not in xml.upper(), "unsafe coverage XML")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise Refused("malformed coverage XML") from exc
    require(root.tag == "coverage", "invalid coverage root")
    groups = {key: [0, 0] for key in ("core", "plugin", "new", "moved")}
    totals = [0, 0, 0, 0]
    seen = set()
    for cls in root.findall(".//class"):
        name = cls.get("filename")
        require(isinstance(name, str) and name in expected_files and name not in seen, "unknown or duplicate coverage file")
        seen.add(name)
        kinds = ["core"] if name.startswith(CORE) else ["plugin", "moved" if re.search(r"/native/[^/]*_lib/", name) else "new"]
        lines = set()
        for line in cls.findall("./lines/line"):
            number, hits = line.get("number", ""), line.get("hits", "")
            require(number.isdigit() and int(number) > 0 and str(int(number)) == number and number not in lines and hits.isdigit(), "invalid or duplicate coverage line")
            lines.add(number)
            hit = int(hits) > 0
            covered, valid = int(hit), 1
            totals[0] += 1
            totals[1] += int(hit)
            if line.get("branch") == "true":
                match = BRANCH.fullmatch(line.get("condition-coverage", ""))
                require(match is not None, "missing branch counts")
                branch_hit, branch_total = int(match[2]), int(match[3])
                require(branch_total > 0 and branch_hit <= branch_total, "invalid branch counts")
                valid += branch_total
                covered += branch_hit
                totals[2] += branch_total
                totals[3] += branch_hit
            else:
                require(line.get("branch") in (None, "false"), "invalid branch marker")
            for kind in kinds:
                groups[kind][0] += covered
                groups[kind][1] += valid
    require(seen == expected_files and all(total > 0 for _, total in groups.values()), "coverage file inventory or group missing")
    try:
        root_totals = [int(root.attrib[name]) for name in ("lines-valid", "lines-covered", "branches-valid", "branches-covered")]
    except (KeyError, ValueError) as exc:
        raise Refused("coverage totals unavailable") from exc
    require(root_totals == totals, "coverage totals do not reconcile")
    values = {name: float(display_covered(100 * hit / total, 0)) for name, (hit, total) in groups.items()}
    require(values["core"] >= 90 and values["plugin"] >= 90, "hosted package coverage is below 90 percent")
    metrics = {"coverage core %": values["core"], "coverage plugin new code %": values["new"],
               "coverage plugin moved libraries %": values["moved"]}
    require(not debt.problems(metrics, baseline), "hosted coverage violates the existing debt ratchet")
    return values


def verify(repo: str, run_id: int, sha: str, installed: Path) -> dict:
    require(bool(REPO.fullmatch(repo)) and type(run_id) is int and run_id > 0 and bool(SHA.fullmatch(sha)), "invalid hosted identity")
    trusted_sources(installed)
    require(command("git", "rev-parse", "HEAD").decode().strip() == sha
            and not command("git", "status", "--porcelain"), "candidate checkout is not clean at the supplied head")
    remote = command("git", "remote", "get-url", "origin").decode().strip()
    require(remote in (f"https://github.com/{repo}.git", f"https://github.com/{repo}", f"git@github.com:{repo}.git"),
            "repository differs from origin")
    branch = command("git", "branch", "--show-current").decode().strip()
    require(bool(branch), "detached candidate branch")
    repository = api(f"repos/{repo}")
    run = api(f"repos/{repo}/actions/runs/{run_id}")
    require(type(repository.get("id")) is int and run.get("id") == run_id and run.get("run_attempt") == 1
            and run.get("status") == "completed" and run.get("conclusion") == "success" and run.get("event") == "push"
            and run.get("path") == ".github/workflows/ci.yml" and run.get("head_sha") == sha and run.get("head_branch") == branch
            and (run.get("repository") or {}).get("id") == repository["id"]
            and (run.get("head_repository") or {}).get("id") == repository["id"], "hosted run identity or result differs")
    jobs = inventory(api(f"repos/{repo}/actions/runs/{run_id}/attempts/1/jobs?per_page=100"), "jobs")
    verify_jobs(jobs, run_id, sha, branch)
    artifacts = inventory(api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"), "artifacts")
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == "coverage"]
    require(len(matches) == 1, "coverage artifact missing or duplicated")
    artifact = matches[0]
    owner = artifact.get("workflow_run") or {}
    require(type(artifact.get("id")) is int and artifact["id"] > 0 and artifact.get("expired") is False
            and type(artifact.get("size_in_bytes")) is int and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE
            and owner.get("id") == run_id and owner.get("head_sha") == sha
            and owner.get("repository_id") == repository["id"] and owner.get("head_repository_id") == repository["id"],
            "coverage artifact identity differs")
    archive = command("gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip")
    require(len(archive) <= MAX_ARCHIVE and artifact.get("digest") == "sha256:" + hashlib.sha256(archive).hexdigest(),
            "coverage artifact digest differs")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            require(zipped.namelist() == ["coverage.xml"] and zipped.getinfo("coverage.xml").file_size <= MAX_XML,
                    "unexpected coverage archive")
            xml = zipped.read("coverage.xml")
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise Refused("invalid coverage archive") from exc
    tracked = command("git", "ls-files", "-z", "src/steuermaschine", "plugins/heinzel/src/steuermaschine_heinzel").decode().split("\0")
    expected = {name for name in tracked if name.endswith(".py") and (name.startswith(CORE) or name.startswith(PLUGIN))}
    try:
        baseline = json.loads((ROOT / "docs/debt.json").read_text())
    except (OSError, ValueError) as exc:
        raise Refused("debt baseline unavailable") from exc
    return {"repo": repo, "run": run_id, "head": sha, "artifact": artifact["id"], "digest": artifact["digest"],
            "coverage": coverage(xml, expected, baseline)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run", required=True, type=int)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--installed", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.repo, args.run, args.sha, args.installed), sort_keys=True))
    except (Refused, UnicodeError, OSError, ValueError) as exc:
        print(f"hosted promotion evidence refused: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
