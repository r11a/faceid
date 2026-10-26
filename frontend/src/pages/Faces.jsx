import React, { useEffect, useMemo, useRef, useState } from "react";
import { Camera, ChevronLeft, Clock3, Grid2X2, ImageOff, Images, List, Search, SlidersHorizontal, Upload, UserPlus, Users } from "lucide-react";
import { api, assetUrl, query } from "../api.js";
import { useImageRetry, useResource } from "../hooks.js";
import { ActionsMenu, FaceScore, Badge, Empty, ErrorState, Loading, Modal, PageHeader, Panel, dateTime, percent, useToast } from "../ui.jsx";
import { EventVideo } from "../EventVideo.jsx";
import { isBlacklistedEvent } from "../alerts.js";

const dayLabel = (value) => new Date(value * 1000).toLocaleDateString("he-IL", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const eventImage = event => event.image || (event.event_id ? `api/activity/${encodeURIComponent(event.event_id)}/image` : "");

function FaceImage({ src, alt }) {
  const image = useImageRetry(src);
  return !src || image.failed ? <span className="face-placeholder"><ImageOff /></span> : <img src={image.url.startsWith("data:") ? image.url : assetUrl(image.url)} alt={alt || "פנים"} onError={image.onError} decoding="async" loading="lazy" />;
}

function Evidence({ event, people, onClose, onSimilar }) {
  const name = event.ground_truth || event.person || "פנים לא מזוהות";
  return <Modal title="פרטי אירוע" onClose={onClose} wide>
    <div className={`face-evidence face-evidence-split ${isBlacklistedEvent(event, people) ? "blacklist-outline" : ""}`}>
      <aside className="face-evidence-summary">
        <span className="face-evidence-photo"><FaceImage src={eventImage(event)} alt="תמונת האירוע" /><FaceScore event={event} /></span>
        <h3>{name}</h3>
        <p><Camera />{event.camera || "מצלמה לא ידועה"}</p>
        <p><Clock3 />{dateTime(event.start_ts || event.updated_ts)}</p>
        {event.similarity != null && <Badge tone="info">{percent(event.similarity)} דמיון</Badge>}
        {(event.sample_id || event.event_id) && <button className="button secondary" aria-label="מצא דומה בציר זמן" onClick={() => { onSimilar(event.sample_id ? { sample_id: event.sample_id } : { event_id: event.event_id }); onClose(); }}><Search />מצא דומה בציר זמן</button>}
      </aside>
      <EventVideo eventId={event.event_id} />
    </div>
  </Modal>;
}

export function HistoryReferenceChooser({ event, person, onClose, onAdded }) {
  const choices = useResource(`faces/event/${encodeURIComponent(event.event_id)}/references?${query({ person })}`);
  const [saving, setSaving] = useState(null);
  const [saved, setSaved] = useState({});
  const [summary, setSummary] = useState(null);
  const toast = useToast();
  const reasons = { too_small: "הפנים קטנות מדי", blurred: "התמונה מטושטשת", poor_illumination: "תאורה לא מתאימה", invalid_embedding: "נתוני פנים לא תקינים", low_quality: "איכות הפנים אינה מספיקה" };
  const add = async candidate => {
    setSaving(candidate.token);
    try {
      const result = await api("faces/enroll", { method: "POST", body: JSON.stringify({ token: candidate.token, person }) });
      setSaved(current => ({ ...current, [candidate.token]: result.added ? "נוספה לגלריה" : "כבר קיימת בגלריה" }));
      setSummary(result);
      toast(result.added ? "תמונת הייחוס נוספה והאדם מוכן לזיהוי ראשוני" : "התמונה כבר מיוצגת בגלריה; לא נוספה כפילות", result.added ? "success" : "info");
      onAdded?.(result);
    } catch (error) { toast(error.message, "error"); }
    finally { setSaving(null); }
  };
  return <Modal title="בחר תמונות ייחוס מהאירוע" subtitle={`${event.camera} · ${dateTime(event.start_ts || event.updated_ts)}`} onClose={onClose} wide>
    <p>בחר במפורש רק פנים ששייכות לאדם זה. אפשר להוסיף כמה תמונות טובות; צפייה באירוע אינה מלמדת את המערכת.</p>
    {choices.loading ? <Loading text="טוען פנים שנשמרו באירוע…" /> : choices.error ? <><ErrorState error={choices.error} retry={choices.reload} /><a className="button secondary" href="#faces"><Upload />העלה תמונת ייחוס חדשה</a></> : <div className="face-candidates history-reference-choices">{(choices.data?.candidates || []).map((candidate, index) => <div key={candidate.token}><FaceImage src={candidate.preview} alt={`פנים ${index + 1} מהאירוע`} /><p>פנים {index + 1}</p><button className="button primary" disabled={Boolean(saving) || !candidate.usable || Boolean(saved[candidate.token])} onClick={() => add(candidate)}>{saving === candidate.token ? "מוסיף…" : saved[candidate.token] || "הוסף לגלריה"}</button>{!candidate.usable && <p>{reasons[candidate.reason] || "התמונה אינה מתאימה ללימוד; בחר צילום ברור יותר"}</p>}</div>)}</div>}
    {summary && <p role="status">{summary.reference_count} תמונות ייחוס · {summary.ready ? "מוכן לזיהוי ראשוני" : "נדרשת תמונת ייחוס איכותית"}</p>}
  </Modal>;
}

export function FaceExplorer({ source: initialSource, title, onEnroll, onReferencesChanged, compact = false, initialView = "grid" }) {
  const people = useResource("users");
  const [source, setSource] = useState(initialSource);
  const [filters, setFilters] = useState({ camera: "", date_from: "", date_to: "", order: "desc", threshold: .55, similar: false });
  const [view, setView] = useState(initialView);
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const [retry, setRetry] = useState(0);
  const [event, setEvent] = useState(null);
  const [samples, setSamples] = useState(null);
  const [referenceMode, setReferenceMode] = useState(false);
  const [referenceEvent, setReferenceEvent] = useState(null);
  const index = useResource("faces/index", { poll: 4000 });
  const indexWasRunning = useRef(false);
  useEffect(() => {
    if (!index.data) return;
    if (indexWasRunning.current && !index.data.running) { setOffset(0); setRetry(value => value + 1); }
    indexWasRunning.current = Boolean(index.data.running);
  }, [index.data]);
  const toast = useToast();
  const sourceKey = JSON.stringify(initialSource);
  useEffect(() => { setSource(initialSource); setOffset(0); setSamples(null); setReferenceEvent(null); setReferenceMode(false); }, [sourceKey]);
  const requestKey = JSON.stringify({ ...source, ...filters, offset });
  const append = view === "timeline" && offset > 0;
  useEffect(() => {
    const controller = new AbortController();
    setState(current => ({ loading: true, data: append ? current.data : null, error: null }));
    const load = async () => {
      try {
        if (source.event_id) {
          const result = await api(`faces/event/${encodeURIComponent(source.event_id)}`, { signal: controller.signal });
          if (result.samples.length > 1) { setSamples(result.samples); setState({ loading: false, data: null, error: null }); return; }
        }
        const data = await api(`faces/timeline?${query({ ...source, ...filters, offset, limit: 50,
          date_from: filters.date_from ? new Date(filters.date_from).getTime() / 1000 : "",
          date_to: filters.date_to ? new Date(filters.date_to).getTime() / 1000 : "" })}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setState(current => ({ loading: false, data: append && current.data ? { ...data, events: [...new Map([...current.data.events, ...data.events].map(row => [row.event_id, row])).values()] } : data, error: null }));
      } catch (error) { if (error.name !== "AbortError") setState(current => ({ loading: false, data: append ? current.data : null, error })); }
    };
    load();
    return () => controller.abort();
  }, [requestKey, retry]);
  const changeView = value => { setView(value); setOffset(0); };
  const change = (key, value) => { setFilters(current => ({ ...current, [key]: value })); setOffset(0); };
  const groups = useMemo(() => {
    const days = new Map();
    for (const row of state.data?.events || []) { const day = dayLabel(row.start_ts ?? row.updated_ts); if (!days.has(day)) days.set(day, []); days.get(day).push(row); }
    return [...days.entries()];
  }, [state.data]);
  const coverage = index.data?.events_total != null ? index.data : state.data?.index;
  const similarityMode = !source.person || filters.similar;
  const rebuild = async () => {
    try { await api(`faces/index${coverage?.remote_available ? "?remote=true&limit=500" : ""}`, { method: "POST" }); indexWasRunning.current = true; toast("השלמת חיפוש בהיסטוריה החלה"); index.reload(); }
    catch (error) { toast(error.message, "error"); }
  };
  return <div className="face-explorer">
    {!compact && <div className="panel-heading"><div><span className="eyebrow">מכל המצלמות</span><h2>{title || "הופעות הפנים שנבחרו"}</h2>{source.person && !filters.similar && <p>אירועים שזוהו או אושרו כאדם זה</p>}</div></div>}
    {similarityMode && <div className="face-search-context"><span>פנים דומות · זהות לא מאומתת</span>{initialSource.person && <button className="text-button" onClick={() => { setSource(initialSource); setFilters(current => ({ ...current, similar: false })); setOffset(0); }}>כל אירועי האדם</button>}</div>}
    {similarityMode && coverage && (coverage.events_missing > 0 || coverage.running) && <div className="face-search-coverage" role="status"><div><b>{coverage.running ? "משלים חיפוש בהיסטוריה…" : "החיפוש בהיסטוריה חלקי"}</b><span>{coverage.running ? `${coverage.processed || 0} / ${coverage.total || 0} נבדקו` : `${coverage.events_indexed || 0} מתוך ${coverage.events_total} אירועים זמינים לחיפוש פנים`}</span>{coverage.unavailable > 0 && <small>{coverage.unavailable} תמונות אינן זמינות</small>}{coverage.no_faces > 0 && <small>{coverage.no_faces} תמונות ללא פנים לחיפוש</small>}</div>{!coverage.running && <button className="button secondary" onClick={rebuild}><Search />השלם חיפוש בהיסטוריה</button>}</div>}
    {source.person && (referenceMode || (people.data?.users || []).some(person => person.slug === source.person && !person.count)) && <p className="inline-alert">כדי להפעיל זיהוי, בחר תמונות ייחוס מהאירועים שכבר שויכו לאדם. תמונה איכותית אחת מספיקה להתחלה; כמה זוויות משפרות את הזיהוי.</p>}
    <div className="face-explorer-controls"><details className="face-filter-disclosure"><summary aria-label="סינון הופעות" title="סינון"><SlidersHorizontal /></summary>
    <div className="face-filters face-filters-compact">
      <label>מצלמה<input value={filters.camera} list="face-camera-options" placeholder="כל המצלמות" onChange={e => change("camera", e.target.value)} /><datalist id="face-camera-options">{(state.data?.cameras || []).map(camera => <option key={camera} value={camera} />)}</datalist></label>
      <label>מתאריך<input type="datetime-local" value={filters.date_from} onChange={e => change("date_from", e.target.value)} /></label>
      <label>עד תאריך<input type="datetime-local" value={filters.date_to} onChange={e => change("date_to", e.target.value)} /></label>
      <label>סדר<select value={filters.order} onChange={e => change("order", e.target.value)}><option value="asc">מהישן לחדש</option><option value="desc">מהחדש לישן</option></select></label>
      {source.person && <label className="face-check"><input type="checkbox" checked={filters.similar} onChange={e => change("similar", e.target.checked)} />כלול חיפוש דומה, גם ללא זיהוי</label>}
      {(!source.person || filters.similar) && <label>סף דמיון {percent(filters.threshold)}<input type="range" min="0.3" max="0.9" step="0.05" value={filters.threshold} onChange={e => change("threshold", Number(e.target.value))} /></label>}
    </div></details><div className="face-explorer-actions">{source.person && (referenceMode ? <button className="button secondary" onClick={() => setReferenceMode(false)}><UserPlus />סיום בחירת תמונות</button> : <ActionsMenu label="אפשרויות היסטוריית פנים"><button className="button secondary" onClick={() => { setReferenceMode(true); change("similar", false); }}><UserPlus />בחר תמונות ייחוס</button></ActionsMenu>)}{onEnroll && <button className="button secondary" onClick={onEnroll}><UserPlus />הוסף לגלריה</button>}</div></div>
    {samples && <div><p>בחר את הפנים לחיפוש מתוך האירוע:</p><div className="face-candidates">{samples.map(sample => <button key={sample.sample_id} onClick={() => { setSource({ sample_id: sample.sample_id }); setSamples(null); setOffset(0); }}><FaceImage src={sample.image} /><span>חפש פנים אלה</span></button>)}</div></div>}
    {state.loading && !state.data ? <Loading text="מחפש הופעות בכל המצלמות…" /> : state.error && !state.data ? <ErrorState error={state.error} retry={() => setRetry(v => v + 1)} /> : state.data && <>
      <div className="face-results-toolbar"><div className="face-result-count"><b>{state.data.total} אירועים</b><span>{state.data.cameras.length} מצלמות</span></div><div className="face-view-toggle" role="group" aria-label="תצוגת אירועים"><button className={`button ${view === "grid" ? "primary" : "secondary"}`} aria-label="תצוגת רשת" aria-pressed={view === "grid"} onClick={() => changeView("grid")}><Grid2X2 /></button><button className={`button ${view === "list" ? "primary" : "secondary"}`} aria-label="תצוגת רשימה" aria-pressed={view === "list"} onClick={() => changeView("list")}><List /></button><button className={`button ${view === "timeline" ? "primary" : "secondary"}`} aria-label="תצוגת ציר זמן" aria-pressed={view === "timeline"} onClick={() => changeView("timeline")}><Clock3 /></button></div></div>
      <div className={view === "timeline" ? "face-timeline-scroll" : undefined} tabIndex={view === "timeline" ? 0 : undefined} role={view === "timeline" ? "region" : undefined} aria-label={view === "timeline" ? "ציר זמן הפנים" : undefined}>{groups.map(([day, rows]) => <section className="face-day" key={day}><h3><Clock3 />{day}</h3><div className={`face-timeline face-timeline-${view === "timeline" ? "vertical" : view}`}>{rows.map(row => <div className="face-timeline-item" key={row.event_id}><button className={`face-appearance ${isBlacklistedEvent(row, people.data?.users || []) ? "blacklist-outline" : ""}`} onClick={() => referenceMode && source.person && !filters.similar ? setReferenceEvent(row) : setEvent(row)}><span className="face-appearance-photo"><FaceImage src={eventImage(row)} alt={`תמונת אירוע · ${row.camera}`} /><FaceScore event={row} source={row.event_id === state.data.source_event_id} /></span><span className="face-appearance-details"><time>{new Date((row.start_ts ?? row.updated_ts)*1000).toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time><b className="face-appearance-camera"><Camera />{row.camera}</b><small>{row.ground_truth || row.person || "לא זוהה"}</small>{row.event_id === state.data.source_event_id && <Badge tone="info">תמונת החיפוש</Badge>}</span><ChevronLeft className="face-appearance-chevron" /></button>{referenceMode && source.person && !filters.similar && <button className="button secondary" onClick={() => setReferenceEvent(row)}>בחר פנים מהאירוע</button>}</div>)}</div></section>)}</div>
      {!state.data.total && <Empty icon={Search} title="לא נמצאו הופעות בטווח שנבחר" text="אפשר להשלים את החיפוש בהיסטוריה או לשנות את הסינון." />}
      {view === "timeline" && state.error && <ErrorState error={state.error} retry={() => setRetry(v => v + 1)} />}
      {view === "timeline" && state.data.total > 50 && <div className="face-timeline-load-more"><span>{state.data.events.length} מתוך {state.data.total} אירועים</span>{state.data.events.length < state.data.total && <button className="button secondary" aria-label="טען עוד אירועים" disabled={state.loading} onClick={() => state.error ? setRetry(v => v + 1) : setOffset(offset + 50)}>{state.loading ? "טוען…" : "טען עוד אירועים"}</button>}</div>}
      {view !== "timeline" && state.data.total > 50 && <div className="face-pagination"><button className="button secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset-50))}>הקודמים</button><span>{offset+1}–{Math.min(offset+50,state.data.total)} מתוך {state.data.total}</span><button className="button secondary" disabled={offset+50 >= state.data.total} onClick={() => setOffset(offset+50)}>הבאים</button></div>}
    </>}
    <details className="face-history-note"><summary>חסרים אירועים ישנים?</summary><p>אירועים מזוהים נשמרים לפי מדיניות ההיסטוריה. חיפוש דומה זמין לאירועים שנאספו מאז 6.1 ולתמונות היסטוריות שעדיין קיימות. תמונות שכבר נמחקו אינן ניתנות לשחזור.</p>{index.data?.running ? <Loading text={`סורק היסטוריה: ${index.data.processed} מתוך ${index.data.total}`} /> : <button className="button secondary" onClick={rebuild}>סרוק תמונות היסטוריות זמינות</button>}{index.data?.error && <p role="alert">{index.data.error}</p>}<button className="text-button" onClick={() => { setOffset(0); setRetry(v => v+1); }}>רענן תוצאות</button></details>
    {event && <Evidence event={event} people={people.data?.users || []} onClose={() => setEvent(null)} onSimilar={nextSource => { setSource(nextSource); setOffset(0); setSamples(null); setView("timeline"); }} />}
    {referenceEvent && <HistoryReferenceChooser event={referenceEvent} person={source.person} onClose={() => setReferenceEvent(null)} onAdded={result => { people.reload(); onReferencesChanged?.(result); }} />}
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
      const saved = await api("faces/enroll", { method: "POST", body: JSON.stringify({ token: result.token, ...(enrollPerson ? { person: enrollPerson } : { name }) }) });
      toast(saved.added ? "התמונה נוספה לגלריה" : "התמונה כבר קיימת בגלריה", saved.added ? "success" : "info"); setEnroll(false); setResult(null); setSource({}); users.reload();
    } catch (err) { toast(err.message, "error"); }
    finally { setSaving(false); }
  };
  return <div className="faces-page">
    <PageHeader eyebrow="גלריה וחיפוש" title="גלריה וחיפוש" description="חיפוש בתמונה או באדם מהגלריה." actions={<button className="button secondary" onClick={() => navigate("people")}><Users />ניהול אנשים</button>} />
    <Panel><div className="face-search-start"><label className={`upload-zone ${busy ? "busy" : ""}`}><Upload /><b>{busy ? "מחפש פנים…" : "העלה תמונה לחיפוש"}</b><span>JPG, PNG או WebP · עד 8MB · חיפוש אינו מוסיף אדם לגלריה</span><input type="file" accept="image/jpeg,image/png,image/webp" disabled={busy} onChange={e => { const selected = e.target.files[0]; if (selected) search(selected); e.target.value = ""; }} /></label><div className="face-person-picker"><label>או בחר אדם קיים<select value={source.person || ""} onChange={e => { setSource(e.target.value ? { person: e.target.value } : {}); setResult(null); }}><option value="">בחר אדם…</option>{(users.data?.users || []).map(person => <option value={person.slug} key={person.slug}>{person.name}</option>)}</select></label><p>בחירת אדם מציגה את כל האירועים שזוהו או אושרו כשייכים לו, מסודרים לפי ימים ושעות.</p></div></div></Panel>
    {error && <ErrorState error={error} retry={() => search(file, faceIndex)} />}
    {result?.status === "needs_selection" && <Panel><h2>בחר את הפנים לחיפוש</h2><div className="face-choice-image"><img src={result.preview} alt="בחירת פנים מהתמונה" />{result.candidates.map(candidate => { const [l,t,r,b] = candidate.bbox; return <button key={candidate.index} aria-label={`בחר פנים ${candidate.index+1}`} style={{ left:`${l*100}%`, top:`${t*100}%`, width:`${(r-l)*100}%`, height:`${(b-t)*100}%` }} onClick={() => search(file,candidate.index)}><span>בחר</span></button>; })}</div></Panel>}
    {result?.token && <Panel><div className="face-query"><FaceImage src={result.preview} /><div><h2>הפנים לחיפוש</h2><p>החיפוש זמני לעשר דקות. הוספה לגלריה נעשית רק בבחירה מפורשת.</p></div></div>{result.matches?.length > 0 && <><h3>פנים דומות בגלריה</h3><div className="face-candidates">{result.matches.map(match => <button key={match.slug} onClick={() => setSource({ person: match.slug })}><FaceImage src={match.photo} /><b>{match.name}</b><small>{percent(match.similarity)} דמיון</small></button>)}</div><button className="text-button" onClick={() => setSource({ token: result.token })}>חזרה לחיפוש לפי התמונה</button></>}</Panel>}
    {Object.keys(source).length > 0 ? <Panel><FaceExplorer key={JSON.stringify(source)} source={Object.fromEntries(Object.entries(source).filter(([key]) => key !== "view"))} initialView={source.view === "timeline" ? "timeline" : "grid"} onEnroll={result?.token && file ? () => setEnroll(true) : null} /></Panel> : !busy && !result && <Empty icon={Images} title="כל ההופעות, במקום אחד" text="אפשר להתחיל מתמונה, מאדם בגלריה או מפנים לא מזוהות בתור הבדיקה." />}
    {enroll && <Modal title="הוספת התמונה לגלריה" subtitle="בחר אדם קיים או צור אדם חדש" onClose={() => setEnroll(false)}><form className="editor-form" onSubmit={save}><label>אדם קיים<select value={enrollPerson} onChange={e => setEnrollPerson(e.target.value)}><option value="">אדם חדש</option>{(users.data?.users || []).map(person => <option key={person.slug} value={person.slug}>{person.name}</option>)}</select></label>{!enrollPerson && <label>שם האדם<input required value={name} onChange={e => setName(e.target.value)} /></label>}<button className="button primary" disabled={saving}>{saving ? "שומר…" : "הוסף תמונה"}</button></form></Modal>}
  </div>;
}
