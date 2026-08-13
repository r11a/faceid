import React, { useEffect, useMemo, useState } from "react";
import {
  Activity, ArrowLeft, Camera, Check, ChevronLeft, ClipboardCheck, Clock3, DoorOpen, Download,
  Film, ImageOff, Images, MapPin, MoreVertical, Play, Plus, Search, ShieldAlert,
  Sparkles, Star, Trash2, Upload, UserCheck, UserPlus, UserRound, Users, X,
} from "lucide-react";
import { api, assetUrl, query } from "../api.js";
import { useResource } from "../hooks.js";
import { Badge, Empty, ErrorState, Loading, Metric, Modal, PageHeader, Panel, dateTime, decision, percent, relativeTime, useToast } from "../ui.jsx";

function PersonAvatar({ person, className = "" }) {
  return <div className={`person-avatar ${className}`}>{person?.photo ? <img src={assetUrl(person.photo)} alt={person.name} /> : <UserRound />}</div>;
}

function EventThumb({ event, onClick }) {
  const [failed, setFailed] = useState(false);
  return <button className="event-thumb" onClick={onClick}>{failed ? <ImageOff /> : <img src={assetUrl(`api/activity/${encodeURIComponent(event.event_id)}/image`)} onError={() => setFailed(true)} alt="תמונת אירוע" loading="lazy" />}</button>;
}

function EventModal({ eventId, people, onClose, onChanged }) {
  const detail = useResource(`audit/${encodeURIComponent(eventId)}`);
  const references = useResource(`activity/${encodeURIComponent(eventId)}/references`);
  const [clip, setClip] = useState(false);
  const [clipError, setClipError] = useState("");
  const [label, setLabel] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const toast = useToast();
  const saveLabel = async (value) => {
    if (!value) return;
    try {
      await api(`audit/${eventId}/ground-truth`, { method: "POST", body: JSON.stringify({ label: value }) });
      toast("האימות נשמר וישמש למדידת הדיוק"); onChanged?.(); onClose();
    } catch (error) { toast(error.message, "error"); }
  };
  const undo = async () => {
    try { await api(`audit/${eventId}/undo`, { method: "POST" }); toast("האימות האחרון בוטל"); onChanged?.(); onClose(); }
    catch (error) { toast(error.message, "error"); }
  };
  const runTest = async () => {
    setTesting(true);
    try { setTestResult(await api(`recognition-test/${eventId}`, { method: "POST" })); }
    catch (error) { toast(error.message, "error"); }
    finally { setTesting(false); }
  };
  const addToBodyLearning = async () => {
    const person = label || predicted;
    if (!person) return toast("בחר תחילה אדם קיים", "info");
    try { await api(`body/from-event/${eventId}`, { method: "POST", body: JSON.stringify({ person }) }); toast("האירוע נוסף לבדיקה בלימוד הגוף"); }
    catch (error) { toast(error.message, "error"); }
  };
  if (detail.loading) return <Modal title="בדיקת אירוע" onClose={onClose}><Loading /></Modal>;
  if (detail.error) return <Modal title="בדיקת אירוע" onClose={onClose}><ErrorState error={detail.error} retry={detail.reload} /></Modal>;
  const event = detail.data?.event || {};
  const predicted = event.person || event.probable_person;
  const info = decision[event.status] || [event.status || "לא ידוע", "neutral"];
  return <Modal title="בדיקת אירוע" subtitle={`${dateTime(event.start_ts || event.updated_ts)} · ${event.camera || "מצלמה לא ידועה"}`} onClose={onClose} wide>
    <div className="event-review-layout">
      <div className="event-media-large">
        {clip ? <video controls autoPlay playsInline onError={() => setClipError("לא נמצא קליפ. ייתכן שההקלטה כבר נמחקה מ־Frigate.")}><source src={assetUrl(`api/activity/${eventId}/clip`)} type="video/mp4" /></video> : <img src={assetUrl(`api/activity/${eventId}/image`)} alt="צילום האירוע" />}
        <div className="media-toolbar"><button className="button primary" onClick={() => { setClip(!clip); setClipError(""); }}>{clip ? <Images /> : <Play />}{clip ? "חזרה לתמונה" : "נגן קליפ"}</button><a className="button secondary" href={assetUrl(`api/activity/${eventId}/clip?download=true`)}><Download />הורדה</a></div>
        {clipError && <div className="inline-alert danger">{clipError}</div>}
      </div>
      <div className="review-summary">
        <div className="review-decision"><Badge tone={info[1]}>{info[0]}</Badge><h3>{predicted || "אדם לא מוכר"}</h3><p>התאמה {percent(event.score)} · פער {percent(event.margin)} · {event.confirmations || 0} תמונות תומכות</p></div>
        <div className="evidence-list">
          <div><ShieldAlert /><span><b>בדיקת חיוּת</b><small>{event.liveness_status === "live" ? `עברה · ${percent(event.liveness_score)}` : event.liveness_status === "spoof" ? "חשד לתמונה או מסך" : "לא הושלמה"}</small></span></div>
          <div><Camera /><span><b>מקור</b><small>{event.camera || "—"}</small></span></div>
          <div><Sparkles /><span><b>הקשר AI</b><small>{event.ai_description || "לא הופעל או שאין תיאור"}</small></span></div>
        </div>
        {references.data?.references?.length > 0 && <div><h4>תמונות ייחוס</h4><div className="reference-strip">{references.data.references.map((item) => <img src={assetUrl(item.url)} alt={item.caption} key={item.url} />)}</div></div>}
        <div className="verify-box"><h4>מי באמת הופיע?</h4>{predicted && <button className="button success" onClick={() => saveLabel(predicted)}><Check />אשר: זה {predicted}</button>}<button className="button secondary" onClick={() => saveLabel("__unknown__")}><X />זה אדם לא מוכר</button><div className="inline-form"><select value={label} onChange={(e) => setLabel(e.target.value)}><option value="">בחר אדם אחר…</option>{people.map((person) => <option value={person.name} key={person.slug}>{person.name}</option>)}</select><button className="button secondary" disabled={!label} onClick={() => saveLabel(label)}>שמור בחירה</button></div><button className="text-button" onClick={undo}>בטל אימות אחרון</button></div>
        <button className="button secondary full" disabled={testing} onClick={runTest}><Sparkles />{testing ? "בודק שלוש שכבות…" : "בדיקה מתקדמת: פנים, גוף ו־AI"}</button>
        <button className="button secondary full" disabled={!label && !predicted} onClick={addToBodyLearning}><UserRound />הוסף לבדיקה בלימוד גוף</button>
        {testResult && <div className="test-result"><div><b>פנים</b><span>{testResult.face?.person || testResult.face?.decision || "—"}</span></div><div><b>גוף (רמז בלבד)</b><span>{testResult.body?.candidate || testResult.body?.person || "—"}</span></div><div><b>AI (תחקור בלבד)</b><span>{testResult.vision?.status || "כבוי"}</span></div></div>}
      </div>
    </div>
  </Modal>;
}

export function DashboardPage({ navigate }) {
  const dashboard = useResource("dashboard", { poll: 15000 });
  const health = useResource("health", { poll: 15000 });
  const [eventId, setEventId] = useState(null);
  if (dashboard.loading) return <Loading text="מכין תמונת מצב…" />;
  if (dashboard.error) return <ErrorState error={dashboard.error} retry={dashboard.reload} />;
  const data = dashboard.data || {};
  const summary = data.summary || {};
  const online = health.data?.status === "ok";
  return <>
    <PageHeader eyebrow="מרכז הפעלה" title="שלום, הנה מה שקורה עכשיו" description="המידע החשוב מוצג כאן. כלי כיול והגדרות מחכים באזור המתקדם." actions={<button className="button primary" onClick={() => navigate("review")}><ClipboardCheck />לבדיקת אירועים{health.data?.queue ? ` (${health.data.queue})` : ""}</button>} />
    <div className="metrics-grid">
      <Metric icon={ShieldAlert} label="מצב המערכת" value={online ? "תקינה" : "דורש בדיקה"} note={online ? "כל השירותים מגיבים" : "פתח את מרכז התקינות"} tone={online ? "green" : "red"} />
      <Metric icon={UserCheck} label="זיהויים היום" value={summary.recognized_today ?? summary.today ?? 0} note={`${summary.events_today ?? 0} אירועים היום`} tone="turquoise" />
      <Metric icon={ClipboardCheck} label="ממתינים לאישור" value={health.data?.queue ?? 0} note="תמונות שנשמרו באופן מבוקר" tone="amber" />
      <Metric icon={Users} label="אנשים במערכת" value={data.people?.length || 0} note={`${summary.seen_today ?? 0} נראו היום`} tone="purple" />
    </div>
    <div className="dashboard-columns">
      <Panel className="people-overview"><div className="panel-heading"><div><span className="eyebrow">אנשים</span><h2>מי נראה לאחרונה</h2></div><button className="text-button" onClick={() => navigate("people")}>לכל האנשים <ChevronLeft /></button></div><div className="people-card-grid">{(data.people || []).slice(0, 8).map((person) => <button className="person-card" key={person.slug} onClick={() => navigate("people")}><PersonAvatar person={person} /><div><h3>{person.name}{person.favorite && <Star className="favorite" />}</h3><p><MapPin />{person.last_camera || "לא נראה עדיין"}</p><span>{relativeTime(person.last_seen)}</span></div><div className="person-score"><strong>{percent(person.avg_score)}</strong><small>דיוק ממוצע</small></div></button>)}</div>{!data.people?.length && <Empty icon={Users} title="עדיין אין אנשים" text="התחל בהטמעה קצרה של אדם ראשון." action={<button className="button primary" onClick={() => navigate("people")}><Plus />הטמעת אדם</button>} />}</Panel>
      <Panel className="recent-panel"><div className="panel-heading"><div><span className="eyebrow">עכשיו</span><h2>אירועים אחרונים</h2></div><button className="text-button" onClick={() => navigate("events")}>לכל האירועים <ChevronLeft /></button></div><div className="recent-list">{(data.recent || []).map((event) => { const info = decision[event.status] || [event.status, "neutral"]; return <button key={event.event_id} onClick={() => setEventId(event.event_id)}><EventThumb event={event} /><span><b>{event.person || "לא מוכר"}</b><small>{event.camera} · {relativeTime(event.start_ts || event.updated_ts)}</small></span><Badge tone={info[1]}>{percent(event.score)}</Badge></button>; })}</div></Panel>
    </div>
    {eventId && <EventModal eventId={eventId} people={data.people || []} onClose={() => setEventId(null)} onChanged={dashboard.reload} />}
  </>;
}

export function EventsPage() {
  const users = useResource("users");
  const [filters, setFilters] = useState({ q: "", person: "", status: "", camera: "", date_from: "", date_to: "" });
  const [request, setRequest] = useState({ limit: 100 });
  const events = useResource(`activity?${query(request)}`);
  const [eventId, setEventId] = useState(null);
  const apply = (event) => { event.preventDefault(); setRequest({ limit: 500, ...filters, date_from: filters.date_from ? new Date(filters.date_from).getTime() / 1000 : "", date_to: filters.date_to ? new Date(filters.date_to).getTime() / 1000 : "" }); };
  return <>
    <PageHeader eyebrow="תחקור" title="אירועים וקליפים" description="מצא כל אירוע לפי אדם, מצלמה, החלטה או טווח זמן. לחץ על תמונה לצפייה, אימות וקליפ." />
    <Panel className="filter-panel"><form onSubmit={apply}><label className="search-field"><Search /><input value={filters.q} onChange={(e) => setFilters({ ...filters, q: e.target.value })} placeholder="חיפוש אדם, מצלמה או תיאור…" /></label><select value={filters.person} onChange={(e) => setFilters({ ...filters, person: e.target.value })}><option value="">כל האנשים</option>{(users.data?.users || []).map((person) => <option key={person.slug}>{person.name}</option>)}</select><select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}><option value="">כל ההחלטות</option><option value="recognized">זוהה</option><option value="ambiguous">דורש אישור</option><option value="unknown">לא מוכר</option><option value="spoof_suspected">חשד לזיוף</option><option value="liveness_unconfirmed">חיוּת לא אומתה</option><option value="no_face">ללא פנים</option></select><input type="datetime-local" value={filters.date_from} onChange={(e) => setFilters({ ...filters, date_from: e.target.value })} /><input type="datetime-local" value={filters.date_to} onChange={(e) => setFilters({ ...filters, date_to: e.target.value })} /><button className="button primary"><Search />חיפוש</button></form></Panel>
    {events.loading ? <Loading /> : events.error ? <ErrorState error={events.error} retry={events.reload} /> : <Panel><div className="panel-heading"><div><span className="eyebrow">תוצאות</span><h2>{events.data?.total ?? events.data?.events?.length ?? 0} אירועים</h2></div></div><div className="events-table"><div className="events-head"><span>תמונה</span><span>זמן</span><span>מצלמה</span><span>החלטה</span><span>אדם</span><span>ביטחון</span></div>{(events.data?.events || []).map((event) => { const info = decision[event.status] || [event.status, "neutral"]; return <button className="event-row" onClick={() => setEventId(event.event_id)} key={event.event_id}><EventThumb event={event} /><span data-label="זמן">{dateTime(event.start_ts || event.updated_ts)}</span><span data-label="מצלמה">{event.camera || "—"}</span><span data-label="החלטה"><Badge tone={info[1]}>{info[0]}</Badge></span><span data-label="אדם"><b>{event.person || event.probable_person || "—"}</b></span><span data-label="ביטחון">{percent(event.score)}<small>{event.confirmations || 0} תמונות</small></span></button>; })}</div>{!events.data?.events?.length && <Empty icon={Search} title="לא נמצאו אירועים" text="נסה טווח תאריכים רחב יותר או נקה חלק מהמסננים." />}</Panel>}
    {eventId && <EventModal eventId={eventId} people={users.data?.users || []} onClose={() => setEventId(null)} onChanged={events.reload} />}
  </>;
}

export function ReviewPage() {
  const unknowns = useResource("unknowns");
  const policy = useResource("unknowns/policy");
  const users = useResource("users");
  const [selected, setSelected] = useState({});
  const [person, setPerson] = useState("");
  const toast = useToast();
  const picked = Object.keys(selected).filter((id) => selected[id]);
  const act = async (action) => {
    if (!picked.length) return toast("בחר לפחות תמונה אחת", "info");
    try {
      if (action === "resolve" && !person) return toast("בחר למי שייכות התמונות", "info");
      const endpoint = action === "resolve" ? "unknowns/resolve" : action === "ignore" ? "unknowns/ignore" : "unknowns/discard";
      await api(endpoint, { method: "POST", body: JSON.stringify({ ids: picked, person }) });
      toast(action === "resolve" ? "האירועים אושרו בלי להעמיס תמונות נוספות בגלריה" : action === "ignore" ? "הפנים הועברו להשתקה" : "התמונות נמחקו מהתור");
      setSelected({}); unknowns.reload();
    } catch (error) { toast(error.message, "error"); }
  };
  const maintain = async () => { try { const result = await api("unknowns/maintenance", { method: "POST" }); toast(`${result.removed || result.pruned || 0} תמונות מיותרות נוקו`); unknowns.reload(); } catch (error) { toast(error.message, "error"); } };
  const autoAssign = async () => { if (!confirm("לאשר אוטומטית רק תמונות שעוברות את סף הזיהוי הנוכחי?")) return; try { const result = await api("unknowns/auto_assign", { method: "POST" }); toast(`${result.total || 0} אירועים אושרו אוטומטית`); unknowns.reload(); } catch (error) { toast(error.message, "error"); } };
  const clusters = unknowns.data || [];
  return <>
    <PageHeader eyebrow="תור עבודה" title="רק מה שבאמת דורש החלטה" description={`המערכת שומרת לכל היותר ${policy.data?.max_total ?? "מספר מוגבל של"} תמונות ומונעת כפילויות. אישור כאן לא מוסיף עוד תמונות לגלריה.`} />
    <Panel className="review-toolbar"><div><b>{clusters.reduce((sum, cluster) => sum + cluster.length, 0)} תמונות · {clusters.length} קבוצות</b><span>{picked.length ? `${picked.length} נבחרו` : "בחר תמונות דומות ובצע פעולה אחת"}</span></div><div><select value={person} onChange={(e) => setPerson(e.target.value)}><option value="">בחר אדם קיים…</option>{(users.data?.users || []).map((item) => <option key={item.slug} value={item.slug}>{item.name}</option>)}</select><button className="button success" disabled={!picked.length || !person} onClick={() => act("resolve")}><Check />אשר זיהוי</button><button className="button secondary" disabled={!picked.length} onClick={() => act("ignore")}>השתק פנים אלה</button><button className="button danger-ghost" disabled={!picked.length} onClick={() => act("discard")}><Trash2 />מחק מהתור</button></div><details className="maintenance-actions"><summary>פעולות תחזוקה</summary><button className="button secondary" onClick={maintain}>נקה כפילויות וישנות</button><button className="button secondary" onClick={autoAssign}>אשר התאמות בטוחות</button></details></Panel>
    {unknowns.loading ? <Loading /> : unknowns.error ? <ErrorState error={unknowns.error} retry={unknowns.reload} /> : <div className="cluster-list">{clusters.map((cluster, index) => <Panel className="cluster" key={cluster[0]?.id || index}><div className="panel-heading"><div><span className="eyebrow">קבוצה {index + 1}</span><h2>{cluster[0]?.guess_name ? `אולי ${cluster[0].guess_name}` : "אדם לא מוכר"}</h2></div><button className="text-button" onClick={() => setSelected((current) => ({ ...current, ...Object.fromEntries(cluster.map((item) => [item.id, true])) }))}>בחר את כל הקבוצה</button></div><div className="unknown-grid">{cluster.map((item) => <label className={selected[item.id] ? "selected" : ""} key={item.id}><input type="checkbox" checked={Boolean(selected[item.id])} onChange={(e) => setSelected({ ...selected, [item.id]: e.target.checked })} /><img src={assetUrl(`media/unknowns/${item.id}.jpg`)} alt="פנים לבדיקה" /><span>{item.camera || "—"}<small>{dateTime(item.ts || item.created_at)}</small></span><i><Check /></i></label>)}</div></Panel>)}</div>}
    {!unknowns.loading && !clusters.length && <Empty icon={Check} title="הכול טופל" text="אין כרגע אירועים שמחכים לאישור שלך." />}
  </>;
}

export function PeoplePage() {
  const users = useResource("users");
  const [editor, setEditor] = useState(null);
  const [profile, setProfile] = useState(null);
  const toast = useToast();
  const create = () => setEditor({ slug: null, name: "", files: [] });
  const favorite = async (person) => { try { await api(`persons/${person.slug}/favorite`, { method: "POST", body: JSON.stringify({ favorite: !person.favorite }) }); toast(person.favorite ? "הוסר מהמועדפים" : "נוסף למועדפים"); users.reload(); } catch (error) { toast(error.message, "error"); } };
  const open = async (person) => {
    setProfile({ loading: true, person });
    try { setProfile({ loading: false, person, data: await api(`persons/${person.slug}/profile?timezone=${encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone)}`) }); }
    catch (error) { toast(error.message, "error"); setProfile(null); }
  };
  if (users.loading) return <Loading />;
  if (users.error) return <ErrorState error={users.error} retry={users.reload} />;
  return <>
    <PageHeader eyebrow="אנשים" title="הטמעה וניהול בלי מסך טכני" description="5–10 תמונות ברורות מזוויות שונות מספיקות לרוב. המערכת מגבילה כפילויות ושומרת את הגלריה קטנה ומגוונת." actions={<button className="button primary" onClick={create}><UserPlus />הטמעת אדם חדש</button>} />
    <div className="people-management-grid">{(users.data?.users || []).map((person) => <Panel className="managed-person" key={person.slug}><button className="person-main" onClick={() => open(person)}><PersonAvatar person={person} className="large" /><div><h2>{person.name}{person.favorite && <Star className="favorite" />}</h2><Badge tone={person.state === "ready" ? "success" : person.state === "review" ? "warning" : "info"}>{person.state_label}</Badge><p><MapPin />{person.statistics?.last_camera || "לא נראה עדיין"} · {relativeTime(person.statistics?.last_seen)}</p></div></button><div className="person-mini-stats"><span><b>{person.statistics?.today || 0}</b>היום</span><span><b>{person.statistics?.last_30_days || 0}</b>ב־30 יום</span><span><b>{percent(person.statistics?.avg_score)}</b>דיוק ממוצע</span><span><b>{person.count}</b>תמונות ייחוס</span></div><div className="card-actions"><button className="icon-button" onClick={() => favorite(person)} title={person.favorite ? "הסר ממועדפים" : "הוסף למועדפים"}><Star className={person.favorite ? "favorite" : ""} /></button><button className="button secondary" onClick={() => open(person)}>סטטיסטיקה</button><button className="icon-button" onClick={() => setEditor(person)} title="עריכה"><MoreVertical /></button></div></Panel>)}</div>
    {!users.data?.users?.length && <Empty icon={Users} title="הגלריה עדיין ריקה" text="הוסף אדם ראשון. האשף יסביר אילו תמונות טובות לזיהוי." action={<button className="button primary" onClick={create}><Plus />התחלה</button>} />}
    {editor && <PersonEditor person={editor} onClose={() => setEditor(null)} onSaved={() => { setEditor(null); users.reload(); }} />}
    {profile && <PersonProfile profile={profile} onClose={() => setProfile(null)} />}
  </>;
}

function PersonEditor({ person, onClose, onSaved }) {
  const [name, setName] = useState(person.name || "");
  const [files, setFiles] = useState([]);
  const [saving, setSaving] = useState(false);
  const [pendingChoice, setPendingChoice] = useState(null);
  const toast = useToast();
  const save = async (event) => {
    event.preventDefault(); setSaving(true);
    try {
      let slug = person.slug;
      if (!slug) slug = (await api("persons", { method: "POST", body: JSON.stringify({ name }) })).slug;
      else if (name.trim() !== person.name) await api(`persons/${slug}`, { method: "PATCH", body: JSON.stringify({ name }) });
      for (const file of files) {
        const form = new FormData(); form.append("files", file);
        const result = await api(`persons/${slug}/photos`, { method: "POST", body: form });
        const choice = result.details?.find((item) => item.status === "needs_selection");
        if (choice) { setPendingChoice({ slug, file, ...choice }); setSaving(false); return; }
        const rejected = result.details?.find((item) => item.status === "rejected");
        if (rejected) toast(`${file.name}: ${rejected.message}`, "error");
      }
      toast(person.slug ? "פרטי האדם נשמרו" : "האדם נוסף למערכת"); onSaved();
    } catch (error) { toast(error.message, "error"); }
    finally { setSaving(false); }
  };
  const chooseFace = async (faceIndex) => {
    setSaving(true);
    try {
      const requestedIndex = pendingChoice.candidates?.[faceIndex]?.index ?? faceIndex;
      const form = new FormData(); form.append("files", pendingChoice.file);
      const result = await api(`persons/${pendingChoice.slug}/photos?face_index=${requestedIndex}`, { method: "POST", body: form });
      const rejected = result.details?.find((item) => item.status !== "added");
      if (rejected) throw new Error(rejected.message);
      toast("הפנים שנבחרו נוספו לאדם"); setPendingChoice(null); onSaved();
    } catch (error) { toast(error.message, "error"); }
    finally { setSaving(false); }
  };
  const remove = async () => {
    if (!confirm(`למחוק את ${person.name}? הפעולה לא ניתנת לביטול.`)) return;
    try { await api(`persons/${person.slug}`, { method: "DELETE" }); toast("האדם נמחק"); onSaved(); }
    catch (error) { toast(error.message, "error"); }
  };
  return <Modal title={person.slug ? `עריכת ${person.name}` : "הטמעת אדם חדש"} subtitle="השתמש בתמונות חדות: פנים מול המצלמה, מעט לצדדים ובתאורה שונה" onClose={onClose}>
    {pendingChoice ? <div className="face-choice"><div className="safety-banner"><UserRound /><div><b>נמצאו כמה אנשים בתמונה</b><p>בחר במפורש את הפנים ששייכות ל־{name}. שום פנים לא יישמרו לפני הבחירה.</p></div></div><div className="face-choice-image"><img src={pendingChoice.preview} alt="בחירת פנים" />{(pendingChoice.candidates || []).map((candidate, index) => { const [left, top, right, bottom] = candidate.bbox; return <button key={index} style={{ left: `${left * 100}%`, top: `${top * 100}%`, width: `${(right - left) * 100}%`, height: `${(bottom - top) * 100}%` }} onClick={() => chooseFace(index)}><span>בחר פנים {index + 1}</span></button>; })}</div><button className="button secondary" onClick={() => setPendingChoice(null)}>חזרה לבחירת תמונות</button></div> : <form className="editor-form" onSubmit={save}><label>שם מלא<input autoFocus required value={name} onChange={(e) => setName(e.target.value)} placeholder="לדוגמה: רונן כהן" /></label><label className="upload-zone"><Upload /><b>הוסף 5–10 תמונות טובות</b><span>JPG או PNG · אפשר לבחור כמה תמונות יחד</span><input type="file" accept="image/*" multiple onChange={(e) => setFiles([...e.target.files])} /></label>{files.length > 0 && <div className="selected-files"><Check />נבחרו {files.length} תמונות</div>}<div className="photo-guidance"><div><Check /><span><b>כן</b>פנים גדולות, חדות ומוארות</span></div><div><X /><span><b>לא</b>קבוצה, משקפי שמש או פנים קטנות</span></div></div><div className="form-actions">{person.slug && <button type="button" className="button danger-ghost" onClick={remove}><Trash2 />מחיקת אדם</button>}<span /><button type="button" className="button secondary" onClick={onClose}>ביטול</button><button className="button primary" disabled={saving}>{saving ? "שומר…" : "שמירה"}</button></div></form>}
  </Modal>;
}

function PersonProfile({ profile, onClose }) {
  const person = profile.person;
  if (profile.loading) return <Modal title={person.name} onClose={onClose}><Loading /></Modal>;
  const data = profile.data || {};
  const stats = person.statistics || {};
  return <Modal title={person.name} subtitle="פרופיל פעילות ודיוק" onClose={onClose} wide><div className="profile-hero"><PersonAvatar person={person} className="xl" /><div><h3>{relativeTime(stats.last_seen)}</h3><p><MapPin />{stats.last_camera || "לא נראה עדיין"}</p></div></div><div className="metrics-grid compact"><Metric label="הופעות" value={stats.appearances || 0} /><Metric label="ב־30 יום" value={stats.last_30_days || 0} tone="turquoise" /><Metric label="דיוק ממוצע" value={percent(stats.avg_score)} tone="purple" /><Metric label="מצלמה נפוצה" value={stats.top_camera || "—"} tone="amber" /></div><Panel><div className="panel-heading"><div><span className="eyebrow">גלריה</span><h2>{data.gallery?.photos || person.count || 0} תמונות ייחוס</h2></div></div><p>{data.gallery?.recommendation}</p></Panel></Modal>;
}
