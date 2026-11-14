"""Authenticate the unchanged PR behind one reviewed resulting-main manifest."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote

from . import focused_admit as a
from . import focused_update as git
from ..queue_lib import core_policy
from ..queue_lib.release_candidate import CandidateError
from steuermaschine.config import search_path

MODE = 'resulting-main-only/v1'
APP = 15368
JUDGE = '.github/workflows/merge-gate.yml'  # sm's own merge gate workflow in Core; the CI workflow is the policy's
STEP_COVERAGE = 'Verify the original workload and applicable coverage evidence'
STEP_REPORT = "Report both verdicts; a gate that didn't pass, or didn't run, fails closed"
STEP_SONAR = 'Quality gate passes, all ratings A, pull request and project'
SOURCE_FIELDS = ('head', 'tested_base', 'tested_tree', 'tested_commit', 'judge_source')
ORIGINAL_FIELDS = {*SOURCE_FIELDS, 'pr', 'pr_id', 'repository_id', 'branch', 'manifest_sha256', 'ci_run', 'judge_run'}


def require(condition, message):
    if not condition:
        raise a.AdmissionError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def positive(value):
    return type(value) is int and value > 0


def load_packet(path, review_root, pr):
    """Load a separately reviewed main-only envelope with frozen original provenance."""
    raw = a._stable_file(path, a.MAX_PACKET_BYTES)
    packet = a._json_bytes(raw, 'resulting-main packet')
    require(set(packet) == {'schema', 'mode', 'manifest', 'original', 'review'}
            and type(packet['schema']) is int and packet['schema'] == 1 and packet['mode'] == MODE,
            'invalid resulting-main-only packet')
    plan, original, review = (packet[key] for key in ('manifest', 'original', 'review'))
    require(isinstance(plan, dict) and plan.get('event') == 'push'
            and plan.get('ref') == 'refs/heads/main' and 'head' not in plan and 'commit' not in plan,
            'resulting-main admission requires one headless push-tree manifest')
    require(isinstance(original, dict) and set(original) == ORIGINAL_FIELDS
            and all(positive(original[k]) for k in ('pr', 'pr_id', 'repository_id', 'ci_run', 'judge_run'))
            and original['pr'] == pr, 'invalid original PR identity')
    for key in SOURCE_FIELDS:
        a._sha(original[key], key)
    a._sha(original['manifest_sha256'], 'original manifest digest', a.HEX)
    a._sha(plan.get('base'), 'resulting-main base')
    a._sha(plan.get('tree'), 'resulting-main tree')
    require(isinstance(review, dict) and set(review) == {
        'path', 'sha256', 'head', 'base', 'tree', 'manifest_sha256', 'original_sha256',
    } and all(review[key] == expected for key, expected in {
        'head': original['head'], 'base': plan['base'], 'tree': plan['tree'],
        'manifest_sha256': digest(a.canonical(plan)), 'original_sha256': digest(a.canonical(original)),
    }.items()), 'review differs from exact original evidence or resulting-main plan')
    relative = review['path']
    require(isinstance(relative, str) and relative and not Path(relative).is_absolute()
            and '..' not in Path(relative).parts, 'unsafe resulting-main review path')
    target = review_root / relative
    require(target.resolve(strict=True).is_relative_to(review_root.resolve(strict=True))
            and digest(a._stable_file(target, a.MAX_REVIEW_BYTES)) == review['sha256'],
            'resulting-main review artifact changed')
    return packet, raw


def _policy(rt, base):
    """Core's reviewed release policy at the protected main commit: the required contexts and the CI aggregate."""
    try:
        return core_policy.at_commit_api(lambda path: rt.api(f'repos/{rt.repo}/{path}'), base)
    except CandidateError as exc:
        raise a.AdmissionError(f'no reviewed release policy at main {base}: {exc}') from exc


def _rules(rt, base, policy):
    branch = rt.api(f'repos/{rt.repo}/branches/main')
    require(branch.get('name') == 'main' and branch.get('protected') is True
            and branch['commit']['sha'] == base, 'protected main identity changed')
    classic = branch['protection']
    require(classic.get('enabled') is False and classic['required_status_checks'] == {
        'enforcement_level': 'off', 'contexts': [], 'checks': [],
    }, 'active or unknown classic branch protection')
    rows = []
    for page in range(1, 11):
        path = f'repos/{rt.repo}/rules/branches/main?per_page=100&page={page}'
        batch = json.loads(rt.run('gh', 'api', path).stdout, object_pairs_hook=a._unique_pairs)
        require(isinstance(batch, list) and len(batch) <= 100, 'invalid effective branch rules')
        rows.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise a.AdmissionError('effective branch rules exceed pagination bound')
    encoded = [a.canonical(row) for row in rows]
    require(rows and len(set(encoded)) == len(rows), 'missing or duplicated effective branch rules')
    contexts, required = set(), set(policy.required_contexts)
    for row in rows:
        if row['type'] != 'required_status_checks':
            continue
        require(row.get('ruleset_source_type') == 'Repository' and row.get('ruleset_source') == rt.repo
                and positive(row.get('ruleset_id')), 'unknown effective status ruleset source')
        parameters = row['parameters']
        require(parameters.get('strict_required_status_checks_policy') is False,
                'effective required checks are strict or unknown')
        for check in parameters['required_status_checks']:
            name, integration = check['context'], check.get('integration_id')
            require(name in required and name not in contexts and (
                integration == APP or (name == policy.aggregate and integration is None)
            ), 'unexpected required context or integration')
            contexts.add(name)
    require(contexts == required, 'required context inventory changed')
    return digest(a.canonical(sorted(row.decode() for row in encoded)))


def _official(repo, rt, original):
    return (isinstance(repo, dict) and positive(repo.get('id'))
            and repo['id'] == original['repository_id'] and repo.get('full_name') == rt.repo
            and repo.get('fork') is False)


def _pull(rt, original):
    root = f'repos/{rt.repo}'
    require(_official(rt.api(root), rt, original), 'official repository identity changed')
    pull = rt.api(f"{root}/pulls/{original['pr']}")
    branch = original['branch']
    require(isinstance(branch, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', branch)
            and branch != 'main' and not branch.startswith(('release/', 'bump/')), 'invalid PR source branch')
    require(pull.get('number') == original['pr'] and pull.get('id') == original['pr_id']
            and pull.get('state') == 'open' and pull.get('draft') is False and pull.get('mergeable_state') == 'clean'
            and _official(pull['head']['repo'], rt, original) and _official(pull['base']['repo'], rt, original)
            and pull['head']['ref'] == branch and pull['head']['sha'] == original['head']
            and pull['base']['ref'] == 'main', 'official unchanged green PR identity changed')
    remote = rt.api(f'{root}/git/ref/heads/{quote(branch, safe="")}')
    require(remote.get('ref') == 'refs/heads/' + branch and remote['object']['type'] == 'commit'
            and remote['object']['sha'] == original['head'], 'official PR branch changed')
    return {'head': pull['head']['sha'], 'branch': branch, 'title': pull['title']}


def _checks(rt, original, policy):
    path = f"repos/{rt.repo}/commits/{original['head']}/check-runs?filter=latest"
    rows = rt.pages(path, 'check_runs')
    checks, required = {}, set(policy.required_contexts)
    for row in rows:
        name = row['name']
        if name not in required:
            continue
        require(name not in checks and positive(row.get('id')) and row['head_sha'] == original['head']
                and row['app']['id'] == APP and row['status'] == 'completed' and row['conclusion'] == 'success',
                'required check is ambiguous or not genuine green evidence')
        checks[name] = row['id']
        if name == policy.aggregate:
            require(row['details_url'] == (f"https://github.com/{rt.repo}/actions/runs/{original['ci_run']}"
                                          f"/job/{row['id']}"), 'CI OK is not from the original workload')
    require(set(checks) == required and len(set(checks.values())) == len(required), 'missing required check evidence')
    return checks


def _run(rt, original, *, judge, workflow='', attempt=False):
    root = f'repos/{rt.repo}'
    run_id = original['judge_run' if judge else 'ci_run']
    record = rt.api(f'{root}/actions/runs/{run_id}' + ('/attempts/1' if attempt else ''))
    branch = 'main' if judge else original['branch']
    head = original['judge_source' if judge else 'head']
    require(record['id'] == run_id and type(record['run_attempt']) is int and record['run_attempt'] == 1
            and record['status'] == 'completed' and record['conclusion'] == 'success'
            and record['event'] == ('workflow_run' if judge else 'pull_request')
            and record['path'] == (JUDGE if judge else workflow) and bool(judge or workflow) and record['head_branch'] == branch
            and record['head_sha'] == head and _official(record['repository'], rt, original)
            and _official(record['head_repository'], rt, original)
            and positive(record.get('workflow_id')) and positive(record.get('check_suite_id')),
            'original CI or protected judgment attempt identity changed')
    return {key: record[key] for key in ('id', 'head_sha', 'workflow_id', 'check_suite_id')}


def live_proof(rt, packet):
    """Recheck identity, rules, latest run, check results and immutable artifact metadata."""
    original, base = packet['original'], packet['manifest']['base']
    root = f'repos/{rt.repo}'
    require(rt.api(f'{root}/git/ref/heads/main')['object']['sha'] == base, 'protected main changed')
    pull = _pull(rt, original)
    policy = _policy(rt, base)
    rules = _rules(rt, base, policy)
    checks = _checks(rt, original, policy)
    ci = _run(rt, original, judge=False, workflow=policy.workflow)
    judge = _run(rt, original, judge=True)
    latest = rt.api(f"{root}/actions/workflows/{ci['workflow_id']}/runs?head_sha={original['head']}"
                    '&event=pull_request&per_page=1')['workflow_runs']
    require(len(latest) == 1 and latest[0]['id'] == ci['id'], 'a newer CI run superseded original evidence')
    artifacts = {}
    for row in rt.pages(f"{root}/actions/runs/{ci['id']}/artifacts", 'artifacts'):
        artifact = row['id']
        require(positive(artifact) and str(artifact) not in artifacts and row['expired'] is False
                and row['workflow_run']['id'] == ci['id'] and positive(row['size_in_bytes'])
                and isinstance(row.get('digest'), str) and re.fullmatch(r'sha256:[0-9a-f]{64}', row['digest']),
                'unavailable or ambiguous original artifact metadata')
        artifacts[str(artifact)] = {key: row[key] for key in ('name', 'size_in_bytes', 'digest')}
    require(artifacts and len(artifacts) < 100, 'invalid original artifact inventory')
    return dict(pull=pull, rules_sha256=rules, checks=checks, ci=ci, judge=judge, artifacts=artifacts)


def judgment_steps(rt, packet, live):
    """Bind the exact successful official judge attempt to the three evidence logs."""
    original, judge = packet['original'], live['judge']
    root = f'repos/{rt.repo}'
    ci = live['ci']
    aggregate = _policy(rt, packet['manifest']['base']).aggregate
    ci_jobs = rt.pages(f"{root}/actions/runs/{ci['id']}/attempts/1/jobs", 'jobs')
    ci_ok = [job for job in ci_jobs if job['name'] == aggregate]
    require(len(ci_ok) == 1 and ci_ok[0]['id'] == live['checks'][aggregate]
            and ci_ok[0]['run_id'] == ci['id'] and ci_ok[0]['run_attempt'] == 1
            and ci_ok[0]['head_sha'] == original['head'] and ci_ok[0]['status'] == 'completed'
            and ci_ok[0]['conclusion'] == 'success', 'current CI OK is not the original attempt job')
    workflow = rt.api(f'{root}/actions/workflows/merge-gate.yml')
    require(workflow['id'] == judge['workflow_id'] and workflow['path'] == JUDGE
            and _run(rt, original, judge=True, attempt=True) == judge,
            'canonical first-attempt protected workflow is unavailable')
    jobs = rt.pages(f"{root}/actions/runs/{judge['id']}/attempts/1/jobs", 'jobs')
    require(len(jobs) == 1, 'protected judge job inventory changed')
    job = jobs[0]
    require(positive(job.get('id')) and positive(job.get('runner_id')) and job['run_id'] == judge['id']
            and job['run_attempt'] == 1 and job['head_sha'] == judge['head_sha']
            and job['name'] == 'Merge gates' and job['status'] == 'completed' and job['conclusion'] == 'success',
            'original protected judge job did not succeed')
    check = rt.api(f"{root}/check-runs/{job['id']}")
    require(check['id'] == job['id'] and check['name'] == 'Merge gates' and check['head_sha'] == judge['head_sha']
            and check['app']['id'] == APP and check['check_suite']['id'] == judge['check_suite_id']
            and check['status'] == 'completed' and check['conclusion'] == 'success', 'judge job check identity changed')
    steps = {}
    numbers = set()
    for step in job['steps']:
        number, name = step['number'], step['name']
        require(positive(number) and number not in numbers and name not in steps, 'ambiguous protected judge steps')
        numbers.add(number)
        steps[name] = step
    checkout = [name for name in steps if re.fullmatch(r'Run actions/checkout@[0-9a-f]{40}', name)]
    require(len(checkout) == 1, 'protected judge checkout step unavailable')
    paths = {}
    for key, name in (('checkout', checkout[0]), ('coverage', STEP_COVERAGE), ('report', STEP_REPORT), ('sonar', STEP_SONAR)):
        step = steps.get(name, {})
        require(step.get('status') == 'completed' and step.get('conclusion') == 'success', 'required protected judge step did not succeed')
        paths[key] = f"actions/jobs/{job['id']}/steps/{step['number']}/logs"
    for name in ('merge-gate', 'coverage-floor'):
        check_id = live['checks'][name]
        published = rt.api(f'{root}/check-runs/{check_id}')
        require(published['id'] == check_id and published['name'] == name and published['head_sha'] == original['head']
                and published['app']['id'] == APP and published['status'] == 'completed'
                and published['conclusion'] == 'success', 'published protected check identity changed')
    return paths


REGISTRY_PROOF = r'''
import hashlib, json, os, sys, types
sys.path.insert(0, sys.argv[1])
pkg = types.ModuleType("scripts")
pkg.__path__ = [os.path.join(sys.argv[1], "scripts")]
pkg.__package__ = "scripts"
sys.modules["scripts"] = pkg
from scripts import focused_plan as core
request = json.load(sys.stdin)
def decode(values):
    return core.plan_registry(values["FOCUSED_CI_PLANS"], tuple(values[name] for name in core.SLOT_VARIABLES))
before = decode(request["before"])
for key, digest in request["required"].items():
    if key not in before or hashlib.sha256(core.registry_bytes(before[key])).hexdigest() != digest:
        raise ValueError("previously admitted manifest differs from its reviewed digest")
if request["after"] is not None:
    after = decode(request["after"])
    key = request["main_key"]
    if set(after) != set(before) | {key} or key in before:
        raise ValueError("resulting-main admission must add exactly one new key")
    if any(core.registry_bytes(after[name]) != core.registry_bytes(plan) for name, plan in before.items()):
        raise ValueError("an existing decoded manifest changed")
    if core.registry_bytes(after[key]) != core.registry_bytes(request["main"]):
        raise ValueError("resulting-main plan changed in the proposed slot")
original_key = request.get("original_key")
print(core.registry_bytes(before[original_key] if original_key else {"old_entries": len(before)}).decode())
'''


def _registry_witness(source, request):
    result = subprocess.run([sys.executable, '-I', '-c', REGISTRY_PROOF, str(source)],
                            input=a.canonical(request), capture_output=True, timeout=30,
                            env={'PATH': os.defpath, 'HOME': str(source)})
    require(result.returncode == 0, 'trusted registry decoder refused original scope or one-entry append')
    return a._json_bytes(result.stdout, 'trusted registry witness')


def registry_proof(source, packet, before, after=None):
    """Use the trusted codec to bind original scope and prove the exact one-entry append."""
    original, main = packet['original'], packet['manifest']
    key = 'pull_request:' + original['head']
    plan = _registry_witness(source, dict(before=before, after=after, original_key=key,
                             required={key: original['manifest_sha256']}, main_key=a.registry_key(main), main=main))
    require(plan['event'] == 'pull_request' and plan['head'] == original['head']
            and plan['base'] == original['tested_base'] and plan['tree'] == original['tested_tree']
            and digest(a.canonical(plan)) == original['manifest_sha256'], 'admitted original PR source changed')
    return plan


def release_registry_proof(source, main, before, after, required):
    """Read-only witness for one release push-tree and any required prior entry."""
    return _registry_witness(source, dict(before=before, after=after, required=required,
                             main_key=a.registry_key(main), main=main))['old_entries']


def replay_original(rt, packet, snapshot, original_plan, live):
    """Use the actual protected judge's archived reader to replay original artifacts."""
    original, base = packet['original'], packet['manifest']['base']
    git._official_origin(snapshot, rt.repo)
    git._plain_history(rt.core)
    git._plain_history(snapshot)
    for commit in (original['tested_base'], original['judge_source']):
        require(git._git(snapshot, 'cat-file', '-t', commit).stdout.strip() == b'commit'
                and git._git(snapshot, 'merge-base', '--is-ancestor', commit, base, check=False).returncode == 0,
                'original tested base or judge source is outside protected main history')
    paths = judgment_steps(rt, packet, live)
    source, _, repo, _, _ = a._canonical_source(snapshot, original['judge_source'])
    try:
        require(repo == rt.repo, 'historical protected reader repository changed')
        bindings = {path: git._git(snapshot, 'rev-parse', f"{original['judge_source']}:{path}").stdout.decode().strip()
                    for path in (JUDGE, 'scripts')}
        driver = source / 'resulting-main-replay.py'
        driver.write_bytes(Path(__file__).with_name('focused_replay.py').read_bytes())
        driver.with_suffix('.py.lock').write_bytes((source / 'scripts/focused_gate.py.lock').read_bytes())
        token = rt.run('gh', 'auth', 'token', '--hostname', 'github.com').stdout.decode().strip()
        require(token and '\n' not in token, 'GitHub evidence authentication unavailable')
        request = dict(token=token, repository=rt.repo, original=original, plan=original_plan, live=live, paths=paths)
        environment = {'PATH': search_path(), 'HOME': str(rt.home), 'GIT_NO_REPLACE_OBJECTS': '1',
                       'GIT_CONFIG_NOSYSTEM': '1', 'UV_PYTHON': '3.14.8'}
        result = subprocess.run(['uv', 'run', '--no-build', '--frozen', '--script', str(driver)],
                                cwd=snapshot, env=environment, input=a.canonical(request),
                                capture_output=True, timeout=240)
        require(result.returncode == 0, 'archived protected original judgment or workload replay refused')
        proof = a._json_bytes(result.stdout, 'original protected replay')
        require(proof.get('tested_commit') == original['tested_commit']
                and proof.get('artifacts') == live['artifacts'], 'replayed original source or artifacts changed')
        return dict(source=bindings, evidence=proof)
    finally:
        shutil.rmtree(source, ignore_errors=True)
