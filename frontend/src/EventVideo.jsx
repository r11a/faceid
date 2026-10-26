import React, { useEffect, useRef, useState } from "react";
import { api, assetUrl } from "./api.js";

export function EventVideo({ eventId, nextEventId, onEnded, onUnavailable, className = "" }) {
  return <Clip key={eventId} eventId={eventId} nextEventId={nextEventId} onEnded={onEnded} onUnavailable={onUnavailable} className={className} />;
}

function Clip({ eventId, nextEventId, onEnded, onUnavailable, className }) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState("loading");
  const [loaded, setLoaded] = useState(false);
  const [diagnostic, setDiagnostic] = useState(null);
  const unavailableAttempt = useRef(null);
  useEffect(() => {
    if (state !== "error") return undefined;
    let active = true;
    let timer;
    (async () => {
      try {
        const result = await api(`activity/${encodeURIComponent(eventId)}/clip/status?probe=true`);
        if (!active) return;
        setDiagnostic(result?.message || "לא התקבל פירוט שגיאה מ־Frigate.");
        // A fresh recording may not have ready segments yet. Never retry permission/TLS errors.
        if (result?.retryable && attempt < 3) {
          timer = globalThis.setTimeout(() => {
            setState("loading"); setLoaded(false); setDiagnostic(null); setAttempt(value => value + 1);
          }, 5000);
        }
      } catch (error) { if (active) setDiagnostic(error.message); }
    })();
    return () => { active = false; globalThis.clearTimeout(timer); };
  }, [state, attempt, eventId]);
  const src = assetUrl(`api/activity/${encodeURIComponent(eventId)}/clip`);
  return <div className={`event-video ${className}`}>
    <video key={attempt} controls autoPlay playsInline preload="auto" src={src}
      onLoadStart={() => setState("loading")} onWaiting={() => setState("buffering")}
      onLoadedData={() => { setLoaded(true); setState("ready"); }}
      onPlaying={() => setState("playing")} onCanPlay={() => setState("ready")}
      onError={() => {
        setState("error");
        if (unavailableAttempt.current !== attempt) {
          unavailableAttempt.current = attempt;
          onUnavailable?.();
        }
      }} onEnded={onEnded} />
    {(state === "loading" || state === "buffering") && <p role="status">{state === "loading" ? "טוען וידאו…" : "ממתין לנתוני וידאו…"}</p>}
    {state === "error" && <div role="alert"><p>{diagnostic || "בודק מדוע הקליפ לא נטען…"}</p><button className="button secondary" onClick={() => { setState("loading"); setLoaded(false); setDiagnostic(null); setAttempt(value => value + 1); }}>נסה שוב</button></div>}
    {loaded && nextEventId && nextEventId !== eventId && <video key={nextEventId} hidden aria-hidden="true" tabIndex={-1} preload="metadata" src={assetUrl(`api/activity/${encodeURIComponent(nextEventId)}/clip`)} />}
  </div>;
}
