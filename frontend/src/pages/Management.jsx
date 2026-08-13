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
  if (health.error) return <ErrorState error={health.error} retry={health.reload} />;
  const h = health.data || {};
  const r = report.data || {};
  const frigateOk = [true, "ok", "connected"].includes(r.frigate?.connected ?? r.frigate?.status);
  const mqttOk = [true, "ok", "connected"].includes(r.mqtt?.connected ?? r.mqtt?.status);
  const runtime = r.advanced?.runtime || h.runtime || {};
  const checks = [
    ["FaceID", true, `גרסה ${h.version || "—"}`],
    ["Frigate", frigateOk, r.frigate?.message || r.frigate?.url || "חיבור מאובטח"],
    ["Home Assistant / MQTT", mqttOk, r.mqtt?.message || `${r.mqtt?.entities || 0} ישויות`],
    ["מנוע זיהוי", Boolean(h.backend || runtime.provider), h.backend || runtime.provider || "לא דווח"],
  ];
  return <>
    <PageHeader eyebrow="תקינות" title={checks.every(([, ok]) => ok) ? "המערכת פועלת כשורה" : "יש רכיבים שדורשים בדיקה"} description="מסך אחד שמתרגם את המצב הטכני לתשובה ברורה ומה אפשר לעשות הלאה." actions={<button className="button secondary" onClick={() => { health.reload(); report.reload(); }}><RefreshCw />בדיקה מחדש</button>} />
    <div className="metrics-grid"><Metric icon={Gauge} label="תור בדיקה" value={h.queue || 0} note="תמונות ממתינות לאישור" tone={h.queue > 100 ? "amber" : "green"} /><Metric icon={Activity} label="בעיבוד עכשיו" value={h.processing || 0} note={`${h.open_events || 0} אירועים פתוחים`} tone="turquoise" /><Metric icon={Users} label="אנשים" value={h.persons || 0} note="גלריות פעילות" tone="purple" /><Metric icon={Database} label="עבודות רקע" value={h.pending_jobs?.length || h.pending_jobs || 0} note="פעולות ארוכות שנשמרות" tone="amber" /></div>
    <Panel className="health-list"><div className="panel-heading"><div><span className="eyebrow">שירותים</span><h2>בדיקת חיבורים</h2></div></div>{checks.map(([name, ok, note]) => <div className="health-row" key={name}><span className={ok ? "good" : "bad"}>{ok ? <CheckCircle2 /> : <AlertTriangle />}</span><div><b>{name}</b><small>{note}</small></div><Badge tone={ok ? "success" : "danger"}>{ok ? "תקין" : "דורש טיפול"}</Badge></div>)}</Panel>
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
  return <>
    <PageHeader eyebrow="Home Assistant" title="אוטומציות שימושיות, לא רק חיישנים" description="FaceID מפרסם ישויות לכל אדם ומידע עשיר על המיקום, הזמן והביטחון. כאן רואים מה אפשר לעשות איתן." />
    <Panel className="integration-status"><span className={`integration-icon ${mqtt.connected === false ? "bad" : "good"}`}><Radio /></span><div><h2>{mqtt.connected === false ? "MQTT אינו מחובר" : "Home Assistant מחובר"}</h2><p>{mqtt.connected === false ? "בדוק את פרטי MQTT בהגדרות התוסף." : `${mqtt.entities || "חיישנים"} התגלו אוטומטית. אין צורך להגדיר YAML ידנית.`}</p></div><Badge tone={mqtt.connected === false ? "danger" : "success"}>{mqtt.connected === false ? "דורש טיפול" : "פעיל"}</Badge></Panel>
    <div className="automation-grid">{automationIdeas.map((item, index) => <Panel key={item.title}><span className={`automation-icon tone-${index}`}><item.icon /></span><h2>{item.title}</h2><div className="automation-flow"><span><small>כאשר</small>{item.trigger}</span><i>←</i><span><small>אז</small>{item.action}</span></div><button className="entity-copy" onClick={() => copy(item.sensor, String(index))}><code>{item.sensor}</code>{copied === String(index) ? <Check /> : <Copy />}</button></Panel>)}</div>
    <Panel><div className="panel-heading"><div><span className="eyebrow">ישויות לפי אדם</span><h2>{users.data?.users?.length || 0} אנשים זמינים לאוטומציות</h2></div></div><div className="entity-table">{(users.data?.users || []).map((person) => <div key={person.slug}><span className="entity-avatar">{person.photo ? <img src={assetUrl(person.photo)} alt="" /> : person.name.slice(0, 2)}</span><span><b>{person.name}</b><small>מיקום אחרון, זמן, נוכחות, הופעות וציון ממוצע</small></span><code>sensor.faceid_{person.slug}_last_seen</code><button className="icon-button" onClick={() => copy(`sensor.faceid_${person.slug}_last_seen`, person.slug)}>{copied === person.slug ? <Check /> : <Copy />}</button></div>)}</div></Panel>
  </>;
}
