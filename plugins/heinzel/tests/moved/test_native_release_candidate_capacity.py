"""Boundary tests use isolated state and synthetic official responses, never a live rerun or receipt."""

import base64
import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy_kit as kit  # noqa: E402
from steuermaschine_heinzel.native.release_lib import candidate_capacity as cap

HEAD, BASE, SOURCE, TREE = (c * 40 for c in 'abcd')
RUN, PR, VERSION, TOKEN = 99, 517, '0.9.17', 'e' * 32
TOOLS = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def reviewed_policy(monkeypatch):
    """Core's release policy at the release head (old CI shape), describing the fixture workflow and recorder."""
    monkeypatch.setattr(cap, 'reviewed', lambda head: kit.policy(commit=head))


def evidence():
    blobs = {kit.WORKFLOW: kit.workflow_bytes(), kit.RECORDER: kit.recorder_bytes()}
    proof = {'schema': 1, 'repository': cap.strict.REPOSITORY, 'event': 'pull_request', 'run_id': RUN,
             'run_attempt': 1, 'head': HEAD, 'base': BASE, 'source_commit': SOURCE, 'tree': TREE, 'version': VERSION,
             'pr': PR, 'sources': kit.manifest()['sources']}
    def step(name, conclusion='success'):
        return {'name': name, 'status': 'completed', 'conclusion': conclusion,
                'started_at': '2026-11-05T00:00:00Z', 'completed_at': '2026-11-05T00:00:10Z'}
    jobs = []
    for index, (name, conclusion) in enumerate(sorted(kit.expected_jobs('pull_request').items()), 1):
        jobs.append({'id': index, 'name': name, 'run_id': RUN, 'run_attempt': 1, 'head_sha': HEAD,
                     'status': 'completed', 'conclusion': conclusion, 'runner_id': index if conclusion == 'success' else 0,
                     'runner_name': 'Hosted Agent' if conclusion == 'success' else '', 'labels': ['macos-latest'],
                     'started_at': '2026-11-05T00:00:00Z', 'completed_at': '2026-11-05T00:00:10Z',
                     'steps': [step('test')] if conclusion == 'success' else [],
                     'check_run_url': f'https://api.github.com/repos/{cap.hzlib.CORE}/check-runs/{index}'})
    notes = {}
    for job in jobs:
        if job['name'] in {'Heinzel Notifier (macOS app)', 'Test (macos-latest, Python 3.13, unit)',
                           'Test (macos-latest, Python 3.15, unit)'}:
            job.update(conclusion='cancelled', runner_id=0, runner_name='', steps=[])
            notes[str(job['id'])] = [{'path': '.github', 'blob_href': f'https://github.com/{cap.hzlib.CORE}/blob/{HEAD}/.github',
                                     'annotation_level': level, 'message': message}
                                    for level, message in [('failure', cap.NOT_ACQUIRED), ('notice', cap.CAPACITY)]]
        if job['name'] == 'Build':
            job['steps'] += [step(name, 'skipped') for name in sorted(cap.BUILD_STEPS)]
        if job['name'] == 'CI OK':
            job.update(conclusion='failure', runner_id=999, runner_name='Hosted Agent',
                       steps=[step('Fail if any required job failed', 'failure')])
    return {'run': {'id': RUN, 'run_attempt': 1, 'status': 'completed', 'conclusion': 'failure',
                    'repository': {'full_name': cap.strict.REPOSITORY}, 'head_repository': {'full_name': cap.strict.REPOSITORY},
                    'path': cap.strict.WORKFLOW, 'event': 'pull_request', 'head_sha': HEAD,
                    'head_branch': f'release/v{VERSION}', 'created_at': '2026-11-05T00:00:00Z'},
            'proof': proof, 'jobs': jobs, 'annotations': notes,
            'pr': {'number': PR, 'state': 'open', 'draft': False, 'title': f'chore(release): v{VERSION}',
                   'head': {'ref': f'release/v{VERSION}', 'sha': HEAD, 'repo': {'full_name': cap.strict.REPOSITORY}},
                   'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': cap.strict.REPOSITORY}}},
            'head': {'sha': HEAD, 'commit': {'tree': {'sha': TREE}}},
            'source': {'sha': SOURCE, 'commit': {'tree': {'sha': TREE}}, 'parents': [{'sha': BASE}, {'sha': HEAD}]},
            'artifact': {'id': 81}, 'artifact_zip': 'original immutable archive',
            'blobs': {path: base64.b64encode(raw).decode() for path, raw in blobs.items()}}


def audit(data=None):
    data = evidence() if data is None else data
    eligible = cap.original(data, VERSION, RUN)
    value = {'schema': cap.SCHEMA, 'version': VERSION, 'freeze_token': TOKEN, 'run_id': RUN,
             'original': data, 'original_sha256': cap.digest(data), 'eligible_ids': eligible,
             'closure': sorted(cap.dependency_closure(data, eligible))}
    value['audit_sha256'] = cap.digest(value)
    return value


def second_attempt(first):
    second = copy.deepcopy(first)
    second['run'].update(run_attempt=2, conclusion='success', created_at='2026-11-05T01:00:00Z')
    for job in second['jobs']:
        if job['conclusion'] == 'cancelled' or job['name'] in cap.CLOSURE:
            job.update(id=job['id'] + 1000, run_attempt=2, conclusion='success', runner_id=123, runner_name='Hosted Agent',
                       started_at='2026-11-05T01:00:00Z', completed_at='2026-11-05T01:01:00Z')
            if job['name'] == 'Build':
                names = cap.BUILD_STEPS | {'Run make build', 'Run actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'}
            elif job['name'] == 'CI OK':
                names = {'Fail if any required job failed'}
            elif job['name'] == 'Heinzel Notifier (macOS app)':
                names = {'Run make notifier', 'Real launches leave no process behind (ADR 0043)',
                         'Pack the app, keeping its executable bit and signature',
                         'Run actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'}
            else:
                names = {'Unit and integration tests (unit first, 4 workers, stop at the first failure)',
                         'Select the locked wheel Node without executing it',
                         'Install Python 3.15 dependencies with the verified PyYAML compatibility build'
                         if 'Python 3.15,' in job['name'] else 'Install wheel dependencies'}
            job['steps'] = [{'name': name, 'status': 'completed', 'conclusion': 'success',
                             'started_at': '2026-11-05T01:00:00Z', 'completed_at': '2026-11-05T01:01:00Z'}
                            for name in sorted(names)]
    return second


def wire(monkeypatch, original=None):
    original = evidence() if original is None else original
    next_attempt = second_attempt(original)
    current = copy.deepcopy(next_attempt['run'])
    def get(path):
        if path == f'actions/runs/{RUN}':
            return copy.deepcopy(current)
        if path.startswith('actions/jobs/'):
            return copy.deepcopy(next(j for j in original['jobs'] if str(j['id']) == path.split('/')[-1]))
        raise AssertionError(path)
    monkeypatch.setattr(cap, 'get', get)
    monkeypatch.setattr(cap, 'source_evidence', lambda run, attempt: copy.deepcopy(original if attempt == 1 else next_attempt))
    monkeypatch.setattr(cap, 'annotations', lambda job: copy.deepcopy(original['annotations'][str(job['id'])]))
    return original, next_attempt, current


def test_distinct_complete_composite_keeps_every_original_execution_and_failure(monkeypatch):
    original, second, _ = wire(monkeypatch)
    result = cap.verify_composite(audit(original), VERSION, TOKEN, RUN, '2026-11-05T01:00:00Z')
    assert result['schema'] == cap.COMPOSITE and result['attempts'] == [1, 2]
    assert result['original_conclusion'] == 'failure'
    assert result['first_attempt_clean'] is result['streak_eligible'] is result['descendant_progress_eligible'] is False
    assert len(result['cells']) == 69
    assert len([c for c in result['cells'] if c['origin'] == 'reacquired']) == 3
    assert len([c for c in result['cells'] if c['origin'] == 'recomposed-non-test']) == 2
    assert original['run']['conclusion'] == 'failure' and original['proof']['run_attempt'] == 1
    # Official APIs may return only the actually executed replacement jobs, never a synthetic all-success attempt1.
    second['jobs'] = [j for j in second['jobs'] if j['run_attempt'] == 2]
    assert cap.verify_composite(audit(original), VERSION, TOKEN, RUN, '2026-11-05T01:00:00Z')['schema'] == cap.COMPOSITE


@pytest.mark.parametrize('field,value', [('runner_id', True), ('runner_id', 1), ('runner_name', 'runner'),
                                         ('steps', [{'name': 'started'}]), ('labels', ['self-hosted', 'macOS', 'ARM64']),
                                         ('conclusion', 'timed_out'), ('conclusion', 'failure'), ('run_attempt', 2)])
def test_any_execution_or_unexplained_failure_disqualifies_original(field, value):
    data = evidence()
    job = next(j for j in data['jobs'] if j['conclusion'] == 'cancelled')
    job[field] = value
    with pytest.raises(cap.InvalidCapacity):
        audit(data)


@pytest.mark.parametrize('defect', ['missing', 'generic', 'wrong-source', 'wrong-severity', 'extra'])
def test_annotation_requires_exact_official_both_causes_and_no_unknown_failure(defect):
    data = evidence()
    notes = next(iter(data['annotations'].values()))
    if defect == 'missing':
        notes.pop()
    elif defect == 'generic':
        notes[0]['message'] = 'capacity unavailable'
    elif defect == 'wrong-source':
        notes[0]['blob_href'] = 'https://foreign.invalid'
    elif defect == 'wrong-severity':
        notes[0]['annotation_level'] = 'notice'
    else:
        notes.append({'message': 'actual software failure'})
    with pytest.raises(cap.InvalidCapacity):
        audit(data)


@pytest.mark.parametrize('defect', ['attempt3', 'run-failure', 'replay-test', 'missing', 'duplicate', 'wrong-source',
                                   'wrong-base', 'wrong-repo', 'wrong-workflow', 'bad-proof', 'bad-pin',
                                   'failed-replacement', 'skipped-build-inclusion', 'changed-original-timestamp', 'partial-reused', 'skipped-notifier-native', 'wrong-test-dependencies'])
def test_composite_refuses_identity_replays_partial_and_incomplete_payload(monkeypatch, defect):
    first, second, current = wire(monkeypatch)
    frozen = audit(copy.deepcopy(first))
    if defect == 'attempt3':
        current['run_attempt'] = 3
    elif defect == 'run-failure':
        current['conclusion'] = 'failure'
    elif defect == 'replay-test':
        job = next(j for j in second['jobs'] if j['name'].startswith('Test (ubuntu'))
        job.update(run_attempt=2, id=9000, started_at='2026-11-05T01:00:00Z', completed_at='2026-11-05T01:01:00Z')
    elif defect == 'missing':
        second['jobs'] = [j for j in second['jobs'] if j['name'] != 'Build']
    elif defect == 'duplicate':
        second['jobs'].append(copy.deepcopy(second['jobs'][0]))
    elif defect == 'wrong-source':
        second['proof']['source_commit'] = 'f' * 40
    elif defect == 'wrong-base':
        second['pr']['base']['sha'] = 'f' * 40
    elif defect == 'wrong-repo':
        second['run']['repository']['full_name'] = 'foreign/repo'
    elif defect == 'wrong-workflow':
        second['run']['path'] = '.github/workflows/other.yml'
    elif defect == 'bad-proof':
        second['proof']['schema'] = 2
    elif defect == 'bad-pin':
        second['blobs'][kit.RECORDER] = base64.b64encode(b'unreviewed').decode()
    elif defect == 'failed-replacement':
        next(j for j in second['jobs'] if j['run_attempt'] == 2)['conclusion'] = 'failure'
    elif defect == 'skipped-build-inclusion':
        next(j for j in second['jobs'] if j['name'] == 'Build')['steps'][0]['conclusion'] = 'skipped'
    elif defect == 'skipped-notifier-native':
        job = next(j for j in second['jobs'] if j['name'] == 'Heinzel Notifier (macOS app)')
        next(s for s in job['steps'] if s['name'].startswith('Real launches'))['conclusion'] = 'skipped'
    elif defect == 'wrong-test-dependencies':
        job = next(j for j in second['jobs'] if 'Python 3.15,' in j['name'] and j['run_attempt'] == 2)
        next(s for s in job['steps'] if s['name'].startswith('Install Python'))['name'] = 'Install wheel dependencies'
    elif defect == 'changed-original-timestamp':
        next(j for j in first['jobs'] if j['name'].startswith('Test (ubuntu'))['completed_at'] = '2026-11-05T00:00:11Z'
    else:
        second['jobs'] = [j for j in second['jobs'] if j['name'] != 'Coverage']
    with pytest.raises(cap.InvalidCapacity):
        cap.verify_composite(frozen, VERSION, TOKEN, RUN, '2026-11-05T01:00:00Z')


def test_replay_guard_parses_actual_graph_and_rejects_test_descendant_before_request():
    data = evidence()
    eligible = cap.original(data, VERSION, RUN)
    assert cap.dependency_closure(data, eligible) == {'Build', 'CI OK'}
    text = base64.b64decode(data['blobs'][cap.strict.WORKFLOW]).decode()
    start = text.index('\n  e2e:\n')
    end = text.index('\n  template:\n', start)
    modified = text[:start] + text[start:end].replace('needs: changes', 'needs: [changes, build]', 1) + text[end:]
    data['blobs'][cap.strict.WORKFLOW] = base64.b64encode(modified.encode()).decode()
    with pytest.raises(cap.InvalidCapacity, match='descendant'):
        cap.dependency_closure(data, eligible)


def test_strict_verifier_rejects_original_capacity_failure():
    data = evidence()
    with pytest.raises(cap.strict.CandidateError):
        cap.strict.validate(data, cap.strict.ExpectedCandidate(RUN, PR, HEAD, TREE), kit.policy(commit=HEAD))


@pytest.fixture
def coordinator(tool, home, monkeypatch):
    rel = tool('hz-release')
    monkeypatch.setattr(rel, 'now_iso', lambda offset=0: '2026-11-05T01:00:00Z')
    rel.remember(version=VERSION)
    rel.acquire_snapshot_freeze(VERSION)
    monkeypatch.setattr(rel, 'pretag_identity', lambda version: {'pr': PR, 'head': HEAD, 'draft': False})
    monkeypatch.setattr(cap, 'collect_original', lambda version, run: copy.deepcopy(evidence()))
    def args(mode='prepare'):
        return [VERSION, '--candidate-capacity', mode, '--candidate-run', str(RUN),
                '--expected-receipt-sha256', hashlib.sha256(rel.paths()[1].read_bytes()).hexdigest(),
                '--expected-freeze-token', rel.state()['freeze_token']]
    return rel, args


def test_supported_cli_prepares_then_requests_once_without_release_actions(coordinator, monkeypatch):
    rel, args = coordinator
    posts = []
    monkeypatch.setattr(cap.hzlib, 'gh', lambda *argv: (posts.append(argv) or (0, '')))
    monkeypatch.setattr(rel, 'release_core', lambda *a: pytest.fail('capacity CLI must not release'))
    monkeypatch.setattr(rel, 'merge_release_pr', lambda *a: pytest.fail('capacity CLI must not merge'))
    assert rel.main(args()) == 0
    assert posts == []
    record = rel.state()['candidate_reacquisition']
    frozen = copy.deepcopy(record['audit'])
    request = args('request') + ['--expected-audit-sha256', record['audit']['audit_sha256']]
    assert rel.main(request) == 0
    assert posts == [('api', '--method', 'POST', f'repos/{cap.hzlib.CORE}/actions/runs/{RUN}/rerun-failed-jobs')]
    assert rel.state()['candidate_reacquisition']['audit'] == frozen
    assert rel.snapshot_hold_path().exists()
    assert rel.main(args('request') + ['--expected-audit-sha256', frozen['audit_sha256']]) == 1
    assert len(posts) == 1


@pytest.mark.parametrize('boundary', ['transport', 'after-intent', 'receipt-cas', 'changed-head', 'changed-hold', 'bad-audit'])
def test_request_crash_identity_or_cas_never_automatically_retries(coordinator, monkeypatch, boundary):
    rel, args = coordinator
    assert rel.main(args()) == 0
    audit_sha = rel.state()['candidate_reacquisition']['audit']['audit_sha256']
    request = args('request') + ['--expected-audit-sha256', audit_sha]
    posts = []
    monkeypatch.setattr(cap.hzlib, 'gh', lambda *argv: (posts.append(argv) or ((1, '') if boundary == 'transport' else (0, ''))))
    if boundary == 'after-intent':
        def crash(*argv):
            posts.append(argv)
            raise OSError('request interrupted after persisted intent')
        monkeypatch.setattr(cap.hzlib, 'gh', crash)
    elif boundary == 'receipt-cas':
        rel.remember(another='change')
    elif boundary == 'changed-head':
        monkeypatch.setattr(rel, 'pretag_identity', lambda version: {'pr': PR, 'head': 'f' * 40, 'draft': False})
    elif boundary == 'changed-hold':
        rel.snapshot_hold_path().write_text('other-version')
    elif boundary == 'bad-audit':
        request[-1] = 'f' * 64
    assert rel.main(request) == 1
    assert len(posts) == (1 if boundary in ('transport', 'after-intent') else 0)
    if posts:
        assert rel.state()['candidate_reacquisition']['request']['status'] == 'intent-recorded'
        assert rel.main(args('request') + ['--expected-audit-sha256', audit_sha]) == 1
        assert len(posts) == 1


@pytest.mark.parametrize('extra', [['--pause'], ['--prepare-only'], ['--snapshot-acceptance'], ['--intro', 'x'],
                                   ['--use-candidate-composite', str(RUN)], ['--timeout', '1'], ['--reason', 'x']])
def test_capacity_cli_rejects_mixed_release_action_flags(coordinator, extra):
    rel, args = coordinator
    with pytest.raises(SystemExit):
        rel.main(args() + extra)
    assert 'candidate_reacquisition' not in rel.state()


def test_composite_guard_is_explicit_and_does_not_alias_first_attempt_receipt(coordinator, monkeypatch):
    rel, _ = coordinator
    proof = {'schema': cap.COMPOSITE, 'first_attempt_clean': False}
    monkeypatch.setattr(rel, 'release_pr', lambda tag: {'number': PR, 'state': 'OPEN'})
    monkeypatch.setattr(cap, 'consume', lambda adapter, run, number: proof if (run, number) == (RUN, PR) else pytest.fail('wrong identity'))
    monkeypatch.setattr(rel.release_candidate_policy, 'verify_pr', lambda number: pytest.fail('must not fake strict validation'))
    fake = SimpleNamespace(train=lambda *a, **kw: (kw['pre_merge_guard'](), 'guard'))
    monkeypatch.setattr(rel.hzlib, 'load', lambda name: fake)
    rel.merge_release_pr('v' + VERSION, RUN)
    assert rel.state()['candidate_composite'] == proof
    assert 'candidate_ci' not in rel.state()


def test_complete_consumer_requires_supported_audit_request_and_owned_hold(coordinator, monkeypatch):
    rel, args = coordinator
    assert rel.main(args()) == 0
    record = rel.state()['candidate_reacquisition']
    monkeypatch.setattr(cap.hzlib, 'gh', lambda *argv: (0, ''))
    assert rel.main(args('request') + ['--expected-audit-sha256', record['audit']['audit_sha256']]) == 0
    wire(monkeypatch)
    result = cap.consume(rel.ReleaseAdapter(), RUN, PR)
    assert result['schema'] == cap.COMPOSITE
    assert 'candidate_ci' not in rel.state()
    record = rel.state()['candidate_reacquisition']
    record['request']['operation'] = 'rerun-all'
    rel.remember(candidate_reacquisition=record)
    with pytest.raises(cap.InvalidCapacity, match='identity'):
        cap.consume(rel.ReleaseAdapter(), RUN, PR)


@pytest.mark.parametrize('defect', ['total', 'duplicates', 'empty-page', 'wrong-id'])
def test_pagination_exhaustion_refuses_inconsistent_or_duplicate_inventory(monkeypatch, defect):
    responses = [{'total_count': 2, 'jobs': [{'id': 1}]}]
    second = {'total_count': 2, 'jobs': [{'id': 2}]}
    if defect == 'total':
        second['total_count'] = 3
    elif defect == 'duplicates':
        second['jobs'][0]['id'] = 1
    elif defect == 'empty-page':
        second['jobs'] = []
    else:
        second['jobs'][0]['id'] = True
    responses.append(second)
    monkeypatch.setattr(cap, 'get', lambda path: responses.pop(0))
    with pytest.raises(cap.InvalidCapacity):
        cap.paged('actions/runs/99/attempts/1/jobs', 'jobs')


def test_annotation_fetch_is_same_job_bound_and_exhausts_pages(monkeypatch):
    job = next(j for j in evidence()['jobs'] if j['conclusion'] == 'cancelled')
    job['check_run_url'] = 'https://foreign.invalid/check-runs/1'
    monkeypatch.setattr(cap, 'get', lambda path: pytest.fail('unbound annotation request'))
    with pytest.raises(cap.InvalidCapacity, match='same job'):
        cap.annotations(job)


def test_bounded_evidence_cannot_persist_oversized_raw_receipt():
    with pytest.raises(cap.InvalidCapacity, match='bound'):
        cap.encoded({'oversized': 'x' * cap.LIMIT})


def clone_carriers(second):
    for job in second['jobs']:
        if job['run_attempt'] == 1:
            job.update(id=job['id'] + 5000, run_attempt=2, runner_group_id=None)
            if job['conclusion'] == 'skipped':
                job['started_at'] = '2026-11-05T01:00:00Z'  # Metadata refresh, not execution.


def test_full_new_carrier_inventory_preserves_both_check_ids_and_original_execution(monkeypatch):
    first, second, _ = wire(monkeypatch)
    clone_carriers(second)
    result = cap.verify_composite(audit(first), VERSION, TOKEN, RUN, '2026-11-05T01:00:00Z')
    cells = {cell['name']: cell for cell in result['cells']}
    for name, cell in cells.items():
        assert cell['original_job_id'] != cell['carrier_job_id']
        assert cell['carrier_attempt'] == 2
        if name.startswith('Test (ubuntu'):
            assert cell['origin'] == 'reused-original-execution/new-check-record'
            assert cell['execution_attempt'] == 1
        if cell['conclusion'] == 'skipped':
            assert cell['execution_attempt'] is None
            assert cell['origin'] == 'skipped-control/new-check-record'
    assert first['run']['run_attempt'] == 1 and first['run']['conclusion'] == 'failure'


@pytest.mark.parametrize('defect', ['runner-id', 'runner-name', 'job-clock', 'step-clock', 'step-result', 'step-content',
                                   'post-intent', 'pre-intent-replacement', 'duplicate-id', 'foreign-head', 'partial',
                                   'naive-clock', 'reversed-step', 'executed-skip'])
def test_carrier_adaptation_never_accepts_a_different_or_replayed_execution(monkeypatch, defect):
    first, second, _ = wire(monkeypatch)
    frozen = audit(copy.deepcopy(first))
    clone_carriers(second)
    job = next(j for j in second['jobs'] if j['name'].startswith('Test (ubuntu'))
    if defect == 'runner-id':
        job['runner_id'] += 1
    elif defect == 'runner-name':
        job['runner_name'] += 'other'
    elif defect == 'job-clock':
        job['completed_at'] = '2026-11-05T00:00:11Z'
    elif defect == 'step-clock':
        job['steps'][0]['completed_at'] = '2026-11-05T00:00:11Z'
    elif defect == 'step-result':
        job['steps'][0]['conclusion'] = 'skipped'
    elif defect == 'step-content':
        job['steps'][0]['name'] += 'changed'
    elif defect in ('post-intent', 'naive-clock', 'reversed-step'):
        original = next(j for j in first['jobs'] if j['name'] == job['name'])
        if defect == 'post-intent':
            original['completed_at'] = '2026-11-05T01:00:01Z'
        elif defect == 'naive-clock':
            original['completed_at'] = '2026-11-05T00:00:10'
        else:
            original['steps'][0]['started_at'] = '2026-11-05T00:00:11Z'
        job.update(copy.deepcopy({key: original[key] for key in cap.EXECUTION}))
        if defect == 'naive-clock':
            with pytest.raises(cap.InvalidCapacity):
                audit(copy.deepcopy(first))
            return
        frozen = audit(copy.deepcopy(first))  # Boundary must independently reject even equal tuples.
    elif defect == 'pre-intent-replacement':
        next(j for j in second['jobs'] if j['name'] == 'Build')['steps'][0]['started_at'] = '2026-11-05T00:59:59Z'
    elif defect == 'duplicate-id':
        job['id'] = second['jobs'][0]['id']
    elif defect == 'foreign-head':
        job['head_sha'] = 'f' * 40
    elif defect == 'partial':
        second['jobs'].remove(job)
    else:
        skipped = next(j for j in second['jobs'] if j['conclusion'] == 'skipped')
        skipped['runner_id'] = 1
    with pytest.raises(cap.InvalidCapacity):
        cap.verify_composite(frozen, VERSION, TOKEN, RUN, '2026-11-05T01:00:00Z')


def test_actual_provider_records_have_new_check_identity_but_unchanged_execution():
    rows = json.loads((Path(__file__).resolve().parent / 'fixtures/candidate-reused-carriers.json').read_text())
    assert len(rows) == 3
    for row in rows:
        old, new = row['original'], row['carrier']
        assert old['id'] != new['id'] and (old['run_attempt'], new['run_attempt']) == (1, 2)
        assert cap.execution_tuple(old) == cap.execution_tuple(new)
        cap.execution_boundary(old, cap.clock('2026-11-05T18:49:35Z'), reused=True)


def test_only_observed_blocked_to_clean_pr_metadata_transition_preserves_raw_audit(monkeypatch):
    first, second, _ = wire(monkeypatch)
    first['pr']['mergeable_state'] = 'blocked'
    frozen = audit(copy.deepcopy(first))
    before = copy.deepcopy(frozen)
    first['pr']['mergeable_state'] = second['pr']['mergeable_state'] = 'clean'
    result = cap.verify_composite(frozen, VERSION, TOKEN, RUN, '2026-11-05T01:00:00Z')
    assert result['schema'] == cap.COMPOSITE and frozen == before


@pytest.mark.parametrize('field,value', [('number', 518), ('state', 'closed'), ('draft', True), ('title', 'different'),
                                       ('user', {'id': 999, 'login': 'different'}), ('updated_at', '2026-11-05T01:00:01Z'),
                                       ('head', {'sha': 'f' * 40}), ('base', {'sha': 'f' * 40}),
                                       ('mergeable_state', 'unknown')])
def test_no_other_pr_identity_or_presentation_change_is_normalized(field, value):
    saved = copy.deepcopy(evidence()['pr'])
    saved.update(mergeable_state='blocked', user={'id': 1, 'login': 'owner'}, updated_at='2026-11-05T00:00:00Z')
    fresh = copy.deepcopy(saved)
    fresh['mergeable_state'] = 'clean'
    fresh[field] = value
    with pytest.raises(cap.InvalidCapacity):
        cap.same_pr_identity(saved, fresh)


def test_unobserved_mergeability_transition_or_field_removal_is_refused():
    for saved, fresh in [({'mergeable_state': 'clean'}, {'mergeable_state': 'blocked'}),
                         ({'mergeable_state': 'blocked'}, {}), ({}, {'mergeable_state': None})]:
        with pytest.raises(cap.InvalidCapacity):
            cap.same_pr_identity(saved, fresh)


@pytest.mark.parametrize('message,expected', [
    ('original PR identity or unapproved metadata changed', 'original PR identity or unapproved metadata changed'),
    ('SECRET_API_RESPONSE=never-print-this', 'capacity evidence failed validation'),
])
def test_guard_reports_only_locally_authored_safe_capacity_reason(coordinator, monkeypatch, capsys, message, expected):
    rel, _ = coordinator
    monkeypatch.setattr(rel, 'release_pr', lambda tag: {'number': PR, 'state': 'OPEN'})
    def refuse(*args):
        raise cap.InvalidCapacity(message)
    monkeypatch.setattr(cap, 'consume', refuse)
    fake = SimpleNamespace(train=lambda *a, **kw: (kw['pre_merge_guard'](), 'guard refused'))
    monkeypatch.setattr(rel.hzlib, 'load', lambda name: fake)
    with pytest.raises(rel.Stop):
        rel.merge_release_pr('v' + VERSION, RUN)
    output = capsys.readouterr().out
    assert 'full candidate refused: ' + expected in output
    assert 'SECRET_API_RESPONSE' not in output
    assert 'candidate_composite' not in rel.state()


@pytest.mark.parametrize('defect', ['before-intent', 'naive', 'changed-workflow', 'changed-original-creation'])
def test_attempt_creation_is_separate_but_original_creation_and_run_identity_stay_strict(monkeypatch, defect):
    first, second, _ = wire(monkeypatch)
    frozen = audit(copy.deepcopy(first))
    if defect == 'before-intent':
        second['run']['created_at'] = '2026-11-05T00:59:59Z'
    elif defect == 'naive':
        second['run']['created_at'] = '2026-11-05T01:00:00'
    elif defect == 'changed-workflow':
        second['run']['workflow_id'] = 999
    else:
        first['run']['created_at'] = '2026-11-05T00:00:01Z'
    with pytest.raises(cap.InvalidCapacity):
        cap.verify_composite(frozen, VERSION, TOKEN, RUN, '2026-11-05T01:00:00Z')


def test_actual_null_runner_skip_controls_remain_nonexecutions_in_complete_inventory(monkeypatch):
    first, second, _ = wire(monkeypatch)
    for jobs in (first['jobs'], second['jobs']):
        for job in jobs:
            if job['conclusion'] == 'skipped':
                job.update(runner_id=None, runner_name=None)
    clone_carriers(second)
    result = cap.verify_composite(audit(first), VERSION, TOKEN, RUN, '2026-11-05T01:00:00Z')
    skips = [cell for cell in result['cells'] if cell['conclusion'] == 'skipped']
    assert len(skips) == 12 and all(cell['execution_attempt'] is None for cell in skips)


@pytest.mark.parametrize('pair', [(0, None), (None, ''), (False, ''), (1, 'runner'), (None, 'runner')])
def test_expected_skip_carrier_cannot_change_original_no_runner_identity(pair):
    original = {'conclusion': 'skipped', 'runner_id': None, 'runner_name': None, 'steps': []}
    carrier = original | {'runner_id': pair[0], 'runner_name': pair[1]}
    with pytest.raises(cap.InvalidCapacity):
        cap.skipped_carrier(carrier, original)


def test_expected_skip_carrier_requires_explicit_original_and_carrier_runner_fields():
    original = {'conclusion': 'skipped', 'runner_id': None, 'runner_name': None, 'steps': []}
    with pytest.raises(cap.InvalidCapacity):
        cap.skipped_carrier({'conclusion': 'skipped', 'steps': []}, original)


def test_pr_identity_projection_preserves_json_value_types():
    with pytest.raises(cap.InvalidCapacity):
        cap.same_pr_identity({'user': {'id': 1}}, {'user': {'id': True}})
