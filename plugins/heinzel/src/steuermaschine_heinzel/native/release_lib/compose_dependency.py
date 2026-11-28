"""Recover once from a protected composition's verified missing-publication receipt."""

import hashlib
import io
import json
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

from ..queue_lib import hzlib
from ..queue_lib.edge import ProcessError
from ..queue_lib.release_candidate import unique_object
from . import edge, snapshot

ARTIFACT = "standard-app-dependency"
MEMBER = ARTIFACT + ".json"
WORKFLOW = ".github/workflows/compose.yml"
ARCHIVE_LIMIT = 128 * 1024
MAX_DIST = 128 * 1024 * 1024


class Invalid(ValueError):
    """Insufficient or contradictory release evidence."""


class Pending(Exception):
    """The authenticated release lookup still reports an unpublished enrolled dependency."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def api(repo, path):
    value = hzlib.ghj("api", "--hostname", "github.com", f"repos/{repo}/{path}", default=None)
    require(isinstance(value, dict), f"cannot verify {repo}/{path}")
    return value


def rows(path, key):
    sep = "&" if "?" in path else "?"
    url = f"{path}{sep}per_page=100"
    first = api(hzlib.PLUGINS, url)
    total = first.get("total_count")
    require(type(total) is int and 0 <= total <= 1000, "compose inventory outside provider search bound")
    items = []
    for page in range(max(1, (total + 99) // 100)):
        data = first if page == 0 else api(hzlib.PLUGINS, f"{url}&page={page + 1}")
        batch = data.get(key)
        require(isinstance(batch, list) and data.get("total_count") == total and len(batch) == min(100, total - len(items)), "incomplete compose inventory")
        items.extend(batch)
    require(all(isinstance(x, dict) and type(x.get("id")) is int and x["id"] > 0 for x in items) and len({x["id"] for x in items}) == total, "duplicate or invalid compose inventory rows")
    return items


def main_sha():
    branch = api(hzlib.PLUGINS, "branches/main")
    sha = branch.get("commit", {}).get("sha")
    require(branch.get("name") == "main" and branch.get("protected") is True and isinstance(sha, str) and snapshot.COMMIT.fullmatch(sha), "cannot verify protected Plugins main")
    return sha


def identity(run, run_id, head):
    require(
        run.get("id") == run_id
        and type(run_id) is int
        and run_id > 0
        and run.get("path") == WORKFLOW
        and run.get("head_sha") == head
        and run.get("repository", {}).get("full_name") == hzlib.PLUGINS
        and run.get("head_repository", {}).get("full_name") == hzlib.PLUGINS
        and run.get("event") == "workflow_dispatch"
        and run.get("head_branch") == "main"
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1,
        "compose run is not the original protected dispatch",
    )


def job_inventory(run_id):
    jobs = rows(f"actions/runs/{run_id}/attempts/1/jobs", "jobs")
    require(all(j.get("run_id") == run_id and j.get("status") == "completed" for j in jobs) and len({j.get("name") for j in jobs}) == len(jobs), "unbound compose jobs")
    return {j["name"]: j for j in jobs}


def read_receipt(artifact, run_id, head):
    origin = artifact.get("workflow_run") or {}
    require(
        origin.get("id") == run_id
        and origin.get("head_sha") == head
        and artifact.get("expired") is False
        and type(artifact.get("size_in_bytes")) is int
        and 0 < artifact["size_in_bytes"] <= ARCHIVE_LIMIT,
        "dependency artifact origin or size differs",
    )
    result = edge.run(["gh", "api", "--hostname", "github.com", f"repos/{hzlib.PLUGINS}/actions/artifacts/{artifact['id']}/zip"], check=True, capture_output=True, timeout=30)
    raw = result.stdout
    require(len(raw) == artifact["size_in_bytes"] and artifact.get("digest") == "sha256:" + hashlib.sha256(raw).hexdigest(), "dependency artifact digest differs")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == [MEMBER] and archive.getinfo(MEMBER).file_size <= 65536, "invalid dependency artifact members")
        return json.loads(archive.read(MEMBER), object_pairs_hook=unique_object)


def checked_assets(repo, tag, revision, assets):
    """Verify the immutable release bytes and their source before asking CI to compose again."""
    with tempfile.TemporaryDirectory(prefix="sm-compose-") as directory:
        for asset in assets:
            name = asset["name"]
            require(
                asset.get("state") == "uploaded" and type(asset.get("size")) is int and 0 < asset["size"] <= MAX_DIST and re.fullmatch(r"sha256:[a-f0-9]{64}", asset.get("digest") or ""),
                "incomplete release asset",
            )
            args = ["gh", "release", "download", tag, "--repo", f"github.com/{repo}", "--pattern", name, "--dir", directory]
            edge.run(args, check=True, capture_output=True, timeout=120)
            path = Path(directory, name)
            require(path.stat().st_size == asset["size"], "release asset size differs")
            with path.open("rb") as stream:
                require("sha256:" + hashlib.file_digest(stream, "sha256").hexdigest() == asset["digest"], "release asset digest differs")
            args = ["gh", "attestation", "verify", str(path), "--repo", repo, "--hostname", "github.com", "--source-digest", revision, "--deny-self-hosted-runners"]
            if repo == hzlib.PLUGINS:
                args.extend(["--source-ref", "refs/heads/main", "--signer-workflow", f"{repo}/.github/workflows/release.yml"])
            edge.run(args, check=True, capture_output=True, timeout=120)


def tag_publication(tag):
    repo = hzlib.PLUGINS
    owner, name = repo.split("/")
    query = "query($owner:String!,$name:String!,$tag:String!,$ref:String!){repository(owner:$owner,name:$name){nameWithOwner release(tagName:$tag){databaseId isDraft} ref(qualifiedName:$ref){name}}}"
    result = hzlib.ghj(
        "api", "--hostname", "github.com", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}", "-f", f"tag={tag}", "-f", f"ref=refs/tags/{tag}", default=None
    )
    found = (result or {}).get("data", {}).get("repository") or {}
    require(result and not result.get("errors") and found.get("nameWithOwner") == repo and "release" in found and "ref" in found, "cannot verify release and tag publication")
    return found


def plugin_release(tag):
    found = tag_publication(tag)
    published = found["release"]
    if published is None or published.get("isDraft") is True:
        raise Pending(tag)
    identifier = published.get("databaseId")
    require(published.get("isDraft") is False and type(identifier) is int and identifier > 0, "invalid plugin release ID")
    release = api(hzlib.PLUGINS, f"releases/{identifier}")
    require(release.get("id") == identifier, "plugin release identity changed")
    return release


def tag_source(repo, tag):
    ref = api(repo, f"git/ref/tags/{quote(tag, safe='')}")
    require(ref.get("ref") == "refs/tags/" + tag, "release tag ref differs")
    obj = ref.get("object", {})
    for _ in range(4):
        sha = obj.get("sha")
        require(isinstance(sha, str) and snapshot.COMMIT.fullmatch(sha), "invalid tag object")
        if obj.get("type") == "commit":
            return sha
        require(obj.get("type") == "tag", "release ref does not identify a commit")
        annotated = api(repo, f"git/tags/{sha}")
        require(annotated.get("sha") == sha, "annotated tag object changed")
        obj = annotated.get("object", {})
    raise Invalid("release tag chain is too long")


def released_inputs(proof, version, head, run_id):
    require(
        isinstance(proof, dict)
        and set(proof) == {"schema", "kind", "plugins_sha", "core_version", "core_sha", "core_wheel_sha256", "run_id", "run_attempt", "missing"}
        and type(proof["schema"]) is int
        and proof["schema"] == 1
        and proof["kind"] == "enrolled-plugin-publication-missing"
        and proof["plugins_sha"] == head
        and proof["core_version"] == version
        and type(proof["run_id"]) is int
        and proof["run_id"] == run_id
        and type(proof["run_attempt"]) is int
        and proof["run_attempt"] == 1,
        "dependency receipt does not describe this release and failed attempt",
    )
    missing = proof["missing"]
    require(isinstance(missing, list) and 0 < len(missing) <= 32, "invalid missing-plugin set")
    names = []
    for row in missing:
        require(
            isinstance(row, dict)
            and set(row) == {"name", "version", "tag"}
            and isinstance(row["name"], str)
            and re.fullmatch(r"heinzel-[a-z0-9]+(?:-[a-z0-9]+)*", row["name"])
            and isinstance(row["version"], str)
            and snapshot.VERSION.fullmatch(row["version"])
            and row["tag"] == f"{row['name']}/v{row['version']}",
            "invalid enrolled dependency",
        )
        names.append(row["name"])
    require(names == sorted(set(names)), "unordered or duplicate enrolled dependencies")
    tag = f"v{version}"
    core = api(hzlib.CORE, f"releases/tags/{tag}")
    source = tag_source(hzlib.CORE, tag)
    require(
        isinstance(source, str) and snapshot.COMMIT.fullmatch(source) and source == proof["core_sha"] and snapshot.core_wheel_digest(core, version) == proof["core_wheel_sha256"],
        "official Core identity changed",
    )
    wheel = f"heinzel-{version}-py3-none-any.whl"
    pins = {}
    distributions = [(hzlib.CORE, tag, source, [a for a in core["assets"] if a["name"] == wheel])]
    for row in missing:
        release = plugin_release(row["tag"])
        commit = tag_source(hzlib.PLUGINS, row["tag"])
        require(
            isinstance(commit, str) and snapshot.COMMIT.fullmatch(commit) and release.get("tag_name") == row["tag"] and release.get("draft") is False and release.get("prerelease") is False,
            "plugin release unavailable",
        )
        prefix = row["name"].replace("-", "_") + "-" + row["version"]
        assets = release.get("assets")
        require(
            isinstance(assets, list)
            and len(assets) == 2
            and all(isinstance(a, dict) for a in assets)
            and sum(a.get("name") == prefix + ".tar.gz" for a in assets) == 1
            and sum(bool(re.fullmatch(re.escape(prefix) + r"-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl", a.get("name") or "")) for a in assets) == 1,
            "incomplete plugin wheel/sdist pair",
        )
        distributions.append((hzlib.PLUGINS, row["tag"], commit, assets))
        pins[row["tag"]] = {"revision": commit, "assets": {a["name"]: a["digest"].removeprefix("sha256:") for a in assets}}
    for distribution in distributions:
        checked_assets(*distribution)
    encoded = json.dumps(pins, sort_keys=True, separators=(",", ":"))
    require(len(encoded) <= 16 * 1024, "recovery dependency pins exceed the dispatch input bound")
    return {
        "dependency_pins": encoded,
        "expected_plugins_sha": head,
        "expected_core_version": version,
        "expected_core_sha": source,
        "expected_core_wheel_sha256": proof["core_wheel_sha256"],
        "recovery_marker": f"dep-{run_id}-{hashlib.sha256(encoded.encode()).hexdigest()}",
    }


def current_recovery(intent):
    """Search the complete bounded inventory after the source failure, including competing dispatches."""
    since = quote(">=" + intent["created_at"], safe="")
    inventory = rows(f"actions/workflows/compose.yml/runs?created={since}&branch=main", "workflow_runs")
    inputs = intent["inputs"]
    prefix = "Compose recovery " + inputs["recovery_marker"]
    title = f"{prefix} {inputs['expected_core_version']} {inputs['expected_core_sha']} {inputs['expected_core_wheel_sha256']}"
    family = f"Compose recovery dep-{intent['failed_run']}-"
    matches = [r for r in inventory if r.get("display_title", "").startswith(family)]
    require(all(r["display_title"] == title for r in matches), "compose recovery has conflicting inputs for the same failure")
    require(len(matches) <= 1, "ambiguous marked compose recovery")
    run = None
    if matches:
        run_id = matches[0]["id"]
        require(run_id != intent["failed_run"], "recovery resolved to the failed original")
        identity(matches[0], run_id, intent["inputs"]["expected_plugins_sha"])
        run = api(hzlib.PLUGINS, f"actions/runs/{run_id}")
        identity(run, run_id, intent["inputs"]["expected_plugins_sha"])
        require(run.get("display_title") == title, "recovery marker changed")
    if (bound := intent.get("run_id")) is not None:
        if not matches:
            run = api(hzlib.PLUGINS, f"actions/runs/{bound}")
            identity(run, bound, intent["inputs"]["expected_plugins_sha"])
            require(run.get("display_title") == title, "bound recovery marker differs")
        require(run["id"] == bound, "recovery run ID changed")
    if run is not None:
        created = run.get("created_at")
        require(
            run["id"] != intent["failed_run"] and isinstance(created, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", created) and created >= intent["created_at"],
            "recovery run predates or aliases its source",
        )
    return run, any(r.get("status") != "completed" for r in inventory if r not in matches)


def recovered(intent, remember):
    run, _ = current_recovery(intent)
    if run is None:
        return False
    if intent.get("run_id") is None:
        remember(compose_dependency=dict(intent, run_id=run["id"]))
    if run["status"] != "completed":
        return False
    require(run.get("conclusion") == "success", f"recovery compose {run['id']} failed; preserve both runs")
    jobs = job_inventory(run["id"])
    require(all(jobs.get(name, {}).get("conclusion") == "success" for name in ("select", "compose", "publish")), "recovery run did not publish through the compose workflow")
    return True


def begin(data, version, run, remember):
    """Called under the release lock. Never infer publication readiness from log text or elapsed time."""
    bound = data.get("compose_dispatch")
    run_id, head = run.get("databaseId"), run.get("headSha")
    if not isinstance(bound, dict) or data.get("compose_run_id") != run_id or data.get("compose_run_attempt") != 1:
        return False  # historical unbound state has no authority for a dependency recovery
    require(
        type(run_id) is int and run_id > 0 and isinstance(head, str) and snapshot.COMMIT.fullmatch(head) and data.get("version") == version and snapshot.VERSION.fullmatch(version),
        "invalid original compose binding",
    )
    artifacts = rows(f"actions/runs/{run_id}/artifacts", "artifacts")
    matches = [a for a in artifacts if a.get("name") == ARTIFACT]
    if not matches:
        jobs = job_inventory(run_id)
        steps = jobs.get("compose", {}).get("steps", [])
        require(not any(s.get("name") == "Check enrolled plugin publications" and s.get("conclusion") == "failure" for s in steps), "protected dependency preflight failed but its receipt is missing")
        return False
    require(len(matches) == 1, "ambiguous dependency receipts")
    wheel = f"https://github.com/{hzlib.CORE}/releases/download/v{version}/heinzel-{version}-py3-none-any.whl"
    require(
        bound.get("repo") == hzlib.PLUGINS
        and bound.get("workflow") == "compose.yml"
        and bound.get("ref") == "main"
        and bound.get("sha") == head
        and bound.get("event") == "workflow_dispatch"
        and bound.get("inputs") in ({}, {"version": version, "core_wheel": wheel}),
        "original dispatch inputs differ",
    )
    original = api(hzlib.PLUGINS, f"actions/runs/{run_id}")
    identity(original, run_id, head)
    require(
        original.get("conclusion") == "failure" and original.get("status") == "completed" and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", original.get("created_at") or ""),
        "original compose is not a terminal failure",
    )
    jobs = job_inventory(run_id)
    require({k: j.get("conclusion") for k, j in jobs.items()} == {"select": "success", "compose": "failure", "publish": "skipped", "finalize": "skipped"}, "another compose job failed or ran")
    steps = jobs["compose"].get("steps", [])
    require(
        [s["name"] for s in steps if s.get("conclusion") == "failure"] == ["Check enrolled plugin publications"]
        and sum(s.get("name") == "Upload enrolled plugin dependency evidence" and s.get("conclusion") == "success" for s in steps) == 1,
        "missing authenticated publication-check failure and evidence upload",
    )
    require(main_sha() == head, "protected Plugins source has moved; review the new source")
    proof = read_receipt(matches[0], run_id, head)
    try:
        inputs = released_inputs(proof, version, head, run_id)
    except Pending:
        return True
    intent = {"failed_run": run_id, "artifact_id": matches[0]["id"], "artifact_digest": matches[0]["digest"], "created_at": original["created_at"], "inputs": inputs, "missing": proof["missing"]}
    existing, busy = current_recovery(intent)
    if existing:
        remember(compose_dependency=dict(intent, run_id=existing["id"]))
        return True
    if busy:
        return True  # preserve a queued or running owner; check again on the next poll
    app = tag_publication(f"app-v{version}")
    require(app["release"] is None and app["ref"] is None, "app tag or release already exists; reconcile its publication")
    require(main_sha() == head, "protected main moved before recovery dispatch")
    identity(api(hzlib.PLUGINS, f"actions/runs/{run_id}"), run_id, head)
    # Persist before the POST. A timeout/crash leaves a reconcilable intent and cannot send a second POST.
    remember(compose_dependency=intent)
    args = ["api", "--hostname", "github.com", "-X", "POST", f"repos/{hzlib.PLUGINS}/actions/workflows/compose.yml/dispatches", "-H", "X-GitHub-Api-Version: 2026-03-10", "-f", "ref=main"]
    for name, value in inputs.items():
        args.extend(["-f", f"inputs[{name}]={value}"])
    code, response = hzlib.gh(*args)
    require(code == 0, "compose dispatch response uncertain; retain intent and reconcile the marked run")
    if response.strip():
        result = json.loads(response)
        require(isinstance(result, dict) and type(result.get("workflow_run_id")) is int and result["workflow_run_id"] > 0 and result["workflow_run_id"] != run_id, "invalid dispatch response")
        remember(compose_dependency=dict(intent, run_id=result["workflow_run_id"]))
    return True


EVIDENCE_ERRORS = (
    Invalid,
    snapshot.InvalidSnapshot,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    OSError,
    zipfile.BadZipFile,
    ProcessError,
)
