"""Bounded visual startup inspection. Detail is not liveness or capture freshness."""
import time
import cv2
import numpy as np
from .crop_diagnostics import preview_images,validate_region


def frame_detail(frame):
    small=cv2.resize(frame,(160,120),interpolation=cv2.INTER_AREA)
    gray=cv2.cvtColor(small,cv2.COLOR_BGR2GRAY)
    lo,hi=np.percentile(gray,[5,95]);std=float(gray.std())
    return {'gray_std':round(std,2),'gray_p05':round(float(lo),2),
            'gray_p95':round(float(hi),2),'gray_mean':round(float(gray.mean()),2),
            'detail':'low_detail' if hi-lo<12 or std<4 else 'structured'}


def contact_sheet(tiles):
    sheet=np.zeros((2*264,2*320,3),np.uint8)
    for index,(image,label) in sorted(tiles.items()):
        x=(index%2)*320;y=(index//2)*264
        h,w=image.shape[:2];scale=min(320/w,232/h)
        resized=cv2.resize(image,(max(1,int(w*scale)),max(1,int(h*scale))))
        sheet[y:y+resized.shape[0],x:x+resized.shape[1]]=resized
        cv2.putText(sheet,label,(x+5,y+250),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1)
    return sheet


def inspect_startup(processor,camera,url,region,preview,reader_factory,blockers,*,seconds=12,sample_interval=.5):
    from urllib.parse import urlsplit
    report={'mode':'preview','frames_saved':0,'access1_submission':'not_implemented',
            'freshness_proven':False,'admission_eligible':False,'samples':[],
            'observation_window_seconds':seconds,'preview_retention_seconds':180,
            'detail_heuristic':{'sample_size':[160,120],'minimum_p95_minus_p05':12,'minimum_gray_std':4},
            'detail_check':'Heuristic image structure only; not decoder integrity, a face, liveness or capture-age proof.'}
    reasons=blockers(processor,camera)
    if reasons:return {**report,'blockers':reasons,'state':'preview_blocked'}
    parsed=urlsplit(url)
    if parsed.scheme!='rtsp' or parsed.username or parsed.password:
        return {**report,'error':'anonymous_configured_restream_required','state':'preview_blocked'}
    region=validate_region(region)
    reader=reader_factory(url,seconds=seconds);begin=time.monotonic();reader.thread.start()
    next_sample=begin;last_seq=0;first=None;tiles={};last_emit=0.;last_images=None
    try:
        while time.monotonic()-begin<seconds and len(report['samples'])<24:
            reasons=blockers(processor,camera)
            if reasons:
                report.update(blockers=reasons,state='preview_blocked');break
            now=time.monotonic()
            if now<next_sample:time.sleep(min(.05,max(0,next_sample-now)));continue
            item,stats=reader.take()
            if item is None or item[0]==last_seq:
                if stats.get('finished'):break
                time.sleep(.05);continue
            seq,received,pts,pts_reason,frame=item
            last_seq=seq;next_sample=now+sample_interval
            if first is None:first=now
            elapsed=now-begin
            sample={'sequence':seq,'elapsed_ms':round(elapsed*1000,2),
                    'timestamp_status':pts_reason or 'progressing',
                    'arrival_age_ms':round(max(0,now-received)*1000,2),**frame_detail(frame)}
            report['samples'].append(sample)
            report['resolution']=[frame.shape[1],frame.shape[0]];report['region']=region
            # Four bounded samples: first, around 3s, around 6s, and the latest.
            index=min(3,int((now-first)/3))
            if index not in tiles or index==3:
                h,w=frame.shape[:2];scale=min(320/w,232/h)
                thumb=cv2.resize(frame,(max(1,int(w*scale)),max(1,int(h*scale))))
                tiles[index]=(thumb,f"{elapsed:.1f}s {'flat' if sample['detail']=='low_detail' else 'detail'} / {'PTS issue' if pts_reason else 'PTS OK'}")
            # Display successive images even when all are low-detail. Never label them ready.
            if last_images is None or now-last_emit>=1:
                last_images=preview_images(frame,region,getattr(processor.engine,'det_size',640))
                last_images['startup']=contact_sheet(tiles)
                preview(last_images);last_emit=now
        samples=report['samples'];tail=samples[-3:]
        report['last_sample_age_ms']=round((time.monotonic()-begin)*1000-samples[-1]['elapsed_ms'],2) if samples else None
        if 'state' not in report:
            stable=len(tail)==3 and tail[-1]['elapsed_ms']-tail[0]['elapsed_ms']>=900
            settled=bool(samples) and samples[-1]['elapsed_ms']-samples[0]['elapsed_ms']>=2000
            if not samples:report['state']='no_decoded_frame'
            elif report['last_sample_age_ms']>2000:report['state']='preview_inconclusive'
            elif stable and settled and all(s['detail']=='structured' and s['arrival_age_ms']<=1000 for s in tail):
                report['state']='preview_detail_recovered' if samples[0]['detail']=='low_detail' else 'preview_detail_observed'
            elif stable and settled and all(s['detail']=='low_detail' for s in tail):report['state']='preview_low_detail'
            else:report['state']='preview_inconclusive'
        report['stop_reason']='safety_changed' if report['state']=='preview_blocked' else ('sample_limit' if len(samples)>=24 else 'window_finished' if time.monotonic()-begin>=seconds else 'decoder_finished')
        if samples:
            report['first_structured_ms']=next((s['elapsed_ms'] for s in samples if s['detail']=='structured'),None)
            report['timestamp_issue_samples']=sum(s['timestamp_status']!='progressing' for s in samples)
            report['low_detail_samples']=sum(s['detail']=='low_detail' for s in samples)
            if report['state']!='preview_blocked' and not blockers(processor,camera):
                images=preview_images(frame,region,getattr(processor.engine,'det_size',640))
                images['startup']=contact_sheet(tiles)
                preview(images)
    except Exception:report.update(error='preview_failed',state='preview_inconclusive')
    finally:
        reader.stop();_,report['decoder']=reader.take()
        if report['decoder'].get('error') or report['decoder'].get('decoder_stopped') is not True:
            report['visual_state_before_decoder_check']=report.get('state')
            if report.get('state')!='preview_blocked':report['state']='preview_inconclusive'
        report['elapsed_ms']=round((time.monotonic()-begin)*1000,2)
    return report
