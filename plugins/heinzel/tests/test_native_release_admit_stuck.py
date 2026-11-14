"""release-main-only accepts only reviewed stuck-queued-pr-run nonconsumers; isolated fake GitHub/launchd."""
import base64
import calendar
import copy
import gzip
import json
import time

import pytest

from test_native_admission_slots import a, env  # noqa: F401
from steuermaschine_heinzel.native.release_admit_lib import release_slots as s
from test_native_release_admit import candidate, operation  # noqa: F401

r = s.release
REAL_GUARD = s.release_guard  # captured before the operation fixture replaces it
STUCK_HEAD = '5' * 40
CREATED = '2026-11-11T20:20:00Z'
NOW = calendar.timegm(time.strptime(CREATED, '%Y-%m-%dT%H:%M:%SZ')) + 3600
IDENTITY = dict(path='.github/workflows/ci.yml', head_sha=STUCK_HEAD, head_branch='copy-standard',
                event='pull_request', run_attempt=1, workflow_id=394704163, created_at=CREATED)
REFUSAL = b'{"message":"Cannot cancel a workflow run that has not been queued yet.","status":"409"}' \
          b'gh: Cannot cancel a workflow run that has not been queued yet. (HTTP 409)\n'
RUN = 43095386045


def slot(registry):
    raw = json.dumps(registry, sort_keys=True, separators=(',', ':')).encode()
    return json.dumps(dict(schema=1, codec='gzip+base64', bytes=len(raw), sha256=s.digest(raw),
                           data=base64.b64encode(gzip.compress(raw, mtime=0)).decode()))


def receipt(rt, **identity):
    return dict(kind=s.STUCK_KIND, run=RUN, repository=rt.repo, assertion=s.STUCK_ASSERTION,
                identity=dict(IDENTITY, **identity))


@pytest.fixture
def stuck(operation, monkeypatch):
    rt, packet, prepared, apply = operation
    # A real compressed candidate: the guard decodes the desired registry too.
    candidate_value = slot({f"push-tree:{packet['release']['tree']}": {}})
    prepared['desired'][prepared['slot']] = candidate_value
    prepared['candidate_sha256'] = s.digest(candidate_value.encode())
    monkeypatch.setattr(a, '_isolated_slot_append', lambda *args: {'result': [0, candidate_value]})
    run = dict(id=RUN, status='queued', conclusion=None, repository={'full_name': rt.repo},
               run_started_at=CREATED, updated_at=CREATED, **IDENTITY)
    rt.rows['queued'] = [run]
    rt.jobs = dict(total_count=0, jobs=[])
    rt.probes = []
    rt.cancel = (1, REFUSAL)
    base_api = rt.api

    def api(path):
        if path.endswith('/jobs?per_page=100'):
            assert path == f'repos/{rt.repo}/actions/runs/{RUN}/jobs?per_page=100'
            return rt.jobs
        return base_api(path)
    rt.api = api
    rt.registry = lambda: {n: rt.values.get(n, '') for n in s.NAMES}
    rt.now = lambda: NOW

    def probe(run_id):
        rt.probes.append(run_id)
        return rt.cancel
    rt.cancel_probe = probe
    prepared['nonconsumers'] = [receipt(rt)]
    return rt, packet, prepared, apply, run


def test_release_admits_exact_stuck_run_and_writes_once(stuck):
    rt, _, prepared, apply, _ = stuck
    result = json.loads(apply().read_bytes())
    assert len(rt.writes) == 1 and result['queue_restored'] is True
    assert set(rt.probes) == {RUN}  # cancel still answered 409 before the write


def test_release_without_receipt_still_refuses_the_queued_run(stuck):
    rt, _, prepared, apply, _ = stuck
    del prepared['nonconsumers']
    with pytest.raises(a.AdmissionError, match='unknown active Actions run'):
        apply()
    assert not rt.writes


DEFECTS = {
    'started': lambda rt, run: run.update(status='in_progress'),
    'jobs': lambda rt, run: rt.jobs.update(total_count=1, jobs=[{'id': 1}]),
    'jobs-count-only': lambda rt, run: rt.jobs.update(total_count=1),
    'head': lambda rt, run: run.update(head_sha='6' * 40),
    'attempt': lambda rt, run: run.update(run_attempt=2),
    'young': lambda rt, run: setattr(rt, 'now', lambda: NOW - 31 * 60),
    'requeued-recently': lambda rt, run: run.update(run_started_at='2026-11-11T21:10:00Z'),
    'no-started-at': lambda rt, run: run.pop('run_started_at'),
    'concluded': lambda rt, run: run.update(conclusion='cancelled'),
    'repository': lambda rt, run: run.update(repository={'full_name': 'other/repo'}),
    'cancellable': lambda rt, run: setattr(rt, 'cancel', (0, b'')),
    'other-409': lambda rt, run: setattr(rt, 'cancel', (1, b'gh: Conflict (HTTP 409)')),
    'live-plan': lambda rt, run: rt.values.update({s.NAMES[0]: json.dumps({f'pull_request:{STUCK_HEAD}': {}})}),
    'new-run': lambda rt, run: rt.rows.update(queued=[run, dict(run, id=RUN + 1)]),
    'release-run': lambda rt, run: rt.rows.update(in_progress=[dict(id=7, status='in_progress', event='push',
                                                                     head_branch='release/v0.9.37')]),
}


@pytest.mark.parametrize('defect', sorted(DEFECTS))
def test_any_change_refuses_release_apply_without_write(stuck, defect):
    rt, _, _, apply, run = stuck
    DEFECTS[defect](rt, run)
    with pytest.raises(a.AdmissionError):
        apply()
    assert not rt.writes and not rt.paused


def test_head_in_desired_release_candidate_refuses(stuck):
    rt, _, prepared, apply, _ = stuck
    value = slot({f'pull_request:{STUCK_HEAD}': {}})
    prepared['desired'][prepared['slot']] = value
    with pytest.raises(a.AdmissionError):
        apply()
    assert not rt.writes and rt.probes == []


@pytest.mark.parametrize('bad', ['closure', 'unknown', 'no-kind', 'duplicate', 'not-list', 'main', 'release', 'push', 'workflow'])
def test_release_reader_accepts_only_stuck_kind(env, bad):
    rt, _, _ = env
    item = receipt(rt)
    receipts = [item]
    if bad == 'closure':
        receipts = [dict(run=7, repository=rt.repo, identity={}, review={}, source={}, referenced_workflows=[],
                         assertion='complete immutable source closure has no focused registry reads')]
    if bad == 'unknown': item['kind'] = 'stuck-queued-anything'
    if bad == 'no-kind': del item['kind']
    if bad == 'duplicate': receipts = [item, copy.deepcopy(item)]
    if bad == 'not-list': receipts = {'receipts': [item]}
    if bad == 'main': item['identity']['head_branch'] = 'main'
    if bad == 'release': item['identity']['head_branch'] = 'release/v0.9.39'
    if bad == 'push': item['identity']['event'] = 'push'
    if bad == 'workflow': item['identity']['path'] = '.github/workflows/release.yml'
    with pytest.raises(a.AdmissionError):
        s.read_release_nonconsumers(rt, receipts)


def test_release_reader_round_trips_a_valid_receipt(env):
    rt, _, _ = env
    assert s.read_release_nonconsumers(rt, [receipt(rt)]) == {RUN: receipt(rt)}
    assert s.read_release_nonconsumers(rt, []) == {}


def test_real_release_guard_passes_desired_registry_to_stuck_proof(stuck, monkeypatch):
    rt, packet, prepared, _, _ = stuck
    if 'recovery' in packet['release']:
        pytest.skip('fresh release form only')
    monkeypatch.setattr(r, 'source_proof', lambda *args: ('src', None))
    monkeypatch.setattr(r, 'remote_identity', lambda *args: 1)
    monkeypatch.setattr(s, 'check_preopen', lambda *args: None)
    monkeypatch.setattr(s.hzlib, 'merge_frozen', lambda: False)
    monkeypatch.setattr(s, 'check_budget', lambda *args: 5)
    rt.queue_state = lambda **kw: 'state = running'
    base_api, base_pages = rt.api, rt.pages
    rt.api = lambda path: dict(id=99, path='.github/workflows/ci.yml', state='active') \
        if path.endswith('/actions/workflows/ci.yml') else base_api(path)
    rt.pages = lambda path, key: [] if '/workflows/99/runs' in path else base_pages(path, key)
    exceptions = s.read_release_nonconsumers(rt, prepared['nonconsumers'])
    guard = REAL_GUARD(rt, packet, rt.home, 99, prepared['desired'], paused=False, exceptions=exceptions)
    assert guard == dict(source='src', repository_id=1, recovery=None, budget_bytes=5) and rt.probes == [RUN]
    with pytest.raises(a.AdmissionError, match='unknown active Actions run'):
        REAL_GUARD(rt, packet, rt.home, 99, prepared['desired'], paused=False)
