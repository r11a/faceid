"""Bounded, disposable CPU sessions; never reconfigure the installed engine."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def prepare(engine,frame,face):
    import cv2
    from insightface.utils.face_align import norm_crop
    model=engine.app.models['recognition']
    aligned=norm_crop(frame,landmark=face.kps,image_size=model.input_size[0])
    ok,data=cv2.imencode('.png',aligned)
    if not ok:raise ValueError('alignment_failed')
    return {'model':str(Path(model.model_file).resolve()),
            'image':base64.b64encode(data).decode('ascii'),
            'reference':face.normed_embedding.tolist(),'allow_spinning':engine.cpu_allow_spinning}


def run(payload,allowed=lambda:True):
    report={'purpose':'isolated_same_face_cpu_comparison','settings_changed':False,
            'runs_per_setting':2,'warmup_runs_per_setting':1,'results':[],
            'limits':'15 seconds wall and 30 CPU seconds per setting; 3 GiB address space; sequential low-priority children.',
            'note':'Exploratory timings include no access decision. These are not end-to-end opening times.'}
    if payload is None:return {**report,'state':'not_run_no_usable_live_face'}
    report['allow_spinning']=bool(payload.get('allow_spinning',True))
    begin=time.monotonic()
    env=os.environ.copy();env.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
    for threads in (2,4):
        if not allowed():report['state']='safety_changed';break
        data={**payload,'threads':threads}
        try:
            result=subprocess.run([sys.executable,str(Path(__file__).resolve())],
                input=json.dumps(data),text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                timeout=15,env=env,check=False)
            entry=json.loads(result.stdout.splitlines()[-1]) if result.returncode==0 else {'state':'worker_failed'}
            # Allowlist only aggregate metrics; never echo model path, face pixels or vectors.
            entry={k:v for k,v in entry.items() if k in ('state','threads','startup_ms','cold_first_ms',
                  'warm_runs_ms','median_ms','min_cosine_to_reference','numerically_consistent')}
            report['results'].append({'threads':threads,**entry})
        except subprocess.TimeoutExpired:report['results'].append({'threads':threads,'state':'wall_time_limit'})
        except Exception:report['results'].append({'threads':threads,'state':'worker_failed'})
    report.setdefault('state','completed' if all(x.get('state')=='completed' for x in report['results']) else 'incomplete')
    report['elapsed_ms']=round((time.monotonic()-begin)*1000,2)
    return report


def child():
    # Limits apply only to this disposable benchmark process, before native imports.
    import resource
    os.nice(10)
    resource.setrlimit(resource.RLIMIT_CPU,(30,31))
    resource.setrlimit(resource.RLIMIT_AS,(3*1024**3,3*1024**3))
    raw=sys.stdin.read(262145)
    if len(raw)>262144:raise ValueError('input_limit')
    payload=json.loads(raw)
    if payload['threads'] not in (2,4):raise ValueError('threads')
    import contextlib
    import io
    import statistics
    import cv2
    import numpy as np
    import onnxruntime as ort
    from insightface.model_zoo import get_model
    image=cv2.imdecode(np.frombuffer(base64.b64decode(payload['image']),dtype=np.uint8),cv2.IMREAD_COLOR)
    if image is None or image.shape!=(112,112,3):raise ValueError('image')
    reference=np.asarray(payload['reference'],dtype=np.float32).reshape(-1)
    if reference.size!=512 or not np.isfinite(reference).all():raise ValueError('reference')
    options=ort.SessionOptions();options.intra_op_num_threads=payload['threads']
    options.add_session_config_entry('session.intra_op.allow_spinning','1' if payload.get('allow_spinning',True) else '0')
    options.add_session_config_entry('session.inter_op.allow_spinning','1' if payload.get('allow_spinning',True) else '0')
    started=time.monotonic()
    with contextlib.redirect_stdout(io.StringIO()):
        model=get_model(payload['model'],providers=['CPUExecutionProvider'],sess_options=options)
    startup=(time.monotonic()-started)*1000
    elapsed=[];cosines=[]
    for _ in range(3):
        begin=time.monotonic();vector=model.get_feat(image).reshape(-1)
        elapsed.append(round((time.monotonic()-begin)*1000,2))
        if vector.size!=512 or not np.isfinite(vector).all():raise ValueError('output')
        cosines.append(float(np.clip(np.dot(vector,reference)/max(1e-12,np.linalg.norm(vector)*np.linalg.norm(reference)),-1.,1.)))
    print(json.dumps({'state':'completed','threads':payload['threads'],'startup_ms':round(startup,2),
        'cold_first_ms':elapsed[0],'warm_runs_ms':elapsed[1:], 'median_ms':round(statistics.median(elapsed[1:]),2),
        'min_cosine_to_reference':round(min(cosines),8),'numerically_consistent':min(cosines)>=.99999}))


if __name__=='__main__':
    try:child()
    except Exception:print(json.dumps({'state':'worker_failed'}))
