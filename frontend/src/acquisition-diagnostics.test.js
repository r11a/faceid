import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { describe, it, expect, vi } from 'vitest';

const html=readFileSync(new URL('../public/acquisition-diagnostics.html',import.meta.url),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
async function fixture(responses){
  const elements={};
  for(const [,id] of html.matchAll(/\bid="([^"]+)"/g)){
    elements[id]={replaceChildren(){this.options=[]},value:'',options:[],listeners:{},addEventListener(name,fn){this.listeners[name]=fn},append(o){this.options.push(o)},
      removeAttribute(name){delete this[name]},getContext(){return {drawImage(){},strokeRect(){}}},
      getBoundingClientRect(){return {left:0,top:0,width:100,height:100}},setPointerCapture(){}};
  }
  elements.source.value='restream';
  ['left','top','right','bottom'].forEach((id,i)=>elements[id].value=[25,10,75,90][i]);
  const fetch=vi.fn(async()=>{const next=responses.shift();return {ok:next.ok!==false,json:async()=>next.body||next}});
  const timer=vi.fn();
  const context=vm.createContext({document:{getElementById:id=>elements[id],createElement:()=>({style:{},remove(){}})},
    location:{href:'http://ha/api/hassio_ingress/example/acquisition-diagnostics'},fetch,
    URL,Blob,Image:class {naturalWidth=100;naturalHeight=100;set src(value){this.value=value;if(value)this.onload?.()}},
    setTimeout:timer,clearTimeout:vi.fn(),console});
  vm.runInContext(script,context);
  for(let i=0;i<15;i++)await Promise.resolve();
  return {elements,fetch,timer};
}

describe('acquisition diagnostic page',()=>{
  it('shows startup sequence and a low-detail warning rather than ready',async()=>{
    const {elements}=await fixture([{running:false,cameras:['Enterance'],
      results:[{mode:'preview',state:'preview_low_detail',timestamp_issue_samples:2}],
      preview:{available:true,revision:1,remaining_seconds:180,kinds:['scene','full_input','crop_input','startup']}}]);
    expect(elements.startupPanel.hidden).toBe(false);
    expect(elements.startupInput.src).toContain('/preview/startup');
    expect(elements.previewState.textContent).toContain('Low-detail picture');
    expect(elements.previewState.textContent).toContain('Timestamp issues are reported separately');
  });
  it('polls startup previews while collection is running',async()=>{
    const {timer}=await fixture([{running:true,current:'preview',cameras:['Enterance'],results:[]}]);
    expect(timer.mock.calls.at(-1)[1]).toBe(2000);
  });
  it('allows a crop selection from a private preview and erases it at expiry',async()=>{
    const {elements,timer,fetch}=await fixture([{running:false,cameras:['Enterance'],results:[],
      preview:{available:true,revision:1,remaining_seconds:30}}]);
    expect(elements.fullInput.src).toContain('/api/hassio_ingress/example/api/acquisition-diagnostics/preview/full_input');
    elements.scene.listeners.pointerdown({clientX:10,clientY:20,pointerId:1});
    elements.scene.listeners.pointerup({clientX:70,clientY:90,pointerId:1});
    expect(['left','top','right','bottom'].map(id=>elements[id].value)).toEqual([10,20,70,90]);
    expect(fetch.mock.calls).toHaveLength(1);
    timer.mock.calls.find(([,ms])=>ms===30000)[0]();
    expect(elements.previewPanel.hidden).toBe(true);expect(elements.scene.width).toBe(0);
    expect(elements.fullInput.src).toBeUndefined();
  });
  it('submits the selected crop only to diagnostic modes',async()=>{
    const {fetch,elements}=await fixture([{running:false,cameras:['Enterance'],results:[]},
      {started:true},{running:true,current:'crop_compare',cameras:['Enterance'],results:[]}]);
    elements.camera.value='Enterance';elements.source.value='crop_compare';elements.direct.value='SECRET';
    await elements.form.listeners.submit({preventDefault(){}});
    const posted=fetch.mock.calls.find(([,o])=>o.method==='POST');
    expect(JSON.parse(posted[1].body)).toEqual({camera:'Enterance',source:'crop_compare',region:[.25,.1,.75,.9]});
    expect(elements.direct.value).toBe('');
  });
  it('does not start a request for an invalid crop',async()=>{
    const {fetch,elements}=await fixture([{running:false,cameras:['Enterance'],results:[]}]);
    elements.source.value='crop_compare';elements.left.value=90;elements.right.value=20;
    await elements.form.listeners.submit({preventDefault(){}});
    expect(fetch.mock.calls).toHaveLength(1);
    expect(elements.status.textContent).toContain('Choose a valid crop');
  });
  it('clearly distinguishes temporary previews from packet-only modes',async()=>{
    const {elements}=await fixture([{running:false,cameras:['Enterance'],results:[]}]);
    elements.source.value='preview';elements.source.listeners.change();
    expect(elements.regionFields.hidden).toBe(false);expect(elements.modeInfo.textContent).toContain('no model inference');
    elements.source.value='direct';elements.source.listeners.change();expect(elements.regionFields.hidden).toBe(true);
    expect(html).not.toContain('automatic recognition continues');
    expect(html).toContain('normal recognition and Access1 remain active');
    expect(html).toContain('does NOT pause cameras');
  });
  it('submits the decode experiment without credentials or automatic start',async()=>{
    const {fetch,elements}=await fixture([{running:false,cameras:['Enterance'],results:[]},
      {started:true},{running:true,current:'decode_chain',cameras:['Enterance'],results:[]}]);
    elements.camera.value='Enterance';elements.source.value='decode_chain';elements.direct.value='SECRET';
    await elements.form.listeners.submit({preventDefault(){}});
    const posted=fetch.mock.calls.find(([,o])=>o.method==='POST');
    expect(JSON.parse(posted[1].body)).toEqual({camera:'Enterance',source:'decode_chain'});
    expect(elements.direct.value).toBe('');
  });
  it('preserves direct source and removes its credential when switching to clock check',async()=>{
    const {elements}=await fixture([{running:false,cameras:['Enterance'],results:[]}]);
    elements.source.value='direct';elements.source.listeners.change();
    expect(elements.directFields.hidden).toBe(false);
    expect(elements.direct.required).toBe(true);
    elements.direct.value='SECRET';elements.source.value='restream_clock';elements.source.listeners.change();
    expect(elements.directFields.hidden).toBe(true);
    expect(elements.direct.required).toBe(false);
    expect(elements.direct.value).toBe('');
  });
  it('sends the bounded clock profile without a direct camera URL',async()=>{
    const {fetch,elements}=await fixture([{running:false,cameras:['Enterance'],results:[]},
      {started:true},{running:true,current:'restream_clock',cameras:['Enterance'],results:[]}]);
    elements.camera.value='Enterance';elements.source.value='restream_clock';elements.direct.value='SECRET';
    await elements.form.listeners.submit({preventDefault(){}});
    const posted=fetch.mock.calls.find(([,o])=>o.method==='POST');
    expect(JSON.parse(posted[1].body)).toEqual({camera:'Enterance',source:'restream_clock'});
    expect(elements.direct.value).toBe('');
  });
  it('uses the current ingress base and never starts acquisition on load',async()=>{
    const {fetch,elements}=await fixture([{running:false,cameras:['Enterance'],results:[]}]);
    expect(fetch.mock.calls[0][0].pathname).toBe('/api/hassio_ingress/example/api/acquisition-diagnostics');
    expect(fetch.mock.calls).toHaveLength(1);
    expect(fetch.mock.calls[0][1].method).toBeUndefined();
    expect(elements.start.disabled).toBe(false);
    expect(elements.camera.options[0].textContent).toBe('Enterance');
  });
  it('clears credentials and posts a single sequential comparison request',async()=>{
    const {fetch,elements,timer}=await fixture([{running:false,cameras:['Enterance'],results:[]},
      {started:true},{running:true,current:'restream',cameras:['Enterance'],results:[]}]);
    elements.camera.value='Enterance';elements.source.value='both';elements.direct.value='rtsp://user:SECRET@10.0.0.100:554/ch01/00';
    await elements.form.listeners.submit({preventDefault(){}});
    expect(elements.direct.value).toBe('');
    const posted=fetch.mock.calls.filter(([,o])=>o.method==='POST');
    expect(posted).toHaveLength(1);
    expect(JSON.parse(posted[0][1].body).source).toBe('both');
    expect(posted[0][1].headers['X-Access1-Pairing']).toBe('1');
    expect(elements.results.textContent).not.toContain('SECRET');
    expect(elements.start.disabled).toBe(true);
    expect(timer.mock.calls.at(-1)[1]).toBe(5000);
  });
  it('fails closed on unavailable administrator verification',async()=>{
    const {elements}=await fixture([{ok:false,body:{detail:'Verified administrator required'}}]);
    expect(elements.start.disabled).toBe(true);
    expect(elements.status.textContent).toBe('Verified administrator required');
  });
});
