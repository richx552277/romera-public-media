"""Romera account-scoped cloud runner. All mutations use a durable CAS ledger.

No automatic takeover, ambiguous POST retry, secret query, or release by QA alone.
The cloud rehearsal never receives a Meta credential and cannot publish.
"""
import argparse, base64, copy, datetime as dt, hashlib, json, os, re
import subprocess, sys, tempfile, urllib.request as ur, urllib.error as ue
from urllib.parse import urlencode
from pathlib import Path

ACCOUNT = '17841449382124561'
USER = 'life_romera'
TASK = 'ZF-OPS-ROMERA-CLOUD-AUTO-001'
KST = dt.timezone(dt.timedelta(hours=9))

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def digest(v): return hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p): return json.loads(Path(p).read_text('utf-8-sig'))
def save(p, v):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(v, ensure_ascii=False, indent=2)+'\n', 'utf-8')

class Hold(Exception): pass
class NoRedirect(ur.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw): raise Hold('REDIRECT_REFUSED')

def request(method, url, token=None, payload=None):
    """Only allow fixed official origins. Never return or log raw errors/headers."""
    if not re.match(r'https://(?:api.github.com|graph.instagram.com)/', url): raise Hold('ORIGIN_REFUSED')
    if re.search(r'(?:access_token|input_token|client_secret)=', url, re.I): raise Hold('SECRET_QUERY_REFUSED')
    headers = {'Accept':'application/json', 'User-Agent':TASK}
    if token: headers['Authorization'] = 'Bearer '+token
    if payload is not None: headers['Content-Type']='application/json'
    req = ur.Request(url, method=method, headers=headers,
                     data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None)
    try:
        with ur.build_opener(NoRedirect()).open(req, timeout=45) as r:
            raw=r.read(8_000_001)
            if len(raw)>8_000_000: raise Hold('RESPONSE_LIMIT')
            return json.loads(raw)
    except ue.HTTPError as ex:
        code=None; sub=None
        try:
            err=json.loads(ex.read(131072)).get('error', {})
            if isinstance(err,dict): code=err.get('code'); sub=err.get('error_subcode')
        except Exception: pass
        raise Hold('HTTP_'+str(ex.code)+'_CODE_'+str(code if type(code) is int else 'NA')+'_SUB_'+str(sub if type(sub) is int else 'NA')) from None
    except Hold: raise
    except Exception as ex: raise Hold('TRANSPORT_'+type(ex).__name__) from None

class MemoryCAS:
    """Test backend only. Production CLI does not select this backend."""
    def __init__(self,v): self.v=copy.deepcopy(v); self.revision=0
    def load(self): return copy.deepcopy(self.v),self.revision
    def put(self,v,rev):
        if rev!=self.revision: raise Hold('CAS_CONFLICT')
        self.v=copy.deepcopy(v); self.revision+=1; return self.revision

class GithubCAS:
    def __init__(self, repo, path, token):
        if repo!='richx552277/romera-public-media' or path not in ('cloud-auto/state.json','cloud-auto/test-state.json'): raise Hold('LEDGER_LOCATION_REFUSED')
        self.url=f'https://api.github.com/repos/{repo}/contents/{path}'
        self.token=token
    def load(self):
        v=request('GET',self.url,self.token)
        if v.get('encoding')!='base64': raise Hold('LEDGER_ENCODING')
        return json.loads(base64.b64decode(v['content'])),v['sha']
    def put(self,v,rev):
        data=base64.b64encode((json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode()).decode()
        kind=v['events'][-1]['kind'] if v['events'] else 'GENESIS'
        result=request('PUT',self.url,self.token,{'message':'Romera durable event '+kind, 'content':data,'sha':rev,'branch':'main'})
        return result['content']['sha']

def initial(jobs):
    return {'schema':1,'task':TASK,'account_id':ACCOUNT,'owner':'GPT/ZF-OPS','support':'ZF-DEV',
            'epoch':2,'kill_switch':True,'active_run':None,'jobs':jobs,'events':[],
            'handoff':{'status':'HOLD','previous_host':'Windows PC','new_host':'GitHub standard ubuntu runner',
                       'old_ledger_preserved':True,'old_writers_excluded':False,'inflight_reconciled':False},
            'credential':{'status':'UNVERIFIED','app_id':None,'granted_scopes':[], 'expires_at':None,'observed_at':None},
            'generation':{'release_enabled':False,'reason':'SEMANTIC_QA_NOT_ACCEPTED'},
            'rights_music_native':'EXCLUDED/NOT_PASS'}

def validate_state(v):
    if v.get('schema')!=1 or v.get('account_id')!=ACCOUNT or v.get('epoch')!=2: raise Hold('LEDGER_BINDING')
    previous='0'*64
    for i,e in enumerate(v['events']):
        body={k:val for k,val in e.items() if k!='hash'}
        if e['seq']!=i+1 or e['previous']!=previous or digest(body)!=e['hash']: raise Hold('EVENT_CHAIN_INVALID')
        previous=e['hash']
    for job in v['jobs']:
        if job.get('account_id')!=ACCOUNT: raise Hold('OTHER_ACCOUNT_REFUSED')
    keys=[j['video_sha256'] for j in v['jobs']]
    if len(keys)!=len(set(keys)): raise Hold('DUPLICATE_VIDEO')

class Journal:
    def __init__(self,backend,run): self.backend=backend; self.run=run
    def change(self,kind,fn):
        v,rev=self.backend.load(); validate_state(v)
        fn(v)
        e={'seq':len(v['events'])+1,'at':now(),'kind':kind,'run':self.run,'epoch':v['epoch'],
           'previous':v['events'][-1]['hash'] if v['events'] else '0'*64}
        e['hash']=digest(e); v['events'].append(e)
        self.backend.put(v,rev) # any uncertain write aborts; caller must not retry
        return v
    def acquire(self):
        def claim(v):
            if v['active_run'] is not None: raise Hold('ACTIVE_OR_INTERRUPTED_RUN_RECONCILE')
            v['active_run']=self.run
        return self.change('LOCK_ACQUIRED',claim)
    def owned(self,v):
        if v['active_run']!=self.run: raise Hold('OWNER_FENCED')
    def release(self):
        def done(v): self.owned(v); v['active_run']=None
        self.change('LOCK_RELEASED',done)
    def update(self,jid,status,**fields):
        def update(v):
            self.owned(v); job=next(j for j in v['jobs'] if j['id']==jid)
            job.update(status=status,**fields)
        return self.change(status,update)
    def check(self,jid):
        v,_=self.backend.load(); validate_state(v); self.owned(v)
        if v['kill_switch'] is not False: raise Hold('KILL_SWITCH')
        h=v['handoff']
        if h['status']!='ACCEPTED' or h['old_writers_excluded'] is not True or h['inflight_reconciled'] is not True: raise Hold('HANDOFF_NOT_ACCEPTED')
        c=v['credential']
        if c['status']!='VERIFIED' or not str(c['app_id'] or '').isdecimal(): raise Hold('CREDENTIAL_UNVERIFIED')
        if 'instagram_business_content_publish' not in c['granted_scopes']: raise Hold('PUBLISH_SCOPE_MISSING')
        try:
            expiry=dt.datetime.fromisoformat(c['expires_at']); observed=dt.datetime.fromisoformat(c['observed_at'])
            if expiry.tzinfo is None or observed.tzinfo is None: raise ValueError()
            if expiry<dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=24): raise Hold('TOKEN_EXPIRY_MARGIN')
            age=dt.datetime.now(dt.timezone.utc)-observed
            if not dt.timedelta(0)<=age<=dt.timedelta(hours=12): raise Hold('ATTESTATION_STALE')
        except Hold: raise
        except Exception: raise Hold('EXPIRY_OR_OBSERVATION_UNKNOWN') from None
        job=next(j for j in v['jobs'] if j['id']==jid)
        if job['approval']!=TASK or job.get('payload_digest')!=digest({k:job[k] for k in ('id','account_id','video_sha256','caption','cover_ms')}): raise Hold('PAYLOAD_APPROVAL_MISMATCH')
        return job

def safe_meta_probe(token):
    out={'at':now(),'credential_present':bool(token),'authentication':'header only','redirects':False,
         'secret_exported':False,'meta_post_count':0,'granted_scopes':'UNVERIFIED','expiry':'UNVERIFIED','app_identity':'UNVERIFIED'}
    if not token: return out
    try:
        v=request('GET','https://graph.instagram.com/v23.0/me?fields=user_id,username,account_type',token)
        if isinstance(v.get('data'),list) and len(v['data'])==1:v=v['data'][0]
        out['identity']={k:v[k] for k in ('id','user_id','username','account_type') if isinstance(v.get(k),(str,int)) and re.fullmatch(r'[\w.]{1,60}',str(v[k]))}
        out['identity_match']=str(v.get('user_id'))==ACCOUNT and v.get('username')==USER
        if out['identity_match']:
            d=request('GET',f'https://graph.instagram.com/v23.0/{ACCOUNT}/content_publishing_limit?fields=quota_usage,config',token)
            out['publishing_limit']=[{k:x[k] for k in ('quota_usage','config') if k in x} for x in d.get('data',[])]
    except Hold as ex: out['failure']=str(ex)
    return out

class Meta:
    def __init__(self,token): self.token=token
    def duplicate(self,job):
        after=None
        for _ in range(10):
            query={'fields':'id,caption,media_type','limit':100}
            if after:query['after']=after
            data=request('GET',f'https://graph.instagram.com/v23.0/{ACCOUNT}/media?'+urlencode(query),self.token)
            for media in data.get('data',[]):
                if media.get('media_type')=='VIDEO' and (media.get('caption') or '').strip()==job['caption'].strip():return True
            paging=data.get('paging',{})
            if not paging.get('next'):return False
            after=paging.get('cursors',{}).get('after')
            if not isinstance(after,str) or len(after)>4096:raise Hold('MEDIA_PAGING_INVALID')
        raise Hold('MEDIA_INVENTORY_INCOMPLETE')
    def create(self,job):
        return request('POST',f'https://graph.instagram.com/v23.0/{ACCOUNT}/media',self.token,
                       {'media_type':'REELS','video_url':job['delivery_url'],'caption':job['caption'], 'thumb_offset':job['cover_ms'],'share_to_feed':True})['id']
    def status(self,cid):
        if not str(cid).isdecimal():raise Hold('CID_INVALID')
        return request('GET',f'https://graph.instagram.com/v23.0/{cid}?fields=status_code',self.token)['status_code']
    def publish(self,cid):
        return request('POST',f'https://graph.instagram.com/v23.0/{ACCOUNT}/media_publish',self.token,{'creation_id':cid})['id']
    def readback(self,mid):
        if not str(mid).isdecimal():raise Hold('MID_INVALID')
        return request('GET',f'https://graph.instagram.com/v23.0/{mid}?fields=id,username,permalink,caption,media_type',self.token)

def verify_delivery(job):
    url=job.get('delivery_url') or ''
    if not re.fullmatch(r'https://raw.githubusercontent.com/richx552277/romera-public-media/[0-9a-f]{40}/approved/[0-9a-f]{64}\.mp4',url):raise Hold('IMMUTABLE_URL_MISSING')
    try:
        with ur.build_opener(NoRedirect()).open(url,timeout=45) as r:
            if r.status!=200:raise Hold('DELIVERY_HTTP')
            data=r.read(25_000_001)
        if len(data)>25_000_000 or hashlib.sha256(data).hexdigest()!=job['video_sha256']:raise Hold('DELIVERY_HASH')
    except Hold:raise
    except Exception as ex:raise Hold('DELIVERY_'+type(ex).__name__) from None

def tick(journal,meta,clock=None,delivery=verify_delivery):
    clock=clock or dt.datetime.now(dt.timezone.utc)
    v=journal.acquire()
    try:
        for old in v['jobs']:
            if old['status'] in ('CREATE_INTENT','PUBLISH_INTENT','UNCERTAIN'):raise Hold('UNRESOLVED_INTENT')
        for old in v['jobs']:
            if old['status'] not in ('READY','CONTAINER_CREATED'):continue
            due=dt.datetime.fromisoformat(old['publish_at'])
            if clock<due:continue
            if clock-due>dt.timedelta(minutes=90):
                journal.update(old['id'],'MISSED_HOLD');continue
            job=journal.check(old['id']); delivery(job)
            if old['status']=='READY':
                if meta.duplicate(job):
                    journal.update(job['id'],'RECONCILE_HOLD',reason='REMOTE_CAPTION_MATCH');raise Hold('REMOTE_DUPLICATE_RECONCILE')
                journal.update(job['id'],'CREATE_INTENT',request_digest=digest({'video':job['video_sha256'],'caption':job['caption'],'cover':job['cover_ms']}))
                # Kill switch/owner/epoch re-read after durable intent, before side effect.
                journal.check(job['id'])
                try: cid=meta.create(job)
                except Exception:
                    journal.update(job['id'],'UNCERTAIN',operation='CREATE');raise Hold('CREATE_UNCERTAIN_NO_RETRY') from None
                if not str(cid).isdecimal():raise Hold('CREATE_RESPONSE_INVALID_INTENT_RETAINED')
                journal.update(job['id'],'CONTAINER_CREATED',container_id=str(cid))
            else:cid=old['container_id']
            status=meta.status(cid)
            if status=='IN_PROGRESS':continue
            if status!='FINISHED':
                journal.update(job['id'],'CONTAINER_HOLD',container_status=status if status in ('ERROR','EXPIRED') else 'UNKNOWN');continue
            journal.check(job['id'])
            journal.update(job['id'],'PUBLISH_INTENT')
            journal.check(job['id'])
            try: mid=meta.publish(cid)
            except Exception:
                journal.update(job['id'],'UNCERTAIN',operation='PUBLISH');raise Hold('PUBLISH_UNCERTAIN_NO_RETRY') from None
            if not str(mid).isdecimal():raise Hold('PUBLISH_RESPONSE_INVALID_INTENT_RETAINED')
            journal.update(job['id'],'PUBLISHED_PENDING_READBACK',media_id=str(mid))
            result=meta.readback(mid)
            if str(result.get('id'))!=str(mid) or result.get('username')!=USER or result.get('caption')!=job['caption'] or not re.fullmatch(r'https://www.instagram.com/(?:reel|p)/[A-Za-z0-9_-]+/?',result.get('permalink','')):raise Hold('READBACK_MISMATCH')
            journal.update(job['id'],'PUBLISHED',permalink=result['permalink'],published_at=now())
        journal.release()
    except Exception:
        # A permanent owner lock intentionally survives any failure. Never TTL takeover.
        raise

def rehearsal(output):
    from test_cloud_auto import run_rehearsal
    result=run_rehearsal()
    result.update(at=now(), runner_os=os.environ.get('RUNNER_OS'),github_run_id=os.environ.get('GITHUB_RUN_ID'),
                  pc_dependency=False,meta_credentials_read=False,real_meta_posts=0,
                  limitation='Software cloud execution with mock Meta; not a physical PC shutdown or real publication test')
    save(output,result);return result

def backend_rehearsal():
    """Real GitHub durable commits and conflict/exclusion test; isolated test ledger."""
    b=GithubCAS('richx552277/romera-public-media','cloud-auto/test-state.json',os.environ['GITHUB_TOKEN'])
    run=os.environ['GITHUB_RUN_ID'];j=Journal(b,run)
    before,revision=b.load();j.acquire();checks=[]
    try:
        try:b.put(before,revision)
        except Hold:checks.append('stale_revision_write_rejected')
        else:raise Hold('CAS_CONFLICT_NOT_REJECTED')
        try:Journal(b,'competing-'+run).acquire()
        except Hold as ex:
            if str(ex)!='ACTIVE_OR_INTERRUPTED_RUN_RECONCILE':raise
            checks.append('other_cloud_runner_excluded')
        else:raise Hold('LOCK_NOT_ENFORCED')
        live,_=b.load();validate_state(live)
        if live['active_run']!=run:raise Hold('DURABLE_LOCK_READBACK')
        checks.append('durable_intent_event_chain_readback')
        j.release();final,_=b.load();validate_state(final)
        if final['active_run'] is not None:raise Hold('RELEASE_READBACK')
        checks.append('durable_release_readback')
        return {'status':'PASS','backend':'GitHub Contents API CAS real cloud writes','tests':checks,
                'meta_posts':0,'production_ledger_modified':False,'pc_dependency':False,'at':now(),'run':run}
    except Exception:raise

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['probe','rehearsal','backend','live']);p.add_argument('--output',default='evidence/result.json')
    a=p.parse_args()
    try:
        if a.mode=='probe':result=safe_meta_probe(os.environ.get('IG_TOKEN_ROMANCE_JP',''))
        elif a.mode=='rehearsal':result=rehearsal(a.output)
        elif a.mode=='backend':result=backend_rehearsal()
        else:
            if os.environ.get('GITHUB_REPOSITORY')!='richx552277/romera-public-media':raise Hold('CLOUD_REPOSITORY_REFUSED')
            if os.environ.get('ROMERA_LIVE_ENABLED')!='true':raise Hold('DEPLOYMENT_LIVE_DISABLED')
            # Probe before acquiring account ownership or creating any intent.
            token=os.environ.get('META_ACCESS_TOKEN','');probe=safe_meta_probe(token)
            if probe.get('identity_match') is not True:raise Hold('CLOUD_TOKEN_IDENTITY')
            j=Journal(GithubCAS(os.environ['GITHUB_REPOSITORY'],'cloud-auto/state.json',os.environ['GITHUB_TOKEN']),os.environ['GITHUB_RUN_ID'])
            tick(j,Meta(token));result={'status':'TICK_COMPLETE','at':now()}
    except Hold as ex:result={'status':'HOLD','reason':str(ex),'at':now()}
    except Exception as ex:result={'status':'HOLD','reason':'INTERNAL_'+type(ex).__name__,'at':now()}
    save(a.output,result);print(json.dumps(result,ensure_ascii=False))
    return 2 if result.get('status')=='HOLD' else 0

if __name__=='__main__':sys.exit(main())
