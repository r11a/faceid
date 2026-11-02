"""Diagnostic previews and same-frame comparison; no production configuration writes."""
import copy
import math
import threading
import time

import cv2
import numpy as np
from fastapi import HTTPException


def validate_region(value):
    if not isinstance(value,list) or len(value)!=4:
        raise ValueError('invalid_region')
    if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in value):
        raise ValueError('invalid_region')
    l,t,r,b=value
    if r-l < .1-1e-9 or b-t < .1-1e-9:raise ValueError('region_too_small')
    return list(value)


def crop_pixels(frame, region):
    h,w=frame.shape[:2];l,t,r,b=validate_region(region)
    x,y=int(l*w),int(t*h)
    right,bottom=min(w,int(r*w)),min(h,int(b*h))
    return frame[y:bottom,x:right],x,y


def detector_input(frame, size=640):
    h,w=frame.shape[:2]
    if h/w > 1:new_h,new_w=size,int(size/(h/w))
    else:new_w,new_h=size,int(size*(h/w))
    output=np.zeros((size,size,3),dtype=np.uint8)
    output[:new_h,:new_w]=cv2.resize(frame,(new_w,new_h))
    return output


def compare(engine, frame, region, crop_first=False):
    crop,x,y=crop_pixels(frame,region)
    found={};timings={}
    for key,image in ([('crop',crop),('full',frame)] if crop_first else [('full',frame),('crop',crop)]):
        begin=time.monotonic();found[key]=engine.detect_faces(image)
        timings[key+'_ms']=round((time.monotonic()-begin)*1000,2)
    mapped=[]
    for face in found['crop']:
        # InsightFace Face is a dict whose __getattr__ returns None for missing
        # pickle hooks; copy.copy invokes those hooks and fails on installed 2.1.
        item=type(face)(dict(face)) if isinstance(face,dict) else copy.copy(face)
        item.bbox=np.asarray(face.bbox).copy()+np.array([x,y,x,y])
        if getattr(face,'kps',None) is not None:item.kps=np.asarray(face.kps).copy()+np.array([x,y])
        mapped.append(item)
    # Retain full-frame ambiguity, including a person outside the chosen crop.
    chosen=found['full'];selected='full'
    if len(chosen)==0:chosen=mapped;selected='crop'
    elif len(chosen)==1 and len(mapped)>1:chosen=mapped;selected='crop'
    elif len(chosen)==1 and len(mapped)==1:
        a,b=chosen[0].bbox,mapped[0].bbox
        intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
        union=max(1,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection)
        if intersection/union < .2:chosen=[chosen[0],mapped[0]];selected='conflicting_locations'
    result={**timings,'full_faces':len(found['full']),'crop_faces':len(mapped),'selected':selected,
            'region':region,'crop_size':[crop.shape[1],crop.shape[0]],
            'order':'crop_first' if crop_first else 'full_first',
            'comparison_only_overhead_ms':timings['crop_ms' if selected=='full' else 'full_ms']}
    return chosen,result


def preview_images(frame, region, size=640):
    crop,x,y=crop_pixels(frame,region)
    scale=min(1.,960/frame.shape[1]);full=cv2.resize(frame,None,fx=scale,fy=scale)
    return {'scene':full,'full_input':detector_input(frame,size),'crop_input':detector_input(crop,size)}


class PreviewCache:
    """Owner-only, bounded JPEGs in RAM. Never part of downloadable reports."""
    def __init__(self,ttl=180):
        self.lock=threading.Lock();self.items={};self.owner=None;self.until=0.;self.ttl=ttl;self.timer=None;self.revision=0
    def clear(self):
        with self.lock:
            if self.timer:self.timer.cancel()
            self.items={};self.owner=None;self.until=0.
    def _expire(self):
        with self.lock:
            if time.monotonic()>=self.until:self.items={};self.owner=None
    def put(self,owner,images):
        encoded={}
        for key,image in images.items():
            if key not in ('scene','full_input','crop_input','startup'):continue
            ok,data=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,85])
            if ok and data.nbytes<=750_000:encoded[key]=data.tobytes()
        with self.lock:
            if self.timer:self.timer.cancel()
            self.revision+=1;self.owner=owner;self.items=encoded;self.until=time.monotonic()+self.ttl
            self.timer=threading.Timer(self.ttl,self._expire);self.timer.daemon=True;self.timer.start()
    def get(self,owner,kind):
        with self.lock:
            if self.owner!=owner:raise HTTPException(403,'Preview belongs to another administrator')
            if time.monotonic()>=self.until:
                self.items={};raise HTTPException(410,'Preview expired')
            if kind not in self.items:raise HTTPException(404,'Preview unavailable')
            return self.items[kind]
    def status(self,owner):
        with self.lock:
            return {'kinds':sorted(self.items) if self.owner==owner and time.monotonic()<self.until else [],'revision':self.revision if self.owner==owner else 0,'available': bool(self.items) and self.owner==owner and time.monotonic()<self.until,
                    'remaining_seconds':max(0,int(self.until-time.monotonic())) if self.owner==owner else 0}
