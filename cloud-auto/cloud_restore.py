"""Quarantine restore of encrypted real draft, no Meta credential or model calls."""
import base64,json,os,sqlite3,subprocess,tempfile
from pathlib import Path
import cloud_auto as c
import cloud_draft as d
from nacl.secret import SecretBox

root=Path(__file__).resolve().parent
try:
    state,_=c.GithubCAS('richx552277/romera-public-media','cloud-auto/state.json',os.environ['GITHUB_TOKEN']).load()
    c.validate_state(state)
    records=[(name,v) for name,v in state['generation'].get('cycles',{}).items() if v.get('persisted') and v.get('archive_path')]
    if not records:raise c.Hold('NO_PERSISTED_DRAFT')
    cycle,record=records[-1];archive=root.parent/record['archive_path']
    if c.sha(archive)!=record['archive_sha256']:raise c.Hold('CIPHER_HASH_MISMATCH')
    box=SecretBox(base64.b64decode(os.environ['ROMERA_DRAFT_KEY'],validate=True))
    with tempfile.TemporaryDirectory(prefix='romera-quarantine-') as folder:
        folder=Path(folder);d.unpack(box,archive.read_bytes(),folder);box=None
        db=sqlite3.connect((folder/'engine.sqlite3').as_uri()+'?mode=ro',uri=True)
        integrity=db.execute('pragma integrity_check').fetchone()[0]
        counts={table:db.execute('select count(*) from '+table).fetchone()[0] for table in ('cycles','content','events')};db.close()
        result=c.read(folder/'cycles'/cycle/'result.json')
        video=folder/'cycles'/cycle/'video.mp4'
        video_match=video.is_file() and c.sha(video)==record.get('video_sha256')
        decode=subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(video),'-f','null','-'],capture_output=True)
        calibration=c.read(folder/'calibration.json')
        cases=calibration['cases']
        details=[]
        for p in sorted((folder/'calibration').glob('*/qa.json')):
            v=c.read(p);review=v.get('review') or {}
            details.append({'case':p.parent.name,'pass':v.get('pass'),'provider_done':v.get('provider_done'),
                'review':{k:review.get(k) for k in ('natural_japanese','coherent','payoff','visual_consistent','issues')},
                'quotes_exact':review.get('quotes')==c.read(p.with_name('language-review-request.json')).get('unused_expected_quotes') if False else None})
        report={'at':c.now(),'status':'PASS' if integrity=='ok' and video_match and decode.returncode==0 and not decode.stderr else 'HOLD',
            'cycle':cycle,'archive_path':record['archive_path'],'cipher_hash_match':True,'authenticated_decryption':True,
            'sqlite_integrity':integrity,'table_counts':counts,'video_sha256':c.sha(video),'video_sha_match':video_match,
            'full_video_decode':decode.returncode==0 and not decode.stderr,'calibration_cases':cases,'calibration_diagnostics':details,
            'production_restore_adopted':False,'quarantine_restore':True,'pc_dependency':False,
            'meta_requests':0,'paid_calls':0,'raw_draft_content_exported':False}
    c.save(root/'restore-result.json',report);print(json.dumps(report,ensure_ascii=False))
    if report['status']!='PASS':raise SystemExit(2)
except c.Hold as ex:
    c.save(root/'restore-result.json',{'status':'HOLD','reason':str(ex),'meta_requests':0});raise SystemExit(2)
