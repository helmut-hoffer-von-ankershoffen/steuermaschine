"""Isolated authorization and original-proof fixtures for the main-only route."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import types
import zipfile

import policy_kit as kit
import pytest

from test_native_admission_slots import env, s
from steuermaschine_heinzel import state as statedir

r = s.resulting
a = s.a
HEAD, OLD_BASE, OLD_TREE, OLD_COMMIT, JUDGE_SHA, MAIN, TREE = (digit * 40 for digit in 'abcdef1')
PR, PR_ID, REPO_ID, CI_ID, JUDGE_ID, JOB = 5, 98, 97, 96, 95, 94
CHECKS = {'CI OK': 93, 'merge-gate': 92, 'coverage-floor': 91}
BRANCH = 'fix/fixture'


@pytest.fixture
def original(env):
    rt, _, _ = env
    rt.main = MAIN
    repo = {'id': REPO_ID, 'full_name': rt.repo, 'fork': False}
    old = dict(schema=1, event='pull_request', ref='refs/pull/*/merge', head=HEAD,
               base=OLD_BASE, tree=OLD_TREE, selection={'python': ['tests/unit/fixture_test.py']})
    original = dict(pr=PR, pr_id=PR_ID, repository_id=REPO_ID, branch=BRANCH, head=HEAD,
                    tested_base=OLD_BASE, tested_tree=OLD_TREE, tested_commit=OLD_COMMIT, judge_source=JUDGE_SHA,
                    manifest_sha256=r.digest(a.canonical(old)), ci_run=CI_ID, judge_run=JUDGE_ID)
    main = dict(schema=1, event='push', ref='refs/heads/main', base=MAIN, tree=TREE, changed={'fixture.py': 'M'})
    packet = dict(schema=1, mode=r.MODE, original=original, manifest=main)
    root = f'repos/{rt.repo}'
    checks = [dict(id=check_id, name=name, head_sha=HEAD, app={'id': r.APP}, status='completed', conclusion='success',
                   details_url=f'https://github.com/{rt.repo}/actions/runs/{CI_ID}/job/{check_id}' if name == 'CI OK'
                   else f'https://github.com/{rt.repo}/runs/{check_id}') for name, check_id in CHECKS.items()]
    ci = dict(id=CI_ID, run_attempt=1, head_sha=HEAD, event='pull_request', path=kit.WORKFLOW, head_branch=BRANCH,
              repository=repo, head_repository=repo, status='completed', conclusion='success', workflow_id=7, check_suite_id=8)
    judge = dict(ci, id=JUDGE_ID, event='workflow_run', path=kit.JUDGE, head_sha=JUDGE_SHA, head_branch='main',
                 workflow_id=9, check_suite_id=10)
    artifact = dict(id=90, name=f'focused-pr-evidence-python-{CI_ID}-1', size_in_bytes=22,
                    digest='sha256:' + '0' * 64, expired=False, workflow_run={'id': CI_ID})
    pull = dict(id=PR_ID, number=PR, state='open', draft=False, mergeable_state='clean', title='fix: fixture',
                head=dict(ref=BRANCH, sha=HEAD, repo=copy.deepcopy(repo)), base=dict(ref='main', repo=copy.deepcopy(repo)))
    rule = dict(type='required_status_checks', ruleset_id=11, ruleset_source=rt.repo, ruleset_source_type='Repository',
                parameters=dict(strict_required_status_checks_policy=False,
                                required_status_checks=[{'context': name, **({'integration_id': r.APP} if name != 'CI OK' else {})}
                                                        for name in CHECKS]))
    branch = dict(name='main', protected=True, commit={'sha': MAIN}, protection=dict(enabled=False,
                  required_status_checks=dict(enforcement_level='off', checks=[], contexts=[])))
    judge_job = dict(id=JOB, runner_id=123, name='Merge gates', run_id=JUDGE_ID, run_attempt=1,
                     head_sha=JUDGE_SHA, status='completed', conclusion='success', steps=[
                         dict(number=index, name=name, status='completed', conclusion='success')
                         for index, name in enumerate(('Run actions/checkout@' + '2' * 40, r.STEP_SONAR,
                                                       r.STEP_COVERAGE, r.STEP_REPORT), 2)])
    ci_job = dict(id=CHECKS['CI OK'], name='CI OK', run_id=CI_ID, run_attempt=1, head_sha=HEAD,
                  status='completed', conclusion='success')
    api = {
        root: repo, f'{root}/pulls/{PR}': pull,
        f'{root}/git/ref/heads/fix%2Ffixture': {'ref': 'refs/heads/' + BRANCH, 'object': {'type': 'commit', 'sha': HEAD}},
        f'{root}/git/ref/heads/main': {'object': {'sha': MAIN}}, f'{root}/branches/main': branch,
        f'{root}/actions/runs/{CI_ID}': ci, f'{root}/actions/runs/{JUDGE_ID}': judge,
        f'{root}/actions/runs/{JUDGE_ID}/attempts/1': copy.deepcopy(judge),
        f'{root}/actions/workflows/7/runs?head_sha={HEAD}&event=pull_request&per_page=1': {'workflow_runs': [{'id': CI_ID}]},
        f'{root}/actions/workflows/merge-gate.yml': {'id': 9, 'path': kit.JUDGE},
        f'{root}/check-runs/{JOB}': dict(id=JOB, name='Merge gates', head_sha=JUDGE_SHA, app={'id': r.APP},
                                       check_suite={'id': 10}, status='completed', conclusion='success'),
        **{f"{root}/check-runs/{row['id']}": copy.deepcopy(row) for row in checks},
        **{f'{root}/{path}': answer for path, answer in kit.contents_routes(MAIN, kit.files('new')).items()},  # Core's policy at main
    }
    pages = {
        f'{root}/commits/{HEAD}/check-runs?filter=latest': checks,
        f'{root}/actions/runs/{CI_ID}/artifacts': [artifact],
        f'{root}/actions/runs/{CI_ID}/attempts/1/jobs': [ci_job],
        f'{root}/actions/runs/{JUDGE_ID}/attempts/1/jobs': [judge_job],
    }
    rt.api = lambda path: copy.deepcopy(api[path])
    original_pages = rt.pages
    rt.pages = lambda path, key: copy.deepcopy(pages[path]) if path in pages else original_pages(path, key)

    def command(*args, **kwargs):
        assert args == ('gh', 'api', f'{root}/rules/branches/main?per_page=100&page=1')
        return subprocess.CompletedProcess(args, 0, a.canonical([rule]))

    rt.run = command
    return types.SimpleNamespace(rt=rt, packet=packet, old=old, root=root, rule=rule, pull=pull, api=api,
                                 ci=ci, judge=judge, artifact=artifact, checks=checks, branch=branch,
                                 ci_job=ci_job, judge_job=judge_job, pages=pages)


@pytest.mark.parametrize('defect', ['strict', 'classic', 'app', 'missing-rule', 'fork', 'pr-head', 'id', 'main',
                                    'latest', 'ci-attempt', 'judge-attempt', 'check-app', 'check-failed',
                                    'check-url', 'check-duplicate', 'artifact-expired', 'artifact-foreign',
                                    'policy-missing', 'policy-garbled', 'policy-contexts', 'policy-workflow'])
def test_current_green_proof_refuses_drift(original, defect):
    f = original
    if defect == 'strict': f.rule['parameters']['strict_required_status_checks_policy'] = True
    elif defect == 'classic': f.branch['protection']['enabled'] = True
    elif defect == 'app': f.rule['parameters']['required_status_checks'][1]['integration_id'] = 1
    elif defect == 'missing-rule': f.rule['parameters']['required_status_checks'].pop()
    elif defect == 'fork': f.pull['head']['repo']['fork'] = True
    elif defect == 'pr-head': f.pull['head']['sha'] = TREE
    elif defect == 'id': f.pull['id'] += 1
    elif defect == 'main': f.api[f'{f.root}/git/ref/heads/main']['object']['sha'] = TREE
    elif defect == 'latest': f.api[f'{f.root}/actions/workflows/7/runs?head_sha={HEAD}&event=pull_request&per_page=1']['workflow_runs'][0]['id'] += 1
    elif defect == 'ci-attempt': f.ci['run_attempt'] = 2
    elif defect == 'judge-attempt': f.judge['run_attempt'] = 2
    elif defect == 'check-app': f.checks[1]['app']['id'] = 1
    elif defect == 'check-failed': f.checks[1]['conclusion'] = 'failure'
    elif defect == 'check-url': f.checks[0]['details_url'] = f'https://github.com/{f.rt.repo}/runs/{CHECKS["CI OK"]}'
    elif defect == 'check-duplicate': f.checks.append(dict(f.checks[1], id=123))
    elif defect == 'artifact-expired': f.artifact['expired'] = True
    elif defect == 'artifact-foreign': f.artifact['workflow_run']['id'] = 123
    elif defect == 'policy-missing': f.api[f'{f.root}/contents/{kit.PATH}?ref={MAIN}'] = None
    elif defect == 'policy-garbled': f.api[f'{f.root}/contents/{kit.PATH}?ref={MAIN}'] = kit.contents(b'{"policy": 2}\n')
    elif defect == 'policy-workflow':  # the run's workflow is not the one main's policy names
        f.api.update({f'{f.root}/{path}': answer for path, answer in kit.contents_routes(
            MAIN, kit.files('new', workflow='.github/workflows/other.yml')).items()})
    elif defect == 'policy-contexts':  # the ruleset still requires coverage-floor; main's policy does not
        f.api.update({f'{f.root}/{path}': answer for path, answer in kit.contents_routes(
            MAIN, kit.files('new', required_contexts=['CI OK', 'merge-gate'])).items()})
    with pytest.raises(a.AdmissionError):
        r.live_proof(f.rt, f.packet)
    assert not f.rt.writes


@pytest.mark.parametrize('defect', ['none', 'wrong-ci-job', 'wrong-judge', 'duplicate-step', 'failed-step', 'other-published'])
def test_judge_uses_official_attempt_job_and_published_check_links(original, defect):
    f = original
    live = r.live_proof(f.rt, f.packet)
    if defect == 'wrong-ci-job': f.ci_job['id'] += 1
    elif defect == 'wrong-judge': f.api[f'{f.root}/actions/workflows/merge-gate.yml']['id'] = 77
    elif defect == 'duplicate-step': f.judge_job['steps'][1]['number'] = 2
    elif defect == 'failed-step': f.judge_job['steps'][2]['conclusion'] = 'failure'
    elif defect == 'other-published': f.api[f'{f.root}/check-runs/{CHECKS["coverage-floor"]}']['head_sha'] = TREE
    if defect == 'none':
        assert r.judgment_steps(f.rt, f.packet, live) == {
            'checkout': f'actions/jobs/{JOB}/steps/2/logs', 'sonar': f'actions/jobs/{JOB}/steps/3/logs',
            'coverage': f'actions/jobs/{JOB}/steps/4/logs', 'report': f'actions/jobs/{JOB}/steps/5/logs',
        }
    else:
        with pytest.raises(a.AdmissionError):
            r.judgment_steps(f.rt, f.packet, live)


def make_packet(f):
    review = f.rt.home / 'review.md'
    review.write_text('Independently reviewed original scope and exact resulting tree.')
    f.packet['review'] = dict(path='review.md', sha256=r.digest(review.read_bytes()), head=HEAD, base=MAIN, tree=TREE,
                              manifest_sha256=r.digest(a.canonical(f.packet['manifest'])),
                              original_sha256=r.digest(a.canonical(f.packet['original'])))
    path = f.rt.home / 'packet.json'
    path.write_bytes(a.canonical(f.packet))
    return path


def prepare_cli(f, monkeypatch):
    rt = f.rt
    path = make_packet(f)
    workspace = rt.home / 'workspace'
    snapshot = workspace / 'repo'
    snapshot.mkdir(parents=True)
    source = rt.home / 'trusted-source'
    (source / 'scripts').mkdir(parents=True)
    # Use a transparent fixture codec so the actual registry witness has to
    # detect altered entries. Core's existing compressed codec stays untouched.
    (source / 'scripts/focused_plan.py').write_text('''import json
SLOT_VARIABLES = tuple(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4))
def registry_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
def plan_registry(legacy, slots):
    return {key: value for text in (legacy, *slots) for key, value in json.loads(text or "{}").items()}
''')
    legacy = {a.registry_key(f.old): f.old, 'push-tree:' + OLD_TREE: {'existing': True}}
    rt.values = {s.NAMES[0]: a.canonical(legacy).decode(), s.NAMES[4]: ''}
    monkeypatch.delenv('HZ_QUEUE_STATE')
    for name in ('HZ_TRAIN_STATE_DIR', 'HZ_RELEASE_DIR'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(s, 'Runtime', lambda core, home=None: rt)
    monkeypatch.setattr(a, '_read_main', lambda repo: rt.main)
    monkeypatch.setattr(a, '_prepare_core_snapshot', lambda core, base: (workspace, snapshot))
    monkeypatch.setattr(a, '_canonical_source', lambda core, base: (source, '2' * 40, rt.repo, s.NAMES[0], ()))
    monkeypatch.setattr(s, 'reader_contract', lambda *args: None)
    monkeypatch.setattr(s.shutil, 'rmtree', lambda *args, **kwargs: None)
    observed = []

    def canonical(packet, repo, base, repository, variable, files, archive, **options):
        assert packet == f.packet and repo == snapshot and base == MAIN and archive == source
        assert options == {'resulting_main': True, 'protected_producer': False}
        observed.append('compose')

    def replay(runtime, packet, repo, old, live):
        assert runtime == rt and packet == f.packet and repo == snapshot and old == f.old
        assert live['checks'] == CHECKS
        observed.append('replay')
        return dict(source={kit.JUDGE: '3' * 40, 'scripts': '4' * 40}, evidence={'tested_commit': OLD_COMMIT})

    def append(archive, old, slots, additions):
        assert archive == source and old == rt.values[s.NAMES[0]] and len(slots) == 4
        assert additions == {a.registry_key(f.packet['manifest']): f.packet['manifest']}
        return {'result': [0, a.canonical(additions).decode()]}

    monkeypatch.setattr(a, 'validate_canonical', canonical)
    monkeypatch.setattr(r, 'replay_original', replay)
    monkeypatch.setattr(a, '_isolated_slot_append', append)
    args = [str(path), '--core-repo', str(rt.core), '--review-root', str(rt.home), '--pr', str(PR), '--resulting-main-only']
    return args, path, source, observed


@pytest.mark.parametrize('defect', ['none', 'prepared-drift', 'rule-after-prepare', 'old-manifest', 'stale-compose',
                                    'postwrite-check', 'postwrite-readback', 'protected-producer', 'already-present'])
def test_actual_cli_main_only_binds_original_and_one_reviewed_append(original, monkeypatch, capsys, defect):
    f = original
    rt = f.rt
    args, packet_path, source, observed = prepare_cli(f, monkeypatch)
    assert s.main(['prepare', *args]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared['resulting_main']['original'] == f.packet['original']
    assert 'pr_update' not in prepared and observed == ['compose', 'replay'] and not rt.writes
    if defect == 'prepared-drift': prepared['resulting_main']['original_replay']['source'][kit.JUDGE] = '5' * 40
    elif defect == 'rule-after-prepare': f.rule['parameters']['strict_required_status_checks_policy'] = True
    elif defect == 'old-manifest': rt.values[s.NAMES[0]] = '{}'
    elif defect == 'stale-compose':
        monkeypatch.setattr(a, 'validate_canonical', lambda *args, **kwargs: (_ for _ in ()).throw(a.AdmissionError('composition changed')))
    elif defect == 'postwrite-check':
        write = rt.set_variable

        def mutate(*args):
            write(*args)
            f.checks[1]['conclusion'] = 'failure'

        rt.set_variable = mutate
    elif defect == 'postwrite-readback': rt.defect = 'readback'
    elif defect == 'protected-producer': args += ['--reviewed-protected-producer']
    elif defect == 'already-present': monkeypatch.setattr(a, '_isolated_slot_append', lambda *args: {'result': None})
    approval = rt.home / 'approved.json'
    approval.write_bytes(a.canonical(prepared))
    result = s.main(['apply', *args, '--prepared', str(approval), '--approved-sha256', r.digest(a.canonical(prepared))])
    if defect == 'none':
        assert result == 0 and observed == ['compose', 'replay', 'compose', 'replay']
        assert rt.events == ['pause', 'write', 'restore'] and len(rt.writes) == 1
        receipt = json.loads(Path(capsys.readouterr().out.strip()).read_bytes())
        assert receipt['resulting_main'] == prepared['resulting_main'] and set(receipt['after']) == set(s.NAMES)
        assert rt.values[s.NAMES[0]] == prepared['expected'][s.NAMES[0]] and rt.values[s.NAMES[4]] == ''
    else:
        assert result == 1
        after = defect.startswith('postwrite-')
        assert len(rt.writes) == after and rt.events == (['pause', 'write', 'restore'] if after else [])
        assert not list(statedir.root().glob('focused-admission-receipts/*.json'))


@pytest.mark.parametrize('defect', ['old-deleted', 'old-altered', 'extra-pr', 'wrong-tree', 'existing-key'])
def test_real_registry_witness_refuses_unapproved_decoded_changes(original, monkeypatch, defect):
    f = original
    _, _, source, _ = prepare_cli(f, monkeypatch)
    before = {name: f.rt.values.get(name, '') for name in s.NAMES}
    after = dict(before)
    plans = {a.registry_key(f.packet['manifest']): f.packet['manifest']}
    if defect == 'old-deleted': after[s.NAMES[0]] = '{}'
    elif defect == 'old-altered':
        old = json.loads(after[s.NAMES[0]])
        old['push-tree:' + OLD_TREE]['existing'] = False
        after[s.NAMES[0]] = a.canonical(old).decode()
    elif defect == 'extra-pr': plans['pull_request:' + TREE] = f.old
    elif defect == 'wrong-tree': plans = {'push-tree:' + OLD_COMMIT: f.packet['manifest']}
    elif defect == 'existing-key': before[s.NAMES[1]] = a.canonical(plans).decode()
    after[s.NAMES[1]] = a.canonical(plans).decode()
    with pytest.raises(a.AdmissionError, match='one-entry append'):
        r.registry_proof(source, f.packet, before, after)


@pytest.mark.parametrize('defect', ['mode', 'original', 'headful', 'producer-option', 'mixed-mode'])
def test_main_only_packet_and_modes_are_distinct(original, monkeypatch, defect):
    f = original
    args, packet_path, _, _ = prepare_cli(f, monkeypatch)
    if defect == 'producer-option':
        assert s.main(['prepare', *args, '--reviewed-protected-producer']) == 1
    elif defect == 'mixed-mode':
        with pytest.raises(SystemExit) as error:
            s.main(['prepare', *args, '--expected-remote-head', HEAD])
        assert error.value.code == 2
    else:
        if defect == 'mode': f.packet['mode'] = 'unknown'
        elif defect == 'original': f.packet['original']['tested_commit'] = TREE
        elif defect == 'headful': f.packet['manifest']['head'] = HEAD
        packet_path.write_bytes(a.canonical(f.packet))
        assert s.main(['prepare', *args]) == 1
    assert not f.rt.writes


@pytest.mark.parametrize('defect', [
    'none', 'leading-bom', 'non-leading-bom', 'wrong-ci', 'duplicate-head', 'checkout',
    'wrong-check', 'artifact-commit', 'artifact-digest',
])
def test_protected_replay_adapter_uses_exact_step_logs_and_verified_artifacts(original, monkeypatch, capsys, defect):
    f = original
    live = r.live_proof(f.rt, f.packet)
    paths = r.judgment_steps(f.rt, f.packet, live)
    prefix = '2026-11-11T02:42:46.4057729Z '

    def log(*rows):
        return ('\n'.join(prefix + row for row in rows) + '\n').encode()

    logs = {
        paths['checkout']: log('[command]/usr/bin/git log -1 --format=%H', TREE if defect == 'checkout' else JUDGE_SHA),
        paths['coverage']: log('##[group]Run uv run --no-build --frozen --script scripts/focused_gate.py',
                               f'  PR_HEAD_SHA: {HEAD}', f'  PR_NUMBER: {PR}', f'  CI_RUN_ID: {CI_ID + (defect == "wrong-ci")}'),
        paths['report']: log(f'  MERGE_GATE: {CHECKS["merge-gate"] + (defect == "wrong-check")}',
                             f'  COVERAGE_FLOOR: {CHECKS["coverage-floor"]}'),
    }
    bom = b'\xef\xbb\xbf'
    if defect == 'leading-bom':
        logs = {path: bom + data for path, data in logs.items()}
    elif defect == 'non-leading-bom':
        logs[paths['coverage']] = log('Earlier step log line') + bom + logs[paths['coverage']]
    if defect == 'duplicate-head': logs[paths['coverage']] += log(f'  PR_HEAD_SHA: {HEAD}')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('evidence.json', a.canonical(dict(commit=TREE if defect == 'artifact-commit' else OLD_COMMIT,
                         tree=OLD_TREE, base=OLD_BASE, manifest_sha256=f.packet['original']['manifest_sha256'])))
    data = buffer.getvalue()
    logs['actions/artifacts/90/zip'] = data
    live['artifacts']['90'].update(size_in_bytes=len(data), digest='sha256:' + ('1' * 64 if defect == 'artifact-digest' else r.digest(data)))
    called = []

    def verify(gate, run_id, manifest, read):
        assert gate == (f.rt.repo, str(PR), HEAD, 'fixture-token') and run_id == CI_ID
        assert json.loads(manifest) == {f'pull_request:{HEAD}': f.old}
        assert read('actions/artifacts/90/zip') == data
        called.append(run_id)
        return 'reviewed fixture scope passed'

    def fetch(url, token, *, limit):
        assert token == 'fixture-token' and limit > 0
        return logs[url.removeprefix(f'https://api.github.com/repos/{f.rt.repo}/')]

    monkeypatch.setitem(sys.modules, 'scripts', types.ModuleType('scripts'))
    monkeypatch.setitem(sys.modules, 'scripts.focused_gate', types.SimpleNamespace(
        require=r.require, verify=verify, parse=json.loads, MAX_UPLOAD_LOG=65536, MAX_ARCHIVE=5 * 1024 * 1024,
        REPORT='evidence.json'))
    monkeypatch.setitem(sys.modules, 'scripts.merge_gate', types.SimpleNamespace(Gate=lambda *args: args,
                         GITHUB='https://api.github.com', fetch_evidence=fetch))
    spec = importlib.util.spec_from_file_location('local_replay', Path(s.__file__).with_name('focused_replay.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(a.canonical(dict(token='fixture-token', repository=f.rt.repo,
                        original=f.packet['original'], plan=f.old, live=live, paths=paths)).decode()))
    if defect in ('none', 'leading-bom'):
        module.main()
        proof = json.loads(capsys.readouterr().out)
        assert called == [CI_ID] and proof['tested_commit'] == OLD_COMMIT and proof['artifacts'] == live['artifacts']
        assert set(proof['judge_log_sha256']) == {'checkout', 'coverage', 'report'}
    else:
        reason = 'protected judge coverage command differs' if defect == 'non-leading-bom' else None
        with pytest.raises(a.AdmissionError, match=reason):
            module.main()
        assert called == ([CI_ID] if defect.startswith('artifact-') else [])
