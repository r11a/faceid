import React, { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, Archive, BookOpenCheck, Brain, Check, ClipboardCheck, CloudDownload,
  Database, Download, FileClock, FileText, Film, FolderSync, HardDrive, Images, Map, Play, RefreshCw,
  RotateCcw, Save, Search, Shield, SlidersHorizontal, Sparkles, Trash2, Upload, UserRound, UserX, Wrench,
} from "lucide-react";
import { api, assetUrl } from "../api.js";
import { useResource } from "../hooks.js";
import { Badge, Empty, ErrorState, Loading, Metric, PageHeader, Panel, Segmented, dateTime, percent, useToast } from "../ui.jsx";

export function CalibrationPage() {
  const report = useResource("calibration");
  const toast = useToast();
  const apply = async () => {
    const recommended = report.data?.recommended || {};
    try { await api("settings", { method: "POST", body: JSON.stringify({ match_threshold: recommended.threshold, match_margin: recommended.margin }) }); toast("הכיול המומלץ הוחל"); report.reload(); }
    catch (error) { toast(error.message, "error"); }
  };
  if (report.loading) return <Loading />;
  if (report.error) return <ErrorState error={report.error} retry={report.reload} />;
  const data = report.data || {};
  const current = data.current || {};
  const recommended = data.recommended || {};
  const count = data.labeled_events ?? data.samples ?? 0;
  const ready = Boolean(data.ready);
  return <>
    <PageHeader eyebrow="דיוק מבוסס ראיות" title={ready ? "יש מספיק מידע להמלצה" : "נדרשים עוד אימותים"} description="כיול טוב אינו “מלמד פנים”. הוא בוחר סף שמאזן בין זיהוי נכון לזיהוי שווא, לפי אירועים שלמים שאישרת בעצמך." />
    <div className="calibration-status"><div className={`calibration-ring ${ready ? "ready" : ""}`} style={{ "--progress": `${Math.min(100, count / 20 * 100)}%` }}><strong>{count}</strong><span>מתוך 20</span></div><div><h2>{ready ? "אפשר לסמוך על ההשוואה" : `אמת עוד ${Math.max(0, 20 - count)} אירועים עצמאיים`}</h2><p>פריימים מאותו אירוע לעולם אינם נספרים כדוגמאות נפרדות. כך נמנעת תחושת דיוק מנופחת.</p></div></div>
    <div className="metrics-grid"><Metric label="זיהוי נכון" value={percent(current.tar)} note="True Accept Rate" /><Metric label="זיהוי שווא" value={percent(current.far)} note="המטרה: נמוך ככל האפשר" tone={current.far > .01 ? "red" : "green"} /><Metric label="דחייה שגויה" value={percent(current.frr)} note="אדם מוכר שנשלח לבדיקה" tone="amber" /><Metric label="זהות שגויה" value={percent(current.wrong_id)} note="הטעות החמורה ביותר" tone={current.wrong_id > 0 ? "red" : "green"} /></div>
    <Panel className="recommendation-card"><span className="recommendation-icon"><Sparkles /></span><div><span className="eyebrow">המלצת המערכת</span><h2>סף זיהוי {Number(recommended.threshold ?? .8).toFixed(2)} · פער {Number(recommended.margin ?? .2).toFixed(2)}</h2><p>{ready ? "הערכים חושבו מהאירועים שאימתת. מומלץ להחיל ולבחון שבוע נוסף." : "זו עדיין המלצה שמרנית ראשונית. אל תחיל אותה לפני שיש לפחות 20 אירועים מגוונים."}</p></div><button className="button primary" disabled={!ready} onClick={apply}><Check />החלת ההמלצה</button></Panel>
  </>;
}

export function LearningPage() {
  const backfill = useResource("backfill", { poll: 4000 });
  const coach = useResource("gallery-coach");
  const [days, setDays] = useState(14);
  const toast = useToast();
  const start = async () => { try { await api("backfill", { method: "POST", body: JSON.stringify({ days: Number(days) }) }); toast("סריקת ההיסטוריה התחילה"); backfill.reload(); } catch (error) { toast(error.message, "error"); } };
  const cancel = async () => { try { await api("backfill/cancel", { method: "POST" }); toast("הסריקה תיעצר בנקודה בטוחה"); backfill.reload(); } catch (error) { toast(error.message, "error"); } };
  const setAside = async (slug, file) => { try { await api("gallery-coach/set-aside", { method: "POST", body: JSON.stringify({ slug, file, reason: "נבדקה והועברה הצידה במרכז הלמידה" }) }); toast("התמונה הועברה הצידה וניתנת לשחזור"); coach.reload(); } catch (error) { toast(error.message, "error"); } };
  if (backfill.loading || coach.loading) return <Loading />;
  if (backfill.error || coach.error) return <ErrorState error={backfill.error || coach.error} retry={() => { backfill.reload(); coach.reload(); }} />;
  return <>
    <PageHeader eyebrow="למידה מבוקרת" title="שיפור חומר הלימוד, לא שינוי זהות" description="המערכת מחפשת תמונות חלשות, סורקת היסטוריה ומציעה מועמדות — שום תמונה אינה משויכת לאדם אוטומטית." />
    <div className="safety-banner"><Brain /><div><b>מה המטרה?</b><p>לשמור לכל אדם מעט תמונות טובות ומגוונות. יותר תמונות דומות לא משפרות דיוק ולעיתים אף פוגעות בו.</p></div></div>
    <Panel><div className="panel-heading"><div><span className="eyebrow">שלב 1</span><h2>איכות הגלריה</h2></div><Badge tone={(coach.data?.summary?.review || 0) ? "warning" : "success"}>{coach.data?.summary?.review || 0} לבדיקה</Badge></div><div className="coach-list">{(coach.data?.people || []).map((person) => <details key={person.slug}><summary><b>{person.person}</b><span>{person.images?.length || 0} תמונות</span><Badge tone={person.review_count ? "warning" : "success"}>{person.review_count ? `${person.review_count} מומלצות לבדיקה` : "כיסוי תקין"}</Badge></summary><p>{person.advice?.join(" · ")}</p><div className="coach-images">{(person.images || []).filter((image) => image.reasons?.length).slice(0, 16).map((image) => <div key={image.file}><img src={assetUrl(image.url)} alt="תמונת ייחוס" /><span><b>{percent(image.score)}</b><small>{image.reasons.join(" · ")}</small></span><button className="button secondary" onClick={() => setAside(person.slug, image.file)}>העבר הצידה</button></div>)}</div></details>)}</div></Panel>
    <Panel><div className="panel-heading"><div><span className="eyebrow">שלב 2</span><h2>סריקה מוגבלת של היסטוריית Frigate</h2></div></div><p>הסריקה מציעה תמונות לתור הבדיקה בלבד. היא אינה מעמיסה אותן ישירות על גלריית האנשים.</p>{backfill.data?.running ? <div className="job-progress"><div><span style={{ width: `${backfill.data.total ? backfill.data.processed / backfill.data.total * 100 : 3}%` }} /></div><b>{backfill.data.processed || 0} מתוך {backfill.data.total || "…"}</b><button className="button danger-ghost" onClick={cancel}>עצירה בטוחה</button></div> : <div className="inline-form"><label>כמה ימים לסרוק?<input type="number" min="1" max="60" value={days} onChange={(e) => setDays(e.target.value)} /></label><button className="button primary" onClick={start}><Play />התחלת סריקה</button></div>}</Panel>
  </>;
}

export function SettingsPage() {
  const settings = useResource("settings");
  const [form, setForm] = useState(null);
  const [advanced, setAdvanced] = useState(false);
  const [restoreFile, setRestoreFile] = useState(null);
  const toast = useToast();
  useEffect(() => { if (settings.data) setForm({ ...settings.data.thresholds, max_faces_per_person: settings.data.max_faces_per_person, trimmed_keep: settings.data.trimmed_keep, match_top_k: settings.data.match_top_k, min_confirmations: settings.data.min_confirmations, hires_enroll: settings.data.hires_enroll, backup_enabled: settings.data.backup?.enabled, backup_hour: settings.data.backup?.hour, backup_keep: settings.data.backup?.keep, backup_dir: settings.data.backup?.dir }); }, [settings.data]);
  const save = async () => { try { const result = await api("settings", { method: "POST", body: JSON.stringify(form) }); toast(result.trimmed ? `ההגדרות נשמרו ו־${result.trimmed} תמונות הועברו הצידה` : "ההגדרות נשמרו"); settings.reload(); } catch (error) { toast(error.message, "error"); } };
  const backupNow = async () => { try { const result = await api("backup/now", { method: "POST" }); toast(`הגיבוי נשמר: ${String(result.file).split(/[\\/]/).pop()}`); } catch (error) { toast(error.message, "error"); } };
  const restore = async (merge) => {
    if (!restoreFile) return toast("בחר קובץ גיבוי מסוג tar.gz", "info");
    if (!merge && !confirm("שחזור מלא יחליף את האנשים, המושתקים וחומרי הלימוד הקיימים. גיבוי בטיחות ייווצר אוטומטית. להמשיך?")) return;
    try { const body = new FormData(); body.append("file", restoreFile); await api(`restore?merge=${merge}`, { method: "POST", body }); toast(merge ? "הגיבוי מוזג בהצלחה" : "הגיבוי שוחזר בהצלחה — מומלץ להפעיל מחדש את התוסף"); settings.reload(); }
    catch (error) { toast(error.message, "error"); }
  };
  if (settings.loading) return <Loading />;
  if (settings.error) return <ErrorState error={settings.error} retry={settings.reload} />;
  if (!form) return <Loading />;
  const slider = (key, label, hint) => { const range = settings.data.ranges[key]; return <label className="advanced-slider" key={key}><span><b>{label}</b><small>{hint}</small></span><input type="range" min={range[0]} max={range[1]} step="0.01" value={form[key]} onChange={(e) => setForm({ ...form, [key]: Number(e.target.value) })} /><strong>{Number(form[key]).toFixed(2)}</strong></label>; };
  return <>
    <PageHeader eyebrow="הגדרות" title="הגדרות בטוחות וברורות" description="אין צורך לשנות מספרים כדי להתחיל. אחרי לפחות 20 אימותים, מסך הכיול יציע ערכים המבוססים על הנתונים שלך." actions={<button className="button primary" onClick={save}><Save />שמירת כל השינויים</button>} />
    <div className="settings-layout"><Panel><div className="panel-heading"><div><span className="eyebrow">התנהגות כללית</span><h2>ברירות מחדל</h2></div></div><div className="setting-row"><div><b>תמונות חדות מההקלטה</b><span>משפר תמונות ייחוס חדשות בלי להשפיע על מהירות הזיהוי החי</span></div><button className={`switch ${form.hires_enroll ? "on" : ""}`} onClick={() => setForm({ ...form, hires_enroll: !form.hires_enroll })}><i /></button></div><label className="simple-setting"><span><b>מספר תמונות מרבי לאדם</b><small>תמונות דומות יועברו הצידה אוטומטית</small></span><input type="number" min="5" max="100" value={form.max_faces_per_person} onChange={(e) => setForm({ ...form, max_faces_per_person: Number(e.target.value) })} /></label><label className="simple-setting"><span><b>מספר תמונות מאשרות לאירוע</b><small>2 היא ברירת מחדל בטוחה</small></span><input type="number" min="1" max="6" value={form.min_confirmations} onChange={(e) => setForm({ ...form, min_confirmations: Number(e.target.value) })} /></label></Panel><Panel><div className="panel-heading"><div><span className="eyebrow">גיבוי</span><h2>שמירה ושחזור</h2></div></div><div className="setting-row"><div><b>גיבוי יומי אוטומטי</b><span>נשמר בתוך תיקיית הנתונים ושורד עדכון תוסף</span></div><button className={`switch ${form.backup_enabled ? "on" : ""}`} onClick={() => setForm({ ...form, backup_enabled: !form.backup_enabled })}><i /></button></div><div className="settings-columns"><label>שעת גיבוי<input type="number" min="0" max="23" value={form.backup_hour} onChange={(e) => setForm({ ...form, backup_hour: Number(e.target.value) })} /></label><label>כמה גיבויים לשמור<input type="number" min="1" max="90" value={form.backup_keep} onChange={(e) => setForm({ ...form, backup_keep: Number(e.target.value) })} /></label></div><div className="button-row"><a className="button secondary" href={assetUrl("api/backup")}><Download />הורדת גיבוי</a><button className="button secondary" onClick={backupNow}><Archive />גיבוי עכשיו</button></div><div className="restore-box"><label>שחזור מקובץ<input type="file" accept=".gz,.tgz,application/gzip" onChange={(e) => setRestoreFile(e.target.files?.[0] || null)} /></label><div className="button-row"><button className="button secondary" disabled={!restoreFile} onClick={() => restore(true)}><Upload />מיזוג בטוח</button><button className="button danger-ghost" disabled={!restoreFile} onClick={() => restore(false)}><RotateCcw />החלפה מלאה</button></div><small>מיזוג מוסיף חומר חסר. החלפה מלאה מחליפה את הגלריה ויוצרת קודם גיבוי בטיחות.</small></div></Panel></div>
    <button className="advanced-disclosure" onClick={() => setAdvanced(!advanced)}><Wrench /><span><b>הגדרות זיהוי מתקדמות</b><small>שנה רק לאחר כיול או כאשר ברור איזה סוג טעות רוצים לצמצם</small></span><Badge tone="warning">למומחים</Badge></button>{advanced && <Panel className="advanced-settings">{slider("match_threshold", "סף זיהוי", "גבוה יותר מפחית זיהויי שווא, אך שולח יותר אנשים אמיתיים לבדיקה")}{slider("unknown_threshold", "סף אדם לא מוכר", "מתחת לערך הזה הפנים מסווגות בבירור כזר")}{slider("match_margin", "פער מהמועמד השני", "המועמד הטוב חייב להוביל על הבא אחריו")}{slider("suggest_threshold", "סף הצעת שם", "מוצג כרמז בלבד, ללא זיהוי סופי")}{slider("cluster_eps", "רגישות קיבוץ", "גבוה יותר מקבץ יותר פנים יחד")}{slider("ignore_threshold", "סף השתקה", "דמיון נדרש לעוגן התעלמות")}{slider("ignore_margin", "עדיפות השתקה", "השתקה חייבת להוביל על אדם מוכר")}<label className="simple-setting"><span><b>תמונות המשוקללות בהתאמה</b><small>ממוצע של תמונות הייחוס המתאימות ביותר</small></span><input type="number" min="1" max="10" value={form.match_top_k} onChange={(e) => setForm({ ...form, match_top_k: Number(e.target.value) })} /></label><label className="simple-setting"><span><b>תמונות שהועברו הצידה לשמור</b><small>0 מוחק מיד; ערך גבוה מאפשר שחזור</small></span><input type="number" min="0" max="100" value={form.trimmed_keep} onChange={(e) => setForm({ ...form, trimmed_keep: Number(e.target.value) })} /></label></Panel>}
  </>;
}

export function AdvancedPage() {
  const [tab, setTab] = useState("body");
  const options = [["body", "זיהוי גוף", Brain], ["gallery", "תחזוקת גלריה", Images], ["ignored", "מושתקים", UserX], ["sync", "סנכרון Frigate", RefreshCw], ["routes", "מסלולים ומפה", Map], ["privacy", "פרטיות", Shield], ["logs", "לוגים", FileText]];
  return <><PageHeader eyebrow="למומחים" title="יכולות מתקדמות, בלי להעמיס על השימוש היומיומי" description="כל הכלים נשמרו. הם מרוכזים כאן לפי נושא ואינם משנים זהות ללא החלטה מפורשת שלך." /><Segmented value={tab} onChange={setTab} options={options} />{tab === "body" ? <BodyTools /> : tab === "gallery" ? <GalleryTools /> : tab === "ignored" ? <IgnoredTools /> : tab === "sync" ? <SyncTools /> : tab === "routes" ? <RouteTools /> : tab === "privacy" ? <PrivacyTools /> : <LogTools />}</>;
}

function BodyTools() {
  const body = useResource("body");
  const users = useResource("users");
  const toast = useToast();
  const review = async (id, action, person) => { try { await api(`body/material/${id}/review`, { method: "POST", body: JSON.stringify({ action, person }) }); toast("הדוגמה נבדקה"); body.reload(); } catch (error) { toast(error.message, "error"); } };
  const train = async () => { try { await api("body/train", { method: "POST" }); toast("מודל הגוף אומן מהחומר שאושר"); body.reload(); } catch (error) { toast(error.message, "error"); } };
  if (body.loading) return <Loading />;
  if (body.error) return <ErrorState error={body.error} retry={body.reload} />;
  return <div className="advanced-grid"><Panel><div className="panel-heading"><div><span className="eyebrow">מדיניות</span><h2>גוף הוא רמז — פנים קובעות זהות</h2></div><Badge tone={body.data?.status?.armed ? "success" : "neutral"}>{body.data?.status?.armed ? "מוכן" : "לא אומן"}</Badge></div><p>זיהוי גוף מסייע לקשר אירועים בין מצלמות או למצוא מועמד בתחקור. הוא לעולם אינו פותח דלת ואינו משנה שם של אדם.</p><button className="button primary" onClick={train}><Brain />אימון מהדוגמאות שאושרו</button></Panel><Panel><div className="panel-heading"><div><span className="eyebrow">חומר ממתין</span><h2>{body.data?.pending?.length || 0} דוגמאות לבדיקה</h2></div></div><div className="body-review-grid">{(body.data?.pending || []).map((item) => <BodyCard item={item} people={users.data?.users || []} onReview={review} key={item.id} />)}</div>{!body.data?.pending?.length && <Empty icon={Check} title="אין חומר ממתין" text="אפשר להוסיף אירוע לבדיקה מתוך חלון התחקור." />}</Panel></div>;
}

function BodyCard({ item, people, onReview }) { const [person, setPerson] = useState(item.suggested_person || ""); return <div className="body-card"><img src={assetUrl(item.image || `api/body/material/${item.id}/image`)} alt="דוגמת גוף" /><select value={person} onChange={(e) => setPerson(e.target.value)}><option value="">בחר אדם…</option>{people.map((p) => <option key={p.slug}>{p.name}</option>)}</select><div><button className="button success" disabled={!person} onClick={() => onReview(item.id, "approve", person)}>אישור</button><button className="button secondary" onClick={() => onReview(item.id, "reject")}>דחייה</button></div></div>; }

function GalleryTools() {
  const people = useResource("persons");
  const [threshold, setThreshold] = useState(.65);
  const [preview, setPreview] = useState(null);
  const toast = useToast();
  const dedupe = async (dryRun) => {
    try {
      const result = await api("deduplicate", { method: "POST", body: JSON.stringify({ threshold: Number(threshold), dry_run: dryRun }) });
      if (dryRun) setPreview(result); else { toast(`${result.moved || 0} תמונות כפולות הועברו הצידה`); people.reload(); setPreview(null); }
    } catch (error) { toast(error.message, "error"); }
  };
  const faceAction = async (slug, file, action) => {
    try {
      if (action === "restore") await api(`persons/${slug}/trimmed/${encodeURIComponent(file)}/restore`, { method: "POST" });
      else if (action === "delete-trimmed") await api(`persons/${slug}/trimmed/${encodeURIComponent(file)}`, { method: "DELETE" });
      else if (action === "unassign") await api(`persons/${slug}/faces/${encodeURIComponent(file)}/unassign`, { method: "POST" });
      else await api(`persons/${slug}/faces/${encodeURIComponent(file)}`, { method: "DELETE" });
      toast(action === "restore" ? "התמונה חזרה לגלריה" : action === "unassign" ? "התמונה הוחזרה לתור הבדיקה" : "התמונה נמחקה"); people.reload();
    } catch (error) { toast(error.message, "error"); }
  };
  if (people.loading) return <Loading />;
  if (people.error) return <ErrorState error={people.error} retry={people.reload} />;
  return <><Panel><div className="panel-heading"><div><span className="eyebrow">כפילויות</span><h2>שמירה על גלריה קטנה ומגוונת</h2></div></div><p>תמונות כמעט זהות מוסיפות מעט מאוד. הבדיקה מציגה תחזית ורק לאחר אישור מעבירה אותן הצידה — לא מוחקת.</p><div className="inline-form"><label>רגישות <input type="range" min=".50" max=".95" step=".01" value={threshold} onChange={(e) => setThreshold(e.target.value)} /></label><strong>{Number(threshold).toFixed(2)}</strong><button className="button secondary" onClick={() => dedupe(true)}><Search />בדיקת תחזית</button><button className="button primary" onClick={() => dedupe(false)} disabled={!preview}><Archive />העבר כפילויות הצידה</button></div>{preview && <div className="inline-alert positive"><Check />יימצאו {preview.would_remove || 0} תמונות: {preview.same_image || 0} זהות ו־{preview.similar_face || 0} פנים דומות מאוד.</div>}</Panel><div className="gallery-person-list">{Object.entries(people.data || {}).map(([slug, person]) => <Panel key={slug}><div className="panel-heading"><div><span className="eyebrow">גלריית ייחוס</span><h2>{person.name}</h2></div><Badge tone={(person.files?.length || 0) >= 5 ? "success" : "warning"}>{person.files?.length || 0} תמונות</Badge></div><div className="gallery-strip">{(person.files || []).map((file) => <div key={file}><img src={assetUrl(`media/persons/${slug}/${file}`)} alt={person.name} /><span><button title="החזרה לתור הבדיקה" onClick={() => faceAction(slug, file, "unassign")}><RotateCcw /></button><button title="מחיקה קבועה" onClick={() => faceAction(slug, file, "delete")}><Trash2 /></button></span></div>)}</div>{person.trimmed?.length > 0 && <details><summary>{person.trimmed.length} תמונות שהועברו הצידה</summary><div className="gallery-strip trimmed">{person.trimmed.map((item) => { const file = item.file || item; return <div key={file}><img src={assetUrl(`media/persons/${slug}/_trimmed/${file}`)} alt="תמונה שהועברה הצידה" /><span><button title="שחזור" onClick={() => faceAction(slug, file, "restore")}><RotateCcw /></button><button title="מחיקה" onClick={() => faceAction(slug, file, "delete-trimmed")}><Trash2 /></button></span></div>; })}</div></details>}</Panel>)}</div></>;
}

function IgnoredTools() {
  const ignored = useResource("ignored");
  const users = useResource("users");
  const [selected, setSelected] = useState({});
  const [person, setPerson] = useState("");
  const [group, setGroup] = useState("");
  const toast = useToast();
  const ids = Object.keys(selected).filter((id) => selected[id]);
  const run = async (action) => {
    if (!ids.length) return toast("בחר לפחות פנים אחת", "info");
    try {
      if (action === "assign") await api("ignored/assign", { method: "POST", body: JSON.stringify({ ids, person }) });
      else if (action === "move") await api("ignored/move", { method: "POST", body: JSON.stringify({ ids, group }) });
      else await api(`ignored/${action}`, { method: "POST", body: JSON.stringify({ ids, person: "" }) });
      toast(action === "restore" ? "הפנים חזרו לתור הבדיקה" : action === "delete" ? "העוגנים נמחקו" : action === "assign" ? "הפנים שויכו לאדם" : "הקבוצה עודכנה");
      setSelected({}); ignored.reload();
    } catch (error) { toast(error.message, "error"); }
  };
  if (ignored.loading) return <Loading />;
  if (ignored.error) return <ErrorState error={ignored.error} retry={ignored.reload} />;
  const clusters = ignored.data || [];
  return <><Panel className="review-toolbar"><div><b>{clusters.reduce((sum, cluster) => sum + cluster.length, 0)} פנים מושתקות</b><span>{ids.length ? `${ids.length} נבחרו` : "מושתקים אינם יוצרים התראות"}</span></div><div><select value={person} onChange={(e) => setPerson(e.target.value)}><option value="">שיוך לאדם…</option>{(users.data?.users || []).map((item) => <option value={item.slug} key={item.slug}>{item.name}</option>)}</select><button className="button success" disabled={!ids.length || !person} onClick={() => run("assign")}>שייך לאדם</button><button className="button secondary" disabled={!ids.length} onClick={() => run("restore")}><RotateCcw />החזר לבדיקה</button><button className="button danger-ghost" disabled={!ids.length} onClick={() => run("delete")}><Trash2 />מחק</button></div><div className="group-actions"><input value={group} onChange={(e) => setGroup(e.target.value)} placeholder="שם קבוצת השתקה" /><button className="button secondary" disabled={!ids.length || !group.trim()} onClick={() => run("move")}>העבר לקבוצה</button></div></Panel><div className="cluster-list">{clusters.map((cluster, index) => <Panel key={cluster[0]?.id || index}><div className="panel-heading"><div><span className="eyebrow">קבוצת השתקה</span><h2>{cluster[0]?.group || `קבוצה ${index + 1}`}</h2></div><button className="text-button" onClick={() => setSelected((current) => ({ ...current, ...Object.fromEntries(cluster.map((item) => [item.id, true])) }))}>בחר הכול</button></div><div className="unknown-grid">{cluster.map((item) => <label className={selected[item.id] ? "selected" : ""} key={item.id}><input type="checkbox" checked={Boolean(selected[item.id])} onChange={(e) => setSelected({ ...selected, [item.id]: e.target.checked })} /><img src={assetUrl(`media/ignored/${item.id}.jpg`)} alt="פנים מושתקות" /><span>{item.from_person || "עוגן השתקה"}</span><i><Check /></i></label>)}</div></Panel>)}</div>{!clusters.length && <Empty icon={UserX} title="אין פנים מושתקות" text="פנים שתבחר להשתיק במסך דורש טיפול יופיעו כאן." />}</>;
}

function SyncTools() {
  const sync = useResource("frigate-sync");
  const [mode, setMode] = useState("import");
  const [selected, setSelected] = useState({});
  const toast = useToast();
  const rows = mode === "import" ? (sync.data?.remote || []).filter((item) => !item.imported && !item.dismissed) : (sync.data?.local || []).filter((item) => !item.exported && !item.dismissed);
  const keyOf = (item) => mode === "import" ? `${item.person}\0${item.file}` : `${item.slug}\0${item.file}`;
  const picked = rows.filter((item) => selected[keyOf(item)]);
  const run = async (action) => {
    if (!picked.length) return toast("בחר לפחות תמונה אחת", "info");
    try {
      const result = action === "dismiss" ? await api("frigate-sync/dismiss", { method: "POST", body: JSON.stringify({ direction: mode, items: picked }) }) : await api(`frigate-sync/${mode}`, { method: "POST", body: JSON.stringify({ items: picked }) });
      const completed = result.imported ?? result.exported ?? result.dismissed ?? 0;
      toast(`${completed} תמונות טופלו${result.errors?.length ? ` · ${result.errors.length} נכשלו` : ""}`, result.errors?.length ? "info" : "success"); setSelected({}); sync.reload();
    } catch (error) { toast(error.message, "error"); }
  };
  if (sync.loading) return <Loading text="משווה ספריות…" />;
  if (sync.error) return <ErrorState error={sync.error} retry={sync.reload} />;
  const s = sync.data?.summary || {};
  return <><div className="metrics-grid"><Metric label="FaceID" value={s.local_images || 0} note={`${s.local_people || 0} אנשים`} /><Metric label="Frigate" value={s.frigate_images || 0} note={`${s.frigate_people || 0} אנשים`} tone="turquoise" /><Metric label="מועמדים לייבוא" value={s.import_candidates || 0} tone="amber" /><Metric label="מועמדים לייצוא" value={s.export_candidates || 0} tone="purple" /></div><Panel><div className="panel-heading"><div><span className="eyebrow">בחירה מפורשת בלבד</span><h2>סנכרון גלריות</h2></div><Segmented value={mode} onChange={(value) => { setMode(value); setSelected({}); }} options={[["import", "Frigate ← ייבוא", CloudDownload], ["export", "ייצוא → Frigate", Upload]]} /></div><p>הסנכרון אינו אוטומטי: בחר רק תמונות חדות ומועילות. בייבוא כל פנים נבדקות שוב לפני שמירה.</p><div className="sync-toolbar"><span>{picked.length} נבחרו</span><button className="button primary" disabled={!picked.length} onClick={() => run("transfer")}><FolderSync />{mode === "import" ? "ייבא נבחרות" : "ייצא נבחרות"}</button><button className="button secondary" disabled={!picked.length} onClick={() => run("dismiss")}>אל תציג שוב</button></div><div className="sync-grid">{rows.map((item) => { const key = keyOf(item); return <label className={selected[key] ? "selected" : ""} key={key}><input type="checkbox" checked={Boolean(selected[key])} onChange={(e) => setSelected({ ...selected, [key]: e.target.checked })} /><img src={assetUrl(item.url)} alt={item.person} loading="lazy" /><span><b>{item.person}</b><small>{item.file}</small>{item.failure && <em>{item.failure.error || "ניסיון קודם נכשל"}</em>}</span><i><Check /></i></label>; })}</div>{!rows.length && <Empty icon={Check} title="הספריות מסונכרנות" text="אין כרגע תמונות חדשות לפעולה בכיוון שנבחר." />}</Panel></>;
}

function RouteTools() {
  const visits = useResource("visits?days=30&limit=100");
  const site = useResource("site-map?days=7");
  const [playlist, setPlaylist] = useState(null);
  if (visits.loading || site.loading) return <Loading />;
  if (visits.error || site.error) return <ErrorState error={visits.error || site.error} retry={() => { visits.reload(); site.reload(); }} />;
  return <><Panel><div className="panel-heading"><div><span className="eyebrow">תנועה בין מצלמות</span><h2>ביקורים ומסלולים</h2></div></div><div className="route-list">{(visits.data?.visits || []).map((visit, index) => <button key={visit.id || index} onClick={() => setPlaylist({ visit, index: 0 })}><span><b>{visit.person || "לא זוהה"}</b><small>{dateTime(visit.start_ts)} · {visit.open ? "עדיין באתר" : "הסתיים"} · {visit.event_count || visit.timeline?.length || 0} קליפים</small></span><div className="route-bubbles">{(visit.route || []).map((camera, cameraIndex) => <Badge tone="info" key={`${camera}-${cameraIndex}`}>{camera}</Badge>)}</div><Play /></button>)}</div></Panel><SiteMapEditor resource={site} />{playlist && <RoutePlaylist playlist={playlist} setPlaylist={setPlaylist} />}</>;
}

function SiteMapEditor({ resource }) {
  const [map, setMap] = useState(resource.data?.map || { title: "האתר שלי", cameras: [], links: [] });
  const [editing, setEditing] = useState(false);
  const toast = useToast();
  useEffect(() => setMap(resource.data?.map || map), [resource.data]);
  const move = (event, camera) => {
    if (!editing) return;
    if (!camera) return;
    const surface = event.currentTarget.closest?.(".site-map-simple") || event.currentTarget;
    const box = surface.getBoundingClientRect();
    const x = Math.max(2, Math.min(98, (event.clientX - box.left) / box.width * 100));
    const y = Math.max(4, Math.min(96, (event.clientY - box.top) / box.height * 100));
    setMap({ ...map, cameras: map.cameras.map((item) => item.camera === camera ? { ...item, x, y } : item) });
  };
  const save = async () => { try { const updated = await api("site-map", { method: "POST", body: JSON.stringify({ title: map.title, cameras: map.cameras, links: map.links || [] }) }); setMap(updated); setEditing(false); toast("מפת המצלמות נשמרה"); resource.reload(); } catch (error) { toast(error.message, "error"); } };
  return <Panel><div className="panel-heading"><div><span className="eyebrow">מפת אתר משוערת</span><h2>{map.title || "מיקומי מצלמות"}</h2></div>{editing ? <div><button className="button secondary" onClick={() => { setMap(resource.data?.map); setEditing(false); }}>ביטול</button><button className="button primary" onClick={save}><Save />שמירה</button></div> : <button className="button secondary" onClick={() => setEditing(true)}><Map />עריכת מיקומים</button>}</div>{editing && <label className="map-title">שם האתר<input value={map.title || ""} onChange={(e) => setMap({ ...map, title: e.target.value })} /></label>}<div className={`site-map-simple ${editing ? "editing" : ""}`} onPointerMove={(event) => { if (event.buttons === 1) move(event, event.currentTarget.dataset.dragging); }} onPointerUp={(event) => delete event.currentTarget.dataset.dragging}>{(map.cameras || []).map((camera) => <button key={camera.camera} style={{ left: `${camera.x}%`, top: `${camera.y}%` }} onPointerDown={(event) => { if (editing) { event.currentTarget.parentElement.dataset.dragging = camera.camera; event.currentTarget.setPointerCapture?.(event.pointerId); } }} onPointerMove={(event) => editing && event.buttons === 1 && move(event, camera.camera)}><Map />{camera.label || camera.camera}</button>)}</div><p className="map-notice">{map.notice || "המיקום והמסלול משוערים לפי מצלמות וזמנים — אינם GPS."}</p></Panel>;
}

function RoutePlaylist({ playlist, setPlaylist }) {
  const timeline = playlist.visit.timeline || [];
  const current = timeline[playlist.index];
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [current?.event_id]);
  const move = (index) => setPlaylist({ ...playlist, index: Math.max(0, Math.min(timeline.length - 1, index)) });
  return <div className="route-player-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setPlaylist(null); }}><div className="route-player"><header><div><span className="eyebrow">מסלול מצולם</span><h2>{playlist.visit.person} · {dateTime(playlist.visit.start_ts)}</h2></div><button className="icon-button" onClick={() => setPlaylist(null)}>×</button></header><div className="route-timeline">{timeline.map((item, index) => <button className={index === playlist.index ? "active" : ""} onClick={() => move(index)} key={`${item.event_id}-${index}`}><span>{index + 1}</span><b>{item.camera}</b><small>{dateTime(item.start_ts)}</small></button>)}</div>{current && <div className="route-video">{failed ? <div className="frame-error"><Film /><b>הקליפ אינו זמין עוד ב־Frigate</b><span>אפשר עדיין לפתוח את תמונת האירוע מתחקור.</span></div> : <video key={current.event_id} controls autoPlay playsInline onError={() => setFailed(true)} onEnded={() => playlist.index < timeline.length - 1 && move(playlist.index + 1)} src={assetUrl(`api/activity/${current.event_id}/clip`)} />}<div className="route-controls"><button className="button secondary" disabled={playlist.index === 0} onClick={() => move(playlist.index - 1)}>הקודם</button><span>{playlist.index + 1} מתוך {timeline.length}</span><button className="button primary" disabled={playlist.index === timeline.length - 1} onClick={() => move(playlist.index + 1)}>הבא</button></div></div>}</div></div>;
}

function PrivacyTools() {
  const privacy = useResource("privacy");
  const [known, setKnown] = useState(30); const [unknown, setUnknown] = useState(14);
  const toast = useToast();
  const prune = async () => { if (!confirm("למחוק כעת תמונות ראיה ישנות לפי התקופות שנבחרו?")) return; try { const result = await api("privacy/prune", { method: "POST", body: JSON.stringify({ known_days: Number(known), unknown_days: Number(unknown) }) }); toast(`${result.removed_images} תמונות ישנות נמחקו`); privacy.reload(); } catch (error) { toast(error.message, "error"); } };
  if (privacy.loading) return <Loading />;
  if (privacy.error) return <ErrorState error={privacy.error} retry={privacy.reload} />;
  return <><div className="metrics-grid"><Metric label="אירועי ביקורת" value={privacy.data?.audit_events || 0} /><Metric label="תמונות ראיה" value={privacy.data?.evidence_images || 0} tone="turquoise" /><Metric label="נפח ראיות" value={`${Math.round((privacy.data?.evidence_bytes || 0) / 1048576)} MB`} tone="purple" /></div><Panel><div className="panel-heading"><div><span className="eyebrow">מדיניות שמירה</span><h2>מחיקת ראיות ישנות</h2></div></div><div className="settings-columns"><label>זיהויים מוכרים — ימים<input type="number" min="1" max="3650" value={known} onChange={(e) => setKnown(e.target.value)} /></label><label>אנשים לא מוכרים — ימים<input type="number" min="1" max="3650" value={unknown} onChange={(e) => setUnknown(e.target.value)} /></label></div><button className="button danger-ghost" onClick={prune}><Trash2 />מחיקה לפי המדיניות עכשיו</button></Panel></>;
}

function LogTools() {
  const [level, setLevel] = useState("");
  const logs = useResource(`logs?limit=400${level ? `&level=${level}` : ""}`);
  return <Panel><div className="panel-heading"><div><span className="eyebrow">אבחון</span><h2>לוג מערכת</h2></div><div><select value={level} onChange={(e) => setLevel(e.target.value)}><option value="">כל הרמות</option><option value="ERROR">שגיאות</option><option value="WARNING">אזהרות</option><option value="INFO">מידע</option></select><button className="button secondary" onClick={logs.reload}><RefreshCw />רענון</button></div></div>{logs.loading ? <Loading /> : logs.error ? <ErrorState error={logs.error} retry={logs.reload} /> : <pre className="log-view">{(logs.data?.lines || []).join("\n") || logs.data?.note || "אין שורות לוג"}</pre>}</Panel>;
}
