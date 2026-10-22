import copy, datetime as dt, unittest
import cloud_auto as c

def fixture():
    job={'id':'romera-001','account_id':c.ACCOUNT,'video_sha256':'a'*64,'caption':'テスト',
         'cover_ms':1000,'approval':c.TASK,'publish_at':c.now(),'status':'READY'}
    job['payload_digest']=c.digest({k:job[k] for k in ('id','account_id','video_sha256','caption','cover_ms')})
    v=c.initial([job]);v['kill_switch']=False
    v['handoff'].update(status='ACCEPTED',old_writers_excluded=True,inflight_reconciled=True)
    v['credential'].update(status='VERIFIED',app_id='123',granted_scopes=['instagram_business_content_publish'],
        observed_at=c.now(),expires_at=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=30)).isoformat())
    return v

class Fake:
    def __init__(self,journal,fail=None):self.journal=journal;self.calls=[];self.fail=fail
    def create(self,job):
        self.calls.append('create')
        assert self.journal.backend.load()[0]['jobs'][0]['status']=='CREATE_INTENT'
        if self.fail=='create':raise TimeoutError()
        return '101'
    def status(self,cid):return 'FINISHED'
    def publish(self,cid):
        self.calls.append('publish')
        assert self.journal.backend.load()[0]['jobs'][0]['status']=='PUBLISH_INTENT'
        if self.fail=='publish':raise TimeoutError()
        return '202'
    def readback(self,mid):return {'id':mid,'username':c.USER,'caption':'テスト','permalink':'https://www.instagram.com/reel/MOCK/'}

class Safety(unittest.TestCase):
    def setup_j(self,v=None):
        b=c.MemoryCAS(v or fixture());j=c.Journal(b,'run-1');return b,j,Fake(j)
    def test_cloud_publish_then_replay(self):
        b,j,m=self.setup_j();c.tick(j,m,delivery=lambda x:None)
        c.tick(c.Journal(b,'run-2'),m,delivery=lambda x:None)
        self.assertEqual(m.calls,['create','publish']);self.assertEqual(b.v['jobs'][0]['status'],'PUBLISHED');c.validate_state(b.v)
    def test_kill_switch(self):
        v=fixture();v['kill_switch']=True;b,j,m=self.setup_j(v)
        with self.assertRaisesRegex(c.Hold,'KILL_SWITCH'):c.tick(j,m,delivery=lambda x:None)
        self.assertEqual(m.calls,[])
    def test_missing_scope(self):
        v=fixture();v['credential']['granted_scopes']=[];b,j,m=self.setup_j(v)
        with self.assertRaisesRegex(c.Hold,'PUBLISH_SCOPE'):c.tick(j,m,delivery=lambda x:None)
        self.assertEqual(m.calls,[])
    def test_missing_expiry(self):
        v=fixture();v['credential']['expires_at']=None;b,j,m=self.setup_j(v)
        with self.assertRaises(c.Hold):c.tick(j,m,delivery=lambda x:None)
        self.assertEqual(m.calls,[])
    def test_unknown_old_writer(self):
        v=fixture();v['handoff']['old_writers_excluded']=False;b,j,m=self.setup_j(v)
        with self.assertRaisesRegex(c.Hold,'HANDOFF'):c.tick(j,m,delivery=lambda x:None)
        self.assertEqual(m.calls,[])
    def test_cas_conflict(self):
        b,j,m=self.setup_j();v,r=b.load();j.acquire()
        with self.assertRaisesRegex(c.Hold,'CAS_CONFLICT'):b.put(v,r)
    def test_competing_runner(self):
        b,j,m=self.setup_j();j.acquire()
        with self.assertRaisesRegex(c.Hold,'INTERRUPTED'):c.Journal(b,'run-2').acquire()
        self.assertEqual(m.calls,[])
    def test_uncertain_create_no_retry(self):
        b,j,m=self.setup_j();m.fail='create'
        with self.assertRaisesRegex(c.Hold,'CREATE_UNCERTAIN'):c.tick(j,m,delivery=lambda x:None)
        with self.assertRaises(c.Hold):c.tick(c.Journal(b,'run-2'),m,delivery=lambda x:None)
        self.assertEqual(m.calls,['create']);self.assertEqual(b.v['jobs'][0]['status'],'UNCERTAIN')
    def test_uncertain_publish_no_retry(self):
        b,j,m=self.setup_j();m.fail='publish'
        with self.assertRaisesRegex(c.Hold,'PUBLISH_UNCERTAIN'):c.tick(j,m,delivery=lambda x:None)
        with self.assertRaises(c.Hold):c.tick(c.Journal(b,'run-2'),m,delivery=lambda x:None)
        self.assertEqual(m.calls,['create','publish'])
    def test_delivery_corruption(self):
        b,j,m=self.setup_j()
        def fail(x):raise c.Hold('DELIVERY_HASH')
        with self.assertRaisesRegex(c.Hold,'DELIVERY_HASH'):c.tick(j,m,delivery=fail)
        self.assertEqual(m.calls,[])
    def test_payload_change(self):
        v=fixture();v['jobs'][0]['caption']='changed';b,j,m=self.setup_j(v)
        with self.assertRaisesRegex(c.Hold,'PAYLOAD'):c.tick(j,m,delivery=lambda x:None)
        self.assertEqual(m.calls,[])
    def test_account_guard(self):
        v=fixture();v['account_id']='other';b,j,m=self.setup_j(v)
        with self.assertRaisesRegex(c.Hold,'BINDING'):j.acquire()
    def test_duplicate_hash(self):
        v=fixture();v['jobs'].append(copy.deepcopy(v['jobs'][0]))
        with self.assertRaisesRegex(c.Hold,'DUPLICATE'):c.validate_state(v)
    def test_event_tamper(self):
        b,j,m=self.setup_j();j.acquire();b.v['events'][0]['kind']='tampered'
        with self.assertRaisesRegex(c.Hold,'CHAIN'):c.validate_state(b.v)
    def test_missed_slot_no_catchup(self):
        v=fixture();v['jobs'][0]['publish_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=3)).isoformat()
        b,j,m=self.setup_j(v);c.tick(j,m,delivery=lambda x:None)
        self.assertEqual(m.calls,[]);self.assertEqual(b.v['jobs'][0]['status'],'MISSED_HOLD')
    def test_backup_restore(self):
        b,j,m=self.setup_j();j.acquire();backup=copy.deepcopy(b.v)
        restored=c.MemoryCAS(backup);c.validate_state(restored.v)
        with self.assertRaises(c.Hold):c.Journal(restored,'restored-run').acquire()
    def test_stale_epoch(self):
        v=fixture();v['epoch']=1
        with self.assertRaisesRegex(c.Hold,'BINDING'):c.validate_state(v)
    def test_token_secret_query_refused(self):
        with self.assertRaisesRegex(c.Hold,'SECRET_QUERY'):c.request('GET','https://graph.instagram.com/me?access_token=TEST')
    def test_nonofficial_secret_destination_refused(self):
        with self.assertRaisesRegex(c.Hold,'ORIGIN'):c.request('GET','https://example.com/',token='TEST')

def run_rehearsal():
    import io
    stream=io.StringIO();r=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Safety))
    return {'status':'PASS' if r.wasSuccessful() else 'FAIL','tests':r.testsRun,'failures':len(r.failures),'errors':len(r.errors),'details':stream.getvalue(),'backend':'MemoryCAS test only','live_ledger_acceptance':False}

if __name__=='__main__':unittest.main()
