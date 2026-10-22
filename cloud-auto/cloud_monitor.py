"""Read-only cloud status; never takes over a lock or calls Meta."""
from pathlib import Path
import json,os,sys
import cloud_auto as c
try:
    token=os.environ.get('GITHUB_TOKEN','')
    backend=c.GithubCAS('richx552277/romera-public-media','cloud-auto/state.json',token)
    state,revision=backend.load();c.validate_state(state)
    result={'at':c.now(),'status':'HOLD' if state['kill_switch'] else 'ACTIVE',
        'account_id':c.ACCOUNT,'epoch':state['epoch'],'kill_switch':state['kill_switch'],
        'credential_status':state['credential']['status'],'handoff_status':state['handoff']['status'],
        'jobs':[{'id':j['id'],'status':j['status'],'publish_at':j['publish_at'],'media_id':j.get('media_id'),'permalink':j.get('permalink')} for j in state['jobs']],
        'active_run':state['active_run'],'revision':revision,'pc_dependency':False,'meta_requests':0,
        'no_automatic_takeover':True,'new_cost_services':0}
    if state['active_run']:
        r=c.request('GET','https://api.github.com/repos/richx552277/romera-public-media/actions/runs/'+str(state['active_run']),token)
        result['active_run_status']=r.get('status')
        if r.get('status')=='completed':
            result.update(status='RECONCILE_HOLD',reason='COMPLETED_RUN_STILL_OWNS_DURABLE_LOCK')
    c.save(Path(__file__).with_name('monitor-result.json'),result)
    print(json.dumps(result,ensure_ascii=False))
    if result['status']=='RECONCILE_HOLD':sys.exit(2)
except c.Hold as ex:
    print(json.dumps({'status':'HOLD','reason':str(ex),'meta_requests':0}));sys.exit(2)
