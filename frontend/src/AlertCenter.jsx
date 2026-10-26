import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bell, X } from "lucide-react";
import { api } from "./api.js";
import { subscribeRealtime } from "./realtime.js";
import { useResource } from "./hooks.js";
import { alertReasons } from "./alerts.js";
import { Empty, ErrorState, Loading, Modal, dateTime } from "./ui.jsx";
import { EventModal } from "./pages/Daily.jsx";

export function AlertCenter() {
  const [open, setOpen] = useState(false);
  const [popup, setPopup] = useState(null);
  const [unread, setUnread] = useState(0);
  const [eventId, setEventId] = useState(null);
  const cursor = useRef(null);
  const history = useResource("alerts?recent=true", { enabled: open, realtime: true, poll: 5000 });
  const users = useResource("users", { enabled: Boolean(eventId) });
  useEffect(() => {
    const controller = new AbortController();
    let busy = false, again = false;
    const load = async () => {
      if (controller.signal.aborted) return;
      if (busy) { again = true; return; }
      busy = true;
      try {
        const data = await api(cursor.current === null ? "alerts" : `alerts?after=${cursor.current}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        const fresh = data.alerts || [];
        cursor.current = data.cursor;
        if (fresh.length) { setPopup(fresh[fresh.length - 1]); setUnread(value => Math.min(value + fresh.length, 999)); }
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
  }, []);
  const view = id => { setEventId(id); setPopup(null); setOpen(false); };
  return <>
    <button className="icon-button" onClick={() => { setOpen(true); setUnread(0); setPopup(null); }} title="התראות זיהוי" aria-label={`התראות זיהוי${unread ? ` · ${unread} חדשות` : ""}`}><Bell />{unread > 0 && <i />}</button>
    {createPortal(<div className="workspace-app workspace-portals" dir="rtl">
      {popup && <div className={`recognition-popup ${popup.watchlist === "black" ? "blacklist-outline" : ""}`} role="alert"><button className="icon-button" aria-label="סגירת התראה" onClick={() => setPopup(null)}><X /></button><b>{popup.person || "אדם לא מוכר"} · {popup.camera}</b><span>{popup.reasons.map(reason => alertReasons[reason] || reason).join(" · ")}</span><button className="button primary" onClick={() => view(popup.event_id)}>צפייה באירוע ובקליפ</button></div>}
      {open && <Modal title="התראות זיהוי" subtitle="מצלמות קריטיות, אנשים במעקב ורשימה שחורה" onClose={() => setOpen(false)}>{history.loading ? <Loading /> : history.error ? <ErrorState error={history.error} retry={history.reload} /> : history.data?.alerts?.length ? <div className="recognition-alert-list">{[...history.data.alerts].reverse().map(alert => <button className={`button secondary ${alert.watchlist === "black" ? "blacklist-outline" : ""}`} key={alert.id} onClick={() => view(alert.event_id)}><b>{alert.person || "אדם לא מוכר"} · {alert.camera}</b><span>{alert.reasons.map(reason => alertReasons[reason] || reason).join(" · ")}</span><small>{dateTime(alert.ts)}</small></button>)}</div> : <Empty title="אין התראות עדיין" />}</Modal>}
      {eventId && <EventModal eventId={eventId} people={users.data?.users || []} onClose={() => setEventId(null)} />}
    </div>, document.body)}
  </>;
}
