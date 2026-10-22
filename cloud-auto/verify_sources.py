from pathlib import Path
import hashlib,json
r=Path(__file__).resolve().parents[1]
m=json.loads((r/'cloud-auto/source-manifest.json').read_text('utf-8'))
fail=[]
for x in m['files']:
    if hashlib.sha256((r/x['path']).read_bytes()).hexdigest()!=x['sha256']:fail.append(x['path'])
print(json.dumps({'source_files':len(m['files']),'mismatches':fail}))
if fail:raise SystemExit(2)
