import React, { Component, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity, AlertTriangle, BookOpenCheck, Bot, Camera, ChevronLeft,
  ClipboardCheck, DoorOpen, Gauge, Home, Images, Moon, Search, Settings,
  ShieldCheck, Sparkles, Sun, UserPlus, Users, Wrench, X,
} from "lucide-react";
import { api } from "./api.js";
import { useResource } from "./hooks.js";
import { AlertCenter } from "./AlertCenter.jsx";
import { ToastProvider } from "./ui.jsx";
import { EventsPage, PeoplePage, ReviewPage } from "./pages/Daily.jsx";
import { DashboardPage } from "./pages/Home.jsx";
import { FacesPage } from "./pages/Faces.jsx";
import { Access1Page } from "./pages/Access1.jsx";
import { CamerasPage, GuestsPage, IntercomPage, LivenessPage } from "./pages/Security.jsx";
import { AutomationsPage, HealthPage } from "./pages/Management.jsx";
import { AdvancedPage, CalibrationPage, LearningPage, SettingsPage } from "./pages/Advanced.jsx";

const VERSION = "6.5.8";
const pages = {
  home: { title: "מבט כללי", subtitle: "אירועים ומיקומים אחרונים", icon: Home, component: DashboardPage },
  review: { title: "דורש טיפול", subtitle: "אישור מהיר של אירועים לא ודאיים", icon: ClipboardCheck, component: ReviewPage },
  events: { title: "אירועים ותחקור", subtitle: "חיפוש, קליפים ומסלולים", icon: Activity, component: EventsPage },
  people: { title: "אנשים", subtitle: "תמונות והופעות של האנשים שלך", icon: Users, component: PeoplePage },
  faces: { title: "גלריה וחיפוש", subtitle: "חפש פנים וצפה בהופעות מכל המצלמות", icon: Images, component: FacesPage },
  access1: { title: "בקרת כניסה", subtitle: "חיבור ותוצאות Access1", icon: DoorOpen, component: Access1Page },
  live: { title: "מצלמות", subtitle: "תמונה חיה ואיכות זיהוי", icon: Camera, component: CamerasPage },
  intercom: { title: "כניסה ואינטרקום", subtitle: "בדיקת פנים ברזולוציה גבוהה", icon: DoorOpen, component: IntercomPage },
  liveness: { title: "הגנה מזיוף", subtitle: "בדיקת אדם חי מול תמונה או מסך", icon: ShieldCheck, component: LivenessPage },
  guests: { title: "אורחים", subtitle: "גישה זמנית ומבוקרת", icon: UserPlus, component: GuestsPage },
  automations: { title: "אוטומציות", subtitle: "חיבור שימושי ל־Home Assistant", icon: Bot, component: AutomationsPage },
  management: { title: "ניהול", subtitle: "הגדרות, חיבורים ותקינות", icon: Settings, component: ManagementMenu },
  health: { title: "תקינות המערכת", subtitle: "Frigate, MQTT, ביצועים ואחסון", icon: Gauge, component: HealthPage },
  learning: { title: "למידה מבוקרת", subtitle: "כלי שיפור שאינם משנים זהות לבד", icon: BookOpenCheck, component: LearningPage },
  calibration: { title: "כיול דיוק", subtitle: "המלצות המבוססות על אימותים שלך", icon: Sparkles, component: CalibrationPage },
  settings: { title: "הגדרות", subtitle: "ברירות מחדל, פרטיות וגיבוי", icon: Settings, component: SettingsPage },
  advanced: { title: "כלים מתקדמים", subtitle: "גוף, סנכרון, מפה, לוגים וכלי מנוע", icon: Wrench, component: AdvancedPage },
};

const workspaces = [
  { id: "activity", title: "פעילות", icon: Activity, home: "home", pages: ["home", "review", "events"] },
  { id: "people", title: "אנשים", icon: Users, home: "people", pages: ["people", "faces"] },
  { id: "cameras", title: "מצלמות", icon: Camera, home: "live", pages: ["live", "intercom"] },
  { id: "management", title: "ניהול", icon: Settings, home: "management", pages: ["management", "health", "access1", "guests", "automations", "settings"] },
];
const expertPages = ["liveness", "learning", "calibration", "advanced"];
const managementItems = [
  ["live", "מצלמות", "blue"], ["access1", "בקרת כניסה", "orange"],
  ["people", "אנשים ותמונות ייחוס", "rose"], ["automations", "חיבורים ואוטומציות", "green"],
  ["guests", "אורחים", "violet"], ["health", "מצב המערכת", "gold"],
  ["events", "יומן אירועים", "neutral"], ["settings", "הגדרות", "orange"],
];
function ManagementMenu({ navigate }) {
  return <section className="workspace-directory"><header className="page-header"><div><h1>ניהול</h1></div></header>
    <nav className="workspace-directory-items" aria-label="נושאי ניהול">{managementItems.map(([id, title, tone]) => {
      const Icon = pages[id].icon;
      return <button key={id} onClick={() => navigate(id)}><span className={`workspace-category-icon tone-${tone}`} aria-hidden="true"><Icon /></span><strong>{title}</strong><ChevronLeft aria-hidden="true" /></button>;
    })}</nav>
  </section>;
}

function resolvePage() {
  const key = location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
  return pages[key] ? key : "home";
}
function savedTheme() {
  try { return localStorage.getItem("faceid-theme") === "light" ? "light" : "dark"; }
  catch { return "dark"; }
}

function Shell() {
  const [page, setPage] = useState(resolvePage);
  const [theme, setTheme] = useState(savedTheme);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [expert, setExpert] = useState(() => expertPages.includes(resolvePage()));
  const searchInput = useRef(null);
  const health = useResource("health", { poll: 15000 });
  const session = useResource("session");
  useEffect(() => {
    const onHash = () => { const next = resolvePage(); setPage(next); if (expertPages.includes(next)) setExpert(true); };
    const onKey = event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); setSearchOpen(value => !value);
      }
      if (event.key === "Escape") setSearchOpen(false);
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("hashchange", onHash); window.removeEventListener("keydown", onKey); };
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("faceid-theme", theme); } catch { /* The theme still applies when storage is blocked. */ }
  }, [theme]);
  useEffect(() => {
    if (!searchOpen) return undefined;
    const previous = document.activeElement;
    searchInput.current?.focus();
    return () => previous?.focus?.();
  }, [searchOpen]);
  const navigate = id => { location.hash = `/${id}`; setPage(id); if (expertPages.includes(id)) setExpert(true); setSearchOpen(false); setSearch(""); };
  const workspace = workspaces.find(item => item.pages.includes(page)) || workspaces[3];
  const showExpert = expert;
  const visiblePages = workspace.id === "management" ? [...workspace.pages, ...(showExpert ? expertPages : expertPages.includes(page) ? [page] : [])] : workspace.pages;
  const CurrentPage = pages[page].component;
  const reviewCount = health.data?.queue || 0;
  const systemOk = health.data?.status === "ok";
  const systemLabel = health.loading ? "בודק מערכת" : systemOk ? "המערכת תקינה" : "נדרשת בדיקה";
  const searchResults = useMemo(() => {
    const q = search.trim().toLowerCase();
    const rank = item => item.title === q ? 0 : item.title.startsWith(q) ? 1 : item.title.includes(q) ? 2 : 3;
    return Object.entries(pages)
      .filter(([, item]) => !q || `${item.title} ${item.subtitle}`.toLowerCase().includes(q))
      .sort(([, first], [, second]) => q ? rank(first) - rank(second) : 0);
  }, [search]);
  return <div className="workspace-app app-shell" dir="rtl">
    <a className="skip-content" href="#workspace-content" onClick={event => { event.preventDefault(); document.getElementById("workspace-content")?.focus(); }}>דלג לתוכן</a>
    <header className="workspace-header">
      <button className="workspace-brand" onClick={() => navigate("home")} aria-label="FaceID — חזרה למבט כללי">
        <svg className="workspace-brand-symbol" viewBox="0 0 78 48" aria-hidden="true"><path d="M36 23C24 3 5 5 5 24s19 22 31 1l7-12C54-4 73 5 73 24s-19 22-30 5L36 18" fill="none" stroke="currentColor" strokeWidth="7" strokeLinecap="round" /></svg><span className="workspace-brand-name">HOME<span>ii</span><small>FaceID</small></span>
      </button>
      <nav className="workspace-primary-nav" aria-label="אזורים ראשיים">
        {workspaces.map(item => { const Icon = item.icon; return <button key={item.id} className={workspace.id === item.id ? "active" : ""} aria-current={workspace.id === item.id ? "page" : undefined} onClick={() => navigate(item.home)}><Icon /><span>{item.title}</span></button>; })}
      </nav>
      <div className="workspace-tools">
        <button className="workspace-search-toggle" onClick={() => setSearchOpen(true)} aria-label="חיפוש מסך או פעולה"><Search /><span>חיפוש</span><kbd>⌘ K</kbd></button>
        <AlertCenter />
        <button className="icon-button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label={theme === "dark" ? "מעבר למצב בהיר" : "מעבר למצב כהה"}>{theme === "dark" ? <Sun /> : <Moon />}</button>
        <button className="workspace-user" onClick={() => navigate("settings")} title={session.data?.name || "Home Assistant"} aria-label="הגדרות ומשתמש"><span>{(session.data?.name || "HA").slice(0, 2)}</span></button>
      </div>
    </header>
    <div className="workspace-context">
      <nav className="workspace-subnav" aria-label={`מסכי ${workspace.title}`}>
        {visiblePages.map(id => <button key={id} className={page === id ? "active" : ""} aria-current={page === id ? "page" : undefined} onClick={() => navigate(id)}>{pages[id].title}{id === "review" && reviewCount > 0 && <span className="workspace-count">{reviewCount > 99 ? "99+" : reviewCount}</span>}</button>)}
        {workspace.id === "management" && <button className="workspace-expert-toggle" onClick={() => setExpert(!showExpert)} aria-expanded={showExpert}><Wrench />{showExpert ? "פחות כלים" : "כלים מתקדמים"}</button>}
      </nav>
      <button className={`workspace-health ${health.loading ? "pending" : systemOk ? "healthy" : "attention"}`} onClick={() => navigate("health")} title="תקינות המערכת"><span aria-hidden="true" />{systemLabel}</button>
    </div>
    <main className="workspace-content" id="workspace-content" tabIndex={-1}>
      <PageBoundary page={page}><CurrentPage navigate={navigate} /></PageBoundary>
    </main>
    <footer className="workspace-footer"><span>FaceID {VERSION}</span><span>{session.data?.role === "admin" ? "מנהל מערכת" : session.data?.role || "משתמש"} · {session.data?.name || "Home Assistant"}</span></footer>
    {searchOpen && <div className="workspace-search-backdrop" onMouseDown={() => setSearchOpen(false)}>
      <section className="workspace-search-dialog" role="dialog" aria-modal="true" aria-label="חיפוש מסך או פעולה" onMouseDown={event => event.stopPropagation()} onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setSearchOpen(false); return; }
        if (event.key !== "Tab") return;
        const controls = event.currentTarget.querySelectorAll('button:not([disabled]),input,[href],[tabindex="0"]');
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
        <div className="workspace-search-input"><Search /><input ref={searchInput} aria-label="חיפוש מסך או פעולה" placeholder="לאן לעבור?" value={search} onChange={event => setSearch(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && searchResults[0]) { event.preventDefault(); navigate(searchResults[0][0]); } }} /><button className="icon-button" aria-label="סגירת חיפוש" onClick={() => setSearchOpen(false)}><X /></button></div>
        <div className="workspace-search-results">{searchResults.map(([id, item]) => { const Icon = item.icon; return <button onClick={() => navigate(id)} key={id}><Icon /><span><b>{item.title}</b><small>{item.subtitle}</small></span><ChevronLeft /></button>; })}{!searchResults.length && <p role="status">לא נמצאו מסכים מתאימים</p>}</div>
        <div className="workspace-search-help">Enter לפתיחת התוצאה הראשונה · Esc לסגירה</div>
      </section>
    </div>}
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
