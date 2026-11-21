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






    # A restore failure can occur after an immutable write receipt. It must still fail the operation.




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




def test_module_entry_is_not_a_second_door_to_the_write():
    # moved: the module has no entry; `python -m` on it imports it and does nothing. The commands are the only door.
    for argv in (['apply','x'],['prepare','x'],['--help']):
        result=subprocess.run([sys.executable,'-m','steuermaschine_heinzel.native.admission_lib.focused_slots',*argv],capture_output=True)
        assert result.returncode==0 and not result.stdout  # (stderr: only the interpreter's runpy notice)
    assert '__main__' not in Path(s.__file__).read_text()




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


