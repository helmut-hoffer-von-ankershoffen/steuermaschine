# mypy: disable-error-code="no-untyped-call"
# (focused_resulting is moved, untyped code; this module calls its reviewed registry witness)
"""Repair a red main whose exact tree has no focused plan: the batch plan trap after a queue merge.

A PR admitted at one main (its pair: `pull_request:<head>` and `push-tree:<merge-tree(base, head)>`) can be merged by
the queue onto a LATER main. The real squash tree then differs from the admitted one, main's push CI fails in
"Changed paths / Resolve the exact reviewed focused plan" with "No focused plan for this exact source", and nothing
supported could add the missing plan (every admission binds the CURRENT main as the base of a NEW head).

This route admits exactly one headless `push-tree:<main tree>` with base `main^`, and only when all of this holds,
re-proved at prepare and again before and after the one slot write:

1. main's latest push run of ci.yml is attempt 1, completed, failed, and its only failing jobs are "Changed paths"
   (failed step: the focused plan resolver) and the aggregate "CI OK"; the resolver's log says there is no plan.
2. main is the single-parent squash of one merged PR whose head is the packet head, the merge queue recorded that
   merge, and an admission receipt of that head is on record (the original pair was admitted and written).
3. the packet's resulting-main plan has main's exact parent and tree, and the same selection and case digests as the
   admitted pair (the PR plan and the admitted resulting-main plan, both read from the registry by the trusted codec).
4. the PR changes the same paths on both bases, and every path the PR and the intervening merges both changed is
   covered by the reviewed selection (a selected test, a declared production file, or web source under a selected
   web lane).

Everything else refuses. Nothing here writes; the slot write is the existing append-only path.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ... import state as statedir  # the plugin's state paths
from . import focused_admit as a
from . import focused_resulting as resulting

MODE = 'repair-main/v1'
CI = '.github/workflows/ci.yml'
JOB, STEP = 'Changed paths', 'Resolve the exact reviewed focused plan'
AGGREGATE, AGGREGATE_STEP = 'CI OK', 'Fail if any required job failed'
MESSAGE = b'No focused plan for this exact source. Submit an independently reviewed scope manifest.'
GREEN = ('success', 'skipped', 'neutral')
RECEIPTS = 'focused-admission-receipts'
QUEUE_STATE = 'queue.json'
MAX_QUEUE_BYTES = 8 * 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_RECEIPTS = 4096
QUEUE_SHA = re.compile(r'[0-9a-f]{7,40}\Z')


def require(condition: object, message: str) -> None:
    if not condition:
        raise a.AdmissionError(f'repair-main: {message}')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def positive(value: object) -> bool:
    return type(value) is int and value > 0


# ----------------------------------------------------------------------------- 1. the failed main run
def failed_run(rt: Any, main: str) -> dict[str, Any]:
    """main's latest push run: attempt 1, failed only because the resolver found no plan for main's tree."""
    root = f'repos/{rt.repo}'
    runs = rt.api(f'{root}/actions/workflows/ci.yml/runs?branch=main&event=push&per_page=1').get('workflow_runs')
    require(isinstance(runs, list) and len(runs) == 1, "main's latest push run is unavailable")
    run = runs[0]
    require(positive(run.get('id')) and run.get('head_sha') == main and run.get('path') == CI
            and run.get('event') == 'push' and run.get('head_branch') == 'main',
            "main's latest push run of ci.yml is not a run of the current main")
    require(run.get('status') == 'completed' and run.get('conclusion') == 'failure',
            f"main's latest push run {run.get('id')} did not fail")
    require(run.get('run_attempt') == 1, f"main's run {run['id']} was already rerun: this route repairs attempt 1 only")
    jobs = rt.pages(f"{root}/actions/runs/{run['id']}/attempts/1/jobs", 'jobs')
    require(jobs and all(job.get('status') == 'completed' for job in jobs), f"run {run['id']} job list is incomplete")
    failed = {job['name']: job for job in jobs if job.get('conclusion') not in GREEN}
    require(set(failed) == {JOB, AGGREGATE} and len([job for job in jobs if job.get('conclusion') not in GREEN]) == 2,
            f"run {run['id']} failed in {sorted(failed)}, not only in '{JOB}' and '{AGGREGATE}'")
    for name, step in ((JOB, STEP), (AGGREGATE, AGGREGATE_STEP)):
        job = failed[name]
        require(positive(job.get('id')) and job.get('run_id') == run['id'] and job.get('run_attempt') == 1
                and job.get('head_sha') == main, f"job '{name}' is not a job of run {run['id']}")
        steps = [row.get('name') for row in job.get('steps') or [] if row.get('conclusion') not in GREEN]
        require(steps == [step], f"job '{name}' failed in steps {steps}, not only in '{step}'")
    log = rt.run('gh', 'api', '--allow-escape-sequences', f"{root}/actions/jobs/{failed[JOB]['id']}/logs", timeout=60).stdout
    require(isinstance(log, bytes) and len(log) <= MAX_LOG_BYTES and MESSAGE in log,
            f"the '{STEP}' log does not report a missing focused plan")
    return dict(run=run['id'], attempt=1, jobs={JOB: failed[JOB]['id'], AGGREGATE: failed[AGGREGATE]['id']},
                log_sha256=digest(log))


# ----------------------------------------------------------------------------- 2. the queue merge and its admission
def merged_pull(rt: Any, main: str, head: str) -> dict[str, Any]:
    """main is the single-parent squash of exactly one merged PR whose head is `head`."""
    root = f'repos/{rt.repo}'
    commit = rt.api(f'{root}/commits/{main}')
    parents = [row.get('sha') for row in commit.get('parents') or []]
    require(commit.get('sha') == main and len(parents) == 1, f'main {main[:8]} is not a single-parent squash commit')
    listed = json.loads(rt.run('gh', 'api', f'{root}/commits/{main}/pulls').stdout)
    require(isinstance(listed, list), 'the pull requests of main are unavailable')
    numbers = [row.get('number') for row in listed if isinstance(row, dict) and row.get('merge_commit_sha') == main]
    require(len(numbers) == 1 and positive(numbers[0]), f'main {main[:8]} is not the merge of exactly one pull request')
    pull = rt.api(f'{root}/pulls/{numbers[0]}')
    require(pull.get('number') == numbers[0] and pull.get('merged') is True and pull.get('merge_commit_sha') == main
            and pull.get('state') == 'closed' and pull.get('base', {}).get('ref') == 'main'
            and pull.get('base', {}).get('repo', {}).get('full_name') == rt.repo
            and pull.get('head', {}).get('sha') == head,
            f'PR #{numbers[0]} is not the merged PR of head {head[:8]} into main {main[:8]}')
    return dict(pr=pull['number'], parent=parents[0], merged_at=pull.get('merged_at'))


def queue_merge(rt: Any, pr: int, main: str, head: str) -> dict[str, Any]:
    """The merge queue recorded this merge: its item of the PR is merged at main, with this head."""
    state = a._json_bytes(a._stable_file(rt.cache / QUEUE_STATE, MAX_QUEUE_BYTES), 'merge queue state')
    items = state.get('items')
    if not isinstance(items, list):
        raise a.AdmissionError('repair-main: merge queue state has no items')
    rows = [row for row in items if isinstance(row, dict) and str(row.get('pr')) == str(pr)
            and row.get('repo', rt.repo) == rt.repo]
    require(len(rows) == 1, f'the merge queue has no single item of PR #{pr}')
    row = rows[0]
    merge_sha, reviewed = row.get('merge_sha'), row.get('reviewed_head') or ''
    require(row.get('status') == 'merged' and isinstance(merge_sha, str) and QUEUE_SHA.fullmatch(merge_sha)
            and main.startswith(merge_sha) and reviewed in ('', head),
            f'the merge queue did not record PR #{pr} as merged at {main[:8]} with head {head[:8]}')
    return dict(pr=pr, merge_sha=merge_sha, reviewed_head=reviewed)


def admission_receipts(rt: Any, head: str) -> list[str]:
    """Transaction digests of the written admissions of `head` (its original pair), from the local receipts."""
    root = rt.cache / RECEIPTS
    require(root.is_dir() and not root.is_symlink(), 'no admission receipt directory')
    paths = sorted(root.glob(f'head-{head}-*.json'))
    require(len(paths) <= MAX_RECEIPTS, 'too many admission receipts')
    found = []
    for path in paths:
        receipt = a._json_bytes(a._stable_file(path, a.MAX_PACKET_BYTES), 'admission receipt')
        if (receipt.get('head') == head and receipt.get('registry_changed') is True
                and not {'repair_main', 'resulting_main'} & receipt.keys()
                and isinstance(receipt.get('transaction_sha256'), str) and a.HEX.fullmatch(receipt['transaction_sha256'])):
            found.append(receipt['transaction_sha256'])
    require(found, f'no written admission of head {head[:8]} is on record')
    return sorted(found)


def live(rt: Any, packet: dict[str, Any], main: str) -> dict[str, Any]:
    """Everything this route reads from GitHub and the coordinator's records; equal at prepare and at every guard."""
    head = packet['manifest']['head']
    pull = merged_pull(rt, main, head)
    return dict(run=failed_run(rt, main), pull=pull, queue=queue_merge(rt, pull['pr'], main, head),
                admissions=admission_receipts(rt, head))


# ----------------------------------------------------------------------------- 3./4. the composition, from Git
def _git(repo: Path, *args: str) -> bytes:
    return a._run(['git', *args], cwd=repo)


def _ancestor(repo: Path, older: str, newer: str) -> bool:
    return _git(repo, 'merge-base', older, newer).decode().strip() == older


def _paths(repo: Path, old: str, new: str) -> set[str]:
    raw = _git(repo, 'diff-tree', '-r', '--name-only', '--no-renames', '-z', old, new)
    return {path for path in raw.decode().split('\0') if path}


def covered(path: str, plan: dict[str, Any]) -> bool:
    """A path is covered by the reviewed scope: a selected test, a declared production file, web source of a selected web lane."""
    selection = plan.get('selection') or {}
    if path in (plan.get('production') or {}):
        return True
    if any(path in ids for ids in selection.values()):
        return True
    return path.startswith('web/src/') and 'web' in selection


def composition(repo: Path, packet: dict[str, Any], main: str, admitted: dict[str, Any]) -> dict[str, Any]:
    """Conditions 3 and 4 from Git objects and the admitted pair (read from the registry by the trusted codec)."""
    plan, head = packet['main_manifest'], packet['manifest']['head']
    line = _git(repo, 'rev-list', '--parents', '-n', '1', main).decode().split()
    require(len(line) == 2 and line[0] == main, f'main {main[:8]} is not a single-parent commit')
    parent = line[1]
    tree = _git(repo, 'rev-parse', f'{main}^{{tree}}').decode().strip()
    require(plan.get('base') == parent and plan.get('tree') == tree,
            f"the packet plans tree {str(plan.get('tree'))[:8]} on {str(plan.get('base'))[:8]}; main is tree {tree[:8]} on {parent[:8]}")
    pr_plan, old_main = admitted['pull_request'], admitted['push_tree']
    base = pr_plan.get('base')
    require(isinstance(pr_plan, dict) and pr_plan.get('head') == head and isinstance(base, str) and a.SHA.fullmatch(base),
            f'the admitted PR plan is not the plan of head {head[:8]}')
    require(base != parent and _ancestor(repo, base, parent),
            f'the admitted base {base[:8]} is not an earlier main below {parent[:8]}: this is not the batch plan trap')
    old_tree = _git(repo, 'merge-tree', '--write-tree', base, head).decode().splitlines()[0].strip()
    require(isinstance(old_main, dict) and old_main.get('base') == base and old_main.get('tree') == old_tree
            and pr_plan.get('tree') == old_tree, 'the admitted resulting-main plan is not the merge of the admitted pair')
    for key in ('selection', 'case_sha256', 'mode'):
        require(plan.get(key) == old_main.get(key) == pr_plan.get(key), f'the packet {key} differs from the admitted pair')
    changed = set(plan.get('changed') or {})
    require(changed and changed == set(old_main.get('changed') or {}),
            'the PR changes other paths on the real base than on the admitted base')
    between = _paths(repo, base, parent)
    overlap = sorted(changed & between)
    uncovered = [path for path in overlap if not covered(path, plan)]
    require(not uncovered, f'paths changed by the PR and by the intervening merges are not covered by the reviewed selection: {uncovered}')
    return dict(main=main, parent=parent, tree=tree, admitted_base=base, admitted_tree=old_tree, overlap=overlap)


# ----------------------------------------------------------------------------- the registry, through Core's codec
def _entry(source: Path, registry: dict[str, str], key: str) -> Any:
    """One decoded entry of the registry, through the reviewed witness of the resulting-main route (None when absent)."""
    try:
        return resulting._registry_witness(source, dict(before=registry, after=None, original_key=key, required={},
                                                         main_key=key, main={}))
    except a.AdmissionError:
        return None


def admitted_pair(source: Path, repo: Path, packet: dict[str, Any], registry: dict[str, str]) -> dict[str, Any]:
    """The admitted pair of the packet head, decoded by the trusted codec of current main."""
    head = packet['manifest']['head']
    pr_key = f'pull_request:{head}'
    pr_plan = _entry(source, registry, pr_key)
    if not (isinstance(pr_plan, dict) and isinstance(pr_plan.get('base'), str) and a.SHA.fullmatch(pr_plan['base'])):
        raise a.AdmissionError(f'repair-main: the registry holds no admitted plan of head {head[:8]}')
    old_tree = _git(repo, 'merge-tree', '--write-tree', pr_plan['base'], head).decode().splitlines()[0].strip()
    tree_key = f'push-tree:{old_tree}'
    old_main = _entry(source, registry, tree_key)
    require(isinstance(old_main, dict), f'the registry holds no admitted resulting-main plan {tree_key}')
    return {'pull_request': pr_plan, 'push_tree': old_main}


def append_proof(source: Path, packet: dict[str, Any], before: dict[str, str], after: dict[str, str]) -> None:
    """The proposed registry is the old one plus exactly the repair plan, every old entry byte-equal."""
    plan = packet['main_manifest']
    resulting.release_registry_proof(source, plan, before, after, {})


# ----------------------------------------------------------------------------- the proof stored in the transaction
def prove(rt: Any, packet: dict[str, Any], snapshot: Path, source: Path, main: str, registry: dict[str, str]) -> dict[str, Any]:
    """The complete repair proof of prepare: live evidence, the admitted pair and the composition."""
    plan = packet['main_manifest']
    require(a.registry_key(plan) == f"push-tree:{plan.get('tree')}", 'the repair plan is not one headless push-tree plan')
    evidence = live(rt, packet, main)
    admitted = admitted_pair(source, snapshot, packet, registry)
    shape = composition(snapshot, packet, main, admitted)
    require(evidence['pull']['parent'] == shape['parent'], "GitHub and Git disagree on main's parent")
    return dict(mode=MODE, main=main, base=shape['parent'], tree=shape['tree'], head=packet['manifest']['head'],
                pr=evidence['pull']['pr'], main_manifest_sha256=digest(a.canonical(plan)),
                admitted_sha256={key: digest(a.canonical(value)) for key, value in admitted.items()},
                composition=shape, live=evidence)


def guard(rt: Any, packet: dict[str, Any], proof: dict[str, Any]) -> None:
    """Freshness at every guard: main unchanged, every live fact unchanged."""
    require(proof.get('mode') == MODE, 'repair proof mode differs')
    current = rt.api(f'repos/{rt.repo}/git/ref/heads/main')['object']['sha']
    require(current == proof['main'], 'main changed')
    require(live(rt, packet, current) == proof['live'], 'the failed run, merge, queue record or admission changed')


def receipt_for(home: Path, run: int, main: str, failed: dict[str, int]) -> dict[str, Any] | None:
    """The written repair of exactly this attempt-1 failure, for the queue's main-rerun: {receipt_sha256, ...} or None."""
    root = statedir.root() / RECEIPTS
    if not root.is_dir() or root.is_symlink():
        return None
    for path in sorted(root.glob('head-*.json'))[:MAX_RECEIPTS]:
        try:
            data = a._stable_file(path, a.MAX_PACKET_BYTES)
            receipt = a._json_bytes(data, 'admission receipt')
        except a.AdmissionError:
            continue
        proof = receipt.get('repair_main')
        if (isinstance(proof, dict) and proof.get('mode') == MODE and proof.get('main') == main
                and receipt.get('registry_changed') is True
                and proof.get('live', {}).get('run', {}).get('run') == run
                and proof['live']['run'].get('jobs') == failed):
            return dict(receipt_sha256=digest(data), transaction_sha256=receipt.get('transaction_sha256'), pr=proof.get('pr'))
    return None
