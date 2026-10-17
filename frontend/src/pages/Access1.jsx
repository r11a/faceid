import React, { useEffect, useState } from "react";
import { DoorOpen, Save, ShieldCheck, Users } from "lucide-react";
import { api } from "../api.js";
import { useResource } from "../hooks.js";
import { Badge, Empty, ErrorState, Loading, PageHeader, Panel, dateTime, useToast } from "../ui.jsx";

export function Access1Page() {
  const status = useResource("access1");
  const directory = useResource("access1/directory");
  const users = useResource("users");
  const cameras = useResource("cameras/studio");
  const [people, setPeople] = useState({});
  const [readers, setReaders] = useState({});
  const [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  useEffect(() => { if (status.data) { setPeople(status.data.people || {}); setReaders(status.data.cameras || {}); setEnabled(status.data.enabled); } }, [status.data]);
  const save = async () => {
    setSaving(true);
    try { await api("access1", { method:"POST", body: JSON.stringify({ people:Object.fromEntries(Object.entries(people).filter(([,id]) => id.trim())), cameras:Object.fromEntries(Object.entries(readers).filter(([,id]) => id.trim())), enabled }) }); toast("המיפויים נשמרו"); status.reload(); }
    catch (error) { toast(error.message,"error"); }
    finally { setSaving(false); }
  };
  if (status.loading) return <Loading />;
  if (status.error) return <ErrorState error={status.error} retry={status.reload} />;
  const available = status.data.available;
  return <>
    <PageHeader eyebrow="בקרת כניסה" title="חיבור Access1" description="שייך אדם למשתמש קיים ומצלמה לקורא כניסה. ההרשאות ולוח הזמנים נשארים ב־Access1." actions={<button className="button primary" disabled={saving || users.loading || !!users.error} onClick={save}><Save />{saving ? "שומר…" : "שמירת מיפויים"}</button>} />
    <Panel><div className="panel-heading"><h2>מצב החיבור</h2><Badge tone={available ? "success" : "warning"}>{available ? "המחבר זמין" : "ממתין לפרטי API"}</Badge></div><p>{available ? "זיהוי פנים חי מועבר ל־Access1 כהצגת אמצעי זיהוי. המערכת מחזירה החלטת הרשאה ומזהה אירוע משלה." : "אפשר להכין מיפויים, אך פתיחת דלתות אינה פעילה. נדרשים פרטי המוצר, גרסת Access1 ותיעוד הממשק כדי לוודא הרשאות ורישום אירוע ביומטרי אמיתי."}</p><label className="face-check"><input type="checkbox" disabled={!available} checked={enabled} onChange={e => setEnabled(e.target.checked)} />הפעל העברת זיהויים ל־Access1 במצלמות המשויכות</label></Panel>
    <Panel><div className="panel-heading"><h2><Users />שיוך משתמשים</h2></div>{users.error ? <ErrorState error={users.error} retry={users.reload} /> : <div className="access1-mappings">{(users.data?.users || []).map(person => <label key={person.slug}><span>{person.name}</span>{directory.data?.verified ? <select value={people[person.slug] || ""} onChange={e => setPeople({ ...people, [person.slug]:e.target.value })}><option value="">ללא שיוך</option>{directory.data.users.map(user => <option key={user.id} value={user.id}>{user.name}</option>)}</select> : <input placeholder="מזהה משתמש ב־Access1 — טרם אומת" value={people[person.slug] || ""} onChange={e => setPeople({ ...people, [person.slug]:e.target.value })} />}</label>)}</div>}</Panel>
    <Panel><div className="panel-heading"><h2><DoorOpen />מצלמות וקוראים</h2></div>{cameras.error ? <ErrorState error={cameras.error} retry={cameras.reload} /> : <div className="access1-mappings">{(cameras.data?.cameras || []).map(camera => { const name = camera.camera || camera.name; return <label key={name}><span>{name}</span>{directory.data?.verified ? <select value={readers[name] || ""} onChange={e => setReaders({ ...readers, [name]:e.target.value })}><option value="">ללא שיוך</option>{directory.data.readers.map(reader => <option key={reader.id} value={reader.id}>{reader.name}</option>)}</select> : <input placeholder="מזהה קורא ב־Access1 — טרם אומת" value={readers[name] || ""} onChange={e => setReaders({ ...readers, [name]:e.target.value })} />}</label>; })}</div>}</Panel>
    <Panel><h2>אירועים שהועברו</h2>{status.data.attempts?.length ? <div className="access1-attempts">{status.data.attempts.map(attempt => <div key={attempt.request_id}><span>{dateTime(attempt.ts)}</span><b>{attempt.user_id} · {attempt.reader_id}</b><Badge tone={attempt.decision === "granted" ? "success" : "warning"}>{attempt.decision === "granted" ? "ההרשאה אושרה ב־Access1" : attempt.decision === "denied" ? "הכניסה נדחתה ב־Access1" : "לא התקבל אישור"}</Badge><small>{attempt.access1_event_id || "ללא מזהה אירוע מאומת"}</small></div>)}</div> : <Empty icon={ShieldCheck} title="אין אירועים שהועברו" text="רק זיהוי מאושר עם בדיקת חיוּת במצלמה משויכת יישלח לאחר חיבור הממשק." />}</Panel>
  </>;
}
