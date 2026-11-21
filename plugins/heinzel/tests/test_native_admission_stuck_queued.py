"""stuck-queued-pr-run nonconsumer receipts: isolated fake GitHub, no live reads or writes."""
import base64
import calendar
import copy
import gzip
import json
import subprocess
import time

import pytest
import test_native_admission_slots as test_focused_slots
from test_native_admission_slots import a, s
from steuermaschine_heinzel import state as statedir

env = test_focused_slots.env  # HZ_QUEUE_STATE and HOME bound to tmp_path before any mutation

STUCK_HEAD = '5' * 40
CREATED = '2026-11-11T20:20:00Z'
NOW = calendar.timegm(time.strptime(CREATED, '%Y-%m-%dT%H:%M:%SZ')) + 3600  # one hour after CREATED
IDENTITY = dict(path='.github/workflows/ci.yml', head_sha=STUCK_HEAD, head_branch='copy-standard',
                event='pull_request', run_attempt=1, workflow_id=394704163, created_at=CREATED)
REFUSAL = b'{"message":"Cannot cancel a workflow run that has not been queued yet.","status":"409"}' \
          b'gh: Cannot cancel a workflow run that has not been queued yet. (HTTP 409)\n'


def slot(registry):
    raw = json.dumps(registry, sort_keys=True, separators=(',', ':')).encode()
    return json.dumps(dict(schema=1, codec='gzip+base64', bytes=len(raw), sha256=s.digest(raw),
                           data=base64.b64encode(gzip.compress(raw, mtime=0)).decode()))


def receipt(rt, **identity):
    return dict(kind=s.STUCK_KIND, run=43095386045, repository=rt.repo, assertion=s.STUCK_ASSERTION,
                identity=dict(IDENTITY, **identity))


@pytest.fixture
def stuck(env, monkeypatch):
    rt, prepared, packet = env
    # A real compressed candidate: the guard decodes the desired registry too.
    candidate = slot({f'pull_request:{test_focused_slots.HEAD}': {}})
    monkeypatch.setattr(a, '_isolated_slot_append', lambda *args: {'result': [0, candidate]})
    prepared['desired'][s.NAMES[1]] = candidate
    prepared['candidate_sha256'] = s.digest(candidate.encode())
    run = dict(id=43095386045, status='queued', conclusion=None, repository={'full_name': rt.repo},
               run_started_at=CREATED, updated_at=CREATED, **IDENTITY)
    rt.rows['queued'] = [run]
    rt.jobs = dict(total_count=0, jobs=[])
    rt.probes = []
    rt.cancel = (1, REFUSAL)
    base_api = rt.api
    def api(path):
        if path.endswith('/jobs?per_page=100'):
            assert path == f'repos/{rt.repo}/actions/runs/{run["id"]}/jobs?per_page=100'
            return rt.jobs
        return base_api(path)
    rt.api = api
    rt.registry = lambda: {n: rt.values.get(n, '') for n in s.NAMES}
    rt.now = lambda: NOW
    def probe(run_id):
        rt.probes.append(run_id)
        rt.events.append('probe')
        return rt.cancel
    rt.cancel_probe = probe
    proofs = s.read_nonconsumers(rt, [receipt(rt)], rt.home)
    return rt, prepared, packet, run, proofs




DEFECTS = {
    'started': lambda rt, run: run.update(status='in_progress'),
    'jobs': lambda rt, run: rt.jobs.update(total_count=1, jobs=[{'id': 1}]),
    'jobs-list-only': lambda rt, run: rt.jobs.update(jobs=[{'id': 1}]),
    'jobs-count-only': lambda rt, run: rt.jobs.update(total_count=1),
    'head': lambda rt, run: run.update(head_sha='6' * 40),
    'attempt': lambda rt, run: run.update(run_attempt=2),
    'created': lambda rt, run: run.update(created_at='2026-11-11T20:21:00Z'),
    'young': lambda rt, run: setattr(rt, 'now', lambda: NOW - 31 * 60),
    'requeued-recently': lambda rt, run: run.update(run_started_at='2026-11-11T21:10:00Z'),
    'no-started-at': lambda rt, run: run.pop('run_started_at'),
    'bad-started-at': lambda rt, run: run.update(run_started_at='soon'),
    'concluded': lambda rt, run: run.update(conclusion='cancelled'),
    'repository': lambda rt, run: run.update(repository={'full_name': 'other/repo'}),
    'cancellable': lambda rt, run: setattr(rt, 'cancel', (0, b'')),
    'cancel-exit-0': lambda rt, run: setattr(rt, 'cancel', (0, REFUSAL)),
    'other-409': lambda rt, run: setattr(rt, 'cancel', (1, b'gh: Conflict (HTTP 409)')),
    'live-plan': lambda rt, run: rt.values.update({s.NAMES[0]: json.dumps({f'pull_request:{STUCK_HEAD}': {}})}),
    'live-slot-plan': lambda rt, run: rt.values.update({s.NAMES[2]: slot({f'pull_request:{STUCK_HEAD}': {}})}),
    'bad-slot': lambda rt, run: rt.values.update({s.NAMES[2]: '{"codec":"gzip+base64","bytes":3,"data":"!!"}'}),
    'new-run': lambda rt, run: rt.rows.update(queued=[run, dict(run, id=43095386046)]),
}




@pytest.mark.parametrize('defect', sorted(set(DEFECTS) - {'new-run', 'bad-slot'}))
def test_each_zombie_condition_is_checked_directly(stuck, defect):
    # Bypasses the shared inventory/identity guards so every condition carries its own weight.
    rt, prepared, _, run, _ = stuck
    pin = dict(IDENTITY)
    registries = (s.registry_text(rt.registry()), s.registry_text(prepared['desired']))
    assert s.stuck_queued_failure(rt, run, pin, registries) is None
    DEFECTS[defect](rt, run)
    registries = (s.registry_text(rt.registry()), s.registry_text(prepared['desired']))
    assert s.stuck_queued_failure(rt, run, pin, registries) is not None


def test_head_in_desired_candidate_refuses(stuck):
    rt, prepared, packet, _, proofs = stuck
    prepared = copy.deepcopy(prepared)
    prepared['desired'][s.NAMES[2]] = slot({f'pull_request:{STUCK_HEAD}': {}})
    with pytest.raises(a.AdmissionError, match='head has a focused plan'):
        s.check_consumers(rt, proofs, prepared['desired'])
    assert rt.probes == []  # the write-probe runs only after every read check passed


def test_stuck_receipt_without_desired_registry_refuses(stuck):
    rt, _, _, _, proofs = stuck
    with pytest.raises(a.AdmissionError, match='desired registry'):
        s.check_consumers(rt, proofs)


@pytest.mark.parametrize('change', [
    dict(event='schedule'), dict(event='push'), dict(path='.github/workflows/release.yml'),
    dict(head_branch='main'), dict(head_branch='release/v0.9.37'), dict(head_branch='bump/x'), dict(head_branch='refs/heads/x'),
    dict(head_sha='short'), dict(run_attempt=0), dict(created_at='yesterday'), dict(extra=1)])
def test_receipt_outside_allowlist_refuses(env, change):
    rt, _, _ = env
    with pytest.raises(a.AdmissionError):
        s.read_nonconsumers(rt, [receipt(rt, **change)], rt.home)


@pytest.mark.parametrize('mutate', ['kind', 'assertion', 'repository', 'review'])
def test_receipt_envelope_is_exact(env, mutate):
    rt, _, _ = env
    item = receipt(rt)
    if mutate == 'kind': item['kind'] = 'stuck-queued-anything'
    if mutate == 'assertion': item['assertion'] = 'trust me'
    if mutate == 'repository': item['repository'] = 'other/repo'
    if mutate == 'review': item['review'] = {}
    with pytest.raises((a.AdmissionError, KeyError)):
        s.read_nonconsumers(rt, [item], rt.home)


def test_unknown_kind_never_falls_back_to_the_closure_receipt(env):
    rt, _, _ = env
    item = dict(kind='other', run=7, repository=rt.repo,
                assertion='complete immutable source closure has no focused registry reads')
    with pytest.raises(a.AdmissionError, match='unknown nonconsumer kind'):
        s.read_nonconsumers(rt, [item], rt.home)


def test_generator_snapshots_only_harmless_runs_without_probing(stuck):
    rt, _, _, run, _ = stuck
    young = dict(run, id=43095386047, created_at='2026-11-11T21:10:00Z', run_started_at='2026-11-11T21:10:00Z')
    pushed = dict(run, id=43095386048, event='push')
    rt.rows['queued'] = [run, young, pushed]
    output, excluded = s.stuck_queued_receipts(rt, set())
    assert [r['run'] for r in output['receipts']] == [run['id']]
    assert {e['id'] for e in excluded} == {young['id'], pushed['id']} and rt.probes == []
    assert s.read_nonconsumers(rt, output['receipts'], rt.home)  # round-trips through the reader




def test_real_runtime_probe_is_one_exact_post(tmp_path, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, b'out', b'err')
    monkeypatch.setattr(s.subprocess, 'run', run)
    rt = object.__new__(s.Runtime)
    rt.repo = 'o/r'
    assert rt.cancel_probe(7) == (1, b'outerr')
    assert calls == [('gh', 'api', '-X', 'POST', 'repos/o/r/actions/runs/7/cancel')]


