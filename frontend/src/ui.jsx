import React, { createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Info, LoaderCircle, MoreHorizontal, X } from "lucide-react";

const ToastContext = createContext(() => {});
let toastSequence = 0;
let modalScrollLocks = 0;
let previousBodyOverflow = "";

export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const notify = (message, kind = "success") => {
    const id = ++toastSequence; // UI-only key; works on HTTP Home Assistant ingress too.
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

function actionChildren(children) {
  return React.Children.toArray(children).flatMap(child => child?.type === React.Fragment ? actionChildren(child.props.children) : [child]);
}

export function ActionsMenu({ children, label = "אפשרויות נוספות" }) {
  const [open, setOpen] = useState(false);
  const root = useRef(null), trigger = useRef(null), popup = useRef(null);
  useLayoutEffect(() => {
    const element = popup.current;
    if (!open || !element) return;
    const bounds = element.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth;
    const shift = bounds.left < 12 ? 12 - bounds.left : bounds.right > viewportWidth - 12 ? viewportWidth - 12 - bounds.right : 0;
    element.style.transform = `translateX(${shift}px)`;
    if (bounds.bottom > window.innerHeight - 12 && bounds.top > window.innerHeight - bounds.bottom) {
      element.style.top = "auto"; element.style.bottom = "calc(100% + 6px)";
    }
    element.querySelector('button:not([disabled]),a')?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    const outside = event => { if (!root.current?.contains(event.target)) setOpen(false); };
    const escape = event => { if (event.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    window.addEventListener("pointerdown", outside);
    window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("pointerdown", outside); window.removeEventListener("keydown", escape); };
  }, [open]);
  return <div className="actions-menu" ref={root}>
    <button ref={trigger} type="button" className="icon-button" aria-label={label} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(value => !value)} onKeyDown={event => {
      if (event.key === "ArrowDown") { event.preventDefault(); setOpen(true); }
    }}><MoreHorizontal /></button>
    {open && <div className="actions-menu-popover" role="menu" aria-label={label} ref={popup} onKeyDown={event => {
      const items = [...event.currentTarget.querySelectorAll('button:not([disabled]),a[href]')];
      const index = items.indexOf(document.activeElement);
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
      if (event.key === "Tab") { trigger.current?.focus(); setOpen(false); }
    }} onClick={event => {
      if (event.target.closest('button:not([disabled]),a[href]')) { setOpen(false); trigger.current?.focus(); }
    }}>{actionChildren(children).map(child => React.isValidElement(child) ? React.cloneElement(child, { role: "menuitem" }) : child)}</div>}
  </div>;
}

export function ActionGroup({ children }) {
  const items = actionChildren(children);
  if (items.length < 2) return children;
  const primary = items.find(item => item.props?.className?.split(" ").includes("primary")) || items[0];
  return <>{primary}<ActionsMenu>{items.filter(item => item !== primary)}</ActionsMenu></>;
}

export function PageHeader({ eyebrow, title, description, actions }) {
  return <header className="page-header">
    <div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1>{description && <p className="workspace-info-line">{description}</p>}</div>
    {actions && <div className="page-actions"><ActionGroup>{actions}</ActionGroup></div>}
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
    if (typeof document === "undefined") return undefined;
    if (modalScrollLocks++ === 0) {
      previousBodyOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
    return () => {
      if (--modalScrollLocks === 0) document.body.style.overflow = previousBodyOverflow;
    };
  }, []);
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
