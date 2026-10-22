"""ROMERA cloud queue. Durable intents precede every Meta POST.

Only the live adapter can write to Meta. Mock mode never reads its secret.
No retry is performed for a POST or an uncertain publishing result.
"""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.parse import urlencode,urlsplit,quote
from urllib.error import HTTPError
import argparse,base64,copy,hashlib,json,os,re,sys,tempfile

ROOT=Path(__file__).resolve().parent
def utc():return datetime.now(timezone.utc)
def iso(t):return t.astimezone(timezone.utc).isoformat()
def date(x):
    d=datetime.fromisoformat(x)
    if d.tzinfo is None:raise ValueError('Timezone required')
    return d
def load(path):return json.loads(Path(path).read_text('utf-8'))
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.write-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(json.dumps(data,ensure_ascii=False,indent=2));f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if Path(tmp).exists():Path(tmp).unlink()
def confined(root,name):
    root=Path(root).resolve();p=(root/name).resolve()
    if not p.is_relative_to(root):raise ValueError('Asset path outside repository')
    return p
def empty():return {'schema':1,'jobs':{}}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise RuntimeError('Redirect refused')
OPENER=build_opener(NoRedirect())
class RemoteError(RuntimeError):pass
def request(host,path,method,token=None,data=None):
    if host == "graph.instagram.com" and method.upper() == "POST":
        raise PermissionError("ZF-OPS-ROMERA-CUTOVER-001: life_romera legacy writer fenced")
    if host not in {'api.github.com','graph.instagram.com'}:raise ValueError('Unapproved API host')
    if not path.startswith('/') or '\n' in path:raise ValueError('Invalid API path')
    headers={'Accept':'application/json','User-Agent':'romera-cloud-publisher/1.0'}
    if token:headers['Authorization']='Bearer '+token
    body=None
    if data is not None:
        if host=='graph.instagram.com':
            headers['Content-Type']='application/x-www-form-urlencoded';body=urlencode(data).encode()
        else:headers['Content-Type']='application/json';body=json.dumps(data).encode()
    req=Request('https://'+host+path,headers=headers,data=body,method=method)
    try:
        with OPENER.open(req,timeout=30) as r:return json.loads(r.read(2_000_000))
    except HTTPError as e:
        # Never print remote bodies: they may echo credentials or private URL parameters.
        code=None
        try:code=json.loads(e.read(30000)).get('error',{}).get('code')
        except Exception:pass
        raise RemoteError(f'{host} {method} HTTP{e.code}; code={code}') from None
    except RemoteError:raise
    except Exception as e:raise RemoteError(f'{host} {method}: {type(e).__name__}') from None

class LocalJournal:
    def __init__(self,path):self.path=Path(path)
    def read(self):return load(self.path) if self.path.exists() else empty()
    def save(self,data):
        if self.path.exists():
            previous=load(self.path);preserve_history(previous,data)
            # Preserve exact prior bytes under their content hash before replacement.
            archive=self.path.parent/(self.path.stem+'-history')/(sha(self.path)+'.json')
            archive.parent.mkdir(parents=True,exist_ok=True)
            if not archive.exists():
                with archive.open('xb') as f:
                    f.write(self.path.read_bytes());f.flush();os.fsync(f.fileno())
        atomic(self.path,data)

def preserve_history(previous,current):
    for key,old in previous.get('jobs',{}).items():
        new=current.get('jobs',{}).get(key)
        if new is None:raise PermissionError('Journal history cannot be removed')
        for field in ('video_sha256','account_id','media_id','published_at','job_digest'):
            if old.get(field) is not None and new.get(field)!=old[field]:
                raise PermissionError('Journal identity cannot be rewritten: '+field)
        if old.get('phase')=='published' and new.get('phase')!='published':
            raise PermissionError('Published journal record is immutable')

class GithubJournal:
    """Compare-and-swap writes; any conflict stops before a new external action."""
    def __init__(self,repo,branch,path,token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('Invalid repository')
        if branch!='main':raise ValueError('Only configured main branch is accepted')
        if path not in {'state/published.json','state/mock-published.json'}:raise ValueError('Invalid journal')
        self.repo,self.branch,self.path,self.token,self.version=repo,branch,path,token,None
        self.baseline=None
    def read(self):
        r=request('api.github.com',f'/repos/{self.repo}/contents/{self.path}?ref={self.branch}','GET',self.token)
        self.version=r['sha'];result=json.loads(base64.b64decode(r['content']))
        self.baseline=copy.deepcopy(result);return result
    def save(self,data):
        if not self.version:raise RuntimeError('Read durable journal before writing')
        preserve_history(self.baseline,data)
        r=request('api.github.com',f'/repos/{self.repo}/contents/{self.path}','PUT',self.token,
            {'message':'Record ROMERA publishing state [skip ci]','branch':self.branch,
             'sha':self.version,'content':base64.b64encode(json.dumps(data,ensure_ascii=False,indent=2).encode()).decode()})
        self.version=r['content']['sha']
        self.baseline=copy.deepcopy(data)

def job_digest(job):return digest(job)

def cadence_gate(job,policy,state,now):
    """A content slot is separate from scheduler wake-ups and legacy ceilings."""
    cadence=policy.get('cadence')
    if cadence is None:return # Older frozen policies remain replayable.
    if cadence!={'timezone':'Asia/Seoul','weekdays':[1,3,6],'time':'21:00:00','maximum_posts_per_week':3,'one_content_per_slot':True}:
        raise PermissionError('Unrecognized cadence policy')
    zone=timezone(timedelta(hours=9));slot=date(job['scheduled_at']).astimezone(zone)
    if slot.weekday() not in cadence['weekdays'] or slot.strftime('%H:%M:%S.%f')!='21:00:00.000000':
        raise PermissionError('Outside Tuesday Thursday Sunday 21:00 KST/JST slots')
    active={'create_intent','created','ready','publish_intent','published','uncertain','failed'}
    for key,record in state['jobs'].items():
        if key!=job['id'] and record.get('mode')=='live' and record.get('phase') in active and record.get('scheduled_at') and date(record['scheduled_at'])==date(job['scheduled_at']):
            raise PermissionError('Content slot is already reserved; no replacement or catch-up burst')
    week=now.astimezone(zone).isocalendar()[:2]
    count=sum(1 for record in state['jobs'].values() if record.get('phase')=='published' and record.get('mode')=='live' and date(record['published_at']).astimezone(zone).isocalendar()[:2]==week)
    if count>=cadence['maximum_posts_per_week']:raise PermissionError('Weekly post limit reached')
def validate_job(job,root):
    if not re.fullmatch(r'[a-z0-9-]+',job['id']):raise ValueError('Invalid job ID')
    if job['language']!='ja' or job['character_version']!='1.1':raise ValueError('Only Japanese v1.1 is accepted')
    if not job['caption'].strip() or len(job['caption'])>2200:raise ValueError('Invalid caption')
    asset=confined(root,job['asset']['path'])
    if sha(asset)!=job['asset']['sha256']:raise ValueError('Local media hash mismatch')
    if job['qa']['video_sha256']!=job['asset']['sha256']:raise ValueError('QA binds different media')
    if not job['qa']['technical_pass'] or not all(job['qa']['checks'].values()):raise ValueError('Technical QA failed')
    if job.get('scheduled_at'):date(job['scheduled_at'])

def live_gate(job,policy,now,enabled):
    if not enabled or not policy['live_enabled']:raise PermissionError('Live publishing is disabled')
    approval=policy.get('representative_approval')
    if not approval or not approval.get('actor') or not approval.get('evidence'):raise PermissionError('Representative activation approval absent')
    if not policy.get('permissions_verified'):raise PermissionError('Publishing permissions not verified')
    if date(approval['expires_at'])<=now:raise PermissionError('Activation approval expired')
    if job['account_id']!=policy['account_id'] or job['username']!='life_romera':raise PermissionError('Account mismatch')
    if job['status']!='ready' or not job.get('scheduled_at'):raise PermissionError('Job is a draft')
    if not job['media_url'] or not job['public_media_approved']:raise PermissionError('Public media not approved')
    if not policy['api_version'] or not re.fullmatch(r'v\d+\.\d+',policy['api_version']):raise PermissionError('API version must be verified and configured')
    q=job['review']
    if q['editorial_score'] is None or q['editorial_score']<85 or q['empathy_score'] is None or q['empathy_score']<24:raise PermissionError('Editorial acceptance incomplete')
    if not all(q[k] is True for k in ['native_japanese','full_playback','mobile','rights']):raise PermissionError('Human release checks incomplete')
    if q.get('content_digest')!=digest({'asset':job['asset'],'caption':job['caption'],'language':job['language'],'account_id':job['account_id']}):raise PermissionError('Stale content review')
    if now>date(job['scheduled_at'])+timedelta(hours=policy['maximum_lateness_hours']):raise PermissionError('Too late; representative decision required')
    if not job.get('token_valid_until') or date(job['token_valid_until'])<=now+timedelta(hours=1):raise PermissionError('Token expiry not verified')

def verify_public_media(job,policy,root):
    parts=urlsplit(job['media_url'])
    if parts.scheme!='https' or parts.hostname not in policy['approved_media_hosts'] or parts.username or parts.password or parts.query or parts.fragment or parts.port not in {None,443}:
        raise PermissionError('Unapproved public video URL')
    # Current route requires a commit-pinned GitHub raw URL, never a private access token in a URL.
    if parts.hostname!='raw.githubusercontent.com' or not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[0-9a-f]{40}/.+\.mp4',parts.path):raise PermissionError('Immutable approved media URL required')
    h=hashlib.sha256();total=0
    req=Request(job['media_url'],headers={'User-Agent':'romera-media-hash-check'},method='GET')
    with OPENER.open(req,timeout=30) as response:
        if response.status!=200:raise PermissionError('Media URL unavailable')
        while True:
            chunk=response.read(1024*1024)
            if not chunk:break
            total+=len(chunk)
            if total>100_000_000:raise PermissionError('Remote video too large')
            h.update(chunk)
    if h.hexdigest()!=job['asset']['sha256']:raise PermissionError('Remote video bytes differ from reviewed media')

class Meta:
    live=True
    def __init__(self,token,version,account):
        if not token:raise PermissionError('Meta secret not configured')
        self.token,self.version,self.account=token,version,account
    def call(self,path,method,data=None):return request('graph.instagram.com','/'+self.version+'/'+path,method,self.token,data)
    def identity(self):
        r=self.call('me?fields=user_id,username','GET')
        if str(r.get('user_id') or r.get('id'))!=self.account or r.get('username')!='life_romera':raise PermissionError('Token owner differs from target')
    def create(self,job):
        r=self.call(self.account+'/media','POST',{'media_type':'REELS','video_url':job['media_url'],'caption':job['caption']})
        if not str(r.get('id','')).isdigit():raise RemoteError('Missing container ID')
        return str(r['id'])
    def status(self,container):return self.call(container+'?fields=status_code','GET').get('status_code')
    def publish(self,container):
        r=self.call(self.account+'/media_publish','POST',{'creation_id':container})
        if not str(r.get('id','')).isdigit():raise RemoteError('Missing media ID')
        return str(r['id'])
    def permalink(self,media):return self.call(media+'?fields=permalink','GET').get('permalink')

class Mock:
    live=False
    def identity(self):pass
    def create(self,job):return 'MOCK-CONTAINER-'+job['id']
    def status(self,container):return 'FINISHED'
    def publish(self,container):return container.replace('CONTAINER','MEDIA')
    def permalink(self,media):return None

def tick(queue,policy,journal,api,now,root,enabled=False,rehearsal=False,verify_remote=verify_public_media):
    state=journal.read()
    if state.get('schema')!=1:raise ValueError('Unknown journal schema')
    ids=[j['id'] for j in queue]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate queue IDs')
    # Check the entire batch before the first external action, including drafts.
    owners={}
    for item in queue:
        validate_job(item,root)
        key=(item['account_id'],item['asset']['sha256'])
        if key in owners:raise ValueError('Identical media occurs under different job IDs')
        owners[key]=item['id']
    mode='live' if api.live else 'mock'
    dirty=False;unresolved=[]
    for previous_id,previous in state['jobs'].items():
        if previous.get('mode')!=mode:raise ValueError('Mock/live journals must be separate')
        phase=previous.get('phase')
        if phase not in {'create_intent','created','ready','publish_intent','published','uncertain','blocked','failed'}:
            raise ValueError('Unknown publication phase; reconcile journal')
        if phase in {'create_intent','publish_intent'}:
            previous.update(phase='uncertain',reason='Previous process stopped after durable POST intent; reconcile externally before any retry.')
            dirty=True;phase='uncertain'
        if phase=='uncertain':unresolved.append(previous_id)
        if phase!='blocked':
            previous_hash=previous.get('video_sha256')
            if not previous_hash:raise PermissionError('Historical media hash unverified; reconcile journal')
            for item in queue:
                if item['asset']['sha256']==previous_hash and item['id']!=previous_id:
                    raise PermissionError('Historical or in-flight media renamed as new job')
    if unresolved:
        # Quarantine the whole ledger, including jobs earlier in queue order.
        state['quarantine']={'jobs':unresolved,'reason':'Read-only reconciliation required; no automatic retry'}
        journal.save(state)
        return state
    if state.get('quarantine'):
        raise PermissionError('Explicit read-only reconciliation must clear quarantine')
    if dirty:journal.save(state)
    local_zone=timezone(timedelta(hours=9));today=now.astimezone(local_zone).date()
    published_today=sum(1 for x in state['jobs'].values() if x.get('phase')=='published' and date(x['published_at']).astimezone(local_zone).date()==today)
    for original in queue:
        job=copy.deepcopy(original);validate_job(job,root)
        if rehearsal and not api.live:job['status']='ready';job['scheduled_at']='2000-01-01T00:00:00+00:00'
        if job['status']!='ready' or not job['scheduled_at']:continue
        scheduled=date(job['scheduled_at'])
        if now<scheduled-timedelta(minutes=policy['prepare_ahead_minutes']):continue
        key=job['id'];fingerprint=job_digest(job)
        record=state['jobs'].get(key)
        if record and record['job_digest']!=fingerprint:raise ValueError('Job changed; use new revision ID')
        if record and record['mode']!=('live' if api.live else 'mock'):raise ValueError('Mock/live journals must be separate')
        phase=record['phase'] if record else 'new'
        if phase=='published':
            if api.live and not record.get('permalink'):
                try:
                    record['permalink']=api.permalink(record['media_id']);journal.save(state)
                except RemoteError:pass
            continue
        if phase in {'create_intent','publish_intent'}:
            record['phase']='uncertain';record['reason']='Previous process stopped after durable POST intent; reconcile externally before any retry.';journal.save(state);continue
        if phase in {'uncertain','blocked','failed'}:continue
        if api.live:
            try:
                live_gate(job,policy,now,enabled)
                cadence_gate(job,policy,state,now)
                if phase=='ready' and now>=scheduled and published_today>=policy['maximum_posts_per_day']:raise PermissionError('Daily post limit reached')
                api.identity()
                if phase in {'new','ready'}:verify_remote(job,policy,root)
            except (PermissionError,RemoteError) as e:
                blocked=copy.deepcopy(record) if record else {}
                blocked.update(phase='blocked',mode='live',job_digest=fingerprint,video_sha256=job['asset']['sha256'],account_id=job['account_id'],reason=str(e),at=iso(now))
                state['jobs'][key]=blocked;journal.save(state);continue
        if phase=='new':
            record={'phase':'create_intent','mode':'live' if api.live else 'mock','job_digest':fingerprint,'video_sha256':job['asset']['sha256'],'account_id':job['account_id'],'scheduled_at':job['scheduled_at'],'at':iso(now)}
            state['jobs'][key]=record;journal.save(state) # Must complete before POST.
            try:container=api.create(job)
            except Exception as e:
                record.update(phase='uncertain',reason='Container POST result unconfirmed: '+type(e).__name__)
                state['quarantine']={'jobs':[key],'reason':'Container result unconfirmed; no automatic retry'}
                journal.save(state);return state
            record.update(phase='created',container_id=container);journal.save(state)
        elif phase=='created':
            try:status=api.status(record['container_id'])
            except RemoteError:continue # Safe GET can be attempted on the next wake-up.
            if status=='FINISHED':record['phase']='ready'
            elif status in {'ERROR','EXPIRED'}:record.update(phase='failed',reason='Container '+status)
            journal.save(state)
        elif phase=='ready' and now>=scheduled:
            record['phase']='publish_intent';journal.save(state) # Must complete before POST.
            try:media=api.publish(record['container_id'])
            except Exception as e:
                record.update(phase='uncertain',reason='Publish POST result unconfirmed: '+type(e).__name__)
                state['quarantine']={'jobs':[key],'reason':'Publish result unconfirmed; no automatic retry'}
                journal.save(state);return state
            record.update(phase='published',media_id=media,published_at=iso(now),permalink=None)
            journal.save(state);published_today+=1
    return state

def sync(ledger,records_path):
    records=load(records_path) if Path(records_path).exists() else {'records':{}}
    for key,value in ledger['jobs'].items():
        if value['mode']=='live' and value['phase']=='published' and str(value.get('media_id','')).isdigit():
            records['records'][key]={'status':'게시됨','media_id':value['media_id'],'url':value.get('permalink'),'published_at':value['published_at']}
    atomic(records_path,records);return records

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['tick','sync','report']);p.add_argument('--mode',choices=['mock','live'],default='mock');p.add_argument('--github',action='store_true');p.add_argument('--rehearsal',action='store_true');p.add_argument('--steps',type=int,default=1)
    a=p.parse_args();policy=load(ROOT/'policy.json');queue=load(ROOT/'queue.json')['jobs']
    if not 1<=a.steps<=4:raise ValueError('Invalid step count')
    if a.mode=='live' and a.rehearsal:raise PermissionError('Live rehearsal is forbidden')
    if a.command=='tick' and a.mode=='live' and not a.github:
        raise PermissionError('Live ticks require the authoritative GitHub journal; local snapshots are read-only evidence')
    if a.command=='tick' and a.mode=='live' and (os.getenv('ROMERA_PUBLISH_ENABLED')!='true' or not policy['live_enabled']):raise PermissionError('Live publishing is disabled')
    filename='published.json' if a.mode=='live' else 'mock-published.json'
    journal=GithubJournal(os.environ['GITHUB_REPOSITORY'],'main','state/'+filename,os.environ['GITHUB_TOKEN']) if a.github else LocalJournal(ROOT/'runtime'/filename)
    if a.command=='sync':
        result=sync(journal.read(),ROOT/'runtime/local-records.json')
    elif a.command=='report':
        ledger=journal.read();result={'at':iso(utc()),'mode':a.mode,'counts':{phase:sum(x['phase']==phase for x in ledger['jobs'].values()) for phase in ['published','ready','created','uncertain','blocked','failed']},'performance_metrics':None}
        atomic(ROOT/'runtime/report.json',result)
    else:
        api=Meta(os.environ.get('META_ACCESS_TOKEN'),policy['api_version'],policy['account_id']) if a.mode=='live' else Mock()
        for _ in range(a.steps):result=tick(queue,policy,journal,api,utc(),ROOT,os.getenv('ROMERA_PUBLISH_ENABLED')=='true',a.rehearsal)
        atomic(ROOT/'runtime/last-result.json',result)
    print(json.dumps({'command':a.command,'mode':a.mode,'status':'completed'},ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as e:
        print(type(e).__name__+': '+str(e),file=sys.stderr);raise SystemExit(1)
