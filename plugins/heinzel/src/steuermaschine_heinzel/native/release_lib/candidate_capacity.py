"""Distinct composite evidence for reviewed, never-acquired hosted ARM jobs; never first-attempt proof."""

import base64
import hashlib
import io
import json
import re
import subprocess
import zipfile
from datetime import datetime

from ..queue_lib import hzlib
from ..queue_lib import release_candidate as strict
from . import candidate_policy as policy
from . import edge  # moved: records, processes and what used to be separate scripts

SCHEMA = 'candidate-capacity-audit/v1'
COMPOSITE = 'candidate-capacity-composite/v1'
LIMIT = 2 * 1024 * 1024
NOT_ACQUIRED = 'The job was not acquired by Runner of type hosted even after multiple attempts'
CAPACITY = 'Due to capacity constraints, jobs targeting macOS arm64 runners may experience longer queue times.'
CLOSURE = {'Build', 'CI OK'}
BUILD_STEPS = {
    'Run actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333',
    'Unpack Heinzel Notifier into the package', 'Check the wheel ships Heinzel Notifier',
}
REUSED = ('id', 'name', 'run_id', 'run_attempt', 'head_sha', 'status', 'conclusion', 'runner_id', 'runner_name',
          'started_at', 'completed_at', 'steps')
EXECUTION = tuple(key for key in REUSED if key not in ('id', 'run_attempt'))
RUN_KEYS = ('id', 'head_sha', 'head_branch', 'event', 'path', 'workflow_id', 'check_suite_id', 'created_at',
            'repository', 'head_repository')


class InvalidCapacity(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise InvalidCapacity(message)


def encoded(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    require(len(raw) <= LIMIT, 'capacity evidence exceeds bound')
    return raw


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def get(path):
    value = hzlib.ghj('api', f'repos/{hzlib.CORE}/{path}', default=None)
    require(value is not None, 'capacity API evidence unavailable')
    encoded(value)
    return value


def download(artifact_id):
    done = edge.run(['gh', 'api', f'repos/{hzlib.CORE}/actions/artifacts/{artifact_id}/zip'],
                          capture_output=True, check=False, timeout=30)
    require(done.returncode == 0 and len(done.stdout) <= 1024 * 1024, 'source artifact unavailable or oversized')
    return done.stdout


def paged(path, key):
    rows, total = [], None
    for page in range(1, 11):
        separator = '&' if '?' in path else '?'
        data = get(f'{path}{separator}per_page=100&page={page}')
        require(isinstance(data, dict) and type(data.get('total_count')) is int and isinstance(data.get(key), list),
                'malformed capacity inventory')
        total = data['total_count'] if total is None else total
        require(data['total_count'] == total and 0 < total <= 1000, 'capacity inventory total changed')
        rows.extend(data[key])
        require(len(rows) <= total, 'extra capacity inventory rows')
        if len(rows) == total:
            require(all(isinstance(row, dict) and type(row.get('id')) is int and row['id'] > 0 for row in rows),
                    'invalid capacity inventory IDs')
            require(len({row['id'] for row in rows}) == total, 'duplicate capacity inventory IDs')
            encoded(rows)
            return rows
        require(data[key], 'incomplete capacity inventory')
    raise InvalidCapacity('capacity inventory pagination exceeded bound')


def annotations(job):
    require(job.get('check_run_url') == f'https://api.github.com/repos/{hzlib.CORE}/check-runs/{job["id"]}',
            'annotations are not bound to the official same job')
    rows = []
    for page in range(1, 11):
        part = get(f'check-runs/{job["id"]}/annotations?per_page=100&page={page}')
        require(isinstance(part, list) and len(part) <= 100, 'malformed annotation inventory')
        rows.extend(part)
        if len(part) < 100:
            encoded(rows)
            return rows
    raise InvalidCapacity('annotation inventory exceeds bound')


def source_evidence(run_id, attempt):
    run = get(f'actions/runs/{run_id}/attempts/{attempt}')
    candidates = paged(f'actions/workflows/ci.yml/runs?head_sha={run.get("head_sha")}', 'workflow_runs')
    require(len(candidates) == 1 and candidates[0]['id'] == run_id, 'competing candidate runs on this head')
    require(run.get('status') == 'completed' and type(run.get('run_attempt')) is int
            and run['run_attempt'] == attempt, 'capacity attempt is not complete')
    jobs = paged(f'actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs')
    artifacts = paged(f'actions/runs/{run_id}/artifacts', 'artifacts')
    matches = [a for a in artifacts if a.get('name') == strict.ARTIFACT]
    require(len(matches) == 1, 'source artifact absent or duplicated')
    artifact = matches[0]
    require(artifact.get('expired') is False and artifact.get('workflow_run', {}).get('id') == run_id
            and artifact.get('workflow_run', {}).get('head_sha') == run.get('head_sha'), 'source artifact identity differs')
    raw = download(artifact['id'])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == [strict.PROOF_FILE] and archive.getinfo(strict.PROOF_FILE).file_size <= strict.MAX_PROOF,
                'invalid source proof archive')
        proof = json.loads(archive.read(strict.PROOF_FILE), object_pairs_hook=strict.unique_object)
    require(isinstance(proof, dict), 'invalid source proof')
    pr_number = proof.get('pr')
    require(type(pr_number) is int and pr_number > 0, 'capacity route requires canonical PR evidence')
    source = proof.get('source_commit')
    require(strict.valid_sha(source), 'invalid tested source identity')
    blobs = {path: base64.b64encode(strict.source_bytes(get, path, source)).decode() for path in policy.APPROVED_SOURCES}
    result = {'run': run, 'jobs': jobs, 'artifact': artifact, 'artifact_zip': base64.b64encode(raw).decode(),
              'proof': proof, 'pr': get(f'pulls/{pr_number}'), 'head': get(f'commits/{proof.get("head")}'),
              'source': get(f'commits/{source}'), 'blobs': blobs}
    require(get(f'actions/runs/{run_id}/attempts/{attempt}') == run, 'capacity attempt changed during collection')
    encoded(result)
    return result


def identity(data, version, run_id, attempt):
    """Independently authenticate the unmodified source protocol, without invoking strict.validate/verify."""
    run, proof, pr, head, source = (data[key] for key in ('run', 'proof', 'pr', 'head', 'source'))
    require(type(run.get('id')) is int and run['id'] == run_id and type(run.get('run_attempt')) is int
            and run['run_attempt'] == attempt and run.get('status') == 'completed', 'wrong capacity run/attempt')
    require(run.get('repository', {}).get('full_name') == strict.REPOSITORY
            and run.get('head_repository', {}).get('full_name') == strict.REPOSITORY
            and run.get('path') == strict.WORKFLOW and run.get('event') == 'pull_request', 'wrong repository/workflow/event')
    require(all(strict.valid_sha(proof.get(k)) for k in ('head', 'tree', 'base', 'source_commit')), 'invalid proof identities')
    require(pr.get('state') == 'open' and pr.get('draft') is False and type(pr.get('number')) is int
            and proof.get('pr') == pr['number'] and pr.get('head', {}).get('sha') == proof['head']
            and pr.get('base', {}).get('sha') == proof['base'], 'release PR/base changed')
    require(strict.release_version('pull_request', strict.REPOSITORY, '', {'pull_request': pr}) == version
            and run.get('head_branch') == f'release/v{version}' and run.get('head_sha') == proof['head'],
            'noncanonical release identity')
    require(type(proof.get('schema')) is int and proof['schema'] == 1 and proof.get('repository') == strict.REPOSITORY
            and proof.get('event') == 'pull_request' and proof.get('version') == version
            and type(proof.get('run_id')) is int and proof['run_id'] == run_id
            and type(proof.get('run_attempt')) is int and proof['run_attempt'] == 1
            and proof.get('sources') == policy.APPROVED_SOURCES, 'source proof protocol differs')
    require(head.get('sha') == proof['head'] and head.get('commit', {}).get('tree', {}).get('sha') == proof['tree']
            and source.get('sha') == proof['source_commit'] and source.get('commit', {}).get('tree', {}).get('sha') == proof['tree']
            and [p.get('sha') for p in source.get('parents', [])] == [proof['base'], proof['head']], 'source tree/parents differ')
    require(set(data['blobs']) == set(policy.APPROVED_SOURCES), 'source fingerprint inventory differs')
    for path, pin in policy.APPROVED_SOURCES.items():
        require(hashlib.sha256(base64.b64decode(data['blobs'][path], validate=True)).hexdigest() == pin,
                'executed source differs from reviewed pins')
    return proof


def job_shape(job, run_id, head):
    require(type(job.get('id')) is int and job['id'] > 0 and job.get('run_id') == run_id
            and job.get('head_sha') == head and job.get('status') == 'completed', 'malformed or incomplete candidate job')
    require(type(job.get('run_attempt')) is int and job['run_attempt'] in (1, 2), 'unexpected candidate job attempt')


def executed(job):
    require(type(job.get('runner_id')) is int and job['runner_id'] > 0
            and isinstance(job.get('runner_name'), str) and bool(job['runner_name'])
            and isinstance(job.get('steps'), list) and bool(job['steps'])
            and isinstance(job.get('started_at'), str) and isinstance(job.get('completed_at'), str),
            'candidate workload never executed')
    require(clock(job['completed_at']) >= clock(job['started_at']), 'job clocks reversed')
    for step in job['steps']:
        require(isinstance(step, dict) and step.get('status') == 'completed'
                and step.get('conclusion') in ('success', 'skipped'), 'unsuccessful executed step')


def replacement_steps(job):
    required = {'Fail if any required job failed'} if job['name'] == 'CI OK' else set()
    if job['name'] == 'Build':
        required = BUILD_STEPS | {'Run make build', 'Run actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'}
    elif job['name'] == 'Heinzel Notifier (macOS app)':
        required = {'Run make notifier', 'Real launches leave no process behind (ADR 0043)',
                    'Pack the app, keeping its executable bit and signature',
                    'Run actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'}
    elif job['name'].startswith('Test (macos-latest, Python '):
        required = {'Unit and integration tests (unit first, 4 workers, stop at the first failure)',
                    'Install Python 3.15 dependencies with the verified PyYAML compatibility build'
                    if 'Python 3.15,' in job['name'] else 'Install wheel dependencies'}
        if job['name'].endswith(', unit)'):
            required.add('Select the locked wheel Node without executing it')
    require(required and all(len([s for s in job['steps'] if s.get('name') == name
                                  and s.get('conclusion') == 'success']) == 1 for name in required),
            'replacement lacks an actual required test/setup/payload step')


def never_acquired(job, rows, head):
    require(job.get('conclusion') == 'cancelled' and type(job.get('runner_id')) is int and job['runner_id'] == 0
            and job.get('runner_name') == '' and job.get('steps') == [] and job.get('labels') == ['macos-latest'],
            'workload was not a never-acquired hosted Mac ARM cell')
    require(rows and all(isinstance(a, dict) and a.get('path') == '.github'
            and a.get('blob_href') == f'https://github.com/{hzlib.CORE}/blob/{head}/.github'
            and (a.get('annotation_level'), a.get('message')) in (('failure', NOT_ACQUIRED), ('notice', CAPACITY))
            for a in rows), 'unknown capacity annotation')
    require(any(a['message'] == NOT_ACQUIRED for a in rows) and any(a['message'] == CAPACITY for a in rows),
            'specific not-acquired and hosted ARM capacity evidence required')


def original(data, version, run_id):
    proof = identity(data, version, run_id, 1)
    require(data['run'].get('conclusion') in ('failure', 'cancelled'), 'original attempt was not a terminal failure')
    inventory = strict.expected_jobs('pull_request')
    jobs = data['jobs']
    require(len(jobs) == len(inventory) and {j.get('name') for j in jobs} == set(inventory)
            and len({j.get('id') for j in jobs}) == len(jobs), 'original full candidate inventory differs')
    eligible = []
    for job in jobs:
        job_shape(job, run_id, proof['head'])
        require(job['run_attempt'] == 1, 'original inventory contains a rerun')
        name = job['name']
        if name == 'CI OK':
            require(job['conclusion'] == 'failure' and type(job.get('runner_id')) is int and job['runner_id'] > 0
                    and job.get('steps') and {s['name'] for s in job['steps'] if s.get('conclusion') == 'failure'}
                    == {'Fail if any required job failed'}, 'original aggregate must retain only required-job failure')
        elif inventory[name] == 'skipped':
            require(job['conclusion'] == 'skipped' and job['steps'] == [], 'unexpected skipped control execution')
        elif job['conclusion'] == 'success':
            executed(job)
        else:
            require(name == 'Heinzel Notifier (macOS app)' or re.fullmatch(r'Test \(macos-latest, Python 3\.(12|13|14|15), (unit|integration)\)', name),
                    'non-Mac workload failed')
            never_acquired(job, data['annotations'].get(str(job['id']), []), proof['head'])
            eligible.append(job['id'])
    require(eligible, 'no missing execution to reacquire')
    require(set(data['annotations']) == {str(i) for i in eligible}, 'extra or missing annotated cells')
    build = next(j for j in jobs if j['name'] == 'Build')
    require(build['conclusion'] == 'success', 'original Build state is not reviewed closure')
    if any(j['name'] == 'Heinzel Notifier (macOS app)' and j['id'] in eligible for j in jobs):
        require({s['name'] for s in build['steps'] if s['conclusion'] == 'skipped'} == BUILD_STEPS,
                'original Build missing-notifier closure differs')
    return sorted(eligible)


def collect_original(version, run_id):
    current = get(f'actions/runs/{run_id}')
    require(current.get('status') == 'completed' and type(current.get('run_attempt')) is int
            and current['run_attempt'] == 1, 'original run must finish before eligibility')
    data = source_evidence(run_id, 1)
    data['annotations'] = {str(j['id']): annotations(j) for j in data['jobs'] if j.get('conclusion') == 'cancelled'}
    original(data, version, run_id)
    require(get(f'actions/runs/{run_id}') == current, 'run changed during original audit')
    return data


def dependency_closure(data, eligible):
    """Parse the authenticated, reviewed simple needs syntax; unknown syntax never authorizes replay."""
    text = base64.b64decode(data['blobs'][strict.WORKFLOW], validate=True).decode()
    jobs, current = {}, None
    for line in text.splitlines():
        match = re.fullmatch(r'  ([a-z][a-z0-9-]*):', line)
        if match:
            current = match[1]
            require(current not in jobs, 'duplicate workflow job')
            jobs[current] = []
        elif current and line.startswith('    needs:'):
            require(not jobs[current], 'duplicate workflow needs')
            value = line.split(':', 1)[1].split(' #', 1)[0].strip()
            require(re.fullmatch(r'(?:[a-z][a-z0-9-]*|\[[a-z0-9, -]*\])', value), 'unreviewed workflow needs syntax')
            jobs[current] = [item.strip() for item in value.strip('[]').split(',') if item.strip()]
    require(jobs and all(n in jobs for row in jobs.values() for n in row), 'incomplete workflow dependency graph')
    names = {j['id']: j['name'] for j in data['jobs']}
    selected = {'notifier' if names[j] == 'Heinzel Notifier (macOS app)' else 'test' for j in eligible}
    found = set(selected)
    while True:
        following = found | {job for job, needs in jobs.items() if set(needs) & found}
        if following == found:
            break
        found = following
    closure = found - selected
    expected = {'ci-ok', 'build'} if 'notifier' in selected else {'ci-ok'}
    require(closure == expected, 'failed-job request would replay an unapproved descendant workload')
    return {'Build', 'CI OK'} if 'build' in closure else {'CI OK'}


def stable_job(job):
    return {key: job.get(key) for key in REUSED}


SAFE_REFUSALS = frozenset({
    'original source or annotation evidence changed', 'original PR identity or unapproved metadata changed',
    'skipped control acquired execution',
    'original PR mergeability transition is not reviewed', 'original run identity changed',
    'original executed jobs/results/timestamps changed', 'reused original execution changed',
    'reused execution is after intent or replacement execution predates intent',
    'partial or extra attempt-two replacement inventory', 'partial reused inventory',
    'composite requires exactly completed attempt two', 'original source proof was replaced',
    'rerun identity changed', 'attempt creation clocks differ from request intent',
    'replacement lacks an actual required test/setup/payload step', 'capacity audit fingerprint differs',
    'release receipt or hold changed during verification', 'capacity evidence exceeds bound',
})


def safe_refusal(error):
    message = str(error)
    return message if message in SAFE_REFUSALS else 'capacity evidence failed validation'


def same_pr_identity(saved, fresh):
    # The only observed mutable field changed when failed CI became green. Everything else stays byte-for-value exact.
    key = 'mergeable_state'
    require(isinstance(saved, dict) and isinstance(fresh, dict)
            and (key in saved) == (key in fresh)
            and encoded({k: v for k, v in saved.items() if k != key}) == encoded({k: v for k, v in fresh.items() if k != key}),
            'original PR identity or unapproved metadata changed')
    require(saved.get(key) == fresh.get(key) or (saved.get(key), fresh.get(key)) == ('blocked', 'clean'),
            'original PR mergeability transition is not reviewed')


def unchanged_original(saved, fresh):
    same_pr_identity(saved['pr'], fresh['pr'])
    require(saved['proof'] == fresh['proof'] and saved['head'] == fresh['head']
            and saved['source'] == fresh['source'] and saved['artifact'] == fresh['artifact']
            and saved['artifact_zip'] == fresh['artifact_zip'] and saved['blobs'] == fresh['blobs']
            and saved['annotations'] == fresh['annotations'], 'original source or annotation evidence changed')
    require({k: saved['run'].get(k) for k in RUN_KEYS} == {k: fresh['run'].get(k) for k in RUN_KEYS}
            and saved['run'].get('conclusion') == fresh['run'].get('conclusion'), 'original run identity changed')
    require({j['id']: stable_job(j) for j in saved['jobs']} == {j['id']: stable_job(j) for j in fresh['jobs']},
            'original executed jobs/results/timestamps changed')


def validate_audit(audit, version, token, run_id):
    require(isinstance(audit, dict) and audit.get('schema') == SCHEMA and audit.get('version') == version
            and audit.get('freeze_token') == token and type(audit.get('run_id')) is int and audit['run_id'] == run_id,
            'unknown audit schema or ownership identity')
    require(audit.get('original_sha256') == digest(audit['original']), 'original audit digest differs')
    eligible = original(audit['original'], version, run_id)
    require(audit.get('eligible_ids') == eligible and audit.get('closure') == sorted(dependency_closure(audit['original'], eligible)),
            'audited replay inventory differs')
    require(audit.get('audit_sha256') == digest({k: v for k, v in audit.items() if k != 'audit_sha256'}),
            'capacity audit fingerprint differs')
    return eligible


def execution_tuple(job):
    return {key: job.get(key) for key in EXECUTION}


def clock(value):
    require(isinstance(value, str), 'missing execution timestamp')
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, 'execution timestamp must include timezone')
    return result


def execution_boundary(job, intent, reused):
    start, finish = clock(job.get('started_at')), clock(job.get('completed_at'))
    require(start <= finish, 'execution job clocks reversed')
    timestamps = [start, finish]
    for step in job['steps']:
        left, right = step.get('started_at'), step.get('completed_at')
        if step.get('conclusion') == 'success':
            require(left is not None and right is not None, 'successful step lacks execution clocks')
        if left is not None and right is not None:
            require(clock(left) <= clock(right), 'execution step clocks reversed')
        timestamps.extend(clock(value) for value in (left, right) if value is not None)
    require(all(value <= intent if reused else value >= intent for value in timestamps),
            'reused execution is after intent or replacement execution predates intent')


def skipped_carrier(job, original):
    pair = (job.get('runner_id'), job.get('runner_name'))
    empty = pair == (None, None) or (type(pair[0]) is int and pair == (0, ''))
    require(all(key in job and key in original for key in ('runner_id', 'runner_name'))
            and job.get('conclusion') == original.get('conclusion') == 'skipped'
            and empty and pair == (original['runner_id'], original['runner_name'])
            and job.get('steps') == original.get('steps') == [], 'skipped control acquired execution')


def verify_composite(audit, version, token, run_id, request_at):
    eligible = validate_audit(audit, version, token, run_id)
    intent = clock(request_at)
    latest = get(f'actions/runs/{run_id}')
    require(latest.get('status') == 'completed' and latest.get('conclusion') == 'success'
            and type(latest.get('run_attempt')) is int and latest['run_attempt'] == 2, 'composite requires exactly completed attempt two')
    first = source_evidence(run_id, 1)
    first['annotations'] = {str(j['id']): annotations(j) for j in first['jobs'] if j.get('conclusion') == 'cancelled'}
    require(original(first, version, run_id) == eligible, 'original eligibility changed')
    unchanged_original(audit['original'], first)
    second = source_evidence(run_id, 2)
    proof = identity(second, version, run_id, 2)
    require(second['run'].get('conclusion') == 'success', 'second attempt did not succeed')
    stable_keys = tuple(key for key in RUN_KEYS if key != 'created_at')
    require({k: first['run'].get(k) for k in stable_keys} == {k: second['run'].get(k) for k in stable_keys}, 'rerun identity changed')
    require(clock(first['run'].get('created_at')) <= intent <= clock(second['run'].get('created_at')),
            'attempt creation clocks differ from request intent')
    require(second['proof'] == first['proof'] and second['artifact'] == first['artifact']
            and second['artifact_zip'] == first['artifact_zip'], 'original source proof was replaced')
    inventory = strict.expected_jobs('pull_request')
    before = {j['name']: j for j in first['jobs']}
    requested = {j['name'] for j in first['jobs'] if j['id'] in eligible} | set(audit['closure'])
    rows = second['jobs']
    require(len({j['name'] for j in rows}) == len(rows), 'duplicate attempt-two job names')
    require(set(j['name'] for j in rows) <= set(inventory), 'unknown attempt-two workload')
    require(len({j['id'] for j in rows}) == len(rows), 'duplicate carrier job IDs')
    original_ids = {j['id'] for j in first['jobs']}
    replacements, reused = {}, {}
    for job in rows:
        job_shape(job, run_id, proof['head'])
        name = job['name']
        if name not in requested:
            if job['run_attempt'] == 1:
                require(stable_job(job) == stable_job(before[name]), 'changed original reused record')
            else:
                require(job['id'] not in original_ids, 'foreign or repeated carrier identity')
                if inventory[name] == 'skipped':
                    skipped_carrier(job, before[name])
                else:
                    require(execution_tuple(job) == execution_tuple(before[name]), 'reused original execution changed')
            if inventory[name] == 'success':
                execution_boundary(before[name], intent, reused=True)
            reused[name] = job
        else:
            require(job['run_attempt'] == 2 and job['id'] not in original_ids
                    and job.get('conclusion') == 'success', 'unapproved/repeated/failed candidate reexecution')
            executed(job)
            replacement_steps(job)
            execution_boundary(job, intent, reused=False)
            replacements[name] = job
    require(set(replacements) == requested, 'partial or extra attempt-two replacement inventory')
    # Complete carrier inventories and execution-only attempt inventories are both official representations.
    # A partial carrier inventory never authorizes silently missing cells.
    require(not reused or set(reused) == set(inventory) - requested, 'partial reused inventory')
    for name, job in before.items():
        actual = get(f'actions/jobs/{job["id"]}')
        require(stable_job(actual) == stable_job(job), 'original successful execution was replayed or changed')
    if 'Build' in requested:
        steps = replacements['Build']['steps']
        require(all(len([s for s in steps if s.get('name') == name and s.get('conclusion') == 'success']) == 1
                    for name in BUILD_STEPS), 'recomposed Build lacks actual notifier download/inclusion checks')
    require(get(f'actions/runs/{run_id}') == latest, 'run changed during composite verification')
    cells = []
    for name, expected in inventory.items():
        job = replacements.get(name, before[name])
        require(job.get('conclusion') == expected, 'combined candidate gate not successful')
        carrier = reused.get(name, job)
        cells.append({'name': name, 'original_job_id': before[name]['id'], 'carrier_job_id': carrier['id'],
                      'carrier_attempt': carrier['run_attempt'],
                      'execution_attempt': 2 if name in replacements else 1 if expected == 'success' else None,
                      'origin': 'recomposed-non-test' if name in audit['closure'] else 'reacquired' if name in replacements
                      else 'skipped-control/new-check-record' if expected == 'skipped' and carrier['run_attempt'] == 2
                      else 'reused-original-execution/new-check-record' if carrier['run_attempt'] == 2 else 'original',
                      'started_at': job.get('started_at'), 'completed_at': job.get('completed_at'), 'conclusion': job['conclusion']})
    return {'schema': COMPOSITE, 'run_id': run_id, 'attempts': [1, 2], 'first_attempt_clean': False,
            'streak_eligible': False, 'descendant_progress_eligible': False, 'original_conclusion': first['run']['conclusion'],
            'audit_sha256': audit['audit_sha256'], 'head': proof['head'], 'tree': proof['tree'], 'base': proof['base'],
            'source_commit': proof['source_commit'], 'cells': cells, 'original_sha256': audit['original_sha256'],
            'replacement_evidence': second, 'replacement_sha256': digest(second)}


def persist(rel, **fields):
    encoded(rel.state() | fields)
    rel.remember(**fields)


def ownership(rel, version, receipt_sha, token):
    raw = rel.paths()[1].read_bytes()
    require(len(raw) <= LIMIT and hashlib.sha256(raw).hexdigest() == receipt_sha, 'release receipt CAS changed')
    data = rel.state()
    require(data.get('version') == version and data.get('freeze_token') == token and not data.get('finished')
            and rel.owned_hold_exists(version), 'exact unfinished owned release hold required')
    require(not data.get('snapshot') and not data.get('acceptance_identity') and not data.get('candidate_ci')
            and not data.get('candidate_composite'),
            'capacity route requires pre-candidate release state')
    return data


def action(rel, args):
    """The supported coordinator holds its existing lock; prepare/request never merge or publish."""
    data = ownership(rel, args.version, args.expected_receipt_sha256, args.expected_freeze_token)
    run_id = args.candidate_run
    require(type(run_id) is int and run_id > 0, 'exact candidate run required')
    pretag = rel.pretag_identity(args.version)
    require(pretag.get('draft') is False, 'candidate capacity requires ready pre-publication PR')
    current = data.get('candidate_reacquisition')
    if args.candidate_capacity == 'prepare':
        require(current is None, 'immutable capacity audit already exists')
        require(args.expected_audit_sha256 is None, 'prepare does not consume an audit fingerprint')
        original_data = collect_original(args.version, run_id)
        eligible = original(original_data, args.version, run_id)
        require(pretag['pr'] == original_data['proof']['pr'] and pretag['head'] == original_data['proof']['head'], 'owned release PR differs from audit')
        audit = {'schema': SCHEMA, 'version': args.version, 'freeze_token': args.expected_freeze_token,
                 'run_id': run_id, 'original': original_data, 'original_sha256': digest(original_data),
                 'eligible_ids': eligible, 'closure': sorted(dependency_closure(original_data, eligible))}
        audit['audit_sha256'] = digest(audit)
        # Recheck CAS and owned hold after all remote reads, immediately before the atomic receipt update.
        ownership(rel, args.version, args.expected_receipt_sha256, args.expected_freeze_token)
        require(rel.pretag_identity(args.version) == pretag, 'pre-publication release identity changed')
        persist(rel, candidate_reacquisition={'audit': audit, 'request': None})
        hzlib.say(f'PREPARED capacity audit {audit["audit_sha256"]}; no workflow request or release action performed')
        return 0
    require(isinstance(current, dict) and current.get('request') is None, 'capacity request already intended; reconcile, never retry automatically')
    audit = current['audit']
    validate_audit(audit, args.version, args.expected_freeze_token, run_id)
    require(args.expected_audit_sha256 == audit['audit_sha256'], 'reviewed capacity audit fingerprint differs')
    require(pretag['pr'] == audit['original']['proof']['pr'] and pretag['head'] == audit['original']['proof']['head'], 'owned release PR changed')
    fresh = collect_original(args.version, run_id)
    unchanged_original(audit['original'], fresh)
    require(dependency_closure(fresh, audit['eligible_ids']) == set(audit['closure']), 'dependent execution closure changed')
    ownership(rel, args.version, args.expected_receipt_sha256, args.expected_freeze_token)
    require(rel.pretag_identity(args.version) == pretag, 'pre-publication release identity changed')
    intent = {'at': rel.now_iso(), 'operation': 'rerun-failed-jobs', 'run_id': run_id, 'expected_attempt': 2,
              'eligible_ids': audit['eligible_ids'], 'closure': audit['closure'], 'status': 'intent-recorded'}
    persist(rel, candidate_reacquisition={'audit': audit, 'request': intent})
    code, _ = hzlib.gh('api', '--method', 'POST', f'repos/{hzlib.CORE}/actions/runs/{run_id}/rerun-failed-jobs')
    require(code == 0, 'request outcome ambiguous or rejected; retained intent prohibits automatic retry')
    persist(rel, candidate_reacquisition={'audit': audit, 'request': intent | {'status': 'submitted'}})
    hzlib.say(f'REQUESTED failed-job reacquisition for {run_id}; no merge/publication; original failure retained')
    return 0


def consume(rel, run_id, pr_number):
    data = rel.state()
    record = data.get('candidate_reacquisition')
    require(isinstance(record, dict) and isinstance(record.get('request'), dict)
            and record['request'].get('status') in ('intent-recorded', 'submitted'), 'no explicit capacity request intent')
    require(data.get('version') and rel.owned_hold_exists(data['version']), 'release-owned hold changed')
    request = record['request']
    require(set(record) == {'audit', 'request'} and set(request) == {
        'at', 'operation', 'run_id', 'expected_attempt', 'eligible_ids', 'closure', 'status'}, 'unknown request protocol')
    require(request.get('run_id') == run_id and type(request.get('expected_attempt')) is int
            and request['expected_attempt'] == 2 and request.get('operation') == 'rerun-failed-jobs'
            and request.get('eligible_ids') == record['audit'].get('eligible_ids')
            and request.get('closure') == record['audit'].get('closure'), 'capacity request run/cell identity differs')
    require(isinstance(request.get('at'), str) and datetime.fromisoformat(request['at']).tzinfo is not None,
            'request intent timestamp missing')
    require(record['audit']['original']['proof']['pr'] == pr_number, 'composite PR differs from merge target')
    result = verify_composite(record['audit'], data['version'], data['freeze_token'], run_id, request['at'])
    require(rel.state() == data and rel.owned_hold_exists(data['version']), 'release receipt or hold changed during verification')
    return result
