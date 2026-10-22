"""Standalone local ROMERA B rehearsal. No publishing or paid API adapter exists.

SQLite persists due slots, QA and mock receipts. Clock advance is explicit and
only affects this local test queue. Nothing is registered with an OS scheduler.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse, hashlib, json, sqlite3, subprocess, shutil, math, re, sys
from fractions import Fraction

ROOT = Path(__file__).resolve().parent
TOOLS = Path(r'C:\Users\PCuser\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\outputs\yt-pilot-codex\vendor\ffmpeg-9.0.2-essentials_build\bin')
FFMPEG, FFPROBE = TOOLS/'ffmpeg.exe', TOOLS/'ffprobe.exe'

def now(): return datetime.now(timezone.utc).isoformat()
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def digest(x): return hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
def write(p, data):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
def read(p): return json.loads(Path(p).read_text('utf-8'))
def confined(base, name):
    base = Path(base).resolve(); p = (base/name).resolve()
    if not p.is_relative_to(base): raise ValueError('Path outside sandbox')
    return p
def aware(value):
    t = datetime.fromisoformat(value)
    if t.tzinfo is None: raise ValueError('Explicit timezone is required')
    return t
def safety(config, mode='mock'):
    if mode != 'mock' or config['mode'] != 'mock': raise PermissionError('Live mode is not implemented or authorized')
    for key in ['publish_enabled','account_connection_enabled','paid_generation_enabled','scheduler_installed']:
        if config[key] is not False: raise PermissionError('Forbidden capability: '+key)
    if config['setting_version'] != '1.1': raise PermissionError('Only adopted v1.1 is allowed')
    if config['live_approval'] is not None: raise PermissionError('This rehearsal does not accept live approval records')

def db(path):
    c = sqlite3.connect(path, timeout=10)
    c.executescript('''
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,slot TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS receipts(job_id TEXT PRIMARY KEY,receipt TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,at TEXT,job_id TEXT,stage TEXT,detail TEXT);
    CREATE TABLE IF NOT EXISTS weekly_reports(slot TEXT PRIMARY KEY,created_at TEXT NOT NULL);
    ''')
    return c
def event(c, job, stage, detail):
    c.execute('INSERT INTO events(at,job_id,stage,detail) VALUES(?,?,?,?)',(now(),job,stage,json.dumps(detail,ensure_ascii=False)))
    c.commit()

def plans(config):
    current=read(ROOT/'inputs/romera-current.json')
    settings=ROOT/'inputs/romera-settings-v1.1/settings.json'
    if current['active_setting_version']!='1.1' or sha(settings)!=current['settings_sha256']:
        raise ValueError('Adopted setting hash/version mismatch')
    if config.get('production_language')=='ja':
        result=read(ROOT/'jp-plans.json')
        for i,p in enumerate(result):
            p['slot']=config['planning_slots'][i]
            p['rights_reference']='RIGHTS-JP.md'
            p['rights_status']='documented_not_human_release_approved'
            p['language']='ja';p['source']='new_v1.1_japanese_local_test';p['visual_version']='1.1'
            p['plan_digest']=digest(p)
        return result
    episodes = read(ROOT/'inputs/romera-pilots/production/episodes.json')
    result = []
    for i,e in enumerate(episodes):
        result.append({'id':'archive-'+e['id'],'slot':config['planning_slots'][i],
            'title':e['title'],'kind':e['kind'],'duration':e['duration'],'scenes':e['scenes'],
            'caption':e['caption'],'language':'ko','source':'archived_pilot_v1.0',
            'visual_version':'1.0','source_video':f"videos/romera_{e['id']}_{e['duration']}s.mp4",
            'source_sha256':sha(ROOT/'inputs/romera-pilots'/f"videos/romera_{e['id']}_{e['duration']}s.mp4"),
            'rights_reference':'inputs/romera-pilots/RIGHTS.md','rights_status':'documented_not_human_release_approved'})
    result.append({'id':'v11-phone','slot':config['planning_slots'][4],
        'title':'잠깐만 확인하려고','kind':'유머','duration':14,'language':'ko',
        'source':'new_v1.1_local_test','visual_version':'1.1',
        'caption':'확인하려던 건 알림 하나였는데, 벌써 저녁. 로메라 독립 모의 테스트.',
        'rights_reference':'RIGHTS-TEST.md','rights_status':'documented_not_human_release_approved',
        'scenes':[
            {'seconds':3,'text':'알림 하나만\n확인해야지.','asset':'front_blank.png','visual':'잠깐 폰을 확인하려는 로메라'},
            {'seconds':3.5,'text':'이것만 보고,\n정말 마지막.','asset':'pose_phone.png','visual':'폰을 들고 작게 웃는다'},
            {'seconds':3,'text':'잠깐…\n벌써 저녁이네?','asset':'front_shock.png','visual':'시간을 발견하고 멈춘다'},
            {'seconds':4.5,'text':'확인한 건 알림인데\n사라진 건 저녁.','asset':'front_tired.png','visual':'폰을 내려놓고 어깨가 처진다'}]})
    for p in result: p['plan_digest'] = digest(p)
    return result

def validate_plan(p):
    if not re.fullmatch('[a-z0-9-]+',p['id']): raise ValueError('Invalid job ID')
    if len(p['scenes']) != 4: raise ValueError('Four scenes required')
    if abs(sum(s['seconds'] for s in p['scenes'])-p['duration']) > .001: raise ValueError('Timing mismatch')
    lo,hi = (12,15) if p['kind']=='유머' else (18,25)
    if not lo <= p['duration'] <= hi: raise ValueError('Genre duration mismatch')
    if any(s['seconds'] <= 0 or len(s['text'].splitlines()) > 2 for s in p['scenes']): raise ValueError('Invalid captions')
    check = dict(p); expected = check.pop('plan_digest')
    if digest(check) != expected: raise ValueError('Plan changed without new revision')
    aware(p['slot'])

def seed(c, pp, until):
    end = aware(until)
    for p in pp:
        validate_plan(p)
        if aware(p['slot']) > end: continue
        existing=c.execute('SELECT plan FROM jobs WHERE id=?',(p['id'],)).fetchone()
        if existing and read_json(existing[0]) != p: raise ValueError('Existing plan changed; create a new revision')
        c.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?)',(p['id'],p['slot'],json.dumps(p,ensure_ascii=False),'planned'))
    c.commit()
def read_json(s): return json.loads(s)

def command(args, timeout=180):
    r = subprocess.run([str(x) for x in args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    if r.returncode: raise RuntimeError(r.stderr[-4000:])
    return r

def render(p, out):
    out.mkdir(parents=True,exist_ok=True)
    video = out/'video.mp4'
    if p['source']=='archived_pilot_v1.0':
        source=confined(ROOT/'inputs/romera-pilots',p['source_video'])
        if sha(source)!=p['source_sha256']: raise ValueError('Source hash mismatch')
        shutil.copyfile(source,video)
        write(out/'production.json',{'operation':'reuse_byte_identical_archived_pilot','sha256':sha(video),'source_sha256':p['source_sha256'],'at':now()})
        return video
    # Local drawing and font use only. No generative or external service calls.
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np
    import wave
    assets=ROOT/'inputs/romera-settings-v1.1/assets'
    font_path=assets/'fonts'/('NotoSansCJKjp-Medium.otf' if p['language']=='ja' else 'NotoSansCJKkr-Medium.otf')
    fonts={n:ImageFont.truetype(str(font_path),n) for n in [25,32,62]}
    frames=[]
    for i,s in enumerate(p['scenes']):
        im=Image.new('RGB',(1080,1920),'#F6F0E6'); d=ImageDraw.Draw(im)
        d.text((140,270),'ROMERA / v1.1',font=fonts[32],fill='#544842')
        d.text((940,270),'LOCAL TEST',font=fonts[25],anchor='ra',fill='#A65F4F')
        d.line((140,325,940,325),fill='#D4C9BC',width=2)
        for j,line in enumerate(s['text'].splitlines()):
            if d.textlength(line,font=fonts[62])>800: raise ValueError('Caption exceeds safe width')
            d.text((540,430+88*j),line,font=fonts[62],anchor='mm',fill='#544842')
        asset=Image.open(confined(assets/'characters',s['asset'])).convert('RGBA')
        asset=asset.crop(asset.getbbox()); asset.thumbnail((540,800),Image.Resampling.LANCZOS)
        im.paste(asset,((1080-asset.width)//2,700),asset)
        if s.get('clock'):
            d.text((790,650),s['clock'],font=fonts[32],anchor='mm',fill='#A65F4F')
        d.text((540,1550),'制作テスト・未公開' if p['language']=='ja' else '모의 제작 · 외부 게시 금지',font=fonts[25],anchor='mm',fill='#8E8276')
        frames.append(im)
        im.save(out/f'scene-{i+1}.png')
    contact=Image.new('RGB',(1080,480))
    for i,im in enumerate(frames): contact.paste(im.resize((270,480)),(270*i,0))
    contact.save(out/'storyboard.jpg',quality=95)
    # Original quiet two-note tones with no external samples, plus restrained vertical motion.
    sr=48000; t=np.arange(sr*p['duration'])/sr
    a=np.zeros(t.shape)
    for start,f in [(0.4,261.63),(3.4,329.63),(7.0,293.66),(10.0,220)]:
        dt=t-start; env=np.where(dt>=0,np.exp(-np.maximum(dt,0)*2.5),0)
        a += .09*np.sin(2*np.pi*f*dt)*env
    a*=np.minimum(t/.05,1)*np.minimum((p['duration']-t)/.5,1)
    audio=out/'original-tones.wav'
    with wave.open(str(audio),'wb') as w:
        w.setparams((2,2,sr,0,'NONE','not compressed')); w.writeframes(np.repeat((a*32767).astype('<i2')[:,None],2,axis=1).tobytes())
    args=[FFMPEG,'-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','rgb24','-s','1080x1920','-r','30','-i','pipe:0','-i',audio,'-c:v','libx264','-preset','fast','-crf','20','-threads','4','-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-t',p['duration'],'-movflags','+faststart',video]
    with (out/'render.log').open('w') as log:
        proc=subprocess.Popen([str(x) for x in args],stdin=subprocess.PIPE,stderr=log)
        try:
            for im,s in zip(frames,p['scenes']):
                for n in range(round(s['seconds']*30)):
                    # Keep captions static; only move pixels in the character region.
                    frame=im.copy(); region=im.crop((240,680,840,1520))
                    frame.paste('#F6F0E6',(240,680,840,1520))
                    frame.paste(region,(240,680+round(3*math.sin(n/30*2))))
                    proc.stdin.write(frame.tobytes())
        finally:
            proc.stdin.close()
        if proc.wait(timeout=180): raise RuntimeError('Render failed; see render.log')
    write(out/'production.json',{'operation':'new_local_v1.1_render','at':now(),'sha256':sha(video),'font_sha256':sha(font_path),'paid_services_called':False})
    start=0;sub=[]
    def stime(s):
        ms=round(s*1000);return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
    for i,s in enumerate(p['scenes']):
        sub.append(f"{i+1}\n{stime(start)} --> {stime(start+s['seconds'])}\n{s['text']}\n");start+=s['seconds']
    (out/f"subtitles.{p['language']}.srt").write_text('\n'.join(sub),'utf-8-sig')
    return video

def quality(p, video, out):
    validate_plan(p)
    probe=read_json(command([FFPROBE,'-v','error','-show_streams','-show_format','-of','json',video]).stdout)
    v=next(x for x in probe['streams'] if x['codec_type']=='video')
    a=next(x for x in probe['streams'] if x['codec_type']=='audio')
    dec=command([FFMPEG,'-v','error','-xerror','-threads','2','-i',video,'-f','null','-'])
    (out/'decode.log').write_text(dec.stderr,'utf-8')
    signal=command([FFMPEG,'-hide_banner','-i',video,'-vf','blackdetect=d=0.1:pix_th=0.02',
        '-af','loudnorm=I=-20:TP=-2:LRA=7:print_format=json','-f','null','-'])
    (out/'signal-analysis.log').write_text(signal.stderr,'utf-8')
    match=re.search(r'\{\s*"input_i".*?\}',signal.stderr,re.S)
    loudness=json.loads(match.group()) if match else None
    checks={'1080x1920':v['width']==1080 and v['height']==1920,
        'h264_yuv420p':v['codec_name']=='h264' and v['pix_fmt']=='yuv420p',
        '30fps':float(Fraction(v['avg_frame_rate']))==30,
        'duration':abs(float(probe['format']['duration'])-p['duration'])<=.1,
        'frame_count':int(v['nb_frames'])==p['duration']*30,
        'aac_48khz_stereo':a['codec_name']=='aac' and a['sample_rate']=='48000' and a['channels']==2,
        'full_decode':True,'size_under_100mb':video.stat().st_size<100_000_000,
        'four_scenes':len(p['scenes'])==4,'source_rights_record':confined(ROOT,p['rights_reference']).is_file(),
        'no_black_frames_detected':'black_start:' not in signal.stderr,
        'true_peak_below_minus1dbtp':loudness is not None and float(loudness['input_tp']) < -1}
    q={'at':now(),'plan_digest':p['plan_digest'],'video_sha256':sha(video),'checks':checks,
       'technical_pass':all(checks.values()),'mock_eligible':all(checks.values()),
       'live_eligible':False,'human_listening':'not_attested','real_phone_review':'not_attested',
       'native_japanese_review':'not_performed','editorial_score':None,'language':p['language'],
       'live_blocks':['separate account and publishing approval absent','preapproved operational B rules not signed','human full playback and mobile QA absent'],
       'visual_version':p['visual_version'],'probe':probe,'loudness':loudness}
    if p['visual_version']!='1.1': q['live_blocks'].append('archived v1.0 visuals are not v1.1 production masters')
    write(out/'qa.json',q)
    return q

def mock_publish(c, p, video, qa, config):
    safety(config)
    validate_plan(p)
    if qa['plan_digest']!=p['plan_digest'] or qa['video_sha256']!=sha(video): raise ValueError('Stale QA or changed video')
    if not qa['technical_pass'] or not qa['mock_eligible'] or not all(qa['checks'].values()): raise ValueError('QA failed')
    job=c.execute('SELECT plan FROM jobs WHERE id=?',(p['id'],)).fetchone()
    if not job or read_json(job[0])!=p: raise ValueError('No matching persisted plan')
    receipt={'id':'MOCK-'+digest({'plan':p['plan_digest'],'video':sha(video)})[:20],
        'job_id':p['id'],'mode':'mock','at':now(),'plan_digest':p['plan_digest'],
        'video_sha256':sha(video),'external_media_id':None,'network_calls':0,'external_publication':False}
    # Durable receipt and status change are one transaction. No external side effects to retry.
    with c:
        c.execute('INSERT OR IGNORE INTO receipts VALUES(?,?)',(p['id'],json.dumps(receipt)))
        saved=read_json(c.execute('SELECT receipt FROM receipts WHERE job_id=?',(p['id'],)).fetchone()[0])
        if saved['video_sha256']!=sha(video) or saved['plan_digest']!=p['plan_digest']: raise ValueError('Existing receipt binds different content')
        c.execute('UPDATE jobs SET state=? WHERE id=?',('mock_published',p['id']))
    return saved

def metrics(rows):
    # Fixtures only; preserve unknowns and never sum repeated cumulative snapshots.
    latest={}
    for r in rows:
        if r['source']!='synthetic_fixture': raise ValueError('Only labeled synthetic test data accepted')
        if not r['media_id'].startswith('MOCK-'): raise ValueError('Fixture media mismatch')
        if r['age_hours']<0: raise ValueError('Negative age')
        for k in ['reach','likes','comments','saves','shares','average_watch_seconds']:
            if r[k] is not None and r[k]<0: raise ValueError('Negative metric')
        old=latest.get(r['media_id'])
        if old is None or r['age_hours']>old['age_hours']: latest[r['media_id']]=r
    result=[]
    for r in latest.values():
        parts=[r[k] for k in ['likes','comments','saves','shares']]
        engagement=sum(parts)/r['reach'] if r['reach'] and all(x is not None for x in parts) else None
        watch=r['average_watch_seconds']/r['duration'] if r['average_watch_seconds'] is not None and r['duration']>0 else None
        result.append({**r,'engagement_per_reached_account':engagement,'watch_ratio':watch})
    return result

def report(c, run_dir, until):
    receipts=[read_json(x[0]) for x in c.execute('SELECT receipt FROM receipts ORDER BY job_id')]
    pp={x[0]:read_json(x[1]) for x in c.execute('SELECT id,plan FROM jobs')}
    fixtures=[]
    for i,r in enumerate(receipts):
        fixtures.append({'source':'synthetic_fixture','media_id':r['id'],'age_hours':24,
          'duration':pp[r['job_id']]['duration'],'reach':100+i*20,'likes':8+i,'comments':1,'saves':2,'shares':1,'average_watch_seconds':8+i})
    write(run_dir/'metrics-fixture.json',fixtures)
    analysis=metrics(fixtures)
    write(run_dir/'analysis.json',{'synthetic_only':True,'rows':analysis,'real_metrics':None,
        'decision':'Too few independently published real posts; no performance claim or automatic policy change.'})
    text='''# ZF-06 모의 주간 보고\n\n실제 실행 시각(UTC): {at}\n\n시험 논리 시각: {until} (실제 시간이 경과한 주간 실적 아님)\n\n- 로컬 모의 게시 영수증: {count}개\n- 실제 Instagram 게시: 0개\n- 실제 계정 성과: 미수집(null)\n- 지표 계산: 합성 24시간 표본으로만 검증. 실제 도달·시청·매출로 해석 금지.\n- 다음 조치: 실제 청취·기기 검수, 계정 승인, B안 규칙 승인 후 다음 단계 검토.\n- 자동 개선 적용: 없음. 소표본 및 합성 수치에서 우열·추세 결론을 내리지 않음.\n'''.format(at=now(),until=until,count=len(receipts))
    (run_dir/'WEEKLY-REPORT.md').write_text(text,'utf-8')

def weekly_due(c, config, until):
    slot=config['weekly_report_slot']
    if aware(until)<aware(slot): return False
    return c.execute('SELECT 1 FROM weekly_reports WHERE slot=?',(slot,)).fetchone() is None

def run(until,mode='mock',state=None):
    config=read(ROOT/'config.json'); safety(config,mode)
    state=state or ('state-jp-v1' if config.get('production_language')=='ja' else 'state')
    state_dir=confined(ROOT,state); state_dir.mkdir(parents=True,exist_ok=True)
    out=state_dir/'artifacts';out.mkdir(exist_ok=True)
    c=db(state_dir/'workflow.sqlite3')
    existing_languages={json.loads(row[0])['language'] for row in c.execute('SELECT plan FROM jobs')}
    requested=config.get('production_language','ko')
    if existing_languages and existing_languages!={requested}:raise ValueError('Use a separate state folder for a different language')
    pp=plans(config); write(state_dir/'plans.json',pp); seed(c,pp,until)
    event(c,'scheduler','tick',{'logical_clock':until,'wall_clock':now(),'os_scheduler_installed':False})
    for job,plan_raw,status in c.execute('SELECT id,plan,state FROM jobs ORDER BY slot').fetchall():
        p=read_json(plan_raw); dest=out/job
        if aware(p['slot'])>aware(until): continue
        if status=='mock_published':
            mock_publish(c,p,dest/'video.mp4',read(dest/'qa.json'),config)
            event(c,job,'idempotent_replay',{'verified_receipt_and_current_bytes':True})
            continue
        try:
            event(c,job,'planning',{'plan_digest':p['plan_digest'],'slot':p['slot']})
            video=render(p,dest);event(c,job,'production',read(dest/'production.json'))
            q=quality(p,video,dest);event(c,job,'qa',{'technical_pass':q['technical_pass'],'live_eligible':q['live_eligible']})
            receipt=mock_publish(c,p,video,q,config);write(dest/'mock-receipt.json',receipt)
            event(c,job,'mock_publish',receipt)
            print(job,'MOCK complete',flush=True)
        except Exception as e:
            with c: c.execute('UPDATE jobs SET state=? WHERE id=?',('blocked',job))
            event(c,job,'blocked',{'error_type':type(e).__name__,'reason':str(e)})
            raise
    if weekly_due(c,config,until):
        report(c,state_dir,until)
        with c:c.execute('INSERT INTO weekly_reports VALUES(?,?)',(config['weekly_report_slot'],now()))
        event(c,'weekly','weekly_report',{'slot':config['weekly_report_slot'],'synthetic_only':True})
    events=[dict(zip(['seq','at','job_id','stage','detail'],r)) for r in c.execute('SELECT * FROM events ORDER BY seq')]
    write(state_dir/'events.json',events)
    c.close()
    return state_dir

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--until',required=True);p.add_argument('--mode',default='mock');p.add_argument('--state')
    a=p.parse_args();run(a.until,a.mode,a.state)
