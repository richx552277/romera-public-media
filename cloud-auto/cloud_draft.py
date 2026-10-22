"""Run the existing digest-pinned v1.1 AI engine on Linux without a PC.

This adapter changes runtime paths, not the frozen character or reviewer results.
Generated content remains a draft until automated quality and delivery acceptance.
"""
import datetime as dt, json, os, shutil, subprocess, sys
from pathlib import Path
import cloud_auto as c

ROOT=Path(__file__).resolve().parent

def run():
    if os.environ.get('GITHUB_REPOSITORY')!='richx552277/romera-public-media':raise c.Hold('DRAFT_REPOSITORY_REFUSED')
    backend=c.GithubCAS(os.environ['GITHUB_REPOSITORY'],'cloud-auto/state.json',os.environ['GITHUB_TOKEN'])
    journal=c.Journal(backend,os.environ['GITHUB_RUN_ID'])
    value=journal.acquire()
    cycle='cloud-'+dt.datetime.now(c.KST).strftime('%Y%m%d')
    if cycle in value['generation'].get('cycles',{}):raise c.Hold('DRAFT_CYCLE_ALREADY_CLAIMED')
    def intent(v):
        journal.owned(v);v['generation'].setdefault('cycles',{})[cycle]={'status':'DRAFT_INTENT','run':journal.run,'at':c.now()}
    journal.change('DRAFT_INTENT',intent)
    # CAS lock/intents are Git commits. Fast-forward the checkout before outputs.
    sync=subprocess.run(['git','pull','--ff-only','origin','main'],cwd=ROOT.parent,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if sync.returncode:raise c.Hold('DRAFT_CHECKOUT_STALE_LOCK_RETAINED')
    sys.path.insert(0,str(ROOT/'portable/romera-cloud-publisher/automation'))
    import engine as e
    import stable_engine as s
    original=e.renderer
    def portable_renderer():
        w=original();w.FFMPEG=Path(shutil.which('ffmpeg'));w.FFPROBE=Path(shutil.which('ffprobe'));return w
    e.renderer=portable_renderer
    state=e.CLOUD/'cloud-state'
    # Calibration and full generation are real model calls on the cloud CPU.
    calibration=s.calibration(state)
    result=s.run(state,cycle,20261022+int(cycle[-4:]),'gemma3:4b')
    result.update(host='GitHub standard ubuntu runner',pc_dependency=False,
                  rights_music_native='EXCLUDED/NOT_PASS',cloud_release_admitted=False)
    c.save(state/'cloud-result.json',result)
    # Persist exact new state/artifacts BEFORE clearing the durable account lock.
    repo=ROOT.parent
    for args in ([ 'git','config','user.name','romera-cloud-bot'],
                 ['git','config','user.email','142954189+richx552277@users.noreply.github.com'],
                 ['git','add','cloud-auto/portable/romera-cloud-publisher/cloud-state'],
                 ['git','commit','-m','Preserve cloud draft '+cycle],
                 ['git','push','origin','HEAD:main']):
        p=subprocess.run(args,cwd=repo,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if p.returncode:raise c.Hold('DRAFT_PERSIST_FAILED_LOCK_RETAINED')
    def finish(v):
        journal.owned(v);v['generation']['cycles'][cycle].update(status=result['status'],video_sha256=result.get('video_sha256'),
            automated_quality_pass=result.get('automated_quality_pass',False),publication='HOLD',persisted=True)
    journal.change('DRAFT_RECORDED',finish);journal.release()
    print(json.dumps(result,ensure_ascii=False))
    return result

if __name__=='__main__':
    try:run()
    except Exception as ex:
        result={'status':'HOLD','reason':str(ex) if isinstance(ex,c.Hold) else type(ex).__name__,'at':c.now(),
                'live_posts':0,'automatic_retry':False}
        c.save(ROOT/'draft-failure.json',result);print(json.dumps(result));sys.exit(2)
