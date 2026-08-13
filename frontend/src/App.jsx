import React, { useEffect, useMemo, useState } from "react";
import {
  Activity, Bell, BookOpenCheck, Bot, Camera, ChevronDown, ChevronLeft, CircleHelp,
  ClipboardCheck, DoorOpen, Gauge, Home, Images, Menu, Moon, Search, Settings,
  ShieldCheck, Sparkles, Sun, UserPlus, Users, Wrench, X,
} from "lucide-react";
import { api } from "./api.js";
import { useResource } from "./hooks.js";
import { Badge, ToastProvider } from "./ui.jsx";
import { DashboardPage, EventsPage, PeoplePage, ReviewPage } from "./pages/Daily.jsx";
import { CamerasPage, GuestsPage, IntercomPage, LivenessPage } from "./pages/Security.jsx";
import { AutomationsPage, HealthPage } from "./pages/Management.jsx";
import { AdvancedPage, CalibrationPage, LearningPage, SettingsPage } from "./pages/Advanced.jsx";

const VERSION = "6.0.0";
const pages = {
  home: { title: "תמונת מצב", subtitle: "כל מה שחשוב עכשיו", icon: Home, component: DashboardPage },
  review: { title: "דורש טיפול", subtitle: "אישור מהיר של אירועים לא ודאיים", icon: ClipboardCheck, component: ReviewPage },
  events: { title: "אירועים ותחקור", subtitle: "חיפוש, קליפים ומסלולים", icon: Activity, component: EventsPage },
  people: { title: "אנשים", subtitle: "הטמעה, עריכה וסטטיסטיקה", icon: Users, component: PeoplePage },
  live: { title: "מצלמות", subtitle: "תמונה חיה ואיכות זיהוי", icon: Camera, component: CamerasPage },
  intercom: { title: "כניסה ואינטרקום", subtitle: "בדיקת פנים ברזולוציה גבוהה", icon: DoorOpen, component: IntercomPage },
  liveness: { title: "הגנה מזיוף", subtitle: "בדיקת אדם חי מול תמונה או מסך", icon: ShieldCheck, component: LivenessPage },
  guests: { title: "אורחים", subtitle: "גישה זמנית ומבוקרת", icon: UserPlus, component: GuestsPage },
  automations: { title: "אוטומציות", subtitle: "חיבור שימושי ל־Home Assistant", icon: Bot, component: AutomationsPage },
  health: { title: "תקינות המערכת", subtitle: "Frigate, MQTT, ביצועים ואחסון", icon: Gauge, component: HealthPage },
  learning: { title: "למידה מבוקרת", subtitle: "כלי שיפור שאינם משנים זהות לבד", icon: BookOpenCheck, component: LearningPage },
  calibration: { title: "כיול דיוק", subtitle: "המלצות המבוססות על אימותים שלך", icon: Sparkles, component: CalibrationPage },
  settings: { title: "הגדרות", subtitle: "ברירות מחדל, פרטיות וגיבוי", icon: Settings, component: SettingsPage },
  advanced: { title: "כלים מתקדמים", subtitle: "גוף, סנכרון, מפה, לוגים וכלי מנוע", icon: Wrench, component: AdvancedPage },
};

const groups = [
  ["שימוש יומיומי", ["home", "review", "events", "people"]],
  ["אבטחה וכניסות", ["live", "intercom", "liveness", "guests"]],
  ["ניהול", ["automations", "health", "settings"]],
  ["למומחים", ["learning", "calibration", "advanced"]],
];

function resolvePage() {
  const key = location.hash.replace(/^#\/?/, "").split("/")[0];
  return pages[key] ? key : "home";
}

function Shell() {
  const [page, setPage] = useState(resolvePage);
  const [drawer, setDrawer] = useState(false);
  const [expertOpen, setExpertOpen] = useState(() => localStorage.getItem("faceid-expert-open") === "true");
  const [theme, setTheme] = useState(() => localStorage.getItem("faceid-theme") || "dark");
  const [clock, setClock] = useState(new Date());
  const [search, setSearch] = useState("");
  const health = useResource("health", { poll: 15000 });
  const session = useResource("session");
  useEffect(() => {
    const onHash = () => setPage(resolvePage());
    window.addEventListener("hashchange", onHash);
    const timer = window.setInterval(() => setClock(new Date()), 1000);
    return () => { window.removeEventListener("hashchange", onHash); window.clearInterval(timer); };
  }, []);
  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem("faceid-theme", theme); }, [theme]);
  const navigate = (id) => { location.hash = `/${id}`; setPage(id); setDrawer(false); };
  const info = pages[page];
  const CurrentPage = info.component;
  const reviewCount = health.data?.queue || 0;
  const systemOk = health.data?.status === "ok";
  const searchResults = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return [];
    return Object.entries(pages).filter(([, item]) => `${item.title} ${item.subtitle}`.toLowerCase().includes(q)).slice(0, 6);
  }, [search]);
  return <div className="app-shell">
    {drawer && <button className="drawer-scrim" onClick={() => setDrawer(false)} aria-label="סגירת תפריט" />}
    <aside className={`sidebar ${drawer ? "open" : ""}`}>
      <div className="brand"><span className="brand-mark"><Images /></span><div><strong>FaceID</strong><small>מרכז זיהוי חכם</small></div><button className="drawer-close" onClick={() => setDrawer(false)}><X /></button></div>
      <nav>
        {groups.map(([label, ids], groupIndex) => {
          const advanced = groupIndex === 3;
          return <div className="nav-group" key={label}>
            {advanced ? <button className="nav-group-toggle" onClick={() => { const next = !expertOpen; setExpertOpen(next); localStorage.setItem("faceid-expert-open", String(next)); }}><span>{label}</span><ChevronDown className={expertOpen ? "rotate" : ""} /></button> : <span className="nav-label">{label}</span>}
            {(!advanced || expertOpen) && ids.map((id) => { const Icon = pages[id].icon; return <button className={`nav-item ${page === id ? "active" : ""}`} onClick={() => navigate(id)} key={id}><span className={`nav-icon tone-${id}`}><Icon /></span><span><b>{pages[id].title}</b><small>{pages[id].subtitle}</small></span>{id === "review" && reviewCount > 0 && <em>{reviewCount > 99 ? "99+" : reviewCount}</em>}<ChevronLeft /></button>; })}
          </div>;
        })}
      </nav>
      <footer><div className="operator"><span>{(session.data?.name || "HA").slice(0, 2)}</span><div><b>{session.data?.name || "Home Assistant"}</b><small>{session.data?.role === "admin" ? "מנהל מערכת" : session.data?.role || "משתמש"}</small></div></div><div className="version">FaceID v{VERSION}</div></footer>
    </aside>
    <main className="main-shell">
      <header className="topbar">
        <button className="mobile-menu" onClick={() => setDrawer(true)}><Menu /></button>
        <div className="top-title"><div className={`status-dot ${systemOk ? "online" : "offline"}`} /><div><strong>{info.title}</strong><small>{info.subtitle}</small></div></div>
        <div className="global-search"><Search /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="חיפוש מסך או פעולה…" />{searchResults.length > 0 && <div className="search-popover">{searchResults.map(([id, item]) => <button onClick={() => { navigate(id); setSearch(""); }} key={id}><item.icon /><span><b>{item.title}</b><small>{item.subtitle}</small></span></button>)}</div>}</div>
        <div className="top-actions"><div className="system-pill"><span className={systemOk ? "ok" : "bad"} />{systemOk ? "מחובר" : "דורש בדיקה"}</div><time>{clock.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" })}</time><button className="icon-button" onClick={() => navigate("review")} title="התראות"><Bell />{reviewCount > 0 && <i />}</button><button className="icon-button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title="ערכת נושא">{theme === "dark" ? <Sun /> : <Moon />}</button><button className="icon-button" onClick={() => navigate("health")} title="עזרה ותקינות"><CircleHelp /></button></div>
      </header>
      <div className="mobile-context"><Badge tone={systemOk ? "success" : "danger"}>{systemOk ? "המערכת מחוברת" : "דורש בדיקה"}</Badge><span>{clock.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" })} · v{VERSION}</span></div>
      <div className="page-content"><CurrentPage navigate={navigate} /></div>
    </main>
  </div>;
}

export default function App() { return <ToastProvider><Shell /></ToastProvider>; }
