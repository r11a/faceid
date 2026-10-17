import React, { useEffect, useMemo, useRef, useState } from "react";
import { Camera, ChevronLeft, Clock3, ImageOff, Images, Search, Upload, UserPlus, Users } from "lucide-react";
import { api, assetUrl, query } from "../api.js";
import { useResource } from "../hooks.js";
import { Badge, Empty, ErrorState, Loading, Modal, PageHeader, Panel, dateTime, percent, useToast } from "../ui.jsx";

const dayLabel = (value) => new Date(value * 1000).toLocaleDateString("he-IL", { weekday: "long", day: "numeric", month: "long", year: "numeric" });

function FaceImage({ src, alt }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return !src || failed ? <span className="face-placeholder"><ImageOff /></span> : <img src={src.startsWith("data:") ? src : assetUrl(src)} alt={alt || "פנים"} onError={() => setFailed(true)} loading="lazy" />;
}

function Evidence({ event, onClose, onSimilar }) {
  const [clip, setClip] = useState(false);
  const [error, setError] = useState("");
  return <Modal title={`${event.ground_truth || event.person || "פנים לא מזוהות"} · ${event.camera}`} subtitle={dateTime(event.start_ts || event.updated_ts)} onClose={onClose} wide>
    <div className="face-evidence"><FaceImage src={event.image} />{clip ? <video controls autoPlay src={assetUrl(`api/activity/${encodeURIComponent(event.event_id)}/clip`)} onError={() => setError("הקליפ אינו זמין עוד ב־Frigate")} /> : <button className="button primary" onClick={() => setClip(true)}>צפייה בקליפ</button>}{error && <p role="alert">{error}</p>}
    {event.sample_id && <button className="button secondary" onClick={() => { onSimilar({ sample_id: event.sample_id }); onClose(); }}><Search />חפש פנים דומות לאלה</button>}</div>
  </Modal>;
}

export function FaceExplorer({ source: initialSource, title, onEnroll }) {
  const [source, setSource] = useState(initialSource);
  const [filters, setFilters] = useState({ camera: "", date_from: "", date_to: "", order: "asc", threshold: .55, similar: false });
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const [retry, setRetry] = useState(0);
  const [event, setEvent] = useState(null);
  const [samples, setSamples] = useState(null);
  const index = useResource("faces/index", { poll: 4000 });
  const indexWasRunning = useRef(false);
  useEffect(() => {
    if (!index.data) return;
    if (indexWasRunning.current && !index.data.running) setRetry(value => value + 1);
    indexWasRunning.current = Boolean(index.data.running);
  }, [index.data]);
  const toast = useToast();
  const sourceKey = JSON.stringify(initialSource);
  useEffect(() => { setSource(initialSource); setOffset(0); setSamples(null); }, [sourceKey]);
  const requestKey = JSON.stringify({ ...source, ...filters, offset });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, data: null, error: null });
    const load = async () => {
      try {
        if (source.event_id) {
          const result = await api(`faces/event/${encodeURIComponent(source.event_id)}`, { signal: controller.signal });
          if (result.samples.length > 1) { setSamples(result.samples); setState({ loading: false, data: null, error: null }); return; }
        }
        const data = await api(`faces/timeline?${query({ ...source, ...filters, offset, limit: 50,
          date_from: filters.date_from ? new Date(filters.date_from).getTime() / 1000 : "",
          date_to: filters.date_to ? new Date(filters.date_to).getTime() / 1000 : "" })}`, { signal: controller.signal });
        setState({ loading: false, data, error: null });
      } catch (error) { if (error.name !== "AbortError") setState({ loading: false, data: null, error }); }
    };
    load();
    return () => controller.abort();
  }, [requestKey, retry]);
  const change = (key, value) => { setFilters(current => ({ ...current, [key]: value })); setOffset(0); };
  const groups = useMemo(() => {
    const days = new Map();
    for (const row of state.data?.events || []) { const day = dayLabel(row.start_ts ?? row.updated_ts); if (!days.has(day)) days.set(day, []); days.get(day).push(row); }
    return [...days.entries()];
  }, [state.data]);
  const rebuild = async () => {
    try { await api("faces/index", { method: "POST" }); indexWasRunning.current = true; toast("סריקת התמונות שנשמרו החלה ברקע"); index.reload(); }
    catch (error) { toast(error.message, "error"); }
  };
  return <div className="face-explorer">
    <div className="panel-heading"><div><span className="eyebrow">מכל המצלמות</span><h2>{title || "הופעות הפנים שנבחרו"}</h2><p>{state.data?.mode === "identity" ? "אירועים שזוהו או אושרו כאדם זה" : "התאמות לפי דמיון בפנים — יש לבדוק לפני קביעת זהות"}</p></div>{onEnroll && <button className="button secondary" onClick={onEnroll}><UserPlus />הוסף לגלריה</button>}</div>
    <div className="face-filters">
      <label>מצלמה<input value={filters.camera} list="face-camera-options" placeholder="כל המצלמות" onChange={e => change("camera", e.target.value)} /><datalist id="face-camera-options">{(state.data?.cameras || []).map(camera => <option key={camera} value={camera} />)}</datalist></label>
      <label>מתאריך<input type="datetime-local" value={filters.date_from} onChange={e => change("date_from", e.target.value)} /></label>
      <label>עד תאריך<input type="datetime-local" value={filters.date_to} onChange={e => change("date_to", e.target.value)} /></label>
      <label>סדר<select value={filters.order} onChange={e => change("order", e.target.value)}><option value="asc">מהישן לחדש</option><option value="desc">מהחדש לישן</option></select></label>
      {source.person && <label className="face-check"><input type="checkbox" checked={filters.similar} onChange={e => change("similar", e.target.checked)} />כלול חיפוש דומה, גם ללא זיהוי</label>}
      {(!source.person || filters.similar) && <label>סף דמיון {percent(filters.threshold)}<input type="range" min="0.3" max="0.9" step="0.05" value={filters.threshold} onChange={e => change("threshold", Number(e.target.value))} /></label>}
    </div>
    {samples && <div><p>בחר את הפנים לחיפוש מתוך האירוע:</p><div className="face-candidates">{samples.map(sample => <button key={sample.sample_id} onClick={() => { setSource({ sample_id: sample.sample_id }); setSamples(null); setOffset(0); }}><FaceImage src={sample.image} /><span>חפש פנים אלה</span></button>)}</div></div>}
    {state.loading ? <Loading text="מחפש הופעות בכל המצלמות…" /> : state.error ? <ErrorState error={state.error} retry={() => setRetry(v => v + 1)} /> : state.data && <>
      <div className="face-result-count"><b>{state.data.total} אירועים</b><span>{state.data.cameras.length} מצלמות</span></div>
      {groups.map(([day, rows]) => <section className="face-day" key={day}><h3><Clock3 />{day}</h3><div className="face-timeline">{rows.map(row => <button className="face-appearance" key={row.event_id} onClick={() => setEvent(row)}><time>{new Date((row.start_ts ?? row.updated_ts)*1000).toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time><FaceImage src={row.image} /><span><b><Camera />{row.camera}</b><small>{row.ground_truth || row.person || "לא זוהה"}</small></span>{row.similarity != null && <Badge tone="info">{percent(row.similarity)} דמיון</Badge>}<ChevronLeft /></button>)}</div></section>)}
      {!state.data.total && <Empty icon={Search} title="לא נמצאו הופעות בטווח שנבחר" text="אפשר להרחיב את התאריכים, לשנות את סף הדמיון או לסרוק את התמונות שנשמרו בהיסטוריה." />}
      {state.data.total > 50 && <div className="face-pagination"><button className="button secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset-50))}>הקודמים</button><span>{offset+1}–{Math.min(offset+50,state.data.total)} מתוך {state.data.total}</span><button className="button secondary" disabled={offset+50 >= state.data.total} onClick={() => setOffset(offset+50)}>הבאים</button></div>}
    </>}
    <details className="face-history-note"><summary>חסרים אירועים ישנים?</summary><p>אירועים מזוהים נשמרים לפי מדיניות ההיסטוריה. חיפוש דומה זמין לאירועים שנאספו מאז 6.1 ולתמונות היסטוריות שעדיין קיימות. תמונות שכבר נמחקו אינן ניתנות לשחזור.</p>{index.data?.running ? <Loading text={`סורק היסטוריה: ${index.data.processed} מתוך ${index.data.total}`} /> : <button className="button secondary" onClick={rebuild}>סרוק תמונות היסטוריות זמינות</button>}{index.data?.error && <p role="alert">{index.data.error}</p>}<button className="text-button" onClick={() => setRetry(v => v+1)}>רענן תוצאות</button></details>
    {event && <Evidence event={event} onClose={() => setEvent(null)} onSimilar={setSource} />}
  </div>;
}

export function FacesPage({ navigate }) {
  const [file, setFile] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [source, setSource] = useState(() => Object.fromEntries(new URLSearchParams(location.hash.split("?")[1] || "")));
  const [enroll, setEnroll] = useState(false);
  const [enrollPerson, setEnrollPerson] = useState("");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [faceIndex, setFaceIndex] = useState(null);
  const requestId = useRef(0);
  const users = useResource("users");
  const toast = useToast();
  const search = async (chosenFile, selectedIndex = null) => {
    const id = ++requestId.current;
    setFile(chosenFile); setBusy(true); setError(null); setResult(null); setSource({}); setFaceIndex(selectedIndex);
    try {
      const data = new FormData(); data.append("file", chosenFile);
      const found = await api(`faces/search${selectedIndex != null ? `?face_index=${selectedIndex}` : ""}`, { method: "POST", body: data });
      if (id !== requestId.current) return;
      setResult(found); if (found.token) { setSource({ token: found.token }); setFaceIndex(found.face_index); }
    } catch (err) { if (id === requestId.current) setError(err); }
    finally { if (id === requestId.current) setBusy(false); }
  };
  const save = async e => {
    e.preventDefault(); setSaving(true);
    try {
      await api("faces/enroll", { method: "POST", body: JSON.stringify({ token: result.token, ...(enrollPerson ? { person: enrollPerson } : { name }) }) });
      toast("התמונה נוספה לגלריה"); setEnroll(false); setResult(null); setSource({}); users.reload();
    } catch (err) { toast(err.message, "error"); }
    finally { setSaving(false); }
  };
  return <div className="faces-page">
    <PageHeader eyebrow="גלריה וחיפוש" title="בחר פנים. מצא את ההופעות שלהן." description="העלה תמונה לחיפוש מול האנשים והאירועים שנשמרו, או בחר אדם מהגלריה." actions={<button className="button secondary" onClick={() => navigate("people")}><Users />ניהול אנשים</button>} />
    <Panel><div className="face-search-start"><label className={`upload-zone ${busy ? "busy" : ""}`}><Upload /><b>{busy ? "מחפש פנים…" : "העלה תמונה לחיפוש"}</b><span>JPG, PNG או WebP · עד 8MB · חיפוש אינו מוסיף אדם לגלריה</span><input type="file" accept="image/jpeg,image/png,image/webp" disabled={busy} onChange={e => { const selected = e.target.files[0]; if (selected) search(selected); e.target.value = ""; }} /></label><div className="face-person-picker"><label>או בחר אדם קיים<select value={source.person || ""} onChange={e => { setSource(e.target.value ? { person: e.target.value } : {}); setResult(null); }}><option value="">בחר אדם…</option>{(users.data?.users || []).map(person => <option value={person.slug} key={person.slug}>{person.name}</option>)}</select></label><p>בחירת אדם מציגה את כל האירועים שזוהו או אושרו כשייכים לו, מסודרים לפי ימים ושעות.</p></div></div></Panel>
    {error && <ErrorState error={error} retry={() => search(file, faceIndex)} />}
    {result?.status === "needs_selection" && <Panel><h2>בחר את הפנים לחיפוש</h2><div className="face-choice-image"><img src={result.preview} alt="בחירת פנים מהתמונה" />{result.candidates.map(candidate => { const [l,t,r,b] = candidate.bbox; return <button key={candidate.index} aria-label={`בחר פנים ${candidate.index+1}`} style={{ left:`${l*100}%`, top:`${t*100}%`, width:`${(r-l)*100}%`, height:`${(b-t)*100}%` }} onClick={() => search(file,candidate.index)}><span>בחר</span></button>; })}</div></Panel>}
    {result?.token && <Panel><div className="face-query"><FaceImage src={result.preview} /><div><h2>הפנים לחיפוש</h2><p>החיפוש זמני לעשר דקות. הוספה לגלריה נעשית רק בבחירה מפורשת.</p></div></div>{result.matches?.length > 0 && <><h3>פנים דומות בגלריה</h3><div className="face-candidates">{result.matches.map(match => <button key={match.slug} onClick={() => setSource({ person: match.slug })}><FaceImage src={match.photo} /><b>{match.name}</b><small>{percent(match.similarity)} דמיון</small></button>)}</div><button className="text-button" onClick={() => setSource({ token: result.token })}>חזרה לחיפוש לפי התמונה</button></>}</Panel>}
    {Object.keys(source).length > 0 ? <Panel><FaceExplorer key={JSON.stringify(source)} source={source} onEnroll={result?.token && file ? () => setEnroll(true) : null} /></Panel> : !busy && !result && <Empty icon={Images} title="כל ההופעות, במקום אחד" text="אפשר להתחיל מתמונה, מאדם בגלריה או מפנים לא מזוהות בתור הבדיקה." />}
    {enroll && <Modal title="הוספת התמונה לגלריה" subtitle="בחר אדם קיים או צור אדם חדש" onClose={() => setEnroll(false)}><form className="editor-form" onSubmit={save}><label>אדם קיים<select value={enrollPerson} onChange={e => setEnrollPerson(e.target.value)}><option value="">אדם חדש</option>{(users.data?.users || []).map(person => <option key={person.slug} value={person.slug}>{person.name}</option>)}</select></label>{!enrollPerson && <label>שם האדם<input required value={name} onChange={e => setName(e.target.value)} /></label>}<button className="button primary" disabled={saving}>{saving ? "שומר…" : "הוסף תמונה"}</button></form></Modal>}
  </div>;
}
