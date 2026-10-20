import React, { Component, useEffect, useMemo, useState } from "react";
import {
  Activity, AlertTriangle, Bell, BookOpenCheck, Bot, Camera, ChevronLeft, CircleHelp,
  ClipboardCheck, DoorOpen, Gauge, Home, Images, Menu, Moon, Search, Settings,
  ShieldCheck, Sparkles, Sun, UserPlus, Users, Wrench, X,
} from "lucide-react";
import { api } from "./api.js";
import { useResource } from "./hooks.js";
import { AlertCenter } from "./AlertCenter.jsx";
import { Badge, ToastProvider } from "./ui.jsx";
import { EventsPage, PeoplePage, ReviewPage } from "./pages/Daily.jsx";
import { DashboardPage } from "./pages/Home.jsx";
import { FacesPage } from "./pages/Faces.jsx";
import { Access1Page } from "./pages/Access1.jsx";
import { CamerasPage, GuestsPage, IntercomPage, LivenessPage } from "./pages/Security.jsx";
import { AutomationsPage, HealthPage } from "./pages/Management.jsx";
import { AdvancedPage, CalibrationPage, LearningPage, SettingsPage } from "./pages/Advanced.jsx";

const VERSION = "6.3.0";
const pages = {
  home: { title: "מבט כללי", subtitle: "אירועים ומיקומים אחרונים", icon: Home, component: DashboardPage },
  review: { title: "דורש טיפול", subtitle: "אישור מהיר של אירועים לא ודאיים", icon: ClipboardCheck, component: ReviewPage },
  events: { title: "אירועים ותחקור", subtitle: "חיפוש, קליפים ומסלולים", icon: Activity, component: EventsPage },
  people: { title: "אנשים", subtitle: "תמונות והופעות של האנשים שלך", icon: Users, component: PeoplePage },
  faces: { title: "גלריה וחיפוש", subtitle: "חפש פנים וצפה בהופעות מכל המצלמות", icon: Images, component: FacesPage },
  access1: { title: "בקרת כניסה", subtitle: "משתמשים וקוראים ב־Access1", icon: DoorOpen, component: Access1Page },
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
  ["שימוש יומיומי", ["home", "faces", "people", "review", "events"]],
  ["אבטחה וכניסות", ["live", "access1", "intercom", "guests"]],
  ["ניהול", ["automations", "health", "settings"]],
  ["למומחים", ["liveness", "learning", "calibration", "advanced"]],
];

function resolvePage() {
  const key = location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
  return pages[key] ? key : "home";
}

function Shell() {
  const [page, setPage] = useState(resolvePage);
  const [drawer, setDrawer] = useState(false);
  const [theme, setTheme] = useState(() => localStorage.getItem("faceid-theme") || "dark");
  const [clock, setClock] = useState(new Date());
  const [search, setSearch] = useState("");
  const [expert, setExpert] = useState(false);
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
  return <div className={`app-shell ${page === "home" ? "shell-home" : ""}`}>
    {drawer && <button className="drawer-scrim" onClick={() => setDrawer(false)} aria-label="סגירת תפריט" />}
    <aside className={`sidebar ${drawer ? "open" : ""}`}>
      <div className="brand"><span className="brand-mark"><Images /></span><div><strong>FaceID</strong><small>מרכז זיהוי חכם</small></div><button className="drawer-close" onClick={() => setDrawer(false)}><X /></button></div>
      <nav>
        <button className="text-button expert-toggle" onClick={() => setExpert(!expert)} aria-expanded={expert}>{expert ? "הסתר כלים מתקדמים" : "כלים מתקדמים"}</button>
        {groups.filter(([label]) => expert || label !== "למומחים").map(([label, ids]) => {
          return <div className="nav-group" key={label}>
            <span className="nav-label">{label}</span>
            {ids.map((id) => { const Icon = pages[id].icon; return <button className={`nav-item ${page === id ? "active" : ""}`} onClick={() => navigate(id)} key={id}><span className={`nav-icon tone-${id}`}><Icon /></span><span><b>{pages[id].title}</b><small>{pages[id].subtitle}</small></span>{id === "review" && reviewCount > 0 && <em>{reviewCount > 99 ? "99+" : reviewCount}</em>}<ChevronLeft /></button>; })}
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
        <div className="top-actions"><div className="system-pill"><span className={systemOk ? "ok" : "bad"} />{systemOk ? "מחובר" : "דורש בדיקה"}</div><time>{clock.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" })}</time><AlertCenter /><button className="icon-button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title="ערכת נושא">{theme === "dark" ? <Sun /> : <Moon />}</button><button className="icon-button" onClick={() => navigate("health")} title="עזרה ותקינות"><CircleHelp /></button></div>
      </header>
      <div className="mobile-context"><Badge tone={systemOk ? "success" : "danger"}>{systemOk ? "המערכת מחוברת" : "דורש בדיקה"}</Badge><span>{clock.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" })} · v{VERSION}</span></div>
      <div className="page-content"><PageBoundary page={page}><CurrentPage navigate={navigate} /></PageBoundary></div>
    </main>
  </div>;
}

class PageBoundary extends Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidUpdate(previous) { if (previous.page !== this.props.page && this.state.error) this.setState({ error: null }); }
  render() {
    if (!this.state.error) return this.props.children;
    return <div className="page-crash"><AlertTriangle /><div><span className="eyebrow">המסך לא נטען</span><h1>נתקלנו בתקלה מקומית</h1><p>שאר המערכת ממשיכה לפעול. אפשר לעבור למסך אחר או לנסות לטעון מחדש.</p><details><summary>פרטים טכניים</summary><code>{this.state.error.message}</code></details><button className="button primary" onClick={() => location.reload()}>טעינה מחדש</button></div></div>;
  }
}

export default function App() { return <ToastProvider><Shell /></ToastProvider>; }
