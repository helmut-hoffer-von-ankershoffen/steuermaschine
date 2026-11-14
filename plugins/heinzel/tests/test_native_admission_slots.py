"""Isolated operating proof: no live GitHub, launchd, queue or release state."""
from contextlib import contextmanager
import hashlib
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from admission_paths import TOOLS
# (moved: the package is imported, no path insert)
from steuermaschine_heinzel.native.admission_lib import focused_slots as s
from steuermaschine_heinzel import state as statedir

a = s.a
HEAD, BASE, TREE = ('a' * 40, 'b' * 40, 'c' * 40)
PLANS = [dict(event='pull_request', ref='refs/pull/*/merge', head=HEAD, base=BASE, tree=TREE),
         dict(event='push', ref='refs/heads/main', base=BASE, tree=TREE)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Explicit owner state binding before any fixture mutation.
    queue = tmp_path / 'queue.json'
    monkeypatch.setenv('HZ_QUEUE_STATE', str(queue))
    assert Path(s.os.environ['HZ_QUEUE_STATE']).resolve() == queue.resolve()
    monkeypatch.setenv('HOME', str(tmp_path))
    class Fake:
        core = tmp_path / 'core'
        home = tmp_path
        cache = tmp_path / '.cache'
        management = tmp_path / 'management'
        repo = s.hzlib.CORE
        held_version_locks = None
        def __init__(self):
            self.core.mkdir(); self.cache.mkdir()
            (self.management / 'tools').mkdir(parents=True)
            (self.management / 'tools/pr-actions.toml').write_text('"5" = { hold = false }\n')
            self.values = {s.NAMES[0]: '{}'}
            self.org = {}; self.rows = {}; self.main = BASE; self.prhead = HEAD
            self.paused = False; self.writes = []; self.events = []; self.defect = None
        def variables(self):
            return dict(self.values), dict(self.org)
        def pages(self, path, key):
            return self.rows.get(path.split('status=')[-1], [])
        def api(self, path):
            if path.endswith('heads/main'): return {'object': {'sha': self.main}}
            if '/actions/runs/' in path:
                return next(row for rows in self.rows.values() for row in rows if row['id']==int(path.rsplit('/',1)[1]))
            return {'state':'open', 'head':{'sha':self.prhead}, 'base':{'ref':'main','repo':{'full_name':self.repo}}}
        def queue_state(self): return None if self.paused else 'state = running'
        def set_variable(self, name, value):
            self.events.append('write'); self.writes.append((name, value)); self.values[name] = value
            if self.defect == 'readback': self.values[name] += ' '
            if self.defect == 'other': self.values[s.NAMES[4]] = 'unexpected'
            if self.defect == 'main-after': self.main = 'd' * 40
        @contextmanager
        def exclusions(self):
            self.events.append('pause'); self.paused = True; self.held_version_locks = []
            try: yield
            finally:
                self.paused = False; self.events.append('restore'); self.held_version_locks = None
                if self.defect == 'restore': raise a.AdmissionError('restore failed')
    rt = Fake()
    def git(args, **kwargs):
        assert args == ['git', 'rev-parse', 'HEAD']
        return HEAD.encode()
    monkeypatch.setattr(a, '_run', git)
    def append(source, legacy, slots, additions):
        assert source == rt.core and legacy == '{}'
        assert tuple(additions) == (f'pull_request:{HEAD}', f'push-tree:{TREE}')
        return {'result': [0, 'exact compressed candidate']}
    monkeypatch.setattr(a, '_isolated_slot_append', append)
    prepared = dict(schema=1, head=HEAD, expected={n: rt.values.get(n,'') for n in s.NAMES},
        desired={n: rt.values.get(n,'') for n in s.NAMES}, presence=[s.NAMES[0]], slot=s.NAMES[1],
        candidate_sha256=s.digest(b'exact compressed candidate'), pr=5, packet_sha256='f'*64)
    prepared['desired'][s.NAMES[1]] = 'exact compressed candidate'
    # moved: fresh copies, so a test that edits its packet cannot change the shared PLANS of later tests
    return rt, prepared, {'manifest':copy.deepcopy(PLANS[0]), 'main_manifest':copy.deepcopy(PLANS[1])}


def apply(env, approval=None):
    rt, prepared, packet = env
    return s.apply_prepared(rt, prepared, packet, rt.core, {}, approval or s.digest(a.canonical(prepared)))


def test_full_operating_proof_one_slot_append_all_state_retained_and_restored(env):
    rt, prepared, _ = env
    path = apply(env)
    assert rt.events == ['pause','write','restore']
    assert rt.writes == [(s.NAMES[1], prepared['desired'][s.NAMES[1]])]
    assert rt.values[s.NAMES[0]] == '{}'
    receipt = json.loads(path.read_text())
    assert receipt['registry_changed'] is True and not rt.paused
    assert set(receipt['after']) == set(s.NAMES)


@pytest.mark.parametrize('defect', ['main','pr','freeze','holds','prhold','queue','new-lock','budget','org','presence','unknown-run'])
def test_guard_failures_do_not_write_and_restore(env, defect):
    rt, prepared, _ = env
    if defect == 'main': rt.main = 'e'*40
    if defect == 'pr': rt.prhead = 'e'*40
    if defect == 'freeze': (rt.cache/'merge-freeze').touch()
    if defect == 'holds':
        (rt.cache/'release-holds').mkdir();(rt.cache/'release-holds/x').touch()
    if defect == 'prhold': (rt.management/'tools/pr-actions.toml').write_text('"5"={hold=true}')
    if defect == 'queue': rt.queue_state = lambda: 'unexpected loaded'
    if defect == 'new-lock':
        (rt.cache/'release').mkdir();(rt.cache/'release/v0.9.37.lock').touch()
    if defect == 'budget': rt.values.update({f'BIG_{i}':'x'*45000 for i in range(6)})
    if defect == 'org': rt.org[s.NAMES[1]] = ''
    if defect == 'presence': rt.values[s.NAMES[3]] = ''
    if defect == 'unknown-run': rt.rows['queued'] = [{'id':9, 'status':'queued'}]
    with pytest.raises(a.AdmissionError): apply(env)
    assert not rt.writes and rt.events[-1] == 'restore'
    assert not list(statedir.root().glob('focused-admission-receipts/*.json'))


@pytest.mark.parametrize('defect', ['readback','other','main-after','restore'])
def test_after_write_mismatch_has_no_rollback_or_success(env, defect):
    rt, _, _ = env; rt.defect=defect
    with pytest.raises(a.AdmissionError): apply(env)
    assert len(rt.writes)==1 and rt.events[-1]=='restore'
    if defect != 'restore': assert not list(statedir.root().glob('focused-admission-receipts/*.json'))
    # A restore failure can occur after an immutable write receipt. It must still fail the operation.


def test_approval_fails_before_process_mutation(env):
    rt, _, _ = env
    with pytest.raises(a.AdmissionError): apply(env, '0'*64)
    assert rt.events == []


def test_exact_nonconsumer_exception_and_unknown_run(env):
    rt, _, _ = env
    identity=dict(path='.github/workflows/docs.yml',head_sha=BASE,head_branch='main',event='schedule',run_attempt=1,workflow_id=8)
    run=dict(id=7,status='in_progress',repository={'full_name':rt.repo},referenced_workflows=[],**identity)
    proof={7:dict(identity=identity,referenced_workflows=[])}
    rt.rows['in_progress']=[run]
    s.check_consumers(rt,proof)
    rt.rows['queued']=[dict(id=9,status='queued')]
    with pytest.raises(a.AdmissionError):s.check_consumers(rt,proof)


@pytest.mark.parametrize('key', ['path','head_sha','head_branch','event','run_attempt','workflow_id','referenced_workflows','repository'])
def test_nonconsumer_wrong_exact_identity_denied(env,key):
    rt,_,_=env
    identity=dict(path='.github/workflows/docs.yml',head_sha=BASE,head_branch='main',event='schedule',run_attempt=1,workflow_id=8)
    run=dict(id=7,status='waiting',repository={'full_name':rt.repo},referenced_workflows=[],**identity)
    proof={7:dict(identity=dict(identity),referenced_workflows=[])}
    run[key] = 2 if key in ('run_attempt','workflow_id') else 'wrong'
    rt.rows['waiting']=[run]
    with pytest.raises(a.AdmissionError):s.check_consumers(rt,proof)


def test_pagination_requires_complete_consistent_total(env):
    rt,_,_=env
    pages=[dict(total_count=31,variables=[{}]*30),dict(total_count=31,variables=[{}])]
    rt.api=lambda _:pages.pop(0)
    assert len(s.Runtime.pages(rt,'endpoint','variables'))==31
    for pages in ([dict(total_count=31,variables=[])],
                  [dict(total_count=31,variables=[{}]*30),dict(total_count=32,variables=[{},{}])]):
        rt.api=lambda _:pages.pop(0)
        with pytest.raises(a.AdmissionError):s.Runtime.pages(rt,'endpoint','variables')


def test_organization_budget_is_counted_even_when_shadowed(env):
    rt,prepared,_=env
    rt.org={f'BIG_{i}':'x'*45000 for i in range(6)}
    with pytest.raises(a.AdmissionError):s.check_budget(rt,prepared['desired'])


def test_all_reader_injections_required(env,monkeypatch):
    rt,_,_=env
    def read(args,**kw):
        path=args[-1].split(':',1)[1]
        if path.endswith('focused_gate.py'):return b'registry_slots selected_registry'
        count=2 if path.endswith('ci.yml') else 1
        return ('\n'.join(f'{n}: ${{{{ vars.{n} }}}}' for n in s.NAMES)*count).encode()
    monkeypatch.setattr(a,'_run',read);s.reader_contract(rt.core,BASE)
    monkeypatch.setattr(a,'_run',lambda *args,**kw:b'FOCUSED_CI_PLANS: old')
    with pytest.raises(a.AdmissionError):s.reader_contract(rt.core,BASE)


def test_component_has_no_generic_write_entry():
    # moved: the component has no entry at all any more (it used to refuse with 'Component only'); running it does nothing
    result=subprocess.run([sys.executable,'-m','steuermaschine_heinzel.native.admission_lib.focused_admit','--apply'],capture_output=True)
    assert result.returncode==0 and not result.stdout  # (stderr: only the interpreter's runpy notice)
    assert '__main__' not in Path(a.__file__).read_text()


def test_pause_failure_always_attempts_restore(env,monkeypatch):
    rt,_,_=env;rt.domain='gui/1';rt.service='gui/1/queue';rt.plist=rt.home/'queue.plist'
    rt.plist.write_text('reviewed original plist')
    rt.queue_state=lambda:'state = running'
    rt.run=lambda *args,**kw:(_ for _ in ()).throw(a.AdmissionError('ambiguous bootout'))
    rt.restore_queue=lambda:rt.events.append('verified restore')
    with pytest.raises(a.AdmissionError):
        with s.Runtime.exclusions(rt):pass
    assert rt.events==['verified restore']


def test_alias_pr_hold_cannot_be_skipped(env):
    rt,_,_=env
    (rt.management/'tools/pr-actions.toml').write_text('"5"={hold=false}\n"#5"={hold=true}')
    with pytest.raises(a.AdmissionError): apply(env)
    assert not rt.writes


def test_preopen_refuses_matching_open_pr(env,monkeypatch):
    rt,_,packet=env
    monkeypatch.setattr(a,'_run',lambda *args,**kwargs:b'owned-branch')
    rt.run=lambda *args,**kwargs:subprocess.CompletedProcess(args,0,json.dumps([dict(number=5,state='open',head=dict(sha=HEAD,ref='owned-branch'))]).encode())
    with pytest.raises(a.AdmissionError):s.check_preopen(rt,packet['manifest'])


def test_active_status_transition_detected_across_membership_passes(env):
    rt,_,_=env;calls=[]
    def pages(path,key):
        calls.append(path)
        return [dict(id=9,status='queued')] if len(calls)==6 else []
    rt.pages=pages
    with pytest.raises(a.AdmissionError,match='membership drift'):s.check_consumers(rt,{})


def test_symlink_installed_entry_supports_help(tmp_path,capsys):
    # moved: there is no installed script any more; the argv form answers --help in process
    with pytest.raises(SystemExit) as stop: s.main(['--help'])
    assert stop.value.code==0 and 'prepare' in capsys.readouterr().out


def test_module_entry_is_not_a_second_door_to_the_write():
    # moved: the module has no entry; `python -m` on it imports it and does nothing. The commands are the only door.
    for argv in (['apply','x'],['prepare','x'],['--help']):
        result=subprocess.run([sys.executable,'-m','steuermaschine_heinzel.native.admission_lib.focused_slots',*argv],capture_output=True)
        assert result.returncode==0 and not result.stdout  # (stderr: only the interpreter's runpy notice)
    assert '__main__' not in Path(s.__file__).read_text()


def test_prepare_uses_trusted_main_and_both_canonical_pair_validations(env,monkeypatch):
    rt,_,packet=env
    # Fixture state mutations above were explicitly isolated; all operational IO below is replaced.
    monkeypatch.delenv('HZ_QUEUE_STATE')
    for key in ('HZ_TRAIN_STATE_DIR','HZ_RELEASE_DIR'):monkeypatch.delenv(key,raising=False)
    review=rt.home/'review.md';review.write_text('Independent source APPROVE')
    packet['schema']=1
    packet['review']=dict(path='review.md',sha256=s.digest(review.read_bytes()),head=HEAD,base=BASE,tree=TREE,
        manifest_sha256=s.digest(a.canonical(packet['manifest'])),main_manifest_sha256=s.digest(a.canonical(packet['main_manifest'])))
    path=rt.home/'packet.json';path.write_bytes(a.canonical(packet))
    work=rt.home/'workspace';work.mkdir();snapshot=work/'snapshot';snapshot.mkdir();source=rt.home/'trusted-source';source.mkdir()
    monkeypatch.setattr(a,'_read_main',lambda _:BASE)
    monkeypatch.setattr(a,'_prepare_core_snapshot',lambda *args:(work,snapshot))
    monkeypatch.setattr(a,'_canonical_source',lambda *args:(source,'d'*40,rt.repo,s.NAMES[0],()))
    monkeypatch.setattr(a,'_repository_from_remote',lambda _:rt.repo)
    monkeypatch.setattr(s,'reader_contract',lambda *args:None)
    seen=[]
    def validate(candidate,repo,main,slug,var,protected,archive,**kw):
        assert repo==snapshot and main==BASE and archive==source
        assert kw['protected_producer'] is True
        seen.append(kw['resulting_main'])
    monkeypatch.setattr(a,'validate_canonical',validate)
    def append(archive,legacy,slots,additions):
        assert archive==source and len(slots)==4 and len(additions)==2
        return dict(result=[0,'exact compressed candidate'])
    monkeypatch.setattr(a,'_isolated_slot_append',append)
    prepared,*_=s.prepare(rt,path,rt.home,protected=True,pr=5)
    assert seen==[False,True] and prepared['main']==BASE and prepared['protected_producer'] is True
    assert 'pr_update' not in prepared
    assert not rt.writes and rt.events==[]


def test_trusted_codec_subprocess_does_not_import_candidate(tmp_path,monkeypatch):
    trusted=tmp_path/'trusted';(trusted/'scripts').mkdir(parents=True)
    (trusted/'scripts/focused_plan.py').write_text('''SLOT_VARIABLES=tuple(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4))
MAX_SLOT=40000
def append_slot(legacy,slots,additions):
    assert len(slots)==4
    return 2,"trusted-result"
''')
    poison=tmp_path/'candidate';(poison/'scripts').mkdir(parents=True)
    (poison/'scripts/focused_plan.py').write_text('raise RuntimeError("candidate imported")')
    monkeypatch.chdir(poison)
    result=a._isolated_slot_append(trusted,'{}',('','','',''),{'example':{}})
    assert result['result']==[2,'trusted-result']


@pytest.mark.parametrize('defect',['review','blob','focused','event','symlink'])
def test_reviewed_nonconsumer_source_closure_refuses_drift(env,monkeypatch,defect):
    rt,_,_=env
    (rt.home/'proof.md').write_text('Exact independent immutable workflow closure review')
    identity=dict(path='.github/workflows/docs.yml',head_sha=BASE,head_branch='main',event='schedule',run_attempt=1,workflow_id=8)
    receipt=dict(run=7,repository=rt.repo,assertion='complete immutable source closure has no focused registry reads',
        review=dict(path='proof.md',sha256=s.digest((rt.home/'proof.md').read_bytes())),identity=identity,
        source={identity['path']:'d'*40},referenced_workflows=[])
    if defect=='review':receipt['review']['sha256']='0'*64
    if defect=='event':receipt['identity']['event']='pull_request'
    def git(args,**kw):
        if args[1]=='fetch':return b''
        if args[1]=='rev-parse':return ('e'*40 if defect=='blob' else 'd'*40).encode()
        if args[1]=='ls-tree':return b'120000 blob id\tpath' if defect=='symlink' else b'100644 blob id\tpath'
        if args[1]=='show':return b'FOCUSED_CI_PLANS' if defect=='focused' else b'echo docs'
        raise AssertionError(args)
    monkeypatch.setattr(a,'_run',git)
    with pytest.raises(a.AdmissionError):s.read_nonconsumers(rt,[receipt],rt.home)


def test_release_tail_lock_is_in_exclusion_inventory(env):
    rt,_,_=env
    (rt.cache/'release').mkdir();tail=rt.cache/'release/v0.9.36.lock';tail.touch()
    assert s.version_locks(rt.cache)==[tail]


@pytest.mark.parametrize('defect', ['none', 'readback', 'main-after', 'restore-drift'])
def test_actual_exclusions_and_restoration_around_slot_operation(env, defect):
    rt,_,_=env
    rt.domain='gui/fixture';rt.service='gui/fixture/queue';rt.plist=rt.home/'original-queue.plist'
    rt.plist.write_text('reviewed original queue configuration')
    loaded=True
    def launch(*args,check=True,**kw):
        nonlocal loaded
        assert args[0]=='launchctl'
        operation=args[1]
        if operation=='print':
            return subprocess.CompletedProcess(args,0 if loaded else 113,
                f'path = {rt.plist}\nstate = running'.encode() if loaded else b'',
                b'' if loaded else b'Could not find service')
        rt.events.append(operation)
        if operation=='bootout':loaded=False
        elif operation=='bootstrap':loaded=True
        else:raise AssertionError(args)
        return subprocess.CompletedProcess(args,0,b'',b'')
    rt.run=launch
    rt.queue_state=lambda **kw:s.Runtime.queue_state(rt,**kw)
    rt.wait_queue_pause=lambda:s.Runtime.wait_queue_pause(rt)
    rt.restore_queue=lambda:s.Runtime.restore_queue(rt)
    rt.exclusions=lambda:s.Runtime.exclusions(rt)
    original=rt.set_variable
    def write(name,value):
        original(name,value)
        if defect=='restore-drift':rt.plist.write_text('changed unapproved queue configuration')
    rt.set_variable=write
    rt.defect=defect
    if defect=='none':
        apply(env)
        assert loaded and rt.events==['bootout','write','bootstrap']
    else:
        with pytest.raises(a.AdmissionError):apply(env)
        assert len(rt.writes)==1
        if defect=='restore-drift':
            assert not loaded and rt.events==['bootout','write']
        else:
            assert loaded and rt.events[-1]=='bootstrap'


def test_unreadable_version_inventory_cannot_be_treated_as_empty(env,monkeypatch):
    rt,_,_=env
    original=Path.iterdir
    def unreadable(path):
        if path==rt.cache/'release':raise PermissionError('unknown release tails')
        return original(path)
    monkeypatch.setattr(Path,'iterdir',unreadable)
    with pytest.raises(PermissionError):s.version_locks(rt.cache)


@pytest.mark.parametrize('owner_type', ['User','Organization'])
def test_variable_inventory_uses_authenticated_owner_type(env,owner_type):
    rt,_,_=env;calls=[]
    owner,name=rt.repo.split('/')
    rt.api=lambda path:dict(full_name=rt.repo,name=name,owner=dict(login=owner,type=owner_type))
    def pages(path,key):
        calls.append(path)
        return [dict(name='REPO_VALUE',value='value')] if path.endswith('/variables') else [dict(name='ORG_VALUE',value='inherited')]
    rt.pages=pages
    repo,org=s.Runtime.variables(rt)
    assert repo=={'REPO_VALUE':'value'}
    assert org==({'ORG_VALUE':'inherited'} if owner_type=='Organization' else {})
    assert len(calls)==(2 if owner_type=='Organization' else 1)
    if owner_type=='User':assert not any('organization-variables' in path for path in calls)


@pytest.mark.parametrize('defect', ['missing-owner','missing-type','unknown-type','wrong-login','wrong-repo','wrong-name','api-error'])
def test_owner_identity_errors_refuse_before_variable_inventory(env,defect):
    rt,_,_=env;calls=[];owner,name=rt.repo.split('/')
    identity=dict(full_name=rt.repo,name=name,owner=dict(login=owner,type='User'))
    if defect=='missing-owner':identity.pop('owner')
    if defect=='missing-type':identity['owner'].pop('type')
    if defect=='unknown-type':identity['owner']['type']='Bot'
    if defect=='wrong-login':identity['owner']['login']='other'
    if defect=='wrong-repo':identity['full_name']='other/repo'
    if defect=='wrong-name':identity['name']='other'
    def api(path):
        if defect=='api-error':raise a.AdmissionError('HTTP403')
        return identity
    rt.api=api;rt.pages=lambda *args:calls.append(args)
    with pytest.raises(a.AdmissionError):s.Runtime.variables(rt)
    assert calls==[]


@pytest.mark.parametrize('owner_type,error', [('Organization','HTTP422'),('Organization','HTTP403'),('User','HTTP403')])
def test_required_variable_inventory_errors_are_not_empty_data(env,owner_type,error):
    rt,_,_=env;owner,name=rt.repo.split('/')
    rt.api=lambda path:dict(full_name=rt.repo,name=name,owner=dict(login=owner,type=owner_type))
    def pages(path,key):
        if owner_type=='Organization' and path.endswith('/variables'):return []
        raise a.AdmissionError(error)
    rt.pages=pages
    with pytest.raises(a.AdmissionError,match=error):s.Runtime.variables(rt)


def test_owner_identity_is_fresh_for_every_variable_read(env):
    rt,_,_=env;owner,name=rt.repo.split('/');kinds=iter(['User','Organization']);calls=[]
    rt.api=lambda path:dict(full_name=rt.repo,name=name,owner=dict(login=owner,type=next(kinds)))
    def pages(path,key):
        calls.append(path)
        return []
    rt.pages=pages
    assert s.Runtime.variables(rt)==({}, {})
    assert s.Runtime.variables(rt)==({}, {})
    assert sum('organization-variables' in path for path in calls)==1


@pytest.mark.parametrize('mode',['delayed','persistent','identity','error','transport-timeout','late-absence'])
def test_bounded_pause_observation_before_actual_exclusion_operation(env,monkeypatch,mode):
    rt,_,_=env
    rt.domain='gui/fixture';rt.service='gui/fixture/queue';rt.plist=rt.home/'original-queue.plist'
    rt.plist.write_text('reviewed original queue configuration')
    clock=[0.0];requested=False;loaded=True;polls=0;failed=False;absence=False;restoring=False;timeouts=[]
    monkeypatch.setattr(s.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(s.time,'sleep',lambda delay:clock.__setitem__(0,clock[0]+delay))
    original_lock=a._lock
    @contextmanager
    def lock(path):
        assert absence, 'a lock was acquired before authenticated pause proof'
        rt.events.append('lock:'+path.name)
        with original_lock(path) as handle:yield handle
    monkeypatch.setattr(a,'_lock',lock)
    def launch(*args,check=True,timeout=30,**kw):
        nonlocal requested,loaded,polls,failed,absence
        assert args[0]=='launchctl'
        op=args[1]
        if op=='print':
            assert args[2]==rt.service
            if requested and not absence and not failed and not restoring:
                assert 0 < timeout <= 3
                timeouts.append(timeout);polls+=1
                assert not rt.writes
                if mode in ('identity','error','transport-timeout'):
                    failed=True
                    if mode=='transport-timeout':raise subprocess.TimeoutExpired(args,timeout)
                    if mode=='error':return subprocess.CompletedProcess(args,5,b'',b'permission denied')
                    return subprocess.CompletedProcess(args,0,b'path = /wrong/plist\nstate = running',b'')
                if mode=='late-absence':
                    clock[0]+=9;failed=True;loaded=False
                elif mode=='delayed' and polls>=3:
                    loaded=False;absence=True;rt.events.append('proven-absent')
                elif mode=='persistent' and clock[0]>=7.9:failed=True
            return subprocess.CompletedProcess(args,0 if loaded else 113,
                f'path = {rt.plist}\nstate = running'.encode() if loaded else b'',
                b'' if loaded else b'Could not find service')
        rt.events.append(op)
        if op=='bootout':
            assert not requested;requested=True
        elif op=='bootstrap':loaded=True
        else:raise AssertionError(args)
        return subprocess.CompletedProcess(args,0,b'',b'')
    rt.run=launch
    rt.queue_state=lambda **kw:s.Runtime.queue_state(rt,**kw)
    rt.wait_queue_pause=lambda:s.Runtime.wait_queue_pause(rt)
    def restore():
        nonlocal restoring
        restoring=True
        return s.Runtime.restore_queue(rt)
    rt.restore_queue=restore
    rt.exclusions=lambda:s.Runtime.exclusions(rt)
    if mode=='delayed':
        apply(env)
        assert len(rt.writes)==1 and loaded and polls==3
        assert rt.events.index('proven-absent') < rt.events.index('lock:queue.lock') < rt.events.index('write')
        assert rt.events[-1]=='bootstrap'
    else:
        with pytest.raises((a.AdmissionError,subprocess.TimeoutExpired)):apply(env)
        assert not rt.writes and loaded
        assert not any(event.startswith('lock:') for event in rt.events)
        if mode=='persistent':assert 8 <= clock[0] < 8.3
    assert rt.events.count('bootout')==1
    assert timeouts and all(0 < timeout <= 3 for timeout in timeouts)


def test_pause_refuses_plist_drift_even_with_absent_response(env):
    rt,_,_=env
    rt.plist=rt.home/'queue.plist';rt.plist.write_text('original')
    rt.queue_baseline=rt.plist.read_bytes()
    def absent(**kwargs):
        rt.plist.write_text('changed unapproved configuration')
        return None
    rt.queue_state=absent
    with pytest.raises(a.AdmissionError,match='plist changed'):s.Runtime.wait_queue_pause(rt)
    assert not rt.writes


@pytest.fixture
def update_env(env, monkeypatch):
    """Actual OLD→NEW Git history; only the authenticated network transport is local."""
    rt, prepared, packet = env
    # Clear caller Git context before any fixture Git operation, including hooks.
    for key in list(s.os.environ):
        if key.startswith('GIT_'):
            monkeypatch.delenv(key)
    def git(core, *args):
        return subprocess.check_output(['git','-C',str(core),*args],stderr=subprocess.PIPE).decode().strip()
    git(rt.core,'init','-b','main')
    git(rt.core,'config','user.name','Fixture')
    git(rt.core,'config','user.email','fixture@example.invalid')
    (rt.core/'file').write_text('old\n')
    git(rt.core,'add','file');git(rt.core,'commit','-m','old')
    old = git(rt.core,'rev-parse','HEAD')
    branch = 'fix/reviewed-update'
    git(rt.core,'switch','-c',branch)
    remote = rt.home/'official.git'
    subprocess.check_output(['git','clone','--bare',str(rt.core),str(remote)],stderr=subprocess.PIPE)
    (rt.core/'file').write_text('new\n')
    git(rt.core,'commit','-am','new')
    new, tree = git(rt.core,'rev-parse','HEAD'), git(rt.core,'rev-parse','HEAD^{tree}')
    official = 'https://github.com/'+rt.repo+'.git'
    git(rt.core,'remote','add','origin',official)
    work = rt.home/'workspace';work.mkdir();snapshot=work/'repo'
    subprocess.check_output(['git','clone','--shared','--no-checkout',str(rt.core),str(snapshot)],stderr=subprocess.PIPE)
    git(snapshot,'remote','set-url','origin',official)
    source=rt.home/'trusted-source';source.mkdir()
    prepared['head']=new
    packet['manifest'].update(head=new,base=old,tree=tree)
    packet['main_manifest'].update(base=old,tree=tree)
    rt.main=old
    identity={'id':1234,'full_name':rt.repo,'fork':False}
    pull={'id':5678,'number':5,'state':'open','head':{'sha':old,'ref':branch,'repo':dict(identity)},
          'base':{'sha':old,'ref':'main','repo':dict(identity)}}
    ref={'ref':'refs/heads/'+branch,'object':{'type':'commit','sha':old}}
    api_calls=[]
    original_api=rt.api
    def api(path):
        api_calls.append(path)
        if path == 'repos/'+rt.repo:return copy.deepcopy(identity)
        if path == f'repos/{rt.repo}/pulls/5':return copy.deepcopy(pull)
        if path == f'repos/{rt.repo}/git/ref/heads/fix%2Freviewed-update':return copy.deepcopy(ref)
        return original_api(path)
    rt.api=api
    commands=[]
    original_git=s.update._git
    def proof_git(core,*args,**kwargs):
        commands.append((str(core),*args))
        if args[:3] == ('fetch','--no-tags','origin'):
            assert Path(core)==snapshot
            args=('fetch','--no-tags',str(remote),*args[3:])
        return original_git(core,*args,**kwargs)
    monkeypatch.setattr(s.update,'_git',proof_git)
    def run(args,**kwargs):
        assert args[:1]==['git']
        return subprocess.check_output(args,cwd=kwargs['cwd'],env={k:v for k,v in s.os.environ.items() if not k.startswith('GIT_')})
    monkeypatch.setattr(a,'_run',run)
    monkeypatch.setattr(a,'_isolated_slot_append',lambda *args:dict(result=[0,'exact compressed candidate']))
    def authenticate():
        return s.update.authenticate(rt,packet['manifest'],5,old,snapshot)
    record=authenticate()
    prepared['pr_update']=record
    rt.defect=None
    return dict(rt=rt,prepared=prepared,packet=packet,old=old,new=new,tree=tree,branch=branch,
                remote=remote,snapshot=snapshot,source=source,work=work,identity=identity,pull=pull,
                ref=ref,commands=commands,api_calls=api_calls,git=git,authenticate=authenticate)


def update_apply(f):
    return s.apply_prepared(f['rt'],f['prepared'],f['packet'],f['source'],{},
                            s.digest(a.canonical(f['prepared'])),snapshot=f['snapshot'])


def test_expected_head_real_history_receipt_keeps_remote_unchanged(update_env):
    f=update_env;rt=f['rt'];before=f['git'](f['remote'],'rev-parse','refs/heads/'+f['branch'])
    receipt=json.loads(update_apply(f).read_bytes())
    assert receipt['pr_update']==f['prepared']['pr_update']
    assert rt.events==['pause','write','restore'] and len(rt.writes)==1
    assert set(receipt['after'])==set(s.NAMES) and rt.values[s.NAMES[0]]=='{}'
    assert f['git'](f['remote'],'rev-parse','refs/heads/'+f['branch'])==before==f['old']
    assert not any('push' in command for command in f['commands'])
    assert all('/actions/' not in path and '/pulls/' in path or '/git/ref/' in path or path=='repos/'+rt.repo
               for path in f['api_calls'])


def test_no_implicit_update_mode_or_prepared_representation_change(update_env):
    f=update_env
    f['prepared'].pop('pr_update')
    with pytest.raises(a.AdmissionError,match='PR identity changed'):update_apply(f)
    assert not f['rt'].writes
    f['pull']['head']['sha']=f['new']
    update_apply(f)
    assert 'pr_update' not in json.loads(next(statedir.root().glob('focused-admission-receipts/*.json')).read_bytes())


@pytest.mark.parametrize('defect',[
    'pr-number','pr-id-bool','official-id-bool','official-name','official-fork','head-repo-id',
    'base-repo-id','fork','deleted-head','closed','base-ref','branch','main-branch','release-branch',
    'pr-old','ref-old','ref-name','ref-type','detached','dirty','index','local-head','wrong-local-branch',
    'unrelated','missing-old','shallow','replace','graft','snapshot-replace','snapshot-graft','api-error'])
def test_explicit_update_refuses_identity_and_real_git_defects(update_env,defect):
    f=update_env;rt=f['rt'];git=f['git'];head=f['pull']['head'];base=f['pull']['base']
    if defect=='pr-number':f['pull']['number']=6
    elif defect=='pr-id-bool':f['pull']['id']=True
    elif defect=='official-id-bool':f['identity']['id']=True
    elif defect=='official-name':f['identity']['full_name']='foreign/repo'
    elif defect=='official-fork':f['identity']['fork']=True
    elif defect=='head-repo-id':head['repo']['id']=42
    elif defect=='base-repo-id':base['repo']['id']=42
    elif defect=='fork':head['repo']['fork']=True
    elif defect=='deleted-head':head['repo']=None
    elif defect=='closed':f['pull']['state']='closed'
    elif defect=='base-ref':base['ref']='other'
    elif defect=='branch':head['ref']='other'
    elif defect=='main-branch':head['ref']='main'
    elif defect=='release-branch':head['ref']='release/v1.2.3'
    elif defect=='pr-old':head['sha']=f['new']
    elif defect=='ref-old':f['ref']['object']['sha']=f['new']
    elif defect=='ref-name':f['ref']['ref']='refs/heads/other'
    elif defect=='ref-type':f['ref']['object']['type']='tag'
    elif defect=='detached':git(rt.core,'switch','--detach',f['new'])
    elif defect=='dirty':(rt.core/'untracked').touch()
    elif defect=='index':
        (rt.core/'file').write_text('staged');git(rt.core,'add','file')
    elif defect=='local-head':git(rt.core,'update-ref','refs/heads/'+f['branch'],f['old'])
    elif defect=='wrong-local-branch':git(rt.core,'switch','-c','other')
    elif defect=='unrelated':
        unrelated=git(rt.core,'commit-tree',f['tree'],'-m','unrelated')
        git(rt.core,'update-ref','refs/heads/'+f['branch'],unrelated)
        f['packet']['manifest']['head']=unrelated
    elif defect=='missing-old':
        git(f['remote'],'update-ref','-d','refs/heads/'+f['branch'])
    elif defect=='shallow':(f['snapshot']/'.git/shallow').write_text(f['old']+'\n')
    elif defect in ('replace','snapshot-replace'):
        git(f['snapshot'] if defect.startswith('snapshot') else rt.core,'replace',f['old'],f['new'])
    elif defect in ('graft','snapshot-graft'):
        core=f['snapshot'] if defect.startswith('snapshot') else rt.core
        (core/'.git/info/grafts').write_text(f['new']+' '+f['old']+'\n')
    elif defect=='api-error':rt.api=lambda _:(_ for _ in ()).throw(a.AdmissionError('API unavailable'))
    with pytest.raises(a.AdmissionError):f['authenticate']()
    assert not rt.writes


def test_inherited_git_context_does_not_change_real_ancestry(update_env,monkeypatch):
    f=update_env
    for key,value in {'GIT_DIR':'/missing/git','GIT_WORK_TREE':'/missing/work','GIT_INDEX_FILE':'/missing/index',
                      'GIT_OBJECT_DIRECTORY':'/missing/objects','GIT_SHALLOW_FILE':'/missing/shallow',
                      'GIT_REPLACE_REF_BASE':'refs/other/','GIT_GRAFT_FILE':'/missing/grafts',
                      'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'remote.origin.url','GIT_CONFIG_VALUE_0':'foreign'}.items():
        monkeypatch.setenv(key,value)
    assert f['authenticate']()==f['prepared']['pr_update']


@pytest.mark.parametrize('field', ['mode','pr','pr_id','repository','repository_id','branch','old','new','extra','missing'])
def test_typed_record_drift_never_writes(update_env,field):
    f=update_env;record=f['prepared']['pr_update']
    if field=='missing':record.pop('old')
    elif field=='extra':record['extra']='unknown'
    elif field in ('pr','pr_id','repository_id'):record[field]+=1
    else:record[field]='wrong'
    with pytest.raises(a.AdmissionError):update_apply(f)
    assert not f['rt'].writes


@pytest.mark.parametrize('stage', ['before','after'])
@pytest.mark.parametrize('drift', ['old','stable-id','official-id','main','hold','consumer','remote-ref','local-head','local-branch'])
def test_update_drift_preserves_write_and_restoration_rules(update_env,stage,drift):
    f=update_env;rt=f['rt']
    def change():
        if drift=='old':f['ref']['object']['sha']=f['new']
        elif drift=='stable-id':f['pull']['id']+=1
        elif drift=='official-id':
            f['identity']['id']+=1
            for side in ('head','base'):f['pull'][side]['repo']['id']=f['identity']['id']
        elif drift=='main':rt.main='d'*40
        elif drift=='hold':(rt.management/'tools/pr-actions.toml').write_text('"5"={hold=true}')
        elif drift=='remote-ref':
            # Simulate another actor advancing the official server after its old
            # tip was approved. The admission helper itself never publishes it.
            f['git'](f['remote'],'fetch',str(rt.core),f['new'])
            f['git'](f['remote'],'update-ref','refs/heads/'+f['branch'],f['new'])
        elif drift=='local-head':f['git'](rt.core,'update-ref','refs/heads/'+f['branch'],f['old'])
        elif drift=='local-branch':f['git'](rt.core,'switch','-c','other')
        else:rt.rows['queued']=[dict(id=9,status='queued')]
    if stage=='before':change()
    else:
        original=rt.set_variable
        def write(*args):original(*args);change()
        rt.set_variable=write
    with pytest.raises(a.AdmissionError):update_apply(f)
    assert len(rt.writes)==(stage=='after') and rt.events[-1]=='restore'
    assert not list(statedir.root().glob('focused-admission-receipts/*.json'))


def test_update_operating_proof_uses_actual_exclusions_and_locks(update_env):
    f=update_env;rt=f['rt'];rt.domain='gui/fixture';rt.service='gui/fixture/queue'
    rt.plist=rt.home/'queue.plist';rt.plist.write_text('reviewed queue fixture');loaded=True
    def launch(*args,**kwargs):
        nonlocal loaded
        assert args[0]=='launchctl'
        if args[1]=='print':
            return subprocess.CompletedProcess(args,0 if loaded else 113,
                f'path = {rt.plist}\nstate = running'.encode() if loaded else b'',
                b'' if loaded else b'Could not find service')
        rt.events.append(args[1])
        if args[1]=='bootout':loaded=False
        elif args[1]=='bootstrap':loaded=True
        else:raise AssertionError(args)
        return subprocess.CompletedProcess(args,0,b'',b'')
    rt.run=launch
    rt.queue_state=lambda **kw:s.Runtime.queue_state(rt,**kw)
    rt.wait_queue_pause=lambda:s.Runtime.wait_queue_pause(rt)
    rt.restore_queue=lambda:s.Runtime.restore_queue(rt)
    rt.exclusions=lambda:s.Runtime.exclusions(rt)
    (rt.cache/'release').mkdir();version=rt.cache/'release/v1.2.3.lock';version.touch()
    receipt=json.loads(update_apply(f).read_bytes())
    assert loaded and rt.events==['bootout','write','bootstrap'] and receipt['pr_update']==f['prepared']['pr_update']
    for lock in (rt.cache/'queue.lock',rt.cache/'train/train.lock',rt.cache/'release.lock',version):
        assert lock.exists()
        with a._lock(lock):pass


@pytest.mark.parametrize(('args','old'), [
    (args,old) for args in ([],['--pr','0'],['--pr','-1'],['--pr','5'])
    for old in ('short','A'*40,'a'*39,'a'*41,'a'*40)
    if args!=['--pr','5'] or old!='a'*40])
def test_update_cli_requires_full_sha_and_positive_pr(tmp_path,args,old,capsys):
    # moved: the argv form runs in process (there is no script and no module entry)
    code=s.main(['prepare','missing','--core-repo',str(tmp_path),'--review-root',str(tmp_path),'--expected-remote-head',old,*args])
    err=capsys.readouterr().err
    assert code==1 and ('expected remote head' in err or 'PR must be positive' in err)


def prepare_update_cli(f,monkeypatch):
    """Replace canonical source IO only; actual CLI/identity/Git/guards remain."""
    rt=f['rt'];packet=f['packet'];review=rt.home/'review.md';review.write_text('Independent exact source review')
    packet['schema']=1
    packet['review']=dict(path='review.md',sha256=s.digest(review.read_bytes()),head=f['new'],base=rt.main,tree=f['tree'],
        manifest_sha256=s.digest(a.canonical(packet['manifest'])),main_manifest_sha256=s.digest(a.canonical(packet['main_manifest'])))
    path=rt.home/'packet.json';path.write_bytes(a.canonical(packet))
    monkeypatch.delenv('HZ_QUEUE_STATE')
    for name in ('HZ_TRAIN_STATE_DIR','HZ_RELEASE_DIR'):monkeypatch.delenv(name,raising=False)
    monkeypatch.setattr(s,'Runtime',lambda core,home=None:rt)
    monkeypatch.setattr(a,'_read_main',lambda _:rt.main)
    monkeypatch.setattr(a,'_prepare_core_snapshot',lambda *args:(f['work'],f['snapshot']))
    monkeypatch.setattr(a,'_canonical_source',lambda *args:(f['source'],'d'*40,rt.repo,s.NAMES[0],()))
    monkeypatch.setattr(s,'reader_contract',lambda *args:None)
    validations=[]
    def validate(item,core,base,repo,var,files,source,**kwargs):
        assert core==f['snapshot'] and source==f['source'] and base==rt.main
        validations.append(kwargs['resulting_main'])
    monkeypatch.setattr(a,'validate_canonical',validate)
    # Keep the fixture snapshot for the second real CLI invocation. Production
    # cleanup remains unchanged and is outside this injected IO boundary.
    monkeypatch.setattr(s.shutil,'rmtree',lambda *args,**kwargs:None)
    args=[str(path),'--core-repo',str(rt.core),'--review-root',str(rt.home),'--pr','5',
          '--expected-remote-head',f['old']]
    return path,args,validations


@pytest.mark.parametrize('defect',['none','mode','old','branch','new','missing-record','no-cli-flag','cli-old','approval','packet'])
def test_actual_cli_prepare_apply_freezes_typed_identity(update_env,monkeypatch,capsys,defect):
    f=update_env;path,args,validations=prepare_update_cli(f,monkeypatch)
    assert s.main(['prepare',*args])==0
    prepared=json.loads(capsys.readouterr().out)
    assert prepared['pr_update']==f['prepared']['pr_update'] and validations==[False,True]
    assert not f['rt'].writes
    approved=copy.deepcopy(prepared)
    if defect=='missing-record':approved.pop('pr_update')
    elif defect in ('mode','old','branch','new'):approved['pr_update'][defect]='tampered'
    elif defect=='no-cli-flag':args=args[:-2]
    elif defect=='cli-old':args[-1]=f['new']
    elif defect=='packet':
        packet=json.loads(path.read_bytes());packet['manifest']['tree']='e'*40;path.write_bytes(a.canonical(packet))
    approved_path=f['rt'].home/'approved.json';approved_path.write_bytes(a.canonical(approved))
    approval=s.digest(a.canonical(approved)) if defect!='approval' else '0'*64
    code=s.main(['apply',*args,'--prepared',str(approved_path),'--approved-sha256',approval])
    if defect=='none':
        assert code==0 and len(f['rt'].writes)==1
        receipt=json.loads(Path(capsys.readouterr().out.strip()).read_bytes())
        assert receipt['pr_update']==prepared['pr_update']
    else:
        assert code==1 and not f['rt'].writes
        assert f['rt'].events==[]


def test_real_normal_merge_of_current_main_is_a_valid_prospective_update(update_env):
    f=update_env;rt=f['rt'];git=f['git']
    git(rt.core,'switch','main');(rt.core/'main-file').write_text('current main')
    git(rt.core,'add','main-file');git(rt.core,'commit','-m','main advance')
    base=git(rt.core,'rev-parse','HEAD')
    git(rt.core,'switch',f['branch']);git(rt.core,'merge','--no-ff','main','-m','normal main merge')
    new=git(rt.core,'rev-parse','HEAD');tree=git(rt.core,'rev-parse','HEAD^{tree}')
    f['packet']['manifest'].update(head=new,base=base,tree=tree)
    f['packet']['main_manifest'].update(base=base,tree=tree)
    rt.main=base;f['prepared']['head']=new
    f['prepared']['pr_update']=f['authenticate']()
    receipt=json.loads(update_apply(f).read_bytes())
    assert receipt['pr_update']['old']==f['old'] and receipt['pr_update']['new']==new


def test_genuine_preopen_inventory_is_unchanged(env,monkeypatch):
    rt,_,packet=env
    monkeypatch.setattr(a,'_run',lambda *args,**kwargs:b'owned-new-branch')
    calls=[]
    def read(*args,**kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args,0,b'[]')
    rt.run=read
    s.check_preopen(rt,packet['manifest'])
    assert len(calls)==2 and not rt.writes


@pytest.mark.parametrize('defect',['missing-old','shared-tree-conflict','partial-pair','full-slots','readback'])
def test_update_retains_codec_and_readback_refusals(update_env,monkeypatch,defect):
    f=update_env
    if defect=='missing-old':f['prepared']['pr_update'].pop('old')
    elif defect=='readback':f['rt'].defect='readback'
    else:
        def refused(source,legacy,slots,additions):
            # Both complete, independently validated plans still reach the same
            # unchanged trusted-codec boundary; no partial repair is attempted.
            assert set(additions)=={f"pull_request:{f['new']}",f"push-tree:{f['tree']}"}
            raise a.AdmissionError(defect)
        monkeypatch.setattr(a,'_isolated_slot_append',refused)
    with pytest.raises(a.AdmissionError):update_apply(f)
    assert len(f['rt'].writes)==(defect=='readback') and f['rt'].events[-1:] in ([],['restore'])
