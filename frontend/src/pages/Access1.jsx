import React from 'react';
import { api } from '../api.js';
import { useResource } from '../hooks.js';
import { Badge, ErrorState, Loading, PageHeader, Panel, dateTime } from '../ui.jsx';

export function Access1Page() {
  const status=useResource('access1', { poll: 5000, realtime: true });
  const connection=useResource('access1/connection', { poll: 5000 });
  const connected=!connection.error && connection.data?.state==='connected';
  const [lang,setLang]=React.useState('he');
  const tr=(en,he)=>lang==='he'?he:en;
  const [setup,setSetup]=React.useState(false);
  const [setupError,setSetupError]=React.useState('');
  const [busy,setBusy]=React.useState(false);
  const [code,setCode]=React.useState('');
  const [step,setStep]=React.useState('start');
  const [copied,setCopied]=React.useState(false);
  const [securityError,setSecurityError]=React.useState('');
  const [secureOverride,setSecureOverride]=React.useState(null);
  const secure=secureOverride ?? (status.data?.secure !== false);
  async function changeSecurity(){
    const next=!secure;
    if(!next&&!window.confirm(tr(
      'Turn Secure off? FaceID may submit recognition from Frigate without verifying its TLS certificate. An impersonated Frigate server could supply false camera evidence and cause an unauthorized access request. FaceID–Access1 TLS, account authentication, freshness, liveness and door permissions remain required. I understand and approve this risk.',
      'לכבות Secure? FaceID יוכל לשלוח זיהוי מ־Frigate ללא אימות תעודת TLS שלו. שרת מתחזה עלול לספק תמונות כוזבות ולגרום לבקשת גישה לא מורשית. TLS בין FaceID ל־Access1, אימות חשבון, עדכניות, חיוּת והרשאות דלת נשארים חובה. אני מבין ומאשר את הסיכון.'
    )))return;
    setBusy(true);setSecurityError('');
    try{
      const result=await api('access1/security',{method:'POST',
        headers:{'Content-Type':'application/json','X-Access1-Pairing':'1'},
        body:JSON.stringify({secure:next,risk_acknowledged:!next})});
      setSecureOverride(result.secure);status.reload();
    }catch(error){setSecurityError(error.status===403?tr('Open from the HA sidebar as administrator.','פתחו מסרגל HA כמנהל.'):tr('Secure setting was not saved. Refresh and try again.','הגדרת Secure לא נשמרה. רעננו ונסו שוב.'));}
    finally{setBusy(false);}
  }
  React.useEffect(()=>{setSecureOverride(null);},[status.data?.secure]);
  const request=async(path,body={})=>api('access1/pairing/'+path,{
    method:'POST',headers:{'Content-Type':'application/json','X-Access1-Pairing':'1'},
    body:JSON.stringify(body)});
  const reasons={
    evidence_not_live:tr('Recognition used a snapshot or recorded clip; no live access request sent.','הזיהוי השתמש בתמונה או בקליפ מוקלט; לא נשלחה בקשת גישה חיה.'),
    event_already_ended:tr('The Frigate event ended before submission.','אירוע Frigate הסתיים לפני השליחה.'),
    evidence_expired_or_missing:tr('Frame timestamp is missing, in the future, or at least 10 seconds old.','זמן התמונה חסר, בעתיד, או ישן ב־10 שניות לפחות.'),
    liveness_unconfirmed:tr('Liveness has not been confirmed.','החיות טרם אושרה.'),
    event_id_missing:tr('The recognition event ID is missing.','מזהה אירוע הזיהוי חסר.'),
    camera_transport_untrusted:tr('Camera transport requirements failed. Check HTTPS, Frigate login and the Secure setting.','דרישות חיבור מצלמה נכשלו. בדקו HTTPS, כניסה ל־Frigate והגדרת Secure.'),
    disabled_stale_or_liveness_unconfirmed:tr('Connector disabled, evidence not live/fresh, or liveness not confirmed.','המחבר כבוי, התמונה אינה חיה ועדכנית, או שהחיוּת לא אושרה.'),
    connector_disabled:tr('Access1 connector is not connected.','המחבר ל־Access1 אינו מחובר.'),
    camera_disabled:tr('This camera is disabled in FaceID.','מצלמה זו כבויה ב־FaceID.'),
    refresh_camera_directory:tr('Refresh the camera mapping in Access1.','רעננו את מיפוי המצלמה ב־Access1.'),
    identity_changed:tr('The person identity changed. Check the user mapping.','זהות האדם השתנתה. בדקו את מיפוי המשתמש.'),
    processing_failed:tr('Connector processing failed; the event will not be replayed automatically.','עיבוד המחבר נכשל; האירוע לא יישלח שוב אוטומטית.')
  };
  function setupFailure(error){
    const diagnostic=/^Access1 pairing: stage=(dns|tls_config|connect|tls|request|response|decode) reason=(dns_failed|private_address_required|certificate_verification_failed|tls_failed|connection_refused|timeout|invalid_response|response_too_large|invalid_status|network_failed|operation_failed|http_[1-5][0-9]{2})$/.exec(error.message||'');
    if(diagnostic){
      const reason=diagnostic[2];
      const messages={
        dns_failed:tr("Access1 hostname could not be resolved.","לא ניתן לאתר את שם המארח של Access1."),
        private_address_required:tr("Access1 must resolve to a private addon address.","Access1 חייב להפנות לכתובת פנימית של התוסף."),
        certificate_verification_failed:tr("Access1 certificate verification failed.","אימות התעודה של Access1 נכשל."),
        tls_failed:tr("The secure TLS connection failed.","חיבור TLS המאובטח נכשל."),
        connection_refused:tr("Access1 refused the connection.","Access1 סירב לחיבור."),
        timeout:tr("Access1 connection timed out.","תם הזמן להמתנה לחיבור Access1."),
        http_410:tr("Code expired. Create a new code in Access1.","הקוד פג. צרו קוד חדש ב־Access1.")
      };
      const message=messages[reason]||(reason.startsWith('http_')?
        tr("Access1 returned HTTP ","Access1 החזיר HTTP ")+reason.slice(5):
        tr("Connection could not be verified.","לא ניתן לאמת את החיבור."));
      return message+' ['+diagnostic[1]+' / '+reason+']';
    }
    if(error.status===403)return tr("Open from the HA sidebar as administrator.","פתחו מסרגל HA כמנהל.");
    if(error.status===503)return tr("Administrator or secure connection unavailable.","אימות מנהל או חיבור מאובטח אינם זמינים.");
    if(error.status===410)return tr("Code expired. Start again in Access1.","הקוד פג. התחילו שוב ב־Access1.");
    return tr("Connection not completed.","החיבור לא הושלם.");
  }
  async function checkConnection(){
    setBusy(true);setSetupError('');
    try{connection.setData(await api('access1/connection'));}
    catch(_){connection.setData({state:'unavailable'});}
    finally{setBusy(false);}
  }
  async function act(action){
    setBusy(true);setSetupError('');
    try{
      if(action==='start'){
        await api('access1/pairing/admin');setSetup(true);
        if(status.data.enabled){
          const result=await request('finish');
          setStep(result.state==='connected'?'connected':result.state==='idle'?'code':'review');
        }
        else setStep('code');
      }else if(action==='submit'){
        await request('submit',{code:code.trim()});setCode('');setStep('review');
      }else if(action==='finish'){
        const result=await request('finish');
        setStep(result.state==='connected'?'connected':'review');
        if(result.state==='connected')status.reload();
      }else if(action==='disconnect'){
        if(!window.confirm(tr("Disconnect FaceID from Access1?","לנתק את FaceID מ־Access1?")))return;
        await request('disconnect');setStep('code');status.reload();
      }
    }catch(error){setSetupError(setupFailure(error));}
    finally{setBusy(false);}
  }
  React.useEffect(()=>{
    if(step!=='review')return;
    let stopped=false,active=false;
    const timer=setInterval(async()=>{
      if(active||stopped)return;active=true;
      try{
        const result=await api('access1');
        if(!stopped&&result.state==='ready'){setStep('connected');status.reload();}
        if(!stopped&&result.state==='pairing_expired'){setStep('code');setSetupError(tr("Code expired. Start again in Access1.","הקוד פג. התחילו שוב ב־Access1."));}
        if(!stopped&&result.state==='pairing_failed'){setStep('code');setSetupError(tr("Setup failed. Create a new code in Access1.","החיבור נכשל. צרו קוד חדש ב־Access1."));}
      }catch(error){
        if(!stopped){setSetupError(setupFailure(error));clearInterval(timer);}
      }finally{active=false;}
    },3000);
    return()=>{stopped=true;clearInterval(timer);};
  },[step]);
  if(status.loading)return <Loading />;
  if(status.error)return <ErrorState error={status.error} retry={status.reload} />;
  return <div dir={lang==='he'?'rtl':'ltr'}>
    <button type="button" className="button secondary" onClick={()=>setLang(lang==='he'?'en':'he')}>{lang==='he'?'English':'עברית'}</button>
    <PageHeader eyebrow="Access1" title={tr("Connect Access1","חיבור Access1")} description={tr("FaceID identifies. Access1 decides.","FaceID מזהה. Access1 מחליט.")} />
    <Panel>
      <Badge tone={connected?'success':'warning'}>
        {connected?tr('Connected','מחובר'):step==='review'?tr('Waiting for Access1 approval','ממתין לאישור Access1'):connection.loading?tr('Checking connection…','בודק חיבור…'):status.data.enabled?tr('Connection unavailable','החיבור אינו זמין'):tr('Setup','הגדרה')}
      </Badge>
      {secure && status.data.camera_evidence_ready === false && <p>{tr(
        "Access requests stay blocked until Frigate provides authenticated images over verified HTTPS, with confirmed liveness.",
        "בקשות גישה חסומות עד לקבלת תמונות מ־Frigate בחיבור HTTPS מאומת ומזוהה, עם חיוּת מאושרת."
      )}</p>}
      <p>{tr("Instance ID","מזהה התקנה")}</p>
      <div style={{display:'flex',gap:8,flexWrap:'wrap',alignItems:'center'}}>
        <code style={{overflowWrap:'anywhere'}}>{status.data.instance_id}</code>
        <button className="button secondary" type="button" onClick={async()=>{
          try{await navigator.clipboard.writeText(status.data.instance_id);setCopied(true);}
          catch(_){setSetupError(tr("Select the ID and copy.","סמנו והעתיקו את המזהה."));}
        }}>{copied?tr("Copied","הועתק"):tr("Copy","העתקה")}</button>
      </div>
      {status.data.enabled&&<button className="button primary" type="button" disabled={busy} onClick={checkConnection}>{tr('Check connection','בדיקת חיבור')}</button>}
      {!setup&&!status.data.enabled&&<button className="button primary" type="button" disabled={busy} onClick={()=>act('start')}>{tr('Connect to Access1','חיבור ל־Access1')}</button>}
      {status.data.enabled&&<p>{tr('Connection is checked automatically every five seconds while this tab is open.','החיבור נבדק אוטומטית כל חמש שניות כשהלשונית פתוחה.')}</p>}
      {setup&&step==='code'&&<>
        <p>{tr("Paste the setup code from Access1.","הדביקו את קוד החיבור מ־Access1.")}</p>
        <textarea aria-label="Access1 setup code" value={code} onChange={e=>setCode(e.target.value)}
          autoComplete="off" spellCheck={false} rows={3} maxLength={16000}
          style={{width:'100%',boxSizing:'border-box',direction:'ltr',overflowWrap:'anywhere'}} />
        <button className="button primary" type="button" disabled={busy||!code.trim()} onClick={()=>act('submit')}>{tr("Submit","שליחה")}</button>
      </>}
      {step==='review'&&<p role="status">{tr("Return to Access1 and press Approve connection.","חזרו ל־Access1 ולחצו אישור חיבור.")}</p>}
      {step==='connected'&&<p role="status">{tr("Next in Access1: cameras in Doors; faces in user cards.","כעת ב־Access1: מצלמות בלשונית דלתות; פנים בכרטיסי משתמשים.")}</p>}
      {setupError&&<div role="alert" style={{marginBlock:12,padding:12,border:'1px solid var(--red)',borderRadius:8,color:'var(--text)',background:'var(--red-soft)'}}>
        {setupError}<button type="button" className="button secondary" disabled={busy} onClick={()=>act(step==='review'?'finish':'start')}>{tr("Retry","ניסיון נוסף")}</button>
      </div>}
      {(setup||status.data.enabled)&&<details style={{marginTop:16}}><summary>{tr("Connection settings","הגדרות חיבור")}</summary>
        <button type="button" className="button secondary" disabled={busy} onClick={()=>act('disconnect')}>{tr("Disconnect","ניתוק")}</button>
      </details>}
    </Panel>
    <Panel>
      <h2>{tr('Connector settings','הגדרות מחבר')}</h2>
      <button type="button" role="switch" aria-label="Secure" aria-checked={secure} disabled={busy}
        className="button secondary" onClick={changeSecurity}>Secure: {secure?'ON':'OFF'}</button>
      <p>{tr('Requires verified Frigate HTTPS for access submissions. This switch does not change either TLS connection or Frigate settings.',
        'דורש HTTPS מאומת של Frigate לשליחת בקשות גישה. המתג אינו משנה את חיבורי TLS או את הגדרות Frigate.')}</p>
      {!secure&&<p role="note">{tr('Secure is OFF: Frigate certificate verification is not required by this connector. Only use camera evidence from a network you trust.',
        'Secure כבוי: המחבר אינו דורש אימות תעודת Frigate. השתמשו רק בנתוני מצלמה מרשת מהימנה.')}</p>}
      {securityError&&<p role="alert">{securityError}</p>}
      <p>{tr('Camera transport requirements: ','דרישות חיבור מצלמה: ')}{status.data.camera_submission_ready?tr('Met — each event still needs freshness and liveness checks.','תקינות — כל אירוע עדיין דורש בדיקות עדכניות וחיוּת.'):tr('Not met. HTTPS and an authenticated Frigate account are still required.','לא תקינות. HTTPS וחשבון Frigate מאומת עדיין נדרשים.')}</p>
      {status.data.last_submission&&<p>{tr('Last connector result: ','תוצאת מחבר אחרונה: ')}{status.data.last_submission.state} · {reasons[status.data.last_submission.reason]||status.data.last_submission.reason||status.data.last_submission.request_id}</p>}
    </Panel>
    <Panel><h2>{tr("Access1 results","תוצאות Access1")}</h2>{(status.data.attempts||[]).map(attempt=><div key={attempt.request_id} style={{padding:8}}>
      <span>{dateTime(attempt.ts)} · </span><b>{attempt.state}</b>
    </div>)}</Panel>
  </div>;
}
