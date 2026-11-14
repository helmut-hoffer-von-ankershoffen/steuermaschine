"""Batch focused admission and blocking-run report: isolated, no live GitHub, launchd or registry."""
import json
from pathlib import Path
import subprocess

import pytest
import test_native_admission_slots as test_focused_slots
from test_native_admission_slots import BASE, a, s
from steuermaschine_heinzel import state as statedir

env = test_focused_slots.env  # the shared isolated fixture: HZ_QUEUE_STATE and HOME bound to tmp_path

HEADS = ('1' * 40, '2' * 40)
TREES = ('3' * 40, '4' * 40)


def make_packet(home, name, head, tree, base=BASE):
    """A reviewed pair packet whose review artifact hash, head, base and tree are pinned."""
    pr = dict(event='pull_request', ref='refs/pull/*/merge', head=head, base=base, tree=tree)
    main = dict(event='push', ref='refs/heads/main', base=base, tree=tree)
    review = home / f'{name}.md'
    review.write_text(f'Independent scope APPROVE {name}')
    packet = dict(schema=1, manifest=pr, main_manifest=main, review=dict(
        path=review.name, sha256=s.digest(review.read_bytes()), head=head, base=base, tree=tree,
        manifest_sha256=s.digest(a.canonical(pr)), main_manifest_sha256=s.digest(a.canonical(main))))
    path = home / f'{name}.json'
    path.write_bytes(a.canonical(packet))
    return path


@pytest.fixture
def batch(env, monkeypatch):
    rt, _, _ = env
    home = rt.home
    cores = [home / 'core-a', home / 'core-b']
    for core in cores:
        core.mkdir()
    # Member A is an open PR (strict identity + holds); member B is pre-open.
    state = dict(local={str(c): h for c, h in zip(cores, HEADS)}, prheads={5: HEADS[0]}, snapshots=[], validated=[],
                 additions=[], result=[1, 'exact batch candidate'], open_prs=[])
    base_api = rt.api
    def api(path):
        if '/pulls/' in path:
            number = int(path.rsplit('/', 1)[1])
            return {'state': 'open', 'head': {'sha': state['prheads'][number]},
                    'base': {'ref': 'main', 'repo': {'full_name': rt.repo}}}
        return base_api(path)
    rt.api = api
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps(state['open_prs']).encode())
    class Member:
        """Per-checkout runtime: own core, shared fake GitHub."""
        def __init__(self, core):
            self.core, self.repo, self.management, self.cache = Path(core), rt.repo, rt.management, rt.cache
            self.api, self.run = api, run
    monkeypatch.setattr(s, 'Runtime', lambda core, home=None: rt if home is None else Member(core))
    def git(args, cwd=None, **kwargs):
        if args == ['git', 'rev-parse', 'HEAD']:
            return state['local'][str(cwd)].encode()
        assert args == ['git', 'branch', '--show-current']
        return f'owned-{Path(cwd).name}'.encode()
    monkeypatch.setattr(a, '_run', git)
    monkeypatch.setattr(a, '_read_main', lambda _: rt.main)
    def snapshot(core, base):
        work = home / f'work-{Path(core).name}'
        state['snapshots'].append((Path(core), base))
        return work, work / 'repo'
    monkeypatch.setattr(a, '_prepare_core_snapshot', snapshot)
    source = home / 'trusted-source'
    monkeypatch.setattr(a, '_canonical_source', lambda *args: (source, 'd' * 40, rt.repo, s.NAMES[0], ()))
    monkeypatch.setattr(s, 'reader_contract', lambda *args: None)
    def validate(item, snap, main, repo, var, files, archive, **kw):
        assert main == BASE and archive == source and kw['protected_producer'] is False
        state['validated'].append((item['manifest']['tree'], snap, kw['resulting_main']))
    monkeypatch.setattr(a, 'validate_canonical', validate)
    def append(archive, legacy, slots, additions):
        assert archive == source and len(slots) == 4
        state['additions'].append(dict(additions))
        if isinstance(state['result'], Exception):
            raise state['result']
        return {'result': state['result']}
    monkeypatch.setattr(a, '_isolated_slot_append', append)
    packets = [make_packet(home, 'a', HEADS[0], TREES[0]), make_packet(home, 'b', HEADS[1], TREES[1])]
    entries = [(packets[0], cores[0], 5), (packets[1], cores[1], None)]
    # The env fixture bound HZ_QUEUE_STATE to a fresh path before its mutations; live
    # admission refuses any coordinator override, so clear them only for the calls below.
    monkeypatch.delenv('HZ_QUEUE_STATE')
    for key in ('HZ_TRAIN_STATE_DIR', 'HZ_RELEASE_DIR'):
        monkeypatch.delenv(key, raising=False)
    return rt, entries, state, cores, packets


def prepare(batch):
    rt, entries, _, _, _ = batch
    return s.prepare_batch(rt, entries, rt.home, [])


def test_batch_validates_every_member_and_applies_one_slot_write(batch):
    rt, _, state, cores, _ = batch
    prepared, members, source, _, proofs = prepare(batch)
    # Each member: own snapshot of its own checkout, both canonical resolutions, no protected producer.
    assert state['snapshots'] == [(cores[0], BASE), (cores[1], BASE)]
    assert state['validated'] == [(TREES[0], rt.home / 'work-core-a/repo', False), (TREES[0], rt.home / 'work-core-a/repo', True),
                                  (TREES[1], rt.home / 'work-core-b/repo', False), (TREES[1], rt.home / 'work-core-b/repo', True)]
    assert sorted(state['additions'][0]) == sorted([f'pull_request:{HEADS[0]}', f'push-tree:{TREES[0]}',
                                                    f'pull_request:{HEADS[1]}', f'push-tree:{TREES[1]}'])
    assert prepared['mode'] == s.BATCH_MODE and prepared['slot'] == s.NAMES[2]
    assert [(m['head'], m['pr']) for m in prepared['members']] == [(HEADS[0], 5), (HEADS[1], None)]
    assert rt.events == [] and not rt.writes
    path = s.apply_batch(rt, prepared, members, source, proofs, s.digest(a.canonical(prepared)))
    assert rt.events == ['pause', 'write', 'restore']
    assert rt.writes == [(s.NAMES[2], 'exact batch candidate')]
    receipt = json.loads(path.read_text())
    assert receipt['registry_changed'] is True and receipt['mode'] == s.BATCH_MODE and len(receipt['members']) == 2
    assert path.resolve().is_relative_to(statedir.root().resolve())


@pytest.mark.parametrize('defect', ['stale-base', 'review-drift', 'local-head', 'pr-hold', 'pr-head', 'preopen-pr',
                                    'duplicate', 'single', 'overflow', 'already', 'shared-key'])
def test_batch_member_defect_refuses_whole_batch_before_any_write(batch, defect):
    rt, entries, state, cores, packets = batch
    if defect == 'stale-base':
        make_packet(rt.home, 'b', HEADS[1], TREES[1], base='e' * 40)
    if defect == 'review-drift':
        (rt.home / 'b.md').write_text('edited after review')
    if defect == 'local-head':
        state['local'][str(cores[1])] = 'e' * 40
    if defect == 'pr-hold':
        (rt.management / 'tools/pr-actions.toml').write_text('"5" = { hold = true }\n')
    if defect == 'pr-head':
        state['prheads'][5] = 'e' * 40
    if defect == 'preopen-pr':
        state['open_prs'] = [dict(number=8, state='open', head=dict(sha=HEADS[1], ref='other'))]
    if defect == 'duplicate':
        entries[1] = (packets[0], cores[1], None)
    if defect == 'single':
        del entries[1]
    if defect == 'overflow':
        state['result'] = a.AdmissionError('trusted-main slot preparation refused: all focused registry slots are full')
    if defect == 'already':
        state['result'] = None
    if defect == 'shared-key':
        make_packet(rt.home, 'b', HEADS[1], TREES[0])
    with pytest.raises(a.AdmissionError) as error:
        prepare(batch)
    expected = {'stale-base': 'stale relative to current main', 'review-drift': 'review artifact bytes',
                'local-head': 'local reviewed head', 'pr-hold': 'held', 'pr-head': 'PR identity changed',
                'preopen-pr': 'matching open PR', 'duplicate': 'distinct heads', 'single': '2..',
                'already': 'already admitted', 'shared-key': 'share a registry key'}
    if defect in expected:
        assert expected[defect] in str(error.value)
    if defect == 'overflow':
        assert 'split the batch' in str(error.value) and 'all focused registry slots are full' in str(error.value)
    assert rt.events == [] and not rt.writes


@pytest.mark.parametrize('drift', ['pr-head', 'local-head', 'main', 'unknown-run'])
def test_batch_apply_drift_of_any_member_never_writes_and_restores(batch, drift):
    rt, _, state, cores, _ = batch
    prepared, members, source, _, proofs = prepare(batch)
    if drift == 'pr-head': state['prheads'][5] = 'e' * 40
    if drift == 'local-head': state['local'][str(cores[1])] = 'e' * 40
    if drift == 'main': rt.main = 'e' * 40
    if drift == 'unknown-run': rt.rows['queued'] = [dict(id=9, status='queued')]
    with pytest.raises(a.AdmissionError):
        s.apply_batch(rt, prepared, members, source, proofs, s.digest(a.canonical(prepared)))
    assert not rt.writes and rt.events == ['pause', 'restore']


def test_batch_apply_requires_exact_approval_and_member_binding(batch):
    rt, _, _, _, _ = batch
    prepared, members, source, _, proofs = prepare(batch)
    with pytest.raises(a.AdmissionError, match='approval'):
        s.apply_batch(rt, prepared, members, source, proofs, '0' * 64)
    tampered = json.loads(a.canonical(prepared))
    tampered['members'][1]['pr'] = 6
    with pytest.raises(a.AdmissionError, match='binding'):
        s.apply_batch(rt, tampered, members, source, proofs, s.digest(a.canonical(tampered)))
    assert rt.events == [] and not rt.writes


@pytest.mark.parametrize('pairs,plans', [(2, 'dup'), (1, 'two-pairs'), (0, 'one-pair')])
def test_apply_slots_pair_count_is_exact(tmp_path, pairs, plans):
    one = [dict(event='pull_request', ref='refs/pull/*/merge', head=HEADS[0], base=BASE, tree=TREES[0]),
           dict(event='push', ref='refs/heads/main', base=BASE, tree=TREES[0])]
    two = one + [dict(one[0], head=HEADS[1], tree=TREES[1]), dict(one[1], tree=TREES[1])]
    chosen = {'dup': one + one, 'two-pairs': two, 'one-pair': one}[plans]
    with pytest.raises(a.AdmissionError):
        a.apply_slots(chosen, tmp_path, {n: '' for n in s.NAMES}, lambda: pytest.fail('read'),
                      lambda *_: pytest.fail('write'), {}, tmp_path / 'lock', tmp_path,
                      approved_candidate_sha256='0' * 64, check_fresh=lambda: None, pairs=pairs)


def test_check_consumers_names_the_exact_blocking_runs(env):
    rt, _, _ = env
    rt.rows['queued'] = [dict(id=9, status='queued', event='pull_request', head_branch='feat/x',
                              path='.github/workflows/ci.yml', head_sha='9' * 40)]
    with pytest.raises(a.AdmissionError, match=r'unknown active Actions run; admission refused: 9 queued pull_request feat/x \.github/workflows/ci\.yml'):
        s.check_consumers(rt, {})


def test_blockers_only_read_and_never_cancel(env):
    """R6 of the dod13 review: the delegation to the retired hz-ci-defer is gone; blockers reads, cancels nothing."""
    rt, _, _ = env
    rt.rows['queued'] = [dict(id=9, status='queued', event='pull_request', head_branch='feat/x', path='ci.yml', head_sha='9' * 40),
                         dict(id=10, status='queued', event='push', head_branch='main', path='ci.yml', head_sha='8' * 40)]
    calls = []
    rt.run = lambda *args, **kwargs: calls.append(args)
    assert [r['id'] for r in s.report_blockers(rt, set())['blocking']] == [9, 10]
    assert [r['id'] for r in s.report_blockers(rt, {10})['blocking']] == [9] and calls == []
    with pytest.raises(SystemExit):
        s.main(['blockers', '--core-repo', 'x', '--review-root', '.', '--cancel-blocking-pr-runs'])


@pytest.mark.parametrize('argv', [
    ['prepare', '--packets', 'a.json', 'b.json', '--core-repos', 'x', 'y', '--pr', '5'],
    ['prepare', '--packets', 'a.json', 'b.json', '--core-repos', 'x', 'y', '--reviewed-protected-producer'],
    ['prepare', '--packets', 'a.json', 'b.json', '--core-repos', 'x'],
    ['prepare', '--packets', 'a.json', 'b.json', '--core-repos', 'x', 'y', '--prs', '5'],
    ['blockers', 'a.json', '--core-repo', 'x'],
])
def test_cli_mode_conflicts_refuse_before_any_runtime(monkeypatch, argv):
    monkeypatch.setattr(s, 'Runtime', lambda *args, **kwargs: pytest.fail('runtime constructed'))
    assert s.main([*argv, '--review-root', '.']) == 1


def test_cli_batch_prepare_then_apply_exact_transaction(batch, capsys, tmp_path):
    rt, entries, _, cores, packets = batch
    argv = ['--packets', *map(str, packets), '--core-repos', *map(str, cores), '--prs', '5', '0', '--review-root', str(rt.home)]
    assert s.main(['prepare', *argv]) == 0
    printed = capsys.readouterr().out.strip()
    prepared = tmp_path / 'prepared.json'
    prepared.write_text(printed)
    approval = s.digest(a.canonical(json.loads(printed)))
    assert s.main(['apply', *argv, '--prepared', str(prepared), '--approved-sha256', approval]) == 0
    assert rt.events == ['pause', 'write', 'restore'] and len(rt.writes) == 1
