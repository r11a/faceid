import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bell, X } from "lucide-react";
import { api, assetUrl } from "./api.js";
import { subscribeRealtime } from "./realtime.js";
import { useResource } from "./hooks.js";
import { alertReasons, recognitionImagePath } from "./alerts.js";
import { Empty, ErrorState, Loading, Modal, dateTime } from "./ui.jsx";
import { EventModal } from "./pages/Daily.jsx";

export function AlertCenter() {
  const [open, setOpen] = useState(false);
  const [popups, setPopups] = useState([]);
  const popup = popups[0];
  const popupImage = recognitionImagePath(popup?.image_url);
  const [allRecognitions, setAllRecognitions] = useState(() => {
    try { return localStorage.getItem("faceid.recognitionPopups") === "true"; } catch { return false; }
  });
  const [storageError, setStorageError] = useState(false);
  const dismiss = () => setPopups(items => items.slice(1));
  const toggleRecognitions = enabled => {
    setAllRecognitions(enabled);
    setPopups(items => items.filter(item => item.reasons?.length));
    try { localStorage.setItem("faceid.recognitionPopups", String(enabled)); setStorageError(false); }
    catch { setStorageError(true); }
  };
  const [unread, setUnread] = useState(0);
  const [eventId, setEventId] = useState(null);
  const cursor = useRef(null);
  const history = useResource("alerts?recent=true", { enabled: open, realtime: true, poll: 5000 });
  const users = useResource("users", { enabled: Boolean(eventId) });
  useEffect(() => {
    const controller = new AbortController();
    cursor.current = null;
    let busy = false, again = false;
    const load = async () => {
      if (controller.signal.aborted) return;
      if (busy) { again = true; return; }
      busy = true;
      try {
        const params = new URLSearchParams();
        if (cursor.current !== null) params.set("after", cursor.current);
        if (allRecognitions) params.set("include_recognitions", "true");
        const data = await api(`alerts${params.size ? `?${params}` : ""}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        const fresh = data.alerts || [];
        cursor.current = data.cursor;
        if (fresh.length) {
          setPopups(items => {
            const seen = new Set(items.map(item => item.event_id));
            const additions = fresh.filter(item => !seen.has(item.event_id) && seen.add(item.event_id));
            // Keep the active popup and the newest waiting encounters during bursts.
            return items.length ? [items[0], ...[...items.slice(1), ...additions].slice(-19)] : additions.slice(-20);
          });
          setUnread(value => Math.min(value + fresh.filter(item => item.reasons?.length).length, 999));
        }
        again ||= data.has_more;
      } catch { /* Polling retries after a disconnect; existing alerts stay available. */ }
      finally {
        busy = false;
        if (again && !controller.signal.aborted) { again = false; void load(); }
      }
    };
    const unsubscribe = subscribeRealtime(load, { immediate: true });
    void load();
    const timer = setInterval(load, 5000);
    return () => { controller.abort(); unsubscribe(); clearInterval(timer); };
  }, [allRecognitions]);
  useEffect(() => {
    if (!popup || !allRecognitions) return;
    const timer = setTimeout(dismiss, 5000);
    return () => clearTimeout(timer);
  }, [popup, allRecognitions]);
  const view = id => { setEventId(id); setPopups([]); setOpen(false); };
  return <>
    <button className="icon-button" onClick={() => { setOpen(true); setUnread(0); setPopups([]); }} title="התראות זיהוי" aria-label={`התראות זיהוי${unread ? ` · ${unread} חדשות` : ""}`}><Bell />{unread > 0 && <i />}</button>
    {createPortal(<div className="workspace-app workspace-portals" dir="rtl">
      {popup && <div className={`recognition-popup ${popup.watchlist === "black" ? "blacklist-outline" : ""}`} role="alert"><button className="icon-button" aria-label="סגירת התראה" onClick={dismiss}><X /></button>{popupImage && <img key={popup.event_id} className="recognition-popup-photo" src={assetUrl(popupImage)} alt={`פנים של ${popup.person || "אדם לא מוכר"}`} onError={event => { event.currentTarget.hidden = true; }} />}<b>{popup.person || "אדם לא מוכר"} · {popup.camera}</b><span>{popup.reasons.length ? popup.reasons.map(reason => alertReasons[reason] || reason).join(" · ") : "זיהוי מאושר"}</span>{Number.isFinite(popup.notified_ts) && <small>גיל ההתראה: {Math.max(0, (Date.now() / 1000 - popup.notified_ts)).toFixed(1)} שניות · אינו זמן מצילום לזיהוי</small>}{popups.length > 1 && <small>{popups.length - 1} התראות ממתינות</small>}<button className="button primary" onClick={() => view(popup.event_id)}>צפייה באירוע ובקליפ</button></div>}
      {open && <Modal title="התראות זיהוי" subtitle="מצלמות קריטיות, אנשים במעקב ורשימה שחורה" onClose={() => setOpen(false)}><label><input type="checkbox" checked={allRecognitions} onChange={event => toggleRecognitions(event.target.checked)} /> הצג חלון ל־5 שניות בכל זיהוי מאושר (בדיקת מצלמות)</label><small>פועל כשהממשק פתוח. בתור נשמרות עד 20 התראות; בעומס נשמרות האחרונות.</small>{storageError && <small role="status">ההעדפה פעילה כעת אך לא נשמרה בדפדפן.</small>}{history.loading ? <Loading /> : history.error ? <ErrorState error={history.error} retry={history.reload} /> : history.data?.alerts?.length ? <div className="recognition-alert-list">{[...history.data.alerts].reverse().map(alert => <button className={`button secondary ${alert.watchlist === "black" ? "blacklist-outline" : ""}`} key={alert.id} onClick={() => view(alert.event_id)}><b>{alert.person || "אדם לא מוכר"} · {alert.camera}</b><span>{alert.reasons.map(reason => alertReasons[reason] || reason).join(" · ")}</span><small>{dateTime(alert.ts)}</small></button>)}</div> : <Empty title="אין התראות עדיין" />}</Modal>}
      {eventId && <EventModal eventId={eventId} people={users.data?.users || []} onClose={() => setEventId(null)} />}
    </div>, document.body)}
  </>;
}
