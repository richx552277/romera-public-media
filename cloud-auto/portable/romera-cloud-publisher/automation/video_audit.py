"""Inspect every decoded frame against the rendered caption source.

This measures sequence integrity and mobile preview geometry. It makes no claim
about natural Japanese, actual device use, listening, or legal acceptance.
"""
from pathlib import Path
import argparse, json, subprocess
import numpy as np
from PIL import Image
import engine as e

def audit(folder):
    folder=Path(folder);p=e.read(folder/'plan.json');w=e.renderer()
    references=[]
    for i in range(1,5):
        with Image.open(folder/f'scene-{i}.png') as im:
            references.append(np.asarray(im.convert('RGB').resize((360,640))).astype(np.float32))
    expected=[i for i,s in enumerate(p['scenes']) for _ in range(round(s['seconds']*30))]
    proc=subprocess.Popen([str(w.FFMPEG),'-v','error','-xerror','-threads','2','-i',str(folder/'video.mp4'),
        '-vf','scale=360:640','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    errors=[];samples=[];frame_count=0;size=360*640*3
    try:
        while True:
            data=proc.stdout.read(size)
            if not data:break
            if len(data)!=size:raise ValueError('Partial decoded frame')
            if frame_count>=len(expected):raise ValueError('Unexpected extra frames')
            actual=np.frombuffer(data,np.uint8).reshape((640,360,3)).astype(np.float32)
            reference=references[expected[frame_count]]
            # Text and top safe zone are static; the character intentionally bobs.
            error=float(np.mean((actual[100:210,35:325]-reference[100:210,35:325])**2))
            errors.append(error)
            if frame_count%30==0:samples.append(Image.fromarray(actual.astype(np.uint8)).resize((180,320)))
            frame_count+=1
        stderr=proc.stderr.read(10000);rc=proc.wait(timeout=30)
    finally:
        proc.stdout.close();proc.stderr.close()
        if proc.poll() is None:proc.kill();proc.wait()
    strip=Image.new('RGB',(7*180,2*320),'#F6F0E6')
    for i,im in enumerate(samples[:14]):strip.paste(im,((i%7)*180,(i//7)*320))
    strip.save(folder/'full-sequence.jpg',quality=95)
    value={'at_utc':e.now(),'video_sha256':e.r.sha(folder/'video.mp4'),'decoded_frames_checked':frame_count,
        'expected_frames':len(expected),'caption_region_mse_max':max(errors,default=None),
        'caption_region_mse_limit':80,'every_caption_region_matches':bool(errors) and max(errors)<=80,
        'decode_success':rc==0 and not stderr,'frame_count_match':frame_count==len(expected),
        'entire_sequence_technical_pass':rc==0 and not stderr and frame_count==len(expected) and bool(errors) and max(errors)<=80,
        'mobile_preview':'360x640','caption_nominal_px':62/3,'actual_device_verified':False,
        'human_full_playback_or_listening':False,'semantic_qa_replaced':False}
    e.write(folder/'full-sequence-qa.json',value)
    return value

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);args=parser.parse_args()
    print(json.dumps(audit(args.folder),ensure_ascii=True))
