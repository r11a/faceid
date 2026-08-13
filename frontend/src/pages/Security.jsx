import React, { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, Camera, Check, CircleDot, DoorOpen, Eye, EyeOff, ImageOff,
  KeyRound, LockKeyhole, Play, RefreshCw, Save, ScanFace, ShieldCheck, SlidersHorizontal,
  UserPlus, Users, X,
} from "lucide-react";
import { api, assetUrl } from "../api.js";
import { useResource } from "../hooks.js";
import { Badge, Empty, ErrorState, Loading, Metric, Modal, PageHeader, Panel, dateTime, percent, useToast } from "../ui.jsx";

const roleLabels = {
  observation: "צפייה בלבד",
  entry: "כניסה",
  exit: "יציאה",
  entry_exit: "כניסה ויציאה",
  restricted: "אזור מוגבל",
  intercom: "אינטרקום",
};
const modeLabels = { standard: "רגילה", intercom: "אינטרקום מאובטח" };

function LiveFrame({ camera, refreshKey = 0, analyze }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [camera, refreshKey]);
  return <div className="live-frame">{failed ? <div className="frame-error"><ImageOff /><b>אין תמונה זמינה</b><span>בדוק את חיבור Frigate ואת שם המצלמה</span></div> : <img src={assetUrl(`api/cameras/${encodeURIComponent(camera)}/frame?_=${Date.now()}-${refreshKey}`)} onError={() => setFailed(true)} alt={`תמונה ממצלמת ${camera}`} />}{analyze?.faces?.map((face, index) => { const [x1, y1, x2, y2] = face.box; return <span className={`face-box ${face.usable ? "good" : "bad"}`} key={index} style={{ left: `${x1 / analyze.width * 100}%`, top: `${y1 / analyze.height * 100}%`, width: `${(x2 - x1) / analyze.width * 100}%`, height: `${(y2 - y1) / analyze.height * 100}%` }}><b>{Math.round(face.face_px)}px</b></span>; })}</div>;
}

export function CamerasPage() {
  const studio = useResource("cameras/studio?days=7");
  const [selected, setSelected] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysis, setAnalysis] = useState(null);
  const toast = useToast();
  useEffect(() => { if (!selected && studio.data?.cameras?.length) setSelected(studio.data.cameras[0].camera); }, [studio.data, selected]);
  const current = studio.data?.cameras?.find((item) => item.camera === selected);
  const toggle = async (camera, enabled) => {
    try { await api(`cameras/${encodeURIComponent(camera)}/enabled`, { method: "POST", body: JSON.stringify({ enabled }) }); toast(enabled ? "המצלמה חוברה לזיהוי" : "המצלמה הוצאה מעיבוד FaceID"); studio.reload(); }
    catch (error) { toast(error.message, "error"); }
  };
  const analyze = async () => {
    setAnalyzing(true); setAnalysis(null);
    try { setAnalysis(await api(`cameras/${encodeURIComponent(selected)}/analyze`)); setRefresh((value) => value + 1); }
    catch (error) { toast(error.message, "error"); }
    finally { setAnalyzing(false); }
  };
  if (studio.loading) return <Loading text="טוען מצלמות מ־Frigate…" />;
  if (studio.error) return <ErrorState error={studio.error} retry={studio.reload} />;
  return <>
    <PageHeader eyebrow="מצלמות" title="שליטה חזותית בכל מקור וידאו" description="כל המצלמות מחוברות כברירת מחדל. כבה רק מצלמות שאינך רוצה שיעברו בדיקות זיהוי." />
    <div className="camera-workspace">
      <Panel className="camera-list"><div className="panel-heading"><div><span className="eyebrow">מקורות</span><h2>{studio.data?.cameras?.length || 0} מצלמות</h2></div></div>{(studio.data?.cameras || []).map((camera) => <button className={selected === camera.camera ? "active" : ""} onClick={() => { setSelected(camera.camera); setAnalysis(null); }} key={camera.camera}><span className={`camera-state ${camera.enabled ? "on" : "off"}`}><Camera /></span><span><b>{camera.camera}</b><small>{modeLabels[camera.mode] || camera.mode} · מינימום {camera.min_face_px}px</small></span><Badge tone={camera.enabled ? "success" : "neutral"}>{camera.enabled ? "פעילה" : "מושבתת"}</Badge></button>)}</Panel>
      {current ? <Panel className="camera-stage"><div className="panel-heading"><div><span className="eyebrow">תצוגה חיה</span><h2>{current.camera}</h2></div><div><button className="button secondary" onClick={() => setRefresh((value) => value + 1)}><RefreshCw />רענון</button><button className="button primary" disabled={analyzing} onClick={analyze}><ScanFace />{analyzing ? "בודק…" : "בדיקת פנים בתמונה"}</button></div></div><LiveFrame camera={current.camera} refreshKey={refresh} analyze={analysis} />{analysis && <div className="analysis-strip"><strong>{analysis.faces?.length || 0} פנים נמצאו</strong>{analysis.faces?.map((face, index) => <Badge key={index} tone={face.usable ? "success" : "warning"}>פנים {index + 1}: {Math.round(face.face_px)}px · איכות {percent(face.score)}</Badge>)}</div>}<CameraSettings camera={current} onSaved={studio.reload} onToggle={toggle} /></Panel> : <Empty icon={Camera} title="אין מצלמות" text="FaceID לא קיבל כרגע רשימת מצלמות מ־Frigate." />}
    </div>
  </>;
}

function CameraSettings({ camera, onSaved, onToggle }) {
  const [form, setForm] = useState({ ...camera });
  const toast = useToast();
  useEffect(() => setForm({ ...camera }), [camera]);
  const save = async () => {
    try { await api(`cameras/${encodeURIComponent(camera.camera)}/profile`, { method: "POST", body: JSON.stringify({ ...form, min_face_px: Number(form.min_face_px), night_min_face_px: Number(form.night_min_face_px || form.min_face_px), burst_frames: Number(form.burst_frames || 3) }) }); toast("הגדרות המצלמה נשמרו"); onSaved(); }
    catch (error) { toast(error.message, "error"); }
  };
  return <div className="camera-settings"><div className="setting-row important"><div><b>המצלמה משתתפת בזיהוי</b><span>כאשר כבוי, אירועים מהמצלמה לא יעובדו ב־FaceID</span></div><button className={`switch ${camera.enabled ? "on" : ""}`} onClick={() => onToggle(camera.camera, !camera.enabled)}><i /></button></div><div className="settings-columns"><label>תפקיד המצלמה<select value={form.role || "observation"} onChange={(e) => setForm({ ...form, role: e.target.value })}>{Object.entries(roleLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label>מצב עבודה<select value={form.mode || "standard"} onChange={(e) => setForm({ ...form, mode: e.target.value })}>{Object.entries(modeLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label className="range-label"><span>גודל פנים מינימלי <b>{form.min_face_px}px</b></span><input type="range" min="24" max="240" step="4" value={form.min_face_px} onChange={(e) => setForm({ ...form, min_face_px: e.target.value })} /></label><label>הגנת חיוּת<select value={form.liveness_mode || "off"} onChange={(e) => setForm({ ...form, liveness_mode: e.target.value })}><option value="off">כבויה</option><option value="advisory">מסייעת בלבד</option><option value="required">חובה — חסימה ללא אישור</option></select></label></div><button className="button primary" onClick={save}><Save />שמירת הגדרות מצלמה</button></div>;
}

export function IntercomPage() {
  const intercom = useResource("intercom");
  const studio = useResource("cameras/studio");
  const [camera, setCamera] = useState("");
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState(null);
  const toast = useToast();
  const candidates = studio.data?.cameras || [];
  useEffect(() => { if (!camera) setCamera(intercom.data?.cameras?.[0]?.camera || candidates[0]?.camera || ""); }, [intercom.data, candidates, camera]);
  const profile = candidates.find((item) => item.camera === camera) || intercom.data?.cameras?.find((item) => item.camera === camera);
  const markIntercom = async () => {
    if (!profile) return;
    try { await api(`cameras/${encodeURIComponent(camera)}/profile`, { method: "POST", body: JSON.stringify({ ...profile, min_face_px: Number(profile.min_face_px || 120), night_min_face_px: Number(profile.night_min_face_px || profile.min_face_px || 120), burst_frames: Number(profile.burst_frames || 5), mode: "intercom", role: "intercom", high_resolution: true, require_second_factor: true, liveness_mode: profile.liveness_mode || "required" }) }); toast(`${camera} הוגדרה כמצלמת אינטרקום`); intercom.reload(); studio.reload(); }
    catch (error) { toast(error.message, "error"); }
  };
  const test = async () => {
    setTesting(true); setResult(null);
    try { setResult(await api(`intercom/${encodeURIComponent(camera)}/capture`, { method: "POST" })); }
    catch (error) { toast(error.message, "error"); }
    finally { setTesting(false); }
  };
  if (intercom.loading || studio.loading) return <Loading text="טוען מצלמות כניסה…" />;
  if (intercom.error || studio.error) return <ErrorState error={intercom.error || studio.error} retry={() => { intercom.reload(); studio.reload(); }} />;
  return <>
    <PageHeader eyebrow="כניסה" title="מצב אינטרקום ממוקד ואמין" description="בחר מצלמה, הגדר אותה כאינטרקום ובדוק תמונה ברזולוציה גבוהה. זיהוי פנים לבדו לעולם אינו פותח דלת." />
    <Panel className="intercom-selector"><label>מצלמת אינטרקום<select value={camera} onChange={(e) => { setCamera(e.target.value); setResult(null); }}>{candidates.map((item) => <option value={item.camera} key={item.camera}>{item.camera}{item.mode === "intercom" ? " · מוגדרת כאינטרקום" : ""}</option>)}</select></label>{profile?.mode !== "intercom" && <button className="button secondary" onClick={markIntercom}><DoorOpen />הגדר מצלמה זו כאינטרקום</button>}<button className="button primary" disabled={!camera || testing} onClick={test}><ScanFace />{testing ? "מצלם ובודק מספר תמונות…" : "בדיקה חיה עכשיו"}</button></Panel>
    {camera && <div className="intercom-grid"><Panel className="intercom-preview"><div className="panel-heading"><div><span className="eyebrow">צילום עדכני</span><h2>{camera}</h2></div></div>{result?.preview_url ? <div className="capture-preview"><img src={assetUrl(`${result.preview_url}&v=${Date.now()}`)} alt="צילום בדיקת אינטרקום" />{result.best && <span className={`face-box ${result.best.usable ? "good" : "bad"}`} style={{ left: `${result.best.box[0] / result.width * 100}%`, top: `${result.best.box[1] / result.height * 100}%`, width: `${(result.best.box[2] - result.best.box[0]) / result.width * 100}%`, height: `${(result.best.box[3] - result.best.box[1]) / result.height * 100}%` }}><b>{Math.round(result.best.face_px)}px</b></span>}</div> : <LiveFrame camera={camera} />}</Panel><Panel className="intercom-result"><div className="panel-heading"><div><span className="eyebrow">תוצאת בדיקה</span><h2>{result ? result.message : "מוכן לבדיקה"}</h2></div>{result && <Badge tone={result.state === "excellent" ? "success" : ["spoof", "no_face"].includes(result.state) ? "danger" : "warning"}>{result.state === "excellent" ? "מצוין" : result.state === "acceptable" ? "מתאים" : result.state === "spoof" ? "נחסם" : "אפשר לשפר"}</Badge>}</div>{result ? <><div className="metrics-grid compact"><Metric label="גודל פנים" value={result.best ? `${Math.round(result.best.face_px)}px` : "—"} note={`הסף: ${result.profile?.min_face_px || 0}px`} /><Metric label="התאמה" value={percent(result.best?.match_score)} tone="turquoise" /><Metric label="חיוּת" value={result.liveness?.confirmed ? "עברה" : result.liveness?.state === "spoof" ? "חשד לזיוף" : "לא אומתה"} tone={result.liveness?.confirmed ? "green" : "red"} /></div><div className="guidance"><h3>מה אפשר לשפר?</h3>{(result.guidance || []).map((item) => <div key={item}><Check />{item}</div>)}{!result.guidance?.length && <div><Check />התמונה ברורה ומתאימה לזיהוי</div>}</div><div className="safety-note"><LockKeyhole /><span><b>מדיניות פתיחה בטוחה</b>נדרש גורם שני בנוסף לפנים. חיוּת RGB מצמצמת זיופים אך אינה תחליף לחיישן עומק או IR.</span></div></> : <Empty icon={ScanFace} title="עמוד מול המצלמה" text="לחץ על “בדיקה חיה עכשיו”. המערכת תציג את התמונה, מסגרת הפנים, מספר הפיקסלים וציון ברור." />}</Panel></div>}
  </>;
}

export function LivenessPage() {
  const data = useResource("liveness");
  const studio = useResource("cameras/studio");
  const toast = useToast();
  const setMode = async (camera, mode) => {
    const profile = studio.data?.cameras?.find((item) => item.camera === camera);
    if (!profile) return;
    try { await api(`cameras/${encodeURIComponent(camera)}/profile`, { method: "POST", body: JSON.stringify({ ...profile, min_face_px: Number(profile.min_face_px), night_min_face_px: Number(profile.night_min_face_px || profile.min_face_px), burst_frames: Number(profile.burst_frames || 3), liveness_mode: mode }) }); toast("רמת הגנת החיוּת נשמרה"); data.reload(); studio.reload(); }
    catch (error) { toast(error.message, "error"); }
  };
  if (data.loading || studio.loading) return <Loading />;
  if (data.error || studio.error) return <ErrorState error={data.error || studio.error} retry={() => { data.reload(); studio.reload(); }} />;
  const status = data.data?.status || {};
  return <>
    <PageHeader eyebrow="Anti-Spoofing" title="הגנה מתמונה ומסך — בשפה פשוטה" description="במצלמות כניסה קרובות מומלץ מצב חובה. במצלמות רחוקות התחל במצב מסייע כדי לא לחסום אנשים אמיתיים." />
    <div className="metrics-grid"><Metric icon={ShieldCheck} label="מודל חיוּת" value={status.model_available ? "מוכן" : "לא זמין"} note={status.enabled ? "הבדיקה פעילה" : "כבויה בהגדרות התוסף"} tone={status.model_available ? "green" : "red"} /><Metric icon={CircleDot} label="פריימים נדרשים" value={status.required_frames || 3} note="החלטה מתקבלת מרצף תמונות" tone="turquoise" /><Metric icon={AlertTriangle} label="נחסמו לאחרונה" value={data.data?.blocked?.length || 0} note="חשד לזיוף או חיוּת שלא אומתה" tone="amber" /></div>
    <div className="safety-banner"><AlertTriangle /><div><b>מגבלת אבטחה חשובה</b><p>מצלמת RGB רגילה יכולה לצמצם תמונות מודפסות ומסכים, אך אינה מבטיחה הגנה מפני מסכה תלת־ממדית או וידאו מתקדם. לפתיחת דלת השאר תמיד אימות נוסף.</p></div></div>
    <div className="protection-grid">{(data.data?.cameras || []).map((camera) => <Panel key={camera.camera}><div className="panel-heading"><div><span className="eyebrow">{camera.mode === "intercom" ? "אינטרקום" : "מצלמה"}</span><h2>{camera.camera}</h2></div><Badge tone={camera.liveness_mode === "required" ? "success" : camera.liveness_mode === "advisory" ? "warning" : "neutral"}>{camera.liveness_mode === "required" ? "חיוּת חובה" : camera.liveness_mode === "advisory" ? "בדיקה מסייעת" : "כבוי"}</Badge></div><LiveFrame camera={camera.camera} /><label>רמת הגנה<select value={camera.liveness_mode || "off"} onChange={(e) => setMode(camera.camera, e.target.value)}><option value="off">כבוי</option><option value="advisory">מסייעת — מציגה אזהרה בלבד</option><option value="required">חובה — חוסמת ללא הוכחת חיוּת</option></select></label></Panel>)}</div>
    <Panel><div className="panel-heading"><div><span className="eyebrow">היסטוריה</span><h2>ניסיונות שנחסמו</h2></div></div><div className="blocked-list">{(data.data?.blocked || []).map((event) => <div key={event.event_id}><Badge tone="danger">{event.status === "spoof_suspected" ? "חשד לתמונה" : "לא אומת"}</Badge><span><b>{event.camera}</b><small>{dateTime(event.start_ts || event.updated_ts)}</small></span><strong>{percent(event.liveness_score)}</strong></div>)}</div>{!data.data?.blocked?.length && <Empty icon={ShieldCheck} title="אין ניסיונות חשודים" text="לא נרשמו לאחרונה אירועים שנחסמו בגלל בדיקת חיוּת." />}</Panel>
  </>;
}

export function GuestsPage() {
  const guests = useResource("guests");
  const cameras = useResource("cameras/studio");
  const [open, setOpen] = useState(false);
  const toast = useToast();
  const revoke = async (id) => { try { await api(`guests/${id}/revoke`, { method: "POST" }); toast("גישת האורח בוטלה"); guests.reload(); } catch (error) { toast(error.message, "error"); } };
  if (guests.loading || cameras.loading) return <Loading />;
  if (guests.error || cameras.error) return <ErrorState error={guests.error || cameras.error} retry={() => { guests.reload(); cameras.reload(); }} />;
  return <>
    <PageHeader eyebrow="גישה זמנית" title="אורחים ללא הרשאה קבועה" description="פנים יוצרות זכאות בלבד. חיוּת וגורם שני נדרשים לפני אישור כניסה." actions={<button className="button primary" onClick={() => setOpen(true)}><UserPlus />אורח חדש</button>} />
    <div className="guest-grid">{(guests.data?.guests || []).map((guest) => <Panel key={guest.id}><div className="panel-heading"><div><span className="eyebrow">אורח זמני</span><h2>{guest.name}</h2></div><Badge tone={guest.status === "active" ? "success" : "neutral"}>{guest.status === "active" ? "פעיל" : guest.status === "used" ? "נוצל" : "בוטל"}</Badge></div><div className="guest-details"><span><b>בתוקף עד</b>{dateTime(guest.valid_until)}</span><span><b>כניסות נותרו</b>{guest.entries_left ?? guest.max_entries}</span><span><b>מצלמות</b>{guest.allowed_cameras?.join(", ") || "כל מצלמות הכניסה"}</span></div>{guest.status === "active" && <button className="button danger-ghost" onClick={() => revoke(guest.id)}>ביטול גישה</button>}</Panel>)}</div>
    {!guests.data?.guests?.length && <Empty icon={Users} title="אין אורחים פעילים" text="צור הרשאה זמנית עם תמונה, תוקף ומספר כניסות מוגבל." />}
    {open && <GuestEditor cameras={cameras.data?.cameras || []} onClose={() => setOpen(false)} onSaved={() => { setOpen(false); guests.reload(); }} />}
  </>;
}

function GuestEditor({ cameras, onClose, onSaved }) {
  const [form, setForm] = useState({ name: "", valid_from: new Date().toISOString().slice(0, 16), valid_until: new Date(Date.now() + 86400000).toISOString().slice(0, 16), max_entries: 1, cameras: [], file: null });
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  const save = async (event) => {
    event.preventDefault(); setSaving(true);
    try { const body = new FormData(); body.append("file", form.file); const params = new URLSearchParams({ name: form.name, valid_from: String(new Date(form.valid_from).getTime() / 1000), valid_until: String(new Date(form.valid_until).getTime() / 1000), max_entries: String(form.max_entries), cameras: form.cameras.join(",") }); await api(`guests?${params}`, { method: "POST", body }); toast("האורח נוסף בהצלחה"); onSaved(); }
    catch (error) { toast(error.message, "error"); }
    finally { setSaving(false); }
  };
  return <Modal title="אורח חדש" subtitle="הרשאה מוגבלת בזמן ובמספר כניסות" onClose={onClose}><form className="editor-form" onSubmit={save}><label>שם האורח<input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label><div className="settings-columns"><label>מתאריך<input required type="datetime-local" value={form.valid_from} onChange={(e) => setForm({ ...form, valid_from: e.target.value })} /></label><label>עד תאריך<input required type="datetime-local" value={form.valid_until} onChange={(e) => setForm({ ...form, valid_until: e.target.value })} /></label><label>מספר כניסות<input type="number" min="1" max="100" value={form.max_entries} onChange={(e) => setForm({ ...form, max_entries: e.target.value })} /></label></div><label>מצלמות מורשות<select multiple value={form.cameras} onChange={(e) => setForm({ ...form, cameras: [...e.target.selectedOptions].map((item) => item.value) })}>{cameras.map((camera) => <option value={camera.camera} key={camera.camera}>{camera.camera}</option>)}</select></label><label className="upload-zone"><UserPlus /><b>תמונה ברורה של האורח</b><span>פנים בודדות, חזיתיות ומוארות</span><input required type="file" accept="image/*" onChange={(e) => setForm({ ...form, file: e.target.files[0] })} /></label><div className="form-actions"><span /><button type="button" className="button secondary" onClick={onClose}>ביטול</button><button className="button primary" disabled={saving}>{saving ? "בודק ושומר…" : "יצירת הרשאה"}</button></div></form></Modal>;
}
