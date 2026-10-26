import React, { useState } from "react";
import {
  Activity, AlertTriangle, Bot, Check, CheckCircle2, Copy, Cpu, Database,
  Gauge, HardDrive, HeartPulse, Home, MemoryStick, MessageSquare, Radio,
  RefreshCw, Server, ShieldCheck, Users, Wifi,
} from "lucide-react";
import { assetUrl } from "../api.js";
import { useResource } from "../hooks.js";
import { Badge, Empty, ErrorState, Loading, Metric, PageHeader, Panel, dateTime, percent, useToast } from "../ui.jsx";

export function HealthPage() {
  const health = useResource("health", { poll: 10000 });
  const report = useResource("system-report", { poll: 20000 });
  if (health.loading || report.loading) return <Loading text="בודק את כל רכיבי המערכת…" />;
  if (health.error || report.error) return <ErrorState error={health.error || report.error} retry={() => { health.reload(); report.reload(); }} />;
  const h = health.data || {};
  const r = report.data || {};
  const frigateOk = r.frigate?.reachable === true;
  const mqttOk = r.mqtt?.connected === true;
  const runtime = r.advanced?.runtime || h.runtime || {};
  const engine = h.engine || {};
  const provider = engine.models?.recognition?.providers?.join(", ") || engine.providers?.join(", ") || runtime.provider || h.provider;
  const checks = [
    ["FaceID", true, `גרסה ${h.version || "—"}`],
    ["Frigate", frigateOk, frigateOk
      ? (r.frigate?.tls_verified ? "החיבור פעיל ואימות TLS מופעל" : "החיבור פעיל; אימות TLS כבוי — בדקו את הגדרת Secure במחבר Access1")
      : r.frigate?.error || (r.frigate?.http_status ? `Frigate HTTP ${r.frigate.http_status}` : "החיבור לא אומת")],
    ["Home Assistant / MQTT", mqttOk, mqttOk ? "מחובר לברוקר; רישום הישויות נבדק ב־Home Assistant" : r.mqtt?.connected === false ? "אין חיבור פעיל לברוקר" : "מצב החיבור לא דווח"],
    ["מנוע זיהוי", Boolean(provider || h.backend), provider || h.backend || "לא דווח"],
  ];
  return <>
    <PageHeader eyebrow="תקינות" title="תקינות המערכת" description={checks.every(([, ok]) => ok) ? "כל הרכיבים תקינים" : "יש רכיבים שדורשים בדיקה"} actions={<button className="button secondary" onClick={() => { health.reload(); report.reload(); }}><RefreshCw />בדיקה מחדש</button>} />
    <div className="metrics-grid"><Metric icon={Gauge} label="תור בדיקה" value={h.queue || 0} note="תמונות ממתינות לאישור" tone={h.queue > 100 ? "amber" : "green"} /><Metric icon={Activity} label="בעיבוד עכשיו" value={h.processing || 0} note={`${h.open_events || 0} אירועים פתוחים`} tone="turquoise" /><Metric icon={Users} label="אנשים" value={h.persons || 0} note="גלריות פעילות" tone="purple" /><Metric icon={Database} label="עבודות רקע" value={h.pending_jobs?.length || h.pending_jobs || 0} note="פעולות ארוכות שנשמרות" tone="amber" /></div>
    <Panel className="health-list"><div className="panel-heading"><div><span className="eyebrow">שירותים</span><h2>בדיקת חיבורים</h2></div></div>{checks.map(([name, ok, note]) => <div className="health-row" key={name}><span className={ok ? "good" : "bad"}>{ok ? <CheckCircle2 /> : <AlertTriangle />}</span><div><b>{name}</b><small>{note}</small></div><Badge tone={ok ? "success" : "danger"}>{ok ? "תקין" : "דורש טיפול"}</Badge></div>)}</Panel>
    <Panel><details><summary>מדידות מהירות ומנוע</summary><p>InsightFace {engine.runtime?.insightface || "—"} · {engine.model_pack || "—"} · {provider || "לא דווח"}</p><div className="metrics-grid compact"><Metric label="עיבוד תמונה — חציון" value={engine.inference?.p50_ms == null ? "—" : `${Math.round(engine.inference.p50_ms)} ms`} /><Metric label="עיבוד תמונה — P95" value={engine.inference?.p95_ms == null ? "—" : `${Math.round(engine.inference.p95_ms)} ms`} note={`${engine.inference?.samples || 0} מדידות · ${engine.inference?.failures || 0} שגיאות`} /><Metric label="המתנה בתור חי — P95" value={h.performance?.snapshot_queue_wait?.p95_ms == null ? "—" : `${Math.round(h.performance.snapshot_queue_wait.p95_ms)} ms`} /><Metric label="קליפים בתור נפרד" value={h.performance?.clip_queue ?? "—"} /></div><p>המדידות הן לעיבוד ולתורים בשרת. הן אינן כוללות השהיית צילום במצלמה או פתיחת דלת.</p></details></Panel>
    <div className="health-columns"><Panel><div className="panel-heading"><div><span className="eyebrow">ביצועים</span><h2>עומס ומשאבים</h2></div></div><div className="resource-list"><div><Cpu /><span><b>מעבד / ספק הרצה</b><small>{runtime.provider || h.provider || "CPU"}</small></span></div><div><MemoryStick /><span><b>זיכרון</b><small>{runtime.memory || "מנוהל אוטומטית"}</small></span></div><div><HardDrive /><span><b>אחסון ראיות</b><small>{r.storage?.evidence || "מדיניות שמירה מוגבלת פעילה"}</small></span></div></div></Panel><Panel><div className="panel-heading"><div><span className="eyebrow">אבחון</span><h2>המלצה נוכחית</h2></div></div>{checks.every(([, ok]) => ok) ? <Empty icon={ShieldCheck} title="אין צורך בפעולה" text="החיבורים פעילים, התורים בשליטה והמנוע מגיב." /> : <div className="guidance">{checks.filter(([, ok]) => !ok).map(([name]) => <div key={name}><AlertTriangle />בדוק את החיבור של {name} בהגדרות התוסף</div>)}</div>}</Panel></div>
  </>;
}

const automationIdeas = [
  { icon: Home, title: "מישהו מהבית הגיע", trigger: "זיהוי אדם מוכר", action: "הדלקת תאורה, עדכון מצב בית ושליחת הודעה", sensor: "binary_sensor.faceid_person_present" },
  { icon: ShieldCheck, title: "אדם לא מוכר בכניסה", trigger: "אירוע unknown במצלמת כניסה", action: "צילום התראה והפעלת תאורת חוץ", sensor: "sensor.faceid_last_unknown" },
  { icon: MessageSquare, title: "מי נראה לאחרונה", trigger: "שינוי חיישן אדם", action: "הודעה עם שם המצלמה, שעה וציון", sensor: "sensor.faceid_<person>_last_seen" },
  { icon: AlertTriangle, title: "חשד להצגת תמונה", trigger: "spoof_suspected", action: "התראה קריטית ללא פתיחת דלת", sensor: "sensor.faceid_liveness_status" },
];

export function AutomationsPage() {
  const health = useResource("health");
  const users = useResource("users");
  const [copied, setCopied] = useState("");
  const toast = useToast();
  const copy = async (value, id) => { await navigator.clipboard.writeText(value); setCopied(id); toast("שם הישות הועתק"); window.setTimeout(() => setCopied(""), 1800); };
  const mqtt = health.data?.mqtt || {};
  if (health.loading || users.loading) return <Loading />;
  if (health.error || users.error) return <ErrorState error={health.error || users.error} retry={() => { health.reload(); users.reload(); }} />;
  return <>
    <PageHeader eyebrow="Home Assistant" title="אוטומציות" description="ישויות ואירועי זיהוי לשימוש ב־Home Assistant." />
    <Panel className="integration-status"><span className={`integration-icon ${mqtt.connected !== true ? "bad" : "good"}`}><Radio /></span><div><h2>{mqtt.connected !== true ? "MQTT אינו מחובר" : "Home Assistant מחובר"}</h2><p>{mqtt.connected !== true ? "בדוק את פרטי MQTT בהגדרות התוסף." : "החיבור לברוקר פעיל. בדוק את הישויות באינטגרציית MQTT ב־Home Assistant."}</p></div><Badge tone={mqtt.connected !== true ? "danger" : "success"}>{mqtt.connected !== true ? "דורש טיפול" : "פעיל"}</Badge></Panel>
    <div className="automation-grid">{automationIdeas.map((item, index) => <Panel key={item.title}><span className={`automation-icon tone-${index}`}><item.icon /></span><h2>{item.title}</h2><div className="automation-flow"><span><small>כאשר</small>{item.trigger}</span><i>←</i><span><small>אז</small>{item.action}</span></div><button className="entity-copy" onClick={() => copy(item.sensor, String(index))}><code>{item.sensor}</code>{copied === String(index) ? <Check /> : <Copy />}</button></Panel>)}</div>
    <Panel><div className="panel-heading"><div><span className="eyebrow">ישויות לפי אדם</span><h2>{users.data?.users?.length || 0} אנשים זמינים לאוטומציות</h2></div></div><div className="entity-table">{(users.data?.users || []).map((person) => <div key={person.slug}><span className="entity-avatar">{person.photo ? <img src={assetUrl(person.photo)} alt="" /> : person.name.slice(0, 2)}</span><span><b>{person.name}</b><small>מיקום אחרון, זמן, נוכחות, הופעות וציון ממוצע</small></span><code>sensor.faceid_{person.slug}_last_seen</code><button className="icon-button" onClick={() => copy(`sensor.faceid_${person.slug}_last_seen`, person.slug)}>{copied === person.slug ? <Check /> : <Copy />}</button></div>)}</div></Panel>
  </>;
}
