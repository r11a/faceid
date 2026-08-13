import React, { createContext, useContext, useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Info, LoaderCircle, X } from "lucide-react";

const ToastContext = createContext(() => {});

export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const notify = (message, kind = "success") => {
    const id = crypto.randomUUID();
    setItems((current) => [...current, { id, message, kind }]);
    window.setTimeout(() => setItems((current) => current.filter((item) => item.id !== id)), 4200);
  };
  return <ToastContext.Provider value={notify}>
    {children}
    <div className="toast-stack" aria-live="polite">
      {items.map((item) => <div className={`toast ${item.kind}`} key={item.id}>
        {item.kind === "error" ? <AlertCircle /> : item.kind === "info" ? <Info /> : <CheckCircle2 />}
        <span>{item.message}</span>
        <button onClick={() => setItems((current) => current.filter((row) => row.id !== item.id))}><X /></button>
      </div>)}
    </div>
  </ToastContext.Provider>;
}

export const useToast = () => useContext(ToastContext);

export function Panel({ children, className = "" }) {
  return <section className={`panel ${className}`}>{children}</section>;
}

export function PageHeader({ eyebrow, title, description, actions }) {
  return <header className="page-header">
    <div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1>{description && <p>{description}</p>}</div>
    {actions && <div className="page-actions">{actions}</div>}
  </header>;
}

export function Empty({ icon: Icon = Info, title, text, action }) {
  return <div className="empty-state"><span><Icon /></span><h3>{title}</h3><p>{text}</p>{action}</div>;
}

export function Loading({ text = "טוען נתונים…" }) {
  return <div className="loading"><LoaderCircle className="spin" /><span>{text}</span></div>;
}

export function ErrorState({ error, retry }) {
  return <div className="error-state"><AlertCircle /><div><b>לא הצלחנו לטעון את המסך</b><p>{error?.message || "שגיאה לא ידועה"}</p></div>{retry && <button className="button secondary" onClick={retry}>נסה שוב</button>}</div>;
}

export function Badge({ children, tone = "neutral" }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function Metric({ icon: Icon, label, value, note, tone = "green" }) {
  return <Panel className={`metric-card ${tone}`}>
    <span className="metric-icon">{Icon && <Icon />}</span><div><small>{label}</small><strong>{value ?? "—"}</strong>{note && <p>{note}</p>}</div>
  </Panel>;
}

export function Modal({ title, subtitle, children, onClose, wide = false }) {
  useEffect(() => {
    const close = (event) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={onClose}>
    <div className={`modal ${wide ? "wide" : ""}`} onMouseDown={(event) => event.stopPropagation()} role="dialog" aria-modal="true">
      <header><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" onClick={onClose} aria-label="סגירה"><X /></button></header>
      <div className="modal-body">{children}</div>
    </div>
  </div>;
}

export function Segmented({ value, onChange, options }) {
  return <div className="segmented">{options.map(([id, label, Icon]) => <button key={id} className={value === id ? "active" : ""} onClick={() => onChange(id)}>{Icon && <Icon />}{label}</button>)}</div>;
}

export const percent = (value) => value == null ? "—" : `${Math.round(Number(value) * 100)}%`;
export const dateTime = (value) => value ? new Intl.DateTimeFormat("he-IL", { dateStyle: "short", timeStyle: "short" }).format(new Date(Number(value) * 1000)) : "לא נראה עדיין";
export const relativeTime = (value) => {
  if (!value) return "לא נראה עדיין";
  const seconds = Math.max(0, Date.now() / 1000 - Number(value));
  if (seconds < 60) return "עכשיו";
  if (seconds < 3600) return `לפני ${Math.floor(seconds / 60)} דקות`;
  if (seconds < 86400) return `לפני ${Math.floor(seconds / 3600)} שעות`;
  return `לפני ${Math.floor(seconds / 86400)} ימים`;
};

export const decision = {
  recognized: ["זוהה", "success"], ambiguous: ["דורש אישור", "warning"],
  unknown: ["לא מוכר", "warning"], no_face: ["לא נמצאו פנים", "neutral"],
  ignored: ["הושתק", "neutral"], spoof_suspected: ["נחסם: חשד לזיוף", "danger"],
  liveness_unconfirmed: ["נחסם: חיוּת לא אומתה", "danger"], processing: ["בעיבוד", "info"],
};
