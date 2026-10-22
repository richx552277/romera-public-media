"""Run the existing digest-pinned v1.1 AI engine on Linux without a PC.

This adapter changes runtime paths, not the frozen character or reviewer results.
Generated content remains a draft until automated quality and delivery acceptance.
"""
import base64, datetime as dt, io, json, os, re, shutil, subprocess, sys, tarfile
from pathlib import Path
import cloud_auto as c

ROOT=Path(__file__).resolve().parent

def unpack(box,blob,state):
    payload=box.decrypt(blob)
    with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as tar:
        members=tar.getmembers()
        if len(members)>2000 or sum(m.size for m in members)>100_000_000:raise c.Hold('DRAFT_ARCHIVE_LIMIT')
        for m in members:
            p=(state/m.name).resolve()
            if not p.is_relative_to(state.resolve()) or not m.isfile():raise c.Hold('DRAFT_ARCHIVE_PATH')
            p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(tar.extractfile(m).read())

def pack(box,state,cycle):
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as tar:
        for p in sorted(state.rglob('*')):
            if not p.is_file() or p.name.endswith(('-wal','-shm')):continue
            rel=p.relative_to(state)
            # Retain old scripts/fingerprints, current complete artifacts, and DB.
            if rel.parts[0]=='cycles' and len(rel.parts)>2 and rel.parts[1]!=cycle and p.name not in ('plan.json','result.json'):continue
            tar.add(p,arcname=rel.as_posix(),recursive=False)
    raw=stream.getvalue();encrypted=bytes(box.encrypt(raw))
    if box.decrypt(encrypted)!=raw:raise c.Hold('ENCRYPTION_ROUNDTRIP')
    if len(encrypted)>5_000_000:raise c.Hold('DRAFT_ARCHIVE_SIZE_LIMIT')
    return encrypted

def run():
    if os.environ.get('GITHUB_REPOSITORY')!='richx552277/romera-public-media':raise c.Hold('DRAFT_REPOSITORY_REFUSED')
    backend=c.GithubCAS(os.environ['GITHUB_REPOSITORY'],'cloud-auto/state.json',os.environ['GITHUB_TOKEN'])
    journal=c.Journal(backend,os.environ['GITHUB_RUN_ID'])
    value,_=backend.load()
    base='cloud-'+dt.datetime.now(c.KST).strftime('%Y%m%d')
    existing=[x for x in value['generation'].get('cycles',{}) if x==base or x.startswith(base+'-a')]
    resumable=[name for name in existing if value['generation']['cycles'][name].get('resume_allowed') is True]
    if len(existing)>=2 and not resumable:raise c.Hold('DAILY_DRAFT_ATTEMPT_LIMIT')
    cycle=resumable[0] if resumable else (base if not existing else base+'-a2')
    from nacl.secret import SecretBox
    key=base64.b64decode(os.environ.get('ROMERA_DRAFT_KEY',''),validate=True)
    if len(key)!=SecretBox.KEY_SIZE:raise c.Hold('DRAFT_ENCRYPTION_KEY_REQUIRED')
    box=SecretBox(key);key=None
    value=journal.acquire()
    def intent(v):
        journal.owned(v);record=v['generation'].setdefault('cycles',{}).setdefault(cycle,{})
        if record.get('run'):record.setdefault('prior_runs',[]).append({k:record.get(k) for k in ('run','status','reason','at')})
        record.update(status='DRAFT_INTENT',run=journal.run,at=c.now(),resume_allowed=False)
    journal.change('DRAFT_INTENT',intent)
    # CAS lock/intents are Git commits. Fast-forward the checkout before outputs.
    sync=subprocess.run(['git','pull','--ff-only','origin','main'],cwd=ROOT.parent,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if sync.returncode:raise c.Hold('DRAFT_CHECKOUT_STALE_LOCK_RETAINED')
    sys.path.insert(0,str(ROOT/'portable/romera-cloud-publisher/automation'))
    import engine as e
    import stable_engine as s
    # Official registry bytes independently verified. New cloud model calibration
    # is not inherited from the old PC model digest.
    e.MODELS={**e.MODELS,'gemma3:4b':'68ceab7e468a31c988b44d5a4984a0e21f58c4f7158c6ce29c1804babac38df7'}
    original=e.renderer
    def portable_renderer():
        w=original();w.FFMPEG=Path(shutil.which('ffmpeg'));w.FFPROBE=Path(shutil.which('ffprobe'));return w
    e.renderer=portable_renderer
    state=e.CLOUD/'cloud-state'
    previous=[x for x in value['generation'].get('cycles',{}).values() if x.get('persisted') and x.get('archive_path')]
    if previous:
        old=previous[-1];path=ROOT.parent/old['archive_path']
        if c.sha(path)!=old['archive_sha256']:raise c.Hold('ENCRYPTED_DRAFT_HASH')
        unpack(box,path.read_bytes(),state)
    inventory=e.local('/api/tags').get('models',[])
    diagnostics=[{'model':name,'expected_digest':expected,
        'observed_digest':next((m.get('digest') for m in inventory if m.get('name')==name),None)} for name,expected in e.MODELS.items()]
    print(json.dumps({'cloud_model_inventory':diagnostics}),flush=True)
    # Calibration and full generation are real model calls on the cloud CPU.
    policy_hash=c.digest(e.MODELS)
    calibration=e.read(state/'calibration.json') if (state/'calibration.json').exists() else {}
    if calibration.get('cloud_model_policy_sha256')!=policy_hash or not all(s.calibrated(state,model) for model in e.MODELS):
        calibration=s.calibration(state);calibration['cloud_model_policy_sha256']=policy_hash;e.write(state/'calibration.json',calibration)
    result=s.run(state,cycle,20261022+int(dt.datetime.now(c.KST).strftime('%m%d')),'gemma3:4b')
    result.update(host='GitHub standard ubuntu runner',pc_dependency=False,
                  rights_music_native='EXCLUDED/NOT_PASS',cloud_release_admitted=False)
    result['model_inventory']=diagnostics
    result['provider_diagnostics']=[{'path':p.relative_to(state).as_posix(),
        **{k:v for k,v in e.read(p).items() if k in ('done','failure','done_reason','eval_count')}}
        for p in state.rglob('*provider.json')]
    c.save(state/'cloud-result.json',result)
    archive=ROOT/'private-drafts'/(cycle+'-'+journal.run+'.nacl');archive.parent.mkdir(exist_ok=True)
    archive.write_bytes(pack(box,state,cycle));box=None
    # Persist exact new state/artifacts BEFORE clearing the durable account lock.
    repo=ROOT.parent
    for args in ([ 'git','config','user.name','romera-cloud-bot'],
                 ['git','config','user.email','142954189+richx552277@users.noreply.github.com'],
                 ['git','add','cloud-auto/private-drafts'],
                 ['git','commit','-m','Preserve cloud draft '+cycle],
                 ['git','pull','--rebase','origin','main'],
                 ['git','push','origin','HEAD:main']):
        p=subprocess.run(args,cwd=repo,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if p.returncode:raise c.Hold('DRAFT_PERSIST_FAILED_LOCK_RETAINED')
    rel=archive.relative_to(ROOT.parent).as_posix()
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}',commit):raise c.Hold('DRAFT_COMMIT_INVALID')
    uri='https://raw.githubusercontent.com/richx552277/romera-public-media/'+commit+'/'+rel
    with c.ur.build_opener(c.NoRedirect()).open(uri,timeout=45) as response:remote=response.read(5_000_001)
    if __import__('hashlib').sha256(remote).hexdigest()!=c.sha(archive):raise c.Hold('DRAFT_STORAGE_READBACK')
    def finish(v):
        journal.owned(v);v['generation']['cycles'][cycle].update(status=result['status'],video_sha256=result.get('video_sha256'),
            rendered=result.get('rendered',False),reason=result['status'],
            automated_quality_pass=result.get('automated_quality_pass',False),publication='HOLD',persisted=True,
            archive_path=rel,archive_sha256=c.sha(archive),encrypted=True,storage_readback=True,
            encryption_roundtrip=True,provider_diagnostics=result['provider_diagnostics'])
    journal.change('DRAFT_RECORDED',finish);journal.release()
    print(json.dumps(result,ensure_ascii=False))
    return result

if __name__=='__main__':
    try:run()
    except Exception as ex:
        result={'status':'HOLD','reason':str(ex) if isinstance(ex,c.Hold) else type(ex).__name__,'at':c.now(),
                'live_posts':0,'automatic_retry':False}
        c.save(ROOT/'draft-failure.json',result);print(json.dumps(result));sys.exit(2)
