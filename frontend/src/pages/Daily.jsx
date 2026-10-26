import React, { useEffect, useMemo, useState } from "react";
import {
  Activity, ArrowLeft, Camera, Check, ChevronLeft, ClipboardCheck, Clock3, DoorOpen, Download,
  Film, ImageOff, Images, LayoutGrid, List, MapPin, Play, Plus, Search, ShieldAlert,
  Sparkles, Star, Trash2, Upload, UserCheck, UserPlus, UserRound, Users, X,
} from "lucide-react";
import { api, assetUrl, query } from "../api.js";
import { useImageRetry, useResource } from "../hooks.js";
import { ActionsMenu, Badge, Empty, ErrorState, Loading, Modal, PageHeader, Panel, dateTime, decision, percent, relativeTime, useToast } from "../ui.jsx";

import { FaceExplorer, HistoryReferenceChooser } from "./Faces.jsx";
import { isBlacklistedEvent } from "../alerts.js";
import { EventVideo } from "../EventVideo.jsx";

function PersonAvatar({ person, className = "" }) {
  return <div className={`person-avatar ${className}`}>{person?.photo ? <img src={assetUrl(person.photo)} alt={person.name} decoding="async" loading="lazy" /> : <UserRound />}</div>;
}

function EventThumb({ event, onClick }) {
  const image = useImageRetry(`api/activity/${encodeURIComponent(event.event_id)}/image`);
  const Element = onClick ? "button" : "span";
  return <Element className="event-thumb" onClick={onClick}>{image.failed ? <ImageOff /> : <img src={assetUrl(image.url)} onError={image.onError} alt="תמונת אירוע" decoding="async" loading="lazy" />}</Element>;
}

export function EventModal({ eventId, people, onClose, onChanged }) {
  const detail = useResource(`audit/${encodeURIComponent(eventId)}`, { poll: 3000 });
  const references = useResource(`activity/${encodeURIComponent(eventId)}/references`);
  const [clip, setClip] = useState(true);
  const [label, setLabel] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [showFaces, setShowFaces] = useState(false);
  const [enroll, setEnroll] = useState(false);
  const toast = useToast();
  const saveLabel = async (value) => {
    if (!value || detail.data?.event?.status === "processing") return;
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
  const event = detail.data?.event || {};
  const predicted = event.person || event.probable_person;
  const processing = event.status === "processing";
  const info = decision[event.status] || [event.status || "לא ידוע", "neutral"];
  return <Modal title="בדיקת אירוע" subtitle={`${dateTime(event.start_ts || event.updated_ts)} · ${event.camera || "מצלמה לא ידועה"}`} onClose={onClose} wide>
    <div className="event-review-layout">
      <div className={`event-media-large ${isBlacklistedEvent(event, people) ? "blacklist-outline" : ""}`}>
        {clip ? <EventVideo eventId={eventId} /> : <img src={assetUrl(`api/activity/${eventId}/image`)} alt="צילום האירוע" />}
        <div className="media-toolbar"><button className="button primary" onClick={() => setClip(!clip)}>{clip ? <Images /> : <Play />}{clip ? "חזרה לתמונה" : "נגן קליפ"}</button><a className="button secondary" href={assetUrl(`api/activity/${eventId}/clip?download=true`)}><Download />הורדה</a></div>
      </div>
      <div className="review-summary">
        {detail.loading ? <Loading /> : detail.error ? <ErrorState error={detail.error} retry={detail.reload} /> : <>
        <div className="review-decision"><Badge tone={info[1]}>{info[0]}</Badge><h3>{predicted || "אדם לא מוכר"}</h3><p>התאמה {percent(event.score)} · פער {percent(event.margin)} · {event.confirmations || 0} תמונות תומכות</p></div>
        <div className="evidence-list">
          <div><ShieldAlert /><span><b>בדיקת חיוּת</b><small>{event.liveness_status === "live" ? `עברה · ${percent(event.liveness_score)}` : event.liveness_status === "spoof" ? "חשד לתמונה או מסך" : "לא הושלמה"}</small></span></div>
          <div><Camera /><span><b>מקור</b><small>{event.camera || "—"}</small></span></div>
          <div><Sparkles /><span><b>הקשר AI</b><small>{event.ai_description || "לא הופעל או שאין תיאור"}</small></span></div>
        </div>
        {references.data?.references?.length > 0 && <div><h4>תמונות ייחוס</h4><div className="reference-strip">{references.data.references.map((item) => <img src={assetUrl(item.url)} alt={item.caption} key={item.url} />)}</div></div>}
        <div className="verify-box"><h4>מי באמת הופיע?</h4><p>האימות מתעד את תוצאת האירוע; הוא אינו מוסיף תמונת פנים לגלריה. להוספת פנים לאדם, השתמש בגלריה או בחיפוש פנים.</p>{processing && <p role="status">האירוע עדיין בעיבוד. אפשר לאמת לאחר סיום העיבוד; המצב מתעדכן אוטומטית.</p>}{predicted && <button className="button success" disabled={processing} onClick={() => saveLabel(predicted)}><Check />אשר: זה {predicted}</button>}<button className="button secondary" disabled={processing} onClick={() => saveLabel("__unknown__")}><X />זה אדם לא מוכר</button><div className="inline-form"><select value={label} onChange={(e) => setLabel(e.target.value)}><option value="">בחר אדם אחר…</option>{people.map((person) => <option value={person.name} key={person.slug}>{person.name}</option>)}</select><button className="button secondary" disabled={!label || processing} onClick={() => saveLabel(label)}>שמור בחירה</button></div><button className="text-button" onClick={undo}>בטל אימות אחרון</button></div>
        <button className="button primary full" onClick={() => setShowFaces(!showFaces)}><Search />כל ההופעות של הפנים</button>
        <button className="button secondary full" disabled={!label || processing} onClick={() => setEnroll(true)}><UserPlus />בחר תמונות פנים להוספה לאדם שנבחר</button>
        {enroll && <HistoryReferenceChooser event={{ ...event, event_id: eventId }} person={people.find(person => person.name === label)?.slug} onClose={() => setEnroll(false)} onAdded={() => onChanged?.()} />}
        {showFaces && <FaceExplorer source={{ event_id: eventId }} />}
        <details><summary>כלי זיהוי מתקדמים</summary>
        <button className="button secondary full" disabled={testing} onClick={runTest}><Sparkles />{testing ? "בודק שלוש שכבות…" : "בדיקה מתקדמת: פנים, גוף ו־AI"}</button>
        <button className="button secondary full" disabled={!label && !predicted} onClick={addToBodyLearning}><UserRound />הוסף לבדיקה בלימוד גוף</button>
        {testResult && <div className="test-result"><div><b>פנים</b><span>{testResult.face?.person || testResult.face?.decision || "—"}</span></div><div><b>גוף (רמז בלבד)</b><span>{testResult.body?.candidate || testResult.body?.person || "—"}</span></div><div><b>AI (תחקור בלבד)</b><span>{testResult.vision?.status || "כבוי"}</span></div></div>}
        </details>
        </>}
      </div>
    </div>
  </Modal>;
}

export function EventsPage() {
  const users = useResource("users");
  const [filters, setFilters] = useState({ q: "", person: "", status: "", camera: "", date_from: "", date_to: "" });
  const [request, setRequest] = useState({ limit: 100 });
  const events = useResource(`activity?${query(request)}`);
  const [eventId, setEventId] = useState(null);
  const apply = (event) => { event.preventDefault(); setRequest({ limit: 500, ...filters, date_from: filters.date_from ? new Date(filters.date_from).getTime() / 1000 : "", date_to: filters.date_to ? new Date(filters.date_to).getTime() / 1000 : "" }); };
  return <section className="events-workspace">
    <PageHeader eyebrow="היסטוריה" title="אירועים" description="חיפוש, צפייה בקליפים ואימות זהות לפי אירוע." />
    <Panel className="filter-panel"><form onSubmit={apply}><label className="search-field"><Search /><input value={filters.q} onChange={(e) => setFilters({ ...filters, q: e.target.value })} placeholder="חיפוש אדם, מצלמה או תיאור" aria-label="חיפוש אירועים" /></label><select aria-label="סינון לפי אדם" value={filters.person} onChange={(e) => setFilters({ ...filters, person: e.target.value })}><option value="">כל האנשים</option>{(users.data?.users || []).map((person) => <option key={person.slug}>{person.name}</option>)}</select><select aria-label="סינון לפי החלטה" value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}><option value="">כל ההחלטות</option><option value="recognized">זוהה</option><option value="ambiguous">דורש אישור</option><option value="unknown">לא מוכר</option><option value="spoof_suspected">חשד לזיוף</option><option value="liveness_unconfirmed">חיוּת לא אומתה</option><option value="no_face">ללא פנים</option></select><input type="datetime-local" aria-label="מתאריך" title="מתאריך" value={filters.date_from} onChange={(e) => setFilters({ ...filters, date_from: e.target.value })} /><input type="datetime-local" aria-label="עד תאריך" title="עד תאריך" value={filters.date_to} onChange={(e) => setFilters({ ...filters, date_to: e.target.value })} /><button className="button primary"><Search />חיפוש</button></form></Panel>
    {events.loading ? <Loading /> : events.error ? <ErrorState error={events.error} retry={events.reload} /> : <Panel><div className="panel-heading"><div><span className="eyebrow">תוצאות</span><h2>{events.data?.total ?? events.data?.events?.length ?? 0} אירועים</h2></div></div><div className="events-table"><div className="events-head"><span>תמונה</span><span>זמן</span><span>מצלמה</span><span>החלטה</span><span>אדם</span><span>רמת התאמה</span></div>{(events.data?.events || []).map((event) => { const info = decision[event.status] || [event.status, "neutral"]; return <button className={`event-row ${isBlacklistedEvent(event, users.data?.users || []) ? "blacklist-outline" : ""}`} onClick={() => setEventId(event.event_id)} key={event.event_id}><EventThumb event={event} /><span data-label="זמן">{dateTime(event.start_ts || event.updated_ts)}</span><span data-label="מצלמה">{event.camera || "—"}</span><span data-label="החלטה"><Badge tone={info[1]}>{info[0]}</Badge></span><span data-label="אדם"><b>{event.person || event.probable_person || "—"}</b></span><span data-label="רמת התאמה">{percent(event.score)}<small>{event.confirmations || 0} תמונות</small></span></button>; })}</div>{!events.data?.events?.length && <Empty icon={Search} title="לא נמצאו אירועים" text="נסה טווח תאריכים רחב יותר או נקה חלק מהמסננים." />}</Panel>}
    {eventId && <EventModal eventId={eventId} people={users.data?.users || []} onClose={() => setEventId(null)} onChanged={events.reload} />}
  </section>;
}

export function ReviewPage() {
  const unknowns = useResource("unknowns");
  const policy = useResource("unknowns/policy");
  const users = useResource("users");
  const [selected, setSelected] = useState({});
  const [person, setPerson] = useState("");
  const [newName, setNewName] = useState("");
  const [learningResult, setLearningResult] = useState(null);
  const [acting, setActing] = useState(false);
  const [faceSource, setFaceSource] = useState(null);
  const toast = useToast();
  const picked = Object.keys(selected).filter((id) => selected[id]);
  const act = async (action) => {
    if (!picked.length) return toast("בחר לפחות תמונה אחת", "info");
    const target = person === "__new__" ? newName.trim() : person;
    if (["resolve", "learn"].includes(action) && !target) return toast("בחר אדם או הזן שם חדש", "info");
    setActing(true);
    try {
      const endpoint = action === "learn" ? "unknowns/assign" : action === "resolve" ? "unknowns/resolve" : action === "ignore" ? "unknowns/ignore" : "unknowns/discard";
      const result = await api(endpoint, { method: "POST", body: JSON.stringify({ ids: picked, person: target }) });
      if (action === "learn") {
        setLearningResult(result);
        toast(`נוספו ${result.gallery_photos_added || 0} תמונות ייחוס · ${result.duplicates || 0} כפילויות · ${result.skipped || 0} דולגו`, result.gallery_photos_added ? "success" : "info");
        setSelected(Object.fromEntries((result.results || []).filter((item) => item.status === "skipped").map((item) => [item.id, true])));
        if (result.slug) { setPerson(result.slug); setNewName(""); }
        users.reload();
      } else {
        setLearningResult(null);
        toast(action === "resolve" ? "האירועים שויכו בלבד, ללא הוספת תמונות ייחוס" : action === "ignore" ? "הפנים הועברו להשתקה" : "התמונות נמחקו מהתור");
        setSelected({});
      }
      unknowns.reload();
    } catch (error) { toast(error.message, "error"); }
    finally { setActing(false); }
  };
  const maintain = async () => { try { const result = await api("unknowns/maintenance", { method: "POST" }); toast(`${result.removed || result.pruned || 0} תמונות מיותרות נוקו`); unknowns.reload(); } catch (error) { toast(error.message, "error"); } };
  const autoAssign = async () => { if (!confirm("לאשר אוטומטית רק תמונות שעוברות את סף הזיהוי הנוכחי?")) return; try { const result = await api("unknowns/auto_assign", { method: "POST" }); toast(`${result.total || 0} אירועים אושרו אוטומטית`); unknowns.reload(); } catch (error) { toast(error.message, "error"); } };
  const clusters = unknowns.data || [];
  const learningReasons = { learned: "נוספה לתמונות הייחוס", duplicate: "כפילות — כבר קיימת תמונת ייחוס דומה", too_small: "הפנים קטנות מדי (נדרשים 60 פיקסלים)", blurred: "התמונה מטושטשת", poor_illumination: "התאורה חלשה או חזקה מדי", invalid_embedding: "נתוני הפנים אינם תקינים", unreadable_image: "לא ניתן לקרוא את התמונה", missing: "התמונה כבר טופלה", invalid_id: "מזהה תמונה אינו תקין" };
  return <section className="review-workspace">
    <PageHeader eyebrow="תור עבודה" title="בדיקת זיהויים" description={`עד ${policy.data?.max_total ?? "מספר מוגבל של"} תמונות בתור. בחר תמונות של אותו אדם והחלט איך לטפל בהן.`} />
    <div className="review-action-guide"><span><b>שייך בלבד</b> אישור זהות האירוע בהיסטוריה.</span><span><b>שייך ולמד</b> הוספת תמונות ייחוס מתאימות לזיהוי.</span></div>
    <Panel className="review-toolbar"><div><b>{clusters.reduce((sum, cluster) => sum + cluster.length, 0)} תמונות · {clusters.length} קבוצות</b><span>{picked.length ? `${picked.length} נבחרו` : "בחר תמונות של אותו אדם ובצע פעולה אחת"}</span></div><div><select aria-label="אדם לשיוך" value={person} onChange={(e) => setPerson(e.target.value)}><option value="">בחר אדם קיים…</option><option value="__new__">משתמש חדש…</option>{(users.data?.users || []).map((item) => <option key={item.slug} value={item.slug}>{item.name}</option>)}</select>{person === "__new__" && <input aria-label="שם המשתמש החדש" placeholder="שם המשתמש החדש" maxLength={100} value={newName} onChange={(e) => setNewName(e.target.value)} />}<button className="button secondary" disabled={acting || !picked.length || !person || person === "__new__"} onClick={() => act("resolve")}><Check />שייך בלבד</button><button className="button success" disabled={acting || !picked.length || !(person === "__new__" ? newName.trim() : person)} onClick={() => act("learn")}><Sparkles />{acting ? "מעבד…" : "שייך ולמד"}</button><button className="button secondary" disabled={acting || !picked.length} onClick={() => act("ignore")}>השתק פנים אלה</button><button className="button danger-ghost" disabled={acting || !picked.length} onClick={() => act("discard")}><Trash2 />מחק מהתור</button></div><details className="maintenance-actions"><summary>פעולות תחזוקה</summary><button className="button secondary" disabled={acting} onClick={maintain}>נקה כפילויות וישנות</button><button className="button secondary" disabled={acting} onClick={autoAssign}>אשר התאמות בטוחות</button></details></Panel>
    {learningResult && <Panel><h3>נוספו {learningResult.gallery_photos_added || 0} תמונות ייחוס · {learningResult.duplicates || 0} כפילויות · {learningResult.skipped || 0} דולגו</h3><p>{learningResult.ready ? `מוכן לזיהוי ראשוני עם ${learningResult.reference_count} תמונות ייחוס. תמונות נוספות מזוויות ותאורה שונות יכולות לשפר את הזיהוי.` : "טרם נוספה תמונת ייחוס מתאימה. בחר צילום ברור יותר כדי להתחיל לזהות את האדם."}{learningResult.skipped > 0 && " התמונות שדולגו נשארו בתור לבדיקה."}</p><details><summary>תוצאה לכל תמונה</summary>{(learningResult.results || []).map((item, index) => <p key={item.id}>{index + 1}. {learningReasons[item.reason] || item.reason}</p>)}</details></Panel>}
    {unknowns.loading ? <Loading /> : unknowns.error ? <ErrorState error={unknowns.error} retry={unknowns.reload} /> : <div className="cluster-list">{clusters.map((cluster, index) => <Panel className="cluster" key={cluster[0]?.id || index}><div className="panel-heading"><div><span className="eyebrow">קבוצה {index + 1}</span><h2>{cluster[0]?.guess_name ? `אולי ${cluster[0].guess_name}` : "אדם לא מוכר"}</h2></div><button className="text-button" onClick={() => setSelected((current) => ({ ...current, ...Object.fromEntries(cluster.map((item) => [item.id, true])) }))}>בחר את כל הקבוצה</button></div><div className="unknown-grid">{cluster.map((item) => <label className={selected[item.id] ? "selected" : ""} key={item.id}><input type="checkbox" checked={Boolean(selected[item.id])} onChange={(e) => setSelected({ ...selected, [item.id]: e.target.checked })} /><img src={assetUrl(`media/unknowns/${item.id}.jpg`)} alt="פנים לבדיקה" /><span>{item.camera || "—"}<small>{dateTime(item.ts || item.created_at)}</small></span><i><Check /></i><button type="button" className="unknown-find" onClick={(e) => { e.preventDefault(); setFaceSource({ unknown_id: item.id }); }}><Search />חפש הופעות</button></label>)}</div></Panel>)}</div>}
    {faceSource && <Modal title="הופעות של פנים לא מזוהות" onClose={() => setFaceSource(null)} wide><FaceExplorer source={faceSource} /></Modal>}
    {!unknowns.loading && !clusters.length && <Empty icon={Check} title="הכול טופל" text="אין כרגע אירועים שמחכים לאישור שלך." />}
  </section>;
}

export function PeoplePage({ navigate }) {
  const users = useResource("users");
  const [editor, setEditor] = useState(null);
  const [profile, setProfile] = useState(null);
  const [view, setView] = useState("grid");
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
  return <section className="people-workspace">
    <PageHeader eyebrow="ספרייה" title="אנשים" description="מיקום אחרון ואירועים מכל המצלמות." actions={<><button className="button secondary" onClick={() => navigate("faces")}><Search />חיפוש בתמונה</button><button className="button primary" onClick={create}><UserPlus />הוסף אדם</button></>} />
    <div className="people-index-summary"><b>{users.data?.users?.length || 0} אנשים</b><div className="segmented" aria-label="תצוגת אנשים"><button aria-label="תצוגת רשת" aria-pressed={view === "grid"} className={view === "grid" ? "active" : ""} onClick={() => setView("grid")}><LayoutGrid />רשת</button><button aria-label="תצוגת רשימה" aria-pressed={view === "list"} className={view === "list" ? "active" : ""} onClick={() => setView("list")}><List />רשימה</button></div></div>
    <div className={`people-management-grid people-view-${view}`}>{(users.data?.users || []).map((person) => <Panel className={`managed-person ${person.watchlist === "black" ? "blacklist-outline" : ""}`} key={person.slug}><button className="person-main" onClick={() => open(person)}><PersonAvatar person={person} className="large" /><div><h2>{person.name}{person.favorite && <Star className="favorite" />}</h2><Badge tone={person.state === "ready" ? "success" : person.state === "review" ? "warning" : "info"}>{person.state_label}</Badge>{person.watchlist === "black" && <Badge tone="danger">רשימה שחורה</Badge>}{person.watchlist === "white" && <Badge tone="neutral">רשימה לבנה</Badge>}<p><MapPin />{person.statistics?.last_camera ? `${person.statistics.last_camera} · ${relativeTime(person.statistics.last_seen)}` : "לא נראה עדיין"}</p></div></button><div className="person-mini-stats"><span><b>{person.statistics?.today || 0}</b>אירועים היום</span><span className="person-reference-count"><b>{person.count}</b>תמונות ייחוס</span></div><div className="card-actions"><button className="button secondary" onClick={() => open(person)}>אירועים</button><ActionsMenu label={`אפשרויות עבור ${person.name}`}><button onClick={() => setEditor(person)}>עריכת אדם</button><button onClick={() => favorite(person)}><Star className={person.favorite ? "favorite" : ""} />{person.favorite ? "הסר ממועדפים" : "הוסף למועדפים"}</button></ActionsMenu></div></Panel>)}</div>
    {!users.data?.users?.length && <Empty icon={Users} title="הגלריה עדיין ריקה" text="הוסף אדם ראשון. האשף יסביר אילו תמונות טובות לזיהוי." action={<button className="button primary" onClick={create}><Plus />התחלה</button>} />}
    {editor && <PersonEditor person={editor} onClose={() => setEditor(null)} onSaved={() => { setEditor(null); users.reload(); }} />}
    {profile && <PersonProfile profile={profile} onClose={() => setProfile(null)} onChanged={users.reload} onEdit={() => { setEditor(profile.person); setProfile(null); }} />}
  </section>;
}

export function PersonEditor({ person, onClose, onSaved }) {
  const [name, setName] = useState(person.name || "");
  const [watchlist, setWatchlist] = useState(person.watchlist || "normal");
  const [notifyOnRecognition, setNotifyOnRecognition] = useState(Boolean(person.notify_on_recognition));
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
      await api(`persons/${slug}/policy`, { method: "PATCH", body: JSON.stringify({ watchlist, notify_on_recognition: notifyOnRecognition }) });
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
  return <Modal title={person.slug ? `עריכת ${person.name}` : "הוספת אדם"} subtitle="השתמש בתמונות חדות: פנים מול המצלמה, מעט לצדדים ובתאורה שונה" onClose={onClose}>
    {!person.slug && <button className="button secondary full" onClick={() => { onClose(); location.hash = "/review"; }}><Camera />בחר פנים מתמונות המצלמות</button>}
    {pendingChoice ? <div className="face-choice"><div className="safety-banner"><UserRound /><div><b>נמצאו כמה אנשים בתמונה</b><p>בחר במפורש את הפנים ששייכות ל־{name}. שום פנים לא יישמרו לפני הבחירה.</p></div></div><div className="face-choice-image"><img src={pendingChoice.preview} alt="בחירת פנים" />{(pendingChoice.candidates || []).map((candidate, index) => { const [left, top, right, bottom] = candidate.bbox; return <button key={index} style={{ left: `${left * 100}%`, top: `${top * 100}%`, width: `${(right - left) * 100}%`, height: `${(bottom - top) * 100}%` }} onClick={() => chooseFace(index)}><span>בחר פנים {index + 1}</span></button>; })}</div><button className="button secondary" onClick={() => setPendingChoice(null)}>חזרה לבחירת תמונות</button></div> : <form className="editor-form" onSubmit={save}><label>שם מלא<input autoFocus required value={name} onChange={(e) => setName(e.target.value)} placeholder="לדוגמה: רונן כהן" /></label><label>רשימת מעקב<select value={watchlist} onChange={event => setWatchlist(event.target.value)}><option value="normal">רגילה</option><option value="white">רשימה לבנה</option><option value="black">רשימה שחורה — התראה וסימון אדום</option></select><small>סימון והתראות בלבד; הרשאות כניסה מנוהלות בנפרד.</small></label><label className="face-check"><input type="checkbox" checked={notifyOnRecognition} onChange={event => setNotifyOnRecognition(event.target.checked)} />התראה בכל הופעת זיהוי של אדם זה</label><label className="upload-zone"><Upload /><b>הוסף 5–10 תמונות טובות</b><span>JPG או PNG · אפשר לבחור כמה תמונות יחד</span><input type="file" accept="image/*" multiple onChange={(e) => setFiles([...e.target.files])} /></label>{files.length > 0 && <div className="selected-files"><Check />נבחרו {files.length} תמונות</div>}<div className="photo-guidance"><div><Check /><span><b>כן</b>פנים גדולות, חדות ומוארות</span></div><div><X /><span><b>לא</b>קבוצה, משקפי שמש או פנים קטנות</span></div></div><div className="form-actions">{person.slug && <button type="button" className="button danger-ghost" onClick={remove}><Trash2 />מחיקת אדם</button>}<span /><button type="button" className="button secondary" onClick={onClose}>ביטול</button><button className="button primary" disabled={saving}>{saving ? "שומר…" : "שמירה"}</button></div></form>}
  </Modal>;
}

function PersonProfile({ profile, onClose, onChanged, onEdit }) {
  const [tab, setTab] = useState("events");
  const [refreshed, setRefreshed] = useState(null);
  const person = profile.person;
  const refreshReferences = () => {
    api(`persons/${person.slug}/profile?timezone=${encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone)}`).then(setRefreshed).catch(() => {});
    onChanged?.();
  };
  if (profile.loading) return <Modal title={person.name} onClose={onClose}><Loading /></Modal>;
  const data = refreshed || profile.data || {};
  const stats = person.statistics || {};
  return <Modal title={person.name} subtitle="אירועים מכל המצלמות" onClose={onClose} wide>
    <div className="profile-overview"><div className="profile-hero profile-hero-compact"><PersonAvatar person={person} /><div><b><MapPin />{stats.last_camera || "לא נראה עדיין"}</b><p>{relativeTime(stats.last_seen)}</p></div><ActionsMenu label={`אפשרויות עבור ${person.name}`}><button onClick={onEdit}>עריכת אדם</button></ActionsMenu></div>
    <div className="segmented profile-tabs" role="tablist" aria-label="פרטי אדם">
      <button id="person-events-tab" role="tab" aria-selected={tab === "events"} aria-controls="person-events-panel" className={tab === "events" ? "active" : ""} onClick={() => setTab("events")}><Film />אירועים</button>
      <button id="person-references-tab" role="tab" aria-selected={tab === "references"} aria-controls="person-references-panel" className={tab === "references" ? "active" : ""} onClick={() => setTab("references")}><Images />תמונות ייחוס</button>
    </div>
    </div>
    <div id="person-events-panel" role="tabpanel" aria-labelledby="person-events-tab" hidden={tab !== "events"}><FaceExplorer compact source={{ person: person.slug }} title={`האירועים של ${person.name}`} onReferencesChanged={refreshReferences} /></div>
    {tab === "references" && <div id="person-references-panel" role="tabpanel" aria-labelledby="person-references-tab"><Panel><div className="panel-heading"><h2>{data.gallery?.photos || person.count || 0} תמונות ייחוס</h2><button className="button secondary" onClick={onEdit}><Plus />הוספת תמונות ועריכה</button></div><div className="gallery-strip">{(data.gallery?.images || []).map(url => <div key={url}><img src={assetUrl(url)} alt={person.name} /></div>)}</div><p>{data.gallery?.recommendation}</p></Panel></div>}
  </Modal>;
}
