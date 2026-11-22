import React, { useEffect, useState } from "react";
import { Camera, CheckCircle2, CircleAlert, DoorOpen, Play, Plug, RotateCcw, ScanFace, Square, Users } from "lucide-react";
import { api } from "../api.js";
import { useResource } from "../hooks.js";
import { Access1Page } from "./Access1.jsx";
import { PeoplePage } from "./Daily.jsx";
import { LiveFrame } from "./Security.jsx";
import { Badge, ErrorState, Loading, PageHeader, Panel, percent, useToast } from "../ui.jsx";

// 6.9.1 guided setup. Every setup call is admin-only on the server; writes carry the setup confirm header.
export const setupApi = (path, body) => api(`setup/${path}`, body === undefined ? {} : {
  method: "POST", headers: { "Content-Type": "application/json", "X-FaceID-Setup": "1" }, body: JSON.stringify(body),
});

export const STEPS = [
  ["connect", "חיבור", Plug], ["cameras", "בחירת מצלמות", Camera], ["view", "בדיקת תמונה", ScanFace],
  ["people", "רישום אנשים", Users], ["access1", "Access1 (אופציונלי)", DoorOpen], ["review", "סקירה ובדיקה", CheckCircle2],
];
const CAMERA_WARNINGS = {
  no_restream: "אין Restream ב־Frigate: זיהוי מהיר בזרם לא יהיה זמין במצלמה זו",
  camera_disabled: "המצלמה כבויה ב־Frigate",
  snapshots_disabled: "Snapshots כבויים ב־Frigate",
  person_not_tracked: "Frigate לא עוקב אחרי person במצלמה זו",
};
const NOTES = {
  low_resolution: "רזולוציה נמוכה: הפנים צריכות להיות קרובות. עדיף הזרם הראשי של המצלמה",
  cpu_codec: "הקידוד אינו נתמך בפענוח GPU: הפענוח יתבצע במעבד",
  no_gpu: "לא נמצא GPU: הפענוח יתבצע במעבד",
};
const CAMERA_STATUS = {
  validated: ["מאושרת", "success"], pending: ["ממתינה לבדיקה הראשונה", "info"],
  changed: ["השתנתה: נדרש אישור", "warning"], unvalidated: ["טרם אושרה", "warning"],
};

function Row({ ok, title, text }) {
  return <div className="health-row"><span className={ok ? "good" : "bad"}>{ok ? <CheckCircle2 /> : <CircleAlert />}</span>
    <div><b>{title}</b><small>{text}</small></div></div>;
}

function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = async (work, done) => {
    setBusy(true);
    try { const result = await work(); if (done) toast(done); return result; }
    catch (error) { toast(error.message || "הפעולה נכשלה", "error"); return null; }
    finally { setBusy(false); }
  };
  return [busy, run];
}

function RestartNotice({ show }) {
  const [busy, run] = useAction();
  if (!show) return null;
  return <div className="safety-note"><RotateCcw /><span><b>נדרשת הפעלה מחדש</b><small>השינוי ייכנס לתוקף אחרי הפעלה מחדש של FaceID.</small></span>
    <button className="button primary" disabled={busy} onClick={() => run(() => setupApi("restart", {}), "FaceID מופעל מחדש…")}>הפעל מחדש עכשיו</button></div>;
}

export function ConnectStep({ discovery, reload }) {
  const [form, setForm] = useState({ url: discovery.frigate.url || "", username: "", password: "", verify_tls: discovery.frigate.verify_tls,
    host: discovery.mqtt.host || "", port: discovery.mqtt.port || 1883, user: "", mqtt_password: "" });
  const [restart, setRestart] = useState(false);
  const [busy, run] = useAction();
  const set = key => event => setForm({ ...form, [key]: event.target.type === "checkbox" ? event.target.checked : event.target.value });
  const save = () => run(async () => {
    const frigate = { url: form.url.trim(), verify_tls: form.verify_tls };
    if (form.username) frigate.username = form.username;
    if (form.password) frigate.password = form.password;
    const mqtt = { host: form.host.trim(), port: Number(form.port) };
    if (form.user) mqtt.user = form.user;
    if (form.mqtt_password) mqtt.password = form.mqtt_password;
    const result = await setupApi("apply", { connection: mqtt.host ? { frigate, mqtt } : { frigate } });
    setRestart(Boolean(result?.restart_required)); reload();
  }, "החיבור נשמר");
  const f = discovery.frigate, m = discovery.mqtt, s = discovery.system;
  return <Panel>
    <div className="health-list">
      <Row ok={f.reachable} title="Frigate" text={f.reachable ? `מחובר: ${f.url} · ${f.cameras.length} מצלמות` : f.configured ? `לא ניתן לקרוא את Frigate בכתובת ${f.url}` : "לא הוגדרה כתובת"} />
      <Row ok={m.configured} title="MQTT" text={m.configured ? `ברוקר: ${m.host}:${m.port}` : "לא הוגדר ברוקר. התקינו את Mosquitto או הזינו ברוקר"} />
      <Row ok={s.gpu_device} title="מאיץ גרפי" text={s.gpu_device ? `GPU זמין · מנוע ${s.backend || "—"}` : `אין GPU: זיהוי ופענוח במעבד · מנוע ${s.backend || "—"}`} />
      {f.reachable && !f.topic_prefix_matches && <Row ok={false} title="קידומת MQTT של Frigate" text={`Frigate משתמש ב־${f.topic_prefix}; עדכנו את frigate_topic_prefix בהגדרות התוסף`} />}
    </div>
    <details open={!f.reachable || !m.configured}><summary>שינוי חיבור</summary>
      <div className="settings-columns">
        <label>כתובת Frigate<input value={form.url} onChange={set("url")} placeholder="http://ccab4aaf-frigate:5000" dir="ltr" /></label>
        <label>משתמש Frigate<input value={form.username} onChange={set("username")} placeholder={f.username_set ? "ללא שינוי" : "אופציונלי"} dir="ltr" /></label>
        <label>סיסמת Frigate<input type="password" value={form.password} onChange={set("password")} placeholder={f.password_set ? "ללא שינוי" : "אופציונלי"} dir="ltr" /></label>
        <label className="face-check"><input type="checkbox" checked={form.verify_tls} onChange={set("verify_tls")} /> אימות תעודת TLS</label>
        <label>ברוקר MQTT<input value={form.host} onChange={set("host")} placeholder="core-mosquitto" dir="ltr" /></label>
        <label>פורט<input type="number" value={form.port} onChange={set("port")} dir="ltr" /></label>
        <label>משתמש MQTT<input value={form.user} onChange={set("user")} placeholder={m.user_set ? "ללא שינוי" : "אופציונלי"} dir="ltr" /></label>
        <label>סיסמת MQTT<input type="password" value={form.mqtt_password} onChange={set("mqtt_password")} placeholder={m.password_set ? "ללא שינוי" : "אופציונלי"} dir="ltr" /></label>
      </div>
      <button className="button primary" disabled={busy || !form.url.trim()} onClick={save}>שמירת חיבור</button>
    </details>
    <RestartNotice show={restart} />
  </Panel>;
}

export function CamerasStep({ discovery, reload }) {
  const [busy, run] = useAction();
  const cameras = discovery.frigate.cameras;
  if (!cameras.length) return <Panel><p>לא נמצאו מצלמות ב־Frigate. חזרו לשלב החיבור.</p></Panel>;
  const toggle = camera => run(async () => {
    await api(`cameras/${encodeURIComponent(camera.camera)}/enabled`, { method: "POST", body: JSON.stringify({ enabled: !camera.used_by_faceid }) });
    reload();
  });
  return <Panel><div className="health-list">{cameras.map(camera => <div className="setting-row" key={camera.camera}>
    <div><b>{camera.camera}</b><span>{camera.detect?.width ? `זיהוי Frigate ${camera.detect.width}×${camera.detect.height}` : "גודל זיהוי לא ידוע"}{camera.restream ? ` · Restream ${camera.restream}` : ""}</span>
      {camera.warnings.map(item => <span key={item}>⚠ {CAMERA_WARNINGS[item] || item}</span>)}</div>
    <button className={`switch ${camera.used_by_faceid ? "on" : ""}`} disabled={busy} aria-pressed={camera.used_by_faceid} aria-label={`זיהוי פנים ב־${camera.camera}`} onClick={() => toggle(camera)}><i /></button>
  </div>)}</div><p className="workspace-info-line">מצלמה כבויה לא נבדקת ולא מעבדת אירועים. אפשר להפעיל אותה כאן בכל עת.</p></Panel>;
}

function Recommendation({ camera, facts, onApplied }) {
  const [busy, run] = useAction();
  const rec = facts.recommendation;
  const apply = decode => run(async () => {
    const result = await setupApi("apply", { cameras: { [camera]: decode } }); onApplied(result);
  }, decode ? "הגדרת הפענוח נשמרה" : "המצלמה תשתמש בהגדרות הכלליות");
  return <div className="verify-box">
    <h4>זרם: {facts.codec?.toUpperCase()} · {facts.width}×{facts.height}{facts.fps ? ` · ${facts.fps} fps` : ""}</h4>
    <span>המלצה: פענוח {rec.gpu_decode ? "GPU" : "מעבד"} · {rec.decode_width ? `${rec.decode_width} px` : "רוחב מקורי"} · כל פריים {rec.decode_step}{rec.delivered_fps ? ` (כ־${rec.delivered_fps} פריימים בשנייה)` : ""}</span>
    {rec.notes.map(note => <small key={note}>⚠ {NOTES[note] || note}</small>)}
    <div className="button-row">
      <button className="button primary" disabled={busy} onClick={() => apply({ gpu_decode: rec.gpu_decode, decode_width: rec.decode_width, decode_step: rec.decode_step })}>שמור המלצה</button>
      <button className="button secondary" disabled={busy} onClick={() => apply(null)}>השתמש בהגדרות הכלליות</button>
    </div>
  </div>;
}

export function ViewStep({ discovery, status, reloadStatus }) {
  const used = discovery.frigate.cameras.filter(camera => camera.used_by_faceid).map(camera => camera.camera);
  const [camera, setCamera] = useState(used[0] || "");
  const [facts, setFacts] = useState(null);
  const [manual, setManual] = useState({ width: "", fps: "", codec: "h264" });
  const [applied, setApplied] = useState(null);
  const [busy, run] = useAction();
  useEffect(() => { setFacts(null); setApplied(null); }, [camera]);
  if (!used.length) return <Panel><p>אין מצלמות פעילות. בחרו מצלמה בשלב הקודם.</p></Panel>;
  const current = status.cameras.find(item => item.camera === camera);
  const [label, tone] = CAMERA_STATUS[current?.status] || ["לא נבדקה עדיין", "neutral"];
  const probe = () => run(async () => setFacts(await setupApi(`cameras/${encodeURIComponent(camera)}/probe`, {})));
  const recommendManual = () => run(async () => setFacts({ ...manual, width: Number(manual.width), fps: Number(manual.fps) || null,
    recommendation: await setupApi("recommend", { width: Number(manual.width), fps: Number(manual.fps) || null, codec: manual.codec }) }));
  const validate = () => run(async () => { await setupApi(`cameras/${encodeURIComponent(camera)}/validate`, {}); reloadStatus(); }, "התצוגה אושרה");
  return <Panel>
    <div className="intercom-selector"><label>מצלמה<select value={camera} onChange={event => setCamera(event.target.value)}>{used.map(name => <option key={name}>{name}</option>)}</select></label>
      <Badge tone={tone}>{label}</Badge></div>
    <LiveFrame camera={camera} controls />
    <p className="workspace-info-line"><b>מדידה בלבד: הדלת עדיין נפתחת בזיהוי רגיל.</b> לבדיקה בלי פתיחת דלת השתמשו בבדיקת ההליכה בשלב 6.</p>
    <p className="workspace-info-line">עמדו במקום שבו מזהים אנשים בדרך כלל ולחצו "מדידת פנים". פנים מתחת לסף מסומנות באדום: קרבו את המצלמה או את נקודת העמידה.</p>
    {current?.changes?.length > 0 && <div className="safety-note"><CircleAlert /><span><b>{current.changes.map(item => item.text).join(" · ")}</b><small>{current.message}</small></span></div>}
    {current?.status === "unvalidated" && <p className="workspace-info-line">{current.message}</p>}
    <div className="button-row">
      <button className="button secondary" disabled={busy} onClick={probe}>בדיקת הזרם</button>
      <button className="button primary" disabled={busy} onClick={validate}>התמונה נכונה: אשר מצלמה</button>
    </div>
    {facts?.ok === false && <div className="verify-box"><h4>לא ניתן לבדוק את הזרם ({facts.reason}). הזינו את נתוני המצלמה:</h4>
      <div className="settings-columns">
        <label>רוחב (px)<input type="number" value={manual.width} onChange={event => setManual({ ...manual, width: event.target.value })} dir="ltr" /></label>
        <label>פריימים בשנייה<input type="number" value={manual.fps} onChange={event => setManual({ ...manual, fps: event.target.value })} dir="ltr" /></label>
        <label>קידוד<select value={manual.codec} onChange={event => setManual({ ...manual, codec: event.target.value })}><option value="h264">H.264</option><option value="hevc">H.265</option><option value="mjpeg">MJPEG</option></select></label>
      </div>
      <button className="button secondary" disabled={busy || !Number(manual.width)} onClick={recommendManual}>קבל המלצה</button></div>}
    {facts?.recommendation && <Recommendation camera={camera} facts={facts} onApplied={setApplied} />}
    {applied && <p className="workspace-info-line">נשמר. ההגדרה תיכנס לתוקף כשזרם המצלמה ייפתח מחדש (אחרי החזקה חמה או הפעלה מחדש).</p>}
    <p className="workspace-info-line">אשרו את המצלמה כשהתמונה מראה את אזור הכניסה כרגיל. אישור מפעיל התאמה מהירה לתיבת האדם של Frigate.</p>
  </Panel>;
}

// Enrollment and pairing reuse the existing screens inside the wizard, so the wizard is never left.
export function PeopleStep({ navigate }) {
  return <div className="setup-embedded"><p className="workspace-info-line">רשמו כל אדם עם כמה תמונות ברורות מזוויות שונות.</p>
    <PeoplePage navigate={navigate} /></div>;
}

export function Access1Step() {
  return <div className="setup-embedded"><p className="workspace-info-line">שלב אופציונלי. אם FaceID פותח דלת דרך Access1, חברו אותו כאן בקוד חיבור. בלי Access1, FaceID מזהה ומדווח בלבד.</p>
    <Access1Page /></div>;
}

export function WalkTest({ cameras, test }) {
  const [camera, setCamera] = useState(cameras[0] || "");
  const [busy, run] = useAction();
  const data = test.data || { results: [] };
  const running = data.active && data.camera === camera;
  return <div className="verify-box"><h4>בדיקת הליכה</h4>
    <small>במהלך הבדיקה FaceID מזהה ומציג את התוצאה, ולא שולח בקשה ל־Access1. הדלת לא תיפתח.</small>
    <div className="inline-form"><select value={camera} onChange={event => setCamera(event.target.value)}>{cameras.map(name => <option key={name}>{name}</option>)}</select>
      {running ? <button className="button secondary" disabled={busy} onClick={() => run(async () => { await setupApi("walk-test/stop", {}); test.reload(); })}><Square />עצור ({data.remaining_seconds} ש׳)</button>
        : <button className="button primary" disabled={busy || !camera} onClick={() => run(async () => { await setupApi("walk-test", { camera, seconds: 120 }); test.reload(); })}><Play />התחל בדיקה (2 דקות)</button>}</div>
    {running && !data.results.length && <small>גשו למצלמה מהמקום הרגיל ובקצב רגיל. אם לא מתקבל זיהוי, ייתכן שהפנים קטנות מדי במרחק הזה או מוסתרות.</small>}
    <div className="evidence-list">{data.results.slice().reverse().map(result => <div key={result.event_id}>
      <CheckCircle2 /><span><b>{result.person} · {percent(result.score)}</b>
        <small>{result.recognized_after_seconds != null ? `זוהה ${result.recognized_after_seconds} ש׳ אחרי תחילת האירוע · ` : ""}{result.liveness_confirmed ? "אדם חי אומת" : "אדם חי לא אומת"} · בקשה ל־Access1 לא נשלחה</small></span></div>)}</div>
  </div>;
}

const IMPORT_WARNINGS = { direct_rtsp_address_needed: "מקור RTSP ישיר דורש כתובת באתר הזה: נשאר המקור הנוכחי" };

// 6.9.3: one secret-free file to copy this setup to another installation.
export function SiteTransfer({ reloadStatus }) {
  const [site, setSite] = useState(null);
  const [preview, setPreview] = useState(null);
  const [done, setDone] = useState(null);
  const [busy, run] = useAction();
  const download = () => run(async () => {
    const data = await api("setup/export");
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: "application/json" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: `faceid-site-${new Date().toISOString().slice(0, 10)}.json` });
    link.click(); URL.revokeObjectURL(url);
  }, "קובץ ההגדרות הורד");
  const choose = file => run(async () => {
    setPreview(null); setDone(null);
    const value = JSON.parse(await file.text());
    setSite(value);
    setPreview(await setupApi("import", { site: value, apply: false }));
  });
  const apply = () => run(async () => {
    const result = await setupApi("import", { site, apply: true });
    setDone(result); setPreview(null); reloadStatus();
  }, "ההגדרות יובאו");
  const settings = Object.entries(preview?.settings || {});
  const cameras = Object.entries(preview?.cameras || {});
  return <div className="verify-box"><h4>העברת הגדרות לאתר אחר</h4>
    <small>הקובץ כולל הגדרות ומצלמות בלבד: ללא סיסמאות, ללא חיבור Frigate/MQTT, ללא כתובות RTSP וללא אנשים (לגלריה יש גיבוי נפרד).</small>
    <div className="button-row">
      <button className="button secondary" disabled={busy} onClick={download}>ייצוא קובץ הגדרות</button>
      <label className="button secondary">ייבוא קובץ הגדרות<input hidden type="file" accept=".json,application/json" onChange={event => event.target.files?.[0] && choose(event.target.files[0])} /></label>
    </div>
    {preview && <div className="evidence-list">
      {!preview.changes && <small>אין שינויים: ההגדרות כבר זהות.</small>}
      {settings.map(([key, change]) => <small key={key}>{key}: {String(change.from ?? "—")} ← {String(change.to)}</small>)}
      {cameras.map(([name, keys]) => <small key={name}>{name}: {keys.includes("new") ? "מצלמה חדשה" : keys.join(", ")}</small>)}
      {preview.skipped.length > 0 && <small>⚠ לא נמצאו ב־Frigate ולא יובאו: {preview.skipped.join(", ")}</small>}
      {preview.warnings.map(item => <small key={item.camera}>⚠ {item.camera}: {IMPORT_WARNINGS[item.reason] || item.reason}</small>)}
      {preview.changes && <button className="button primary" disabled={busy} onClick={apply}>החל את השינויים</button>}
    </div>}
    {done && <div className="evidence-list"><small>יובא: {done.applied.join(", ") || "ללא שינוי"}. מצלמה חדשה, או מצלמה שמקור התמונה שלה השתנה, צריכה אישור תמונה בשלב 3.</small>
      {done.restart_required && <button className="button secondary" disabled={busy} onClick={() => run(async () => { await setupApi("restart", {}); }, "FaceID מופעל מחדש")}>הפעל מחדש את FaceID</button>}</div>}
  </div>;
}

export function ReviewStep({ status, reloadStatus, walkTest }) {
  const effective = useResource("setup/effective");
  const [busy, run] = useAction();
  if (effective.loading) return <Loading />;
  if (effective.error) return <ErrorState error={effective.error} retry={effective.reload} />;
  const e = effective.data;
  const cameras = Object.entries(e.cameras).filter(([, camera]) => camera.enabled);
  return <Panel>
    <div className="health-list">
      <Row ok={!e.setup_mode.length} title="חיבורים" text={e.setup_mode.length ? `חסר: ${e.setup_mode.join(", ")}` : `Frigate ${e.connection.frigate.url} · MQTT ${e.connection.mqtt.host}`} />
      <Row ok={Boolean(e.engine.active_backend)} title="מנוע זיהוי" text={`${e.engine.active_backend || "—"}${e.engine.native_device ? ` · ${e.engine.native_device}` : ""}`} />
      <Row ok={e.gallery.persons > 0} title="גלריה" text={`${e.gallery.persons} אנשים · ${e.gallery.reference_photos} תמונות ייחוס`} />
      {cameras.map(([name, camera]) => <Row key={name} ok={camera.box_association} title={name}
        text={`פענוח ${camera.decode.gpu_decode ? "GPU" : "מעבד"} · ${camera.decode.decode_width || "רוחב מקורי"} · כל פריים ${camera.decode.decode_step} (${camera.decode_source === "camera" ? "הגדרת מצלמה" : "הגדרה כללית"}) · ${camera.box_association ? "התאמה לתיבת Frigate פעילה" : "ממתינה לאישור תמונה"}`} />)}
      {e.access1 && <Row ok={e.access1.state === "ready"} title="Access1" text={e.access1.enabled ? `מצב: ${e.access1.state}` : "לא מחובר"} />}
    </div>
    <WalkTest cameras={cameras.map(([name]) => name)} test={walkTest} />
    <SiteTransfer reloadStatus={reloadStatus} />
    <div className="button-row">
      <button className="button primary" disabled={busy || e.setup_mode.length > 0} onClick={() => run(async () => { await setupApi("complete", {}); reloadStatus(); }, "ההגדרה הושלמה")}>סיום הגדרה</button>
      {status.rollback_available && <button className="button danger-ghost" disabled={busy} onClick={() => run(async () => { await setupApi("rollback", {}); reloadStatus(); }, "השינוי האחרון בוטל; הפעילו מחדש את FaceID")}><RotateCcw />ביטול השינוי האחרון</button>}
    </div>
  </Panel>;
}

const STEP_KEY = "faceid-setup-step";
const savedStep = () => {
  try { const value = Number(sessionStorage.getItem(STEP_KEY)); return value >= 0 && value < STEPS.length ? value : 0; }
  catch { return 0; }
};

export function SetupPage({ navigate }) {
  // The current step survives leaving the wizard (e.g. a link inside an embedded screen).
  const [step, setStepState] = useState(savedStep);
  const setStep = value => { setStepState(value); try { sessionStorage.setItem(STEP_KEY, String(value)); } catch { /* storage blocked */ } };
  const status = useResource("setup/status");
  const discovery = useResource("setup/discover");
  // One walk-test poll for the whole wizard: drives the banner on every step and the test panel.
  const walkTest = useResource("setup/walk-test", { poll: 2000 });
  const [stopping, runStop] = useAction();
  if (status.loading || discovery.loading) return <Loading text="בודק חיבורים ומצלמות…" />;
  const error = status.error || discovery.error;
  if (error?.status === 403) return <Panel><Row ok={false} title="נדרש מנהל Home Assistant" text="פתחו את FaceID מסרגל הצד של Home Assistant עם משתמש מנהל." /></Panel>;
  if (error) return <ErrorState error={error} retry={() => { status.reload(); discovery.reload(); }} />;
  const reload = () => { discovery.reload(); status.reload(); };
  const [id] = STEPS[step];
  const props = { discovery: discovery.data, status: status.data, reload, reloadStatus: status.reload, navigate, walkTest };
  const walk = walkTest.data;
  const view = { connect: ConnectStep, cameras: CamerasStep, view: ViewStep, people: PeopleStep, access1: Access1Step, review: ReviewStep }[id];
  return <section>
    <PageHeader eyebrow={status.data.state === "completed" ? "ההגדרה הושלמה" : "הגדרה"} title="אשף הגדרה"
      description="שישה צעדים: חיבור, מצלמות, בדיקת תמונה, אנשים, Access1 ובדיקת הליכה. כל שינוי נשמר רק אחרי אישור." />
    <nav className="segmented" aria-label="שלבי ההגדרה">{STEPS.map(([key, title, StepIcon], index) =>
      <button key={key} className={index === step ? "active" : ""} aria-current={index === step ? "step" : undefined} onClick={() => setStep(index)}><StepIcon />{index + 1}. {title}</button>)}</nav>
    {walk?.active && <div className="safety-note" role="status"><CircleAlert /><span><b>בדיקת הליכה פועלת ב־{walk.camera}: הדלת לא תיפתח</b>
      <small>נותרו {walk.remaining_seconds} ש׳. זיהויים מוצגים בשלב 6 ולא נשלחת בקשה ל־Access1.</small></span>
      <button className="button secondary" disabled={stopping} onClick={() => runStop(async () => { await setupApi("walk-test/stop", {}); walkTest.reload(); })}><Square />עצור בדיקה</button></div>}
    {discovery.data.manual_fallback && id !== "connect" && <div className="safety-note"><CircleAlert /><span><b>אין גישה להגדרות Frigate</b><small>בדקו את החיבור בשלב הראשון.</small></span></div>}
    {React.createElement(view, props)}
    <div className="button-row">
      {step > 0 && <button className="button secondary" onClick={() => setStep(step - 1)}>הקודם</button>}
      {step < STEPS.length - 1 && <button className="button primary" onClick={() => setStep(step + 1)}>הבא: {STEPS[step + 1][1]}</button>}
    </div>
  </section>;
}
