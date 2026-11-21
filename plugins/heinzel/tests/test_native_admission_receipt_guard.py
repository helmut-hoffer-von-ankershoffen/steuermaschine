"""Fast stuck-queued receipt guard (admission_lib/receipt_guard.py): list rows at every guard, one full proof before the write.

The guard-level tests run the real `focused_slots.Runtime` IO (api, pages, cancel_probe) against a scripted `gh`
(`steuermaschine.github.testing.FakeGh`) holding 67 untouched zombie runs, the shape of the real ones on 2026-11-13.
The apply-level tests reuse the isolated fakes of the moved stuck-queued tests.
"""
import calendar
import json
import subprocess
import time

import pytest
import test_native_admission_stuck_queued as stuck_tests
import test_native_release_admit_stuck as release_stuck_tests
from test_native_admission_slots import a, s

from steuermaschine.github.testing import FakeGh
from steuermaschine.protocols import Result
from steuermaschine_heinzel.native.admission_lib import receipt_guard

stuck = stuck_tests.stuck  # fixture
env = stuck_tests.env  # fixture
operation = release_stuck_tests.operation  # fixture
candidate = release_stuck_tests.candidate  # fixture
release_stuck = release_stuck_tests.stuck

REPO = 'helmut-hoffer-von-ankershoffen/heinzel'
CREATED = '2026-11-11T20:20:00Z'
NOW = calendar.timegm(time.strptime(CREATED, '%Y-%m-%dT%H:%M:%SZ')) + 2 * 86400
FIRST = 43095386000
ZOMBIES = 67
REFUSAL = 'gh: Cannot cancel a workflow run that has not been queued yet. (HTTP 409)'


def zombie(run_id):
    return dict(id=run_id, status='queued', conclusion=None, repository={'full_name': REPO}, event='pull_request',
                path='.github/workflows/ci.yml', head_sha=f'{run_id:040d}', head_branch=f'fix/z{run_id}', run_attempt=1,
                workflow_id=394704163, created_at=CREATED, updated_at=CREATED, run_started_at=CREATED, referenced_workflows=[])


def receipt(row):
    return dict(kind=s.STUCK_KIND, run=row['id'], repository=REPO, assertion=s.STUCK_ASSERTION,
                identity={k: row[k] for k in s.STUCK_IDENTITY})


class GhRuntime(s.Runtime):
    """The real Runtime methods; only the process boundary answers from FakeGh."""
    def __init__(self, gh):
        self.repo, self.gh = REPO, gh

    def run(self, *args, data=None, check=True, timeout=30):
        assert args[0] == 'gh' and data is None, args
        result = self.gh.run(list(args))
        if check and result.rc:
            raise a.AdmissionError('gh operation failed; inspect outcome before retry')
        return subprocess.CompletedProcess(args, result.rc, result.out.encode(), result.err.encode())

    def now(self):
        return NOW


@pytest.fixture
def world():
    rows = [zombie(FIRST + i) for i in range(ZOMBIES)]
    gh = FakeGh()
    root = f'api repos/{REPO}'
    gh.on(root, dict(full_name=REPO, name='heinzel', owner=dict(login='helmut-hoffer-von-ankershoffen', type='User')))
    gh.on(f'{root}/actions/variables', dict(total_count=0, variables=[]))
    for state in s.ACTIVE:
        gh.on(f'{root}/actions/runs?status={state}', lambda argv, state=state: dict(
            total_count=len([r for r in rows if r['status'] == state]), workflow_runs=[r for r in rows if r['status'] == state]))
    gh.on(f'{root}/actions/runs/', lambda argv: dict(total_count=0, jobs=[]) if argv[2].endswith('/jobs?per_page=100')
          else next(r for r in rows if r['id'] == int(argv[2].rsplit('/', 1)[1])))
    gh.on('/cancel', Result(1, '', REFUSAL))
    rt = GhRuntime(gh)
    proofs = {row['id']: receipt(row) for row in rows}
    desired = {name: '' for name in s.NAMES}
    return rt, gh, rows, proofs, desired


def per_run(gh):
    return [line for line in gh.lines() if '/actions/runs/' in line]


def test_untouched_receipts_are_proved_from_the_list_rows_alone(world):
    rt, gh, _, proofs, desired = world
    s.check_consumers(rt, proofs, desired, full=False)
    lines = gh.lines()
    # One queued list call per inventory scan (the two scans the guard always made); not one call per run.
    assert sum('actions/runs?status=queued' in line for line in lines) == 2
    assert per_run(gh) == [] and gh.writes() == []
    assert len(lines) == 2 * len(s.ACTIVE) + 2  # the inventory scans + the live registry (identity, variables)


def test_the_full_proof_is_todays_per_run_proof(world):
    rt, gh, _, proofs, desired = world
    s.check_consumers(rt, proofs, desired, full=True)
    calls = per_run(gh)
    assert len(calls) == 3 * ZOMBIES  # run GET, jobs GET, cancel POST per zombie (on bounded threads)
    assert len(gh.writes()) == ZOMBIES and all(line.endswith('/cancel') for line in gh.writes())


@pytest.mark.parametrize('change', [
    dict(updated_at='2026-11-13T09:00:00Z'), dict(status='in_progress'), dict(run_started_at='2026-11-12T20:20:00Z'),
    dict(run_attempt=2), dict(conclusion='cancelled'), dict(head_sha='6' * 40), dict(created_at='2026-11-11T20:21:00Z'),
    dict(repository={'full_name': 'other/repo'})])
def test_a_receipt_run_that_started_or_changed_refuses(world, change):
    rt, gh, rows, proofs, desired = world
    rows[17].update(change)
    with pytest.raises(a.AdmissionError):
        s.check_consumers(rt, proofs, desired, full=False)
    assert gh.writes() == []


def test_a_vanished_receipt_run_refuses(world):
    rt, _, rows, proofs, desired = world
    gone = rows.pop(5)
    with pytest.raises(a.AdmissionError, match=f"{gone['id']} no longer harmless: run vanished"):
        s.check_consumers(rt, proofs, desired, full=False)


def test_a_new_uncovered_queued_run_refuses(world):
    rt, gh, rows, proofs, desired = world
    rows.append(zombie(FIRST + ZOMBIES))
    with pytest.raises(a.AdmissionError, match='unknown active Actions run'):
        s.check_consumers(rt, proofs, desired, full=False)
    assert per_run(gh) == []


def test_a_receipt_head_with_a_plan_refuses(world):
    rt, _, rows, proofs, desired = world
    desired = dict(desired, FOCUSED_CI_PLANS=json.dumps({f"pull_request:{rows[3]['head_sha']}": {}}))
    with pytest.raises(a.AdmissionError, match='head has a focused plan'):
        s.check_consumers(rt, proofs, desired, full=False)


def test_pre_write_marks_only_the_second_guard_before_a_write():
    written = [False]
    full = receipt_guard.pre_write(lambda: written[0])
    first, second = full(), full()
    written[0] = True
    assert (first, second, full()) == (False, True, False)


def test_parallel_raises_the_first_refusal_in_order_after_all_ran():
    ran = []

    def work(item):
        ran.append(item)
        if item in (3, 6):
            raise a.AdmissionError(f'refused {item}')
        return item
    with pytest.raises(a.AdmissionError, match='refused 3'):
        receipt_guard.parallel(work, range(10))
    assert sorted(ran) == list(range(10))
    assert receipt_guard.parallel(work, [1, 2]) == [1, 2]


def counting(rt):
    """Log every per-run read and probe into rt.events, next to the fixture's 'pause', 'write', 'restore'."""
    api = rt.api

    def logged(path):
        if '/actions/runs/' in path:
            rt.events.append('jobs' if path.endswith('/jobs?per_page=100') else 'run')
        return api(path)
    rt.api = logged






def test_pr_prepare_guard_is_list_only(stuck):
    rt, prepared, _, _, proofs = stuck
    counting(rt)
    s.global_guard(rt, prepared['desired'], proofs, paused=False)
    assert not any(e in ('run', 'jobs', 'probe') for e in rt.events)


def test_release_apply_probes_once_before_the_write(release_stuck):
    rt, _, _, apply, _ = release_stuck
    jobs = []
    api = rt.api
    rt.api = lambda path: (jobs.append(len(rt.writes)) if path.endswith('/jobs?per_page=100') else None) or api(path)
    probe = rt.cancel_probe
    seen = []
    rt.cancel_probe = lambda run_id: seen.append(len(rt.writes)) or probe(run_id)
    apply()
    assert jobs == [0] and seen == [0] and len(rt.writes) == 1  # one full proof, before the one write; none after


def test_release_pre_write_proof_catches_jobs(release_stuck):
    rt, _, _, apply, _ = release_stuck
    rt.jobs.update(total_count=1)
    with pytest.raises(a.AdmissionError):
        apply()
    assert not rt.writes and rt.probes == []

