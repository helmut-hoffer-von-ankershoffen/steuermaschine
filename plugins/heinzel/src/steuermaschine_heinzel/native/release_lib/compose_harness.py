"""Explicit replacement of a failed compose after reviewed test-only changes."""

import base64
import hashlib
import json
import re
from typing import Any

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
TEST_PATH = re.compile(r"plugins/heinzel-[a-z0-9-]+/tests/(?:browser|acceptance|e2e)/[a-zA-Z0-9_./-]+\.(?:py|mjs|js|ts|tsx|json)")


def replace(rel: Any, version: str, run_id: int, attempt: int, reviewed_head: str,
            core_source: str, receipt_sha256: str, reason: str) -> None:
    """Verify the exact failed run and test-only diff, preserve its binding, and dispatch once.

    Call only while holding the existing release-tail lock. The normal compose workflow and
    its existing publication checks remain authoritative; this never accepts a failed run.
    """
    def require(condition: object, message: str) -> None:
        if not condition:
            raise rel.Stop(message)

    require(bool(rel.EXACT_VERSION.fullmatch(version)), "exact release version required")
    require(type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0,
            "exact failed compose run and attempt required")
    require(bool(SHA.fullmatch(reviewed_head)) and bool(SHA.fullmatch(core_source))
            and bool(DIGEST.fullmatch(receipt_sha256)), "invalid reviewed identity")
    require(isinstance(reason, str) and 10 <= len(reason.strip()) <= 4000, "written review reason required")
    path = rel.paths()[1]
    require(path == rel.tail_receipt(version), "compose replacement requires the exact release tail")
    raw = rel.release_rejection.private_read(path)
    require(hashlib.sha256(raw).hexdigest() == receipt_sha256, "release receipt changed")
    state = rel.state()
    require(state.get("version") == version and not state.get("finished"), "release tail is not unfinished")
    require(not state.get("compose_replacement_guard"), "a compose replacement is already pending")
    require(state.get("compose_run_id") == run_id and state.get("compose_run_attempt") == attempt,
            "receipt does not bind the exact failed run attempt")
    require(not any(row.get("failed_run") == run_id for row in state.get("compose_replaced", [])),
            "failed compose was already replaced")
    dispatch = state.get("compose_dispatch")
    require(isinstance(dispatch, dict) and dispatch.get("repo") == rel.PLUGINS
            and dispatch.get("workflow") == "compose.yml" and dispatch.get("ref") == "main"
            and dispatch.get("event") == "workflow_dispatch", "original compose dispatch is not bound")
    old_head = dispatch.get("sha")
    require(isinstance(old_head, str) and bool(SHA.fullmatch(old_head)) and old_head != reviewed_head,
            "original compose source is missing or unchanged")
    expected_wheel = (f"https://github.com/{rel.CORE}/releases/download/v{version}/"
                      f"heinzel-{version}-py3-none-any.whl")
    require(dispatch.get("inputs") == {"version": version, "core_wheel": expected_wheel},
            "original Core wheel inputs differ")
    run = rel.hzlib.ghj("api", f"repos/{rel.PLUGINS}/actions/runs/{run_id}", default=None)
    require(isinstance(run, dict) and run.get("id") == run_id and run.get("run_attempt") == attempt
            and run.get("path") == ".github/workflows/compose.yml" and run.get("event") == "workflow_dispatch"
            and run.get("head_sha") == old_head and run.get("status") == "completed"
            and run.get("conclusion") == "failure", "failed compose identity or attempt changed")
    code, tag_head = rel.hzlib.gh("api", f"repos/{rel.CORE}/commits/v{version}", "--jq", ".sha")
    require(code == 0 and tag_head.strip() == core_source and rel.release_producer_ready(f"v{version}"),
            "Core tag or published producer changed")
    code, main_head = rel.hzlib.gh("api", f"repos/{rel.PLUGINS}/commits/main", "--jq", ".sha")
    require(code == 0 and main_head.strip() == reviewed_head, "reviewed Plugins head is not current main")
    comparison = rel.hzlib.ghj("api", f"repos/{rel.PLUGINS}/compare/{old_head}...{reviewed_head}", default=None)
    files = comparison.get("files") if isinstance(comparison, dict) else None
    require(isinstance(comparison, dict) and comparison.get("status") == "ahead"
            and isinstance(files, list) and 0 < len(files) < 300, "Plugins comparison is incomplete or not ahead")
    require(all(isinstance(item, dict) and item.get("status") == "modified"
                and isinstance(item.get("filename"), str) and TEST_PATH.fullmatch(item["filename"])
                and ".." not in item["filename"].split("/") for item in files),
            "Plugins comparison includes changes outside reviewed acceptance tests")
    names = [item["filename"] for item in files]
    require(len(names) == len(set(names)), "duplicate changed test paths")
    for sha in (old_head, reviewed_head):
        tree = rel.hzlib.ghj("api", f"repos/{rel.PLUGINS}/git/trees/{sha}?recursive=1", default=None)
        entries = tree.get("tree") if isinstance(tree, dict) else None
        require(isinstance(tree, dict) and tree.get("truncated") is False and isinstance(entries, list),
                "Plugins source tree is unavailable or truncated")
        for name in names:
            found = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") == name]
            require(len(found) == 1 and found[0].get("type") == "blob" and found[0].get("mode") in ("100644", "100755"),
                    "changed test is not a regular file in both source trees")
    require(not rel.compose_busy(), "another compose is active")
    require(not rel.published_release(rel.PLUGINS, f"app-v{version}"), "an app release already exists")
    code, tag_head = rel.hzlib.gh("api", f"repos/{rel.CORE}/commits/v{version}", "--jq", ".sha")
    require(code == 0 and tag_head.strip() == core_source, "Core tag changed during verification")
    require(rel.release_rejection.private_read(path) == raw, "release receipt changed during verification")
    history = {"failed_run": run_id, "failed_attempt": attempt, "failed_head": old_head,
               "reviewed_head": reviewed_head, "core_source": core_source, "reason": reason,
               "receipt_sha256": receipt_sha256, "receipt_base64": base64.b64encode(raw).decode(),
               "files": names, "at": rel.now_iso()}
    rel.remember(compose_replaced=[*state.get("compose_replaced", []), history],
                 compose_replacement_guard={"failed_run": run_id, "reviewed_head": reviewed_head})
    code, main_head = rel.hzlib.gh("api", f"repos/{rel.PLUGINS}/commits/main", "--jq", ".sha")
    require(code == 0 and main_head.strip() == reviewed_head, "Plugins main moved before dispatch")
    rel.remember(compose=rel.now_iso(-5), compose_run_id=None, compose_run_attempt=None,
                 compose_dispatch={"repo": rel.PLUGINS, "workflow": "compose.yml", "ref": "main",
                                   "sha": reviewed_head, "event": "workflow_dispatch",
                                   "inputs": {"version": version, "core_wheel": expected_wheel}})
    code, response = rel.hzlib.gh("api", "-X", "POST", f"repos/{rel.PLUGINS}/actions/workflows/compose.yml/dispatches",
                                  "-H", "X-GitHub-Api-Version: 2026-03-10", "-f", "ref=main",
                                  "-f", f"inputs[version]={version}", "-f", f"inputs[core_wheel]={expected_wheel}")
    require(code == 0, "compose dispatch result is uncertain; reconcile before continuing")
    try:
        result = json.loads(response)
        replacement_id = result.get("workflow_run_id")
    except (ValueError, AttributeError):
        replacement_id = None
    if type(replacement_id) is int and replacement_id > 0 and replacement_id != run_id:
        rel.remember(compose_run_id=replacement_id, compose_run_attempt=1)
    rel.hzlib.say(f"fresh compose requested for {version}; preserved failed run {run_id}")
